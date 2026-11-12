"""Rebuild wow_invest/recipes.txt from the AtlasLoot Continued addon in a WoW Forever folder.

    python scripts/recipes.py "C:/Program Files (x86)/World of Warcraft/_forever_"

One line per recipe: `item_id profession skill made reagent:count,...`. AtlasLoot's Profession.lua
holds `[spell] = { item, profession, skill, .., .., { reagents }, { counts }, made }` rows; the Classic
rows and the Forever blocks are kept (a Forever row replaces the Classic one for the same spell), the
Burning Crusade and Wrath blocks are skipped.
"""
import re
import sys
from pathlib import Path

OUT = Path(__file__).parent.parent / "wow_invest" / "recipes.txt"
ROW = re.compile(r"\[(\d+)\] = \{ (\d+), (\d+), (\d+), \d+, \d+, \{([\d, ]*)\}, \{([\d, ]*)\}(?:, (\d+))? \}")
PROFESSIONS = {1: "First Aid", 2: "Blacksmithing", 3: "Leatherworking", 4: "Alchemy", 6: "Cooking", 7: "Mining",
               8: "Tailoring", 9: "Engineering", 10: "Enchanting", 13: "Poisons"}


def main(wow_dir: str) -> int:
    lua = (Path(wow_dir) / "Interface/AddOns/AtlasLootContinued/Data/Profession.lua").read_text(encoding="utf-8")
    recipes, skip = {}, False
    for line in lua.splitlines():
        if line.startswith("if AtlasLoot:GameVersion_"):
            skip = "FOREVER" not in line
        elif line == "end":
            skip = False
        elif not skip and (m := ROW.search(line)):
            spell, item, prof, skill, ids, counts, made = m.groups()
            ids, counts = ids.replace(" ", "").split(","), counts.replace(" ", "").split(",")
            if int(prof) in PROFESSIONS and ids != [""] and len(ids) == len(counts):
                mats = ",".join(f"{i}:{n}" for i, n in zip(ids, counts))
                recipes[int(spell)] = f"{item} {PROFESSIONS[int(prof)].replace(' ', '_')} {skill} {made or 1} {mats}"
    lines = sorted(set(recipes.values()), key=lambda s: int(s.split()[0]))
    assert any(s.startswith("4358 Engineering 1 2 4357:2,2589:1") for s in lines), "Rough Dynamite recipe"
    OUT.write_text("\n".join(lines) + "\n")
    print(f"{len(lines)} recipes -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
