# Market Bot (stocks & crypto) for Discord

A Discord bot for live **stock and crypto** updates, alerts, a breakout radar, forecasts built on **a century of price history**, automated research reports, and a news desk that
estimates **which markets each headline should move, which way, and by roughly how much**.

It uses free public data, so you don't need any API keys: Yahoo Finance (prices, fundamentals, options, news),
Robert Shiller's S&P 500 data back to 1871, CoinGecko, the Crypto Fear & Greed index, and RSS feeds from
MarketWatch, WSJ, Nasdaq, Investing.com, Seeking Alpha, the Federal Reserve, the SEC, CoinDesk, Cointelegraph,
The Block, Decrypt and Google News. An Anthropic API key is optional: with one, Claude also reads the headlines.

## Channels

Run **`/setup`** once and the bot creates a **📊 Markets** category with four channels:

| Channel | What it gets |
|---|---|
| **📈-stocks** | A **live board** (pinned, edited every minute): S&P 500, Nasdaq, Dow, Russell 2000, VIX, futures outside market hours, the 10-year yield, dollar, gold, oil, and the watchlist with pre-market and after-hours moves. **Alerts** for big moves (indices ±1/2/3%…, stocks ±3/5/7.5/10%…), VIX spikes, new 52-week highs and lows, and fresh **breakout setups** during market hours. A **pre-market brief** at 9:00 ET and a **closing recap** at 4:10 ET on trading days (NYSE holidays are skipped). |
| **🪙-crypto** | A **live board**, 24/7: total market cap, BTC dominance, Fear & Greed, and the watchlist with 1-hour, 24-hour and 7-day changes. Alerts for big daily moves (±5/10/15%… since midnight UTC), **fast moves** (BTC ±2%, ETH ±3%, others ±4% within an hour) and breakout setups. A **daily crypto brief** (8:00 by default; `/settings brief_hour: timezone:`). |
| **📰-market-news** | Market-moving headlines, each with its **expected impact**: e.g. *🔴 ▼ S&P 500 −0.5–1.4% · 🟢 ▲ 10-yr yield +4–12 bp · 🔴 ▼ Gold −0.3–1.1%*. A morning headline digest at 7:30 ET. The biggest stories (importance 80+) are also posted in the stocks or crypto channel. |
| **🔬-research** | A **research digest** after every US close (the strongest setups across both watchlists and the sector ETFs, with deep dives and charts on the top two) and a **week-ahead outlook** every Sunday at 6 PM ET (macro dashboard, valuation, seasonality, presidential cycle, outlooks for the S&P 500, Nasdaq, Bitcoin and Ether). |

Already have channels? Run `/channel kind:` in each instead. `/settings` turns alerts or briefs off, sets how
picky the news channel is (`major` 75+, `important` 55+ by default, `all` 35+), and the crypto brief's hour and
time zone. `/watchlist` changes a stocks or crypto channel's list (up to 30; names work too, e.g. `add: tesla, AMD`).

## Commands

