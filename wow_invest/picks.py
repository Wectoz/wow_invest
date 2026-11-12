"""Candidate builders for the four kinds of pick. When unsure, they return None (no pick)."""
import math
from datetime import timedelta

from . import config as c
from .crowd import item_band, overlaps
from .models import Pick
from .pricing import (change, days_to_sell, deposit, going_price, history_hours, ladder_buy,
                      listed_units, max_units, net_proceeds, sales_per_day, sales_per_day_window,
                      score, sureness, undercut, window_median)
from .tables import GATHER_BANDS, VENDOR_ITEMS, vendor_deal, vendor_unit


def _money(cu):
    parts = ((cu // 10000, "g"), (cu // 100 % 100, "s"), (cu % 100, "c"))
    return " ".join(f"{v}{u}" for v, u in parts if v) or "0c"


def _sales_hold(s, now):
    return sales_per_day_window(s, now, 0, 24) >= 0.9 * sales_per_day_window(s, now, 24, 48)


def _trend_ok(s, now):
    """A falling price is only a buy while listed units shrink and sales hold."""
    trend, units = change(s, now, "min_ask"), change(s, now, "units")
    if trend is None or units is None:
        return False
    return trend >= c.FALLING or (units < 0 and _sales_hold(s, now))


def _left(ladder, qty):
    """The ladder after we bought its cheapest `qty` units."""
    left = []
    for price, units in ladder:
        take = min(units, qty)
        qty -= take
        if units > take:
            left.append((price, units - take))
    return left


def _build(kind, row, sell, held, now, buy_at, why, going=0, ladder=None, limit=0, unit_cost=0, target=None,
           slow=False):
    """Shared middle: sales, cap, buy, proceeds, days, score and sure, all on the sell house.
    `ladder=None` means a vendor buy at `unit_cost`; `target=None` means list under the competition.
    `slow` is for slow sellers: a lower sales floor, a few pieces at most, and the sure bar shows the risk."""
    s = sell.series
    hours, spd, trend = history_hours(s, now), sales_per_day(s, now), change(s, now, "min_ask")
    sure = sureness(hours, spd)
    if slow and spd >= c.MIN_SALES_PER_DAY and sure >= c.SURE_MIN:
        return None  # sells fast enough for the ordinary rules, which already said no
    floor = c.SLOW_MIN_SALES if slow else c.MIN_SALES_PER_DAY
    if hours < c.MIN_HISTORY_HOURS or spd < floor or trend is None or (sure < c.SURE_MIN and not slow):
        return None
    listed = listed_units(s, now)
    if slow:
        cap = max(0, max(1, math.floor(spd * c.CAPTURE_SHARE * c.SLOW_DAYS)) - held)
    else:  # what the sell house can absorb, counting what we already hold of it
        cap = min(max_units(spd, held), max(0, math.floor(c.SUPPLY_SHARE * listed) - held))
    dep = deposit(row.vendor_sell, sell.house)

    def price(left):
        return target if target is not None else undercut(min(going, left[0][0]) if left else going)

    if ladder is None:
        qty, cost, top, left = cap, unit_cost * cap, unit_cost, sell.ladder
    else:
        while True:  # no unit may cost more than it nets; buying less can lower our own list price, so repeat
            qty, cost, top = ladder_buy(ladder, limit, cap)
            left = _left(sell.ladder, qty) if buy_at == sell.house else sell.ladder
            net = int(price(left) * (1 - sell.cut)) - dep
            if qty == 0 or top <= net:
                break
            limit = net
    if qty == 0:
        return None
    target = price(left)
    profit = net_proceeds(target, qty, sell.cut, dep) - cost
    wait = {"hold": c.HOLD_DAYS, "rise": c.RISE_DAYS, "trend": c.INVEST_DAYS}.get(kind)
    if wait:  # these wait for the price to move, so they never score as selling instantly
        days = min(wait, 1 + days_to_sell(qty, spd))
        sell_by = now + timedelta(days=wait)
    else:
        days = days_to_sell(qty, spd)
        sell_by = now + timedelta(days=max(1, math.ceil(days)))
    return Pick(kind=kind, item_id=row.item_id, name=row.name or f"Item {row.item_id}", icon=row.icon,
                quality=row.quality, buy_at=buy_at, sell_at=sell.house, qty=qty, cost=cost,
                max_unit_price=top, sell_price=target, profit=profit, days=days,
                score=score(profit, cost, days), sure=sure,
                reason=f"{why(top)}; ≈{spd:.0f} sold/day (estimate).", spd=spd, trend=trend,
                depth=sum(u for p, u in left if target < p <= target * 1.05), sell_by=sell_by, listed=listed)


def _flip(kind, row, buy, sell, held, now):
    going = going_price(sell.series, now)
    if going is None or not _trend_ok(sell.series, now):
        return None
    where = "" if buy.house == sell.house else f" on {buy.house.title()}"

    def why(top):
        return (f"Listed at up to {_money(top)}{where}, {1 - top / going:.0%} under its 24h price"
                f" of {_money(going)} on {sell.house.title()}")
    return _build(kind, row, sell, held, now, buy.house, why, going=going, ladder=buy.ladder,
                  limit=int(c.BUY_BELOW * going))


def _snipe(row, d, held, now, slow=False):
    """Underpriced listings: a lot well under the next price on the ladder, small enough to resell
    soon. It is measured against the listings there now, not yesterday's price, so it also holds while
    a price is falling. `slow` allows a slow seller: a few pieces, resold over days, for a good profit each."""
    going = going_price(d.series, now)
    spd = sales_per_day(d.series, now)
    room = (max(1, math.floor(spd * c.CAPTURE_SHARE * c.SLOW_DAYS)) if slow
            else math.floor(spd * c.CAPTURE_SHARE * c.SNIPE_DAYS))
    qty, found = 0, None
    for (price, units), (wall, _) in zip(d.ladder, d.ladder[1:]):
        qty += units
        if qty > room:
            break
        if price <= c.BUY_BELOW * wall:
            found = price, wall
    if going is None or found is None:
        return None
    limit, wall = found
    wall = min(wall, going)  # a thin wall far above the usual price is not the price it will sell at

    def why(top):
        wait = "it sells slowly, so expect to wait days" if slow else "and few enough to resell within hours"
        return (f"Underpriced: listed at up to {_money(top)}, {1 - top / wall:.0%} under the next price of"
                f" {_money(wall)}, {wait}")
    p = _build("slow" if slow else "flip", row, d, held, now, d.house, why, going=wall, ladder=d.ladder,
               limit=limit, slow=slow)
    return None if p and slow and p.profit < c.SLOW_MIN_UNIT_PROFIT * p.qty else p


def quick_flip(row, d, held, now):
    """A flip, or failing that a slow seller: the same underpriced lot of an item that sells under the usual floor."""
    return (_flip("flip", row, d, d, held, now) or _snipe(row, d, held, now)
            or _snipe(row, d, held, now, slow=True))


def cross_flip(row, buy, sell, held, now):
    return _flip("cross", row, buy, sell, held, now)


def vendor_flip(row, d, held, now, rep=None):
    going = going_price(d.series, now)
    # items every vendor runs out of (a few per restock) are left out: not worth a trip
    if (VENDOR_ITEMS.get(row.item_id, 0) is not None or not d.purchase_price or going is None
            or not _trend_ok(d.series, now)):
        return None
    unit = vendor_unit(row.item_id, d.purchase_price, d.purchase_quantity, rep)
    off, who = vendor_deal(row.item_id, rep)
    who = (f": {who}" if who else "") + (f" ({off:.0%} off for your reputation)" if off else "")
    text = (f"A vendor sells it for {_money(unit)}{who} and it goes for {_money(going)}"
            f" on {d.house.title()}")
    return _build("vendor", row, d, held, now, "vendor", lambda _: text, going=going, unit_cost=unit)


def hold(row, d, crowd, held, now):
    band = item_band(row.item_id, row.required_level)
    s = d.series
    trend, units = change(s, now, "min_ask"), change(s, now, "units")
    if (band is None or not overlaps(band, crowd) or band[1] >= c.HOLD_ENDGAME_LEVEL
            or trend is None or units is None
            or units < c.FLOOD_UNITS_RISE or trend > c.FALLING or not _sales_hold(s, now)):
        return None
    current = going_price(s, now, hours=6)
    pre = window_median(s, now, 24, 48, "min_ask")
    target = round(current + c.HOLD_RECOVERY * (pre - current))
    text = (f"Flooded: {units:.0%} more listed and the price fell from {_money(round(pre))} to"
            f" {_money(current)}, aiming for {_money(target)} within {c.HOLD_DAYS} days")
    return _build("hold", row, d, held, now, d.house, lambda _: text, ladder=d.ladder, limit=current,
                  target=target)


def rise(row, d, behind, held, now):
    """Forecast: the crowd has levelled past where this is farmed or used, so fewer people are
    supplying it. Once the listed amount is shrinking and the price has stopped falling, bet on a rise."""
    going = going_price(d.series, now)
    trend, units = change(d.series, now, "min_ask"), change(d.series, now, "units")
    if not behind or going is None or trend is None or units is None or trend < 0 or units > 0:
        return None
    target = round(going * (1 + c.RISE_GAIN))
    text = (f"Forecast: players have levelled past it, {-units:.0%} fewer are listed than a day ago and the"
            f" price is {trend:+.0%}; aiming for {_money(target)} within {c.RISE_DAYS} days, up from {_money(going)}")
    return _build("rise", row, d, held, now, d.house, lambda _: text, ladder=d.ladder, limit=going,
                  target=target)


def trend(row, d, held, now):
    """Multi-day investment: the price has been climbing for a day, the item still sells as before and
    sellers are not piling in. Bet that the climb continues."""
    going = going_price(d.series, now)
    up, units = change(d.series, now, "min_ask"), change(d.series, now, "units")
    if (going is None or up is None or units is None or up < c.TREND_MIN or units >= c.FLOOD_UNITS_RISE
            or not _sales_hold(d.series, now)):
        return None
    target = round(going * (1 + c.RISE_GAIN))
    text = (f"Rising: the price is {up:+.0%} against a day ago, sales are holding and the listed amount is"
            f" {units:+.0%}; aiming for {_money(target)} within {c.INVEST_DAYS} days, up from {_money(going)}")
    return _build("trend", row, d, held, now, d.house, lambda _: text, ladder=d.ladder, limit=going, target=target)


def craft_ladder(mats, made, limit):
    """What each crafted unit costs in materials, cheapest first, as a (unit_price, units) ladder.
    `mats` is [(count per craft, ladder)] with a vendor reagent as a one-step ladder."""
    left = [(count, [list(step) for step in ladder]) for count, ladder in mats]
    out = []
    while sum(u for _, u in out) < limit:
        cost = 0
        for count, ladder in left:
            need = count
            while need and ladder:
                take = min(need, ladder[0][1])
                cost, need = cost + take * ladder[0][0], need - take
                ladder[0][1] -= take
                if not ladder[0][1]:
                    ladder.pop(0)
            if need:  # this reagent ran out
                return out
        out.append((math.ceil(cost / made), made))
    return out


def craft_flip(row, d, recipe, mats, held, now):
    """Buy the reagents, craft, sell the product. `mats` is [(name, count, ladder, from_vendor)]."""
    _, prof, skill, made, _ = recipe
    going = going_price(d.series, now)
    if going is None or not _trend_ok(d.series, now):
        return None
    ladder = craft_ladder([(n, lad) for _, n, lad, _ in mats], made, c.CRAFT_MAX_UNITS)
    p = _build("craft", row, d, held, now, "craft", lambda _: "", going=going, ladder=ladder,
               limit=int(c.BUY_BELOW * going))
    if p is None or p.qty < made:
        return None
    p.profession = prof
    crafts = math.ceil(p.qty / made)
    need = ", ".join(f"{crafts * n}× {name} ({'vendor' if vend else 'AH'})" for name, n, _, vend in mats)
    p.reason = (f"Craft {crafts}× with {prof} (skill {skill}): {need}. Materials up to {_money(p.max_unit_price)}"
                f" a piece, it goes for {_money(going)} on {d.house.title()}" + p.reason)
    return p
