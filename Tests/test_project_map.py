"""Tests for the tree-sitter source index (Logic_Code_Index_TS) and the v3 mind
maps built from the Code Map (Logic_Project_Map)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Code_Index as ck
from Application_Logic import Logic_Code_Index_TS as ts
from Application_Logic import Logic_Project_Map as pm
from Application_Logic import Logic_AI_Context as ctx

pytestmark = pytest.mark.skipif(not ts.available(), reason="tree-sitter is not installed")


@pytest.fixture(autouse=True)
def _tree_sitter_on(monkeypatch):
    # These tests are about tree-sitter; a whole-suite run with the regex
    # override still checks everything else.
    monkeypatch.delenv("ARCH_SOURCE_PARSER", raising=False)

AUTOSAR_C = """\
#include "Std_Types.h"

VAR(uint16, AUTOMATIC) Com_RxCounter;
STATIC VAR(uint8, AUTOMATIC) Com_State;
static void (*const Com_Handlers[2])(void) = { Com_A, Com_B };

STATIC FUNC(void, COM_CODE) Com_Helper(P2CONST(Com_ConfigType, AUTOMATIC, COM_CONST) cfg)
{
    Com_RxCounter += cfg->id;
}

FUNC(Std_ReturnType, COM_CODE) Com_SendSignal(uint16 id,
                                              P2VAR(void, AUTOMATIC, COM_DATA) data)
{
    if (Com_State == 0u) { return 1u; }
    Com_Prepare(id);
    Com_Helper(data);
    return Com_Transmit(id, data);
}

