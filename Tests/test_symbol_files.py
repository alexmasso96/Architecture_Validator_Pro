"""
Several symbol files per release (one per component, e.g. FBL + APP):
DB layer (CRUD, migration, legacy single-ELF writes, union readers), the
code-map merge rule, and the API/job integration.
"""
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic.Logic_Database import (
    DEFAULT_SYMBOL_COMPONENT, BaselineLockedError, ProjectDatabase)
from Application_Logic.Logic_Code_Map import ComponentParsers, build_code_map

SAMPLE_ELF = os.path.abspath("Tests/Resources/sample.elf")


def _fn(name, addr, size=16):
    return {"name": name, "address": addr, "size": size,
            "parameters": [], "return_type": "void"}


def _sym(name, addr, kind="STT_FUNC"):
    return {"name": name, "address": addr, "size": 16, "symbol_type": kind,
            "binding": "STB_GLOBAL", "section": ".text"}


def _cache(tmp_path, stem, elf_hash, funcs, gvars=None):
    """A JSON symbol cache — stands in for one component's ELF."""
    path = tmp_path / f"{stem}.json"
    path.write_text(json.dumps({
        "elf_path": f"{stem}.out", "elf_hash": elf_hash,
        "symbols": [_sym(f["name"], f["address"]) for f in funcs],
        "functions": funcs, "structures": {},
        "global_vars": gvars or {},
    }))
    return str(path)


def _store(db, elf_hash, names, gvars=None):
    db.register_elf(elf_hash, f"/x/{elf_hash}.out")
    db.bulk_insert_functions(elf_hash, [_fn(n, 0x1000 + 16 * i)
                                        for i, n in enumerate(names)])
    db.bulk_insert_global_vars(elf_hash, gvars or {})


@pytest.fixture()
def db(tmp_path):
    d = ProjectDatabase()
    d.open(str(tmp_path / "p.arch"))
    yield d
    d.close()


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------
def test_add_replace_rename_remove_and_primary_mirror(db):
    rid = db.create_release("R1")
    assert db.set_release_symbol_file(rid, "APP", "h_app", "/b/app.out") == "added"
    assert db.set_release_symbol_file(rid, "FBL", "h_fbl", "/b/fbl.out") == "added"
    files = db.get_release_symbol_files(rid)
    assert [f["component"] for f in files] == ["APP", "FBL"]
    rel = next(r for r in db.get_all_releases() if r["id"] == rid)
    assert (rel["elf_hash"], rel["elf_path"]) == ("h_app", "/b/app.out")

    # Replace keeps the component's position.
    assert db.set_release_symbol_file(rid, "APP", "h_app2", "/b/app2.out") == "replaced"
    assert db.get_release_elf_hashes(rid) == ["h_app2", "h_fbl"]

    db.rename_release_symbol_file(rid, "FBL", "Bootloader")
    with pytest.raises(ValueError):
        db.rename_release_symbol_file(rid, "Bootloader", "APP")   # taken
    with pytest.raises(ValueError):
        db.set_release_symbol_file(rid, "Other", "h_fbl")         # same file twice

    # Removing the primary promotes the next component; removing the last
    # clears the mirror.
    assert db.remove_release_symbol_file(rid, "APP")["elf_hash"] == "h_app2"
    rel = next(r for r in db.get_all_releases() if r["id"] == rid)
    assert rel["elf_hash"] == "h_fbl"
    db.remove_release_symbol_file(rid, "Bootloader")
    rel = next(r for r in db.get_all_releases() if r["id"] == rid)
    assert rel["elf_hash"] is None and db.get_release_symbol_files(rid) == []
    assert db.remove_release_symbol_file(rid, "nope") is None


def test_frozen_baseline_symbol_files_are_locked(db):
    rid = db.create_release("B1", is_baseline=1, elf_hash="h1", elf_path="/a.out")
    assert db.get_release_symbol_files(rid)[0]["component"] == DEFAULT_SYMBOL_COMPONENT
    with pytest.raises(BaselineLockedError):
        db.set_release_symbol_file(rid, "FBL", "h2")
    with pytest.raises(BaselineLockedError):
        db.remove_release_symbol_file(rid, DEFAULT_SYMBOL_COMPONENT)


def test_legacy_update_release_sets_primary_and_keeps_components(db):
    rid = db.create_release("R1")
    db.update_release(rid, elf_hash="h1", elf_path="/a.out")
    assert db.get_release_symbol_files(rid)[0]["component"] == DEFAULT_SYMBOL_COMPONENT
    db.set_release_symbol_file(rid, "FBL", "h2", "/f.out")
    # A save of a stale in-memory release (elf_hash None) must not drop files…
    db.update_release(rid, elf_hash=None, elf_path=None, name="R1")
    assert db.get_release_elf_hashes(rid) == ["h1", "h2"]
    rel = next(r for r in db.get_all_releases() if r["id"] == rid)
    assert rel["elf_hash"] == "h1"
    # …and a new legacy single-ELF import replaces only the primary.
    db.update_release(rid, elf_hash="h3", elf_path="/a3.out")
    assert db.get_release_elf_hashes(rid) == ["h3", "h2"]


