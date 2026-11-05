"""
In-app updates: find the build this machine should run, download it, check it
and unpack it ready for desktop/updater.py to install.

Two channels:

* ``release`` — the newest published GitHub release. Its asset for this OS is
  downloaded and checked against the SHA-256 digest GitHub records for it.
* ``branch`` — the newest CI build of a branch (an Actions artifact of the
  "Build Desktop App" workflow). Handy while developing: no need to download
  artifacts by hand. GitHub only serves artifacts to a signed-in client, so this
  needs a GitHub token (Preferences → Updates), stored encrypted. Each artifact
  also carries a SHA-256 digest, checked the same way.

Everything runs in the worker so ``requests`` uses the system proxy (see net.py).
Only URLs that come back from the GitHub API for this repository are fetched;
the SPA never passes one in.

Only the Windows and macOS builds are updated in place for now. Linux packages
and runs from source report why they can't be.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from Application_Logic import Logic_AI_Credentials as credentials
from Application_Logic.Logic_App_Data import app_data_dir
from desktop import build_info

logger = logging.getLogger(__name__)

GITHUB_REPO = "alexmasso96/Architecture_Validator_Pro"
API = f"https://api.github.com/repos/{GITHUB_REPO}"
WORKFLOW_FILE = "build-desktop.yml"

CHANNEL_RELEASE = "release"
CHANNEL_BRANCH = "branch"
CHANNELS = (CHANNEL_RELEASE, CHANNEL_BRANCH)

PREF_CHANNEL = "update.channel"
PREF_BRANCH = "update.branch"

TIMEOUT = 15
CHUNK = 1 << 20
# How many recent runs of a branch to look through for one with this OS's build.
RUNS_TO_SEARCH = 10

STAGED_FILE = "staged.json"


class UpdateError(Exception):
    """A failure the user should see, with a message that says what to do."""


# ---------------------------------------------------------------------------
# This machine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """Which build this machine runs: the release asset and the CI artifact that
    hold it, and the program inside."""
    key: str
    release_asset: str
    artifact: str
    exe_name: str


TARGETS = {
    "windows-x64": Target("windows-x64", "architecture-validator-desktop-windows-x64.zip",
                          "desktop-windows-artifact-windows-latest", "ArchitectureValidator.exe"),
    "windows-arm64": Target("windows-arm64", "architecture-validator-desktop-windows-arm64.zip",
                            "desktop-windows-artifact-windows-11-arm", "ArchitectureValidator.exe"),
    "macos": Target("macos", "architecture-validator-desktop-macos.zip",
                    "desktop-macos-artifact", "ArchitectureValidator.app"),
}


def target_key(sys_platform: str | None = None, machine: str | None = None) -> str | None:
    sys_platform = sys_platform or sys.platform
    machine = (machine or platform.machine()).upper()
    if sys_platform == "win32":
        # An x64 Python emulated on Windows-on-ARM reports AMD64 and keeps
        # running the x64 build, which is what it is.
        return "windows-arm64" if machine == "ARM64" else "windows-x64"
    if sys_platform == "darwin":
        return "macos"
    return None


def current_target() -> Target | None:
    key = target_key()
    return TARGETS.get(key) if key else None


def install_location(executable: str | None = None, sys_platform: str | None = None) -> Path | None:
    """What an update replaces: the onedir folder on Windows, the .app on macOS."""
    exe = Path(executable or sys.executable).resolve()
    sys_platform = sys_platform or sys.platform
    if sys_platform == "darwin":
        # .../ArchitectureValidator.app/Contents/MacOS/ArchitectureValidator
        app = exe.parents[2] if len(exe.parents) > 2 else None
        return app if app is not None and app.suffix == ".app" else None
    return exe.parent


def install_support() -> dict:
    """Whether this build can update itself, and if not, why."""
    info = build_info.current()
    if not info.get("frozen"):
        return {"supported": False,
                "reason": "Running from source: updates install only in a packaged build."}
    if current_target() is None:
        return {"supported": False,
                "reason": "In-app updates are available on Windows and macOS. On Linux, "
                          "install the new package from GitHub."}
    if install_location() is None:
        return {"supported": False, "reason": "Couldn't tell where the app is installed."}
    return {"supported": True, "reason": ""}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings() -> dict:
    from .routers.prefs import read_pref

    channel = read_pref(PREF_CHANNEL) or CHANNEL_RELEASE
    if channel not in CHANNELS:
        channel = CHANNEL_RELEASE
    return {
        "channel": channel,
        "branch": read_pref(PREF_BRANCH) or "main",
        "token_configured": bool(credentials.get_github_token()),
    }


def save_settings(channel: str | None = None, branch: str | None = None,
                  token: str | None = None) -> dict:
    from .routers.prefs import write_pref

    if channel is not None:
        if channel not in CHANNELS:
            raise UpdateError(f"Unknown update channel: {channel}")
        write_pref(PREF_CHANNEL, channel)
    if branch is not None:
        branch = branch.strip()
        if not branch:
            raise UpdateError("Choose a branch.")
        write_pref(PREF_BRANCH, branch)
    if token is not None:
        credentials.set_github_token(token.strip())
    return get_settings()


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _headers(auth: bool = True) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = credentials.get_github_token() if auth else None
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get_json(url: str, params: dict | None = None):
    try:
        res = requests.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise UpdateError(f"GitHub unreachable ({exc.__class__.__name__}).") from exc
    if res.status_code == 401:
        raise UpdateError("GitHub rejected the token. Check it in Preferences → Updates.")
    if res.status_code == 403 and res.headers.get("X-RateLimit-Remaining") == "0":
        raise UpdateError("GitHub's rate limit was reached. Add a GitHub token, or try later.")
    if res.status_code == 404:
        raise UpdateError("Not found on GitHub.")
    if not res.ok:
        raise UpdateError(f"GitHub API responded {res.status_code}.")
    return res.json()


def _digest(value) -> str | None:
    """``"sha256:ab12…"`` → ``"ab12…"``; anything else → None."""
    if isinstance(value, str) and value.lower().startswith("sha256:"):
        hexd = value.split(":", 1)[1].strip().lower()
        if len(hexd) == 64 and all(c in "0123456789abcdef" for c in hexd):
            return hexd
    return None


def latest_release() -> dict:
    """The newest published release: ``{tag_name, body, assets, html_url, published_at}``."""
    data = _get_json(f"{API}/releases/latest")
    return {
        "tag_name": str(data.get("tag_name") or ""),
        "body": str(data.get("body") or ""),
        "html_url": str(data.get("html_url") or ""),
        "published_at": data.get("published_at"),
        "assets": list(data.get("assets") or []),
        # CI publishes with target_commitish = the commit it built.
        "commit": _commit_sha(data.get("target_commitish")),
    }


def _commit_sha(value) -> str | None:
    if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower()):
        return value.lower()
    return None


def list_branches() -> list[str]:
    names: list[str] = []
    for page in range(1, 4):
        batch = _get_json(f"{API}/branches", {"per_page": 100, "page": page})
        names += [str(b.get("name")) for b in batch if b.get("name")]
        if len(batch) < 100:
            break
    # main first, the rest alphabetical.
    return sorted(set(names), key=lambda n: (n != "main", n.lower()))


def latest_branch_build(branch: str, target: Target) -> dict:
    """The newest run of the desktop workflow on ``branch`` that still has this
    OS's build. A run whose tests failed still counts if its build finished,
    since the build is what's installed; its conclusion is reported."""
    runs = _get_json(f"{API}/actions/workflows/{WORKFLOW_FILE}/runs",
                     {"branch": branch, "status": "completed", "per_page": RUNS_TO_SEARCH})
    for run in runs.get("workflow_runs") or []:
        arts = _get_json(f"{API}/actions/runs/{run['id']}/artifacts",
                         {"name": target.artifact, "per_page": 5})
        artifact = next((a for a in arts.get("artifacts") or [] if not a.get("expired")), None)
        if artifact is None:
            continue
        return {
            "run_id": run["id"],
            "run_number": run.get("run_number"),
            "commit": run.get("head_sha"),
            "branch": run.get("head_branch") or branch,
            "title": run.get("display_title") or "",
            "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"),
            "html_url": run.get("html_url"),
            "artifact": {
                "id": artifact["id"],
                "name": artifact.get("name"),
                "size": artifact.get("size_in_bytes"),
                "sha256": _digest(artifact.get("digest")),
                "url": artifact.get("archive_download_url"),
            },
        }
    raise UpdateError(f"No recent build of '{branch}' has a {target.key} build.")


