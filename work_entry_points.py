"""Chats, besides a Bot Chat, that may submit work.

The list is ``work_entry_points.yaml`` beside this file. A regular file at
``<profile home>/conduit/work-entry-points.yaml`` replaces it for that
profile. Nothing here opens a socket, looks up a name, or reads a network.
The peer, when there is one, is read off a socket the gateway already accepted.

An entry authorizes a turn only when its source and profile match and it
names a session id or a ``user_id``. Source alone is not enough: a client
chooses ``source`` on ``session.create``. ``user_id`` is the login Hermes
stores from the authenticated transport
(``tui_gateway`` ``_transport_auth_user_id``), not from the create params.
An empty ``user_id`` never matches.

An entry may also list ``peer_ips`` or ``peer_nodes``. They match only
together with ``user_ids``, never instead of them. The peer is the TCP
address of this turn's WebSocket socket (``getpeername``). When this
process has no socket, ``client_peer`` and ``client_peer_node`` on the
session row are used if Hermes stored them there. The value logged as
``ws accepted peer=`` is not a match: in gated mode uvicorn's proxy
headers replace ``ws.client`` with ``X-Forwarded-For``, and a bare
address gets port 0. A missing, empty, loopback, unspecified, or port-0
peer does not match, and neither does a node carried on such a peer.
Nothing here reads ``X-Forwarded-For``, ``X-Real-IP``, ``Forwarded``,
or ``X-Conduit-Client``, and nothing runs ``tailscale whois``. A node
is used only when the socket's transport or the session row already
carries one the host stored.

Platform and chat type come only from the gateway session key on the row
(``agent:<profile>:<platform>:<chat type>:<chat id>``), or from the row's
``chat_type`` column when the key has none. They are never taken from
``HERMES_SESSION_PLATFORM``. That context value is the gateway's copy of
the session source, and the client sends the source, so a client can
influence it. ``_turn_platform`` reads it for tests and is not an
authorization input.

A fork or a delegated child never matches. With ``capture: true``, a
refused turn logs the raw identity, the socket peer, and the logged ws
client, so a login can be pinned later. The logged client is not a match.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
# Homes whose override has already been reported, so a tool call doesn't log it again.
_logged_override: set[str] = set()

FILE_NAME = "work_entry_points.yaml"
OVERRIDE_NAME = "work-entry-points.yaml"
# The shared-workspace coordinator. Other listed chats submit into its registry.
PRIMARY_ENTRY = "focal-point-primary"
_OVERRIDE_MAX = 64 * 1024
_MAX_ENTRIES = 32
_MAX_IDS = 16
_ID_MAX = 256
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A Hermes profile name (hermes_cli.profiles.normalize_profile_name), lowercased.
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLUMNS = ("id", "source", "session_key", "model_config", "user_id", "chat_type",
            "client_peer", "client_peer_node")
# A Tailscale-style DNS name the host stored. Not an IP, and not "unknown".
_NODE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_NOT_A_NODE = frozenset({"unknown", "localhost"})


@dataclass(frozen=True)
class Entry:
    name: str
    source: str
    profile: str
    enabled: bool = True
    platform: str = ""
    chat_type: str = ""
    session_ids: tuple[str, ...] = ()
    user_ids: tuple[str, ...] = ()
    peer_ips: tuple[str, ...] = ()
    peer_nodes: tuple[str, ...] = ()

    def matches(self, found: "Seen") -> bool:
        """Whether this entry authorizes ``found``. Disabled entries, and
        entries with neither a session id nor a user id, never do. Peer
        lists do not stand in for the user id."""
        if not self.enabled or found.delegated or found.profile_mismatch:
            return False
        if not self.session_ids and not self.user_ids:
            return False
        if found.source != self.source or found.profile != self.profile:
            return False
        if self.platform and found.platform != self.platform:
            return False
        if self.chat_type and found.chat_type != self.chat_type:
            return False
        if self.session_ids:
            ids = {found.session_id, found.row_id, found.chat_id} - {""}
            if not ids.intersection(self.session_ids):
                return False
        if self.user_ids and found.user_id not in self.user_ids:
            return False
        if self.peer_ips or self.peer_nodes:
            if not self.user_ids or found.user_id not in self.user_ids:
                return False
            ip, node = _usable(found.peer, found.peer_node)
            if self.peer_ips and ip not in self.peer_ips:
                return False
            if self.peer_nodes and node not in self.peer_nodes:
                return False
        return True


@dataclass(frozen=True)
class Seen:
    """Identity read from the session row. ``platform`` is the session
    key's platform, or "" when the row has no key. ``chat_type`` is the
    key's chat type, else the row's column. Neither is filled in."""

    source: str = ""
    platform: str = ""
    chat_type: str = ""
    profile: str = ""
    user_id: str = ""
    session_id: str = ""
    row_id: str = ""
    chat_id: str = ""
    delegated: bool = False
    profile_mismatch: bool = False
    # ``peer`` is the socket, or the row, never the logged ws client.
    # ``ws_client`` is that log value, kept so a refusal can show it.
    peer: str = ""
    peer_node: str = ""
    ws_client: str = ""


def allows(session_id: Any, home: Path) -> bool:
    """Whether ``session_id`` is a listed entry point for ``home``."""
    found = observe(session_id, home)
    return any(entry.matches(found) for entry in load(home))


def primary_session_id(home: Optional[Path] = None) -> str:
    """The Focal Point session the other listed chats submit work through,
    or "" when that entry is missing or disabled."""
    for entry in load(home):
        if entry.name == PRIMARY_ENTRY and entry.enabled and entry.session_ids:
            return entry.session_ids[0]
    return ""


def seen(session_id: Any, home: Optional[Path]) -> dict[str, str]:
    """The source, platform, chat type, profile, user id and socket peer
    a refusal should report. The peer is the socket or the row, not the
    logged ws client."""
    found = observe(session_id, home) if home is not None else Seen()
    reported = {"source": found.source, "platform": found.platform, "chat_type": found.chat_type,
                "profile": found.profile, "user_id": found.user_id, "peer": found.peer,
                "peer_node": found.peer_node}
    if found.delegated:
        reported["delegated"] = "true"
    return reported


def record_refusal(session_id: Any, home: Optional[Path]) -> None:
    """Log the raw identity of a refused turn while capture is on.

    The line is how a desktop or phone login gets pinned: source, the
    stored platform and chat type, the profile, ``user_id``, the socket
    peer, and the logged ws client. No message text. The logged client
    is not a match."""
    if home is None or not _capture(home):
        return
    found = observe(session_id, home)
    logger.info("conduit_push: work refused source=%s platform=%s chat_type=%s profile=%s "
                "user_id=%s session=%s peer=%s peer_node=%s ws_client=%s",
                found.source or "-", found.platform or "-", found.chat_type or "-", found.profile or "-",
                found.user_id or "-", found.session_id or "-", found.peer or "-", found.peer_node or "-",
                found.ws_client or "-")


def observe(session_id: Any, home: Optional[Path]) -> Seen:
    """The turn's identity from the session row. An unreadable store is an
    empty source, which matches nothing. The turn's platform is not read."""
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if home is None:
        return Seen(session_id=sid)
    from . import work_broker

    profile = work_broker.profile_of(Path(home))
    row = _row(Path(home), sid)
    key = _parse_key(str(row.get("session_key") or ""))
    if key is None and _looks_like_key(sid):
        key = _parse_key(sid)
        if not row.get("source") and key is not None:
            row = _row_by_key(Path(home), sid) or row
            key = _parse_key(str(row.get("session_key") or "")) or key
    source = str(row.get("source") or "").strip().lower()
    platform = key[0] if key is not None else ""
    chat_type = key[1] if key is not None else str(row.get("chat_type") or "").strip().lower()
    chat_id = key[2] if key is not None else ""
    key_profile = key[3] if key is not None else ""
    peer, peer_node, ws_client = _observed(row)
    return Seen(
        source=source, platform=platform, chat_type=chat_type, profile=profile,
        user_id=str(row.get("user_id") or "").strip(), session_id=sid,
        row_id=str(row.get("id") or ""), chat_id=chat_id,
        delegated=_delegated(source, row.get("model_config")),
        profile_mismatch=bool(key_profile) and key_profile != profile,
        peer=peer, peer_node=peer_node, ws_client=ws_client)