| Command | What you get |
|---|---|
| `/price symbol:` | Live price, day and 52-week range, volume vs normal, returns, and an intraday chart |
| `/chart symbol: period:` | Candles with 20/50/200-day averages, Bollinger Bands, support/resistance, volume, RSI and the forecast cone (1D to MAX; long periods switch to a log-scale line) |
| `/forecast symbol:` | The outlook: breakout odds, chance of being higher in a week / month / 3 months, Monte Carlo price ranges, historical look-alikes, active setups with their track record, key levels and what's driving the model |
| `/research symbol:` | Everything in `/forecast`, plus performance and risk since listing, valuation, analyst targets and ratings, earnings dates and beat rate, **options positioning** (expected move, implied vs realized volatility, put/call ratios, call and put walls, max pain), this month's seasonality, the halving cycle for Bitcoin, and the latest news scored for sentiment |
| `/breakouts market:` | Scans 60–80 symbols (watchlist, mega caps, sector ETFs and today's most active stocks; or the top 40 coins) for fresh breakouts, coils, flags and squeezes, ranked by breakout pressure, with a chart of the top one |
| `/news [symbol] [market]` | The last day's most important stories with impact estimates, or one symbol's news |
| `/history symbol:` | The long view: growth since the first price, decades, the biggest crashes and recoveries, best and worst years, the average month, the US presidential cycle. The S&P 500 goes back to **1871** (Shiller's monthly data joined to daily data from 1927) |
| `/macro` | VIX (and its percentile since 1990), yields and the yield curve, the dollar, gold, oil, copper, crypto Fear & Greed with what Bitcoin did after similar readings, a stock-market fear & greed gauge, and the S&P 500's **Shiller CAPE** with the 10-year return history implies at today's valuation |
| `/movers market:` | Today's biggest US gainers and losers, or the top-100 coins' |
| `/compare first: second:` | Returns side by side, volatility, worst falls, correlation and beta, and a growth chart |
| `/backtest symbol:` | Would following the model have beaten buy-and-hold? A walk-forward test (each year predicted by a model trained only on earlier years) with an equity curve |
| `/alert symbol: price:` · `/alerts [remove]` | Pings you in the channel when a price is crossed |
| `/record` | How the bot's own calls have done: forecasts, breakout calls and news calls, graded automatically |
| `/brief kind:` | Posts any of the scheduled briefs here now |
| `/status` · `/update` · `/help` | Health of every job and data source, models and news reader; update from GitHub; overview |

Symbols autocomplete as you type, and names work: `nvidia`, `btc`, `s&p`, `gold`, `10y`, `dollar`.

## How the predictions work (and how good they are)

Nobody can predict markets reliably, and this bot doesn't pretend to: every number comes with the base rate
it's competing against, and the bot grades itself in public with `/record`.

- **Prediction models.** Logistic regressions on 29 features (momentum over 6 horizons, distance from moving
  averages and 52-week highs/lows, RSI, MACD, Bollinger width, volatility regime, volume, range tightness, ADX…),
  trained on pooled daily history: ~120,000 days from 27 stock and index histories going back to **1929**, and
  14 coins back to 2015. They're retrained every 3 days in the background.
- **Tested on years they never saw.** Walk-forward over the last 12 years (train on everything before each
  2-year block, predict the block):
  - **Breakouts** (*will it close above its 20-day high within 10 sessions?*): AUC **0.82** for stocks and
    crypto, well calibrated (when it said 70% or more, it happened 75–78% of the time). This is the strong suit.
  - **Direction** (*higher in 1 week / 1 month / 3 months?*): AUC **0.48–0.51**, i.e. no better than a coin
    flip, as you'd expect from markets. So the bot shrinks these numbers toward history's base rates (the S&P 500
    has been higher after a month 58% of the time) in proportion to the model's tested skill, and blends in the
    look-alikes. It says so on every forecast.
- **Historical look-alikes.** The 30 days in all of history (e.g. S&P 500 since 1928) whose setup looked most
  like today's (momentum, trend, volatility and the last 30 days' path), and what happened 5, 20 and 60 days
  later.
- **Price ranges.** Monte Carlo (filtered historical simulation): 2,000 paths replaying history's own shocks
  with volatility that clusters and drifts back to normal like a GARCH model. Gives the 90% range for 1 month,
  3 months and 1 year, and the odds of touching ±10%.
- **Setups.** 28 patterns (20-day and 52-week breakouts, record highs, squeezes, coils under resistance, bull and
  bear flags, golden/death crosses, 200-day reclaims, RSI divergences, volume surges, gaps…) found across each
  symbol's whole history, so each one shows how often it worked **on that chart** before (and on the S&P 500).
- **The score.** A −100…+100 *technical* score (trend, momentum, direction odds, breakout balance, setups). It's
  a summary of the picture, not a prediction on its own.
- **Backtests.** `/backtest ^GSPC` (at the time of writing): timing the S&P 500 with the model since 2017 made
  9.6%/yr against 13.5%/yr for holding, with half the worst drawdown (−17% vs −34%). Market timing rarely beats holding; the bot shows
  that rather than hiding it.

### News impact

Each headline is classified into one of 29 event types (Fed/rates, inflation, jobs, growth, tariffs,
geopolitics, oil, banking stress, bond yields, the dollar, AI/chips, earnings, M&A, analyst ratings, legal,
FDA, bankruptcy, buybacks, index changes, layoffs, crypto ETFs, crypto regulation, hacks, exchange failures,
stablecoins, adoption, network upgrades…). Then:

1. **Which way:** event-specific wording decides whether it's good or bad news for risk assets (a "hot" CPI is
   bad for stocks even though prices "rise"; "cuts the odds of a hike" is good news even though it says "hike").
2. **Which markets and how much:** each event type moves a set of markets by a typical amount from event studies
   (a CPI surprise ≈ 0.9% on the S&P 500, 8 bp on the 10-year yield, 2.5% on Bitcoin), scaled by how strong the
   wording is, whether it's speculation ("could", "reportedly"), and how volatile each market is right now.
3. **Ranking:** opinion pieces, listicles, recaps of moves that already happened, and other countries' data rank
   low; stories carried by several outlets rank higher.
4. **Learning:** every call on a posted story is checked against what the market did over the next day. The
   hit rate shows in `/record`, and each event type's sizes are re-scaled toward what markets actually did.

**Optional: Claude reads the news too.** Set `ANTHROPIC_API_KEY` and the important headlines (importance 40+)
are sent to Claude in batches of 10; its read (event, one-line takeaway, which markets, direction, size,
confidence) replaces the keyword model's for those stories. It uses `claude-opus-5-5` by default
(`NEWS_AI_MODEL` changes it) and is capped at 150 calls a day (`NEWS_AI_DAILY_CALLS`); a busy news day is
typically 50–100 calls, roughly a few dollars a day at Opus pricing. The news footer says which reader scored
each story.

