# wow_invest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local Python program that reads blackwater.exchange every hour and serves a bronze-themed page with a ranked, gold-fitted list of AH flips, cross-AH flips, vendor flips and paper-tracked short holds for an Alliance player on WoW Forever EU Normal (PvE).

**Architecture:** One process runs two things:
- **A scanner thread** that, when the site has a new scan, pulls item lists and a capped number of item details into SQLite, builds candidate picks with pure pricing functions, saves them, and judges older picks.
- **A stdlib HTTP server** on 127.0.0.1 that renders the page from SQLite. The page calls the planner on every render with the user's current gold and holdings.

**Tech Stack:** Python ≥ 3.12, standard library only at runtime (`urllib`, `sqlite3`, `http.server`, `json`, `string.Template`), pytest as the only dev dependency, `uv` to run.

**Spec:** `docs/superpowers/specs/2026-11-11-wow-invest-design.md`

## Global Constraints

- Markets: Alliance AH `eu/2/111` and Blackwater AH `eu/2/117` only. In code they are named `"alliance"` and `"blackwater"`; a vendor is `"vendor"`.
- The program never touches the game. It only recommends.
- Be polite to the site:
  - Fetch lists and details only when `/v1/markets` shows a new `captured_at`.
  - Keep at least 1.0 s between requests. That works out at about 200 requests per new scan.
  - User-Agent is the neutral `ah-helper/0.1`. Don't name it after the public repo, because that would link the requests to the GitHub account.
  - Back off on errors: 3 tries at 2, 4 and 8 s, or the `Retry-After` value on a 429. Then skip that request.
- **One pick per item.** The quantity cap counts every open holding of the item, whatever its kind. The planner keeps only the best-scoring buy for each item. A paper hold or a vendor check never hides a buy.
- **Flips list under the real competition:** `sell_price = undercut(min(going, cheapest ladder price left after our buy))`.
- **Holds don't sell instantly:** `days = min(HOLD_DAYS, 1 + days_to_sell)`.
- **Missing history means no pick.** If `change()` returns `None`, the builder returns no pick.
- **The track record is judged only at sell-by.**
  - A pick is a hit if `min_ask ≥ target` held for at least **3 consecutive** snapshots of the sell house between creation and sell-by.
  - A new pick for the same item and kind isn't recorded while an earlier pick's sell-by is still ahead.
  - Holds are proven only when the **last 20** judged holds have:
    - at least 10 judged, and at least 70% hits
    - at least **5 different items**
    - at least **3 different pick days**
  - Only picks the page would show are recorded: profit ≥ 3g and in the top 15.
- **Data age comes from the Blizzard snapshot, `auctions_at`**, not from `captured_at`. Use it for the stale check and show it on the page.
- **Commits list paths explicitly:** `git add <the task's Files>`, never `git add -A`.
- All money is integer copper. 1g = 10000.
- Minimum net profit per pick: **30000 copper (3g)**. Show the **top 15**.
- Capture share: **0.5**. Quantity cap: **2.5 days** of capturable sales, minus units already held.
- AH cut comes from the API's `auction_cut` (Alliance 0.05, Blackwater 0.15). Store it per scan; never hard-code it in logic.
- Effective sales per day = `partial_fills + 0.5 × early_disappearances` over the last 24h of `series`. For screening from list rows: `0.5 × sold_24h`.
- Holds:
  - Sell-by 3 days after the pick. Target = current + **0.5** × (pre-flood − current).
  - Total open plus new hold cost ≤ **25%** of (gold + open hold cost).
  - Holds stay on paper until **≥ 10 judged and ≥ 70% hit**.
  - No holds on items whose level band reaches **50 or higher**. This is the plan's reading of "extra suspicion"; it covers Devilsaur Leather and Thorium.
- Time windows are half-open: "the last 24h" means points aged in `[0, 24)` hours, and "24–48h ago" means `[24, 48)`.
- Vendor flips are ranked only for items in `VENDOR_ITEMS` or user-confirmed items. Other items with AH going price ≥ 2× `purchase_price` show only as "check whether a vendor sells this".
- Icons are displayed only if the URL starts with `https://render.worldofwarcraft.com/`.
- The server binds to `127.0.0.1` only.
- The look copies `.superpowers/brainstorm/7150-1794385546/content/top-flips-priority.html`, the approved mockup. That folder is git-ignored, so copy its CSS into `wow_invest/static/style.css` in Task 9.
- Commits use the repo's configured noreply identity. Don't change git config.

## Review Focus

1. **Site down, slow, 5xx or bad JSON in the middle of a cycle.** The cycle is abandoned, no half scan is saved, and the page keeps showing the last good data with a "last scan N minutes ago" note. Tested in Task 2 and Task 8.
2. **Null fields from the API.** These were seen live: `name`, `icon`, `quality` and `required_level` null on real items, and `minimum_ask_copper` null in `series`. No crash; unpriced items are skipped; the name falls back to `Item {id}`. Tested in Task 2 and Task 9.
3. **Bad gold input** (empty, negative, `abc`, `1e99`). The input is rejected with a message and the previous gold kept. Gold 0 means every pick is listed but unfunded. Tested in Task 6 and Task 9.
4. **Stale or empty data.**
   - First run with no history: the page says "Gathering price history".
   - Site scan older than 2h: a warning banner, and no new picks saved.

   Tested in Task 8 and Task 9.
5. **Re-recommending what the user already holds.**
   - Held units shrink the quantity cap. An item at its cap is not recommended.
   - Marking an item sold frees the cap.
   - The same item and kind is not added to the track record again while an earlier pick for it is still unjudged.

   Tested in Task 5, Task 6 and Task 7.

---

## File structure

```
pyproject.toml                 project + pytest dev group
wow_invest/__init__.py
wow_invest/__main__.py         entry: python -m wow_invest [--port 8765] [--db data/wow_invest.db]
wow_invest/config.py           every constant and threshold, nothing else
wow_invest/models.py           dataclasses shared by all modules
wow_invest/pricing.py          pure money/sales/trend math
wow_invest/api.py              HTTP client + JSON → models
wow_invest/db.py               SQLite schema and queries
wow_invest/tables.py           GATHER_BANDS, VENDOR_ITEMS (hand-made data)
wow_invest/crowd.py            crowd level estimate + item level band
wow_invest/picks.py            candidate builders for the four kinds
wow_invest/planner.py          ranking, funding, caps, paper split
wow_invest/tracker.py          judging saved picks, hit rates
wow_invest/scanner.py          one scan cycle + loop
wow_invest/web.py              page rendering + HTTP routes
wow_invest/page.html           string.Template page
wow_invest/static/style.css    bronze theme from the mockup
tests/test_*.py                one test file per module
```

---

### Task 1: Project scaffold, config, models, pricing math