def _compare_versions(a: str, b: str) -> int:
    def parse(v: str) -> list[int]:
        parts = v.strip().lstrip("vV").split("-")[0].split(".")
        return [int(p) if p.isdigit() else 0 for p in parts]
    pa, pb = parse(a), parse(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)


def check() -> dict:
    """What's available on the chosen channel, compared with this build."""
    settings = get_settings()
    info = build_info.current()
    target = current_target()
    out = {
        "channel": settings["channel"],
        "branch": settings["branch"],
        "token_configured": settings["token_configured"],
        "current": {k: info.get(k) for k in ("version", "commit", "branch", "run_id", "frozen")},
        "install": install_support(),
        "available": False,
        "latest": None,
    }

    if settings["channel"] == CHANNEL_RELEASE:
        rel = latest_release()
        if not rel["tag_name"]:
            raise UpdateError("The latest release has no version tag.")
        version = rel["tag_name"].lstrip("vV")
        asset = next((a for a in rel["assets"] if target and a.get("name") == target.release_asset), None)
        out["latest"] = {
            "version": version,
            "tag": rel["tag_name"],
            "label": f"v{version}",
            "body": rel["body"],
            "url": rel["html_url"],
            "published_at": rel["published_at"],
            "downloadable": asset is not None,
            "size": asset.get("size") if asset else None,
        }
        newer = _compare_versions(version, str(info["version"]))
        # Same version, different commit: this is a branch build, and the user
        # has switched back to releases, so the release is offered.
        mine = (info.get("commit") or "").lower()
        out["available"] = newer > 0 or (
            newer == 0 and bool(mine) and bool(rel["commit"]) and rel["commit"] != mine)
        return out

    if target is None:
        raise UpdateError("Branch builds are installed in place only on Windows and macOS.")
    build = latest_branch_build(settings["branch"], target)
    commit = build["commit"] or ""
    out["latest"] = {
        "version": None,
        "label": f"{build['branch']}@{commit[:7]}",
        "commit": commit,
        "title": build["title"],
        "run_number": build["run_number"],
        "conclusion": build["conclusion"],
        "published_at": build["created_at"],
        "url": build["html_url"],
        "downloadable": True,
        "size": build["artifact"]["size"],
        "body": "",
    }
    # A build is "newer" when it's a different commit, or this build doesn't
    # know its own (a release from before build_info existed).
    out["available"] = bool(commit) and commit != (info.get("commit") or "")
    return out


