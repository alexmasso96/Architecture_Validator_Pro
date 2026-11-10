"""
Source-Level Test Code Injection router.

Owns the test-project lifecycle (create/list/delete), helper-file import, the
injection hooks, fuzzy anchor resolution/shifting, source export, and the build
runner. Reads/writes go through ``AppState`` (the open .arch); the production
source it injects into comes from the active release's DB-stored source (#2E).

It also shows the whole project folder (the test build's source folder on
disk), so any file can be changed for a test build: a makefile, an ARXML, a
linker script. A changed file is a full copy kept in the test project
(``Logic_Project_Files``).

The build runner spawns a compiler in a subprocess on a worker thread and streams
its output line-by-line over the shared ``build`` SSE event so the frontend
console can scroll logs live. It runs independently of export.
"""
from __future__ import annotations

import os
import subprocess
import threading
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_bus, get_state
from ..events import EventBus
from ..security import require_token
from ..state import AppState, ProjectError

from Application_Logic import Logic_Code_Injection as injection
from Application_Logic import Logic_Project_Files as project_files
from Application_Logic import Logic_Test_Builds as test_builds
from Application_Logic.Logic_File_Locking import FileLockManager
from Application_Logic.Logic_Source_Store import (
    FilesystemSourceProvider, SourceFile, SOURCE_EXTENSIONS,
    release_source_provider,
)

router = APIRouter(prefix="/api/injection", tags=["injection"],
                   dependencies=[Depends(require_token)])

# Terminal kinds we know how to launch (read from project settings).
TERMINALS = {"cmd", "powershell", "bash", "wsl"}

# Active build subprocesses, keyed by build_id (so they can be cancelled).
_builds: dict[str, subprocess.Popen] = {}
_builds_lock = threading.Lock()


