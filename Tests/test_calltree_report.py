"""
Call-tree Test column HTML report: data assembly (valid rows, overrides,
stale marks, call-tree snapshots), the self-contained document, and the API.
"""
import json
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_CallTree_Test as ct
from Application_Logic import Logic_CallTree_Report as rep

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
        "loop_a": {"calls": ["loop_b"]},
        "loop_b": {"calls": ["loop_a"]},
    },
}
LAYOUT = [("Mapped Func", "Function Search", True, 100),
          ("Mapped Func (Match)", "Function Match", True, 100),
          ("Port State", "PortStateColumn", True, 80),
          ("T1", ct.TEST_COLUMN_TYPE, True, 100)]
BINDING = {"release_id": 1, "match_col": "Mapped Func (Match)", "strict": False}


def _rows():
    rows = [
        {"Mapped Func": {"text": "motor stop"},
         "Mapped Func (Match)": {"widget_text": "motor_stop (90%)"}},
        {"Mapped Func": {"text": "isr"},
         "Mapped Func (Match)": {"widget_text": "ISR_Timer (88%)"}},
        {"Mapped Func": {"text": "<dead & gone>"},
         "Mapped Func (Match)": {"widget_text": "dead_helper (75%)"}},
        {"Mapped Func": {"text": "old"}, "Port State": {"text": "Retired"},
         "Mapped Func (Match)": {"widget_text": "log_write (70%)"}},
        {"Mapped Func": {"text": "unmatched"}},
    ]
    ct.run_on_rows(rows, "T1", BINDING, CODE_MAP, None,
                   port_state_col="Port State", tested_at="2026-10-27T09:00:00+00:00")
    rows.append({"Mapped Func": {"text": "never run"}})   # added after the run
    return rows


def _report(rows=None, code_map=CODE_MAP, **kw):
    return rep.build_report(
        project="Demo", release="R1", col_name="T1", binding=BINDING,
        author="alice", created_at="2026-10-27 10:00 UTC", layout=LAYOUT,
        models=[("Motor", rows if rows is not None else _rows(), code_map)],
        port_state_col="Port State", **kw)


def test_only_valid_result_rows_become_entries():
    data = _report()
    ch = data["chapters"][0]
    # Retired port (No Result) and the never-run row are not valid results.
    assert [e["verdict"] for e in ch["entries"]] == \
        [ct.PASSED, ct.ENTRY_POINT, ct.FAILED, ct.NO_MATCH]
    assert ch["skipped"] == 2
    assert data["totals"] == {ct.PASSED: 1, ct.ENTRY_POINT: 1, ct.FAILED: 1,
                              ct.NO_MATCH: 1}
    e = ch["entries"][0]
    assert (e["port"], e["function"], e["row"]) == ("motor stop", "motor_stop", 1)
    assert e["reason"].startswith("Called by 1")


def test_manual_override_and_stale_are_reported():
    rows = _rows()
    rows[2]["T1"] = ct.apply_manual_override(rows[2]["T1"], ct.PASSED,
                                             user="bob", at="2026-10-27T09:30")
    newer = dict(CODE_MAP, built_at="2026-10-27T12:00:00+00:00")
    e = _report(rows, code_map=newer)["chapters"][0]["entries"][2]
    assert e["manual"] and e["verdict"] == ct.PASSED and e["auto"] == ct.FAILED
    assert e["override_by"] == "bob" and e["stale"]


def test_snapshot_path_and_trees():
    index = ct.CallIndex(CODE_MAP, {("app_run", "motor_stop")})
    s = rep.call_tree_snapshot(index, "motor_stop")
    assert s["found"] and s["path"] == ["main", "app_run", "motor_stop"]
    assert s["path_verified"] == [False, True]
    assert s["callers"][0]["name"] == "app_run"
    assert s["callers"][0]["children"][0]["name"] == "main"
    root = rep.call_tree_snapshot(index, "ISR_Timer")
    assert root["path"] == ["ISR_Timer"]
    assert root["callees"][0]["name"] == "timer_tick"
    assert not rep.call_tree_snapshot(index, "ghost")["found"]
    # Every route loops: still a finite chain ending at the function.
    loop = rep.call_tree_snapshot(index, "loop_a")
    assert loop["path"][-1] == "loop_a" and len(loop["path"]) == 2


