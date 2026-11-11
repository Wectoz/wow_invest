"""Polite HTTP client for the blackwater.exchange JSON API, plus JSON -> models parsers."""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import config
from .models import Detail, Point, Row


class ApiError(Exception):
    def __init__(self, msg="", transient=True):
        super().__init__(msg)
        self.transient = transient  # False: retrying will not help (4xx, wrong shape)


def _fetch(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
        return r.read()


def _int(v):
    return None if v is None else round(v)


def parse_row(d):
    return Row(
        item_id=d["id"], name=d.get("name"), icon=d.get("icon"), quality=d.get("quality"),
        class_name=d.get("class_name"), subclass_name=d.get("subclass_name"),
        required_level=d.get("required_level"), item_level=d.get("item_level"),
        min_ask=_int(d.get("minimum_ask_copper")), avg_ask=_int(d.get("average_ask_copper")),
        units=d.get("units") or 0, listings=d.get("listings") or 0, sold_24h=d.get("sold_24h") or 0,
        vendor_sell=_int(d.get("vendor_sell_copper")),
    )


def parse_detail(d, house, cut):
    it = d.get("item") or {}
    ladder = sorted((round(x["unit_price_copper"]), x.get("units") or 0) for x in d.get("ladder") or []
                    if x.get("unit_price_copper") is not None)
    series = [Point(at=datetime.fromisoformat(s["captured_at"]), units=s.get("units") or 0,
                    min_ask=_int(s.get("minimum_ask_copper")), partial_fills=s.get("partial_fills") or 0,
                    early=s.get("early_disappearances") or 0) for s in d.get("series") or []]
    return Detail(item_id=it["id"], house=house, ladder=ladder, series=series, cut=cut,
                  purchase_price=it.get("purchase_price") or 0, purchase_quantity=it.get("purchase_quantity") or 1,
                  sell_price=it.get("sell_price") or 0, required_level=it.get("required_level"))


def _shaped(fn):
    """Wrong-shape JSON (error object, missing key, bad timestamp) becomes ApiError."""
    def wrap(self, *a, **k):
        try:
            return fn(self, *a, **k)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ApiError(f"unexpected response in {fn.__name__}{a}: {e!r}", transient=False) from e
    return wrap


class Client:
    def __init__(self, fetch=None, sleep=time.sleep, clock=time.monotonic):
        self._fetch, self._sleep, self._clock, self._last = fetch or _fetch, sleep, clock, None

    def get_json(self, path):
        url, delays = config.BASE_URL + path, [*config.RETRY_DELAYS_S, None]
        for delay in delays:
            if self._last is not None:
                wait = config.REQUEST_GAP_S - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            retry_after = None
            try:
                raw = self._fetch(url, {"User-Agent": config.USER_AGENT})
                self._last = self._clock()
                return json.loads(raw)
            except urllib.error.HTTPError as e:
                self._last = self._clock()
                if e.code == 404 or not (e.code == 429 or e.code >= 500):
                    raise ApiError(f"HTTP {e.code} for {path}", transient=False) from e
                ra = e.headers.get("Retry-After") if e.headers else None
                retry_after = int(ra) if ra and ra.isdigit() else None
                err = e
            except (urllib.error.URLError, ValueError, OSError) as e:  # ValueError covers bad JSON
                self._last = self._clock()
                err = e
            if delay is None:
                raise ApiError(f"giving up on {path}: {err}") from err
            self._sleep(retry_after if retry_after is not None else delay)

    @_shaped
    def scan_times(self):
        out = {}
        for m in self.get_json("/v1/markets"):
            for name, key in config.HOUSES.items():
                region, rs, house = key.split("/")
                if (m["region"], m["ruleset_id"], m["house_id"]) == (region, int(rs), int(house)):
                    out[name] = (m["captured_at"], m["auctions_at"])
        return out

    @_shaped
    def all_items(self, house):
        key, rows, offset = config.HOUSES[house], [], 0
        while True:
            d = self.get_json(f"/v1/markets/{key}/items?limit=200&offset={offset}")
            rows += [parse_row(x) for x in d["items"]]
            offset += 200
            if offset >= d["total"] or not d["items"]:
                return rows

    @_shaped
    def deals(self, house, limit=100):
        d = self.get_json(f"/v1/markets/{config.HOUSES[house]}/deals?limit={limit}")
        return [x["item_id"] for x in d["items"]], d["auction_cut"]

    @_shaped
    def item_detail(self, house, item_id, cut):
        return parse_detail(self.get_json(f"/v1/markets/{config.HOUSES[house]}/items/{item_id}"), house, cut)