def load(home: Optional[Path] = None) -> tuple[Entry, ...]:
    """The entries in force: the profile's override when it is a valid
    list, otherwise the shipped list. A missing override changes nothing.
    An override that can't be read or parsed is not applied."""
    text, override = _text(home)
    if text is None:
        logger.warning("conduit_push: shipped work entry points aren't usable; no extra chat can submit work")
        return ()
    parsed = _parse(text)
    if parsed is None:
        if override:
            logger.warning("conduit_push: work entry points override isn't usable; using the shipped list")
            shipped = _shipped_text()
            parsed = _parse(shipped) if shipped is not None else None
        if parsed is None:
            logger.warning("conduit_push: shipped work entry points aren't usable; no extra chat can submit work")
            return ()
    elif override:
        key = str(home)
        if key not in _logged_override:
            _logged_override.add(key)
            logger.info("conduit_push: work entry points for %s are the profile override", home)
    return parsed[0]


def _turn_platform() -> str:
    """``HERMES_SESSION_PLATFORM`` for this turn, or "" when nothing bound it.

    Not used for authorization. The messaging gateway sets the context var
    from the session's platform, and a desktop ``session.create`` chooses
    ``source``, which the gateway copies into that platform. A client can
    therefore influence the value. ``observe`` does not call this.
    """
    try:
        from . import reply_sources

        value = reply_sources.current_platform()
    except Exception:  # noqa: BLE001 — unreadable: still not an authorization input
        return ""
    return value.strip().lower() if isinstance(value, str) else ""


