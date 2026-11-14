"""Fit candidate picks to the user's gold. Pure, no I/O."""
from dataclasses import replace

from wow_invest import config as C
from wow_invest.models import Holding, Pick, Plan


def holds_proven(stats: dict) -> bool:
    j = stats["hold_judged"]
    return (j >= C.PAPER_MIN_JUDGED and stats["hold_hits"] / j >= C.PAPER_MIN_HIT
            and stats["hold_items"] >= C.PROVEN_MIN_ITEMS and stats["hold_days"] >= C.PROVEN_MIN_DAYS)


def best_per_item(picks: list[Pick]) -> list[Pick]:
    best: dict[int, Pick] = {}
    for p in picks:
        if p.item_id not in best or p.score > best[p.item_id].score:
            best[p.item_id] = p
    return list(best.values())  # dict keeps first-seen order


def _top(picks: list[Pick], n: int) -> list[Pick]:
    return sorted(picks, key=lambda p: -p.score)[:n]


def _fit(p: Pick, limit: int, min_profit: int) -> Pick:
    """Funded copy of p if it fits in limit (partially filled if needed), else p unchanged."""
    if p.cost <= limit:
        return replace(p, funded=True)
    if p.kind == "flip":  # part of a quick flip leaves cheaper units of the same ladder under our listing
        return p
    # linear scaling over a cheapest-first ladder overstates cost, so the estimate errs safe
    q = p.qty * limit // p.cost if p.cost > 0 else 0
    profit = int(p.profit * q / p.qty) if p.qty else 0
    if q == 0 or profit < min_profit:
        return p
    return replace(p, qty=q, cost=int(p.cost * q / p.qty), profit=profit, funded=True)


def plan(candidates: list[Pick], gold: int, holdings: list[Holding], proven: bool,
         min_profit: int | None = None, min_unit: int | None = None, top: int | None = None) -> Plan:
    min_profit = C.MIN_PROFIT if min_profit is None else min_profit
    min_unit = C.MIN_UNIT_PROFIT if min_unit is None else min_unit
    cands = [p for p in candidates if p.profit >= min_profit and p.profit >= min_unit * p.qty]
    is_paper = lambda p: p.kind in ("rise", "trend", "buyout") or (p.kind == "hold" and not proven)  # bets are never funded
    pool = best_per_item([p for p in cands if not is_paper(p)])
    # kept apart from the buys, so a multi-day bet on an item never hides (or is hidden by) a flip of it
    paper = best_per_item([p for p in cands if is_paper(p)])
    top = C.TOP_N if top is None else top
    ranked, paper = _top(pool, top), _top(paper, top)

    open_hold = sum(h.price_paid for h in holdings if h.kind == "hold" and h.sold_at is None)
    hold_room = C.HOLD_MAX_SHARE * (gold + open_hold) - open_hold
    remaining, out = gold, []
    for p in ranked:
        limit = remaining
        if p.kind == "hold" and proven:
            limit = min(remaining, int(hold_room))
        f = _fit(p, max(limit, 0), min_profit)
        if f.funded:
            remaining -= f.cost
            if p.kind == "hold":
                hold_room -= f.cost
        out.append(f)
    funded = [p for p in out if p.funded]
    return Plan(out, paper, sum(p.cost for p in funded), sum(p.profit for p in funded))
