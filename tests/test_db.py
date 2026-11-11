from datetime import timedelta

from tests.conftest import NOW
from wow_invest import db
from wow_invest.models import Detail, Pick, Row


def row(item_id=1, name="Linen Cloth", min_ask=100, units=10, sold=5, icon="i"):
    return Row(item_id, name, icon, "Common", "Tradeskill", "Cloth", 1, 5,
               min_ask, min_ask, units, 3, sold, None)


def conn():
    return db.connect(":memory:")


def pick(item_id=1, kind="hold", sell_by=None):
    return Pick(kind, item_id, "N", None, None, "alliance", "blackwater", 5, 1000, 250,
                400, 50000, 3.0, 2.0, 0.8, "r", 4.0, 0.1, 7, sell_by=sell_by, funded=True)


def iso(dt):
    return dt.isoformat()


def test_save_scan_is_idempotent_per_captured_at():
    c = conn()
    a = db.save_scan(c, "alliance", iso(NOW), iso(NOW), 0.05, [row()])
    b = db.save_scan(c, "alliance", iso(NOW), iso(NOW), 0.05, [row()])
    assert a == b
    assert c.execute("select count(*) from scans").fetchone()[0] == 1
    assert db.last_captured(c, "alliance") == iso(NOW)
    assert db.last_captured(c, "blackwater") is None


def test_save_scan_keeps_known_name_when_row_has_null():
    c = conn()
    db.save_scan(c, "alliance", iso(NOW - timedelta(hours=1)), iso(NOW), 0.05, [row()])
    db.save_scan(c, "alliance", iso(NOW), iso(NOW), 0.05, [row(name=None, icon=None)])
    r = db.static(c, 1)
    assert r["name"] == "Linen Cloth" and r["icon"] == "i"


def test_rows_at_picks_newest_scan_old_enough():
    c = conn()
    db.save_scan(c, "alliance", iso(NOW - timedelta(hours=30)), iso(NOW), 0.05, [row(min_ask=111)])
    db.save_scan(c, "alliance", iso(NOW - timedelta(hours=1)), iso(NOW), 0.05, [row(min_ask=222)])
    assert db.rows_at(c, "alliance", 24, NOW) == {1: (111, 10, 5)}
    assert db.rows_at(c, "blackwater", 24, NOW) == {}


def test_scan_crowd_and_prune():
    c = conn()
    sid = db.save_scan(c, "alliance", iso(NOW - timedelta(days=40)), iso(NOW), 0.05, [row()])
    db.set_scan_crowd(c, sid, (3, 9))
    assert c.execute("select crowd from scans").fetchone()[0] == "3-9"
    db.save_scan(c, "alliance", iso(NOW), iso(NOW), 0.05, [row()])
    db.prune(c, NOW)
    assert c.execute("select count(*) from scans").fetchone()[0] == 1
    assert c.execute("select count(*) from snapshots").fetchone()[0] == 1


def test_gold_roundtrip_and_default():
    c = conn()
    assert db.get_gold(c) == 0
    db.set_gold(c, 8000000)
    assert db.get_gold(c) == 8000000
    assert db.get_setting(c, "nope", "d") == "d"


def test_holdings_add_sell_and_held_units():
    c = conn()
    a = db.add_holding(c, 1, "flip", 5, 500, 200, NOW, None)
    db.add_holding(c, 1, "hold", 3, 300, 200, NOW, NOW + timedelta(days=3))
    assert db.held_units(c) == {1: 8}
    hs = db.open_holdings(c)
    assert len(hs) == 2 and hs[0].bought_at == NOW and hs[0].sell_by is None
    assert hs[1].sell_by == NOW + timedelta(days=3)
    db.mark_sold(c, a, NOW)
    assert db.held_units(c) == {1: 3}
    assert len(db.open_holdings(c)) == 1


def test_record_picks_skips_item_kind_with_open_pick():
    c = conn()
    assert db.record_picks(c, [pick(1), pick(2)], NOW) == 2
    assert db.record_picks(c, [pick(1)], NOW) == 0
    p = next(r for r in db.open_picks(c) if r["item_id"] == 1)
    db.judge_pick(c, p["id"], True, NOW)
    assert db.record_picks(c, [pick(1)], NOW) == 1


def test_candidates_roundtrip_preserves_datetimes():
    c = conn()
    assert db.load_candidates(c) == ([], None)
    ps = [pick(1, sell_by=NOW), pick(2)]
    db.save_candidates(c, ps, iso(NOW))
    got, at = db.load_candidates(c)
    assert got == ps and at == iso(NOW)
    assert got[0].sell_by == NOW and got[1].sell_by is None and got[0].funded is True


def test_hit_stats_counts_recent_holds_items_and_days():
    c = conn()
    for i in range(25):
        t = NOW + timedelta(days=i // 5, minutes=i)
        db.record_picks(c, [pick(i)], t)
    db.record_picks(c, [pick(99, kind="flip")], NOW)
    for i, p in enumerate(db.open_picks(c)):
        db.judge_pick(c, p["id"], i % 2 == 0, NOW + timedelta(minutes=i))
    s = db.hit_stats(c)
    assert s["judged"] == 26
    assert s["hold_judged"] == 20
    assert s["hold_items"] == 20  # last 20 judged holds, one item each
    assert 1 <= s["hold_days"] <= 5
    assert s["hold_hits"] <= 20 and s["hits"] >= s["hold_hits"]


def test_mark_static_bad_excludes_from_missing_static():
    c = conn()
    db.save_scan(c, "alliance", iso(NOW), iso(NOW), 0.05,
                 [row(1, sold=9), row(2, name="B", sold=50), row(3, name="C", sold=1)])
    assert db.missing_static(c, 10) == [2, 1, 3]
    db.mark_static_bad(c, 2)
    db.save_static(c, Detail(1, "alliance", [], [], 0.05, 40, 1, 20, 12))
    assert db.missing_static(c, 10) == [3]
    r = db.static(c, 1)
    assert r["static_done"] == 1 and r["purchase_price"] == 40 and r["required_level"] == 12


def test_confirmed_vendors():
    c = conn()
    db.confirm_vendor(c, 4)
    db.confirm_vendor(c, 4)
    assert db.confirmed_vendors(c) == {4}
