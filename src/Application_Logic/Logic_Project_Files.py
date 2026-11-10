"""
Project files for test builds
=============================
A test build often needs more than hooks in C code: a compiler option in a
makefile, an ARXML value, a linker script. Test Injection therefore shows the
whole project folder, not only the C sources imported into the release, and a
test project keeps its own copy of every file the user changes there.

* The **project folder** is the source folder of the test build (the checkout
  the build runs in). It is listed one folder at a time, straight from disk, so
  nothing but the changed files is stored in the project.
* A **changed file** is a full copy kept in the test project
  (``test_project_files``). A test build writes it over the original, builds,
  and puts the original back. ``base_hash`` remembers which original the copy
  was made from, so a build can say when that original has changed since.

Pure module: paths and bytes in, plain values out.
"""
from __future__ import annotations

import hashlib
import os
from typing import List, Optional

# Version-control folders hold nothing a build is changed through.
HIDDEN_DIRS = {".git", ".svn", ".hg"}
# The editor is for configuration and source text, not generated megabytes.
MAX_OPEN_BYTES = 4 * 1024 * 1024
_SNIFF_BYTES = 8192


class ProjectFileError(Exception):
    """A file or folder that can't be shown or changed, with the reason."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_rel(rel_path: str) -> str:
    """A path inside the project folder, with forward slashes. Anything that
    points outside it is refused."""
    parts = [p for p in (rel_path or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts or (parts and ":" in parts[0]):
        raise ProjectFileError("The path must be inside the project folder.")
    return "/".join(parts)


def resolve(root: str, rel_path: str) -> str:
    """The file's place on disk, checked to be inside ``root`` (links too)."""
    rel = clean_rel(rel_path)
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
    if full != base and not full.startswith(base + os.sep):
        raise ProjectFileError("The path must be inside the project folder.")
    return full


def list_dir(root: str, rel_dir: str = "") -> List[dict]:
    """One folder's entries, folders first: ``{name, rel_path, is_dir, size}``.

    One directory read per call, so opening a big project costs nothing until
    a folder is unfolded."""
    folder = resolve(root, rel_dir)
    rel_dir = clean_rel(rel_dir)
    out = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    if is_dir and e.name in HIDDEN_DIRS:
                        continue
                    size = 0 if is_dir else e.stat().st_size
                except OSError:
                    continue
                out.append({"name": e.name,
                            "rel_path": f"{rel_dir}/{e.name}" if rel_dir else e.name,
                            "is_dir": is_dir, "size": size})
    except OSError as e:
        raise ProjectFileError(f"The folder can't be read: {e.strerror or e}") from e
    out.sort(key=lambda x: (not x["is_dir"], x["name"].casefold()))
    return out


def read_bytes(root: str, rel_path: str) -> Optional[bytes]:
    """The file's bytes, or None when it isn't there."""
    full = resolve(root, rel_path)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "rb") as f:
            return f.read()
    except OSError as e:
        raise ProjectFileError(f"The file can't be read: {e.strerror or e}") from e


def as_text(data: bytes) -> tuple:
    """``(text, editable, reason)``. A file that isn't UTF-8 text is shown as
    well as it can be but can't be changed here: saving it would rewrite it in
    another encoding."""
    if b"\x00" in data[:_SNIFF_BYTES]:
        return "", False, "This is a binary file."
    try:
        return data.decode("utf-8"), True, ""
    except UnicodeDecodeError:
        return (data.decode("utf-8", errors="replace"), False,
                "This file isn't UTF-8 text, so it can't be changed here.")


def open_text(root: str, rel_path: str) -> dict:
    """A file for the editor: ``{content, editable, reason, size, sha256}``."""
    full = resolve(root, rel_path)
    try:
        size = os.path.getsize(full)
    except OSError:
        raise ProjectFileError(f"No such file in the project folder: {rel_path}") from None
    if size > MAX_OPEN_BYTES:
        raise ProjectFileError(
            f"This file is {size / 1e6:.1f} MB, too large to open here "
            f"(the limit is {MAX_OPEN_BYTES // (1024 * 1024)} MB).")
    data = read_bytes(root, rel_path) or b""
    text, editable, reason = as_text(data)
    return {"content": text, "editable": editable, "reason": reason, "size": size,
            "sha256": sha256_bytes(data)}


def original_changed(root: str, rel_path: str, base_hash: Optional[str]) -> Optional[bool]:
    """Whether the file on disk still is the one a changed file was made from.
    None when that can't be told: no hash was kept, or the file isn't on disk."""
    if not base_hash or not root or not os.path.isdir(root):
        return None
    try:
        data = read_bytes(root, rel_path)
    except ProjectFileError:
        return None
    return None if data is None else sha256_bytes(data) != base_hash
