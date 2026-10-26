"""
Leading-underscore symbol convention (Renesas CC-RH / NEC V850 for RH850).

Those toolchains prefix every C symbol with "_" in the ELF symbol table while
DWARF keeps the plain C name. Before the fix every function looked
compiler-internal and was dropped (empty Match columns), and the DWARF
parameter join missed, which also tripped the native sanity check into the slow
pyelftools fallback. The fixture reproduces the layout with C asm labels:
symbol `_add`, DWARF name `add`.
"""
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

RENESAS_SRC = """
int global_counter = 0;
int add(int a, int b) __asm__("_add");
int sub(int a, int b) __asm__("_sub");
int compute(int x) __asm__("_compute");
int add(int a, int b) { return a + b; }
int sub(int a, int b) { return a - b; }
int compute(int x) { return add(x, sub(x, global_counter)); }
"""


@pytest.fixture(scope="module")
def renesas_obj(tmp_path_factory):
    if CLANG is None:
        pytest.skip("clang not available")
    d = tmp_path_factory.mktemp("rh850")
    src = d / "rh.c"
    src.write_text(RENESAS_SRC)
    out = d / "renesas_style.out"
    subprocess.run([CLANG, "--target=armv7-none-eabi", "-g", "-O0", "-c",
                    str(src), "-o", str(out)], check=True)
    return str(out)


# ---------------------------------------------------------------------------
# Detection + normalization (pure)
# ---------------------------------------------------------------------------
def test_detection_threshold():
    assert ep.uses_leading_underscore(["_a", "_b", "_c", "_d"]) is True
    # A normal ELF: a few runtime helpers start with '_', most functions don't.
    assert ep.uses_leading_underscore(["main", "_start", "motor_stop", "app_run"]) is False
    assert ep.uses_leading_underscore(["_a", "_b"]) is False      # too few to decide
    assert ep.uses_leading_underscore([]) is False


def test_strip_is_single_underscore():
    assert ep.strip_leading_underscore("_Bms_Init") == "Bms_Init"
    assert ep.strip_leading_underscore("__internal") == "_internal"  # stays internal
    assert ep.strip_leading_underscore("plain") == "plain"
    assert ep.strip_leading_underscore("_") == "_"


def test_normalize_native_output_rewrites_symbols_functions_edges():
    data = {
        "symbols": [{"name": f"_{n}", "symbol_type": "STT_FUNC", "section": ".text"}
                    for n in ("a", "b", "c")]
                   + [{"name": "_ext", "symbol_type": "STT_FUNC", "section": "UNDEF"},
                      {"name": "_g", "symbol_type": "OBJECT", "section": ".data"}],
        "functions": [{"name": "_a"}, {"name": "_b"}, {"name": "_c"}],
        "call_edges": [{"from": "_a", "to": "_ext"}],
    }
    assert ep.normalize_native_output(data) is True
    assert [s["name"] for s in data["symbols"]] == ["a", "b", "c", "ext", "g"]
    assert [f["name"] for f in data["functions"]] == ["a", "b", "c"]
    assert data["call_edges"] == [{"from": "a", "to": "ext"}]
    assert data["symbol_prefix_stripped"] == "_"


def test_normal_elf_output_untouched():
    data = {"symbols": [{"name": n, "symbol_type": "STT_FUNC", "section": ".text"}
                        for n in ("main", "_start", "app_run", "motor_stop")],
            "functions": [{"name": "_start"}]}
    assert ep.normalize_native_output(data) is False
    assert data["functions"] == [{"name": "_start"}]


# ---------------------------------------------------------------------------
# End to end, both backends
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["rust", "python"])
def test_renesas_style_import_keeps_functions_and_params(renesas_obj, backend,
                                                         tmp_path, monkeypatch):
    if backend == "rust" and not ep.RUST_PARSER_AVAILABLE:
        pytest.skip("rust_elf_parser not installed")
    from Application_Logic.Logic_Database import ProjectDatabase
    from Application_Logic.Logic_Symbol_Matcher import SymbolMatcher

    monkeypatch.setenv("ARCH_PARSER_BACKEND", backend)
    db = ProjectDatabase()
    db.open(str(tmp_path / "p.arch"))
    try:
        p = ELFParser()
        p.load_elf(renesas_obj)
        p.extract_all_streaming_to_db(db)
        h = p.md5_hash
        assert p.symbol_prefix_stripped is True

        rows = db.execute("SELECT name, parameters FROM elf_functions WHERE elf_hash=?",
                          (h,)).fetchall()
        params = {r[0]: json.loads(db._dec_text("A", r[1]) or "[]") for r in rows}
        assert set(params) == {"add", "sub", "compute"}
        assert [x["name"] for x in params["add"]] == ["a", "b"]

        # The native result must NOT be mistaken for KI-01 corruption.
        expected = "rust_elf_parser" if backend == "rust" else "pyelftools"
        assert db.get_parser_backend(h) == expected

        m = SymbolMatcher(None, db=db, elf_hash=h)
        assert m.find_top_function_matches("add", 1) == [("add", 100)]
    finally:
        db.close()


def test_normal_elf_is_not_renamed(tmp_path):
    if CLANG is None:
        pytest.skip("clang not available")
    src = os.path.join(os.path.dirname(__file__), "Resources", "sample_elf_source.c")
    out = str(tmp_path / "plain.o")
    subprocess.run([CLANG, "--target=armv7-none-eabi", "-g", "-O0", "-c", src, "-o", out],
                   check=True)
    p = ELFParser()
    p.load_elf(out)
    p.extract_all()
    assert p.symbol_prefix_stripped is False
    assert {f.name for f in p.functions} == {"add", "sub", "compute", "dist"}


# ---------------------------------------------------------------------------
# File-type detection: .out / extensionless images are ELF by content
# ---------------------------------------------------------------------------
def test_analyze_and_detect_recognise_elf_by_content(renesas_obj, tmp_path):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.handlers import _detect_import_kind

    noext = tmp_path / "firmware"
    shutil.copy(renesas_obj, noext)
    assert _detect_import_kind(renesas_obj) == "elf"
    assert _detect_import_kind(str(noext)) == "elf"

    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(token="t")) as c:
        for path in (renesas_obj, str(noext)):
            r = c.post("/api/import/analyze", json={"file_path": path}, headers=auth)
            assert r.status_code == 200 and r.json() == {"format": "elf"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
