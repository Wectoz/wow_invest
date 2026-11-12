import logging
import sqlite3
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
from wow_invest.tables import VENDOR_ITEMS

CUT = {"alliance": 0.05, "blackwater": 0.15}
WALL = [(12000, 10), (15000, 5), (19900, 100)]
FLIP = 100001


def row(item_id, min_ask=20000, sold=20, units=300):
    return Row(item_id=item_id, name=None, icon=None, quality=None, class_name=None, subclass_name=None,
               required_level=None, item_level=None, min_ask=min_ask, avg_ask=None, units=units,
               listings=1, sold_24h=sold, vendor_sell=None)


def detail(item_id, series=steady, ladder=(), purchase_price=0, purchase_quantity=1):
    return Detail(item_id=item_id, house="alliance", ladder=list(ladder), series=series, cut=0.0,
                  purchase_price=purchase_price, purchase_quantity=purchase_quantity, sell_price=0,
                  required_level=None)


class FakeClient:
    """Fixture site. `fail` raises a transient ApiError for: "scan_times", "deals", a house (its
    all_items), an item id, or "details" (every detail). Item ids in `gone` raise a permanent one."""

    def __init__(self, alliance, blackwater=(), deals=(), details=None, fail=(), gone=()):
        self.rows = {"alliance": list(alliance), "blackwater": list(blackwater)}
        self.deal_ids, self.details, self.fail, self.gone = list(deals), details or {}, set(fail), set(gone)
        self.times = {h: (NOW.isoformat(), NOW.isoformat()) for h in CUT}
        self.calls, self.fetched = Counter(), []

    def scan_times(self):
        self.calls["scan_times"] += 1
        if "scan_times" in self.fail:
            raise ApiError("markets")
        return dict(self.times)

    def all_items(self, house):
        self.calls["all_items"] += 1
        if house in self.fail:
            raise ApiError(house)
        return self.rows[house]

    def deals(self, house, limit=100):
        self.calls["deals"] += 1
        if "deals" in self.fail:
            raise ApiError("deals")
        return (self.deal_ids if house == "alliance" else []), CUT[house]

    def item_detail(self, house, item_id, cut):
        self.calls["item_detail"] += 1
        self.fetched.append((house, item_id))
        if item_id in self.gone:
            raise ApiError(item_id, transient=False)
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


@pytest.mark.parametrize("failing", ["blackwater", "deals", "scan_times"])
def test_error_before_save_leaves_db_untouched(failing):
    conn = db.connect(":memory:")
    assert run_cycle(flip_client(fail={failing}), conn, NOW) == "error"
    assert count(conn, "scans") == 0 and count(conn, "items") == 0


@pytest.mark.parametrize("auctions_at", ["missing house", "garbage", None, "2026-11-11T12:00:00"])
def test_bad_markets_payload_is_an_error_before_any_list(auctions_at):
    conn, fc = db.connect(":memory:"), flip_client()
    fc.times["alliance"] = (NOW.isoformat(), (NOW - timedelta(hours=3)).isoformat())  # stale: Blackwater still checked
    if auctions_at == "missing house":
        del fc.times["blackwater"]
    else:
        fc.times["blackwater"] = (NOW.isoformat(), auctions_at)
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
def test_flooded_item_in_the_crowd_band_becomes_a_paper_hold(units, picked, monkeypatch):
    herb = 785  # Mageroyal, band (10, 25) overlaps the default crowd (20, 30)
    monkeypatch.delitem(VENDOR_ITEMS, herb, raising=False)  # a herb vendor stocks it; not this test's subject
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
    # a transient failure is retried next cycle, never marked bad
    assert [db.static(conn, i)["static_done"] for i in ids] == [0, 1, 0, 1, 0, 1]


def test_permanent_errors_do_not_block_cycle():
    gone = [100002, 100003, 100004]
    conn, fc = db.connect(":memory:"), flip_client(gone=gone)
    fc.rows["alliance"] += [row(i, sold=50) for i in gone]  # backfilled first, all in a row
    assert run_cycle(fc, conn, NOW) == "done"
    assert [db.static(conn, i)["static_done"] for i in gone] == [-1, -1, -1]
    assert [(p.kind, p.item_id) for p in db.load_candidates(conn)[0]] == [("flip", FLIP)]


def test_isolated_candidate_fetch_error_skips_that_candidate(monkeypatch):
    monkeypatch.setattr(config, "STATIC_PER_CYCLE", 0)  # the error happens in the candidate step
    other = 100002
    fc = FakeClient([row(FLIP), row(other, sold=50)], deals=[FLIP, other], fail={other},
                    details={("alliance", i): detail(i, ladder=WALL) for i in (FLIP, other)})
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    assert [(p.kind, p.item_id) for p in db.load_candidates(conn)[0]] == [("flip", FLIP)]


