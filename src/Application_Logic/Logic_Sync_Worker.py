"""
Sync Worker (#16)
=================
A separate background applet that keeps each tracked project's local copy and
shared copy in step, whether or not the app is running. The user can add it to
their startup apps. It polls only the small manifest, opens a database only when
something changed, and needs no password.

Because it's always there, it also watches each project's lock, so a stopped
session's lock is known to be stale before anyone tries to open the project, and
it's where the change log is read, so the app stays clean.

The app and the worker talk through small files in ``<app data>/sync/<uuid>/``,
so it needs no port and works with the app closed:

    status.json    worker → app   the state shown in the UI
    hold.json      worker → app   a pull is about to start, so reads must stop
    hold.ack       app → worker   reads have stopped
    session.json   app → worker   the project is open here, and how
    push.request   app → worker   Save was pressed, or the session is closing
    lock_watch.json  worker → app   when the lock's current bytes were first seen
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import Logic_Sync_Engine as engine
from .Logic_File_Locking import FileLockManager

logger = logging.getLogger(__name__)

TRACKED_FILE = "tracked.json"
PID_FILE = "worker.pid"
STATUS_FILE = "status.json"
HOLD_FILE = "hold.json"
HOLD_ACK = "hold.ack"
SESSION_FILE = "session.json"
PUSH_REQUEST = "push.request"
LOCK_WATCH = "lock_watch.json"
SYNC_NOW = "sync.now"

POLL_SECONDS = 30
LOOP_SECONDS = 2
HOLD_WAIT_SECONDS = 5

STATE_SYNCED = "synced"
STATE_PULLING = "pulling"
STATE_PUSHING = "pushing"
STATE_OFFLINE = "offline"
STATE_CONFLICT = "conflict"
STATE_BLOCKED = "blocked"


# ----------------------------------------------------------------------
# Small files
# ----------------------------------------------------------------------
def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(temp, path)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ----------------------------------------------------------------------
# The tracked list
# ----------------------------------------------------------------------
class SyncStore:
    """The worker's folder: which projects are tracked, and each one's files."""

    def __init__(self, root: str) -> None:
        self.root = root

    def tracked(self) -> list:
        return read_json(os.path.join(self.root, TRACKED_FILE), []) or []

    def _save(self, entries: list) -> None:
        write_json(os.path.join(self.root, TRACKED_FILE), entries)

    def find(self, remote: str) -> Optional[dict]:
        wanted = os.path.normcase(os.path.abspath(remote))
        return next((e for e in self.tracked()
                     if os.path.normcase(os.path.abspath(e["remote"])) == wanted), None)

    def project_dir(self, project_uuid: str) -> str:
        return os.path.join(self.root, project_uuid)

    def control(self, entry: dict, name: str) -> str:
        return os.path.join(self.project_dir(entry["project_uuid"]), name)

    def add(self, remote: str, username: str) -> dict:
        """Tracks a shared database, preparing it if it was copied there by hand."""
        existing = self.find(remote)
        if existing:
            return existing
        if not os.path.isfile(remote):
            raise engine.SyncError(f"No such file: {remote}")
        if engine.read_manifest(remote) is None:
            engine.adopt_remote(remote, username)
        project_uuid = engine.project_uuid_of(remote)
        if not project_uuid:
            raise engine.SyncBlocked("This file isn't a project database.")
        entry = {"project_uuid": project_uuid, "remote": remote,
                 "name": os.path.basename(remote),
                 "local": os.path.join(self.project_dir(project_uuid),
                                       os.path.basename(remote))}
        self._save([*self.tracked(), entry])
        return entry

    def remove(self, project_uuid: str) -> None:
        """Stops tracking. Both copies stay where they are."""
        self._save([e for e in self.tracked() if e["project_uuid"] != project_uuid])

    def status(self, entry: dict) -> dict:
        return read_json(self.control(entry, STATUS_FILE), {}) or {}

    def session(self, entry: dict) -> Optional[dict]:
        session = read_json(self.control(entry, SESSION_FILE))
        if session and _alive(session.get("pid", 0)):
            return session
        return None


def _alive(pid: int) -> bool:
    return FileLockManager.is_process_alive(pid, socket.gethostname())


# ----------------------------------------------------------------------
# The worker
# ----------------------------------------------------------------------
class SyncWorker:
    def __init__(self, store: SyncStore, username: str, poll_seconds: float = POLL_SECONDS,
                 hold_wait: float = HOLD_WAIT_SECONDS) -> None:
        self.store = store
        self.username = username
        self.poll_seconds = poll_seconds
        self.hold_wait = hold_wait
        self._next_poll: dict = {}
        self._blocked: set = set()

    def tick(self, force: bool = False) -> None:
        """One pass over every tracked project. Problems stay with their project."""
        requested = os.path.join(self.store.root, SYNC_NOW)
        if os.path.exists(requested):
            # "Sync now" is also how a blocked project gets another try.
            remove_file(requested)
            self._blocked.clear()
            force = True
        for entry in self.store.tracked():
            try:
                self._tick_project(entry, force)
            except engine.SyncBlocked as e:
                self._blocked.add(entry["project_uuid"])
                self._set_status(entry, STATE_BLOCKED, str(e))
            except engine.SyncError as e:
                self._set_status(entry, STATE_OFFLINE, str(e))
            except (OSError, sqlite3.Error) as e:
                logger.warning("Sync of %s failed", entry["name"], exc_info=True)
                self._set_status(entry, STATE_OFFLINE, str(e))

    def _set_status(self, entry: dict, state: str, message: str = "", **extra) -> None:
        current = self.store.status(entry)
        status = {"state": state, "message": message,
                  "last_synced": current.get("last_synced", ""),
                  "revision": current.get("revision", 0), **extra}
        if state == STATE_SYNCED:
            status["last_synced"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if status != current:
            write_json(self.store.control(entry, STATUS_FILE), status)

    def _tick_project(self, entry: dict, force: bool) -> None:
        project = entry["project_uuid"]
        if project in self._blocked:
            return
        request = read_json(self.store.control(entry, PUSH_REQUEST))
        if request is not None:
            self._push(entry, request)
            return
        now = time.monotonic()
        if not force and now < self._next_poll.get(project, 0):
            return
        self._next_poll[project] = now + self.poll_seconds
        self._poll(entry)

    def _watch_lock(self, entry: dict) -> None:
        """Notes when the lock's current bytes were first seen. A live owner
        rewrites them every minute, so the same bytes later are proof of silence,
        on this PC's clock alone. Only different valid contents restart the watch:
        reads over a share can fail or come back empty for a moment, and those
        prove nothing. A released lock needs no clean-up, because a new lock has
        a new timestamp."""
        path = self.store.control(entry, LOCK_WATCH)
        try:
            with open(FileLockManager.get_lock_file_path(entry["remote"]), "r") as f:
                contents = f.read()
            json.loads(contents)
        except (OSError, ValueError):
            return
        watch = read_json(path) or {}
        if watch.get("contents") != contents:
            write_json(path, {"contents": contents, "since": time.time()})

    def _poll(self, entry: dict) -> None:
        manifest = engine.read_manifest(entry["remote"])
        if manifest is None:
            return
        self._watch_lock(entry)
        if manifest.get("writing"):
            if FileLockManager.check_lock(entry["remote"])["status"] != "unlocked":
                return    # Being published. The next poll picks it up.
            engine.clear_interrupted_write(entry["remote"])
            manifest = engine.read_manifest(entry["remote"])
        if not os.path.exists(entry["local"]):
            # Nobody has to open a project for it to stay current.
            engine.clone(entry["remote"], entry["local"], only_if_missing=True)
        state = engine.local_state(entry["local"])
        session = self.store.session(entry)
        editing = bool(session) and session.get("mode") == "exclusive"
        if manifest["revision"] > state["base"] and not editing:
            if state["dirty"]:
                self._keep_conflict(entry, f"{manifest.get('username', 'Someone')} published "
                                           "first.")
            else:
                self._pull(entry, manifest)
            return
        self._set_status(entry, STATE_SYNCED, revision=state["base"])

    def _pull(self, entry: dict, manifest: dict) -> None:
        by = manifest.get("username", "")
        self._set_status(entry, STATE_PULLING, f"Syncing changes from {by}", by=by)
        session = self.store.session(entry)
        hold = self.store.control(entry, HOLD_FILE)
        ack = self.store.control(entry, HOLD_ACK)
        remove_file(ack)
        if session:
            write_json(hold, {"by": by})
            deadline = time.monotonic() + self.hold_wait
            while not os.path.exists(ack) and time.monotonic() < deadline:
                time.sleep(0.05)
        try:
            report = engine.pull(entry["local"], entry["remote"])
        finally:
            remove_file(hold)
            remove_file(ack)
        self._set_status(entry, STATE_SYNCED, revision=report["revision"])

    def _push(self, entry: dict, request: dict) -> None:
        token = request.get("token")
        remote = entry["remote"]
        request_path = self.store.control(entry, PUSH_REQUEST)
        closing = bool(request.get("release_lock"))
        if closing:
            # The app has gone, so the lock's heartbeat is ours until we finish.
            FileLockManager.write_heartbeat(remote, token)

        def still_ours() -> bool:
            return FileLockManager.check_lock(remote, token)["status"] == "locked_by_me"

        self._set_status(entry, STATE_PUSHING, "Publishing your changes")
        try:
            report = engine.push(entry["local"], remote, self.username, still_ours)
        except engine.SyncConflict as e:
            remove_file(request_path)
            self._keep_conflict(entry, str(e))
            return
        except engine.SyncError as e:
            if os.path.isfile(remote) and not engine.remote_is_healthy(remote):
                raise engine.SyncBlocked(
                    "The shared copy is damaged. Republish it from this copy.") from e
            engine.restore_dirty_flags(entry["local"])
            raise engine.SyncError(f"Edits aren't published yet: {e}") from e
        remove_file(request_path)
        if closing:
            FileLockManager.release_lock(remote, token)
        self._set_status(entry, STATE_SYNCED, revision=report["revision"])

    def _keep_conflict(self, entry: dict, reason: str) -> None:
        """Saves work that can't be published, then returns to the shared copy once
        no session has the local copy open. Nothing is ever merged."""
        copy = engine.save_conflict_copy(entry["local"], entry["remote"], self.username)
        self._set_status(entry, STATE_CONFLICT,
                         f"{reason} Your work was saved as {os.path.basename(copy)}.",
                         conflict_copy=copy)
        if self.store.session(entry) is None:
            engine.clone(entry["remote"], entry["local"])


def claim_worker(store: SyncStore) -> bool:
    """Makes this process the user's one worker, unless another already is."""
    pid_path = os.path.join(store.root, PID_FILE)
    running = read_json(pid_path)
    if running and running != os.getpid() and _alive(running):
        return False
    write_json(pid_path, os.getpid())
    return True


