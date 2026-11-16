# WoW Invest

A desktop helper for the World of Warcraft auction house. It watches prices and suggests what to buy and what to sell, with the profit it expects after the auction house cut.

It never touches the game. You buy and sell by hand.

Prices come from the public [blackwater.exchange](https://blackwater.exchange) API, read at about one request a second. WoW Invest is not affiliated with Blizzard or blackwater.exchange.

## Install (Windows 10/11)

1. On GitHub, click **Code → Download ZIP** and extract it anywhere.
2. Double-click **`install.cmd`**.
3. Double-click **WoW Invest** on your desktop.

No admin rights are needed.

**Updating:** the program shows an **Update** button when a newer version is on GitHub. Download the new version and run `install.cmd` again; your data is kept. The version you have is shown at the bottom of the window.

If Windows says the file is blocked, right-click `install.cmd`, choose **Properties**, tick **Unblock**, and run it again.

To uninstall, delete the desktop icon and the folder `%LOCALAPPDATA%\WowInvest`. That folder also holds your data.

## First start

1. Pick your region, realm type and faction.
2. Enter the gold you want to trade with.
3. Read the short list of risks and tick the box.
4. Press **Start** and wait for the first scan. The window shows how many minutes are left.

You can change your realm later under **Change realm** at the top.

## Using it

The list is ordered: buy from the top. Click a pick to see why it was chosen and to mark it as bought. Items you hold show up under **Sell now** with the price to list at.

| Tab | What it shows |
|---|---|
| AH flips | Items listed well under their usual price |
| Slow sellers | Underpriced items that sell a few times a day |
| Vendor flips | Items a vendor sells for less than the auction house pays (Alliance only) |
| Crafting | Recipes whose product sells for more than the materials cost |
| Investments | Bets on a price rising over several days |
| Buyouts | Buying a whole cheap shelf and relisting it |
| Daily swings | Fast sellers whose price dips and recovers every day |
| ★ Tracked | Items you starred, with their price on every realm |

Investments, Buyouts and Daily swings are not proven yet. The program shows them but never spends your gold on them.

Other things you can do:

- **Search** for any item by name with the box above the tabs.
- **Star** an item to track it. A tracked item opens to its price on every realm, cheapest first.
- **Filters** set the item type, the sort order and the smallest profit worth showing.
- **Hide** an item you never want suggested again.

## Good to know

- **You can lose gold.** The program gives suggestions, not guarantees: prices move, other players undercut you, and items can fail to sell.
- Every profit is an estimate. A pick that held its price did not necessarily sell.
- Changing your main realm clears the price history gathered so far. Your holdings and settings stay.
- The listing-deposit rates are a guess. Check them in game.
- Your gold, realm and other choices are stored only on your PC.

Made by Wectoz. AI was used in the development of this app.

## For developers

```
uv run pytest -q                 # tests
uv run python -m wow_invest      # run as a local web page on http://localhost:8765 (Linux/WSL)
```

For a release, raise `__version__` in `wow_invest/__init__.py`: installed copies compare theirs with that file on GitHub.

Data lives in `%LOCALAPPDATA%\WowInvest\wow_invest.db` on Windows, and in `data/wow_invest.db` when run from the repo (git ignores it).
