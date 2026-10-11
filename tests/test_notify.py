"""🔔 Game notifications: who follows what (and for how long), the panel's menus within Discord's limits, every menu
and button rebuilt from its id after a restart, DMs falling back to one ping in the channel, the Red Alert role, and
panels that keep themselves up to date (and are forgotten once deleted)."""

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
from discord import app_commands

from sportsbot import notify as N
from sportsbot.boxscore import StatsButton
from sportsbot.espn import Game, Team, start_time
from sportsbot.tracker import CALLED_OFF, FINAL, INNINGS, KICKOFF, OVERS, PERIOD, SCORE, WICKET, Update

NOW = time.time()
PANEL = 500  # the notifications channel


def game(gid, league="epl", home="Arsenal", away="Chelsea", hours=2.0, state="pre", status="STATUS_SCHEDULED"):
    start = datetime.fromtimestamp(NOW + hours * 3600, timezone.utc)
    return Game(gid, league, Team("1", home, home[:3].upper(), 0), Team("2", away, away[:3].upper(), 0), state,
                status, "", f"{start:%Y-%m-%dT%H:%M:%SZ}")


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="nope"), "nope")


class Message:
    def __init__(self, mid, channel_id):
        self.id, self.channel, self.pinned = mid, SimpleNamespace(id=channel_id), False

    async def pin(self):
        self.pinned = True


class User:
    def __init__(self, uid, closed=False):
        self.id, self.closed, self.dms, self.tries = uid, closed, [], 0

    async def send(self, content=None, embed=None, view=None):
        self.tries += 1
        if self.closed:
            raise http_error(discord.Forbidden, 403)
        self.dms.append((content, embed, view))
        return Message(9000 + len(self.dms), 7000 + self.id)


class Channel:
    """A channel whose panel messages can be edited, unless they were deleted (or I may not)."""

    def __init__(self, alive=(), forbidden=False):
        self.alive, self.edits, self.forbidden = set(alive), [], forbidden

    def get_partial_message(self, mid):
        channel = self

        class Partial:
            async def edit(self, **kw):
                if channel.forbidden:
                    raise http_error(discord.Forbidden, 403)
                if mid not in channel.alive:
                    raise http_error(discord.NotFound, 404)
                channel.edits.append((mid, kw))
        return Partial()


class Bot:
    def __init__(self, users=()):
        self.users, self.sent, self.messages, self.channels = {u.id: u for u in users}, [], [], {}

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        raise http_error(discord.NotFound, 404)

    async def _send(self, channel_id, embed=None, content=None, view=None):
        self.sent.append(SimpleNamespace(channel=channel_id, embed=embed, content=content, view=view))
        self.messages.append(Message(100 + len(self.sent), channel_id))
        return self.messages[-1]

    async def _channel(self, cid):
        if cid not in self.channels:
            raise http_error(discord.NotFound, 404)
        return self.channels[cid]


def desk_with(tmp_path, games=(), users=(), fail=False):
    bot = Bot(users)

    async def source():
        if fail:
            raise RuntimeError("ESPN is down")
        return list(games)
    desk = N.NotifyDesk(bot, tmp_path / "notify.json", source)
    bot.notify = desk
    return desk, bot


class Inter:
    """A click or command: records every reply and edit."""

    def __init__(self, desk, user_id=7, channel_id=PANEL, message=None, guild=None, user=None):
        self.client = SimpleNamespace(notify=desk)
        self.user = user or SimpleNamespace(id=user_id)
        self.channel_id, self.message, self.guild = channel_id, message, guild
        self.replies, self.edits, self.deferred = [], [], []
        inter = self

        class Response:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **kw):
                inter.deferred.append(kw)
                self.done = True

            async def send_message(self, content=None, **kw):
                inter.replies.append((content, kw))
                self.done = True

            async def edit_message(self, **kw):
                inter.edits.append(kw)
                self.done = True

        async def followup(content=None, **kw):
            inter.replies.append((content, kw))
        self.response, self.followup = Response(), SimpleNamespace(send=followup)

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


