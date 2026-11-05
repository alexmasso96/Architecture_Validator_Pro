"""Writes build_info.json at the repo root for the desktop spec to bundle.

    python scripts/write_build_info.py

Run in CI before PyInstaller. It records the version, the commit and branch, and
the Actions run, so the app can tell which build it is and whether a newer build
of its branch exists. See src/desktop/build_info.py.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    with open(ROOT / "src" / "frontend" / "package.json", encoding="utf-8") as f:
        version = json.load(f)["version"]
    # On a pull request GITHUB_SHA is a merge commit; the head branch's commit is
    # what a branch build is compared against.
    commit = os.environ.get("BUILD_COMMIT") or os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD")
    branch = (os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")
              or _git("rev-parse", "--abbrev-ref", "HEAD"))
    info = {
        "version": version,
        "commit": commit,
        "branch": branch,
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "updater": 1,
    }
    (ROOT / "build_info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info))


if __name__ == "__main__":
    main()
