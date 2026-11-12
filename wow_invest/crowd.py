"""Who is on the AH right now (crowd level range) and which level band an item serves."""
from . import config
from .tables import GATHER_BANDS


def level_sales(rows) -> dict[int, int]:
    """Equipment sold in the last 24h by required level: where the players are."""
    w = {}
    for r in rows:
        if r.class_name in ("Armor", "Weapon") and (r.required_level or 0) > 0 and r.sold_24h > 0:
            w[r.required_level] = w.get(r.required_level, 0) + r.sold_24h
    return w


def estimate_crowd(rows):
    w = level_sales(rows)
    total = sum(w.values())
    if total < config.CROWD_MIN_SALES:
        return config.CROWD_PRIOR

    def pct(q):
        run = 0
        for lvl in sorted(w):
            run += w[lvl]
            if run >= q * total:
                return lvl

    return pct(0.25), pct(0.75)


def item_band(item_id, required_level):
    if item_id in GATHER_BANDS:
        return GATHER_BANDS[item_id]
    if required_level and required_level > 0:
        return (max(1, required_level - 2), required_level + 3)
    return None


def overlaps(band, crowd):
    return band[0] <= crowd[1] and crowd[0] <= band[1]
