"""Saved files: atomic JSON writes, and a small key/value store for bot state."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


def write_json(path: Path, data) -> None:
    """Saves atomically and flushes to disk, so a crash or power cut leaves the old or new file, never half of one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default):
    """Loads a saved file; a damaged one is set aside (not deleted) so the bot still starts."""
    path = Path(path)
    try:
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        aside = path.with_name(f"{path.name}.damaged-{int(time.time())}")
        os.replace(path, aside)
        log.error("%s was damaged; moved it to %s and started fresh", path, aside.name)
        return default


class StateStore:
    """Sections of keys and values saved to one JSON file.

    Each change is saved straight away, except inside `batch()`, where changes are
    saved once at the end (one pass can change hundreds of entries).
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
