from datetime import timedelta

import pytest

from tests.conftest import NOW, pts, steady
from wow_invest import config
from wow_invest.models import Detail, Row
from wow_invest.picks import cross_flip, hold, quick_flip, vendor_flip
from wow_invest.planner import plan
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
    assert p.name == "Item 1" and "2g" in p.reason and "≈48 sold/day (estimate)" in p.reason
    assert p.listed == 300  # supply cap floor(0.25 × 300) = 75 does not bind


def test_quick_flip_lists_under_going_when_wall_is_higher():
    p = quick_flip(row(), detail(ladder=[(12000, 10), (25000, 50)]), held=0, now=NOW)
    assert (p.qty, p.cost, p.sell_price, p.depth) == (10, 120000, 19999, 0)


def test_builders_return_none_without_24_48h_window(monkeypatch):
    # ages 0–23: history 23h ≥ 12h and sure 23/48 ≥ 0.4, but change() has no 24–48h window
    young = pts([(h, 300, 20000, 0, 4) for h in range(24)])
    d = detail(series=young, ladder=WALL, purchase_price=5000)
    assert quick_flip(row(), d, held=0, now=NOW) is None
    assert cross_flip(row(), detail(house="blackwater", ladder=WALL), d, held=0, now=NOW) is None
    monkeypatch.setitem(VENDOR_ITEMS, 1, None)
    assert vendor_flip(row(), d, held=0, now=NOW) is None
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
    assert quick_flip(row(), detail(ladder=WALL), held=50, now=NOW).qty == 10
    # cap 5 leaves 5 at 12000 on the ladder, so we would list at 11999 and net int(11999 × 0.95) = 11399
    assert quick_flip(row(), detail(ladder=WALL), held=55, now=NOW) is None


def test_cross_flip_never_buys_a_unit_above_its_net_proceeds():
    # sell on Blackwater: target 19999 nets int(19999 × 0.85) − int(5000 × 0.75) = 16999 − 3750 = 13249 < 16000
    buy, sell = detail(series=[], ladder=WALL), detail(cut=0.15, house="blackwater")
    p = cross_flip(row(vendor_sell=5000), buy, sell, held=0, now=NOW)
    # only the 10 at 12000; int(19999 × 10 × 0.85) = 169991 − 37500 − 120000
    assert (p.qty, p.cost, p.max_unit_price, p.sell_price, p.profit) == (10, 120000, 12000, 19999, 12491)


def test_hold_never_buys_above_net_proceeds_of_its_target():
    # target 80 nets int(80 × 0.95) − int(120 × 0.15) = 76 − 18 = 58, under the current 60
    d = detail(series=flood, ladder=[(55, 30), (60, 500)])
    p = hold(row(item_id=785, vendor_sell=120), d, crowd=(20, 30), held=0, now=NOW)
    assert (p.qty, p.cost, p.max_unit_price) == (30, 1650, 55)


