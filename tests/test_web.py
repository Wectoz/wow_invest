import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.conftest import NOW
from wow_invest import db
from wow_invest.models import Pick, Row
from wow_invest.pricing import undercut
from wow_invest.web import build_state, fmt_money, make_server, parse_gold, render_page, safe_icon

ICON = "https://render.worldofwarcraft.com/forever-eu/icons/56/1.jpg"


def mkpick(item_id=1, name="Linen Cloth", icon=ICON, kind="flip", cost=195000, funded=True, **kw):
    return Pick(kind, item_id, name, icon, "Uncommon", "alliance", "alliance", 5, cost, 40000,
                60000, 50000, 2.0, 1.0, 0.8, "cheap now", 4.0, 0.1, 7,
                sell_by=NOW + timedelta(days=3), funded=funded, **kw)


def seed(path, picks=(), captured=NOW, gold=800 * 10000):
    c = db.connect(path)
    db.set_gold(c, gold)
    if picks:
        db.save_candidates(c, list(picks), captured.isoformat())
    return c


def state_html(c, error=None, now=NOW):
    return render_page(build_state(c, now), error)


def test_fmt_money():
    assert "2g" in fmt_money(25000) and "50s" in fmt_money(25000) and 'class="c"' not in fmt_money(25000)
    assert 'class="c"' in fmt_money(25050)
    assert fmt_money(0) == "0c"


def test_parse_gold_accepts_and_rejects():
    assert parse_gold("800") == 8_000_000 and parse_gold("800g 50s") == 8_005_000 and parse_gold("1 234") == 12_340_000
    assert parse_gold("800.5") == 8_005_000
    for bad in ["", "-5", "abc", "1e99", "99999999"]:
        with pytest.raises(ValueError):
            parse_gold(bad)


def test_safe_icon():
    assert safe_icon(ICON) == ICON
    assert safe_icon("https://evil/x.png") is None and safe_icon(None) is None


def test_render_escapes_api_strings_and_drops_foreign_icons():
    c = seed(":memory:", [mkpick(name="<script>", icon="https://evil/x.png")])
    h = state_html(c)
    assert "&lt;script&gt;" in h and "<script>" not in h and "evil" not in h


def test_render_escapes_error():
    assert "<b>" not in state_html(seed(":memory:"), error="<b>x")


def test_render_null_name_falls_back():
    assert "Item 14551" in state_html(seed(":memory:", [mkpick(item_id=14551, name=None)]))


def test_render_empty_state_says_gathering_history():
    h = state_html(seed(":memory:", captured=NOW))
    assert "Gathering price history" in h and 'http-equiv="refresh"' in h


def test_no_gathering_banner_when_history_long_enough():
    c = seed(":memory:")
    db.save_scan(c, "alliance", (NOW - timedelta(hours=13)).isoformat(), NOW.isoformat(), 0.05, [])
    assert "Gathering price history" not in state_html(c)


def test_render_stale_banner_and_age():
    c = seed(":memory:", [mkpick()])
    db.save_scan(c, "alliance", NOW.isoformat(), (NOW - timedelta(minutes=48)).isoformat(), 0.05, [])
    assert "Site data is stale" not in state_html(c)
    db.set_setting(c, "stale", "1")
    h = state_html(c)
    assert "Site data is stale" in h and "48 min old" in h


def test_last_scan_banner_uses_candidates_age():
    c = seed(":memory:", [mkpick()], captured=NOW - timedelta(minutes=120))
    db.save_scan(c, "alliance", NOW.isoformat(), NOW.isoformat(), 0.05, [])  # newer scan, errored cycle
    assert "Last scan 120 minutes ago" in state_html(c)
    assert "minutes ago" not in state_html(seed(":memory:", [mkpick()]))


def test_render_unfunded_rows_marked():
    h = state_html(seed(":memory:", [mkpick(funded=False)], gold=0))
    assert "unfunded" in h and "not enough gold" in h
    assert "not enough gold" not in state_html(seed(":memory:", [mkpick()]))