def test_single_elf_project_migrates_to_one_component(tmp_path):
    path = str(tmp_path / "old.arch")
    d = ProjectDatabase()
    d.open(path)
    rid = d.create_release("R1", elf_hash="h_old", elf_path="/old.out")
    d.commit()
    d.close()
    # Simulate a project written before component files existed.
    con = sqlite3.connect(path)
    con.execute("DROP TABLE release_symbol_files")
    con.commit()
    con.close()

    d = ProjectDatabase()
    d.open(path)
    files = d.get_release_symbol_files(rid)
    assert [(f["component"], f["elf_hash"], f["elf_path"]) for f in files] == \
        [(DEFAULT_SYMBOL_COMPONENT, "h_old", "/old.out")]
    d.close()
    # Re-opening never duplicates the migrated entry.
    d = ProjectDatabase()
    d.open(path)
    assert len(d.get_release_symbol_files(rid)) == 1
    d.close()


def test_union_readers_and_component_lookup(db):
    _store(db, "h_app", ["main", "Can_Init", "App_Run"], {"g_app": "int"})
    _store(db, "h_fbl", ["main", "Can_Init", "Fbl_Flash"], {"g_fbl": "int"})
    union = db.get_function_names(["h_app", "h_fbl"])
    assert sorted(union) == ["App_Run", "Can_Init", "Fbl_Flash", "main"]
    assert union[-1] == "Fbl_Flash"          # FBL-only names after APP's
    assert sorted(db.get_function_names("h_fbl")) == ["Can_Init", "Fbl_Flash", "main"]
    assert sorted(db.get_variable_names(["h_app", "h_fbl"])) == ["g_app", "g_fbl"]
    rid = db.create_release("R1")
    db.set_release_symbol_file(rid, "APP", "h_app")
    db.set_release_symbol_file(rid, "FBL", "h_fbl")
    comps = db.get_symbol_components(db.get_release_symbol_files(rid),
                                     ["Can_Init", "Fbl_Flash", "g_app", "ghost"])
    assert comps == {"Can_Init": ["APP", "FBL"], "Fbl_Flash": ["FBL"], "g_app": ["APP"]}


def test_inlined_only_is_judged_across_components(db):
    _store(db, "h_a", ["Helper"])
    _store(db, "h_b", ["Other"])
    db.bulk_insert_inlined_functions("h_b", [{"name": "Helper", "caller": "Other"},
                                             {"name": "Gone", "caller": "Other"}])
    # Helper has an address in component A → not "inlined-only" for the release.
    assert db.get_inlined_only_function_names(["h_a", "h_b"]) == ["Gone"]
    assert db.get_inlined_only_function_names("h_b") == ["Gone", "Helper"]


def test_unreferenced_elf_data_is_dropped(db):
    _store(db, "h1", ["f"])
    r1 = db.create_release("R1")
    r2 = db.create_release("R2")
    db.set_release_symbol_file(r1, "APP", "h1")
    db.copy_release_symbol_files(r1, r2)
    db.remove_release_symbol_file(r1, "APP")
    assert db.drop_elf_if_unreferenced("h1") is False      # R2 still uses it
    db.remove_release_symbol_file(r2, "APP")
    assert db.drop_elf_if_unreferenced("h1") is True
    assert not db.has_elf("h1")


# ---------------------------------------------------------------------------
# Code map: union of components, merge-by-name rule
# ---------------------------------------------------------------------------
class _FakeFunc:
    def __init__(self, name, address):
        self.name, self.address, self.size = name, address, 16
        self.parameters, self.return_type = [], "void"


class _FakeParser:
    """In-memory parser with canned Capstone results."""

    def __init__(self, funcs, calls):
        self.functions = [_FakeFunc(n, a) for n, a in funcs]
        self.global_vars_dwarf, self.structures = {}, {}
        self._calls = calls

    def extract_subcalls(self, name):
        return list(self._calls.get(name, []))


