"""Tests for the Sync Worker and its link to the app (#16)."""
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient

from Application_Logic import Logic_Sync_Engine as engine
from Application_Logic import Logic_File_Locking as lock_module
from Application_Logic import Logic_Sync_Worker as sw
from Application_Logic.Logic_File_Locking import FileLockManager
from backend.app import create_app
from Tests.test_helpers import make_project_db

TOKEN = "sync-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def no_wait_for_released_locks(monkeypatch):
    monkeypatch.setattr(lock_module, "LOCK_GONE_CHECKS", 0)


def _origin(tmp, name="origin.arch"):
    path = str(tmp / name)
    db = make_project_db(path, layout=[("TC. ID", "Static Text", True)],
                         models=[{"name": "A", "status": "In Work", "rows": []}],
                         releases=[{"name": "R1"}])
    db.close()
    return path


@pytest.fixture()
def shared(tmp_path):
    (tmp_path / "nas").mkdir()
    remote = str(tmp_path / "nas" / "Project.arch")
    engine.publish(_origin(tmp_path), remote, "alex")
    return remote


def _set_prompt(path, value):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("INSERT OR REPLACE INTO project_meta (key, value) VALUES ('ai_prompt', ?)",
                     (value,))
    conn.close()


def _prompt(path):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM project_meta WHERE key='ai_prompt'").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _user(tmp, name, remote):
    store = sw.SyncStore(str(tmp / name))
    entry = store.add(remote, name)
    engine.clone(remote, entry["local"])
    return store, entry, sw.SyncWorker(store, name, poll_seconds=0, hold_wait=0.1)


def test_tracking_a_database_copied_there_by_hand(tmp_path):
    remote = _origin(tmp_path, "byhand.arch")
    store = sw.SyncStore(str(tmp_path / "worker"))
    entry = store.add(remote, "alex")
    assert store.find(remote) == entry
    assert os.path.exists(engine.manifest_path(remote))
    assert store.add(remote, "alex") == entry


def test_removing_a_database_leaves_both_copies(shared, tmp_path):
    store, entry, _ = _user(tmp_path, "alex", shared)
    store.remove(entry["project_uuid"])
    assert store.tracked() == []
    assert os.path.exists(shared) and os.path.exists(entry["local"])


def test_save_publishes_and_the_other_user_receives_it(shared, tmp_path):
    store_a, entry_a, worker_a = _user(tmp_path, "alex", shared)
    _store_b, entry_b, worker_b = _user(tmp_path, "bo", shared)
    token = "t1"
    assert FileLockManager.acquire_lock(shared, token)[0]
    _set_prompt(entry_a["local"], "shared text")
    sw.write_json(store_a.control(entry_a, sw.PUSH_REQUEST), {"token": token})
    worker_a.tick()
    assert not os.path.exists(store_a.control(entry_a, sw.PUSH_REQUEST))
    assert _prompt(shared) == "shared text"
    assert FileLockManager.check_lock(shared, token)["status"] == "locked_by_me"

    worker_b.tick(force=True)
    assert _prompt(entry_b["local"]) == "shared text"
    FileLockManager.release_lock(shared, token)