def test_churn_on_a_small_house_is_not_a_pick():
    # Strange Dust on Blackwater, first live run: 1645 bought against 734 listed. Each hour 10 likely
    # sold, 89 gone early, 71 new: ≈ 10 + 0.5 × 18 = 19 an hour = 456 a day (1308 before netting).
    # Listed units creep up 2 an hour; the price drifts from 150 to 139 (−6%, so the trend check passes).
    s = pts([(h, 740 - 2 * h, 139 + h // 4, 10, 89, 71) for h in range(48)])
    sell = detail(series=s, ladder=[(139, 300), (145, 440)], cut=0.15, house="blackwater")
    buy = detail(series=[], ladder=[(75, 300), (80, 400), (89, 1000)])
    p = cross_flip(row(item_id=10940, vendor_sell=1), buy, sell, held=0, now=NOW)
    # median listed 717 → cap floor(179.25) = 179, all at 75c; sell undercut(min(141, 139)) = 138;
    # int(138 × 179 × 0.85) = 20996 − 13425 = 7571 (old rules: 1635 units, +54070, funded)
    assert (p.spd, p.listed, p.qty, p.cost, p.sell_price, p.profit) == (456, 717, 179, 13425, 138, 7571)
    assert p.qty <= 0.25 * p.listed and p.profit < config.MIN_PROFIT
    assert plan([p], gold=10**9, holdings=[], proven=False).ranked == []


def test_high_churn_with_real_fills_is_capped_at_a_quarter_of_listed():
    # 50 likely sold + 0.5 × (200 − 180) = 60 an hour = 1440 a day → sales cap 1800, but only 400 listed
    s = pts([(h, 400, 20000, 50, 200, 180) for h in range(48)])
    buy = detail(series=[], ladder=[(12000, 500)], cut=0.15, house="blackwater")
    p = cross_flip(row(), buy, detail(series=s, ladder=[(19900, 400)]), held=0, now=NOW)
    # int(19899 × 100 × 0.95) = 1890405 − 1200000
    assert (p.spd, p.listed, p.qty, p.cost, p.profit) == (1440, 400, 100, 1200000, 690405)


def test_supply_cap_counts_units_already_held():
    # same market: a quarter of 400 listed is 100; holding 60 leaves room for 40, holding 100 for none
    s = pts([(h, 400, 20000, 50, 200, 180) for h in range(48)])
    buy = detail(series=[], ladder=[(12000, 500)], cut=0.15, house="blackwater")
    sell = detail(series=s, ladder=[(19900, 400)])
    p = cross_flip(row(), buy, sell, held=60, now=NOW)
    # int(19899 × 40 × 0.95) = 756162 − 480000
    assert (p.qty, p.cost, p.profit) == (40, 480000, 276162)
    assert cross_flip(row(), buy, sell, held=100, now=NOW) is None


def test_cross_flip_buys_on_blackwater_sells_on_alliance_with_alliance_cut():
    buy = detail(series=[], ladder=WALL, cut=0.15, house="blackwater")  # buy series is never read
    sell = detail(series=steady, ladder=[(19500, 20)])
    p = cross_flip(row(vendor_sell=500), buy, sell, held=0, now=NOW)
    # sell under Alliance's own wall: 19499; int(19499*15*0.95)=277860 − 75*15 − 195000
    assert (p.kind, p.buy_at, p.sell_at) == ("cross", "blackwater", "alliance")
    assert (p.qty, p.cost, p.sell_price, p.profit, p.depth) == (15, 195000, 19499, 81735, 20)


def test_vendor_flip_only_for_items_a_vendor_sells(monkeypatch):
    d = detail(ladder=[(19900, 100)], purchase_price=5000)
    assert vendor_flip(row(vendor_sell=500), d, held=0, now=NOW) is None  # item 1: no vendor sells it
    assert vendor_flip(row(item_id=2320), d, held=0, now=NOW).kind == "vendor"
    monkeypatch.setitem(VENDOR_ITEMS, 1, None)
    p = vendor_flip(row(vendor_sell=500), d, held=0, now=NOW)
    # qty 60 (cap) at 5000 = 300000; int(19899*60*0.95)=1134243 − 75*60 − 300000
    assert (p.kind, p.buy_at, p.sell_at, p.qty, p.cost, p.max_unit_price, p.sell_price, p.profit) == \
        ("vendor", "vendor", "alliance", 60, 300000, 5000, 19899, 829743)
    assert p.sell_by == NOW + timedelta(days=3)  # days 2.5
    assert "limited" not in p.reason
    assert vendor_flip(row(), detail(purchase_price=0), held=0, now=NOW) is None
    # sold in stacks of 5 for 10001 → ceil(2000.2) = 2001 a unit
    p = vendor_flip(row(), detail(purchase_price=10001, purchase_quantity=5), held=0, now=NOW)
    assert (p.max_unit_price, p.cost) == (2001, 2001 * 60)
    monkeypatch.setitem(VENDOR_ITEMS, 1, 7)  # limited stock caps the quantity
    p = vendor_flip(row(), d, held=0, now=NOW)
    assert p.qty == 7 and "limited" in p.reason


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