def message_of(view):
    """The message as Discord hands it back: its components, from the view that was posted."""
    return SimpleNamespace(components=[discord.ActionRow(row) for row in view.to_components()])


async def click(inter, custom_id, values=None):
    """What discord.py does with a click, even after a restart: rebuild the item from its id, then run it."""
    [component] = [c for row in inter.message.components for c in row.children if c.custom_id == custom_id]
    base = (discord.ui.Select if isinstance(component, discord.SelectMenu) else discord.ui.Button).from_component(
        component)
    [cls] = [c for c in N.ITEMS if c.__discord_ui_compiled_template__.fullmatch(custom_id)]
    item = await cls.from_custom_id(inter, base, cls.__discord_ui_compiled_template__.fullmatch(custom_id))
    if values is not None:
        item._refresh_state(inter, {"values": list(values)})
    await item.callback(inter)
    return item


def menus(view):
    return [c for c in view.children if isinstance(c.item, discord.ui.Select)]


# ----- the store -----

def test_store_follows_games_and_remembers_them(tmp_path):
    path = tmp_path / "notify.json"
    s = N.NotifyStore(path)
    g1, g2 = game("1"), game("2", "nfl", "Chiefs", "Bills", 30)
    assert s.add(1, [g2, g1]) == ["nfl:2", "epl:1"]
    assert s.add(1, [g1]) == []  # already followed
    s.add(2, [N.Watch("epl", "1", "⚽ Arsenal v Chelsea")], now=NOW)  # start unknown: kept six hours from now
    s.set_dm(2, False)
    assert s.followers("epl", "1") == [(1, True), (2, False)] and s.followers("nfl", "2") == [(1, True)]
    assert s.followers("epl", "9") == []

    again = N.NotifyStore(path)
    assert [w.key for w in again.games_of(1)] == ["epl:1", "nfl:2"]  # soonest first
    assert again.dm_of(2) is False and again.dm_of(3) is True  # DMs unless you say otherwise
    raw = json.loads(path.read_text())
    assert raw["users"]["1"]["games"]["epl:1"] == {"league": "epl", "game_id": "1", "label": "⚽ Arsenal v Chelsea",
                                                   "start": g1.start,
                                                   "until": start_time(g1).timestamp() + N.KEEP_AFTER_START}
    assert raw["users"]["2"]["games"]["epl:1"]["until"] == NOW + N.KEEP_AFTER_START
    assert again.counts() == (2, 2)
    assert again.remove(1, ["epl:1", "nba:404"]) == 1 and again.clear(1) == 1 and again.games_of(1) == []
    assert "1" not in json.loads(path.read_text())["users"]  # nothing left worth keeping
    assert again.clear(2) == 1 and again.dm_of(2) is False  # a chosen setting is kept
    assert not list(tmp_path.glob("*.tmp"))  # saved atomically, nothing left over


def test_games_drop_off_after_the_final_or_six_hours_after_the_start(tmp_path):
    s = N.NotifyStore(tmp_path / "notify.json")
    g1, g2 = game("1"), game("2", hours=3)
    s.add(1, [g1, g2])
    s.add(2, [g1])
    kickoff = start_time(g1).timestamp()
    assert s.prune(kickoff + 6 * 3600 - 1) == 0
    assert s.finished("epl", "2", now=kickoff) == 1  # over: gone an hour later, well before its six hours
    assert s.prune(kickoff + 3601) == 1 and [w.key for w in s.games_of(1)] == ["epl:1"]
    assert s.extend("epl", "1", kickoff + 9 * 3600) == 2  # still going (extra time, a Test): kept for everyone
    assert s.extend("epl", "1", kickoff + 9 * 3600 + 60) == 0  # a minute more isn't worth rewriting the file
    assert s.prune(kickoff + 6 * 3600 + 1) == 0
    assert s.prune(kickoff + 9 * 3600 + 1) == 2 and s.counts() == (0, 0)