**Files:**
- Create: `pyproject.toml`, `wow_invest/__init__.py`, `wow_invest/config.py`, `wow_invest/models.py`, `wow_invest/pricing.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Produces, in `config.py`, these exact names:
  - `BASE_URL = "https://blackwater.exchange"`
  - `HOUSES = {"alliance": "eu/2/111", "blackwater": "eu/2/117"}`
  - `USER_AGENT`, `REQUEST_GAP_S = 1.0`, `RETRY_DELAYS_S = (2, 4, 8)`
  - `POLL_MINUTES = 5`, `STALE_HOURS = 2`
  - `MIN_PROFIT = 30000`, `TOP_N = 15`
  - `CAPTURE_SHARE = 0.5`, `MAX_SALES_DAYS = 2.5`, `SOLD_OR_CANCELLED_WEIGHT = 0.5`
  - `BUY_BELOW = 0.80`, `FALLING = -0.10`, `FLOOD_UNITS_RISE = 0.25`
  - `MIN_HISTORY_HOURS = 12`, `MIN_SALES_PER_DAY = 5.0`, `SURE_MIN = 0.4`
  - `HOLD_RECOVERY = 0.5`, `HOLD_DAYS = 3`, `HOLD_MAX_SHARE = 0.25`, `HOLD_ENDGAME_LEVEL = 50`
  - `PAPER_MIN_JUDGED = 10`, `PAPER_MIN_HIT = 0.70`
  - `VENDOR_CHECK_RATIO = 2.0`, `CROWD_PRIOR = (20, 30)`, `CROWD_MIN_SALES = 200`
  - `STATIC_PER_CYCLE = 60`, `DETAIL_QUOTAS = {"flip": 25, "cross": 20, "vendor": 10, "hold": 25}`, `MAX_ERRORS_IN_ROW = 3`, `CHECKS_N = 5`
  - `JUDGE_RUN = 3`, `PROVEN_RECENT = 20`, `PROVEN_MIN_ITEMS = 5`, `PROVEN_MIN_DAYS = 3`
  - `DEPOSIT_RATE = {"alliance": 0.15, "blackwater": 0.75}`, as a fraction of vendor sell price per unit for a 24h listing. Add the comment `# shortcut: classic-style guess, verify by listing one item in game`.
- Produces, in `models.py`, these dataclasses (all `frozen=False`, `slots=True`):
  - `Row(item_id:int, name:str|None, icon:str|None, quality:str|None, class_name:str|None, subclass_name:str|None, required_level:int|None, item_level:int|None, min_ask:int|None, avg_ask:int|None, units:int, listings:int, sold_24h:int, vendor_sell:int|None)`
  - `Point(at:datetime, units:int, min_ask:int|None, partial_fills:int, early:int)`. `at` is timezone-aware UTC.
  - `Detail(item_id:int, house:str, ladder:list[tuple[int,int]], series:list[Point], cut:float, purchase_price:int, purchase_quantity:int, sell_price:int, required_level:int|None)`. `ladder` is `(unit_price_copper, units)`, cheapest first.
  - `Pick(kind:str, item_id:int, name:str, icon:str|None, quality:str|None, buy_at:str, sell_at:str, qty:int, cost:int, max_unit_price:int, sell_price:int, profit:int, days:float, score:float, sure:float, reason:str, spd:float, trend:float|None, depth:int, sell_by:datetime|None=None, funded:bool=False)`.
    - `kind` is one of `"flip"`, `"cross"`, `"vendor"`, `"vendor_check"`, `"hold"`.
    - `spd` is effective sales per day.
    - `trend` is `change(min_ask)`.
    - `depth` is the units listed within 5% above `sell_price` after our buy.
    - These last three feed the row's "why" details.
  - `Holding(id:int, item_id:int, kind:str, qty:int, price_paid:int, target:int, bought_at:datetime, sell_by:datetime|None, sold_at:datetime|None)`.
    - `price_paid` is the total for all units.
    - `target` is the pick's `sell_price` per unit.
  - `Plan(ranked:list[Pick], paper:list[Pick], checks:list[Pick], spend:int, profit:int)`
- Produces, in `pricing.py`:
  - `ladder_buy(ladder, max_unit_price:int, max_qty:int) -> tuple[int,int,int]`. Returns `(qty, cost, top_unit_price)`, buying the cheapest units with price ≤ `max_unit_price`, up to `max_qty`. Returns `(0, 0, 0)` if none.
  - `going_price(series, now, hours=24) -> int|None`. The median `min_ask` of points within the last `hours`, nulls skipped.
  - `window_median(series, now, start_h:float, end_h:float, field:str) -> float|None`. Median of `field` (`"min_ask"` or `"units"`) over points aged between `start_h` and `end_h` hours.
  - `change(series, now, field) -> float|None`. Equals `window_median(0,6)/window_median(24,48) − 1`. `None` if either side is missing.
  - `sales_per_day(series, now) -> float`. Equals `Σ partial_fills + SOLD_OR_CANCELLED_WEIGHT × Σ early` over the last 24h.
    - Don't rescale for gaps in the series: each point already counts everything that left since the previous scan.
    - The reviewer confirmed that partial fills plus early disappearances over 24h equal `sold_24h`.
  - `sales_per_day_window(series, now, start_h, end_h) -> float`. The same sum over hours `start_h`–`end_h` ago, scaled to 24h.
  - `history_hours(series, now) -> float`. Hours from the oldest priced point to `now`.
  - `undercut(price:int) -> int`. Returns `max(1, price − 1)`.
  - `deposit(vendor_sell:int|None, house:str) -> int`. Returns `int(vendor_sell × DEPOSIT_RATE[house])`, or 0 when `vendor_sell` is None.
  - `net_proceeds(unit_price:int, qty:int, cut:float, deposit_per_unit:int) -> int`. Equals `int(unit_price × qty × (1 − cut)) − deposit_per_unit × qty`.
  - `days_to_sell(qty:int, sold_per_day:float) -> float`. Equals `qty / (sold_per_day × CAPTURE_SHARE)`, or `math.inf` when sales are 0.
  - `max_units(sold_per_day:float, held:int) -> int`. Equals `max(0, floor(sold_per_day × CAPTURE_SHARE × MAX_SALES_DAYS) − held)`.
  - `score(profit:int, cost:int, days:float) -> float`. Equals `(profit / cost) / max(days, 1/24)`, or 0.0 if cost ≤ 0.
  - `sureness(hours:float, sold_per_day:float) -> float`. Equals `min(1, hours/48) × min(1, sold_per_day / (4 × MIN_SALES_PER_DAY))`.

- [ ] **Step 1: Write `pyproject.toml` and the package files.** `pyproject.toml` declares `name = "wow-invest"`, `requires-python = ">=3.12"`, `dependencies = []`, `[dependency-groups] dev = ["pytest>=8"]`, `[tool.pytest.ini_options] testpaths = ["tests"]`. Add a `tests/conftest.py` that provides:
  - `NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)`
  - a helper `pts(spec)` that builds `Point`s from `(hours_ago, units, min_ask, partial, early)` tuples
  - `steady`: points at ages **0 to 47 inclusive**, each with units 300, min_ask 20000, partial 0 and early 4. That's 24 points in `[0, 24)`, so 48 effective sales a day.

