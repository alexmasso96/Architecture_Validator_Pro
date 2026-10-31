"""
Sync router (#16): the open project's sync. The tracked list and the change log
live in the Sync Worker's own window, so the app stays clean.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import get_state
from ..security import require_token
from ..state import AppState, ProjectError

from Application_Logic import Logic_Sync_Engine as engine
from Application_Logic.Logic_File_Locking import FileLockManager
from Application_Logic.Logic_Sync_Worker import (
    STATE_BLOCKED, request_sync_now, worker_running,
)

router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(require_token)])


def _guard(fn):
    try:
        return fn()
    except (ProjectError, engine.SyncError, OSError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.get("/status")
def status(state: AppState = Depends(get_state)) -> dict:
    def go():
        store = state.sync_store()
        entry = state.sync_entry
        return {
            "worker_running": worker_running(store),
            "current": store.status(entry) if entry else None,
            "hold_by": state.sync_hold,
        }
    return _guard(go)


class PublishBody(BaseModel):
    remote: str
    password: Optional[str] = None


@router.post("/publish")
def publish(body: PublishBody, state: AppState = Depends(get_state)) -> dict:
    """Saves the open project to a shared location. It's tracked from then on, and
    the session continues on the local copy."""
    def go():
        state.require_edit()
        if state.sync_entry is not None:
            raise ProjectError("This project is already shared.")
        if not os.path.isdir(os.path.dirname(body.remote) or "."):
            raise ProjectError(f"No such folder: {os.path.dirname(body.remote)}")
        state.save_project()
        source = state.project_path
        password = state._password
        state.close_project()
        engine.publish(source, body.remote, FileLockManager.get_username())
        state.sync_store().add(body.remote, FileLockManager.get_username())
        return state.open_project(body.remote, "exclusive", password)
    return _guard(go)


@router.post("/republish")
def republish(state: AppState = Depends(get_state)) -> dict:
    """Replaces a damaged shared copy from this complete local copy."""
    def go():
        entry = state.sync_entry
        if entry is None:
            raise ProjectError("Open the project first.")
        store = state.sync_store()
        if store.status(entry).get("state") != STATE_BLOCKED:
            raise ProjectError("Sync isn't blocked for this project.")
        engine.republish(entry["local"], entry["remote"], FileLockManager.get_username())
        request_sync_now(store)
        return {"ok": True}
    return _guard(go)


@router.post("/open-worker")
def open_worker(state: AppState = Depends(get_state)) -> dict:
    """Opens the Sync Worker's window, and starts the worker if it isn't running."""
    def go():
        from desktop.sync_worker import WINDOW_FLAG, start_detached

        state.ensure_sync_worker()
        start_detached(WINDOW_FLAG)
        return {"ok": True}
    return _guard(go)
