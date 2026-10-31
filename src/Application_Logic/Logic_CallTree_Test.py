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

With the model's MAIN FUNCTION(S) set (the module's entry, e.g. its cyclic
``xxxAppl()``; stored per model, asked for on the first run), the test checks
the relationship to them instead:

    1. function not in the call tree                  → Failed
    2. reached from a main function, any depth        → Passed  (main → … → fn)
    3. not reached from any main function             → Failed  (callers named)

None of the main functions in the release's call tree → "No Data".

Rows whose Port State is Retired/Deleted get "No Result" (as the release-result
column does); an empty Match cell or a matched name that is not in the call tree
→ "Not Matched"; no code map for the model and release → "No Data".

Callers come from the code map (source-derived edges) UNION the release's .o
enrichment dataset (text relocations + inline expansions), so a function called
only from assembly — e.g. a bootloader's low-level init invoked from startup
code — is a real caller when the startup .o is imported. A caller is "verified"
when the .o dataset contains that edge. Strict mode counts verified callers only.

A Test column bound to a Variable Search column's Match column checks a
parameter port instead of a call. The search column's title says the direction:
"required" (the value comes in: a parameter of the function, or read by it) or
"provided" (the value goes out: returned, written, or stored through an output
pointer). The function is the row's nearest Function Search match to the left,
and helpers it calls count too. A row with no parameter in that column has
nothing to test, so it reports No Result.

When the release has .o files, their data relocations name each variable a
function references. That confirms an access the source shows, and passes one it
doesn't (a macro, assembly, or no source imported). A relocation can't tell a read
from a write, so such a pass says the direction isn't confirmed.

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
# Project meta: {model_id: [main function names]} — per model, every release.
MAINS_KEY = "model_main_functions"
MAX_MAINS = 10

PASSED = "Passed"
FAILED = "Failed"
ENTRY_POINT = "Entry point"
NOT_MATCHED = "Not Matched"
LEGACY_NOT_MATCHED = "No Match"   # the same verdict in cells recorded by older runs
NO_DATA = "No Data"
NO_RESULT = "No Result"
NOT_RUN = "Not Run"

# Verdicts a tester may set by hand.
MANUAL_OPTIONS = (PASSED, FAILED, ENTRY_POINT)

# Search column types whose Match column holds a FUNCTION (a "call" test is
# meaningless for variables — variable support is a separate follow-up).
_FUNCTION_SEARCH_TYPES = ("Port Search", "Function Search")

# Variable names are often generated, so they match less exactly than functions.
DEFAULT_VARIABLE_SCORE = 70
REQUIRED = "required"
PROVIDED = "provided"
_DIRECTION_RES = {key: re.compile(rf"(?<![a-z]){key}(?![a-z])") for key in (REQUIRED, PROVIDED)}
MAX_ACCESS_NODES = 5000     # functions visited per row when following helpers

MAX_EVIDENCE_CALLERS = 25
MAX_SUGGEST_DEPTH = 10      # caller levels walked up from a port for suggestions
MAX_SUGGEST_NODES = 20000   # nodes visited per port when suggesting

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


def match_score(value: str) -> Optional[int]:
    """'Name (NN%)' → NN; None when the cell holds no score."""
    m = _MATCH_RE.match((value or "").strip())
    return int(m.group(2)) if m else None


def function_match_columns(layout) -> List[str]:
    """Match columns a Test column may bind to: the column right after a Port
    Search / Function Search column. ``layout`` = [(name, type, ...), ...]."""
    out = []
    for i, col in enumerate(layout):
        if col[1] in _FUNCTION_SEARCH_TYPES and i + 1 < len(layout):
            out.append(layout[i + 1][0])
    return out


def column_direction(col_name: str) -> Optional[str]:
    """"required" or "provided" from a column title; None when the title has
    neither, or both."""
    title = (col_name or "").lower()
    found = [key for key, rx in _DIRECTION_RES.items() if rx.search(title)]
    return found[0] if len(found) == 1 else None


