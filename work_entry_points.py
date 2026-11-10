"""Chats, besides a Bot Chat, that may submit work.

The list is ``work_entry_points.yaml`` beside this file. A regular file at
``<profile home>/conduit/work-entry-points.yaml`` replaces it for that
profile. An entry matches a turn only when source, platform, chat type and
profile all match, and the session id matches too when the entry lists any.
Nothing here opens a socket or reads a network.

Source is the session row's ``source``. Platform and chat type come from
the gateway session key (``agent:<profile>:<platform>:<chat type>:<chat id>``,
``gateway/session.py`` ``build_session_key``). A local UI row with no key
(the desktop and TUI gateways often have none) takes the platform and chat
type documented on the shipped list. A fork or a delegated child never
matches, whatever its source.
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

FILE_NAME = "work_entry_points.yaml"
OVERRIDE_NAME = "work-entry-points.yaml"
_OVERRIDE_MAX = 64 * 1024
_MAX_ENTRIES = 32
_MAX_SESSION_IDS = 16
_SESSION_ID_MAX = 256
# Local UI sources whose row often has no gateway key. The platform is the
# one Hermes uses for that surface when the turn didn't name one; the chat
# type is dm, SessionSource's own default. api_server is not here: its key
# carries both, and a row without them must not match.
_INFER_PLATFORM = {"tui": "local", "desktop": "desktop", "gui": "desktop"}
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A Hermes profile name (hermes_cli.profiles.normalize_profile_name), lowercased.
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLUMNS = ("id", "source", "session_key", "model_config")


@dataclass(frozen=True)
class Entry:
    name: str
    source: str
    platform: str
    chat_type: str
    profile: str
    session_ids: tuple[str, ...] = ()

    def matches(self, found: "Seen") -> bool:
        if found.delegated or found.profile_mismatch:
            return False
        if (found.source, found.platform, found.chat_type, found.profile) != (
                self.source, self.platform, self.chat_type, self.profile):
            return False
        if not self.session_ids:
            return True
        ids = {found.session_id, found.row_id, found.chat_id} - {""}
        return bool(ids.intersection(self.session_ids))


@dataclass(frozen=True)
class Seen:
    """The turn's identity. ``platform`` and ``chat_type`` include the
    local-UI fill-in; ``raw_platform`` and ``raw_chat_type`` are what was
    stored or bound, which is what a refusal reports."""

    source: str = ""
    platform: str = ""
    chat_type: str = ""
    profile: str = ""
    session_id: str = ""
    row_id: str = ""
    chat_id: str = ""
    delegated: bool = False
    profile_mismatch: bool = False
    raw_platform: str = ""
    raw_chat_type: str = ""
    inferred: tuple[str, ...] = ()


def allows(session_id: Any, home: Path) -> bool:
    """Whether ``session_id`` is a listed entry point for ``home``."""
    found = observe(session_id, home)
    return any(entry.matches(found) for entry in load(home))


def seen(session_id: Any, home: Optional[Path]) -> dict[str, str]:
    """The source, platform, chat type and profile a refusal should report.

    Raw values, before a missing local key is filled in, so a mismatch
    shows what the turn actually had. ``delegated`` is set when the row is
    a subagent that kept its parent's source."""
    found = observe(session_id, home) if home is not None else Seen()
    reported = {"source": found.source, "platform": found.raw_platform,
                "chat_type": found.raw_chat_type, "profile": found.profile}
    if found.delegated:
        reported["delegated"] = "true"
    return reported


def observe(session_id: Any, home: Optional[Path]) -> Seen:
    """The turn's identity. An unreadable store is an empty source, which
    matches nothing."""
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
    platform = key[0] if key is not None else _turn_platform()
    chat_type = key[1] if key is not None else ""
    chat_id = key[2] if key is not None else ""
    key_profile = key[3] if key is not None else ""
    inferred: list[str] = []
    raw_platform, raw_chat = platform, chat_type
    if not platform and source in _INFER_PLATFORM:
        platform = _INFER_PLATFORM[source]
        inferred.append("platform")
    if not chat_type and source in _INFER_PLATFORM:
        chat_type = "dm"
        inferred.append("chat_type")
    return Seen(
        source=source, platform=platform, chat_type=chat_type, profile=profile,
        session_id=sid, row_id=str(row.get("id") or ""), chat_id=chat_id,
        delegated=_delegated(source, row.get("model_config")),
        profile_mismatch=bool(key_profile) and key_profile != profile,
        raw_platform=raw_platform, raw_chat_type=raw_chat, inferred=tuple(inferred))


def load(home: Optional[Path] = None) -> tuple[Entry, ...]:
    """The entries in force: the profile's override when it is a valid
    list, otherwise the shipped list. A missing override changes nothing.
    An override that can't be read or parsed is not applied."""
    if home is not None:
        override = _override_text(Path(home))
        if override is not None:
            parsed = _parse(override)
            if parsed is not None:
                return parsed
            logger.warning("conduit_push: work entry points override isn't usable; using the shipped list")
    shipped = _shipped_text()
    parsed = _parse(shipped) if shipped is not None else None
    if parsed is None:
        logger.warning("conduit_push: shipped work entry points aren't usable; no extra chat can submit work")
        return ()
    return parsed


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


def _parse(text: str) -> Optional[tuple[Entry, ...]]:
    if len(text) > _OVERRIDE_MAX:
        return None
    try:
        from . import session_titles

        data = session_titles._yaml_load(text)
    except Exception:  # noqa: BLE001 — an unreadable list allows nothing extra
        logger.debug("conduit_push: couldn't parse work entry points", exc_info=True)
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or len(entries) > _MAX_ENTRIES:
        return None
    parsed: list[Entry] = []
    for item in entries:
        entry = _entry(item)
        if entry is None:
            return None
        parsed.append(entry)
    return tuple(parsed)


def _entry(item: Any) -> Optional[Entry]:
    if not isinstance(item, dict):
        return None
    name = _token(item.get("name"), _NAME)
    source = _token(item.get("source"), _TOKEN)
    platform = _token(item.get("platform"), _TOKEN)
    chat_type = _token(item.get("chat_type"), _TOKEN)
    profile = _token(item.get("profile"), _PROFILE)
    if name is None or source is None or platform is None or chat_type is None or profile is None:
        return None
    raw_ids = item.get("session_ids", [])
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list) or len(raw_ids) > _MAX_SESSION_IDS:
        return None
    session_ids: list[str] = []
    for value in raw_ids:
        if not _session_id(value):
            return None
        session_ids.append(value)
    return Entry(name=name, source=source, platform=platform, chat_type=chat_type,
                 profile=profile, session_ids=tuple(session_ids))


def _session_id(value: Any) -> bool:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > _SESSION_ID_MAX:
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


def _turn_platform() -> str:
    """The platform Hermes bound to this turn, or "" when it bound none."""
    try:
        from . import reply_sources

        value = reply_sources.current_platform()
    except Exception:  # noqa: BLE001 — an unknown platform matches nothing by itself
        return ""
    return value.strip().lower() if isinstance(value, str) else ""


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
