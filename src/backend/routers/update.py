"""In-app updates (backend/updates.py does the work).

    GET  /api/update/latest    → {tag_name, body} of the newest release (kept for
                                 the plain-browser fallback and older callers)
    GET  /api/update/check     → this build, the chosen channel's newest build,
                                 whether it's newer and whether it can be installed
    GET  /api/update/settings  → {channel, branch, token_configured}
    PUT  /api/update/settings  → change any of them (token "" clears it)
    GET  /api/update/branches  → the repository's branches, main first
    GET  /api/update/staged    → the downloaded update waiting to be installed
    GET  /api/update/result    → how the last install went (read once)
    GET  /api/update/busy      → jobs a restart would cut short

The download is the ``update_download`` job (POST /api/jobs/update_download);
installing is the desktop shell's ``install_update`` bridge call, since the app
has to close for it.

GitHub is reached from here, not the webview: the shell runs WebView2 with
--no-proxy-server, so only the worker sees the system proxy.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import updates
from ..deps import get_jobs
from ..jobs import JobManager
from ..security import require_token

router = APIRouter(prefix="/api/update", tags=["update"],
                   dependencies=[Depends(require_token)])

# Kept for callers that import them from here.
GITHUB_REPO = updates.GITHUB_REPO
RELEASES_API = f"{updates.API}/releases/latest"


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except updates.UpdateError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if detail == "Not found on GitHub." else 502,
                            detail=detail)


@router.get("/latest")
def latest_release() -> dict[str, str]:
    """Return ``{tag_name, body}`` of the newest published GitHub release."""
    try:
        rel = updates.latest_release()
    except updates.UpdateError as exc:
        if str(exc) == "Not found on GitHub.":
            raise HTTPException(status_code=404, detail="No published releases found.")
        raise HTTPException(status_code=502, detail=str(exc))
    return {"tag_name": rel["tag_name"], "body": rel["body"]}


@router.get("/check")
def check() -> dict:
    out = _guard(updates.check)
    out["staged"] = _staged_public()
    return out


class SettingsPatch(BaseModel):
    channel: Optional[str] = None
    branch: Optional[str] = None
    token: Optional[str] = None


@router.get("/settings")
def get_settings() -> dict:
    return updates.get_settings()


@router.put("/settings")
def put_settings(patch: SettingsPatch) -> dict:
    try:
        return updates.save_settings(patch.channel, patch.branch, patch.token)
    except updates.UpdateError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/branches")
def branches() -> dict:
    return {"branches": _guard(updates.list_branches)}


def _staged_public() -> dict | None:
    stage = updates.staged()
    if not stage:
        return None
    return {k: stage.get(k) for k in ("channel", "version", "label", "commit")}


@router.get("/staged")
def staged() -> dict:
    return {"staged": _staged_public()}


@router.delete("/staged")
def discard_staged() -> dict:
    updates.clear_staging()
    return {"staged": None}


@router.get("/result")
def result() -> dict:
    return {"result": updates.last_result()}


@router.get("/busy")
def busy(jobs: JobManager = Depends(get_jobs)) -> dict:
    """Kinds of the jobs still running, apart from the update's own download."""
    return {"busy": jobs.running_kinds(exclude=("update_download", "_demo"))}
