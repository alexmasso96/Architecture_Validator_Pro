"""
Call-tree Test columns — per-release automated "is this port's function called?"
verdicts in the architecture table.

A Test column is bound to ONE release and ONE Match column (a column fed by a
Port Search / Function Search column). Running it reads that release's code map
for the model, takes each row's matched function, and records a verdict:

    1. function not in the call tree                  → Failed   (not found)
    2. called by any function                         → Passed
    3. not called, but calls other functions          → Entry point
    4. not called and calls nothing                   → Failed   (never called)

Rows whose Port State is Retired/Deleted get "No Result" (as the release-result
column does); an empty Match cell → "No Match"; no code map for the model and
release → "No Data".

Callers come from the code map (source-derived edges) UNION the release's .o
enrichment dataset (text relocations + inline expansions), so a function called
only from assembly — e.g. a bootloader's low-level init invoked from startup
code — is a real caller when the startup .o is imported. A caller is "verified"
when the .o dataset contains that edge. Strict mode counts verified callers only.

Results are RECORDED (not recomputed on view) so an ASPICE verdict is stable;
each cell carries its evidence, the run time and the code-map build time (for
the stale marker). A tester may override a verdict while the release is active
and not baselined; re-runs refresh the tool verdict and evidence but never
replace a manual verdict.

Pure module: no DB or HTTP. The worker job and the API call these.
"""
from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

TEST_COLUMN_TYPE = "CallTreeTestColumn"
BINDINGS_KEY = "calltree_test_columns"

PASSED = "Passed"
FAILED = "Failed"
ENTRY_POINT = "Entry point"
NO_MATCH = "No Match"
NO_DATA = "No Data"
NO_RESULT = "No Result"
NOT_RUN = "Not Run"

# Verdicts a tester may set by hand.
MANUAL_OPTIONS = (PASSED, FAILED, ENTRY_POINT)

# Search column types whose Match column holds a FUNCTION (a "call" test is
# meaningless for variables — variable support is a separate follow-up).
_FUNCTION_SEARCH_TYPES = ("Port Search", "Function Search")

MAX_EVIDENCE_CALLERS = 25

_MATCH_RE = re.compile(r"^(.*?)\s*\((\d+)%\)\s*$")


# ---------------------------------------------------------------------------
# Cells & layout helpers
# ---------------------------------------------------------------------------

def cell_text(cell) -> str:
    if not isinstance(cell, dict):
        return ""
    return str(cell.get("widget_text") or cell.get("text") or "").strip()


def parse_match(value: str) -> str:
    """'Name (NN%)' → 'Name'; plain names pass through."""
    v = (value or "").strip()
    m = _MATCH_RE.match(v)
    return (m.group(1) if m else v).strip()


def function_match_columns(layout) -> List[str]:
    """Match columns a Test column may bind to: the column right after a Port
    Search / Function Search column. ``layout`` = [(name, type, ...), ...]."""
    out = []
    for i, col in enumerate(layout):
        if col[1] in _FUNCTION_SEARCH_TYPES and i + 1 < len(layout):
            out.append(layout[i + 1][0])
    return out


def default_column_name(release_name: str, match_col: str) -> str:
    return f"Test {release_name} · {match_col}"


# ---------------------------------------------------------------------------
# Bindings: {test_col_name: {"release_id", "match_col", "strict"}}
# ---------------------------------------------------------------------------

def parse_bindings(raw: Optional[str]) -> Dict[str, dict]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    out = {}
    if not isinstance(data, dict):
        return {}
    for name, b in data.items():
        if isinstance(b, dict) and "release_id" in b and b.get("match_col"):
            try:
                out[name] = {"release_id": int(b["release_id"]),
                             "match_col": str(b["match_col"]),
                             "strict": bool(b.get("strict", False))}
            except (TypeError, ValueError):
                continue
    return out


def dump_bindings(bindings: Dict[str, dict]) -> str:
    return json.dumps(bindings, sort_keys=True)