def test_store_panels_and_a_damaged_file(tmp_path):
    path = tmp_path / "notify.json"
    s = N.NotifyStore(path)
    s.set_panel(5, 50)
    s.set_panel(6, 60)
    s.drop_panel(5)
    s.drop_panel(99)
    assert N.NotifyStore(path).panels() == {6: 60}
    path.write_text("{not json")
    assert N.NotifyStore(path).panels() == {}  # starts fresh; the damaged file is set aside, not lost
    assert list(tmp_path.glob("notify.json.damaged-*"))


# ----- the panel -----

def test_panel_menus_fit_discords_limits():
    games = ([game(str(i), "epl", f"Home{i}", f"Away{i}", hours=i) for i in range(1, 31)]
             + [game(f"u{i}", league, f"H{i}", f"A{i}", hours=i) for i, league in enumerate(["nba", "nhl", "mlb"] * 10)]
             + [game("n1", "nfl", "Chiefs", "Bills", 26),
                game("live", "laliga", "Real Madrid", "Barcelona", -1, "in", "STATUS_FIRST_HALF"),
                game("c1", "cricket", "United Arab Emirates" * 3, "United States of America" * 3, 5),
                game("old", "epl", "Old", "Game", -2, "post", "STATUS_FINAL"),
                game("stale", "epl", "Stale", "Game", -5)])
    embed, view = N.panel(games, NOW)
    assert embed.title == "🔔 Game notifications" and "every score" in embed.description
    assert len(view.to_components()) <= 5
    found = menus(view)
    assert [m.custom_id for m in found] == ["notify:pick:soccer", "notify:pick:nfl", "notify:pick:us",
                                            "notify:pick:cricket"]
    for m in found:
        options = m.item.options
        assert 1 <= len(options) <= 25 and m.item.min_values == 1 and m.item.max_values == len(options)
        assert all(len(o.label) <= 100 and len(o.value) <= 100 and len(o.description) <= 100 for o in options)
    soccer = found[0].item.options
    assert len(soccer) == 25 and soccer[0].value == "laliga:live" and soccer[0].label.endswith("· 🔴 Live")
    assert soccer[1].label.startswith("⚽ Home1 v Away1 · ") and soccer[1].label.endswith(" ET")
    assert soccer[1].description == "Premier League" and soccer[-1].value == "epl:24"  # the soonest 25
    assert not {"epl:old", "epl:stale"} & {o.value for o in soccer}  # over, or long past its start
    assert {o.value.split(":")[0] for o in found[2].item.options} == {"nba", "nhl", "mlb"}  # "US sports"
    assert found[1].item.options[0].label.startswith("🏈 Bills @ Chiefs · ")  # US style: away @ home
    [cricket] = found[3].item.options
    assert cricket.label.startswith("🏏 UNI v UNI · ")  # too long with full names: abbreviations
    assert [b.custom_id for b in view.children[4:]] == ["notify:mine", "notify:clear", "notify:mode", "notify:role"]


def test_panel_shows_only_menus_with_games():
    _, view = N.panel([game("1", "nfl", "Chiefs", "Bills")], NOW)
    assert [m.custom_id for m in menus(view)] == ["notify:pick:nfl"]
    rows = view.to_components()
    assert len(rows) == 2 and [c["custom_id"] for c in rows[1]["components"]][0] == "notify:mine"
    embed, view = N.panel([], NOW)
    assert menus(view) == [] and len(view.to_components()) == 1
    assert any(f.name == "No games right now" for f in embed.fields)


def test_every_menu_and_button_is_rebuilt_from_its_id():
    async def main():
        _, view = N.panel([game("1"), game("2", "nfl", "Chiefs", "Bills"), game("3", "ipl", "CSK", "MI")], NOW)
        items = [*view.children, N.StopButton("brasileirao", "401234"),
                 N.UnpickSelect([discord.SelectOption(label="⚽ Arsenal v Chelsea", value="epl:1")]),
                 N.SetSelect("us", [discord.SelectOption(label="🏀 Celtics @ Lakers", value="nba:3", default=True)])]
        for item in items:
            assert len(item.custom_id) <= 100
            [cls] = [c for c in N.ITEMS if c.__discord_ui_compiled_template__.fullmatch(item.custom_id)]
            assert cls is type(item)
            again = await cls.from_custom_id(None, item.item, cls.__discord_ui_compiled_template__.fullmatch(
                item.custom_id))
            assert type(again) is cls and again.to_component_dict() == item.to_component_dict()
        stop = await N.StopButton.from_custom_id(None, None, N.StopButton.__discord_ui_compiled_template__.fullmatch(
            "notify:stop:brasileirao:401234"))
        assert (stop.league_key, stop.game_id) == ("brasileirao", "401234")
    asyncio.run(main())


