# wow_invest: design

Date: 2026-11-11
Status: draft, awaiting the user's approval

## Intent

A program the user runs on their own PC. Every hour it reads auction house data from blackwater.exchange, researches the deals there more deeply than the site does, and shows a ranked "buy this first" list of flips and short investments. The list is sized to how much gold the user has right now.

Why: the site's own deals page lists everything priced under its usual price. In a market only days old that list is misleading. Prices are falling fast as more players farm, so "under usual" often just means "the new normal". The user wants a list they can trust: picks that actually sell and don't lose gold.

## Context (from the user)

- Game: WoW Forever, **EU, Normal (PvE)** ruleset, **Alliance**. The user can also buy and sell on the neutral **Blackwater** auction house.
- The realm launched only days ago. Almost nobody is 60, and the user estimates the average player is level **20–30**. The user is level 45, ahead of the curve.
- The market moves very fast right now. Example from the API: Devilsaur Leather on the Alliance AH fell from about 15g to 1g50 between 10 and 11 November, while the site's "usual price" for it still said 16g58.
- The user does not use the Forever Craft Assistant addon or the site's Ledger.

## Goals

1. **Hourly ranked list.** A priority list where #1 is what to buy first, with real item icons. It covers four kinds of trade:
   - **Quick flip:** buy under the going price on the Alliance AH and relist there.
   - **Cross-AH flip:** buy on Blackwater, sell on Alliance, or the reverse.
   - **Vendor flip:** buy from an NPC vendor the Alliance can reach, sell on the AH.
   - **Short hold:** 1–3 days. Buy items the crowd is flooding the market with right now, and sell once the crowd has levelled past them. Examples: herbs and ores from the zones most players are in, and world drops from lower zones.
