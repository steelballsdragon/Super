"""Supported leagues and their ESPN scoreboard paths."""

from dataclasses import dataclass


@dataclass(frozen=True)
class League:
    key: str
    name: str
    sport: str  # "football" or "soccer"
    path: str  # ESPN path segment: /apis/site/v2/sports/{path}/scoreboard
    emoji: str


LEAGUES: dict[str, League] = {
    league.key: league
    for league in [
        League("nfl", "NFL", "football", "football/nfl", "🏈"),
        League("epl", "Premier League", "soccer", "soccer/eng.1", "⚽"),
        League("laliga", "La Liga", "soccer", "soccer/esp.1", "⚽"),
        League("seriea", "Serie A", "soccer", "soccer/ita.1", "⚽"),
        League("bundesliga", "Bundesliga", "soccer", "soccer/ger.1", "⚽"),
        League("ligue1", "Ligue 1", "soccer", "soccer/fra.1", "⚽"),
        League("mls", "MLS", "soccer", "soccer/usa.1", "⚽"),
        League("ucl", "Champions League", "soccer", "soccer/uefa.champions", "⚽"),
        League("uel", "Europa League", "soccer", "soccer/uefa.europa", "⚽"),
        League("worldcup", "FIFA World Cup", "soccer", "soccer/fifa.world", "⚽"),
    ]
}
