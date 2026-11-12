"""Rebuild wow_invest/item_icons.txt: tab-separated `item_id, icon file id, quality` for every item.

    python scripts/item_icons.py Item.csv ItemSparse.csv

The price site has no icon or quality for a few hundred items. Both tables are the game's own item
data, as CSV from https://wago.tools/db2/Item/csv?branch=wow_classic_era (and .../ItemSparse/...).
"""
import csv
import sys
from pathlib import Path

OUT = Path(__file__).parent.parent / "wow_invest" / "item_icons.txt"
QUALITY = ["POOR", "COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "ARTIFACT"]


def main(item_csv: str, sparse_csv: str) -> int:
    def column(path, name):
        with open(path, encoding="utf-8", newline="") as f:
            return {int(r["ID"]): int(r[name]) for r in csv.DictReader(f)}
    icons, quality = column(item_csv, "IconFileDataID"), column(sparse_csv, "OverallQualityID")
    rows = [(i, icon, QUALITY[quality[i]]) for i, icon in sorted(icons.items())
            if icon and 0 <= quality.get(i, -1) < len(QUALITY)]
    assert (3466, 133587, "COMMON") in rows, "Strong Flux is a white item with icon 133587"
    OUT.write_text("".join(f"{i}\t{icon}\t{q}\n" for i, icon, q in rows), encoding="utf-8")
    print(f"{len(rows)} items -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