def test_a_message_is_rebuilt_as_posted_with_picks_cleared_or_ticked(tmp_path):
    desk, _ = desk_with(tmp_path)
    _, view = N.panel([game("1"), game("2"), game("3", "nba", "Celtics", "Lakers")], NOW)
    assert N.view_from_message(message_of(view)).to_components() == view.to_components()
    desk.store.add(7, [game("2")])
    _, picker = desk.picker(7, [game("1"), game("2"), game("3", "nba", "Celtics", "Lakers")], NOW, role=False)
    ticked = N.view_from_message(message_of(picker), chosen={"epl:1", "nba:3"})
    defaults = {o.value: o.default for m in menus(ticked) for o in m.item.options}
    assert defaults == {"epl:1": True, "epl:2": False, "nba:3": True}


# ----- picking games -----

def test_picking_from_the_panel_follows_and_resets_the_menu(tmp_path):
    g1, g2 = game("1", "brasileirao", "Flamengo", "Fluminense"), game("2", "brasileirao", "Santos", "Palmeiras", 5)
    desk, bot = desk_with(tmp_path, [g1, g2])

    async def main():
        assert await desk.post_panel(PANEL)
        inter = Inter(desk, message=message_of(bot.sent[0].view))
        await click(inter, "notify:pick:soccer", ["brasileirao:1", "brasileirao:2"])
        again = Inter(desk, message=message_of(bot.sent[0].view))
        await click(again, "notify:pick:soccer", ["brasileirao:1"])
        return inter, again
    inter, again = asyncio.run(main())
    assert desk.store.panels() == {PANEL: bot.messages[0].id} and bot.messages[0].pinned
    watches = desk.store.games_of(7)
    assert [(w.label, w.start) for w in watches] == [("⚽ Flamengo v Fluminense", g1.start),
                                                     ("⚽ Santos v Palmeiras", g2.start)]
    [reset] = inter.edits  # sent again unchanged: the menu is clear for the next pick
    assert reset["view"].to_components() == bot.sent[0].view.to_components()
    text, kw = inter.replies[-1]
    assert kw["ephemeral"] and "✅ Following ⚽ Flamengo v Fluminense, ⚽ Santos v Palmeiras" in text
    assert "You follow 2 games" in text and "by **DM**" in text
    assert again.replies[-1][0].startswith("You already follow ⚽ Flamengo v Fluminense.")
    assert desk.store.channel_of(7) == PANEL  # pings (if any) go to the panel they used


def test_picks_still_work_after_a_restart_with_espn_down(tmp_path):
    g1 = game("1", "brasileirao", "Flamengo", "Fluminense")
    desk, bot = desk_with(tmp_path, [g1])
    asyncio.run(desk.post_panel(PANEL))
    restarted, _ = desk_with(tmp_path, fail=True)  # a fresh desk: no games cached, and the source fails
    inter = Inter(restarted, message=message_of(bot.sent[0].view))
    before = time.time()
    asyncio.run(click(inter, "notify:pick:soccer", ["brasileirao:1"]))
    [w] = restarted.store.games_of(7)
    assert (w.key, w.label, w.start) == ("brasileirao:1", "⚽ Flamengo v Fluminense", "")  # the menu's own label
    assert before + N.KEEP_AFTER_START <= w.until <= time.time() + N.KEEP_AFTER_START


