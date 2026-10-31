"""
Start the Sync Worker at login (#16)
====================================
Each user opts in from the worker's own menu, so sync keeps running without the
app. Everything stays in the user's own account: a LaunchAgent on macOS, a value
under HKEY_CURRENT_USER on Windows, and an autostart entry on Linux.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import sys
from pathlib import Path
from typing import Optional

LAUNCH_LABEL = "com.architecture.validator.syncworker"
RUN_VALUE = "ArchitectureValidatorSync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DESKTOP_FILE = "architecture-validator-sync.desktop"


def _launch_agent(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def _desktop_entry(home: Path) -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    return (Path(config) if config else home / ".config") / "autostart" / DESKTOP_FILE


def is_enabled(platform: str = sys.platform, home: Optional[Path] = None, registry=None) -> bool:
    home = home or Path.home()
    if platform == "win32":
        winreg = registry or __import__("winreg")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except OSError:
            return False
    if platform == "darwin":
        return _launch_agent(home).exists()
    return _desktop_entry(home).exists()


def enable(argv: list, platform: str = sys.platform, home: Optional[Path] = None,
           registry=None) -> None:
    home = home or Path.home()
    if platform == "win32":
        winreg = registry or __import__("winreg")
        command = " ".join(f'"{a}"' if " " in a else a for a in argv)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    elif platform == "darwin":
        path = _launch_agent(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            plistlib.dump({"Label": LAUNCH_LABEL, "ProgramArguments": argv,
                           "RunAtLoad": True, "ProcessType": "Interactive"}, f)
    else:
        path = _desktop_entry(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Desktop Entry]\nType=Application\n"
                        "Name=Architecture Validator Sync\n"
                        f"Exec={shlex.join(argv)}\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")


def disable(platform: str = sys.platform, home: Optional[Path] = None, registry=None) -> None:
    home = home or Path.home()
    if platform == "win32":
        winreg = registry or __import__("winreg")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass
    else:
        path = _launch_agent(home) if platform == "darwin" else _desktop_entry(home)
        path.unlink(missing_ok=True)
