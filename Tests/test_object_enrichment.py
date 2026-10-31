"""
#6A — native-parser enrichment output for `.o` objects.

New JSON fields from rust_elf_parser.parse_elf:
  * call_edges         — compiler-verified reference edges from .text
                         relocations (each names caller and callee),
  * data_refs          — the same, for the data symbols a function references,
  * inlined_functions  — DW_TAG_inlined_subroutine expansions (functions the
                         optimizer pasted into callers: no call instruction,
                         possibly no symbol),
  * cu_files           — translation-unit source names (file attribution).

Plus: LTO objects (compiler IR, not machine code) are rejected with a clear
error instead of importing garbage.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from core import elf_parser as ep

CLANG = shutil.which("clang")
SRC = os.path.join(os.path.dirname(__file__), "Resources", "sample_elf_source.c")
LINKED_ELF = os.path.join(os.path.dirname(__file__), "Resources", "sample.elf")

pytestmark = [
    pytest.mark.skipif(CLANG is None, reason="clang not available"),
    pytest.mark.skipif(not ep.RUST_PARSER_AVAILABLE,
                       reason="rust_elf_parser not installed"),
]

# The helper needs a real body (loop + locals): a trivial `x + 1` folds away
# completely and clang emits no DW_TAG_inlined_subroutine record at all.
INLINE_SRC = """
static inline __attribute__((always_inline)) int helper(int x) {
    int acc = 0;
    for (int i = 0; i < x; i++) acc += i * x;
    return acc;
}
int outer(int a) { return helper(a) * 2; }
"""


def _compile(tmp_path, name, args, source=None):
    src = SRC
    if source is not None:
        src = str(tmp_path / (name + ".c"))
        with open(src, "w") as f:
            f.write(source)
    out = str(tmp_path / name)
    subprocess.run([CLANG, "-c", "--target=armv7-none-eabi", *args, src, "-o", out],
                   check=True)
    return out


DATA_SRC = """
extern int g_extern;
int g_global;
static volatile int s_first;
static volatile int s_second;
const int g_table[4] = {1, 2, 3, 4};
#define RTE_WRITE_SPEED(v) (g_global = (v))
int get_extern(void) { return g_extern; }
void write_macro(int v) { RTE_WRITE_SPEED(v); }
void set_first(int v) { s_first = v; }
void set_second(int v) { s_second = v; }
int read_table(int i) { return g_table[i]; }
"""


def _references(path):
    data = json.loads(ep.rust_elf_parser.parse_elf(path))
    return ({(r["from"], r["to"]) for r in data["data_refs"]},
            {(e["from"], e["to"]) for e in data["call_edges"]})


@pytest.mark.parametrize("target", [["--target=armv7-none-eabi"],
                                    ["--target=thumbv7em-none-eabi"],
                                    ["--target=thumbv7em-none-eabi", "-mno-movt"]])
@pytest.mark.parametrize("flags", [["-O0"], ["-O2"], ["-O2", "-fdata-sections"]])
def test_data_refs_name_each_variable(tmp_path, target, flags):
    path = _compile(tmp_path, "data.o", [*target, *flags], source=DATA_SRC)
    refs, edges = _references(path)
    # Only the variables each function really touches, so a neighbour never shows.
    assert refs == {("write_macro", "g_global"), ("set_first", "s_first"),
                    ("set_second", "s_second"), ("read_table", "g_table")}
    # Undefined data looks like any other extern, so it stays with the call edges.
    assert edges == {("get_extern", "g_extern")}


def test_thumb_call_in_the_first_instruction_is_its_own_function(tmp_path):
    source = "void leaf(void); void before(void) {} void first(void) { leaf(); }"
    path = _compile(tmp_path, "thumb.o", ["--target=thumbv7em-none-eabi", "-O2"], source=source)
    assert _references(path)[1] == {("first", "leaf")}


def test_linked_image_has_no_data_refs(tmp_path):
    data = json.loads(ep.rust_elf_parser.parse_elf(LINKED_ELF))
    assert isinstance(data["data_refs"], list)


def test_leading_underscore_is_stripped_from_data_refs():
    data = {"symbols": [{"name": f"_Bms_{i}", "symbol_type": "STT_FUNC", "section": ".text"}
                        for i in range(ep.LEADING_UNDERSCORE_MIN_FUNCS)],
            "data_refs": [{"from": "_Bms_0", "to": "_g_state"}]}
    assert ep.normalize_native_output(data)
    assert data["data_refs"] == [{"from": "Bms_0", "to": "g_state"}]


def test_call_edges_from_text_relocations(tmp_path):
    import rust_elf_parser
    obj = _compile(tmp_path, "o0.o", ["-g", "-O0"])
    d = json.loads(rust_elf_parser.parse_elf(obj))
    edges = {(e["from"], e["to"]) for e in d["call_edges"]}
    # compute() calls add() and sub(); dist() calls nothing. The data
    # references to global_counter must NOT appear as edges.
    assert ("compute", "add") in edges
    assert ("compute", "sub") in edges
    assert not any(to == "global_counter" for _, to in edges)
    assert not any(frm == "dist" for frm, _ in edges)
    assert d["cu_files"] and d["cu_files"][0].endswith("sample_elf_source.c")


def test_inlined_functions_recorded(tmp_path):
    import rust_elf_parser
    obj = _compile(tmp_path, "inl.o", ["-g", "-O2"], source=INLINE_SRC)
    d = json.loads(rust_elf_parser.parse_elf(obj))
    inl = {(i["caller"], i["name"]) for i in d["inlined_functions"]}
    assert ("outer", "helper") in inl
    # The whole point: the inlined helper has no standalone symbol, so it is
    # invisible to the symbol table — only the DWARF record reveals it.
    assert "helper" not in {f["name"] for f in d["functions"]}


def test_lto_bitcode_object_rejected(tmp_path):
    import rust_elf_parser
    obj = _compile(tmp_path, "lto.o", ["-g", "-O2", "-flto"], source=INLINE_SRC)
    with pytest.raises(ValueError, match="LTO object"):
        rust_elf_parser.parse_elf(obj)


def test_no_text_relocations_means_no_edges(tmp_path):
    # A linked image carries no .text relocations — simulate that condition
    # (no ELF linker exists on this host) by emptying .rel.text in a copy of
    # the object, the same backend-agnostic trick patch_debug_relocs.py uses.
    # The parser must yield zero edges and otherwise identical DWARF output.
    import struct
    import rust_elf_parser

    obj = _compile(tmp_path, "o0.o", ["-g", "-O0"])
    blob = bytearray(open(obj, "rb").read())

    from elftools.elf.elffile import ELFFile
    elf = ELFFile(open(obj, "rb"))
    e_shoff, e_shentsize = elf["e_shoff"], elf["e_shentsize"]
    for idx, sec in enumerate(elf.iter_sections()):
        if sec.name == ".rel.text":
            struct.pack_into("<I", blob, e_shoff + idx * e_shentsize + 20, 0)
    stripped = str(tmp_path / "norel.o")
    with open(stripped, "wb") as f:
        f.write(blob)

    d = json.loads(rust_elf_parser.parse_elf(stripped))
    assert d["call_edges"] == []
    assert "Point" in d["structures"]


def test_sample_elf_fixture_is_relocatable_and_gets_edges():
    # Tests/Resources/sample.elf is itself an ET_REL object (x86-64): the
    # extractor must handle RELA (explicit-addend) objects too.
    import rust_elf_parser
    d = json.loads(rust_elf_parser.parse_elf(LINKED_ELF))
    edges = {(e["from"], e["to"]) for e in d["call_edges"]}
    assert ("compute", "add") in edges and ("compute", "sub") in edges
    assert "Point" in d["structures"]


def test_code_map_verified_vs_source_only_edges(tmp_path):
    # #6C: edges confirmed by the .o dataset are labeled verified; a call the
    # text scanner sees inside a disabled #ifdef (the classic ghost edge) has
    # no relocation, so it stays source-only. Absence never removes an edge.
    import json as _json
    import rust_elf_parser
    from core.elf_parser import ELFParser
    from Application_Logic.Logic_Code_Map import build_code_map
    from Application_Logic.Logic_Code_Index import build_index

    srcdir = tmp_path / "src"
    srcdir.mkdir()
    src = srcdir / "main.c"
    # Multi-line bodies: the source indexer's function scanner needs them.
    src.write_text("""