- [ ] **Step 2: Write the failing tests in `tests/test_pricing.py`**

```python
def test_ladder_buy_takes_cheapest_up_to_price_and_qty():
    assert ladder_buy([(15000, 38), (19700, 20), (19800, 155)], 19700, 50) == (50, 806400, 19700)
def test_ladder_buy_nothing_cheap_enough():
    assert ladder_buy([(20000, 5)], 19999, 10) == (0, 0, 0)
def test_going_price_median_of_last_24h_skips_nulls_and_old():
    s = pts([(1,0,100,0,0),(2,0,None,0,0),(3,0,300,0,0),(5,0,200,0,0),(30,0,9999,0,0)])
    assert going_price(s, NOW) == 200
def test_change_compares_last_6h_to_24_48h():
    s = pts([(1,0,80,0,0),(3,0,80,0,0),(30,0,100,0,0),(40,0,100,0,0)])
    assert change(s, NOW, "min_ask") == pytest.approx(-0.2)
def test_change_none_without_old_window():
    assert change(pts([(1,0,80,0,0)]), NOW, "min_ask") is None
def test_sales_per_day_weights_sold_or_cancelled_half():
    s = pts([(1,0,1,4,10),(5,0,1,6,10),(30,0,1,100,100)])
    assert sales_per_day(s, NOW) == 20.0
def test_net_proceeds_after_cut_and_deposit():
    assert net_proceeds(10000, 10, 0.05, 50) == 94500
def test_days_to_sell_and_zero_sales():
    assert days_to_sell(10, 10.0) == 2.0
    assert days_to_sell(10, 0.0) == math.inf
def test_max_units_minus_held_never_negative():
    assert max_units(20.0, 5) == 20
    assert max_units(2.0, 50) == 0
def test_score_is_return_per_day():
    assert score(50, 100, 2.0) == 0.25
    assert score(50, 0, 2.0) == 0.0
def test_deposit_uses_house_rate_and_handles_none():
    assert deposit(1000, "alliance") == 150
    assert deposit(None, "blackwater") == 0
```

- [ ] **Step 3: Run the tests to make sure they fail.** Run `uv run pytest tests/test_pricing.py -q`. Expected: a collection error / ImportError for `wow_invest.pricing`.

- [ ] **Step 4: Implement `config.py`, `models.py` and `pricing.py`** with the names and formulas above. Use `statistics.median`.

- [ ] **Step 5: Run the tests to make sure they pass.** Run `uv run pytest tests/test_pricing.py -q`. Expected: all pass.

- [ ] **Step 6: Commit.** Run `git add <this task's Files> && git commit -m "feat: scaffold, config, models and pricing math"`.

---

### Task 2: API client

**Files:**
- Create: `wow_invest/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `config`, `models.Row`, `models.Point`, `models.Detail`.
- Produces:
  - `class ApiError(Exception)`
  - `class Client`, with `__init__(self, fetch=None, sleep=time.sleep, clock=time.monotonic)`. `fetch(url, headers) -> bytes` defaults to a `urllib.request` call with a 20 s timeout. It is injectable for tests.
  - `Client.get_json(path:str) -> dict|list`. It waits until `REQUEST_GAP_S` has passed since the previous request, and retries on `URLError`, HTTP 5xx, 429 and bad JSON, sleeping `RETRY_DELAYS_S`. On a 429 it sleeps for `Retry-After` when that header is present. After the last failure it raises `ApiError`. A 404 raises `ApiError` immediately.
  - `Client.scan_times() -> dict[str, tuple[str, str]]`. Maps `"alliance"`/`"blackwater"` to `(captured_at, auctions_at)` from `/v1/markets`, matching on `region`, `ruleset_id` and `house_id`.
  - `Client.all_items(house) -> list[Row]`. Pages `/v1/markets/{key}/items?limit=200&offset=N` until it has `total`.
  - `Client.deals(house, limit=100) -> tuple[list[int], float]`. Returns the item ids from `/deals?limit=…` and `auction_cut`.
  - `Client.item_detail(house, item_id, cut:float) -> Detail`. One request; the caller passes the cut it got from `deals`.
  - Module-level parsers `parse_row(d:dict) -> Row` and `parse_detail(d:dict, house:str, cut:float) -> Detail`.
    - Prices are rounded to int copper. `None` stays `None`. Missing counts become 0.
    - `series` nulls in `partial_fills` and `early_disappearances` become 0.
    - `ladder` is sorted by price.
    - An unknown id comes back as HTTP 200 with `item: {id, name: null}` and no `purchase_price`. That parses to a `Detail` with `purchase_price=0`, an empty ladder and an empty series. It must not raise.

- [ ] **Step 1: Write the failing tests in `tests/test_api.py`.** Use a `FakeFetch` that returns queued bytes or raises, and a fake `sleep` that records delays.

```python
def test_parse_row_keeps_nulls_and_rounds_prices():
    r = parse_row({"id": 14551, "name": None, "icon": None, "quality": None, "class_name": None, "subclass_name": None,
                   "required_level": None, "item_level": None, "minimum_ask_copper": 1800000.0, "average_ask_copper": 1800000.4,
                   "units": 2, "listings": 2, "sold_24h": 6, "vendor_sell_copper": None})
    assert (r.name, r.min_ask, r.avg_ask, r.required_level) == (None, 1800000, 1800000, None)
def test_parse_detail_sorts_ladder_and_zeroes_null_counts(): ...   # ladder given out of order → ascending; partial_fills None → 0
def test_get_json_waits_between_requests(): ...                    # two calls at clock 0 and 0.2 → sleep(0.8) recorded
def test_get_json_retries_5xx_then_succeeds(): ...                 # 503, 503, ok → returns data, sleeps include 2 and 4
def test_get_json_raises_after_three_failures(): ...               # bad JSON ×4 → ApiError
def test_get_json_404_raises_without_retry(): ...                  # one fetch call only
def test_all_items_pages_until_total(): ...                        # total 450 → offsets 0, 200, 400 requested; 450 rows
def test_scan_times_maps_both_houses(): ...                        # from a /v1/markets sample with 111 and 117 entries → (captured_at, auctions_at)
def test_parse_detail_unknown_item_does_not_raise(): ...           # {"item": {"id": 1, "name": None}} → purchase_price 0, empty ladder/series
def test_get_json_honours_retry_after_on_429(): ...                # 429 with Retry-After: 30 → sleep(30) recorded
def test_requests_send_generic_user_agent(): ...                   # headers["User-Agent"] == config.USER_AGENT
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_api.py -q`. Expected: an ImportError.

- [ ] **Step 3: Implement `api.py`.** Map an HTTP status to retry or fail by catching `urllib.error.HTTPError` and reading `.code`.

- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_api.py -q`. Expected: all pass.

