"""
Parameter ports: direction from the column title, the variable check along the
call chain, what the code map records as returned, and full runs.
"""
import json as _json
import os
import sys
import time as _time
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_CallTree_Report as rep
from Application_Logic import Logic_CallTree_Test as ct
from Application_Logic import Logic_Code_Index as ci
from Application_Logic import Logic_Pipeline as lp
from Application_Logic.Logic_Source_Macros import release_variable_names
from Application_Logic.Logic_Symbol_Matcher import SymbolMatcher
from cli.main import EXIT_ERROR, EXIT_FAILED, EXIT_PASSED, main

VCU = Path("Resources/VCU_TestProject/releases/R5.0").resolve()
TOKEN = "t"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

FUNCTIONS = {
    "take": {"parameters": [{"name": "value", "type": "uint16_t"},
                            {"name": "out", "type": "uint16_t*"},
                            {"name": "in", "type": "const uint16_t*"}],
             "reads_vars": ["g_read"], "writes_vars": ["g_written"],
             "returns_vars": ["g_returned"], "calls": ["helper"]},
    "helper": {"reads_vars": ["g_cfg", "g_cfg.limit"], "writes_vars": ["g_state.phase"],
               "returns_vars": [], "calls": []},
    "macro_user": {"reads_vars": ["SOME_MACRO"], "writes_vars": [], "returns_vars": []},
    "from_elf_only": {"reads_vars": [], "writes_vars": [], "parameters": []},
}
CODE_MAP = {"built_at": "t", "functions": FUNCTIONS}
GLOBALS = {"g_read", "g_written", "g_returned", "g_cfg", "g_state", "g_unused"}


def _evaluate(variable, function="take", direction=ct.REQUIRED):
    return ct.evaluate_variable(variable, function, direction, ct.CallIndex(CODE_MAP),
                                FUNCTIONS, GLOBALS)


# ---------------------------------------------------------------------------
# Direction from the column title
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("title, direction", [
    ("Required Parameter", ct.REQUIRED),
    ("provided parameter", ct.PROVIDED),
    ("PROVIDED_PARAM", ct.PROVIDED),
    ("Param (Required)", ct.REQUIRED),
    ("Required Parameter (Match)", ct.REQUIRED),
    ("Mapped Parameter", None),
    ("Required / Provided", None),
    ("Unprovided", None),
    ("", None),
])
def test_direction_from_title(title, direction):
    assert ct.column_direction(title) == direction


LAYOUT = [("TC. ID", "Static Text", True, 90),
          ("Input Port", "Port Search", True, 100), ("Input Port (Match)", "Static Text", True, 100),
          ("Mapped Func", "Function Search", True, 100),
          ("Mapped Func (Match)", "Static Text", True, 100),
          ("Required Param", "Variable Search", True, 100),
          ("Required Param (Match)", "Static Text", True, 100),
          ("Output", "Variable Search", True, 100), ("Output (Match)", "Static Text", True, 100)]


def test_spec_pairs_the_function_and_direction():
    assert ct.variable_test_spec(LAYOUT, "Required Param (Match)") == \
        {"function_col": "Mapped Func (Match)", "direction": ct.REQUIRED}
    assert ct.variable_test_spec(LAYOUT, "Output (Match)")["direction"] is None
    assert ct.variable_test_spec(LAYOUT, "Mapped Func (Match)") is None


def test_spec_falls_back_to_the_port_column():
    layout = [c for c in LAYOUT if not c[0].startswith("Mapped Func")]
    assert ct.variable_test_spec(layout, "Required Param (Match)")["function_col"] == \
        "Input Port (Match)"


