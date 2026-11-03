"""
Sync Worker applet (#16): a separate program that doesn't need the app.

It syncs on a background thread and shows a tray icon on the main thread, which
macOS requires. Without a desktop to show the icon on, it simply keeps syncing.
The user can add it to their startup apps from its menu, and the app starts it if
it isn't running. In a frozen build it's its own executable next to the app's,
``ArchitectureValidatorSync``, so Task Manager shows it under its own name, and
its window is the same program with ``--sync-window``. Its entry is
``desktop/sync_main.py``.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FLAG = "--sync-worker"
WINDOW_FLAG = "--sync-window"
DETACH_FLAG = "--detach"
EXE_NAME = "ArchitectureValidatorSync"

logger = logging.getLogger(__name__)

_started: list = []


def sync_store():
    from Application_Logic.Logic_App_Data import app_data_dir
    from Application_Logic.Logic_Sync_Worker import SyncStore
    return SyncStore(str(app_data_dir() / "sync"))


def setup_logging(store, name: str) -> None:
    """A log file, because a windowed build has no console to show a failure on."""
    os.makedirs(store.root, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(
            os.path.join(store.root, f"{name}.log"), maxBytes=1_000_000, backupCount=1,
            encoding="utf-8")])


def main() -> None:
    from Application_Logic.Logic_File_Locking import FileLockManager
    from Application_Logic.Logic_Sync_Worker import SyncWorker, claim_worker, run_until

    store = sync_store()
    setup_logging(store, "sync-worker")
    if not claim_worker(store):
        return
    update_login_item()
    stop = threading.Event()
    worker = SyncWorker(store, FileLockManager.get_username())
    thread = threading.Thread(target=run_until, args=(worker, stop),
                              name="sync-loop", daemon=True)
    thread.start()
    try:
        from desktop import sync_tray
        sync_tray.run(store, stop, worker)
    except Exception:  # noqa: BLE001 — no tray is no reason to stop syncing
        logger.warning("No tray icon on this desktop; syncing without one", exc_info=True)
    if thread.is_alive() and not stop.is_set():
        stop.wait()


def program() -> list:
    """The Sync Worker's own program: its executable in a frozen build, or else
    its entry script. A build without that executable falls back to the app's,
    which still takes the flags."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name(EXE_NAME + (".exe" if os.name == "nt" else ""))
        return [str(exe) if exe.is_file() else sys.executable]
    return [sys.executable, str(Path(__file__).resolve().with_name("sync_main.py"))]


def command(*flags: str) -> list:
    """How to start this applet from anywhere, such as at login."""
    return [*program(), *(flags or (FLAG,))]


def update_login_item() -> None:
    """Points "Start at login" at the worker's own program. A login item made
    before it had one still starts the app's executable."""
    from Application_Logic import Logic_Sync_Autostart as autostart

    if not getattr(sys, "frozen", False):
        return    # A run from source mustn't take over the installed app's login item.
    try:
        if autostart.is_enabled():
            autostart.enable(command())
    except OSError:
        logger.warning("Could not update the login item", exc_info=True)


def reap_stopped() -> None:
    """Clears out applets that have stopped. Until then a stopped child still looks
    like a running process, so the app wouldn't start a new worker."""
    _started[:] = [p for p in _started if p.poll() is None]


def start_detached(*flags: str, own_group: bool = False) -> None:
    """Starts the applet so it keeps running after this process exits.

    With ``own_group``, which the app uses, the applet starts itself once more and
    that first copy exits at once. The applet is then nobody's child, so Task
    Manager shows it on its own rather than inside the app that started it.
    """
    argv = command(*flags)
    if own_group:
        argv.append(DETACH_FLAG)
    kwargs = {"start_new_session": True}
    if os.name == "nt":
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW
                  | subprocess.DETACHED_PROCESS}
    _started.append(subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **kwargs))
