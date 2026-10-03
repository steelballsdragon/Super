import json
import logging

from sportsbot.settings import SettingsStore, StateStore
from sportsbot.storage import SubscriptionStore


def test_damaged_files_are_set_aside_and_the_bot_still_starts(tmp_path, caplog):
    for name in ("subscriptions.json", "settings.json", "state.json"):
        (tmp_path / name).write_text('{"half a fi')
    with caplog.at_level(logging.ERROR):
        subs = SubscriptionStore(tmp_path / "subscriptions.json")
        settings = SettingsStore(tmp_path / "settings.json")
        state = StateStore(tmp_path / "state.json")
    assert subs.leagues() == set()
    assert settings.get(1).threads is False
    assert state.get("threads", "x") is None
    # Nothing is lost for good: each damaged file is kept next to the original.
    kept = sorted(p.name.split(".damaged-")[0] for p in tmp_path.glob("*.damaged-*"))
    assert kept == ["settings.json", "state.json", "subscriptions.json"]
    assert "was damaged" in caplog.text

    # And the bot carries on saving normally.
    assert subs.add(1, "nfl")
    assert json.loads((tmp_path / "subscriptions.json").read_text())[0]["league"] == "nfl"


def test_saves_leave_no_temp_files(tmp_path):
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    state = StateStore(tmp_path / "state.json")
    for i in range(5):
        store.add(i, "nba")
        state.set("finals", str(i), i)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "subscriptions.json"]
    assert SubscriptionStore(tmp_path / "subscriptions.json").leagues() == {"nba"}
    assert StateStore(tmp_path / "state.json").get("finals", "4") == 4