def test_code_map_merges_components_by_name():
    app = _FakeParser([("main", 0x8000), ("App_Run", 0x8100), ("Can_Init", 0x8200)],
                      {"main": ["App_Run", "Can_Init"]})
    fbl = _FakeParser([("main", 0x100), ("Fbl_Flash", 0x200), ("Can_Init", 0x300)],
                      {"main": ["Fbl_Flash", "Can_Init"]})
    cm = build_code_map(ComponentParsers([("APP", app, True), ("FBL", fbl, True)]),
                        None, source_root="")
    fns = cm["functions"]
    assert cm["components"] == ["APP", "FBL"]
    assert set(fns) == {"main", "App_Run", "Can_Init", "Fbl_Flash"}
    # Duplicate: address from the first component, edges from both.
    assert fns["main"]["address"] == 0x8000
    assert fns["main"]["components"] == ["APP", "FBL"]
    assert fns["main"]["calls"] == ["App_Run", "Can_Init", "Fbl_Flash"]
    assert fns["Fbl_Flash"]["component"] == "FBL"
    assert fns["App_Run"]["components"] == ["APP"]


def test_code_map_skips_disassembly_per_component():
    app = _FakeParser([("main", 0x8000)], {"main": ["x"]})
    fbl = _FakeParser([("boot", 0x100)], {"boot": ["y"]})
    cm = build_code_map(ComponentParsers([("APP", app, True), ("FBL", fbl, False)]),
                        None, source_root="")
    assert cm["functions"]["main"]["calls"] == ["x"]
    assert cm["functions"]["boot"]["calls"] == []    # no call tree in FBL


def test_single_parser_code_map_has_no_component_keys():
    cm = build_code_map(_FakeParser([("main", 1)], {}), None, source_root="",
                        prefer_source_calls=True)
    assert "component" not in cm["functions"]["main"]
    assert "components" not in cm


# ---------------------------------------------------------------------------
# API / jobs
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


def _add(client, rid, component, path):
    r = client.post(f"/api/releases/{rid}/symbol-files",
                    json={"component": component, "file_path": path}, headers=AUTH)
    assert r.status_code == 200, r.text
    return _wait(client, r.json()["job_id"])


def _release(client, name="R1"):
    rid = client.post("/api/releases", json={"name": name}, headers=AUTH).json()["id"]
    assert client.post(f"/api/releases/{rid}/activate", headers=AUTH).status_code == 200
    return rid


def test_add_two_components_search_and_history(client, tmp_path):
    rid = _release(client)
    app_json = _cache(tmp_path, "app", "a" * 32,
                      [_fn("App_Run", 0x8000), _fn("Can_Init", 0x8100)])
    fbl_json = _cache(tmp_path, "fbl", "f" * 32,
                      [_fn("Fbl_Flash", 0x100), _fn("Can_Init", 0x200)])
    b = _add(client, rid, "APP", app_json)
    assert b["status"] == "done", b
    assert b["result"]["component"] == "APP" and b["result"]["action"] == "added"
    b = _add(client, rid, "FBL", fbl_json)
    assert b["status"] == "done", b
    assert b["result"]["shared_functions"] == 1

    listing = client.get(f"/api/releases/{rid}/symbol-files", headers=AUTH).json()
    assert [(f["component"], f["functions"], f["duplicates_of_earlier"])
            for f in listing["files"]] == [("APP", 2, 0), ("FBL", 2, 1)]

    # Union symbol search, labeled by component.
    r = client.get("/api/symbols", params={"q": "Fbl_Flash", "kind": "function"},
                   headers=AUTH).json()
    top = r["candidates"][0]
    assert top["name"] == "Fbl_Flash" and top["components"] == ["FBL"]
    r = client.get("/api/symbols", params={"q": "Can_Init", "kind": "function"},
                   headers=AUTH).json()
    assert r["candidates"][0]["components"] == ["APP", "FBL"]
    assert [c["name"] for c in r["candidates"]].count("Can_Init") == 1

    lineage = client.get("/api/releases/lineage", headers=AUTH).json()["nodes"]
    node = next(n for n in lineage if n["id"] == rid)
    assert [t["component"] for t in node["symbol_files"]] == ["APP", "FBL"]

    hist = [h["description"] for h in client.app.state.appstate.db.get_history(rid)]
    assert any("component 'FBL' added: fbl.json" in d for d in hist)


def test_rename_remove_and_branch_copy(client, tmp_path):
    rid = _release(client)
    _add(client, rid, "APP", _cache(tmp_path, "app", "a" * 32, [_fn("App_Run", 1)]))
    _add(client, rid, "FBL", _cache(tmp_path, "fbl", "f" * 32, [_fn("Fbl_Flash", 2)]))

    r = client.patch(f"/api/releases/{rid}/symbol-files/FBL", json={"name": "BOOT"},
                     headers=AUTH)
    assert r.status_code == 200
    assert [f["component"] for f in r.json()["files"]] == ["APP", "BOOT"]

    br = client.post(f"/api/releases/{rid}/branch", json={"name": "R1b"},
                     headers=AUTH).json()["id"]
    branch_files = client.get(f"/api/releases/{br}/symbol-files", headers=AUTH).json()
    assert [f["component"] for f in branch_files["files"]] == ["APP", "BOOT"]

    db = client.app.state.appstate.db
    r = client.delete(f"/api/releases/{rid}/symbol-files/BOOT", headers=AUTH)
    assert r.status_code == 200
    assert [f["component"] for f in r.json()["files"]] == ["APP"]
    assert db.has_elf("f" * 32)            # the branch still uses it
    hist = [h["description"] for h in db.get_history(rid)]
    assert any("component 'BOOT' removed" in d for d in hist)
    assert any("renamed to 'BOOT'" in d for d in hist)

    assert client.delete(f"/api/releases/{rid}/symbol-files/NOPE",
                         headers=AUTH).status_code == 409


