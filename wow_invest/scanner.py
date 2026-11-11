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
        return s is not None and s["static_done"] == 1 and 0 < s["purchase_price"] <= r.min_ask / c.VENDOR_CHECK_RATIO

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
        if any(h not in times for h in HOUSES):
            return "error"
        if all(times[h][0] == db.last_captured(conn, h) for h in HOUSES):
            return "no-change"
        rows = {h: client.all_items(h) for h in HOUSES}
        deals = {h: client.deals(h) for h in HOUSES}
    except ApiError:
        return "error"
    stale = any(datetime.fromisoformat(times[h][1]) < now - timedelta(hours=c.STALE_HOURS) for h in HOUSES)
    cut = {h: deals[h][1] for h in HOUSES}
    scan_id = {h: db.save_scan(conn, h, *times[h], cut[h], rows[h]) for h in HOUSES}
    db.set_setting(conn, "stale", "1" if stale else "0")
    if stale:
        tracker.judge(conn, now)
        return "stale"

    cache, bad, errors = {}, [], 0

    def detail(house, item_id):
        """Cached detail, or None on an ApiError. A static id that failed is marked bad only once a
        later request succeeds, so a short outage never blacklists items."""
        nonlocal errors
        key = (house, item_id)
        if key not in cache:
            try:
                cache[key] = client.item_detail(house, item_id, cut[house])
            except ApiError:
                cache[key], errors = None, errors + 1
                if errors >= c.MAX_ERRORS_IN_ROW:
                    raise _Outage
                return None
            errors = 0
            for i in bad:
                db.mark_static_bad(conn, i)
            bad.clear()
        return cache[key]

    try:
        for i in db.missing_static(conn, c.STATIC_PER_CYCLE):
            d = detail("alliance", i)
            if d is None:
                bad.append(i)
            else:
                db.save_static(conn, d)

        crowd = estimate_crowd(rows["alliance"])
        db.set_scan_crowd(conn, scan_id["alliance"], crowd)
        db.set_setting(conn, "crowd", f"{crowd[0]}-{crowd[1]}")

        held, confirmed, picks = db.held_units(conn), db.confirmed_vendors(conn), []
        for kind, house, r, buy_house in _choose(conn, rows, deals["alliance"][0], crowd, now):
            sell, h = detail(house, r.item_id), held.get(r.item_id, 0)
            if sell is None:
                continue
            if kind == "cross":
                buy = detail(buy_house, r.item_id)
                if buy is not None:
                    picks.append(cross_flip(r, buy, sell, h, now))
            elif kind == "hold":
                picks.append(hold(r, sell, crowd, h, now))
            else:  # vendor and flip candidates sell on Alliance; a flip also tries a vendor buy
                picks.append(vendor_flip(r, sell, h, confirmed, now))
                if kind == "flip":
                    picks.append(quick_flip(r, sell, h, now))
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
    conn, client = db.connect(db_path), client_factory()
    while not stop.is_set():
        try:
            log.info("scan cycle: %s", run_cycle(client, conn, datetime.now(UTC)))
        except Exception:
            log.exception("scan cycle failed")
        stop.wait(c.POLL_MINUTES * 60)