def reconcile_bindings(old: Dict[str, dict], new_layout, renames: Dict[str, str],
                       incoming: Dict[str, dict],
                       release_ids: Iterable[int]) -> Dict[str, dict]:
    """Bindings for a newly saved layout, or raise ValueError.

    - Test columns keep their binding across renames; a renamed Match column
      is followed by every Test column bound to it.
    - Removed Test columns drop their binding.
    - ``incoming`` supplies bindings for Test columns created in this save
      (and may update ``strict`` on existing ones).
    - Every Test column must end up bound to an existing release and to a
      function Match column present in the new layout; a (release, match)
      pair may be tested only once.
    """
    valid_releases = set(release_ids)
    names = [c[0] for c in new_layout]
    match_cols = set(function_match_columns(new_layout))

    carried: Dict[str, dict] = {}
    for old_name, b in old.items():
        new_name = renames.get(old_name, old_name)
        nb = dict(b)
        nb["match_col"] = renames.get(b["match_col"], b["match_col"])
        carried[new_name] = nb

    result: Dict[str, dict] = {}
    seen_pairs: Set[Tuple[int, str]] = set()
    for col in new_layout:
        name, ctype = col[0], col[1]
        if ctype != TEST_COLUMN_TYPE:
            continue
        inc = incoming.get(name) or {}
        if name in carried:
            # Existing column: target is fixed; only strict mode may change.
            b = dict(carried[name])
            if "strict" in inc:
                b["strict"] = bool(inc["strict"])
        else:
            b = {"release_id": inc.get("release_id"),
                 "match_col": inc.get("match_col"),
                 "strict": bool(inc.get("strict", False))}
        if not b.get("release_id") or not b.get("match_col"):
            raise ValueError(f"Test column '{name}' needs a release and a Match column.")
        try:
            b["release_id"] = int(b["release_id"])
        except (TypeError, ValueError):
            raise ValueError(f"Test column '{name}' has an invalid release.")
        if b["release_id"] not in valid_releases:
            raise ValueError(f"Test column '{name}' is bound to a release that "
                             "no longer exists.")
        if b["match_col"] not in names:
            raise ValueError(f"Test column '{name}' depends on Match column "
                             f"'{b['match_col']}', which is not in the layout — "
                             "delete the Test column first.")
        if b["match_col"] not in match_cols:
            raise ValueError(f"'{b['match_col']}' is not a function/port Match "
                             "column, so it cannot be tested for calls.")
        pair = (b["release_id"], b["match_col"])
        if pair in seen_pairs:
            raise ValueError(f"'{b['match_col']}' already has a Test column for "
                             "that release.")
        seen_pairs.add(pair)
        b["strict"] = bool(b.get("strict", False))
        result[name] = {"release_id": b["release_id"], "match_col": b["match_col"],
                        "strict": b["strict"]}
    return result


def rename_name_keyed_bindings(bindings: Dict[str, int], renames: Dict[str, str],
                               new_names: Iterable[str]) -> Dict[str, int]:
    """For the release-RESULT column bindings ({col_name: release_id}): follow
    renames and drop removed columns. Previously a rename silently orphaned
    the result column's release binding."""
    keep = set(new_names)
    out = {}
    for name, rid in bindings.items():
        nn = renames.get(name, name)
        if nn in keep:
            out[nn] = rid
    return out


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

class CallIndex:
    """Callers/callees of every function in one release's call tree, merged
    from the code map and the .o dataset."""

    def __init__(self, code_map: Optional[dict],
                 object_pairs: Optional[Iterable[Tuple[str, str]]] = None):
        self.callers: Dict[str, Set[str]] = {}
        self.callees: Dict[str, Set[str]] = {}
        self.verified: Set[Tuple[str, str]] = set()
        self.known: Set[str] = set()
        self.has_objects = object_pairs is not None
        self._alias: Dict[str, str] = {}

        functions = (code_map or {}).get("functions") or {}
        for fname, fdata in functions.items():
            self._add_node(fname)
            for callee in (fdata or {}).get("calls") or []:
                self._add_edge(fname, callee)
            for callee in (fdata or {}).get("verified_calls") or []:
                self.verified.add((fname, callee))
        for caller, callee in object_pairs or ():
            self._add_edge(caller, callee)
            self.verified.add((caller, callee))

    def _add_node(self, name: str) -> None:
        if not name:
            return
        self.known.add(name)
        # C++: let a mangled match value find the demangled code-map key.
        try:
            from Application_Logic.Logic_Code_Map import demangle_name, normalize_cpp_name
            norm = normalize_cpp_name(demangle_name(name))
            if norm and norm != name:
                self._alias.setdefault(norm, name)
        except Exception:  # noqa: BLE001 — aliasing is best-effort
            pass

    def _add_edge(self, caller: str, callee: str) -> None:
        if not caller or not callee or caller == callee:
            return
        self._add_node(caller)
        self._add_node(callee)
        self.callees.setdefault(caller, set()).add(callee)
        self.callers.setdefault(callee, set()).add(caller)

    def resolve(self, name: str) -> Optional[str]:
        if name in self.known:
            return name
        try:
            from Application_Logic.Logic_Code_Map import demangle_name, normalize_cpp_name
            norm = normalize_cpp_name(demangle_name(name))
        except Exception:  # noqa: BLE001
            norm = name
        if norm in self.known:
            return norm
        return self._alias.get(norm)


