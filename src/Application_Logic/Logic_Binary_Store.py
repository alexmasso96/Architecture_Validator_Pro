"""
Binary store for Test Builds (#15)
==================================
Captured build files live next to the project, not inside it:

    Project.arch
    Project.arch.binaries/<first two hex digits>/<sha256>

Each file is stored once under the SHA-256 of its contents, so a file that's the
same in Main and in a test build takes no extra space. A blob is written under a
temp name and renamed, so a dropped connection can't leave a partial file under a
valid name. On an encrypted project a blob is a series of length-prefixed chunks,
each encrypted separately, so large files never have to fit in memory.

``build_manifests`` in the project database says which blobs make up a build.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import uuid
from typing import Iterable, Iterator, Optional

CHUNK_SIZE = 1024 * 1024
ENCRYPTED_MAGIC = b"ARCHBIN1"
BLOB_CATEGORY = "F"
_LENGTH = struct.Struct(">I")
_TEMP_SUFFIX = ".tmp"


class StoreError(RuntimeError):
    """The store can't be reached, or a blob is missing or damaged."""


def store_path_for(project_path: str) -> str:
    return project_path + ".binaries"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BinaryStore:
    def __init__(self, root: str, cipher=None) -> None:
        self.root = root
        self.cipher = cipher

    def _blob_path(self, sha256: str) -> str:
        return os.path.join(self.root, sha256[:2], sha256)

    def has(self, sha256: str) -> bool:
        return os.path.isfile(self._blob_path(sha256))

    def is_reachable(self) -> bool:
        """Whether blobs can be written here: the folder can be created and used."""
        try:
            os.makedirs(self.root, exist_ok=True)
            probe = os.path.join(self.root, f".probe-{uuid.uuid4().hex}")
            with open(probe, "wb"):
                pass
            os.remove(probe)
            return True
        except OSError:
            return False

    def free_bytes(self) -> Optional[int]:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return None

    def put_file(self, path: str) -> dict:
        """Adds a file, returning its ``sha256`` and ``size``. A file already in
        the store is only hashed, and its contents aren't transferred again."""
        sha256 = sha256_file(path)
        size = os.path.getsize(path)
        if self.has(sha256):
            return {"sha256": sha256, "size": size}
        target = self._blob_path(sha256)
        temp = f"{target}.{uuid.uuid4().hex}{_TEMP_SUFFIX}"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(path, "rb") as src, open(temp, "wb") as dst:
                self._write_blob(src, dst)
            os.replace(temp, target)
        except OSError as e:
            raise StoreError(f"Could not save {os.path.basename(path)} to the binary "
                             f"store: {e}") from e
        finally:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except OSError:
                pass
        return {"sha256": sha256, "size": size}

    def _write_blob(self, src, dst) -> None:
        if self.cipher is None:
            shutil.copyfileobj(src, dst, CHUNK_SIZE)
            return
        dst.write(ENCRYPTED_MAGIC)
        for chunk in iter(lambda: src.read(CHUNK_SIZE), b""):
            token = self.cipher.encrypt(BLOB_CATEGORY, chunk)
            dst.write(_LENGTH.pack(len(token)))
            dst.write(token)

    def _read_chunks(self, f) -> Iterator[bytes]:
        magic = f.read(len(ENCRYPTED_MAGIC))
        if magic != ENCRYPTED_MAGIC:
            # Not encrypted: what was read is the start of the file itself.
            if magic:
                yield magic
            yield from iter(lambda: f.read(CHUNK_SIZE), b"")
            return
        if self.cipher is None:
            raise StoreError("This binary is encrypted. Open the project with its "
                             "master password.")
        while True:
            header = f.read(_LENGTH.size)
            if not header:
                return
            if len(header) != _LENGTH.size:
                raise StoreError("A binary in the store is damaged.")
            length = _LENGTH.unpack(header)[0]
            token = f.read(length)
            if len(token) != length:
                raise StoreError("A binary in the store is damaged.")
            yield self.cipher.decrypt(BLOB_CATEGORY, token)

    def read_to(self, sha256: str, target: str) -> None:
        """Writes the blob to ``target`` and checks the result against its hash."""
        digest = hashlib.sha256()
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            with open(self._blob_path(sha256), "rb") as src, open(target, "wb") as dst:
                for chunk in self._read_chunks(src):
                    digest.update(chunk)
                    dst.write(chunk)
        except OSError as e:
            raise StoreError(f"Could not read {sha256[:12]} from the binary store: {e}") from e
        if digest.hexdigest() != sha256:
            raise StoreError(f"Binary {sha256[:12]} failed its hash check.")

    def iter_blobs(self) -> Iterator[tuple]:
        """Yields ``(sha256, path, size)`` for every complete blob."""
        if not os.path.isdir(self.root):
            return
        for prefix in os.listdir(self.root):
            folder = os.path.join(self.root, prefix)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                # macOS adds "._" files of its own on an SMB share.
                if not name.endswith(_TEMP_SUFFIX) and not name.startswith("."):
                    path = os.path.join(folder, name)
                    yield name, path, os.path.getsize(path)

    def cleanup(self, referenced: Iterable[str]) -> dict:
        """Removes blobs no manifest refers to, and returns what was freed."""
        keep = set(referenced)
        removed, freed = 0, 0
        for sha256, path, size in list(self.iter_blobs()):
            if sha256 not in keep:
                try:
                    os.remove(path)
                except OSError as e:
                    raise StoreError(f"Could not remove {sha256[:12]}: {e}") from e
                removed += 1
                freed += size
        return {"removed": removed, "freed_bytes": freed}
