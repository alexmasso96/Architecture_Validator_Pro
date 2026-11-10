"""
In-app updates: finding the right build (backend/updates.py), downloading and
checking it, and installing it over a fake install folder (desktop/updater.py).

GitHub is replaced by a fake ``requests.get``; nothing here touches the network
or a real install.
"""
import hashlib
import io
import json
import os
import sys
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient

from backend import updates
from backend.app import create_app
from desktop import build_info, updater

TOKEN = "arch-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WIN = updates.TARGETS["windows-x64"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """App data, prefs and the encrypted store all under tmp_path."""
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "appdata" / "prefs.json"))
    monkeypatch.setenv("ARCHVALIDATOR_CONFIG_DIR", str(tmp_path / "config"))
    yield tmp_path


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self._content = content
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload

    def iter_content(self, size):
        for i in range(0, len(self._content), size):
            yield self._content[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_github(monkeypatch, routes: dict, calls: list | None = None):
    """``routes`` maps a URL (prefix match, longest first) to a FakeResponse."""
    def get(url, headers=None, params=None, timeout=None, stream=False):
        if calls is not None:
            calls.append({"url": url, "headers": headers or {}, "params": params})
        for prefix in sorted(routes, key=len, reverse=True):
            if url.startswith(prefix):
                return routes[prefix]
        return FakeResponse(404)
    monkeypatch.setattr(updates.requests, "get", get)


def build_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def windows_build(version="3.5.0", commit="b" * 40, updater_protocol=1) -> bytes:
    info = {"version": version, "commit": commit, "branch": "main"}
    if updater_protocol:
        info["updater"] = updater_protocol
    return build_zip({
        "ArchitectureValidator/ArchitectureValidator.exe": b"new-exe",
        "ArchitectureValidator/ArchitectureValidatorSync.exe": b"new-sync",
        "ArchitectureValidator/_internal/build_info.json": json.dumps(info),
        "ArchitectureValidator/_internal/lib.dll": b"new-lib",
    })


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_payload(asset_zip: bytes, digest: str | None = "auto", tag="V3.5.0",
                    prerelease=False, body="## Notes"):
    asset = {"name": WIN.release_asset, "size": len(asset_zip),
             "browser_download_url": f"https://github.com/dl/{tag}/win.zip"}
    if digest:
        asset["digest"] = f"sha256:{sha(asset_zip) if digest == 'auto' else digest}"
    return {"tag_name": tag, "body": body, "html_url": f"https://github.com/rel/{tag}",
            "published_at": "2026-11-01T00:00:00Z", "prerelease": prerelease,
            "assets": [asset]}


@pytest.fixture()
def as_windows_build(monkeypatch):
    """Pretend to be a frozen Windows x64 build of 3.4.0 at commit aaaa…."""
    monkeypatch.setattr(updates, "current_target", lambda: WIN)
    monkeypatch.setattr(updates, "install_support", lambda: {"supported": True, "reason": ""})
    monkeypatch.setattr(build_info, "current", lambda: {
        "version": "3.4.0", "commit": "a" * 40, "branch": "main", "run_id": None,
        "frozen": True})


def running(monkeypatch, version, commit="a" * 40):
    """Pretend the running build is ``version``."""
    monkeypatch.setattr(build_info, "current", lambda: {
        "version": version, "commit": commit, "branch": "main", "run_id": None,
        "frozen": True})


# ---------------------------------------------------------------------------
# This machine
# ---------------------------------------------------------------------------

def test_target_key_per_platform():
    assert updates.target_key("win32", "AMD64") == "windows-x64"
    assert updates.target_key("win32", "ARM64") == "windows-arm64"
    assert updates.target_key("darwin", "arm64") == "macos"
    assert updates.target_key("linux", "x86_64") is None


def test_install_location_onedir_and_app_bundle(tmp_path):
    exe = tmp_path / "ArchitectureValidator" / "ArchitectureValidator.exe"
    assert updates.install_location(str(exe), "win32") == exe.parent.resolve()
    mac = tmp_path / "ArchitectureValidator.app" / "Contents" / "MacOS" / "ArchitectureValidator"
    assert updates.install_location(str(mac), "darwin") == (tmp_path / "ArchitectureValidator.app").resolve()
    assert updates.install_location(str(tmp_path / "bin" / "x"), "darwin") is None


def test_running_from_source_cannot_install():
    support = updates.install_support()
    assert support["supported"] is False
    assert "source" in support["reason"]


def test_digest_parsing():
    good = "a" * 64
    assert updates._digest(f"sha256:{good}") == good
    assert updates._digest(f"SHA256:{good.upper()}") == good
    assert updates._digest("sha1:abc") is None
    assert updates._digest("sha256:xyz") is None
    assert updates._digest(None) is None


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def test_release_check_finds_newer_version(monkeypatch, as_windows_build):
    z = windows_build()
    fake_github(monkeypatch, {f"{updates.API}/releases/latest": FakeResponse(payload=release_payload(z))})
    out = updates.check()
    assert out["channel"] == "release"
    assert out["available"] is True
    assert out["latest"]["version"] == "3.5.0"
    assert out["latest"]["label"] == "v3.5.0"
    assert out["latest"]["downloadable"] is True


def test_release_check_same_version_is_latest(monkeypatch, as_windows_build):
    payload = release_payload(windows_build(), tag="v3.4.0")
    fake_github(monkeypatch, {f"{updates.API}/releases/latest": FakeResponse(payload=payload)})
    assert updates.check()["available"] is False


def beta_routes(betas: dict, stable: str | None = "V3.4.0", extra: list | None = None):
    """GitHub with the beta versions ``{tag: zip}`` and a latest release.
    ``extra``: more entries for the release list (drafts, releases)."""
    listing = [release_payload(z, tag=t, prerelease=True, body=f"## {t} notes")
               for t, z in betas.items()]
    routes = {f"{updates.API}/releases": FakeResponse(payload=listing + (extra or [])),
              # What GitHub answers for a tag with no release, and with no release at all.
              f"{updates.API}/releases/tags/": FakeResponse(404),
              f"{updates.API}/releases/latest": FakeResponse(404)}
    for rel in listing:
        routes[f"{updates.API}/releases/tags/{rel['tag_name']}"] = FakeResponse(payload=rel)
        routes[rel["assets"][0]["browser_download_url"]] = FakeResponse(
            content=betas[rel["tag_name"]])
    if stable:
        routes[f"{updates.API}/releases/latest"] = FakeResponse(
            payload=release_payload(windows_build(), tag=stable))
    return routes


@pytest.mark.parametrize("older, newer", [
    ("3.4.0", "3.5.0-beta.1"),
    ("3.5.0-beta.1", "3.5.0-beta.2"),
    ("3.5.0-beta.2", "3.5.0-beta.10"),       # numbers, not text
    ("3.5.0-beta", "3.5.0-beta.1"),
    ("3.5.0-beta.10", "3.5.0-rc.1"),
    ("3.5.0-rc.1", "3.5.0"),                 # a beta comes before its release
    ("3.5.0", "3.5.1-beta.1"),
    ("V3.5", "desktop-v3.5.1"),
])
def test_versions_are_ordered_the_semver_way(older, newer):
    assert updates._compare_versions(older, newer) < 0
    assert updates._compare_versions(newer, older) > 0


def test_equal_versions_and_tag_forms():
    assert updates._compare_versions("V3.5.0", "3.5") == 0
    assert updates.version_of("desktop-v3.5.0-beta.1") == "3.5.0-beta.1"
    assert updates.is_prerelease("V3.5.0-beta.1") and not updates.is_prerelease("V3.5.0")


def test_beta_check_lists_the_beta_versions_newest_first(monkeypatch, as_windows_build):
    updates.save_settings(channel="beta")
    z = windows_build()
    draft = release_payload(z, tag="V3.6.0-beta.1", prerelease=True) | {"draft": True}
    release = release_payload(z, tag="V3.4.0")          # in the list, but not a beta
    fake_github(monkeypatch, beta_routes(
        {"V3.5.0-beta.1": z, "V3.5.0-beta.10": z, "V3.5.0-beta.2": z}, extra=[draft, release]))
    out = updates.check()
    assert out["channel"] == "beta"
    assert [v["label"] for v in out["versions"]] == [
        "v3.5.0-beta.10", "v3.5.0-beta.2", "v3.5.0-beta.1"]
    assert all(v["prerelease"] and v["relation"] == "newer" for v in out["versions"])
    assert out["available"] is True
    assert out["latest"]["tag"] == "V3.5.0-beta.10"
    # Each version comes with its own change log.
    assert out["versions"][1]["body"] == "## V3.5.0-beta.2 notes"
    assert out["current"]["prerelease"] is False


def test_beta_check_knows_which_beta_is_installed(monkeypatch, as_windows_build):
    updates.save_settings(channel="beta")
    running(monkeypatch, "3.5.0-beta.2", commit=None)
    z = windows_build()
    fake_github(monkeypatch, beta_routes(
        {"V3.5.0-beta.1": z, "V3.5.0-beta.2": z, "V3.5.0-beta.3": z}))
    out = updates.check()
    assert [v["relation"] for v in out["versions"]] == ["newer", "installed", "older"]
    assert out["available"] is True and out["current"]["prerelease"] is True

    running(monkeypatch, "3.5.0-beta.3", commit=None)
    assert updates.check()["available"] is False


def test_a_release_newer_than_every_beta_heads_the_beta_list(monkeypatch, as_windows_build):
    updates.save_settings(channel="beta")
    running(monkeypatch, "3.5.0-beta.2", commit=None)
    z = windows_build()
    fake_github(monkeypatch, beta_routes({"V3.5.0-beta.1": z, "V3.5.0-beta.2": z},
                                         stable="V3.5.0"))
    out = updates.check()
    assert [v["label"] for v in out["versions"]] == ["v3.5.0", "v3.5.0-beta.2", "v3.5.0-beta.1"]
    assert out["latest"]["prerelease"] is False and out["available"] is True

    # An older release isn't listed: only the beta versions are.
    fake_github(monkeypatch, beta_routes({"V3.5.0-beta.1": z, "V3.5.0-beta.2": z}))
    assert [v["label"] for v in updates.check()["versions"]] == ["v3.5.0-beta.2", "v3.5.0-beta.1"]


def test_beta_check_without_beta_versions(monkeypatch, as_windows_build):
    updates.save_settings(channel="beta")
    fake_github(monkeypatch, beta_routes({}, stable="V3.5.0"))
    out = updates.check()
    assert [v["label"] for v in out["versions"]] == ["v3.5.0"]

    fake_github(monkeypatch, beta_routes({}, stable=None))
    with pytest.raises(updates.UpdateError, match="No beta versions"):
        updates.check()


def test_release_channel_never_offers_a_beta(monkeypatch, as_windows_build):
    running(monkeypatch, "3.5.0-beta.2", commit=None)
    calls = []
    fake_github(monkeypatch, beta_routes({"V3.5.0-beta.3": windows_build()}), calls)
    out = updates.check()
    # Only the latest release is asked for, and it's older than this beta.
    assert [c["url"] for c in calls] == [f"{updates.API}/releases/latest"]
    assert out["available"] is False and out["latest"]["relation"] == "older"
    assert out["current"]["prerelease"] is True


def test_no_token_is_sent(monkeypatch, as_windows_build):
    calls = []
    fake_github(monkeypatch, {f"{updates.API}/releases/latest":
                              FakeResponse(payload=release_payload(windows_build()))}, calls)
    updates.check()
    assert "Authorization" not in calls[0]["headers"]


def test_rate_limit_message(monkeypatch, as_windows_build):
    fake_github(monkeypatch, {f"{updates.API}/releases/latest":
                              FakeResponse(403, headers={"X-RateLimit-Remaining": "0"})})
    with pytest.raises(updates.UpdateError, match="rate limit"):
        updates.check()


# ---------------------------------------------------------------------------
# Downloading and staging
# ---------------------------------------------------------------------------

def run_download(tag=None):
    messages = []
    result = updates.download_and_stage(lambda m, p=None: messages.append((m, p)),
                                        threading.Event(), tag=tag)
    return result, messages


def test_release_download_is_verified_and_staged(monkeypatch, as_windows_build):
    z = windows_build()
    fake_github(monkeypatch, {
        f"{updates.API}/releases/latest": FakeResponse(payload=release_payload(z)),
        "https://github.com/dl/V3.5.0/win.zip": FakeResponse(content=z),
    })
    result, messages = run_download()
    assert result["version"] == "3.5.0" and result["channel"] == "release"
    stage = updates.staged()
    assert stage is not None
    assert stage["sha256"] == sha(z)
    assert Path(stage["executable"]).read_bytes() == b"new-exe"
    assert Path(stage["bundle"]).name == "ArchitectureValidator"
    assert messages[-1] == ("Ready to install", 100)


def test_checksum_mismatch_is_rejected(monkeypatch, as_windows_build):
    z = windows_build()
    fake_github(monkeypatch, {
        f"{updates.API}/releases/latest": FakeResponse(payload=release_payload(z, digest="0" * 64)),
        "https://github.com/dl/V3.5.0/win.zip": FakeResponse(content=z),
    })
    with pytest.raises(updates.UpdateError, match="SHA-256"):
        run_download()
    assert updates.staged() is None
    assert not updates.updates_dir().exists()


def test_missing_checksum_is_rejected(monkeypatch, as_windows_build):
    z = windows_build()
    fake_github(monkeypatch, {
        f"{updates.API}/releases/latest": FakeResponse(payload=release_payload(z, digest=None)),
    })
    with pytest.raises(updates.UpdateError, match="checksum"):
        run_download()


def test_unsafe_zip_paths_are_rejected(monkeypatch, as_windows_build):
    z = build_zip({"../evil.txt": b"x", "ArchitectureValidator/ArchitectureValidator.exe": b"e"})
    fake_github(monkeypatch, {
        f"{updates.API}/releases/latest": FakeResponse(payload=release_payload(z)),
        "https://github.com/dl/V3.5.0/win.zip": FakeResponse(content=z),
    })
    with pytest.raises(updates.UpdateError, match="unsafe"):
        run_download()
    assert not (updates.updates_dir().parent / "evil.txt").exists()


def test_build_without_updater_is_refused(monkeypatch, as_windows_build):
    z = windows_build(updater_protocol=0)
    fake_github(monkeypatch, {
        f"{updates.API}/releases/latest": FakeResponse(payload=release_payload(z)),
        "https://github.com/dl/V3.5.0/win.zip": FakeResponse(content=z),
    })
    with pytest.raises(updates.UpdateError, match="before in-app updates"):
        run_download()


def test_beta_download_takes_the_newest_or_the_picked_version(monkeypatch, as_windows_build):
    updates.save_settings(channel="beta")
    old, new = windows_build("3.5.0-beta.1", "c" * 40), windows_build("3.5.0-beta.2", "d" * 40)
    fake_github(monkeypatch, beta_routes({"V3.5.0-beta.1": old, "V3.5.0-beta.2": new}))

    result, _ = run_download()
    assert result == {"channel": "beta", "version": "3.5.0-beta.2", "tag": "V3.5.0-beta.2",
                      "label": "v3.5.0-beta.2", "commit": "d" * 40}
    assert updates.staged()["sha256"] == sha(new)

    # The version the user picked replaces what was downloaded.
    result, _ = run_download(tag="V3.5.0-beta.1")
    assert result["label"] == "v3.5.0-beta.1"
    assert updates.staged()["sha256"] == sha(old)


def test_only_a_published_version_can_be_picked(monkeypatch, as_windows_build):
    calls = []
    fake_github(monkeypatch, beta_routes({"V3.5.0-beta.1": windows_build()}), calls)
    with pytest.raises(updates.UpdateError, match="isn't a published version"):
        run_download(tag="V9.9.9-beta.1")
    for bad in ("../../latest", "V3.5.0/../x", "https://evil.example/x.zip", " "):
        n = len(calls)
        with pytest.raises(updates.UpdateError, match="isn't a version tag"):
            run_download(tag=bad)
        assert len(calls) == n          # GitHub isn't even asked
    assert updates.staged() is None


def test_download_job_passes_the_picked_tag(client, monkeypatch):
    seen = {}

    def fake(progress, cancel, tag=None):
        seen["tag"] = tag
        return {"label": "v3.5.0-beta.1"}
    monkeypatch.setattr(updates, "download_and_stage", fake)
    job = client.post("/api/jobs/update_download", headers=AUTH,
                      json={"tag": "V3.5.0-beta.1"}).json()["job_id"]
    for _ in range(100):
        if client.get(f"/api/jobs/{job}", headers=AUTH).json()["status"] == "done":
            break
        threading.Event().wait(0.02)
    assert seen == {"tag": "V3.5.0-beta.1"}


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def make_install(root: Path, tag: str) -> Path:
    install = root / "Apps" / "ArchitectureValidator"
    (install / "_internal").mkdir(parents=True)
    (install / "ArchitectureValidator.exe").write_text(tag)
    (install / "_internal" / "lib.dll").write_text(tag)
    return install


def make_bundle(root: Path, tag: str) -> Path:
    bundle = root / "staged" / "ArchitectureValidator"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "ArchitectureValidator.exe").write_text(tag)
    (bundle / "_internal" / "lib.dll").write_text(tag)
    (bundle / "_internal" / "fresh.txt").write_text("only in new")
    return bundle


def test_install_build_swaps_and_keeps_old(tmp_path):
    install = make_install(tmp_path, "old")
    bundle = make_bundle(tmp_path, "new")
    updater.install_build(bundle, install)
    assert (install / "ArchitectureValidator.exe").read_text() == "new"
    assert (install / "_internal" / "fresh.txt").exists()
    old = updater.sibling(install, updater.OLD_SUFFIX)
    assert (old / "ArchitectureValidator.exe").read_text() == "old"
    assert not updater.sibling(install, updater.NEW_SUFFIX).exists()


def test_swap_puts_old_back_when_second_rename_fails(tmp_path, monkeypatch):
    install = make_install(tmp_path, "old")
    updater.copy_new(make_bundle(tmp_path, "new"), install)
    real_rename = os.rename

    def rename(src, dst):
        if Path(src).name.endswith(updater.NEW_SUFFIX):
            raise PermissionError(13, "locked")
        real_rename(src, dst)
    monkeypatch.setattr(updater.os, "rename", rename)
    with pytest.raises(updater.SwapError, match="into place"):
        updater.swap(install, retry_seconds=0)
    assert (install / "ArchitectureValidator.exe").read_text() == "old"


def test_swap_reports_an_install_in_use(tmp_path, monkeypatch):
    install = make_install(tmp_path, "old")
    updater.copy_new(make_bundle(tmp_path, "new"), install)
    monkeypatch.setattr(updater.os, "rename",
                        lambda s, d: (_ for _ in ()).throw(PermissionError(13, "in use")))
    with pytest.raises(updater.SwapError, match="still in use"):
        updater.swap(install, retry_seconds=0)
    assert (install / "ArchitectureValidator.exe").read_text() == "old"


def test_rollback_restores_previous(tmp_path):
    install = make_install(tmp_path, "old")
    updater.install_build(make_bundle(tmp_path, "new"), install)
    updater.rollback(install)
    assert (install / "ArchitectureValidator.exe").read_text() == "old"
    assert (updater.sibling(install, updater.FAILED_SUFFIX) / "ArchitectureValidator.exe").read_text() == "new"


class FakeProc:
    def __init__(self, marker: Path | None, exit_code=None):
        self.marker = marker
        self.exit_code = exit_code
        if marker is not None:
            marker.write_text("1")

    def poll(self):
        return self.exit_code


def write_plan(tmp_path: Path, install: Path, bundle: Path) -> Path:
    updater.updates_dir().mkdir(parents=True, exist_ok=True)
    plan = {
        "app_pid": 0, "install": str(install), "bundle": str(bundle),
        "relaunch": "ArchitectureValidator.exe", "sync_exe": "ArchitectureValidatorSync.exe",
        "marker": str(updater.updates_dir() / "ready.marker"),
        "from": {"version": "3.4.0", "commit": "a"}, "to": {"label": "v3.5.0", "version": "3.5.0"},
    }
    path = updater.updates_dir() / updater.PLAN_FILE
    path.write_text(json.dumps(plan))
    return path


def test_apply_installs_and_reports(tmp_path, monkeypatch):
    install = make_install(tmp_path, "old")
    plan = write_plan(tmp_path, install, make_bundle(tmp_path, "new"))
    started = []

    def start(inst, p):
        started.append((inst / p["relaunch"]).read_text())
        return FakeProc(Path(p["marker"]))
    monkeypatch.setattr(updater, "_start_app", start)
    monkeypatch.setattr(updater, "_stop_sync_worker", lambda: False)

    assert updater.apply(plan) == 0
    assert started == ["new"]
    result = json.loads(updater.result_path().read_text())
    assert result["ok"] is True and result["to"]["label"] == "v3.5.0"


def test_apply_rolls_back_when_new_build_dies(tmp_path, monkeypatch):
    install = make_install(tmp_path, "old")
    plan = write_plan(tmp_path, install, make_bundle(tmp_path, "new"))
    started = []

    def start(inst, p):
        started.append((inst / p["relaunch"]).read_text())
        # The new build exits without reporting ready; the restored one is fine.
        return FakeProc(None, exit_code=1) if len(started) == 1 else FakeProc(Path(p["marker"]))
    monkeypatch.setattr(updater, "_start_app", start)
    monkeypatch.setattr(updater, "_stop_sync_worker", lambda: False)

    assert updater.apply(plan) == 1
    assert started == ["new", "old"]
    assert (install / "ArchitectureValidator.exe").read_text() == "old"
    result = json.loads(updater.result_path().read_text())
    assert result["ok"] is False and "restored" in result["error"]


def test_apply_restarts_the_sync_worker(tmp_path, monkeypatch):
    install = make_install(tmp_path, "old")
    plan = write_plan(tmp_path, install, make_bundle(tmp_path, "new"))
    launched = []
    monkeypatch.setattr(updater, "_start_app", lambda i, p: FakeProc(Path(p["marker"])))
    monkeypatch.setattr(updater, "_stop_sync_worker", lambda: True)
    monkeypatch.setattr(updater, "_popen", lambda argv, **kw: launched.append(argv))
    assert updater.apply(plan) == 0
    assert launched == [[str(install / "ArchitectureValidatorSync.exe"), "--sync-worker"]]


def test_cleanup_removes_old_build_and_spent_staging(tmp_path):
    install = make_install(tmp_path, "old")
    updater.install_build(make_bundle(tmp_path, "new"), install)
    write_plan(tmp_path, install, tmp_path / "staged")
    updater.cleanup_after_update(install=install)
    assert not updater.sibling(install, updater.OLD_SUFFIX).exists()
    assert not updater.updates_dir().exists()
    assert install.exists()


def test_cleanup_keeps_a_download_not_yet_installed(tmp_path):
    install = make_install(tmp_path, "cur")
    updater.updates_dir().mkdir(parents=True)
    (updater.updates_dir() / "staged.json").write_text("{}")
    updater.cleanup_after_update(install=install)
    assert (updater.updates_dir() / "staged.json").exists()


def test_cleanup_waits_for_a_running_updater(tmp_path, monkeypatch):
    install = make_install(tmp_path, "old")
    updater.install_build(make_bundle(tmp_path, "new"), install)
    write_plan(tmp_path, install, tmp_path / "staged")
    (updater.updates_dir() / updater.UPDATER_PID_FILE).write_text("4242")
    monkeypatch.setattr(updater, "wait_for_exit", lambda pid, t, **kw: False)
    updater.cleanup_after_update(install=install, wait=0)
    # Still running, so it may yet roll back: nothing is removed.
    assert updater.sibling(install, updater.OLD_SUFFIX).exists()
    assert updater.updates_dir().exists()


def test_mark_ready_writes_marker(tmp_path):
    marker = tmp_path / "ready"
    updater.mark_ready(str(marker))
    assert marker.exists()
    updater.mark_ready(None)  # not started by the updater: nothing to do


# ---------------------------------------------------------------------------
# The Sync Worker exits when asked
# ---------------------------------------------------------------------------

def test_sync_worker_quits_after_a_pass(tmp_path):
    from Application_Logic import Logic_Sync_Worker as sw

    store = sw.SyncStore(str(tmp_path / "sync"))
    os.makedirs(store.root, exist_ok=True)
    passes = []

    class Worker:
        def __init__(self):
            self.store = store

        def tick(self):
            passes.append(1)

        def wait(self, seconds):
            pass

    sw.request_quit(store)
    stop = threading.Event()
    sw.run_until(Worker(), stop)
    assert stop.is_set()
    assert passes == [1]  # the pending pass ran before it quit
    assert not os.path.exists(os.path.join(store.root, sw.QUIT_REQUEST))


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    with TestClient(create_app(token=TOKEN)) as c:
        yield c


def test_settings_roundtrip(client):
    r = client.get("/api/update/settings", headers=AUTH)
    assert r.json() == {"channel": "release"}
    r = client.put("/api/update/settings", headers=AUTH, json={"channel": "beta"})
    assert r.json() == {"channel": "beta"}
    assert client.get("/api/update/settings", headers=AUTH).json() == {"channel": "beta"}


def test_bad_channel_is_rejected(client):
    for channel in ("nightly", "branch"):
        r = client.put("/api/update/settings", headers=AUTH, json={"channel": channel})
        assert r.status_code == 422


def test_a_channel_saved_by_an_older_build_means_releases(client):
    from backend.routers.prefs import write_pref
    write_pref(updates.PREF_CHANNEL, "branch")
    assert client.get("/api/update/settings", headers=AUTH).json() == {"channel": "release"}
    assert client.get("/api/update/branches", headers=AUTH).status_code == 404


def test_check_reports_install_support(client, monkeypatch):
    fake_github(monkeypatch, {f"{updates.API}/releases/latest":
                              FakeResponse(payload=release_payload(windows_build(), tag="V99.0.0"))})
    r = client.get("/api/update/check", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["install"]["supported"] is False  # tests run from source
    assert body["staged"] is None


def test_check_failure_is_502(client, monkeypatch):
    fake_github(monkeypatch, {f"{updates.API}/releases/latest": FakeResponse(500)})
    r = client.get("/api/update/check", headers=AUTH)
    assert r.status_code == 502


def test_latest_endpoint_kept(client, monkeypatch):
    fake_github(monkeypatch, {f"{updates.API}/releases/latest":
                              FakeResponse(payload=release_payload(windows_build()))})
    r = client.get("/api/update/latest", headers=AUTH)
    assert r.json() == {"tag_name": "V3.5.0", "body": "## Notes"}


def test_busy_lists_running_jobs(client):
    jobs = client.app.state.jobs
    gate = threading.Event()
    jobs.register("slow", lambda p, progress, cancel: gate.wait(5))
    jobs.start("slow", {})
    try:
        assert client.get("/api/update/busy", headers=AUTH).json() == {"busy": ["slow"]}
    finally:
        gate.set()


def test_result_is_read_once(client):
    updater.result_path().parent.mkdir(parents=True, exist_ok=True)
    updater.result_path().write_text(json.dumps({"ok": True, "to": {"label": "v3.5.0"}}))
    assert client.get("/api/update/result", headers=AUTH).json()["result"]["ok"] is True
    assert client.get("/api/update/result", headers=AUTH).json() == {"result": None}


def test_build_info_takes_a_release_builds_version_from_its_tag():
    sys.path.insert(0, os.path.abspath("scripts"))
    import write_build_info as w

    assert w.release_tag({"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "V3.5.0-beta.1"}) \
        == "V3.5.0-beta.1"
    # A manual run names its tag; a branch build has none.
    assert w.release_tag({"BUILD_TAG": "V3.5.0", "GITHUB_REF_NAME": "main"}) == "V3.5.0"
    assert w.release_tag({"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main"}) is None
    assert w.release_tag({"BUILD_TAG": "nightly"}) is None
    assert w.tag_version("V3.5.0-beta.1") == "3.5.0-beta.1"
    assert w.tag_version("desktop-v3.5.0") == "3.5.0"
    assert updates._compare_versions(w.tag_version("V3.5.0-beta.1"), "3.5.0") < 0


def test_release_offered_to_branch_build_of_same_version(monkeypatch, as_windows_build):
    payload = release_payload(windows_build(), tag="v3.4.0")
    payload["target_commitish"] = "d" * 40      # the release's commit, not this build's
    fake_github(monkeypatch, {f"{updates.API}/releases/latest": FakeResponse(payload=payload)})
    assert updates.check()["available"] is True
    payload["target_commitish"] = "a" * 40      # this very build
    assert updates.check()["available"] is False
