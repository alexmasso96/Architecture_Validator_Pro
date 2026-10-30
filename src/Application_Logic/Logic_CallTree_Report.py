"""
Call-tree Test column → one self-contained HTML report (inline CSS/JS/SVG,
no external assets), for handing a verdict record to a reviewer or auditor.

Layout: a header (optional logo, project, release, Test column, operator,
date), an overall results table with the score, a per-model score table, then
one chapter per architecture model listing every valid result row of that
model's table, followed by the model's call tree from its main function(s)
down to its ports (only the paths that reach a port) and the ports not
reached. Clicking an entry opens its detail view inside the same file
(hash-routed ``#e-<n>`` sections): why the verdict is what it is — the stored
reason and caller evidence, any manual override — and a snapshot of the call
tree around the function (the call path from an entry point as an SVG chain,
plus nested caller / callee trees), taken from the code map at report time.

Pure module: no DB, HTTP or file I/O. ``build_report`` gathers the data from
plain inputs; ``render_html`` turns it into the document.
"""
from __future__ import annotations

import base64
import html
import json
import re
from collections import deque
from typing import Dict, Iterable, List, Optional, Tuple

from Application_Logic import Logic_CallTree_Test as ct

CALLER_DEPTH = 3       # levels of callers shown in a snapshot
CALLEE_DEPTH = 2       # levels of callees shown in a snapshot
TREE_BREADTH = 12      # children shown per tree node (the rest are counted)
MAX_PATH_SEARCH = 5000  # nodes visited when looking for an entry-point path

# Rows that are not valid test results: retired/deleted ports and rows the
# column was never run on.
_EXCLUDED = (ct.NO_RESULT, ct.NOT_RUN, "")

_VERDICT_CLASS = {
    ct.PASSED: "pass", ct.FAILED: "fail", ct.ENTRY_POINT: "entry",
    ct.NOT_MATCHED: "warn", ct.NO_DATA: "warn",
}


# ---------------------------------------------------------------------------
# Call-tree snapshot
# ---------------------------------------------------------------------------

def _tree(index: ct.CallIndex, start: str, edges: Dict[str, set], depth: int,
          verified_dir: str) -> List[dict]:
    """Nested [{name, verified, children, more, cycle}] following ``edges``
    (callers or callees) from ``start`` up to ``depth`` levels."""
    def walk(node: str, level: int, seen: frozenset) -> List[dict]:
        out = []
        nxt = sorted(edges.get(node, ()))
        for child in nxt[:TREE_BREADTH]:
            pair = (child, node) if verified_dir == "callers" else (node, child)
            item = {"name": child, "verified": pair in index.verified,
                    "children": [], "more": 0, "cycle": child in seen}
            if level < depth and not item["cycle"]:
                item["children"] = walk(child, level + 1, seen | {child})
            elif not item["cycle"]:
                item["more"] = len(edges.get(child, ()))
            out.append(item)
        if len(nxt) > TREE_BREADTH:
            out.append({"name": f"… {len(nxt) - TREE_BREADTH} more", "verified": False,
                        "children": [], "more": 0, "cycle": False, "ellipsis": True})
        return out
    return walk(start, 1, frozenset({start}))


def _entry_path(index: ct.CallIndex, key: str) -> List[str]:
    """Shortest caller chain from a root (a function nobody calls) down to
    ``key`` — [root, …, key]. Just [key] when it has no callers; the longest
    chain found when every route loops."""
    if not index.callers.get(key):
        return [key]
    prev: Dict[str, Optional[str]] = {key: None}
    queue = deque([key])
    last = key
    while queue and len(prev) < MAX_PATH_SEARCH:
        node = queue.popleft()
        last = node
        callers = sorted(index.callers.get(node, ()))
        if not callers:
            break
        for c in callers:
            if c not in prev:
                prev[c] = node
                queue.append(c)
    chain = [last]
    while prev.get(chain[-1]) is not None:
        chain.append(prev[chain[-1]])
    return chain


def call_tree_snapshot(index: Optional[ct.CallIndex], function: str) -> dict:
    """What the report draws for one function, from the current code map."""
    if index is None or not function:
        return {"found": False, "key": None}
    key = index.resolve(function)
    if key is None:
        return {"found": False, "key": None}
    path = _entry_path(index, key)
    return {
        "found": True,
        "key": key,
        "path": path,
        "path_verified": [(path[i], path[i + 1]) in index.verified
                          for i in range(len(path) - 1)],
        "callers": _tree(index, key, index.callers, CALLER_DEPTH, "callers"),
        "callees": _tree(index, key, index.callees, CALLEE_DEPTH, "callees"),
        "callers_total": len(index.callers.get(key, ())),
        "callees_total": len(index.callees.get(key, ())),
    }


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

# Preferences key (per-user prefs store) holding the operator details shown in
# the report header: a JSON list of {"label", "value"} — e.g. Operator, Email,
# Team. Without any, the report names the computer's user account.
OPERATOR_PREF_KEY = "arch.report.operator"
MAX_OPERATOR_FIELDS = 12
MAX_OPERATOR_TEXT = 200


