import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from wow_invest.config import PROVEN_RECENT
from wow_invest.models import Detail, Holding, Pick, Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, house TEXT, captured_at TEXT,
  auctions_at TEXT, cut REAL, crowd TEXT, UNIQUE(house, captured_at));
CREATE TABLE IF NOT EXISTS snapshots(scan_id INTEGER, item_id INTEGER, min_ask INTEGER,
  units INTEGER, sold_24h INTEGER, PRIMARY KEY(scan_id, item_id));
CREATE TABLE IF NOT EXISTS items(item_id INTEGER PRIMARY KEY, name, icon, quality, class_name,
  subclass_name, required_level, item_level, vendor_sell, purchase_price, purchase_quantity,
  static_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS picks(id INTEGER PRIMARY KEY, created_at TEXT, kind TEXT,
  item_id INTEGER, sell_at TEXT, qty INTEGER, cost INTEGER, sell_price INTEGER,
  profit INTEGER, sell_by TEXT, judged_at TEXT, hit INTEGER);
CREATE TABLE IF NOT EXISTS holdings(id INTEGER PRIMARY KEY, item_id INTEGER, kind TEXT,
  qty INTEGER, price_paid INTEGER, target INTEGER, bought_at TEXT, sell_by TEXT, sold_at TEXT, sell_at TEXT NOT NULL DEFAULT 'alliance');
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS vendor_confirmed(item_id INTEGER PRIMARY KEY);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    if "sell_at" not in {r[1] for r in conn.execute("PRAGMA table_info(holdings)")}:
        conn.execute("ALTER TABLE holdings ADD COLUMN sell_at TEXT NOT NULL DEFAULT 'alliance'")
    return conn


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def last_captured(conn, house):
    r = conn.execute("SELECT max(captured_at) FROM scans WHERE house=?", (house,)).fetchone()
    return r[0]


def save_scan(conn, house, captured_at, auctions_at, cut, rows: list[Row]) -> int:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO scans(house, captured_at, auctions_at, cut) VALUES(?,?,?,?)",
            (house, captured_at, auctions_at, cut))
        sid = conn.execute("SELECT id FROM scans WHERE house=? AND captured_at=?",
                           (house, captured_at)).fetchone()[0]
        conn.executemany(
            "INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)",
            [(sid, r.item_id, r.min_ask, r.units, r.sold_24h) for r in rows])
        conn.executemany(
            """INSERT INTO items(item_id, name, icon, quality, class_name, subclass_name,
                 required_level, item_level, vendor_sell) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                 name=COALESCE(excluded.name, name), icon=COALESCE(excluded.icon, icon),
                 quality=COALESCE(excluded.quality, quality),
                 class_name=COALESCE(excluded.class_name, class_name),
                 subclass_name=COALESCE(excluded.subclass_name, subclass_name),
                 required_level=COALESCE(excluded.required_level, required_level),
                 item_level=COALESCE(excluded.item_level, item_level),
                 vendor_sell=COALESCE(excluded.vendor_sell, vendor_sell)""",
            [(r.item_id, r.name, r.icon, r.quality, r.class_name, r.subclass_name,
              r.required_level, r.item_level, r.vendor_sell) for r in rows])
    return sid


def set_scan_crowd(conn, scan_id, crowd: tuple[int, int]):
    with conn:
        conn.execute("UPDATE scans SET crowd=? WHERE id=?", (f"{crowd[0]}-{crowd[1]}", scan_id))


def latest_scan(conn, house) -> sqlite3.Row | None:
    return conn.execute("SELECT captured_at, auctions_at, cut FROM scans WHERE house=? "
                        "ORDER BY captured_at DESC LIMIT 1", (house,)).fetchone()


def rows_at(conn, house, hours_ago: float, now) -> dict[int, tuple[int | None, int, int]]:
    cutoff = _iso(now - timedelta(hours=hours_ago))
    s = conn.execute("SELECT id FROM scans WHERE house=? AND captured_at<=? "
                     "ORDER BY captured_at DESC LIMIT 1", (house, cutoff)).fetchone()
    if not s:
        return {}
    return {r["item_id"]: (r["min_ask"], r["units"], r["sold_24h"]) for r in conn.execute(
        "SELECT item_id, min_ask, units, sold_24h FROM snapshots WHERE scan_id=?", (s["id"],))}


def missing_static(conn, limit: int) -> list[int]:
    return [r[0] for r in conn.execute(
        """SELECT i.item_id FROM items i
           LEFT JOIN snapshots s ON s.item_id = i.item_id AND s.scan_id =
             (SELECT max(scan_id) FROM snapshots WHERE item_id = i.item_id)
           WHERE i.static_done = 0 ORDER BY coalesce(s.sold_24h, 0) DESC, i.item_id LIMIT ?""",
        (limit,))]


