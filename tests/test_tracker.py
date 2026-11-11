from datetime import timedelta

from tests.conftest import NOW
from wow_invest import db
from wow_invest.models import Pick, Row
from wow_invest.tracker import judge

SELL_BY = NOW + timedelta(days=2)


def make(conn, asks, house="Alliance", target=20000, sell_at="Alliance"):
    p = Pick("hold", 1, "x", None, None, "Blackwater", sell_at, 1, 100, 100, target, 10, 1, 1, 1,
             "", 1, None, 0, SELL_BY)
    db.record_picks(conn, [p], NOW)
    for i, a in enumerate(asks):
        row = Row(1, "x", None, None, None, None, None, None, a, a, 1, 1, 1, None)
        db.save_scan(conn, house, (NOW + timedelta(hours=i + 1)).isoformat(), None, 0.05,
                     [row] if a is not None else [])


def hit(conn):
    return conn.execute("SELECT judged_at, hit FROM picks").fetchone()


def run(asks, now=SELL_BY + timedelta(hours=1), **kw):
    conn = db.connect(":memory:")
    make(conn, asks, **kw)
    n = judge(conn, now)
    return n, hit(conn)


def test_hit_needs_three_consecutive_snapshots_at_target():
    n, r = run([18000, 20500, 20600, 20100])
    assert n == 1 and r["hit"] == 1


def test_single_spike_is_a_miss():
    n, r = run([18000, 20500, 18000, 20500, 18000])
    assert n == 1 and r["hit"] == 0


def test_miss_when_sell_by_passes():
    assert run([1000, 2000, 3000])[1]["hit"] == 0


def test_not_judged_before_sell_by_even_if_reached():
    n, r = run([20500] * 3, now=SELL_BY - timedelta(hours=1))
    assert n == 0 and r["judged_at"] is None


def test_only_snapshots_of_sell_house_count():
    assert run([20500] * 3, house="Blackwater")[1]["hit"] == 0


def test_missing_listing_breaks_run():
    assert run([20500, 20500, None, 20500])[1]["hit"] == 0