def test_variable_columns_can_be_bound():
    assert "Required Param (Match)" in ct.test_match_columns(LAYOUT)
    bound = ct.reconcile_bindings({}, LAYOUT + [("T", ct.TEST_COLUMN_TYPE, True, 100)], {},
                                  {"T": {"release_id": 1, "match_col": "Required Param (Match)"}},
                                  [1])
    assert bound["T"]["match_col"] == "Required Param (Match)"


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variable, direction, function", [
    ("value", ct.REQUIRED, "take"),             # a parameter
    ("in", ct.REQUIRED, "take"),                # read through a const pointer
    ("g_read", ct.REQUIRED, "take"),            # reads the global
    ("g_cfg.limit", ct.REQUIRED, "take"),       # a struct field, in a helper
    ("g_cfg", ct.REQUIRED, "take"),             # the struct behind that field
    ("limit", ct.REQUIRED, "take"),             # the field's own name
    ("g_returned", ct.PROVIDED, "take"),        # returned
    ("g_written", ct.PROVIDED, "take"),         # written
    ("out", ct.PROVIDED, "take"),               # an output pointer
    ("g_state.phase", ct.PROVIDED, "take"),     # written in a helper
])
def test_passes(variable, direction, function):
    assert _evaluate(variable, function, direction)["verdict"] == ct.PASSED


def test_helper_evidence_names_the_path():
    res = _evaluate("g_cfg.limit")
    assert res["access_path"] == ["take", "helper"] and "take → helper" in res["reason"]


@pytest.mark.parametrize("variable, direction, reason", [
    ("g_written", ct.REQUIRED, "Wrong direction"),
    ("g_read", ct.PROVIDED, "Wrong direction"),
    ("out", ct.REQUIRED, "Wrong direction"),
    ("g_unused", ct.REQUIRED, "does not use"),
    ("g_unused", ct.PROVIDED, "does not use"),
])
def test_fails(variable, direction, reason):
    res = _evaluate(variable, "take", direction)
    assert res["verdict"] == ct.FAILED and reason in res["reason"]


def test_macro_is_not_a_variable():
    res = _evaluate("SOME_MACRO", "macro_user")
    assert res["verdict"] == ct.NOT_MATCHED and "not a variable" in res["reason"]


def test_no_symbol_data_still_checks_the_source():
    res = ct.evaluate_variable("g_read", "take", ct.REQUIRED, ct.CallIndex(CODE_MAP),
                               FUNCTIONS, set())
    assert res["verdict"] == ct.PASSED


def test_unknown_function_is_not_matched():
    assert _evaluate("g_read", "ghost")["verdict"] == ct.NOT_MATCHED


def test_function_without_source_is_no_data():
    res = _evaluate("g_read", "from_elf_only")
    assert res["verdict"] == ct.NO_DATA and "source" in res["reason"]


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
BINDING = {"release_id": 1, "match_col": "Required Param (Match)", "strict": False}
SPEC = {**ct.variable_test_spec(LAYOUT, "Required Param (Match)"),
        "search_col": "Required Param", "min_score": 70, "globals": GLOBALS}


def _row(variable="g_read (100%)", function="take (100%)", source="read"):
    row = {"Mapped Func (Match)": {"widget_text": function}}
    if source is not None:
        row["Required Param"] = {"text": source}
    if variable is not None:
        row["Required Param (Match)"] = {"widget_text": variable}
    return row


def _run(row, spec=SPEC, code_map=CODE_MAP, state=None, **kw):
    rows = [row]
    ct.run_on_rows(rows, "T", BINDING, code_map, None, port_state_col=state, tested_at="t",
                   variable_test=spec, **kw)
    return rows[0]["T"]


def test_row_passes_and_records_evidence():
    cell = _run(_row())
    assert cell["text"] == ct.PASSED
    assert (cell["function"], cell["variable"], cell["direction"]) == \
        ("take", "g_read", ct.REQUIRED)
    assert cell["access"] == "reads" and "main_path" not in cell


def test_port_with_no_parameter_here_is_no_result():
    assert _run(_row(variable=None, source=None))["text"] == ct.NO_RESULT


def test_parameter_without_a_match_is_not_matched():
    cell = _run(_row(variable=None))
    assert cell["text"] == ct.NOT_MATCHED and "'read'" in cell["reason"]


def test_low_score_uses_the_parameter_threshold():
    assert _run(_row(variable="g_read (69%)"))["text"] == ct.NOT_MATCHED
    assert _run(_row(variable="g_read (70%)"))["text"] == ct.PASSED
    # Lower than the function threshold on purpose: generated names match badly.
    assert _run(_row(variable="g_read (75%)"), min_match_score=90)["text"] == ct.PASSED


