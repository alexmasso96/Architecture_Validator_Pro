"""
Test Builds router (#15).

Test builds and their tracked artifacts, the clean-release lock, pre-flight,
run history, the binary store and the mapped folder. Long work runs as jobs in
``handlers.py``: ``test_build_run`` and ``mapped_switch``. The mapped folder's
path is saved per user and machine, because paths differ between PCs.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..deps import get_bus, get_state
from ..events import EventBus
from ..security import require_token
from ..state import AppState, ProjectError
from . import prefs

from Application_Logic import Logic_Mapped_Release as mapped
from Application_Logic import Logic_Test_Builds as tb
from Application_Logic.Logic_Binary_Store import BinaryStore, StoreError, store_path_for
from Application_Logic.Logic_File_Locking import FileLockManager

router = APIRouter(prefix="/api/test-builds", tags=["test-builds"],
                   dependencies=[Depends(require_token)])

TERMINALS = {"cmd", "powershell", "bash", "wsl"}


def _guard(fn):
    try:
        return fn()
    except ProjectError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (mapped.SwitchError, StoreError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def binary_store(state: AppState) -> BinaryStore:
    state.require_open()
    return BinaryStore(store_path_for(state.shared_path), cipher=state.block_cipher())


def _release_names(state: AppState) -> dict:
    return {r.id: r.name for r in state.require_releases().releases}


def _selected_release(state: AppState, release_id: Optional[int] = None) -> int:
    """The release a request is about: the one selected in the table by default."""
    if release_id is not None:
        if release_id not in _release_names(state):
            raise ProjectError(f"No such release: {release_id}")
        return release_id
    active = state.require_releases().get_active_release()
    if active is None:
        raise ProjectError("Select a release first.")
    return active.id


def _require_build(db, build_id: int) -> dict:
    build = db.get_test_build(build_id)
    if build is None:
        raise ProjectError(f"No such test build: {build_id}")
    return build


# ----------------------------------------------------------------------
# Builds
# ----------------------------------------------------------------------
@router.get("")
def list_builds(release_id: Optional[int] = None,
                state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        rid = _selected_release(state, release_id)
        builds = []
        for b in db.list_test_builds(rid):
            builds.append({**b, "files": len(db.get_build_manifest(b["id"])),
                           "size": db.get_build_manifest_size(b["id"])})
        return {"release_id": rid, "builds": builds}
    return _guard(go)


class BuildBody(BaseModel):
    name: Optional[str] = None
    test_project_id: Optional[int] = None
    release_id: Optional[int] = None
    build_command: Optional[str] = None
    build_cwd: Optional[str] = None
    clean_command: Optional[str] = None
    terminal: Optional[str] = None
    wsl_distro: Optional[str] = None
    source_dir: Optional[str] = None
    output_dir: Optional[str] = None


_SETTINGS = ("build_command", "build_cwd", "clean_command", "terminal", "wsl_distro",
             "source_dir", "output_dir")


def _settings(body: BuildBody) -> dict:
    fields = {k: getattr(body, k) for k in _SETTINGS if getattr(body, k) is not None}
    if fields.get("terminal") and fields["terminal"] not in TERMINALS:
        raise ProjectError(f"Unknown terminal: {fields['terminal']}")
    return fields


@router.post("")
def create_build(body: BuildBody, state: AppState = Depends(get_state),
                 bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        name = (body.name or "").strip()
        if not name:
            raise ProjectError("Test build name cannot be empty.")
        if name.lower() == "main":
            raise ProjectError("Main is the release's clean capture. Use another name.")
        rid = _selected_release(state, body.release_id)
        if any(b["name"] == name for b in db.list_test_builds(rid)):
            raise ProjectError(f"This release already has a test build called {name}.")
        if not any(p["id"] == body.test_project_id for p in db.list_test_projects()):
            raise ProjectError("Choose a test project for this build.")
        build_id = db.create_test_build(rid, name, body.test_project_id, **_settings(body))
        bus.publish("db-changed", {"reason": "test-build-created", "id": build_id})
        return db.get_test_build(build_id)
    return _guard(go)


@router.patch("/{build_id}")
def update_build(build_id: int, body: BuildBody, state: AppState = Depends(get_state),
                 bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        build = _require_build(db, build_id)
        fields = _settings(body)
        name = (body.name or "").strip()
        if name and name != build["name"]:
            if build["is_main"]:
                raise ProjectError("Main can't be renamed.")
            if any(b["name"] == name for b in db.list_test_builds(build["release_id"])):
                raise ProjectError(f"This release already has a test build called {name}.")
            fields["name"] = name
        db.update_test_build(build_id, **fields)
        bus.publish("db-changed", {"reason": "test-build-updated", "id": build_id})
        return db.get_test_build(build_id)
    return _guard(go)


@router.delete("/{build_id}")
def delete_build(build_id: int, state: AppState = Depends(get_state),
                 bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        _require_build(db, build_id)
        db.delete_test_build(build_id)
        bus.publish("db-changed", {"reason": "test-build-deleted", "id": build_id})
        return {"id": build_id, "deleted": True}
    return _guard(go)


# ----------------------------------------------------------------------
# Tracked artifacts
# ----------------------------------------------------------------------
class ArtifactBody(BaseModel):
    path: str
    is_folder: bool = False
    include_patterns: str = ""
    exclude_patterns: str = ""
    role: str = tb.ROLE_FLASH


class ArtifactsBody(BaseModel):
    test_build_id: Optional[int] = None    # None is the project-wide list
    artifacts: list[ArtifactBody]


@router.get("/artifacts/list")
def list_artifacts(test_build_id: Optional[int] = None,
                   state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        return {"test_build_id": test_build_id,
                "artifacts": db.list_tracked_artifacts(test_build_id)}
    return _guard(go)


@router.put("/artifacts/list")
def save_artifacts(body: ArtifactsBody, state: AppState = Depends(get_state),
                   bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        if body.test_build_id is not None:
            _require_build(db, body.test_build_id)
        for artifact in body.artifacts:
            if artifact.role not in tb.ROLES:
                raise ProjectError(f"Unknown role: {artifact.role}")
            if not artifact.path.strip():
                raise ProjectError("A tracked entry needs a path.")
        db.replace_tracked_artifacts(body.test_build_id,
                                     [a.model_dump() for a in body.artifacts])
        bus.publish("db-changed", {"reason": "tracked-artifacts-saved"})
        return {"count": len(body.artifacts)}
    return _guard(go)


# ----------------------------------------------------------------------
# Clean-release lock, pre-flight
# ----------------------------------------------------------------------
@router.get("/clean-lock/status")
def clean_lock(release_id: Optional[int] = None,
               state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        rid = _selected_release(state, release_id)
        status = tb.clean_lock_status(db, rid, _release_names(state).get(rid, ""))
        return {**status, "overrides": db.list_clean_lock_overrides(rid)}
    return _guard(go)


class PreflightBody(BaseModel):
    build_ids: list[int]
    continue_anyway: list[int] = []        # release ids


@router.post("/preflight")
def run_preflight(body: PreflightBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        return tb.preflight(db, binary_store(state), body.build_ids,
                            tuple(body.continue_anyway), _release_names(state))
    return _guard(go)


# ----------------------------------------------------------------------
# Main capture
# ----------------------------------------------------------------------
class ManualCaptureBody(BaseModel):
    release_id: Optional[int] = None
    folder: str


@router.post("/main/capture")
def capture_main(body: ManualCaptureBody, state: AppState = Depends(get_state),
                 bus: EventBus = Depends(get_bus)) -> dict:
    """Captures Main from an existing build output folder, before a run."""
    def go():
        db = state.require_edit()
        rid = _selected_release(state, body.release_id)
        if not os.path.isdir(body.folder):
            raise ProjectError(f"Not a folder: {body.folder}")
        if not db.list_tracked_artifacts(None):
            main = db.get_main_build(rid)
            if main is None or not db.list_tracked_artifacts(main["id"]):
                raise ProjectError("Choose which files to track first.")
        main = tb.ensure_main_build(db, rid, None)
        result = tb.capture_build(db, binary_store(state), main, db._now_iso(),
                                  output_dir=body.folder)
        bus.publish("db-changed", {"reason": "main-captured", "id": main["id"]})
        return result
    return _guard(go)


@router.post("/main/objects")
def main_objects(release_id: Optional[int] = None,
                 state: AppState = Depends(get_state)) -> dict:
    """Unpacks Main's clean .o files, so a late import can't take in test code."""
    def go():
        db = state.require_open()
        rid = _selected_release(state, release_id)
        main = db.get_main_build(rid)
        objects = [e for e in (db.get_build_manifest(main["id"]) if main else [])
                   if e["role"] == tb.ROLE_OBJECTS]
        if not objects:
            raise ProjectError("The Main capture has no .o files. Track them with "
                               "the Objects role.")
        store = binary_store(state)
        folder = tempfile.mkdtemp(prefix="arch-main-objects-")
        for entry in objects:
            store.read_to(entry["sha256"],
                          os.path.join(folder, entry["rel_path"].replace("/", os.sep)))
        return {"folder": folder, "count": len(objects)}
    return _guard(go)


# ----------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------
@router.get("/runs/list")
def list_runs(state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        unfinished = db.get_unfinished_build_run()
        if unfinished is not None and state.active_run_id == unfinished["id"]:
            unfinished = None
        return {"runs": db.list_build_runs(), "unfinished": unfinished,
                "active_run_id": state.active_run_id}
    return _guard(go)


@router.get("/runs/log/{item_id}", response_class=PlainTextResponse)
def run_log(item_id: int, state: AppState = Depends(get_state)) -> str:
    def go():
        text = state.require_open().read_build_run_log(item_id)
        if text is None:
            raise ProjectError("No log was saved for this build.")
        return text
    return _guard(go)


# ----------------------------------------------------------------------
# Binary store
# ----------------------------------------------------------------------
@router.get("/store/summary")
def store_summary(state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        store = binary_store(state)
        blobs = list(store.iter_blobs())
        referenced = db.get_referenced_blob_hashes()
        return {"path": store.root, "blobs": len(blobs),
                "bytes": sum(b[2] for b in blobs),
                "unreferenced": sum(1 for b in blobs if b[0] not in referenced)}
    return _guard(go)


@router.post("/store/cleanup")
def store_cleanup(state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_edit()
        return binary_store(state).cleanup(db.get_referenced_blob_hashes())
    return _guard(go)


# ----------------------------------------------------------------------
# Mapped folder
# ----------------------------------------------------------------------
def _mapped_pref_key(db) -> str:
    return f"mapped_folder::{db.get_meta('project_uuid', '')}"


def mapped_folder(state: AppState) -> str:
    return prefs.read_pref(_mapped_pref_key(state.require_open()), "") or ""


def mapped_identity(state: AppState, build: dict) -> dict:
    names = _release_names(state)
    return {"project_uuid": state.require_open().get_meta("project_uuid", ""),
            "build_id": build["id"], "build_name": build["name"],
            "release_id": build["release_id"],
            "release_name": names.get(build["release_id"], "")}


@router.get("/mapped/status")
def mapped_status(state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        folder = mapped_folder(state)
        rid = _selected_release(state)
        names = _release_names(state)
        held = mapped.read_state(folder) if folder else {}
        warning = ""
        if held.get("release_id") is not None and held["release_id"] != rid:
            warning = (f"Mapped folder holds {held.get('release_name', '')} "
                       f"{held.get('build_name', '')}, which doesn't match "
                       f"{names.get(rid, '')}")
        return {
            "folder": folder,
            "build_id": held.get("build_id"),
            "build_name": held.get("build_name"),
            "release_name": held.get("release_name"),
            "warning": warning,
            "interrupted": mapped.interrupted_switch(folder) if folder else None,
            "targets": [{"id": b["id"], "name": b["name"], "is_main": b["is_main"]}
                        for b in db.list_test_builds(rid)
                        if b["capture_status"] == tb.CAPTURE_COMPLETE],
        }
    return _guard(go)


class MappedFolderBody(BaseModel):
    folder: str


@router.put("/mapped/folder")
def set_mapped_folder(body: MappedFolderBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        if body.folder and not os.path.isdir(body.folder):
            raise ProjectError(f"Not a folder: {body.folder}")
        prefs.write_pref(_mapped_pref_key(db), body.folder or None)
        return {"folder": body.folder}
    return _guard(go)


class MappedPlanBody(BaseModel):
    build_id: int


@router.post("/mapped/plan")
def mapped_plan(body: MappedPlanBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        folder = mapped_folder(state)
        if not folder:
            raise ProjectError("Choose the mapped folder first.")
        _require_build(db, body.build_id)
        plan = mapped.plan_switch(folder, db.get_build_manifest(body.build_id))
        return {"copy": [e["rel_path"] for e in plan["copy"]], "remove": plan["remove"],
                "unchanged": len(plan["unchanged"]),
                "outside_changes": plan["outside_changes"],
                "transfer_bytes": plan["transfer_bytes"]}
    return _guard(go)


class MappedRecoverBody(BaseModel):
    action: str


@router.post("/mapped/recover")
def mapped_recover(body: MappedRecoverBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        folder = mapped_folder(state)
        if not folder:
            raise ProjectError("Choose the mapped folder first.")
        held = mapped.recover(folder, body.action)
        return {"build_name": held.get("build_name")}
    return _guard(go)