- [ ] **Step 5: Do a live check once, by hand.** Run:

```
uv run python -c "from wow_invest.api import Client; c=Client(); print(c.scan_times()); ids,cut=c.deals('alliance',5); d=c.item_detail('alliance', 15417, cut); print(d.cut, d.ladder[:2], len(d.series))"
```

Expected: two timestamps, `0.05`, two ladder tuples, and a series length > 0.

- [ ] **Step 6: Commit.** Run `git add <this task's Files> && git commit -m "feat: polite API client and parsers"`.

---

### Task 3: Database

**Files:**
- Create: `wow_invest/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: the models.
- Produces:
  - `connect(path:str) -> sqlite3.Connection`. Creates the parent directory, turns on WAL, sets `row_factory = sqlite3.Row`, and creates the schema below if it's missing. `":memory:"` is allowed.
  - Schema:
    - `scans(id INTEGER PRIMARY KEY, house TEXT, captured_at TEXT, auctions_at TEXT, cut REAL, crowd TEXT, UNIQUE(house, captured_at))`. `crowd` is `"lo-hi"` on Alliance scans, which keeps the crowd's history.
    - `snapshots(scan_id INTEGER, item_id INTEGER, min_ask INTEGER, units INTEGER, sold_24h INTEGER, PRIMARY KEY(scan_id, item_id))`
    - `items(item_id INTEGER PRIMARY KEY, name, icon, quality, class_name, subclass_name, required_level, item_level, vendor_sell, purchase_price, purchase_quantity, static_done INTEGER DEFAULT 0)`
    - `picks(id INTEGER PRIMARY KEY, created_at TEXT, kind TEXT, item_id INTEGER, sell_at TEXT, qty INTEGER, cost INTEGER, sell_price INTEGER, profit INTEGER, sell_by TEXT, judged_at TEXT, hit INTEGER)`
    - `holdings(id INTEGER PRIMARY KEY, item_id INTEGER, kind TEXT, qty INTEGER, price_paid INTEGER, target INTEGER, bought_at TEXT, sell_by TEXT, sold_at TEXT)`
    - `settings(key TEXT PRIMARY KEY, value TEXT)`
    - `vendor_confirmed(item_id INTEGER PRIMARY KEY)`
  - All timestamps are stored as ISO-8601 UTC strings.
  - `last_captured(conn, house) -> str|None`
  - `save_scan(conn, house, captured_at, auctions_at, cut, rows:list[Row]) -> int`. One transaction that inserts the scan and snapshots and upserts `items` names, icons and levels. It never overwrites a non-null value with null.
  - `set_scan_crowd(conn, scan_id, crowd:tuple[int,int])`
  - `rows_at(conn, house, hours_ago:float, now) -> dict[int, tuple[int|None,int,int]]`. From the newest scan at least `hours_ago` old, maps `item_id` to `(min_ask, units, sold_24h)`.
  - `missing_static(conn, limit:int) -> list[int]`. Items with `static_done = 0`, highest latest `sold_24h` first.
  - `save_static(conn, d:Detail)`. Sets `static_done = 1`.
  - `mark_static_bad(conn, item_id)`. Sets `static_done = -1`, so a failing id isn't retried every hour.
  - `static(conn, item_id) -> sqlite3.Row|None`
  - `set_setting(conn, key, value:str)` and `get_setting(conn, key, default=None) -> str|None`
  - `get_gold(conn) -> int`. Copper, default 0. `set_gold(conn, copper:int)`
  - `add_holding(conn, item_id, kind, qty, price_paid, target, now, sell_by) -> int`, `mark_sold(conn, holding_id, now)`, `open_holdings(conn) -> list[Holding]`
  - `held_units(conn) -> dict[int, int]`. Open holdings summed by `item_id`, across all kinds.
  - `confirm_vendor(conn, item_id)` and `confirmed_vendors(conn) -> set[int]`
  - `save_candidates(conn, picks:list[Pick], captured_at:str)` and `load_candidates(conn) -> tuple[list[Pick], str|None]`. Stored as JSON in `settings["candidates"]`, with datetimes as ISO strings.
  - `record_picks(conn, picks:list[Pick], now) -> int`. Inserts into `picks`, skipping any `(item_id, kind)` that already has an unjudged row. Since rows are judged only after sell-by, that blocks re-recording until the earlier window has closed. Returns how many were inserted.
  - `open_picks(conn) -> list[sqlite3.Row]`, `judge_pick(conn, pick_id, hit:bool, now)`
  - `hit_stats(conn) -> dict`. Keys:
    - `judged` and `hits`, counted over all time
    - `hold_judged`, `hold_hits`, `hold_items` (distinct item ids) and `hold_days` (distinct `created_at` dates), counted over the last `PROVEN_RECENT` judged holds
  - `prune(conn, now, days=30)`. Deletes snapshots and scans older than 30 days. Add the comment `# shortcut: fixed 30-day retention, make configurable if history analysis needs more`.

- [ ] **Step 1: Write the failing tests in `tests/test_db.py`** (all on `:memory:`).

```python
def test_save_scan_is_idempotent_per_captured_at(): ...       # second save with same captured_at → no duplicate scan, returns same id
def test_save_scan_keeps_known_name_when_row_has_null(): ...  # name "Linen Cloth" then None → still "Linen Cloth"
def test_rows_at_picks_newest_scan_old_enough(): ...           # scans at now-30h and now-1h; rows_at(24) → the -30h values
def test_gold_roundtrip_and_default(): assert get_gold(c) == 0; set_gold(c, 8000000); assert get_gold(c) == 8000000
def test_holdings_add_sell_and_held_units(): ...                # a flip and a hold of the same item → held_units[item] sums both; mark_sold one → drops
def test_record_picks_skips_item_kind_with_open_pick(): ...     # record twice → second returns 0; after judge_pick → records again
def test_candidates_roundtrip_preserves_datetimes(): ...
def test_hit_stats_counts_recent_holds_items_and_days(): ...    # 25 judged holds → only the last 20 counted; hold_items/hold_days distinct
def test_mark_static_bad_excludes_from_missing_static(): ...
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_db.py -q`. Expected: an ImportError.

- [ ] **Step 3: Implement `db.py`.** Use `with conn:` for each write transaction.

- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_db.py -q`.

- [ ] **Step 5: Commit.** Run `git add <this task's Files> && git commit -m "feat: sqlite storage"`.

---

### Task 4: Crowd estimate and hand-made tables

**Files:**
- Create: `wow_invest/tables.py`, `wow_invest/crowd.py`
- Test: `tests/test_crowd.py`

