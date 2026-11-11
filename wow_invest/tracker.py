from wow_invest import db
from wow_invest.config import JUDGE_RUN


def judge(conn, now) -> int:
    n = 0
    for p in db.open_picks(conn):
        if now.isoformat() <= p["sell_by"]:
            continue
        # LEFT JOIN: a scan with no listing yields NULL and breaks the run
        asks = conn.execute(
            """SELECT s.min_ask FROM scans c
               LEFT JOIN snapshots s ON s.scan_id = c.id AND s.item_id = ?
               WHERE c.house = ? AND c.captured_at > ? AND c.captured_at <= ?
               ORDER BY c.captured_at""",
            (p["item_id"], p["sell_at"], p["created_at"], p["sell_by"])).fetchall()
        run = best = 0
        for (a,) in asks:
            run = run + 1 if a is not None and a >= p["sell_price"] else 0
            best = max(best, run)
        db.judge_pick(conn, p["id"], best >= JUDGE_RUN, now)
        n += 1
    return n
