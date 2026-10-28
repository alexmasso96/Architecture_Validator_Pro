"""
A Code Map belongs to a release, not to the architecture model that was
active when it was built (the reported bug: a map built with model 1 active
was invisible from every other model).
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic.Logic_Block_Crypto import BlockCipher, is_marked_text
from Application_Logic.Logic_Database import ProjectDatabase

SAMPLE_ELF = os.path.abspath("Tests/Resources/sample.elf")


def _cm(built_at, *names):
    return {"built_at": built_at, "functions": {n: {"calls": []} for n in names}}


@pytest.fixture()
def db(tmp_path):
    d = ProjectDatabase()
    d.open(str(tmp_path / "p.arch"))
    yield d
    d.close()


def test_map_is_shared_by_every_model_of_a_release(db):
    m1, m2 = db.create_model("M1"), db.create_model("M2")
    r1, r2 = db.create_release("R1"), db.create_release("R2")
    db.save_model_code_map(m1, json.dumps(_cm("t1", "main")), release_id=r1)
    assert db.get_model_code_map(m2, release_id=r1)["functions"] == {"main": {"calls": []}}
    assert db.get_release_code_map(r1) == db.get_model_code_map(m1, release_id=r1)
    assert db.get_release_code_map(r2) is None            # still per release


def test_old_per_model_maps_are_adopted_newest_first(db):
    m1, m2 = db.create_model("M1"), db.create_model("M2")
    rid = db.create_release("R1")
    # Written the way older versions stored maps: per model, in ai_release_maps.
    db.save_model_mindmap(m1, "{}", code_map_json=json.dumps(_cm("2026-10-01", "old")),
                          release_id=rid)
    db.save_model_mindmap(m2, "{}", code_map_json=json.dumps(_cm("2026-10-20", "new")),
                          release_id=rid)
    assert list(db.get_release_code_map(rid)["functions"]) == ["new"]
    row = db.execute("SELECT 1 FROM release_code_maps WHERE release_id=?", (rid,)).fetchone()
    assert row is not None                                # adopted + stored
    # A rebuild replaces it for everyone; the old rows no longer matter.
    db.save_model_code_map(m1, json.dumps(_cm("2026-10-28", "rebuilt")), release_id=rid)
    assert list(db.get_model_code_map(m2, release_id=rid)["functions"]) == ["rebuilt"]


def test_read_only_project_adopts_without_writing(tmp_path):
    path = str(tmp_path / "p.arch")
    d = ProjectDatabase()
    d.open(path)
    mid, rid = d.create_model("M1"), d.create_release("R1")
    d.save_model_mindmap(mid, "{}", code_map_json=json.dumps(_cm("t", "main")),
                         release_id=rid)
    d.commit()
    d.set_read_only(True)
    assert d.get_release_code_map(rid) is not None
    assert d.execute("SELECT COUNT(*) FROM release_code_maps").fetchone()[0] == 0
    d.close()


def test_release_map_is_encrypted_at_rest(tmp_path):
    d = ProjectDatabase()
    d.open(str(tmp_path / "p.arch"))
    d.set_block_cipher(BlockCipher.from_password("Pw-123456!", BlockCipher.new_salt()))
    rid = d.create_release("R1")
    d.save_release_code_map(json.dumps(_cm("t", "secret_fn")), release_id=rid)
    raw = d.execute("SELECT code_map_json FROM release_code_maps").fetchone()[0]
    assert is_marked_text(raw) and "secret_fn" not in raw
    assert "secret_fn" in d.get_release_code_map(rid)["functions"]
    d.close()


def test_deleting_a_release_drops_its_map(db):
    rid = db.create_release("R1")
    db.save_release_code_map(json.dumps(_cm("t", "main")), release_id=rid)
    db.delete_release_record(rid)
    assert db.execute("SELECT COUNT(*) FROM release_code_maps").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# API: build with model 1 active, view from model 2.
# ---------------------------------------------------------------------------
TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "prefs.json"))
    from fastapi.testclient import TestClient
    from backend.app import create_app
    with TestClient(create_app(token=TOKEN)) as c:
        assert c.post("/api/project/new", json={"path": str(tmp_path / "p.arch")},
                      headers=AUTH).status_code == 200
        yield c


def _wait(client, job_id, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        b = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
        if b["status"] in ("done", "failed", "cancelled"):
            return b
        time.sleep(0.03)
    raise AssertionError("job did not finish")


def test_code_map_built_under_one_model_is_visible_from_another(client):
    rid = client.post("/api/releases", json={"name": "R1"}, headers=AUTH).json()["id"]
    client.post(f"/api/releases/{rid}/activate", headers=AUTH)
    job = client.post("/api/jobs/import_symbols",
                      json={"file_path": SAMPLE_ELF, "release_id": rid}, headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"
    m1 = client.post("/api/models", json={"name": "M1"}, headers=AUTH).json()["id"]
    m2 = client.post("/api/models", json={"name": "M2"}, headers=AUTH).json()["id"]
    client.post(f"/api/models/{m1}/activate", headers=AUTH)

    job = client.post("/api/jobs/build_code_map",
                      json={"model_id": m1, "release_id": rid}, headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"

    client.post(f"/api/models/{m2}/activate", headers=AUTH)
    r = client.get("/api/codemap", params={"release_id": rid}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["function_count"] > 0
    assert client.get("/api/codemap", params={"model_id": m2, "release_id": rid},
                      headers=AUTH).json()["function_count"] == r.json()["function_count"]

    # A build no longer needs a model at all.
    job = client.post("/api/jobs/build_code_map", json={"release_id": rid},
                      headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"