**Interfaces:**
- Produces, in `tables.py`:
  - `GATHER_BANDS: dict[int, tuple[int,int]]`. Maps an item id to the zone level band, for every classic herb and ore, e.g. Mageroyal `785: (10, 25)`. Check every id with `/v1/markets/eu/2/111/search?q=<name>` before adding it, and comment each line with the item name.
  - `VENDOR_ITEMS: dict[int, int|None]`. Maps an item id to the vendor's limited stock per restock, or `None` if the stock is unlimited. It's a starter list of items sold by NPC vendors the Alliance can reach, e.g. trade-supply goods and limited recipes. Confirm each one's `purchase_price` with `/items/{id}`, and comment each id with its name and vendor.
- Produces, in `crowd.py`:
  - `estimate_crowd(rows:list[Row]) -> tuple[int,int]`. Takes the weighted 25th and 75th percentiles of `required_level`, weighted by `sold_24h`. A weighted percentile q is the lowest level whose running weight is ≥ q × total weight. It only counts rows with `class_name` in `{"Armor", "Weapon"}`, `required_level > 0` and `sold_24h > 0`. It returns `CROWD_PRIOR` when the total weight is below `CROWD_MIN_SALES`.
  - `item_band(item_id:int, required_level:int|None) -> tuple[int,int]|None`. Returns `GATHER_BANDS[item_id]` if the item is listed. Otherwise, for `required_level > 0`, returns `(max(1, r − 2), r + 3)`. Otherwise `None`.
  - `overlaps(band, crowd) -> bool`

- [ ] **Step 1: Write the failing tests.**

```python
def test_crowd_weighted_quartiles():
    rows = [row(cls="Armor", lvl=22, sold=100), row(cls="Weapon", lvl=25, sold=100), row(cls="Armor", lvl=28, sold=100), row(cls="Armor", lvl=40, sold=10)]
    assert estimate_crowd(rows) == (22, 28)
def test_crowd_falls_back_to_prior_when_few_sales(): assert estimate_crowd([row(cls="Armor", lvl=50, sold=5)]) == (20, 30)
def test_crowd_ignores_non_equipment(): ...      # 1000 sold "Trade Goods" lvl 55 + qualifying armor → unaffected
def test_item_band_table_then_required_level_then_none():
    assert item_band(785, None) == (10, 25)
    assert item_band(1, 30) == (28, 33)
    assert item_band(1, 0) is None
def test_gather_bands_are_sane(): assert all(1 <= lo < hi <= 60 for lo, hi in GATHER_BANDS.values())
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_crowd.py -q`.
- [ ] **Step 3: Implement `crowd.py` and fill in `tables.py`.** Look up and verify the ids as described above.
- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_crowd.py -q`.
- [ ] **Step 5: Commit.** Run `git add <this task's Files> && git commit -m "feat: crowd estimate and gather/vendor tables"`.

---

### Task 5: Pick builders

**Files:**
- Create: `wow_invest/picks.py`
- Test: `tests/test_picks.py`

**Interfaces:**
- Consumes: `pricing.*`, `crowd.item_band`, `crowd.overlaps`, `config`, and the models.
- Every builder below returns `None` when:
  - the item's history is shorter than `MIN_HISTORY_HOURS`, or
  - any `change()` it needs returns `None`, or
  - effective sales per day are below `MIN_SALES_PER_DAY`, or
  - its `qty` comes out as 0, or
  - its `sure` is below `SURE_MIN`.

  A builder never filters on minimum profit; the planner does that. Every builder fills in `reason` with one plain sentence that names the numbers, and sets `spd`, `trend` and `depth`.
- Produces:
  - `quick_flip(row:Row, d:Detail, held:int, now) -> Pick|None`. Here `d` is the Alliance detail and `row.vendor_sell` feeds `deposit`.
    - `going = going_price(d.series, now)`.
    - Reject if `change(min_ask) < FALLING`, unless `change(units) < 0` and the sales in the last 24h are at least 0.9 × the sales 24–48h ago.
    - Buy from `d.ladder` up to `BUY_BELOW × going`, with `max_qty = max_units(spd, held)`.
    - `sell_price = undercut(min(going, cheapest ladder price left after the buy))`, so the listing sits under the existing wall. Profit uses `net_proceeds` with `d.cut` and the Alliance deposit.
    - `kind = "flip"`, `buy_at = sell_at = "alliance"`.
    - `sell_by = now + max(1 day, ceil(days) days)`.
  - `cross_flip(row:Row, buy:Detail, sell:Detail, held:int, now) -> Pick|None`. Same as above, except:
    - the going price, trend and sales come from `sell`
    - the ladder comes from `buy`
    - the cut and deposit belong to `sell.house`
    - `kind = "cross"`, `buy_at = buy.house`, `sell_at = sell.house`
  - `vendor_flip(row:Row, d:Detail, held:int, confirmed:set[int], now) -> Pick|None`.
    - Unit cost is `ceil(purchase_price / purchase_quantity)`.
    - If the item is in `VENDOR_ITEMS` or in `confirmed`, return `kind="vendor"` with:
      - `buy_at="vendor"`, `sell_at="alliance"`
      - `qty = max_units(spd, held)`, further capped by the `VENDOR_ITEMS` stock limit when there is one
      - `sell_by` as for `quick_flip`
      - `sell_price` as for `quick_flip`, against the full Alliance ladder
    - Else, if `going ≥ VENDOR_CHECK_RATIO × unit cost`, return `kind="vendor_check"` with the same numbers.
    - Else return `None`.
  - `hold(row:Row, d:Detail, crowd:tuple[int,int], held:int, now) -> Pick|None`.
    - The item needs a band from `item_band`; the band must overlap `crowd`, and the band's high end must be below `HOLD_ENDGAME_LEVEL`.
    - Flood: `change(units) ≥ FLOOD_UNITS_RISE` and `change(min_ask) ≤ FALLING`.
    - Sales holding: sales in the last 24h ≥ 0.9 × sales 24–48h ago.
    - `current = going_price(d.series, now, hours=6)`, `pre = window_median(24, 48, "min_ask")`, `target = round(current + HOLD_RECOVERY × (pre − current))`.
    - Buy from the ladder at ≤ `current`, `max_qty = max_units(spd, held)`. `sell_price = target`.
    - `kind = "hold"`, `sell_by = now + HOLD_DAYS days`, `days = min(HOLD_DAYS, 1 + days_to_sell)`. The extra day accounts for the recovery, so a hold is never scored as selling instantly.
- Use one test helper `detail(series=..., ladder=..., cut=0.05, house="alliance", purchase_price=0, purchase_quantity=1)`. Use the steady series `steady = 48 hourly points, units 300, min_ask 20000, partial 0, early 4`; its effective sales are 48/day.

- [ ] **Step 1: Write the failing tests**

