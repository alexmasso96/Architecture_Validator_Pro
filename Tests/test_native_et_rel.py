"""
KI-01 regression: native parser vs relocatable (ET_REL) objects.

ELF REL relocations store their addend *in place* in the section bytes. The
native Rust parser used to apply `symbol_value + 0` on .debug_* sections of
ET_REL objects, zeroing every DWARF string/type offset: all names collapsed
to the DWARF producer string, struct fields vanished, and the symtab→DWARF
name join missed so every function lost its parameters.

These tests compile a synthetic ET_REL fixture from repo-owned source (no
company files) and pin:
  1. the relocation fix itself (correct structs/params/globals natively),
  2. backend agreement (native output == pyelftools output),
  3. the output sanity check that discards a corrupt native result and falls
     back to pyelftools instead of silently trusting garbage.
"""
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from core import elf_parser as ep
from core.elf_parser import ELFParser

CLANG = shutil.which("clang")
SRC = os.path.join(os.path.dirname(__file__), "Resources", "sample_elf_source.c")

needs_clang = pytest.mark.skipif(
    CLANG is None, reason="clang not available to build the ET_REL fixture")
needs_native = pytest.mark.skipif(
    not ep.RUST_PARSER_AVAILABLE, reason="rust_elf_parser not installed")

EXPECTED_PARAMS = {
    "add": [("int", "a"), ("int", "b")],
    "sub": [("int", "a"), ("int", "b")],
    "compute": [("int", "x")],
    "dist": [("const Point_t*", "p")],
}
EXPECTED_POINT_FIELDS = [("x", "int"), ("y", "int")]
EXPECTED_GLOBALS = {"global_counter": "int", "global_point": "Point_t"}


@pytest.fixture(scope="module")
def et_rel_obj(tmp_path_factory):
    out = tmp_path_factory.mktemp("et_rel") / "sample_et_rel.o"
    subprocess.run(
        [CLANG, "-c", "--target=armv7-none-eabi", "-g", "-O0", SRC, "-o", str(out)],
        check=True)
    return str(out)


def _pyelftools_extract(obj_path: str) -> ELFParser:
    p = ELFParser()
    p.load_elf(obj_path)
    # Force the safe backend the way _fallback_to_pyelftools does: the native
    # fast-path in load_elf skips loading the stream.
    p.parser_backend = "pyelftools"
    with open(p.elf_path, "rb") as f:
        p.stream = io.BytesIO(f.read())
    p._load_elf_file()
    p.extract_all()
    return p


# ---------------------------------------------------------------------------
# 1. The relocation fix: REL in-place addends must be applied (S + A), so an
#    ET_REL object parses natively with correct names/fields/params.
# ---------------------------------------------------------------------------
@needs_clang
@needs_native
def test_native_parses_et_rel_correctly(et_rel_obj):
    import rust_elf_parser
    d = json.loads(rust_elf_parser.parse_elf(et_rel_obj))

    producer = d.get("producer", "")
    assert producer, "producer missing from native output"

    # The KI-01 corruption collapsed every struct name to the producer string.
    structs = d["structures"]
    assert "Point" in structs and "Point_t" in structs
    assert all(name != producer for name in structs)
    for name in ("Point", "Point_t"):
        assert [(f["name"], f["type"]) for f in structs[name]] == EXPECTED_POINT_FIELDS

    params = {f["name"]: [(p["type"], p["name"]) for p in f["parameters"]]
              for f in d["functions"]}
    for fn, expected in EXPECTED_PARAMS.items():
        assert params.get(fn) == expected, f"{fn}: {params.get(fn)} != {expected}"

    assert d["global_vars"] == EXPECTED_GLOBALS


# ---------------------------------------------------------------------------
# 2. Backend agreement: the native and pyelftools backends must produce the
#    same structures, parameters, and globals for the ET_REL fixture.
# ---------------------------------------------------------------------------
@needs_clang
@needs_native
def test_native_and_pyelftools_agree_on_et_rel(et_rel_obj):
    native = ELFParser()
    native.load_elf(et_rel_obj)
    assert native.parser_backend == "rust_elf_parser"
    native.extract_all()
    assert native.parser_backend == "rust_elf_parser", \
        "native extraction unexpectedly fell back (sanity check tripped?)"

    py = _pyelftools_extract(et_rel_obj)

    n_structs = {k: [(f["name"], f["type"]) for f in v]
                 for k, v in native.structures.items()}
    p_structs = {k: [(f["name"], f["type"]) for f in v]
                 for k, v in py.structures.items()}
    assert n_structs == p_structs

    n_params = {f.name: [(p["type"], p["name"]) for p in f.parameters]
                for f in native.functions}
    p_params = {f.name: [(p["type"], p["name"]) for p in f.parameters]
                for f in py.functions}
    assert n_params == p_params

    assert native.global_vars_dwarf == py.global_vars_dwarf


# ---------------------------------------------------------------------------
# 3. The sanity check: corrupt native output must be detected and discarded.
# ---------------------------------------------------------------------------
PROD = "Apple clang version 21.0.0 (clang-2100.1.1.101)"