def test_notify_opens_a_picker_that_ticks_and_unticks(tmp_path):
    g1, g2, g3, far = game("1"), game("2", home="Spurs"), game("3", "nba", "Celtics", "Lakers"), game("9", hours=400)
    desk, _ = desk_with(tmp_path, [g1, g2, g3])
    desk.store.add(7, [g1, far])  # far isn't on the menus: picking in them must leave it alone
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    desk.register(tree)

    async def main():
        inter = Inter(desk, channel_id=123)
        await tree.get_command("notify").callback(inter)
        _, kw = inter.replies[0]
        picked = Inter(desk, channel_id=123, message=message_of(kw["view"]))
        await click(picked, "notify:set:soccer", ["epl:2"])  # untick Arsenal v Chelsea, tick Spurs v Chelsea
        return inter, kw, picked
    inter, kw, picked = asyncio.run(main())
    assert kw["ephemeral"] and inter.deferred == [{"ephemeral": True, "thinking": True}]
    view = kw["view"]
    assert {o.value: o.default for m in menus(view) for o in m.item.options} == {"epl:1": True, "epl:2": False,
                                                                                 "nba:3": False}
    assert [c.custom_id for c in view.children][-1] == "notify:mode"  # no Red Alert role outside a server
    assert "Following 2" in [f.name for f in kw["embed"].fields]
    assert {w.key for w in desk.store.games_of(7)} == {"epl:2", "epl:9"}
    [edit] = picked.edits
    assert {o.value: o.default for m in menus(edit["view"]) for o in m.item.options} == {
        "epl:1": False, "epl:2": True, "nba:3": False}
    assert "Spurs v Chelsea" in edit["embed"].fields[0].value
    assert desk.store.channel_of(7) is None  # 123 isn't a notifications channel


def test_my_games_drop_clear_switch_and_stop(tmp_path):
    g1, g2 = game("1", "brasileirao", "Flamengo", "Fluminense"), game("2", "brasileirao", "Santos", "Palmeiras", 5)
    desk, bot = desk_with(tmp_path, [g1, g2])
    desk.store.set_panel(PANEL, 55)
    desk.store.add(7, [g1, g2])
    _, view = N.panel([g1, g2], NOW)
    panel = message_of(view)

    async def main():
        mine = Inter(desk, message=panel)
        await click(mine, "notify:mine")
        _, kw = mine.replies[0]
        assert kw["ephemeral"] and "Flamengo v Fluminense" in kw["embed"].description
        drop = Inter(desk, message=message_of(kw["view"]))
        await click(drop, "notify:unpick", ["brasileirao:1"])
        assert [w.key for w in desk.store.games_of(7)] == ["brasileirao:2"]
        assert "Flamengo" not in drop.edits[0]["embed"].description
        [menu] = menus(drop.edits[0]["view"])
        assert [o.value for o in menu.item.options] == ["brasileirao:2"]

        mode = Inter(desk, message=panel)
        await click(mode, "notify:mode")
        assert desk.store.dm_of(7) is False and f"<#{PANEL}>" in mode.replies[0][0]
        await click(Inter(desk, message=panel), "notify:mode")
        assert desk.store.dm_of(7) is True

        stop_view = desk._with_stop(None, "brasileirao", "2")
        stop = Inter(desk, channel_id=7007, message=message_of(stop_view))
        await click(stop, "notify:stop:brasileirao:2")
        assert desk.store.games_of(7) == [] and "Santos v Palmeiras" in stop.replies[0][0]

        desk.store.add(7, [g1, g2])
        clear = Inter(desk, message=panel)
        await click(clear, "notify:clear")
        assert desk.store.games_of(7) == [] and clear.replies[0][0].startswith("🗑️ Stopped all 2 games")
        empty = Inter(desk, message=panel)
        await click(empty, "notify:mine")
        assert "not following any games" in empty.replies[0][1]["embed"].description
        assert "view" not in empty.replies[0][1]
    asyncio.run(main())


def test_buttons_explain_when_notifications_are_off():
    inter = Inter(None, message=message_of(discord.ui.View(timeout=None).add_item(N.MineButton())))
    asyncio.run(click(inter, "notify:mine"))
    assert inter.replies[0][0] == "Game notifications aren't switched on here."


# ----- delivering -----

def stats(g):
    view = discord.ui.View(timeout=None)
    view.add_item(StatsButton(g.league_key, g.id))
    return view


