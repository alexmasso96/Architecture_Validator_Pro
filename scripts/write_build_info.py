"""Writes build_info.json at the repo root for the desktop spec to bundle.

    python scripts/write_build_info.py

Run in CI before PyInstaller. It records the version, the commit and branch, and
the Actions run, so the app can tell which build it is and whether a newer
version has been published. See src/desktop/build_info.py.

A build made for a release tag takes its version from the tag, so a beta knows
it is one: the tag ``V3.5.0-beta.1`` gives the version ``3.5.0-beta.1``, which
in-app updates order before ``3.5.0``. Any other build has package.json's.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


_TAG_VERSION = re.compile(r"(?:desktop-)?[vV]?(\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?)")


def release_tag(env=os.environ) -> str | None:
    """The release tag this build is for: the one typed into a manual run
    (BUILD_TAG), else the tag that was pushed. None for a branch build."""
    tag = env.get("BUILD_TAG") or (
        env.get("GITHUB_REF_NAME") if env.get("GITHUB_REF_TYPE") == "tag" else None)
    return tag if tag and _TAG_VERSION.fullmatch(tag) else None


def tag_version(tag: str) -> str:
    """``V3.5.0-beta.1`` / ``desktop-v3.5.0`` → ``3.5.0-beta.1`` / ``3.5.0``."""
    return _TAG_VERSION.fullmatch(tag).group(1)


def main() -> None:
    with open(ROOT / "src" / "frontend" / "package.json", encoding="utf-8") as f:
        version = json.load(f)["version"]
    tag = release_tag()
    if tag:
        version = tag_version(tag)
    # On a pull request GITHUB_SHA is a merge commit; the head branch's commit is
    # what a branch build is compared against.
    commit = os.environ.get("BUILD_COMMIT") or os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD")
    branch = (os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")
              or _git("rev-parse", "--abbrev-ref", "HEAD"))
    info = {
        "version": version,
        "tag": tag,
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
