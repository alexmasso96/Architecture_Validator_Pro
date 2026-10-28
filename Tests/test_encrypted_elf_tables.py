"""
Regression tests: ELF-table reads on ENCRYPTED projects (per-block-v1).

The suite otherwise runs plaintext projects (cipher=None → the _enc/_dec
helpers are identity), which is exactly how three raw-SELECT-of-ciphertext
bugs shipped: `json.loads` only ever received `fb1:…` tokens on real
password-protected projects (crash log 2026-07-07, `Failed to query DWARF
from DB: Expecting value: line 1 column 1 (char 0)`).

Every test here attaches a real BlockCipher, so any new raw read of an
encrypted elf_* column (elf_functions.parameters / return_type,
elf_structures.fields, elf_global_vars.var_type — category A) fails loudly
in CI instead of only in the field.
"""
import json
import os
import sys
import logging
import tempfile
from pathlib import Path

logging.disable(logging.CRITICAL)
sys.path.append(os.path.abspath("src"))

from core.elf_parser import ELFParser
from Application_Logic.Logic_Block_Crypto import BlockCipher, is_marked_text
from Application_Logic.Logic_Code_Map import build_code_map
from Application_Logic.Logic_Database import ProjectDatabase

ELF = str(Path(__file__).parent / "Resources" / "sample.elf")
PW = "Sup3r-Secret-Pass!"


def _encrypted_db(tmp: str) -> ProjectDatabase:
    db = ProjectDatabase()
    db.open(os.path.join(tmp, "p.arch"))
    db.set_block_cipher(BlockCipher.from_password(PW, BlockCipher.new_salt()))
    return db


def _streamed_parser(db: ProjectDatabase) -> ELFParser:
    p = ELFParser()
    p.load_elf(ELF)
    p.extract_all_streaming_to_db(db)
    return p


# --------------------------------------------------------------------------
# Sanity: the fixture data really is stored as ciphertext (otherwise these
# tests silently degrade back into the plaintext suite).
# --------------------------------------------------------------------------
def test_elf_tables_are_ciphertext_at_rest():
    with tempfile.TemporaryDirectory() as tmp:
        db = _encrypted_db(tmp)
        p = _streamed_parser(db)
        raw = db.execute(
            "SELECT parameters, return_type FROM elf_functions"
            " WHERE elf_hash=? AND name='add'", (p.md5_hash,)).fetchone()
        assert is_marked_text(raw[0])
        # return_type is empty for this fixture (falsy values skip encryption:
        # "" from the native parser, NULL from pyelftools — CI has no native
        # module); either way it must never be readable plaintext of a value.
        assert not raw[1] or is_marked_text(raw[1])
        raw_s = db.execute(
            "SELECT fields FROM elf_structures WHERE elf_hash=? AND name='Point'",
            (p.md5_hash,)).fetchone()
        assert is_marked_text(raw_s[0])
        db.close()


# --------------------------------------------------------------------------
# The production crash: build_code_map on a DB-backed parser must decrypt
# parameters/return_type/var_type before json.loads.
# --------------------------------------------------------------------------
def test_build_code_map_decrypts_dwarf_tables():
    with tempfile.TemporaryDirectory() as tmp:
        db = _encrypted_db(tmp)
        p = _streamed_parser(db)
        cm = build_code_map(p, None, source_root="")
        add = cm["functions"]["add"]
        assert [pp["name"] for pp in add["parameters"]] == ["a", "b"]
        assert all(pp["type"] == "int" for pp in add["parameters"])
        assert not is_marked_text(add["return_type"])
        assert cm["global_variables"].get("global_counter") == "int"
        field_names = {f["name"] for f in cm["structures"]["Point"]}
        assert {"x", "y"} <= field_names
        db.close()