def variable_test_spec(layout, match_col: str) -> Optional[dict]:
    """How to test a variable Match column, or None for a function one:
    {function_col, direction}. Read from the layout, so renames just work."""
    names = [c[0] for c in layout]
    if match_col not in names:
        return None
    i = names.index(match_col)
    if i == 0 or layout[i - 1][1] != "Variable Search":
        return None
    left = list(enumerate(layout[:i - 1]))[::-1]
    source = (next((j for j, c in left if c[1] == "Function Search"), None)
              if left else None)
    if source is None:
        source = next((j for j, c in left if c[1] == "Port Search"), None)
    return {"function_col": names[source + 1] if source is not None else None,
            "direction": column_direction(names[i - 1])}


def test_match_columns(layout) -> List[str]:
    """Every Match column a Test column may bind to: functions and variables."""
    return function_match_columns(layout) + [
        c[0] for c in layout if variable_test_spec(layout, c[0]) is not None]


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


def clean_mains(names: Iterable) -> List[str]:
    """Trimmed, de-duplicated main function names (order kept, capped)."""
    out: List[str] = []
    for n in names or ():
        n = str(n or "").strip()
        if n and n not in out:
            out.append(n)
    return out[:MAX_MAINS]


def parse_mains(raw: Optional[str]) -> Dict[int, List[str]]:
    """{model_id: [main function names]} from the project meta value."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    out: Dict[int, List[str]] = {}
    for k, v in (data.items() if isinstance(data, dict) else ()):
        try:
            mid = int(k)
        except (TypeError, ValueError):
            continue
        names = clean_mains(v if isinstance(v, list) else ())
        if names:
            out[mid] = names
    return out


def dump_mains(mains: Dict[int, List[str]]) -> str:
    return json.dumps({str(k): v for k, v in sorted(mains.items()) if v},
                      sort_keys=True)


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
    match_cols = set(test_match_columns(new_layout))

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
            raise ValueError(f"'{b['match_col']}' is not a port, function or "
                             "variable Match column, so it cannot be tested.")
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
        return {"verdict": NOT_MATCHED,
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


class Reach:
    """Everything reachable from a model's main functions, with the shortest
    call chain to each (BFS over callees; verified edges only in strict mode)."""

    def __init__(self, index: CallIndex, mains: Iterable[str], strict: bool = False):
        self.mains = clean_mains(mains)
        self.roots: List[str] = []
        self.missing: List[str] = []
        self.parent: Dict[str, Optional[str]] = {}
        for m in self.mains:
            key = index.resolve(m)
            if key is None:
                self.missing.append(m)
            elif key not in self.parent:
                self.roots.append(key)
                self.parent[key] = None
        queue = list(self.roots)
        for node in queue:                     # grows while iterating: BFS
            for callee in sorted(index.callees.get(node, ())):
                if callee in self.parent:
                    continue
                if strict and (node, callee) not in index.verified:
                    continue
                self.parent[callee] = node
                queue.append(callee)

    def path(self, key: str) -> List[str]:
        """[main, …, key] — empty when ``key`` is not reached."""
        if key not in self.parent:
            return []
        chain = [key]
        while self.parent[chain[-1]] is not None:
            chain.append(self.parent[chain[-1]])
        return chain[::-1]


def evaluate_from_mains(name: str, index: CallIndex, reach: Reach,
                        strict: bool = False) -> dict:
    """Verdict for one matched function relative to the model's main functions:
    Passed when a main function reaches it through any number of calls."""
    key = index.resolve(name)
    if key is None:
        return {"verdict": NOT_MATCHED,
                "reason": f"'{name}' is not in this release's call tree."}
    callers = index.callers.get(key, set())
    if strict:
        callers = {c for c in callers if (c, key) in index.verified}
    evidence = {
        "callers": [{"name": c, "verified": (c, key) in index.verified}
                    for c in sorted(callers)][:MAX_EVIDENCE_CALLERS],
        "callers_total": len(callers),
        "callees_total": len(index.callees.get(key, set())),
    }
    chain = reach.path(key)
    if chain:
        if len(chain) == 1:
            reason = "It is a main function of this model."
        else:
            calls = len(chain) - 1
            reason = (f"Reached from {chain[0]} in {calls} call"
                      f"{'' if calls == 1 else 's'}: {' → '.join(chain)}.")
        return {"verdict": PASSED, "reason": reason, "main_path": chain, **evidence}
    mains = ", ".join(reach.roots)
    edge = "a verified call path" if strict else "any call path"
    if callers:
        shown = sorted(callers)[:5]
        more = f" and {len(callers) - len(shown)} more" if len(callers) > len(shown) else ""
        reason = (f"Not reached from {mains} by {edge}; called only by "
                  f"{', '.join(shown)}{more}, outside that call tree.")
    else:
        reason = f"Not reached from {mains} by {edge}; no function calls it."
    return {"verdict": FAILED, "reason": reason, **evidence}


def _access(names: Optional[Iterable[str]], variable: str) -> Optional[str]:
    """The access in ``names`` that reaches ``variable``, field paths included."""
    for name in names or ():
        if (name == variable or name.startswith((variable + ".", variable + "->", variable + "["))
                or name.endswith(("." + variable, "->" + variable))):
            return name
    return None


def _is_output_pointer(param: dict) -> bool:
    ptype = str(param.get("type") or "")
    return "*" in ptype and "const" not in ptype.split("*")[0].split()


def _reachable(start: str, index: CallIndex) -> List[Tuple[str, List[str]]]:
    """[(function, chain from start)], closest first."""
    chains = {start: [start]}
    order = [start]
    for fname in order:
        for callee in sorted(index.callees.get(fname, ())):
            if callee not in chains and len(order) < MAX_ACCESS_NODES:
                chains[callee] = chains[fname] + [callee]
                order.append(callee)
    return [(fname, chains[fname]) for fname in order]


def evaluate_variable(variable: str, function: str, direction: str, index: CallIndex,
                      functions: Dict[str, dict], globals_: Set[str],
                      object_refs: Optional[Set[Tuple[str, str]]] = None) -> dict:
    """Is ``variable`` used by ``function``, or a helper it calls, the way the
    port's direction says? ``object_refs`` = the release's (function, variable)
    pairs from .o data relocations; empty without them."""
    key = index.resolve(function)
    if key is None:
        return {"verdict": NOT_MATCHED,
                "reason": f"'{function}' is not in this release's call tree."}
    params = (functions.get(key) or {}).get("parameters") or []
    param = next((p for p in params if p.get("name") == variable), None)
    if param is not None:
        goes_out = _is_output_pointer(param)
        if (direction == PROVIDED) == goes_out:
            how = "an output pointer" if goes_out else "a parameter"
            return {"verdict": PASSED, "access": "parameter",
                    "reason": f"{variable} is {how} of {key}.", "access_path": [key]}
        if direction == REQUIRED:
            return {"verdict": FAILED,
                    "reason": f"Wrong direction: {variable} is an output pointer of "
                              f"{key}, but the port requires it."}
        # A provided input parameter may still be returned: keep looking.

    root = re.split(r"\.|->|\[", variable, maxsplit=1)[0]
    chain = _reachable(key, index)

    def seen_as_variable() -> bool:
        # Reads also catch macros and enum values, so only a written name or a
        # struct field proves it's a variable when the ELF doesn't list it.
        for fname, _path in chain:
            fdata = functions.get(fname) or {}
            if _access(fdata.get("writes_vars"), variable):
                return True
            hit = _access(fdata.get("reads_vars"), variable)
            if hit and hit != variable:
                return True
        return False

    def referenced():
        for fname, path in chain:
            if object_refs and (fname, root) in object_refs:
                return root, fname, path
        return None

    if (globals_ and root not in globals_ and param is None and not seen_as_variable()
            and not referenced()):
        return {"verdict": NOT_MATCHED,
                "reason": f"{variable} is not a variable in this release."}

    def find(field: str):
        for fname, path in chain:
            hit = _access((functions.get(fname) or {}).get(field), variable)
            if hit:
                return hit, fname, path
        return None

    def passed(hit, what: str) -> dict:
        name, fname, path = hit
        route = f" (through {' → '.join(path)})" if len(path) > 1 else ""
        confirmed = " Confirmed by its object file." if (fname, root) in (object_refs or ()) else ""
        return {"verdict": PASSED, "access": what, "access_path": path,
                "reason": f"{fname} {what} {name}{route}.{confirmed}"}

    if not object_refs and (functions.get(key) or {}).get("returns_vars") is None:
        return {"verdict": NO_DATA,
                "reason": f"No source data for {key}: import the release's source "
                          "and rebuild its Code Map."}
    reads, writes, returns = find("reads_vars"), find("writes_vars"), find("returns_vars")
    if direction == REQUIRED:
        if reads:
            return passed(reads, "reads")
        if writes:
            return {"verdict": FAILED,
                    "reason": f"Wrong direction: {writes[1]} only writes {variable}, "
                              "but the port requires it."}
    else:
        if returns:
            return passed(returns, "returns")
        if writes:
            return passed(writes, "writes")
        if reads:
            return {"verdict": FAILED,
                    "reason": f"Wrong direction: {reads[1]} only reads {variable}, "
                              "but the port provides it."}
    hit = referenced()
    if hit:
        name, fname, path = hit
        route = f" (through {' → '.join(path)})" if len(path) > 1 else ""
        return {"verdict": PASSED, "access": "references", "access_path": path,
                "reason": f"{fname} references {name} in its object file{route}. "
                          "The source doesn't show how, so the direction isn't confirmed."}
    cut = (f" Only the first {MAX_ACCESS_NODES} functions it calls were checked."
           if len(chain) >= MAX_ACCESS_NODES else "")
    return {"verdict": FAILED, "reason": f"{key} does not use {variable}.{cut}"}


def _evaluate_variable_row(row: dict, variable_test: dict, index: Optional[CallIndex],
                           functions: Dict[str, dict], binding: dict,
                           min_match_score: Optional[int]) -> dict:
    """The verdict for one row of a variable Test column, and the names used."""
    match_text = cell_text(row.get(binding["match_col"]))
    variable = parse_match(match_text)
    source = cell_text(row.get(variable_test.get("search_col")))
    function_text = cell_text(row.get(variable_test.get("function_col")))
    function = parse_match(function_text)
    names = {"function": function, "variable": variable,
             "direction": variable_test.get("direction") or ""}
    match_cell = row.get(binding["match_col"])
    picked = isinstance(match_cell, dict) and match_cell.get("user_changed")
    score = match_score(match_text)
    minimum = variable_test.get("min_score", DEFAULT_VARIABLE_SCORE)
    function_score = match_score(function_text)
    if not source and not variable:
        res = {"verdict": NO_RESULT, "reason": "This port has no parameter in this column."}
    elif not variable:
        res = {"verdict": NOT_MATCHED, "reason": f"'{source}' has no match."}
    elif score is not None and not picked and score < minimum:
        res = {"verdict": NOT_MATCHED,
               "reason": f"Best match {variable} scored {score}%, below the "
                         f"{minimum}% threshold."}
    elif not function:
        res = {"verdict": NOT_MATCHED, "reason": "This row has no matched function."}
    elif (min_match_score is not None and function_score is not None
          and function_score < min_match_score):
        res = {"verdict": NOT_MATCHED,
               "reason": f"Best function match {function} scored {function_score}%, "
                         f"below the {min_match_score}% threshold."}
    elif not variable_test.get("direction"):
        res = {"verdict": NO_DATA,
               "reason": f"Add \"required\" or \"provided\" to the title of "
                         f"'{variable_test.get('search_col')}' to set the direction."}
    elif index is None:
        res = {"verdict": NO_DATA,
               "reason": "No code map for this release — build it first."}
    else:
        res = evaluate_variable(variable, function, variable_test["direction"], index,
                                functions, variable_test.get("globals") or set(),
                                variable_test.get("object_refs"))
    return {**res, **names}


def suggest_mains(index: CallIndex, functions: Iterable[str],
                  limit: int = 5) -> List[dict]:
    """Likely main functions for a model: the functions that reach the most
    of its matched port functions, closest first — the common ancestor of the
    module's ports rather than the OS task above it.
    [{name, covers, total}]"""
    keys = []
    for f in functions:
        k = index.resolve(f) if f else None
        if k and k not in keys:
            keys.append(k)
    covers: Dict[str, int] = {}
    dist: Dict[str, int] = {}
    for k in keys:
        seen = {k: 0}
        queue = [k]
        for node in queue:
            d = seen[node]
            if d >= MAX_SUGGEST_DEPTH or len(seen) >= MAX_SUGGEST_NODES:
                continue
            for c in index.callers.get(node, ()):
                if c not in seen:
                    seen[c] = d + 1
                    queue.append(c)
        for node, d in seen.items():
            covers[node] = covers.get(node, 0) + 1
            dist[node] = dist.get(node, 0) + d
    # A port function that reaches only itself is no suggestion.
    own = set(keys)
    ranked = sorted((n for n in covers if not (n in own and covers[n] == 1)),
                    key=lambda n: (-covers[n], dist[n] / covers[n], n))
    return [{"name": n, "covers": covers[n], "total": len(keys)}
            for n in ranked[:limit]]


def run_on_rows(rows: List[dict], col_name: str, binding: dict,
                code_map: Optional[dict],
                object_pairs: Optional[Iterable[Tuple[str, str]]],
                *, port_state_col: Optional[str], tested_at: str,
                mains: Optional[Iterable[str]] = None,
                min_match_score: Optional[int] = None,
                variable_test: Optional[dict] = None) -> Dict[str, int]:
    """Evaluate every row in place. Manual overrides keep their verdict; the
    tool verdict and evidence are refreshed. Returns a verdict histogram.

    ``mains`` = the model's main functions: when given, a row passes only if
    one of them reaches its function (see ``evaluate_from_mains``).
    ``min_match_score`` = a scored match below it counts as Not Matched; a Match
    cell the user picked by hand always counts.
    ``variable_test`` = a variable column's ``variable_test_spec`` plus its
    search_col, min_score, and the release's globals and object_refs."""
    index = CallIndex(code_map, object_pairs) if code_map else None
    built_at = (code_map or {}).get("built_at", "")
    strict = bool(binding.get("strict"))
    main_list = clean_mains(mains) if mains is not None else None
    reach = Reach(index, main_list, strict) if index is not None and main_list else None
    counts: Dict[str, int] = {}
    functions = (code_map or {}).get("functions") or {}

    for row in rows:
        state = cell_text(row.get(port_state_col)) if port_state_col else ""
        match_cell = row.get(binding["match_col"])
        match_text = cell_text(match_cell)
        name = parse_match(match_text)
        score = match_score(match_text)
        picked = isinstance(match_cell, dict) and match_cell.get("user_changed")
        if state in ("Retired", "Deleted"):
            res = {"verdict": NO_RESULT, "reason": f"Port is {state}."}
        elif variable_test is not None:
            res = _evaluate_variable_row(row, variable_test, index, functions, binding,
                                         min_match_score)
            name = res["function"]
        elif not name:
            res = {"verdict": NOT_MATCHED,
                   "reason": f"'{binding['match_col']}' is empty."}
        elif (min_match_score is not None and score is not None and not picked
              and score < min_match_score):
            res = {"verdict": NOT_MATCHED,
                   "reason": f"Best match {name} scored {score}%, below the "
                             f"{min_match_score}% threshold."}
        elif index is None:
            res = {"verdict": NO_DATA,
                   "reason": "No code map for this release — build it first."}
        elif strict and not index.has_objects:
            res = {"verdict": NO_DATA,
                   "reason": "Strict mode needs the release's .o files imported."}
        elif main_list is not None and not (reach and reach.roots):
            res = {"verdict": NO_DATA,
                   "reason": ("Main function " + ", ".join(main_list or ["(none set)"])
                              + " is not in this release's call tree.")}
        elif reach is not None:
            res = evaluate_from_mains(name, index, reach, strict)
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
        if main_list is not None:
            cell["mains"] = main_list
            cell["main_path"] = res.get("main_path", [])
        if variable_test is not None:
            for key in ("variable", "direction", "access", "access_path"):
                if res.get(key):
                    cell[key] = res[key]
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
