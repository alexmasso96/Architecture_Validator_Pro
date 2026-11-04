"""
AI Chat history — this user's saved conversations, per project.

Conversations are personal and hold source code, so they're kept:
  * per user, in the app-data folder — not in the .arch, so a read-only or
    shared project still saves them and colleagues don't see each other's;
  * encrypted, with the same machine-bound Fernet key as the AI credentials;
  * one file per project (keyed by a hash of the project's path).

A session is the whole conversation as the chat shows it: messages (with the
tool calls and token usage of each answer), the model and settings it last
used, and its running usage totals. The frontend owns the session and saves it
after each turn, so a rollback (cutting the conversation at a prompt) is just
a save of the shorter list.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.fernet import InvalidToken

from . import Logic_AI_Credentials as creds
from .Logic_App_Data import app_data_dir

MAX_SESSIONS = 200
MAX_MESSAGES = 2000
_TITLE_LEN = 80
_lock = threading.Lock()


def _dir() -> Path:
    override = os.environ.get("ARCH_CHAT_HISTORY_DIR")
    return Path(override) if override else app_data_dir() / "ai_chats"


def project_key(project_path: Optional[str]) -> str:
    """Stable key of a project: its normalised path (no project → 'none')."""
    if not project_path:
        return "none"
    norm = os.path.normcase(os.path.abspath(project_path))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]


def _file(key: str) -> Path:
    return _dir() / f"{key}.aichats"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _read(key: str) -> Dict:
    path = _file(key)
    try:
        token = path.read_bytes()
    except OSError:
        return {"sessions": {}}
    try:
        data = json.loads(creds._fernet().decrypt(token).decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeDecodeError):
        # Another machine's or a damaged file: keep it aside, start empty.
        try:
            path.replace(path.with_suffix(".unreadable"))
        except OSError:
            pass
        return {"sessions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {"sessions": {}}
    return data


def _write(key: str, data: Dict) -> None:
    path = _file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = creds._fernet().encrypt(json.dumps(data).encode("utf-8"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(token)
    os.replace(tmp, path)


def _title_from(messages: List[Dict]) -> str:
    first = next((m.get("content", "") for m in messages
                  if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    line = " ".join(first.split())
    if not line:
        return "New chat"
    return line if len(line) <= _TITLE_LEN else line[: _TITLE_LEN - 1] + "…"


def _summary(s: Dict) -> Dict:
    msgs = s.get("messages") or []
    return {
        "id": s.get("id"),
        "title": s.get("title") or _title_from(msgs),
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
        "provider_id": s.get("provider_id", ""),
        "model": s.get("model", ""),
        "agent_id": s.get("agent_id", ""),
        "message_count": len(msgs),
        "usage": s.get("usage") or {},
    }


def list_sessions(key: str) -> List[Dict]:
    with _lock:
        data = _read(key)
    out = [_summary(s) for s in data["sessions"].values() if isinstance(s, dict)]
    out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return out


def get_session(key: str, session_id: str) -> Optional[Dict]:
    with _lock:
        s = _read(key)["sessions"].get(session_id)
    return dict(s) if isinstance(s, dict) else None


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def save_session(key: str, session: Dict) -> Dict:
    """Create or replace a session; returns its summary. Keeps the newest
    MAX_SESSIONS sessions of the project."""
    sid = str(session.get("id") or "") or new_session_id()
    msgs = [m for m in (session.get("messages") or []) if isinstance(m, dict)][-MAX_MESSAGES:]
    with _lock:
        data = _read(key)
        old = data["sessions"].get(sid) or {}
        title = str(session.get("title") or "").strip()
        if not title or (not old.get("title_locked") and title == "New chat"):
            title = _title_from(msgs)
        s = {
            "id": sid,
            "title": title[:_TITLE_LEN * 2],
            "title_locked": bool(session.get("title_locked") or old.get("title_locked")),
            "created_at": old.get("created_at") or session.get("created_at") or _now(),
            "updated_at": _now(),
            "provider_id": str(session.get("provider_id") or ""),
            "model": str(session.get("model") or ""),
            "agent_id": str(session.get("agent_id") or ""),
            "settings": session.get("settings") if isinstance(session.get("settings"), dict) else {},
            "usage": session.get("usage") if isinstance(session.get("usage"), dict) else {},
            "messages": msgs,
        }
        data["sessions"][sid] = s
        if len(data["sessions"]) > MAX_SESSIONS:
            keep = sorted(data["sessions"].values(), key=lambda x: x.get("updated_at") or "",
                          reverse=True)[:MAX_SESSIONS]
            data["sessions"] = {x["id"]: x for x in keep}
        _write(key, data)
    return _summary(s)


def rename_session(key: str, session_id: str, title: str) -> Optional[Dict]:
    with _lock:
        data = _read(key)
        s = data["sessions"].get(session_id)
        if not isinstance(s, dict):
            return None
        s["title"] = (" ".join((title or "").split()) or _title_from(s.get("messages") or []))[:160]
        s["title_locked"] = True
        _write(key, data)
    return _summary(s)


def delete_session(key: str, session_id: str) -> bool:
    with _lock:
        data = _read(key)
        if data["sessions"].pop(session_id, None) is None:
            return False
        _write(key, data)
    return True
