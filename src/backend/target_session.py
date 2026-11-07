"""
The worker's side of Target Tests: connection settings, the one-user-at-a-time
target lease, the AI chat's live connection, and the run / debug jobs.

Only one thing drives the target at a time: a run, a debug session or the AI.
Runs and debug sessions use their own connection in a child process
(``Logic_Target_Tests.run_in_process``); the AI's connection lives in this
process between chat turns and is closed before a run starts.
"""
from __future__ import annotations

import json
import queue
import threading
from typing import Callable, Dict, Optional

from Application_Logic import Logic_Target_Tests as tt
from Application_Logic.Logic_File_Locking import FileLockManager
from Application_Logic.Logic_WinIdea_Bridge import (TargetBackend, TargetError,
                                                    make_backend, summarize_checks)

from .routers import prefs
from .state import AppState, ProjectError

DEFAULT_CONFIG = {"kind": "winidea", "workspace": "", "winidea_dir": "", "probe": "main",
                  "test_timeout_s": tt.DEFAULT_TEST_TIMEOUT_S}
CONFIG_FIELDS = tuple(DEFAULT_CONFIG)


# ----------------------------------------------------------------------
# Settings (per user and machine: paths differ between PCs)
# ----------------------------------------------------------------------
def _config_key(state: AppState) -> str:
    return f"target_tests::{state.require_open().get_meta('project_uuid', '')}"


def get_config(state: AppState) -> dict:
    raw = prefs.read_pref(_config_key(state), "") or ""
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    return {**DEFAULT_CONFIG, **{k: v for k, v in saved.items() if k in CONFIG_FIELDS}}


def set_config(state: AppState, patch: dict) -> dict:
    cfg = get_config(state)
    for k, v in patch.items():
        if k in CONFIG_FIELDS and v is not None:
            cfg[k] = v
    if cfg["kind"] not in ("winidea", "simulator"):
        raise ProjectError(f"Unknown target kind: {cfg['kind']}")
    cfg["test_timeout_s"] = max(5.0, min(float(cfg["test_timeout_s"]), 3600.0))
    prefs.write_pref(_config_key(state), json.dumps(cfg))
    return cfg


# ----------------------------------------------------------------------
# The lease: one user of the target at a time
# ----------------------------------------------------------------------
class Lease:
    def __init__(self):
        self._lock = threading.Lock()
        self.holder: Optional[str] = None

    def acquire(self, who: str) -> None:
        with self._lock:
            if self.holder is not None and self.holder != who:
                raise ProjectError(f"The target is in use ({self.holder}). "
                                   "Wait for it to finish or stop it.")
            self.holder = who

    def release(self, who: str) -> None:
        with self._lock:
            if self.holder == who:
                self.holder = None


LEASE = Lease()


# ----------------------------------------------------------------------
# The AI chat's connection
# ----------------------------------------------------------------------
class AISession:
    """A connection the chat's target tools share between turns."""

    WHO = "AI chat"

    def __init__(self):
        self._lock = threading.Lock()
        self.backend: Optional[TargetBackend] = None
        self.config: Optional[dict] = None
        self.checked = False
        self.audit: list = []

    def get(self, config: dict) -> TargetBackend:
        """The live backend, connected and checked once per connection."""
        with self._lock:
            LEASE.acquire(self.WHO)
            if self.backend is not None and self.config != config:
                self._close()
            if self.backend is None:
                self.backend = make_backend(config)
                self.config = dict(config)
                self.checked = False
            if not self.checked:
                summary = summarize_checks(self.backend.checks())
                if not summary["ok"]:
                    self._close()
                    LEASE.release(self.WHO)
                    raise TargetError(summary["message"])
                self.checked = True
            return self.backend

    def _close(self):
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:  # noqa: BLE001
                pass
        self.backend = None
        self.checked = False

    def close(self):
        with self._lock:
            self._close()
            LEASE.release(self.WHO)

    def log(self, entry: dict):
        self.audit.append({"at": tt.now_iso(), "user": FileLockManager.get_username(),
                           **entry})
        del self.audit[:-500]


AI_SESSION = AISession()


# ----------------------------------------------------------------------
# Debug commands: router → running debug job
# ----------------------------------------------------------------------
_DEBUG_QUEUES: Dict[str, "queue.Queue"] = {}


def debug_command(session: str, cmd: dict) -> None:
    q = _DEBUG_QUEUES.get(session)
    if q is None:
        raise ProjectError("That debug session has ended.")
    q.put(cmd)


