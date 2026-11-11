"""Page rendering and the localhost HTTP routes."""
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urlsplit

from . import config as C
from . import db
from .planner import holds_proven, plan
from .pricing import deposit, undercut

HERE = Path(__file__).parent
ICON_HOST = "https://render.worldofwarcraft.com/"
MAX_BODY = 10_240
MAX_GOLD = 10_000_000
KINDS = {"flip": ("Quick flip", "flip"), "cross": ("Cross-AH", "flip"), "hold": ("Hold", "hold"),
         "vendor": ("Vendor flip", "vendor"), "vendor_check": ("Check vendor", "vendor")}
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


def sell_advice(h, current: int | None, cut: float, dep: int, now: datetime) -> tuple[str, int | None]:
    """kind: nolist | wait | list | loss. Price is per unit."""
    if current is None:
        return "nolist", h.target
    breakeven = math.ceil(h.price_paid / h.qty / (1 - cut)) + dep
    if h.sell_by is not None and now >= h.sell_by:
        return "loss", undercut(current)
    if current < breakeven:
        return "wait", None
    return "list", max(undercut(current), h.target)


def build_state(conn, now: datetime) -> dict:
    cands, captured = db.load_candidates(conn)
    holdings = db.open_holdings(conn)
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
        dep = deposit(it["vendor_sell"] if it else None, h.sell_at)
        kind, price = sell_advice(h, cur, cut, dep, now)
        loss = max(0, math.ceil(h.price_paid / h.qty / (1 - cut)) + dep - (price or 0)) * h.qty
        held.append({"h": h, "name": it["name"] if it else None, "icon": it["icon"] if it else None,
                     "quality": it["quality"] if it else None, "current": cur,
                     "kind": kind, "price": price, "loss": loss})
    p = plan(cands, db.get_gold(conn), holdings, holds_proven(stats))
    oldest = conn.execute("SELECT min(captured_at) FROM scans WHERE house='alliance'").fetchone()[0]
    history_ok = bool(oldest) and now - _aware(oldest) >= timedelta(hours=C.MIN_HISTORY_HOURS)
    last_ok = _aware(captured) or (_aware(ali["captured_at"]) if ali else None)
    auctions = _aware(ali["auctions_at"]) if ali else None
    return {
        "gold": db.get_gold(conn), "plan": p, "holdings": held, "stats": stats,
        "crowd": db.get_setting(conn, "crowd"), "stale": db.get_setting(conn, "stale") == "1",
        "gathering": not cands and not history_ok,
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
    if p.kind in ("vendor", "vendor_check"):
        return f"Vendor → {HOUSE.get(p.sell_at, p.sell_at)}"
    return HOUSE.get(p.sell_at, p.sell_at)


def _name(item_id, name) -> str:
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


def _pick_row(p, rank=None, form=True, check=False, dim=False) -> str:
    label, tcls = KINDS.get(p.kind, (p.kind, ""))
    unf = " unfunded" if dim and not p.funded else ""
    note = '<span class="note">not enough gold</span>' if unf else ""
    sb = f"<p>Sell by {_hm(p.sell_by)}</p>" if p.sell_by else ""
    if check:
        side = '<form method="post" action="/vendor-confirm"><input type="hidden" name="item_id" value="%s">' \
               '<button>A vendor sells this</button></form>' % esc(p.item_id)
        body = ""
    else:
        side = (f'{p.qty} × {fmt_money(p.max_unit_price)} → {fmt_money(p.sell_price)}<br>'
                f'<span class="profit">+{fmt_money(p.profit)}</span> · ~{p.days:.0f} days · sure '
                f'<span class="conf"><i style="width:{int(p.sure * 100)}%"></i></span>')
        trend = "trend unknown" if p.trend is None else f"price {p.trend * 100:+.0f}% vs a day ago"
        body = (f'<details><summary>Why</summary><p>{esc(p.reason)}</p>'
                f'<p>≈{p.spd:.0f} sold/day (estimate) · {p.listed} listed on the sell AH · {trend}'
                f' · {p.depth} listed just above your price</p>{sb}</details>'
                + (f'<details class="bought"><summary>I bought this</summary>{_bought_form(p)}</details>' if form else ""))
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
    sb = f' · sell by {_hm(h.sell_by)}' if h.sell_by else ""
    return (f'<div class="held"><div class="row"><div></div>{_ico(v["icon"], v["quality"])}'
            f'<div><span class="name {_qcls(v["quality"])}">{_name(h.item_id, v["name"])}</span>'
            f'<span class="tag">{esc(KINDS.get(h.kind, (h.kind,))[0])}</span><span class="tag">{esc(HOUSE.get(h.sell_at, h.sell_at))}</span>'
            f'<div class="why">{h.qty} × paid {fmt_money(h.price_paid // h.qty)}, target {fmt_money(h.target)}{sb}</div></div>'
            f'<div class="nums">{adv}<br><form class="inline" method="post" action="/sold">'
            f'<input type="hidden" name="holding_id" value="{esc(h.id)}"><button>Sold</button></form></div></div></div>')


def _rows(items, empty, fn) -> str:
    return "".join(fn(i, n) for n, i in enumerate(items, 1)) or f'<div class="empty">{empty}</div>'


def render_page(state: dict, error: str | None = None, css_href: str = "/static/style.css") -> str:
    p, st = state["plan"], state["stats"]
    ban = []
    if error:
        ban.append(f'<div class="banner err">{esc(error)}</div>')
    if state["gathering"]:
        ban.append('<div class="banner">Gathering price history… picks appear once about '
                   f'{C.MIN_HISTORY_HOURS} hours of scans exist.</div>')
    if state["stale"]:
        ban.append('<div class="banner">Site data is stale: the auction snapshot is old, so no new picks are saved.</div>')
    if state["scan_age_min"] is not None and state["scan_age_min"] > 90:
        ban.append(f'<div class="banner">Last scan {state["scan_age_min"]} minutes ago. Showing the last good data.</div>')
    age = (f'AH data from {_hm(state["auctions_at"])} ({state["data_age_min"]} min old)'
           if state["auctions_at"] else "No AH data yet")
    nxt = f' · next scan about {_hm(state["next_scan"])}' if state["next_scan"] else ""
    hit = f'{st["hits"] * 100 // st["judged"]}% of {st["judged"]} judged' if st["judged"] else "none judged yet"
    last = _hm(state["last_scan"]) if state["last_scan"] else "never"
    return Template((HERE / "page.html").read_text(encoding="utf-8")).substitute(
        css_href=esc(css_href), banners="".join(ban),
        sub=f'Alliance · Normal (PvE) · EU — Alliance AH + Blackwater AH — {esc(age)}{esc(nxt)}',
        paper_rate=esc(f'{st["hold_hits"]} of {st["hold_judged"]} hit ({st["hold_hits"] * 100 // st["hold_judged"]}%)'
                       if st["hold_judged"] else "none judged yet"),
        gold_text=esc(plain_gold(state["gold"])), spend=fmt_money(p.spend), profit=fmt_money(p.profit),
        crowd=f'lvl {esc(state["crowd"])}' if state["crowd"] else "unknown",
        last_scan=esc(last), hit_rate=esc(hit),
        buy=_rows(p.ranked, "Nothing worth buying right now.", lambda x, n: _pick_row(x, n, dim=True)),
        sell=_rows(state["holdings"], "Nothing held.", lambda x, n: _held_row(x)),
        paper=_rows(p.paper, "No paper holds.", lambda x, n: _pick_row(x, n)),
        checks=_rows(p.checks, "Nothing to check.", lambda x, n: _pick_row(x, None, False, True)))


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
    if kind not in ("flip", "cross", "vendor", "hold"):
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


ROUTES = {
    "/gold": lambda c, f, now: db.set_gold(c, parse_gold(f.get("gold", ""))),
    "/bought": _bought,
    "/sold": lambda c, f, now: db.mark_sold(c, _int(f.get("holding_id"), "Holding", 1, 10**12), now),
    "/vendor-confirm": lambda c, f, now: db.confirm_vendor(c, _int(f.get("item_id"), "Item", 1, 10**9)),
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

    def _page(self, code=200, error=None):
        conn = db.connect(self.server.db_path)
        try:
            html = render_page(build_state(conn, datetime.now(UTC)), error)
        finally:
            conn.close()
        self._send(code, html, "text/html; charset=utf-8")

    def do_GET(self):
        if not self._allowed(False):
            return self._send(403, "forbidden")
        path = urlsplit(self.path).path
        if path == "/":
            self._page()
        elif path == "/static/style.css":
            self._send(200, (HERE / "static" / "style.css").read_bytes(), "text/css; charset=utf-8")
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
            return self._page(400, str(e))
        finally:
            conn.close()
        self._send(303, headers=[("Location", "/")])


class Server(ThreadingHTTPServer):
    daemon_threads = True


def make_server(db_path: str, port: int) -> ThreadingHTTPServer:
    srv = Server(("127.0.0.1", port), Handler)
    srv.db_path = db_path
    return srv
