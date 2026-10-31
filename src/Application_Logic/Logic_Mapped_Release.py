"""
Mapped release switching (#15)
==============================
The mapped folder is what the debugger points at. Switching replaces only the
files that differ between what's there and the target build, in this order:

  1. compare the target's manifest with the folder
  2. list files changed outside the app, and ask before overwriting them
  3. download to a temp folder on the same drive and verify by hash
  4. check that no file is held open
  5. swap the files by rename, with a journal written first
  6. verify, then update ``.arch_mapped.json``

The folder doesn't change until step 5, so a dropped NAS connection before then
leaves it as it was. A journal found later means step 5 was interrupted, and
``recover`` either finishes the switch or goes back.

Switching only reads the store and writes to the user's own drive, so it works
in a view-only session and doesn't need the edit lock.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Callable, Optional

from .Logic_Binary_Store import BinaryStore, StoreError, sha256_file
from .Logic_Test_Builds import ROLE_OBJECTS

STATE_FILE = ".arch_mapped.json"
JOURNAL_FILE = ".arch_switch_journal.json"
TEMP_DIR = ".arch_switch_tmp"
_NEW = "new"
_OLD = "old"

ACTION_REPLACE = "replace"
ACTION_REMOVE = "remove"

RECOVER_FINISH = "finish"
RECOVER_BACK = "back"


class SwitchError(RuntimeError):
    """A switch stopped. ``files`` names what the user has to deal with."""

    def __init__(self, message: str, files: Optional[list] = None) -> None:
        super().__init__(message)
        self.files = files or []


def _local(folder: str, rel_path: str) -> str:
    return os.path.join(folder, rel_path.replace("/", os.sep))


def read_state(folder: str) -> dict:
    try:
        with open(os.path.join(folder, STATE_FILE), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: dict) -> None:
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)


def switch_entries(manifest: list) -> list:
    """The manifest entries that go into a mapped folder. Main's objects are
    kept for late .o imports only."""
    return [e for e in manifest if e["role"] != ROLE_OBJECTS]


def plan_switch(folder: str, manifest: list) -> dict:
    """Works out a switch without changing anything.

    ``copy`` is what has to be transferred and ``remove`` what the old build
    left behind. ``outside_changes`` lists files that don't match what the app
    last placed there, which covers everything on a first switch into a folder
    that already holds a build. Files the app didn't place, and that the target
    doesn't include, aren't mentioned and are left alone.
    """
    placed = read_state(folder).get("files", {})
    target = {e["rel_path"]: e for e in switch_entries(manifest)}
    copy, remove, unchanged, outside = [], [], [], []

    def local_hash(rel_path: str) -> Optional[str]:
        path = _local(folder, rel_path)
        return sha256_file(path) if os.path.isfile(path) else None

    for rel_path, entry in target.items():
        current = local_hash(rel_path)
        if current == entry["sha256"]:
            unchanged.append(rel_path)
            continue
        copy.append(entry)
        if current is not None and current != placed.get(rel_path):
            outside.append(rel_path)
    for rel_path, placed_hash in placed.items():
        if rel_path in target:
            continue
        current = local_hash(rel_path)
        if current is None:
            continue
        remove.append(rel_path)
        if current != placed_hash:
            outside.append(rel_path)
    return {"copy": copy, "remove": remove, "unchanged": unchanged,
            "outside_changes": sorted(outside),
            "transfer_bytes": sum(e["size"] for e in copy)}


def file_is_held(path: str) -> bool:
    """Whether another program has the file open in a way that blocks replacing it."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return True


def _move(source: str, target: str) -> None:
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    os.replace(source, target)


def _remove_empty_parents(folder: str, rel_path: str) -> None:
    parent = os.path.dirname(_local(folder, rel_path))
    while parent and os.path.normpath(parent) != os.path.normpath(folder):
        try:
            os.rmdir(parent)
        except OSError:
            return
        parent = os.path.dirname(parent)


def _clear_temp(folder: str) -> None:
    shutil.rmtree(os.path.join(folder, TEMP_DIR), ignore_errors=True)


def _apply_journal(folder: str, journal: dict) -> None:
    """Moves each file to its new place. A step already done is skipped, so
    this can be repeated after an interruption."""
    temp = os.path.join(folder, TEMP_DIR)
    for op in journal["ops"]:
        rel_path = op["rel_path"]
        current = _local(folder, rel_path)
        old = _local(os.path.join(temp, _OLD), rel_path)
        new = _local(os.path.join(temp, _NEW), rel_path)
        if os.path.exists(current) and not os.path.exists(old) and (
                op["action"] == ACTION_REMOVE or os.path.exists(new)):
            if op["had_file"]:
                _move(current, old)
        if op["action"] == ACTION_REPLACE and os.path.exists(new):
            _move(new, current)
        elif op["action"] == ACTION_REMOVE:
            _remove_empty_parents(folder, rel_path)


