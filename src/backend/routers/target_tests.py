"""
Target Tests router: architecture tests on the real ECU through winIDEA.

Tests, their versions and results live in the project; the connection
settings are saved per user and machine. Runs and debug sessions are jobs
(``target_test_run``, ``target_test_debug``) in ``target_session``.

Approval rules (``Logic_Target_Tests.Approvals``): the AI's tests and changes
are drafts; a user's edit is approved by that user; accepting an AI proposal
and approving it are two steps; a new release never removes approval.
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import get_bus, get_jobs, get_state
from ..events import EventBus
from ..jobs import JobManager
from ..security import require_token
from ..state import AppState, ProjectError
from .. import target_session as ts

from Application_Logic import Logic_Target_Tests as tt
from Application_Logic.Logic_File_Locking import FileLockManager

router = APIRouter(prefix="/api/target-tests", tags=["target-tests"],
                   dependencies=[Depends(require_token)])


def _guard(fn):
    try:
        return fn()
    except (ProjectError, tt.ApprovalError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _approvals(db) -> tt.Approvals:
    return tt.Approvals(db, FileLockManager.get_username())


def _summary(t: dict, last: Optional[dict]) -> dict:
    return {k: t[k] for k in ("id", "test_key", "title", "port", "requirement", "model_id",
                              "version", "approved_version", "approved_by", "approved_at",
                              "updated_at")} | {
        "status": tt.status_of(t), "has_proposal": bool(t["pending_code"]),
        "last_result": last}


def _changed(bus: EventBus, test_id=None):
    bus.publish("db-changed", {"reason": "target-tests-changed", "test_id": test_id})


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------
@router.get("")
def list_tests(state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        last = db.latest_target_test_results()
        return {"tests": [_summary(t, last.get(t["id"])) for t in db.list_target_tests()],
                "busy": ts.LEASE.holder}
    return _guard(go)


def _detail(db, test_id: int) -> dict:
    t = db.get_target_test(test_id)
    if t is None:
        raise ProjectError(f"No such target test: {test_id}")
    approved_code = ""
    if t["approved_version"]:
        v = db.get_target_test_version(test_id, t["approved_version"])
        approved_code = v["code"] if v else ""
    return {**t, "status": tt.status_of(t), "approved_code": approved_code,
            "issues": tt.validate(t["code"]),
            "versions": db.list_target_test_versions(test_id),
            "results": db.list_target_test_results(test_id)}


@router.get("/{test_id}")
def get_test(test_id: int, state: AppState = Depends(get_state)) -> dict:
    return _guard(lambda: _detail(state.require_open(), test_id))


class NewTestBody(BaseModel):
    code: Optional[str] = None
    test_key: Optional[str] = None
    title: str = ""
    port: str = ""
    function: str = "main"
    model_id: Optional[int] = None


def _next_key(db) -> str:
    n = 0
    for t in db.list_target_tests():
        key = t["test_key"] or ""
        if key.startswith("TT-") and key[3:].isdigit():
            n = max(n, int(key[3:]))
    return f"TT-{n + 1:04d}"


@router.post("")
def create_test(body: NewTestBody, state: AppState = Depends(get_state),
                bus: EventBus = Depends(get_bus)) -> dict:
    """A test the user creates: approved by them, like any manual edit."""
    def go():
        db = state.require_edit()
        code = body.code or tt.new_test_code(body.test_key or _next_key(db), body.title,
                                             body.port, body.function)
        tid = _approvals(db).create(code, tt.ORIGIN_USER, release_id=db.get_active_release_id(),
                                    model_id=body.model_id)
        _changed(bus, tid)
        return _detail(db, tid)
    return _guard(go)


class CodeBody(BaseModel):
    code: str


@router.put("/{test_id}")
def save_test(test_id: int, body: CodeBody, state: AppState = Depends(get_state),
              bus: EventBus = Depends(get_bus)) -> dict:
    """The user's correction: saved as a new version, approved by them."""
    def go():
        db = state.require_edit()
        current = db.get_target_test(test_id)
        if current is None:
            raise ProjectError(f"No such target test: {test_id}")
        if current["code"] != body.code:
            _approvals(db).save_manual(test_id, body.code)
        _changed(bus, test_id)
        return _detail(db, test_id)
    return _guard(go)


