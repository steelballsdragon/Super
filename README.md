# Live Scores Discord Bot

A Discord bot that posts live **NFL, NBA, MLB, NHL, soccer and cricket** updates to your channels, using ESPN's free public data (you don't need an API key).

## Features

In a channel that follows a league, `/scores`, `/research` and `/unfollow` don't need the league: they use the
channel's. If it follows several, a team you type picks the league (e.g. `/research team:Chiefs` in a channel following
NFL and MLB), and `/scores` on its own shows every followed league.

- `/scores [league] [team]`: shows the current scoreboard (live, upcoming and finished games).
- `/follow <league> [team]`: posts live updates in this channel for a whole league or for one team.
- `/unfollow [league] [team]`: stops those updates.
- `/following`: lists what this channel follows.
- `/status`: shows when the bot last checked each followed league, how many games are live, and any errors.
- `/update`: (admins) checks GitHub for a new version right away, instead of waiting up to 5 minutes.
- `/scoreboard`: posts a live scoreboard in this channel and pins it. It keeps editing itself with every game the
  channel follows (live first, then upcoming, then recent results). `/scoreboard enabled:False` removes it.
- `/schedule`: today's games for everything this channel follows.
- `/daily enabled:True hour:9 timezone:America/Toronto`: posts today's games every morning at that hour (skipped on
  days with nothing on). Start times show in each reader's own time zone.
- `/reminders enabled:True`: posts a heads-up 15 minutes before each followed game.
- `/odds enabled:False`: hides betting lines in this channel (on by default). Game starts show the DraftKings line
  (spread, over/under, moneyline; draw for soccer), and finals show how it settled, e.g.
  *Spread: Indianapolis Colts -4.5 ✅ covered · Total: Under 47.5 ✅ (47) · Moneyline: Indianapolis Colts -205 ✅*. The pre-game line is saved, so
  bets are graded against the closing line. Soccer bets settle on the 90-minute score, as sportsbooks do.
- `/threads enabled:True`: puts each game's updates in its own thread. The start and result post in the channel;
  goals, plays, wickets and ball-by-ball go in the game's thread.

The team option suggests teams as you type, e.g. typing `ind` offers *India* and *West Indies*.

Each sport posts the moments that matter for it:

| Sport | Live updates |
|---|---|
| ⚽ **Soccer** | Kick-off, every goal (scorer, minute and assist, marked as a penalty or own goal), half-time, full-time, and VAR score corrections |
| 🏈 **NFL** | Game start, every scoring play with the players, yards and extra point or two-point try (e.g. *Roman Wilson 12 Yd pass from Aaron Rodgers (Chris Boswell Kick)*), the score at the end of each quarter, half-time, and the final with passing, rushing and receiving leaders |
| ⚾ **MLB** | First pitch, every run with the play (e.g. *Albies homered to right center (419 feet), Riley scored.*), and the final with each team's top performer |
| 🏒 **NHL** | Puck drop, every goal with the scorer, shot type and assists (on their own line; marked as power-play, shorthanded or empty-net), the score at the end of each period, and the final with each team's top scorer |
| 🏀 **NBA** | Tip-off, the score at the end of each quarter, half-time and the final with each team's top performer. It doesn't post every basket. |
| 🏏 **Cricket** | Match start with the toss, every wicket, the score every 5 overs (T20) or 10 overs (ODI and Test) with the chase equation, the innings break, and the result (e.g. *RCB won by 5 wkts (12b rem)*). Optional **ball-by-ball** mode posts every delivery (see below). |

Supported leagues: NFL, NBA, MLB, NHL, Premier League, La Liga, Serie A, Bundesliga, Ligue 1, MLS, Champions League,
Europa League, FIFA World Cup, IPL, and international cricket (every current Test, ODI and T20I, men's and women's).
To add more, edit `sportsbot/leagues.py` with any ESPN path, for example `soccer/ned.1`.

Results are reported correctly in tricky cases too: penalty shootouts (*Paraguay win 4-3 on penalties*),
extra time, NHL shootouts, and postponed, suspended or cancelled games (posted as **Postponed** etc., never as a result).
When a game ends on a score, such as a walk-off home run, the winning play is posted before the final result.

### Betting research

Everything is one command, `/research [league] [team] [parlay]` (the league can be left out in a channel that follows one):

| You type | You get |
|---|---|
| `/research league:NFL` | The league's strongest leans, most likely results, and a **Safe** parlay |
| `/research league:NFL team:Chiefs` | Everything on that team's next game: market, model, form, injuries, leans and player trends |
| `/research league:NFL parlay:Safe` | A parlay built to about **+100** |
| `/research league:NFL parlay:Big payout` | A parlay built to between **+1000 and +10000** |
| `/research league:NFL parlay:Lotto` | A 4–10 leg parlay built to between **+3000 and +20000** |
| `/research league:NFL team:Chiefs parlay:Safe` | A same-game parlay from the Chiefs' next game |

`/record` shows how the leans and parlays have done.