```python
def test_quick_flip_buys_below_80pct_and_lists_under_remaining_wall():
    p = quick_flip(row(vendor_sell=500), detail(series=steady, ladder=[(12000,10),(15000,5),(19900,100)]), held=0, now=NOW)
    # going 20000, cheapest left 19900 → sell 19899; int(19899*15*0.95)=283560 − 75*15 = 282435 − 195000
    assert (p.kind, p.qty, p.cost, p.sell_price, p.profit) == ("flip", 15, 195000, 19899, 87435)
    assert p.days == pytest.approx(0.625)
def test_quick_flip_lists_under_going_when_wall_is_higher(): ...    # ladder [(12000,10),(25000,50)] → sell_price 19999
def test_builders_return_none_without_24_48h_window(): ...         # 20 hourly points (ages 0–19): history ≥ 12h but change() is None → None
def test_quick_flip_rejects_falling_price_with_rising_units(): ...   # 24-48h at 30000/200 units, last 6h 20000/400 → None
def test_quick_flip_allows_falling_price_when_units_shrink_and_sales_hold(): ...
def test_quick_flip_none_with_thin_history(): ...                   # 6 hourly points → None
def test_quick_flip_respects_held_units(): ...                      # held = 60 (cap is 60) → None
def test_cross_flip_buys_on_blackwater_sells_on_alliance_with_alliance_cut(): ...
def test_vendor_flip_only_for_known_or_confirmed_items(): ...       # unknown + going ≥ 2× cost → kind "vendor_check"; confirmed → "vendor"
def test_hold_targets_half_recovery_and_three_day_sell_by():
    # Mageroyal 785, crowd (20, 30); 24-48h: min 100, units 1000; last 6h: min 60, units 1500; sales steady
    p = hold(row(item_id=785), detail(series=flood, ladder=[(60, 500)]), crowd=(20, 30), held=0, now=NOW)
    assert (p.kind, p.sell_price, p.sell_by) == ("hold", 80, NOW + timedelta(days=3))
    assert p.days >= 1.0
def test_hold_none_for_endgame_band(): ...                          # row required_level 50 → band (48, 53), crowd (45, 52) → None
def test_hold_none_when_sales_fall_with_price(): ...
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_picks.py -q`.
- [ ] **Step 3: Implement `picks.py`.** Put the shared middle steps in one private helper `_build(...)`: sales, cap, ladder buy, proceeds, days, score and sure.
- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_picks.py -q`.
- [ ] **Step 5: Commit.** Run `git add <this task's Files> && git commit -m "feat: flip, cross, vendor and hold pick builders"`.

---

### Task 6: Planner

**Files:**
- Create: `wow_invest/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: `Pick`, `Plan`, `Holding`, `config`.
- Produces:
  - `holds_proven(stats:dict) -> bool`. Takes `db.hit_stats` output. True when:
    - `hold_judged ≥ PAPER_MIN_JUDGED`
    - `hold_hits / hold_judged ≥ PAPER_MIN_HIT`
    - `hold_items ≥ PROVEN_MIN_ITEMS`
    - `hold_days ≥ PROVEN_MIN_DAYS`
  - `best_per_item(picks:list[Pick]) -> list[Pick]`. Keeps the highest-score pick for each `item_id`. The scanner also uses this before saving.
  - `plan(candidates:list[Pick], gold:int, holdings:list[Holding], proven:bool) -> Plan`. In order:
    1. Drop picks with `profit < MIN_PROFIT`.
    2. Send `vendor_check` to `checks`. Send `hold` to `paper` unless `proven`. The rest is the ranked pool. Apply `best_per_item` to the pool. Then apply it to `paper` and to `checks`, each leaving out items already in the pool. A paper hold or a vendor check never hides a buy.
    3. Sort the pool by `score` descending and keep `TOP_N`. Sort `paper` by score and keep `TOP_N`. Sort `checks` by score and keep `CHECKS_N`.
    4. Fund top-down from `gold`:
       - A pick that fits is `funded=True`.
       - A pick that doesn't fit is scaled to `qty' = floor(qty × remaining / cost)`, with cost and profit scaled by `qty' / qty`. It's kept as funded only if the scaled profit is ≥ `MIN_PROFIT`. Otherwise it's left unfunded at its original size. Add the comment `# shortcut: linear scaling ignores ladder steps, fine for partial fills`.
    5. When holds are proven, a hold is funded only while `open hold cost + funded hold cost ≤ HOLD_MAX_SHARE × (gold + open hold cost)`. Open hold cost is the sum of `price_paid` over open holdings with `kind == "hold"`.
    6. `spend` and `profit` sum the funded picks.
  - Unfunded picks stay in `ranked`, with `funded=False`, so the page still shows them.

- [ ] **Step 1: Write the failing tests**

```python
def test_drops_picks_under_3g(): ...
def test_keeps_top_15_by_score(): ...                                        # 20 in → 15, descending score
def test_funds_top_down_and_partially_fills_next():
    a, b = pk(cost=500_000, profit=200_000, score=2.0), pk(qty=10, cost=1_000_000, profit=400_000, score=1.0)
    out = plan([a, b], gold=1_000_000, holdings=[], proven=False)
    assert [p.funded for p in out.ranked] == [True, True]
    assert (out.ranked[1].qty, out.ranked[1].cost, out.ranked[1].profit) == (5, 500_000, 200_000)
    assert out.spend == 1_000_000
def test_partial_fill_below_min_profit_stays_unfunded(): ...
def test_zero_gold_lists_everything_unfunded(): ...                          # out.ranked non-empty, all funded False, spend 0
def test_holds_go_to_paper_until_proven(): ...
def test_proven_holds_capped_at_quarter_including_open_holds(): ...          # gold 1000g, open hold 200g → cap 0.25 × 1200g = 300g → max new hold cost 100g
def test_vendor_check_never_funded(): ...
def test_holds_proven_thresholds():
    ok = dict(judged=0, hits=0, hold_judged=10, hold_hits=7, hold_items=5, hold_days=3)
    assert holds_proven(ok)
    assert not holds_proven({**ok, "hold_judged": 9, "hold_hits": 9})
    assert not holds_proven({**ok, "hold_hits": 6})
    assert not holds_proven({**ok, "hold_items": 4})    # one lucky item can't prove the idea
    assert not holds_proven({**ok, "hold_days": 2})
def test_best_per_item_keeps_highest_score(): ...    # flip and cross for the same item → only the higher score kept
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_planner.py -q`.
- [ ] **Step 3: Implement `planner.py`.** Copy picks with `dataclasses.replace`; never mutate the input.
- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_planner.py -q`.
- [ ] **Step 5: Commit.** Run `git add <this task's Files> && git commit -m "feat: gold-fitted planner with paper holds"`.

---

### Task 7: Track record

**Files:**
- Create: `wow_invest/tracker.py`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `db.open_picks`, `db.judge_pick`, and the snapshots and scans tables.
- Produces `judge(conn, now) -> int`, which returns how many picks were judged.
  - Only picks with `now > sell_by` are judged; everything else stays open. This keeps hits and misses on the same clock, so the hit rate can't be inflated by quick hits.
  - A pick is a **hit** if the snapshots of `sell_at`'s house between `created_at` and `sell_by` include a run of at least `JUDGE_RUN` consecutive snapshots with `min_ask ≥ sell_price`. That means the price really held at the target, not one hourly spike.
  - Otherwise it's a **miss**.
  - `vendor_check` picks are never recorded, so they're never judged.

