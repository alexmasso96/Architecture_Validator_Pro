"""
Source #define macros as symbol-match candidates: generated config accessors
like `#define cfgGetRLMaxValueForHigh_u16()` never reach the ELF, yet ports
name them and the Code Map shows them as callees.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Source_Macros as sm
from Application_Logic.Logic_Database import ProjectDatabase
from Application_Logic.Logic_Symbol_Matcher import SymbolMatcher

HEADER = """\
#ifndef CFG_GEN_GET_MACRO_H
#define CFG_GEN_GET_MACRO_H
#define cfgGetPvBuckFactorK4_b16
#define cfgGetRLMaxValueForHigh_u16() (cfgData.rlMaxHigh_u16)
#if ( defined MISRA_CHECK_ON )
#pragma PRQA_MACRO_MESSAGES_OFF "cfgGetRLMaxValueForHigh_u16" 0310
#endif
#define cfgGetRLMaxValueForHigh_pu16(index_x) (&cfgData.rlMaxHigh_u16[index_x])
  #  define cfgGetRLMinValueForLow_u16()   (cfgData.rlMinLow_u16)
#define RL_MAX_SAMPLES 16u
#define _RESERVED_X
#endif
"""
SOURCE = """\
#include "cfg_gen_get_macro.h"
rlsSignal_t rlsCfgGetRlSignal(void)
{
    rlMaxValueForHigh_u16 = cfgGetRLMaxValueForHigh_u16();
}
"""


def test_extract_function_and_object_macros():
    funcs, objs = sm.extract_macros(HEADER)
    assert funcs == {"cfgGetRLMaxValueForHigh_u16", "cfgGetRLMaxValueForHigh_pu16",
                     "cfgGetRLMinValueForLow_u16"}
    # Include guard and reserved names are skipped; an empty accessor stays.
    assert objs == {"cfgGetPvBuckFactorK4_b16", "RL_MAX_SAMPLES"}


def test_name_defined_both_ways_counts_as_function():
    res = sm.collect_macros([("a.h", "#define X_GET()  1\n"), ("b.h", "#define X_GET 1\n")])
    assert res == {"functions": ["X_GET"], "objects": []}


@pytest.fixture()
def db(tmp_path):
    d = ProjectDatabase()
    d.open(str(tmp_path / "p.arch"))
    yield d
    d.close()


def test_release_macros_cached_and_refreshed_when_source_changes(db):
    rid = db.create_release("R1")
    assert sm.release_macros(db, rid) == {"functions": [], "objects": []}
    db.save_release_source_files(rid, [("inc/cfg.h", HEADER), ("src/rls.c", SOURCE)])
    first = sm.release_macros(db, rid)
    assert "cfgGetRLMaxValueForHigh_u16" in first["functions"]
    assert json.loads(db.get_meta(f"source_macros:{rid}"))["functions"] == first["functions"]
    # New source → new fingerprint → re-extracted.
    db.save_release_source_files(rid, [("inc/cfg.h", "#define NEW_ONE() 0\n")])
    assert sm.release_macros(db, rid)["functions"] == ["NEW_ONE"]


def test_matcher_offers_macros_and_elf_symbols_win(db):
    db.register_elf("h1", "/x.out")
    db.bulk_insert_functions("h1", [{"name": "RL_MAX_SAMPLES", "address": 1, "size": 4,
                                     "parameters": [], "return_type": "void"}])
    m = SymbolMatcher(None, db=db, elf_hash=["h1"],
                      macros={"functions": ["cfgGetRLMaxValueForHigh_u16"],
                              "objects": ["RL_MAX_SAMPLES", "cfgGetPvBuckFactorK4_b16"]})
    assert m.find_top_function_matches("cfgGetRLMaxValueForHigh_u16", 1) == \
        [("cfgGetRLMaxValueForHigh_u16", 100)]
    assert m.find_top_matches("cfgGetPvBuckFactorK4_b16", 1)[0][1] == 100
    # RL_MAX_SAMPLES is a real ELF symbol here: not tagged as a macro.
    assert m.macro_names == {"cfgGetRLMaxValueForHigh_u16", "cfgGetPvBuckFactorK4_b16"}


# ---------------------------------------------------------------------------
# API: the reported case — a Port Search column naming a config macro.
# ---------------------------------------------------------------------------
TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SAMPLE_ELF = os.path.abspath("Tests/Resources/sample.elf")


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


def test_port_search_matches_source_macro(client, tmp_path):
    src = tmp_path / "src"
    (src / "inc").mkdir(parents=True)
    (src / "inc" / "cfg_gen_get_macro.h").write_text(HEADER)
    (src / "rls_cfg.c").write_text(SOURCE)

    rid = client.post("/api/releases", json={"name": "R1"}, headers=AUTH).json()["id"]
    client.post(f"/api/releases/{rid}/activate", headers=AUTH)
    job = client.post("/api/jobs/import_symbols",
                      json={"file_path": SAMPLE_ELF, "release_id": rid}, headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"

    # Before the source is imported the macro can't be found.
    r = client.get("/api/symbols", params={"q": "cfgGetRLMaxValueForHigh_u16"},
                   headers=AUTH).json()
    assert all(c["name"] != "cfgGetRLMaxValueForHigh_u16" for c in r["candidates"])

    job = client.post(f"/api/releases/{rid}/source", json={"source_dir": str(src)},
                      headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"
    r = client.get("/api/symbols", params={"q": "cfgGetRLMaxValueForHigh_u16"},
                   headers=AUTH).json()
    top = r["candidates"][0]
    assert top["name"] == "cfgGetRLMaxValueForHigh_u16" and top["score"] == 100
    assert top["macro"] is True

    # Re-match fills the Match cell of a Port Search column.
    mid = client.post("/api/models", json={"name": "M1"}, headers=AUTH).json()["id"]
    cols = client.get("/api/columns", headers=AUTH).json()["columns"]
    names = [c["name"] for c in cols]
    search = next(c["name"] for c in cols if c["type"] == "Port Search")
    match = names[names.index(search) + 1]
    db = client.app.state.appstate.db
    db.save_model_rows(mid, [{search: {"text": "cfgGetRLMaxValueForHigh_u16"}}])
    db.commit()
    job = client.post("/api/jobs/fuzzy_rematch", json={"model_ids": [mid]},
                      headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"
    assert db.get_model_rows(mid)[0][match]["widget_text"] == \
        "cfgGetRLMaxValueForHigh_u16 (100%)"

    # Dropping the source takes the macros out of the pool again.
    assert client.delete(f"/api/releases/{rid}/source", headers=AUTH).status_code == 200
    r = client.get("/api/symbols", params={"q": "cfgGetRLMaxValueForHigh_u16"},
                   headers=AUTH).json()
    assert all(not c.get("macro") for c in r["candidates"])
