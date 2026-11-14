"""One scan cycle (lists, static backfill, candidate details, picks) and the loop that repeats it."""
import json
import logging
import math
import threading
import time
from datetime import UTC, datetime, timedelta

from . import config as c
from . import db, planner, tracker
from .api import ApiError, Client, ShapeError
from .crowd import estimate_crowd, item_band, overlaps
from .picks import buyout, craft_flip, cross_flip, hold, quick_flip, rise, trend, vendor_flip
from .tables import CRAFT_SKIP, RECIPES, VENDOR_ITEMS, vendor_unit

HOUSES = c.SCAN_HOUSES
log = logging.getLogger(__name__)
busy = threading.Lock()  # held while a scan cycle runs
started = 0.0  # time.time() when the running (or last) cycle began
notify = None  # set by the desktop app: called with a line of text when a strong new pick appears


# vendor goods and recipe reagents: their vendor price decides vendor and crafting picks
_PRICED_FIRST = {i for i, stock in VENDOR_ITEMS.items() if stock is None} | {i for r in RECIPES for i, _ in r[4]}


class _Outage(Exception):
    """MAX_ERRORS_IN_ROW requests failed in a row."""


def _choose(conn, rows, deal_ids, crowd, now):
    """Candidates as (kind, sell_house, sell_row, buy_house), each kind up to its quota, one kind per item."""
    a = {r.item_id: r for r in rows["alliance"] if r.min_ask is not None}
    b = {r.item_id: r for r in rows.get("blackwater", []) if r.min_ask is not None}
    prev = db.rows_at(conn, "alliance", 24, now)
    usual = db.recent_medians(conn, "alliance", now)
    # cheapest listing well under the item's own price of the last day: usually an underpriced lot. The
    # site's deals list misses most of these (it compares with a week-old price), so it only tops this up.
    drops = sorted((i for i, r in a.items() if i in usual and r.min_ask <= c.BUY_BELOW * usual[i]),
                   key=lambda i: -(usual[i] - a[i].min_ask) * a[i].sold_24h)

    def climbing(r):
        ask, units, _ = prev.get(r.item_id, (None, 0, 0))
        return bool(ask) and r.min_ask >= (1 + c.TREND_MIN) * ask and units > 0 and r.units < (1 + c.FLOOD_UNITS_RISE) * units

    rep = _rep(conn)

    def vendor_margin(r):
        """Sales-weighted gap between the ask and the vendor price; None while the price is unknown."""
        s = db.static(conn, r.item_id)
        if s is None or s["static_done"] != 1:
            return None
        return (r.min_ask - vendor_unit(r.item_id, s["purchase_price"], s["purchase_quantity"], rep)) * r.sold_24h

    def vendor_list():
        sold = [(r, vendor_margin(r)) for i, r in a.items() if VENDOR_ITEMS.get(i, 0) is None]  # unlimited stock only
        known = sorted(((r, m) for r, m in sold if m is not None and m > 0), key=lambda x: -x[1])
        return ([("alliance", r, None) for r, _ in known]
                + by_sales([("alliance", r, None) for r, m in sold if m is None]))

    def flooded(r):
        band, old = item_band(r.item_id, r.required_level), prev.get(r.item_id, (None, 0, 0))[1]
        return (band is not None and overlaps(band, crowd) and band[1] < c.HOLD_ENDGAME_LEVEL
                and old > 0 and r.units >= (1 + c.FLOOD_UNITS_RISE) * old)

    def by_sales(xs):
        return sorted(xs, key=lambda x: -x[1].sold_24h)

    week = db.recent_medians(conn, "alliance", now, hours=24 * 7)
    cheap = sorted((i for i, r in a.items() if i in week and r.min_ask <= c.BUY_BELOW * week[i]),
                   key=lambda i: -(week[i] - a[i].min_ask) * a[i].sold_24h)

    lists = {  # dedupe order: cross, vendor, hold, flip, rise, trend, buyout, slow
        "cross": by_sales([("alliance", r, "blackwater") for i, r in a.items()
                           if i in b and b[i].min_ask <= c.BUY_BELOW * r.min_ask]
                          + [("blackwater", r, "alliance") for i, r in b.items()
                             if i in a and a[i].min_ask <= c.BUY_BELOW * r.min_ask]),
        "vendor": vendor_list(),
        "hold": by_sales([("alliance", r, None) for r in a.values() if flooded(r)]),
        "flip": [("alliance", a[i], None) for i in drops]
        + by_sales([("alliance", a[i], None) for i in deal_ids if i in a and i not in drops]),
        "rise": by_sales([("alliance", r, None) for r in a.values() if _behind(r, crowd)]),
        "trend": by_sales([("alliance", r, None) for r in a.values() if climbing(r)]),
        # cheapest listing well under the item's price of the last week: a shelf worth buying out
        "buyout": [("alliance", a[i], None) for i in cheap],
        # slow sellers (gear, recipes, dear items) fail the sales floor below: take the ones with the most
        # gold between their cheapest listing and their own price
        "slow": sorted([("alliance", a[i], None) for i in drops if a[i].sold_24h >= 2],
                       key=lambda x: -(usual[x[1].item_id] - x[1].min_ask)),
    }
    taken, out = set(), []
    for kind, xs in lists.items():
        # pre-screen on the house the item sells on
        xs = [x for x in xs if x[1].item_id not in taken
              and (kind == "slow" or 0.5 * x[1].sold_24h >= c.MIN_SALES_PER_DAY)]
        xs = xs[:c.DETAIL_QUOTAS[kind]]
        taken |= {x[1].item_id for x in xs}
        out += [(kind, *x) for x in xs]
    return out