def test_hand_picked_variable_skips_the_threshold():
    row = _row(variable="g_read (20%)")
    row["Required Param (Match)"]["user_changed"] = True
    assert _run(row)["text"] == ct.PASSED


def test_missing_or_weak_function_is_not_matched():
    assert _run(_row(function=""))["text"] == ct.NOT_MATCHED
    assert _run(_row(function="take (60%)"), min_match_score=90)["text"] == ct.NOT_MATCHED


def test_title_without_a_direction_is_no_data():
    cell = _run(_row(), spec={**SPEC, "direction": None})
    assert cell["text"] == ct.NO_DATA and "required" in cell["reason"]


def test_no_code_map_is_no_data():
    assert _run(_row(), code_map=None)["text"] == ct.NO_DATA


def test_retired_port_is_no_result():
    row = _row()
    row["Port State"] = {"text": "Retired"}
    assert _run(row, state="Port State")["text"] == ct.NO_RESULT


def test_manual_override_survives_a_rerun():
    row = _row(variable="g_written (100%)")
    row["T"] = ct.apply_manual_override(_run(row), ct.PASSED, user="bob", at="t")
    cell = _run(row)
    assert cell["text"] == ct.PASSED and cell["auto"] == ct.FAILED


# ---------------------------------------------------------------------------
# What leaves a function
# ---------------------------------------------------------------------------
SOURCE = """
typedef struct {
    unsigned short limit;
} cfg_t;

cfg_t g_cfg;
unsigned short g_value;
unsigned short g_shadowed;
unsigned short g_other;

unsigned short get_value(void)
{
    return g_value;
}

unsigned short get_field(void)
{
    return g_cfg.limit;
}

unsigned short get_local(void)
{
    unsigned short tmp;
    tmp = g_value;
    return tmp;
}

unsigned short get_param(unsigned short v)
{
    return v;
}

unsigned short get_shadow(void)
{
    unsigned short g_shadowed = 1;
    return g_shadowed;
}

unsigned short get_cast(void)
{
    return (unsigned short)(g_value + 1u);
}

unsigned short get_call(void)
{
    return get_value();
}

void put_out(unsigned short *out)
{
    *out = g_other;
}

void put_const(const unsigned short *in)
{
    g_other = *in;
}

void no_return(void)
{
    g_other = 1u;
}

void bump(void)
{
    g_value++;
    ++g_other;
    g_cfg.limit--;
}

void compound(void)
{
    g_value += 2u;
}
"""


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    root = tmp_path_factory.mktemp("src")
    (root / "demo.c").write_text(SOURCE, encoding="utf-8")
    return ci.build_index(str(root)).functions


@pytest.mark.parametrize("function, expected", [
    ("get_value", ["g_value"]),
    ("get_field", ["g_cfg.limit"]),
    ("get_local", ["g_value"]),
    ("get_param", []),
    ("get_shadow", []),
    ("get_cast", ["g_value"]),
    ("get_call", []),
    ("put_out", ["g_other"]),
    ("put_const", []),
    ("no_return", []),
])
def test_returned_variables(indexed, function, expected):
    returned = [n for n in indexed[function].returns_vars if n.startswith("g_")]
    assert returned == expected


def test_plain_global_assignment_is_a_write(indexed):
    assert indexed["no_return"].writes_vars == ["g_other"]
    assert "g_other" in indexed["put_const"].writes_vars


def test_increment_is_a_write_and_a_read(indexed):
    func = indexed["bump"]
    assert {"g_value", "g_other", "g_cfg.limit"} <= set(func.writes_vars)
    assert {"g_value", "g_other", "g_cfg.limit"} <= set(func.reads_vars)


def test_compound_assignment_is_a_write_and_a_read(indexed):
    assert "g_value" in indexed["compound"].writes_vars
    assert "g_value" in indexed["compound"].reads_vars