def test_vendor_candidate_still_gets_quick_flip(monkeypatch):
    # not a deal: a candidate only because a vendor sells it
    item = 100002
    monkeypatch.setitem(VENDOR_ITEMS, item, None)
    fc = FakeClient([row(item)], details={
        ("alliance", item): detail(item, ladder=WALL, purchase_price=25000, purchase_quantity=5)})
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    assert sorted(p.kind for p in db.load_candidates(conn)[0]) == ["flip", "vendor"]


def test_builder_crash_skips_only_that_candidate(monkeypatch, caplog):
    other, real = 100002, scanner.quick_flip

    def flaky(r, d, held, now):
        if r.item_id == other:
            raise ValueError("bad data")
        return real(r, d, held, now)

    monkeypatch.setattr(scanner, "quick_flip", flaky)
    fc = FakeClient([row(FLIP), row(other, sold=50)], deals=[FLIP, other],
                    details={("alliance", i): detail(i, ladder=WALL) for i in (FLIP, other)})
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    assert [(p.kind, p.item_id) for p in db.load_candidates(conn)[0]] == [("flip", FLIP)]
    assert "ValueError: bad data" in caplog.text


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


def test_records_only_showable_picks(monkeypatch):
    small, check = 100002, 100003
    monkeypatch.setitem(VENDOR_ITEMS, check, None)
    fc = FakeClient([row(i) for i in (FLIP, small, check)], deals=[FLIP, small, check],
                    details={("alliance", FLIP): detail(FLIP, ladder=WALL),
                             ("alliance", small): detail(small, ladder=[(15000, 3)]),  # about 1.2g profit
                             ("alliance", check): detail(check, purchase_price=5000)})  # vendor flip
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    picks = db.load_candidates(conn)[0]
    # `check` is a deal too, but it is chosen once (as a vendor candidate), so its vendor flip is not doubled
    assert sorted((p.kind, p.item_id) for p in picks) == [("flip", FLIP), ("flip", small), ("vendor", check)]
    assert [p.score for p in picks] == sorted((p.score for p in picks), reverse=True)
    assert sorted(recorded(conn)) == [("flip", FLIP), ("vendor", check)]  # `small` is under the minimum profit


class FakeStop:
    """Records each wait; is set after `n` waits."""

    def __init__(self, n):
        self.n, self.waits = n, []

    def is_set(self):
        return len(self.waits) >= self.n

    def wait(self, timeout):
        self.waits.append(timeout)


def test_loop_doubles_wait_after_errors_up_to_an_hour_and_resets(monkeypatch, tmp_path):
    results = iter(["error", "error", "error", "error", "error", "no-change", "error", "done"])
    monkeypatch.setattr(scanner, "run_cycle", lambda client, conn, now: next(results))
    stop = FakeStop(8)
    scanner.loop(lambda: "client", str(tmp_path / "w.db"), stop)
    assert stop.waits == [m * 60 for m in (10, 20, 40, 60, 60, 5, 10, 5)]


def test_loop_retries_db_connect_after_the_wait(monkeypatch, tmp_path, caplog):
    real, tries = db.connect, []

    def connect(path):
        tries.append(path)
        if len(tries) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(path)

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(scanner, "run_cycle", lambda client, conn, now: "done")
    stop = FakeStop(3)
    scanner.loop(lambda: "client", str(tmp_path / "w.db"), stop)
    assert len(tries) == 2  # failed once, then connected and kept the connection
    assert stop.waits == [600, 300, 300] and "OperationalError: disk I/O error" in caplog.text


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


def test_refresh_forces_a_cycle_unless_one_is_running(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(scanner, "run_cycle", lambda client, conn, now, force=False: calls.append(force) or "done")
    with scanner.busy:
        scanner.refresh(str(tmp_path / "w.db"), lambda: "client")
    assert calls == []
    scanner.refresh(str(tmp_path / "w.db"), lambda: "client")
    assert calls == [True] and not scanner.busy.locked()


def test_forced_cycle_rebuilds_picks_from_an_unchanged_scan():
    fc = FakeClient([row(FLIP)], deals=[FLIP], details={("alliance", FLIP): detail(FLIP, ladder=WALL)})
    conn = db.connect(":memory:")
    assert run_cycle(fc, conn, NOW) == "done"
    assert run_cycle(fc, conn, NOW) == "no-change"
    db.set_setting(conn, "candidates", "")
    assert run_cycle(fc, conn, NOW, force=True) == "done"
    assert [(p.kind, p.item_id) for p in db.load_candidates(conn)[0]] == [("flip", FLIP)]
    assert conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 2  # no duplicate scan rows
