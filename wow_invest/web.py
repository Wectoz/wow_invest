"""Page rendering and the localhost HTTP routes."""
import json
import logging
import math
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urlsplit

from . import config as C
from . import db, scanner
from .planner import holds_proven, plan
from .pricing import undercut
from .tables import ITEM_INFO, PROFESSIONS, VENDOR_ITEMS

HERE = Path(__file__).parent
ICON_HOST = "https://render.worldofwarcraft.com/"
MAX_BODY = 10_240
MAX_GOLD = 10_000_000
KINDS = {"flip": ("Quick flip", "flip"), "cross": ("Cross-AH", "flip"), "hold": ("Hold", "hold"),
         "vendor": ("Vendor flip", "vendor"), "craft": ("Crafting", "vendor"), "rise": ("Forecast", "hold"),
         "trend": ("Rising price", "hold"), "buyout": ("Buyout", "hold"), "slow": ("Slow seller", "flip")}
# item type -> (label, the site's class names it covers; None = whatever the others leave)
TYPES = {"gear": ("Gear", {"Armor", "Weapon"}), "mats": ("Materials", {"Trade Goods", "Reagent"}),
         "consumables": ("Consumables", {"Consumable"}), "recipes": ("Recipes", {"Recipe"}), "other": ("Other", None)}
# how the buy list can be ordered; gold is always spent in best-return order
SORTS = {"return": ("Best return", lambda p: -p.score), "profit": ("Most gold", lambda p: -p.profit),
         "fast": ("Fastest", lambda p: p.days)}
# tab -> (label, the pick kinds it shows; None = all)
TABS = {"all": ("All", None), "ah": ("AH flips", {"flip", "cross", "hold"}),
        "slow": ("Slow sellers", {"slow"}),  # underpriced, but they sell under five a day
        "vendor": ("Vendor flips", {"vendor"}), "craft": ("Crafting", {"craft"}),
        "invest": ("Investments", {"hold", "rise", "trend"}),  # multi-day bets: these two also list the paper picks
        "buyout": ("Buyouts", {"buyout"})}
PAPER_TABS = ("invest", "buyout")
QUALITY = {"uncommon": "green", "rare": "blue", "epic": "purple"}
log = logging.getLogger(__name__)


def esc(x) -> str:
    return escape(str(x), quote=True)


def _parts(copper: int) -> tuple[int, int, int]:
    return copper // 10000, copper // 100 % 100, copper % 100


def fmt_money(copper: int) -> str:
    g, s, c = _parts(copper)
    out = [f'<span class="{k}">{v}{k}</span>' for k, v in (("g", g), ("s", s), ("c", c)) if v]
    return " ".join(out) or "0c"


def plain_gold(copper: int) -> str:
    g, s, c = _parts(copper)
    return " ".join(f"{v}{k}" for k, v in (("g", g), ("s", s), ("c", c)) if v) or "0g"


def parse_gold(text: str) -> int:
    t = (text or "").strip().lower()
    if m := re.fullmatch(r"(\d[\d ]*(?:\.\d+)?)\s*g?", t):
        try:
            copper = int(Decimal(m[1].replace(" ", "")) * 10000)
        except InvalidOperation:
            raise ValueError("Gold must be a number, like 800 or 800g 50s") from None
    elif (m := re.fullmatch(r"(?:(\d+)\s*g)?\s*(?:(\d+)\s*s)?\s*(?:(\d+)\s*c)?", t)) and t:
        g, s, c = (int(x or 0) for x in m.groups())
        copper = g * 10000 + s * 100 + c
    else:
        raise ValueError("Gold must be a number, like 800 or 800g 50s")
    if copper > MAX_GOLD * 10000:
        raise ValueError("That is more gold than exists")
    return copper


def safe_icon(url: str | None) -> str | None:
    return url if url and url.startswith(ICON_HOST) else None


