"""
What this build is: its version, the commit and branch it was built from, and
whether it can install updates.

CI writes ``build_info.json`` (scripts/write_build_info.py) before PyInstaller
runs, and the spec bundles it, so a frozen build reads it from its bundle
folder. A run from source has no file: it reports the version in package.json
and no commit, and can't be updated in place.

``updater`` is the version of the update protocol the build understands: a
build with ``updater >= 1`` can install itself with ``--apply-update`` (see
desktop/updater.py). The app only installs a downloaded build that says so,
since it's the new build that does the swap.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FILE_NAME = "build_info.json"
UPDATER_PROTOCOL = 1

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def bundle_dir() -> Path:
    """Where a frozen build keeps its data files (``_internal`` in a onedir
    build, ``Contents/Frameworks`` in a macOS app)."""
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))


def _source_version() -> str:
    try:
        with open(_REPO_ROOT / "src" / "frontend" / "package.json", encoding="utf-8") as f:
            return str(json.load(f).get("version") or "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


def read_file(path: Path) -> dict | None:
    """A build_info.json as a dict, or None when it's missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def current() -> dict:
    """This build's info. Always has ``version``; ``commit`` and ``branch`` are
    None when unknown (a run from source, or a build from before this file)."""
    frozen = bool(getattr(sys, "frozen", False))
    data = read_file(bundle_dir() / FILE_NAME) if frozen else None
    data = dict(data or {})
    data.setdefault("version", _source_version())
    data.setdefault("commit", None)
    data.setdefault("branch", None)
    data.setdefault("run_id", None)
    data["frozen"] = frozen
    return data
