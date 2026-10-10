import pytest


@pytest.fixture(autouse=True)
def no_livescore(monkeypatch):
    """Tests never reach LiveScore unless they set its answers themselves (see test_livescore.py)."""
    from sportsbot import livescore

    async def offline(self, url):
        return None
    monkeypatch.setattr(livescore.FastGoals, "_get", offline)