def _capture(home: Path) -> bool:
    text, override = _text(home)
    if text is None:
        return False
    data = _document(text)
    if data is None and override:
        data = _document(_shipped_text() or "")
    if not isinstance(data, dict) or "capture" not in data:
        return False
    return data.get("capture") is True


def _text(home: Optional[Path]) -> tuple[Optional[str], bool]:
    """``(text, from_override)``. The text is None when nothing can be read."""
    if home is not None:
        override = _override_text(Path(home))
        if override is not None:
            return override, True
    return _shipped_text(), False


def _shipped_text() -> Optional[str]:
    path = Path(__file__).resolve().parent / FILE_NAME
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("conduit_push: couldn't read %s", FILE_NAME, exc_info=True)
        return None


def _override_text(home: Path) -> Optional[str]:
    """The override's text, None when the profile has no file, "" when a
    file is there but must not be trusted (a symlink, or not a small
    regular file)."""
    path = Path(home) / "conduit" / OVERRIDE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _OVERRIDE_MAX:
            return ""
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read(_OVERRIDE_MAX + 1)
    except (OSError, UnicodeError):
        return ""
    finally:
        if fd >= 0:
            os.close(fd)


def _document(text: str) -> Optional[dict]:
    if not isinstance(text, str) or len(text) > _OVERRIDE_MAX:
        return None
    try:
        from . import session_titles

        data = session_titles._yaml_load(text)
    except Exception:  # noqa: BLE001 — an unreadable list allows nothing extra
        logger.debug("conduit_push: couldn't parse work entry points", exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _parse(text: str) -> Optional[tuple[tuple[Entry, ...], bool]]:
    data = _document(text)
    if data is None:
        return None
    entries = data.get("entries")
    if not isinstance(entries, list) or len(entries) > _MAX_ENTRIES:
        return None
    if "capture" in data and data.get("capture") is not True and data.get("capture") is not False:
        return None
    parsed: list[Entry] = []
    for item in entries:
        entry = _entry(item)
        if entry is None:
            return None
        parsed.append(entry)
    return tuple(parsed), data.get("capture") is True


def _entry(item: Any) -> Optional[Entry]:
    if not isinstance(item, dict):
        return None
    name = _token(item.get("name"), _NAME)
    source = _token(item.get("source"), _TOKEN)
    profile = _token(item.get("profile"), _PROFILE)
    if name is None or source is None or profile is None:
        return None
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        return None
    platform = _optional(item, "platform")
    chat_type = _optional(item, "chat_type")
    if platform is None or chat_type is None:
        return None
    session_ids = _id_list(item.get("session_ids", []))
    user_ids = _id_list(item.get("user_ids", []))
    peer_ips = _peer_ips(item.get("peer_ips", []))
    peer_nodes = _peer_nodes(item.get("peer_nodes", []))
    if session_ids is None or user_ids is None or peer_ips is None or peer_nodes is None:
        return None
    return Entry(name=name, source=source, profile=profile, enabled=enabled, platform=platform,
                 chat_type=chat_type, session_ids=tuple(session_ids), user_ids=tuple(user_ids),
                 peer_ips=peer_ips, peer_nodes=peer_nodes)


def _optional(item: dict, key: str) -> Optional[str]:
    """``""`` when the key is absent. None when it is present and not a token."""
    if key not in item or item[key] is None:
        return ""
    return _token(item[key], _TOKEN)


def _id_list(raw: Any) -> Optional[list[str]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > _MAX_IDS:
        return None
    values: list[str] = []
    for value in raw:
        if not _identity(value):
            return None
        values.append(value)
    return values


def _identity(value: Any) -> bool:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > _ID_MAX:
        return False
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _peer_ips(raw: Any) -> Optional[tuple[str, ...]]:
    """Canonical IPs, or None when the list is not a short list of IPs.
    A port, a name, or a range is rejected so the file is not applied."""
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > _MAX_IDS:
        return None
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str) or value != value.strip() or not value or len(value) > _ID_MAX:
            return None
        try:
            values.append(str(ipaddress.ip_address(value)))
        except ValueError:
            return None
    return tuple(values)


def _peer_nodes(raw: Any) -> Optional[tuple[str, ...]]:
    """Canonical node names, or None when one is not a node name."""
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > _MAX_IDS:
        return None
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str) or value != value.strip() or not value:
            return None
        node = _matchable_node(value)
        if not node:
            return None
        values.append(node)
    return tuple(values)