def _behind(r, crowd) -> bool:
    """The crowd has levelled past this item: the middle of its level band is under the crowd's low end."""
    band = item_band(r.item_id, r.required_level)
    return band is not None and (band[0] + band[1]) / 2 < crowd[0]


def _rep(conn) -> dict:
    """The player's city standings, e.g. {"S": "Revered"}: they set the vendor discounts."""
    return json.loads(db.get_setting(conn, "rep", c.REP_DEFAULT))


def _vendor_unit(conn, item_id) -> int | None:
    """Vendor price of one unit, for items with unlimited vendor stock and a known price."""
    s = db.static(conn, item_id)
    if VENDOR_ITEMS.get(item_id, 0) is not None or s is None or s["static_done"] != 1 or not s["purchase_price"]:
        return None
    return vendor_unit(item_id, s["purchase_price"], s["purchase_quantity"], _rep(conn))


def _crafts(conn, a, cut, skills, chosen):
    """The most promising recipes on list prices, one per product: [(recipe, product row)].
    `skills` is {profession: skill} and `chosen` the professions to look at; empty means no limit."""
    best = {}
    for recipe in RECIPES:
        item, prof, skill, made, mats = recipe
        r = a.get(item)
        if (r is None or item in CRAFT_SKIP or 0.5 * r.sold_24h < c.MIN_SALES_PER_DAY
                or (chosen and prof not in chosen) or (prof in skills and skills[prof] < skill)
                or (prof == "Alchemy" and r.class_name != "Consumable")):  # transmutes are on a cooldown
            continue
        prices = [_vendor_unit(conn, i) or (a[i].min_ask if i in a else None) for i, _ in mats]
        if None in prices:
            continue
        margin = r.min_ask * (1 - cut) - sum(p * n for p, (_, n) in zip(prices, mats)) / made
        worth = margin * min(0.5 * r.sold_24h, 200)
        if margin > 0 and worth > best.get(item, (0,))[0]:
            best[item] = (worth, recipe, r)
    return [(recipe, r) for _, recipe, r in sorted(best.values(), key=lambda x: -x[0])[:c.CRAFT_QUOTA]]


