"""Trusted entry points may submit work; forks, subagents, multi-bot rooms and unknown sources may not."""
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from test_work import BOT_CHAT, FakeHermes, call, store_mod, titles, work

entries = sys.modules["conduit_push.work_entry_points"]
room_rules = sys.modules["conduit_push.room_rules"]

FOCAL = "20261003_132936_3ec1ac"
VOICE = "f3235085-389c-4498-9b03-1369cd08af1f"
DESKTOP = "20261108_desktop_mac"
PHONE = "20261108_conduit_phone"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "hermes"


@pytest.fixture
def hermes(home, monkeypatch):
    home.mkdir(parents=True, exist_ok=True)
    fake = FakeHermes(home)
    server = types.ModuleType("tui_gateway.server")
    server.dispatch = fake.dispatch
    server._sessions = fake._sessions
    server._methods = fake._methods
    transport = types.ModuleType("tui_gateway.transport")
    transport.bind_transport = fake._current.set
    transport.current_transport = fake._current.get
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    monkeypatch.setitem(sys.modules, "tui_gateway.transport", transport)
    monkeypatch.setattr(work, "_home", lambda: home)
    monkeypatch.setattr(titles, "_read", lambda h, key: "Bot Chat" if key == BOT_CHAT else "Some chat")
    monkeypatch.setattr(titles, "read_or_raise", lambda h, key: "Bot Chat" if key == BOT_CHAT else "Some chat")
    monkeypatch.setattr(titles, "_bot_name", lambda h: "Furina")
    monkeypatch.setattr(work, "WAKE_BATCH_S", 0.01)
    monkeypatch.setattr(work, "_swept", set())
    monkeypatch.setattr(work, "_not_compacted", set())
    monkeypatch.setattr(work, "_title_cache", {})
    monkeypatch.setattr(work, "_is_child", lambda session_id: False)
    monkeypatch.setattr(work, "_policy_cache", {})
    work._wakes.clear()
    work._wake_asks.clear()
    work._wake_held.clear()
    work._finished.clear()
    work._unjournaled.clear()
    work._turn_began.clear()
    work._replies.clear()
    work._redirecting.clear()
    work._shutting_down.clear()
    room_rules.forget()
    return fake


def _session(home, session_id, *, source, key="", title="Chat", model_config=None):
    path = Path(home) / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS sessions ("
               "id TEXT PRIMARY KEY, source TEXT, title TEXT, session_key TEXT, model_config TEXT, "
               "parent_session_id TEXT)")
    db.execute("INSERT OR REPLACE INTO sessions (id, source, title, session_key, model_config) VALUES (?, ?, ?, ?, ?)",
               (session_id, source, title, key or None, model_config))
    db.commit()
    db.close()
    room_rules.forget()


def _start(session_id):
    return call("work_start", {"title": "Login bug", "instructions": "Find out why the login page is slow."},
                session_id=session_id)


def _refused_shape(answer):
    assert answer["error"] == "not_bot_chat"
    assert set(answer) >= {"source", "platform", "chat_type", "profile"}
    return answer


def test_the_shipped_list_names_the_four_entry_points():
    names = {entry.name for entry in entries.load(None)}
    assert {"focal-point-primary", "focal-banger-voice", "hermes-desktop", "conduit-iphone"} <= names


def test_a_bot_chat_still_submits_work(home, hermes):
    started = _start(BOT_CHAT)
    assert started["ok"] and started["ref"] == "W1"
    assert "source" not in started


def test_the_focal_point_session_submits_work_without_the_bot_chat_title(home, hermes):
    _session(home, FOCAL, source="tui", key=f"agent:main:local:dm:{FOCAL}", title="Focal Point")
    started = _start(FOCAL)
    assert started["ok"] and store_mod.Store(home).get("W1").coordinator == FOCAL


def test_the_focal_point_session_still_matches_when_its_row_has_no_gateway_key(home, hermes):
    # source tui, platform local: the local platform is filled in when the
    # old row has no session key.
    _session(home, FOCAL, source="tui", title="Focal Point")
    found = entries.observe(FOCAL, home)
    assert found.inferred == ("platform", "chat_type") and found.platform == "local"
    assert _start(FOCAL)["ok"]


