"""Durable UI-preferences store (issue 6).

The desktop shell binds a random localhost port each launch, so the SPA's
``localStorage`` — keyed by origin (scheme+host+port) — is wiped every session.
We therefore mirror the small set of UI prefs (recent projects, theme mode +
accent, toolbar options, file-explorer choice) into a JSON file in the OS
per-user app-data directory, which survives both restarts and app updates.

The frontend hydrates ``localStorage`` from here at boot and writes through on
every change. Values are opaque strings (the same blobs the SPA already keeps in
``localStorage``), so this router stays agnostic to their meaning.

The store path can be overridden with ``ARCH_PREFS_FILE`` (used by tests).
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import require_token

router = APIRouter(prefix="/api/prefs", tags=["prefs"],
                   dependencies=[Depends(require_token)])

APP_DIR_NAME = "ArchitectureValidator"
_lock = threading.Lock()


def _store_path() -> Path:
    """Per-user app-data location for the prefs file (platform-appropriate)."""
    override = os.environ.get("ARCH_PREFS_FILE")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_DIR_NAME / "prefs.json"


def _read() -> dict[str, str]:
    try:
        with _store_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write(data: dict[str, str]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a crash mid-write can't leave a truncated prefs file.
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def read_pref(key: str, default: str | None = None) -> str | None:
    """One stored preference, for backend features that use a user setting
    (e.g. the operator details in the call-tree report)."""
    with _lock:
        return _read().get(key, default)


class PrefPatch(BaseModel):
    key: str
    value: str | None = None  # None deletes the key


@router.get("")
def get_prefs() -> dict[str, dict[str, str]]:
    with _lock:
        return {"prefs": _read()}


@router.put("")
def put_pref(patch: PrefPatch) -> dict[str, dict[str, str]]:
    with _lock:
        data = _read()
        if patch.value is None:
            data.pop(patch.key, None)
        else:
            data[patch.key] = patch.value
        _write(data)
        return {"prefs": data}


class LogoBody(BaseModel):
    path: str


@router.post("/report-logo")
def set_report_logo(body: LogoBody) -> dict[str, str]:
    """Read an image file and store it as the call-tree report logo (an
    inline data URI, so reports stay single self-contained files)."""
    from Application_Logic import Logic_CallTree_Report as rep
    try:
        with open(body.path, "rb") as fh:
            raw = fh.read(rep.MAX_LOGO_BYTES + 1)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Could not read the image: {e}")
    try:
        uri = rep.logo_data_uri(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with _lock:
        data = _read()
        data[rep.LOGO_PREF_KEY] = uri
        _write(data)
    return {"key": rep.LOGO_PREF_KEY, "value": uri}
