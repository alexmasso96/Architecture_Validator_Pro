"""
KI-02 (#5): parser-backend provenance + ARCH_PARSER_BACKEND override.

The backend that produced an ELF import must be recorded (elf_index.parser_backend),
exposed per release by the API, and forcible via the ARCH_PARSER_BACKEND env var
so a debug session can pin the safe pyelftools backend without rebuilding.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath("src"))

from core import elf_parser as ep
from core.elf_parser import ELFParser
from Application_Logic.Logic_Database import ProjectDatabase

ELF = str(Path(__file__).parent / "Resources" / "sample.elf")

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# ARCH_PARSER_BACKEND override
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not ep.RUST_PARSER_AVAILABLE, reason="rust_elf_parser not installed")
def test_default_backend_is_native(monkeypatch):
    monkeypatch.delenv("ARCH_PARSER_BACKEND", raising=False)
    p = ELFParser()
    p.load_elf(ELF)
    assert p.parser_backend == "rust_elf_parser"


def test_override_forces_pyelftools(monkeypatch):
    monkeypatch.setenv("ARCH_PARSER_BACKEND", "python")
    p = ELFParser()
    p.load_elf(ELF)
    assert p.parser_backend == "pyelftools"
    assert p.md5_hash, "python path must still hash the file"
    assert p.elf_file is not None, "python path must load the ELF stream"
    # And extraction genuinely runs on pyelftools.
    p.extract_all()
    assert p.parser_backend == "pyelftools"
    assert "Point" in p.structures


@pytest.mark.skipif(not ep.RUST_PARSER_AVAILABLE, reason="rust_elf_parser not installed")
def test_override_accepts_rust_aliases(monkeypatch):
    for alias in ("rust", "native", "rust_elf_parser"):
        monkeypatch.setenv("ARCH_PARSER_BACKEND", alias)
        p = ELFParser()
        p.load_elf(ELF)
        assert p.parser_backend == "rust_elf_parser", alias


@pytest.mark.skipif(not ep.RUST_PARSER_AVAILABLE, reason="rust_elf_parser not installed")
def test_unknown_override_is_ignored(monkeypatch):
    monkeypatch.setenv("ARCH_PARSER_BACKEND", "cobol")
    p = ELFParser()
    p.load_elf(ELF)
    assert p.parser_backend == "rust_elf_parser"


# ---------------------------------------------------------------------------
# DB provenance
# ---------------------------------------------------------------------------
def test_get_parser_backend_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        db = ProjectDatabase()
        db.open(os.path.join(tmp, "p.arch"))
        try:
            h = "ab" * 16
            db.register_elf(h, "/x/y.elf", "rust_elf_parser")
            assert db.get_parser_backend(h) == "rust_elf_parser"
            db.register_elf(h, "/x/y.elf", "pyelftools")  # re-import swaps it
            assert db.get_parser_backend(h) == "pyelftools"
            assert db.get_parser_backend("cd" * 16) is None
            assert db.get_parser_backend("") is None
        finally:
            db.close()


# ---------------------------------------------------------------------------
# API exposure (per-release payloads)
# ---------------------------------------------------------------------------
def test_release_payloads_carry_parser_backend():
    from fastapi.testclient import TestClient
    from backend.app import create_app

    app = create_app(token=TOKEN)
    with TestClient(app) as client, tempfile.TemporaryDirectory() as d:
        arch = os.path.join(d, "p.arch")
        assert client.post("/api/project/new", json={"path": arch},
                           headers=AUTH).status_code == 200
        client.post("/api/releases", json={"name": "R1"}, headers=AUTH)

        releases = client.get("/api/releases", headers=AUTH).json()["releases"]
        assert releases, "expected at least one release"
        for r in releases:
            assert "parser_backend" in r
            assert r["parser_backend"] is None  # no ELF imported yet

        nodes = client.get("/api/releases/lineage", headers=AUTH).json()["nodes"]
        assert nodes and all("parser_backend" in n for n in nodes)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
