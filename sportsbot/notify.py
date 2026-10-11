"""🔔 Game notifications: pick the games you care about and get told about them wherever you are.

A panel in the notifications channel lists the soonest live and upcoming games in up to four menus (soccer, NFL, the
other US sports, cricket). Picking games there, or in your own /notify picker, follows them: you're sent the kickoff,
every score, halftime and the final (wickets in cricket, quarter scores in the NBA), by DM unless you switch to pings
in the notifications channel. When a DM can't be delivered (closed DMs), it becomes a ping in that channel instead:
one message mentioning everyone it's for. Games drop off by themselves an hour after the final, or six hours after
the start if the final never came. Under the menus, buttons list your games (and drop some), clear them all, switch
between DMs and pings, and give or take the "Red Alert" role, which is pinged when red alerts go out.

Every menu and button carries what it needs in its id, so they keep working after the bot restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import discord

from .espn import EASTERN, Game, start_time
from .leagues import LEAGUES
from .limits import MESSAGE, clip, fit_embed
from .storage import read_json, write_json
from .tracker import CALLED_OFF, FINAL, HALFTIME, INNINGS, KICKOFF, PERIOD, SCORE, WICKET, Update, called_off

log = logging.getLogger(__name__)

RED_ALERT_ROLE = "Red Alert"
KEEP_AFTER_START = 6 * 3600  # forget a game this long after its start, if its final never came
KEEP_AFTER_FINAL = 3600
KEEP_WHILE_LIVE = 6 * 3600  # each update from a followed game keeps it this much longer (Tests, ODIs, extra time)
EXTEND_STEP = 600  # ...saved only when that moves it on by 10+ minutes, so a busy game doesn't rewrite the file
STALE_START = 3 * 3600  # a game still "upcoming" this long after its start was called off without notice
MAX_OPTIONS = 25  # Discord: options per menu
MAX_LABEL = 100  # Discord: characters in an option's label, value and description
MAX_MENUS = 4  # Discord allows 5 rows; the buttons take the last
GAMES_TTL = 60  # seconds the games behind the menus are reused
FETCH_TIMEOUT = 8.0
DM_RETRY_SECONDS = 3600  # after a DM bounces, ping that person in the channel for an hour before trying DMs again
DM_AT_ONCE = 5  # DMs sent at the same time (Discord rate-limits opening DMs)

# What followers hear about. Basketball has no per-score updates (it's posted by quarter) and cricket's are wickets.
NOTIFY_KINDS = frozenset({KICKOFF, SCORE, HALFTIME, FINAL, CALLED_OFF, WICKET})
SPORT_KINDS = {"basketball": frozenset({PERIOD}), "cricket": frozenset({INNINGS})}
ENDED = (FINAL, CALLED_OFF)


def kinds_for(sport: str) -> frozenset[str]:
    return NOTIFY_KINDS | SPORT_KINDS.get(sport, frozenset())


@dataclass(frozen=True)
class Group:
    """One menu on the panel."""

    key: str
    name: str
    sports: tuple[str, ...]


GROUPS = (
    Group("soccer", "⚽ Soccer", ("soccer",)),
    Group("nfl", "🏈 NFL", ("football",)),
    Group("us", "🏀 🏒 ⚾ US sports", ("basketball", "hockey", "baseball")),
    Group("cricket", "🏏 Cricket", ("cricket",)),
)
GROUP_BY_KEY = {g.key: g for g in GROUPS}


def group_of(league_key: str) -> Group | None:
    league = LEAGUES.get(league_key)
    return next((g for g in GROUPS if league and league.sport in g.sports), None)


def game_key(league_key: str, game_id: str) -> str:
    return f"{league_key}:{game_id}"


def split_key(key: str) -> tuple[str, str]:
    league, _, game_id = key.partition(":")
    return league, game_id


def _ts(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Watch:
    """A game someone follows."""

    league: str
    game_id: str
    label: str  # e.g. "⚽ Flamengo v Fluminense"
    start: str = ""  # ISO, as ESPN gives it
    until: float = 0.0  # when to forget it; 0 means work it out from the start

    @property
    def key(self) -> str:
        return game_key(self.league, self.game_id)

    @property
    def start_ts(self) -> float | None:
        return _ts(self.start)


def matchup(game: Game) -> str:
    """ "Flamengo v Fluminense" (home first) in soccer and cricket, "Bills @ Chiefs" in US sports."""
    first, second = game.teams
    sep = " v " if game.league.sport in ("soccer", "cricket") else " @ "
    return f"{first.name}{sep}{second.name}"


def watch_of(game: Game) -> Watch:
    return Watch(game.league_key, game.id, f"{game.league.emoji} {matchup(game)}", game.start)


def when_text(ts: float | None, now: float) -> str:
    """ "Sun 4:00 PM ET", with the date when it's a week or more away."""
    if ts is None:
        return ""
    dt = datetime.fromtimestamp(ts, EASTERN)
    clock = f"{dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'} ET"
    return f"{dt:%a %b} {dt.day} {clock}" if ts - now > 6 * 86400 else f"{dt:%a} {clock}"


