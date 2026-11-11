import math

import pytest

from tests.conftest import NOW, pts
from wow_invest.pricing import (change, days_to_sell, deposit, going_price,
                                ladder_buy, max_units, net_proceeds, score)


def test_ladder_buy_takes_cheapest_up_to_price_and_qty():
    assert ladder_buy([(15000, 38), (19700, 20), (19800, 155)], 19700, 50) == (50, 806400, 19700)
def test_ladder_buy_nothing_cheap_enough():
    assert ladder_buy([(20000, 5)], 19999, 10) == (0, 0, 0)
def test_going_price_median_of_last_24h_skips_nulls_and_old():
    s = pts([(1,0,100,0,0),(2,0,None,0,0),(3,0,300,0,0),(5,0,200,0,0),(30,0,9999,0,0)])
    assert going_price(s, NOW) == 200
def test_change_compares_last_6h_to_24_48h():
    s = pts([(1,0,80,0,0),(3,0,80,0,0),(30,0,100,0,0),(40,0,100,0,0)])
    assert change(s, NOW, "min_ask") == pytest.approx(-0.2)
def test_change_none_without_old_window():
    assert change(pts([(1,0,80,0,0)]), NOW, "min_ask") is None
def test_sales_per_day_weights_sold_or_cancelled_half():
    from wow_invest.pricing import sales_per_day
    s = pts([(1,0,1,4,10),(5,0,1,6,10),(30,0,1,100,100)])
    assert sales_per_day(s, NOW) == 20.0
def test_sales_net_relisted_units_out_of_early_disappearances():
    from wow_invest.pricing import sales_per_day, sales_per_day_window
    # 4 + 0.5×(10−6) = 6; 6 + 0.5×max(0, 10−12) = 6 → 12 a day; the 30h point is outside
    s = pts([(1,0,1,4,10,6),(5,0,1,6,10,12),(30,0,1,100,100,0)])
    assert sales_per_day(s, NOW) == 12.0
    assert sales_per_day_window(s, NOW, 0, 12) == 24.0
def test_listed_units_is_median_of_last_24h():
    from tests.conftest import steady
    from wow_invest.pricing import listed_units
    assert listed_units(pts([(1,10,1,0,0),(5,31,1,0,0),(30,999,1,0,0)]), NOW) == 20  # median 20.5
    assert listed_units(steady, NOW) == 300 and listed_units([], NOW) == 0
def test_steady_fixture_is_48_sales_a_day():
    from tests.conftest import steady
    from wow_invest.pricing import sales_per_day, sales_per_day_window, history_hours, sureness
    assert sales_per_day(steady, NOW) == 48.0
    assert sales_per_day_window(steady, NOW, 24, 48) == 48.0
    assert sales_per_day_window(steady, NOW, 0, 12) == 48.0
    assert history_hours(steady, NOW) == 47
    assert sureness(48, 20) == 1.0
def test_net_proceeds_after_cut_and_deposit():
    assert net_proceeds(10000, 10, 0.05, 50) == 94500
def test_days_to_sell_and_zero_sales():
    assert days_to_sell(10, 10.0) == 2.0
    assert days_to_sell(10, 0.0) == math.inf
def test_max_units_minus_held_never_negative():
    assert max_units(20.0, 5) == 20
    assert max_units(2.0, 50) == 0
def test_score_is_return_per_day():
    assert score(50, 100, 2.0) == 0.25
    assert score(50, 0, 2.0) == 0.0
def test_deposit_uses_house_rate_and_handles_none():
    assert deposit(1000, "alliance") == 150
    assert deposit(None, "blackwater") == 0
def test_undercut_floor_one():
    from wow_invest.pricing import undercut
    assert undercut(100) == 99 and undercut(1) == 1
