"""
Installs a downloaded update (backend/updates.py stages it).

A running program can't replace itself — on Windows its files are locked — so
the *new* build does the swap. The app writes a plan, starts the staged build's
own executable with ``--apply-update <plan>`` from the staging folder, and
closes. That updater then:

1. waits for the app to exit (its worker, and so the project's edit lock, goes
   with it), then asks the Sync Worker to finish its pass and exit;
2. copies the new build next to the installed one, renames the installed one to
   ``<name>.old`` and the copy into its place — renames, so a failure half-way
   puts the old one straight back;
3. starts the new app and waits until it reports ready. If it exits first, the
   old build is put back and started instead;
4. starts the Sync Worker again if it was running, and writes how it went to
   ``update_result.json``, which the app shows once.

On Windows, when the install folder can't be written without admin rights, only
the renames run elevated (one UAC prompt), and the app is still started as the
user. The next start of the app removes the ``.old`` folder and the staging
folder.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

FLAG = "--apply-update"
STEP_FLAG = "--step"          # elevated helper: --step install | --step rollback
MARKER_ENV = "ARCH_UPDATE_MARKER"
PLAN_FILE = "plan.json"
UPDATER_PID_FILE = "updater.pid"
RESULT_FILE = "update_result.json"
OLD_SUFFIX = ".old"
NEW_SUFFIX = ".new"
FAILED_SUFFIX = ".failed"

APP_EXIT_TIMEOUT = 120
SYNC_EXIT_TIMEOUT = 60
READY_TIMEOUT = 180
RENAME_RETRY_SECONDS = 60

logger = logging.getLogger(__name__)


class SwapError(Exception):
    pass


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _app_data() -> Path:
    from Application_Logic.Logic_App_Data import app_data_dir
    return app_data_dir()


def updates_dir() -> Path:
    return _app_data() / "updates"


def result_path() -> Path:
    # Outside updates/, which the new app clears on start.
    return _app_data() / RESULT_FILE


def sibling(install: Path, suffix: str) -> Path:
    return install.with_name(install.name + suffix)


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(SYNCHRONIZE | QUERY, False, pid)
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_exit(pid: int, timeout: float, alive: Callable[[int], bool] = pid_alive,
                  interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def _detached_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def _popen(argv: list[str], env: dict | None = None, cwd: str | None = None):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env, cwd=cwd, **_detached_kwargs())


# ---------------------------------------------------------------------------
# The swap
# ---------------------------------------------------------------------------

def _retry(fn: Callable[[], None], seconds: float = RENAME_RETRY_SECONDS) -> None:
    """Runs ``fn`` until it stops raising OSError. Windows keeps a file locked
    for a moment after its process exits, and a virus scanner can hold one."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            fn()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def _rmtree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def copy_new(bundle: Path, install: Path) -> Path:
    """Copies the new build next to the install, so the swap is two renames on
    the same disk. Returns the copy."""
    new = sibling(install, NEW_SUFFIX)
    _rmtree(new)
    shutil.copytree(bundle, new, symlinks=True)
    return new


def swap(install: Path, retry_seconds: float = RENAME_RETRY_SECONDS) -> None:
    """``<install>.new`` takes the install's place; the install becomes
    ``<install>.old``. Puts the old one back if the second rename fails."""
    new, old = sibling(install, NEW_SUFFIX), sibling(install, OLD_SUFFIX)
    if not new.exists():
        raise SwapError("The new build wasn't copied.")
    _rmtree(old)  # a leftover from an earlier update
    try:
        _retry(lambda: os.rename(install, old), retry_seconds)
    except OSError as exc:
        raise SwapError(f"The installed app is still in use ({exc.strerror or exc}). "
                        "Close every Architecture Validator window and try again.") from exc
    try:
        _retry(lambda: os.rename(new, install), retry_seconds)
    except OSError as exc:
        os.rename(old, install)
        raise SwapError(f"Couldn't move the new build into place ({exc}).") from exc


def rollback(install: Path, retry_seconds: float = RENAME_RETRY_SECONDS) -> None:
    """Puts ``<install>.old`` back. The build that failed is kept as ``.failed``."""
    old, failed = sibling(install, OLD_SUFFIX), sibling(install, FAILED_SUFFIX)
    if not old.exists():
        raise SwapError("The previous version is gone; it can't be restored.")
    _rmtree(failed)
    if install.exists():
        _retry(lambda: os.rename(install, failed), retry_seconds)
    _retry(lambda: os.rename(old, install), retry_seconds)


def writable(folder: Path) -> bool:
    """Whether this user can create and rename things in ``folder``."""
    probe = folder / f".arch-update-probe-{os.getpid()}"
    try:
        probe.mkdir()
        probe.rmdir()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Windows elevation for the renames only