def parse_operator_fields(raw: Optional[str]) -> List[Tuple[str, str]]:
    """[(label, value)] from the stored preference — filled-in fields only,
    trimmed and length-capped; anything malformed is ignored."""
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or "").strip()[:MAX_OPERATOR_TEXT]
        value = str(it.get("value") or "").strip()[:MAX_OPERATOR_TEXT]
        if value:
            out.append((label or "Operator", value))
    return out[:MAX_OPERATOR_FIELDS]


# Preferences key holding the report logo: an image data URI (PNG/JPEG/GIF/
# WebP/SVG), shown in the top-right corner of the report header.
LOGO_PREF_KEY = "arch.report.logo"
MAX_LOGO_CHARS = 1_500_000
_LOGO_RE = re.compile(r"data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,"
                      r"[A-Za-z0-9+/]+={0,2}")


def parse_logo(raw: Optional[str]) -> Optional[str]:
    """The stored logo data URI, or None when unset, too large or not an
    inline image (the report stays self-contained: never a file or URL)."""
    s = (raw or "").strip()
    if not s or len(s) > MAX_LOGO_CHARS or not _LOGO_RE.fullmatch(s):
        return None
    return s


MAX_LOGO_BYTES = 1024 * 1024
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def logo_data_uri(raw: bytes) -> str:
    """An image file's bytes → the data URI stored as the report logo. The
    type comes from the content, not the file name. Raises ValueError for
    anything that isn't a PNG/JPEG/GIF/WebP/SVG image or is over 1 MB."""
    if not raw:
        raise ValueError("The file is empty.")
    if len(raw) > MAX_LOGO_BYTES:
        raise ValueError("The logo is larger than 1 MB — use a smaller image.")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "jpeg"
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        mime = "gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "webp"
    elif b"<svg" in raw[:4096].lower():
        mime = "svg+xml"
    else:
        raise ValueError("Not a PNG, JPEG, GIF, WebP or SVG image.")
    return f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")


# Verdicts that count towards the score: the function is called, or it is an
# entry point started by the OS / scheduler / startup code.
PASSING = (ct.PASSED, ct.ENTRY_POINT)


def score(counts: Dict[str, int]) -> dict:
    """Totals for a verdict count map. ``score`` = passing results as a
    percentage of all valid results (None when there are none)."""
    total = sum(counts.values())
    passing = sum(counts.get(k, 0) for k in PASSING)
    failed = counts.get(ct.FAILED, 0)
    return {"total": total, "passed": counts.get(ct.PASSED, 0),
            "entry": counts.get(ct.ENTRY_POINT, 0), "failed": failed,
            "other": total - passing - failed, "passing": passing,
            "score": (100.0 * passing / total) if total else None}


def port_name_column(layout, match_col: str) -> Optional[str]:
    """The search column feeding ``match_col`` (the column just before it)."""
    names = [c[0] for c in layout]
    if match_col in names:
        i = names.index(match_col)
        if i > 0:
            return names[i - 1]
    return None


