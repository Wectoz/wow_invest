from datetime import timedelta

import pytest

from tests.conftest import NOW, pts, steady
from wow_invest.models import Detail, Row
from wow_invest.picks import cross_flip, hold, quick_flip, vendor_flip
from wow_invest.tables import VENDOR_ITEMS


def row(item_id=1, vendor_sell=None, required_level=None):
    return Row(item_id=item_id, name=None, icon=None, quality=None, class_name=None, subclass_name=None,
               required_level=required_level, item_level=None, min_ask=None, avg_ask=None, units=0,
               listings=0, sold_24h=0, vendor_sell=vendor_sell)


def detail(series=steady, ladder=(), cut=0.05, house="alliance", purchase_price=0, purchase_quantity=1):
    return Detail(item_id=1, house=house, ladder=list(ladder), series=series, cut=cut,
                  purchase_price=purchase_price, purchase_quantity=purchase_quantity, sell_price=0,
                  required_level=None)


WALL = [(12000, 10), (15000, 5), (19900, 100)]
# 24-48h ago: min 100, 1000 units; last 24h: min 60, 1500 units; 48 sales/day in both windows
flood = pts([(h, 1500, 60, 0, 4) for h in range(24)] + [(h, 1000, 100, 0, 4) for h in range(24, 48)])


def test_quick_flip_buys_below_80pct_and_lists_under_remaining_wall():
    p = quick_flip(row(vendor_sell=500), detail(series=steady, ladder=WALL), held=0, now=NOW)
    # going 20000, cheapest left 19900 → sell 19899; int(19899*15*0.95)=283560 − 75*15 = 282435 − 195000
    assert (p.kind, p.qty, p.cost, p.sell_price, p.profit) == ("flip", 15, 195000, 19899, 87435)
    assert p.days == pytest.approx(0.625)
    assert (p.buy_at, p.sell_at, p.max_unit_price, p.depth, p.spd, p.trend) == \
        ("alliance", "alliance", 15000, 100, 48, 0)
    assert p.sell_by == NOW + timedelta(days=1)
    assert p.name == "Item 1" and "2g" in p.reason and "48" in p.reason


def test_quick_flip_lists_under_going_when_wall_is_higher():
    p = quick_flip(row(), detail(ladder=[(12000, 10), (25000, 50)]), held=0, now=NOW)
    assert (p.qty, p.cost, p.sell_price, p.depth) == (10, 120000, 19999, 0)


def test_builders_return_none_without_24_48h_window():
    # ages 0–23: history 23h ≥ 12h and sure 23/48 ≥ 0.4, but change() has no 24–48h window
    young = pts([(h, 300, 20000, 0, 4) for h in range(24)])
    d = detail(series=young, ladder=WALL, purchase_price=5000)
    assert quick_flip(row(), d, held=0, now=NOW) is None
    assert cross_flip(row(), detail(house="blackwater", ladder=WALL), d, held=0, now=NOW) is None
    assert vendor_flip(row(), d, held=0, confirmed={1}, now=NOW) is None
    assert hold(row(item_id=785), d, crowd=(20, 30), held=0, now=NOW) is None


def test_quick_flip_rejects_falling_price_with_rising_units():
    s = pts([(h, 400, 20000, 0, 4) for h in range(24)] + [(h, 200, 30000, 0, 4) for h in range(24, 48)])
    assert quick_flip(row(), detail(series=s, ladder=[(12000, 10)]), held=0, now=NOW) is None


def test_quick_flip_allows_falling_price_when_units_shrink_and_sales_hold():
    s = pts([(h, 200, 20000, 0, 4) for h in range(24)] + [(h, 400, 30000, 0, 4) for h in range(24, 48)])
    p = quick_flip(row(vendor_sell=500), detail(series=s, ladder=[(12000, 10)]), held=0, now=NOW)
    # nothing left on the ladder → undercut(going 20000); int(19999*10*0.95)=189990 − 750 − 120000
    assert (p.kind, p.qty, p.sell_price, p.profit) == ("flip", 10, 19999, 69240)
    assert p.trend == pytest.approx(-1 / 3)
    # same slide, but sales fell from 48/day to 24/day → no pick
    s = pts([(h, 200, 20000, 0, 2) for h in range(24)] + [(h, 400, 30000, 0, 4) for h in range(24, 48)])
    assert quick_flip(row(), detail(series=s, ladder=[(12000, 10)]), held=0, now=NOW) is None


