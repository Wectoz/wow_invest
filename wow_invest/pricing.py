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
    """Likely sold, plus a share of the early disappearances that new listings did not replace:
    an hour with as many new units as early disappearances is mostly sellers cancelling and relisting."""
    return sum(p.partial_fills + c.SOLD_OR_CANCELLED_WEIGHT * max(0, p.early - p.new_units)
               for p in series if start_h <= _age_h(p, now) < end_h)


def sales_per_day(series, now) -> float:
    return _sales(series, now, 0, 24)


def sales_per_day_window(series, now, start_h, end_h) -> float:
    return _sales(series, now, start_h, end_h) * 24 / (end_h - start_h)


def history_hours(series, now) -> float:
    ages = [_age_h(p, now) for p in series if p.min_ask is not None]
    return max(ages) if ages else 0.0


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
