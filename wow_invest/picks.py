"""Candidate builders for the four kinds of pick. When unsure, they return None (no pick)."""
import math
from datetime import timedelta

from . import config as c
from .crowd import item_band, overlaps
from .models import Pick
from .pricing import (change, days_to_sell, deposit, going_price, history_hours, ladder_buy,
                      listed_units, max_units, net_proceeds, sales_per_day, sales_per_day_window,
                      score, sureness, undercut, window_median)
from .tables import VENDOR_ITEMS


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


def _build(kind, row, sell, held, now, buy_at, why, going=0, ladder=None, limit=0, unit_cost=0, target=None):
    """Shared middle: sales, cap, buy, proceeds, days, score and sure, all on the sell house.
    `ladder=None` means a vendor buy at `unit_cost`; `target=None` means list under the competition."""
    s = sell.series
    hours, spd, trend = history_hours(s, now), sales_per_day(s, now), change(s, now, "min_ask")
    sure = sureness(hours, spd)
    if hours < c.MIN_HISTORY_HOURS or spd < c.MIN_SALES_PER_DAY or trend is None or sure < c.SURE_MIN:
        return None
    listed = listed_units(s, now)
    cap = min(max_units(spd, held), math.floor(c.SUPPLY_SHARE * listed))  # what the sell house can absorb
    dep = deposit(row.vendor_sell, sell.house)

    def price(left):
        return target if target is not None else undercut(min(going, left[0][0]) if left else going)

    if ladder is None:
        stock = VENDOR_ITEMS.get(row.item_id)  # None: unlimited or unknown stock
        qty = cap if stock is None else min(cap, stock)
        cost, top, left = unit_cost * qty, unit_cost, sell.ladder
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
    if kind == "hold":  # a hold waits for the recovery, so it never scores as selling instantly
        days = min(c.HOLD_DAYS, 1 + days_to_sell(qty, spd))
        sell_by = now + timedelta(days=c.HOLD_DAYS)
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


def quick_flip(row, d, held, now):
    return _flip("flip", row, d, d, held, now)


def cross_flip(row, buy, sell, held, now):
    return _flip("cross", row, buy, sell, held, now)


def vendor_flip(row, d, held, confirmed, now):
    going = going_price(d.series, now)
    if not d.purchase_price or going is None or not _trend_ok(d.series, now):
        return None
    unit = math.ceil(d.purchase_price / d.purchase_quantity)
    if row.item_id in VENDOR_ITEMS or row.item_id in confirmed:
        kind, text = "vendor", f"A vendor sells it for {_money(unit)}"
    elif going >= c.VENDOR_CHECK_RATIO * unit:
        kind, text = "vendor_check", f"Check whether a vendor sells it for {_money(unit)}"
    else:
        return None
    text += f" and it goes for {_money(going)} on {d.house.title()}"
    return _build(kind, row, d, held, now, "vendor", lambda _: text, going=going, unit_cost=unit)


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