def test_the_voice_tunnel_submits_work(home, hermes):
    _session(home, VOICE, source="api_server", key=f"agent:main:api_server:dm:{VOICE}", title="Banger")
    started = _start(VOICE)
    assert started["ok"] and store_mod.Store(home).get("W1").coordinator == VOICE


def test_the_desktop_app_submits_work(home, hermes):
    # No gateway key: tui_gateway stores source desktop and the turn's
    # platform is filled in as desktop.
    _session(home, DESKTOP, source="desktop", title="Mac")
    assert entries.observe(DESKTOP, home).inferred == ("platform", "chat_type")
    assert _start(DESKTOP)["ok"]


def test_a_desktop_session_the_gateway_keyed_as_local_submits_work(home, hermes):
    _session(home, DESKTOP, source="desktop", key=f"agent:main:local:dm:{DESKTOP}")
    found = entries.observe(DESKTOP, home)
    assert (found.platform, found.chat_type, found.inferred) == ("local", "dm", ())
    assert _start(DESKTOP)["ok"]


def test_the_conduit_app_submits_work(home, hermes):
    # gateway_chats stores source gui; the authority keys the chat as a local dm.
    _session(home, PHONE, source="gui", key=f"agent:main:local:dm:{PHONE}", title="iPhone")
    assert _start(PHONE)["ok"]


def test_a_conduit_turn_bound_as_gui_submits_work(home, hermes, monkeypatch):
    _session(home, PHONE, source="gui", title="iPhone")
    monkeypatch.setattr(entries, "_turn_platform", lambda: "gui")
    found = entries.observe(PHONE, home)
    assert (found.raw_platform, found.platform, found.inferred) == ("gui", "gui", ("chat_type",))
    assert _start(PHONE)["ok"]


def test_a_gui_row_with_no_key_and_no_bound_platform_submits_work(home, hermes):
    # SURFACES maps gui onto the desktop surface, which is the fill-in.
    _session(home, PHONE, source="gui", title="iPhone")
    found = entries.observe(PHONE, home)
    assert found.platform == "desktop" and "platform" in found.inferred
    assert _start(PHONE)["ok"]


def test_another_api_server_chat_is_refused_and_names_what_it_saw(home, hermes):
    other = "11111111-1111-1111-1111-111111111111"
    _session(home, other, source="api_server", key=f"agent:main:api_server:dm:{other}")
    refused = _refused_shape(_start(other))
    assert (refused["source"], refused["platform"], refused["chat_type"], refused["profile"]) == (
        "api_server", "api_server", "dm", "default")
    assert store_mod.Store(home).list() == []


def test_a_voice_row_without_a_chat_type_is_refused(home, hermes, monkeypatch):
    _session(home, VOICE, source="api_server")
    monkeypatch.setattr(entries, "_turn_platform", lambda: "api_server")
    refused = _refused_shape(_start(VOICE))
    assert refused["source"] == "api_server" and refused["platform"] == "api_server"
    assert refused["chat_type"] == "" and refused["profile"] == "default"


def test_a_fork_of_a_listed_chat_is_refused(home, hermes, monkeypatch):
    _session(home, VOICE, source="api_server", key=f"agent:main:api_server:dm:{VOICE}")
    monkeypatch.setattr(work, "_is_child", lambda session_id: True)
    refused = _refused_shape(_start(VOICE))
    assert refused["source"] == "api_server"
    assert store_mod.Store(home).list() == []


def test_a_subagent_that_kept_the_desktop_source_is_refused(home, hermes):
    _session(home, "child-1", source="desktop", key="agent:main:local:dm:child-1",
             model_config=json.dumps({"_delegate_from": DESKTOP}))
    refused = _refused_shape(_start("child-1"))
    assert refused["delegated"] == "true"
    assert (refused["source"], refused["platform"], refused["chat_type"]) == ("desktop", "local", "dm")
    assert store_mod.Store(home).list() == []


def test_a_session_whose_source_is_subagent_is_refused(home, hermes):
    _session(home, "child-2", source="subagent", key="agent:main:local:dm:child-2")
    assert _refused_shape(_start("child-2"))["delegated"] == "true"


def test_a_multi_bot_room_is_refused_even_when_its_source_is_listed(home, hermes, monkeypatch):
    _session(home, DESKTOP, source="desktop")
    monkeypatch.setattr(room_rules, "room_turn", lambda session_id, home=None: room_rules.RoomTurn(
        room_id="room-1", side="home", with_others=True, session_id=str(session_id)))
    refused = _start(DESKTOP)
    assert refused["error"] == "room_with_others"
    assert store_mod.Store(home).list() == []