def build_report(*, project: str, release: str, col_name: str, binding: dict,
                 author: str, created_at: str, layout,
                 operator: Optional[List[Tuple[str, str]]] = None,
                 logo: Optional[str] = None,
                 models: Iterable[Tuple[str, List[dict], Optional[dict]]],
                 object_pairs: Optional[Iterable[Tuple[str, str]]] = None,
                 port_state_col: Optional[str] = None,
                 components: Optional[List[str]] = None) -> dict:
    """The report as plain data. ``models`` = [(model name, rows, code map
    [, main functions])].

    ``operator`` = [(label, value)] from Preferences → Reports; without it the
    header names ``author`` (the computer's user account) as the operator.
    ``logo`` = an image data URI (see ``parse_logo``) for the header.

    Valid result rows are rows with a recorded verdict in ``col_name`` that is
    not "No Result" (retired/deleted port) or "Not Run"."""
    port_col = port_name_column(layout, binding.get("match_col", ""))
    pairs = set(object_pairs) if object_pairs is not None else None
    totals: Dict[str, int] = {}
    chapters = []
    n = 0
    for model in models:
        model_name, rows, code_map = model[:3]
        mains = ct.clean_mains(model[3]) if len(model) > 3 and model[3] else []
        index = ct.CallIndex(code_map, pairs) if code_map else None
        built_at = (code_map or {}).get("built_at", "")
        entries, counts = [], {}
        skipped = 0
        for row_no, row in enumerate(rows, 1):
            cell = row.get(col_name)
            verdict = (cell.get("text") or "") if isinstance(cell, dict) else ""
            if verdict == ct.LEGACY_NOT_MATCHED:
                verdict = ct.NOT_MATCHED
            if verdict in _EXCLUDED:
                skipped += 1
                continue
            n += 1
            function = cell.get("function") or ct.parse_match(
                ct.cell_text(row.get(binding.get("match_col"))))
            stale = bool(built_at and cell.get("code_map_built_at")
                         and cell["code_map_built_at"] != built_at)
            cell_mains = list(cell.get("mains") or [])
            mains_changed = bool(cell_mains and mains and cell_mains != mains)
            entries.append({
                "id": f"e-{n}",
                "row": row_no,
                "port": ct.cell_text(row.get(port_col)) if port_col else "",
                "port_state": ct.cell_text(row.get(port_state_col)) if port_state_col else "",
                "function": function,
                "verdict": verdict,
                "auto": cell.get("auto") or "",
                "reason": cell.get("reason") or "",
                "manual": bool(cell.get("user_changed")),
                "override_by": cell.get("override_by") or "",
                "override_at": cell.get("override_at") or "",
                "callers": list(cell.get("callers") or []),
                "callers_total": int(cell.get("callers_total") or 0),
                "callees_total": int(cell.get("callees_total") or 0),
                "tested_at": cell.get("tested_at") or "",
                "code_map_built_at": cell.get("code_map_built_at") or "",
                "strict": bool(cell.get("strict")),
                "stale": stale,
                "mains_changed": mains_changed,
                "main_path": [str(x) for x in cell.get("main_path") or []],
                "mains": cell_mains,
                "component": (((code_map or {}).get("functions") or {})
                              .get(function) or {}).get("component", ""),
                "snapshot": call_tree_snapshot(index, function),
            })
            counts[verdict] = counts.get(verdict, 0) + 1
            totals[verdict] = totals.get(verdict, 0) + 1
        chapters.append({"model": model_name, "entries": entries, "counts": counts,
                         "score": score(counts), "skipped": skipped, "mains": mains,
                         "tree": main_tree(entries, index),
                         "unreached": [e["id"] for e in entries if not e["main_path"]], "has_code_map": bool(code_map),
                         "code_map_built_at": built_at})
    return {
        "project": project, "release": release, "col_name": col_name,
        "match_col": binding.get("match_col", ""), "strict": bool(binding.get("strict")),
        "has_objects": pairs is not None, "author": author, "created_at": created_at,
        "operator": list(operator or []) or [("Operator", author or "—")],
        "components": list(components or []),
        "logo": parse_logo(logo),
        "chapters": chapters, "totals": totals, "score": score(totals),
        "entry_count": n,
    }


def main_tree(entries: List[dict], index: Optional[ct.CallIndex]) -> List[dict]:
    """The model's call tree from its main function(s), keeping only the
    recorded main → … → port paths (merged where they share a prefix):
    [{name, verified, ports: [entry ids], children: [...]}] per main."""
    roots: Dict[str, dict] = {}
    for e in entries:
        path = e["main_path"]
        if not path:
            continue
        level, parent = roots, None
        for name in path:
            node = level.get(name)
            if node is None:
                node = level[name] = {
                    "name": name, "ports": [], "kids": {},
                    "verified": bool(index and parent and (parent, name) in index.verified)}
            level, parent = node["kids"], name
        node["ports"].append(e["id"])

    def done(nodes: Dict[str, dict]) -> List[dict]:
        return [{"name": n["name"], "verified": n["verified"], "ports": n["ports"],
                 "children": done(n["kids"])}
                for n in sorted(nodes.values(), key=lambda n: n["name"].lower())]
    return done(roots)


def report_filename(project: str, release: str, col_name: str, date: str) -> str:
    """A filesystem-safe default name for the saved report."""
    raw = f"CallTree Report {project} {release} {col_name} {date[:10]}"
    safe = re.sub(r"[^\w.\- ]+", "_", raw).strip()
    return re.sub(r"\s+", "_", safe) + ".html"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _badge(verdict: str) -> str:
    return (f'<span class="v v-{_VERDICT_CLASS.get(verdict, "warn")}">'
            f'{_e(verdict)}</span>')


def _counts_line(counts: Dict[str, int]) -> str:
    if not counts:
        return '<span class="muted">no results</span>'
    order = [ct.PASSED, ct.ENTRY_POINT, ct.FAILED, ct.NOT_MATCHED, ct.NO_DATA]
    keys = [k for k in order if k in counts] + sorted(k for k in counts if k not in order)
    return " ".join(f'{_badge(k)}<b>{counts[k]}</b>' for k in keys)


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.1f} %"


def _share(n: int, total: int) -> str:
    return _pct(100.0 * n / total) if total else "—"


def _score_cell(value: Optional[float]) -> str:
    """A percentage with a small bar, coloured by how much passed."""
    if value is None:
        return '<span class="muted">—</span>'
    cls = "good" if value >= 90 else "mid" if value >= 60 else "low"
    return (f'<span class="score"><span class="bar"><i class="{cls}" '
            f'style="width:{value:.1f}%"></i></span><b>{_pct(value)}</b></span>')


