"""Hand-made tables, ids verified against the API search/items endpoints.

GATHER_BANDS: level range of the zones where players gather or loot the item. Covers herbs and
ores; cloth and leather are also included (controller ruling), as they are farmed the same way.
VENDOR_ITEMS: item id -> limited stock per restock, None = unlimited. Built from vendor_items.txt
(every item an Alliance-friendly vendor sells, from the Questie addon database; rebuild it with
scripts/vendor_ids.py), with the hand-checked unlimited staples on top.
"""
from pathlib import Path

from .config import VENDOR_COMMON

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


def _vendor_items() -> dict[int, int | None]:
    # shortcut: Questie has no stock numbers, so an item only a few vendors sell counts as one per
    # restock (herbs, recipes); replace with real stock data if a source turns up
    lines = (Path(__file__).parent / "vendor_items.txt").read_text().splitlines()
    return {int(i): None if int(n) >= VENDOR_COMMON else 1 for i, n in map(str.split, lines)}


VENDOR_ITEMS = {**_vendor_items(), **_UNLIMITED}