def _guard(fn):
    try:
        return fn()
    except (ProjectError, project_files.ProjectFileError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


# ----------------------------------------------------------------------
# Production-source helpers (active release's DB-stored source)
# ----------------------------------------------------------------------
def _production_provider(state: AppState, release_id: Optional[int] = None):
    """A source provider for a release's production source, or raise. Without
    ``release_id`` it's the active release."""
    db = state.require_open()
    rid = release_id
    if rid is None:
        active = state.require_releases().get_active_release()
        rid = active.id if active else None
    provider = release_source_provider(db, rid)
    if provider is None:
        raise ProjectError(
            "No source imported for the active release. Import release source "
            "before injecting test code.")
    return provider


def _read_production_lines(state: AppState, rel_path: str) -> list[str]:
    text = _production_provider(state).read_file(rel_path)
    if text is None:
        raise ProjectError(f"No such source file in the active release: {rel_path}")
    return injection.source_lines(text)


class _SingleFileProvider:
    """Minimal SourceProvider over one in-memory file (for a cheap one-file
    CodeIndex when we only need a single function's boundaries)."""

    def __init__(self, rel_path: str, text: str):
        self.rel_path = rel_path
        self.text = text
        self.ext = os.path.splitext(rel_path)[1].lower()

    def list_files(self, exts=None):
        return [SourceFile(self.rel_path, len(self.text), self.ext)]

    def read_file(self, rel_path: str):
        return self.text if rel_path == self.rel_path else None

    def iter_text(self, exts=None):
        yield self.rel_path, self.text

    def change_key(self, sf):
        return (len(self.text),)


def _function_bounds(rel_path: str, text: str, function_name: str):
    """``(line_start, line_end)`` for ``function_name`` via a one-file CodeIndex."""
    if not function_name:
        return None
    from Application_Logic.Logic_Code_Index import build_index
    try:
        idx = build_index(_SingleFileProvider(rel_path, text))
    except Exception:  # noqa: BLE001 — indexing best-effort; fall back to no bounds
        return None
    return injection.function_bounds(idx, function_name)


def _require_test_project(state: AppState, test_project_id: int):
    db = state.require_open()
    for p in db.list_test_projects():
        if p["id"] == test_project_id:
            return db
    raise ProjectError(f"No such test project: {test_project_id}")


# ----------------------------------------------------------------------
# Test projects
# ----------------------------------------------------------------------
@router.get("/projects")
def list_projects(state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        return {"projects": db.list_test_projects()}
    return _guard(go)


class CreateProjectBody(BaseModel):
    name: str


@router.post("/projects")
def create_project(body: CreateProjectBody, state: AppState = Depends(get_state),
                   bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        name = body.name.strip()
        if not name:
            raise ProjectError("Test project name cannot be empty.")
        pid = db.create_test_project(name)
        bus.publish("db-changed", {"reason": "test-project-created", "id": pid})
        return {"id": pid, "name": name}
    return _guard(go)


class RenameProjectBody(BaseModel):
    name: str


@router.patch("/projects/{test_project_id}")
def rename_project(test_project_id: int, body: RenameProjectBody,
                   state: AppState = Depends(get_state),
                   bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        _require_test_project(state, test_project_id)
        name = body.name.strip()
        if not name:
            raise ProjectError("Test project name cannot be empty.")
        db.rename_test_project(test_project_id, name)
        bus.publish("db-changed", {"reason": "test-project-renamed",
                                   "id": test_project_id})
        return {"id": test_project_id, "name": name}
    return _guard(go)


@router.delete("/projects/{test_project_id}")
def delete_project(test_project_id: int, state: AppState = Depends(get_state),
                   bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        db.delete_test_project(test_project_id)
        bus.publish("db-changed", {"reason": "test-project-deleted",
                                   "id": test_project_id})
        return {"id": test_project_id, "deleted": True}
    return _guard(go)


# ----------------------------------------------------------------------
# Helper-file import
# ----------------------------------------------------------------------
class ImportFile(BaseModel):
    rel_path: str
    content: str


class ImportBody(BaseModel):
    # Either walk a folder for .c/.h files, or pass explicit file contents.
    folder: Optional[str] = None
    files: Optional[list[ImportFile]] = None
    # Folder in the source tree the files go into. Empty is the source root.
    dest: str = ""
    # Only report what would be imported.
    preview: bool = False


def _clean_dest(dest: str) -> str:
    parts = [p for p in dest.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts or ":" in dest:
        raise ProjectError("The destination must be a folder inside the source tree.")
    return "/".join(parts)


@router.post("/projects/{test_project_id}/import")
def import_files(test_project_id: int, body: ImportBody,
                 state: AppState = Depends(get_state),
                 bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        _require_test_project(state, test_project_id)
        pairs: list[tuple[str, str]] = []
        if body.folder:
            provider = FilesystemSourceProvider(body.folder)
            # Helper files are C/C++ sources only.
            for rel, text in provider.iter_text(SOURCE_EXTENSIONS):
                pairs.append((rel, text))
        if body.files:
            for f in body.files:
                ext = os.path.splitext(f.rel_path)[1].lower()
                if ext in SOURCE_EXTENSIONS:
                    pairs.append((f.rel_path, f.content))
        if not pairs:
            raise ProjectError("No .c/.h helper files found to import.")
        dest = _clean_dest(body.dest)
        if dest:
            pairs = [(f"{dest}/{rel}", text) for rel, text in pairs]
        if body.preview:
            active = state.require_releases().get_active_release()
            provider = release_source_provider(db, active.id if active else None)
            production = ({sf.rel_path for sf in provider.list_files()}
                          if provider else set())
            return {"files": [{"rel_path": rel, "replaces_source": rel in production}
                              for rel, _text in pairs]}
        count = db.import_test_project_files(test_project_id, pairs)
        bus.publish("db-changed", {"reason": "test-files-imported",
                                   "id": test_project_id, "count": count})
        return {"id": test_project_id, "imported": count}
    return _guard(go)


@router.get("/projects/{test_project_id}/files")
def list_files(test_project_id: int, state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        return {"files": db.list_test_project_files(test_project_id)}
    return _guard(go)


@router.get("/projects/{test_project_id}/files/content")
def read_file(test_project_id: int, rel_path: str = Query(...),
              state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        text = db.read_test_project_file(test_project_id, rel_path)
        if text is None:
            raise ProjectError(f"No such helper file: {rel_path}")
        return {"rel_path": rel_path, "content": text}
    return _guard(go)


@router.delete("/projects/{test_project_id}/files")
def delete_file(test_project_id: int, rel_path: str = Query(...),
                state: AppState = Depends(get_state),
                bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        db.delete_test_project_file(test_project_id, rel_path)
        bus.publish("db-changed", {"reason": "test-file-deleted",
                                   "id": test_project_id})
        return {"id": test_project_id, "rel_path": rel_path, "deleted": True}
    return _guard(go)


# ----------------------------------------------------------------------
# The project folder, and the files changed for a test project
# ----------------------------------------------------------------------
def _project_folder(state: AppState, test_project_id: int) -> str:
    """The folder a test project's builds run in: the source folder of its test
    build for the active release, else of that release's Main build, else the
    default source folder (Preferences → Paths). "" when none is on this PC."""
    db = state.require_open()
    active = state.require_releases().get_active_release()
    builds = db.list_test_builds(active.id) if active else []
    candidates = [b["source_dir"] for b in builds if b["test_project_id"] == test_project_id]
    candidates += [b["source_dir"] for b in builds if b["is_main"]]
    candidates.append(db.get_meta("inject_source_path", ""))
    return next((c for c in candidates if c and os.path.isdir(c)), "")


def _original(state: AppState, test_project_id: int, rel_path: str) -> tuple:
    """``(bytes or None, where)`` of the file a changed file replaces: the
    project folder's copy, else the release's imported source."""
    folder = _project_folder(state, test_project_id)
    if folder:
        data = project_files.read_bytes(folder, rel_path)
        if data is not None:
            return data, "disk"
    active = state.require_releases().get_active_release()
    provider = release_source_provider(state.require_open(), active.id if active else None)
    text = provider.read_file(rel_path) if provider is not None else None
    return (text.encode("utf-8"), "source") if text is not None else (None, None)


@router.get("/projects/{test_project_id}/tree")
def project_tree(test_project_id: int, dir: str = Query(""),
                 state: AppState = Depends(get_state)) -> dict:
    """One folder of the project folder on disk. ``available`` is false when
    this PC has no project folder; the view then lists the imported source."""
    def go():
        _require_test_project(state, test_project_id)
        folder = _project_folder(state, test_project_id)
        if not folder:
            return {"folder": "", "available": False, "dir": "", "entries": []}
        return {"folder": folder, "available": True, "dir": project_files.clean_rel(dir),
                "entries": project_files.list_dir(folder, dir)}
    return _guard(go)


@router.get("/projects/{test_project_id}/disk/content")
def disk_content(test_project_id: int, rel_path: str = Query(...),
                 state: AppState = Depends(get_state)) -> dict:
    """A file of the project folder, for reading. ``editable`` says whether it
    can become a changed file (it must be UTF-8 text)."""
    def go():
        _require_test_project(state, test_project_id)
        folder = _project_folder(state, test_project_id)
        if not folder:
            raise ProjectError("This PC has no project folder for the test project. Set "
                               "the test build's source folder.")
        return {"rel_path": project_files.clean_rel(rel_path),
                **project_files.open_text(folder, rel_path)}
    return _guard(go)


class FileContentBody(BaseModel):
    rel_path: str
    content: str


@router.put("/projects/{test_project_id}/files/content")
def save_file(test_project_id: int, body: FileContentBody,
              state: AppState = Depends(get_state),
              bus: EventBus = Depends(get_bus)) -> dict:
    """Save a file of the test project. The first save of a project file makes
    it a changed file and remembers which original it was made from."""
    def go():
        db = state.require_edit()
        _require_test_project(state, test_project_id)
        rel_path = project_files.clean_rel(body.rel_path)
        if not rel_path:
            raise ProjectError("A file path is required.")
        known = any(f["rel_path"] == rel_path
                    for f in db.list_test_project_files(test_project_id))
        base_hash = None
        if not known:
            data, _where = _original(state, test_project_id, rel_path)
            if data is not None:
                _text, editable, reason = project_files.as_text(data)
                if not editable:
                    raise ProjectError(reason)
                base_hash = project_files.sha256_bytes(data)
        db.save_test_project_file(test_project_id, rel_path, body.content, base_hash)
        bus.publish("db-changed", {"reason": "test-file-saved", "id": test_project_id})
        return {"id": test_project_id, "rel_path": rel_path, "created": not known}
    return _guard(go)


@router.get("/projects/{test_project_id}/files/original")
def original_file(test_project_id: int, rel_path: str = Query(...),
                  state: AppState = Depends(get_state)) -> dict:
    """What a changed file replaces, to compare with. ``content`` is None for a
    file the test project adds."""
    def go():
        _require_test_project(state, test_project_id)
        data, where = _original(state, test_project_id, rel_path)
        if data is None:
            return {"rel_path": rel_path, "content": None, "source": None}
        return {"rel_path": rel_path, "content": project_files.as_text(data)[0],
                "source": where}
    return _guard(go)


@router.get("/projects/{test_project_id}/changes")
def changes(test_project_id: int, state: AppState = Depends(get_state)) -> dict:
    """Everything the test project changes in a build: its changed and added
    files, and the files its hooks go into."""
    def go():
        db = state.require_open()
        _require_test_project(state, test_project_id)
        folder = _project_folder(state, test_project_id)
        active = state.require_releases().get_active_release()
        provider = release_source_provider(db, active.id if active else None)
        loaded = {sf.rel_path for sf in provider.list_files()} if provider else set()
        hooks: dict = {}
        for h in db.list_injections(test_project_id):
            hooks[h["src_file_path"]] = hooks.get(h["src_file_path"], 0) + 1
        out = []
        for f in db.list_test_project_files(test_project_id):
            rel = f["rel_path"]
            on_disk = bool(folder) and os.path.isfile(project_files.resolve(folder, rel))
            replaces = on_disk or rel in loaded
            out.append({
                "rel_path": rel, "size": f["size"],
                "status": "modified" if replaces or f["base_hash"] else "added",
                "original_changed": project_files.original_changed(
                    folder, rel, f["base_hash"]) is True,
                # The file replaces the whole original, so hooks in it aren't applied.
                "hooks_lost": hooks.pop(rel, 0)})
        out += [{"rel_path": rel, "size": 0, "status": "hooks", "hooks": n,
                 "original_changed": False, "hooks_lost": 0}
                for rel, n in sorted(hooks.items())]
        return {"folder": folder, "files": sorted(out, key=lambda x: x["rel_path"].casefold())}
    return _guard(go)


# ----------------------------------------------------------------------
# Production source listing (the tree the user injects into)
# ----------------------------------------------------------------------
@router.get("/source/files")
def source_files(state: AppState = Depends(get_state)) -> dict:
    """List the active release's production source files (for the file tree)."""
    def go():
        provider = _production_provider(state)
        return {"files": [{"rel_path": sf.rel_path, "size": sf.size, "ext": sf.ext}
                          for sf in provider.list_files()]}
    return _guard(go)


# Listing a release's functions reads every file, so it's kept until the source
# changes. It isn't stored: the project's source can be encrypted.
_functions_cache: dict = {"key": None, "functions": []}


@router.get("/source/functions")
def source_functions(state: AppState = Depends(get_state)) -> dict:
    """Every function in the active release's source, for the search box."""
    def go():
        from Application_Logic.Logic_Code_Index import file_function_list
        provider = _production_provider(state)
        files = provider.list_files()
        key = tuple((sf.rel_path, sf.content_hash) for sf in files)
        if _functions_cache["key"] != key:
            found = []
            for sf in files:
                text = provider.read_file(sf.rel_path)
                if text is None:
                    continue
                found.extend({"name": f.name, "rel_path": sf.rel_path,
                              "line": f.line_start}
                             for f in file_function_list(text, sf.rel_path))
            _functions_cache.update(key=key, functions=found)
        return {"functions": _functions_cache["functions"]}
    return _guard(go)


@router.get("/source/content")
def source_content(rel_path: str = Query(...),
                   state: AppState = Depends(get_state)) -> dict:
    def go():
        text = _production_provider(state).read_file(rel_path)
        if text is None:
            raise ProjectError(f"No such source file: {rel_path}")
        return {"rel_path": rel_path, "content": text}
    return _guard(go)


# ----------------------------------------------------------------------
# Injection hooks
# ----------------------------------------------------------------------
class InjectionBody(BaseModel):
    injection_id: Optional[int] = None   # present → update, absent → add
    src_file_path: str
    function_name: str = ""
    line_above_code: str = ""
    line_below_code: str = ""
    injected_code: str = ""
    offset_lines: int = 0
    # Where to splice, as a line index in the source. Sets both anchors.
    at_index: Optional[int] = None


@router.get("/projects/{test_project_id}/injections")
def list_injections(test_project_id: int, src_file_path: Optional[str] = None,
                    state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        rows = db.list_injections(test_project_id, src_file_path)
        # Annotate each hook with its current resolution against live source.
        out = []
        for r in rows:
            try:
                lines = _read_production_lines(state, r["src_file_path"])
                res = injection.resolve_injection(
                    lines, r["line_above_code"], r["line_below_code"])
            except ProjectError:
                res = {"index": None, "confidence": 0, "anchor": "none"}
            out.append({**r, "resolved_index": res["index"],
                        "confidence": res["confidence"], "anchor": res["anchor"]})
        return {"injections": out}
    return _guard(go)


@router.post("/projects/{test_project_id}/injections")
def upsert_injection(test_project_id: int, body: InjectionBody,
                     state: AppState = Depends(get_state),
                     bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        _require_test_project(state, test_project_id)
        above, below = body.line_above_code, body.line_below_code
        if body.at_index is not None:
            above, below = injection.anchor_context(
                _read_production_lines(state, body.src_file_path), body.at_index)
        if body.injection_id is not None:
            db.update_injection(
                body.injection_id,
                src_file_path=body.src_file_path,
                function_name=body.function_name,
                line_above_code=above,
                line_below_code=below,
                injected_code=body.injected_code,
                offset_lines=body.offset_lines)
            iid = body.injection_id
        else:
            iid = db.add_injection(
                test_project_id, body.src_file_path, body.function_name,
                above, below, body.injected_code, body.offset_lines)
        bus.publish("db-changed", {"reason": "injection-saved",
                                   "id": test_project_id, "injection_id": iid})
        return {"injection_id": iid}
    return _guard(go)


@router.delete("/injections/{injection_id}")
def remove_injection(injection_id: int, state: AppState = Depends(get_state),
                     bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        db.remove_injection(injection_id)
        bus.publish("db-changed", {"reason": "injection-removed",
                                   "injection_id": injection_id})
        return {"injection_id": injection_id, "deleted": True}
    return _guard(go)


class ResolveBody(BaseModel):
    src_file_path: str
    line_above_code: str = ""
    line_below_code: str = ""


@router.post("/resolve")
def resolve(body: ResolveBody, state: AppState = Depends(get_state)) -> dict:
    """Resolve a candidate splice point against the live source (for the UI's
    confidence badge / conflict placeholder)."""
    def go():
        lines = _read_production_lines(state, body.src_file_path)
        return injection.resolve_injection(
            lines, body.line_above_code, body.line_below_code)
    return _guard(go)


class ShiftBody(BaseModel):
    direction: str   # "up" | "down"


@router.post("/injections/{injection_id}/shift")
def shift(injection_id: int, body: ShiftBody,
          state: AppState = Depends(get_state),
          bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_edit()
        inj = db.get_injection(injection_id)
        if inj is None:
            raise ProjectError(f"No such injection: {injection_id}")
        text = _production_provider(state).read_file(inj["src_file_path"])
        if text is None:
            raise ProjectError(f"No such source file: {inj['src_file_path']}")
        lines = injection.source_lines(text)
        bounds = _function_bounds(inj["src_file_path"], text, inj["function_name"])
        result = injection.shift_injection(
            lines, injection_id, body.direction, db, func_bounds=bounds)
        if not result.get("ok"):
            raise ProjectError(result.get("reason", "Shift refused."))
        bus.publish("db-changed", {"reason": "injection-shifted",
                                   "injection_id": injection_id})
        return result
    return _guard(go)


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------
class ExportBody(BaseModel):
    mode: str = "modified"        # "modified" | "reconstruct"
    out_dir: str
    overwrite: bool = False       # allow writing into a non-empty directory
    release_id: Optional[int] = None    # defaults to the active release
    continue_anyway: bool = False       # past the clean-release lock
    continue_reason: str = ""


def require_clean_lock(state: AppState, release_id: int, continue_anyway: bool,
                       reason: str = "") -> None:
    """Stops before code changes if the release's clean data isn't imported.
    Continuing is the user's responsibility and is recorded, so it needs a
    session that can write."""
    db = state.require_open()
    rm = state.require_releases()
    name = next((r.name for r in rm.releases if r.id == release_id), "")
    lock = test_builds.clean_lock_status(db, release_id, name)
    if lock["ok"]:
        return
    if not continue_anyway:
        raise HTTPException(status_code=409, detail={"code": "clean_lock", **lock})
    db = state.require_edit()
    db.add_clean_lock_override(release_id, lock["missing"], reason,
                               FileLockManager.get_username())


@router.post("/projects/{test_project_id}/export")
def export(test_project_id: int, body: ExportBody,
           state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        _require_test_project(state, test_project_id)
        if body.mode not in ("modified", "reconstruct"):
            raise ProjectError(f"Unknown export mode: {body.mode}")
        out_dir = body.out_dir
        if not out_dir:
            raise ProjectError("An output directory is required.")
        if os.path.isdir(out_dir) and os.listdir(out_dir) and not body.overwrite:
            raise ProjectError(
                "Output directory is not empty. Enable overwrite to proceed.")
        os.makedirs(out_dir, exist_ok=True)

        release_id = body.release_id
        if release_id is None:
            release_id = state.require_releases().get_active_release().id
        require_clean_lock(state, release_id, body.continue_anyway, body.continue_reason)
        provider = _production_provider(state, release_id)
        files, conflicts = test_builds.render_hooked_files(
            db, provider, test_project_id, body.mode)
        written = [test_builds.write_source_file(out_dir, rel_path, text)
                   for rel_path, text in files.items()]

        return {"out_dir": out_dir, "count": len(written),
                "written": written, "conflicts": conflicts}
    return _guard(go)


# ----------------------------------------------------------------------
# Build settings + runner
# ----------------------------------------------------------------------
@router.get("/settings")
def get_settings(state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        return {
            "terminal": db.get_meta("inject_terminal", test_builds.default_terminal()),
            "wsl_distro": db.get_meta("inject_wsl_distro", ""),
            "build_command": db.get_meta("inject_build_command", ""),
            "build_cwd": test_builds.stored_build_folder(db),
            "source_path": db.get_meta("inject_source_path", ""),
        }
    return _guard(go)


class SettingsBody(BaseModel):
    terminal: Optional[str] = None
    wsl_distro: Optional[str] = None
    build_command: Optional[str] = None
    build_cwd: Optional[str] = None
    source_path: Optional[str] = None


@router.post("/settings")
def set_settings(body: SettingsBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_edit()
        if body.terminal is not None:
            if body.terminal not in TERMINALS:
                raise ProjectError(f"Unknown terminal: {body.terminal}")
            db.set_meta("inject_terminal", body.terminal)
        if body.wsl_distro is not None:
            db.set_meta("inject_wsl_distro", body.wsl_distro)
        if body.build_command is not None:
            db.set_meta("inject_build_command", body.build_command)
        if body.build_cwd is not None:
            db.set_meta("inject_build_cwd", body.build_cwd)
        if body.source_path is not None:
            db.set_meta("inject_source_path", body.source_path)
        db.commit()
        return {"ok": True}
    return _guard(go)


_build_argv = test_builds.build_argv


def _require_build_folder(cwd: str, terminal: str) -> None:
    """A build started from no folder runs in the app's own install folder, where
    a project's make can't be found."""
    if not cwd.strip():
        raise ProjectError(
            "No build folder is set. Choose your make script or its folder.")
    # WSL paths are inside the distro, so they can't be checked from here.
    if terminal != "wsl" and not os.path.isdir(cwd):
        raise ProjectError(f"The build folder can't be found: {cwd}")


class BuildBody(BaseModel):
    command: Optional[str] = None     # overrides the stored build_command
    cwd: Optional[str] = None         # overrides the stored build_cwd
    terminal: Optional[str] = None    # overrides the stored terminal
    wsl_distro: Optional[str] = None


def _stream_build(build_id: str, argv: list[str], cwd: Optional[str],
                  terminal: str, bus: EventBus) -> None:
    """Run ``argv`` and fan its merged stdout/stderr over the ``build`` SSE event."""
    bus.publish("build", {"build_id": build_id, "event": "start",
                          "argv": argv, "terminal": terminal})
    # WSL handles its own `cd`; for other terminals run inside cwd directly.
    popen_cwd = None if (terminal == "wsl" or not cwd) else cwd
    try:
        proc = subprocess.Popen(
            test_builds.popen_args(argv), cwd=popen_cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace")
    except (OSError, ValueError) as e:
        bus.publish("build", {"build_id": build_id, "event": "error",
                              "line": f"Failed to launch build: {e}"})
        bus.publish("build", {"build_id": build_id, "event": "done",
                              "returncode": -1})
        return
    with _builds_lock:
        _builds[build_id] = proc
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            bus.publish("build", {"build_id": build_id, "event": "log",
                                  "line": line.rstrip("\n")})
        proc.wait()
    finally:
        with _builds_lock:
            _builds.pop(build_id, None)
    bus.publish("build", {"build_id": build_id, "event": "done",
                          "returncode": proc.returncode})


@router.post("/projects/{test_project_id}/build")
def build(test_project_id: int, body: BuildBody,
          state: AppState = Depends(get_state),
          bus: EventBus = Depends(get_bus)) -> dict:
    def go():
        db = state.require_open()
        terminal = (body.terminal
                    or db.get_meta("inject_terminal", test_builds.default_terminal()))
        if terminal not in TERMINALS:
            raise ProjectError(f"Unknown terminal: {terminal}")
        distro = body.wsl_distro if body.wsl_distro is not None \
            else (db.get_meta("inject_wsl_distro", "") or "")
        command = body.command if body.command is not None \
            else (db.get_meta("inject_build_command", "") or "")
        cwd = test_builds.build_folder(body.cwd) if body.cwd is not None \
            else test_builds.stored_build_folder(db)
        if not command.strip():
            raise ProjectError("No build command configured.")
        _require_build_folder(cwd, terminal)
        build_id = uuid.uuid4().hex
        argv = _build_argv(terminal, command, cwd, distro)
        threading.Thread(
            target=_stream_build,
            args=(build_id, argv, cwd, terminal, bus),
            name=f"build-{build_id[:8]}", daemon=True).start()
        return {"build_id": build_id, "terminal": terminal}
    return _guard(go)


class TerminalBody(BaseModel):
    cwd: Optional[str] = None         # overrides the stored build_cwd
    terminal: Optional[str] = None    # overrides the stored terminal


@router.post("/terminal")
def open_terminal(body: TerminalBody, state: AppState = Depends(get_state)) -> dict:
    """Opens an empty terminal window in the build folder."""
    def go():
        db = state.require_open()
        terminal = (body.terminal
                    or db.get_meta("inject_terminal", test_builds.default_terminal()))
        if terminal not in TERMINALS:
            raise ProjectError(f"Unknown terminal: {terminal}")
        cwd = test_builds.build_folder(body.cwd) if body.cwd is not None \
            else test_builds.stored_build_folder(db)
        _require_build_folder(cwd, terminal)
        distro = db.get_meta("inject_wsl_distro", "") or ""
        # A window of its own: this app has no console for it to share.
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(test_builds.terminal_window_argv(terminal, cwd, distro),
                             cwd=None if terminal == "wsl" else cwd, creationflags=flags)
        except (OSError, ValueError) as e:
            raise ProjectError(f"Couldn't open a terminal: {e}") from e
        return {"terminal": terminal, "cwd": cwd}
    return _guard(go)


@router.post("/build/{build_id}/cancel")
def cancel_build(build_id: str) -> dict:
    with _builds_lock:
        proc = _builds.get(build_id)
    if proc is None:
        raise HTTPException(status_code=404, detail="No such running build.")
    try:
        proc.terminate()
    except OSError:
        pass
    return {"build_id": build_id, "cancelling": True}