def _corrupt_payload():
    # The exact observable shape of the KI-01 corruption.
    return {
        "elf_path": "/x/y.o", "elf_hash": "0" * 32, "producer": PROD,
        "symbols": [{"name": "add", "address": 0, "size": 4,
                     "symbol_type": "STT_FUNC", "binding": "STB_GLOBAL",
                     "section": ".text"}],
        "functions": [{"name": "add", "address": 0, "size": 4,
                       "parameters": [], "return_type": ""}],
        "structures": {PROD: []},
        "global_vars": {PROD: PROD},
    }


def test_sanity_check_detects_ki01_signature():
    corrupt = _corrupt_payload()
    assert ELFParser._native_result_is_sane(corrupt) is False

    # Struct names collapsed to the producer alone is enough.
    only_structs = dict(_corrupt_payload(), functions=[], symbols=[])
    assert ELFParser._native_result_is_sane(only_structs) is False

    # Params dropped while symbols exist is enough (even with sane structs).
    no_params = dict(_corrupt_payload(),
                     structures={"Point": [{"name": "x", "type": "int"}]})
    assert ELFParser._native_result_is_sane(no_params) is False


def test_sanity_check_detects_pre_fix_module_output():
    # A stale pre-fix native module (e.g. bundled in an old frozen build)
    # emits no "producer" key at all — the corrupt shape must still be
    # rejected via the structural checks.
    legacy = _corrupt_payload()
    legacy.pop("producer")
    assert ELFParser._native_result_is_sane(legacy) is False

    # Structural symptom (a) alone: every struct name collapsed to a compiler
    # banner (contains spaces — never a valid C identifier).
    legacy_structs_only = dict(legacy, functions=[], symbols=[])
    assert ELFParser._native_result_is_sane(legacy_structs_only) is False

    # Legacy module with sane output must still be accepted.
    legacy_good = {
        "symbols": [{"name": "add"}],
        "functions": [{"name": "add",
                       "parameters": [{"name": "a", "type": "int"}]}],
        "structures": {"Point": [{"name": "x", "type": "int"}]},
        "global_vars": {},
    }
    assert ELFParser._native_result_is_sane(legacy_good) is True


def test_sanity_check_accepts_good_output():
    good = {
        "producer": PROD,
        "symbols": [{"name": "add"}],
        "functions": [{"name": "add",
                       "parameters": [{"name": "a", "type": "int"}]}],
        "structures": {"Point": [{"name": "x", "type": "int"}]},
        "global_vars": {"global_counter": "int"},
    }
    assert ELFParser._native_result_is_sane(good) is True

    # No DWARF (producer empty): nothing the checks can say — accept.
    stripped = {"producer": "", "symbols": [{"name": "reset"}],
                "functions": [{"name": "reset", "parameters": []}],
                "structures": {}, "global_vars": {}}
    assert ELFParser._native_result_is_sane(stripped) is True


@needs_clang
@needs_native
def test_corrupt_native_output_falls_back_to_pyelftools(et_rel_obj, monkeypatch):
    # Simulate a still-broken native parser: extract_all must discard the
    # corrupt result, fall back to pyelftools, and produce CORRECT data.
    monkeypatch.setattr(ep.rust_elf_parser, "parse_elf",
                        lambda path: json.dumps(_corrupt_payload()))
    p = ELFParser()
    p.load_elf(et_rel_obj)
    assert p.parser_backend == "rust_elf_parser"
    p.extract_all()

    assert p.parser_backend == "pyelftools"
    structs = {k: [(f["name"], f["type"]) for f in v] for k, v in p.structures.items()}
    assert structs.get("Point") == EXPECTED_POINT_FIELDS
    params = {f.name: [(pp["type"], pp["name"]) for pp in f.parameters]
              for f in p.functions}
    assert params.get("add") == EXPECTED_PARAMS["add"]


@needs_clang
@needs_native
def test_corrupt_native_output_never_reaches_the_db(et_rel_obj, monkeypatch, tmp_path):
    # The app's production import path is extract_all_streaming_to_db, which
    # bulk-inserts native output straight into the project DB — the sanity
    # gate must cover it too, not just the in-memory extract_all path.
    from Application_Logic.Logic_Database import ProjectDatabase

    monkeypatch.setattr(ep.rust_elf_parser, "parse_elf",
                        lambda path: json.dumps(_corrupt_payload()))
    db = ProjectDatabase()
    db.open(str(tmp_path / "p.arch"))
    try:
        p = ELFParser()
        p.load_elf(et_rel_obj)
        assert p.parser_backend == "rust_elf_parser"
        p.extract_all_streaming_to_db(db)
        h = p.md5_hash

        row = db.execute(
            "SELECT parser_backend FROM elf_index WHERE elf_hash=?", (h,)
        ).fetchone()
        assert row is not None and row[0] == "pyelftools"

        structs = db.get_all_structures(h)
        assert PROD not in structs, "corrupt struct name reached the DB"
        assert [(f["name"], f["type"]) for f in structs["Point"]] == \
            EXPECTED_POINT_FIELDS
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