- [ ] **Step 1: Write the failing tests**

```python
def test_hit_needs_three_consecutive_snapshots_at_target(): ...  # target 20000; 18000, 20500, 20600, 20100 then sell_by passes → hit
def test_single_spike_is_a_miss(): ...                            # 18000, 20500, 18000, 20500, 18000 → miss
def test_miss_when_sell_by_passes(): ...                          # target 30000 never reached, now > sell_by → hit == 0
def test_not_judged_before_sell_by_even_if_reached(): ...         # three snapshots at target but now < sell_by → stays open
def test_only_snapshots_of_sell_house_count(): ...                # Blackwater reaching target doesn't count for an Alliance sale
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_tracker.py -q`.
- [ ] **Step 3: Implement `tracker.py`.** Use one SQL query per pick, joining `snapshots` to `scans` on house and the time window.
- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_tracker.py -q`.
- [ ] **Step 5: Commit.** Run `git add <this task's Files> && git commit -m "feat: judge past picks for hit rate"`.

---

### Task 8: Scanner cycle

**Files:**
- Create: `wow_invest/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `api.Client`, `api.ApiError`, `db.*`, `picks.*`, `crowd.estimate_crowd`, `crowd.item_band`, `planner.plan`, `tracker.judge`.
- Produces:
  - `run_cycle(client, conn, now) -> str`, which returns `"no-change"`, `"stale"`, `"done"` or `"error"`. In order:
    1. `times = client.scan_times()`. If both houses' `captured_at` equal `db.last_captured`, return `"no-change"`.
    2. Fetch `client.all_items` and `client.deals` for both houses before writing anything. Keep each house's cut from `deals`. On `ApiError`, return `"error"` without saving.
    3. Save both houses with `db.save_scan`, including `auctions_at`.
    4. If either `auctions_at` is older than `STALE_HOURS`, set `settings["stale"] = "1"`, run `tracker.judge`, and return `"stale"` without building picks. Otherwise set `stale` to `"0"`.
    5. Backfill static data: up to `STATIC_PER_CYCLE` ids from `db.missing_static`, via `client.item_detail("alliance", id, cut)` and `db.save_static`. If an id raises `ApiError`, mark it with `db.mark_static_bad` and continue.
    6. `crowd = estimate_crowd(alliance rows)`. Store it with `db.set_scan_crowd` on the Alliance scan and in `settings["crowd"] = "lo-hi"`.
    7. **Pre-screen:** a candidate row must have `0.5 × sold_24h ≥ MIN_SALES_PER_DAY` on the house it would sell on.
    8. **Choose candidates** per category, each up to its own quota in `DETAIL_QUOTAS`, deduplicated across categories in this order: `cross`, `vendor`, `hold`, then `flip`. Each category, highest `sold_24h` first:
       - `cross`: Blackwater rows with `min_ask ≤ BUY_BELOW × Alliance min_ask`, and Alliance rows with `min_ask ≤ BUY_BELOW × Blackwater min_ask`
       - `vendor`: `VENDOR_ITEMS` plus confirmed vendor items, then static items whose `purchase_price` is greater than 0 and ≤ the Alliance `min_ask` ÷ `VENDOR_CHECK_RATIO`
       - `hold`: items whose `item_band` overlaps the crowd, whose band's high end is below `HOLD_ENDGAME_LEVEL`, and whose units rose by `FLOOD_UNITS_RISE` against `db.rows_at(24h)`
       - `flip`: Alliance deal ids
    9. For each candidate, fetch the detail for the house it sells on. Cross candidates also need the house they buy on. Run the builders for that candidate's category; a `flip` candidate also tries `vendor_flip`. Pass `held` from `db.held_units()[item_id]`.
       - An `ApiError` on one candidate skips that candidate.
       - `MAX_ERRORS_IN_ROW` consecutive errors end the cycle with `"error"`. Data already committed stays, and candidates are not replaced.
    10. Call `db.save_candidates(all non-None picks sorted by score, alliance captured_at)`. Don't dedupe across kinds here; the planner does that.
    11. Record only what the page could show: `p = planner.plan(candidates, gold=0, holdings=[], proven=False)`, then `db.record_picks(p.ranked + p.paper, now)`. That's the same filtering, dedupe and top-N the page uses. `vendor_check` is never recorded.
    12. Call `tracker.judge`, then `db.prune`. Return `"done"`.
  - `loop(client_factory, db_path, stop:threading.Event)`. It opens its own connection, calls `run_cycle` every `POLL_MINUTES`, logs the result with `logging`, and keeps running after any exception (logged with traceback).

- [ ] **Step 1: Write the failing tests.** Use a `FakeClient` built from small dict fixtures; it counts calls.

```python
def test_no_change_when_captured_at_unchanged(): ...      # second run → "no-change", no all_items calls
def test_error_before_save_leaves_db_untouched(): ...      # all_items raises ApiError for blackwater → "error", no scans rows
def test_done_saves_candidates_and_records_picks(): ...    # fixture with one clear quick flip → load_candidates has it
def test_detail_quotas_leave_room_for_every_kind(): ...    # 200 deal ids + 30 cross rows → item_detail calls for flips ≤ 25, cross candidates still fetched
def test_stale_site_data_saves_scan_but_no_new_picks(): ...  # auctions_at 3h old → "stale", candidates unchanged
def test_one_bad_item_is_skipped_not_fatal(): ...          # item_detail raises for one static id → marked bad, cycle "done"
def test_records_only_showable_picks(): ...                # a 1g-profit pick is a candidate but not recorded
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_scanner.py -q`.
- [ ] **Step 3: Implement `scanner.py`.**
- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_scanner.py -q`.
- [ ] **Step 5: Commit.** Run `git add <this task's Files> && git commit -m "feat: hourly scan cycle"`.

---

### Task 9: Web page