## Setup

1. Create an application at https://discord.com/developers/applications, add a **Bot**, and copy its token.
2. Invite it: **OAuth2 → URL Generator**, scopes `bot` and `applications.commands`, permissions
   `Send Messages`, `Embed Links`, `Attach Files`, `Manage Messages` (to pin the live boards) and `Manage Channels`
   (for `/setup`).
3. Install and run:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   export MARKET_DISCORD_TOKEN=your-token-here
   python -m marketbot
   ```

4. In Discord, run `/setup`. Boards appear within a minute; the models train in the background on first start
   (about a minute; forecasts work meanwhile from look-alikes and history).

### 24/7 on a server (Oracle Cloud Always Free, any Ubuntu 22.04+)

```bash
curl -fsSL https://raw.githubusercontent.com/steelballsdragon/Super/main/deploy/install-marketbot.sh | sudo bash
```

It asks for the token (and an optional Anthropic API key) once, stores them in `/etc/marketbot.env` (readable
only by root), and runs the bot as its own `marketbot` service and user, with its data in `/var/lib/marketbot`.
A timer checks GitHub every 5 minutes and installs new code (or use `/update`). Logs: `sudo journalctl -u marketbot -f`. It peaks at about 350–400 MB of memory (while training).

### Switching a server over from ScoreBot

This repository used to hold ScoreBot, a sports bot; it has been removed. A server that ran it picks up this
change by itself and starts the market bot under its old `scorebot` service and token, but that service no
longer updates. Move the bot to its own service (and keep the same bot in Discord):

```bash
sudo grep DISCORD_TOKEN /etc/scorebot.env        # copy the token: the market bot can reuse it
sudo systemctl disable --now scorebot scorebot-update.timer
sudo rm -f /etc/systemd/system/scorebot.service /etc/systemd/system/scorebot-update.service \
           /etc/systemd/system/scorebot-update.timer /etc/sudoers.d/scorebot-update
sudo systemctl daemon-reload
sudo rm -rf /opt/scorebot /var/lib/scorebot /etc/scorebot.env   # also deletes ScoreBot's saved data
sudo userdel scorebot
curl -fsSL https://raw.githubusercontent.com/steelballsdragon/Super/main/deploy/install-marketbot.sh | sudo bash
```

Paste the old token when the installer asks. The sports commands disappear from Discord once the market bot
registers its own (it can take up to an hour to show everywhere). Give the bot's role **Manage Channels**,
**Manage Messages** and **Attach Files** in Server Settings → Roles, then run `/setup`.

### Railway

Deploy this repository; the included `Procfile` starts the bot (`python main.py`). Add the `MARKET_DISCORD_TOKEN`
variable, attach a volume and set `MARKET_DATA_DIR` to a path on it so the saved history, channels and track
record survive redeploys. (A service set up for ScoreBot keeps working: its `DISCORD_TOKEN` is used, and the
folder of its `DATA_FILE` becomes the data folder.)

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MARKET_DISCORD_TOKEN` | (required) | Bot token (`DISCORD_TOKEN` also works) |
| `MARKET_DATA_DIR` | `market-data` (or the folder of an older `DATA_FILE` setting) | Where price history, models, channel settings (`channels.json`), state and the track record (`record.json`) are kept |
| `LIVE_INTERVAL` | `60` | Seconds between live board and alert updates (minimum 30) |
| `ANTHROPIC_API_KEY` | (none) | Turns on Claude as a second news reader |
| `NEWS_AI_MODEL` | `claude-opus-5-5` | Claude model for the news reader |
| `NEWS_AI_DAILY_CALLS` | `150` | Cap on news-reader calls per day |
| `DEV_GUILD_ID` | (none) | Sync slash commands to one server instantly while developing |

## Built to run unattended

- Every job (boards, alerts, news, scans, briefs, grading, training) runs on its own schedule and is isolated:
  a failure is logged, shown in `/status`, and retried next time. Yahoo and CoinGecko rate limits are retried
  with backoff; a stale answer beats none when a source is down.
- Price history is saved on disk (each symbol's century downloaded once, then only the latest days; fully
  re-downloaded weekly so dividend adjustments stay right). Saved files are written atomically.
- Alerts never repeat: each move line, setup, 52-week high, news story and brief is remembered (and pruned).
  A restart doesn't repost anything, and the first news run on a new install posts only the top 3 stories.
- Every post fits Discord's limits.

Prices from free sources can be delayed (stocks up to 15 minutes on some exchanges). This is research and
entertainment, not financial advice.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest
```

The tests use synthetic prices and fake Discord and data clients, so they run offline.