A team's report labels every number by source:

- **Market (DraftKings via ESPN):** spread, total and moneyline, the implied chance of each result with the
  bookmaker's margin removed, and how the line has moved since it opened.
- **ESPN Matchup Predictor** (where ESPN publishes one, e.g. NFL and MLB) next to the market's number.
- **Last 5 games** with points scored and allowed, records against the spread, and **injuries** (QBs first).
- **Leans**, only when the data disagrees with the line: ESPN's model at least 5 points above the market's no-vig
  chance, or a projected total (from recent scoring) clearly off the over/under. Each lean lists the numbers behind
  it, a Low/Medium/High label, and ⚠️ cautions when the data may be misleading: a starting QB out, a big line move
  against the lean since the open, or a gap so large it usually means the model is missing news. Leans with
  cautions are always Low; totals from recent form top out at Medium.

Every pre-game lean is saved and graded at the final (win/loss/push, units at the recorded price), and `/record`
breaks the results down by market and by confidence. Break-even at standard -110 prices is about 52.4%, so judge the
leans by this record, not by how convincing they sound. It's research, not advice, and it can't guarantee winners.

### Player trends and parlays (Linemate-style)

A team's report also shows each key player's **most likely line** for every stat in the team's next
game, with how often it hit: last 10 games, this season, last season and against this opponent, from ESPN's game
logs, e.g. *~92% Josh Downs Over 1.5 Receptions · L10 10/10 · 2026 3/3 · 2025 15/16*. Key players come from ESPN's
team leaders; players listed Out, Doubtful or on IR are skipped.

- NFL: passing yards and TDs, rushing yards, receptions, receiving yards, anytime TD
- NBA: points, rebounds, assists, 3-pointers, points + rebounds + assists
- NHL: shots on goal, points, goals, assists
- MLB (batters): hits, total bases, runs, RBIs, home runs
- Soccer: shots, shots on target, anytime goal, to assist, goal or assist, fouls committed
- Cricket: runs, fours, sixes (batters); wickets (bowlers)

