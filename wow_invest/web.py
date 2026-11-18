"""Page rendering and the localhost HTTP routes."""
import json
import logging
import math
import re
import threading
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urlsplit

from . import __version__
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
         "trend": ("Rising price", "hold"), "buyout": ("Buyout", "hold"),
         "swing": ("Daily swing", "hold"), "slow": ("Slow seller", "flip")}
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
        "buyout": ("Buyouts", {"buyout"}), "swing": ("Daily swings", {"swing"}),
        "tracked": ("★ Tracked", set())}  # no picks: the items you starred, and what a search finds
# what each tab means: shown when the pointer rests on the tab (the little ⓘ marks it)
TAB_INFO = {
    "all": "Every pick, in the order you chose.",
    "ah": "An item listed well under its own usual price. Buy it and sell it again at the usual price, most often within "
          "a day or two.",
    "slow": "Underpriced items that sell fewer than five a day, like gear and recipes. More gold a piece, but "
            "they can take days to sell.",
    "vendor": "Bought from a vendor who never runs out, sold on the auction house for more.",
    "craft": "Buy the reagents, craft the item, and sell it for more than the reagents cost.",
    "invest": "Bets over several days: prices that are climbing or expected to rise. Riskier, and some are only "
              "followed on paper.",
    "buyout": "Buy the whole cheap shelf and sell it again at the price the item has held over the past days.",
    "swing": "Fast sellers whose price dips and comes back every day. Buy the dip and sell at the daily high.",
    "tracked": "The items you starred, with their price on other realms.",
}
SEARCH_MAX = 30
PAPER_TABS = ("invest", "buyout", "swing")
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
    if h.kind not in ("hold", "rise", "trend", "buyout", "swing"):  # a flip sells under the competition; listed above it, it would not sell
        return ("list", undercut(current)) if undercut(current) >= breakeven else ("wait", None)
    if current < breakeven:
        return "wait", None
    return "list", max(undercut(current), h.target)


def _last_scan(conn) -> datetime | None:
    ali = db.latest_scan(conn, "alliance")
    return _aware(db.candidates_at(conn)) or (_aware(ali["captured_at"]) if ali else None)


def _watch(conn, now, q: str) -> dict:
    """The starred items, and the best sellers whose name holds `q`, with the latest scan's numbers."""
    ids = json.loads(db.get_setting(conn, "starred", "[]"))
    q = q.strip().lower()
    found = [r[0] for r in conn.execute(
        """SELECT i.item_id FROM items i LEFT JOIN snapshots s ON s.item_id = i.item_id AND s.scan_id =
             (SELECT id FROM scans WHERE house='alliance' ORDER BY captured_at DESC LIMIT 1)
           WHERE instr(lower(i.name), ?) ORDER BY coalesce(s.sold_24h, -1) DESC, i.name LIMIT ?""",
        (q, SEARCH_MAX))] if len(q) >= 2 else []
    cur, old = db.rows_at(conn, "alliance", 0, now), db.rows_at(conn, "alliance", 24, now)
    across = json.loads(db.get_setting(conn, "compare", "{}"))

    def item(i):
        it = db.static(conn, i)
        ask, units, sold = cur.get(i, (None, 0, 0))
        return {"item_id": i, "name": it["name"] if it else None, "icon": it["icon"] if it else None,
                "quality": it["quality"] if it else None, "ask": ask, "units": units, "sold": sold,
                "old": old.get(i, (None,))[0], "starred": i in ids, "across": across.get(str(i))}
    return {"q": q, "ids": set(ids), "found": [item(i) for i in found], "starred": [item(i) for i in ids]}


def _star(conn, f, now):
    i = _int(f.get("item_id"), "Item", 1, 10**9)
    ids = json.loads(db.get_setting(conn, "starred", "[]"))
    db.set_setting(conn, "starred", json.dumps([x for x in ids if x != i] if i in ids else ids + [i]))


