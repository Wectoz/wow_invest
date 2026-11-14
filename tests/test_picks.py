from datetime import timedelta

import pytest

from tests.conftest import NOW, pts, steady
from wow_invest import config
from wow_invest.models import Detail, Row
from wow_invest.picks import buyout, craft_flip, craft_ladder, cross_flip, hold, quick_flip, rise, vendor_flip
from wow_invest.planner import plan
from wow_invest.pricing import failed_resets
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
    # going 20000, cheapest left 19900 → sell 19899; int(19899*15*0.95)=283560 − 195000
    # (sold within a day, so no listing runs out and no deposit is lost)
    assert (p.kind, p.qty, p.cost, p.sell_price, p.profit) == ("flip", 15, 195000, 19899, 88560)
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
    # nothing left on the ladder → undercut(going 20000); int(19999*10*0.95)=189990 − 120000
    assert (p.kind, p.qty, p.sell_price, p.profit) == ("flip", 10, 19999, 69990)
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
    # a house with a steep cut: target 19999 nets int(19999 × 0.75) = 14999 < the 15000 step
    buy, sell = detail(series=[], ladder=WALL), detail(cut=0.25, house="blackwater")
    p = cross_flip(row(), buy, sell, held=0, now=NOW)
    # only the 10 at 12000; int(19999 × 10 × 0.75) = 149992 − 120000
    assert (p.qty, p.cost, p.max_unit_price, p.sell_price, p.profit) == (10, 120000, 12000, 19999, 29992)


def test_hold_never_buys_above_net_proceeds_of_its_target():
    # with a steep cut the target 80 nets int(80 × 0.70) = 56, under the current 60
    d = detail(series=flood, ladder=[(55, 30), (60, 500)], cut=0.30)
    p = hold(row(item_id=785), d, crowd=(20, 30), held=0, now=NOW)
    assert (p.qty, p.cost, p.max_unit_price) == (30, 1650, 55)