def _undo_journal(folder: str, journal: dict) -> None:
    temp = os.path.join(folder, TEMP_DIR)
    for op in journal["ops"]:
        rel_path = op["rel_path"]
        current = _local(folder, rel_path)
        old = _local(os.path.join(temp, _OLD), rel_path)
        if os.path.exists(old):
            _move(old, current)
        elif not op["had_file"] and os.path.exists(current):
            os.remove(current)
            _remove_empty_parents(folder, rel_path)


def _complete(folder: str, journal: dict) -> None:
    for rel_path, sha256 in journal["state"]["files"].items():
        path = _local(folder, rel_path)
        if not os.path.isfile(path) or sha256_file(path) != sha256:
            raise SwitchError("A file failed its check after the switch.", [rel_path])
    _write_json(os.path.join(folder, STATE_FILE), journal["state"])
    os.remove(os.path.join(folder, JOURNAL_FILE))
    _clear_temp(folder)


def interrupted_switch(folder: str) -> Optional[dict]:
    """What an interrupted switch was doing, if one is waiting to be recovered."""
    try:
        with open(os.path.join(folder, JOURNAL_FILE), encoding="utf-8") as f:
            journal = json.load(f)
    except (OSError, ValueError):
        return None
    return {"target": journal["state"].get("build_name", ""),
            "release": journal["state"].get("release_name", ""),
            "previous": journal.get("previous", "")}


def recover(folder: str, action: str) -> dict:
    """Finishes an interrupted switch or goes back to the previous build."""
    journal_path = os.path.join(folder, JOURNAL_FILE)
    with open(journal_path, encoding="utf-8") as f:
        journal = json.load(f)
    if action == RECOVER_FINISH:
        _apply_journal(folder, journal)
        _complete(folder, journal)
        return read_state(folder)
    if action != RECOVER_BACK:
        raise SwitchError(f"Unknown recovery action: {action}")
    _undo_journal(folder, journal)
    os.remove(journal_path)
    _clear_temp(folder)
    return read_state(folder)


def switch(folder: str, store: BinaryStore, manifest: list, identity: dict,
           allow_outside_changes: bool = False,
           progress: Callable[..., None] = lambda *_a, **_k: None,
           held: Callable[[str], bool] = file_is_held) -> dict:
    """Runs a switch. ``identity`` names the target, and goes into the state
    file with the hashes of what's now in the folder."""
    if not os.path.isdir(folder):
        raise SwitchError(f"The mapped folder doesn't exist: {folder}")
    if interrupted_switch(folder) is not None:
        raise SwitchError("An earlier switch was interrupted. Finish it or go back first.")
    _clear_temp(folder)

    plan = plan_switch(folder, manifest)
    if plan["outside_changes"] and not allow_outside_changes:
        raise SwitchError("Some files were changed outside the app.",
                          plan["outside_changes"])

    temp = os.path.join(folder, TEMP_DIR)
    new_dir = os.path.join(temp, _NEW)
    try:
        for number, entry in enumerate(plan["copy"]):
            progress(f"Downloading {entry['rel_path']}",
                     80.0 * number / max(len(plan["copy"]), 1))
            store.read_to(entry["sha256"], _local(new_dir, entry["rel_path"]))
    except (StoreError, OSError) as e:
        _clear_temp(folder)
        raise SwitchError(f"The download stopped, so the mapped folder wasn't changed: {e}") from e

    paths = [e["rel_path"] for e in plan["copy"]] + plan["remove"]
    busy = [p for p in paths if held(_local(folder, p))]
    if busy:
        _clear_temp(folder)
        raise SwitchError("Close the debugger, because it has these files open.", busy)

    previous = read_state(folder)
    journal = {
        "previous": previous.get("build_name", ""),
        "state": {**identity,
                  "files": {e["rel_path"]: e["sha256"] for e in switch_entries(manifest)}},
        "ops": [{"rel_path": e["rel_path"], "action": ACTION_REPLACE,
                 "had_file": os.path.exists(_local(folder, e["rel_path"]))}
                for e in plan["copy"]]
             + [{"rel_path": p, "action": ACTION_REMOVE, "had_file": True}
                for p in plan["remove"]],
    }
    _write_json(os.path.join(folder, JOURNAL_FILE), journal)
    progress("Replacing files", 85.0)
    try:
        _apply_journal(folder, journal)
        _complete(folder, journal)
    except (OSError, SwitchError) as e:
        # A file locked after step 4: nothing is left half done.
        _undo_journal(folder, journal)
        os.remove(os.path.join(folder, JOURNAL_FILE))
        _clear_temp(folder)
        raise SwitchError(f"The switch was undone: {e}",
                          getattr(e, "files", [])) from e
    return {"transferred": len(plan["copy"]), "removed": len(plan["remove"]),
            "unchanged": len(plan["unchanged"]), "transfer_bytes": plan["transfer_bytes"]}