def _newer(latest: str | None) -> str | None:
    """`latest` when it is a higher version than this program's, else None."""
    try:
        num = lambda v: tuple(int(x) for x in v.split("."))
        return latest if latest and num(latest) > num(__version__) else None
    except ValueError:
        return None


def _market(conn) -> dict:
    scanner.use_market(conn)  # the house names in every label follow the player's faction
    ms, main = scanner.known_markets(conn), scanner.market(conn)
    return {"markets": ms, "main": main,
            "label": f"{main[2].title()} · {_realm_name(ms, main[1])} · {C.REGIONS.get(main[0], main[0].upper())}"}


def build_state(conn, now: datetime, q: str = "") -> dict:
    mk = _market(conn)
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
        advice, price = sell_advice(h, cur, cut, now)
        loss = max(0, math.ceil(h.price_paid / h.qty / (1 - cut)) - (price or 0)) * h.qty
        held.append({"h": h, "name": it["name"] if it else None, "icon": it["icon"] if it else None,
                     "quality": it["quality"] if it else None, "current": cur,
                     "kind": advice, "price": price, "loss": loss})
    p = plan(fresh, db.get_gold(conn), holdings, holds_proven(stats), floor, unit_floor)
    oldest = conn.execute("SELECT min(captured_at) FROM scans WHERE house='alliance'").fetchone()[0]
    history_ok = bool(oldest) and now - _aware(oldest) >= timedelta(hours=C.MIN_HISTORY_HOURS)
    last_ok = _last_scan(conn)
    auctions = _aware(ali["auctions_at"]) if ali else None
    return {
        "gold": db.get_gold(conn), "plan": p, "holdings": held, "stats": stats,
        "crowd": db.get_setting(conn, "crowd"), "stale": db.get_setting(conn, "stale") == "1", "blocked": db.get_setting(conn, "blocked") == "1",
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
        "now": now, "watch": _watch(conn, now, q), "market": mk,
        "update": _newer(db.get_setting(conn, "latest_version")),
    }


def _house(h) -> str:
    return f"{C.HOUSE_NAMES.get(h, h.title())} AH"


def _realm_name(markets, ruleset) -> str:
    """The realm type's name, as the site writes it after the house: "Alliance Auction House · Normal (PvE)"."""
    names = [m[3].split(" · ", 1)[1] for m in markets if m[1] == ruleset and " · " in m[3]]
    return names[0].replace(" (provisional)", "") if names else f"Realm type {ruleset}"


BLOCKED = ("The price site (blackwater.exchange) is refusing this program: it now asks programs for an API "
           "key. No new prices can be read until that is sorted out.")


def _set_realm(conn, f, now):
    ms = scanner.known_markets(conn)
    region, faction = f.get("region"), f.get("faction")
    ruleset = _int(f.get("realm"), "Realm", 0, 10**6)
    if faction not in C.FACTIONS[:2] or not scanner.houses_of(ms, region, ruleset):
        raise ValueError("The price site does not cover that realm")
    new = f"{region}/{ruleset}/{faction}"
    if new == db.get_setting(conn, "market"):
        return
    # a scan still running would save the old realm's prices under the new one
    if not scanner.busy.acquire(blocking=False):
        raise ValueError("A scan is running. Change realm when it has finished")
    try:
        db.switch_market(conn, new)
    finally:
        scanner.busy.release()
    _refresh(conn, f, now)


def _realm_selects(ms, region=None, ruleset=None, faction=None) -> str:
    """Lists for region, realm type and faction, from what the site covers; nothing is chosen for you."""
    opt = lambda v, label, on: f'<option value="{esc(v)}"{" selected" if on else ""}>{esc(label)}</option>'
    first = "" if region else '<option value="" selected disabled>Choose…</option>'
    regions = "".join(opt(r, C.REGIONS.get(r, r.upper()), r == region) for r in sorted({x[0] for x in ms}))
    realms = "".join(opt(rs, _realm_name(ms, rs), rs == ruleset) for rs in sorted({x[1] for x in ms}))
    factions = "".join(opt(x, x.title(), x == faction) for x in C.FACTIONS[:2])
    return (f'<label>Region <select name="region" required>{first}{regions}</select></label><label>Realm '
            f'<select name="realm" required>{first}{realms}</select></label><label>Faction '
            f'<select name="faction" required>{first}{factions}</select></label>')