def _overall_html(data: dict) -> str:
    """Overall results: one row per verdict (count + share) and the score."""
    sc, totals = data["score"], data["totals"]
    order = [ct.PASSED, ct.ENTRY_POINT, ct.FAILED, ct.NOT_MATCHED, ct.NO_DATA]
    keys = [k for k in order if k in (ct.PASSED, ct.FAILED) or totals.get(k)]
    keys += sorted(k for k in totals if k not in order)
    body = "".join(
        f'<tr><td>{_badge(k)}</td><td class="num">{totals.get(k, 0)}</td>'
        f'<td class="num">{_share(totals.get(k, 0), sc["total"])}</td></tr>'
        for k in keys)
    table = (f'<table class="grid overall"><thead><tr><th>Result</th>'
             f'<th class="num">Test cases</th><th class="num">Share</th></tr></thead>'
             f'<tbody>{body}</tbody><tfoot><tr><th>Total</th>'
             f'<th class="num">{sc["total"]}</th>'
             f'<th class="num">{"100.0 %" if sc["total"] else "—"}</th></tr></tfoot></table>')
    tile = (f'<div class="tile"><div class="tile-k">Overall score</div>'
            f'<div class="tile-v">{_pct(sc["score"])}</div>{_score_cell(sc["score"])}'
            f'<div class="muted small">{sc["passing"]} of {sc["total"]} passed'
            f'{" (Passed + Entry point)" if sc["entry"] else ""}</div></div>')
    return f'<div class="overall-wrap">{tile}{table}</div>'


def _models_html(data: dict) -> str:
    """Per-model table: counts and score, each model linking to its chapter."""
    t = data["score"]
    entry = bool(t["entry"])    # never with main functions set: hide the column

    def ent(n, tag="td"):
        return f'<{tag} class="num">{n}</{tag}>' if entry else ""
    rows = []
    for c in data["chapters"]:
        s = c["score"]
        mains = ("<br>".join(f"<code>{_e(m)}()</code>" for m in c["mains"])
                 or '<span class="muted">—</span>')
        rows.append(
            f'<tr><td><a href="#{_e(_chapter_id(c))}">{_e(c["model"])}</a></td>'
            f'<td>{mains}</td>'
            f'<td class="num">{s["total"]}</td><td class="num">{s["passed"]}</td>'
            f'{ent(s["entry"])}<td class="num">{s["failed"]}</td>'
            f'<td class="num">{s["other"]}</td><td>{_score_cell(s["score"])}</td></tr>')
    foot = (f'<tr><th>All models</th><th></th><th class="num">{t["total"]}</th>'
            f'<th class="num">{t["passed"]}</th>{ent(t["entry"], "th")}'
            f'<th class="num">{t["failed"]}</th><th class="num">{t["other"]}</th>'
            f'<th>{_score_cell(t["score"])}</th></tr>')
    return (f'<div class="scroll"><table class="grid models"><thead><tr><th>Model</th>'
            f'<th>Main function</th>'
            f'<th class="num">Results</th><th class="num">Passed</th>'
            f'{'<th class="num">Entry point</th>' if entry else ""}<th class="num">Failed</th>'
            f'<th class="num" title="Not Matched / No Data">Other</th><th>Score</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody><tfoot>{foot}</tfoot>'
            f'</table></div>')


def _count_ports(node: dict) -> int:
    return len(node["ports"]) + sum(_count_ports(c) for c in node["children"])


def _main_tree_html(chapter: dict) -> str:
    """The chapter's call tree from its main function(s) — an Eclipse-style
    call hierarchy, collapsible, with each port's verdict on its function."""
    by_id = {e["id"]: e for e in chapter["entries"]}
    mains = chapter["mains"]

    def node_html(nd: dict, depth: int) -> str:
        tags = []
        if depth == 0:
            tags.append('<span class="tag main">main</span>')
        if nd["verified"]:
            tags.append('<span class="ok" title="Call confirmed by object files">✓</span>')
        ports = "".join(
            f'<a class="port" href="#{e["id"]}">{_badge(e["verdict"])}'
            f'{_e(e["port"] or "Row " + str(e["row"]))}</a>'
            for e in (by_id[i] for i in nd["ports"]))
        label = (f'<span class="fn{" is-port" if nd["ports"] else ""}">'
                 f'<code>{_e(nd["name"])}()</code>{"".join(tags)}{ports}</span>')
        if not nd["children"]:
            return f'<li class="leaf">{label}</li>'
        n = _count_ports(nd) - len(nd["ports"])
        hint = f'<span class="muted small"> {n} port{"" if n == 1 else "s"} below</span>'
        kids = "".join(node_html(c, depth + 1) for c in nd["children"])
        return (f'<li><details open><summary>{label}{hint}</summary>'
                f'<ul>{kids}</ul></details></li>')

    title = ", ".join(f"{m}()" for m in mains) or "the main function"
    if not mains:
        body = ('<p class="muted">No main function was set for this model when the test '
                'ran, so there is no call tree to draw.</p>')
    elif not chapter["tree"]:
        body = '<p class="muted">No port of this model is reached from the main function.</p>'
    else:
        body = (f'<ul class="ctree">{"".join(node_html(n, 0) for n in chapter["tree"])}</ul>')

    unreached = [by_id[i] for i in chapter["unreached"]] if mains else []
    if unreached:
        rows = "".join(
            f'<tr class="entry" data-e="{e["id"]}"><td><a href="#{e["id"]}">'
            f'{_e(e["port"]) or "—"}</a></td><td><code>{_e(e["function"]) or "—"}</code></td>'
            f'<td>{_badge(e["verdict"])}</td><td class="muted">{_e(e["reason"])}</td></tr>'
            for e in unreached)
        body += (f'<h4>Not reached from {_e(title)} ({len(unreached)})</h4>'
                 f'<div class="scroll"><table class="grid results"><thead><tr><th>Port</th>'
                 f'<th>Function</th><th>Verdict</th><th>Reason</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table></div>')
    return (f'<div class="calltree"><h3>Call tree from {_e(title)}</h3>'
            f'<p class="muted small">Only the calls that lead from the main function to '
            f'a port of this model; click a port to open its result.</p>{body}</div>')