def test_quick_flip_none_with_thin_history():
    thin = pts([(h, 300, 20000, 0, 4) for h in range(6)])
    assert quick_flip(row(), detail(series=thin, ladder=WALL), held=0, now=NOW) is None


def test_quick_flip_respects_held_units():
    assert quick_flip(row(), detail(ladder=WALL), held=60, now=NOW) is None
    assert quick_flip(row(), detail(ladder=WALL), held=55, now=NOW).qty == 5


def test_cross_flip_buys_on_blackwater_sells_on_alliance_with_alliance_cut():
    buy = detail(series=[], ladder=WALL, cut=0.15, house="blackwater")  # buy series is never read
    sell = detail(series=steady, ladder=[(19500, 20)])
    p = cross_flip(row(vendor_sell=500), buy, sell, held=0, now=NOW)
    # sell under Alliance's own wall: 19499; int(19499*15*0.95)=277860 − 75*15 − 195000
    assert (p.kind, p.buy_at, p.sell_at) == ("cross", "blackwater", "alliance")
    assert (p.qty, p.cost, p.sell_price, p.profit, p.depth) == (15, 195000, 19499, 81735, 20)


def test_vendor_flip_only_for_known_or_confirmed_items(monkeypatch):
    d = detail(ladder=[(19900, 100)], purchase_price=5000)
    p = vendor_flip(row(vendor_sell=500), d, held=0, confirmed=set(), now=NOW)
    # qty 60 (cap) at 5000 = 300000; int(19899*60*0.95)=1134243 − 75*60 − 300000
    assert (p.kind, p.buy_at, p.sell_at, p.qty, p.cost, p.max_unit_price, p.sell_price, p.profit) == \
        ("vendor_check", "vendor", "alliance", 60, 300000, 5000, 19899, 829743)
    assert p.sell_by == NOW + timedelta(days=3)  # days 2.5
    assert vendor_flip(row(vendor_sell=500), d, held=0, confirmed={1}, now=NOW).kind == "vendor"
    assert vendor_flip(row(item_id=2320), d, held=0, confirmed=set(), now=NOW).kind == "vendor"
    # unknown and going 20000 < 2 × 15000 → nothing
    assert vendor_flip(row(), detail(purchase_price=15000), held=0, confirmed=set(), now=NOW) is None
    assert vendor_flip(row(), detail(purchase_price=0), held=0, confirmed={1}, now=NOW) is None
    # sold in stacks of 5 for 10001 → ceil(2000.2) = 2001 a unit
    p = vendor_flip(row(), detail(purchase_price=10001, purchase_quantity=5), held=0, confirmed={1}, now=NOW)
    assert (p.max_unit_price, p.cost) == (2001, 2001 * 60)
    monkeypatch.setitem(VENDOR_ITEMS, 1, 7)  # limited stock caps the quantity
    assert vendor_flip(row(), d, held=0, confirmed=set(), now=NOW).qty == 7


def test_hold_targets_half_recovery_and_three_day_sell_by():
    # Mageroyal 785, crowd (20, 30); 24-48h: min 100, units 1000; last 6h: min 60, units 1500; sales steady
    p = hold(row(item_id=785), detail(series=flood, ladder=[(60, 500)]), crowd=(20, 30), held=0, now=NOW)
    assert (p.kind, p.sell_price, p.sell_by) == ("hold", 80, NOW + timedelta(days=3))
    assert p.days >= 1.0
    # 60 units at 60c; days = min(3, 1 + 60/24); int(80*60*0.95) = 4560 − 3600
    assert (p.qty, p.cost, p.max_unit_price, p.days, p.profit) == (60, 3600, 60, 3, 960)


def test_hold_none_for_endgame_band():
    d = detail(series=flood, ladder=[(60, 500)])
    assert hold(row(required_level=50), d, crowd=(45, 52), held=0, now=NOW) is None
    assert hold(row(required_level=45), d, crowd=(45, 52), held=0, now=NOW).kind == "hold"


def test_hold_none_when_sales_fall_with_price():
    s = pts([(h, 1500, 60, 0, 2) for h in range(24)] + [(h, 1000, 100, 0, 4) for h in range(24, 48)])
    assert hold(row(item_id=785), detail(series=s, ladder=[(60, 500)]), crowd=(20, 30), held=0, now=NOW) is None
