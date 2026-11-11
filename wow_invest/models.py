from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Row:
    item_id: int
    name: str | None
    icon: str | None
    quality: str | None
    class_name: str | None
    subclass_name: str | None
    required_level: int | None
    item_level: int | None
    min_ask: int | None
    avg_ask: int | None
    units: int
    listings: int
    sold_24h: int
    vendor_sell: int | None


@dataclass(slots=True)
class Point:
    at: datetime  # timezone-aware UTC
    units: int
    min_ask: int | None
    partial_fills: int
    early: int


@dataclass(slots=True)
class Detail:
    item_id: int
    house: str
    ladder: list[tuple[int, int]]  # (unit_price_copper, units), cheapest first
    series: list[Point]
    cut: float
    purchase_price: int
    purchase_quantity: int
    sell_price: int
    required_level: int | None


@dataclass(slots=True)
class Pick:
    kind: str  # flip | cross | vendor | vendor_check | hold
    item_id: int
    name: str
    icon: str | None
    quality: str | None
    buy_at: str
    sell_at: str
    qty: int
    cost: int
    max_unit_price: int
    sell_price: int
    profit: int
    days: float
    score: float
    sure: float
    reason: str
    spd: float  # effective sales per day
    trend: float | None  # change(min_ask)
    depth: int  # units listed within 5% above sell_price after our buy
    sell_by: datetime | None = None
    funded: bool = False


@dataclass(slots=True)
class Holding:
    id: int
    item_id: int
    kind: str
    qty: int
    price_paid: int  # total for all units
    target: int  # pick's sell_price per unit
    bought_at: datetime
    sell_by: datetime | None
    sold_at: datetime | None
    sell_at: str = "alliance"


@dataclass(slots=True)
class Plan:
    ranked: list[Pick]
    paper: list[Pick]
    checks: list[Pick]
    spend: int
    profit: int