def evaluate(name: str, index: CallIndex, strict: bool = False) -> dict:
    """Verdict + evidence for one matched function name."""
    key = index.resolve(name)
    if key is None:
        return {"verdict": FAILED,
                "reason": f"'{name}' is not in this release's call tree."}
    callers = index.callers.get(key, set())
    if strict:
        callers = {c for c in callers if (c, key) in index.verified}
    callees = index.callees.get(key, set())
    evidence = {
        "callers": [{"name": c, "verified": (c, key) in index.verified}
                    for c in sorted(callers)][:MAX_EVIDENCE_CALLERS],
        "callers_total": len(callers),
        "callees_total": len(callees),
    }
    if callers:
        nver = sum(1 for c in callers if (c, key) in index.verified)
        how = (f", {nver} verified by object files" if index.has_objects
               else "")
        return {"verdict": PASSED,
                "reason": f"Called by {len(callers)} function(s){how}.",
                **evidence}
    if callees:
        return {"verdict": ENTRY_POINT,
                "reason": (f"Not called by any function, but calls {len(callees)} "
                           "— treated as an entry point (OS/startup/scheduler)."),
                **evidence}
    reason = "Not called by any function and calls none."
    if strict:
        reason = "No caller verified by the object files" + (
            "." if index.has_objects else " (no .o files imported for this release).")
    return {"verdict": FAILED, "reason": reason, **evidence}


def run_on_rows(rows: List[dict], col_name: str, binding: dict,
                code_map: Optional[dict],
                object_pairs: Optional[Iterable[Tuple[str, str]]],
                *, port_state_col: Optional[str], tested_at: str) -> Dict[str, int]:
    """Evaluate every row in place. Manual overrides keep their verdict; the
    tool verdict and evidence are refreshed. Returns a verdict histogram."""
    index = CallIndex(code_map, object_pairs) if code_map else None
    built_at = (code_map or {}).get("built_at", "")
    strict = bool(binding.get("strict"))
    counts: Dict[str, int] = {}

    for row in rows:
        state = cell_text(row.get(port_state_col)) if port_state_col else ""
        name = parse_match(cell_text(row.get(binding["match_col"])))
        if state in ("Retired", "Deleted"):
            res = {"verdict": NO_RESULT, "reason": f"Port is {state}."}
        elif not name:
            res = {"verdict": NO_MATCH, "reason": f"'{binding['match_col']}' is empty."}
        elif index is None:
            res = {"verdict": NO_DATA,
                   "reason": "No code map for this release — build it first."}
        elif strict and not index.has_objects:
            res = {"verdict": NO_DATA,
                   "reason": "Strict mode needs the release's .o files imported."}
        else:
            res = evaluate(name, index, strict)

        prev = row.get(col_name) if isinstance(row.get(col_name), dict) else {}
        cell = {
            "auto": res["verdict"],
            "reason": res["reason"],
            "function": name,
            "callers": res.get("callers", []),
            "callers_total": res.get("callers_total", 0),
            "callees_total": res.get("callees_total", 0),
            "tested_at": tested_at,
            "code_map_built_at": built_at,
            "strict": strict,
        }
        if prev.get("user_changed") and prev.get("text") in MANUAL_OPTIONS:
            cell["text"] = prev["text"]
            cell["user_changed"] = True
            for k in ("override_by", "override_at"):
                if prev.get(k):
                    cell[k] = prev[k]
        else:
            cell["text"] = res["verdict"]
        row[col_name] = cell
        counts[cell["text"]] = counts.get(cell["text"], 0) + 1
    return counts


def apply_manual_override(cell: Optional[dict], value: str, *, user: str,
                          at: str) -> dict:
    """A tester's verdict ('' resets to the tool verdict). Raises ValueError
    for an unknown value."""
    base = dict(cell) if isinstance(cell, dict) else {}
    if value == "":
        base["text"] = base.get("auto") or NOT_RUN
        for k in ("user_changed", "override_by", "override_at"):
            base.pop(k, None)
        return base
    if value not in MANUAL_OPTIONS:
        raise ValueError(f"Test verdict must be one of: {', '.join(MANUAL_OPTIONS)}.")
    base["text"] = value
    base["user_changed"] = True
    base["override_by"] = user
    base["override_at"] = at
    return base