const Com_StateType *Com_GetState(void)
{
    return &Com_State;
}
"""


def _tree(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return str(tmp_path)


# ---------------------------------------------------------------------------
# The source index
# ---------------------------------------------------------------------------

def test_autosar_declarations_are_found_without_their_compiler_header(tmp_path):
    idx = ck.build_index(_tree(tmp_path, {"Com.c": AUTOSAR_C}))
    assert idx.source_parser == "tree-sitter"
    assert {"Com_Helper", "Com_SendSignal", "Com_GetState"} <= set(idx.functions)
    send = idx.functions["Com_SendSignal"]
    assert send.signature == "Std_ReturnType Com_SendSignal(uint16 id, void *data)"
    assert send.params == [("uint16", "id"), ("void *", "data")]
    # Calls keep the order they're written in.
    assert send.calls == ["Com_Prepare", "Com_Helper", "Com_Transmit"]
    assert {"Com_RxCounter", "Com_State", "Com_Handlers"} <= set(idx.globals)
    assert "Com_RxCounter" in idx.functions["Com_Helper"].writes_vars


def test_line_numbers_stay_where_they_are_written(tmp_path):
    idx = ck.build_index(_tree(tmp_path, {"Com.c": AUTOSAR_C}))
    lines = AUTOSAR_C.split("\n")
    send = idx.functions["Com_SendSignal"]
    assert "Com_SendSignal(" in lines[send.line_start - 1]
    assert lines[send.line_end - 1] == "}"


def test_pointer_returning_function_the_regex_scan_misses(tmp_path):
    root = _tree(tmp_path, {"Com.c": AUTOSAR_C})
    assert "Com_GetState" not in ck.build_index_regex(root).functions
    f = ck.build_index(root).functions["Com_GetState"]
    assert f.signature == "const Com_StateType *Com_GetState(void)"


def test_type_macros_expand_on_the_same_lines():
    text = "FUNC(void,\n     COM_CODE) Com_Init(void)\n{\n}\n"
    out = ts.expand_type_macros(text, ts.learn_type_macros({"a.c": text}))
    assert out.count("\n") == text.count("\n")
    assert out.split("\n")[2] == "{"
    assert "FUNC" not in out


def test_the_projects_own_macro_wins_over_the_autosar_one():
    macros = ts.learn_type_macros({"Compiler.h": "#define FUNC(a, b) do_something(a, b)\n"})
    assert "FUNC" not in macros    # not a type macro: left alone
    assert "P2VAR" in macros


def test_same_results_as_the_regex_scan_on_plain_c(tmp_path):
    text = ("static int g_count;\n"
            "struct S { int a; } g_s;\n"
            "static int helper(int v)\n{\n    return v + g_count;\n}\n"
            "void Run(int x)\n{\n    if (x > 2) {\n        g_s.a = helper(x);\n    }\n"
            "    g_count++;\n}\n")
    root = _tree(tmp_path, {"m.c": text})
    a, b = ck.build_index(root), ck.build_index_regex(root)
    for name in ("helper", "Run"):
        fa, fb = a.functions[name], b.functions[name]
        for field in ("signature", "reads_vars", "writes_vars", "returns_vars", "conditions",
                      "line_start", "line_end"):
            assert getattr(fa, field) == getattr(fb, field), (name, field)
        assert set(fa.calls) == set(fb.calls)


def test_regex_scan_when_asked_or_when_tree_sitter_fails(tmp_path, monkeypatch):
    root = _tree(tmp_path, {"Com.c": AUTOSAR_C})
    monkeypatch.setenv("ARCH_SOURCE_PARSER", "regex")
    assert ck.build_index(root).source_parser == "regex"
    monkeypatch.delenv("ARCH_SOURCE_PARSER")

    def broken(_source):
        raise RuntimeError("parser crashed")
    monkeypatch.setattr(ts, "build_index", broken)
    idx = ck.build_index(root)
    assert idx.source_parser == "regex"


def test_cpp_files_use_the_regex_scan(tmp_path, monkeypatch):
    root = _tree(tmp_path, {"Door.cpp": "int DoorOpen(int a)\n{\n    return Lock(a);\n}\n"})
    parsed = []
    real = ts._collect_functions
    monkeypatch.setattr(ts, "_collect_functions", lambda *a, **k: parsed.append(a[4]) or real(*a, **k))
    idx = ck.build_index(root)
    assert idx.functions["DoorOpen"].calls == ["Lock"]
    assert parsed == []    # tree-sitter's C grammar never saw the C++ file


def test_a_file_tree_sitter_cant_parse_keeps_the_regex_scans_functions(tmp_path, monkeypatch):
    text = "void Ok(void)\n{\n}\n\nUNKNOWN_MACRO(x) void Odd(void)\n{\n    Ok();\n}\n"
    root = _tree(tmp_path, {"m.c": text})
    idx = ck.build_index(root)
    assert "Ok" in idx.functions
    regex_names = set(ck.build_index_regex(root).functions)
    assert regex_names <= set(idx.functions)


def test_one_file_function_list_for_test_injection():
    found = {f.name: f for f in ck.file_function_list(AUTOSAR_C, "Com.c")}
    assert {"Com_Helper", "Com_SendSignal", "Com_GetState"} <= set(found)
    assert found["Com_SendSignal"].line_start == 12


# ---------------------------------------------------------------------------
# The maps
# ---------------------------------------------------------------------------

def _fn(file, line, calls=(), reads=(), writes=(), sig=None, params=(), ret="void"):
    return {"file": file, "line_start": line, "calls": list(calls), "reads_vars": list(reads),
            "writes_vars": list(writes), "parameters": [{"name": n, "type": t} for t, n in params],
            "return_type": ret, "signature": sig}


CODE_MAP = {
    "functions": {
        "Main_Cyclic": _fn("main.c", 5, ["Pwt_Step", "Bms_Step"], sig="void Main_Cyclic(void)"),
        "Pwt_Step": _fn("pwt/torque.c", 40, ["Pwt_Compute", "pwt_clamp"],
                        writes=["g_pwt.value"], sig="void Pwt_Step(void)"),
        "Pwt_Compute": _fn("pwt/torque.c", 30, ["Bms_GetSoc"], reads=["g_soc"],
                           sig="uint16_t Pwt_Compute(void)", ret="uint16_t"),
        "Pwt_SetMax": _fn("pwt/torque.c", 20, ["pwt_clamp"], writes=["g_pwt.max"],
                          sig="void Pwt_SetMax(uint16_t v)", params=[("uint16_t", "v")]),
        "pwt_clamp": _fn("pwt/torque.c", 10, sig="uint16_t pwt_clamp(uint16_t v)", ret="uint16_t"),
        "Bms_Step": _fn("bms/soc.c", 30, ["Bms_Measure"], writes=["g_soc"], sig="void Bms_Step(void)"),
        "Bms_Measure": _fn("bms/soc.c", 20, sig="void Bms_Measure(void)"),
        "Bms_GetSoc": _fn("bms/soc.c", 10, reads=["g_soc"], sig="uint16_t Bms_GetSoc(void)",
                          ret="uint16_t"),
    },
    "global_variables": {"g_pwt": "Pwt_State_t", "g_soc": "uint16_t"},
    "structures": {"Pwt_State_t": [{"name": "value", "type": "uint16_t"},
                                   {"name": "max", "type": "uint16_t"}]},
    "source_parser": "tree-sitter",
}
PWT_PORTS = [{"name": "p_pwt_Step", "function": "Pwt_Step"},
             {"name": "p_pwt_SetMax", "function": "Pwt_SetMax"},
             {"name": "p_pwt_Value", "function": "g_pwt", "direction": "provided"}]
BMS_PORTS = [{"name": "p_bms_Step", "function": "Bms_Step"},
             {"name": "p_bms_GetSoc", "function": "Bms_GetSoc", "direction": "required"}]


def test_model_map_covers_its_files_and_who_runs_it():
    mm = pm.build_model_map(CODE_MAP, 1, "Powertrain", PWT_PORTS, "R1")
    assert set(mm["scope"]) == {"Pwt_Step", "Pwt_Compute", "Pwt_SetMax", "pwt_clamp"}
    assert mm["entries"] == ["Main_Cyclic"]
    # A port matched to a global is a variable port, not a function.
    value = next(r for r in mm["ports"] if r["name"] == "p_pwt_Value")
    assert value["function"] == "" and value["variable"] == "g_pwt"
    assert mm["structures"]["Pwt_State_t"][0]["name"] == "value"


def test_model_map_text_has_ports_signatures_calls_and_data():
    mm = pm.build_model_map(CODE_MAP, 1, "Powertrain", PWT_PORTS, "R1")
    text = ctx.mind_map_to_text(mm, budget_chars=6000)
    assert text.startswith("# MODEL MAP — Powertrain, release R1")
    assert "SetMax(u16 v)→void:20" in text
    assert "Value→var g_pwt" in text
    assert "Main_Cyclic  (called by" not in text    # nothing calls it
    assert "  Pwt_Step → Pwt_Compute, pwt_clamp" in text    # source order
    assert "g_pwt: Pwt_State_t {value u16, max u16}" in text
    assert "- void Pwt_Step(void) @pwt/torque.c:40" in text


def test_model_map_drops_the_least_central_functions_first():
    mm = pm.build_model_map(CODE_MAP, 1, "Powertrain", PWT_PORTS, "R1")
    full = ctx.mind_map_to_text(mm, budget_chars=100_000)
    head_len = full.index("## FUNCTIONS")
    small = ctx.mind_map_to_text(mm, budget_chars=head_len + 260)
    assert len(small) <= head_len + 260
    assert "less central functions not shown" in small


def test_project_map_shows_what_runs_each_model_and_what_they_share():
    models = [(1, "Powertrain", PWT_PORTS), (2, "Battery", BMS_PORTS)]
    proj = pm.build_project_map(CODE_MAP, models, "R1")
    assert proj["between_models"] == {"Powertrain → Battery": ["Pwt_Compute→Bms_GetSoc"]}
    assert proj["shared_globals"]["g_soc"] == {"type": "uint16_t", "written_by": ["Battery"],
                                               "read_by": ["Powertrain"]}
    text = ctx.mind_map_to_text(proj, budget_chars=6000)
    assert text.startswith("# PROJECT MAP — release R1")
    assert "Main_Cyclic → Pwt_Step, Bms_Step" in text
    assert "### Powertrain — 3 ports, 4 functions in pwt/ (torque.c)" in text
    assert "Powertrain → Battery: Pwt_Compute→Bms_GetSoc" in text
    assert "g_soc: uint16_t — written by Battery, read by Powertrain" in text


def test_project_map_fits_its_budget():
    models = [(1, "Powertrain", PWT_PORTS), (2, "Battery", BMS_PORTS)]
    text = ctx.mind_map_to_text(pm.build_project_map(CODE_MAP, models), budget_chars=300)
    assert len(text) <= 300 and text.endswith("ask about one model for more)")


def test_older_maps_still_render():
    v2 = {"builder_version": "2.0", "model_name": "M", "files": {}, "ports": {},
          "requirements": {}, "functions": {}}
    assert "MIND MAP v2.0" in ctx.mind_map_to_text(v2)


def test_project_map_lives_in_the_release_code_map(tmp_path):
    from Tests.test_helpers import make_project_db
    db = make_project_db(str(tmp_path / "p.arch"), layout=[("Input Port", "Port Search", True)],
                         models=[], releases=[{"name": "R1"}])
    rid = db.get_all_releases()[0]["id"]
    db.save_project_map({"kind": "project"}, release_id=rid)
    assert db.get_project_map(rid) is None    # no Code Map yet, so nowhere to keep it
    db.save_release_code_map(json.dumps(CODE_MAP), release_id=rid)
    db.save_project_map({"kind": "project", "models": []}, release_id=rid)
    assert db.get_project_map(rid)["kind"] == "project"
    assert db.get_release_code_map(rid)["functions"]    # the Code Map is untouched
    db.save_release_code_map(json.dumps(CODE_MAP), release_id=rid)
    assert db.get_project_map(rid) is None    # a rebuilt Code Map drops the stale map
    db.close()


def test_ports_come_from_the_rows_matched_symbols(tmp_path):
    from backend.handlers import _extract_ports
    from Tests.test_helpers import make_project_db
    layout = [("Input Port", "Port Search", True), ("Input Port (Match)", "Static Text", True),
              ("Mapped Func", "Function Search", True), ("Mapped Func (Match)", "Static Text", True),
              ("Mapped Parameter", "Variable Search", True),
              ("Mapped Parameter (Match)", "Static Text", True),
              ("Direction", "Static Text", True)]
    db = make_project_db(str(tmp_path / "p.arch"), layout=layout, models=[{
        "name": "A", "status": "In Work", "rows": [
            {"Input Port": {"text": "p_speed"},
             "Input Port (Match)": {"text": "", "widget_text": "Spd_Port (90%)"},
             "Mapped Func (Match)": {"text": "", "widget_text": "Spd_Read (100%)"},
             "Mapped Parameter (Match)": {"text": "", "widget_text": "g_speed (80%)"},
             "Direction": {"text": "required"}},
            {"Input Port": {"text": "p_mode"},
             "Input Port (Match)": {"text": "", "widget_text": "Mode_Get (95%)"}},
        ]}], releases=[{"name": "R1"}])
    mid = db.get_all_models()[0]["id"]
    ports = _extract_ports(db, db.load_column_layout(), mid)
    assert ports[0] == {"name": "p_speed", "operation": "", "function": "Spd_Read",
                        "variable": "g_speed", "direction": "required"}
    assert ports[1]["function"] == "Mode_Get"
    db.close()
