"""Chats, besides a Bot Chat, that may submit work.

The list is ``work_entry_points.yaml`` beside this file. A regular file at
``<profile home>/conduit/work-entry-points.yaml`` replaces it for that
profile. Nothing here opens a socket or reads a network.

An entry authorizes a turn only when its source and profile match and it
names a session id or a ``user_id``. Source alone is not enough: a client
chooses ``source`` on ``session.create``. ``user_id`` is the login Hermes
stores from the authenticated transport
(``tui_gateway`` ``_transport_auth_user_id``), not from the create params.
An empty ``user_id`` never matches.

Platform and chat type come only from the gateway session key on the row
(``agent:<profile>:<platform>:<chat type>:<chat id>``), or from the row's
``chat_type`` column when the key has none. They are never taken from
``HERMES_SESSION_PLATFORM``. That context value is the gateway's copy of
the session source, and the client sends the source, so a client can
influence it. ``_turn_platform`` reads it for tests and is not an
authorization input.

A fork or a delegated child never matches. With ``capture: true``, a
refused turn logs the raw identity so a login can be pinned later.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
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
_COLUMNS = ("id", "source", "session_key", "model_config", "user_id", "chat_type")


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

    def matches(self, found: "Seen") -> bool:
        """Whether this entry authorizes ``found``. Disabled entries, and
        entries with neither a session id nor a user id, never do."""
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
    """The source, platform, chat type, profile and user id a refusal
    should report, as the row stored them."""
    found = observe(session_id, home) if home is not None else Seen()
    reported = {"source": found.source, "platform": found.platform, "chat_type": found.chat_type,
                "profile": found.profile, "user_id": found.user_id}
    if found.delegated:
        reported["delegated"] = "true"
    return reported


def record_refusal(session_id: Any, home: Optional[Path]) -> None:
    """Log the raw identity of a refused turn while capture is on.

    The line is how a desktop or phone login gets pinned: source, the
    stored platform and chat type, the profile, and ``user_id``. No
    message text."""
    if home is None or not _capture(home):
        return
    found = observe(session_id, home)
    logger.info("conduit_push: work refused source=%s platform=%s chat_type=%s profile=%s user_id=%s session=%s",
                found.source or "-", found.platform or "-", found.chat_type or "-", found.profile or "-",
                found.user_id or "-", found.session_id or "-")


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
    return Seen(
        source=source, platform=platform, chat_type=chat_type, profile=profile,
        user_id=str(row.get("user_id") or "").strip(), session_id=sid,
        row_id=str(row.get("id") or ""), chat_id=chat_id,
        delegated=_delegated(source, row.get("model_config")),
        profile_mismatch=bool(key_profile) and key_profile != profile)


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
    if session_ids is None or user_ids is None:
        return None
    return Entry(name=name, source=source, profile=profile, enabled=enabled, platform=platform,
                 chat_type=chat_type, session_ids=tuple(session_ids), user_ids=tuple(user_ids))


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