class NotifyStore:
    """Who follows which games, how they want to hear, and where the panels are, saved in one JSON file.

    {"users": {uid: {"dm": bool, "games": {key: {"league", "game_id", "label", "start", "until"}}, "channel": id}},
     "panels": {channel_id: message_id}}; "channel" is the panel channel the person last used, for their pings.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        raw = read_json(self.path, {})
        raw = raw if isinstance(raw, dict) else {}
        self._users: dict[str, dict] = {uid: u for uid, u in (raw.get("users") or {}).items() if isinstance(u, dict)}
        self._panels: dict[str, int] = {cid: int(mid) for cid, mid in (raw.get("panels") or {}).items()}

    def _save(self) -> None:
        write_json(self.path, {"users": self._users, "panels": self._panels})

    def _user(self, uid: int) -> dict:
        return self._users.setdefault(str(uid), {"dm": True, "games": {}})

    def _games(self, uid: int) -> dict[str, dict]:
        return (self._users.get(str(uid)) or {}).get("games") or {}

    # ----- games -----

    def add(self, uid: int, games: Iterable[Watch | Game], now: float | None = None) -> list[str]:
        """Follows these games (Watch or Game); returns the keys that are new."""
        now = time.time() if now is None else now
        mine = self._user(uid).setdefault("games", {})
        added = []
        for g in games:
            w = g if isinstance(g, Watch) else watch_of(g)
            if w.key in mine:
                continue
            until = w.until or (w.start_ts or now) + KEEP_AFTER_START
            mine[w.key] = {"league": w.league, "game_id": w.game_id, "label": w.label, "start": w.start,
                           "until": until}
            added.append(w.key)
        if added:
            self._save()
        return added

    def remove(self, uid: int, keys: Iterable[str]) -> int:
        mine = self._games(uid)
        gone = [k for k in set(keys) if k in mine]
        for k in gone:
            del mine[k]
        if gone:
            self._tidy(str(uid))
            self._save()
        return len(gone)

    def clear(self, uid: int) -> int:
        return self.remove(uid, list(self._games(uid)))

    def games_of(self, uid: int) -> list[Watch]:
        """The games this person follows, soonest first."""
        out = [Watch(e["league"], e["game_id"], e.get("label") or k, e.get("start") or "", e.get("until") or 0.0)
               for k, e in self._games(uid).items()]
        return sorted(out, key=lambda w: (w.start_ts or float("inf"), w.label))

    def followers(self, league_key: str, game_id: str) -> list[tuple[int, bool]]:
        """(user id, wants a DM) for everyone following this game."""
        key = game_key(league_key, game_id)
        return sorted((int(uid), bool(u.get("dm", True))) for uid, u in self._users.items()
                      if key in (u.get("games") or {}))

    def leagues(self) -> set[str]:
        """Every league with a followed game (the bot keeps checking them even if no channel follows them)."""
        return {split_key(k)[0] for u in self._users.values() for k in (u.get("games") or {})}

    def counts(self) -> tuple[int, int]:
        """(people following something, distinct games followed)."""
        people = [u for u in self._users.values() if u.get("games")]
        return len(people), len({k for u in people for k in u["games"]})

    def _set_until(self, league_key: str, game_id: str, until: float, only_later: bool) -> int:
        key, changed = game_key(league_key, game_id), 0
        for u in self._users.values():
            entry = (u.get("games") or {}).get(key)
            if entry is None or (only_later and until < entry.get("until", 0) + EXTEND_STEP):
                continue
            entry["until"], changed = until, changed + 1
        if changed:
            self._save()
        return changed

    def finished(self, league_key: str, game_id: str, now: float | None = None) -> int:
        """The game is over: everyone stops following it an hour from now."""
        now = time.time() if now is None else now
        return self._set_until(league_key, game_id, now + KEEP_AFTER_FINAL, only_later=False)

    def extend(self, league_key: str, game_id: str, until: float) -> int:
        """Keeps a game that's still going (a Test match, extra time) at least until then."""
        return self._set_until(league_key, game_id, until, only_later=True)

    def prune(self, now: float | None = None) -> int:
        """Forgets games whose time is up; returns how many follows went."""
        now = time.time() if now is None else now
        dropped = 0
        for uid in list(self._users):
            games = self._users[uid].get("games") or {}
            for key in [k for k, e in games.items() if e.get("until", 0) <= now]:
                del games[key]
                dropped += 1
            self._tidy(uid)
        if dropped:
            self._save()
        return dropped

    def _tidy(self, uid: str) -> None:
        """Someone with no games and the default settings needn't be kept."""
        u = self._users.get(uid)
        if u is not None and not u.get("games") and u.get("dm", True):
            del self._users[uid]

    # ----- how each person hears -----

    def set_dm(self, uid: int, dm: bool) -> None:
        self._user(uid)["dm"] = bool(dm)
        self._tidy(str(uid))
        self._save()

    def dm_of(self, uid: int) -> bool:
        return bool((self._users.get(str(uid)) or {}).get("dm", True))

    def set_channel(self, uid: int, channel_id: int) -> None:
        """The panel channel this person uses: their pings go there."""
        u = self._users.get(str(uid))
        if u is not None and u.get("channel") != channel_id:
            u["channel"] = channel_id
            self._save()

    def channel_of(self, uid: int) -> int | None:
        return (self._users.get(str(uid)) or {}).get("channel")

    # ----- panels -----

    def panels(self) -> dict[int, int]:
        """{channel id: panel message id}, oldest first."""
        return {int(cid): mid for cid, mid in self._panels.items()}

    def set_panel(self, channel_id: int, message_id: int) -> None:
        self._panels[str(channel_id)] = int(message_id)
        self._save()

    def drop_panel(self, channel_id: int) -> None:
        if self._panels.pop(str(channel_id), None) is not None:
            self._save()