def _tree_html(nodes: List[dict], target: str, arrow: str) -> str:
    if not nodes:
        return '<p class="muted">none</p>'
    def items(ns):
        out = []
        for nd in ns:
            if nd.get("ellipsis"):
                out.append(f'<li class="muted">{_e(nd["name"])}</li>')
                continue
            flags = ""
            if nd["verified"]:
                flags += ' <span class="ok" title="Edge confirmed by object files">✓ verified</span>'
            if nd["cycle"]:
                flags += ' <span class="muted">(cycle)</span>'
            if nd["more"]:
                flags += f' <span class="muted">+{nd["more"]} {arrow}</span>'
            sub = f"<ul>{items(nd['children'])}</ul>" if nd["children"] else ""
            out.append(f'<li><code>{_e(nd["name"])}</code>{flags}{sub}</li>')
        return "".join(out)
    return f'<ul class="tree"><li><code class="hl">{_e(target)}</code><ul>{items(nodes)}</ul></li></ul>'


def _path_svg(path: List[str], verified: List[bool]) -> str:
    """The entry-point → function call chain as a vertical box diagram."""
    box_w, box_h, gap = 380, 30, 22
    h = len(path) * box_h + (len(path) - 1) * gap + 4
    parts = [f'<svg class="path" viewBox="0 0 {box_w + 4} {h}" width="{box_w + 4}" '
             f'height="{h}" role="img" aria-label="Call path">']
    for i, name in enumerate(path):
        y = 2 + i * (box_h + gap)
        last = i == len(path) - 1
        label = name if len(name) <= 46 else name[:45] + "…"
        parts.append(
            f'<rect x="2" y="{y}" width="{box_w}" height="{box_h}" rx="6" '
            f'class="{"node hl" if last else "node"}"><title>{_e(name)}</title></rect>'
            f'<text x="{2 + box_w / 2}" y="{y + box_h / 2 + 4}" text-anchor="middle">'
            f'{_e(label)}</text>')
        if not last:
            y1, y2 = y + box_h, y + box_h + gap
            cls = "edge ver" if verified[i] else "edge"
            parts.append(
                f'<line x1="{2 + box_w / 2}" y1="{y1}" x2="{2 + box_w / 2}" y2="{y2 - 5}" '
                f'class="{cls}"/><path d="M{box_w / 2 - 3} {y2 - 7} L{2 + box_w / 2} {y2} '
                f'L{box_w / 2 + 7} {y2 - 7} Z" class="{cls} head"/>')
    parts.append("</svg>")
    return "".join(parts)


