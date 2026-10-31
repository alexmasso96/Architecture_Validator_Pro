"""
Sync Worker applet (#16): a separate process that doesn't need the app.

It syncs on a background thread and shows a tray icon on the main thread, which
macOS requires. Without a desktop to show the icon on, it simply keeps syncing.
The user can add it to their startup apps from its menu, and the app starts it if
it isn't running. In a frozen build it's the same executable with ``--sync-worker``,
and its window is ``--sync-window``.
"""
from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

_started: list = []


def sync_store():
    from Application_Logic.Logic_Sync_Worker import SyncStore
    from backend.routers.prefs import app_data_dir
    return SyncStore(str(app_data_dir() / "sync"))


def main() -> None:
    from Application_Logic.Logic_File_Locking import FileLockManager
    from Application_Logic.Logic_Sync_Worker import SyncWorker, claim_worker, run_until

    logging.basicConfig(level=logging.INFO)
    store = sync_store()
    if not claim_worker(store):
        return
    stop = threading.Event()
    worker = SyncWorker(store, FileLockManager.get_username())
    thread = threading.Thread(target=run_until, args=(worker, stop),
                              name="sync-loop", daemon=True)
    thread.start()
    try:
        from desktop import sync_tray
        sync_tray.run(store, stop)
    except Exception:  # noqa: BLE001 — no tray is no reason to stop syncing
        logger.warning("No tray icon on this desktop; syncing without one", exc_info=True)
    if thread.is_alive() and not stop.is_set():
        stop.wait()


def command(*flags: str) -> list:
    """How to start this applet from anywhere, such as at login."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *(flags or (FLAG,))]
    return [sys.executable, str(Path(__file__).resolve()), *flags]


def reap_stopped() -> None:
    """Clears out applets that have stopped. Until then a stopped child still looks
    like a running process, so the app wouldn't start a new worker."""
    _started[:] = [p for p in _started if p.poll() is None]


def start_detached(*flags: str) -> None:
    """Starts the applet so it keeps running after this process exits."""
    kwargs = {"start_new_session": True}
    if os.name == "nt":
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW
                  | subprocess.DETACHED_PROCESS}
    _started.append(subprocess.Popen(
        command(*flags), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **kwargs))


if __name__ == "__main__":
    if WINDOW_FLAG in sys.argv:
        from desktop import sync_window
        sync_window.main()
    else:
        main()
