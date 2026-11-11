from wow_invest.crowd import estimate_crowd, item_band, overlaps
from wow_invest.models import Row
from wow_invest.tables import GATHER_BANDS


def row(cls="Armor", lvl=20, sold=0):
    return Row(item_id=1, name=None, icon=None, quality=None, class_name=cls, subclass_name=None,
               required_level=lvl, item_level=None, min_ask=None, avg_ask=None, units=0, listings=0,
               sold_24h=sold, vendor_sell=None)


def test_crowd_weighted_quartiles():
    rows = [row("Armor", 22, 100), row("Weapon", 25, 100), row("Armor", 28, 100), row("Armor", 40, 10)]
    assert estimate_crowd(rows) == (22, 28)


def test_crowd_falls_back_to_prior_when_few_sales():
    assert estimate_crowd([row("Armor", 50, 5)]) == (20, 30)


def test_crowd_ignores_non_equipment():
    rows = [row("Armor", 22, 100), row("Weapon", 25, 100), row("Armor", 28, 100),
            row("Trade Goods", 55, 1000), row("Armor", 0, 1000)]
    assert estimate_crowd(rows) == (22, 28)


def test_item_band_table_then_required_level_then_none():
    assert item_band(785, None) == (10, 25)
    assert item_band(1, 30) == (28, 33)
    assert item_band(1, 0) is None
    assert item_band(1, None) is None


def test_overlaps_inclusive():
    assert overlaps((10, 25), (25, 30)) and not overlaps((10, 24), (25, 30))


def test_gather_bands_are_sane():
    assert all(1 <= lo < hi <= 60 for lo, hi in GATHER_BANDS.values())