def _action(name: str):
    def endpoint(test_id: int, state: AppState = Depends(get_state),
                 bus: EventBus = Depends(get_bus)) -> dict:
        def go():
            db = state.require_edit()
            getattr(_approvals(db), name)(test_id)
            _changed(bus, test_id)
            return _detail(db, test_id)
        return _guard(go)
    endpoint.__name__ = name
    return endpoint


router.post("/{test_id}/approve")(_action("approve"))
router.post("/{test_id}/revert")(_action("revert"))
router.post("/{test_id}/proposal/accept")(_action("accept_proposal"))
router.post("/{test_id}/proposal/reject")(_action("reject_proposal"))


@router.delete("/{test_id}")
def delete_test(test_id: int, state: AppState = Depends(get_state),
                bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        db.delete_target_test(test_id)
        _changed(bus, test_id)
        return {"deleted": test_id}
    return _guard(go)


@router.get("/{test_id}/versions/{version}")
def get_version(test_id: int, version: int, state: AppState = Depends(get_state)) -> dict:
    def go():
        v = state.require_open().get_target_test_version(test_id, version)
        if v is None:
            raise ProjectError(f"No version {version} of test {test_id}")
        return v
    return _guard(go)


@router.get("/results/{result_id}")
def get_result(result_id: int, state: AppState = Depends(get_state)) -> dict:
    def go():
        r = state.require_open().get_target_test_result(result_id)
        if r is None:
            raise ProjectError(f"No such result: {result_id}")
        return r
    return _guard(go)


@router.post("/validate")
def validate(body: CodeBody) -> dict:
    return {"issues": tt.validate(body.code), "meta": tt.parse_meta(body.code)}


@router.get("/meta/api")
def api_reference() -> dict:
    return {"target": tt.TARGET_API, "expect": tt.EXPECT_API}


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------
@router.get("/config/target")
def get_target_config(state: AppState = Depends(get_state)) -> dict:
    return _guard(lambda: {**ts.get_config(state), "busy": ts.LEASE.holder})


class ConfigBody(BaseModel):
    kind: Optional[str] = None
    workspace: Optional[str] = None
    winidea_dir: Optional[str] = None
    probe: Optional[str] = None
    test_timeout_s: Optional[float] = None


@router.put("/config/target")
def put_target_config(body: ConfigBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        cfg = ts.set_config(state, body.model_dump(exclude_none=True))
        ts.AI_SESSION.close()
        return cfg
    return _guard(go)


@router.post("/connection/check")
def check_connection(state: AppState = Depends(get_state)) -> dict:
    return _guard(lambda: ts.check_connection(state))


# ----------------------------------------------------------------------
# Run and debug (jobs)
# ----------------------------------------------------------------------
class RunBody(BaseModel):
    test_ids: List[int]
    release_id: Optional[int] = None


@router.post("/run")
def run_tests(body: RunBody, state: AppState = Depends(get_state),
              jobs: JobManager = Depends(get_jobs)) -> dict:
    def go():
        state.require_open()
        if ts.LEASE.holder and ts.LEASE.holder != ts.AISession.WHO:
            raise ProjectError(f"The target is in use ({ts.LEASE.holder}).")
        job = jobs.start("target_test_run", body.model_dump())
        return {"job_id": job.id}
    return _guard(go)


class DebugBody(BaseModel):
    code: str
    breakpoints: List[int] = []
    watches: List[str] = []
    pause_at_start: bool = False


@router.post("/debug")
def start_debug(body: DebugBody, state: AppState = Depends(get_state),
                jobs: JobManager = Depends(get_jobs)) -> dict:
    def go():
        state.require_open()
        if ts.LEASE.holder and ts.LEASE.holder != ts.AISession.WHO:
            raise ProjectError(f"The target is in use ({ts.LEASE.holder}).")
        session = uuid.uuid4().hex
        job = jobs.start("target_test_debug", {**body.model_dump(), "session": session})
        return {"job_id": job.id, "session": session}
    return _guard(go)


class DebugCommand(BaseModel):
    action: str = ""
    breakpoints: Optional[List[int]] = None
    watches: Optional[List[str]] = None


@router.post("/debug/{session}/command")
def debug_command(session: str, body: DebugCommand) -> dict:
    def go():
        if body.action not in ("", "continue", "step", "stop", "snapshot"):
            raise ProjectError(f"Unknown debug action: {body.action}")
        ts.debug_command(session, body.model_dump(exclude_none=True))
        return {"ok": True}
    return _guard(go)
