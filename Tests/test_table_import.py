"""
#8 Import Profiles engine — reader, rule pipeline, mapping, update diff, and
parity of the built-in InterfaceExport profile with the legacy importer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Table_Import as ti
from Application_Logic import Logic_InterfaceExport_Import as legacy


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------
def test_csv_semicolon_delimiter_sniffed(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("Name;Value\na;1\nb;2\n", encoding="utf-8")
    cols, rows = ti.read_table(str(p))
    assert cols == ["Name", "Value"]
    assert rows == [{"Name": "a", "Value": "1"}, {"Name": "b", "Value": "2"}]


def test_xlsx_header_row_and_duplicate_headers(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Big Title Banner"])
    ws.append(["Name", "", "Name"])
    ws.append(["a", "x", "b"])
    p = str(tmp_path / "t.xlsx")
    wb.save(p)

    assert ti.suggest_header_row(p) == 1
    cols, rows = ti.read_table(p, header_row=1)
    assert cols == ["Name", "Column 2", "Name (2)"]
    assert rows == [{"Name": "a", "Column 2": "x", "Name (2)": "b"}]


def test_xlsx_x000d_normalized(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Ops"])
    ws.append(["op1,_x000D_\nop2"])
    p = str(tmp_path / "t.xlsx")
    wb.save(p)
    _, rows = ti.read_table(p)
    assert "_x000D_" not in rows[0]["Ops"]


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,op,text,expected", [
    ("Hello World", "contains", "world", True),
    ("Hello", "not_contains", "x", True),
    ("ABC", "equals", "abc", True),
    ("ABC", "not_equals", "abd", True),
    ("Foo::Bar", "starts_with", "foo", True),
    ("file.xlsx", "ends_with", ".XLSX", True),
    ("P10_SW_Arch", "regex", r"P\d+_SW", True),
    ("   ", "is_empty", "", True),
    ("x", "not_empty", "", True),
    ("Hello", "contains", "bye", False),
    ("x", "regex", r"[", False),   # invalid regex → no match, no crash
])
def test_match_condition(value, op, text, expected):
    assert ti.match_condition(value, op, text) is expected


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
ROWS = [
    {"Path": "A::B::ModelX::P10_SW_Arch_Public::p", "Port": "p1",
     "Ops": "op1,\nop2", "Req": "IFoo"},
    {"Path": "A::B::ModelY::P10_SW_Arch_Public::q", "Port": "p2",
     "Ops": "", "Req": "IBar"},
    {"Path": "A::B::ModelX::Private::r", "Port": "p3", "Ops": "", "Req": "IX"},
    {"Path": "A::B::ModelX::P10_SW_Arch_Public::s", "Port": "p4",
     "Ops": "op9", "Req": ""},
]
COLS = ["Path", "Port", "Ops", "Req"]


def test_filter_keep_and_drop():
    keep = ti.apply_pipeline(COLS, ROWS, [
        {"type": "filter", "action": "keep", "match": "all",
         "conditions": [{"column": "Path", "op": "contains",
                         "value": "P10_SW_Arch_Public"}]}])
    assert [r["Port"] for r in keep["rows"]] == ["p1", "p2", "p4"]

    drop = ti.apply_pipeline(COLS, ROWS, [
        {"type": "filter", "action": "drop", "match": "any",
         "conditions": [{"column": "Req", "op": "is_empty", "value": ""},
                        {"column": "Port", "op": "equals", "value": "p3"}]}])
    assert [r["Port"] for r in drop["rows"]] == ["p1", "p2"]


def test_derive_conditional_column():
    out = ti.apply_pipeline(COLS, ROWS, [
        {"type": "derive", "target": "Kind",
         "cases": [{"column": "Req", "op": "not_empty", "value": "",
                    "result": "with-tc"},
                   {"column": "Port", "op": "equals", "value": "p4",
                    "result": "special"}],
         "default": "without-tc"}])
    kinds = [r["Kind"] for r in out["rows"]]
    assert kinds == ["with-tc", "with-tc", "with-tc", "special"]
    assert "Kind" in out["columns"]


def test_split_cell_segments():
    out = ti.apply_pipeline(COLS, ROWS, [
        {"type": "split_cell", "column": "Path", "delimiter": "::",
         "segment": 2, "target": "Model"},
        {"type": "split_cell", "column": "Path", "delimiter": "::",
         "segment": -1, "target": "Leaf"}])
    assert out["rows"][0]["Model"] == "ModelX"
    assert out["rows"][0]["Leaf"] == "p"
    short = ti.apply_pipeline(["A"], [{"A": "one"}], [
        {"type": "split_cell", "column": "A", "delimiter": "::",
         "segment": 5, "target": "B"}])
    assert short["rows"][0]["B"] == ""   # out of range → empty, no crash


def test_expand_rows():
    out = ti.apply_pipeline(COLS, ROWS, [
        {"type": "expand", "column": "Ops", "delimiter": "\n",
         "take_before": ","}])
    p1 = [r for r in out["rows"] if r["Port"] == "p1"]
    assert [r["Ops"] for r in p1] == ["op1", "op2"]
    # Empty cell → the row survives unexpanded.
    assert len([r for r in out["rows"] if r["Port"] == "p2"]) == 1


def test_clean_fill_down_for_merged_cells():
    rows = [{"G": "Group1", "V": "a"}, {"G": "", "V": "b"},
            {"G": "Group2", "V": "c"}, {"G": "  ", "V": "d"}]
    out = ti.apply_pipeline(["G", "V"], rows, [
        {"type": "clean", "ops": [{"op": "fill_down", "column": "G"}]}])
    assert [r["G"] for r in out["rows"]] == ["Group1", "Group1",
                                            "Group2", "Group2"]


def test_pipeline_stage_counters_and_partition():
    out = ti.apply_pipeline(COLS, ROWS, ti.interface_export_rules(
        "Path", ops_col="Ops", required_col="Req"))
    # Stage counters: 4 → 3 (P10) → 2 (required) → derive → partition → expand
    assert out["stages"][0]["rows_in"] == 4
    assert out["stages"][0]["rows_out"] == 3
    assert out["stages"][1]["rows_out"] == 2
    parts = ti.partition_rows(out["rows"], out["partition_column"])
    assert set(parts) == {"ModelX", "ModelY"}
    assert [r["Ops"] for r in parts["ModelX"]] == ["op1", "op2"]


def test_unknown_rule_type_raises():
    with pytest.raises(ValueError):
        ti.apply_pipeline(COLS, ROWS, [{"type": "teleport"}])


# ---------------------------------------------------------------------------
# Mapping + provenance locking (#7)
# ---------------------------------------------------------------------------
def test_apply_mapping_locks_configured_targets():
    rows = [{"Req": "IFoo", "TC": "TC-001"}]
    out = ti.apply_mapping(rows, {"Req": "Interface", "TC": "TC. ID"},
                           locked_targets={"TC. ID"})
    assert out[0]["Interface"] == {"text": "IFoo"}
    assert out[0]["TC. ID"] == {"text": "TC-001", "origin": "import"}


# ---------------------------------------------------------------------------
# Match-and-update diff (#7 fill-by-duplicates + locked semantics)
# ---------------------------------------------------------------------------
def test_update_diff_fills_all_duplicate_key_rows():
    existing = [
        {"Interface": {"text": "IFoo"}, "Function": {"text": "f1"},
         "TC. ID": {"text": ""}},
        {"Interface": {"text": "ifoo "}, "Function": {"text": "f2"},
         "TC. ID": {"text": ""}},          # same key, case/space-insensitive
        {"Interface": {"text": "IBar"}, "Function": {"text": "g"},
         "TC. ID": {"text": ""}},
    ]
    imported = [{"Interface": {"text": "IFoo"}, "TC. ID": {"text": "TC-1"}}]
    d = ti.compute_update_diff(existing, imported,
                               key_columns=["Interface"],
                               value_columns=["TC. ID"])
    assert [u["row_index"] for u in d["updates"]] == [0, 1]
    assert all(u["changes"]["TC. ID"]["new"] == "TC-1" for u in d["updates"])
    assert d["unmatched"] == 1
    assert d["orphans"] == [] and d["conflicts"] == [] and d["locked"] == []


def test_update_diff_locked_cells_flagged_never_applied():
    existing = [{"Interface": {"text": "IFoo"},
                 "TC. ID": {"text": "TC-1", "origin": "import"}}]
    imported = [{"Interface": {"text": "IFoo"}, "TC. ID": {"text": "TC-99"}}]
    d = ti.compute_update_diff(existing, imported, ["Interface"], ["TC. ID"])
    assert d["updates"] == []
    assert d["locked"] == [{"row_index": 0, "column": "TC. ID",
                            "old": "TC-1", "import_value": "TC-99"}]


def test_update_diff_composite_key_and_conflicts_and_orphans():
    existing = [{"Comp": {"text": "C1"}, "Interface": {"text": "IFoo"},
                 "TC. ID": {"text": ""}}]
    imported = [
        {"Comp": {"text": "C1"}, "Interface": {"text": "IFoo"},
         "TC. ID": {"text": "TC-1"}},
        {"Comp": {"text": "C1"}, "Interface": {"text": "IFoo"},
         "TC. ID": {"text": "TC-2"}},      # duplicate key, differing value
        {"Comp": {"text": "C2"}, "Interface": {"text": "IZap"},
         "TC. ID": {"text": "TC-3"}},      # matches nothing
    ]
    d = ti.compute_update_diff(existing, imported,
                               ["Comp", "Interface"], ["TC. ID"])
    assert d["updates"][0]["changes"]["TC. ID"]["new"] == "TC-1"
    assert d["conflicts"] == [{"key": ("c1", "ifoo"), "columns": ["TC. ID"]}]
    assert d["orphans"] == [{"key": ("c2", "izap")}]


# ---------------------------------------------------------------------------
# Acceptance: the built-in InterfaceExport profile reproduces the legacy importer.
# ---------------------------------------------------------------------------
def test_interface_export_profile_parity_with_legacy_importer():
    columns = ["Port", "Path", "Operations", "Required Interface"]
    rows = [
        {"Port": "pA", "Operations": "op1,\nop2", "Required Interface": "IFoo",
         "Path": "Components::P_SW_Components::ModelA::P10_SW_Arch_Public::x"},
        {"Port": "pB", "Operations": "", "Required Interface": "IBar",
         "Path": "Components::P_SW_Components::ModelB::P10_SW_Arch_Public::y"},
        {"Port": "pC", "Operations": "nope", "Required Interface": "IX",
         "Path": "Components::P_SW_Components::ModelA::Private::z"},
        {"Port": "pD", "Operations": "op9", "Required Interface": "",
         "Path": "Components::P_SW_Components::ModelA::P10_SW_Arch_Public::w"},
    ]
    mapping = {"Port": "Port Name", "Operations": "Function",
               "Required Interface": "Interface"}

    old = legacy.build_import_data(
        rows, mapping, path_col="Path", ops_col="Operations",
        required_col="Required Interface")

    out = ti.apply_pipeline(
        columns, rows,
        ti.interface_export_rules("Path", ops_col="Operations",
                          required_col="Required Interface"))
    new = {model: ti.apply_mapping(part, mapping)
           for model, part in
           ti.partition_rows(out["rows"], out["partition_column"]).items()}

    assert new == old


# ---------------------------------------------------------------------------
# Phase B — sample-session + preview + profiles API
# ---------------------------------------------------------------------------
TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

INTERFACE_EXPORT_CSV = (
    "Port;Path;Operations;Required Interface\n"
    "pA;Components::P_SW_Components::ModelA::P10_SW_Arch_Public::x;op1\nop2;IFoo\n"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_IMPORT_PROFILES_FILE",
                       str(tmp_path / "profiles.json"))
    from fastapi.testclient import TestClient
    from backend.app import create_app
    with TestClient(create_app(token=TOKEN)) as c:
        yield c


def _make_sample_csv(tmp_path) -> str:
    p = tmp_path / "INTERFACE_EXPORT_export.csv"
    rows = ["Port,Path,Operations,Required Interface"]
    for i in range(30):
        model = "ModelA" if i % 2 == 0 else "ModelB"
        req = "IFoo" if i % 3 else ""
        rows.append(
            f"p{i},Components::P_SW_Components::{model}::P10_SW_Arch_Public::n{i},"
            f"op{i},{req}")
    rows.append("px,Components::P_SW_Components::ModelA::Private::z,opx,IBar")
    p.write_text("\n".join(rows), encoding="utf-8")
    return str(p)


def test_sample_preview_and_render_all_roundtrip(client, tmp_path):
    path = _make_sample_csv(tmp_path)
    r = client.post("/api/import/sample", json={"path": path}, headers=AUTH)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["columns"] == ["Port", "Path", "Operations", "Required Interface"]
    assert s["row_count"] == 31 and s["truncated"] is False
    assert len(s["rows"]) == 10

    rules = ti.interface_export_rules("Path", ops_col="Operations",
                              required_col="Required Interface")
    r = client.post("/api/import/preview",
                    json={"sample_id": s["sample_id"], "rules": rules,
                          "mapping": {"Port": "Port Name",
                                      "Operations": "Function"},
                          "locked_targets": ["Function"], "limit": 5},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["stages"][0]["rows_in"] == 31       # P10 filter sees all rows
    assert pv["partition_column"] == "__model__"
    assert set(pv["partitions"]) == {"ModelA", "ModelB"}
    assert len(pv["rows"]) == 5
    assert pv["mapped_rows"][0]["Function"]["origin"] == "import"

    # Render-all view: complete filtered dataset, paged.
    r = client.post("/api/import/preview/all",
                    json={"sample_id": s["sample_id"], "rules": rules,
                          "offset": 0, "limit": 8}, headers=AUTH)
    all1 = r.json()
    assert all1["total"] == pv["row_count"]
    assert len(all1["rows"]) == 8
    r = client.post("/api/import/preview/all",
                    json={"sample_id": s["sample_id"], "rules": rules,
                          "offset": all1["total"] - 3, "limit": 8},
                    headers=AUTH)
    assert len(r.json()["rows"]) == 3

    # Dropping the sample invalidates it.
    client.delete(f"/api/import/sample/{s['sample_id']}", headers=AUTH)
    r = client.post("/api/import/preview",
                    json={"sample_id": s["sample_id"], "rules": []},
                    headers=AUTH)
    assert r.status_code == 404


def test_preview_rejects_unknown_rule(client, tmp_path):
    path = _make_sample_csv(tmp_path)
    sid = client.post("/api/import/sample", json={"path": path},
                      headers=AUTH).json()["sample_id"]
    r = client.post("/api/import/preview",
                    json={"sample_id": sid, "rules": [{"type": "teleport"}]},
                    headers=AUTH)
    assert r.status_code == 400


def test_sample_bad_path_is_400(client):
    r = client.post("/api/import/sample", json={"path": "/no/such/file.xlsx"},
                    headers=AUTH)
    assert r.status_code == 400


def test_profiles_crud_and_builtins(client):
    r = client.get("/api/import/profiles", headers=AUTH)
    profiles = r.json()["profiles"]
    builtins = [p for p in profiles if p["builtin"]]
    assert {p["id"] for p in builtins} == {"builtin-interface_export-required",
                                          "builtin-interface_export-all-ports"}

    # Create → list → update → delete.
    body = {"name": "DNG TC IDs", "mode": "update",
            "key_columns": ["Interface"],
            "rules": [{"type": "filter", "action": "keep", "match": "all",
                       "conditions": [{"column": "ID", "op": "not_empty",
                                       "value": ""}]}],
            "mapping": {"ID": "TC. ID", "Interface": "Interface"},
            "locked_targets": ["TC. ID"],
            "filename_pattern": "DNG_*.csv"}
    created = client.post("/api/import/profiles", json=body,
                          headers=AUTH).json()["profile"]
    assert created["id"] and created["builtin"] is False

    listed = client.get("/api/import/profiles", headers=AUTH).json()["profiles"]
    assert any(p["id"] == created["id"] for p in listed)

    created["name"] = "DNG TC IDs v2"
    updated = client.post("/api/import/profiles", json=created,
                          headers=AUTH).json()["profile"]
    assert updated["name"] == "DNG TC IDs v2"
    listed = client.get("/api/import/profiles", headers=AUTH).json()["profiles"]
    assert sum(1 for p in listed if p["id"] == created["id"]) == 1

    assert client.delete(f"/api/import/profiles/{created['id']}",
                         headers=AUTH).status_code == 200
    listed = client.get("/api/import/profiles", headers=AUTH).json()["profiles"]
    assert not any(p["id"] == created["id"] for p in listed)


def test_profiles_reject_invalid(client):
    r = client.post("/api/import/profiles",
                    json={"name": "bad", "mode": "sideways"}, headers=AUTH)
    assert r.status_code == 400
    r = client.post("/api/import/profiles",
                    json={"name": "bad", "rules": [{"type": "teleport"}]},
                    headers=AUTH)
    assert r.status_code == 400
    r = client.post("/api/import/profiles",
                    json={"id": "builtin-interface_export-required", "name": "hack"},
                    headers=AUTH)
    assert r.status_code == 400
    assert client.delete("/api/import/profiles/builtin-interface_export-required",
                         headers=AUTH).status_code == 400
    assert client.delete("/api/import/profiles/nope",
                         headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# Phase C — diff + commit (append and match-and-update with locking)
# ---------------------------------------------------------------------------
def _open_project(client, tmp_path):
    arch = str(tmp_path / "p.arch")
    assert client.post("/api/project/new", json={"path": arch},
                       headers=AUTH).status_code == 200


def test_append_diff_and_commit_end_to_end(client, tmp_path):
    _open_project(client, tmp_path)
    path = _make_sample_csv(tmp_path)
    rules = ti.interface_export_rules("Path", ops_col="Operations",
                              required_col="Required Interface")
    body = {"path": path, "rules": rules,
            "mapping": {"Port": "Port Name", "Operations": "Function",
                        "Required Interface": "Interface"},
            "mode": "append", "profile_name": "InterfaceExport test"}

    d = client.post("/api/import/diff", json=body, headers=AUTH)
    assert d.status_code == 200, d.text
    diff = d.json()
    assert diff["mode"] == "append"
    names = {m["name"]: m for m in diff["models"]}
    assert set(names) == {"ModelA", "ModelB"}
    assert all(not m["exists"] for m in diff["models"])
    assert diff["total_rows"] > 0

    c = client.post("/api/import/commit", json={"diff_id": diff["diff_id"]},
                    headers=AUTH)
    assert c.status_code == 200, c.text
    commit = c.json()
    assert commit["total_added"] == diff["total_rows"]
    assert all(m["created"] for m in commit["models"])

    # Rows really landed, and a re-diff now reports the models as existing.
    models = client.get("/api/models", headers=AUTH).json()["models"]
    by_name = {m["name"]: m for m in models}
    assert by_name["ModelA"]["row_count"] == names["ModelA"]["rows"]

    d2 = client.post("/api/import/diff", json=body, headers=AUTH).json()
    assert all(m["exists"] for m in d2["models"])

    # A diff id is one-shot.
    again = client.post("/api/import/commit",
                        json={"diff_id": diff["diff_id"]}, headers=AUTH)
    assert again.status_code == 404


def test_update_diff_commit_locks_and_respects_locks(client, tmp_path):
    _open_project(client, tmp_path)
    mid = client.post("/api/models", json={"name": "M1"},
                      headers=AUTH).json()["id"]
    db = client.app.state.appstate.db
    db.save_model_rows(mid, [
        {"Interface": {"text": "IFoo"}, "Function": {"text": "f1"},
         "TC. ID": {"text": ""}},
        {"Interface": {"text": "IFoo"}, "Function": {"text": "f2"},
         "TC. ID": {"text": ""}},
        {"Interface": {"text": "IBar"}, "Function": {"text": "g"},
         "TC. ID": {"text": ""}},
    ])
    db.commit()

    tc = tmp_path / "DNG_tcids.csv"
    tc.write_text("Interface,TC\nIFoo,TC-1\nIBar,TC-2\n", encoding="utf-8")
    body = {"path": str(tc), "rules": [],
            "mapping": {"Interface": "Interface", "TC": "TC. ID"},
            "locked_targets": ["TC. ID"],
            "mode": "update", "key_columns": ["Interface"], "model_id": mid,
            "profile_name": "DNG TC IDs"}

    d = client.post("/api/import/diff", json=body, headers=AUTH).json()
    assert d["updates"] == 3 and d["cells"] == 3       # fill-by-duplicates
    assert d["locked"] == [] and d["conflicts"] == []

    c = client.post("/api/import/commit", json={"diff_id": d["diff_id"]},
                    headers=AUTH).json()
    assert c["cells_updated"] == 3

    rows = db.get_model_rows(mid)
    assert [r["TC. ID"]["text"] for r in rows] == ["TC-1", "TC-1", "TC-2"]
    assert all(r["TC. ID"].get("origin") == "import" for r in rows)

    # Re-import with a DIFFERENT id: locked cells are flagged, never applied.
    tc.write_text("Interface,TC\nIFoo,TC-99\n", encoding="utf-8")
    d2 = client.post("/api/import/diff", json=body, headers=AUTH).json()
    assert d2["updates"] == 0
    assert len(d2["locked"]) == 2
    c2 = client.post("/api/import/commit", json={"diff_id": d2["diff_id"]},
                     headers=AUTH).json()
    assert c2["cells_updated"] == 0
    assert len(c2["locked_skipped"]) == 2
    rows = db.get_model_rows(mid)
    assert [r["TC. ID"]["text"] for r in rows] == ["TC-1", "TC-1", "TC-2"]


def test_locked_cell_edit_refused_until_unlocked(client, tmp_path):
    # #7: the backend enforces the lock — a PATCH on a locked cell is a 409;
    # the explicit unlock clears only the provenance flag (value kept, change
    # logged), after which the cell is hand-editable again.
    _open_project(client, tmp_path)
    mid = client.post("/api/models", json={"name": "M1"},
                      headers=AUTH).json()["id"]
    db = client.app.state.appstate.db
    db.save_model_rows(mid, [
        {"Interface": {"text": "IFoo"},
         "TC. ID": {"text": "TC-1", "origin": "import"}},
    ])
    db.commit()

    r = client.patch(f"/api/models/{mid}/ports/0",
                     json={"updates": {"TC. ID": "TC-X"}}, headers=AUTH)
    assert r.status_code == 409
    assert "locked" in r.json()["detail"].lower()
    assert db.get_model_rows(mid)[0]["TC. ID"]["text"] == "TC-1"

    r = client.post(f"/api/models/{mid}/ports/0/unlock",
                    json={"column": "TC. ID"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["unlocked"] is True
    assert db.get_model_rows(mid)[0]["TC. ID"] == {"text": "TC-1"}

    r = client.patch(f"/api/models/{mid}/ports/0",
                     json={"updates": {"TC. ID": "TC-X"}}, headers=AUTH)
    assert r.status_code == 200
    assert db.get_model_rows(mid)[0]["TC. ID"]["text"] == "TC-X"

    # Unlocking a cell that is not locked is a clean 409.
    r = client.post(f"/api/models/{mid}/ports/0/unlock",
                    json={"column": "TC. ID"}, headers=AUTH)
    assert r.status_code == 409


def test_update_diff_validation_errors(client, tmp_path):
    _open_project(client, tmp_path)
    tc = tmp_path / "x.csv"
    tc.write_text("A,B\n1,2\n", encoding="utf-8")
    base = {"path": str(tc), "rules": [], "mode": "update",
            "mapping": {"A": "Interface", "B": "TC. ID"}}
    # missing model_id
    r = client.post("/api/import/diff",
                    json={**base, "key_columns": ["Interface"]}, headers=AUTH)
    assert r.status_code == 400
    # key not among mapped targets
    r = client.post("/api/import/diff",
                    json={**base, "key_columns": ["Nope"], "model_id": 1},
                    headers=AUTH)
    assert r.status_code == 400
    # key only, no value columns
    r = client.post("/api/import/diff",
                    json={"path": str(tc), "rules": [], "mode": "update",
                          "mapping": {"A": "Interface"},
                          "key_columns": ["Interface"], "model_id": 1},
                    headers=AUTH)
    assert r.status_code == 400


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