def _detail_html(entry: dict, chapter: dict, data: dict) -> str:
    s = entry["snapshot"]
    rows = [
        ("Model", _e(chapter["model"])),
        ("Port", _e(entry["port"]) or '<span class="muted">—</span>'),
        ("Matched function", f'<code>{_e(entry["function"]) or "—"}</code>'),
        ("Verdict", _badge(entry["verdict"])),
    ]
    if entry["mains"]:
        rows.append(("Main function", ", ".join(f"<code>{_e(m)}()</code>"
                                                for m in entry["mains"])))
    if entry["component"]:
        rows.append(("Component", _e(entry["component"])))
    if entry["port_state"]:
        rows.append(("Port state", _e(entry["port_state"])))
    rows.append(("Tested", _e(entry["tested_at"]) or "—"))
    rows.append(("Code map built", _e(entry["code_map_built_at"]) or "—"))
    rows.append(("Mode", "strict (object-verified callers only)" if entry["strict"]
                 else "normal"))
    meta = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)

    why = [f'<p class="reason">{_e(entry["reason"]) or "No reason recorded."}</p>']
    if entry["manual"]:
        who = _e(entry["override_by"]) or "a tester"
        when = f" on {_e(entry['override_at'])}" if entry["override_at"] else ""
        why.append(
            f'<div class="note manual"><b>Manual override.</b> Set to '
            f'{_badge(entry["verdict"])} by {who}{when}. The tool verdict was '
            f'{_badge(entry["auto"] or ct.NOT_RUN)} — the reason above is the '
            f"tool's.</div>")
    if entry["mains_changed"]:
        why.append('<div class="note warn"><b>Main function changed.</b> This verdict '
                   'was recorded against other main function(s) than the model has now. '
                   'Re-run the Test column to refresh it.</div>')
    if entry["stale"]:
        why.append('<div class="note warn"><b>Stale.</b> This verdict was recorded '
                   'against an older code-map build; the snapshot below is from the '
                   'current one. Re-run the Test column to refresh it.</div>')
    if entry["callers"]:
        lis = "".join(
            f'<li><code>{_e(c.get("name"))}</code>'
            + (' <span class="ok">✓ verified</span>' if c.get("verified") else "")
            + "</li>" for c in entry["callers"])
        more = (f'<p class="muted">{entry["callers_total"] - len(entry["callers"])} more '
                "not stored.</p>" if entry["callers_total"] > len(entry["callers"]) else "")
        why.append(f'<h4>Callers recorded at test time ({entry["callers_total"]})</h4>'
                   f"<ul>{lis}</ul>{more}")
    elif entry["verdict"] in (ct.PASSED, ct.ENTRY_POINT, ct.FAILED):
        why.append(f'<p class="muted">No callers recorded · calls '
                   f'{entry["callees_total"]} function(s).</p>')

    # The recorded main → … → function chain replaces the root chain.
    main_snap = ""
    if entry["main_path"]:
        p = entry["main_path"]
        note = ("It is a main function of this model." if len(p) == 1 else
                f"{len(p) - 1} call(s) from the main function {p[0]}() — recorded when "
                "the test ran.")
        main_snap = (f"<h4>Path from the main function</h4><p class=\"muted\">{_e(note)}</p>"
                     f"{_path_svg(p, [False] * (len(p) - 1))}")
    if not s["found"]:
        snap = main_snap + ('<p class="muted">The function is not in the current call tree of '
                "this release, so there is nothing to draw.</p>")
    else:
        alias = (f' <span class="muted">(code-map name <code>{_e(s["key"])}</code>)</span>'
                 if s["key"] != entry["function"] else "")
        path_note = ("Not called by any function — it is its own root."
                     if len(s["path"]) == 1 else
                     f"Shortest chain from a root (a function nothing calls) — "
                     f"{len(s['path']) - 1} call(s); green edges are object-verified.")
        root_path = ("" if main_snap else
                     f"<h4>Call path{alias}</h4><p class=\"muted\">{path_note}</p>"
                     f"{_path_svg(s['path'], s['path_verified'])}")
        snap = (f"{main_snap}{root_path}"
                f'<div class="trees"><div><h4>Called by ({s["callers_total"]}) — '
                f'{CALLER_DEPTH} levels</h4>{_tree_html(s["callers"], s["key"], "callers")}</div>'
                f'<div><h4>Calls ({s["callees_total"]}) — {CALLEE_DEPTH} levels</h4>'
                f'{_tree_html(s["callees"], s["key"], "callees")}</div></div>')

    return (f'<section class="detail" id="{entry["id"]}">'
            f'<p><a href="#{_e(_chapter_id(chapter))}" class="back">← {_e(chapter["model"])}</a>'
            f' · <a href="#top" class="back">Overview</a></p>'
            f'<h2>{_e(entry["port"] or entry["function"] or "Row " + str(entry["row"]))}</h2>'
            f'<table class="grid meta">{meta}</table>'
            f'<h3>Why this verdict</h3>{"".join(why)}'
            f'<h3>Call-tree snapshot</h3>{snap}</section>')


def _chapter_id(chapter: dict) -> str:
    return "m-" + re.sub(r"[^\w-]+", "-", chapter["model"]).strip("-").lower()