def _usable(peer: str, node: str) -> tuple[str, str]:
    """``(ip, node)`` that may satisfy a peer list. A peer that was seen
    and is not a real remote address drops the node too: a loopback or
    port-0 observation must not match by name."""
    ip = _matchable_ip(peer)
    if peer.strip() and not ip:
        return "", ""
    return ip, _matchable_node(node)


def _matchable_ip(raw: str) -> str:
    """The canonical IP in ``raw``, or "" when it is missing, not an
    address, loopback, unspecified, or port 0. A bare IP is kept. An
    ``host:port`` with a real port keeps the host."""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text or text.lower() in _NOT_A_NODE:
        return ""
    host, port = _split_peer(text)
    if host is None or port == 0:
        return ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return ""
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_loopback or addr.is_unspecified:
        return ""
    return str(addr)


def _matchable_node(raw: str) -> str:
    """A stored node name, lowercased, or "" when it is not one. An IP
    is not a node. ``unknown`` and ``localhost`` are not names."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip().lower().rstrip(".")
    if not text or text in _NOT_A_NODE or len(text) > 253 or not _NODE.fullmatch(text):
        return ""
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return text
    return ""


def _split_peer(text: str) -> tuple[Optional[str], Optional[int]]:
    """``(host, port)`` from ``ip``, ``ip:port``, or ``[ip]:port``.
    The port is None when the text is a bare address. The host is None
    when the shape is not one of those. An unbracketed IPv6 value is a
    bare address only when the whole text is an address."""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None, None
        host, rest = text[1:end], text[end + 1:]
        if not rest:
            return host, None
        if not rest.startswith(":") or not rest[1:].isdigit():
            return None, None
        port = int(rest[1:])
        return (host, port) if 0 <= port <= 65535 else (None, None)
    if text.count(":") == 1:
        host, port_text = text.rsplit(":", 1)
        if not port_text.isdigit():
            return None, None
        port = int(port_text)
        return (host, port) if 0 <= port <= 65535 else (None, None)
    return text, None


def _observed(row: dict[str, Any]) -> tuple[str, str, str]:
    """``(peer, node, ws_client)`` for this turn.

    A bound WebSocket is conclusive: its socket is the peer, and a
    missing socket is an unknown peer. The logged client label is
    returned beside it and is not the peer. With no WebSocket bound,
    the session row's ``client_peer`` and ``client_peer_node`` are
    what Hermes stored, and may both be empty."""
    live = _live_peer(_current_transport())
    if live is not None:
        return live
    return (str(row.get("client_peer") or "").strip(), str(row.get("client_peer_node") or "").strip(), "")


def _current_transport() -> Any:
    """The gateway transport bound to this turn, or None. Never imports
    the gateway: a copy this plugin imported would be a private one."""
    getter = getattr(sys.modules.get("tui_gateway.transport"), "current_transport", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 — unreadable: no peer
        return None


def _live_peer(transport: Any) -> Optional[tuple[str, str, str]]:
    """None when ``transport`` is not a WebSocket and not a fanout of
    one. Otherwise ``(socket peer, node, logged client)``. More than
    one socket, or a socket that can't be read, is an unknown peer:
    the row is not consulted in its place."""
    leaves = _ws_leaves(transport)
    if leaves is None:
        return ("", "", "")
    if not leaves:
        return None
    if len(leaves) != 1:
        return ("", "", "")
    leaf = leaves[0]
    label = getattr(leaf, "_peer", None)
    ws_client = label.strip() if isinstance(label, str) else ""
    endpoint = _socket_endpoint(getattr(leaf, "_ws", None))
    if endpoint is None:
        return ("", "", ws_client)
    host, port = endpoint
    peer = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    node = ""
    if _matchable_ip(peer):
        raw = getattr(leaf, "server_peer_node", None)
        node = raw if isinstance(raw, str) else ""
    return peer, node, ws_client


def _ws_leaves(transport: Any) -> Optional[list]:
    """WebSocket transports under ``transport``. None when a fanout
    can't be read. An object without a socket is not one, so the
    process fallback and the broker's own transport are not peers."""
    if transport is None:
        return []
    peers = getattr(transport, "transports", None)
    if callable(peers):
        try:
            found = list(peers())
        except Exception:  # noqa: BLE001 — unreadable: unknown peer
            return None
        leaves: list = []
        for peer in found:
            nested = _ws_leaves(peer)
            if nested is None:
                return None
            leaves.extend(nested)
        return leaves
    if getattr(transport, "_ws", None) is not None:
        return [transport]
    return []


