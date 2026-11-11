# wow_invest

A local auction-house advisor for World of Warcraft. It watches prices, then suggests what to buy and what to sell, with the profit it expects after the auction house cut.

Built for **WoW Forever, EU, Normal (PvE), Alliance**, plus the **Blackwater** AH. Other realms and factions are not covered.

It reads the public [blackwater.exchange](https://blackwater.exchange) API, and does it politely: about one request a second, and only when the site has a new scan. It is not affiliated with Blizzard or blackwater.exchange.

It never touches the game. You buy and sell by hand.

## Run it

1. Install [uv](https://docs.astral.sh/uv/).
2. `uv run python -m wow_invest`
3. Open http://localhost:8765 (the server listens on localhost only).

Options: `--port 8765`, `--db data/wow_invest.db`.

Enter the gold you have on the page, and it suggests a spend. Mark what you bought and sold so it can track your holds.

## Good to know

- For the first few hours the page may show "Gathering price history". It needs history before it can judge a price.
- Holds stay "on paper" until they have proven themselves: 10 judged, a 70% hit rate, across 5 items and 3 days.
- The listing-deposit rates are a guess. Check them in game.

## Tests

`uv run pytest -q`

## Data

Everything lives in `data/wow_invest.db` (git-ignored).