RISKS = ("WoW Invest gives suggestions, not guarantees. Prices move, other players undercut you, and items can "
         "fail to sell. You can lose gold.",
         "Every profit it shows is an estimate from price history. It is not proof that an item will sell.",
         "You make every purchase and sale yourself, in the game, and you alone are responsible for them.",
         "It is not affiliated with Blizzard or with blackwater.exchange, the site its prices come from.")


def _setup(conn, f, now):
    """The welcome page: the risks accepted, then gold and realm saved together or not at all."""
    if f.get("agree") != "yes":
        raise ValueError("Tick the box to confirm you understand that you can lose gold")
    gold = parse_gold(f.get("gold", ""))
    _set_realm(conn, f, now)
    db.set_gold(conn, gold)


def welcome_page(markets: list, error: str | None = None, css_href: str = "/static/style.css",
                 blocked: bool = False) -> str:
    """Shown instead of the program until a realm is chosen: realm, gold and the risks to accept."""
    ban = f'<div class="banner err">{esc(error)}</div>' if error else ""
    if markets:
        body = ('<form class="welcome" method="post" action="/setup"><div class="sect">Your realm</div>'
                f'<div class="controls">{_realm_selects(markets)}</div><div class="sect">Your gold</div>'
                '<div class="controls"><label>Gold you want to trade with <input type="text" name="gold" '
                'placeholder="e.g. 100g" required></label></div><div class="sect">Before you start</div>'
                f'<ul>{"".join(f"<li>{esc(r)}</li>" for r in RISKS)}</ul>'
                '<label class="agree"><input type="checkbox" name="agree" value="yes" required> I understand that '
                'I can lose gold by following these suggestions.</label><button>Start</button></form>')
        wait = ""
    elif blocked:
        body, wait = f'<div class="empty">{esc(BLOCKED)}</div>', ""
    else:  # the scanner is fetching the list right now: look again shortly
        body = '<div class="empty">Reading the list of realms from the price site… This takes a few seconds.</div>'
        wait = '<meta http-equiv="refresh" content="3">'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{wait}<meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>WoW Invest</title><link rel="icon" href="/favicon.ico">'
            f'<link rel="stylesheet" href="{esc(css_href)}"></head><body><main>{ban}<div class="frame">'
            '<header class="brand"><img src="/static/icon.svg" alt="" width="28" height="28"><h1>Welcome to WoW Invest</h1>'
            '</header><p class="sub">It watches auction house prices and suggests what to buy and sell. It never touches '
            f'the game: you trade by hand. Tell it where you play and what you have.</p>{body}</div><div class="sub foot">'
            'Made by Wectoz · AI was used in the development of this app.</div></main></body></html>')


def _realm_form(m: dict) -> str:
    """Region, realm type and faction of the player's main realm."""
    ms = m["markets"]
    if not ms:
        return ""
    return ('<details class="filters realm"><summary>Change realm</summary><form class="controls" method="post" '
            f'action="/realm">{_realm_selects(ms, *m["main"])}<button>Make this my main realm</button></form><div class="sub">Picks, prices and scans follow '
            'your main realm. Each realm keeps its own price history, picks and holdings, which come back when you return to it. New picks need a scan (a few '
            'minutes). Your settings stay.</div></details>')