_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e3e3e8;--card:#f6f6f8;
--pass:#1e7e34;--pass-bg:#e3f4e7;--fail:#b3261e;--fail-bg:#fbe5e3;--entry:#8a5a00;
--entry-bg:#fdf1d6;--warn:#555;--warn-bg:#ececef;--accent:#0a66d8;--zebra:#fafafb}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f2f2f4;--muted:#a1a1a6;
--line:#3a3a3c;--card:#2c2c2e;--pass:#7ad08f;--pass-bg:#1f3a26;--fail:#ff8a80;
--fail-bg:#47201d;--entry:#f2c46d;--entry-bg:#3d3017;--warn:#c7c7cc;--warn-bg:#3a3a3c;
--accent:#5aa2ff;--zebra:#232325}}
@media (prefers-color-scheme:dark){.head img.logo{background:#fff;padding:6px;
border-radius:6px}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1060px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 8px}
h3{font-size:15px;margin:22px 0 6px}h4{font-size:13px;margin:14px 0 4px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.muted{color:var(--muted)}.ok{color:var(--pass);font-size:12px}
.head{border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:8px;
display:flex;align-items:flex-start;justify-content:space-between;gap:20px}
.head img.logo{max-height:64px;max-width:240px;object-fit:contain;flex:none}
.small{font-size:12px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
table.grid{border:1px solid var(--line);border-radius:8px;border-collapse:separate;
border-spacing:0;overflow:hidden}
table.grid th,table.grid td{border-bottom:1px solid var(--line);
border-right:1px solid var(--line)}
table.grid tr>:last-child{border-right:0}
table.grid tbody tr:last-child>*{border-bottom:0}
table.grid thead th{background:var(--card);color:var(--fg)}
table.grid tbody tr:nth-child(even){background:var(--zebra)}
table.grid tfoot th{background:var(--card);color:var(--fg);font-size:13px;
text-transform:none;letter-spacing:0;border-top:1px solid var(--line);border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
table.meta{width:auto;max-width:100%}table.meta th{width:150px;background:var(--card)}
table.meta tbody tr:nth-child(even){background:none}
.overall-wrap{display:flex;gap:20px;align-items:stretch;flex-wrap:wrap}
table.overall{width:auto;min-width:340px}
.tile{border:1px solid var(--line);border-radius:8px;padding:12px 16px;min-width:200px;
background:var(--card);flex:0 0 auto}.tile .score{width:100%}
.tile-k{font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.tile-v{font-size:30px;font-weight:700;margin:2px 0 6px}
.tile .score b{display:none}
.score{display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
.bar{display:inline-block;width:90px;height:8px;border-radius:4px;background:var(--line);
overflow:hidden}.tile .bar{width:100%}
.bar i{display:block;height:100%}.bar .good{background:var(--pass)}
.bar .mid{background:var(--entry)}.bar .low{background:var(--fail)}
.scroll{overflow-x:auto}.scroll table{min-width:640px}
td code{word-break:break-all}
table.facts{width:auto}table.facts th{padding-right:24px}
tr.entry{cursor:pointer}tr.entry:hover{background:var(--card)}
.v{display:inline-block;white-space:nowrap;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;
margin-right:4px}.v-pass{color:var(--pass);background:var(--pass-bg)}
.v-fail{color:var(--fail);background:var(--fail-bg)}
.v-entry{color:var(--entry);background:var(--entry-bg)}
.v-warn{color:var(--warn);background:var(--warn-bg)}
b+.v{margin-left:10px}
.note{border-radius:8px;padding:8px 12px;margin:8px 0;background:var(--card)}
.note.manual{border-left:4px solid var(--accent)}.note.warn{border-left:4px solid var(--entry)}
.reason{font-size:15px}
.tree,.tree ul{list-style:none;margin:0;padding-left:18px}
.tree{padding-left:0}.tree li{position:relative;padding:2px 0}
.tree ul li::before{content:"";position:absolute;left:-12px;top:12px;width:8px;
border-top:1px solid var(--line)}
.tree ul{border-left:1px solid var(--line);margin-left:6px}
code.hl{background:var(--card);padding:1px 6px;border-radius:4px;font-weight:600}
.trees{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media (max-width:720px){.trees{grid-template-columns:1fr}}
svg.path{display:block;max-width:100%;height:auto;margin:6px 0}
svg.path .node{fill:var(--card);stroke:var(--line)}
svg.path .node.hl{stroke:var(--accent);stroke-width:2}
svg.path text{fill:var(--fg);font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
svg.path .edge{stroke:var(--muted);stroke-width:1.5;fill:var(--muted)}
svg.path .edge.ver{stroke:var(--pass);fill:var(--pass)}svg.path .head{stroke:none}
.chapter{margin-top:30px}
.calltree{margin-top:18px}
.ctree,.ctree ul{list-style:none;margin:0;padding-left:20px}
.ctree{padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.ctree ul{border-left:1px dotted var(--line);margin-left:7px}
.ctree li{padding:2px 0}.ctree summary{cursor:pointer}
.ctree li.leaf{padding-left:14px}
.ctree .fn code{font-size:13px}.ctree .fn.is-port code{font-weight:700}
.ctree .fn::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;
background:var(--pass);margin-right:6px;vertical-align:1px;opacity:.55}
.ctree .fn.is-port::before{opacity:1}
.ctree .port{margin-left:10px;font-size:12.5px;white-space:nowrap}
.ctree .port .v{margin-right:4px}
.tag{display:inline-block;margin-left:6px;padding:0 6px;border-radius:4px;font-size:11px;
font-weight:600}.tag.main{background:var(--accent);color:#fff}
.filter{margin:10px 0}.filter input{padding:5px 8px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--fg);width:260px}
.js .detail{display:none}.js.routed .overview{display:none}
.js .detail.on{display:block}
@media print{.js .detail{display:block;page-break-before:always}
.js.routed .overview{display:block}.filter,.back{display:none}}
"""

_JS = """
(function(){var d=document.documentElement;d.className+=' js';
function route(){var id=location.hash.slice(1),el=id&&document.getElementById(id);
var on=document.querySelectorAll('.detail.on');for(var i=0;i<on.length;i++)
on[i].classList.remove('on');if(el&&el.classList.contains('detail')){
el.classList.add('on');d.classList.add('routed');window.scrollTo(0,0);}
else{d.classList.remove('routed');if(el)el.scrollIntoView();}}
window.addEventListener('hashchange',route);route();
var rows=document.querySelectorAll('tr.entry');for(var j=0;j<rows.length;j++)
rows[j].addEventListener('click',function(){location.hash=this.getAttribute('data-e');});
var f=document.getElementById('flt');if(f)f.addEventListener('input',function(){
var q=f.value.toLowerCase();for(var k=0;k<rows.length;k++)rows[k].style.display=
rows[k].textContent.toLowerCase().indexOf(q)<0?'none':'';});})();
"""


def render_html(data: dict) -> str:
    """The whole report as one HTML document string."""
    facts = [
        ("Project", _e(data["project"])),
        ("Release", _e(data["release"])),
        ("Test column", _e(data["col_name"])),
        ("Match column", _e(data["match_col"])),
        ("Mode", "strict (object-verified callers only)" if data["strict"] else "normal"),
        ("Object files (.o)", "imported — edges can be verified" if data["has_objects"]
         else "not imported"),
    ]
    if data["components"]:
        facts.append(("Symbol files", _e(", ".join(data["components"]))))
    facts += [(_e(label), _e(value)) for label, value in data["operator"]]
    facts.append(("Created", _e(data["created_at"])))
    fact_rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in facts)

    chapters, details = [], []
    for c in data["chapters"]:
        if c["entries"]:
            body = "".join(
                f'<tr class="entry" data-e="{e["id"]}"><td>{e["row"]}</td>'
                f'<td><a href="#{e["id"]}">{_e(e["port"]) or "—"}</a></td>'
                f'<td><code>{_e(e["function"]) or "—"}</code></td>'
                f'<td>{_badge(e["verdict"])}'
                + (' <span class="muted" title="Manual override">✎</span>' if e["manual"] else "")
                + (' <span class="muted" title="Recorded against an older code map">⟳ stale</span>'
                   if e["stale"] else "")
                + f'</td><td class="muted">{_e(e["reason"])}</td></tr>'
                for e in c["entries"])
            table = (f'<div class="scroll"><table class="grid results"><thead><tr><th>#</th><th>Port</th>'
                     f'<th>Function</th><th>Verdict</th><th>Reason</th></tr></thead>'
                     f'<tbody>{body}</tbody></table></div>')
            details += [_detail_html(e, c, data) for e in c["entries"]]
        else:
            table = '<p class="muted">No recorded results for this model — the Test column has not been run on it.</p>'
        extra = (f' · <span class="muted">{c["skipped"]} row(s) without a valid result '
                 f'(not run / retired / deleted)</span>' if c["skipped"] else "")
        nocm = ("" if c["has_code_map"] else
                ' · <span class="muted">no code map for this release — snapshots unavailable</span>')
        sc = (f' · score <b>{_pct(c["score"]["score"])}</b>'
              if c["score"]["total"] else "")
        chapters.append(
            f'<section class="chapter" id="{_chapter_id(c)}"><h2>{_e(c["model"])}</h2>'
            f'<p>{_counts_line(c["counts"])}{sc}{extra}{nocm}</p>{table}'
            f'{_main_tree_html(c) if c["entries"] else ""}</section>')

    title = f'Call-tree test report — {data["release"]} — {data["col_name"]}'
    logo = (f'<img class="logo" src="{_e(data["logo"])}" alt="Logo">'
            if data.get("logo") else "")
    payload = json.dumps({"entries": data["entry_count"]})
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<meta name=\"generator\" content=\"Architecture Validator\">"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main id=\"top\">"
        f'<div class="overview"><div class="head"><div><h1>Call-tree test report</h1>'
        f'<p class="muted">{_e(data["project"])} · {_e(data["release"])} · '
        f'{_e(data["col_name"])}</p></div>{logo}</div>'
        f'<table class="facts">{fact_rows}</table>'
        f'<h3>Overall test results — {data["entry_count"]} result(s)</h3>'
        f'{_overall_html(data)}'
        f'<h3>Architecture models</h3>{_models_html(data)}'
        f'<div class="filter"><input id="flt" placeholder="Filter entries…" '
        f'aria-label="Filter entries"></div>'
        f'{"".join(chapters)}</div>{"".join(details)}</main>'
        f'<script type="application/json" id="report-meta">{payload}</script>'
        f"<script>{_JS}</script></body></html>\n")
