# Live Scores Discord Bot

A Discord bot that posts live **NFL, NBA, MLB, NHL, soccer and cricket** updates to your channels, using ESPN's free public data (you don't need an API key).

## Features

- `/scores <league> [team]`: shows the current scoreboard (live, upcoming and finished games).
- `/follow <league> [team]`: posts live updates in this channel for a whole league or for one team.
- `/unfollow <league> [team]`: stops those updates.
- `/following`: lists what this channel follows.

Each sport posts the moments that matter for it:

| Sport | Live updates |
|---|---|
| ⚽ **Soccer** | Kick-off, every goal (scorer, minute and assist, marked as a penalty or own goal), half-time, full-time, and VAR score corrections |
| 🏈 **NFL** | Game start, every scoring play with the players, yards and extra point or two-point try (e.g. *Roman Wilson 12 Yd pass from Aaron Rodgers (Chris Boswell Kick)*), half-time, and the final with passing, rushing and receiving leaders |
| ⚾ **MLB** | First pitch, every run with the play (e.g. *Albies homered to right center (419 feet), Riley scored.*), and the final with each team's top performer |
| 🏒 **NHL** | Puck drop, every goal with the scorer, shot type and assists (on their own line; marked as power-play, shorthanded or empty-net), the score at the end of each period, and the final with each team's top scorer |
| 🏀 **NBA** | Tip-off, the score at the end of each quarter, half-time and the final with each team's top performer. It doesn't post every basket. |
| 🏏 **Cricket** | Match start with the toss, every wicket, the innings break with the target, and the result (e.g. *RCB won by 5 wkts (12b rem)*). It doesn't post every run. |

Supported leagues: NFL, NBA, MLB, NHL, Premier League, La Liga, Serie A, Bundesliga, Ligue 1, MLS, Champions League,
Europa League, FIFA World Cup, IPL, and international cricket (every current Test, ODI and T20I, men's and women's).
To add more, edit `sportsbot/leagues.py` with any ESPN path, for example `soccer/ned.1`.

## Setup

1. Create an application at https://discord.com/developers/applications, add a **Bot**, and copy its token.
2. Invite the bot. Under **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes and the `Send Messages` and `Embed Links` permissions, then open the URL it generates.
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

It asks for your bot token once and stores it in `/etc/scorebot.env`, readable only by root.
The server checks GitHub every hour and installs new code automatically. Run the same command again to update right away.
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
| `POLL_INTERVAL` | `30` | Seconds between score checks |
| `DATA_FILE` | `subscriptions.json` | Where channel subscriptions are saved |
| `DEV_GUILD_ID` | (none) | Sync slash commands to one server instantly. Global sync can take up to an hour to appear. |

By default, only members with **Manage Channels** can use `/follow` and `/unfollow`. Server admins can change this under Server Settings → Integrations.

## How it works

Every `POLL_INTERVAL` seconds, the bot fetches the scoreboard for each league that some channel follows. It compares that scoreboard with the previous one and posts whatever changed. It only polls leagues that are followed. The first fetch after startup is recorded without posting anything, so restarting the bot doesn't repost old results.

## Tests

```bash
pip install pytest
python -m pytest
```