def test_lock_is_released_only_after_the_last_push(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    token = "t1"
    FileLockManager.acquire_lock(shared, token)
    _set_prompt(entry["local"], "last edit")
    sw.write_json(store.control(entry, sw.PUSH_REQUEST),
                  {"token": token, "release_lock": True})
    assert FileLockManager.check_lock(shared, token)["status"] == "locked_by_me"
    worker.tick()
    assert _prompt(shared) == "last edit"
    assert FileLockManager.check_lock(shared)["status"] == "unlocked"


def test_lock_stays_held_while_a_push_keeps_failing(shared, tmp_path, monkeypatch):
    store, entry, worker = _user(tmp_path, "alex", shared)
    token = "t1"
    FileLockManager.acquire_lock(shared, token)
    _set_prompt(entry["local"], "edit")
    sw.write_json(store.control(entry, sw.PUSH_REQUEST),
                  {"token": token, "release_lock": True})

    def offline(*_a, **_k):
        raise engine.SyncError("The shared folder can't be reached")
    real = engine.push
    monkeypatch.setattr(engine, "push", offline)
    worker.tick()
    assert store.status(entry)["state"] == sw.STATE_OFFLINE
    assert "aren't published" in store.status(entry)["message"]
    assert os.path.exists(store.control(entry, sw.PUSH_REQUEST))

    monkeypatch.setattr(engine, "push", real)
    worker.tick()
    assert _prompt(shared) == "edit"
    assert FileLockManager.check_lock(shared)["status"] == "unlocked"


def test_share_outage_keeps_the_work_and_publishes_when_it_returns(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    token = "t1"
    FileLockManager.acquire_lock(shared, token)
    _set_prompt(entry["local"], "written offline")
    sw.write_json(store.control(entry, sw.PUSH_REQUEST), {"token": token})
    nas = os.path.dirname(shared)
    os.rename(nas, nas + ".away")
    worker.tick()
    assert store.status(entry)["state"] == sw.STATE_OFFLINE
    assert os.path.exists(store.control(entry, sw.PUSH_REQUEST))
    os.rename(nas + ".away", nas)
    worker.tick()
    assert _prompt(shared) == "written offline"
    assert store.status(entry)["state"] == sw.STATE_SYNCED


def test_pull_stops_reads_until_it_finishes(shared, tmp_path, monkeypatch):
    store, entry, worker = _user(tmp_path, "bo", shared)
    other = str(tmp_path / "alex-copy" / "Project.arch")
    engine.clone(shared, other)
    _set_prompt(other, "new")
    engine.push(other, shared, "alex")
    sw.write_json(store.control(entry, sw.SESSION_FILE), {"pid": os.getpid(), "mode": "view"})
    seen = {}
    real = engine.pull

    def spying(*args):
        seen["hold"] = sw.read_json(store.control(entry, sw.HOLD_FILE))
        return real(*args)
    monkeypatch.setattr(engine, "pull", spying)
    worker.tick(force=True)
    assert seen["hold"] == {"by": "alex"}
    assert not os.path.exists(store.control(entry, sw.HOLD_FILE))
    assert _prompt(entry["local"]) == "new"


def test_editor_doesnt_pull(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    sw.write_json(store.control(entry, sw.SESSION_FILE),
                  {"pid": os.getpid(), "mode": "exclusive"})
    other = str(tmp_path / "other" / "Project.arch")
    engine.clone(shared, other)
    _set_prompt(other, "someone else")
    engine.push(other, shared, "bo")
    worker.tick(force=True)
    assert _prompt(entry["local"]) is None


def test_lost_lock_conflict_loses_no_work(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    _set_prompt(entry["local"], "offline work")
    other = str(tmp_path / "other" / "Project.arch")
    engine.clone(shared, other)
    _set_prompt(other, "their work")
    engine.push(other, shared, "bo")

    assert FileLockManager.acquire_lock(shared, "bo-token")[0]
    sw.write_json(store.control(entry, sw.PUSH_REQUEST), {"token": "my-old-token"})
    worker.tick()
    status = store.status(entry)
    assert status["state"] == sw.STATE_CONFLICT
    assert _prompt(status["conflict_copy"]) == "offline work"
    assert _prompt(shared) == "their work"
    assert _prompt(entry["local"]) == "their work"


def test_damaged_share_blocks_sync_until_republished(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    token = "t1"
    FileLockManager.acquire_lock(shared, token)
    _set_prompt(entry["local"], "good")
    with open(shared, "r+b") as f:
        f.write(b"garbage" * 500)
    sw.write_json(store.control(entry, sw.PUSH_REQUEST), {"token": token})
    worker.tick()
    assert store.status(entry)["state"] == sw.STATE_BLOCKED
    assert "damaged" in store.status(entry)["message"]
    worker.tick()
    assert store.status(entry)["state"] == sw.STATE_BLOCKED


def test_one_bad_project_doesnt_stop_the_others(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    tracked = store.tracked()
    bad = {**entry, "project_uuid": "bad", "remote": str(tmp_path / "gone" / "x.arch")}
    sw.write_json(os.path.join(store.root, sw.TRACKED_FILE), [bad, *tracked])
    worker.tick(force=True)
    assert store.status(bad)["state"] == sw.STATE_OFFLINE
    assert store.status(entry)["state"] == sw.STATE_SYNCED


# ----------------------------------------------------------------------
# The always-running applet
# ----------------------------------------------------------------------
def test_worker_keeps_a_project_current_that_nobody_opened(shared, tmp_path):
    store = sw.SyncStore(str(tmp_path / "quiet"))
    entry = store.add(shared, "bo")
    worker = sw.SyncWorker(store, "bo", poll_seconds=0)
    assert not os.path.exists(entry["local"])
    worker.tick(force=True)
    assert os.path.exists(entry["local"])
    assert store.status(entry)["state"] == sw.STATE_SYNCED

    other = str(tmp_path / "other" / "Project.arch")
    engine.clone(shared, other)
    _set_prompt(other, "while the app was closed")
    engine.push(other, shared, "alex")
    worker.tick(force=True)
    assert _prompt(entry["local"]) == "while the app was closed"
    assert [r["section"] for r in sw.read_change_log(entry["local"])] == ["Settings"]


def test_change_log_reads_from_the_local_copy_without_the_app(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    FileLockManager.acquire_lock(shared, "t")
    _set_prompt(entry["local"], "x")
    sw.write_json(store.control(entry, sw.PUSH_REQUEST), {"token": "t"})
    worker.tick()
    assert sw.read_change_log(entry["local"])[0]["username"] == "alex"
    assert sw.read_change_log(str(tmp_path / "nothing.arch")) == []


def test_clone_leaves_a_copy_that_appeared_meanwhile(shared, tmp_path, monkeypatch):
    local = str(tmp_path / "raced" / "Project.arch")
    real = engine.shutil.copyfile

    def app_got_there_first(source, target):
        real(source, target)
        open(local, "wb").write(b"the app's open copy")
    monkeypatch.setattr(engine.shutil, "copyfile", app_got_there_first)
    engine.clone(shared, local, only_if_missing=True)
    assert open(local, "rb").read() == b"the app's open copy"
    assert os.listdir(os.path.dirname(local)) == ["Project.arch"]


def _stale_lock(remote):
    lock = FileLockManager.get_lock_file_path(remote)
    with open(lock, "w") as f:
        json.dump({"user": "crashed", "hostname": "their-pc", "timestamp": "t",
                   "last_seen": "t", "pid": 4242}, f)
    return lock


def test_worker_notes_when_it_first_saw_a_lock(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "bo", shared)
    lock = _stale_lock(shared)
    worker.tick(force=True)
    contents = open(lock).read()
    assert sw.lock_age_seconds(store, entry, contents) is not None
    first = sw.read_json(store.control(entry, sw.LOCK_WATCH))["since"]
    worker.tick(force=True)
    assert sw.read_json(store.control(entry, sw.LOCK_WATCH))["since"] == first

    with open(lock, "w") as f:
        f.write(contents.replace('"t"', '"later"'))
    assert sw.lock_age_seconds(store, entry, open(lock).read()) is None
    worker.tick(force=True)
    assert sw.read_json(store.control(entry, sw.LOCK_WATCH))["since"] >= first


def test_a_bad_read_doesnt_restart_the_watch(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "bo", shared)
    lock = _stale_lock(shared)
    worker.tick(force=True)
    contents = open(lock).read()
    first = sw.read_json(store.control(entry, sw.LOCK_WATCH))

    open(lock, "w").close()    # an SMB client can briefly read a file as empty
    worker.tick(force=True)
    os.remove(lock)            # or not see it at all
    worker.tick(force=True)
    with open(lock, "w") as f:
        f.write(contents)
    worker.tick(force=True)
    assert sw.read_json(store.control(entry, sw.LOCK_WATCH)) == first


def test_worker_clears_a_write_nobody_will_finish(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "bo", shared)
    engine._begin_write(shared, engine.read_manifest(shared), "alex")
    worker.tick(force=True)
    assert "writing" not in engine.read_manifest(shared)


def test_worker_leaves_a_write_alone_while_its_editor_is_live(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "bo", shared)
    assert FileLockManager.acquire_lock(shared, "alex-token")[0]
    engine._begin_write(shared, engine.read_manifest(shared), "alex")
    worker.tick(force=True)
    assert engine.read_manifest(shared)["writing"] == "alex"
    FileLockManager.release_lock(shared, "alex-token")


def test_sync_now_gives_a_blocked_project_another_try(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    worker._blocked.add(entry["project_uuid"])
    worker.tick(force=True)
    assert store.status(entry) == {}
    sw.request_sync_now(store)
    worker.tick()
    assert store.status(entry)["state"] == sw.STATE_SYNCED


def test_only_one_worker_runs_per_user(tmp_path):
    store = sw.SyncStore(str(tmp_path / "w"))
    assert sw.claim_worker(store) is True
    assert sw.claim_worker(store) is True    # this process already holds it
    sw.write_json(os.path.join(store.root, sw.PID_FILE), os.getppid())
    assert sw.claim_worker(store) is False
    sw.write_json(os.path.join(store.root, sw.PID_FILE), 999999)
    assert sw.claim_worker(store) is True


def test_summary_stays_quiet_unless_something_needs_the_user(shared, tmp_path):
    store, entry, worker = _user(tmp_path, "alex", shared)
    assert sw.summarize(sw.SyncStore(str(tmp_path / "none"))) == {
        "title": "No shared projects", "attention": False}
    worker.tick(force=True)
    assert sw.summarize(store) == {"title": "1 shared project in sync", "attention": False}
    sw.write_json(store.control(entry, sw.STATUS_FILE), {"state": sw.STATE_CONFLICT})
    assert sw.summarize(store) == {"title": "1 shared project needs attention",
                                   "attention": True}
    sw.write_json(store.control(entry, sw.STATUS_FILE), {"state": sw.STATE_OFFLINE})
    assert sw.summarize(store)["title"] == "Shared folder can't be reached"


# ----------------------------------------------------------------------
# The app
# ----------------------------------------------------------------------
@pytest.fixture()
def app_env(tmp_path, monkeypatch, shared):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "appdata" / "prefs.json"))
    monkeypatch.setenv("ARCH_SYNC_WORKER", "off")
    app = create_app(token=TOKEN, heartbeat_interval=0.05)
    with TestClient(app) as c:
        state = app.state.appstate
        yield c, state, shared, sw.SyncWorker(state.sync_store(), "alex", poll_seconds=0,
                                              hold_wait=1)


def _open(c, path, mode="exclusive"):
    r = c.post("/api/project/open", json={"path": path, "mode": mode}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def _save_title(c, state, text):
    state.db.set_meta("ai_prompt", text)
    assert c.post("/api/project/save", headers=AUTH).status_code == 200


def test_opening_a_shared_project_uses_a_local_copy_and_locks_the_share(app_env):
    c, state, remote, _worker = app_env
    status = _open(c, remote)
    assert status["tracked"] is True
    assert status["path"] == remote
    assert state.db.db_path != remote
    assert os.path.exists(FileLockManager.get_lock_file_path(remote))
    assert FileLockManager.check_lock(remote, state.sync_token)["status"] == "locked_by_me"
    assert not os.path.exists(FileLockManager.get_lock_file_path(state.db.db_path))


def test_watching_worker_clears_a_stale_lock_without_the_10_minute_wait(app_env):
    c, state, remote, worker = app_env
    store = state.sync_store()
    entry = store.add(remote, "alex")
    lock = _stale_lock(remote)
    worker.tick(force=True)

    r = c.post("/api/project/open", json={"path": remote, "mode": "exclusive"}, headers=AUTH)
    assert r.status_code == 409
    assert "about 10 minutes" in r.text

    watch_path = store.control(entry, sw.LOCK_WATCH)
    watch = sw.read_json(watch_path)
    watch["since"] -= 700
    sw.write_json(watch_path, watch)
    assert _open(c, remote)["tracked"] is True
    assert FileLockManager.check_lock(remote, state.sync_token)["status"] == "locked_by_me"


def test_heartbeat_follows_the_shared_lock(app_env):
    c, state, remote, _worker = app_env
    _open(c, remote)
    time.sleep(0.3)
    assert c.get("/api/project/status", headers=AUTH).json()["lock_lost"] is False


def test_share_outage_doesnt_end_the_edit_session(app_env):
    c, state, remote, _worker = app_env
    _open(c, remote)
    nas = os.path.dirname(remote)
    os.rename(nas, nas + ".away")
    time.sleep(0.3)
    assert c.get("/api/project/status", headers=AUTH).json()["lock_lost"] is False
    os.rename(nas + ".away", nas)
    time.sleep(0.2)
    assert c.get("/api/project/status", headers=AUTH).json()["lock_lost"] is False


def test_save_then_close_publishes_and_releases(app_env):
    c, state, remote, worker = app_env
    _open(c, remote)
    _save_title(c, state, "from the app")
    worker.tick()
    assert _prompt(remote) == "from the app"
    state.db.set_meta("ai_prompt", "last change")
    assert c.post("/api/project/close", headers=AUTH).status_code == 200
    assert FileLockManager.check_lock(remote)["status"] == "locked_by_other"
    worker.tick()
    assert _prompt(remote) == "last change"
    assert FileLockManager.check_lock(remote)["status"] == "unlocked"


def test_locked_by_another_user_can_still_view(app_env):
    c, state, remote, _worker = app_env
    assert FileLockManager.acquire_lock(remote, "someone-elses-token")[0]
    r = c.post("/api/project/open", json={"path": remote, "mode": "exclusive"}, headers=AUTH)
    assert r.status_code == 409
    assert "locked" in r.text.lower()
    _open(c, remote, "view")
    FileLockManager.release_lock(remote, "someone-elses-token")


def test_reads_are_refused_during_a_pull_and_views_refresh_after(app_env, tmp_path):
    c, state, remote, worker = app_env
    _open(c, remote, "view")
    other = str(tmp_path / "bo" / "Project.arch")
    engine.clone(remote, other)
    conn = sqlite3.connect(other)
    with conn:
        conn.execute("INSERT INTO releases (name) VALUES ('R9')")
    conn.close()
    engine.push(other, remote, "bo")

    bridge = state._sync_bridge
    bridge._stop.set()
    bridge._thread.join()
    store = state.sync_store()
    entry = state.sync_entry
    sw.write_json(store.control(entry, sw.HOLD_FILE), {"by": "bo"})
    bridge.step()
    r = c.get("/api/releases", headers=AUTH)
    assert r.status_code == 503
    assert r.json()["detail"] == {"code": "sync_in_progress", "by": "bo",
                                  "message": "Syncing changes from bo"}
    assert c.get("/api/project/status", headers=AUTH).status_code == 200
    assert os.path.exists(store.control(entry, sw.HOLD_ACK))

    engine.pull(entry["local"], remote)
    sw.remove_file(store.control(entry, sw.HOLD_FILE))
    bridge.step()
    assert state.sync_hold is None
    names = [r.name for r in state.require_releases().releases]
    assert "R9" in names
    assert c.get("/api/releases", headers=AUTH).status_code == 200


def test_reads_come_back_if_the_worker_dies_during_a_pull(app_env, monkeypatch):
    from backend import sync_bridge
    c, state, remote, _worker = app_env
    _open(c, remote, "view")
    bridge = state._sync_bridge
    bridge._stop.set()
    bridge._thread.join()
    store = state.sync_store()
    sw.write_json(store.control(state.sync_entry, sw.HOLD_FILE), {"by": "bo"})
    bridge.step()
    assert c.get("/api/releases", headers=AUTH).status_code == 503
    bridge._hold_started -= sync_bridge.HOLD_LIMIT_SECONDS + 1
    bridge.step()
    assert state.sync_hold is None
    assert c.get("/api/releases", headers=AUTH).status_code == 200
    assert not os.path.exists(store.control(state.sync_entry, sw.HOLD_FILE))


def test_conflict_drops_the_session_to_view_only(app_env):
    c, state, remote, _worker = app_env
    _open(c, remote)
    bridge = state._sync_bridge
    bridge._stop.set()
    bridge._thread.join()
    sw.write_json(state.sync_store().control(state.sync_entry, sw.STATUS_FILE),
                  {"state": sw.STATE_CONFLICT, "message": "saved a copy"})
    bridge.step()
    assert c.get("/api/project/status", headers=AUTH).json()["lock_lost"] is True


def test_project_with_a_manifest_is_tracked_when_opened(app_env):
    c, state, remote, _worker = app_env
    assert state.sync_store().tracked() == []
    assert _open(c, remote, "view")["tracked"] is True
    assert state.sync_store().find(remote) is not None


def test_untracked_shared_project_still_opens_directly(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "appdata" / "prefs.json"))
    path = _origin(tmp_path, "plain.arch")
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        status = _open(c, path)
        assert status["tracked"] is False
        assert app.state.appstate.db.db_path == path


def test_publish_moves_the_open_project_to_the_share(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "appdata" / "prefs.json"))
    monkeypatch.setenv("ARCH_SYNC_WORKER", "off")
    path = _origin(tmp_path, "mine.arch")
    (tmp_path / "share").mkdir()
    remote = str(tmp_path / "share" / "mine.arch")
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        _open(c, path)
        r = c.post("/api/sync/publish", json={"remote": remote}, headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["tracked"] is True
        assert os.path.exists(engine.manifest_path(remote))


def test_new_project_on_a_network_path_is_tracked(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "appdata" / "prefs.json"))
    monkeypatch.setenv("ARCH_SYNC_WORKER", "off")
    remote = str(tmp_path / "new.arch")
    app = create_app(token=TOKEN)
    # Through the live session's globals: other tests reload the backend modules.
    monkeypatch.setitem(type(app.state.appstate).new_project.__globals__,
                        "is_network_path", lambda _p: True)
    with TestClient(app) as c:
        r = c.post("/api/project/new", json={"path": remote}, headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["tracked"] is True
        assert app.state.appstate.db.db_path != remote
        assert os.path.exists(engine.manifest_path(remote))
        assert not [n for n in os.listdir(app.state.appstate.sync_store().root)
                    if n.startswith("new-")]


def test_binary_store_stays_on_the_share(app_env):
    c, state, remote, _worker = app_env
    _open(c, remote)
    summary = c.get("/api/test-builds/store/summary", headers=AUTH).json()
    assert summary["path"] == remote + ".binaries"


def test_sync_status_reports_the_open_project(app_env):
    c, state, remote, worker = app_env
    _open(c, remote)
    _save_title(c, state, "x")
    worker.tick()
    status = c.get("/api/sync/status", headers=AUTH).json()
    assert status["current"]["state"] == sw.STATE_SYNCED


def test_change_log_is_no_longer_part_of_the_app(app_env):
    c, _state, remote, _worker = app_env
    _open(c, remote)
    assert c.get("/api/sync/log", headers=AUTH).status_code in (404, 405)