def _commands(q: "queue.Queue") -> Callable[[float], Optional[dict]]:
    def get(timeout: float):
        try:
            return q.get(timeout=timeout) if timeout else q.get_nowait()
        except queue.Empty:
            return None
    return get


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------
def run_job(state: AppState, params: dict, progress, cancel, worker_db) -> dict:
    """``target_test_run``: the connection check, then each test from a reset.
    Results are recorded; only an approved test's result is a verdict."""
    test_ids = [int(i) for i in params.get("test_ids") or []]
    if not test_ids:
        raise ProjectError("Pick the tests to run.")
    config = get_config(state)
    release_id = params.get("release_id")
    user = FileLockManager.get_username()
    with worker_db(state) as wdb:
        if release_id is None:
            release_id = wdb.get_active_release_id()
        tests = []
        for tid in test_ids:
            t = wdb.get_target_test(tid)
            if t is None:
                raise ProjectError(f"No such target test: {tid}")
            tests.append(t)
    AI_SESSION.close()
    LEASE.acquire("test run")
    summary = {"checks": None, "results": []}
    try:
        info: dict = {}
        started: dict = {}

        def on_event(msg: dict):
            kind = msg.get("type")
            if kind == "checks":
                summary["checks"] = msg
                info.update(msg.get("info") or {})
                state.bus.publish("target-test", {"event": "checks", **msg})
            elif kind == "started":
                started[msg["test_id"]] = tt.now_iso()
                t = next(x for x in tests if x["id"] == msg["test_id"])
                n = [x["id"] for x in tests].index(t["id"]) + 1
                progress(f"Running {t['test_key'] or t['id']} ({n}/{len(tests)})…",
                         100.0 * (n - 1) / len(tests))
                state.bus.publish("target-test", {"event": "started", "test_id": t["id"]})
            elif kind == "result":
                state.bus.publish("target-test", {"event": "result", "test_id": msg["test_id"],
                                                   "verdict": msg["result"]["verdict"]})
            elif kind == "fatal":
                state.bus.publish("target-test", {"event": "fatal", **msg})

        results = tt.run_in_process(
            config, [{"id": t["id"], "code": t["code"]} for t in tests], on_event,
            cancel=cancel.is_set, test_timeout_s=float(config["test_timeout_s"]),
            isolate=params.get("isolate", True))
        checks = summary["checks"] or {"ok": False, "message": "The runner stopped early."}
        if not checks.get("ok"):
            raise ProjectError(checks.get("message") or "Connection check failed.")
        by_id = dict(zip([t["id"] for t in tests], results))
        with worker_db(state) as wdb:
            for t in tests:
                r = by_id.get(t["id"])
                if r is None:
                    continue
                approved = tt.status_of(t) == tt.STATUS_APPROVED
                rid = wdb.add_target_test_result(
                    t["id"], t["version"], approved, release_id, r["verdict"], r["message"],
                    started.get(t["id"], tt.now_iso()), r["duration_ms"], user, info,
                    {"steps": r["steps"], "expectations": r["expectations"],
                     "checks": checks.get("checks", [])})
                summary["results"].append({"test_id": t["id"], "result_id": rid,
                                           "verdict": r["verdict"], "message": r["message"],
                                           "approved": approved})
            wdb.commit()
    finally:
        LEASE.release("test run")
    state.bus.publish("db-changed", {"reason": "target-tests-run"})
    return {"checks": summary["checks"], "results": summary["results"],
            "not_run": [t["id"] for t in tests if t["id"] not in
                        {r["test_id"] for r in summary["results"]}]}


def debug_job(state: AppState, params: dict, progress, cancel) -> dict:
    """``target_test_debug``: one test, pausing at the editor's breakpoints.
    Runs the editor's code (saved or not); nothing is recorded."""
    code = params.get("code")
    if not code:
        raise ProjectError("Nothing to debug.")
    session = params.get("session") or ""
    config = get_config(state)
    AI_SESSION.close()
    LEASE.acquire("debug session")
    q: "queue.Queue" = queue.Queue()
    _DEBUG_QUEUES[session] = q
    out: dict = {"checks": None, "result": None}
    try:
        progress("Debugging…")

        def on_event(msg: dict):
            if msg.get("type") == "checks":
                out["checks"] = msg
            if msg.get("type") == "result":
                out["result"] = msg["result"]
            state.bus.publish("target-debug", {"session": session, **msg})

        tt.run_in_process(
            config, [{"id": "debug", "code": code}], on_event, commands=_commands(q),
            cancel=cancel.is_set,
            debug={"breakpoints": [int(b) for b in params.get("breakpoints") or []],
                   "watches": [str(w) for w in params.get("watches") or []],
                   "pause_at_start": bool(params.get("pause_at_start"))},
            test_timeout_s=float(config["test_timeout_s"]),
            isolate=params.get("isolate", True))
    finally:
        _DEBUG_QUEUES.pop(session, None)
        LEASE.release("debug session")
    if out["checks"] and not out["checks"].get("ok"):
        raise ProjectError(out["checks"].get("message") or "Connection check failed.")
    return out


def check_connection(state: AppState) -> dict:
    """The pre-run connection check, on demand (a short connection of its own)."""
    config = get_config(state)
    AI_SESSION.close()
    LEASE.acquire("connection check")
    backend = None
    try:
        backend = make_backend(config)
        try:
            checks = backend.checks()
        except TargetError as e:
            checks = [{"name": "Target", "ok": False, "detail": str(e)}]
        info = {}
        try:
            info = backend.info()
        except Exception:  # noqa: BLE001
            pass
        return {**summarize_checks(checks), "info": info, "config": config}
    finally:
        if backend is not None:
            backend.close()
        LEASE.release("connection check")
