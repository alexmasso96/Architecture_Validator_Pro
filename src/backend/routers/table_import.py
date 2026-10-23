"""
Import Profiles + sample-session API (#8 Phase B).

Sample sessions: the profile editor loads a sample Excel/CSV ONCE into server
memory; every preview recomputation afterwards is pure in-memory work — no
file I/O, so debounced live preview is free even on EDR-monitored machines.
Sessions are capped, TTL-evicted, and never persisted.

Profiles: JSON in the per-user app-data directory (same pattern as prefs —
they describe org file formats, not project data, so they are global across
projects and exportable for sharing). Built-in read-only profiles reproduce
the legacy InterfaceExport flows through the new engine.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from Application_Logic import Logic_Table_Import as engine

from ..deps import get_bus, get_state
from ..events import EventBus
from ..security import require_token
from ..state import AppState, ProjectError

router = APIRouter(prefix="/api/import", tags=["import"],
                   dependencies=[Depends(require_token)])


def _guard(fn):
    try:
        return fn()
    except ProjectError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

# ---------------------------------------------------------------------------
# Sample sessions (in-memory)
# ---------------------------------------------------------------------------
_SAMPLE_TTL_S = 30 * 60
_MAX_SAMPLES = 8
SAMPLE_ROW_CAP = 1000

_samples: dict[str, dict] = {}
_samples_lock = threading.Lock()


def _evict_samples() -> None:
    now = time.time()
    dead = [k for k, v in _samples.items() if now - v["at"] > _SAMPLE_TTL_S]
    for k in dead:
        _samples.pop(k, None)
    while len(_samples) > _MAX_SAMPLES:
        oldest = min(_samples, key=lambda k: _samples[k]["at"])
        _samples.pop(oldest, None)


def _get_sample(sample_id: str) -> dict:
    with _samples_lock:
        s = _samples.get(sample_id)
        if s is None:
            raise HTTPException(status_code=404,
                                detail="Sample session not found (expired?). "
                                       "Reload the sample file.")
        s["at"] = time.time()
        return s


class SampleBody(BaseModel):
    path: str
    sheet: Optional[str] = None
    header_row: Optional[int] = None


@router.post("/sample")
def load_sample(body: SampleBody) -> dict:
    """Read the sample file once and keep it in memory for previewing."""
    if not body.path or not os.path.isfile(body.path):
        raise HTTPException(status_code=400, detail=f"Not a file: {body.path}")
    try:
        data = engine.load_sample(body.path, sheet=body.sheet,
                                  header_row=body.header_row,
                                  cap=SAMPLE_ROW_CAP)
    except Exception as e:  # unreadable/corrupt file — a user-facing error
        raise HTTPException(status_code=400,
                            detail=f"Could not read the file: {e}") from e
    sid = uuid.uuid4().hex[:12]
    with _samples_lock:
        _evict_samples()
        _samples[sid] = {"at": time.time(), "path": body.path, **data}
    return {"sample_id": sid,
            "sheets": data["sheets"], "sheet": data["sheet"],
            "header_row": data["header_row"],
            "columns": data["columns"],
            "row_count": len(data["rows"]),
            "truncated": data["truncated"],
            "rows": data["rows"][:10]}


@router.delete("/sample/{sample_id}")
def drop_sample(sample_id: str) -> dict:
    with _samples_lock:
        _samples.pop(sample_id, None)
    return {"dropped": sample_id}


# ---------------------------------------------------------------------------
# Preview (pure in-memory)
# ---------------------------------------------------------------------------
class PreviewBody(BaseModel):
    sample_id: str
    rules: list[dict] = Field(default_factory=list)
    mapping: Optional[dict[str, str]] = None
    locked_targets: list[str] = Field(default_factory=list)
    limit: int = 10


@router.post("/preview")
def preview(body: PreviewBody) -> dict:
    """Staged pipeline preview: per-rule counters + ~10 result rows, and the
    target-shaped rows when a mapping is set."""
    s = _get_sample(body.sample_id)
    try:
        out = engine.apply_pipeline(s["columns"], s["rows"], body.rules)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    parts = engine.partition_rows(out["rows"], out["partition_column"])
    resp = {
        "stages": out["stages"],
        "columns": out["columns"],
        "partition_column": out["partition_column"],
        "partitions": {k: len(v) for k, v in parts.items()},
        "row_count": len(out["rows"]),
        "truncated": s["truncated"],
        "rows": out["rows"][:body.limit],
    }
    if body.mapping:
        resp["mapped_rows"] = engine.apply_mapping(
            out["rows"][:body.limit], body.mapping,
            locked_targets=set(body.locked_targets))
    return resp


class PreviewAllBody(BaseModel):
    sample_id: str
    rules: list[dict] = Field(default_factory=list)
    offset: int = 0
    limit: int = 200


@router.post("/preview/all")
def preview_all(body: PreviewAllBody) -> dict:
    """The render-all view: the complete filtered dataset, paged — for
    manually cross-checking the filter against the known-good export.
    Recomputed per page; that is still pure in-memory work."""
    s = _get_sample(body.sample_id)
    try:
        out = engine.apply_pipeline(s["columns"], s["rows"], body.rules)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    rows = out["rows"]
    lo = max(0, body.offset)
    hi = min(len(rows), lo + max(1, min(body.limit, 500)))
    return {"columns": out["columns"], "total": len(rows),
            "offset": lo, "rows": rows[lo:hi],
            "truncated": s["truncated"]}


# ---------------------------------------------------------------------------
# Diff + commit (#8 pre-import diff; #7 locked-cell semantics)
#
# Two-step contract: /diff reads the FULL file once, computes everything
# without writing, and stages the result under a diff_id; /commit applies the
# staged import. Update-mode row targeting is recomputed against the CURRENT
# table at commit time (staged row indices could be stale if the table changed
# between diff and confirm — recomputing from the staged imported rows is
# always safe because the join is by key, not by index).
# ---------------------------------------------------------------------------
_diffs: dict[str, dict] = {}
_diffs_lock = threading.Lock()
_DIFF_TTL_S = 15 * 60


def _evict_diffs() -> None:
    now = time.time()
    for k in [k for k, v in _diffs.items() if now - v["at"] > _DIFF_TTL_S]:
        _diffs.pop(k, None)


class DiffBody(BaseModel):
    path: str
    sheet: Optional[str] = None
    header_row: int = 0
    rules: list[dict] = Field(default_factory=list)
    mapping: dict[str, str]
    locked_targets: list[str] = Field(default_factory=list)
    mode: str = "append"                     # "append" | "update"
    key_columns: list[str] = Field(default_factory=list)   # update: TARGET cols
    model_id: Optional[int] = None           # update-mode target model
    create_missing: bool = True              # append mode
    profile_name: str = ""                   # for the change-log entry


@router.post("/diff")
def import_diff(body: DiffBody, state: AppState = Depends(get_state)) -> dict:
    """Compute the pre-import diff on the FULL file. Writes nothing."""
    def go():
        db = state.require_open()
        mgr = state.require_arch()
        if not body.mapping:
            raise HTTPException(status_code=400,
                                detail="A column mapping is required.")
        if body.mode not in ("append", "update"):
            raise HTTPException(status_code=400,
                                detail="mode must be 'append' or 'update'")
        if not os.path.isfile(body.path):
            raise HTTPException(status_code=400,
                                detail=f"Not a file: {body.path}")
        try:
            columns, rows = engine.read_table(body.path, sheet=body.sheet,
                                              header_row=body.header_row)
            out = engine.apply_pipeline(columns, rows, body.rules)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        filename = os.path.basename(body.path)
        staged = {"at": time.time(), "mode": body.mode,
                  "filename": filename, "profile_name": body.profile_name,
                  "locked_targets": list(body.locked_targets)}

        if body.mode == "append":
            parts = engine.partition_rows(out["rows"], out["partition_column"])
            existing_names = {m.name for m in mgr.models if not m.is_deleted}
            model_data, models = {}, []
            for name in sorted(parts):
                target = name or "(current model)"
                mapped = engine.apply_mapping(
                    parts[name], body.mapping,
                    locked_targets=set(body.locked_targets))
                model_data[name] = mapped
                models.append({"name": target,
                               "exists": name in existing_names,
                               "rows": len(mapped)})
            staged.update(model_data=model_data,
                          create_missing=body.create_missing)
            summary = {"mode": "append", "models": models,
                       "total_rows": sum(m["rows"] for m in models)}
        else:
            if body.model_id is None:
                raise HTTPException(status_code=400,
                                    detail="model_id is required for "
                                           "match-and-update imports.")
            targets = list(body.mapping.values())
            missing = [k for k in body.key_columns if k not in targets]
            if not body.key_columns or missing:
                raise HTTPException(
                    status_code=400,
                    detail="key_columns must be mapped table columns "
                           f"(missing: {missing or 'none given'}).")
            value_columns = [t for t in targets if t not in body.key_columns]
            if not value_columns:
                raise HTTPException(status_code=400,
                                    detail="The mapping has no value columns "
                                           "beyond the key.")
            imported = engine.apply_mapping(
                out["rows"], body.mapping,
                locked_targets=set(body.locked_targets))
            existing = db.get_model_rows(body.model_id)
            diff = engine.compute_update_diff(existing, imported,
                                              body.key_columns, value_columns)
            staged.update(imported=imported, key_columns=body.key_columns,
                          value_columns=value_columns, model_id=body.model_id)
            summary = {
                "mode": "update", "model_id": body.model_id,
                "updates": len(diff["updates"]),
                "cells": sum(len(u["changes"]) for u in diff["updates"]),
                "locked": diff["locked"],
                "conflicts": diff["conflicts"],
                "orphans": len(diff["orphans"]),
                "unmatched": diff["unmatched"],
                "update_preview": diff["updates"][:20],
            }

        diff_id = uuid.uuid4().hex[:12]
        with _diffs_lock:
            _evict_diffs()
            _diffs[diff_id] = staged
        return {"diff_id": diff_id, **summary}
    return _guard(go)


class CommitBody(BaseModel):
    diff_id: str


def _history(db, description: str) -> None:
    try:
        from Application_Logic.Logic_File_Locking import FileLockManager
        db.add_history_entry(description=description,
                             username=FileLockManager.get_username(),
                             release_id=db.get_active_release_id())
    except Exception:  # noqa: BLE001 — history must never break the import
        pass


@router.post("/commit")
def import_commit(body: CommitBody, state: AppState = Depends(get_state),
                  bus: EventBus = Depends(get_bus)) -> dict:
    """Apply a staged diff. Locked cells are never overwritten (#7)."""
    with _diffs_lock:
        staged = _diffs.pop(body.diff_id, None)
    if staged is None:
        raise HTTPException(status_code=404,
                            detail="Diff expired or already committed — "
                                   "run the diff again.")

    def go():
        db = state.require_edit()
        mgr = state.require_arch()
        label = staged["profile_name"] or "table import"

        if staged["mode"] == "append":
            by_name = {m.name: m for m in mgr.models if not m.is_deleted}
            summary, model_ids, total = [], [], 0
            for name in sorted(staged["model_data"]):
                new_rows = staged["model_data"][name]
                if not new_rows:
                    continue
                model = by_name.get(name) if name else mgr.get_active_model()
                created = False
                if model is None:
                    if not name or not staged["create_missing"]:
                        continue
                    model = mgr.create_model(name, "In Work")
                    by_name[name] = model
                    created = True
                existing = db.get_model_rows(model.id)
                existing.extend(new_rows)
                db.save_model_rows(model.id, existing)
                summary.append({"name": model.name, "created": created,
                                "added": len(new_rows)})
                model_ids.append(model.id)
                total += len(new_rows)
            db.commit()
            _history(db, f"Import ({label}): added {total} row(s) to "
                         f"{len(summary)} model(s) from {staged['filename']}")
            bus.publish("db-changed", {"reason": "table-imported",
                                       "models": len(summary)})
            return {"mode": "append", "models": summary,
                    "total_added": total, "model_ids": model_ids}

        # update mode — recompute against the CURRENT rows (see note above).
        model_id = staged["model_id"]
        rows = db.get_model_rows(model_id)
        diff = engine.compute_update_diff(rows, staged["imported"],
                                          staged["key_columns"],
                                          staged["value_columns"])
        locked_set = set(staged["locked_targets"])
        applied_cells = 0
        for upd in diff["updates"]:
            row = rows[upd["row_index"]]
            for col, change in upd["changes"].items():
                cell = row.get(col)
                new_cell = dict(cell) if isinstance(cell, dict) else {}
                new_cell["text"] = change["new"]
                if col in locked_set:
                    new_cell["origin"] = "import"   # #7: lock on import
                row[col] = new_cell
                applied_cells += 1
        db.save_model_rows(model_id, rows)
        db.commit()
        _history(db, f"Import ({label}): updated {applied_cells} cell(s) in "
                     f"{len(diff['updates'])} row(s) from {staged['filename']}"
                     + (f"; {len(diff['locked'])} locked cell(s) left "
                        f"untouched" if diff["locked"] else ""))
        bus.publish("db-changed", {"reason": "table-imported",
                                   "model_id": model_id})
        return {"mode": "update", "model_id": model_id,
                "rows_updated": len(diff["updates"]),
                "cells_updated": applied_cells,
                "locked_skipped": diff["locked"],
                "conflicts": diff["conflicts"]}
    return _guard(go)


# ---------------------------------------------------------------------------
# Profiles (app-data JSON store)
# ---------------------------------------------------------------------------
_profiles_lock = threading.Lock()
APP_DIR_NAME = "ArchitectureValidator"


def _profiles_path() -> Path:
    override = os.environ.get("ARCH_IMPORT_PROFILES_FILE")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_DIR_NAME / "import_profiles.json"


def _read_profiles() -> list[dict]:
    try:
        with _profiles_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_profiles(profiles: list[dict]) -> None:
    path = _profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(profiles, fh, indent=2)
    os.replace(tmp, path)


def _builtin_profiles() -> list[dict]:
    """The legacy InterfaceExport flows as read-only profiles (standard export
    headers; the wizard re-binds detected column names when they differ)."""
    return [
        {
            "id": "builtin-interface_export-required",
            "name": "InterfaceExport ports (required interface)",
            "builtin": True,
            "filename_pattern": "INTERFACE_EXPORT_*.xls*",
            "mode": "append",
            "rules": engine.interface_export_rules(
                "Path", ops_col="Operations",
                required_col="Required Interface"),
            "mapping": {}, "locked_targets": [], "key_columns": [],
        },
        {
            "id": "builtin-interface_export-all-ports",
            "name": "InterfaceExport ports (all P10 rows)",
            "builtin": True,
            "filename_pattern": "",
            "mode": "append",
            "rules": engine.interface_export_rules("Path", ops_col="Operations"),
            "mapping": {}, "locked_targets": [], "key_columns": [],
        },
    ]


class ProfileBody(BaseModel):
    id: Optional[str] = None
    name: str
    filename_pattern: str = ""
    sheet: Optional[str] = None
    header_row: Optional[int] = None
    mode: str = "append"                    # "append" | "update"
    key_columns: list[str] = Field(default_factory=list)
    rules: list[dict] = Field(default_factory=list)
    mapping: dict[str, str] = Field(default_factory=dict)
    locked_targets: list[str] = Field(default_factory=list)


@router.get("/profiles")
def list_profiles() -> dict:
    with _profiles_lock:
        return {"profiles": _builtin_profiles() + _read_profiles()}


@router.post("/profiles")
def save_profile(body: ProfileBody) -> dict:
    if body.mode not in ("append", "update"):
        raise HTTPException(status_code=400,
                            detail="mode must be 'append' or 'update'")
    for rule in body.rules:
        if rule.get("type") not in engine.RULE_TYPES:
            raise HTTPException(status_code=400,
                                detail=f"Unknown rule type: {rule.get('type')!r}")
    pid = body.id or uuid.uuid4().hex[:12]
    if pid.startswith("builtin-"):
        raise HTTPException(status_code=400,
                            detail="Built-in profiles are read-only — "
                                   "duplicate one to customize it.")
    profile = body.model_dump()
    profile["id"] = pid
    profile["builtin"] = False
    with _profiles_lock:
        stored = [p for p in _read_profiles() if p.get("id") != pid]
        stored.append(profile)
        _write_profiles(stored)
    return {"profile": profile}


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict:
    if profile_id.startswith("builtin-"):
        raise HTTPException(status_code=400,
                            detail="Built-in profiles cannot be deleted.")
    with _profiles_lock:
        stored = _read_profiles()
        remaining = [p for p in stored if p.get("id") != profile_id]
        if len(remaining) == len(stored):
            raise HTTPException(status_code=404, detail="No such profile.")
        _write_profiles(remaining)
    return {"deleted": profile_id}