def route(p) -> str:
    if p.kind == "cross":
        a, b = (C.HOUSE_NAMES.get(x, x.capitalize()) for x in (p.buy_at, p.sell_at))
        return f"{a} → {b}"
    if p.kind in ("vendor", "craft"):
        return f"{p.kind.capitalize()} → {_house(p.sell_at)}"
    return _house(p.sell_at)


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
    what = (f'<input type="hidden" name="item_id" value="{esc(p.item_id)}">'
            f'<input type="hidden" name="kind" value="{esc(p.kind)}">'
            f'<input type="hidden" name="sell_at" value="{esc(p.sell_at)}">'
            f'<input type="hidden" name="target" value="{esc(p.sell_price)}">'
            f'<input type="hidden" name="sell_by" value="{esc(sb)}">')
    # one click records the suggested amount at the suggested cost; anything else is one step away
    return (f'<div class="boughtform"><form method="post" action="/bought">{what}'
            f'<input type="hidden" name="qty" value="{esc(p.qty)}">'
            f'<input type="hidden" name="price_paid" value="{esc(plain_gold(p.cost))}">'
            f'<button title="Records {esc(p.qty)} bought for {esc(plain_gold(p.cost))} in total">I bought this</button></form>'
            f'<details class="other"><summary>I bought a different amount</summary>'
            f'<form class="boughtform" method="post" action="/bought">{what}'
            f'<label>Qty <input type="text" class="qty" name="qty" value="{esc(p.qty)}"></label>'
            f'<label>Paid in total <input type="text" name="price_paid" value="{esc(plain_gold(p.cost))}"></label>'
            f'<button>Save</button></form></details></div>')


def _star_form(item_id, on) -> str:
    tip = "Stop tracking this item" if on else "Track this item on the ★ Tracked tab"
    return (f'<form class="star" method="post" action="/star"><input type="hidden" name="item_id" value="{esc(item_id)}">'
            f'<button{" class=\"on\"" if on else ""} title="{tip}" aria-label="{tip}">{"★" if on else "☆"}</button></form>')


def _across(v, mk) -> str:
    """A tracked item's price on the player's faction house of every realm, cheapest first."""
    a = v["across"]
    if not a:
        return ('<div class="more"><p>Its prices on the other realms arrive with the next scan '
                f'({C.COMPARE_PER_CYCLE} tracked items at a time).</p></div>')
    lines = ""
    for reg, rs, ask, units, spd in sorted(a["rows"], key=lambda r: (r[2] is None, r[2] or 0)):
        mine = ' <span class="tag">your realm</span>' if (reg, rs) == mk["main"][:2] else ""
        nums = (f'<td>{fmt_money(ask)}</td><td>{units} listed</td><td>≈{spd} sold/day</td>' if ask is not None
                else '<td colspan="3">not listed</td>')
        lines += (f'<tr><td>{esc(C.REGIONS.get(reg, reg.upper()))} · {esc(_realm_name(mk["markets"], rs))}{mine}</td>'
                  f'{nums}</tr>')
    return (f'<div class="more"><p>{esc(_house("alliance"))} on every realm, cheapest first · checked '
            f'{_hm(_aware(a["at"]))}. Gold is worth more or less on each realm, so compare with care.</p>'
            f'<table class="cmp">{lines}</table></div>')


def _item_row(v, picks, mk) -> str:
    """A tracked or found item: what it costs and how it sells now, and any pick on it. A tracked one
    opens to its price on every realm."""
    tags = "".join(f'<span class="tag">{esc(k)} pick</span>' for k in picks.get(v["item_id"], ()))
    if v["ask"] is None:
        price, moved = "not listed now", ""
    else:
        price = fmt_money(v["ask"])
        moved = f'<br>{v["ask"] / v["old"] - 1:+.0%} vs a day ago' if v["old"] else ""
    row = (f'<div></div>{_ico(v["icon"], v["quality"])}'
           f'<div><span class="name {_qcls(v["quality"])}">{_name(v["item_id"], v["name"])}</span>{tags}'
           f'<div class="why">{v["units"]} listed · ≈{v["sold"] // 2} sold/day (estimate)</div></div>'
           f'<div class="nums">{price}{moved}</div>')
    body = (f'<details><summary class="row">{row}</summary>{_across(v, mk)}</details>' if v["starred"]
            else f'<div class="row">{row}</div>')
    return f'<div class="pick">{body}{_star_form(v["item_id"], v["starred"])}</div>'


