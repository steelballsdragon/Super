# Market Bot (stocks & crypto) for Discord

A Discord bot for live **stock and crypto** updates, alerts, a breakout radar, forecasts built on **a century of price history**, automated research reports, and a news desk that
estimates **which markets each headline should move, which way, and by roughly how much**.

It uses free public data, so you don't need any API keys: Yahoo Finance (prices, fundamentals, options, news),
Robert Shiller's S&P 500 data back to 1871, CoinGecko, the Crypto Fear & Greed index, and RSS feeds from
MarketWatch, WSJ, Nasdaq, Investing.com, Seeking Alpha, the Federal Reserve, the SEC, CoinDesk, Cointelegraph,
The Block, Decrypt and Google News. When Yahoo doesn't answer, Nasdaq (US stocks and ETFs), Coinbase and CoinGecko
(crypto) stand in. It knows **every US-listed stock and ETF and the top 1,000 coins** by ticker and name.

Two keys are optional: with an Anthropic key, Claude also reads the headlines; with a
[Massive](https://massive.com) (formerly Polygon.io) key, the NVIDIA channel adds Massive's data, using it for
NVIDIA only and never more than 5 calls a minute (the free plan's limit).

## Channels

Run **`/setup`** once and the bot creates a **📊 Markets** category with six channels:

| Channel | What it gets |
|---|---|
| **📈-stocks** | A **live board** (pinned, edited every minute): S&P 500, Nasdaq, Dow, Russell 2000, VIX, futures outside market hours, the 10-year yield, dollar, gold, oil, and the watchlist with pre-market and after-hours moves. **Alerts** for big moves (indices ±1/2/3%…, stocks ±3/5/7.5/10%…), VIX spikes, new 52-week highs and lows, and fresh **breakout setups** during market hours. A **pre-market brief** at 9:00 ET and a **closing recap** at 4:10 ET on trading days (NYSE holidays are skipped). |
| **🪙-crypto** | A **live board**, 24/7: total market cap, BTC dominance, Fear & Greed, and the watchlist with 1-hour, 24-hour and 7-day changes. Alerts for big daily moves (±5/10/15%… since midnight UTC), **fast moves** (BTC ±2%, ETH ±3%, others ±4% within an hour) and breakout setups. A **daily crypto brief** (8:00 by default; `/settings brief_hour: timezone:`). |
| **📰-market-news** | Market-moving headlines, each with its **expected impact**: e.g. *🔴 ▼ S&P 500 −0.5–1.4% · 🟢 ▲ 10-yr yield +4–12 bp · 🔴 ▼ Gold −0.3–1.1%*. A morning headline digest at 7:30 ET. The biggest stories (importance 80+) are also posted in the stocks or crypto channel. |
| **🔬-research** | A **research digest** after every US close (the strongest setups across both watchlists and the sector ETFs, with deep dives and charts on the top two) and a **week-ahead outlook** every Sunday at 6 PM ET (macro dashboard, valuation, seasonality, presidential cycle, outlooks for the S&P 500, Nasdaq, Bitcoin and Ether). |
| **🔥-trends** | A **live trends board** (pinned, every 5 minutes while the market is open): today's top gainers, losers and most traded US stocks worth $2B+, S&P 500 and Nasdaq-100 breadth, the sectors, the week's leaders and the top crypto movers. A **daily recap** at 4:20 ET, a **weekly recap** on the week's last trading day, a **monthly recap** on the month's last trading day (with year-to-date leaders), and a **crypto recap** just after midnight UTC. Week, month, quarter, year-to-date and 1-year moves cover the S&P 500, the Nasdaq-100, the sectors and 30 major ETFs; crypto covers the top 250 coins (no stablecoins or wrapped coins). |
| **🟩-nvidia** | Everything on **NVIDIA**: a live board (price from Yahoo, plus Massive's last-session bar and VWAP, 50/200-day averages, 20-day EMA, RSI, MACD, 52-week range, market cap, dividends and news sentiment), alerts at ±2/3/4/5/7.5/10%, Massive's NVIDIA news with its sentiment as it comes, a **pre-market brief** at 9:05 ET with the forecast and chart, and a **closing recap** at 4:15 ET. |

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
| `/trends period: market:` | Biggest gainers and losers today, this week, this month, over 3 months, year to date or a year, for stocks, sectors & ETFs, or crypto |
| `/nvidia` | NVIDIA's board (live price and Massive's data) with the forecast and chart |
| `/compare first: second:` | Returns side by side, volatility, worst falls, correlation and beta, and a growth chart |
| `/backtest symbol:` | Would following the model have beaten buy-and-hold? A walk-forward test (each year predicted by a model trained only on earlier years) with an equity curve |
| `/alert symbol: price:` · `/alerts [remove]` | Pings you in the channel when a price is crossed |
| `/record` | How the bot's own calls have done: forecasts, breakout calls and news calls, graded automatically |
| `/brief kind:` | Posts any of the scheduled briefs here now |
| `/status` · `/update` · `/help` | Health of every job and data source, models and news reader; update from GitHub; overview |

Symbols autocomplete as you type from the full list (every US stock and ETF, the top 1,000 coins, indices and
futures), and names work: `nvidia`, `brk.b`, `hyperliquid`, `btc`, `s&p`, `gold`, `10y`, `dollar`.

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

### Railway (about $5/month, no server to manage)

1. **New Project → Deploy from GitHub repo →** this repository. Railway detects Python, installs
   `requirements.txt` and runs `python main.py` by itself; no start command or config file is needed.
2. In the service's **Variables** tab, add `MARKET_DISCORD_TOKEN` (and optionally `ANTHROPIC_API_KEY` and
   `MASSIVE_API_KEY`), then apply the changes.
3. **Attach a volume** to the service (right-click it on the canvas, or ⌘K → *Add volume*) with mount path
   `/data`. The bot finds it on its own (Railway sets `RAILWAY_VOLUME_MOUNT_PATH`), and keeps its price history,
   channels, alerts and track record there across redeploys. With a volume, Railway also never runs two copies at
   once during a redeploy, so nothing gets posted twice.
4. On the Hobby plan, set **Settings → Deploy → Restart policy** to *Always*, and set a spending cap under
   **Workspace → Usage → Set Usage Limits** (e.g. an email alert at $5 and a hard limit at $10).

It uses about 0.3–0.4 GB of memory and very little CPU: roughly $4–5 of usage a month, which the Hobby plan's
$5 covers. Notes:

- **Trial:** a verified trial runs the bot fine. An unverified ("Limited") trial blocks most outbound network
  access, so the bot can't reach Discord.
- **After the trial:** the Free plan ($1 of credit and 0.5 GB of memory a month) can't keep it running 24/7.
  Switch to Hobby; the volume and its data carry over. A lapsed trial's volume is deleted after 30 days.
- **Backups:** Hobby has no automatic volume backups. If the volume were ever lost, price history re-downloads
  and `/setup` reuses the existing channels; watchlists, price alerts and the track record would start over.
- **Updates:** Railway redeploys on every change pushed to GitHub, so `/update` isn't needed there.
- **Yahoo on cloud hosts:** Yahoo turns away requests that don't look like a browser's, so the bot connects the
  way Chrome does (curl_cffi). `/status` shows each data source's state and the exact error if one fails. If
  Yahoo still refuses Railway's shared IP address, the backups keep boards, prices, forecasts and today's movers
  going, and `YAHOO_PROXY` can send just the Yahoo requests through a proxy.

A service first set up for ScoreBot keeps working: its `DISCORD_TOKEN` is used, and the folder of its
`DATA_FILE` becomes the data folder.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MARKET_DISCORD_TOKEN` | (required) | Bot token (`DISCORD_TOKEN` also works) |
| `MARKET_DATA_DIR` | the Railway volume if one is attached, else `market-data` | Where price history, models, channel settings (`channels.json`), state and the track record (`record.json`) are kept |
| `LIVE_INTERVAL` | `60` | Seconds between live board and alert updates (minimum 30) |
| `ANTHROPIC_API_KEY` | (none) | Turns on Claude as a second news reader |
| `MASSIVE_API_KEY` | (none) | Massive (Polygon.io) key for the NVIDIA channel; at most 5 calls a minute, NVIDIA only (`POLYGON_API_KEY` also works). Massive's free and individual plans are licensed for personal use |
| `YAHOO_PROXY` | (none) | Proxy URL for Yahoo Finance requests only, if Yahoo blocks the host's IP address |
| `NEWS_AI_MODEL` | `claude-opus-5-5` | Claude model for the news reader |
| `NEWS_AI_DAILY_CALLS` | `150` | Cap on news-reader calls per day |
| `DEV_GUILD_ID` | (none) | Sync slash commands to one server instantly while developing |

## Built to run unattended

- Every job (boards, alerts, news, scans, briefs, grading, training) runs on its own schedule and is isolated:
  a failure is logged, shown in `/status`, and retried next time. A source that fails three times in a row is
  rested for 30 seconds to 5 minutes while the backups answer; a stale answer beats none when a source is down.
  When nothing can answer, commands say the data sources are down instead of claiming a symbol doesn't exist.
- Massive calls go through one sliding-window limiter: never more than 5 in any 61 seconds, whatever asks
  (refreshes, commands, retries), and a 429 pauses them for a minute. The free plan has end-of-day data only, so
  NVIDIA's live price comes from Yahoo; with a paid key, Massive's delayed snapshot is used automatically.
- Price history is saved on disk (each symbol's century downloaded once, then only the latest days; fully
  re-downloaded weekly so dividend adjustments stay right). A backup's shorter history is joined onto the saved
  one rather than replacing it. Saved files are written atomically.
- The symbol list ships with the bot and refreshes itself weekly (Nasdaq's stock and ETF lists, Yahoo's crypto
  list, the S&P 500 and Nasdaq-100 members).
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

The tests (about 1,500) use synthetic prices and fake Discord and data clients, so they run offline: every data
source failing in every way it can, the 5-a-minute Massive budget under heavy concurrency and restarts, and every
post checked against Discord's limits.

For a dress rehearsal against the real data sources (Discord faked out), with Yahoo up or unreachable:

```bash
python -m tests.live_rehearsal
python -m tests.live_rehearsal --yahoo-down
```