def run_until(worker: SyncWorker, stop: threading.Event) -> None:
    """The worker's loop. A tick that fails is tried again on the next one."""
    while not stop.is_set():
        try:
            worker.tick()
        except Exception:  # noqa: BLE001 — the applet outlives any one failure
            logger.exception("Sync pass failed")
        stop.wait(LOOP_SECONDS)


def worker_running(store: SyncStore) -> bool:
    pid = read_json(os.path.join(store.root, PID_FILE))
    return bool(pid) and _alive(pid)


def request_sync_now(store: SyncStore) -> None:
    write_json(os.path.join(store.root, SYNC_NOW), True)


# ----------------------------------------------------------------------
# What the applet shows
# ----------------------------------------------------------------------
def lock_age_seconds(store: SyncStore, entry: dict, contents: str) -> Optional[float]:
    """How long the lock has been these exact bytes, if the worker saw them."""
    watch = read_json(store.control(entry, LOCK_WATCH)) or {}
    if watch.get("contents") != contents:
        return None
    return max(time.time() - watch.get("since", time.time()), 0.0)


def lock_summary(store: SyncStore, entry: dict) -> Optional[dict]:
    """Who holds a project's lock and how long it's been silent, if it's held."""
    try:
        with open(FileLockManager.get_lock_file_path(entry["remote"]), "r") as f:
            contents = f.read()
        data = json.loads(contents)
    except (OSError, ValueError):
        return None
    return {"user": data.get("user", "unknown"), "hostname": data.get("hostname", "unknown"),
            "silent_seconds": lock_age_seconds(store, entry, contents) or 0.0}