def test_html_is_self_contained_routed_and_escaped():
    doc = rep.render_html(_report())
    assert doc.startswith("<!doctype html>")
    # No external assets of any kind.
    assert not re.search(r"""(src|href)\s*=\s*["'](https?:|//)""", doc)
    assert "<link" not in doc and "@import" not in doc
    # Header facts.
    for text in ("Demo", "R1", "T1", "alice", "2026-10-27 10:00 UTC"):
        assert text in doc
    # A chapter for the model and a detail section per entry, linked by hash.
    assert 'id="m-motor"' in doc
    ids = re.findall(r'<section class="detail" id="(e-\d+)"', doc)
    assert ids == ["e-1", "e-2", "e-3", "e-4"]
    for i in ids:
        assert f'href="#{i}"' in doc
    assert "<svg" in doc and "Call path" in doc
    # User data is escaped.
    assert "&lt;dead &amp; gone&gt;" in doc and "<dead & gone>" not in doc


def test_model_without_results_or_code_map_gets_a_note():
    data = rep.build_report(
        project="Demo", release="R1", col_name="T1", binding=BINDING, author="a",
        created_at="now", layout=LAYOUT, models=[("Empty", [{"x": {}}], None)])
    doc = rep.render_html(data)
    assert data["entry_count"] == 0
    assert "has not been run on it" in doc and "no code map" in doc


def test_report_filename_is_safe():
    name = rep.report_filename("My/Proj", "R 1", "Test R1 · Func (Match)", "2026-10-27T1")
    assert name.endswith("_2026-10-27.html") and "/" not in name and " " not in name


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "prefs.json"))
    from fastapi.testclient import TestClient
    from backend.app import create_app
    with TestClient(create_app(token=TOKEN)) as c:
        assert c.post("/api/project/new", json={"path": str(tmp_path / "Demo.arch")},
                      headers=AUTH).status_code == 200
        yield c


