"""API tests for Test Builds (#15): setup, the lock, a real run, and switching."""
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient

from backend.app import create_app
from Tests.test_helpers import make_project_db

TOKEN = "tb-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SOURCE = "void main(void)\n{\n    int x = 1;\n    int y = 2;\n}\n"
BUILD = ("cat main.c > ../out/app.hex && echo built")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX build command")


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "appdata" / "prefs.json"))
    path = str(tmp_path / "proj.arch")
    db = make_project_db(path, layout=[("TC. ID", "Static Text", True)],
                         models=[{"name": "A", "status": "In Work", "rows": []}],
                         releases=[{"name": "R1"}, {"name": "R2"}])
    db.close()
    (tmp_path / "tree").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "mapped").mkdir()
    (tmp_path / "tree" / "main.c").write_text(SOURCE)
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        c.post("/api/project/open", json={"path": path, "mode": "exclusive"}, headers=AUTH)
        state = app.state.appstate
        rels = state.require_releases().releases
        state.db.save_release_source_files(rels[0].id, [("main.c", SOURCE)])
        _activate(c, rels[0].id)
        yield c, tmp_path, app, rels[0].id, rels[1].id


def _activate(c, release_id):
    assert c.post(f"/api/releases/{release_id}/activate", headers=AUTH).status_code == 200


def _ok(r):
    assert r.status_code < 300, r.text
    return r.json()


def _make_build(c, tmp, name="Watchdog"):
    tp = _ok(c.post("/api/injection/projects", json={"name": name}, headers=AUTH))
    _ok(c.post(f"/api/injection/projects/{tp['id']}/injections", headers=AUTH, json={
        "src_file_path": "main.c", "function_name": "main", "line_above_code": "int x = 1;",
        "line_below_code": "int y = 2;", "injected_code": "    while(1);"}))
    build = _ok(c.post("/api/test-builds", headers=AUTH, json={
        "name": name, "test_project_id": tp["id"], "build_command": BUILD,
        "build_cwd": str(tmp / "tree"), "source_dir": str(tmp / "tree"),
        "output_dir": str(tmp / "out"), "clean_command": "rm -f ../out/app.hex"}))
    _ok(c.put("/api/test-builds/artifacts/list", headers=AUTH, json={
        "artifacts": [{"path": "app.hex", "role": "Flash"}]}))
    return build


def _job(c, kind, params):
    job = _ok(c.post(f"/api/jobs/{kind}", json=params, headers=AUTH))
    for _ in range(600):
        snapshot = _ok(c.get(f"/api/jobs/{job['job_id']}", headers=AUTH))
        if snapshot["status"] in ("done", "failed", "cancelled"):
            return snapshot
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_run_and_switch_there_and_back(bench):
    c, tmp, _app, r1, _r2 = bench
    build = _make_build(c, tmp)
    preflight = _ok(c.post("/api/test-builds/preflight", headers=AUTH,
                           json={"build_ids": [build["id"]]}))
    assert any(".o files" in p["problem"] for p in preflight["problems"])

    job = _job(c, "test_build_run", {"build_ids": [build["id"]], "continue_anyway": [r1],
                                     "continue_reason": "not built yet"})
    assert job["status"] == "done", job
    assert job["result"]["counts"] == {"passed": 2}

    assert (tmp / "tree" / "main.c").read_text() == SOURCE
    locks = _ok(c.get("/api/test-builds/clean-lock/status", headers=AUTH))
    assert locks["overrides"][0]["reason"] == "not built yet"
    assert locks["overrides"][0]["username"]

    runs = _ok(c.get("/api/test-builds/runs/list", headers=AUTH))
    assert runs["unfinished"] is None
    item = runs["runs"][0]["items"][1]
    log = c.get(f"/api/test-builds/runs/log/{item['id']}", headers=AUTH)
    assert "built" in log.text

    builds = _ok(c.get("/api/test-builds", headers=AUTH))["builds"]
    main = next(b for b in builds if b["is_main"])
    assert main["capture_status"] == "complete"

    _ok(c.put("/api/test-builds/mapped/folder", headers=AUTH,
              json={"folder": str(tmp / "mapped")}))
    status = _ok(c.get("/api/test-builds/mapped/status", headers=AUTH))
    assert {t["name"] for t in status["targets"]} == {"Main", "Watchdog"}

    assert _job(c, "mapped_switch", {"build_id": main["id"]})["status"] == "done"
    original = (tmp / "mapped" / "app.hex").read_bytes()
    assert b"while" not in original
    done = _job(c, "mapped_switch", {"build_id": build["id"]})
    assert done["result"]["build_name"] == "Watchdog"
    assert b"while(1);" in (tmp / "mapped" / "app.hex").read_bytes()
    assert _job(c, "mapped_switch", {"build_id": main["id"]})["status"] == "done"
    assert (tmp / "mapped" / "app.hex").read_bytes() == original