def read_change_log(local: str, limit: int = 500) -> list:
    """The project's changes, newest first, from its local copy."""
    if not os.path.exists(local):
        return []
    conn = sqlite3.connect(f"{Path(local).as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT revision, timestamp, username, hostname, release_name, model_id, "
            "section, rows_changed FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))
        return [{"revision": r[0], "timestamp": r[1], "username": r[2], "hostname": r[3],
                 "release_name": r[4], "model_id": r[5], "section": r[6],
                 "rows_changed": r[7]} for r in rows.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def summarize(store: SyncStore) -> dict:
    """One line for the tray, and whether anything needs the user."""
    tracked = store.tracked()
    states = [store.status(e).get("state") for e in tracked]
    trouble = sum(1 for s in states if s in (STATE_CONFLICT, STATE_BLOCKED))
    offline = sum(1 for s in states if s == STATE_OFFLINE)
    if not tracked:
        title = "No shared projects"
    elif trouble:
        title = (f"{trouble} shared project needs attention" if trouble == 1
                 else f"{trouble} shared projects need attention")
    elif offline:
        title = "Shared folder can't be reached"
    else:
        title = f"{len(tracked)} shared project{'' if len(tracked) == 1 else 's'} in sync"
    return {"title": title, "attention": bool(trouble or offline)}
