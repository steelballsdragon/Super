"""Per-channel settings and bot state that must survive restarts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .storage import read_json, write_json


@dataclass(frozen=True)
class ChannelSettings:
    threads: bool = False  # put each game's updates in its own thread
    board_message_id: int | None = None  # the live scoreboard message, if any
    odds: bool = True  # show the betting line at the start and grade it at the final
    daily_hour: int | None = None  # post today's schedule at this hour (local), or never
    timezone: str = "America/Toronto"  # for the daily schedule
    reminders: bool = False  # post a heads-up 15 minutes before followed games
    lottos: bool = False  # post the day's lottos here every morning
    picks: bool = False  # post the day's best picks and a safe parlay here every morning


class SettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        known = {f.name for f in fields(ChannelSettings)}
        raw = read_json(self.path, {})
        self._settings = {
            int(cid): ChannelSettings(**{k: v for k, v in values.items() if k in known})
            for cid, values in raw.items()
        }

    def get(self, channel_id: int) -> ChannelSettings:
        return self._settings.get(channel_id, ChannelSettings())

    def update(self, channel_id: int, **changes) -> ChannelSettings:
        new = replace(self.get(channel_id), **changes)
        if new == ChannelSettings():
            self._settings.pop(channel_id, None)
        else:
            self._settings[channel_id] = new
        write_json(self.path, {str(c): asdict(s) for c, s in sorted(self._settings.items())})
        return new

    def channels_with(self, predicate) -> list[tuple[int, ChannelSettings]]:
        return [(c, s) for c, s in self._settings.items() if predicate(s)]


class StateStore:
    """Small key/value store for bot state like which thread belongs to which game.

    Each change is saved straight away, except inside `batch()`, where changes are
    saved once at the end (an update cycle can change dozens of entries at once).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, dict] = read_json(self.path, {})
        self._held = 0
        self._dirty = False

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value) -> None:
        self._data.setdefault(section, {})[key] = value
        self._changed()

    def delete(self, section: str, key: str) -> None:
        if self._data.get(section, {}).pop(key, None) is not None:
            self._changed()

    def items(self, section: str):
        return list(self._data.get(section, {}).items())

    def _changed(self) -> None:
        if self._held:
            self._dirty = True
        else:
            write_json(self.path, self._data)

    @contextmanager
    def batch(self):
        """Saves once when the outermost batch ends, however many changes it made (even if it fails)."""
        self._held += 1
        try:
            yield self
        finally:
            self._held -= 1
            if not self._held and self._dirty:
                self._dirty = False
                write_json(self.path, self._data)


def move_sections(source: StateStore, target: StateStore, sections) -> None:
    """Moves whole sections from one store to another (e.g. when a section gets its own file), once."""
    with source.batch(), target.batch():
        for section in sections:
            for key, value in source.items(section):
                if target.get(section, key) is None:
                    target.set(section, key, value)
                source.delete(section, key)