def _socket_endpoint(ws: Any) -> Optional[tuple[str, int]]:
    """``(host, port)`` from the upgrade's TCP socket, or None. Headers
    and ``ws.client`` are not read: those are where a proxy rewrite
    shows up."""
    if ws is None:
        return None
    try:
        scope = getattr(ws, "scope", None) or {}
        extensions = scope.get("extensions") if isinstance(scope, dict) else None
        raw = extensions.get("transport") if isinstance(extensions, dict) else None
        if raw is None:
            raw = getattr(ws, "transport", None)
        get_extra = getattr(raw, "get_extra_info", None)
        sock = get_extra("socket") if callable(get_extra) else None
        peer = sock.getpeername() if sock is not None and hasattr(sock, "getpeername") else None
    except Exception:  # noqa: BLE001 — unreadable: unknown peer
        return None
    if not isinstance(peer, tuple) or len(peer) < 2:
        return None
    host, port = peer[0], peer[1]
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeError:
            return None
    if not isinstance(host, str) or isinstance(port, bool) or not isinstance(port, int):
        return None
    return host.split("%", 1)[0], port


def _token(value: Any, pattern: re.Pattern[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if pattern.fullmatch(text) else None


def _looks_like_key(value: str) -> bool:
    return value.startswith("agent:") and value.count(":") >= 3


def _parse_key(key: str) -> Optional[tuple[str, str, str, str]]:
    """``(platform, chat type, chat id, profile)`` from a gateway session
    key, or None when it isn't one."""
    parts = key.split(":") if isinstance(key, str) else []
    if len(parts) < 4 or parts[0] != "agent" or not parts[2] or not parts[3]:
        return None
    namespace = parts[1]
    profile = {"main": "default", "main~": "main"}.get(namespace, namespace)
    return (parts[2].strip().lower(), parts[3].strip().lower(), ":".join(parts[4:]),
            profile.strip().lower())


def _delegated(source: str, config: Any) -> bool:
    """A subagent row. Hermes marks one in model_config; a child spawned
    under a gateway turn keeps the turn's source, so the mark is what
    tells it from the chat it was spawned in."""
    if source == "subagent":
        return True
    try:
        parsed = json.loads(config) if isinstance(config, str) and config else {}
    except (ValueError, RecursionError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get("_delegate_from"))


def _row(home: Path, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    return _select(home, "id", session_id)


def _row_by_key(home: Path, session_key: str) -> dict[str, Any]:
    return _select(home, "session_key", session_key)


def _select(home: Path, column: str, value: str) -> dict[str, Any]:
    if column not in {"id", "session_key"}:
        return {}
    path = Path(home) / "state.db"
    if not path.is_file():
        return {}
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        logger.debug("conduit_push: couldn't open sessions to check a work entry point", exc_info=True)
        return {}
    try:
        columns = {str(info[1]) for info in db.execute("PRAGMA table_info(sessions)")}
        wanted = [name for name in _COLUMNS if name in columns]
        if column not in columns or "source" not in wanted:
            return {}
        row = db.execute(f"SELECT {', '.join(wanted)} FROM sessions WHERE {column} = ? LIMIT 1",
                         (value,)).fetchone()
        if row is None:
            return {}
        return dict(zip(wanted, row))
    except sqlite3.Error:
        logger.debug("conduit_push: couldn't read a session's source", exc_info=True)
        return {}
    finally:
        db.close()
