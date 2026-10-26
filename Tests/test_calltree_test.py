"""
Call-tree Test columns: verdict logic, evidence, overrides, layout bindings
(pure layer), plus the API/job integration further down.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_CallTree_Test as ct
from Application_Logic.Logic_Column_Layout import DEFAULT_COLUMN_LAYOUT

# A small release call tree:
#   main → app_run → (motor_stop, log_write)
#   ISR_Timer → timer_tick           (nothing calls ISR_Timer)
#   dead_helper                      (calls nothing, called by nothing)
#   LowLevel_Init                    (called only from assembly startup code)
CODE_MAP = {
    "built_at": "2026-10-26T10:00:00+00:00",
    "functions": {
        "main": {"calls": ["app_run"]},
        "app_run": {"calls": ["motor_stop", "log_write"],
                    "verified_calls": ["motor_stop"]},
        "motor_stop": {"calls": []},
        "log_write": {"calls": []},
        "ISR_Timer": {"calls": ["timer_tick"]},
        "timer_tick": {"calls": []},
        "dead_helper": {"calls": []},
        "LowLevel_Init": {"calls": []},
    },
}
# The release's .o dataset: the startup object's relocation proves the
# assembly call Reset_Handler → LowLevel_Init.
OBJECT_PAIRS = {("Reset_Handler", "LowLevel_Init"), ("app_run", "motor_stop")}


def _verdict(name, objects=None, strict=False):
    return ct.evaluate(name, ct.CallIndex(CODE_MAP, objects), strict)


# ---------------------------------------------------------------------------
# Verdict order
# ---------------------------------------------------------------------------
def test_called_function_passes_with_callers_listed():
    r = _verdict("app_run")
    assert r["verdict"] == ct.PASSED
    assert r["callers"] == [{"name": "main", "verified": False}]
    assert r["callers_total"] == 1


def test_uncalled_function_that_calls_others_is_entry_point():
    r = _verdict("ISR_Timer")
    assert r["verdict"] == ct.ENTRY_POINT
    assert r["callees_total"] == 1


def test_uncalled_leaf_fails_as_never_called():
    r = _verdict("dead_helper")
    assert r["verdict"] == ct.FAILED
    assert "Not called" in r["reason"]


def test_missing_function_fails_as_not_found():
    r = _verdict("ghost_func")
    assert r["verdict"] == ct.FAILED
    assert "not in this release's call tree" in r["reason"]


def test_assembly_only_caller_counts_when_objects_imported():
    # Without the .o dataset the low-level init looks never-called (rule a)…
    assert _verdict("LowLevel_Init")["verdict"] == ct.FAILED
    # …with the startup object imported, the relocation is a real, verified caller.
    r = _verdict("LowLevel_Init", OBJECT_PAIRS)
    assert r["verdict"] == ct.PASSED
    assert r["callers"] == [{"name": "Reset_Handler", "verified": True}]


def test_verified_flag_from_code_map_and_objects():
    r = _verdict("motor_stop", OBJECT_PAIRS)
    assert r["callers"] == [{"name": "app_run", "verified": True}]
    r = _verdict("log_write", OBJECT_PAIRS)
    assert r["callers"] == [{"name": "app_run", "verified": False}]


def test_strict_mode_counts_only_verified_callers():
    assert _verdict("motor_stop", OBJECT_PAIRS, strict=True)["verdict"] == ct.PASSED
    # log_write's only caller is source-only → no verified caller, calls nothing.
    r = _verdict("log_write", OBJECT_PAIRS, strict=True)
    assert r["verdict"] == ct.FAILED
    assert "verified" in r["reason"]


def test_evidence_caller_list_is_capped():
    cm = {"functions": {f"c{i}": {"calls": ["util"]} for i in range(60)}}
    cm["functions"]["util"] = {"calls": []}
    r = ct.evaluate("util", ct.CallIndex(cm), False)
    assert r["callers_total"] == 60
    assert len(r["callers"]) == ct.MAX_EVIDENCE_CALLERS


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
BINDING = {"release_id": 1, "match_col": "Mapped Func (Match)", "strict": False}


def _rows():
    return [
        {"Mapped Func (Match)": {"widget_text": "app_run (92%)"},
         "Port State": {"text": "Released"}},
        {"Mapped Func (Match)": {"widget_text": "dead_helper (80%)"}},
        {"Mapped Func (Match)": {"text": ""}},
        {"Mapped Func (Match)": {"widget_text": "app_run (92%)"},
         "Port State": {"text": "Retired"}},
    ]


def test_run_on_rows_verdicts_and_evidence():
    rows = _rows()
    counts = ct.run_on_rows(rows, "T", BINDING, CODE_MAP, None,
                            port_state_col="Port State", tested_at="T1")
    texts = [r["T"]["text"] for r in rows]
    assert texts == [ct.PASSED, ct.FAILED, ct.NO_MATCH, ct.NO_RESULT]
    assert counts == {ct.PASSED: 1, ct.FAILED: 1, ct.NO_MATCH: 1, ct.NO_RESULT: 1}
    first = rows[0]["T"]
    assert first["function"] == "app_run"
    assert first["tested_at"] == "T1"
    assert first["code_map_built_at"] == CODE_MAP["built_at"]


def test_run_without_code_map_is_no_data():
    rows = _rows()[:1]
    ct.run_on_rows(rows, "T", BINDING, None, None, port_state_col=None, tested_at="T1")
    assert rows[0]["T"]["text"] == ct.NO_DATA


def test_strict_without_objects_is_no_data():
    rows = _rows()[:1]
    ct.run_on_rows(rows, "T", {**BINDING, "strict": True}, CODE_MAP, None,
                   port_state_col=None, tested_at="T1")
    assert rows[0]["T"]["text"] == ct.NO_DATA


def test_rerun_keeps_manual_override_but_refreshes_tool_verdict():
    rows = _rows()[1:2]   # dead_helper → Failed
    ct.run_on_rows(rows, "T", BINDING, CODE_MAP, None, port_state_col=None, tested_at="T1")
    rows[0]["T"] = ct.apply_manual_override(rows[0]["T"], ct.PASSED, user="ana", at="T2")
    ct.run_on_rows(rows, "T", BINDING, CODE_MAP, None, port_state_col=None, tested_at="T3")
    cell = rows[0]["T"]
    assert cell["text"] == ct.PASSED and cell["user_changed"] is True
    assert cell["override_by"] == "ana"
    assert cell["auto"] == ct.FAILED and cell["tested_at"] == "T3"


def test_manual_override_set_reset_and_reject():
    cell = {"text": ct.FAILED, "auto": ct.FAILED}
    o = ct.apply_manual_override(cell, ct.ENTRY_POINT, user="u", at="t")
    assert o["text"] == ct.ENTRY_POINT and o["user_changed"]
    r = ct.apply_manual_override(o, "", user="u", at="t")
    assert r["text"] == ct.FAILED and "user_changed" not in r
    with pytest.raises(ValueError):
        ct.apply_manual_override(cell, "Maybe", user="u", at="t")


@pytest.mark.parametrize("raw,name", [
    ("app_run (92%)", "app_run"), ("app_run", "app_run"),
    ("  ns::f(int) (55%) ", "ns::f(int)"), ("", ""),
])
def test_parse_match(raw, name):
    assert ct.parse_match(raw) == name


# ---------------------------------------------------------------------------
# Layout bindings
# ---------------------------------------------------------------------------
LAYOUT = list(DEFAULT_COLUMN_LAYOUT)


def test_function_match_columns_excludes_variables():
    assert ct.function_match_columns(LAYOUT) == ["Input Port (Match)",
                                                 "Mapped Func (Match)"]


def _with_test(layout, name="T1"):
    return layout + [(name, ct.TEST_COLUMN_TYPE, True, 110)]


def test_reconcile_new_column_and_rename_follow():
    lay = _with_test(LAYOUT)
    b = ct.reconcile_bindings({}, lay, {}, {"T1": BINDING}, [1, 2])
    assert b == {"T1": BINDING}

    # Rename both the Test column and its Match column.
    lay2 = [(("Func (Match)" if c[0] == "Mapped Func (Match)" else
              "Renamed T" if c[0] == "T1" else c[0]), *c[1:]) for c in lay]
    b2 = ct.reconcile_bindings(b, lay2, {"T1": "Renamed T",
                                         "Mapped Func (Match)": "Func (Match)"},
                               {}, [1, 2])
    assert b2 == {"Renamed T": {**BINDING, "match_col": "Func (Match)"}}

    # Removing the Test column drops its binding.
    assert ct.reconcile_bindings(b, LAYOUT, {}, {}, [1, 2]) == {}


def test_reconcile_strict_toggle_on_existing_column():
    lay = _with_test(LAYOUT)
    b = ct.reconcile_bindings({"T1": BINDING}, lay, {}, {"T1": {"strict": True,
                                                                "release_id": 2}}, [1, 2])
    assert b["T1"]["strict"] is True and b["T1"]["release_id"] == 1  # target fixed


@pytest.mark.parametrize("incoming,releases,msg", [
    ({}, [1], "needs a release"),
    ({"T1": {**BINDING, "release_id": 9}}, [1], "no longer exists"),
    ({"T1": {**BINDING, "match_col": "Nope"}}, [1], "not in the layout"),
    ({"T1": {**BINDING, "match_col": "Mapped Parameter (Match)"}}, [1],
     "cannot be tested"),
])
def test_reconcile_rejects_bad_bindings(incoming, releases, msg):
    with pytest.raises(ValueError, match=msg):
        ct.reconcile_bindings({}, _with_test(LAYOUT), {}, incoming, releases)


def test_reconcile_rejects_duplicate_release_match_pair():
    lay = _with_test(_with_test(LAYOUT, "A"), "B")
    with pytest.raises(ValueError, match="already has a Test column"):
        ct.reconcile_bindings({}, lay, {}, {"A": BINDING, "B": BINDING}, [1])


def test_removing_bound_match_column_is_refused():
    lay = [c for c in _with_test(LAYOUT) if c[0] != "Mapped Func (Match)"]
    with pytest.raises(ValueError, match="delete the Test column first"):
        ct.reconcile_bindings({"T1": BINDING}, lay, {}, {}, [1])


def test_result_bindings_follow_renames():
    b = {"Release R1 Result": 1, "Gone": 2}
    out = ct.rename_name_keyed_bindings(b, {"Release R1 Result": "R1 Result"},
                                        ["R1 Result"])
    assert out == {"R1 Result": 1}


# ===========================================================================
# API + job integration
# ===========================================================================
import json as _json
import time as _time

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


def _wait(client, job_id, timeout=10.0):
    end = _time.time() + timeout
    while _time.time() < end:
        b = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
        if b["status"] in ("done", "failed", "cancelled"):
            return b
        _time.sleep(0.03)
    raise AssertionError("job did not finish")


def _setup(client):
    """Active release + one model with three ports and a code map for it."""
    rid = client.post("/api/releases", json={"name": "R1"}, headers=AUTH).json()["id"]
    assert client.post(f"/api/releases/{rid}/activate", headers=AUTH).status_code == 200
    mid = client.post("/api/models", json={"name": "M1"}, headers=AUTH).json()["id"]
    db = client.app.state.appstate.db
    db.save_model_rows(mid, [
        {"Mapped Func": {"text": "app run"},
         "Mapped Func (Match)": {"widget_text": "app_run (90%)"}},
        {"Mapped Func": {"text": "isr"},
         "Mapped Func (Match)": {"widget_text": "ISR_Timer (88%)"}},
        {"Mapped Func": {"text": "dead"},
         "Mapped Func (Match)": {"widget_text": "dead_helper (75%)"}},
    ])
    db.save_model_code_map(mid, _json.dumps(CODE_MAP), release_id=rid)
    db.commit()
    return rid, mid


def _add_test_column(client, rid, name="T1", match="Mapped Func (Match)", strict=False):
    cols = client.get("/api/columns", headers=AUTH).json()["columns"]
    cols.append({"name": name, "type": ct.TEST_COLUMN_TYPE, "visible": True,
                 "width": 110,
                 "test_binding": {"release_id": rid, "match_col": match,
                                  "strict": strict}})
    return client.put("/api/columns", json={"columns": cols, "renames": {}},
                      headers=AUTH)


def test_create_run_and_report(client):
    rid, mid = _setup(client)
    assert _add_test_column(client, rid).status_code == 200

    meta = client.get("/api/columns", headers=AUTH).json()["test_columns"]
    assert meta == [{"name": "T1", "release_id": rid, "release_name": meta[0]["release_name"],
                     "match_col": "Mapped Func (Match)", "strict": False,
                     "is_active": True, "is_baselined": False}]

    job = client.post("/api/jobs/run_calltree_tests",
                      json={"col_name": "T1", "model_ids": [mid]}, headers=AUTH).json()
    body = _wait(client, job["job_id"])
    assert body["status"] == "done", body
    assert body["result"]["models"][0]["counts"] == {"Passed": 1, "Entry point": 1,
                                                     "Failed": 1}

    rows = client.get(f"/api/models/{mid}/ports", headers=AUTH).json()["rows"]
    cells = [r["cells"]["T1"] for r in rows]
    assert [c["text"] for c in cells] == ["Passed", "Entry point", "Failed"]
    assert cells[0]["callers"] == [{"name": "main", "verified": False}]
    assert cells[0]["code_map_built_at"] == CODE_MAP["built_at"]

    status = client.get(f"/api/models/{mid}/calltree-tests", headers=AUTH).json()
    assert status["tests"][0]["code_map_built_at"] == CODE_MAP["built_at"]
    assert status["tests"][0]["has_code_map"] is True

    hist = client.app.state.appstate.db.get_history(release_id=rid)
    assert any("Call-tree test 'T1' run" in (h.get("description") or "")
               for h in hist)


def test_override_gated_and_survives_rerun(client):
    rid, mid = _setup(client)
    _add_test_column(client, rid)
    _wait(client, client.post("/api/jobs/run_calltree_tests",
                              json={"col_name": "T1", "model_ids": [mid]},
                              headers=AUTH).json()["job_id"])

    r = client.patch(f"/api/models/{mid}/ports/2", json={"updates": {"T1": "Maybe"}},
                     headers=AUTH)
    assert r.status_code == 409
    r = client.patch(f"/api/models/{mid}/ports/2", json={"updates": {"T1": "Passed"}},
                     headers=AUTH)
    assert r.status_code == 200, r.text
    cell = r.json()["cells"]["T1"]
    assert cell["text"] == "Passed" and cell["auto"] == "Failed" and cell["user_changed"]

    _wait(client, client.post("/api/jobs/run_calltree_tests",
                              json={"col_name": "T1", "model_ids": [mid]},
                              headers=AUTH).json()["job_id"])
    cell = client.get(f"/api/models/{mid}/ports", headers=AUTH).json()["rows"][2]["cells"]["T1"]
    assert cell["text"] == "Passed" and cell["auto"] == "Failed"

    # '' resets to the tool verdict.
    r = client.patch(f"/api/models/{mid}/ports/2", json={"updates": {"T1": ""}},
                     headers=AUTH)
    assert r.json()["cells"]["T1"]["text"] == "Failed"


def test_invalid_bindings_rejected(client):
    rid, _ = _setup(client)
    assert _add_test_column(client, rid, match="Mapped Parameter (Match)").status_code == 409
    assert _add_test_column(client, 9999).status_code == 409
    cols = client.get("/api/columns", headers=AUTH).json()["columns"]
    cols.append({"name": "Bare", "type": ct.TEST_COLUMN_TYPE, "visible": True, "width": 100})
    assert client.put("/api/columns", json={"columns": cols, "renames": {}},
                      headers=AUTH).status_code == 409


def test_renames_keep_bindings_and_cells(client):
    rid, mid = _setup(client)
    _add_test_column(client, rid)
    _wait(client, client.post("/api/jobs/run_calltree_tests",
                              json={"col_name": "T1", "model_ids": [mid]},
                              headers=AUTH).json()["job_id"])
    # A release-result column too — its binding used to be lost on rename.
    assert client.post(f"/api/releases/{rid}/result-column", headers=AUTH).status_code == 200
    cols = client.get("/api/columns", headers=AUTH).json()["columns"]
    result_name = next(c["name"] for c in cols if c["type"] == "ReleaseResultColumn")

    renames = {"T1": "Smoke", "Mapped Func (Match)": "Func (Match)",
               result_name: "R Result"}
    for c in cols:
        c["name"] = renames.get(c["name"], c["name"])
    assert client.put("/api/columns", json={"columns": cols, "renames": renames},
                      headers=AUTH).status_code == 200

    payload = client.get("/api/columns", headers=AUTH).json()
    assert payload["test_columns"][0]["name"] == "Smoke"
    assert payload["test_columns"][0]["match_col"] == "Func (Match)"
    assert [r["name"] for r in payload["result_columns"]] == ["R Result"]
    rows = client.get(f"/api/models/{mid}/ports", headers=AUTH).json()["rows"]
    assert rows[0]["cells"]["Smoke"]["text"] == "Passed"

    # Removing the Match column while a Test column depends on it is refused.
    cols = [c for c in payload["columns"] if c["name"] != "Func (Match)"]
    assert client.put("/api/columns", json={"columns": cols, "renames": {}},
                      headers=AUTH).status_code == 409


def test_run_and_override_refused_when_release_inactive_or_baselined(client):
    rid, mid = _setup(client)
    _add_test_column(client, rid)
    _wait(client, client.post("/api/jobs/run_calltree_tests",
                              json={"col_name": "T1", "model_ids": [mid]},
                              headers=AUTH).json()["job_id"])
    # New release baselining the bound one, and activate it.
    r2 = client.post("/api/releases", json={"name": "R-next", "copy_from_active": True,
                                            "baseline_previous": True},
                     headers=AUTH).json()["id"]
    client.post(f"/api/releases/{r2}/activate", headers=AUTH)

    meta = client.get("/api/columns", headers=AUTH).json()["test_columns"][0]
    assert meta["is_active"] is False and meta["is_baselined"] is True

    body = _wait(client, client.post("/api/jobs/run_calltree_tests",
                                     json={"col_name": "T1", "model_ids": [mid]},
                                     headers=AUTH).json()["job_id"])
    assert body["status"] == "failed"
    r = client.patch(f"/api/models/{mid}/ports/0", json={"updates": {"T1": "Failed"}},
                     headers=AUTH)
    assert r.status_code == 409