def test_followers_get_a_dm_with_a_stop_button(tmp_path):
    g, alice = game("1"), User(1)
    desk, bot = desk_with(tmp_path, [g], users=[alice])
    desk.store.add(1, [g])
    embed = discord.Embed(title="⚽ GOAL")
    posted = asyncio.run(desk.notify(Update(SCORE, g), embed, stats(g)))
    [(cid, message)] = posted
    assert cid == 7001 and message.id == 9001
    content, sent, view = alice.dms[0]
    assert sent is embed and content is None
    assert [c.custom_id for c in view.children] == ["stats:epl:1", "notify:stop:epl:1"]
    assert bot.sent == []  # nothing in the channel


def test_closed_dms_become_one_ping_in_the_channel(tmp_path):
    g = game("1")
    closed1, closed2, fine = User(1, closed=True), User(2, closed=True), User(5)
    desk, bot = desk_with(tmp_path, [g], users=[closed1, closed2, fine])
    desk.store.set_panel(PANEL, 55)
    for uid in (1, 2, 3, 4, 5):
        desk.store.add(uid, [g])
    desk.store.set_dm(3, False)  # wants pings, not DMs
    embed = discord.Embed(title="Kickoff")
    posted = asyncio.run(desk.notify(Update(KICKOFF, g), embed))  # user 4 can't be found at all
    [ping] = bot.sent
    assert ping.channel == PANEL and ping.content == "🔔 <@1> <@2> <@3> <@4>" and ping.embed is embed
    assert [c.custom_id for c in ping.view.children] == ["notify:stop:epl:1"]
    assert posted == [(7005, posted[0][1]), (PANEL, bot.messages[0])] and len(fine.dms) == 1

    closed1.closed = False  # DMs aren't tried again for an hour: no 403s on every goal
    asyncio.run(desk.notify(Update(SCORE, g), embed))
    assert closed1.tries == 1 and bot.sent[-1].content == "🔔 <@1> <@2> <@3> <@4>"
    desk._dm_bounced.clear()  # an hour later
    asyncio.run(desk.notify(Update(SCORE, g), embed))
    assert closed1.tries == 2 and len(closed1.dms) == 1 and bot.sent[-1].content == "🔔 <@2> <@3> <@4>"


def test_pings_go_to_the_panel_each_person_uses(tmp_path):
    g = game("1")
    desk, bot = desk_with(tmp_path, [g])
    desk.store.set_panel(PANEL, 55)
    desk.store.set_panel(600, 66)  # a second server's notifications channel
    for uid in (1, 2):
        desk.store.add(uid, [g])
        desk.store.set_dm(uid, False)
    desk.store.set_channel(2, 600)
    asyncio.run(desk.notify(Update(SCORE, g), discord.Embed()))
    assert sorted((p.channel, p.content) for p in bot.sent) == [(PANEL, "🔔 <@1>"), (600, "🔔 <@2>")]


def test_only_the_updates_followers_care_about(tmp_path):
    soccer, nba, ipl = game("1"), game("2", "nba", "Celtics", "Lakers"), game("3", "ipl", "CSK", "MI")
    desk, bot = desk_with(tmp_path, users=[User(1)])
    desk.store.add(1, [soccer, nba, ipl])
    embed = discord.Embed()

    def sent(update):
        return len(asyncio.run(desk.notify(update, embed)))
    assert sent(Update(PERIOD, soccer)) == 0  # no such thing in soccer anyway
    assert sent(Update(PERIOD, nba)) == 1  # NBA scores come by quarter
    assert sent(Update(OVERS, ipl)) == 0 and sent(Update(WICKET, ipl)) == 1 and sent(Update(INNINGS, ipl)) == 1
    assert sent(Update(SCORE, soccer, edit=True)) == 0  # a correction: the bot edits the posts it got back
    assert sent(Update(SCORE, game("4"))) == 0  # nobody follows it
    assert sent(Update(CALLED_OFF, soccer)) == 1
    assert desk.store.games_of(1)[0].until <= time.time() + N.KEEP_AFTER_FINAL  # gone an hour after