def test_an_unknown_source_is_refused_and_names_what_it_saw(home, hermes):
    _session(home, "tg-1", source="telegram", key="agent:main:telegram:dm:99")
    refused = _refused_shape(_start("tg-1"))
    assert (refused["source"], refused["platform"], refused["chat_type"], refused["profile"]) == (
        "telegram", "telegram", "dm", "default")
    assert "Saw source telegram, platform telegram, chat type dm, profile default." in refused["note"]


@pytest.mark.parametrize("source", ["push", "conduit"])
def test_a_guessed_phone_source_the_gateway_does_not_store_is_refused(home, hermes, source):
    _session(home, "phone-guess", source=source, key=f"agent:main:{source}:dm:phone-guess")
    refused = _refused_shape(_start("phone-guess"))
    assert refused["source"] == source and refused["chat_type"] == "dm"


def test_a_desktop_group_is_not_a_listed_dm(home, hermes):
    _session(home, "group-1", source="desktop", key="agent:main:local:group:group-1")
    refused = _refused_shape(_start("group-1"))
    assert refused["chat_type"] == "group" and refused["source"] == "desktop"


def test_a_listed_shape_on_another_profile_is_refused(home, hermes, monkeypatch):
    nahida = home / "profiles" / "nahida"
    nahida.mkdir(parents=True)
    _session(nahida, DESKTOP, source="desktop")
    monkeypatch.setattr(work, "_home", lambda: nahida)
    refused = _refused_shape(_start(DESKTOP))
    assert refused["profile"] == "nahida" and refused["source"] == "desktop"


def test_a_session_key_from_another_profile_does_not_match(home, hermes):
    _session(home, DESKTOP, source="desktop", key=f"agent:nahida:local:dm:{DESKTOP}")
    refused = _refused_shape(_start(DESKTOP))
    assert refused["profile"] == "default" and refused["platform"] == "local"


def test_an_override_replaces_the_shipped_list(home, hermes):
    _session(home, VOICE, source="api_server", key=f"agent:main:api_server:dm:{VOICE}")
    _session(home, DESKTOP, source="desktop")
    path = home / "conduit" / "work-entry-points.yaml"
    path.parent.mkdir()
    path.write_text(
        "entries:\n"
        "  - name: only-voice\n"
        "    source: api_server\n"
        "    platform: api_server\n"
        "    chat_type: dm\n"
        "    profile: default\n"
        "    session_ids:\n"
        f"      - {VOICE}\n")
    assert _start(VOICE)["ok"]
    assert _start(DESKTOP)["error"] == "not_bot_chat"


def test_a_broken_override_does_not_widen_the_list(home, hermes):
    _session(home, "stranger", source="telegram", key="agent:main:telegram:dm:1")
    path = home / "conduit" / "work-entry-points.yaml"
    path.parent.mkdir()
    path.write_text("entries:\n  - name: everything\n    source: '*'\n    platform: local\n"
                    "    chat_type: dm\n    profile: default\n")
    assert _start("stranger")["error"] == "not_bot_chat"
    assert _start(BOT_CHAT)["ok"]


def test_an_override_can_name_a_hyphenated_profile(home, hermes, monkeypatch):
    side = home / "profiles" / "focal-point"
    side.mkdir(parents=True)
    _session(side, "desk", source="desktop", key="agent:focal-point:local:dm:desk")
    path = side / "conduit" / "work-entry-points.yaml"
    path.parent.mkdir()
    path.write_text("entries:\n"
                    "  - name: side-desktop\n"
                    "    source: desktop\n"
                    "    platform: local\n"
                    "    chat_type: dm\n"
                    "    profile: focal-point\n")
    monkeypatch.setattr(work, "_home", lambda: side)
    assert _start("desk")["ok"]


def test_an_empty_override_allows_no_extra_chat(home, hermes):
    _session(home, DESKTOP, source="desktop")
    path = home / "conduit" / "work-entry-points.yaml"
    path.parent.mkdir()
    path.write_text("entries: []\n")
    assert _start(DESKTOP)["error"] == "not_bot_chat"
    assert _start(BOT_CHAT)["ok"]
