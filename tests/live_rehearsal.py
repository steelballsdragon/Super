"""A dress rehearsal against the real data sources, with Discord faked out: runs the live jobs, every recap and the
main commands, and prints what each would post (titles and sizes). Not collected by pytest (it uses the network).

    python -m tests.live_rehearsal               # everything up
    python -m tests.live_rehearsal --yahoo-down  # Yahoo unreachable: the backups must carry the bot
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import tempfile
import time
from types import SimpleNamespace

from marketbot import briefs
from marketbot.ai import NewsAI
from marketbot.bot import MarketBot
from marketbot.commands import register_commands

KINDS = [(1, "stocks"), (2, "crypto"), (3, "news"), (4, "research"), (5, "trends"), (6, "nvidia")]
COMMANDS = [
    ("trends", {}, 5), ("trends", {"period": SimpleNamespace(value="WTD"), "market": SimpleNamespace(value="crypto")}, 5),
    ("trends", {"period": SimpleNamespace(value="YTD"), "market": SimpleNamespace(value="sectors")}, 5),
    ("nvidia", {}, 6), ("price", {"symbol": "nvidia"}, 1), ("price", {"symbol": "hyperliquid"}, 2),
    ("forecast", {"symbol": "brk.b"}, 1), ("chart", {"symbol": "btc"}, 2), ("breakouts", {}, 1), ("movers", {}, 1),
    ("status", {}, 1), ("price", {"symbol": "notarealthing123"}, 1),
]


class Interaction:
    """Just enough of discord.Interaction for the commands."""

    def __init__(self, channel_id: int):
        self.channel_id, self.guild_id, self.guild, self.command = channel_id, 9, None, None
        self.user = SimpleNamespace(id=42)
        self.permissions = SimpleNamespace(manage_channels=True, manage_messages=True)
        self.out: list[tuple[str | None, dict]] = []
        done = []

        async def defer(**_):
            done.append(1)

        async def send_message(content=None, **kw):
            done.append(1)
            self.out.append((content, kw))

        async def send(content=None, **kw):
            self.out.append((content, kw))

        self.response = SimpleNamespace(defer=defer, send_message=send_message, is_done=lambda: bool(done))
        self.followup = SimpleNamespace(send=send)


def describe(out) -> str:
    parts = []
    for content, kw in out:
        embeds = kw.get("embeds") or ([kw["embed"]] if kw.get("embed") else [])
        files = kw.get("files") or ([kw["file"]] if kw.get("file") else [])
        parts.append((content or "") + " ".join(f"[{e.title} · {len(e)} chars]" for e in embeds)
                     + (f" +{len(files)} file(s)" if files else ""))
    return " | ".join(parts) or "(nothing)"


async def rehearse(yahoo_down: bool) -> None:
    folder = tempfile.mkdtemp(prefix="marketbot-rehearsal-")
    bot = MarketBot(folder, ai=NewsAI(api_key=""))
    if yahoo_down:
        bot.engine.data.http.proxies["Yahoo"] = "http://127.0.0.1:9"  # nothing listens there
    posts, boards = [], []

    async def send(cid, post):
        posts.append((cid, post))
        return SimpleNamespace(id=1)

    async def show_board(cid, embed):
        boards.append((cid, embed))

    bot.send, bot.show_board = send, show_board
    register_commands(bot)
    for cid, kind in KINDS:
        bot.channels.set(cid, kind, 9)
    for name, job in (("live", bot.job_live), ("crypto data", bot.job_crypto_data), ("trends", bot.job_trends),
                      ("nvidia", bot.job_nvidia)):
        started = time.time()
        await job()
        print(f"job {name}: {time.time() - started:.1f}s")
    for cid, embed in boards:
        print(f"  board in {cid}: {embed.title} · {len(embed)} chars · {embed.footer.text}")
    for label, make in (("trends today", lambda: briefs.trends_recap(bot, "1D")),
                        ("trends week", lambda: briefs.trends_recap(bot, "1W")),
                        ("trends month", lambda: briefs.trends_recap(bot, "1M")),
                        ("crypto trends", lambda: briefs.crypto_trends(bot)),
                        ("nvidia brief", lambda: briefs.nvidia_brief(bot, "premarket"))):
        found = await make()
        print(f"recap {label}: " + " | ".join(f"{e.title} · {len(e)}" for p in found for e in p.embeds))
    commands = {c.name: c for c in bot.tree.get_commands()}
    for name, kwargs, channel in COMMANDS:
        it = Interaction(channel)
        started = time.time()
        try:
            await commands[name].callback(it, **kwargs)
        except Exception as exc:
            it.out.append((f"!! {type(exc).__name__}: {exc}", {}))
        print(f"/{name} {kwargs or ''} {time.time() - started:.1f}s: {describe(it.out)}")
    print("sources:", {k: h.line() for k, h in bot.engine.data.health.items()})
    await bot.engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yahoo-down", action="store_true", help="make Yahoo unreachable")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(rehearse(args.yahoo_down))


if __name__ == "__main__":
    main()
