from dataclasses import replace

from tests.conftest import NOW
from wow_invest.models import Holding, Pick
from wow_invest.planner import best_per_item, holds_proven, plan

G = 10_000
_n = [0]


def pk(kind="flip", item_id=None, qty=1, cost=100 * G, profit=50 * G, score=1.0):
    _n[0] += 1
    return Pick(kind, item_id or _n[0], "x", None, None, "alliance", "blackwater", qty, cost,
                cost // qty, 0, profit, 1.0, score, 1.0, "", 1.0, None, 0)


def held(cost, kind="hold", sold=None):
    return Holding(1, 99, kind, 1, cost, 0, NOW, None, sold)


def test_drops_picks_under_3g():
    out = plan([pk(profit=29_999), pk(profit=30_000)], 10**9, [], False)
    assert len(out.ranked) == 1


def test_keeps_top_15_by_score():
    out = plan([pk(score=i) for i in range(20)], 0, [], False)
    assert [p.score for p in out.ranked] == list(range(19, 4, -1))


def test_funds_top_down_and_partially_fills_next():
    a, b = pk(cost=500_000, profit=200_000, score=2.0), pk(qty=10, cost=1_000_000, profit=400_000, score=1.0)
    out = plan([a, b], gold=1_000_000, holdings=[], proven=False)
    assert [p.funded for p in out.ranked] == [True, True]
    assert (out.ranked[1].qty, out.ranked[1].cost, out.ranked[1].profit) == (5, 500_000, 200_000)
    assert out.spend == 1_000_000 and out.profit == 400_000
    assert b.qty == 10 and not b.funded  # input untouched


def test_partial_fill_below_min_profit_stays_unfunded():
    b = pk(qty=10, cost=1_000_000, profit=400_000)
    out = plan([b], 200_000, [], False)  # 2 units -> profit 80000 >= MIN, funded
    assert out.ranked[0].funded
    out = plan([b], 50_000, [], False)  # 0 units
    p = out.ranked[0]
    assert not p.funded and (p.qty, p.cost, p.profit) == (10, 1_000_000, 400_000)
    b2 = pk(qty=10, cost=1_000_000, profit=40_000)
    out = plan([b2], 500_000, [], False)  # 5 units -> 20000 < MIN
    assert not out.ranked[0].funded and out.ranked[0].qty == 10 and out.spend == 0


def test_zero_gold_lists_everything_unfunded():
    out = plan([pk(), pk()], 0, [], False)
    assert len(out.ranked) == 2 and not any(p.funded for p in out.ranked) and out.spend == 0


def test_holds_go_to_paper_until_proven():
    h = pk("hold")
    out = plan([h, pk()], 10**9, [], False)
    assert [p.kind for p in out.ranked] == ["flip"] and [p.kind for p in out.paper] == ["hold"]
    assert not out.paper[0].funded
    out = plan([h], 10**9, [], True)
    assert out.ranked[0].funded and not out.paper


def test_proven_holds_capped_at_quarter_including_open_holds():
    h = pk("hold", qty=10, cost=500 * G, profit=300 * G)
    out = plan([h], 1000 * G, [held(200 * G), held(999 * G, sold=NOW), held(50 * G, "flip")], True)
    p = out.ranked[0]
    assert p.funded and p.cost == 100 * G and p.qty == 2
    assert out.spend == 100 * G


def test_vendor_check_never_funded():
    out = plan([pk("vendor_check")], 10**9, [], True)
    assert not out.ranked and len(out.checks) == 1 and not out.checks[0].funded


def test_holds_proven_thresholds():
    ok = dict(judged=0, hits=0, hold_judged=10, hold_hits=7, hold_items=5, hold_days=3)
    assert holds_proven(ok)
    assert not holds_proven({**ok, "hold_judged": 9, "hold_hits": 9})
    assert not holds_proven({**ok, "hold_hits": 6})
    assert not holds_proven({**ok, "hold_items": 4})
    assert not holds_proven({**ok, "hold_days": 2})


def test_best_per_item_keeps_highest_score():
    a, b, c = pk("flip", 5, score=1.0), pk("cross", 5, score=2.0), pk("flip", 6, score=0.1)
    assert best_per_item([a, b, c]) == [b, c]
    t1, t2 = pk(item_id=7, score=1.0), pk("cross", 7, score=1.0)
    assert best_per_item([t1, t2]) == [t1]


def test_paper_hold_does_not_hide_flip_for_same_item():
    out = plan([pk("hold", 5, score=9.0), pk("flip", 5, score=1.0)], 10**9, [], False)
    assert [p.kind for p in out.ranked] == ["flip"] and not out.paper


def test_vendor_check_does_not_hide_flip_for_same_item():
    out = plan([pk("vendor_check", 5, score=9.0), pk("flip", 5, score=1.0)], 10**9, [], False)
    assert [p.kind for p in out.ranked] == ["flip"] and not out.checks


def test_exhausted_hold_cap_still_funds_later_flips():
    h, f = pk("hold", cost=100 * G, profit=50 * G, score=2.0), pk("flip", cost=100 * G, score=1.0)
    out = plan([h, f], 600 * G, [held(200 * G)], True)  # cap 0.25*800=200 -> room 0
    assert [p.funded for p in out.ranked] == [False, True]
