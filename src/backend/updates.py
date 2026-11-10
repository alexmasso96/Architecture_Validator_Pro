"""
In-app updates: find the build this machine should run, download it, check it
and unpack it ready for desktop/updater.py to install.

Two channels, both GitHub releases of this repository:

* ``release`` — the newest published release.
* ``beta`` — the beta versions: releases published as a pre-release, each from
  its own tag (``V3.5.0-beta.1``) and each with its own change log. The user
  sees the list and picks one; the newest is offered by default. A release
  newer than every beta heads the list, so a beta user gets the final version.

A build is the release's asset for this OS, downloaded and checked against the
SHA-256 digest GitHub records for it.

Everything runs in the worker so ``requests`` uses the system proxy (see net.py).
Only URLs that come back from the GitHub API for this repository are fetched;
the SPA passes at most a tag name, which GitHub is asked about.

Only the Windows and macOS builds are updated in place for now. Linux packages
and runs from source report why they can't be.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import requests

from Application_Logic.Logic_App_Data import app_data_dir
from desktop import build_info

logger = logging.getLogger(__name__)

GITHUB_REPO = "alexmasso96/Architecture_Validator_Pro"
API = f"https://api.github.com/repos/{GITHUB_REPO}"

CHANNEL_RELEASE = "release"
CHANNEL_BETA = "beta"
CHANNELS = (CHANNEL_RELEASE, CHANNEL_BETA)

PREF_CHANNEL = "update.channel"

TIMEOUT = 15
CHUNK = 1 << 20
# How many of the newest releases to look through for beta versions.
RELEASES_TO_LIST = 50
# What a tag the SPA names may look like, before GitHub is asked about it.
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}")

STAGED_FILE = "staged.json"


class UpdateError(Exception):
    """A failure the user should see, with a message that says what to do."""


# ---------------------------------------------------------------------------
# This machine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """Which build this machine runs: the release asset that holds it and the
    program inside."""
    key: str
    release_asset: str
    exe_name: str


TARGETS = {
    "windows-x64": Target("windows-x64", "architecture-validator-desktop-windows-x64.zip",
                          "ArchitectureValidator.exe"),
    "windows-arm64": Target("windows-arm64", "architecture-validator-desktop-windows-arm64.zip",
                            "ArchitectureValidator.exe"),
    "macos": Target("macos", "architecture-validator-desktop-macos.zip",
                    "ArchitectureValidator.app"),
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
    # Anything else (a channel from an older build) means releases.
    return {"channel": channel if channel in CHANNELS else CHANNEL_RELEASE}


def save_settings(channel: str | None = None) -> dict:
    from .routers.prefs import write_pref

    if channel is not None:
        if channel not in CHANNELS:
            raise UpdateError(f"Unknown update channel: {channel}")
        write_pref(PREF_CHANNEL, channel)
    return get_settings()


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _get_json(url: str, params: dict | None = None):
    try:
        res = requests.get(url, headers=dict(_HEADERS), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise UpdateError(f"GitHub unreachable ({exc.__class__.__name__}).") from exc
    if res.status_code == 403 and res.headers.get("X-RateLimit-Remaining") == "0":
        raise UpdateError("GitHub's rate limit was reached. Try again later.")
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


def _commit_sha(value) -> str | None:
    if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower()):
        return value.lower()
    return None


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def version_of(tag: str) -> str:
    """``V3.5.0-beta.1`` / ``desktop-v3.5.0`` → ``3.5.0-beta.1`` / ``3.5.0``."""
    v = (tag or "").strip()
    if v.lower().startswith("desktop-"):
        v = v[len("desktop-"):]
    return v.lstrip("vV")


def is_prerelease(version: str) -> bool:
    """A version with a suffix (``3.5.0-beta.1``) comes before ``3.5.0``."""
    return "-" in version_of(version)


def _version_key(version: str):
    """Orders versions the semver way: ``3.5.0-beta.2`` < ``3.5.0-beta.10`` <
    ``3.5.0`` < ``3.5.1-beta.1``."""
    core, _, pre = version_of(version).split("+")[0].partition("-")
    nums = [int(p) if p.isdigit() else 0 for p in core.split(".")]
    while len(nums) > 1 and nums[-1] == 0:      # 3.5 is 3.5.0
        nums.pop()
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower())
                for p in pre.split(".")) if pre else ()
    return (tuple(nums), 0 if pre else 1, ids)


def _compare_versions(a: str, b: str) -> int:
    ka, kb = _version_key(a), _version_key(b)
    return (ka > kb) - (ka < kb)


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def _release(data: dict) -> dict:
    tag = str(data.get("tag_name") or "")
    return {
        "tag_name": tag,
        "version": version_of(tag),
        "body": str(data.get("body") or ""),
        "html_url": str(data.get("html_url") or ""),
        "published_at": data.get("published_at"),
        "prerelease": bool(data.get("prerelease")),
        "assets": list(data.get("assets") or []),
        # CI publishes with target_commitish = the commit it built.
        "commit": _commit_sha(data.get("target_commitish")),
    }


def latest_release() -> dict:
    """The newest published release (GitHub never names a pre-release here)."""
    return _release(_get_json(f"{API}/releases/latest"))


def list_betas() -> list[dict]:
    """The published beta versions, newest version first."""
    data = _get_json(f"{API}/releases", {"per_page": RELEASES_TO_LIST})
    betas = [_release(r) for r in data or []
             if r.get("prerelease") and not r.get("draft") and r.get("tag_name")]
    return sorted(betas, key=lambda r: _version_key(r["version"]), reverse=True)


def release_by_tag(tag: str) -> dict:
    """The release published from ``tag``. The tag comes from the SPA, so it is
    checked and then looked up: only a release GitHub returns is ever fetched."""
    if not _TAG_RE.fullmatch(tag or ""):
        raise UpdateError("That isn't a version tag.")
    try:
        return _release(_get_json(f"{API}/releases/tags/{quote(tag, safe='')}"))
    except UpdateError as exc:
        if str(exc) == "Not found on GitHub.":
            raise UpdateError(f"{tag} isn't a published version.") from None
        raise


def offered_versions(channel: str) -> list[dict]:
    """What the channel offers, the default choice first.

    ``release``: the newest release. ``beta``: the beta versions, newest first,
    headed by the newest release when that is newer than every beta (the beta
    series it belongs to is over)."""
    if channel == CHANNEL_RELEASE:
        rel = latest_release()
        if not rel["tag_name"]:
            raise UpdateError("The latest release has no version tag.")
        return [rel]
    betas = list_betas()
    try:
        stable = latest_release()
    except UpdateError as exc:
        if str(exc) != "Not found on GitHub.":
            raise
        stable = None                            # only beta versions so far
    if stable and stable["tag_name"] and (
            not betas or _compare_versions(stable["version"], betas[0]["version"]) > 0):
        return [stable] + betas
    if not betas:
        raise UpdateError("No beta versions have been published yet.")
    return betas


def _relation(rel: dict, info: dict) -> str:
    """``newer`` / ``installed`` / ``older`` than the build that's running.

    The same version from another commit counts as newer: this build isn't the
    published one (a CI or local build), so the published one is offered."""
    order = _compare_versions(rel["version"], str(info["version"]))
    if order:
        return "newer" if order > 0 else "older"
    mine = (info.get("commit") or "").lower()
    return "newer" if mine and rel["commit"] and rel["commit"] != mine else "installed"


def _entry(rel: dict, info: dict, target: Target | None) -> dict:
    asset = next((a for a in rel["assets"] if target and a.get("name") == target.release_asset), None)
    return {
        "version": rel["version"],
        "tag": rel["tag_name"],
        "label": f"v{rel['version']}",
        "prerelease": rel["prerelease"],
        "body": rel["body"],
        "url": rel["html_url"],
        "published_at": rel["published_at"],
        "relation": _relation(rel, info),
        "downloadable": asset is not None,
        "size": asset.get("size") if asset else None,
    }


def check() -> dict:
    """What's available on the chosen channel, compared with this build.

    ``latest`` is the version offered by default and ``available`` says whether
    it's newer than this build. ``versions`` is everything the channel lists:
    one release, or the beta versions to pick from."""
    settings = get_settings()
    info = build_info.current()
    target = current_target()
    versions = [_entry(rel, info, target) for rel in offered_versions(settings["channel"])]
    current = {k: info.get(k) for k in ("version", "commit", "branch", "run_id", "frozen")}
    current["prerelease"] = is_prerelease(str(info["version"]))
    return {
        "channel": settings["channel"],
        "current": current,
        "install": install_support(),
        "available": versions[0]["relation"] == "newer",
        "latest": versions[0],
        "versions": versions,
    }


# ---------------------------------------------------------------------------
# Download, check and stage
# ---------------------------------------------------------------------------

def updates_dir() -> Path:
    return app_data_dir() / "updates"


def _download(url: str, dest: Path, size: int | None,
              progress: Callable[..., None], cancel: threading.Event, label: str) -> str:
    """Streams ``url`` to ``dest`` and returns its SHA-256 hex digest."""
    sha = hashlib.sha256()
    try:
        with requests.get(url, headers=_HEADERS | {"Accept": "application/octet-stream"},
                          stream=True, timeout=TIMEOUT) as res:
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


def download_and_stage(progress: Callable[..., None], cancel: threading.Event,
                       tag: str | None = None) -> dict:
    """The ``update_download`` job: fetch a version's build for this machine,
    check its SHA-256, unpack it and record it as ready to install.

    ``tag`` names the version the user picked; without it, it's the one the
    chosen channel offers by default."""
    support = install_support()
    if not support["supported"]:
        raise UpdateError(support["reason"])
    target = current_target()
    assert target is not None
    settings = get_settings()

    progress("Asking GitHub for the update…", None)
    rel = release_by_tag(tag) if tag else offered_versions(settings["channel"])[0]
    label = f"v{rel['version']}"
    asset = next((a for a in rel["assets"] if a.get("name") == target.release_asset), None)
    if asset is None:
        raise UpdateError(f"{label} has no {target.key} build.")
    url = asset.get("browser_download_url")
    size = asset.get("size")
    expected = _digest(asset.get("digest"))
    meta = {"channel": CHANNEL_BETA if rel["prerelease"] else CHANNEL_RELEASE,
            "version": rel["version"], "tag": rel["tag_name"], "label": label,
            "commit": rel["commit"]}
    if not url:
        raise UpdateError("GitHub didn't give a download address for the build.")
    if not expected:
        raise UpdateError("GitHub has no SHA-256 checksum for this build, so it can't be "
                          "verified. Download it from GitHub instead.")

    clear_staging()
    work = updates_dir() / "download"
    work.mkdir(parents=True, exist_ok=True)
    archive = work / "download.zip"
    actual = _download(url, archive, size, progress, cancel, label)
    if actual != expected:
        clear_staging()
        raise UpdateError("The download doesn't match GitHub's SHA-256 checksum; it was "
                          "discarded. Try again.")

    progress("Unpacking…", 92)
    unpacked = work / "unpacked"
    _safe_extract(archive, unpacked)
    archive.unlink(missing_ok=True)

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
        "commit": meta["commit"] or info.get("commit"),
        "build_info": info,
    })
    (updates_dir() / STAGED_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    progress("Ready to install", 100)
    return {k: meta[k] for k in ("channel", "version", "tag", "label", "commit")}


def last_result(consume: bool = True) -> Optional[dict]:
    """How the last install went, written by desktop/updater.py. Read once."""
    from desktop import updater

    path = updater.result_path()
    data = build_info.read_file(path)
    if data is not None and consume:
        path.unlink(missing_ok=True)
    return data