def _pick_row(p, rank=None, form=True, dim=False, starred=False) -> str:
    label, tcls = KINDS.get(p.kind, (p.kind, ""))
    unf = " unfunded" if dim and not p.funded else ""
    note = '<span class="note">not enough gold</span>' if unf else ""
    sb = f"<p>Sell by {_dhm(p.sell_by)}</p>" if p.sell_by else ""
    side = (f'{p.qty} × {fmt_money(p.max_unit_price)} → {fmt_money(p.sell_price)}<br>'
            f'<span class="profit">+{fmt_money(p.profit)}</span> · ~{p.days:.1f} days · sure '
            f'<span class="conf"><i style="width:{int(p.sure * 100)}%"></i></span>')
    trend = "trend unknown" if p.trend is None else f"price {p.trend * 100:+.0f}% vs a day ago"
    # one quiet row; a click opens it for the full reason, the numbers behind it and the buttons
    more = (f'<div class="more"><p>≈{p.spd:.0f} sold/day (estimate) · {p.listed} listed on the sell AH · {trend}'
            f' · {p.depth} listed just above your price</p>{sb}'
            + (_bought_form(p) if form else "")
            + f'<form class="hide" method="post" action="/hide"><input type="hidden" name="item_id" value="{esc(p.item_id)}">'
              '<button title="Never suggest this item again">Hide this item</button></form></div>')
    rk = f'<div class="rank">{rank}</div>' if rank else "<div></div>"
    return (f'<div class="pick{unf}"><details><summary class="row">{rk}{_ico(p.icon, p.quality)}'
            f'<div><span class="name {_qcls(p.quality)}">{_name(p.item_id, p.name)}</span>'
            f'<span class="tag {tcls}">{esc(label)}</span><span class="tag">{esc(route(p))}</span>{note}'
            f'<div class="why">{esc(p.reason)}</div></div><div class="nums">{side}</div></summary>{more}</details>'
            f'{_star_form(p.item_id, starred)}</div>')


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
            f'<span class="tag">{esc(KINDS.get(h.kind, (h.kind,))[0])}</span><span class="tag">{esc(_house(h.sell_at))}</span>'
            f'<div class="why">{h.qty} × paid {fmt_money(h.price_paid // h.qty)}, target {fmt_money(h.target)}{sb}</div></div>'
            f'<div class="nums">{adv}<br><form class="inline" method="post" action="/sold">'
            f'<input type="hidden" name="holding_id" value="{esc(h.id)}"><label>sold <input type="text" class="qty" '
            f'name="qty" value="{esc(h.qty)}" aria-label="How many you sold"></label> <label>for <input type="text" '
            f'name="price" value="{esc(sold_for)}" aria-label="Sold for, each"> each</label> <button title="Sell them '
            f'in batches: enter how many sold and for how much, the rest stays here">Sold</button></form></div></div></div>')


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
                tab: str = "all", page: int = 1) -> str:
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
    cut = by_floor + by_type + state["hidden"]
    told = " · ".join(([TYPES[state["type"]][0]] if state["type"] in TYPES else []) + [SORTS[sort][0]]
                      + ([f"{cut} picks hidden"] if cut > 0 else []))
    controls = (f'<details class="filters"><summary>Filters <span>{esc(told)}</span></summary>'
                f'<div class="controls"><form method="post" action="/type"><label>Show <select name="type" data-auto>'
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
                + '</details>'
                + ('<div class="sub">Multi-day bets on price moves, not proven yet: the program does not spend your gold '
                   'on them, but you can buy and track them. Holds wait out a flood, forecasts bet on items the crowd '
                   'has levelled past, rising prices bet that a day-long climb continues. Price held at target — holds: '
                   f'{esc(_rate(st["hold_hits"], st["hold_judged"]))} · forecasts and rising: '
                   f'{esc(_rate(st["rise_hits"], st["rise_judged"]))}</div>' if tab == "invest" else "")
                + ('<div class="sub">Buy an item\'s whole cheap shelf and relist it at the price it usually holds. '
                   'Only shown when days of history back it: no earlier price jump that fell back, no flood of '
                   'listings, steady sales and a price that has stopped falling. Not proven yet, so the program does '
                   'not spend your gold on them, and one only pays if you really buy the whole shelf. Price held at '
                   f'target: {esc(_rate(st["buyout_hits"], st["buyout_judged"]))}</div>' if tab == "buyout" else "")
                + ('<div class="sub">Fast sellers whose price dipped and came back on each of the last days: buy '
                   'the dip, relist at the daily high and wait for the price to return, usually within a day. '
                   'Refused when the highs are sinking or when less sells in a day than is listed. Not proven yet, '
                   'so the program does not spend your gold on them. Price came back to target: '
                   f'{esc(_rate(st["swing_hits"], st["swing_judged"]))}</div>' if tab == "swing" else ""))
    w, mk = state["watch"], state["market"]
    search = ('<form class="search" method="get" action="/"><input type="hidden" name="tab" value="tracked">'
              f'<input type="text" name="q" value="{esc(w["q"])}" placeholder="Search items…" aria-label="Search items">'
              '</form>')  # one box: Enter searches
    if tab == "tracked":
        on = {}
        for x in p.ranked + p.paper:
            on.setdefault(x.item_id, []).append(KINDS.get(x.kind, (x.kind,))[0])
        controls = ""  # the pick filters do not apply to this list
        buy = ((f'<div class="sect">Found for “{esc(w["q"])}”</div>'
                + _rows(w["found"], "No item with that name.", lambda v, n: _item_row(v, on, mk))
                + '<div class="sect">Tracked</div>' if len(w["q"]) >= 2 else "")
               + _rows(w["starred"], "Nothing tracked yet. Search for an item, or press ☆ on a pick.",
                       lambda v, n: _item_row(v, on, mk)))
    else:
        pages = max(1, math.ceil(len(shown) / C.PAGE_SIZE))
        page = min(max(page, 1), pages)
        skip = (page - 1) * C.PAGE_SIZE
        link = lambda n, text: f'<a href="{esc(_at(tab, n))}">{text}</a>'
        pager = ('<nav class="pager">' + (link(page - 1, "‹ Previous") if page > 1 else "<span>‹ Previous</span>")
                 + f'<b>Page {page} of {pages}</b>'
                 + (link(page + 1, "Next ›") if page < pages else "<span>Next ›</span>") + '</nav>') if pages > 1 else ""
        buy = _rows(shown[skip:skip + C.PAGE_SIZE], "Nothing worth buying right now.",
                    lambda x, n: _pick_row(x, n + skip, dim=x.kind in funded_kinds, starred=x.item_id in w["ids"])) + pager
    count = lambda k: len(w["starred"]) if k == "tracked" else len(in_tab(k))
    tabs = "".join(
        f'<a title="{esc(TAB_INFO[k])}" class="tab{" on" if k == tab else "" if count(k) else " none"}" '
        f'href="/?tab={k}">{esc(label)} <span>{count(k)}</span><i class="info" aria-hidden="true">ⓘ</i></a>'
        for k, (label, kinds) in TABS.items())
    ban = []
    if error:
        ban.append(f'<div class="banner err">{esc(error)}</div>')
    if state["scanning"]:
        at = min(97, max(0, int(100 * state["scan_for"] / state["scan_secs"])))
        left = max(1, int(state["scan_secs"] - state["scan_for"]))
        # each reload restarts the animation from where the scan is, so it reads as one slow sweep
        mins = f"about {math.ceil(left / 60)} min left" if state["scan_for"] < state["scan_secs"] else "almost done"
        ban.append(f'<div class="banner">Scanning the auction house now, {mins}. The page reloads '
                   f'by itself.<div class="bar"><i style="--from:{at}%;--left:{left}s"></i></div></div>')
    if state["gathering"]:
        ban.append('<div class="banner">Gathering price history… Picks can appear after the first scan cycle '
                   '(a few minutes), since the site already keeps price history. Holds need about a day of scans.</div>')
    if state["blocked"]:
        ban.append(f'<div class="banner err">{esc(BLOCKED)} Showing the last good data.</div>')
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
        css_href=esc(css_href), banners="".join(ban), here=esc(_at(tab, page)),
        version=esc(_version(state)),
        search=search, tabs=f'<nav class="tabs">{tabs}</nav>' + controls + (_profession_bar(state["professions"]) + _skills_form(state["skills"]) if tab == "craft" else "")
        + (_rep_form(state["rep"]) if tab == "vendor" else ""), reload_s=5 if state["scanning"] else 300,
        refresh_off=" disabled" if state["scanning"] else "",
        sub=f'{esc(mk["label"])} — {esc(" + ".join(_house(h) for h in C.SCAN_HOUSES))} — {esc(age)}{esc(nxt)}',
        realm=_realm_form(mk), app_version=esc(__version__),
        update=(f'<form method="post" action="/update"><button class="new" title="Opens the download page in your '
                f'browser">Update to v{esc(state["update"])}</button></form>' if state["update"] else ""),
        gold_text=esc(plain_gold(state["gold"])), spend=fmt_money(p.spend), profit=fmt_money(p.profit),
        crowd=(f'lvl {esc(state["crowd"])}' + (f' <span class="sub">+{esc(state["drift"])} lvl/day</span>'
                                                 if state["drift"] else "")) if state["crowd"] else "unknown",
        last_scan=esc(last), hit_rate=esc(hit), real=_real(state["real"]),
        buy=buy,
        sell=_rows(state["holdings"], "Nothing held.", lambda x, n: _held_row(x)), held_n=len(state["holdings"]))


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
    if kind not in ("flip", "cross", "vendor", "craft", "hold", "rise", "trend", "buyout", "swing", "slow"):
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


