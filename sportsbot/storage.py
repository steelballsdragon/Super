"""JSON-file persistence for channel subscriptions."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Subscription:
    channel_id: int
    league: str
    team: str | None = None  # None means every game in the league
    ball_by_ball: bool = False  # cricket: post every delivery


class SubscriptionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._subs: set[Subscription] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open() as f:
            raw = json.load(f)
        self._subs = {
            Subscription(int(s["channel_id"]), s["league"], s.get("team"), bool(s.get("ball_by_ball", False)))
            for s in raw
        }

    def _save(self) -> None:
        data = [
            {"channel_id": s.channel_id, "league": s.league, "team": s.team, "ball_by_ball": s.ball_by_ball}
            for s in sorted(self._subs, key=lambda s: (s.channel_id, s.league, s.team or ""))
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def _norm(team: str | None) -> str | None:
        team = (team or "").strip().lower()
        return team or None

    def add(self, channel_id: int, league: str, team: str | None = None, ball_by_ball: bool = False) -> bool:
        """Follow a league/team; following again in the other mode switches modes."""
        sub = Subscription(channel_id, league, self._norm(team), ball_by_ball)
        if sub in self._subs:
            return False
        self._subs.discard(Subscription(channel_id, league, self._norm(team), not ball_by_ball))
        self._subs.add(sub)
        self._save()
        return True

    def remove(self, channel_id: int, league: str, team: str | None = None) -> bool:
        team = self._norm(team)
        gone = {s for s in self._subs if (s.channel_id, s.league, s.team) == (channel_id, league, team)}
        if not gone:
            return False
        self._subs -= gone
        self._save()
        return True

    def remove_channel(self, channel_id: int) -> None:
        before = len(self._subs)
        self._subs = {s for s in self._subs if s.channel_id != channel_id}
        if len(self._subs) != before:
            self._save()

    def for_channel(self, channel_id: int) -> list[Subscription]:
        return sorted(
            (s for s in self._subs if s.channel_id == channel_id),
            key=lambda s: (s.league, s.team or ""),
        )

    def for_league(self, league: str) -> list[Subscription]:
        return [s for s in self._subs if s.league == league]

    def leagues(self) -> set[str]:
        return {s.league for s in self._subs}