def test_pick_bought_after_the_scan_is_hidden_until_the_next_scan():
    c = seed(":memory:", [mkpick(1), mkpick(2)], captured=NOW - timedelta(minutes=30))
    db.add_holding(c, 1, "flip", 5, 195000, 60000, NOW - timedelta(minutes=10), None)  # after the scan
    db.add_holding(c, 2, "flip", 5, 195000, 60000, NOW - timedelta(hours=1), None)  # the scan counted it
    assert [p.item_id for p in build_state(c, NOW)["plan"].ranked] == [2]


def test_bought_form_defaults():
    h = state_html(seed(":memory:", [mkpick()]))
    assert 'name="price_paid" value="19g 50s"' in h and 'name="target" value="60000"' in h
    assert 'action="/bought"' in h


def sell_html(min_ask, sell_by, paid=10000, target=15000, vendor_sell=None, house="alliance", cut=0.05):
    c = seed(":memory:")
    db.save_scan(c, house, NOW.isoformat(), NOW.isoformat(), cut,
                 [Row(5, "Ore", ICON, "Common", None, None, 1, 1, min_ask, min_ask, 3, 1, 1, vendor_sell)])
    db.add_holding(c, 5, "hold", 1, paid, target, NOW - timedelta(days=1), sell_by, house)
    return state_html(c)


def listing(price):
    return f"list at {fmt_money(price)}"


def test_sell_now_never_suggests_below_cost_before_sell_by():
    h = sell_html(8000, NOW + timedelta(days=1))
    assert "below cost — wait until sell-by" in h and "list at" not in h and "now " + fmt_money(8000) in h
    h = sell_html(20000, NOW + timedelta(days=1))
    assert "below cost" not in h and listing(undercut(20000)) in h and "now " + fmt_money(20000) in h
    h = sell_html(11000, NOW + timedelta(days=1), target=19000)  # break-even 10527
    assert listing(19000) in h


def test_sell_now_after_sell_by_and_unlisted():
    h = sell_html(8000, NOW - timedelta(hours=1))
    assert "sell-by passed: " + listing(undercut(8000)) in h and "(loss " in h and "below cost" not in h
    assert "now " + fmt_money(8000) in h
    h = sell_html(None, NOW + timedelta(days=1))
    assert "no listings — list at target " + fmt_money(15000) in h


def test_blackwater_holding_uses_blackwater_price_and_cut():
    # break-even at 15% cut is 11765 > 11000, so wait; the 5% cut would have said list
    h = sell_html(11000, NOW + timedelta(days=1), house="blackwater", cut=0.15)
    assert "below cost — wait until sell-by" in h and "Blackwater AH" in h
    assert "below cost" not in sell_html(11000, NOW + timedelta(days=1), house="alliance", cut=0.05)


def test_bought_form_visible_on_ranked_and_paper_not_inside_why():
    c = seed(":memory:", [mkpick(1), mkpick(2, kind="hold")])
    h = state_html(c)
    assert h.count('action="/bought"') == 2 and h.count("I bought this") == 4  # summary + button each
    why = h[h.index("<summary>Why</summary>"):]
    assert 'action="/bought"' not in why[:why.index("</details>")]
    assert 'name="sell_at" value="alliance"' in h


def test_paper_rows_not_marked_unfunded_but_ranked_are():
    paper = mkpick(2, kind="hold", funded=False)
    h = state_html(seed(":memory:", [paper]))
    assert "unfunded" not in h.replace(".unfunded", "") and "not enough gold" not in h.replace(
        "do not spend real gold", "")


def test_paper_hit_rate_shown():
    c = seed(":memory:")
    assert "Hold hit rate: none judged yet" in state_html(c)
    st = build_state(c, NOW)
    st["stats"] = {**st["stats"], "hold_judged": 10, "hold_hits": 7}
    assert "7 of 10 hit (70%)" in render_page(st)


def test_labels_header_trend_depth():
    c = seed(":memory:", [replace(mkpick(1, kind="cross"), buy_at="blackwater", trend=-0.12, listed=740)])
    h = state_html(c)
    assert "Blackwater → Alliance" in h and "price -12% vs a day ago" in h and "7 listed just above your price" in h
    assert "≈4 sold/day (estimate) · 740 listed on the sell AH" in h and "sales per day" not in h
    assert "Alliance · Normal (PvE) · EU — Alliance AH + Blackwater AH" in h
    assert "trend unknown" in state_html(seed(":memory:", [replace(mkpick(1), trend=None)]))
    assert "Vendor → Alliance AH" in state_html(seed(":memory:", [mkpick(1, kind="vendor")]))


