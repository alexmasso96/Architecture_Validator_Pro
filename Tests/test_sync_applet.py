"""Tests for the Sync Worker applet (#16): start at login and its window."""
import os
import plistlib
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Sync_Autostart as autostart
from Application_Logic import Logic_Sync_Engine as engine
from Application_Logic.Logic_Sync_Worker import SyncStore, SyncWorker
from desktop.sync_window import PAGE, SyncWindowApi
from desktop import sync_worker
from Tests.test_helpers import make_project_db

ARGV = ["/Applications/Architecture Validator/app", "--sync-worker"]


def test_macos_login_item_starts_the_worker(tmp_path):
    assert autostart.is_enabled("darwin", tmp_path) is False
    autostart.enable(ARGV, "darwin", tmp_path)
    assert autostart.is_enabled("darwin", tmp_path) is True
    plist = plistlib.loads(
        (tmp_path / "Library" / "LaunchAgents" / f"{autostart.LAUNCH_LABEL}.plist").read_bytes())
    assert plist["ProgramArguments"] == ARGV
    assert plist["RunAtLoad"] is True
    autostart.disable("darwin", tmp_path)
    assert autostart.is_enabled("darwin", tmp_path) is False
    autostart.disable("darwin", tmp_path)


def test_linux_login_item_starts_the_worker(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    autostart.enable(ARGV, "linux", tmp_path)
    entry = (tmp_path / ".config" / "autostart" / autostart.DESKTOP_FILE).read_text()
    assert "Exec='/Applications/Architecture Validator/app' --sync-worker" in entry
    assert autostart.is_enabled("linux", tmp_path) is True
    autostart.disable("linux", tmp_path)
    assert autostart.is_enabled("linux", tmp_path) is False


class FakeRegistry:
    """The few winreg calls used, over one in-memory key."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def OpenKey(self, *_args):
        return self._Key()

    CreateKey = OpenKey

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        self.values[name] = value

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_windows_login_item_is_per_user_and_quotes_paths():
    registry = FakeRegistry()
    assert autostart.is_enabled("win32", registry=registry) is False
    autostart.enable(ARGV, "win32", registry=registry)
    assert registry.values[autostart.RUN_VALUE] == (
        '"/Applications/Architecture Validator/app" --sync-worker')
    assert autostart.is_enabled("win32", registry=registry) is True
    autostart.disable("win32", registry=registry)
    autostart.disable("win32", registry=registry)
    assert autostart.is_enabled("win32", registry=registry) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_stopped_worker_no_longer_looks_alive(tmp_path, monkeypatch):
    import signal
    import subprocess
    import time
    from Application_Logic import Logic_Sync_Worker as sw

    store = SyncStore(str(tmp_path / "w"))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sw.write_json(os.path.join(store.root, sw.PID_FILE), child.pid)
    monkeypatch.setattr(sync_worker, "_started", [child])
    assert sw.worker_running(store) is True
    os.kill(child.pid, signal.SIGKILL)
    time.sleep(0.2)
    sync_worker.reap_stopped()
    assert sw.worker_running(store) is False
    assert sync_worker._started == []


def test_frozen_command_is_the_app_with_the_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/app/ArchitectureValidator")
    assert sync_worker.command() == ["/app/ArchitectureValidator", "--sync-worker"]
    assert sync_worker.command(sync_worker.WINDOW_FLAG) == [
        "/app/ArchitectureValidator", "--sync-window"]


def test_window_lists_projects_and_their_changes_without_the_app(tmp_path):
    origin = str(tmp_path / "origin.arch")
    make_project_db(origin, layout=[("TC. ID", "Static Text", True)], models=[],
                    releases=[{"name": "R1"}]).close()
    (tmp_path / "nas").mkdir()
    remote = str(tmp_path / "nas" / "Project.arch")
    engine.publish(origin, remote, "alex")
    store = SyncStore(str(tmp_path / "worker"))
    api = SyncWindowApi(store, ARGV)

    assert api.overview()["projects"] == []
    assert api.add_path(remote) is None
    assert "No such file" in api.add_path(str(tmp_path / "missing.arch"))
    project = api.overview()["projects"][0]
    assert project["name"] == "Project.arch"
    assert project["lock"] is None

    entry = store.tracked()[0]
    SyncWorker(store, "alex", poll_seconds=0).tick(force=True)
    conn = sqlite3.connect(entry["local"])
    with conn:
        conn.execute("INSERT INTO project_meta (key, value) VALUES ('ai_prompt', 'p')")
    conn.close()
    engine.push(entry["local"], remote, "alex")
    changes = api.changes(entry["project_uuid"])
    assert [(c["username"], c["section"]) for c in changes] == [("alex", "Settings")]
    assert api.changes("nobody") == []

    api.stop_tracking(entry["project_uuid"])
    assert api.overview()["projects"] == []
    assert os.path.exists(remote) and os.path.exists(entry["local"])


def test_page_calls_only_methods_the_api_has():
    import re
    called = set(re.findall(r"api\(\)\.(\w+)\(", PAGE))
    assert called == {"overview", "changes", "add_database", "sync_now", "stop_tracking",
                      "set_start_at_login"}
    assert all(callable(getattr(SyncWindowApi, name)) for name in called)
