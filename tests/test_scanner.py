import logging
import threading
from collections import Counter
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.conftest import NOW, pts, steady
from wow_invest import config, db, scanner
from wow_invest.api import ApiError
from wow_invest.models import Detail, Row
from wow_invest.scanner import run_cycle

CUT = {"alliance": 0.05, "blackwater": 0.15}
WALL = [(12000, 10), (15000, 5), (19900, 100)]
FLIP = 100001


def row(item_id, min_ask=20000, sold=20, units=300):
    return Row(item_id=item_id, name=None, icon=None, quality=None, class_name=None, subclass_name=None,
               required_level=None, item_level=None, min_ask=min_ask, avg_ask=None, units=units,
               listings=1, sold_24h=sold, vendor_sell=None)


def detail(item_id, series=steady, ladder=(), purchase_price=0):
    return Detail(item_id=item_id, house="alliance", ladder=list(ladder), series=series, cut=0.0,
                  purchase_price=purchase_price, purchase_quantity=1, sell_price=0, required_level=None)


class FakeClient:
    """Fixture site. `fail` holds houses (all_items raises), item ids, or "details" (every detail raises)."""

    def __init__(self, alliance, blackwater=(), deals=(), details=None, fail=()):
        self.rows = {"alliance": list(alliance), "blackwater": list(blackwater)}
        self.deal_ids, self.details, self.fail = list(deals), details or {}, set(fail)
        self.times = {h: (NOW.isoformat(), NOW.isoformat()) for h in CUT}
        self.calls, self.fetched = Counter(), []

    def scan_times(self):
        self.calls["scan_times"] += 1
        return dict(self.times)

    def all_items(self, house):
        self.calls["all_items"] += 1
        if house in self.fail:
            raise ApiError(house)
        return self.rows[house]

    def deals(self, house, limit=100):
        self.calls["deals"] += 1
        return (self.deal_ids if house == "alliance" else []), CUT[house]

    def item_detail(self, house, item_id, cut):
        self.calls["item_detail"] += 1
        self.fetched.append((house, item_id))
        if item_id in self.fail or "details" in self.fail:
            raise ApiError(item_id)
        return replace(self.details.get((house, item_id), detail(item_id)), house=house, cut=cut)


def flip_client(**kw):
    return FakeClient([row(FLIP)], deals=[FLIP], details={("alliance", FLIP): detail(FLIP, ladder=WALL)}, **kw)


def recorded(conn):
    return [tuple(r) for r in conn.execute("SELECT kind, item_id FROM picks ORDER BY id")]


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def new_scan(fc, at, auctions_age_h=0):
    fc.times = {h: (at.isoformat(), (at - timedelta(hours=auctions_age_h)).isoformat()) for h in CUT}


def test_no_change_when_captured_at_unchanged():
    conn, fc = db.connect(":memory:"), flip_client()
    assert run_cycle(fc, conn, NOW) == "done"
    calls = fc.calls["all_items"]
    assert run_cycle(fc, conn, NOW) == "no-change"
    assert fc.calls["all_items"] == calls


def test_error_before_save_leaves_db_untouched():
    conn = db.connect(":memory:")
    assert run_cycle(flip_client(fail={"blackwater"}), conn, NOW) == "error"
    assert count(conn, "scans") == 0 and count(conn, "items") == 0


def test_missing_house_is_an_error():
    conn, fc = db.connect(":memory:"), flip_client()
    del fc.times["blackwater"]
    assert run_cycle(fc, conn, NOW) == "error"
    assert fc.calls["all_items"] == 0 and count(conn, "scans") == 0


def test_done_saves_candidates_and_records_picks():
    conn, fc = db.connect(":memory:"), flip_client()
    assert run_cycle(fc, conn, NOW) == "done"
    picks, captured = db.load_candidates(conn)
    assert [(p.kind, p.item_id) for p in picks] == [("flip", FLIP)] and captured == NOW.isoformat()
    assert recorded(conn) == [("flip", FLIP)]
    assert fc.fetched.count(("alliance", FLIP)) == 1  # static backfill and the flip share one fetch
    assert db.static(conn, FLIP)["static_done"] == 1
    assert db.get_setting(conn, "stale") == "0" and db.get_setting(conn, "crowd") == "20-30"
    assert conn.execute("SELECT crowd FROM scans WHERE house='alliance'").fetchone()[0] == "20-30"


def test_detail_quotas_leave_room_for_every_kind(monkeypatch):
    monkeypatch.setattr(config, "STATIC_PER_CYCLE", 0)  # count only candidate fetches
    deals, cross = list(range(100001, 100201)), list(range(200001, 200031))
    fc = FakeClient([row(i) for i in deals + cross], blackwater=[row(i, min_ask=10000) for i in cross],
                    deals=deals)
    assert run_cycle(fc, db.connect(":memory:"), NOW) == "done"
    assert len({i for _, i in fc.fetched if i in deals}) == config.DETAIL_QUOTAS["flip"]
    bw = {i for h, i in fc.fetched if h == "blackwater"}
    assert len(bw) == config.DETAIL_QUOTAS["cross"] and bw <= set(cross)
    assert {i for h, i in fc.fetched if h == "alliance" and i in cross} == bw
    assert len(fc.fetched) == len(set(fc.fetched))  # no detail fetched twice


