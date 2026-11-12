# WoW Invest

A desktop auction-house advisor for World of Warcraft. It watches prices, then suggests what to buy and what to sell, with the profit it expects after the auction house cut.

Built for **WoW Forever, EU, Normal (PvE), Alliance**. The neutral **Blackwater** AH is switched off for now (`SCAN_HOUSES` in `wow_invest/config.py` turns it back on). Other realms and factions are not covered.

It reads the public [blackwater.exchange](https://blackwater.exchange) API politely: about one request a second, and only when the site has a new scan. It is not affiliated with Blizzard or blackwater.exchange.

It never touches the game. You buy and sell by hand.

## Install (Windows 10/11)

1. On GitHub, click **Code → Download ZIP**, then extract it anywhere. You can also `git clone` it.
2. Double-click **`install.cmd`**.
3. Double-click **WoW Invest** on your desktop.

The installer sets up everything for your Windows user only:
- [uv](https://docs.astral.sh/uv/), a Python manager
- Python 3.12
- pywebview, which draws the window with WebView2, a component built into Windows
- the program, installed in `%LOCALAPPDATA%\WowInvest`

It doesn't need admin rights. To update, download the new version and run `install.cmd` again; your data is kept.

The program opens in its own window. Closing the window quits it.

If Windows says the file is blocked, right-click `install.cmd`, choose **Properties**, tick **Unblock**, then run it again.

**Uninstall:** delete the desktop icon and the `%LOCALAPPDATA%\WowInvest` folder, which also holds your data.

## Using it

Enter the gold you have and the program suggests what to spend it on. Mark what you bought and sold, so it can keep track of what you hold.

- Right after the first start the page says "Gathering price history". Picks can appear after the first scan cycle, which takes a few minutes, because the site already keeps price history. Holds need about a day of scans.
- Holds stay "on paper", meaning tracked but not funded, until their price has held at target often enough: at least 10 judged holds, 70% of them hitting, across at least 5 items and 3 days.
- The listing-deposit rates are a guess. Check them in game.

## For developers

```
uv run pytest -q                 # tests
uv run python -m wow_invest      # run as a local web page on http://localhost:8765 (Linux/WSL)
```

Data: `%LOCALAPPDATA%\WowInvest\wow_invest.db` on Windows; `data/wow_invest.db` when run from the repo, which git ignores.