def _aware(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _hm(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%H:%M UTC")


def _dhm(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%a %d %b %H:%M UTC")


def sell_advice(h, current: int | None, cut: float, now: datetime) -> tuple[str, int | None]:
    """kind: nolist | wait | list | loss. Price is per unit."""
    if current is None:
        return "nolist", h.target
    breakeven = math.ceil(h.price_paid / h.qty / (1 - cut))  # a sale returns the listing deposit
    if h.sell_by is not None and now >= h.sell_by:
        return "loss", undercut(current)
    if h.kind not in ("hold", "rise", "trend", "buyout"):  # a flip sells under the competition; listed above it, it would not sell
        return ("list", undercut(current)) if undercut(current) >= breakeven else ("wait", None)
    if current < breakeven:
        return "wait", None
    return "list", max(undercut(current), h.target)


def _last_scan(conn) -> datetime | None:
    ali = db.latest_scan(conn, "alliance")
    return _aware(db.candidates_at(conn)) or (_aware(ali["captured_at"]) if ali else None)


def build_state(conn, now: datetime) -> dict:
    cands, captured = db.load_candidates(conn)
    holdings = db.open_holdings(conn)
    # the scan already capped for holdings bought before it; one bought since makes its pick out of date
    since = {h.item_id for h in holdings if captured and h.bought_at > _aware(captured)}
    hidden = set(json.loads(db.get_setting(conn, "hidden", "[]")))
    chosen = set(json.loads(db.get_setting(conn, "professions", "[]")))
    fresh = [p for p in cands if p.item_id not in since and p.item_id not in hidden
             and not (p.kind == "craft" and chosen and _profession(p) not in chosen)
             # a vendor pick saved before its item was known to be limited-stock
             and not (p.kind == "vendor" and VENDOR_ITEMS.get(p.item_id) is not None)]
    kind, untyped = db.get_setting(conn, "type", ""), fresh
    if kind in TYPES:
        fresh = [p for p in fresh if _type((db.static(conn, p.item_id) or {"class_name": None})["class_name"]) == kind]
    floor = int(db.get_setting(conn, "min_profit", str(C.MIN_PROFIT)))
    unit_floor = int(db.get_setting(conn, "min_unit", str(C.MIN_UNIT_PROFIT)))
    stats = db.hit_stats(conn)
    ali = db.latest_scan(conn, "alliance")
    houses: dict[str, tuple] = {}
    for hs in {h.sell_at for h in holdings}:
        sc = db.latest_scan(conn, hs)
        houses[hs] = (sc["cut"] if sc else 0.05, db.rows_at(conn, hs, 0, now))
    held = []
    for h in sorted(holdings, key=lambda h: (h.sell_by is None, h.sell_by or now)):
        it = db.static(conn, h.item_id)
        cut, prices = houses[h.sell_at]
        cur = prices.get(h.item_id, (None,))[0]
        kind, price = sell_advice(h, cur, cut, now)
        loss = max(0, math.ceil(h.price_paid / h.qty / (1 - cut)) - (price or 0)) * h.qty
        held.append({"h": h, "name": it["name"] if it else None, "icon": it["icon"] if it else None,
                     "quality": it["quality"] if it else None, "current": cur,
                     "kind": kind, "price": price, "loss": loss})
    p = plan(fresh, db.get_gold(conn), holdings, holds_proven(stats), floor, unit_floor)
    oldest = conn.execute("SELECT min(captured_at) FROM scans WHERE house='alliance'").fetchone()[0]
    history_ok = bool(oldest) and now - _aware(oldest) >= timedelta(hours=C.MIN_HISTORY_HOURS)
    last_ok = _last_scan(conn)
    auctions = _aware(ali["auctions_at"]) if ali else None
    return {
        "gold": db.get_gold(conn), "plan": p, "holdings": held, "stats": stats,
        "crowd": db.get_setting(conn, "crowd"), "stale": db.get_setting(conn, "stale") == "1",
        "gathering": not cands and not history_ok, "scanning": scanner.busy.locked(),
        # the loading bar is paced by how long the last full scan took
        "scan_secs": int(db.get_setting(conn, "scan_secs", "360")), "scan_for": time.time() - scanner.started,
        "professions": chosen, "type": kind,
        # the same picks without the profit limits, and without the item type filter as well: what each hides
        "loose": plan(fresh, 0, holdings, holds_proven(stats), 0, 0, top=len(cands)),
        # with the profit limits but without the length cap, so the cap is not counted as a limit
        "floored": plan(fresh, 0, holdings, holds_proven(stats), floor, unit_floor, top=len(cands)),
        "untyped": plan(untyped, 0, holdings, holds_proven(stats), 0, 0, top=len(cands)), "min_profit": floor, "min_unit": unit_floor, "hidden": len(hidden), "sort": db.get_setting(conn, "sort", "return"),
        "drift": db.get_setting(conn, "drift"), "real": db.real_results(conn, ali["cut"] if ali else 0.05), "skills": json.loads(db.get_setting(conn, "skills", "{}")),
        "rep": json.loads(db.get_setting(conn, "rep", C.REP_DEFAULT)),
        "last_scan": last_ok, "scan_age_min": int((now - last_ok).total_seconds() // 60) if last_ok else None,
        "auctions_at": auctions, "data_age_min": int((now - auctions).total_seconds() // 60) if auctions else None,
        # scans run hourly (the site's cadence), so the next one is expected an hour after the last
        "next_scan": last_ok + timedelta(hours=1) if last_ok else None,
        "now": now,
    }


HOUSE = {"alliance": "Alliance AH", "blackwater": "Blackwater AH"}


def route(p) -> str:
    if p.kind == "cross":
        a, b = (x.capitalize() for x in (p.buy_at, p.sell_at))
        return f"{a} → {b}"
    if p.kind in ("vendor", "craft"):
        return f"{p.kind.capitalize()} → {HOUSE.get(p.sell_at, p.sell_at)}"
    return HOUSE.get(p.sell_at, p.sell_at)


def _name(item_id, name) -> str:
    if not name or name == f"Item {item_id}":  # the site had no name when this was saved
        name = ITEM_INFO.get(item_id, (None,))[0]
    return esc(name) if name else f"Item {item_id}"


def _ico(icon, quality) -> str:
    src = safe_icon(icon)
    q = QUALITY.get((quality or "").lower(), "")
    return f'<img class="ico {q}" src="{esc(src)}" alt="">' if src else f'<div class="ico {q}"></div>'


def _qcls(quality) -> str:
    return "q-" + QUALITY.get((quality or "").lower(), "white")


def _bought_form(p) -> str:
    sb = p.sell_by.isoformat() if p.sell_by else ""
    return (f'<form class="boughtform" method="post" action="/bought">'
            f'<input type="hidden" name="item_id" value="{esc(p.item_id)}">'
            f'<input type="hidden" name="kind" value="{esc(p.kind)}">'
            f'<input type="hidden" name="sell_at" value="{esc(p.sell_at)}">'
            f'<input type="hidden" name="target" value="{esc(p.sell_price)}">'
            f'<input type="hidden" name="sell_by" value="{esc(sb)}">'
            f'<label>Qty <input type="text" class="qty" name="qty" value="{esc(p.qty)}"></label>'
            f'<label>Paid <input type="text" name="price_paid" value="{esc(plain_gold(p.cost))}"></label>'
            f'<button>I bought this</button></form>')


def _pick_row(p, rank=None, form=True, dim=False) -> str:
    label, tcls = KINDS.get(p.kind, (p.kind, ""))
    unf = " unfunded" if dim and not p.funded else ""
    note = '<span class="note">not enough gold</span>' if unf else ""
    sb = f"<p>Sell by {_dhm(p.sell_by)}</p>" if p.sell_by else ""
    side = (f'{p.qty} × {fmt_money(p.max_unit_price)} → {fmt_money(p.sell_price)}<br>'
            f'<span class="profit">+{fmt_money(p.profit)}</span> · ~{p.days:.1f} days · sure '
            f'<span class="conf"><i style="width:{int(p.sure * 100)}%"></i></span>')
    trend = "trend unknown" if p.trend is None else f"price {p.trend * 100:+.0f}% vs a day ago"
    body = (f'<div class="acts"><details><summary>Why</summary><p>{esc(p.reason)}</p>'
            f'<p>≈{p.spd:.0f} sold/day (estimate) · {p.listed} listed on the sell AH · {trend}'
            f' · {p.depth} listed just above your price</p>{sb}</details>'
            + (f'<details class="bought"><summary>I bought this</summary>{_bought_form(p)}</details>' if form else "")
            + f'<form class="hide" method="post" action="/hide"><input type="hidden" name="item_id" value="{esc(p.item_id)}">'
              '<button title="Never suggest this item again">Hide</button></form></div>')
    rk = f'<div class="rank">{rank}</div>' if rank else "<div></div>"
    return (f'<div class="pick{unf}"><div class="row">{rk}{_ico(p.icon, p.quality)}'
            f'<div><span class="name {_qcls(p.quality)}">{_name(p.item_id, p.name)}</span>'
            f'<span class="tag {tcls}">{esc(label)}</span><span class="tag">{esc(route(p))}</span>{note}'
            f'<div class="why">{esc(p.reason)}</div></div><div class="nums">{side}</div></div>{body}</div>')


def _held_row(v) -> str:
    h = v["h"]
    now_p = f'now {fmt_money(v["current"])}: ' if v["current"] is not None else ""
    if v["kind"] == "nolist":
        adv = f'no listings — list at target {fmt_money(v["price"])}'
    elif v["kind"] == "wait":
        adv = f'below cost — wait until sell-by'
    elif v["kind"] == "loss":
        adv = f'sell-by passed: list at {fmt_money(v["price"])}' + (
            f' <span class="loss">(loss {fmt_money(v["loss"])})</span>' if v["loss"] else "")
    else:
        adv = f'list at {fmt_money(v["price"])}'
    adv = now_p + adv if v["kind"] != "nolist" else adv
    if v["current"] is not None and v["current"] < h.target and v["kind"] in ("list", "wait"):
        adv = '<span class="tag undercut">undercut</span> ' + adv  # cheaper than the price this pick aimed for
    sold_for = plain_gold(v["price"] or v["current"] or h.target)
    sb = f' · sell by {_dhm(h.sell_by)}' if h.sell_by else ""
    return (f'<div class="held"><div class="row"><div></div>{_ico(v["icon"], v["quality"])}'
            f'<div><span class="name {_qcls(v["quality"])}">{_name(h.item_id, v["name"])}</span>'
            f'<span class="tag">{esc(KINDS.get(h.kind, (h.kind,))[0])}</span><span class="tag">{esc(HOUSE.get(h.sell_at, h.sell_at))}</span>'
            f'<div class="why">{h.qty} × paid {fmt_money(h.price_paid // h.qty)}, target {fmt_money(h.target)}{sb}</div></div>'
            f'<div class="nums">{adv}<br><form class="inline" method="post" action="/sold">'
            f'<input type="hidden" name="holding_id" value="{esc(h.id)}"><label>for <input type="text" name="price" '
            f'value="{esc(sold_for)}" aria-label="Sold for, each"> each</label> <button>Sold</button></form></div></div></div>')


def _rows(items, empty, fn) -> str:
    return "".join(fn(i, n) for n, i in enumerate(items, 1)) or f'<div class="empty">{empty}</div>'


def _real(real: dict) -> str:
    """Real results line: profit over the holdings sold with a reported price."""
    if not real:
        return "No sales reported yet. Enter what each one sold for when you click Sold."
    total = sum(p for _, p in real.values())
    money = lambda cu: ("+" if cu >= 0 else "−") + fmt_money(abs(cu))
    parts = " · ".join(f"{esc(KINDS.get(k, (k,))[0])} {money(p)} ({n})" for k, (n, p) in sorted(real.items()))
    return (f'Real profit so far: <span class="{"profit" if total >= 0 else "loss"}">{money(total)}</span>'
            f' over {sum(n for n, _ in real.values())} sales — {parts}. After the AH cut, before listing deposits.')


def _rate(hits, judged) -> str:
    return f"{hits} of {judged} ({hits * 100 // judged}%)" if judged else "none judged yet"


def _type(class_name) -> str:
    return next((k for k, (_, names) in TYPES.items() if names and class_name in names), "other")


def _profession(p) -> str:
    """The profession of a crafting pick; ones saved before picks carried it are read from their reason."""
    m = re.search(r" with (.+?) \(skill ", p.reason)
    return p.profession or (m[1] if m else "")


def _profession_bar(chosen: set) -> str:
    options = "".join(f'<option value="{esc(n)}"{" selected" if n in chosen else ""}>{esc(n)}</option>'
                      for n in PROFESSIONS)
    return ('<form class="profs" method="post" action="/profession"><label>Profession <select name="profession" '
            f'data-auto><option value="">All professions</option>{options}</select></label><button>Show</button></form>')


def _skills_form(skills: dict) -> str:
    boxes = "".join(f'<label>{esc(n)} <input type="text" class="qty" name="{esc(n)}" value="{esc(skills.get(n, ""))}">'
                    f'</label>' for n in PROFESSIONS)
    return ('<details class="skills"><summary>My profession skills</summary><form class="boughtform" method="post" '
            f'action="/skills">{boxes}<button>Save</button></form><p>Optional: recipes above a skill you enter are '
            'left out from the next scan on. An empty box means any skill.</p></details>')


def render_page(state: dict, error: str | None = None, css_href: str = "/static/style.css",
                tab: str = "all") -> str:
    p, st = state["plan"], state["stats"]
    tab = tab if tab in TABS else "all"
    sort = state["sort"] if state["sort"] in SORTS else "return"

    def in_tab(k, p=p):
        pool = p.ranked + p.paper if k in PAPER_TABS else p.ranked
        return [x for x in pool if TABS[k][1] is None or x.kind in TABS[k][1]]

    shown = sorted(in_tab(tab), key=SORTS[sort][1])
    # counted for the open tab, so an empty tab says what emptied it
    by_floor = len(in_tab(tab, state["loose"])) - len(in_tab(tab, state["floored"]))
    by_type = len(in_tab(tab, state["untyped"])) - len(in_tab(tab, state["loose"]))
    funded_kinds = {x.kind for x in p.ranked}
    sorts = "".join(f'<button name="sort" value="{k}"{" class=\"on\"" if k == sort else ""}>{esc(v[0])}</button>'
                    for k, v in SORTS.items())
    unhide = (f'<form method="post" action="/unhide"><button>Show {state["hidden"]} hidden</button></form>'
              if state["hidden"] else "")
    types = "".join(f'<option value="{k}"{" selected" if k == state["type"] else ""}>{esc(v[0])}</option>'
                    for k, v in TYPES.items())
    controls = (f'<div class="controls"><form method="post" action="/type"><label>Show <select name="type" data-auto>'
                f'<option value="">All item types</option>{types}</select></label><button>Show</button></form>'
                f'<form method="post" action="/sort">Sort: {sorts}</form>'
                f'<form method="post" action="/min-profit"><label>Smallest profit worth showing <input type="text" '
                f'name="min_profit" value="{esc(plain_gold(state["min_profit"]))}"></label><label>and per item <input type="text" '
                f'class="qty" name="min_unit" value="{esc(plain_gold(state["min_unit"]))}"></label><button>Set</button></form>'
                f'{unhide}</div>'
                + (f'<div class="sub filtered">{by_floor} more picks are hidden by the two profit limits above.</div>'
                   if by_floor > 0 else "")
                + (f'<div class="sub filtered">{by_type} more picks are hidden because only '
                   f'{esc(TYPES[state["type"]][0])} is shown.</div>' if by_type > 0 else "")
                + ('<div class="sub">Multi-day bets on price moves, not proven yet: the program does not spend your gold '
                   'on them, but you can buy and track them. Holds wait out a flood, forecasts bet on items the crowd '
                   'has levelled past, rising prices bet that a day-long climb continues. Price held at target — holds: '
                   f'{esc(_rate(st["hold_hits"], st["hold_judged"]))} · forecasts and rising: '
                   f'{esc(_rate(st["rise_hits"], st["rise_judged"]))}</div>' if tab == "invest" else "")
                + ('<div class="sub">Buy an item\'s whole cheap shelf and relist it at the price it usually holds. '
                   'Only shown when days of history back it: no earlier price jump that fell back, no flood of '
                   'listings, steady sales and a price that has stopped falling. Not proven yet, so the program does '
                   'not spend your gold on them, and one only pays if you really buy the whole shelf. Price held at '
                   f'target: {esc(_rate(st["buyout_hits"], st["buyout_judged"]))}</div>' if tab == "buyout" else ""))
    tabs = "".join(
        f'<a class="tab{" on" if k == tab else ""}" href="/?tab={k}">{esc(label)} '
        f'<span>{len(in_tab(k))}</span></a>'
        for k, (label, kinds) in TABS.items())
    ban = []
    if error:
        ban.append(f'<div class="banner err">{esc(error)}</div>')
    if state["scanning"]:
        at = min(97, max(0, int(100 * state["scan_for"] / state["scan_secs"])))
        left = max(1, int(state["scan_secs"] - state["scan_for"]))
        # each reload restarts the animation from where the scan is, so it reads as one slow sweep
        ban.append('<div class="banner">Scanning the auction house now… This takes a few minutes; the page reloads '
                   f'by itself.<div class="bar"><i style="--from:{at}%;--left:{left}s"></i></div></div>')
    if state["gathering"]:
        ban.append('<div class="banner">Gathering price history… Picks can appear after the first scan cycle '
                   '(a few minutes), since the site already keeps price history. Holds need about a day of scans.</div>')
    if state["stale"]:
        ban.append('<div class="banner">Site data is stale: the auction snapshot is old, so no new picks are saved.</div>')
    if state["scan_age_min"] is not None and state["scan_age_min"] > 90:
        ban.append(f'<div class="banner">Last scan {state["scan_age_min"]} minutes ago. Showing the last good data.</div>')
    age = (f'AH data from {_hm(state["auctions_at"])} ({state["data_age_min"]} min old)'
           if state["auctions_at"] else "No AH data yet")
    nxt = f' · next scan about {_hm(state["next_scan"])}' if state["next_scan"] else ""
    hit = f'{st["hits"]} of {st["judged"]} ({st["hits"] * 100 // st["judged"]}%)' if st["judged"] else "none judged yet"
    last = _hm(state["last_scan"]) if state["last_scan"] else "never"
    return Template((HERE / "page.html").read_text(encoding="utf-8")).substitute(
        css_href=esc(css_href), banners="".join(ban), here=f"/?tab={tab}",
        version=esc(_version(state)),
        tabs=f'<nav class="tabs">{tabs}</nav>' + controls + (_profession_bar(state["professions"]) + _skills_form(state["skills"]) if tab == "craft" else "")
        + (_rep_form(state["rep"]) if tab == "vendor" else ""), reload_s=5 if state["scanning"] else 300,
        refresh_off=" disabled" if state["scanning"] else "",
        sub=f'Alliance · Normal (PvE) · EU — {esc(" + ".join(HOUSE[h] for h in C.SCAN_HOUSES))} — {esc(age)}{esc(nxt)}',
        gold_text=esc(plain_gold(state["gold"])), spend=fmt_money(p.spend), profit=fmt_money(p.profit),
        crowd=(f'lvl {esc(state["crowd"])}' + (f' <span class="sub">+{esc(state["drift"])} lvl/day</span>'
                                                 if state["drift"] else "")) if state["crowd"] else "unknown",
        last_scan=esc(last), hit_rate=esc(hit), real=_real(state["real"]),
        buy=_rows(shown, "Nothing worth buying right now.",
                  lambda x, n: _pick_row(x, n, dim=x.kind in funded_kinds)),
        sell=_rows(state["holdings"], "Nothing held.", lambda x, n: _held_row(x)))


def _int(text, name, lo, hi) -> int:
    try:
        v = int(text)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def _bought(conn, f, now):
    kind = f.get("kind")
    if kind not in ("flip", "cross", "vendor", "craft", "hold", "rise", "trend", "buyout", "slow"):
        raise ValueError("Unknown kind")
    try:
        sell_by = _aware(f.get("sell_by"))
    except ValueError:
        raise ValueError("Bad sell-by date") from None
    qty = _int(f.get("qty"), "Quantity", 1, 100_000)
    paid = parse_gold(f.get("price_paid", ""))
    sell_at = f.get("sell_at", "alliance")
    if sell_at not in ("alliance", "blackwater"):
        raise ValueError("Unknown sell house")
    db.add_holding(conn, _int(f.get("item_id"), "Item", 1, 10**9), kind, qty, paid,
                   _int(f.get("target"), "Target", 0, 10**12), now, sell_by, sell_at)


def _version(state) -> str:
    """Changes whenever the page has something new to show; the page polls it instead of reloading."""
    return f'{state["last_scan"]}|{state["scanning"]}'


def _hide(conn, f, now):
    hidden = set(json.loads(db.get_setting(conn, "hidden", "[]"))) | {_int(f.get("item_id"), "Item", 1, 10**9)}
    db.set_setting(conn, "hidden", json.dumps(sorted(hidden)))


def _sort(conn, f, now):
    if f.get("sort") not in SORTS:
        raise ValueError("Unknown sort order")
    db.set_setting(conn, "sort", f["sort"])


def _pick_profession(conn, f, now):
    """Show one profession's crafts; the empty name is All professions."""
    name = f.get("profession", "")
    if name and name not in PROFESSIONS:
        raise ValueError("Unknown profession")
    db.set_setting(conn, "professions", json.dumps([name] if name else []))


def _pick_type(conn, f, now):
    if f.get("type", "") not in ("", *TYPES):
        raise ValueError("Unknown item type")
    db.set_setting(conn, "type", f.get("type", ""))


def _floors(conn, f, now):
    for key in ("min_profit", "min_unit"):
        if key in f:
            db.set_setting(conn, key, str(parse_gold(f[key])))


def _skills(conn, f, now):
    skills = {n: _int(f[n], f"{n} skill", 1, 300) for n in PROFESSIONS if f.get(n, "").strip()}
    db.set_setting(conn, "skills", json.dumps(skills))


def _rep(conn, f, now):
    db.set_setting(conn, "rep", json.dumps({k: f[k] for k in C.REP_CITIES if f.get(k) in C.REP_DISCOUNT}))


def _rep_form(rep: dict) -> str:
    def select(k, city):
        opts = "".join(f'<option{" selected" if rep.get(k) == s else ""}>{s}</option>' for s in C.REP_DISCOUNT)
        return f'<label>{esc(city)} <select name="{k}"><option value="">Neutral</option>{opts}</select></label> '
    return ('<details class="skills"><summary>My city reputations</summary><form class="boughtform" method="post" '
            f'action="/rep">{"".join(select(k, v) for k, v in C.REP_CITIES.items())}<button>Save</button></form>'
            '<p>Vendors of a city sell 5% cheaper at Friendly, 10% at Honored, 15% at Revered and 20% at Exalted. '
            'Vendor prices use this from the next scan on (press Refresh picks).</p></details>')


def _tab(url: str | None) -> str:
    """The tab named in a page URL, so a reload or a form post lands back on it."""
    tab = parse_qs(urlsplit(url or "").query).get("tab", ["all"])[0]
    return tab if tab in TABS else "all"


def _refresh(conn, f, now):
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    threading.Thread(target=scanner.refresh, args=(path,), daemon=True).start()


ROUTES = {
    "/gold": lambda c, f, now: db.set_gold(c, parse_gold(f.get("gold", ""))),
    "/bought": _bought,
    "/sold": lambda c, f, now: db.mark_sold(c, _int(f.get("holding_id"), "Holding", 1, 10**12), now,
                                            parse_gold(f["price"]) if f.get("price", "").strip() else None),
    "/refresh": _refresh,
    "/skills": _skills,
    "/rep": _rep,
    "/profession": _pick_profession,
    "/type": _pick_type,
    "/hide": _hide,
    "/unhide": lambda c, f, now: db.set_setting(c, "hidden", "[]"),
    "/sort": _sort,
    "/min-profit": _floors,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if ctype.startswith("text/html"):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self, post: bool) -> bool:
        port = self.server.server_address[1]
        ok = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in ok:
            return False
        origin = self.headers.get("Origin")
        return not (post and origin is not None and origin not in {f"http://{h}" for h in ok})

    def _page(self, code=200, error=None, tab="all"):
        conn = db.connect(self.server.db_path)
        try:
            html = render_page(build_state(conn, datetime.now(UTC)), error, tab=tab)
        finally:
            conn.close()
        self._send(code, html, "text/html; charset=utf-8")

    def do_GET(self):
        if not self._allowed(False):
            return self._send(403, "forbidden")
        path = urlsplit(self.path).path
        if path == "/":
            self._page(tab=_tab(self.path))
        elif path == "/version":
            conn = db.connect(self.server.db_path)
            try:
                self._send(200, _version({"last_scan": _last_scan(conn), "scanning": scanner.busy.locked()}))
            finally:
                conn.close()
        elif path == "/static/style.css":
            self._send(200, (HERE / "static" / "style.css").read_bytes(), "text/css; charset=utf-8")
        elif path == "/static/icon.svg":
            self._send(200, (HERE / "static" / "icon.svg").read_bytes(), "image/svg+xml")
        elif path == "/static/app.js":
            self._send(200, (HERE / "static" / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(200, (HERE / "static" / "favicon.ico").read_bytes(), "image/x-icon")
        else:
            self._send(404, "not found")

    def do_POST(self):
        if not self._allowed(True):
            return self._send(403, "forbidden")
        path = urlsplit(self.path).path
        if path not in ROUTES:
            return self._send(404, "not found")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411, "length required")
        if not 0 <= n <= MAX_BODY:
            return self._send(413, "too large")
        if not (self.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
            return self._send(415, "unsupported")
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace"),
                                            keep_blank_values=True).items()}
        conn = db.connect(self.server.db_path)
        try:
            ROUTES[path](conn, form, datetime.now(UTC))
        except ValueError as e:
            return self._page(400, str(e), _tab(self.headers.get("Referer")))
        finally:
            conn.close()
        self._send(303, headers=[("Location", f"/?tab={_tab(self.headers.get('Referer'))}")])


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        log.exception("page request failed")  # the desktop app has no console for the default traceback


def make_server(db_path: str, port: int) -> ThreadingHTTPServer:
    srv = Server(("127.0.0.1", port), Handler)
    srv.db_path = db_path
    return srv
