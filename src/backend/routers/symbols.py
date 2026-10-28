"""
Symbols router (plan §3.2): fuzzy candidate lookup feeding the match-picker.

    GET /api/symbols?q=<port>&kind=function|variable|any&limit=10[&elf_hash=...]

Candidates come from the active release's symbols stored in the DB — the
union of all its component symbol files (FBL, APP, …) — or an explicit
``elf_hash``. Each candidate is ``{"name", "score"}`` — the same ``Name (NN%)``
data the Qt match dropdown showed, minus the widget — plus, when the release
has several component files, ``components`` (which files define the name).
``macro`` marks a name that exists only as a #define in the release's stored
source (preprocessor-expanded, so no ELF symbol and no address).
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
        active = (state.release_manager.get_active_release()
                  if state.release_manager is not None else None)
        rid = active.id if (active is not None and not elf_hash) else None
        has_source = rid is not None and db.has_release_source(rid)
        if not hashes and not has_source:
            # No ELF imported for the active release — empty candidate list, not an error.
            return {"query": q, "kind": kind, "elf_hash": None, "candidates": []}
        # The active release's source #define macros join the pool (generated
        # accessors never reach the ELF but are what ports often name).
        matcher = state.get_symbol_matcher(hashes, release_id=rid if has_source else None)
        files = db.get_release_symbol_files(rid) if rid is not None else []
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
        cand = {"name": n, "score": s, "inlined": n in inlined,
                "macro": n in matcher.macro_names}
        if n in comps:
            cand["components"] = comps[n]
        candidates.append(cand)
    return {
        "query": q,
        "kind": kind,
        "elf_hash": hashes[0] if hashes else None,
        "elf_hashes": hashes,
        "candidates": candidates,
    }
