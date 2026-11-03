"""Tests for the Sync Worker applet (#16): start at login and its window."""
import os
import plistlib
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Sync_Autostart as autostart
from Application_Logic import Logic_Sync_Engine as engine
from Application_Logic import Logic_Sync_Worker as sw
from Application_Logic.Logic_Sync_Worker import SyncStore, SyncWorker
from desktop.sync_window import PAGE, SyncWindowApi
from desktop import sync_window, sync_worker
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


def _frozen(monkeypatch, folder, with_sync_program=True):
    suffix = ".exe" if os.name == "nt" else ""
    app = folder / f"ArchitectureValidator{suffix}"
    app.write_text("")
    sync = folder / f"{sync_worker.EXE_NAME}{suffix}"
    if with_sync_program:
        sync.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app))
    return str(app), str(sync)


def test_frozen_command_is_the_workers_own_program(monkeypatch, tmp_path):
    _app, sync = _frozen(monkeypatch, tmp_path)
    assert sync_worker.command() == [sync, "--sync-worker"]
    assert sync_worker.command(sync_worker.WINDOW_FLAG) == [sync, "--sync-window"]


def test_frozen_build_without_the_program_falls_back_to_the_app(monkeypatch, tmp_path):
    app, _sync = _frozen(monkeypatch, tmp_path, with_sync_program=False)
    assert sync_worker.command() == [app, "--sync-worker"]


class _Recorder:
    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(sync_worker, "_started", [])
        monkeypatch.setattr(sync_worker.subprocess, "Popen",
                            lambda argv, **_kw: self.calls.append(argv))


def test_app_starts_the_worker_outside_its_own_process_tree(monkeypatch):
    calls = _Recorder(monkeypatch).calls
    sync_worker.start_detached(own_group=True)
    sync_worker.start_detached(sync_worker.WINDOW_FLAG)
    assert calls[0] == sync_worker.command() + [sync_worker.DETACH_FLAG]
    assert calls[1] == sync_worker.command(sync_worker.WINDOW_FLAG)


def test_detach_starts_the_worker_again_without_the_flag(monkeypatch):
    from desktop import sync_main
    calls = _Recorder(monkeypatch).calls
    monkeypatch.setattr(sync_worker, "main", lambda: calls.append("ran here"))
    sync_main.main([sync_worker.FLAG, sync_worker.DETACH_FLAG])
    assert calls == [sync_worker.command(sync_worker.FLAG)]


def test_old_login_item_on_the_app_hands_over_to_the_worker(monkeypatch, tmp_path):
    from desktop import sync_main
    _app, sync = _frozen(monkeypatch, tmp_path)
    calls = _Recorder(monkeypatch).calls
    monkeypatch.setattr(sync_worker, "main", lambda: calls.append("ran here"))
    sync_main.main([sync_worker.FLAG])
    assert calls == [[sync, sync_worker.FLAG]]

    # The worker's own program runs it.
    calls.clear()
    monkeypatch.setattr(sys, "executable", sync)
    sync_main.main([sync_worker.FLAG])
    assert calls == ["ran here"]


def test_worker_login_item_moves_to_its_own_program(monkeypatch, tmp_path):
    _app, sync = _frozen(monkeypatch, tmp_path)
    enabled = []
    monkeypatch.setattr(autostart, "is_enabled", lambda: True)
    monkeypatch.setattr(autostart, "enable", enabled.append)
    sync_worker.update_login_item()
    assert enabled == [[sync, sync_worker.FLAG]]


def test_worker_doesnt_load_the_app():
    import subprocess
    heavy = ("fastapi", "uvicorn", "pydantic", "webview", "rapidfuzz", "elftools",
             "capstone", "rust_elf_parser", "pandas", "openpyxl", "backend")
    code = ("import sys; sys.argv = ['x']; import desktop.sync_main, desktop.sync_tray; "
            "from desktop import sync_worker; sync_worker.sync_store(); "
            f"print(sorted({{m.split('.')[0] for m in sys.modules}} & set({heavy!r})))")
    out = subprocess.run([sys.executable, "-c", code], cwd=os.path.abspath("src"),
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


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


def test_any_failure_adding_a_project_reaches_the_page(tmp_path, monkeypatch):
    """An exception would only be a rejected promise, with nothing on screen."""
    store = SyncStore(str(tmp_path / "worker"))
    api = SyncWindowApi(store, ARGV)

    def fails(*_a, **_k):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(store, "add", fails)
    problem = api.add_path(str(tmp_path / "Project.arch"))
    assert "unable to open database file" in problem
    assert api.overview()["operation"]["error"] == problem
    api.dismiss()
    assert api.overview()["operation"] is None


def test_adding_a_project_shows_steps_and_starts_the_worker(tmp_path):
    origin = str(tmp_path / "hand.arch")
    make_project_db(origin, layout=[("TC. ID", "Static Text", True)], models=[],
                    releases=[]).close()
    store = SyncStore(str(tmp_path / "worker"))
    started = []
    api = SyncWindowApi(store, ARGV, start_worker=lambda: started.append(True))
    seen = []
    real = store.add

    def spying(*args):
        seen.append(api.overview()["operation"]["message"])
        return real(*args)
    store.add = spying
    assert api.add_path(origin) is None
    assert seen == ["Preparing hand.arch on the shared folder"]
    assert api.overview()["operation"] is None
    assert started == [True]
    assert os.path.exists(os.path.join(store.root, sw.SYNC_NOW))


def test_second_window_brings_the_first_forward(tmp_path, monkeypatch):
    import threading
    store = SyncStore(str(tmp_path / "worker"))
    sw.write_json(os.path.join(store.root, sync_window.WINDOW_PID), os.getppid())
    request = os.path.join(store.root, sync_window.FOCUS_REQUEST)

    def open_window_answers():
        while not os.path.exists(request):
            pass
        os.remove(request)
    threading.Thread(target=open_window_answers, daemon=True).start()
    assert sync_window._hand_over_to_open_window(store) is True


def test_stale_window_record_cant_stop_the_window_opening(tmp_path, monkeypatch):
    """The process id may belong to a crashed window, or to something else by now."""
    monkeypatch.setattr(sync_window, "FOCUS_WAIT_SECONDS", 0.05)
    store = SyncStore(str(tmp_path / "worker"))
    pid_path = os.path.join(store.root, sync_window.WINDOW_PID)
    assert sync_window._hand_over_to_open_window(store) is False
    sw.write_json(pid_path, 999999)
    assert sync_window._hand_over_to_open_window(store) is False
    sw.write_json(pid_path, os.getppid())
    assert sync_window._hand_over_to_open_window(store) is False
    assert not os.path.exists(os.path.join(store.root, sync_window.FOCUS_REQUEST))


def test_spinning_icon_has_distinct_frames():
    from desktop import sync_tray
    frames = sync_tray.spin_frames(sync_tray.icon_image(False))
    assert len(frames) == sync_tray.SPIN_FRAMES
    assert len({f.tobytes() for f in frames}) == sync_tray.SPIN_FRAMES


def test_page_calls_only_methods_the_api_has():
    import re
    called = set(re.findall(r"api\(\)\.(\w+)\(", PAGE))
    assert called == {"overview", "changes", "add_database", "sync_now", "stop_tracking",
                      "set_start_at_login", "dismiss"}
    assert all(callable(getattr(SyncWindowApi, name)) for name in called)
