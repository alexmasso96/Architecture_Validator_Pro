"""
#define macros of a release's source, as symbol-match candidates.

Generated configuration code often exposes its accessors as macros —
``#define cfgGetRLMaxValueForHigh_u16() (…)`` — which the preprocessor
expands away, so they never reach the ELF symbol table. The Code Map still
shows them (its call graph comes from the source), but an architecture port
naming one could not be matched. This module lists them so the symbol matcher
can offer them: function-like macros join the function pool, object-like ones
the variable pool.

The list is extracted from the source stored in the project for a release and
cached in project_meta, keyed by a fingerprint of that source, so it is read
again only after the source changes.

A macro defined in a header that never reached the project (over the importer's
size limit, under a skipped build/output folder, or outside the imported tree)
can't be extracted — but every name the release's Code Map shows is still a
real call target. ``release_call_targets`` lists those names so anything
visible in the call tree can be matched.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Dict, Iterable, List, Set, Tuple

_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(\()?([^\n]*)$", re.M)
# Header include guards and compiler-reserved names are not symbols anyone
# would map a port to.
_GUARD_RE = re.compile(r"(^_|_H_?$|_H__$|_INCLUDED_?$|_HPP_?$)", re.I)

_CACHE_PREFIX = "source_macros:"


def extract_macros(text: str) -> Tuple[Set[str], Set[str]]:
    """(function-like, object-like) macro names defined in ``text``."""
    funcs, objs = set(), set()
    for m in _DEFINE_RE.finditer(text or ""):
        name, paren, body = m.group(1), m.group(2), m.group(3).strip()
        if paren:
            funcs.add(name)
        elif not (_GUARD_RE.search(name) and not body):
            objs.add(name)
    return funcs, objs


def collect_macros(files: Iterable[Tuple[str, str]]) -> Dict[str, List[str]]:
    """Macros over ``(rel_path, text)`` pairs. A name defined both ways (e.g.
    in different #if branches) counts as function-like."""
    funcs: Set[str] = set()
    objs: Set[str] = set()
    for _rel, text in files:
        f, o = extract_macros(text)
        funcs |= f
        objs |= o
    return {"functions": sorted(funcs), "objects": sorted(objs - funcs)}


def _fingerprint(db, release_id: int) -> str:
    rows = db.execute(
        "SELECT rel_path, content_hash FROM release_source_files"
        " WHERE release_id=? ORDER BY rel_path", (release_id,)).fetchall()
    h = hashlib.md5()
    for rel, ch in rows:
        h.update(f"{rel}\0{ch}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def release_macros(db, release_id) -> Dict[str, List[str]]:
    """{"functions": [...], "objects": [...]} for a release's stored source
    (both empty when it has none). Cached per release in project_meta."""
    empty = {"functions": [], "objects": []}
    if db is None or release_id is None or not db.has_release_source(release_id):
        return empty
    fp = _fingerprint(db, release_id)
    key = f"{_CACHE_PREFIX}{release_id}"
    try:
        cached = json.loads(db.get_meta(key) or "null")
    except (TypeError, ValueError):
        cached = None
    if isinstance(cached, dict) and cached.get("fp") == fp:
        return {"functions": list(cached.get("functions") or []),
                "objects": list(cached.get("objects") or [])}
    from Application_Logic.Logic_Source_Store import DbReleaseSourceProvider
    result = collect_macros(DbReleaseSourceProvider(db, release_id).iter_text())
    db.set_meta(key, json.dumps({"fp": fp, **result}))   # no-op in view mode
    return result


def release_call_targets(db, release_id) -> List[str]:
    """Every function name in the release's Code Maps (all models): the
    functions plus everything they call. Raw ``0x…`` addresses are left out."""
    if db is None or release_id is None:
        return []
    rows = db.execute(
        "SELECT model_id FROM ai_release_maps WHERE release_id=?"
        " AND code_map_json IS NOT NULL", (release_id,)).fetchall()
    names: Set[str] = set()
    for (model_id,) in rows:
        cm = db.get_model_code_map(model_id, release_id=release_id) or {}
        for fname, fdata in (cm.get("functions") or {}).items():
            names.add(fname)
            names.update((fdata or {}).get("calls") or [])
    return sorted(n for n in names if n and not n.startswith("0x"))


def release_match_extras(db, release_id) -> Dict[str, List[str]]:
    """Source-side match candidates of a release: its #define macros plus the
    names in its Code Maps' call trees."""
    out = release_macros(db, release_id)
    out["call_targets"] = release_call_targets(db, release_id)
    return out
