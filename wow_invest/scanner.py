"""One scan cycle (lists, static backfill, candidate details, picks) and the loop that repeats it."""
import logging
from datetime import UTC, datetime, timedelta

from . import config as c
from . import db, planner, tracker
from .api import ApiError
from .crowd import estimate_crowd, item_band, overlaps
from .picks import cross_flip, hold, quick_flip, vendor_flip
from .tables import VENDOR_ITEMS

HOUSES = ("alliance", "blackwater")
log = logging.getLogger(__name__)


class _Outage(Exception):
    """MAX_ERRORS_IN_ROW requests failed in a row."""


def _choose(conn, rows, deal_ids, crowd, now):
    """Candidates as (kind, sell_house, sell_row, buy_house), each kind up to its quota, one kind per item."""
    a = {r.item_id: r for r in rows["alliance"] if r.min_ask is not None}
    b = {r.item_id: r for r in rows["blackwater"] if r.min_ask is not None}
    prev = db.rows_at(conn, "alliance", 24, now)
    vendors = set(VENDOR_ITEMS) | db.confirmed_vendors(conn)

    def cheap_static(r):
        s = db.static(conn, r.item_id)
        return (s is not None and s["static_done"] == 1 and s["purchase_price"] > 0
                and s["purchase_price"] / (s["purchase_quantity"] or 1) <= r.min_ask / c.VENDOR_CHECK_RATIO)

    def flooded(r):
        band, old = item_band(r.item_id, r.required_level), prev.get(r.item_id, (None, 0, 0))[1]
        return (band is not None and overlaps(band, crowd) and band[1] < c.HOLD_ENDGAME_LEVEL
                and old > 0 and r.units >= (1 + c.FLOOD_UNITS_RISE) * old)

    def by_sales(xs):
        return sorted(xs, key=lambda x: -x[1].sold_24h)

    lists = {  # dedupe order: cross, vendor, hold, flip
        "cross": by_sales([("alliance", r, "blackwater") for i, r in a.items()
                           if i in b and b[i].min_ask <= c.BUY_BELOW * r.min_ask]
                          + [("blackwater", r, "alliance") for i, r in b.items()
                             if i in a and a[i].min_ask <= c.BUY_BELOW * r.min_ask]),
        "vendor": by_sales([("alliance", r, None) for i, r in a.items() if i in vendors])
        + by_sales([("alliance", r, None) for i, r in a.items() if i not in vendors and cheap_static(r)]),
        "hold": by_sales([("alliance", r, None) for r in a.values() if flooded(r)]),
        "flip": by_sales([("alliance", a[i], None) for i in deal_ids if i in a]),
    }
    taken, out = set(), []
    for kind, xs in lists.items():
        # pre-screen on the house the item sells on
        xs = [x for x in xs if x[1].item_id not in taken and 0.5 * x[1].sold_24h >= c.MIN_SALES_PER_DAY]
        xs = xs[:c.DETAIL_QUOTAS[kind]]
        taken |= {x[1].item_id for x in xs}
        out += [(kind, *x) for x in xs]
    return out


def run_cycle(client, conn, now) -> str:
    try:
        times = client.scan_times()
        # a list, not a lazy any(), so both houses are checked: a missing house (KeyError) or a missing,
        # malformed or naive auctions_at costs this one request, not both lists
        stale = any([datetime.fromisoformat(times[h][1]) < now - timedelta(hours=c.STALE_HOURS) for h in HOUSES])
    except (ApiError, KeyError, TypeError, ValueError):
        return "error"
    if all(times[h][0] == db.last_captured(conn, h) for h in HOUSES):
        return "no-change"
    try:
        rows = {h: client.all_items(h) for h in HOUSES}
        deals = {h: client.deals(h) for h in HOUSES}
    except ApiError:
        return "error"
    cut = {h: deals[h][1] for h in HOUSES}
    scan_id = {h: db.save_scan(conn, h, *times[h], cut[h], rows[h]) for h in HOUSES}
    db.set_setting(conn, "stale", "1" if stale else "0")
    if stale:
        tracker.judge(conn, now)
        return "stale"

    cache, errors = {}, 0

    def detail(house, item_id, static=False):
        """Cached detail, or None on an ApiError. Only transient errors count toward the outage
        streak; a permanent one on a static id marks it bad."""
        nonlocal errors
        key = (house, item_id)
        if key not in cache:
            cache[key] = None
            try:
                cache[key], errors = client.item_detail(house, item_id, cut[house]), 0
            except ApiError as e:
                if e.transient:
                    errors += 1
                    if errors >= c.MAX_ERRORS_IN_ROW:
                        raise _Outage from e
                elif static:
                    db.mark_static_bad(conn, item_id)
        return cache[key]

    try:
        for i in db.missing_static(conn, c.STATIC_PER_CYCLE):
            d = detail("alliance", i, static=True)
            if d is not None:
                db.save_static(conn, d)

        crowd = estimate_crowd(rows["alliance"])
        db.set_scan_crowd(conn, scan_id["alliance"], crowd)
        db.set_setting(conn, "crowd", f"{crowd[0]}-{crowd[1]}")

        held, confirmed, picks = db.held_units(conn), db.confirmed_vendors(conn), []
        for kind, house, r, buy_house in _choose(conn, rows, deals["alliance"][0], crowd, now):
            sell, h = detail(house, r.item_id), held.get(r.item_id, 0)
            # only a cross buys on another house; the rest buy where they sell
            buy = detail(buy_house, r.item_id) if kind == "cross" and sell is not None else sell
            if sell is None or buy is None:
                continue
            try:
                new = [cross_flip(r, buy, sell, h, now)] if kind == "cross" else []
                if house == "alliance":  # every builder that sells on Alliance: never hide a buy
                    new += [quick_flip(r, sell, h, now), vendor_flip(r, sell, h, confirmed, now),
                            hold(r, sell, crowd, h, now)]
            except Exception:
                log.exception("pick builders failed for item %s", r.item_id)
                continue
            picks += new
    except _Outage:
        return "error"

    picks = sorted((p for p in picks if p), key=lambda p: -p.score)
    db.save_candidates(conn, picks, times["alliance"][0])
    shown = planner.plan(picks, gold=0, holdings=[], proven=False)
    db.record_picks(conn, shown.ranked + shown.paper, now)
    tracker.judge(conn, now)
    db.prune(conn, now)
    return "done"


def loop(client_factory, db_path, stop):
    conn, client, wait = None, client_factory(), c.POLL_MINUTES
    while not stop.is_set():
        try:
            if conn is None:
                conn = db.connect(db_path)
            result = run_cycle(client, conn, datetime.now(UTC))
            log.info("scan cycle: %s", result)
        except Exception:
            log.exception("scan cycle failed")
            result = "error"
        # back off while things keep failing, so a down site is not polled every few minutes
        wait = min(2 * wait, 60) if result == "error" else c.POLL_MINUTES
        stop.wait(wait * 60)