def test_churn_on_a_small_house_is_not_a_pick():
    # Strange Dust on Blackwater, first live run: 1645 bought against 734 listed. Each hour 10 likely
    # sold, 18 possibly sold: 10 + 0.5 × 18 = 19 an hour = 456 a day (1308 before netting).
    # Listed units creep up 2 an hour; the price drifts from 150 to 139 (−6%, so the trend check passes).
    s = pts([(h, 740 - 2 * h, 139 + h // 4, 10, 18) for h in range(48)])
    sell = detail(series=s, ladder=[(139, 300), (145, 440)], cut=0.15, house="blackwater")
    buy = detail(series=[], ladder=[(75, 300), (80, 400), (89, 1000)])
    p = cross_flip(row(item_id=10940, vendor_sell=1), buy, sell, held=0, now=NOW)
    # median listed 717 → cap floor(179.25) = 179, all at 75c; sell undercut(min(141, 139)) = 138;
    # int(138 × 179 × 0.85) = 20996 − 13425 = 7571 (old rules: 1635 units, +54070, funded)
    assert (p.spd, p.listed, p.qty, p.cost, p.sell_price, p.profit) == (456, 717, 179, 13425, 138, 7571)
    assert p.qty <= 0.25 * p.listed and p.profit < config.MIN_PROFIT
    assert plan([p], gold=10**9, holdings=[], proven=False).ranked == []


def test_high_churn_with_real_fills_is_capped_at_a_quarter_of_listed():
    # 50 sold + 0.5 × 20 possibly sold = 60 an hour = 1440 a day → sales cap 1800, but only 400 listed
    s = pts([(h, 400, 20000, 50, 20) for h in range(48)])
    buy = detail(series=[], ladder=[(12000, 500)], cut=0.15, house="blackwater")
    p = cross_flip(row(), buy, detail(series=s, ladder=[(19900, 400)]), held=0, now=NOW)
    # int(19899 × 100 × 0.95) = 1890405 − 1200000
    assert (p.spd, p.listed, p.qty, p.cost, p.profit) == (1440, 400, 100, 1200000, 690405)


def test_supply_cap_counts_units_already_held():
    # same market: a quarter of 400 listed is 100; holding 60 leaves room for 40, holding 100 for none
    s = pts([(h, 400, 20000, 50, 20) for h in range(48)])
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
    # sell under Alliance's own wall: 19499; int(19499*15*0.95)=277860 − 195000
    assert (p.kind, p.buy_at, p.sell_at) == ("cross", "blackwater", "alliance")
    assert (p.qty, p.cost, p.sell_price, p.profit, p.depth) == (15, 195000, 19499, 82860, 20)


def test_vendor_flip_only_for_items_a_vendor_sells(monkeypatch):
    d = detail(ladder=[(19900, 100)], purchase_price=5000)
    assert vendor_flip(row(vendor_sell=500), d, held=0, now=NOW) is None  # item 1: no vendor sells it
    assert vendor_flip(row(item_id=2320), d, held=0, now=NOW).kind == "vendor"
    monkeypatch.setitem(VENDOR_ITEMS, 1, None)
    p = vendor_flip(row(vendor_sell=500), d, held=0, now=NOW)
    # qty 60 (cap) at 5000 = 300000; int(19899*60*0.95)=1134243 − 300000, less the deposit of
    # 75 lost on relisting: 2.5 days to sell → (2.5 − 1) / 2 relists a unit → 56 × 60
    assert (p.kind, p.buy_at, p.sell_at, p.qty, p.cost, p.max_unit_price, p.sell_price, p.profit) == \
        ("vendor", "vendor", "alliance", 60, 300000, 5000, 19899, 830883)
    assert p.sell_by == NOW + timedelta(days=3)  # days 2.5
    assert vendor_flip(row(), detail(purchase_price=0), held=0, now=NOW) is None
    # sold in stacks of 5 for 10001 → ceil(2000.2) = 2001 a unit
    p = vendor_flip(row(), detail(purchase_price=10001, purchase_quantity=5), held=0, now=NOW)
    assert (p.max_unit_price, p.cost) == (2001, 2001 * 60)
    monkeypatch.setitem(VENDOR_ITEMS, 1, 1)  # limited stock: never a vendor flip
    assert vendor_flip(row(), d, held=0, now=NOW) is None


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


def test_rise_bets_on_an_item_the_crowd_left_once_supply_shrinks_and_the_price_holds():
    # listed 1500 a day ago, 1000 now; price steady at 20000
    drying = pts([(h, 1000, 20000, 0, 4) for h in range(24)] + [(h, 1500, 20000, 0, 4) for h in range(24, 48)])
    lad = [(19000, 10), (20000, 10), (21000, 50)]
    p = rise(row(item_id=785), detail(series=drying, ladder=lad), True, held=0, now=NOW)
    # buys up to the going price (20000), aims 15% above it, waits a week
    assert (p.kind, p.qty, p.cost, p.sell_price) == ("rise", 20, 390000, 23000)
    assert p.sell_by == NOW + timedelta(days=7) and "33% fewer are listed" in p.reason
    assert rise(row(), detail(series=drying, ladder=lad), False, held=0, now=NOW) is None  # the crowd is still there
    assert rise(row(), detail(series=flood, ladder=[(50, 10)]), True, held=0, now=NOW) is None  # still falling
    growing = pts([(h, 1500, 20000, 0, 4) for h in range(24)] + [(h, 1000, 20000, 0, 4) for h in range(24, 48)])
    assert rise(row(), detail(series=growing, ladder=lad), True, held=0, now=NOW) is None  # supply still building


def test_craft_ladder_prices_each_crafted_unit_from_the_reagent_ladders():
    cloth, thread = [(100, 3), (200, 10)], [(10, 10**9)]  # AH ladder, vendor at a flat 10
    # 2 cloth + 1 thread a craft: 210, then 100+200+10, then 410..., until the cloth runs out after 6 crafts
    assert craft_ladder([(2, cloth), (1, thread)], 1, 100) == [(210, 1), (310, 1)] + [(410, 1)] * 4
    assert craft_ladder([(2, cloth), (1, thread)], 1, 2) == [(210, 1), (310, 1)]
    assert craft_ladder([(1, [(100, 5)])], 200, 1000) == [(1, 200)] * 5  # 200 arrows a craft, rounded up
    assert cloth == [(100, 3), (200, 10)]  # the caller's ladders are not consumed


def test_craft_flip_buys_reagents_while_each_piece_still_pays():
    recipe = (1, "Tailoring", 125, 1, ((7, 2), (8, 1)))
    mats = [("Bolt", 2, [(4000, 6), (9000, 100)], False), ("Thread", 1, [(500, 10**9)], True)]
    p = craft_flip(row(), detail(ladder=[(19900, 100)]), recipe, mats, held=0, now=NOW)
    # going 20000, limit 16000 a piece: 3 crafts at 8500, the 4th would need 9000-a-bolt cloth (18500)
    assert (p.kind, p.buy_at, p.qty, p.cost, p.max_unit_price, p.sell_price) == ("craft", "craft", 3, 25500, 8500, 19899)
    assert p.reason.startswith("Craft 3× with Tailoring (skill 125): 6× Bolt (AH), 3× Thread (vendor). Materials up to 85s")
    assert "≈48 sold/day (estimate)" in p.reason
    dear = [("Bolt", 2, [(9000, 100)], False)]
    assert craft_flip(row(), detail(ladder=[(19900, 100)]), recipe, dear, held=0, now=NOW) is None


def test_craft_flip_pays_for_whole_crafts():
    recipe = (1, "Tailoring", 125, 2, ((8, 1),))  # one thread makes two
    mats = [("Thread", 1, [(500, 10**9)], True)]
    p = craft_flip(row(), detail(ladder=[(19900, 100)]), recipe, mats, held=1, now=NOW)
    # room for 59 pieces takes 30 crafts: 30 threads, not 29.5
    assert (p.qty, p.cost) == (59, 15000) and p.reason.startswith("Craft 30× with Tailoring")


def test_underpriced_lot_is_a_flip_even_while_the_price_falls():
    # yesterday 200, today 100: a falling price, so the ordinary flip refuses it
    falling = pts([(h, 300, 100, 0, 40) for h in range(24)] + [(h, 300, 200, 0, 40) for h in range(24, 48)])
    lot = [(60, 30), (100, 500), (101, 500)]  # 30 units 40% under the next listing
    p = quick_flip(row(), detail(series=falling, ladder=lot), held=0, now=NOW)
    assert (p.kind, p.qty, p.cost, p.sell_price) == ("flip", 30, 1800, 99)
    assert p.reason.startswith("Underpriced: listed at up to 60c, 40% under the next price of 1s")
    # no gap of a fifth anywhere near the bottom: nothing
    assert quick_flip(row(), detail(series=falling, ladder=[(95, 30), (100, 500)]), held=0, now=NOW) is None
    # the cheap lot is bigger than what resells in six hours (480 a day × 0.5 × 0.25 = 60)
    assert quick_flip(row(), detail(series=falling, ladder=[(60, 61), (100, 500)]), held=0, now=NOW) is None
    # a thin wall far above the usual price: it sells at the usual price, not the wall
    p = quick_flip(row(), detail(series=falling, ladder=[(60, 30), (500, 2)]), held=0, now=NOW)
    assert p.sell_price == 99


def test_slow_seller_with_an_underpriced_piece_and_a_good_profit():
    slow = pts([(h, 6, 50000, 0, 1 if h % 6 == 0 else 0) for h in range(48)])  # 2 sales a day: under the usual floor
    lad = [(30000, 1), (50000, 1), (51300, 1)]
    p = quick_flip(row(), detail(series=slow, ladder=lad), held=0, now=NOW)
    assert (p.kind, p.qty, p.cost, p.sell_price) == ("slow", 1, 30000, 49999) and "expect to wait days" in p.reason
    assert p.days == 1 and p.sure < 0.4  # the sure bar shows the risk
    assert quick_flip(row(), detail(series=slow, ladder=[(48000, 1), (50000, 1)]), held=0, now=NOW) is None  # no gap
    assert quick_flip(row(), detail(series=slow, ladder=lad), held=3, now=NOW) is None  # already holding three days of sales
    cheap = pts([(h, 6, 3000, 0, 1 if h % 6 == 0 else 0) for h in range(48)])
    assert quick_flip(row(), detail(series=cheap, ladder=[(2000, 1), (3000, 1)]), held=0, now=NOW) is None  # 8s: not worth the wait


def test_stormwind_vendor_price_has_the_reputation_discount():
    from wow_invest.tables import vendor_deal, vendor_unit
    assert vendor_unit(3466, 2000, 1, {"S": "Revered"}) == 1700  # Strong Flux: 20s less 15%
    assert vendor_unit(3466, 2000, 1) == 2000 and vendor_unit(3466, 2000, 1, {"S": "Hated"}) == 2000
    assert vendor_unit(1, 2000, 1, {"S": "Revered"}) == 2000  # no vendor of a city sells it: full price
    assert vendor_unit(1, 10001, 5) == 2001
    # the best standing wins, and names the vendor to visit
    assert vendor_deal(3466, {"S": "Friendly", "I": "Exalted"}) == (0.20, "Golnir Bouldertoe, Ironforge")
    assert vendor_deal(3466, {}) == (0, "Edna Mullby, Stormwind City")
    d = detail(ladder=[(19900, 100)], purchase_price=5000)
    p = vendor_flip(row(item_id=3466), d, held=0, now=NOW, rep={"S": "Revered"})
    assert p.max_unit_price == 4250 and "Edna Mullby, Stormwind City (15% off for your reputation)" in p.reason


# four days at 20000 with 48 sales a day; a cheap shelf has sat at 12000 for the last three scans
def shelf(hist=None, last=None):
    return pts([*(last or ((h, 300, 12000, 0, 4) for h in range(3))),
                *(hist or ((h, 300, 20000, 0, 4) for h in range(3, 96)))])


SHELF = [(12000, 40), (12500, 20), (19900, 100)]


def test_buyout_takes_the_whole_shelf_and_relists_under_the_wall():
    p = buyout(row(), detail(series=shelf(), ladder=SHELF), held=0, now=NOW)
    # held price 20000, buys up to 16000: 40×12000 + 20×12500; int(19899×60×0.95) = 1134243
    assert (p.kind, p.qty, p.cost, p.max_unit_price, p.sell_price, p.profit) == ("buyout", 60, 730000, 12500, 19899, 404243)
    assert p.sell_by == NOW + timedelta(days=config.BUYOUT_DAYS)
    assert "all 60 listed up to 1g 25s (38% of the 160 listed)" in p.reason and "≈48 sold/day" in p.reason
    out = plan([p], 10**9, [], False, min_profit=0)
    assert out.paper == [p] and not out.ranked  # a bet: shown under Investments, never bought with your gold


def test_buyout_refused_when_the_shelf_is_too_big_to_resell_in_time():
    # room for floor(48 × 0.5 × 3 days) = 72: buying part of a step leaves cheaper units under our listing
    assert buyout(row(), detail(series=shelf(), ladder=[(12000, 500), (19900, 100)]), held=0, now=NOW) is None
    assert buyout(row(), detail(series=shelf(), ladder=SHELF), held=20, now=NOW) is None  # 52 left, shelf is 60


@pytest.mark.parametrize("series", [
    shelf(hist=((h, 300, 20000, 0, 4) for h in range(3, 60))),  # under three days of history
    # someone bought it out to 40000 three days ago and it was back at 20000 within hours
    shelf(hist=((h, 300, 40000 if 70 <= h < 73 else 20000, 0, 4) for h in range(3, 96))),
    shelf(last=((h, 600, 12000, 0, 4) for h in range(24))),  # flooded: twice the usual amount listed
    shelf(last=((0, 300, 12000, 0, 4), (1, 300, 13000, 0, 4), (2, 300, 14000, 0, 4))),  # still falling
    shelf(last=((h, 300, 12000, 0, 1) for h in range(24)), hist=((h, 300, 20000, 0, 4) for h in range(24, 96))),  # sales fading
])
def test_buyout_refused_when_history_does_not_back_it(series):
    assert buyout(row(), detail(series=series, ladder=SHELF), held=0, now=NOW) is None


def test_buyout_ignores_a_small_lot_and_a_price_jump_that_held():
    assert buyout(row(), detail(series=shelf(), ladder=[(12000, 10), (19900, 100)]), held=0, now=NOW) is None
    # the price doubled two days ago and stayed: that is the new price, not a failed buyout
    rose = shelf(hist=((h, 300, 20000 if h < 48 else 10000, 0, 4) for h in range(3, 96)))
    assert failed_resets(rose, NOW) == 0
    fell_back = shelf(hist=((h, 300, 40000 if 70 <= h < 73 else 20000, 0, 4) for h in range(3, 96)))
    assert failed_resets(fell_back, NOW) == 1


def test_items_with_one_unlimited_vendor_among_limited_ones_count_as_limited():
    # Strong Fishing Pole: the source data calls one vendor unlimited, in game she has one
    assert VENDOR_ITEMS[6365] == 1 and VENDOR_ITEMS[4389] == 1
    d = detail(ladder=[(19900, 100)], purchase_price=901)
    assert vendor_flip(row(item_id=6365), d, held=0, now=NOW) is None