# --------------------------------------------------------------------------
# _parse_dwarf's typedef forward-declaration path resolves aliases via a raw
# elf_structures SELECT — the helper must decrypt before json.loads.
# --------------------------------------------------------------------------
def test_lookup_structure_fields_decrypts():
    with tempfile.TemporaryDirectory() as tmp:
        db = _encrypted_db(tmp)
        h = "ab" * 16
        fields = [{"name": "x", "type": "int"}, {"name": "y", "type": "int"}]
        db.bulk_insert_structures(h, {"FwdStruct": fields})
        assert ELFParser._lookup_structure_fields(db, h, "FwdStruct") == fields
        assert ELFParser._lookup_structure_fields(db, h, "NoSuch") is None
        db.close()


def test_lookup_structure_fields_tolerates_garbage():
    # A non-JSON plaintext value (legacy/corrupt row without the fb1: marker
    # passes decrypt_text through unchanged) must yield None, not raise.
    with tempfile.TemporaryDirectory() as tmp:
        db = _encrypted_db(tmp)
        h = "cd" * 16
        db.execute(
            "INSERT INTO elf_structures (elf_hash, name, fields) VALUES (?,?,?)",
            (h, "Broken", "not json"))
        assert ELFParser._lookup_structure_fields(db, h, "Broken") is None
        db.close()


# --------------------------------------------------------------------------
# export_elf_cache: must decrypt parameters/return_type, and a failed export
# must not leave a truncated cache file behind.
# --------------------------------------------------------------------------
def test_export_elf_cache_encrypted_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        db = _encrypted_db(tmp)
        p = _streamed_parser(db)

        out = p.export_elf_cache(tmp)
        assert out is not None and os.path.exists(out)

        with open(out, encoding="utf-8") as f:
            data = json.load(f)   # whole file is valid JSON (not truncated)
        funcs = {fn["name"]: fn for fn in data["functions"]}
        assert [pp["name"] for pp in funcs["add"]["parameters"]] == ["a", "b"]
        assert all(pp["type"] == "int" for pp in funcs["add"]["parameters"])
        assert not is_marked_text(funcs["add"]["return_type"] or "")

        # And load_cache() accepts the export, as in the plaintext test.
        reloaded = ELFParser()
        assert reloaded.load_cache(out) is True
        assert reloaded.md5_hash == p.md5_hash
        db.close()


def test_export_elf_cache_failure_leaves_no_partial_file():
    with tempfile.TemporaryDirectory() as tmp:
        db = _encrypted_db(tmp)
        h = "ef" * 16
        db.register_elf(h, "/x/y.elf")
        # An fb1:-marked value that is NOT a valid Fernet token: decrypt_text
        # raises PasswordInvalid mid-write → export must clean up and return None.
        db.execute(
            "INSERT INTO elf_functions (elf_hash, name, address, size, parameters, return_type)"
            " VALUES (?,?,?,?,?,?)", (h, "f", 0, 4, "fb1:not-a-token", "fb1:junk"))

        p = ELFParser()
        p._db = db
        p._active_elf_hash = h
        p.md5_hash = h
        cache_dir = os.path.join(tmp, "cache")
        assert p.export_elf_cache(cache_dir) is None
        assert not os.path.exists(os.path.join(cache_dir, f"elf_{h}.json"))
        db.close()


def test_export_elf_cache_failure_preserves_previous_cache():
    # A failed RE-export must leave a pre-existing good cache untouched. The
    # export writes to a temp file and renames on success; a failure before or
    # during the write (here: the DB is closed under the parser — the BUG-E
    # race) must not delete the previous export.
    with tempfile.TemporaryDirectory() as tmp:
        db = _encrypted_db(tmp)
        p = _streamed_parser(db)
        cache_dir = os.path.join(tmp, "cache")
        out = p.export_elf_cache(cache_dir)
        assert out is not None
        with open(out, encoding="utf-8") as f:
            good = f.read()

        db.close()   # every DB read in the export now raises
        assert p.export_elf_cache(cache_dir) is None
        assert os.path.exists(out), "failed export deleted the previous good cache"
        with open(out, encoding="utf-8") as f:
            assert f.read() == good
        assert not os.path.exists(out + ".tmp")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