2. **Ranking (the user's rule):** profit per gold spent, and how fast it sells. Score = (net profit ÷ gold spent) ÷ expected days to sell. Ties don't need special handling.
3. **Fitted to the user's gold.** The user types their current gold into the page. Picks are funded from #1 downward until the gold runs out. The page shows gold, suggested spend, expected profit, and the estimated level of the crowd.
4. **Follows the crowd.** The program estimates where the typical player's level is and how it is moving. It starts from the user's 20–30 guess and corrects that from AH data. Hold picks and their reasons use this estimate.
5. **Keeps score.** Every pick the page could show is saved.
   - A pick is judged at its sell-by time.
   - It's a hit if the price held at or above its target for at least 3 scans in a row before then.
   - The page shows that hit rate.
6. **Knows what the user holds (user decision).**
   - **"I bought this" button** on each pick. It records quantity and price paid, filled in from the pick and editable.
   - **"Sell now" list** of what the user holds, showing for each item:
     - its current going price
     - a suggested listing price
     - its sell-by time
   - **"Sold" button** to clear an item from the list.
   - **Holdings shape the advice:**
     - The quantity cap (2–3 days of sales) counts units already held, so the same item isn't recommended again and again.
     - The 25% cap on holds counts all open holds together.
   - **Gold is still typed in by the user.**
7. **Holds start on paper (user decision).**
   - Hold picks appear in a separate "On paper" section. They are tracked but not funded, and not counted in suggested spend.
   - They only join the funded list once, **among the last 20 judged holds**, these all hold:
     - at least 10 have been judged
     - at least 70% hit
     - they cover at least 5 different items
     - they were picked on at least 3 different days

     These thresholds are my call; the user agreed to "paper first". The item and day rules stop one lucky item from unlocking real gold.
   - Quick, cross-AH and vendor flips are funded from day one.
8. **Worth the user's time (user decision).** Cheap items are fine, but every pick must make **at least 3g net profit** (my reading of "a few gold at least"). The list shows the top 15 that clear that (my call).

## Anti-goals: what makes it a failure even if it runs

The user said they would stop trusting it if:
- **It recommends flips that never sell.**
- **They lose money on investments.**

Every rule below that makes a pick less likely to be wrong exists because of this. When in doubt, the program shows fewer picks, not more.

## Non-goals

- Horde AH, other rulesets, US region.
- Crafting profit. The site has a crafting endpoint, but crafting is out of scope.
- Any in-game automation, an addon, or reading game files. The program only recommends; the user buys and sells by hand.
- Discord or phone notifications. A desktop notification for exceptional deals was shown in a mockup and not chosen. It can come later.
- Accounts, multiple users, hosting it online.

## Data source (verified 2026-11-11)

Public JSON API, no login needed. `robots.txt` allows everything except `/admin`. The site refreshes about hourly; the latest scan was captured at around :13 past the hour.

Market keys used: `eu/2/111` is the Alliance AH (Normal PvE) and `eu/2/117` is the Blackwater AH (Normal PvE).

| Endpoint | What it gives |
|---|---|
| `GET /v1/markets` | All markets with `captured_at`. Use this to detect a new scan before fetching anything else. |
| `GET /v1/markets/{r}/{rs}/{h}/deals?limit=&offset=` | Items under their usual price: `cost_copper`, `usual_copper`, `profit_copper`, `sold_24h`, `units`, `below_vendor`, `vendor_sell_copper`, `icon`, `quality`. Also returns `auction_cut` (0.05) and a 7-day baseline. |
| `GET /v1/markets/{r}/{rs}/{h}/items?sort=&limit=&offset=` | Every item on the AH with `minimum_ask_copper`, `average_ask_copper`, `units`, `listings`, `sold_24h`, `removed_24h`, `early_removed_24h`, `sold_value_24h`, `required_level`, `item_level`, `class_name`, `subclass_name`. At most 200 per page; about 5,100 items, so about 26 pages per AH. |
| `GET /v1/markets/{r}/{rs}/{h}/items/{id}` | One item. `item` holds static data: `purchase_price` (vendor buy), `sell_price` (vendor sell), `purchase_quantity`, `required_level`, `class`. `ladder` is the price depth: units and listings at each price. `series` is hourly history since about 2026-11-08 (`units`, `minimum_ask_copper`, `new_units`, `removed_units`, `early_disappearances`). `current` has `usual` at 3, 7 and 14 days, `sold_24h`, `median_ask_copper`. `houses` has this item's price on every AH. |
| `GET /v1/markets/{r}/{rs}/{h}/rises`, `/sales`, `/search?q=` | Items priced above usual, sales per hour across the whole AH, and name search. |

**What the sales numbers mean (confirmed 2026-11-11 from the site's own help text):**
- Blizzard does not report sales. The site estimates them from what left the AH between hourly scans.
- **Likely sold** (`partial_fills` in `series`): a stack shrank at the same price.
- **Sold or cancelled** (`early_disappearances`): a listing vanished before it could have expired.
- **Expired or unknown** (`ambiguous_removals`): everything else that left.
- `sold_24h` and `early_removed_24h` include sold-or-cancelled. For Devilsaur Leather, `sold_24h` equalled `early_removed_24h` and `partial_fills` was 0. So `sold_24h` is an upper bound.

*Rule (my call, cautious on purpose):*
- **Effective sales per day** = likely sold + ½ × sold or cancelled, over the last 24h of `series`.
- **For screening from the item list**, where `series` isn't fetched, use ½ × `sold_24h`.

**Being polite to the site:**
- Fetch only when `captured_at` changes.
- Keep requests about a second apart.
- Use a generic User-Agent with no personal details.
- Back off on errors.

A full refresh is roughly 26 list pages × 2 AHs, plus item detail for the top candidates and a slow backfill of static item data, so about 200 requests per new scan. At one per second that's about 3–4 minutes an hour. The User-Agent is neutral (`ah-helper/0.1`), so it can't be traced to the public repo. Static item data (`purchase_price`, `required_level` and so on) never changes; fetch it once per item and keep it.

## How a pick is judged

1. **Going price, not the 7-day average.** The reference price is what the item actually cleared at recently, from the last 24h of `series`, `median_ask_copper` and the `ladder`. The site's `usual` price is ignored when the series shows a trend.
2. **Trend check.** If the price has been sliding for 24–48h, a low price is not a deal. Allow a buy of a falling item only if sales are holding and listed units are shrinking.
3. **Real cost.** The cost of buying N units is walked up the `ladder`, not N × the minimum price.
4. **Realistic sell price.** Price just under the current competition, minus that AH's cut and the listing deposit. Read the cut from the API's `auction_cut` field. Checked 2026-11-11: Alliance takes **5%** and Blackwater takes **15%**, which matters for cross-AH flips.
5. **Days to sell** = units bought ÷ the share of daily likely-sold units the user can expect to capture. *My call:* assume half of daily sales unless the data supports more.
6. **Quantity cap.** Never more than about 2–3 days of normal sales, even for a hold.
7. **Confidence is a filter, not part of the score.** Hide items with too few sales or too little history. The page shows a "sure" bar for each pick.

### Short holds and the crowd

- **Crowd level estimate:** a weighted median of `required_level` across equipment actually sold in the last 24h. Track it hourly to see the crowd moving. Start from 20–30.
- **Gathered goods:** a hand-made table maps each herb and ore to the level band of the zones it grows in. The builder writes this table. Items with a `required_level` use that.
- **Flood signal:** an item from the crowd's current band, with listed units rising and the price falling.
- **Hold pick:** buy during the flood, sell 1–3 days later as the crowd moves on.
- **Holds use their own rules (my call).** A flood is by definition a falling price, so rule 2 (trend check) and rule 4 (sell under current competition) don't apply to holds. Instead:
  - **Sell target:** a forecast, not today's price. Start conservative: recover only half of the drop back toward the price before the flood (the median of the 24–48h before listed units started rising). Then tune it from the track record.
  - **Extra filter in place of the trend check:** sales must still be steady or rising during the flood. If sales fall along with the price, it's not a flood, it's lost demand, and there's no pick.
- **Holds are always riskier than flips:**
  - Each hold carries a sell-by time.
  - No single hold may take more than about 25% of the user's gold.
  - Holds stay on paper until proven (Goal 7).
- **Endgame materials are a special case.** Level-50+ goods like Devilsaur Leather, Black Lotus and Thorium are scarce now and likely to get cheaper as more players reach 60. Treat holds on them with extra suspicion; quick flips on them are fine.

### Vendor flips

- `purchase_price` exists on many items no NPC actually sells, so it alone does not mean a vendor flip.
- *My call:* vendor flips use a small hand-kept list of items known to be sold by vendors the Alliance can reach, or items the user marks as confirmed in the page. Anything else with AH price far above `purchase_price` is shown at most as "check whether a vendor sells this", never ranked.
- Also account for `purchase_quantity` (sold in stacks) and limited-stock vendors.

## The page

The look the user approved in the mockups (`.superpowers/brainstorm/*/content/top-flips-priority.html`):

- **Look:** dark background, bronze frames, serif type, gold/silver/copper coloured money, item names in quality colours. It's styled after WoW but uses no Blizzard art apart from the item icons the API provides.
- **Header:** realm and faction, last scan and next scan, a gold input, suggested spend, expected profit, and the estimated crowd level.
- **List:** one numbered list. Each row has:
  - rank and real icon
  - name, plus a tag for the kind of trade (Hold / Quick flip / Cross-AH / Vendor flip)
  - where to buy and sell, e.g. "Blackwater → Alliance"
  - a one-line reason
  - quantity × max price → sell price
  - net profit, time to sell, and the "sure" bar
- **Clicking a row** shows why it was picked: price trend, sales per day, ladder depth.
- **Bought button:** each row has an "I bought this" button.
- **Sell now:** a "Sell now" section lists holdings, sorted by sell-by time, each with a "Sold" button.
- **On paper:** an "On paper" section shows unfunded hold picks and the hold hit rate so far, e.g. "6 of 8 hit (75%)".
- **Track record:** the page shows the hit rate of past picks.
- **Refresh:** the page refreshes itself after each new scan.

## How it gets made (my calls; the user handed these to me)

- **Python**, running on the user's PC (WSL), as one long-running process that:
  - checks `/v1/markets` every few minutes and fetches only when there's a new scan
  - saves every scan in a local **SQLite** database
  - serves the page on `localhost`, so the Windows browser can open it
- Keep dependencies few. The standard library covers HTTP, SQLite and serving; add a small web framework only if it clearly saves effort.
- Icons are shown from `render.worldofwarcraft.com`, where the site itself loads them.

## Left to the builder

- Exact thresholds: minimum sales per day, minimum hours of history, and what counts as a falling trend. Pick sensible values, keep them as constants in one place, and tune them against the stored history.
- The herb and ore level-band table, and the starter list of vendor-sold items.
- The listing deposit formula for this game version.
- Layout details beyond the approved mockup.
- How picks are re-judged for the track record: "target reached before sell-by" is the rule; the details are the builder's.
