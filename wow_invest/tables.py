"""Hand-made tables, ids verified against the API search/items endpoints.

GATHER_BANDS: level range of the zones where players gather or loot the item. Covers herbs and
ores; cloth and leather are also included (controller ruling), as they are farmed the same way.
VENDOR_ITEMS: item id -> limited stock per restock, None = unlimited. Built from vendor_items.txt
(every item an Alliance-friendly vendor sells, from the Questie addon database, with vendor stock
from pfQuest's Classic data; rebuild it with scripts/vendor_ids.py), with the hand-checked
unlimited staples on top.
"""
import math
from pathlib import Path

from .config import REP_DISCOUNT


GATHER_BANDS = {
    2447: (1, 15),  # Peacebloom
    765: (1, 15),  # Silverleaf
    2449: (5, 20),  # Earthroot
    785: (10, 25),  # Mageroyal
    2450: (15, 30),  # Briarthorn
    2452: (15, 30),  # Swiftthistle
    3820: (15, 35),  # Stranglekelp
    2453: (20, 35),  # Bruiseweed
    3355: (25, 40),  # Wild Steelbloom
    3369: (25, 40),  # Grave Moss
    3356: (25, 40),  # Kingsblood
    3819: (30, 40),  # Wintersbite
    3357: (30, 45),  # Liferoot
    3818: (30, 45),  # Fadeleaf
    3821: (35, 45),  # Goldthorn
    3358: (35, 50),  # Khadgar's Whisker
    4625: (40, 50),  # Firebloom
    8831: (40, 50),  # Purple Lotus
    8836: (40, 50),  # Arthas' Tears
    8839: (40, 50),  # Blindweed
    8838: (40, 55),  # Sungrass
    8845: (40, 55),  # Ghost Mushroom
    8846: (45, 55),  # Gromsblood
    13464: (45, 60),  # Golden Sansam
    13463: (50, 60),  # Dreamfoil
    13465: (50, 60),  # Mountain Silversage
    13466: (52, 60),  # Plaguebloom
    13467: (53, 60),  # Icecap
    13468: (55, 60),  # Black Lotus
    2770: (1, 20),  # Copper Ore
    2771: (15, 35),  # Tin Ore
    2775: (20, 40),  # Silver Ore
    2772: (30, 45),  # Iron Ore
    2776: (35, 50),  # Gold Ore
    3858: (40, 55),  # Mithril Ore
    7911: (40, 55),  # Truesilver Ore
    10620: (45, 60),  # Thorium Ore
    2589: (5, 20),  # Linen Cloth
    2592: (15, 30),  # Wool Cloth
    4306: (25, 40),  # Silk Cloth
    4338: (35, 50),  # Mageweave Cloth
    14047: (48, 60),  # Runecloth
    2318: (5, 20),  # Light Leather
    2319: (15, 30),  # Medium Leather
    4234: (25, 40),  # Heavy Leather
    4304: (35, 50),  # Thick Leather
    8170: (45, 60),  # Rugged Leather
}

_UNLIMITED = {
    2320: None,  # Coarse Thread, trade supplies
    2321: None,  # Fine Thread, trade supplies
    4291: None,  # Silken Thread, trade supplies
    8343: None,  # Heavy Silken Thread, trade supplies
    14341: None,  # Rune Thread, trade supplies
    3371: None,  # Empty Vial, alchemy supplies (sold in stacks of 5)
    3372: None,  # Leaded Vial, alchemy supplies (sold in stacks of 5)
    8925: None,  # Crystal Vial, alchemy supplies (sold in stacks of 5)
    2880: None,  # Weak Flux, blacksmithing supplies
    3466: None,  # Strong Flux, blacksmithing supplies
    4289: None,  # Salt, cooking/general goods
    2678: None,  # Mild Spices, cooking supplies (sold in stacks of 5)
    2692: None,  # Hot Spices, cooking supplies
    159: None,  # Refreshing Spring Water, food/drink vendor (sold in stacks of 5)
    10284: None,  # Simple Flour, general goods
    2324: None,  # Bleach, trade supplies
    2604: None,  # Red Dye, trade supplies
    6260: None,  # Blue Dye, trade supplies
    4340: None,  # Gray Dye, trade supplies
    17028: None,  # Holy Candle, reagent vendor
    5140: None,  # Flash Powder, poison supplies
}


_VENDORS = {int(i): (int(stock), who, reps) for i, _, stock, who, reps in (
    line.split("\t") for line in (Path(__file__).parent / "vendor_items.txt").read_text(encoding="utf-8").splitlines())}
# unknown stock (-1, a Forever-only item or vendor) is treated as one a restock: never assumed unlimited
VENDOR_ITEMS = {**{i: None if stock == 0 else abs(stock) for i, (stock, _, _) in _VENDORS.items()}, **_UNLIMITED}
VENDOR_WHERE = {i: who for i, (_, who, _) in _VENDORS.items()}  # one vendor to visit, a capital city if possible

# item -> {city reputation: one of its vendors}; only these vendors give a reputation discount
VENDOR_REP = {i: dict(x.split("=", 1) for x in reps.split("|") if x) for i, (_, _, reps) in _VENDORS.items()}


def vendor_deal(item_id: int, rep: dict | None) -> tuple[float, str | None]:
    """(discount, vendor to visit) for the player's city standings `rep`, e.g. {"S": "Revered"}:
    the vendor whose city gives the most off, else the usual one at full price."""
    deals = [(REP_DISCOUNT.get((rep or {}).get(city), 0), who) for city, who in VENDOR_REP.get(item_id, {}).items()]
    off, who = max(deals, key=lambda d: d[0], default=(0, None))
    return (off, who) if off else (0, VENDOR_WHERE.get(item_id))


def vendor_unit(item_id: int, price: int, quantity: int, rep: dict | None = None) -> int:
    """What one unit costs at the vendor, after the player's best reputation discount."""
    return math.ceil(price / (quantity or 1) * (1 - vendor_deal(item_id, rep)[0]))


def _recipes() -> list[tuple[int, str, int, int, tuple[tuple[int, int], ...]]]:
    """(item made, profession, skill needed, units per craft, ((reagent, count), ...)), from the
    AtlasLoot addon data; rebuild recipes.txt with scripts/recipes.py."""
    out = []
    for line in (Path(__file__).parent / "recipes.txt").read_text().splitlines():
        item, prof, skill, made, mats = line.split()
        out.append((int(item), prof.replace("_", " "), int(skill), int(made),
                    tuple(tuple(map(int, m.split(":"))) for m in mats.split(","))))
    return out


RECIPES = _recipes()
PROFESSIONS = sorted({r[1] for r in RECIPES})
# made on a days-long cooldown, so they cannot be crafted in bulk: Mooncloth, Refined Deeprock Salt
CRAFT_SKIP = {14342, 15409}

# item id -> (name, class name, required level or None), from the Questie addon database: the price
# site has no name, icon or class for a few hundred items (rebuilt by scripts/vendor_ids.py)
ITEM_INFO = {int(i): (name, cls or None, int(lvl) if lvl else None) for i, name, cls, lvl in (
    line.split("\t") for line in (Path(__file__).parent / "item_names.txt").read_text(encoding="utf-8").splitlines())}

# item id -> (icon URL, quality), from the game's own item tables, for the same items (rebuilt by
# scripts/item_icons.py)
ITEM_ICON = {int(i): (f"https://render.worldofwarcraft.com/forever-eu/icons/56/{icon}.jpg", q) for i, icon, q in (
    line.split("\t") for line in (Path(__file__).parent / "item_icons.txt").read_text(encoding="utf-8").splitlines())}