# ----- the menus -----

def _order(game: Game) -> tuple:
    """Live games first, then the soonest."""
    st = start_time(game)
    return (game.state != "in", st.timestamp() if st else float("inf"), game.league_key, game.id)


def _showable(game: Game, now: float) -> bool:
    if game.league_key not in LEAGUES or game.state == "post" or called_off(game):
        return False
    st = start_time(game)
    return game.state == "in" or st is None or st.timestamp() >= now - STALE_START


def menus_for(games: Iterable[Game], now: float, keep: Iterable[str] = ()) -> list[tuple[Group, list[Game]]]:
    """The games for each menu that has any: the soonest live and upcoming, at most 25, always including the games
    in keep (a picker shows what you follow)."""
    keep, seen, by_group = set(keep), set(), {}
    for g in games:
        group, key = group_of(g.league_key), game_key(g.league_key, g.id)
        if group is None or key in seen or (group.key, g.id) in seen or not _showable(g, now):
            continue
        seen |= {key, (group.key, g.id)}  # one game in two feeds is listed once
        by_group.setdefault(group.key, []).append(g)
    out = []
    for group in GROUPS:
        found = sorted(by_group.get(group.key, []), key=_order)
        if found:
            kept = [g for g in found if game_key(g.league_key, g.id) in keep]
            rest = [g for g in found if game_key(g.league_key, g.id) not in keep]
            out.append((group, sorted((kept + rest)[:MAX_OPTIONS], key=_order)))
    return out[:MAX_MENUS]


def option_label(game: Game, now: float) -> str:
    """ "⚽ Flamengo v Fluminense · Sun 4:00 PM ET" (or "· 🔴 Live"), shortened to abbreviations if too long."""
    st = start_time(game)
    when = "🔴 Live" if game.state == "in" else when_text(st.timestamp() if st else None, now)
    tail = f" · {when}" if when else ""
    label = f"{game.league.emoji} {matchup(game)}{tail}"
    if len(label) > MAX_LABEL:
        first, second = game.teams
        sep = " v " if game.league.sport in ("soccer", "cricket") else " @ "
        label = f"{game.league.emoji} {first.abbrev}{sep}{second.abbrev}{tail}"
    return clip(label, MAX_LABEL)


def _option(game: Game, now: float, default: bool = False) -> discord.SelectOption:
    return discord.SelectOption(label=option_label(game, now), value=game_key(game.league_key, game.id),
                                description=clip(game.league.name, MAX_LABEL), default=default)


def _copy_option(o, default: bool | None = None) -> discord.SelectOption:
    return discord.SelectOption(label=o.label, value=o.value, description=o.description, emoji=o.emoji,
                                default=o.default if default is None else default)


def _desk(interaction: discord.Interaction) -> "NotifyDesk | None":
    return getattr(interaction.client, "notify", None)


async def _run(interaction: discord.Interaction, action: Callable[["NotifyDesk"], Awaitable[None]]) -> None:
    """Runs a menu or button's action, telling the person if notifications are off or it went wrong."""
    desk = _desk(interaction)
    try:
        if desk is None:
            await interaction.response.send_message("Game notifications aren't switched on here.", ephemeral=True)
            return
        await action(desk)
    except Exception:
        log.exception("A notifications menu or button failed")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("That didn't work, try again in a moment.", ephemeral=True)
            else:
                await interaction.response.send_message("That didn't work, try again in a moment.", ephemeral=True)
        except discord.HTTPException:
            pass


class PickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"notify:pick:(?P<group>[a-z]+)"):
    """A panel menu: picking games follows them, then the menu resets for the next person."""

    def __init__(self, group: str, options: list[discord.SelectOption], placeholder: str | None = None,
                 row: int | None = None):
        name = GROUP_BY_KEY[group].name if group in GROUP_BY_KEY else "Games"
        super().__init__(discord.ui.Select(custom_id=f"notify:pick:{group}", options=options, min_values=1,
                                           max_values=max(1, len(options)),
                                           placeholder=placeholder or f"{name}: pick games to follow"), row=row)
        self.group = group

    @classmethod
    def rebuild(cls, component, match, row=None, chosen=None):
        return cls(match["group"], [_copy_option(o, False) for o in component.options], component.placeholder, row)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls.rebuild(item, match)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.picked(interaction, list(self.item.values), self.item.options))


class SetSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"notify:set:(?P<group>[a-z]+)"):
    """A /notify picker menu: the games ticked in it are exactly the ones followed from it."""

    def __init__(self, group: str, options: list[discord.SelectOption], placeholder: str | None = None,
                 row: int | None = None):
        name = GROUP_BY_KEY[group].name if group in GROUP_BY_KEY else "Games"
        super().__init__(discord.ui.Select(custom_id=f"notify:set:{group}", options=options, min_values=0,
                                           max_values=max(1, len(options)),
                                           placeholder=placeholder or f"{name}: tick games to follow"), row=row)
        self.group = group

    @classmethod
    def rebuild(cls, component, match, row=None, chosen=None):
        options = [_copy_option(o, None if chosen is None else o.value in chosen) for o in component.options]
        return cls(match["group"], options, component.placeholder, row)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls.rebuild(item, match)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.set_group(interaction, list(self.item.values), self.item.options))


class UnpickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"notify:unpick"):
    """Under "My games": the games picked here are dropped."""

    def __init__(self, options: list[discord.SelectOption], row: int | None = None):
        super().__init__(discord.ui.Select(custom_id="notify:unpick", options=options, min_values=1,
                                           max_values=max(1, len(options)),
                                           placeholder="Pick games to stop notifications for"), row=row)

    @classmethod
    def rebuild(cls, component, match, row=None, chosen=None):
        return cls([_copy_option(o, False) for o in component.options], row)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls.rebuild(item, match)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.unpicked(interaction, list(self.item.values)))


class _Button:
    """The fixed buttons: their id says everything, so they're rebuilt the same way every time."""

    @classmethod
    def rebuild(cls, component, match, row=None, chosen=None):
        return cls(row=row)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()


def _button(label: str, emoji: str, custom_id: str, style=discord.ButtonStyle.secondary) -> discord.ui.Button:
    return discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=custom_id)


class MineButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=r"notify:mine"):
    def __init__(self, row: int | None = None):
        super().__init__(_button("My games", "📋", "notify:mine"), row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.show_mine(interaction))


class ClearButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=r"notify:clear"):
    def __init__(self, row: int | None = None):
        super().__init__(_button("Clear all", "🗑️", "notify:clear"), row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.clear(interaction))


class ModeButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=r"notify:mode"):
    def __init__(self, row: int | None = None):
        super().__init__(_button("DM me / Ping me here", "🔁", "notify:mode"), row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.toggle_mode(interaction))


class RoleButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=r"notify:role"):
    def __init__(self, row: int | None = None):
        super().__init__(_button("Red alert pings", "🚨", "notify:role", discord.ButtonStyle.danger), row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.toggle_role(interaction))


class StopButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"notify:stop:(?P<league>[a-z0-9_]+):(?P<gid>[A-Za-z0-9_.-]+)"):
    """Under a notification: stop notifications for this game (for whoever taps it)."""

    def __init__(self, league_key: str, game_id: str, row: int | None = None):
        super().__init__(discord.ui.Button(label="Stop this game", emoji="🔕", style=discord.ButtonStyle.secondary,
                                           custom_id=f"notify:stop:{league_key}:{game_id}"), row=row)
        self.league_key, self.game_id = league_key, game_id

    @classmethod
    def rebuild(cls, component, match, row=None, chosen=None):
        return cls(match["league"], match["gid"], row)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["league"], match["gid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run(interaction, lambda desk: desk.stop(interaction, self.league_key, self.game_id))


ITEMS = (PickSelect, SetSelect, UnpickSelect, MineButton, ClearButton, ModeButton, RoleButton, StopButton)


def _buttons(row: int, role: bool = True) -> list[discord.ui.Item]:
    items = [MineButton(row), ClearButton(row), ModeButton(row)]
    return items + [RoleButton(row)] if role else items


def view_from_message(message, chosen: set[str] | None = None) -> discord.ui.View:
    """A message's menus and buttons as fresh items. Sending them back resets a menu after a pick, and in a /notify
    picker ticks exactly the games in chosen."""
    view = discord.ui.View(timeout=None)
    for row, component in enumerate(getattr(message, "components", None) or []):
        for child in getattr(component, "children", None) or [component]:
            custom_id = getattr(child, "custom_id", None) or ""
            for cls in ITEMS:
                if match := cls.__discord_ui_compiled_template__.fullmatch(custom_id):
                    view.add_item(cls.rebuild(child, match, min(row, 4), chosen))
                    break
    return view


HOW_TO = ("**📋 My games** lists what you follow, and lets you drop some\n"
          "**🗑️ Clear all** stops every game\n"
          "**🔁 DM me / Ping me here** switches between DMs and pings in this channel\n"
          "**🚨 Red alert pings** gives you the Red Alert role (or takes it away), pinged when red alerts go out\n"
          "`/notify` opens your own picker anywhere. Games drop off an hour after the final.")


def panel(games: Iterable[Game], now: float | None = None) -> tuple[discord.Embed, discord.ui.View]:
    """The notifications panel: what it does, a menu per group with games, and the buttons."""
    now = time.time() if now is None else now
    menus = menus_for(games, now)
    embed = discord.Embed(title="🔔 Game notifications", color=discord.Color.gold(), description=(
        "Pick games from the menus and I'll tell you about them: the **kickoff**, **every score**, **halftime** and "
        "the **final** (wickets in cricket, quarter scores in the NBA). By **DM**, unless you'd rather be pinged "
        "here. Pick as many as you like, from as many menus as you like."))
    embed.add_field(name="Manage your games", value=HOW_TO, inline=False)
    if menus:
        counts = " · ".join(f"{group.name.split(' ')[0]} {len(found)}" for group, found in menus)
        embed.set_footer(text=f"On the menus: {counts} · live games first, then the soonest · times are US Eastern")
    else:
        embed.add_field(name="No games right now", value="Nothing is live or coming up in the leagues I follow. "
                        "The menus come back as soon as there are games.", inline=False)
    view = discord.ui.View(timeout=None)
    for row, (group, found) in enumerate(menus):
        view.add_item(PickSelect(group.key, [_option(g, now) for g in found], row=row))
    for item in _buttons(len(menus)):
        view.add_item(item)
    return fit_embed(embed), view


def _signature(embed: discord.Embed, view: discord.ui.View) -> str:
    return json.dumps([embed.to_dict(), view.to_components()], sort_keys=True, default=str)


def _mentions(uids: list[int], content: str | None) -> list[str]:
    """The ping text: everyone's mention, then any message, split to fit Discord's 2000 characters (the message
    goes with the first part)."""
    content = clip(content, MESSAGE // 2) if content else ""
    room = MESSAGE - len(content) - 1
    chunks, line = [], "🔔"
    for uid in uids:
        mention = f" <@{uid}>"
        if len(line) + len(mention) > room:
            chunks.append(line)
            line, room = "🔔", MESSAGE
        line += mention
    chunks.append(line)
    if content:
        chunks[0] += f"\n{content}"
    return chunks


# ----- the Red Alert role -----

def _can_manage_roles(guild) -> bool:
    me = getattr(guild, "me", None)
    return bool(getattr(getattr(me, "guild_permissions", None), "manage_roles", False))


async def ensure_role(guild, name: str = RED_ALERT_ROLE) -> "discord.Role | None":
    """The server's role with this name, made (mentionable) if it's missing and I'm allowed to; else None."""
    if guild is None:
        return None
    role = discord.utils.get(guild.roles, name=name)
    if role is not None and not role.mentionable and _can_manage_roles(guild):
        try:  # made by hand: a role nobody can ping is no use for alerts
            await role.edit(mentionable=True, reason="ScoreBot: red alert pings")
        except discord.HTTPException:
            log.warning("Couldn't make the %s role mentionable", name, exc_info=True)
    if role is not None or not _can_manage_roles(guild):
        return role
    try:
        return await guild.create_role(name=name, mentionable=True, colour=discord.Colour.red(),
                                       reason="ScoreBot: red alert pings")
    except discord.HTTPException:
        log.warning("Couldn't make the %s role", name, exc_info=True)
        return None


def role_mention(guild, name: str = RED_ALERT_ROLE) -> str:
    """The role's mention for a post's text, or "" if the server doesn't have it."""
    role = discord.utils.get(guild.roles, name=name) if guild is not None else None
    return role.mention if role is not None else ""


async def toggle_role(guild, member, name: str = RED_ALERT_ROLE) -> str:
    """Gives the member the role, or takes it away if they have it. Returns what happened, for them."""
    role = await ensure_role(guild, name)
    if role is None:
        return (f"I can't make the **{name}** role: I need the **Manage Roles** permission. Ask an admin to give me "
                f"it (Server Settings → Roles), or to make a mentionable role called {name}.")
    if not _can_manage_roles(guild):
        return (f"I can't give out the **{name}** role: I need the **Manage Roles** permission "
                "(Server Settings → Roles).")
    had = any(r.id == role.id for r in getattr(member, "roles", None) or [])
    try:
        if had:
            await member.remove_roles(role, reason="Turned off red alert pings")
        else:
            await member.add_roles(role, reason="Turned on red alert pings")
    except discord.Forbidden:
        return (f"I couldn't change your roles: I need **Manage Roles**, and my own role has to sit above **{name}** "
                "in Server Settings → Roles. Ask an admin to move it up.")
    except discord.HTTPException:
        return "Discord didn't let me change your roles just now, try again shortly."
    if had:
        return f"🔕 Took away **{name}**: no more red alert pings."
    return f"🚨 You have the **{name}** role now: you'll be pinged when red alerts go out. Tap again to stop."


# ----- the desk -----

GamesSource = Callable[[], Awaitable[list[Game]]]


class NotifyDesk:
    """Everything about game notifications: the panels, /notify, the menus and buttons, and delivering updates."""

    def __init__(self, bot, path: str | Path, games_source: GamesSource):
        self.bot, self.store, self.games_source = bot, NotifyStore(path), games_source
        self._games: dict[str, Game] = {}  # the games behind the menus, by key
        self._fetched: float | None = None
        self._dm_bounced: dict[int, float] = {}  # user -> until when to ping them instead of trying a DM
        self._users: dict[int, Any] = {}  # users fetched for a DM (members aren't cached without that intent)
        self._shown: dict[int, str] = {}  # panel channel -> what its panel shows, to skip edits that change nothing

    def dynamic_items(self) -> tuple[type, ...]:
        return ITEMS

    async def games(self, max_age: float = GAMES_TTL) -> list[Game]:
        """The live and upcoming games for the menus, reused for a minute; the last ones if the source fails."""
        if self._fetched is None or time.monotonic() - self._fetched >= max_age:
            try:
                found = await asyncio.wait_for(self.games_source(), FETCH_TIMEOUT)
                self._games = {game_key(g.league_key, g.id): g for g in found}
                self._fetched = time.monotonic()
            except Exception:
                log.warning("Couldn't load the games for the notification menus", exc_info=True)
        return list(self._games.values())

    async def _watches(self, keys: list[str], options=()) -> list[Watch]:
        """What to store for these picks: from the games behind the menus, else the menu's own label."""
        if any(k not in self._games for k in keys):
            await self.games()
        labels = {o.value: o.label for o in options}
        out = []
        for key in keys:
            if (game := self._games.get(key)) is not None:
                out.append(watch_of(game))
            else:
                league, gid = split_key(key)
                out.append(Watch(league, gid, labels.get(key, key).split(" · ")[0]))
        return out

    # ----- where and how people hear -----

    def _ping_channel(self, uid: int) -> int | None:
        panels = self.store.panels()
        mine = self.store.channel_of(uid)
        return mine if mine in panels else next(iter(panels), None)

    def _note_channel(self, uid: int, channel_id: int | None) -> None:
        if channel_id in self.store.panels():
            self.store.set_channel(uid, channel_id)

    def _how(self, uid: int) -> str:
        if self.store.dm_of(uid):
            return "by **DM**"
        cid = self._ping_channel(uid)
        return f"with a ping in <#{cid}>" if cid else "with a ping in the notifications channel"

    # ----- the panel and the picker -----

    async def post_panel(self, channel_id: int) -> bool:
        """Posts the panel in this channel (once: an existing one is brought up to date instead)."""
        try:
            games = await self.games()
            embed, view = panel(games)
            if (mid := self.store.panels().get(channel_id)) is not None:
                if await self._edit_panel(channel_id, mid, embed, view):
                    return True
                if channel_id in self.store.panels():
                    return False  # it's there but Discord refused the edit: don't post a second one
                embed, view = panel(games)  # it was deleted: post again, with a view of its own
            message = await self.bot._send(channel_id, embed, view=view)
            if message is None:
                return False
            self.store.set_panel(channel_id, message.id)
            self._shown[channel_id] = _signature(embed, view)
            try:
                await message.pin()  # easy to find again under the pings
            except Exception:
                log.info("Couldn't pin the notifications panel in %s", channel_id)
            return True
        except Exception:
            log.exception("Couldn't post the notifications panel in %s", channel_id)
            return False

    async def _edit_panel(self, channel_id: int, message_id: int, embed, view) -> bool:
        """Edits a panel; False (and forgets it) if it was deleted, or if Discord refused."""
        try:
            channel = await self.bot._channel(channel_id)
            await channel.get_partial_message(message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("The notifications panel in %s is gone; forgetting it", channel_id)
            self.store.drop_panel(channel_id)
            self._shown.pop(channel_id, None)
            return False
        except discord.HTTPException:
            log.warning("Couldn't update the notifications panel in %s", channel_id, exc_info=True)
            return False
        self._shown[channel_id] = _signature(embed, view)
        return True

    async def refresh_panels(self) -> None:
        """Brings every panel's menus up to date (games that started, finished or were added), forgets games
        whose time is up, and drops panels whose message was deleted."""
        try:
            self.store.prune()
            if not self.store.panels():
                return
            games, now = await self.games(), time.time()
            shown = _signature(*panel(games, now))
            for cid, mid in self.store.panels().items():
                if self._shown.get(cid) != shown:
                    await self._edit_panel(cid, mid, *panel(games, now))  # each message gets its own view
        except Exception:
            log.exception("Couldn't refresh the notification panels")

    def picker(self, uid: int, games: Iterable[Game], now: float | None = None,
               role: bool = True) -> tuple[discord.Embed, discord.ui.View]:
        """Someone's own picker (/notify): the same menus, with what they follow ticked."""
        now = time.time() if now is None else now
        followed = {w.key for w in self.store.games_of(uid)}
        menus = menus_for(games, now, keep=followed)
        view = discord.ui.View(timeout=None)
        for row, (group, found) in enumerate(menus):
            options = [_option(g, now, game_key(g.league_key, g.id) in followed) for g in found]
            view.add_item(SetSelect(group.key, options, row=row))
        for item in _buttons(len(menus), role):
            view.add_item(item)
        return self.picker_embed(uid, now, bool(menus)), view

    def picker_embed(self, uid: int, now: float | None = None, has_games: bool = True) -> discord.Embed:
        now = time.time() if now is None else now
        mine = self.store.games_of(uid)
        lines = ["Tick the games you want and untick the ones you don't. I'll tell you about the kickoff, every "
                 f"score, halftime and the final, {self._how(uid)}."]
        if not has_games:
            lines.append("\nNothing is live or coming up right now: try again later.")
        embed = discord.Embed(title="🔔 Your game notifications", color=discord.Color.gold(),
                              description="\n".join(lines))
        if mine:
            embed.add_field(name=f"Following {len(mine)}", value="\n".join(self._line(w, now) for w in mine),
                            inline=False)
        embed.set_footer(text="Only you can see this · menu times are US Eastern")
        return fit_embed(embed)

    @staticmethod
    def _line(w: Watch, now: float) -> str:
        ts = w.start_ts
        if ts is None:
            return w.label
        return f"{w.label} · {'🔴 started' if ts <= now else 'starts'} <t:{int(ts)}:R>"

    def mine(self, uid: int, now: float | None = None) -> tuple[discord.Embed, discord.ui.View | None]:
        """ "My games": the list, and a menu to drop some."""
        now = time.time() if now is None else now
        watches = self.store.games_of(uid)
        embed = discord.Embed(title="📋 Your games", color=discord.Color.gold())
        if not watches:
            embed.description = ("You're not following any games. Pick some from the menus in the notifications "
                                 "channel, or with `/notify`.")
            return fit_embed(embed), None
        embed.description = (f"You'll hear about these {self._how(uid)}:\n"
                             + "\n".join(self._line(w, now) for w in watches))
        embed.set_footer(text="Pick games below to stop them")
        options = [discord.SelectOption(label=clip(w.label, MAX_LABEL), value=w.key,
                                        description=clip(when_text(w.start_ts, now) or None, MAX_LABEL))
                   for w in watches[:MAX_OPTIONS]]
        view = discord.ui.View(timeout=None)
        view.add_item(UnpickSelect(options))
        return fit_embed(embed), view

    # ----- what the menus and buttons do -----

    async def picked(self, interaction: discord.Interaction, keys: list[str], options=()) -> None:
        """A panel pick: follow those games, reset the menu, and say what's followed now."""
        await interaction.response.defer()
        uid = interaction.user.id
        watches = await self._watches(keys, options)
        added = set(self.store.add(uid, watches))
        self._note_channel(uid, interaction.channel_id)
        try:
            await interaction.edit_original_response(view=view_from_message(interaction.message))
        except discord.HTTPException:
            log.info("Couldn't reset the notifications menu", exc_info=True)
        new = [w.label for w in watches if w.key in added]
        old = [w.label for w in watches if w.key not in added]
        lines = []
        if new:
            lines.append(f"✅ Following {', '.join(new)}.")
        if old:
            lines.append(f"(Already following {', '.join(old)}.)" if new else f"You already follow {', '.join(old)}.")
        total = len(self.store.games_of(uid))
        lines.append(f"You follow {total} game{'s' if total != 1 else ''}; I'll tell you about the kickoff, every "
                     f"score, halftime and the final {self._how(uid)}. **📋 My games** to see or drop them.")
        await interaction.followup.send(clip("\n".join(lines), MESSAGE), ephemeral=True)

    async def set_group(self, interaction: discord.Interaction, keys: list[str], options=()) -> None:
        """A picker menu changed: what's ticked in it is followed, what's unticked isn't."""
        await interaction.response.defer()
        uid = interaction.user.id
        shown, chosen = {o.value for o in options}, set(keys)
        current = {w.key for w in self.store.games_of(uid)}
        self.store.remove(uid, (shown & current) - chosen)
        self.store.add(uid, await self._watches([k for k in keys if k not in current], options))
        self._note_channel(uid, interaction.channel_id)
        followed = {w.key for w in self.store.games_of(uid)}
        await interaction.edit_original_response(embed=self.picker_embed(uid),
                                                 view=view_from_message(interaction.message, followed))

    async def show_mine(self, interaction: discord.Interaction) -> None:
        embed, view = self.mine(interaction.user.id)
        await interaction.response.send_message(embed=embed, ephemeral=True, **({"view": view} if view else {}))

    async def unpicked(self, interaction: discord.Interaction, keys: list[str]) -> None:
        self.store.remove(interaction.user.id, keys)
        embed, view = self.mine(interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def clear(self, interaction: discord.Interaction) -> None:
        n = self.store.clear(interaction.user.id)
        text = (f"🗑️ Stopped all {n} game{'s' if n != 1 else ''}. Pick new ones any time." if n
                else "You weren't following any games.")
        await interaction.response.send_message(text, ephemeral=True)

    async def toggle_mode(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        dm = not self.store.dm_of(uid)
        self.store.set_dm(uid, dm)
        self._note_channel(uid, interaction.channel_id)
        self._dm_bounced.pop(uid, None)
        if dm:
            text = ("🔔 Notifications now come **by DM**. If your DMs are closed to me, I'll ping you in the "
                    "notifications channel instead. Tap again to be pinged there.")
        else:
            where = self._how(uid).removeprefix("with a ping ")
            text = f"📣 I'll ping you {where} instead of DMing you. Tap again for DMs."
        await interaction.response.send_message(text, ephemeral=True)

    async def toggle_role(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Red alert pings are a server role: use this in the server.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await toggle_role(interaction.guild, interaction.user), ephemeral=True)

    async def stop(self, interaction: discord.Interaction, league_key: str, game_id: str) -> None:
        key = game_key(league_key, game_id)
        label = next((w.label for w in self.store.games_of(interaction.user.id) if w.key == key), "this game")
        if self.store.remove(interaction.user.id, [key]):
            text = f"🔕 No more notifications for {label}."
        else:
            text = "You're not following this game (any more)."
        await interaction.response.send_message(text, ephemeral=True)

    async def open_picker(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, view = self.picker(interaction.user.id, await self.games(), role=interaction.guild is not None)
        self._note_channel(interaction.user.id, interaction.channel_id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    def register(self, tree: discord.app_commands.CommandTree) -> None:
        """Adds /notify: your own picker, only shown to you."""
        desk = self

        @tree.command(name="notify", description="Pick games to be told about: kickoff, every score, halftime, final")
        async def notify(interaction: discord.Interaction):
            try:
                await desk.open_picker(interaction)
            except Exception:
                log.exception("/notify failed")
                await interaction.followup.send("Couldn't load the games, try again shortly.", ephemeral=True)

    # ----- delivering -----

    def _with_stop(self, view, league_key: str, game_id: str) -> discord.ui.View:
        """The post's own buttons plus "Stop this game"."""
        out = discord.ui.View(timeout=None)
        for item in (view.children if view is not None else []):
            out.add_item(item)
        try:
            out.add_item(StopButton(league_key, game_id))
        except ValueError:  # no room left
            pass
        return out

    async def _dm(self, uid: int, embed, content, view, limit: asyncio.Semaphore):
        try:
            async with limit:
                user = self._users.get(uid) or self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                self._users[uid] = user
                return await user.send(content=content, embed=embed, **({"view": view} if view else {}))
        except discord.HTTPException as exc:
            log.info("Couldn't DM %s (%s); pinging them instead", uid, exc.status)
        except Exception:
            log.exception("Couldn't DM %s", uid)
        return None

    async def _deliver(self, followers: list[tuple[int, bool]], embed=None, content=None, view=None) -> list:
        now = time.monotonic()
        dms = [uid for uid, dm in followers if dm and self._dm_bounced.get(uid, 0) <= now]
        pings = [uid for uid, dm in followers if uid not in dms]
        posted, limit = [], asyncio.Semaphore(DM_AT_ONCE)
        sent = await asyncio.gather(*(self._dm(uid, embed, content, view, limit) for uid in dms))
        for uid, message in zip(dms, sent):
            if message is None:
                self._dm_bounced[uid] = now + DM_RETRY_SECONDS
                pings.append(uid)
            else:
                self._dm_bounced.pop(uid, None)
                posted.append((message.channel.id, message))
        by_channel: dict[int, list[int]] = {}
        for uid in sorted(pings):
            if (cid := self._ping_channel(uid)) is None:
                log.warning("No notifications channel to ping %s in", uid)
                continue
            by_channel.setdefault(cid, []).append(uid)
        for cid, people in by_channel.items():
            for i, text in enumerate(_mentions(people, content)):
                message = await self.bot._send(cid, embed if i == 0 else None, text, view if i == 0 else None)
                if message is not None and i == 0:  # the one with the update: the bot may edit it later
                    posted.append((cid, message))
        return posted

    async def notify(self, update: Update, embed, view=None) -> list[tuple[int, Any]]:
        """Tells everyone following the update's game, if it's the kind they hear about. Returns every
        (channel id, message) posted, DMs included, so the bot can edit them when ESPN corrects the play."""
        game = update.game
        try:
            if update.edit or update.drop or update.kind not in kinds_for(game.league.sport):
                return []
            followers = self.store.followers(game.league_key, game.id)
            posted = []
            if followers:
                posted = await self._deliver(followers, embed, None, self._with_stop(view, game.league_key, game.id))
            if update.kind in ENDED:
                self.store.finished(game.league_key, game.id)
            elif followers:
                self.store.extend(game.league_key, game.id, time.time() + KEEP_WHILE_LIVE)
            return posted
        except Exception:
            log.exception("Couldn't send notifications for %s %s", game.league_key, game.id)
            return []

    async def notify_game(self, league_key: str, game_id: str, embed=None, content: str | None = None) -> list:
        """Any other news about a followed game (e.g. confirmed lineups), delivered the same way."""
        try:
            followers = self.store.followers(league_key, game_id)
            if not followers or (embed is None and not content):
                return []
            return await self._deliver(followers, embed, content, self._with_stop(None, league_key, game_id))
        except Exception:
            log.exception("Couldn't send game news for %s %s", league_key, game_id)
            return []

    def status(self) -> str:
        """For /status: "3 people follow 5 games · panel in 1 channel"."""
        people, games = self.store.counts()
        panels = len(self.store.panels())
        text = (f"{people} {'person follows' if people == 1 else 'people follow'} "
                f"{games} game{'s' if games != 1 else ''}")
        return text + (f" · panel in {panels} channel{'s' if panels != 1 else ''}" if panels else " · no panel yet")