def test_a_big_function_keeps_every_access(tmp_path):
    lines = "\n".join(f"    g_{i:03d} = 1u;" for i in range(120))
    (tmp_path / "big.c").write_text(f"void big(void)\n{{\n{lines}\n}}\n", encoding="utf-8")
    assert len(ci.build_index(str(tmp_path)).functions["big"].writes_vars) == 120


def test_local_shadowing_a_global_is_not_its_read(indexed):
    assert "g_shadowed" not in indexed["get_shadow"].reads_vars


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
class _Db:
    def get_release_code_map(self, _rid):
        return CODE_MAP

    def get_function_names(self, _hash):
        return ["take"]

    def get_variable_names(self, _hash):
        return ["g_read"]

    def get_inlined_only_function_names(self, _hash):
        return []


def test_parameters_and_fields_are_variable_candidates_only():
    names = release_variable_names(_Db(), 1)
    assert {"value", "g_written", "g_cfg.limit"} <= set(names)
    assert "SOME_MACRO" not in names
    matcher = SymbolMatcher(None, db=_Db(), elf_hash=["h"], macros={"variables": names})
    assert "g_cfg.limit" in matcher.all_variable_names
    assert "g_cfg.limit" not in matcher.search_pool


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "prefs.json"))
    from fastapi.testclient import TestClient
    from backend.app import create_app
    with TestClient(create_app(token=TOKEN)) as c:
        assert c.post("/api/project/new", json={"path": str(tmp_path / "p.arch")},
                      headers=AUTH).status_code == 200
        yield c


def _wait(client, job_id):
    end = _time.time() + 10
    while _time.time() < end:
        body = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in ("done", "failed", "cancelled"):
            return body
        _time.sleep(0.03)
    raise AssertionError("job did not finish")


def test_api_runs_and_reports_a_variable_column(client):
    rid = client.post("/api/releases", json={"name": "R1"}, headers=AUTH).json()["id"]
    client.post(f"/api/releases/{rid}/activate", headers=AUTH)
    mid = client.post("/api/models", json={"name": "M1"}, headers=AUTH).json()["id"]
    cols = client.get("/api/columns", headers=AUTH).json()["columns"]
    at = next(i for i, c in enumerate(cols) if c["name"] == "Mapped Parameter")
    cols[at:at] = [{"name": "Required Param", "type": "Variable Search", "visible": True, "width": 100},
                   {"name": "Required Param (Match)", "type": "Static Text", "visible": True, "width": 100}]
    cols.append({"name": "T1", "type": ct.TEST_COLUMN_TYPE, "visible": True, "width": 100,
                 "test_binding": {"release_id": rid, "match_col": "Required Param (Match)"}})
    assert client.put("/api/columns", json={"columns": cols, "renames": {}},
                      headers=AUTH).status_code == 200
    db = client.app.state.appstate.db
    db.save_model_rows(mid, [
        {"Mapped Func (Match)": {"widget_text": "take (100%)"},
         "Required Param": {"text": "read"},
         "Required Param (Match)": {"widget_text": "g_read (100%)"}},
        {"Mapped Func (Match)": {"widget_text": "take (100%)"},
         "Required Param": {"text": "written"},
         "Required Param (Match)": {"widget_text": "g_written (100%)"}},
    ])
    db.save_model_code_map(mid, _json.dumps(CODE_MAP), release_id=rid)
    db.commit()

    # No main function is needed: the Run button must not ask for one.
    r = client.get(f"/api/models/{mid}/main-functions",
                   params={"col_name": "T1"}, headers=AUTH).json()
    assert r["needs_input"] is False

    job = client.post("/api/jobs/run_calltree_tests",
                      json={"col_name": "T1", "model_ids": [mid]}, headers=AUTH).json()
    body = _wait(client, job["job_id"])
    assert body["status"] == "done", body
    assert body["result"]["models"][0]["counts"] == {"Passed": 1, "Failed": 1}

    html = client.post("/api/calltree-tests/report", json={"col_name": "T1"},
                       headers=AUTH).json()["html"]
    assert "g_read" in html and "Wrong direction" in html
    assert "Matched variable" in html and "Not reached from" not in html


