"""Supported leagues and their ESPN data feeds."""

from dataclasses import dataclass


@dataclass(frozen=True)
class League:
    key: str
    name: str
    sport: str  # "football", "soccer", "basketball", "baseball", "hockey" or "cricket"
    path: str  # ESPN path segment: /apis/site/v2/sports/{path}/scoreboard
    emoji: str
    # "scoreboard" for a single league; "scorepanel" for ESPN's feed of every
    # current cricket match, which we narrow to internationals.
    feed: str = "scoreboard"


LEAGUES: dict[str, League] = {
    league.key: league
    for league in [
        League("nfl", "NFL", "football", "football/nfl", "🏈"),
        League("nba", "NBA", "basketball", "basketball/nba", "🏀"),
        League("mlb", "MLB", "baseball", "baseball/mlb", "⚾"),
        League("nhl", "NHL", "hockey", "hockey/nhl", "🏒"),
        League("epl", "Premier League", "soccer", "soccer/eng.1", "⚽"),
        League("laliga", "La Liga", "soccer", "soccer/esp.1", "⚽"),
        League("seriea", "Serie A", "soccer", "soccer/ita.1", "⚽"),
        League("bundesliga", "Bundesliga", "soccer", "soccer/ger.1", "⚽"),
        League("ligue1", "Ligue 1", "soccer", "soccer/fra.1", "⚽"),
        League("mls", "MLS", "soccer", "soccer/usa.1", "⚽"),
        League("ucl", "Champions League", "soccer", "soccer/uefa.champions", "⚽"),
        League("uel", "Europa League", "soccer", "soccer/uefa.europa", "⚽"),
        League("worldcup", "FIFA World Cup", "soccer", "soccer/fifa.world", "⚽"),
        League("ipl", "IPL", "cricket", "cricket/8048", "🏏"),
        League("cricket", "International cricket", "cricket", "cricket", "🏏", feed="scorepanel"),
    ]
}
