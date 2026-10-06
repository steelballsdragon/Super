"""What each Discord channel is for (stocks, crypto, news or research) and its settings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .storage import read_json, write_json
from .universe import CRYPTO, DEFAULT_CRYPTO, DEFAULT_STOCKS, STOCKS

KINDS = ("stocks", "crypto", "news", "research")
KIND_NAMES = {"stocks": "📈 Stocks", "crypto": "🪙 Crypto", "news": "📰 News", "research": "🔬 Research"}
NEWS_LEVELS = {"major": 75, "important": 55, "all": 35}
MAX_WATCHLIST = 30


@dataclass(frozen=True)
class ChannelConfig:
    kind: str
    guild_id: int = 0
    watchlist: tuple[str, ...] = ()  # empty: the market's default list
    board_message_id: int | None = None
    alerts: bool = True  # big moves, 52-week highs and breakout setups
    news_level: str = "important"  # news channels: "major", "important" or "all"
    news_markets: tuple[str, ...] = ("stocks", "crypto", "macro")
    briefs: bool = True  # scheduled briefs and research digests
    timezone: str = "America/New_York"
    brief_hour: int = 8  # crypto brief hour (local time)

    @property
    def market(self) -> str | None:
        return self.kind if self.kind in (STOCKS, CRYPTO) else None

    def symbols(self) -> list[str]:
        if self.watchlist:
            return list(self.watchlist)
        if self.kind == STOCKS:
            return list(DEFAULT_STOCKS)
        if self.kind == CRYPTO:
            return list(DEFAULT_CRYPTO)
        return []


class ChannelStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        known = {f.name for f in fields(ChannelConfig)}
        raw = read_json(self.path, {})
        self._configs: dict[int, ChannelConfig] = {}
        for cid, values in raw.items():
            values = {k: tuple(v) if isinstance(v, list) else v for k, v in values.items() if k in known}
            if values.get("kind") in KINDS:
                self._configs[int(cid)] = ChannelConfig(**values)

    def _save(self) -> None:
        write_json(self.path, {str(c): asdict(cfg) for c, cfg in sorted(self._configs.items())})

    def get(self, channel_id: int) -> ChannelConfig | None:
        return self._configs.get(channel_id)

    def set(self, channel_id: int, kind: str, guild_id: int = 0) -> ChannelConfig:
        old = self._configs.get(channel_id)
        cfg = replace(old, kind=kind, board_message_id=None) if old else ChannelConfig(kind, guild_id)
        if old and old.kind != kind:
            cfg = replace(cfg, watchlist=())
        self._configs[channel_id] = cfg
        self._save()
        return cfg

    def update(self, channel_id: int, **changes) -> ChannelConfig | None:
        old = self._configs.get(channel_id)
        if old is None:
            return None
        self._configs[channel_id] = replace(old, **changes)
        self._save()
        return self._configs[channel_id]

    def remove(self, channel_id: int) -> bool:
        if self._configs.pop(channel_id, None) is None:
            return False
        self._save()
        return True

    def of_kind(self, kind: str) -> list[tuple[int, ChannelConfig]]:
        return [(c, cfg) for c, cfg in self._configs.items() if cfg.kind == kind]

    def all(self) -> list[tuple[int, ChannelConfig]]:
        return list(self._configs.items())