def _wait(client, job_id, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        b = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
        if b["status"] in ("done", "failed", "cancelled"):
            return b
        time.sleep(0.03)
    raise AssertionError("job did not finish")


def _setup_and_run(client):
    rid = client.post("/api/releases", json={"name": "R1"}, headers=AUTH).json()["id"]
    assert client.post(f"/api/releases/{rid}/activate", headers=AUTH).status_code == 200
    mids = [client.post("/api/models", json={"name": n}, headers=AUTH).json()["id"]
            for n in ("Motor", "Door")]
    db = client.app.state.appstate.db
    db.save_model_rows(mids[0], [
        {"Mapped Func": {"text": "app run"},
         "Mapped Func (Match)": {"widget_text": "app_run (90%)"}},
        {"Mapped Func": {"text": "dead"},
         "Mapped Func (Match)": {"widget_text": "dead_helper (75%)"}},
    ])
    db.save_model_code_map(mids[0], json.dumps(CODE_MAP), release_id=rid)
    db.commit()
    cols = client.get("/api/columns", headers=AUTH).json()["columns"]
    cols.append({"name": "T1", "type": ct.TEST_COLUMN_TYPE, "visible": True, "width": 110,
                 "test_binding": {"release_id": rid, "match_col": "Mapped Func (Match)",
                                  "strict": False}})
    assert client.put("/api/columns", json={"columns": cols, "renames": {}},
                      headers=AUTH).status_code == 200
    job = client.post("/api/jobs/run_calltree_tests",
                      json={"col_name": "T1", "model_ids": [mids[0]]}, headers=AUTH).json()
    assert _wait(client, job["job_id"])["status"] == "done"
    return rid, mids


def test_report_written_to_chosen_path(client, tmp_path, monkeypatch):
    from Application_Logic.Logic_File_Locking import FileLockManager
    monkeypatch.setattr(FileLockManager, "get_username", staticmethod(lambda: "tester1"))
    _setup_and_run(client)
    out = tmp_path / "report"          # no extension → .html appended
    r = client.post("/api/calltree-tests/report",
                    json={"col_name": "T1", "output_path": str(out)}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"].endswith("report.html") and body["entries"] == 2
    assert body["models"] >= 2 and "html" not in body   # + any default model
    doc = open(body["path"], encoding="utf-8").read()
    assert "tester1" in doc and "Demo" in doc and "R1" in doc
    assert 'id="m-motor"' in doc and 'id="m-door"' in doc
    assert doc.count('<section class="detail"') == 2


def test_report_returned_inline_and_gated_on_active_release(client):
    rid, _mids = _setup_and_run(client)
    r = client.post("/api/calltree-tests/report", json={"col_name": "T1"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["html"].startswith("<!doctype html>")
    assert r.json()["filename"].endswith(".html")
    assert client.post("/api/calltree-tests/report", json={"col_name": "nope"},
                       headers=AUTH).status_code == 409
    other = client.post("/api/releases", json={"name": "R2"}, headers=AUTH).json()["id"]
    client.post(f"/api/releases/{other}/activate", headers=AUTH)
    r = client.post("/api/calltree-tests/report", json={"col_name": "T1"}, headers=AUTH)
    assert r.status_code == 409 and "Activate release" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Operator details (Preferences → Reports) instead of the computer user name.
# ---------------------------------------------------------------------------
def test_parse_operator_fields():
    raw = json.dumps([{"label": "Operator", "value": " Jane Doe "},
                      {"label": "Email", "value": ""},          # empty → dropped
                      {"label": "", "value": "QA Team"},        # no label → Operator
                      "junk", {"label": "Site", "value": "x" * 500}])
    fields = rep.parse_operator_fields(raw)
    assert fields[:2] == [("Operator", "Jane Doe"), ("Operator", "QA Team")]
    assert fields[2][0] == "Site" and len(fields[2][1]) == rep.MAX_OPERATOR_TEXT
    assert rep.parse_operator_fields("not json") == []
    assert rep.parse_operator_fields(None) == []
    many = json.dumps([{"label": f"F{i}", "value": "v"} for i in range(30)])
    assert len(rep.parse_operator_fields(many)) == rep.MAX_OPERATOR_FIELDS


def test_operator_fields_replace_created_by():
    doc = rep.render_html(_report(operator=[("Operator", "Jane <Doe>"),
                                            ("Email", "jane@example.com")]))
    assert "<th>Operator</th><td>Jane &lt;Doe&gt;</td>" in doc
    assert "<th>Email</th><td>jane@example.com</td>" in doc
    assert "Created by" not in doc and "alice" not in doc
    # Nothing configured → the computer's user name, labeled Operator.
    doc = rep.render_html(_report())
    assert "<th>Operator</th><td>alice</td>" in doc


def test_report_uses_operator_preference(client, tmp_path, monkeypatch):
    from Application_Logic.Logic_File_Locking import FileLockManager
    monkeypatch.setattr(FileLockManager, "get_username", staticmethod(lambda: "pc-user42"))
    _setup_and_run(client)
    assert client.put("/api/prefs", json={
        "key": rep.OPERATOR_PREF_KEY,
        "value": json.dumps([{"label": "Operator", "value": "Jane Doe"},
                             {"label": "Team", "value": "Validation"}])},
        headers=AUTH).status_code == 200
    doc = client.post("/api/calltree-tests/report", json={"col_name": "T1"},
                      headers=AUTH).json()["html"]
    assert "Jane Doe" in doc and "Validation" in doc and "pc-user42" not in doc
