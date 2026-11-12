"""Rebuild wow_invest/vendor_items.txt from the Questie database addon in a WoW Forever folder.

    python scripts/vendor_ids.py "C:/Program Files (x86)/World of Warcraft/_forever_"

Writes `item_id vendor_count` for every item that an Alliance-friendly (or neutral) vendor sells. QuestieDB stores each
field as base64 CBOR in its .toc: `## X-Item-<id>-14` is the vendor NPC list, `## X-Npc-<id>-S` the
small fields, where field 13 is the faction ("A", "H" or "AH").
"""
import base64
import re
import struct
import sys
from pathlib import Path

OUT = Path(__file__).parent.parent / "wow_invest" / "vendor_items.txt"
VENDORS, FACTION = 14, 13


def cbor(b, i=0):
    """Decode one CBOR value at b[i]; returns (value, next index). Only the types QuestieDB uses."""
    major, info = b[i] >> 5, b[i] & 31
    i += 1
    if major == 7:  # floats and simple values
        size = {25: 2, 26: 4, 27: 8}.get(info, 0)
        val = struct.unpack(">" + "efd"[size // 4], b[i:i + size])[0] if size else None
        return val, i + size
    if info < 24:
        n = info
    else:
        size = 1 << (info - 24)
        n, i = int.from_bytes(b[i:i + size], "big"), i + size
    if major == 0:
        return n, i
    if major == 1:
        return -1 - n, i
    if major in (2, 3):
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if major == 4:
        out = []
        for _ in range(n):
            v, i = cbor(b, i)
            out.append(v)
        return out, i
    if major == 5:
        out = {}
        for _ in range(n):
            k, i = cbor(b, i)
            out[k], i = cbor(b, i)
        return out, i
    raise ValueError(f"unsupported CBOR major type {major}")


def fields(toc: str, entity: str, field: str) -> dict[int, object]:
    """{entity id: decoded value} for one field; long values are split over `-1`, `-2`, ... lines."""
    raw: dict[int, str] = {}
    for m in re.finditer(rf"^## X-{entity}-(\d+)-{field}(?:-\d+)?: (.*)$", toc, re.M):
        if not m[2].startswith("~"):  # `~4~` only announces how many parts follow
            raw[int(m[1])] = raw.get(int(m[1]), "") + m[2].strip()
    return {i: cbor(base64.b64decode(s))[0] for i, s in raw.items()}


def main(wow_dir: str) -> int:
    toc = (Path(wow_dir) / "Interface/AddOns/QuestieDB/QuestieDB_Forever.toc").read_text(encoding="utf-8")
    def faction(small):  # a {field: value} map, or a positional list starting at field 1
        if isinstance(small, dict):
            return small.get(FACTION) or ""
        return (small[FACTION - 1] if len(small) >= FACTION else None) or ""

    alliance = {i for i, small in fields(toc, "Npc", "S").items() if "A" in faction(small)}
    ids = {i: len(alliance & set(npcs)) for i, npcs in fields(toc, "Item", str(VENDORS)).items()}
    ids = {i: n for i, n in sorted(ids.items()) if n}
    assert 2320 in ids and 2589 not in ids, "Coarse Thread is vendor-sold, Linen Cloth is not"
    OUT.write_text("".join(f"{i} {n}\n" for i, n in ids.items()))
    print(f"{len(ids)} vendor items -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
