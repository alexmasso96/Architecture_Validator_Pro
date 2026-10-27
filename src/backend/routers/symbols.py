"""
Symbols router (plan §3.2): fuzzy candidate lookup feeding the match-picker.

    GET /api/symbols?q=<port>&kind=function|variable|any&limit=10[&elf_hash=...]

Candidates come from the active release's symbols stored in the DB — the
union of all its component symbol files (FBL, APP, …) — or an explicit
``elf_hash``. Each candidate is ``{"name", "score"}`` — the same ``Name (NN%)``
data the Qt match dropdown showed, minus the widget — plus, when the release
has several component files, ``components`` (which files define the name).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_state
from ..security import require_token
from ..state import AppState, ProjectError

router = APIRouter(prefix="/api", tags=["symbols"],
                   dependencies=[Depends(require_token)])


@router.get("/symbols")
def symbols(q: str = Query(..., min_length=1),
            kind: str = Query("any", pattern="^(function|variable|any)$"),
            limit: int = Query(10, ge=1, le=50),
            elf_hash: Optional[str] = None,
            state: AppState = Depends(get_state)) -> dict:
    try:
        db = state.require_open()
        hashes = [elf_hash] if elf_hash else state.active_elf_hashes()
        if not hashes:
            # No ELF imported for the active release — empty candidate list, not an error.
            return {"query": q, "kind": kind, "elf_hash": None, "candidates": []}
        matcher = state.get_symbol_matcher(hashes)
        files = []
        if not elf_hash and state.release_manager is not None:
            active = state.release_manager.get_active_release()
            if active is not None and active.id is not None:
                files = db.get_release_symbol_files(active.id)
    except ProjectError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    if kind == "function":
        pairs = matcher.find_top_function_matches(q, limit=limit)
    elif kind == "variable":
        pairs = matcher.find_top_variable_matches(q, limit=limit)
    else:
        pairs = matcher.find_top_matches(q, limit=limit)

    # #6D: mark candidates that exist only as inline expansions — they are
    # matchable by name but carry no address (the UI shows "inlined" and
    # disables address-dependent affordances for them).
    inlined = getattr(matcher, "inlined_names", set())
    comps = (db.get_symbol_components(files, [n for n, _s in pairs])
             if len(files) > 1 else {})
    candidates = []
    for n, s in pairs:
        cand = {"name": n, "score": s, "inlined": n in inlined}
        if n in comps:
            cand["components"] = comps[n]
        candidates.append(cand)
    return {
        "query": q,
        "kind": kind,
        "elf_hash": hashes[0],
        "elf_hashes": hashes,
        "candidates": candidates,
    }
