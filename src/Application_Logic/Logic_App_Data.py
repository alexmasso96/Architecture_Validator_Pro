"""
Where this user's app data lives. Kept apart from the prefs router so the Sync
Worker can find its folder without loading the web server.

The prefs file can be moved with ``ARCH_PREFS_FILE`` (used by tests), and the
rest of the app data moves with it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "ArchitectureValidator"


def prefs_file() -> Path:
    """Per-user app-data location for the prefs file (platform-appropriate)."""
    override = os.environ.get("ARCH_PREFS_FILE")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_DIR_NAME / "prefs.json"


def app_data_dir() -> Path:
    """This user's app-data folder, for state that belongs to the machine."""
    return prefs_file().parent