def run_cycle(client, conn, now, force=False) -> str:
    try:
        times = client.scan_times()
        # a list, not a lazy any(), so both houses are checked: a missing house (KeyError) or a missing,
        # malformed or naive auctions_at costs this one request, not both lists
        stale = any([datetime.fromisoformat(times[h][1]) < now - timedelta(hours=c.STALE_HOURS) for h in HOUSES])
    except (ApiError, KeyError, TypeError, ValueError) as e:
        log.warning("scan failed reading the scan times: %r", e)
        return "error"
    if not force and all(times[h][0] == db.last_captured(conn, h) for h in HOUSES):
        return "no-change"
    try:
        rows = {h: client.all_items(h) for h in HOUSES}
        deals = {h: client.deals(h) for h in HOUSES}
    except ApiError as e:
        log.warning("scan failed reading the item lists: %s", e)
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
                if isinstance(e, ShapeError):  # every other item would fail the same way
                    raise _Outage from e
                if e.transient:
                    errors += 1
                    if errors >= c.MAX_ERRORS_IN_ROW:
                        raise _Outage from e
                elif static:
                    db.mark_static_bad(conn, item_id)
        return cache[key]

    try:
        for i in db.missing_static(conn, c.STATIC_PER_CYCLE, _PRICED_FIRST):
            d = detail("alliance", i, static=True)
            if d is not None:
                db.save_static(conn, d)

        crowd = estimate_crowd(rows["alliance"])
        db.set_scan_crowd(conn, scan_id["alliance"], crowd)
        db.set_setting(conn, "crowd", f"{crowd[0]}-{crowd[1]}")

        drift = db.crowd_drift(conn, now)
        drift = min(c.CROWD_DRIFT_MAX, max(0.0, c.CROWD_DRIFT_PRIOR if drift is None else drift))
        db.set_setting(conn, "drift", f"{drift:.1f}")

        held, picks = db.held_units(conn), []
        for kind, house, r, buy_house in _choose(conn, rows, deals["alliance"][0], crowd, now):
            sell, h = detail(house, r.item_id), held.get(r.item_id, 0)
            # only a cross buys on another house; the rest buy where they sell
            buy = detail(buy_house, r.item_id) if kind == "cross" and sell is not None else sell
            if sell is None or buy is None:
                continue
            try:
                new = [cross_flip(r, buy, sell, h, now)] if kind == "cross" else []
                if house == "alliance":  # every builder that sells on Alliance: never hide a buy
                    new += [quick_flip(r, sell, h, now), vendor_flip(r, sell, h, now, _rep(conn)),
                            hold(r, sell, crowd, h, now), rise(r, sell, _behind(r, crowd), h, now),
                            trend(r, sell, h, now), buyout(r, sell, h, now)]
            except Exception:
                log.exception("pick builders failed for item %s", r.item_id)
                continue
            picks += new

        a = {r.item_id: r for r in rows["alliance"] if r.min_ask is not None}
        skills = json.loads(db.get_setting(conn, "skills", "{}"))
        chosen = set(json.loads(db.get_setting(conn, "professions", "[]")))
        for recipe, r in _crafts(conn, a, cut["alliance"], skills, chosen):
            sell, mats = detail("alliance", r.item_id), []
            for i, n in recipe[4]:
                unit = _vendor_unit(conn, i)
                d = None if unit else detail("alliance", i)
                if sell is None or (unit is None and d is None):
                    break
                name = (a[i].name if i in a else None) or (db.static(conn, i) or {"name": None})["name"] or f"Item {i}"
                mats.append((name, n, [(unit, 10**9)] if unit else d.ladder, bool(unit)))
            else:
                try:
                    picks.append(craft_flip(r, sell, recipe, mats, held.get(r.item_id, 0), now))
                except Exception:
                    log.exception("craft builder failed for item %s", r.item_id)
    except _Outage as e:
        log.warning("scan stopped early: %s", e.__cause__)
        return "error"

    picks = sorted((p for p in picks if p), key=lambda p: -p.score)
    known = {(p.kind, p.item_id) for p in db.load_candidates(conn)[0]}
    fresh = [p for p in picks if p.kind != "rise" and p.profit >= c.NOTIFY_PROFIT and (p.kind, p.item_id) not in known]
    db.save_candidates(conn, picks, times["alliance"][0])
    if notify and fresh and known:  # not on the very first scan, when everything is new
        best = max(fresh, key=lambda p: p.profit)
        more = f" and {len(fresh) - 1} more" if len(fresh) > 1 else ""
        notify(f"New pick: {best.name}, about +{best.profit // 10000}g {best.profit // 100 % 100}s{more}")
    shown = planner.plan(picks, gold=0, holdings=[], proven=False)
    db.record_picks(conn, shown.ranked + shown.paper, now)
    tracker.judge(conn, now)
    db.prune(conn, now)
    return "done"


def _timed(conn, cycle):
    """Run a cycle, remembering how long a full one takes so the page can pace its loading bar."""
    global started
    started = time.time()
    result = cycle()
    if result == "done":
        db.set_setting(conn, "scan_secs", str(max(1, round(time.time() - started))))
    return result


def loop(client_factory, db_path, stop):
    conn, client, wait, result = None, client_factory(), c.POLL_MINUTES, None
    while not stop.is_set():
        try:
            if conn is None:
                conn = db.connect(db_path)
            # a cycle that failed after saving its scan would otherwise read as "no-change" until the next scan
            force = result == "error"
            with busy:
                result = _timed(conn, lambda: run_cycle(client, conn, datetime.now(UTC), force=force))
            log.info("scan cycle: %s", result)
        except Exception:
            log.exception("scan cycle failed")
            result = "error"
        # back off while things keep failing, so a down site is not polled every few minutes
        wait = min(2 * wait, 60) if result == "error" else c.POLL_MINUTES
        stop.wait(wait * 60)


def refresh(db_path, client_factory=Client):
    """One forced cycle now, rebuilding the picks even if the site has no newer scan. Skipped while
    a cycle is already running."""
    if not busy.acquire(blocking=False):
        return
    try:
        conn = db.connect(db_path)
        try:
            client = client_factory()
            log.info("refresh: %s", _timed(conn, lambda: run_cycle(client, conn, datetime.now(UTC), force=True)))
        finally:
            conn.close()
    except Exception:
        log.exception("refresh failed")
    finally:
        busy.release()
