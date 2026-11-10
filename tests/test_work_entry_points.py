"""Trusted entry points may submit work; forks, subagents, multi-bot rooms and unknown sources may not."""
import json
import logging
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


def _session(home, session_id, *, source, key="", title="Chat", model_config=None, user_id="", chat_type=""):
    path = Path(home) / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS sessions ("
               "id TEXT PRIMARY KEY, source TEXT, title TEXT, session_key TEXT, model_config TEXT, "
               "parent_session_id TEXT, user_id TEXT, chat_type TEXT)")
    db.execute("INSERT OR REPLACE INTO sessions (id, source, title, session_key, model_config, user_id, chat_type) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (session_id, source, title, key or None, model_config, user_id or None, chat_type or None))
    db.commit()
    db.close()
    room_rules.forget()


def _voice(home):
    _session(home, VOICE, source="api_server", key=f"agent:main:api_server:dm:{VOICE}", title="Banger")


def _focal(home):
    _session(home, FOCAL, source="tui", title="Focal Point")


def _start(session_id):
    return call("work_start", {"title": "Login bug", "instructions": "Find out why the login page is slow."},
                session_id=session_id)


def _refused_shape(answer):
    assert answer["error"] == "not_bot_chat"
    assert set(answer) >= {"source", "platform", "chat_type", "profile"}
    return answer


def test_the_shipped_list_enables_only_the_pinned_sessions():
    listed = entries.load(None)
    names = {entry.name for entry in listed}
    assert {"focal-point-primary", "focal-banger-voice", "hermes-desktop", "conduit-iphone"} <= names
    enabled = {entry.name for entry in listed if entry.enabled and (entry.session_ids or entry.user_ids)}
    assert enabled == {"focal-point-primary", "focal-banger-voice"}
    assert all(not entry.enabled for entry in listed if entry.name in {"hermes-desktop", "conduit-iphone"})


def test_a_bot_chat_still_submits_work(home, hermes):
    started = _start(BOT_CHAT)
    assert started["ok"] and started["ref"] == "W1"
    assert "source" not in started


def test_the_focal_point_session_submits_work_without_the_bot_chat_title(home, hermes):
    _focal(home)
    started = _start(FOCAL)
    assert started["ok"] and store_mod.Store(home).get("W1").coordinator == FOCAL


def test_the_focal_point_session_matches_with_no_gateway_key_and_an_empty_chat_type(home, hermes):
    # The pin is the session id and source tui. An empty chat type is not
    # rewritten to dm, and no platform is invented.
    _focal(home)
    found = entries.observe(FOCAL, home)
    assert (found.platform, found.chat_type, found.source) == ("", "", "tui")
    assert _start(FOCAL)["ok"]


def test_the_voice_tunnel_submits_work_through_the_focal_point_coordinator(home, hermes):
    _focal(home)
    _voice(home)
    started = _start(VOICE)
    assert started["ok"] and "deduped" not in started
    row = store_mod.Store(home).get("W1")
    assert row.coordinator == FOCAL
    assert row.reply_to == {"to": "coordinator", "session": FOCAL}


def test_the_same_request_from_two_entry_points_does_not_start_twice(home, hermes):
    _focal(home)
    _voice(home)
    assert _start(VOICE)["ok"]
    # The primary's own second copy is the similar-title refusal.
    again = _start(FOCAL)
    assert again["error"] == "similar_work"
    assert [row.ref for row in store_mod.Store(home).list()] == ["W1"]
    # new=true from the other entry point still does not open a second one.
    forced = call("work_start", {"title": "Login bug", "instructions": "Find out why the login page is slow.",
                                 "new": True}, session_id=VOICE)
    assert forced["deduped"] is True and forced["ref"] == "W1"
    assert [row.ref for row in store_mod.Store(home).list()] == ["W1"]


def test_two_different_requests_both_belong_to_the_primary(home, hermes):
    _focal(home)
    _voice(home)
    assert _start(VOICE)["ok"]
    other = call("work_start", {"title": "Mailbox", "instructions": "Read the new mail."}, session_id=FOCAL)
    assert other["ok"] and other["ref"] == "W2"
    rows = {row.ref: row for row in store_mod.Store(home).list()}
    assert rows["W1"].coordinator == FOCAL and rows["W2"].coordinator == FOCAL


def test_desktop_and_conduit_are_refused_until_a_login_is_pinned(home, hermes):
    # Host shape: a new id every chat, empty chat type, no session key.
    _session(home, DESKTOP, source="desktop", user_id="uid:1000", title="Mac")
    _session(home, PHONE, source="desktop", user_id="basic:frank", title="iPhone")
    _session(home, "voice-saved", source="desktop", title="Call")  # a saved transcript, no login
    for session_id in (DESKTOP, PHONE, "voice-saved"):
        assert _start(session_id)["error"] == "not_bot_chat"
    assert store_mod.Store(home).list() == []


def test_a_desktop_entry_pinned_to_the_authenticated_login_accepts_only_that_login(home, hermes):
    _focal(home)
    _write_override(home, "capture: false\n"
                    "entries:\n"
                    "  - name: focal-point-primary\n"
                    "    source: tui\n"
                    "    profile: default\n"
                    "    session_ids:\n"
                    f"      - {FOCAL}\n"
                    "  - name: hermes-desktop\n"
                    "    enabled: true\n"
                    "    source: desktop\n"
                    "    profile: default\n"
                    "    user_ids:\n"
                    "      - \"uid:1000\"\n")
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _session(home, "older", source="desktop", user_id="basic:frank")
    _session(home, "voice-saved", source="desktop")
    started = _start(DESKTOP)
    assert started["ok"] and store_mod.Store(home).get("W1").coordinator == FOCAL
    assert _start("older")["error"] == "not_bot_chat"
    assert _start("voice-saved")["error"] == "not_bot_chat"
    assert [row.ref for row in store_mod.Store(home).list()] == ["W1"]


def test_enabling_desktop_without_a_login_or_session_id_authorizes_nothing(home, hermes):
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _write_override(home, "entries:\n"
                    "  - name: hermes-desktop\n"
                    "    enabled: true\n"
                    "    source: desktop\n"
                    "    profile: default\n")
    assert _start(DESKTOP)["error"] == "not_bot_chat"


def test_the_turn_platform_is_client_influenced_and_not_used_for_authorization(home, hermes, monkeypatch):
    # session.create's source is what the gateway copies into
    # HERMES_SESSION_PLATFORM. A client chooses that source, so the context
    # value is not an authorization input: a voice row with no key stays
    # refused even when the turn claims platform api_server.
    context = types.ModuleType("gateway.session_context")
    context.get_session_env = lambda name, default="": "api_server" if name == "HERMES_SESSION_PLATFORM" else default
    monkeypatch.setitem(sys.modules, "gateway.session_context", context)
    assert entries._turn_platform() == "api_server"
    _session(home, VOICE, source="api_server")
    refused = _refused_shape(_start(VOICE))
    assert refused["platform"] == "" and refused["source"] == "api_server"
    assert entries.observe(VOICE, home).platform == ""


def test_another_api_server_chat_is_refused_and_names_what_it_saw(home, hermes):
    other = "11111111-1111-1111-1111-111111111111"
    _session(home, other, source="api_server", key=f"agent:main:api_server:dm:{other}")
    refused = _refused_shape(_start(other))
    assert (refused["source"], refused["platform"], refused["chat_type"], refused["profile"]) == (
        "api_server", "api_server", "dm", "default")
    assert store_mod.Store(home).list() == []


def test_a_voice_row_without_a_chat_type_is_refused(home, hermes, monkeypatch):
    monkeypatch.setattr(entries, "_turn_platform", lambda: "api_server")
    _session(home, VOICE, source="api_server")
    refused = _refused_shape(_start(VOICE))
    assert refused["source"] == "api_server" and refused["platform"] == ""
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
    assert "Saw source telegram, platform telegram, chat type dm, profile default, user_id unknown." in refused["note"]


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
    _write_override(side, "entries:\n"
                    "  - name: side-desktop\n"
                    "    source: desktop\n"
                    "    platform: local\n"
                    "    chat_type: dm\n"
                    "    profile: focal-point\n"
                    "    session_ids:\n"
                    "      - desk\n")
    monkeypatch.setattr(work, "_home", lambda: side)
    assert _start("desk")["ok"]


def test_a_pinned_session_on_another_profile_is_refused(home, hermes, monkeypatch):
    nahida = home / "profiles" / "nahida"
    nahida.mkdir(parents=True)
    _voice(nahida)
    monkeypatch.setattr(work, "_home", lambda: nahida)
    refused = _refused_shape(_start(VOICE))
    assert refused["profile"] == "nahida" and refused["source"] == "api_server"


def test_a_refused_desktop_turn_is_logged_while_capture_is_on(home, hermes, caplog):
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    with caplog.at_level(logging.INFO, logger=entries.logger.name):
        refused = _start(DESKTOP)
    assert refused["error"] == "not_bot_chat" and refused["user_id"] == "uid:1000"
    assert any("work refused" in record.message and "user_id=uid:1000" in record.message
               and "source=desktop" in record.message and "chat_type=-" in record.message
               for record in caplog.records)


def test_capture_off_does_not_log_a_refusal(home, hermes, caplog):
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _write_override(home, "capture: false\nentries: []\n")
    with caplog.at_level(logging.INFO, logger=entries.logger.name):
        assert _start(DESKTOP)["error"] == "not_bot_chat"
    assert not any("work refused" in record.message for record in caplog.records)


def _write_override(home, text):
    path = Path(home) / "conduit" / "work-entry-points.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_an_empty_override_allows_no_extra_chat(home, hermes):
    _session(home, DESKTOP, source="desktop")
    path = home / "conduit" / "work-entry-points.yaml"
    path.parent.mkdir()
    path.write_text("entries: []\n")
    assert _start(DESKTOP)["error"] == "not_bot_chat"
    assert _start(BOT_CHAT)["ok"]
