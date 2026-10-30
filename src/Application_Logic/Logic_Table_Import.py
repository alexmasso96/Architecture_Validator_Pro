"""
Import Profiles engine (#8) — declarative rule pipelines for CSV/Excel import.

An Import Profile is a named, ordered list of RULES applied to a parsed table,
followed by a column MAPPING and a write mode (append / match-and-update).
Rules are plain JSON-serializable dicts configured via UI forms — deliberately
no scripting (previewable, shareable, impossible to write a crash).

Rule catalogue (each dict carries "type"):

  {"type": "filter", "action": "keep"|"drop", "match": "all"|"any",
   "conditions": [{"column": C, "op": OP, "value": V}, ...]}

  {"type": "derive", "target": T,
   "cases": [{"column": C, "op": OP, "value": V, "result": R}, ...],
   "default": D}                       # D optional; absent → "" when no case hits

  {"type": "split_cell", "column": C, "delimiter": "::", "segment": N,
   "target": T}                        # N 0-based, negative = from the end

  {"type": "expand", "column": C, "delimiter": "\\n",
   "take_before": ","}                 # one row per part; take_before optional

  {"type": "partition", "column": C}   # group final rows by C (→ one model each)

  {"type": "clean", "ops": [{"op": "trim"|"upper"|"lower"|"replace"|"fill_down",
                             "column": C or "*", "find": F, "replace": R}, ...]}

Condition OPs: contains, not_contains, equals, not_equals, starts_with,
ends_with, regex, is_empty, not_empty. Text ops compare case-insensitively;
regex is used as written.

The reader normalizes openpyxl's ``_x000D_`` carriage-return artifact and
sniffs CSV delimiters (';' Excel locales). All functions are pure — the
sample-session/preview API and the import job both call the same code.
"""
import csv
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CONDITION_OPS = ("contains", "not_contains", "equals", "not_equals",
                 "starts_with", "ends_with", "regex", "is_empty", "not_empty")

RULE_TYPES = ("filter", "derive", "split_cell", "expand", "partition", "clean")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _clean_cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("_x000D_", "\r")


