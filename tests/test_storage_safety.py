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


def test_a_batch_saves_once_and_still_saves_if_it_fails(tmp_path, monkeypatch):
    import sportsbot.settings as settings
    saves = []
    real = settings.write_json
    monkeypatch.setattr(settings, "write_json", lambda path, data: (saves.append(1), real(path, data)))
    state = StateStore(tmp_path / "state.json")
    with state.batch():
        for i in range(50):
            state.set("odds", str(i), i)
        with state.batch():  # nested batches save once, at the outermost end
            state.delete("odds", "0")
    assert len(saves) == 1 and StateStore(tmp_path / "state.json").get("odds", "49") == 49
    try:
        with state.batch():
            state.set("odds", "x", 1)
            raise RuntimeError("cycle failed")
    except RuntimeError:
        pass
    assert len(saves) == 2 and StateStore(tmp_path / "state.json").get("odds", "x") == 1
    state.set("odds", "y", 2)  # outside a batch: saved straight away
    assert len(saves) == 3


def test_betting_record_moves_to_its_own_file_and_keeps_its_history(tmp_path):
    from sportsbot.bot import SportsBot
    old = StateStore(tmp_path / "state.json")  # how earlier versions kept it
    old.set("parlays", "p1", {"id": "p1", "status": "won"})
    old.set("leans", "nfl:1:total", {"result": "win"})
    old.set("threads", "1:nfl:9", {"id": 5, "at": 1.0})
    bot = SportsBot(SubscriptionStore(tmp_path / "subscriptions.json"), 10, None)
    assert bot.records.get("parlays", "p1")["status"] == "won" and bot.records.get("leans", "nfl:1:total")
    assert bot.state.items("parlays") == [] and bot.state.items("leans") == []
    assert bot.state.get("threads", "1:nfl:9") == {"id": 5, "at": 1.0}  # everything else stays put
    assert StateStore(tmp_path / "record.json").get("parlays", "p1") and not StateStore(tmp_path / "state.json").items("leans")
    SportsBot(SubscriptionStore(tmp_path / "subscriptions.json"), 10, None)  # moving again changes nothing
    assert StateStore(tmp_path / "record.json").get("parlays", "p1")["status"] == "won"
