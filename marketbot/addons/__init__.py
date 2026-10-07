"""Add-on features. Each lives in its own module and plugs into the bot through a Feature: its slash commands,
its scheduled jobs, its lines in /status and /help, and its clients' clean-up. The bot core only loops over them.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class Feature:
    """Override what the feature needs. `bot` is the MarketBot."""

    name = "feature"
    help_group = ""  # the /help heading its commands go under ("" for none)

    def __init__(self, bot):
        self.bot = bot

    def jobs(self) -> list[tuple[str, float, object]]:
        """(job name, seconds between runs, async callable) for the scheduler."""
        return []

    def register(self, tree) -> None:
        """Adds its slash commands to the command tree."""

    def help(self) -> list[tuple[str, str]]:
        """(command name, what it does) for /help."""
        return []

    def status(self) -> list[str]:
        """Lines for /status (data sources, keys, budgets)."""
        return []

    async def close(self) -> None:
        pass


def load(bot) -> list[Feature]:
    """Every add-on, in /help order. A feature that fails to start is logged and left out; the bot runs on."""
    from . import registry
    features = []
    for cls in registry.FEATURES:
        try:
            features.append(cls(bot))
        except Exception:
            log.exception("Feature %s failed to start; running without it", getattr(cls, "name", cls))
    return features