**Files:**
- Create: `wow_invest/web.py`, `wow_invest/page.html`, `wow_invest/static/style.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `db.*`, `planner.plan`, `planner.holds_proven`, `pricing.undercut`.
- Produces:
  - `fmt_money(copper:int) -> str`. Returns HTML spans with classes `g`/`s`/`c`, leaving out zero parts. `fmt_money(25000)` contains `2g` and `50s` and no copper span. `fmt_money(0)` returns `0c`.
  - `parse_gold(text:str) -> int`. Accepts `800`, `800.5`, `800g`, `800g 50s` and `1 234`, and returns copper. Raises `ValueError` if the text is empty, negative, not a number, or over 10,000,000g.
  - `safe_icon(url:str|None) -> str|None`. Returns the URL only if it starts with `https://render.worldofwarcraft.com/`.
  - `build_state(conn, now) -> dict`. Gathers:
    - gold
    - candidates and their `captured_at`
    - `plan(...)` with `proven = holds_proven(db.hit_stats(conn))`
    - open holdings with the current Alliance `min_ask` per item
    - crowd, stale flag and hit stats
    - data age from the latest Alliance `auctions_at`
    - the next expected scan
  - `render_page(state:dict, error:str|None=None) -> str`
    - Uses `page.html` through `string.Template`. Every API-supplied string goes through `html.escape`.
    - The name falls back to `Item {id}`.
    - A `<meta http-equiv="refresh" content="300">` refreshes the page.
    - Sections, top to bottom:
      - the stats frame: gold form, suggested spend, expected profit, crowd, last scan, hit rate
      - **Buy in this order**: numbered rows as in the mockup, with an unfunded pick dimmed and labelled "not enough gold", and each row's `<details>` holding the reason, sales per day and sell-by
      - **Sell now**: holdings sorted by sell-by, each with a suggested price of `max(undercut(current min_ask), target)` and a Sold button.
        - If the current price is below break-even, the row says "below cost — wait until sell-by" instead of suggesting a sale. Break-even per unit is `ceil(price_paid / qty / (1 − cut))` plus the deposit.
        - Once sell-by has passed, it shows `undercut(current min_ask)`, labelled with the loss.
      - **On paper**
      - **Check a vendor**, each row with a "A vendor sells this" button
    - Banners:
      - "Gathering price history…" when there are no candidates and no scan older than `MIN_HISTORY_HOURS`
      - "Site data is stale" when `stale == "1"`
      - "Last scan N minutes ago" when the last successful scan is more than 90 minutes old
  - `make_server(db_path:str, port:int) -> ThreadingHTTPServer`, bound to `127.0.0.1`. Routes:
    - `GET /` and `GET /static/style.css`
    - `POST /gold` (`gold`)
    - `POST /bought` (`item_id`, `kind`, `qty`, `price_paid` in gold text, `target` in copper, `sell_by`)
    - `POST /sold` (`holding_id`)
    - `POST /vendor-confirm` (`item_id`)

    Every POST:
    - answers 303 to `/` on success, or re-renders with `error` and status 400 on a `ValueError`
    - is rejected with 403 if an `Origin` header is present and isn't `http://127.0.0.1:<port>` or `http://localhost:<port>`
    - Every request, GET too, is rejected with 403 if the `Host` header isn't `127.0.0.1:<port>` or `localhost:<port>`. This stops a DNS-rebinding page from reading or changing your holdings.
    - uses a new `db.connect` per request

  - `style.css` is the mockup's `.bw` rules, moved to top-level selectors, plus styles for the form, buttons, `.unfunded`, banners and the `details` element.

- [ ] **Step 1: Write the failing tests**

```python
def test_fmt_money():
    assert "2g" in fmt_money(25000) and "50s" in fmt_money(25000) and 'class="c"' not in fmt_money(25000)
    assert 'class="c"' in fmt_money(25050)   # 2g 50s 50c
def test_sell_now_never_suggests_below_cost_before_sell_by(): ...
def test_foreign_host_header_rejected(server): ...                   # Host: evil.example:8765 → 403
def test_parse_gold_accepts_and_rejects():
    assert parse_gold("800") == 8_000_000 and parse_gold("800g 50s") == 8_005_000 and parse_gold("1 234") == 12_340_000
    for bad in ["", "-5", "abc", "1e99"]:
        with pytest.raises(ValueError): parse_gold(bad)
def test_render_escapes_api_strings_and_drops_foreign_icons(): ...   # name "<script>" → "&lt;script&gt;"; icon "https://evil/x.png" absent
def test_render_null_name_falls_back(): ...                          # "Item 14551"
def test_render_empty_state_says_gathering_history(): ...
def test_render_stale_banner(): ...
def test_render_unfunded_rows_marked(): ...
def test_post_gold_stores_and_redirects(server): ...                 # real server on port 0, urllib POST → 303, get_gold == 8_000_000
def test_post_gold_bad_input_keeps_old_value(server): ...            # "abc" → 400, gold unchanged
def test_post_with_foreign_origin_rejected(server): ...              # Origin http://evil.example → 403
def test_bought_then_sold_updates_holdings(server): ...
```

- [ ] **Step 2: Run the tests to make sure they fail.** Run `uv run pytest tests/test_web.py -q`.
- [ ] **Step 3: Implement `web.py`, `page.html` and `style.css`.** For the server fixture, call `make_server(tmp_db, 0)` and run `serve_forever` in a daemon thread.
- [ ] **Step 4: Run the tests to make sure they pass.** Run `uv run pytest tests/test_web.py -q`.
- [ ] **Step 5: Commit.** Run `git add <this task's Files> && git commit -m "feat: bronze priority page with holdings and paper holds"`.

---

### Task 10: Entry point, README and a live run

**Files:**
- Create: `wow_invest/__main__.py`, `README.md`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `scanner.loop`, `web.make_server`, `api.Client`.
- Produces `main(argv:list[str]|None=None) -> int`. It takes `--port` (default 8765) and `--db` (default `data/wow_invest.db`), and handles Ctrl+C. It:
  1. Sets up `logging` at INFO level.
  2. Starts `scanner.loop` in a daemon thread.
  3. Prints `Open http://localhost:<port>`.
  4. Serves until Ctrl+C, then sets the stop event and returns 0.

- [ ] **Step 1: Write the failing test.** `test_main_parses_args_and_starts_both` monkeypatches `scanner.loop` and `web.make_server` with fakes, then calls `main(["--port","0","--db",tmp])`. It asserts that both were called with those values and that `KeyboardInterrupt` from `serve_forever` returns 0.
- [ ] **Step 2: Run the test to make sure it fails.** Run `uv run pytest tests/test_main.py -q`.
- [ ] **Step 3: Implement `__main__.py`.** Write `README.md` covering:
  - what the program does
  - `uv run python -m wow_invest`
  - that the first hours show "Gathering price history"
  - that holds stay on paper until proven
  - that the deposit rates are a guess to check in game
  - that it never touches the game
- [ ] **Step 4: Run the full suite.** Run `uv run pytest -q`. Expected: everything passes.
- [ ] **Step 5: Do a live run.** Run `uv run python -m wow_invest` and wait for the log line `cycle: done` (or `no-change` on a second start). Open `http://localhost:8765` in the Windows browser and enter gold. Expected:
  - the bronze page shows real items and icons
  - suggested spend ≤ the gold entered
  - every row's profit is ≥ 3g
  - Bought and Sold work

  If the history is too short for picks, the "Gathering price history" banner shows instead. That's expected on the first run, because the item `series` holds about 3 days already.
- [ ] **Step 6: Commit and push.** Run `git add <this task's Files> && git commit -m "feat: entry point and README" && git push`.