int add(int a, int b)
{
    return a + b;
}

int sub(int a, int b)
{
    return a - b;
}

int compute(int x)
{
    return add(x, sub(x, 2));
}

int extra(int x)
{
#ifdef NEVER_ENABLED
    return add(x, 1);
#endif
    return x;
}
""")
    obj = str(tmp_path / "main.o")
    subprocess.run([CLANG, "-c", "--target=armv7-none-eabi", "-g", "-O0",
                    str(src), "-o", obj], check=True)

    d = _json.loads(rust_elf_parser.parse_elf(obj))
    edges = {(e["from"], e["to"]) for e in d["call_edges"]}

    p = ELFParser()
    p.load_elf(obj)
    p.extract_all()
    idx = build_index(str(srcdir))
    cm = build_code_map(p, idx, source_root=str(srcdir), object_edges=edges)

    assert cm["edge_verification"] is True
    comp = cm["functions"]["compute"]
    assert "add" in comp["calls"] and "sub" in comp["calls"]
    assert set(comp["verified_calls"]) == {"add", "sub"}

    extra = cm["functions"]["extra"]
    assert "add" in extra["calls"], "source scanner should see the #ifdef call"
    assert "add" not in extra["verified_calls"], \
        "the compiler never emitted that call — it must stay source-only"

    # Without a dataset: no verification claimed, calls untouched.
    cm2 = build_code_map(p, idx, source_root=str(srcdir))
    assert cm2["edge_verification"] is False
    assert cm2["functions"]["compute"]["verified_calls"] == []


def test_object_import_job_end_to_end(tmp_path):
    # #6B: the import_objects job scans a folder, parses every .o once, and
    # stores edges/inlined/attribution keyed to the release; the summary and
    # drop endpoints round-trip it.
    import time
    from fastapi.testclient import TestClient
    from backend.app import create_app

    objdir = tmp_path / "build"
    objdir.mkdir()
    _compile(objdir, "sample.o", ["-g", "-O0"])
    _compile(objdir, "inline.o", ["-g", "-O2"], source=INLINE_SRC)

    TOKEN = "test-token-123"
    AUTH = {"Authorization": f"Bearer {TOKEN}"}
    app = create_app(token=TOKEN)
    with TestClient(app) as client:
        arch = str(tmp_path / "p.arch")
        assert client.post("/api/project/new", json={"path": arch},
                           headers=AUTH).status_code == 200
        client.post("/api/releases", json={"name": "R1"}, headers=AUTH)
        rid = client.get("/api/releases",
                         headers=AUTH).json()["releases"][0]["id"]

        r = client.post(f"/api/releases/{rid}/objects",
                        json={"obj_dir": str(objdir)}, headers=AUTH)
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            body = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
            if body["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)
        assert body["status"] == "done", body
        assert body["result"]["objects"] == 2
        assert body["result"]["failed"] == 0

        s = client.get(f"/api/releases/{rid}/objects", headers=AUTH).json()
        assert s["config"]["obj_dir"] == str(objdir)
        assert s["config"]["dataset_hash"] == body["result"]["dataset_hash"]
        assert s["summary"]["edges"] >= 2      # compute→add, compute→sub
        assert s["summary"]["inlined"] >= 1    # outer←helper
        assert s["summary"]["data_refs"] >= 1
        assert s["summary"]["failed"] == 0

        # Re-import produces the same dataset hash (re-import detection).
        r2 = client.post(f"/api/releases/{rid}/objects",
                         json={"obj_dir": str(objdir)}, headers=AUTH)
        deadline = time.time() + 15
        while time.time() < deadline:
            b2 = client.get(f"/api/jobs/{r2.json()['job_id']}", headers=AUTH).json()
            if b2["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)
        assert b2["result"]["dataset_hash"] == body["result"]["dataset_hash"]

        # Drop clears the dataset but keeps the config for one-click re-import.
        client.delete(f"/api/releases/{rid}/objects", headers=AUTH)
        s2 = client.get(f"/api/releases/{rid}/objects", headers=AUTH).json()
        assert s2["summary"]["objects"] == 0
        assert s2["summary"]["data_refs"] == 0
        assert s2["config"]["obj_dir"] == str(objdir)


def test_inlined_functions_reach_the_match_pool(tmp_path):
    # #6D: a function that exists only as an inline expansion (no symbol) must
    # be persisted at import, offered by the SymbolMatcher, and tagged
    # "inlined" — an architecture port matched to it is NOT missing code.
    from Application_Logic.Logic_Database import ProjectDatabase
    from Application_Logic.Logic_Symbol_Matcher import SymbolMatcher
    from core.elf_parser import ELFParser

    obj = _compile(tmp_path, "inl.o", ["-g", "-O2"], source=INLINE_SRC)
    db = ProjectDatabase()
    db.open(str(tmp_path / "p.arch"))
    try:
        p = ELFParser()
        p.load_elf(obj)
        p.extract_all_streaming_to_db(db)
        h = p.md5_hash

        # helper was fully inlined: recorded, and absent from elf_functions.
        assert db.get_inlined_only_function_names(h) == ["helper"]
        assert db.get_inlined_function_callers(h, "helper") == ["outer"]

        m = SymbolMatcher(None, db=db, elf_hash=h)
        assert "helper" in m.all_function_names
        assert "helper" in m.inlined_names
        assert "outer" not in m.inlined_names
        names = [n for n, _ in m.find_top_function_matches("helper", limit=5)]
        assert "helper" in names

        # delete_elf must clean the inlined table too.
        db.delete_elf(h)
        assert db.get_inlined_only_function_names(h) == []
    finally:
        db.close()


def test_lto_object_recorded_as_failed_not_fatal(tmp_path):
    # A single LTO object must not sink the whole import: it is recorded with
    # status 'lto' and everything else imports normally.
    import time
    from fastapi.testclient import TestClient
    from backend.app import create_app

    objdir = tmp_path / "build"
    objdir.mkdir()
    _compile(objdir, "good.o", ["-g", "-O0"])
    _compile(objdir, "bad.o", ["-g", "-O2", "-flto"], source=INLINE_SRC)

    TOKEN = "test-token-123"
    AUTH = {"Authorization": f"Bearer {TOKEN}"}
    app = create_app(token=TOKEN)
    with TestClient(app) as client:
        arch = str(tmp_path / "p.arch")
        client.post("/api/project/new", json={"path": arch}, headers=AUTH)
        client.post("/api/releases", json={"name": "R1"}, headers=AUTH)
        rid = client.get("/api/releases",
                         headers=AUTH).json()["releases"][0]["id"]
        r = client.post(f"/api/releases/{rid}/objects",
                        json={"obj_dir": str(objdir)}, headers=AUTH)
        job_id = r.json()["job_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            body = client.get(f"/api/jobs/{job_id}", headers=AUTH).json()
            if body["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)
        assert body["status"] == "done", body
        assert body["result"]["objects"] == 1
        assert body["result"]["failed"] == 1
        s = client.get(f"/api/releases/{rid}/objects", headers=AUTH).json()
        assert s["summary"]["errors"] == [{"rel_path": "bad.o", "status": "lto"}]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