def test_the_final_ends_a_follow_and_delivery_never_raises(tmp_path):
    g = game("1")
    desk, bot = desk_with(tmp_path, users=[User(1)])
    desk.store.add(1, [g])
    before = time.time()
    assert len(asyncio.run(desk.notify(Update(FINAL, g), discord.Embed()))) == 1
    [w] = desk.store.games_of(1)
    assert before + N.KEEP_AFTER_FINAL <= w.until <= time.time() + N.KEEP_AFTER_FINAL

    async def broken(*a, **kw):
        raise RuntimeError("boom")
    desk.store.set_dm(1, False)
    desk.store.set_panel(PANEL, 55)
    bot._send = broken
    assert asyncio.run(desk.notify(Update(SCORE, g), discord.Embed())) == []
    assert asyncio.run(desk.notify_game("epl", "1", content="Lineups are in")) == []


def test_game_news_and_huge_pings(tmp_path):
    g = game("1")
    alice = User(1)
    desk, bot = desk_with(tmp_path, users=[alice])
    desk.store.set_panel(PANEL, 55)
    desk.store.add(1, [g])
    [(_, _)] = asyncio.run(desk.notify_game("epl", "1", content="🚨 Saka starts"))
    assert alice.dms[0][0] == "🚨 Saka starts"
    assert asyncio.run(desk.notify_game("epl", "1")) == []  # nothing to say
    for uid in range(10 ** 17, 10 ** 17 + 200):  # real ids are 18 digits: 200 mentions need three posts
        desk.store.add(uid, [g])
        desk.store.set_dm(uid, False)
    posted = asyncio.run(desk.notify_game("epl", "1", embed=discord.Embed(title="XI"), content="Lineups are in"))
    pings = bot.sent
    assert len(pings) > 1 and all(len(p.content) <= 2000 for p in pings)
    assert pings[0].content.endswith("\nLineups are in") and pings[0].embed.title == "XI"
    assert all(p.embed is None and p.view is None for p in pings[1:])  # the update itself goes out once
    assert sum(p.content.count("<@") for p in pings) == 200
    assert [cid for cid, _ in posted] == [7001, PANEL]  # only the post with the update, for later edits


# ----- the Red Alert role -----

class Role:
    def __init__(self, rid, name, mentionable=False):
        self.id, self.name, self.mentionable = rid, name, mentionable

    @property
    def mention(self):
        return f"<@&{self.id}>"

    async def edit(self, mentionable, reason=None):
        self.mentionable = mentionable


class Guild:
    def __init__(self, manage_roles=True, roles=()):
        self.roles = list(roles)
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=manage_roles))

    async def create_role(self, name, mentionable, colour, reason):
        self.roles.append(Role(800 + len(self.roles), name, mentionable))
        return self.roles[-1]


class Member:
    def __init__(self, uid=7, forbidden=False):
        self.id, self.roles, self.forbidden = uid, [], forbidden

    async def add_roles(self, role, reason=None):
        if self.forbidden:
            raise http_error(discord.Forbidden, 403)
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


def test_red_alert_role_toggles_and_is_made_when_missing(tmp_path):
    guild, member = Guild(roles=[Role(1, "@everyone")]), Member()
    assert N.role_mention(guild) == "" and N.role_mention(None) == ""
    text = asyncio.run(N.toggle_role(guild, member))
    [role] = member.roles
    assert role.name == "Red Alert" and role.mentionable and text.startswith("🚨")
    assert N.role_mention(guild) == "<@&801>"
    assert asyncio.run(N.toggle_role(guild, member)).startswith("🔕") and member.roles == []
    assert len(guild.roles) == 2  # made once

    desk, _ = desk_with(tmp_path)
    inter = Inter(desk, guild=guild, user=member, message=message_of(N.panel([], NOW)[1]))
    asyncio.run(click(inter, "notify:role"))
    assert member.roles == [role] and inter.deferred == [{"ephemeral": True, "thinking": True}]
    assert inter.replies[0][1]["ephemeral"] and inter.replies[0][0].startswith("🚨")
    dm = Inter(desk, guild=None, message=message_of(N.panel([], NOW)[1]))
    asyncio.run(click(dm, "notify:role"))
    assert "in the server" in dm.replies[0][0]