# ---------------------------------------------------------------------------

def _run_elevated(argv: list[str]) -> int:
    """Runs ``argv`` with a UAC prompt, waits, and returns its exit code
    (1223 when the user declines)."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = argv[0]
    info.lpParameters = subprocess.list2cmdline(argv[1:])
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return ctypes.GetLastError() or 1
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return int(code.value)


def install_build(bundle: Path, install: Path) -> None:
    """Copies the new build next to the install and swaps it in."""
    copy_new(bundle, install)
    swap(install)


def _run_step(plan: dict, step: str) -> None:
    install = Path(plan["install"])
    if step == "install":
        install_build(Path(plan["bundle"]), install)
    elif step == "rollback":
        rollback(install)
    else:
        raise SwapError(f"Unknown update step: {step}")


def _step(plan_file: Path, plan: dict, step: str, elevate: bool) -> None:
    """Runs a step here, or in an elevated copy of this program (one UAC
    prompt for the install; a second only if it has to be rolled back)."""
    if not elevate:
        _run_step(plan, step)
        return
    code = _run_elevated([sys.executable, FLAG, str(plan_file), STEP_FLAG, step])
    if code == 1223:  # ERROR_CANCELLED
        raise SwapError("The update needs administrator rights, and the prompt was declined.")
    if code != 0:
        raise SwapError(f"The elevated {step} step failed (exit code {code}); see update.log.")


# ---------------------------------------------------------------------------
# The app's side: start the updater and close
# ---------------------------------------------------------------------------

def _install_location() -> Path | None:
    from backend.updates import install_location
    return install_location()


def _relative_exe(install: Path) -> str:
    """The app's program, relative to what's replaced (it's the same name in
    the new build)."""
    return os.path.relpath(Path(sys.executable).resolve(), install)


def launch(app_pid: int) -> dict:
    """Writes the plan and starts the staged build's updater. The caller closes
    the app straight after; the updater waits for that."""
    from backend.updates import staged
    from desktop import build_info

    stage = staged()
    if stage is None:
        raise SwapError("No downloaded update is ready. Download it again.")
    install = _install_location()
    if install is None or not getattr(sys, "frozen", False):
        raise SwapError("Only a packaged build can be updated in place.")
    exe = Path(stage["executable"])
    if not exe.is_file():
        raise SwapError("The downloaded update is incomplete. Download it again.")
    current = build_info.current()
    plan = {
        "app_pid": app_pid,
        "install": str(install),
        "bundle": stage["bundle"],
        "relaunch": _relative_exe(install),
        "sync_exe": os.path.relpath(
            Path(sys.executable).resolve().with_name(
                "ArchitectureValidatorSync" + (".exe" if os.name == "nt" else "")),
            install),
        "marker": str(updates_dir() / "ready.marker"),
        "from": {"version": current.get("version"), "commit": current.get("commit")},
        "to": {"version": stage.get("version"), "commit": stage.get("commit"),
               "label": stage.get("label"), "channel": stage.get("channel")},
    }
    plan_file = updates_dir() / PLAN_FILE
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    Path(plan["marker"]).unlink(missing_ok=True)
    logger.info("Starting the updater: %s", exe)
    # cwd outside the install folder, or Windows keeps that folder busy.
    _popen([str(exe), FLAG, str(plan_file)], cwd=str(updates_dir()))
    return plan


def mark_ready(marker: str | None) -> None:
    """Tells a waiting updater that the new build works: called once the app's
    page has loaded, with the path the updater passed in ``MARKER_ENV``."""
    if not marker:
        return
    try:
        Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logger.warning("Couldn't write the update marker", exc_info=True)


def cleanup_after_update(install: Path | None = None, wait: float = 600) -> None:
    """Removes what an update leaves behind once the new build runs: the old
    build and the staging folder the updater ran from. Best effort.

    The updater may still be running (it waits for this app to report ready,
    and can still roll back to ``.old``), so this first waits for it to exit.
    Run it on a background thread."""
    install = install or _install_location()
    staging = updates_dir()
    pid_file = staging / UPDATER_PID_FILE
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and not wait_for_exit(pid, wait, interval=1.0):
            return  # try again next start
    if install is not None:
        for suffix in (OLD_SUFFIX, NEW_SUFFIX):
            _rmtree(sibling(install, suffix))
    # A downloaded update not yet installed stays; one that's been used goes.
    if (staging / PLAN_FILE).exists() or not (staging / "staged.json").exists():
        shutil.rmtree(staging, ignore_errors=True)


def start_cleanup() -> None:
    """Runs cleanup_after_update on a background thread, in a packaged build."""
    if not getattr(sys, "frozen", False):
        return
    import threading
    threading.Thread(target=cleanup_after_update, name="update-cleanup", daemon=True).start()


# ---------------------------------------------------------------------------
# The updater's side
# ---------------------------------------------------------------------------

def _stop_sync_worker() -> bool:
    """Asks the Sync Worker to exit and waits. Returns whether it was running."""
    try:
        from Application_Logic.Logic_Sync_Worker import request_quit, worker_pid
        from desktop.sync_worker import sync_store
        store = sync_store()
        pid = worker_pid(store)
    except Exception:  # noqa: BLE001 — no sync set up is not a failure
        logger.warning("Couldn't check the Sync Worker", exc_info=True)
        return False
    if not pid:
        return False
    logger.info("Asking the Sync Worker (pid %s) to exit", pid)
    request_quit(store)
    if not wait_for_exit(pid, SYNC_EXIT_TIMEOUT):
        raise SwapError("The Sync Worker didn't exit; it may still be syncing. "
                        "Try the update again in a minute.")
    return True


def _start_app(install: Path, plan: dict) -> subprocess.Popen:
    exe = install / plan["relaunch"]
    env = dict(os.environ)
    env[MARKER_ENV] = plan["marker"]
    return _popen([str(exe)], env=env, cwd=str(exe.parent))


def _wait_ready(proc, marker: Path, timeout: float = READY_TIMEOUT) -> bool:
    """True once the new app reports ready; False if it exits first. Still
    starting at the timeout counts as working: it's not been seen to fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        if proc.poll() is not None:
            return marker.exists()
        time.sleep(0.5)
    return True