# ---------------------------------------------------------------------------
# Download, check and stage
# ---------------------------------------------------------------------------

def updates_dir() -> Path:
    return app_data_dir() / "updates"


def _download(url: str, dest: Path, size: int | None, auth: bool,
              progress: Callable[..., None], cancel: threading.Event, label: str) -> str:
    """Streams ``url`` to ``dest`` and returns its SHA-256 hex digest.

    ``requests`` drops the Authorization header when GitHub redirects to its
    storage host, which is what that host expects."""
    sha = hashlib.sha256()
    try:
        with requests.get(url, headers=_headers(auth) | {"Accept": "application/octet-stream"},
                          stream=True, timeout=TIMEOUT) as res:
            if res.status_code in (401, 403) and auth:
                raise UpdateError("GitHub needs a token with access to this repository's "
                                  "Actions to download branch builds (Preferences → Updates).")
            if not res.ok:
                raise UpdateError(f"Download failed: GitHub responded {res.status_code}.")
            total = size or int(res.headers.get("Content-Length") or 0) or None
            done = 0
            with open(dest, "wb") as fh:
                for chunk in res.iter_content(CHUNK):
                    if cancel.is_set():
                        raise UpdateError("Download cancelled.")
                    fh.write(chunk)
                    sha.update(chunk)
                    done += len(chunk)
                    pct = min(99.0, done * 90.0 / total) if total else None
                    progress(f"Downloading {label}… {done / 1e6:.0f} MB", pct)
    except requests.RequestException as exc:
        raise UpdateError(f"Download failed ({exc.__class__.__name__}).") from exc
    return sha.hexdigest()


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """Unpacks a zip, refusing entries that would land outside ``dest``.

    macOS uses ``ditto``, which keeps the app bundle's symlinks and permissions
    (zipfile drops both). Elsewhere zipfile, with each path checked first."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            target = (root / name).resolve()
            if target != root and root not in target.parents:
                raise UpdateError("The download contains an unsafe path; it wasn't installed.")
        if sys.platform == "darwin" and shutil.which("ditto"):
            zf.close()
            subprocess.run(["ditto", "-x", "-k", str(zip_path), str(dest)], check=True)
        else:
            zf.extractall(dest)


def find_bundle(root: Path, target: Target) -> Path | None:
    """The unpacked program: the folder holding the .exe, or the .app itself."""
    if target.exe_name.endswith(".app"):
        hits = sorted(p for p in root.rglob(target.exe_name) if p.is_dir())
    else:
        hits = sorted(p.parent for p in root.rglob(target.exe_name) if p.is_file())
    # The shallowest match is the program; anything deeper is something it carries.
    return min(hits, key=lambda p: len(p.parts)) if hits else None


def bundle_build_info(bundle: Path) -> dict | None:
    """Reads build_info.json from an unpacked build, wherever PyInstaller put it."""
    for rel in ("_internal", "Contents/Frameworks", "Contents/Resources", "."):
        data = build_info.read_file(bundle / rel / build_info.FILE_NAME)
        if data is not None:
            return data
    return None


def bundle_executable(bundle: Path, target: Target) -> Path:
    if target.exe_name.endswith(".app"):
        return bundle / "Contents" / "MacOS" / "ArchitectureValidator"
    return bundle / target.exe_name


def clear_staging() -> None:
    shutil.rmtree(updates_dir(), ignore_errors=True)


def staged() -> dict | None:
    """The build that's downloaded and ready to install, if it's still there."""
    data = build_info.read_file(updates_dir() / STAGED_FILE)
    if not data:
        return None
    if not Path(str(data.get("bundle", ""))).exists():
        return None
    return data


def download_and_stage(progress: Callable[..., None], cancel: threading.Event) -> dict:
    """The ``update_download`` job: fetch the chosen channel's build for this
    machine, check its SHA-256, unpack it and record it as ready to install."""
    support = install_support()
    if not support["supported"]:
        raise UpdateError(support["reason"])
    target = current_target()
    assert target is not None
    settings = get_settings()

    progress("Asking GitHub for the update…", None)
    if settings["channel"] == CHANNEL_RELEASE:
        rel = latest_release()
        asset = next((a for a in rel["assets"] if a.get("name") == target.release_asset), None)
        if asset is None:
            raise UpdateError(f"The latest release has no {target.key} build.")
        url = asset.get("browser_download_url")
        size = asset.get("size")
        expected = _digest(asset.get("digest"))
        auth = False
        label = rel["tag_name"]
        meta = {"channel": CHANNEL_RELEASE, "version": rel["tag_name"].lstrip("vV"),
                "label": f"v{rel['tag_name'].lstrip('vV')}", "commit": None}
    else:
        if not credentials.get_github_token():
            raise UpdateError("Downloading a branch build needs a GitHub token "
                              "(Preferences → Updates).")
        build = latest_branch_build(settings["branch"], target)
        art = build["artifact"]
        url, size, expected, auth = art["url"], art["size"], art["sha256"], True
        label = f"{build['branch']}@{(build['commit'] or '')[:7]}"
        meta = {"channel": CHANNEL_BRANCH, "version": None, "label": label,
                "commit": build["commit"], "run_id": build["run_id"]}
    if not url:
        raise UpdateError("GitHub didn't give a download address for the build.")
    if not expected:
        raise UpdateError("GitHub has no SHA-256 checksum for this build, so it can't be "
                          "verified. Download it from GitHub instead.")

    clear_staging()
    work = updates_dir() / "download"
    work.mkdir(parents=True, exist_ok=True)
    archive = work / "download.zip"
    actual = _download(url, archive, size, auth, progress, cancel, label)
    if actual != expected:
        clear_staging()
        raise UpdateError("The download doesn't match GitHub's SHA-256 checksum; it was "
                          "discarded. Try again.")

    progress("Unpacking…", 92)
    unpacked = work / "unpacked"
    _safe_extract(archive, unpacked)
    archive.unlink(missing_ok=True)
    # An Actions artifact is a zip around the release zip: unpack that too.
    inner = unpacked / target.release_asset
    if inner.is_file():
        _safe_extract(inner, unpacked / "inner")
        inner.unlink()
        unpacked = unpacked / "inner"

    bundle = find_bundle(unpacked, target)
    if bundle is None:
        clear_staging()
        raise UpdateError("The download doesn't contain the app.")
    info = bundle_build_info(bundle)
    if not info or int(info.get("updater") or 0) < build_info.UPDATER_PROTOCOL:
        clear_staging()
        raise UpdateError("This build was made before in-app updates and can't install "
                          "itself. Download it from GitHub this once.")
    exe = bundle_executable(bundle, target)
    if not exe.is_file():
        clear_staging()
        raise UpdateError("The download is missing the app's program.")
    if os.name != "nt":
        exe.chmod(exe.stat().st_mode | 0o111)

    meta.update({
        "bundle": str(bundle),
        "executable": str(exe),
        "sha256": actual,
        "version": meta["version"] or info.get("version"),
        "commit": meta["commit"] or info.get("commit"),
        "build_info": info,
    })
    (updates_dir() / STAGED_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    progress("Ready to install", 100)
    return {k: meta[k] for k in ("channel", "version", "label", "commit")}


def last_result(consume: bool = True) -> Optional[dict]:
    """How the last install went, written by desktop/updater.py. Read once."""
    from desktop import updater

    path = updater.result_path()
    data = build_info.read_file(path)
    if data is not None and consume:
        path.unlink(missing_ok=True)
    return data