Cricket has no player game logs on ESPN, so its history is rebuilt from full scorecards (taken from the
ball-by-ball commentary, since ESPN's match summary only carries the latest innings): every IPL match of the current
season, and for internationals, earlier matches in the current series plus every match the bot records as it finishes.
International history therefore grows over time; players need 3+ matches before trends appear.

The **~%** is the hit rate adjusted for sample size (10/10 becomes about 92%, so nothing is ever "certain"),
weighted toward the last 10 games. A line needs about 75% (and 7 of the last 10) to be shown. MLB hitting is far less
consistent, so its bar is about 60%; its estimates are shown either way.

**Parlays are built to a payout, not a number of legs.** Legs are added, most likely first, until the estimated
odds land in range:

- **Safe (around +100):** the most likely lines until the parlay is close to even money (+100 means about a 50%
  chance), usually 2–4 legs.
- **Big payout (+1000 to +10000):** the higher, better-paying lines (each stat's near-certain line is skipped), usually
  6–12 legs.
- **Lotto (4–10 legs, +3000 to +20000):** the longest shots that still have a track record: high lines, plus
  underdog moneylines the market gives at least a 25% chance (not soccer, where draws are possible).

Legs are spread out (at most two per game, one per player; legs in the same game move together) unless you pick a
team, which builds a same-game parlay. Clear moneyline favorites can be legs too. Each leg shows its evidence, and
the parlay shows its **estimated odds** from those hit rates, with a plain slip to copy or screenshot for an odds bot.
The estimate is optimistic (it treats legs as independent) and your book's real price will differ, so check it.
If there aren't enough games or strong legs to reach the range, it says how close it got. Parlays look up to three
days ahead when there are no games left today.

**Every parlay is graded.** It's saved with the channel it was built in; after the games, each leg is checked against
the player's actual stats (game log, or the cricket scorecard) or the final score, players who didn't play are voided as
books do, and the result is posted back, e.g. *✅ Aaron Rodgers Over 199.5 Passing Yards · 299 · predicted ~79%*.
`/record` adds parlay and leg results per sport with **hit rate vs predicted**, the honest test of whether the
estimates can be trusted.

These are historical frequencies, not odds, and books price these trends in; check prices before betting.

### Cricket ball by ball

`/follow` → a cricket league → `ball_by_ball: True` posts every delivery from ESPNcricinfo's commentary, e.g.

```
🏏 India v West Indies
`2.3` Seales to Shubman Gill, 🔴 OUT! · India 3/1
> Shubman Gill c †Hope b Seales 1 (6b 0x4 0x6)
`2.4` Seales to Kohli, 4️⃣ FOUR! · India 7/1
`2.6` Seales to Kohli, 1 run · India 8/1
End of over 3: 6 runs
```

Balls bowled between two checks are combined into one message. That's still a message every ball or two, so a
dedicated channel works best. Following mid-match starts from the current ball. A restart (e.g. an update) carries on
from the last ball posted, with no repeats or gaps; after a long outage it skips ahead to the latest ball rather than
posting overs of backlog. Ball-by-ball channels still get the match start, innings break and result, but not the
separate wicket and every-5/10-overs posts.

## Setup

1. Create an application at https://discord.com/developers/applications, add a **Bot**, and copy its token.
2. Invite the bot. Under **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes and these
   permissions, then open the URL it generates:
   - `Send Messages`, `Embed Links`: required
   - `Create Public Threads`, `Send Messages in Threads`: for `/threads`
   - `Manage Messages`: to pin the `/scoreboard` message

   If the bot is already in your server, add the extra permissions to its role in **Server Settings → Roles**
   instead. Without them, `/threads` falls back to posting in the channel and the scoreboard just isn't pinned.
3. Install and run:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   export DISCORD_TOKEN=your-token-here
   python -m sportsbot
   ```

You don't need privileged gateway intents.

### Free 24/7 hosting (Oracle Cloud Always Free)

On any Ubuntu 22.04+ server, including Oracle Cloud's free VM, one command installs the bot as a service
that restarts automatically after crashes and reboots:

```bash
curl -fsSL https://raw.githubusercontent.com/steelballsdragon/Super/main/deploy/install.sh | sudo bash
```

It asks for your bot token once and stores it in `/etc/scorebot.env`, readable only by root. The bot runs as its own
`scorebot` user, which can read but not change the code in `/opt/scorebot` (root runs the update scripts there), and
keeps its data in `/var/lib/scorebot`.
The server checks GitHub every 5 minutes and installs new code automatically. To update right away, run `/update` in
Discord (server admins only) or run the install command again.
View the logs with `sudo journalctl -u scorebot -f`.

**No terminal? (e.g. setting up from a phone)** When creating the server, paste this as its startup script
(on Oracle: *Create instance → Show advanced options → Management → Initialization script → Paste cloud-init script*).
It installs the bot on first boot, with no typing in a terminal:

```bash
#!/bin/bash
export DISCORD_TOKEN='paste-your-token-here'
curl -fsSL https://raw.githubusercontent.com/steelballsdragon/Super/main/deploy/install.sh | bash
```

Note: the startup script, including the token, is saved in your server's settings. Anyone who can open
those settings in your Oracle account can read the token.

### Hosting 24/7 on Railway

The bot has to stay running, so for round-the-clock updates host it in the cloud.
For example, on [Railway](https://railway.app), which you can set up from a phone browser:

1. **New Project → Deploy from GitHub repo**, then pick this repository and branch.
2. In the service's **Variables** tab, add `DISCORD_TOKEN`.
3. The included `Procfile` starts the bot with `python -m sportsbot`.

Subscriptions are saved in `subscriptions.json`. On hosts where the disk is wiped on every redeploy,
attach a volume and set `DATA_FILE` to a path on it (e.g. `/data/subscriptions.json`). Otherwise, run `/follow` again after a redeploy.

### Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | (required) | Bot token |
| `POLL_INTERVAL` | `10` | Seconds between score checks (minimum 5). ESPN refreshes about every 5–8 seconds. |
| `DATA_FILE` | `subscriptions.json` | Where channel subscriptions are saved. Channel settings (`settings.json`), bot state (`state.json`), ball-by-ball positions (`balls.json`) and recorded cricket scorecards (`cricket.json`) are kept next to it. |
| `DEV_GUILD_ID` | (none) | Sync slash commands to one server instantly. Global sync can take up to an hour to appear. |

By default, only members with **Manage Channels** can use `/follow` and `/unfollow`. Server admins can change this under Server Settings → Integrations.

## How it works

Every `POLL_INTERVAL` seconds, the bot fetches the scoreboard for each league that some channel follows. It compares that scoreboard with the previous one and posts whatever changed. It only polls leagues that are followed. The first fetch after startup is recorded without posting anything, so restarting the bot doesn't repost old results. When ESPN is still showing an earlier day (it can lag well into a game day), today's games are fetched too, so no game's start is missed.

It's built to keep running unattended:

- **One failure never stops the updates.** Each league and each step (scoreboards, grading, reminders, daily
  schedules) is isolated: an error is logged and that piece is retried on the next check, and the update loop restarts
  itself if it ever stops. ESPN rate limits and server errors are retried with backoff.
- **Every post fits Discord's limits.** Long embeds are trimmed at a line break (marked "…") instead of being
  rejected, and any command that fails still replies instead of leaving "The application did not respond".
- **Bets always settle.** Leans and parlay legs are graded even if the bot was offline at the final; postponed,
  cancelled or abandoned games are voided, and anything still unsettled after a week is voided.
- **Small and steady on a 1 GB server.** Research caches only the stats it uses (about 150 MB of memory in a full
  test with all 15 leagues followed), old entries in the saved state are pruned hourly, and saved files are written
  atomically and flushed to disk. A damaged file is set aside (`*.damaged-<time>`) so the bot still starts.

## Tests

```bash
pip install pytest
python -m pytest
```
