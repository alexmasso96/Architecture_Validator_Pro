"""
Phase 3 desktop shell: native pywebview window over the FastAPI worker (plan §5).

    python -m desktop.main

Spawns the worker process, waits for it to report its port and become ready,
then opens a native window pointing at the worker's statically-served React
build. The session token and native file dialogs are exposed to the SPA through
pywebview's ``js_api`` (so the token never travels over HTTP).
"""
from __future__ import annotations

import faulthandler
import json
import logging
import os
import platform
import sys
from pathlib import Path

import webview

from backend.security import generate_token
from desktop.worker import spawn_worker, wait_until_ready

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Architecture Validator"


def _log_dir() -> Path:
    """Per-user, always-writable directory for the crash log.

    A frozen onedir app installed under Program Files (or run from a read-only
    share) can't write next to its .exe, so logs go to the user profile:
    ``%LOCALAPPDATA%\\ArchitectureValidator`` on Windows, ``~/Library/Logs/...``
    on macOS, ``$XDG_STATE_HOME`` (or ``~/.local/state``) elsewhere.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = Path(base) / "ArchitectureValidator"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Logs" / "ArchitectureValidator"
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        d = Path(base) / "ArchitectureValidator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_crash_logging() -> Path:
    """Route logs + low-level fatal faults to a file so a windowed (no-console)
    build is never a *silent* crash.

    Windowed PyInstaller builds have no stdout/stderr, so an exception during
    startup (e.g. the pywebview winforms/pythonnet backend failing to load .NET)
    vanishes with no trace. We attach a file handler and arm ``faulthandler`` —
    which dumps native-level tracebacks for hard crashes the Python ``except``
    can't catch — both pointed at ``<log_dir>/``.
    """
    log_path = _log_dir() / "crash.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Also echo to stderr so the ARCH_BUILD_CONSOLE debug build still prints live.
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
    # Keep this handle open for the whole run so faulthandler can write into it
    # from a fault/signal context.
    fault_fp = open(log_path.with_name("faulthandler.log"), "w", encoding="utf-8")
    faulthandler.enable(file=fault_fp)
    return log_path


# --- Windows runtime preflight --------------------------------------------
# The pywebview winforms backend hard-depends on two Microsoft runtimes that
# PyInstaller cannot bundle: the Edge WebView2 Runtime (renders the SPA) and
# .NET Framework 4.7.2+ (pythonnet/clr_loader bootstrap). On a machine missing
# either, the app otherwise dies with an opaque .NET loader error or a blank
# white window. We detect them via the registry and, if absent, show a native
# dialog that links to Microsoft's download page instead of crashing.

# Microsoft's download links (fwlink IDs are stable evergreen redirects).
_WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_DOTNET_FX_DOWNLOAD = "https://go.microsoft.com/fwlink/?LinkId=2085155"  # .NET Fx 4.8 web installer
_DOTNET_DESKTOP_ARM64_DOWNLOAD = (
    "https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-arm64.exe"
)


def _is_windows_arm64() -> bool:
    """True when running a *native* ARM64 Python on Windows.

    An x64 Python emulated on Windows-on-ARM reports AMD64 and correctly keeps
    the .NET Framework (netfx) path, which works under emulation.
    """
    return sys.platform == "win32" and platform.machine().upper() == "ARM64"


def _dotnet_desktop_runtime_installed() -> bool:
    """True if a .NET (Core) Windows Desktop shared framework is installed."""
    root = Path(
        os.environ.get("DOTNET_ROOT")
        or Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "dotnet"
    )
    shared = root / "shared" / "Microsoft.WindowsDesktop.App"
    try:
        return shared.is_dir() and any(shared.iterdir())
    except OSError:
        return False


def _sanitize_host_python_env() -> None:
    """Drop host-machine Python configuration from a frozen bundle's environment.

    Work machines often carry several (sometimes custom) Python installs whose
    tooling sets global PYTHON*/PYTHONNET_* variables. The frozen interpreter
    itself is immune (PyInstaller boots it in isolated mode), but pythonnet
    reads PYTHONNET_* from the environment at ``import clr``: a stray
    ``PYTHONNET_PYDLL`` binds a *foreign* Python DLL into this process, which
    dies as a native crash with no Python traceback — a silent exit on a
    windowed build. Set ``ARCH_KEEP_PYTHON_ENV=1`` to skip the scrub when
    deliberately experimenting with runtime overrides.
    """
    if not getattr(sys, "frozen", False) or os.environ.get("ARCH_KEEP_PYTHON_ENV"):
        return
    for var in (
        "PYTHONNET_PYDLL",
        "PYTHONNET_RUNTIME",
        "PYTHONNET_CORECLR_RUNTIME_CONFIG",
        "PYTHONNET_SHUTDOWN_MODE",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
    ):
        val = os.environ.pop(var, None)
        if val is not None:
            logger.warning(
                "Ignoring host environment %s=%s (frozen bundle uses its own runtime)",
                var,
                val,
            )


def _configure_dotnet_runtime() -> None:
    """Route pythonnet to CoreCLR on native Windows-on-ARM.

    clr_loader ships no ARM64 build of its .NET Framework bootstrap shim
    (only amd64/x86 — see pythonnet/pythonnet#2523), so on a native ARM64
    Python the default netfx path dies with ``error 0xc1`` (wrong-architecture
    DLL) before any window appears. CoreCLR works, but pythonnet's default
    runtimeconfig only references ``Microsoft.NETCore.App``, which lacks
    WinForms — so we point it at ``Microsoft.WindowsDesktop.App`` instead.

    Must run before anything triggers ``import clr`` (pywebview's winforms
    backend does, inside ``webview.start()``). A user-provided
    ``PYTHONNET_RUNTIME`` always wins.
    """
    if not _is_windows_arm64() or os.environ.get("PYTHONNET_RUNTIME"):
        return
    cfg = _log_dir() / "coreclr.runtimeconfig.json"
    cfg.write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "tfm": "net8.0",
                    "rollForward": "LatestMajor",
                    "framework": {
                        "name": "Microsoft.WindowsDesktop.App",
                        "version": "8.0.0",
                    },
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.environ["PYTHONNET_RUNTIME"] = "coreclr"
    os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"] = str(cfg)

    # On CoreCLR, Microsoft.Win32.SystemEvents lives in its own assembly that
    # pywebview never references explicitly (on .NET Framework the type sits
    # in System.dll, so netfx machines never need this). Preload it so
    # winforms.py's ``from Microsoft.Win32 import SystemEvents`` resolves.
    import clr

    clr.AddReference("Microsoft.Win32.SystemEvents")


def _unblock_bundled_assemblies() -> None:
    """Strip the NTFS Zone.Identifier stream (Mark of the Web) from bundled
    .NET assemblies in a frozen build.

    When the onedir folder is distributed as a zip and extracted from a
    browser download, every file carries MOTW, and .NET Framework refuses to
    load ``Python.Runtime.dll`` from the "internet zone" — which surfaces as
    the opaque ``Failed to resolve Python.Runtime.Loader.Initialize`` crash
    seen on target machines. Deleting the stream is exactly what Explorer's
    "Unblock" checkbox does.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for pattern in ("pythonnet/**/*.dll", "clr_loader/**/*.dll", "webview/lib/**/*.dll"):
        for dll in base.glob(pattern):
            try:
                os.remove(f"{dll}:Zone.Identifier")
            except OSError:
                pass  # no stream present (the normal case) or read-only medium


def _show_fatal_dialog(message: str) -> None:
    """Last-resort native error box so a windowed build never dies silently."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        MB_OK, MB_ICONERROR = 0x0, 0x10
        ctypes.windll.user32.MessageBoxW(
            None, message, "Architecture Validator — startup failed", MB_OK | MB_ICONERROR
        )
    except Exception:
        logger.exception("Could not display the fatal-error dialog")


def _webview2_installed() -> bool:
    """True if the Edge WebView2 Runtime is registered (any architecture).

    Per Microsoft's detection guidance, the runtime writes a non-empty ``pv``
    (product version) under the EdgeUpdate client GUID, machine- or user-wide.
    """
    import winreg

    client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + client),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + client),
        (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + client),
    )
    for root, sub in candidates:
        try:
            with winreg.OpenKey(root, sub) as key:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv and pv not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


def _dotnet_fx_installed() -> bool:
    """True if .NET Framework 4.7.2 or newer is installed.

    The single ``Release`` DWORD under NDP\\v4\\Full encodes the version;
    461808 is the 4.7.2 threshold (4.8 = 528040). .NET 4.x is in-place, so any
    value at/above the threshold is sufficient.
    """
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
        ) as key:
            release, _ = winreg.QueryValueEx(key, "Release")
            return int(release) >= 461808
    except (OSError, ValueError):
        return False


def _windows_prereqs_ok() -> bool:
    """Preflight the Windows-only runtime deps; warn + link to downloads if any
    are missing. Returns False when the app cannot run (caller should exit).

    No-op (returns True) off Windows, where the GTK / WKWebView backends are used.
    """
    if sys.platform != "win32":
        return True

    missing: list[tuple[str, str]] = []
    if not _webview2_installed():
        missing.append(("Microsoft Edge WebView2 Runtime", _WEBVIEW2_DOWNLOAD))
    if _is_windows_arm64():
        # Native ARM64 cannot use .NET Framework (no ARM64 clr_loader shim);
        # pythonnet runs on CoreCLR and needs the Windows Desktop runtime.
        if not _dotnet_desktop_runtime_installed():
            missing.append(
                (".NET Desktop Runtime 8.0 (ARM64)", _DOTNET_DESKTOP_ARM64_DOWNLOAD)
            )
    elif not _dotnet_fx_installed():
        missing.append((".NET Framework 4.8", _DOTNET_FX_DOWNLOAD))
    if not missing:
        return True

    logger.error("Missing Windows runtime prerequisites: %s", [n for n, _ in missing])
    detail = "\n".join(f"• {name}\n    {url}" for name, url in missing)
    message = (
        "Architecture Validator needs the following Microsoft runtime "
        "component(s), which are not installed on this machine:\n\n"
        f"{detail}\n\n"
        "Click OK to open the download page(s), then install and relaunch."
    )
    try:
        import ctypes

        MB_OKCANCEL, MB_ICONWARNING, IDOK = 0x1, 0x30, 1
        choice = ctypes.windll.user32.MessageBoxW(
            None, message, "Missing prerequisites", MB_OKCANCEL | MB_ICONWARNING
        )
        if choice == IDOK:
            import webbrowser

            for _, url in missing:
                webbrowser.open(url)
    except Exception:
        # Headless / no user32 — the message is already in crash.log.
        logger.exception("Could not display the prerequisites dialog")
    return False


def _file_types(exts: list[str] | None) -> tuple[str, ...]:
    """Turn raw extensions (``[".elf", ".json"]``) into pywebview file filters.

    pywebview requires every ``file_types`` entry to match the wildcard form
    ``"Description (*.ext;*.ext2)"`` — handing it a bare ``".arch"`` raises a
    ``ValueError`` that surfaced to the SPA as a rejected promise, so the native
    picker appeared to "do nothing". We build a combined "Supported" filter plus
    an "All files" escape hatch.
    """
    cleaned = [e.lstrip(".").lower() for e in (exts or []) if e and e.strip(".")]
    if not cleaned:
        return ("All files (*.*)",)
    patterns = ";".join(f"*.{e}" for e in cleaned)
    return (f"Supported files ({patterns})", "All files (*.*)")


class JsApi:
    """Bridge exposed to the SPA as ``window.pywebview.api``.

    ``get_token`` hands the per-session bearer token to the frontend without it
    ever crossing HTTP. The ``pick_*`` methods open real OS dialogs so the app
    can resolve actual filesystem paths (needed for ``.arch``/ELF/source on
    network shares — browser file inputs can't).
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._window: webview.Window | None = None

    def set_window(self, window: webview.Window) -> None:
        self._window = window

    def get_token(self) -> str:
        return self._token

    def set_title(self, title: str) -> None:
        # Best-effort native title sync (document.title already drives this on
        # most backends; this guarantees it on the rest).
        if self._window and title:
            try:
                self._window.set_title(title)
            except Exception:
                pass

    def pick_folder(self) -> str | None:
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else None

    def pick_open_file(self, file_types: list[str] | None = None) -> str | None:
        res = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=_file_types(file_types),
        )
        return res[0] if res else None

    def pick_save_file(self, default_name: str = "") -> str | None:
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=default_name or ""
        )
        if not res:
            return None
        return res if isinstance(res, str) else res[0]


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Fail fast with a helpful dialog if the Windows webview/.NET runtimes are
    # missing, rather than crashing deep inside the pywebview backend.
    if not _windows_prereqs_ok():
        raise SystemExit(2)

    # De-MOTW bundled .NET assemblies (zip-extracted downloads), drop hostile
    # host Python config, and pick the right .NET runtime for this CPU — all
    # before pywebview loads .NET.
    _unblock_bundled_assemblies()
    _sanitize_host_python_env()
    _configure_dotnet_runtime()

    # Pin WebView2's user-data folder to a guaranteed per-user writable location.
    # Unset, WebView2 creates its profile next to the .exe; when the app lives on
    # a read-only/restricted path (Program Files, a VM shared folder, a locked-down
    # extract) the non-admin user can't write there, so the browser process never
    # starts and the window stays blank white — while running as admin "fixes" it.
    # setdefault honours a user-provided WEBVIEW2_USER_DATA_FOLDER if one is set.
    if sys.platform == "win32":
        os.environ.setdefault(
            "WEBVIEW2_USER_DATA_FOLDER", str(_log_dir() / "WebView2")
        )

    token = generate_token()

    proc, port, lifeline = spawn_worker(token)
    url = f"http://127.0.0.1:{port}"
    logger.info("Worker started on %s", url)

    if not wait_until_ready(port):
        lifeline.close()
        proc.terminate()
        proc.join(timeout=5)
        logger.error("Worker failed to become ready; aborting.")
        _show_fatal_dialog(
            "The background worker process did not become ready in time.\n\n"
            "Common causes: endpoint-protection software delaying first "
            "launch, or a proxy/firewall interfering with local connections.\n\n"
            f"The exact reason is logged in:\n{_log_dir() / 'crash.log'}"
        )
        raise SystemExit(1)

    # Breadcrumbs: a hard native crash (CLR/WebView2) or an external kill (EDR)
    # produces no Python traceback, so these log lines are the only way to see
    # how far startup got on a machine where the shell dies silently.
    logger.info("Worker ready; creating native window (pywebview shell)")
    api = JsApi(token)
    window = webview.create_window(
        WINDOW_TITLE,
        # ?desktop=1 marks the desktop shell (native dialogs available). The
        # session token rides in the URL *fragment* (after '#'): the webview reads
        # it synchronously at startup, and fragments are never sent to the server
        # in the HTTP request — so the token still never travels over HTTP, but we
        # avoid the fragile async js_api bridge that left every call unauthorised
        # if the bridge wasn't ready when the first request fired.
        f"{url}/?desktop=1#token={token}",
        js_api=api,
        width=1280,
        height=820,
        min_size=(900, 600),
    )
    api.set_window(window)

    logger.info("Entering webview.start() — .NET/WebView2 bootstrap happens now")
    try:
        webview.start()
        logger.info("webview.start() returned cleanly (window closed); shutting down")
    finally:
        # Closing the lifeline triggers the worker's graceful shutdown, which
        # runs the FastAPI lifespan finally → releases the .arch edit lock.
        lifeline.close()
        proc.join(timeout=8)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)
        if proc.is_alive():
            proc.kill()


if __name__ == "__main__":
    import multiprocessing

    # Required so the spawned worker re-executes correctly inside a frozen
    # (PyInstaller) bundle; a no-op in a normal interpreter.
    multiprocessing.freeze_support()

    # Arm crash logging before anything else can fail. A windowed build has no
    # console, so without this an early failure (e.g. the Windows winforms/
    # pythonnet backend failing to bootstrap .NET) is a *silent* crash. Any
    # uncaught exception is written, with full traceback, to <log_dir>/crash.log.
    _crash_log = _setup_crash_logging()
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        logger.exception("Fatal error during startup")
        sys.stderr.write(f"Fatal error — see log: {_crash_log}\n")
        # A windowed (no-console) build must never fail invisibly: the process
        # otherwise appears briefly in Task Manager and vanishes with no trace.
        _show_fatal_dialog(
            f"{type(exc).__name__}: {exc}\n\nFull traceback written to:\n{_crash_log}"
        )
        raise