def test_red_alert_role_explains_missing_permissions():
    no_perms = Guild(manage_roles=False)
    assert asyncio.run(N.ensure_role(no_perms)) is None
    assert "Manage Roles" in asyncio.run(N.toggle_role(no_perms, Member())) and no_perms.roles == []
    exists = Guild(manage_roles=False, roles=[Role(5, "Red Alert", True)])
    assert asyncio.run(N.ensure_role(exists)).id == 5
    assert "Manage Roles" in asyncio.run(N.toggle_role(exists, Member()))
    too_low = Guild(roles=[Role(5, "Red Alert", True)])
    assert "above **Red Alert**" in asyncio.run(N.toggle_role(too_low, Member(forbidden=True)))
    by_hand = Guild(roles=[Role(6, "Red Alert", mentionable=False)])
    assert asyncio.run(N.ensure_role(by_hand)).mentionable  # an alert role nobody can ping is no use


# ----- keeping panels current -----

def test_refresh_edits_panels_once_and_forgets_deleted_ones(tmp_path):
    games = [game("1", "brasileirao", "Flamengo", "Fluminense")]
    desk, bot = desk_with(tmp_path, games)
    desk.store.set_panel(1, 11)
    desk.store.set_panel(2, 22)  # its message was deleted
    desk.store.set_panel(3, 33)  # its whole channel was deleted
    bot.channels = {1: Channel(alive={11}), 2: Channel()}
    desk.store.add(9, [N.Watch("epl", "old", "⚽ Old", until=NOW - 1)])
    asyncio.run(desk.refresh_panels())
    assert desk.store.panels() == {1: 11} and desk.store.games_of(9) == []  # pruned too
    [(mid, kw)] = bot.channels[1].edits
    assert mid == 11 and kw["embed"].title == "🔔 Game notifications"
    assert [m.custom_id for m in menus(kw["view"])] == ["notify:pick:soccer"]
    asyncio.run(desk.refresh_panels())
    assert len(bot.channels[1].edits) == 1  # nothing changed: no edit
    games.append(game("2", "nfl", "Chiefs", "Bills"))
    desk._fetched = None  # the minute's cache is up
    asyncio.run(desk.refresh_panels())
    assert [m.custom_id for m in menus(bot.channels[1].edits[-1][1]["view"])] == ["notify:pick:soccer",
                                                                                  "notify:pick:nfl"]


def test_post_panel_once_per_channel(tmp_path):
    desk, bot = desk_with(tmp_path, [game("1")])
    assert asyncio.run(desk.post_panel(PANEL)) and len(bot.sent) == 1
    bot.channels = {PANEL: Channel(alive={bot.messages[0].id})}
    assert asyncio.run(desk.post_panel(PANEL)) and len(bot.sent) == 1  # brought up to date instead
    bot.channels = {PANEL: Channel(forbidden=True)}  # still there, but I may not edit it: no second panel
    assert not asyncio.run(desk.post_panel(PANEL)) and len(bot.sent) == 1
    bot.channels = {PANEL: Channel()}  # someone deleted it: post a new one
    assert asyncio.run(desk.post_panel(PANEL)) and len(bot.sent) == 2
    assert desk.store.panels() == {PANEL: bot.messages[1].id}

    async def fail(*a, **kw):
        return None
    bot._send = fail
    assert not asyncio.run(desk.post_panel(42)) and 42 not in desk.store.panels()


def test_status_and_dynamic_items(tmp_path):
    desk, _ = desk_with(tmp_path)
    assert desk.status() == "0 people follow 0 games · no panel yet"
    desk.store.add(1, [game("1"), game("2")])
    desk.store.add(2, [game("1")])
    desk.store.set_panel(PANEL, 55)
    assert desk.status() == "2 people follow 2 games · panel in 1 channel"
    assert set(desk.dynamic_items()) == set(N.ITEMS) and len({c.__discord_ui_compiled_template__.pattern
                                                               for c in N.ITEMS}) == len(N.ITEMS)