def test_html_security_headers_and_refresh():
    c = seed(":memory:")
    assert 'content="300;url=/"' in state_html(c)


@pytest.fixture
def server(tmp_path):
    path = str(tmp_path / "t.db")
    db.connect(path).close()
    srv = make_server(path, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    yield path, f"http://127.0.0.1:{port}", port
    srv.shutdown()
    srv.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def req(url, data=None, headers=None):
    r = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data is not None else None,
                               headers=headers or {})
    try:
        with urllib.request.build_opener(_NoRedirect).open(r) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_foreign_host_header_rejected(server):
    _, base, _ = server
    assert req(base + "/", headers={"Host": "evil.example:8765"})[0] == 403
    assert req(base + "/")[0] == 200


def test_post_gold_stores_and_redirects(server):
    path, base, _ = server
    assert req(base + "/gold", {"gold": "800"})[0] == 303
    assert db.get_gold(db.connect(path)) == 8_000_000


def test_post_gold_bad_input_keeps_old_value(server):
    path, base, _ = server
    req(base + "/gold", {"gold": "800"})
    code, body = req(base + "/gold", {"gold": "abc"})
    assert code == 400 and "Gold must be a number" in body
    assert db.get_gold(db.connect(path)) == 8_000_000


def test_post_with_foreign_origin_rejected(server):
    path, base, _ = server
    assert req(base + "/gold", {"gold": "5"}, {"Origin": "http://evil.example"})[0] == 403
    assert db.get_gold(db.connect(path)) == 0


def test_oversize_body_413_and_unknown_path_404(server):
    _, base, _ = server
    assert req(base + "/gold", {"gold": "1" * 20000})[0] == 413
    assert req(base + "/nope")[0] == 404
    assert req(base + "/static/../db.py")[0] == 404
    assert req(base + "/static/style.css")[0] == 200


def test_bought_then_sold_updates_holdings(server):
    path, base, port = server
    form = {"item_id": "7", "kind": "hold", "qty": "5", "price_paid": "19g 50s",
            "target": "50000", "sell_by": NOW.isoformat()}
    assert req(base + "/bought", form, {"Origin": f"http://localhost:{port}"})[0] == 303
    c = db.connect(path)
    (h,) = db.open_holdings(c)
    assert (h.item_id, h.qty, h.price_paid, h.target) == (7, 5, 195000, 50000)
    assert req(base + "/bought", {**form, "qty": "0"})[0] == 400
    assert req(base + "/sold", {"holding_id": str(h.id)})[0] == 303
    assert db.open_holdings(c) == []
    assert req(base + "/vendor-confirm", {"item_id": "7"})[0] == 303
    assert db.confirmed_vendors(c) == {7}


def raw(port, request: bytes):
    import socket
    with socket.create_connection(("127.0.0.1", port)) as sk:
        sk.sendall(request)
        sk.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sk.recv(4096):
            data += chunk
    return int(data.split()[1])


def test_post_body_edge_cases(server):
    _, _, port = server
    head = f"POST /gold HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n".encode()
    form = b"Content-Type: application/x-www-form-urlencoded\r\n"
    assert raw(port, head + form + b"Content-Length: -1\r\n\r\n") == 413
    assert raw(port, head + form + b"\r\n") == 411
    assert raw(port, head + b"Content-Type: text/plain\r\nContent-Length: 3\r\n\r\ngold") == 415
    bad = f"POST /gold HTTP/1.0\r\nHost: evil.example\r\n".encode()
    assert raw(port, bad + form + b"Content-Length: 0\r\n\r\n") == 403


def test_html_response_has_frame_headers(server):
    _, base, _ = server
    with urllib.request.urlopen(base + "/") as r:
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_bought_rejects_unknown_sell_house(server):
    _, base, _ = server
    form = {"item_id": "7", "kind": "hold", "qty": "5", "price_paid": "1", "target": "5", "sell_by": "",
            "sell_at": "evil"}
    code, body = req(base + "/bought", form)
    assert code == 400 and "Unknown sell house" in body
