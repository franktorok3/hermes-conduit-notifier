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


def _session(home, session_id, *, source, key="", title="Chat", model_config=None, user_id="", chat_type="",
             client_peer=None, client_peer_node=None):
    path = Path(home) / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS sessions ("
               "id TEXT PRIMARY KEY, source TEXT, title TEXT, session_key TEXT, model_config TEXT, "
               "parent_session_id TEXT, user_id TEXT, chat_type TEXT, client_peer TEXT, client_peer_node TEXT)")
    db.execute("INSERT OR REPLACE INTO sessions (id, source, title, session_key, model_config, user_id, chat_type, "
               "client_peer, client_peer_node) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (session_id, source, title, key or None, model_config, user_id or None, chat_type or None,
                client_peer, client_peer_node))
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


IPHONE = "100.66.76.84"
MAC = "100.71.31.105"


def test_the_shipped_list_enables_only_the_pinned_sessions():
    listed = entries.load(None)
    names = {entry.name for entry in listed}
    assert {"focal-point-primary", "focal-banger-voice", "hermes-desktop", "conduit-iphone"} <= names
    enabled = {entry.name for entry in listed if entry.enabled and (entry.session_ids or entry.user_ids)}
    assert enabled == {"focal-point-primary", "focal-banger-voice"}
    by_name = {entry.name: entry for entry in listed}
    for name in ("hermes-desktop", "conduit-iphone"):
        assert not by_name[name].enabled
        assert by_name[name].user_ids == ()
        assert by_name[name].peer_nodes == ()
    # IT named these addresses. They are not verified, and the entries stay off.
    assert by_name["conduit-iphone"].peer_ips == (IPHONE,)
    assert by_name["hermes-desktop"].peer_ips == (MAC,)
    assert by_name["focal-point-primary"].peer_ips == ()
    assert by_name["focal-banger-voice"].peer_ips == ()


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


class _Sock:
    def __init__(self, host, port):
        self._endpoint = (host, port)

    def getpeername(self):
        return self._endpoint


class _Raw:
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, key):
        return self._sock if key == "socket" else None


class _Poison:
    """A client-supplied header or ws.client. Reading it is a failure."""

    def __getattr__(self, name):
        raise AssertionError(f"client-supplied {name} was read")

    def __getitem__(self, key):
        raise AssertionError(f"client-supplied {key!r} was read")

    def __contains__(self, key):
        raise AssertionError(f"client-supplied {key!r} was read")

    def __iter__(self):
        raise AssertionError("client-supplied headers were read")

    def get(self, *args, **kwargs):
        raise AssertionError("client-supplied headers were read")

    def items(self):
        raise AssertionError("client-supplied headers were read")


class _Upgrade:
    """The WebSocket upgrade Hermes' WSTransport holds. ``endpoint`` is the
    TCP peer getpeername would return. ``label`` is the server's ``_peer``
    string, the value logged as ``ws accepted peer=``. Headers and
    ``ws.client`` are poisoned."""

    def __init__(self, *, endpoint=None, label="", node=""):
        self._peer = label
        self.server_peer_node = node
        scope = {}
        if endpoint is not None:
            scope["extensions"] = {"transport": _Raw(_Sock(*endpoint))}
        self._ws = types.SimpleNamespace(headers=_Poison(), scope=scope, client=_Poison())


class _Fanout:
    def __init__(self, peers):
        self._list = list(peers)

    def transports(self):
        return list(self._list)


def _bind(hermes, transport):
    hermes._current.set(transport)


def _desktop_peer_list(home, *, enabled=True, user_ids=("uid:1000",), peer_ips=(MAC,), peer_nodes=(),
                       session_ids=()):
    users = "".join(f"      - \"{value}\"\n" for value in user_ids)
    ips = "".join(f"      - \"{value}\"\n" for value in peer_ips)
    nodes = "".join(f"      - \"{value}\"\n" for value in peer_nodes)
    sessions = "".join(f"      - \"{value}\"\n" for value in session_ids)
    text = ("capture: false\n"
            "entries:\n"
            "  - name: focal-point-primary\n"
            "    source: tui\n"
            "    profile: default\n"
            "    session_ids:\n"
            f"      - {FOCAL}\n"
            "  - name: hermes-desktop\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            "    source: desktop\n"
            "    profile: default\n")
    if users:
        text += "    user_ids:\n" + users
    if ips:
        text += "    peer_ips:\n" + ips
    if nodes:
        text += "    peer_nodes:\n" + nodes
    if sessions:
        text += "    session_ids:\n" + sessions
    _write_override(home, text)


