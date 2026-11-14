"""Pure money / sales / trend math."""
import math
from datetime import datetime
from statistics import median

from . import config as c


def ladder_buy(ladder, max_unit_price: int, max_qty: int) -> tuple[int, int, int]:
    qty = cost = top = 0
    for price, units in ladder:
        if price > max_unit_price or qty >= max_qty:
            break
        take = min(units, max_qty - qty)
        qty += take
        cost += take * price
        top = price
    return (qty, cost, top) if qty else (0, 0, 0)


def _age_h(p, now: datetime) -> float:
    return (now - p.at).total_seconds() / 3600


def going_price(series, now, hours=24) -> int | None:
    v = [p.min_ask for p in series if p.min_ask is not None and 0 <= _age_h(p, now) < hours]
    return int(median(v)) if v else None


def window_median(series, now, start_h: float, end_h: float, field: str) -> float | None:
    v = [getattr(p, field) for p in series
         if getattr(p, field) is not None and start_h <= _age_h(p, now) < end_h]
    return median(v) if v else None


def change(series, now, field) -> float | None:
    recent = window_median(series, now, 0, 6, field)
    old = window_median(series, now, 24, 48, field)
    if recent is None or not old:
        return None
    return recent / old - 1


def _sales(series, now, start_h, end_h) -> float:
    """Confirmed sales, plus a share of the possible ones (the site already leaves out relisted units)."""
    return sum(p.partial_fills + c.SOLD_OR_CANCELLED_WEIGHT * p.early
               for p in series if start_h <= _age_h(p, now) < end_h)


def sales_per_day(series, now) -> float:
    return _sales(series, now, 0, 24)


def sales_per_day_window(series, now, start_h, end_h) -> float:
    return _sales(series, now, start_h, end_h) * 24 / (end_h - start_h)


def listed_units(series, now) -> int:
    m = window_median(series, now, 0, 24, "units")
    return 0 if m is None else int(m)


def history_hours(series, now) -> float:
    ages = [_age_h(p, now) for p in series if p.min_ask is not None]
    return max(ages) if ages else 0.0


def held_price(series, now) -> int | None:
    """The cheapest ask the item met or beat BUYOUT_HELD_SHARE of its whole history: a price it holds."""
    v = sorted(p.min_ask for p in series if p.min_ask is not None and _age_h(p, now) >= 0)
    return v[int((1 - c.BUYOUT_HELD_SHARE) * len(v))] if v else None


def failed_resets(series, now) -> int:
    """Earlier buyouts that did not hold: the cheapest ask jumped BUYOUT_SPIKE over the median of the
    day before, then fell back under the midpoint within a day."""
    pts = sorted((p for p in series if p.min_ask is not None and _age_h(p, now) >= 0), key=lambda p: p.at)
    n, last = 0, None
    for i, p in enumerate(pts):
        before = [q.min_ask for q in pts[:i] if (p.at - q.at).total_seconds() <= 86400]
        if not before or p.min_ask < c.BUYOUT_SPIKE * median(before):
            continue
        if last is not None and (p.at - last).total_seconds() <= 86400:
            continue  # still the same jump
        last = p.at
        after = [q.min_ask for q in pts[i + 1:] if (q.at - p.at).total_seconds() <= 86400]
        n += bool(after) and min(after) <= (median(before) + p.min_ask) / 2
    return n


def undercut(price: int) -> int:
    return max(1, price - 1)


def deposit(vendor_sell: int | None, house: str) -> int:
    return 0 if vendor_sell is None else int(vendor_sell * c.DEPOSIT_RATE[house])


def net_proceeds(unit_price: int, qty: int, cut: float, deposit_per_unit: int) -> int:
    return int(unit_price * qty * (1 - cut)) - deposit_per_unit * qty


def days_to_sell(qty: int, sold_per_day: float) -> float:
    if sold_per_day <= 0:
        return math.inf
    return qty / (sold_per_day * c.CAPTURE_SHARE)


def max_units(sold_per_day: float, held: int) -> int:
    return max(0, math.floor(sold_per_day * c.CAPTURE_SHARE * c.MAX_SALES_DAYS) - held)


def score(profit: int, cost: int, days: float) -> float:
    if cost <= 0:
        return 0.0
    return (profit / cost) / max(days, 1 / 24)


def sureness(hours: float, sold_per_day: float) -> float:
    return min(1, hours / 48) * min(1, sold_per_day / (4 * c.MIN_SALES_PER_DAY))
