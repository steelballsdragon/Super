# Live Scores Discord Bot

A Discord bot that posts live **NFL** and **soccer** score updates to your channels, using ESPN's free public scoreboard data (you don't need an API key).

## Features

- `/scores <league> [team]`: shows the current scoreboard (live, upcoming and finished games).
- `/follow <league> [team]`: posts live updates in this channel for a whole league or for one team.
- `/unfollow <league> [team]`: stops those updates.
- `/following`: lists what this channel follows.

Live updates are posted as embeds for:
- **Kick-off / game start**
- **Goals / scores.** Soccer goals include the scorer and minute, and are tagged as a penalty or own goal where that applies. NFL scores include the scoring play.
- **Half-time** and **Full-time / Final**, with the result
- **Score corrections**, such as a goal overturned by VAR

Supported leagues: NFL, Premier League, La Liga, Serie A, Bundesliga, Ligue 1, MLS, Champions League, Europa League and the FIFA World Cup. To add more, edit `sportsbot/leagues.py` with any ESPN path, for example `soccer/ned.1`.

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
Run the same command again to update to the latest code. View the logs with `sudo journalctl -u scorebot -f`.

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
