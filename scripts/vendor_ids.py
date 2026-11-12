"""Rebuild wow_invest/vendor_items.txt from the Questie database addon in a WoW Forever folder.

    python scripts/vendor_ids.py "C:/Program Files (x86)/World of Warcraft/_forever_" pfquest/db/items.lua

The second file gives vendor stock: db/items.lua from https://github.com/shagu/pfQuest (Classic 1.12
data, `["V"] = { [npc] = stock }` with 0 for unlimited). An item is limited when every Alliance
vendor known there has a stock number. Items none of whose vendors pfQuest knows are marked unknown.

Writes tab-separated `item_id, vendor_count, stock per restock (0 = unlimited, -1 = unknown), one vendor and its zone,
one vendor per city reputation as S=..|I=..|D=..|G=..` (those give a reputation discount) for every item that an Alliance-friendly (or neutral) vendor sells. QuestieDB stores each
field as base64 CBOR in its .toc: `## X-Item-<id>-14` is the vendor NPC list, `## X-Npc-<id>-S` the
small fields, where field 13 is the faction ("A", "H" or "AH").
"""
import base64
import re
import struct
import sys
from pathlib import Path

OUT = Path(__file__).parent.parent / "wow_invest" / "vendor_items.txt"
NAMES = OUT.with_name("item_names.txt")
VENDORS, FACTION, NAME, ZONE, TEMPLATE = 14, 13, 1, 9, 12
# NPC faction template -> the city reputation that sets its prices: Stormwind, Ironforge, Darnassus, Gnomeregan
REPS = {11: "S", 12: "S", 123: "S", 55: "I", 57: "I", 79: "D", 80: "D", 23: "G", 64: "G", 875: "G"}
ZONES = "Interface/AddOns/Questie/Localization/lookups/lookupZones.lua"
CAPITALS = ["Stormwind City", "Ironforge", "Darnassus"]


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


def vendor_stock(path: str) -> dict[int, dict[int, int]]:
    """{item: {npc: stock}} from pfQuest's items.lua, where 0 means the vendor never runs out."""
    out, item, in_v = {}, None, False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if m := re.fullmatch(r"  \[(\d+)\] = \{\}?,?", line):
            item, in_v = int(m[1]), False
        elif line.startswith('    ["'):
            in_v = line.startswith('    ["V"]')
        elif in_v and (m := re.fullmatch(r"      \[(\d+)\] = (\d+),", line)):
            out.setdefault(item, {})[int(m[1])] = int(m[2])
    return out


def main(wow_dir: str, pfquest_items: str) -> int:
    toc = (Path(wow_dir) / "Interface/AddOns/QuestieDB/QuestieDB_Forever.toc").read_text(encoding="utf-8")
    zones = dict(re.findall(r'\[(\d+)\]="([^"]+)"', (Path(wow_dir) / ZONES).read_text(encoding="utf-8")))

    def get(small, field):  # a {field: value} map, or a positional list starting at field 1
        if isinstance(small, dict):
            return small.get(field)
        return small[field - 1] if len(small) >= field else None

    npcs = fields(toc, "Npc", "S")
    alliance = {i for i, small in npcs.items() if "A" in (get(small, FACTION) or "")}

    stock = vendor_stock(pfquest_items)
    assert stock[2320][66] == 0 and stock[8846], "Coarse Thread is unlimited; Gromsblood has a stock number"

    def limit(item, sellers):
        """0 when some vendor never runs out, -1 when pfQuest knows none of its vendors (a Forever-only
        item or vendor), else the largest stock on offer."""
        known = [stock[item][n] for n in sellers if n in stock.get(item, {})]
        return -1 if not known else 0 if 0 in known else max(known)

    def where(item, sellers):
        """One vendor to send the player to: one that never runs out, in a capital city if any.
        Then the same choice among the vendors of each city reputation."""
        if limit(item, sellers) == 0:
            sellers = [n for n in sellers if stock.get(item, {}).get(n, 0) == 0]
        zone = lambda n: zones.get(str(get(npcs[n], ZONE)), "")

        def best(ns):
            n = min(ns, key=lambda n: (CAPITALS.index(zone(n)) if zone(n) in CAPITALS else len(CAPITALS), n))
            return f"{get(npcs[n], NAME)}, {zone(n)}".rstrip(", ")
        by_rep = {r: [n for n in sellers if REPS.get(get(npcs[n], TEMPLATE)) == r] for r in "SIDG"}
        return best(sellers) + "\t" + "|".join(f"{r}={best(ns)}" for r, ns in by_rep.items() if ns)

    ids = {i: sorted(alliance & set(sold_by)) for i, sold_by in fields(toc, "Item", str(VENDORS)).items()}
    ids = {i: v for i, v in sorted(ids.items()) if v}
    assert 2320 in ids and 2589 not in ids, "Coarse Thread is vendor-sold, Linen Cloth is not"
    assert "S=Edna Mullby, Stormwind City" in where(3466, ids[3466]), "Strong Flux is sold in Stormwind"
    OUT.write_text("".join(f"{i}\t{len(v)}\t{limit(i, v)}\t{where(i, v)}\n" for i, v in ids.items()), encoding="utf-8")
    # names, classes and levels for the items the price site has no data for
    classes = {0: "Consumable", 1: "Container", 2: "Weapon", 4: "Armor", 5: "Reagent", 6: "Projectile",
               7: "Trade Goods", 9: "Recipe", 11: "Quiver", 12: "Quest", 13: "Key", 15: "Miscellaneous"}
    info = {i: (get(v, NAME), classes.get(get(v, 12), ""), get(v, 10) or "")
            for i, v in sorted(fields(toc, "Item", "S").items()) if get(v, NAME)}
    assert info[13046][:2] == ("Blanchard's Stout", "Weapon")
    NAMES.write_text("".join(f"{i}\t{n}\t{c}\t{lvl}\n" for i, (n, c, lvl) in info.items()), encoding="utf-8")
    print(f"{len(info)} item names -> {NAMES}")
    print(f"{len(ids)} vendor items ({sum(1 for i, v in ids.items() if limit(i, v) > 0)} limited, {sum(1 for i, v in ids.items() if limit(i, v) < 0)} unknown) -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
