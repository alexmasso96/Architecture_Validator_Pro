"""Logic-layer tests for the Test Builds binary store (#15)."""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic.Logic_Binary_Store import (
    BinaryStore, CHUNK_SIZE, ENCRYPTED_MAGIC, StoreError, store_path_for,
)
from Application_Logic.Logic_Block_Crypto import BlockCipher


def _cipher():
    return BlockCipher(b"k" * 32)


def _file(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


@pytest.fixture()
def store(tmp_path):
    return BinaryStore(str(tmp_path / "Project.arch.binaries"))


def test_store_path_sits_next_to_the_project():
    assert store_path_for("/nas/Project.arch") == "/nas/Project.arch.binaries"


def test_put_and_read_round_trip(store, tmp_path):
    data = b"\x00firmware\xff" * 1000
    saved = store.put_file(_file(tmp_path, "a.hex", data))
    assert saved == {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    target = str(tmp_path / "out" / "deep" / "a.hex")
    store.read_to(saved["sha256"], target)
    assert open(target, "rb").read() == data


def test_identical_files_are_stored_once(store, tmp_path):
    first = store.put_file(_file(tmp_path, "main.hex", b"same"))
    second = store.put_file(_file(tmp_path, "watchdog.hex", b"same"))
    assert first["sha256"] == second["sha256"]
    assert len(list(store.iter_blobs())) == 1


def test_failed_write_leaves_no_blob_under_a_valid_name(store, tmp_path, monkeypatch):
    path = _file(tmp_path, "a.hex", b"data")

    def dropped(src, dst, *_):
        dst.write(b"da")
        raise OSError("network dropped")
    monkeypatch.setattr("shutil.copyfileobj", dropped)
    with pytest.raises(StoreError):
        store.put_file(path)
    assert list(store.iter_blobs()) == []
    assert not store.has(hashlib.sha256(b"data").hexdigest())
    leftovers = [f for _, _, files in os.walk(store.root) for f in files]
    assert leftovers == []


def test_damaged_blob_fails_its_hash_check(store, tmp_path):
    saved = store.put_file(_file(tmp_path, "a.hex", b"good data"))
    with open(store._blob_path(saved["sha256"]), "wb") as f:
        f.write(b"bad data!")
    with pytest.raises(StoreError, match="hash check"):
        store.read_to(saved["sha256"], str(tmp_path / "out.hex"))


def test_missing_blob_is_a_store_error(store, tmp_path):
    with pytest.raises(StoreError):
        store.read_to("ab" * 32, str(tmp_path / "out.hex"))


def test_encrypted_blob_round_trip_across_chunks(tmp_path):
    store = BinaryStore(str(tmp_path / "s"), cipher=_cipher())
    data = os.urandom(CHUNK_SIZE * 2 + 17)
    saved = store.put_file(_file(tmp_path, "big.bin", data))
    raw = open(store._blob_path(saved["sha256"]), "rb").read()
    assert raw.startswith(ENCRYPTED_MAGIC)
    assert data[:64] not in raw
    target = str(tmp_path / "big.out")
    store.read_to(saved["sha256"], target)
    assert open(target, "rb").read() == data


def test_encrypted_blob_needs_the_cipher(tmp_path):
    saved = BinaryStore(str(tmp_path / "s"), cipher=_cipher()).put_file(
        _file(tmp_path, "a.bin", b"secret"))
    with pytest.raises(StoreError, match="master password"):
        BinaryStore(str(tmp_path / "s")).read_to(saved["sha256"], str(tmp_path / "o"))


def test_truncated_encrypted_blob_is_reported(tmp_path):
    store = BinaryStore(str(tmp_path / "s"), cipher=_cipher())
    saved = store.put_file(_file(tmp_path, "a.bin", b"secret" * 100))
    path = store._blob_path(saved["sha256"])
    raw = open(path, "rb").read()
    with open(path, "wb") as f:
        f.write(raw[:-5])
    with pytest.raises(StoreError, match="damaged"):
        store.read_to(saved["sha256"], str(tmp_path / "o"))


def test_empty_file(store, tmp_path):
    saved = store.put_file(_file(tmp_path, "empty.bin", b""))
    target = str(tmp_path / "o")
    store.read_to(saved["sha256"], target)
    assert open(target, "rb").read() == b""


def test_cleanup_removes_only_unreferenced_blobs(store, tmp_path):
    keep = store.put_file(_file(tmp_path, "keep.bin", b"keep"))
    drop = store.put_file(_file(tmp_path, "drop.bin", b"drop-me"))
    assert store.cleanup({keep["sha256"]}) == {"removed": 1, "freed_bytes": 7}
    assert store.has(keep["sha256"])
    assert not store.has(drop["sha256"])


def test_macos_metadata_files_on_a_share_arent_blobs(store, tmp_path):
    saved = store.put_file(_file(tmp_path, "a.bin", b"data"))
    metadata = os.path.join(store.root, saved["sha256"][:2], "._" + saved["sha256"])
    with open(metadata, "wb") as f:
        f.write(b"AppleDouble")
    assert [b[0] for b in store.iter_blobs()] == [saved["sha256"]]
    assert store.cleanup({saved["sha256"]})["removed"] == 0


def test_is_reachable_is_false_when_the_folder_cant_be_made(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert BinaryStore(str(blocker / "store")).is_reachable() is False
