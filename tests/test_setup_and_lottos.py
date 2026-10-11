"""/setup makes every channel (leagues, picks, lottos) and wires each up once; lotto and picks channels get the
morning's posts once a day, quietly skipping leagues with nothing to bet on."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sportsbot.bot import SETUP_CHANNELS, SOCCER, ChannelPoster, SportsBot, register_commands
from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.storage import SubscriptionStore


def make_bot(tmp_path):
    bot = SportsBot(SubscriptionStore(tmp_path / "subscriptions.json"), 10, None)
    register_commands(bot)
    bot.sent = []

    async def send(channel_id, embed=None, content=None, view=None):
        bot.sent.append((channel_id, embed, content, view))

        async def pin():
            pass
        return SimpleNamespace(id=900 + len(bot.sent), pin=pin)
    bot._send = send
    return bot


class Guild:
    def __init__(self):
        self.categories, self.next_id = [], 100
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True))

    async def create_category(self, name):
        cat = SimpleNamespace(name=name, text_channels=[])
        self.categories.append(cat)
        return cat

    async def create_text_channel(self, name, category, topic):
        self.next_id += 1
        ch = SimpleNamespace(id=self.next_id, name=name, topic=topic, mention=f"<#{self.next_id}>")
        category.text_channels.append(ch)
        return ch


class Inter:
    def __init__(self, guild):
        self.guild, self.channel_id, self.replies = guild, 1, []
        replies = self.replies

        class Response:
            async def defer(self, **kw):
                pass

            async def send_message(self, content=None, **kw):
                replies.append(content)

        async def followup(content=None, **kw):
            replies.append(content)

        self.response, self.followup = Response(), SimpleNamespace(send=followup)


def test_setup_makes_every_channel_once(tmp_path):
    bot = make_bot(tmp_path)
    guild = Guild()
    setup = bot.tree.get_command("setup").callback
    asyncio.run(setup(Inter(guild), category="🎰 ScoreBot"))
    [cat] = guild.categories
    names = [c.name for c in cat.text_channels]
    assert names == [name for name, *_ in SETUP_CHANNELS]
    by = {c.name: c.id for c in cat.text_channels}
    assert {s.league for s in bot.store.for_channel(by["⚽-soccer"])} == set(SOCCER)
    assert {s.league for s in bot.store.for_channel(by["🏏-cricket"])} == {"ipl", "cricket"}
    nfl = bot.settings.get(by["🏈-nfl"])
    assert nfl.daily_hour == 9 and nfl.board_message_id and nfl.odds and not nfl.lottos
    assert bot.settings.get(by["🎰-lottos"]).lottos and not bot.store.for_channel(by["🎰-lottos"])
    assert bot.settings.get(by["🎯-picks"]).picks
    intros = [e.title for _, e, _, _ in bot.sent if e is not None and e.title in names]
    assert intros == names
    boards = len(bot.sent)
    asyncio.run(setup(Inter(guild), category="🎰 ScoreBot"))  # again: nothing new, nothing posted twice
    assert len(cat.text_channels) == len(SETUP_CHANNELS) and len(bot.sent) == boards
    asyncio.run(bot.espn.close())


def test_setup_needs_manage_channels(tmp_path):
    bot = make_bot(tmp_path)
    guild = Guild()
    guild.me.guild_permissions.manage_channels = False
    i = Inter(guild)
    asyncio.run(bot.tree.get_command("setup").callback(i, category="x"))
    assert "Manage Channels" in i.replies[0] and not guild.categories
    asyncio.run(bot.espn.close())


def game(gid, home, away, start: datetime, league="mls"):
    comp = {"status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"id": home, "abbreviation": home,
                                                                         "displayName": home}},
                            {"homeAway": "away", "score": "0", "team": {"id": away, "abbreviation": away,
                                                                         "displayName": away}}]}
    [g] = parse_scoreboard({"events": [{"id": gid, "date": f"{start:%Y-%m-%dT%H:%MZ}", "competitions": [comp]}]},
                           LEAGUES[league])
    return g


def test_morning_lottos_post_only_what_can_be_built(tmp_path):
    bot = make_bot(tmp_path)
    now = datetime.now(timezone.utc)
    tz = timezone(timedelta(hours=1 - now.hour, minutes=-now.minute))  # it's about 1 AM there: games at 3 AM are today
    games = [game(str(10 + i), f"H{i}", f"A{i}", now + timedelta(hours=2)) for i in range(6)]

    async def week_games(key):
        return games if key == "mls" else []
    bot.week_games = week_games

    async def longshots(g, kind, payout=None):
        return []  # no likely scorers: the slate lotto is skipped quietly
    bot.longshots = longshots

    async def game_props(g, bigger=False, underdog=False, scorers=False):
        if underdog or scorers:
            return [], None
        return [SimpleNamespace(pick=f"{g.home.name} P{i} Over 0.5 Shots", probability=0.62, evidence="",
                                player_id=f"{g.id}-{i}", prop=SimpleNamespace(stat="shots"), line=1, player="P",
                                team=g.home.abbrev) for i in range(2)], None
    bot.game_props = game_props
    posted = asyncio.run(bot.auto_post(7, "lottos", tz))
    assert posted == 1
    header, parlay, slip = bot.sent
    assert header[1].title.startswith("🎰 Today's lottos") and parlay[1].title.startswith("🎟️ ⚽ MLS · Lotto")
    assert slip[1] is None and slip[2].startswith("📋 Copy or screenshot") and slip[3] is not None
    assert all(cid == 7 for cid, *_ in bot.sent)
    [p] = bot.parlays.pending()
    assert p["channel"] == 7  # graded in the lotto channel

    bot.sent.clear()
    bot.week_games = lambda key: asyncio.sleep(0, result=[])
    assert asyncio.run(bot.auto_post(7, "lottos", tz)) == 0 and bot.sent == []  # no games: no header either
    asyncio.run(bot.espn.close())


def test_the_morning_posts_go_out_once_a_day_at_seven(tmp_path):
    bot = make_bot(tmp_path)
    calls = []

    async def poster(cid, kind, tz):
        calls.append((cid, kind))
    bot.auto_post = poster
    bot.settings.update(5, lottos=True, picks=True, timezone="America/Toronto")
    bot.settings.update(6, odds=True)

    async def run(at):
        await bot._post_daily_bets(at)
        await asyncio.gather(*bot._tasks)

    toronto = timezone(timedelta(hours=-4))
    asyncio.run(run(datetime(2026, 10, 10, 6, 30, tzinfo=toronto)))
    assert calls == []
    asyncio.run(run(datetime(2026, 10, 10, 7, 5, tzinfo=toronto)))
    asyncio.run(run(datetime(2026, 10, 10, 8, 0, tzinfo=toronto)))
    assert calls == [(5, "picks"), (5, "lottos")]
    asyncio.run(run(datetime(2026, 10, 11, 7, 0, tzinfo=toronto)))
    assert len(calls) == 4
    bot.settings.update(5, redalerts=True)
    asyncio.run(run(datetime(2026, 10, 12, 7, 0, tzinfo=toronto)))
    asyncio.run(run(datetime(2026, 10, 12, 14, 30, tzinfo=toronto)))
    asyncio.run(run(datetime(2026, 10, 12, 15, 0, tzinfo=toronto)))
    assert calls[4:] == [(5, "picks"), (5, "lottos"), (5, "redalerts"), (5, "redalerts-late")]
    asyncio.run(bot.espn.close())


def test_channel_poster_drops_notes_and_sends_the_header_first():
    sent = []

    async def send(cid, embed=None, content=None, view=None):
        sent.append((cid, getattr(embed, "title", None), content))
    out = ChannelPoster(SimpleNamespace(_send=send), 3, SimpleNamespace(title="H"))
    asyncio.run(out.followup.send("Not enough strong legs"))
    assert sent == [] and out.posted == 0
    asyncio.run(out.followup.send(embed=SimpleNamespace(title="E")))
    asyncio.run(out.followup.send("slip", view=object()))
    assert sent == [(3, "H", None), (3, "E", None), (3, None, "slip")] and out.posted == 1


def test_railway_defaults(monkeypatch):
    from sportsbot import bot as B
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
    assert B.default_data_file() == "/data/subscriptions.json"
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH")
    assert B.default_data_file() == "subscriptions.json"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef1234")
    assert B.code_version() == "abcdef1"


def test_setup_again_follows_a_newly_added_league_quietly(tmp_path):
    bot = make_bot(tmp_path)
    guild = Guild()
    setup = bot.tree.get_command("setup").callback
    asyncio.run(setup(Inter(guild), category="🎰 ScoreBot"))
    soccer = next(c for c in guild.categories[0].text_channels if c.name == "⚽-soccer")
    bot.store.remove(soccer.id, "brasileirao")  # as if set up before Brazil's Série A was added
    posts = len(bot.sent)
    asyncio.run(setup(Inter(guild), category="🎰 ScoreBot"))
    assert "brasileirao" in {s.league for s in bot.store.for_channel(soccer.id)} and len(bot.sent) == posts
    asyncio.run(bot.espn.close())