def test_cross_candidates_buy_on_the_cheaper_house():
    to_a, to_b = 100001, 100002  # cheaper on Blackwater, cheaper on Alliance
    # buy-house rows sell nothing: the pre-screen looks only at the house the item sells on
    fc = FakeClient([row(to_a, min_ask=20000), row(to_b, min_ask=10000, sold=0)],
                    blackwater=[row(to_a, min_ask=10000, sold=0), row(to_b, min_ask=20000)],
                    details={("blackwater", to_a): detail(to_a, ladder=[(12000, 10)]),
                             ("alliance", to_b): detail(to_b, ladder=[(12000, 10)])})
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    p = {p.item_id: p for p in db.load_candidates(conn)[0]}
    assert (p[to_a].kind, p[to_a].buy_at, p[to_a].sell_at) == ("cross", "blackwater", "alliance")
    assert (p[to_b].kind, p[to_b].buy_at, p[to_b].sell_at) == ("cross", "alliance", "blackwater")
    assert p[to_b].profit == int(199990 * 0.85) - 120000  # sells under the Blackwater cut from deals


@pytest.mark.parametrize("units, picked", [(1500, True), (1100, False)])
def test_flooded_item_in_the_crowd_band_becomes_a_paper_hold(units, picked):
    herb = 785  # Mageroyal, band (10, 25) overlaps the default crowd (20, 30)
    conn = db.connect(":memory:")
    day_ago = (NOW - timedelta(hours=25)).isoformat()
    db.save_scan(conn, "alliance", day_ago, day_ago, 0.05, [row(herb, units=1000)])
    flood = pts([(h, 1500, 60000, 0, 4) for h in range(24)] + [(h, 1000, 100000, 0, 4) for h in range(24, 48)])
    fc = FakeClient([row(herb, min_ask=60000, units=units)],
                    details={("alliance", herb): detail(herb, series=flood, ladder=[(60000, 100)])})
    assert run_cycle(fc, conn, NOW) == "done"
    want = [("hold", herb)] if picked else []
    assert [(p.kind, p.item_id) for p in db.load_candidates(conn)[0]] == want
    assert recorded(conn) == want


def test_stale_site_data_saves_scan_but_no_new_picks():
    conn, fc = db.connect(":memory:"), flip_client()
    assert run_cycle(fc, conn, NOW) == "done"
    before, fetches = db.get_setting(conn, "candidates"), fc.calls["item_detail"]
    later = NOW + timedelta(hours=1)
    new_scan(fc, later, auctions_age_h=3)
    assert run_cycle(fc, conn, later) == "stale"
    assert count(conn, "scans") == 4 and db.get_setting(conn, "stale") == "1"
    assert db.get_setting(conn, "candidates") == before and fc.calls["item_detail"] == fetches


def test_one_bad_item_is_skipped_not_fatal():
    ids = list(range(100002, 100008))  # backfilled by sales: bad, good, bad, good, bad, good
    conn = db.connect(":memory:")
    fc = FakeClient([row(i, sold=100 - k) for k, i in enumerate(ids)], fail=set(ids[::2]))
    assert run_cycle(fc, conn, NOW) == "done"  # each success resets the error streak
    assert [db.static(conn, i)["static_done"] for i in ids] == [-1, 1, -1, 1, -1, 1]


def test_slow_sellers_are_not_candidates():
    fc = FakeClient([row(FLIP, sold=9)], deals=[FLIP], details={("alliance", FLIP): detail(FLIP, ladder=WALL)})
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    assert db.load_candidates(conn)[0] == []


def test_outage_ends_cycle_marks_nothing_and_keeps_candidates():
    conn, fc = db.connect(":memory:"), flip_client()
    assert run_cycle(fc, conn, NOW) == "done"
    before, fetches = db.get_setting(conn, "candidates"), fc.calls["item_detail"]
    fc.rows["alliance"] += [row(i) for i in range(100002, 100008)]
    fc.fail.add("details")
    later = NOW + timedelta(hours=1)
    new_scan(fc, later)
    assert run_cycle(fc, conn, later) == "error"
    assert fc.calls["item_detail"] - fetches == config.MAX_ERRORS_IN_ROW
    assert count(conn, "scans") == 4  # data already committed stays
    assert conn.execute("SELECT count(*) FROM items WHERE static_done=-1").fetchone()[0] == 0
    assert db.get_setting(conn, "candidates") == before


def test_records_only_showable_picks():
    small, check = 100002, 100003
    fc = FakeClient([row(i) for i in (FLIP, small, check)], deals=[FLIP, small, check],
                    details={("alliance", FLIP): detail(FLIP, ladder=WALL),
                             ("alliance", small): detail(small, ladder=[(15000, 3)]),  # about 1.2g profit
                             ("alliance", check): detail(check, purchase_price=5000)})  # vendor_check
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    picks = db.load_candidates(conn)[0]
    # `check` is a deal too, but as a vendor candidate it is not a flip candidate as well
    assert sorted((p.kind, p.item_id) for p in picks) == [("flip", FLIP), ("flip", small), ("vendor_check", check)]
    assert [p.score for p in picks] == sorted((p.score for p in picks), reverse=True)
    assert recorded(conn) == [("flip", FLIP)]


def test_loop_logs_errors_and_keeps_going(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    stop, seen, made = threading.Event(), [], []

    def cycle(client, conn, now):
        seen.append(client)
        if len(seen) == 1:
            raise RuntimeError("boom")
        stop.set()
        return "done"

    monkeypatch.setattr(scanner, "run_cycle", cycle)
    monkeypatch.setattr(config, "POLL_MINUTES", 0)
    scanner.loop(lambda: made.append(1) or "client", str(tmp_path / "w.db"), stop)
    assert seen == ["client", "client"] and made == [1]
    assert "RuntimeError: boom" in caplog.text and "done" in caplog.text