def save_static(conn, d: Detail):
    with conn:
        conn.execute(
            """INSERT INTO items(item_id, required_level, purchase_price, purchase_quantity,
                 static_done) VALUES(?,?,?,?,1)
               ON CONFLICT(item_id) DO UPDATE SET
                 required_level=COALESCE(excluded.required_level, required_level),
                 purchase_price=excluded.purchase_price,
                 purchase_quantity=excluded.purchase_quantity, static_done=1""",
            (d.item_id, d.required_level, d.purchase_price, d.purchase_quantity))


def mark_static_bad(conn, item_id):
    with conn:
        conn.execute("UPDATE items SET static_done=-1 WHERE item_id=?", (item_id,))


def static(conn, item_id):
    return conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()


def set_setting(conn, key, value: str):
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def get_gold(conn) -> int:
    return int(get_setting(conn, "gold", "0"))


def set_gold(conn, copper: int):
    set_setting(conn, "gold", str(copper))


def add_holding(conn, item_id, kind, qty, price_paid, target, now, sell_by, sell_at="alliance") -> int:
    with conn:
        return conn.execute(
            "INSERT INTO holdings(item_id, kind, qty, price_paid, target, bought_at, sell_by, sell_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (item_id, kind, qty, price_paid, target, _iso(now), _iso(sell_by), sell_at)).lastrowid


def mark_sold(conn, holding_id, now):
    with conn:
        conn.execute("UPDATE holdings SET sold_at=? WHERE id=?", (_iso(now), holding_id))


def open_holdings(conn) -> list[Holding]:
    return [Holding(r["id"], r["item_id"], r["kind"], r["qty"], r["price_paid"], r["target"],
                    _dt(r["bought_at"]), _dt(r["sell_by"]), None, r["sell_at"])
            for r in conn.execute("SELECT * FROM holdings WHERE sold_at IS NULL ORDER BY id")]


def held_units(conn) -> dict[int, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT item_id, sum(qty) FROM holdings WHERE sold_at IS NULL GROUP BY item_id")}


def confirm_vendor(conn, item_id):
    with conn:
        conn.execute("INSERT OR IGNORE INTO vendor_confirmed VALUES(?)", (item_id,))


def confirmed_vendors(conn) -> set[int]:
    return {r[0] for r in conn.execute("SELECT item_id FROM vendor_confirmed")}


def save_candidates(conn, picks: list[Pick], captured_at: str):
    items = [{**asdict(p), "sell_by": _iso(p.sell_by)} for p in picks]
    set_setting(conn, "candidates", json.dumps({"captured_at": captured_at, "picks": items}))


def load_candidates(conn) -> tuple[list[Pick], str | None]:
    raw = get_setting(conn, "candidates")
    if not raw:
        return [], None
    d = json.loads(raw)
    return [Pick(**{**p, "sell_by": _dt(p["sell_by"])}) for p in d["picks"]], d["captured_at"]


def record_picks(conn, picks: list[Pick], now) -> int:
    n = 0
    with conn:
        for p in picks:
            if conn.execute("SELECT 1 FROM picks WHERE item_id=? AND kind=? AND judged_at IS NULL",
                            (p.item_id, p.kind)).fetchone():
                continue
            conn.execute(
                "INSERT INTO picks(created_at, kind, item_id, sell_at, qty, cost, sell_price, "
                "profit, sell_by) VALUES(?,?,?,?,?,?,?,?,?)",
                (_iso(now), p.kind, p.item_id, p.sell_at, p.qty, p.cost, p.sell_price,
                 p.profit, _iso(p.sell_by)))
            n += 1
    return n


def open_picks(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM picks WHERE judged_at IS NULL ORDER BY id").fetchall()


def judge_pick(conn, pick_id, hit: bool, now):
    with conn:
        conn.execute("UPDATE picks SET judged_at=?, hit=? WHERE id=?",
                     (_iso(now), int(hit), pick_id))


def hit_stats(conn) -> dict:
    j, h = conn.execute("SELECT count(*), coalesce(sum(hit),0) FROM picks "
                        "WHERE judged_at IS NOT NULL").fetchone()
    hold = conn.execute(
        """SELECT count(*), coalesce(sum(hit),0), count(DISTINCT item_id),
                  count(DISTINCT substr(created_at,1,10)) FROM (
             SELECT * FROM picks WHERE kind='hold' AND judged_at IS NOT NULL
             ORDER BY judged_at DESC, id DESC LIMIT ?)""", (PROVEN_RECENT,)).fetchone()
    return {"judged": j, "hits": h, "hold_judged": hold[0], "hold_hits": hold[1],
            "hold_items": hold[2], "hold_days": hold[3]}


def prune(conn, now, days=30):
    # shortcut: fixed 30-day retention, make configurable if history analysis needs more
    cutoff = _iso(now - timedelta(days=days))
    with conn:
        conn.execute("DELETE FROM snapshots WHERE scan_id IN "
                     "(SELECT id FROM scans WHERE captured_at < ?)", (cutoff,))
        conn.execute("DELETE FROM scans WHERE captured_at < ?", (cutoff,))