def _forbid_processes(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("peer matching spawned a process")

    monkeypatch.setattr("subprocess.run", boom)
    monkeypatch.setattr("subprocess.Popen", boom)
    monkeypatch.setattr("os.system", boom)


def test_a_server_observed_peer_matches_only_together_with_the_user_id(home, hermes, monkeypatch):
    _forbid_processes(monkeypatch)
    _focal(home)
    _desktop_peer_list(home)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _session(home, "other-user", source="desktop", user_id="basic:frank")
    _session(home, "other-peer", source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=(MAC, 54321), label="127.0.0.1:0", node=""))
    started = _start(DESKTOP)
    assert started["ok"] and store_mod.Store(home).get("W1").coordinator == FOCAL
    assert entries.observe(DESKTOP, home).peer == f"{MAC}:54321"
    assert entries.observe(DESKTOP, home).ws_client == "127.0.0.1:0"
    _bind(hermes, _Upgrade(endpoint=(IPHONE, 54321), label=f"{MAC}:54321"))
    assert _start("other-peer")["error"] == "not_bot_chat"
    _bind(hermes, _Upgrade(endpoint=(MAC, 4000), label=f"{MAC}:4000"))
    assert _start("other-user")["error"] == "not_bot_chat"
    assert [row.ref for row in store_mod.Store(home).list()] == ["W1"]


@pytest.mark.parametrize("endpoint,label", [
    (None, ""),
    (None, f"{MAC}:0"),
    (None, f"{MAC}:54321"),
    (("127.0.0.1", 4321), f"{MAC}:54321"),
    (("::1", 4321), f"{MAC}:54321"),
    ((MAC, 0), f"{MAC}:0"),
    (("0.0.0.0", 4321), f"{MAC}:4321"),
    (("::ffff:127.0.0.1", 4321), f"{MAC}:54321"),
])
def test_a_missing_empty_loopback_or_rewritten_peer_does_not_match(home, hermes, endpoint, label):
    _focal(home)
    _desktop_peer_list(home)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=endpoint, label=label))
    refused = _start(DESKTOP)
    assert refused["error"] == "not_bot_chat"
    assert store_mod.Store(home).list() == []


def test_spoofed_forwarded_headers_are_not_the_peer(home, hermes):
    _focal(home)
    _desktop_peer_list(home)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    # No socket. The label is the logged ws client, which is what a proxy
    # rewrite of X-Forwarded-For looks like, including a client-chosen port.
    _bind(hermes, _Upgrade(label=f"{MAC}:54321"))
    found = entries.observe(DESKTOP, home)
    assert found.peer == "" and found.ws_client == f"{MAC}:54321"
    assert _start(DESKTOP)["error"] == "not_bot_chat"
    _bind(hermes, _Upgrade(endpoint=("203.0.113.5", 4000), label=f"{MAC}:4000"))
    assert entries.observe(DESKTOP, home).peer == "203.0.113.5:4000"
    assert _start(DESKTOP)["error"] == "not_bot_chat"
    assert store_mod.Store(home).list() == []


def test_an_ipv4_mapped_socket_matches_the_listed_ipv4(home, hermes):
    # A dual-stack listener reports an IPv4 client as ::ffff:a.b.c.d.
    _focal(home)
    _desktop_peer_list(home)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=("::ffff:100.71.31.105", 54321)))
    assert _start(DESKTOP)["ok"]


def test_listing_loopback_does_not_make_a_proxy_peer_match(home, hermes):
    _desktop_peer_list(home, peer_ips=("127.0.0.1",))
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=("127.0.0.1", 4321), label=f"{MAC}:0"))
    assert _start(DESKTOP)["error"] == "not_bot_chat"


def test_a_disabled_entry_does_not_match_a_peer(home, hermes):
    _desktop_peer_list(home, enabled=False)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=(MAC, 54321), label=f"{MAC}:54321"))
    assert _start(DESKTOP)["error"] == "not_bot_chat"
    assert store_mod.Store(home).list() == []


def test_peer_matchers_do_not_authorize_without_a_user_id(home, hermes):
    _desktop_peer_list(home, user_ids=(), peer_ips=(MAC,), session_ids=(DESKTOP,))
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=(MAC, 54321)))
    assert _start(DESKTOP)["error"] == "not_bot_chat"