def test_symbol_search_without_component_param_replaces_primary(client, tmp_path):
    """The single-file import (no component) keeps its old meaning: it sets
    the release's primary file."""
    rid = _release(client)
    job = client.post("/api/jobs/import_symbols",
                      json={"file_path": SAMPLE_ELF, "release_id": rid},
                      headers=AUTH).json()
    b = _wait(client, job["job_id"])
    assert b["status"] == "done", b
    assert b["result"]["component"] == DEFAULT_SYMBOL_COMPONENT
    files = client.get(f"/api/releases/{rid}/symbol-files", headers=AUTH).json()["files"]
    assert len(files) == 1 and files[0]["functions"] > 0


def test_baseline_symbol_files_refused(client, tmp_path):
    rid = _release(client)
    _add(client, rid, "APP", _cache(tmp_path, "app", "a" * 32, [_fn("App_Run", 1)]))
    bl = client.post("/api/baselines", json={"name": "BL", "release_id": rid},
                     headers=AUTH)
    assert bl.status_code == 200, bl.text
    blid = next(n["id"] for n in client.get("/api/releases/lineage", headers=AUTH)
                .json()["nodes"] if n["name"] == "BL")
    files = client.get(f"/api/releases/{blid}/symbol-files", headers=AUTH).json()
    assert files["is_frozen"] and [f["component"] for f in files["files"]] == ["APP"]
    r = client.post(f"/api/releases/{blid}/symbol-files",
                    json={"component": "FBL", "file_path": "/x.json"}, headers=AUTH)
    assert r.status_code == 409
    assert client.delete(f"/api/releases/{blid}/symbol-files/APP",
                         headers=AUTH).status_code == 409


def test_code_map_job_maps_all_components(client, tmp_path):
    rid = _release(client)
    mid = client.post("/api/models", json={"name": "M1"}, headers=AUTH).json()["id"]
    _add(client, rid, "APP", _cache(tmp_path, "app", "a" * 32,
                                    [_fn("App_Run", 0x8000), _fn("main", 0x8100)]))
    _add(client, rid, "FBL", _cache(tmp_path, "fbl", "f" * 32,
                                    [_fn("Fbl_Flash", 0x100), _fn("main", 0x200)]))
    job = client.post("/api/jobs/build_code_map",
                      json={"model_id": mid, "release_id": rid}, headers=AUTH).json()
    b = _wait(client, job["job_id"])
    assert b["status"] == "done", b
    cm = client.app.state.appstate.db.get_model_code_map(mid, release_id=rid)
    assert cm["components"] == ["APP", "FBL"]
    assert cm["functions"]["Fbl_Flash"]["component"] == "FBL"
    assert cm["functions"]["main"]["components"] == ["APP", "FBL"]
    detail = client.get("/api/codemap/function/main",
                        params={"model_id": mid, "release_id": rid}, headers=AUTH).json()
    assert detail["component"] == "APP"


def test_fuzzy_rematch_uses_every_component(client, tmp_path):
    rid = _release(client)
    mid = client.post("/api/models", json={"name": "M1"}, headers=AUTH).json()["id"]
    cols = client.get("/api/columns", headers=AUTH).json()["columns"]
    search = next(c["name"] for c in cols if c["type"] in ("Port Search", "Function Search"))
    db = client.app.state.appstate.db
    db.save_model_rows(mid, [{search: {"text": "Fbl_Flash"}}, {search: {"text": "App_Run"}}])
    db.commit()
    _add(client, rid, "APP", _cache(tmp_path, "app", "a" * 32, [_fn("App_Run", 1)]))
    _add(client, rid, "FBL", _cache(tmp_path, "fbl", "f" * 32, [_fn("Fbl_Flash", 2)]))
    job = client.post("/api/jobs/fuzzy_rematch", json={"model_ids": [mid]},
                      headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"
    rows = db.get_model_rows(mid)
    texts = [next(v.get("widget_text", "") for k, v in r.items() if k != search)
             for r in rows]
    assert texts[0].startswith("Fbl_Flash (") and texts[1].startswith("App_Run (")