# ---------------------------------------------------------------------------
# Full runs over the VCU corpus
# ---------------------------------------------------------------------------
COLUMNS = ["Port Name", "Operations", "Required Parameter", "Provided Parameter"]
INIT = "Bcm_Lighting_Init"
STEP = "Bcm_Lighting_Step"


def _cli(tmp_path, capsys, monkeypatch, rows, *columns, test=()):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "prefs.json"))
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Body"
    sheet.append(COLUMNS)
    for row in rows:
        sheet.append(row)
    mains = wb.create_sheet("Main Functions")
    mains.append(["Model", "Main Function"])
    mains.append(["Body", "Vcu_InitAll"])
    mains.append(["Body", "Vcu_Cyclic10ms"])
    wb.save(tmp_path / "wb.xlsx")
    path = tmp_path / "run.cfg"
    path.write_text("\n".join([
        "[project]", "name = demo", "release = R5",
        "[inputs]", "workbook = wb.xlsx", f"symbols = {VCU / 'vcu_R5.0.elf'}",
        f"source = {VCU / 'src'}",
        "[columns]", "function_column = Operations", "port_column = Port Name",
        *columns,
        "[test]", *test,
        "[output]", "folder = out",
    ]) + "\n", encoding="utf-8")
    code = main(["run", "--config", str(path)])
    return code, capsys.readouterr().out


PARAMETERS = ("parameter_columns = Required Parameter", "    Provided Parameter")


def test_cli_passes_correct_parameter_ports(tmp_path, capsys, monkeypatch):
    code, out = _cli(tmp_path, capsys, monkeypatch, [
        ["p1", INIT, "cfg", None],
        ["p2", STEP, None, "g_bcm_lighting_state.uptime_ticks"],
    ], *PARAMETERS)
    assert code == EXIT_PASSED, out
    assert (tmp_path / "out" / "demo-required-parameter.html").exists()
    assert (tmp_path / "out" / "demo-provided-parameter.html").exists()


def test_cli_fails_a_wrong_direction(tmp_path, capsys, monkeypatch):
    code, out = _cli(tmp_path, capsys, monkeypatch, [
        ["p1", STEP, "g_bcm_lighting_state.uptime_ticks.nothing", None],
        ["p2", STEP, None, "g_bcm_lighting_cfg.max_current_ma"],
    ], *PARAMETERS, test=("max_unmatched = -1",))
    assert code == EXIT_FAILED, out
    assert "Failed" in out
    html = (tmp_path / "out" / "demo-provided-parameter.html").read_text(encoding="utf-8")
    assert "Wrong direction" in html


def test_cli_unmatched_parameter_counts_towards_the_limit(tmp_path, capsys, monkeypatch):
    rows = [["p1", INIT, "Zzqq Nothing Like This", None]]
    code, _out = _cli(tmp_path, capsys, monkeypatch, rows, *PARAMETERS)
    assert code == EXIT_FAILED
    code, _out = _cli(tmp_path, capsys, monkeypatch, rows, *PARAMETERS,
                      test=("max_unmatched = 1",))
    assert code == EXIT_PASSED


def test_cli_parameter_threshold_is_separate(tmp_path, capsys, monkeypatch):
    rows = [["p1", INIT, "cfgg", None]]
    code, out = _cli(tmp_path, capsys, monkeypatch, rows, *PARAMETERS,
                     test=("min_parameter_score = 100", "max_unmatched = 0"))
    assert code == EXIT_FAILED and "Not Matched" in out


def test_parameter_title_needs_a_direction(tmp_path, capsys, monkeypatch):
    code, out = _cli(tmp_path, capsys, monkeypatch, [["p1", INIT, "cfg", None]],
                     "parameter_columns = Port Name")
    assert code == EXIT_ERROR and "parameter_columns" in out


def test_layout_puts_parameters_after_the_function():
    names = [c[0] for c in lp.build_layout(["Required Parameter", "Direction"],
                                           ["Required Parameter"])]
    assert names.index("Mapped Func (Match)") < names.index("Required Parameter") \
        < names.index("Required Parameter (Match)") < names.index("Direction")