def test_a_peer_node_matches_only_with_the_user_and_is_not_looked_up(home, hermes, monkeypatch):
    _forbid_processes(monkeypatch)
    _focal(home)
    _desktop_peer_list(home, peer_ips=(), peer_nodes=("iphone",))
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _session(home, "other", source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=(IPHONE, 1111), node="iPhone"))
    assert _start(DESKTOP)["ok"]
    _bind(hermes, _Upgrade(endpoint=(IPHONE, 1111), node="macbook-air"))
    assert _start("other")["error"] == "not_bot_chat"
    _bind(hermes, _Upgrade(endpoint=(IPHONE, 1111), node=""))
    assert entries.allows("other", home) is False


def test_peer_ip_and_peer_node_must_both_match_when_both_are_listed(home, hermes):
    _focal(home)
    _desktop_peer_list(home, peer_ips=(IPHONE,), peer_nodes=("iphone",))
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _session(home, "wrong-node", source="desktop", user_id="uid:1000")
    _session(home, "wrong-ip", source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=(IPHONE, 1111), node="iphone"))
    assert _start(DESKTOP)["ok"]
    _bind(hermes, _Upgrade(endpoint=(IPHONE, 1111), node="macbook-air"))
    assert _start("wrong-node")["error"] == "not_bot_chat"
    _bind(hermes, _Upgrade(endpoint=(MAC, 1111), node="iphone"))
    assert _start("wrong-ip")["error"] == "not_bot_chat"


def test_a_stored_row_peer_is_used_only_when_no_socket_is_attached(home, hermes):
    _focal(home)
    _desktop_peer_list(home)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000", client_peer=MAC)
    assert _start(DESKTOP)["ok"]
    _session(home, "rewritten", source="desktop", user_id="uid:1000", client_peer=f"{MAC}:0")
    _session(home, "loop", source="desktop", user_id="uid:1000", client_peer="127.0.0.1")
    _session(home, "blank", source="desktop", user_id="uid:1000", client_peer="")
    assert _start("rewritten")["error"] == "not_bot_chat"
    assert _start("loop")["error"] == "not_bot_chat"
    assert _start("blank")["error"] == "not_bot_chat"


def test_a_live_loopback_socket_is_not_replaced_by_a_stored_peer(home, hermes):
    _desktop_peer_list(home)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000", client_peer=MAC,
             client_peer_node="macbook-air")
    _bind(hermes, _Upgrade(endpoint=("127.0.0.1", 9), label=f"{MAC}:54321", node="macbook-air"))
    found = entries.observe(DESKTOP, home)
    assert found.peer == "127.0.0.1:9" and found.peer_node == ""
    assert _start(DESKTOP)["error"] == "not_bot_chat"


def test_a_stored_node_matches_and_a_loopback_row_drops_it(home, hermes):
    _focal(home)
    _desktop_peer_list(home, peer_ips=(), peer_nodes=("iphone",))
    _session(home, DESKTOP, source="desktop", user_id="uid:1000", client_peer_node="iphone")
    assert _start(DESKTOP)["ok"]
    _session(home, "proxied", source="desktop", user_id="uid:1000", client_peer="127.0.0.1",
             client_peer_node="iphone")
    assert _start("proxied")["error"] == "not_bot_chat"


def test_two_live_sockets_are_not_one_peer(home, hermes):
    _desktop_peer_list(home)
    _session(home, DESKTOP, source="desktop", user_id="uid:1000", client_peer=MAC)
    _bind(hermes, _Fanout([
        _Upgrade(endpoint=(MAC, 1111), node="macbook-air"),
        _Upgrade(endpoint=(IPHONE, 2222), node="iphone"),
    ]))
    assert entries.observe(DESKTOP, home).peer == ""
    assert _start(DESKTOP)["error"] == "not_bot_chat"


def test_primary_voice_and_bot_chat_ignore_an_unusable_live_peer(home, hermes):
    _focal(home)
    _voice(home)
    _bind(hermes, _Upgrade(endpoint=("127.0.0.1", 9), label="127.0.0.1:0"))
    assert entries.allows(FOCAL, home) and entries.allows(VOICE, home)
    assert not entries.allows(DESKTOP, home)
    started = _start(BOT_CHAT)
    assert started["ok"] and "source" not in started


def test_a_refused_peer_is_logged_while_capture_is_on(home, hermes, caplog):
    _session(home, DESKTOP, source="desktop", user_id="uid:1000")
    _bind(hermes, _Upgrade(endpoint=("127.0.0.1", 9), label=f"{MAC}:0"))
    with caplog.at_level(logging.INFO, logger=entries.logger.name):
        refused = _start(DESKTOP)
    assert refused["error"] == "not_bot_chat"
    assert "Peer 127.0.0.1:9." in refused["note"]
    assert any("peer=127.0.0.1:9" in record.message and f"ws_client={MAC}:0" in record.message
               for record in caplog.records)
