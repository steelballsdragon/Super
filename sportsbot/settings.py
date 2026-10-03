"""Per-channel settings and bot state that must survive restarts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


@dataclass(frozen=True)
class ChannelSettings:
    threads: bool = False  # put each game's updates in its own thread
    board_message_id: int | None = None  # the live scoreboard message, if any


class SettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        known = {f.name for f in fields(ChannelSettings)}
        raw = _read_json(self.path, {})
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
        _write_json(self.path, {str(c): asdict(s) for c, s in sorted(self._settings.items())})
        return new

    def channels_with(self, predicate) -> list[tuple[int, ChannelSettings]]:
        return [(c, s) for c, s in self._settings.items() if predicate(s)]


class StateStore:
    """Small key/value store for bot state like which thread belongs to which game."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, dict] = _read_json(self.path, {})

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value) -> None:
        self._data.setdefault(section, {})[key] = value
        _write_json(self.path, self._data)

    def delete(self, section: str, key: str) -> None:
        if self._data.get(section, {}).pop(key, None) is not None:
            _write_json(self.path, self._data)

    def items(self, section: str):
        return list(self._data.get(section, {}).items())