def _write_result(ok: bool, plan: dict, error: str = "") -> None:
    data = {"ok": ok, "from": plan.get("from"), "to": plan.get("to"), "error": error,
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        result_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Couldn't write the update result", exc_info=True)


def _setup_logging() -> None:
    _app_data().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(_app_data() / "update.log", encoding="utf-8")],
        force=True)


def apply(plan_file: Path, step: str | None = None) -> int:
    """``--apply-update``: the updater, run from the staged new build."""
    _setup_logging()
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    install = Path(plan["install"])

    if step:  # the elevated helper: one step and nothing else
        try:
            _run_step(plan, step)
            return 0
        except Exception:  # noqa: BLE001
            logger.exception("Elevated %s failed", step)
            return 2

    logger.info("Updating %s from %s to %s", install, plan.get("from"), plan.get("to"))
    try:
        (Path(plan_file).parent / UPDATER_PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    sync_was_running = False
    swapped = False
    elevate = False
    try:
        if not wait_for_exit(int(plan["app_pid"]), APP_EXIT_TIMEOUT):
            raise SwapError("The app didn't close, so it couldn't be replaced.")
        sync_was_running = _stop_sync_worker()
        elevate = os.name == "nt" and not writable(install.parent)
        logger.info("Installing the new build (elevated: %s)", elevate)
        if not elevate and not writable(install.parent):
            raise SwapError(f"{install.parent} can't be written to. Move the app somewhere "
                            "you can write, or update it from GitHub.")
        _step(plan_file, plan, "install", elevate)
        swapped = True
        logger.info("Swapped; starting the new build")
        proc = _start_app(install, plan)
        if not _wait_ready(proc, Path(plan["marker"])):
            raise SwapError("The new version closed before it finished starting, so the "
                            "previous version was restored. See update.log and crash.log.")
        _write_result(True, plan)
        logger.info("Update finished")
        code = 0
    except Exception as exc:  # noqa: BLE001 — the user must always get an app back
        logger.exception("Update failed")
        message = str(exc) if isinstance(exc, SwapError) else f"{type(exc).__name__}: {exc}"
        if swapped:
            try:
                _step(plan_file, plan, "rollback", elevate)
            except Exception:  # noqa: BLE001
                logger.exception("Rollback failed")
                message += " Restoring the previous version also failed: reinstall from GitHub."
        _rmtree(sibling(install, NEW_SUFFIX))
        _write_result(False, plan, message)
        try:
            _start_app(install, plan)
        except OSError:
            logger.exception("Couldn't start the app again")
        code = 1
    if sync_was_running:
        sync = install / plan["sync_exe"]
        try:
            _popen([str(sync), "--sync-worker"], cwd=str(sync.parent))
        except OSError:
            logger.warning("Couldn't start the Sync Worker again", exc_info=True)
    return code


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    plan_file = Path(argv[argv.index(FLAG) + 1])
    step = argv[argv.index(STEP_FLAG) + 1] if STEP_FLAG in argv else None
    return apply(plan_file, step)