def _at(tab: str, page: int = 1) -> str:
    """The address of a tab, and of a later page of its picks."""
    return f"/?tab={tab}" + (f"&page={page}" if page > 1 else "")


def _page_no(url: str | None) -> int:
    """The page of picks named in a page URL: a reload or a form post lands back on it."""
    n = parse_qs(urlsplit(url or "").query).get("page", ["1"])[0]
    return int(n) if n.isdigit() and 0 < int(n) < 10**6 else 1


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
                                            parse_gold(f["price"]) if f.get("price", "").strip() else None,
                                            _int(f["qty"], "Quantity", 1, 100_000) if f.get("qty", "").strip() else None),
    "/refresh": _refresh,
    "/skills": _skills,
    "/rep": _rep,
    "/profession": _pick_profession,
    "/type": _pick_type,
    "/hide": _hide,
    "/star": _star,
    "/realm": _set_realm,
    "/setup": _setup,
    "/update": lambda c, f, now: webbrowser.open(C.UPDATE_PAGE),
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

    def _page(self, code=200, error=None, tab="all", q="", page=1):
        conn = db.connect(self.server.db_path)
        try:
            if db.get_setting(conn, "market") is None:  # first start: nothing to show until a realm is chosen
                html = welcome_page(scanner.known_markets(conn), error, blocked=db.get_setting(conn, "blocked") == "1")
            else:
                html = render_page(build_state(conn, datetime.now(UTC), q), error, tab=tab, page=page)
        finally:
            conn.close()
        self._send(code, html, "text/html; charset=utf-8")

    def do_GET(self):
        if not self._allowed(False):
            return self._send(403, "forbidden")
        path = urlsplit(self.path).path
        if path == "/":
            self._page(tab=_tab(self.path), q=parse_qs(urlsplit(self.path).query).get("q", [""])[0][:60],
                       page=_page_no(self.path))
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
            return self._page(400, str(e), _tab(self.headers.get("Referer")), page=_page_no(self.headers.get("Referer")))
        finally:
            conn.close()
        ref = self.headers.get("Referer")
        self._send(303, headers=[("Location", _at(_tab(ref), _page_no(ref)))])


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        log.exception("page request failed")  # the desktop app has no console for the default traceback


def make_server(db_path: str, port: int) -> ThreadingHTTPServer:
    srv = Server(("127.0.0.1", port), Handler)
    srv.db_path = db_path
    return srv