def test_changing_release_hides_builds_and_warns_about_the_folder(bench):
    c, tmp, _app, r1, r2 = bench
    build = _make_build(c, tmp)
    _job(c, "test_build_run", {"build_ids": [build["id"]], "continue_anyway": [r1]})
    _ok(c.put("/api/test-builds/mapped/folder", headers=AUTH,
              json={"folder": str(tmp / "mapped")}))
    _job(c, "mapped_switch", {"build_id": build["id"]})
    before = (tmp / "mapped" / "app.hex").read_bytes()

    _activate(c, r2)
    status = _ok(c.get("/api/test-builds/mapped/status", headers=AUTH))
    assert status["targets"] == []
    assert "R1 Watchdog" in status["warning"] and "R2" in status["warning"]
    refused = _job(c, "mapped_switch", {"build_id": build["id"]})
    assert refused["status"] == "failed"
    assert "another release" in refused["error"]
    assert (tmp / "mapped" / "app.hex").read_bytes() == before

    _activate(c, r1)
    status = _ok(c.get("/api/test-builds/mapped/status", headers=AUTH))
    assert {t["name"] for t in status["targets"]} == {"Main", "Watchdog"}


def test_run_with_problems_doesnt_start(bench):
    c, tmp, _app, _r1, _r2 = bench
    build = _make_build(c, tmp)
    job = _job(c, "test_build_run", {"build_ids": [build["id"]]})
    assert job["status"] == "failed"
    assert ".o files" in job["error"]


def test_export_is_stopped_by_the_clean_lock_until_continued(bench):
    c, tmp, _app, _r1, _r2 = bench
    build = _make_build(c, tmp)
    out = str(tmp / "export")
    url = f"/api/injection/projects/{build['test_project_id']}/export"
    r = c.post(url, headers=AUTH, json={"out_dir": out})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "clean_lock"
    assert r.json()["detail"]["missing"] == [".o files"]
    assert not os.path.exists(os.path.join(out, "main.c"))
    _ok(c.post(url, headers=AUTH, json={"out_dir": out, "overwrite": True,
                                        "continue_anyway": True}))
    assert "while(1);" in open(os.path.join(out, "main.c")).read()


def test_export_can_target_a_release_that_isnt_active(bench):
    c, tmp, _app, r1, r2 = bench
    build = _make_build(c, tmp)
    _activate(c, r2)
    out = str(tmp / "export")
    _ok(c.post(f"/api/injection/projects/{build['test_project_id']}/export", headers=AUTH,
               json={"out_dir": out, "release_id": r1, "continue_anyway": True}))
    assert "while(1);" in open(os.path.join(out, "main.c")).read()


def test_project_cant_be_closed_during_a_run(bench):
    c, tmp, app, r1, _r2 = bench
    state = app.state.appstate
    state.begin_build_run()
    try:
        assert c.post("/api/project/close", headers=AUTH).status_code == 409
    finally:
        state.end_build_run()


def test_store_cleanup_keeps_what_manifests_use(bench):
    c, tmp, _app, r1, _r2 = bench
    build = _make_build(c, tmp)
    _job(c, "test_build_run", {"build_ids": [build["id"]], "continue_anyway": [r1]})
    summary = _ok(c.get("/api/test-builds/store/summary", headers=AUTH))
    assert summary["blobs"] > 0 and summary["unreferenced"] == 0
    _ok(c.delete(f"/api/test-builds/{build['id']}", headers=AUTH))
    summary = _ok(c.get("/api/test-builds/store/summary", headers=AUTH))
    assert summary["unreferenced"] > 0
    assert _ok(c.post("/api/test-builds/store/cleanup", headers=AUTH))["removed"] > 0


def test_finished_run_publishes_a_shared_project(bench, monkeypatch):
    c, tmp, app, r1, _r2 = bench
    state = app.state.appstate
    published = []
    monkeypatch.setattr(state, "publish_now", lambda: published.append(True))
    build = _make_build(c, tmp)
    _job(c, "test_build_run", {"build_ids": [build["id"]], "continue_anyway": [r1]})
    assert published == [True]


def test_main_name_is_reserved(bench):
    c, tmp, _app, _r1, _r2 = bench
    tp = _ok(c.post("/api/injection/projects", json={"name": "T"}, headers=AUTH))
    r = c.post("/api/test-builds", headers=AUTH,
               json={"name": "Main", "test_project_id": tp["id"]})
    assert r.status_code == 409
