import json
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from wow_invest import config
from wow_invest.api import ApiError, Client, parse_detail, parse_row


class Fake:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        it = self.items.pop(0)
        if isinstance(it, Exception):
            raise it
        return it if isinstance(it, bytes) else json.dumps(it).encode()


def http(code, retry_after=None):
    h = Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return HTTPError("u", code, "m", h, None)


def client(*items, times=None):
    f, sleeps, t = Fake(*items), [], iter(times or [0] * 50)
    return Client(fetch=f, sleep=sleeps.append, clock=lambda: next(t)), f, sleeps


def test_parse_row_keeps_nulls_and_rounds_prices():
    r = parse_row({"id": 14551, "name": None, "icon": None, "quality": None, "class_name": None, "subclass_name": None,
                   "required_level": None, "item_level": None, "minimum_ask_copper": 1800000.0, "average_ask_copper": 1800000.4,
                   "units": 2, "listings": 2, "sold_24h": 6, "vendor_sell_copper": None})
    assert (r.name, r.min_ask, r.avg_ask, r.required_level) == (None, 1800000, 1800000, None)


def test_parse_detail_sorts_ladder_and_zeroes_null_counts():
    d = parse_detail({"item": {"id": 5, "purchase_price": 2000, "purchase_quantity": 1, "sell_price": 500, "required_level": 0},
                      "ladder": [{"units": 3, "unit_price_copper": 200.0}, {"units": 1, "unit_price_copper": 100.0}],
                      "series": [{"captured_at": "2026-11-08T20:13:51.301379+00:00", "units": 4, "minimum_ask_copper": None,
                                  "partial_fills": None, "early_disappearances": None}]}, "alliance", 0.05)
    assert d.ladder == [(100, 1), (200, 3)]
    p = d.series[0]
    assert (p.partial_fills, p.early, p.min_ask, p.at.utcoffset().total_seconds()) == (0, 0, None, 0)
    assert (d.cut, d.house, d.purchase_price) == (0.05, "alliance", 2000)


def test_get_json_waits_between_requests():
    c, _, sleeps = client({"a": 1}, {"a": 2}, times=[0, 0.2, 0.2])
    c.get_json("/x"); c.get_json("/x")
    assert sleeps == [0.8]


def test_get_json_retries_5xx_then_succeeds():
    c, f, sleeps = client(http(503), http(503), {"ok": 1})
    assert c.get_json("/x") == {"ok": 1}
    assert 2 in sleeps and 4 in sleeps


def test_get_json_raises_after_three_failures():
    c, f, _ = client(b"no", b"no", b"no", b"no")
    with pytest.raises(ApiError):
        c.get_json("/x")
    assert len(f.calls) == 4


def test_get_json_retries_urlerror():
    c, _, _ = client(URLError("x"), {"ok": 1})
    assert c.get_json("/x") == {"ok": 1}


def test_get_json_404_raises_without_retry():
    c, f, _ = client(http(404))
    with pytest.raises(ApiError):
        c.get_json("/x")
    assert len(f.calls) == 1


def test_all_items_pages_until_total():
    row = {"id": 1, "units": 1, "listings": 1, "sold_24h": 0}
    c, f, _ = client({"items": [row] * 200, "total": 450}, {"items": [row] * 200, "total": 450},
                     {"items": [row] * 50, "total": 450})
    assert len(c.all_items("alliance")) == 450
    assert [u.split("offset=")[1] for u, _ in f.calls] == ["0", "200", "400"]


def test_scan_times_maps_both_houses():
    m = lambda h, cap, au: {"region": "eu", "ruleset_id": 2, "house_id": h, "captured_at": cap, "auctions_at": au}
    c, _, _ = client([m(111, "c1", "a1"), m(5, "x", "y"), m(117, "c2", "a2")])
    assert c.scan_times() == {"alliance": ("c1", "a1"), "blackwater": ("c2", "a2")}


def test_deals_returns_ids_and_cut():
    c, f, _ = client({"items": [{"item_id": 8494}, {"item_id": 7}], "auction_cut": 0.05})
    assert c.deals("alliance", 2) == ([8494, 7], 0.05)
    assert f.calls[0][0].endswith("/v1/markets/eu/2/111/deals?limit=2")


def test_parse_detail_unknown_item_does_not_raise():
    d = parse_detail({"item": {"id": 1, "name": None}}, "alliance", 0.05)
    assert (d.purchase_price, d.ladder, d.series) == (0, [], [])


def test_get_json_honours_retry_after_on_429():
    c, _, sleeps = client(http(429, 30), {"ok": 1})
    c.get_json("/x")
    assert 30 in sleeps and 2 not in sleeps


def test_requests_send_generic_user_agent():
    c, f, _ = client({"ok": 1})
    c.get_json("/x")
    assert f.calls[0][1]["User-Agent"] == config.USER_AGENT


def test_wrong_shape_items_response_is_api_error():
    c, _, _ = client({"error": "x"})
    with pytest.raises(ApiError):
        c.all_items("alliance")


def test_item_detail_bad_timestamp_is_api_error():
    c, _, _ = client({"item": {"id": 1}, "series": [{"captured_at": "garbage"}]})
    with pytest.raises(ApiError):
        c.item_detail("alliance", 1, 0.05)