def _read_raw(file_path: str, sheet: Optional[str] = None,
              limit: Optional[int] = None
              ) -> Tuple[List[List[str]], List[str]]:
    """One sheet as raw string lists + all sheet names — ONE file open total
    (EDR machines intercept every open, so sample loading must not multiply
    them)."""
    suffix = Path(file_path).suffix.lower()
    if suffix in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        try:
            sheets = list(wb.sheetnames)
            ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
            raw = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if limit is not None and i >= limit:
                    break
                raw.append([_clean_cell(c) for c in row])
        finally:
            wb.close()
        return raw, sheets
    with open(file_path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(64 * 1024)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(f, dialect)
        raw = []
        for i, row in enumerate(reader):
            if limit is not None and i >= limit:
                break
            raw.append([_clean_cell(c) for c in row])
    return raw, ["CSV"]


def list_sheets(file_path: str) -> List[str]:
    """Sheet names for .xlsx/.xls; a CSV reports a single pseudo-sheet."""
    return _read_raw(file_path, limit=0)[1]


def _build_table(raw: List[List[str]], header_row: int
                 ) -> Tuple[List[str], List[Dict[str, str]]]:
    """(columns, rows-as-dicts) from raw rows. Rows above header_row are
    discarded (title/logo rows). Duplicate/empty headers are disambiguated
    ("", "Name", "Name" → "Column 1", "Name", "Name (2)") so rule configs can
    address every column."""
    if header_row >= len(raw):
        return [], []
    header, data = raw[header_row], raw[header_row + 1:]

    columns: List[str] = []
    seen: Dict[str, int] = {}
    for i, h in enumerate(header):
        name = str(h).strip() or f"Column {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        columns.append(name)

    rows = [{col: (r[i] if i < len(r) else "") for i, col in enumerate(columns)}
            for r in data]
    return columns, rows


def read_table(file_path: str, sheet: Optional[str] = None,
               header_row: int = 0, max_rows: Optional[int] = None
               ) -> Tuple[List[str], List[Dict[str, str]]]:
    """Read one sheet into (columns, rows-as-dicts). One file open."""
    limit = None if max_rows is None else header_row + 1 + max_rows
    raw, _ = _read_raw(file_path, sheet=sheet, limit=limit)
    return _build_table(raw, header_row)


def _suggest_header_from_raw(raw: List[List[str]], scan: int = 10) -> int:
    counts = [sum(1 for c in cells if str(c).strip())
              for cells in raw[:scan]]
    if not counts:
        return 0
    # Earliest row that is "wide enough" (≥ half the widest row in the scan):
    # title banners are narrow and lose; the header wins before any data row
    # is considered. A wrong guess is only a default — the UI can override.
    threshold = max(2, max(counts) / 2)
    for idx, n in enumerate(counts):
        if n >= threshold:
            return idx
    return 0


def suggest_header_row(file_path: str, sheet: Optional[str] = None,
                       scan: int = 10) -> int:
    """Best-guess 0-based header row (title banners lose to real headers)."""
    raw, _ = _read_raw(file_path, sheet=sheet, limit=scan)
    return _suggest_header_from_raw(raw, scan)


def load_sample(file_path: str, sheet: Optional[str] = None,
                header_row: Optional[int] = None,
                cap: int = 1000) -> dict:
    """Load an in-memory sample session in ONE file open: sheet list, header
    guess (when not given), parsed rows capped at ``cap``. Everything the
    live preview needs afterwards is pure in-memory computation."""
    limit = (header_row or 10) + cap + 1
    raw, sheets = _read_raw(file_path, sheet=sheet, limit=limit)
    hdr = header_row if header_row is not None else _suggest_header_from_raw(raw)
    columns, rows = _build_table(raw, hdr)
    return {"sheets": sheets, "sheet": sheet or (sheets[0] if sheets else ""),
            "header_row": hdr, "columns": columns, "rows": rows,
            "truncated": len(raw) >= limit}


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

def match_condition(value: str, op: str, text: str) -> bool:
    v = str(value)
    t = str(text)
    if op == "is_empty":
        return not v.strip()
    if op == "not_empty":
        return bool(v.strip())
    if op == "regex":
        try:
            return re.search(t, v) is not None
        except re.error:
            return False
    lv, lt = v.lower(), t.lower()
    if op == "contains":
        return lt in lv
    if op == "not_contains":
        return lt not in lv
    if op == "equals":
        return lv == lt
    if op == "not_equals":
        return lv != lt
    if op == "starts_with":
        return lv.startswith(lt)
    if op == "ends_with":
        return lv.endswith(lt)
    raise ValueError(f"Unknown condition op: {op!r}")


def _row_matches(row: Dict[str, str], conditions: List[dict],
                 mode: str) -> bool:
    if not conditions:
        return True
    results = (match_condition(row.get(c.get("column", ""), ""),
                               c.get("op", "contains"), c.get("value", ""))
               for c in conditions)
    return all(results) if mode == "all" else any(results)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _apply_filter(rows, rule):
    keep = rule.get("action", "keep") == "keep"
    mode = rule.get("match", "all")
    conds = rule.get("conditions", [])
    return [r for r in rows if _row_matches(r, conds, mode) == keep]


def _apply_derive(rows, rule, columns):
    target = rule["target"]
    if target not in columns:
        columns.append(target)
    default = rule.get("default", "")
    for r in rows:
        result = default
        for case in rule.get("cases", []):
            if match_condition(r.get(case.get("column", ""), ""),
                               case.get("op", "contains"),
                               case.get("value", "")):
                result = case.get("result", "")
                break
        r[target] = result
    return rows


def _apply_split_cell(rows, rule, columns):
    target = rule.get("target") or rule["column"]
    if target not in columns:
        columns.append(target)
    delim = rule.get("delimiter", "::")
    seg = int(rule.get("segment", 0))
    for r in rows:
        parts = str(r.get(rule["column"], "")).split(delim)
        try:
            r[target] = parts[seg].strip()
        except IndexError:
            r[target] = ""
    return rows


def _split_expand_parts(value: str, delimiter: str,
                        take_before: Optional[str]) -> List[str]:
    if not str(value).strip():
        return []
    text = str(value)
    if delimiter in ("\\n", "\n"):
        parts = re.split(r"\r?\n", text)
    else:
        parts = text.split(delimiter)
    out = []
    for p in parts:
        if take_before:
            p = p.split(take_before, 1)[0]
        p = p.strip()
        if p:
            out.append(p)
    return out


def _apply_expand(rows, rule):
    col = rule["column"]
    delim = rule.get("delimiter", "\n")
    take_before = rule.get("take_before")
    out = []
    for r in rows:
        parts = _split_expand_parts(r.get(col, ""), delim, take_before)
        if not parts:
            out.append(r)
            continue
        for p in parts:
            nr = dict(r)
            nr[col] = p
            out.append(nr)
    return out


def _apply_clean(rows, rule, columns):
    for op in rule.get("ops", []):
        kind = op.get("op", "trim")
        targets = columns if op.get("column", "*") == "*" else [op["column"]]
        if kind == "fill_down":
            # Merged cells: openpyxl reports blanks for the continuation
            # cells — carry the last non-empty value downward.
            for col in targets:
                last = ""
                for r in rows:
                    if str(r.get(col, "")).strip():
                        last = r[col]
                    else:
                        r[col] = last
            continue
        for r in rows:
            for col in targets:
                v = str(r.get(col, ""))
                if kind == "trim":
                    r[col] = v.strip()
                elif kind == "upper":
                    r[col] = v.upper()
                elif kind == "lower":
                    r[col] = v.lower()
                elif kind == "replace":
                    r[col] = v.replace(op.get("find", ""), op.get("replace", ""))
    return rows


def apply_pipeline(columns: List[str], rows: List[Dict[str, str]],
                   rules: List[dict]) -> dict:
    """Run the rule pipeline. Returns:

    {"columns": [... incl. derived], "rows": [...], "partition_column": C|None,
     "stages": [{"index", "type", "rows_in", "rows_out"}]}

    Pure and non-destructive (input rows are copied once up front).
    """
    cols = list(columns)
    cur = [dict(r) for r in rows]
    stages = []
    partition_column = None
    for i, rule in enumerate(rules):
        rtype = rule.get("type")
        n_in = len(cur)
        if rtype == "filter":
            cur = _apply_filter(cur, rule)
        elif rtype == "derive":
            cur = _apply_derive(cur, rule, cols)
        elif rtype == "split_cell":
            cur = _apply_split_cell(cur, rule, cols)
        elif rtype == "expand":
            cur = _apply_expand(cur, rule)
        elif rtype == "partition":
            partition_column = rule.get("column")
        elif rtype == "clean":
            cur = _apply_clean(cur, rule, cols)
        else:
            raise ValueError(f"Unknown rule type: {rtype!r}")
        stages.append({"index": i, "type": rtype,
                       "rows_in": n_in, "rows_out": len(cur)})
    return {"columns": cols, "rows": cur,
            "partition_column": partition_column, "stages": stages}


def partition_rows(rows: List[Dict[str, str]],
                   column: Optional[str]) -> Dict[str, List[Dict[str, str]]]:
    """Group rows by the partition column ('' key when blank/missing).
    With no partition column, everything lands under the '' key."""
    if not column:
        return {"": list(rows)}
    out: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        out.setdefault(str(r.get(column, "")).strip(), []).append(r)
    return out


# ---------------------------------------------------------------------------
# Mapping → table rows
# ---------------------------------------------------------------------------

def apply_mapping(rows: List[Dict[str, str]], mapping: Dict[str, str],
                  locked_targets: Optional[set] = None) -> List[dict]:
    """{source col → table col} applied to pipeline output. Cells whose table
    column is in ``locked_targets`` carry import provenance (#7: they render
    locked and no import or edit overwrites them afterwards)."""
    locked = locked_targets or set()
    out = []
    for r in rows:
        row_dict = {}
        for src, tbl in mapping.items():
            cell = {"text": str(r.get(src, ""))}
            if tbl in locked:
                cell["origin"] = "import"
            row_dict[tbl] = cell
        out.append(row_dict)
    return out


# ---------------------------------------------------------------------------
# Match-and-update diff (#7 fill-by-duplicates; #8 pre-import diff)
# ---------------------------------------------------------------------------

def _cell_text(cell) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text", ""))
    return str(cell) if cell is not None else ""


def _cell_locked(cell) -> bool:
    return isinstance(cell, dict) and cell.get("origin") == "import"


def compute_update_diff(existing_rows: List[dict], imported: List[dict],
                        key_columns: List[str],
                        value_columns: List[str]) -> dict:
    """Join imported rows onto existing table rows by ``key_columns``
    (composite-capable; single column is the common case) and stage writes of
    ``value_columns`` into EVERY matching row.

    Returns (nothing is written here):
      updates    — [{"row_index", "changes": {col: {"old", "new"}}}]
      locked     — locked cells whose import value differs (flagged, never applied)
      conflicts  — same key appearing in the import with differing values
      orphans    — imported keys matching no table row
      unmatched  — table-row count the import never touched
    """
    def key_of(cells: dict) -> tuple:
        return tuple(_cell_text(cells.get(k)).strip().lower()
                     for k in key_columns)

    by_key: Dict[tuple, dict] = {}
    conflicts = []
    for imp in imported:
        k = key_of(imp)
        if all(not part for part in k):
            continue
        if k in by_key:
            prior = by_key[k]
            diff_vals = {c for c in value_columns
                         if _cell_text(prior.get(c)) != _cell_text(imp.get(c))}
            if diff_vals:
                conflicts.append({"key": k, "columns": sorted(diff_vals)})
            continue  # first occurrence wins; the conflict is surfaced
        by_key[k] = imp

    updates, locked_flags = [], []
    matched_keys = set()
    unmatched = 0
    for idx, row in enumerate(existing_rows):
        k = key_of(row)
        imp = by_key.get(k)
        if imp is None:
            unmatched += 1
            continue
        matched_keys.add(k)
        changes = {}
        for col in value_columns:
            new = _cell_text(imp.get(col))
            old = _cell_text(row.get(col))
            if new == old:
                continue
            if _cell_locked(row.get(col)):
                locked_flags.append({"row_index": idx, "column": col,
                                     "old": old, "import_value": new})
                continue
            changes[col] = {"old": old, "new": new}
        if changes:
            updates.append({"row_index": idx, "changes": changes})

    orphans = [{"key": k} for k in by_key if k not in matched_keys]
    return {"updates": updates, "locked": locked_flags,
            "conflicts": conflicts, "orphans": orphans,
            "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Built-in profiles — the legacy interface export flows expressed as rule pipelines.
# Read-only in the UI; parity with Logic_Interface_Export is the engine's
# acceptance test. The legacy import path itself stays untouched (#8).
# ---------------------------------------------------------------------------

def interface_export_rules(path_col: str, ops_col: Optional[str] = None,
                   required_col: Optional[str] = None) -> List[dict]:
    rules: List[dict] = [
        {"type": "filter", "action": "keep", "match": "all",
         "conditions": [{"column": path_col, "op": "contains",
                         "value": "P10_SW_Arch_Public"}]},
        {"type": "split_cell", "column": path_col, "delimiter": "::",
         "segment": 2, "target": "__model__"},
        {"type": "partition", "column": "__model__"},
    ]
    if required_col:
        rules.insert(1, {"type": "filter", "action": "keep", "match": "all",
                         "conditions": [{"column": required_col,
                                         "op": "not_empty", "value": ""}]})
    if ops_col:
        rules.append({"type": "expand", "column": ops_col,
                      "delimiter": "\n", "take_before": ","})
    return rules
