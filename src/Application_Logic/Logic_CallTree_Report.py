"""
Call-tree Test column → one self-contained HTML report (inline CSS/JS/SVG,
no external assets), for handing a verdict record to a reviewer or auditor.

Layout: a header (project, release, Test column, author, date, totals), then
one chapter per architecture model listing every valid result row of that
model's table. Clicking an entry opens its detail view inside the same file
(hash-routed ``#e-<n>`` sections): why the verdict is what it is — the stored
reason and caller evidence, any manual override — and a snapshot of the call
tree around the function (the call path from an entry point as an SVG chain,
plus nested caller / callee trees), taken from the code map at report time.

Pure module: no DB, HTTP or file I/O. ``build_report`` gathers the data from
plain inputs; ``render_html`` turns it into the document.
"""
from __future__ import annotations

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
    ct.NO_MATCH: "warn", ct.NO_DATA: "warn",
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
                 models: Iterable[Tuple[str, List[dict], Optional[dict]]],
                 object_pairs: Optional[Iterable[Tuple[str, str]]] = None,
                 port_state_col: Optional[str] = None,
                 components: Optional[List[str]] = None) -> dict:
    """The report as plain data. ``models`` = [(model name, rows, code map)].

    Valid result rows are rows with a recorded verdict in ``col_name`` that is
    not "No Result" (retired/deleted port) or "Not Run"."""
    port_col = port_name_column(layout, binding.get("match_col", ""))
    pairs = set(object_pairs) if object_pairs is not None else None
    totals: Dict[str, int] = {}
    chapters = []
    n = 0
    for model_name, rows, code_map in models:
        index = ct.CallIndex(code_map, pairs) if code_map else None
        built_at = (code_map or {}).get("built_at", "")
        entries, counts = [], {}
        skipped = 0
        for row_no, row in enumerate(rows, 1):
            cell = row.get(col_name)
            verdict = (cell.get("text") or "") if isinstance(cell, dict) else ""
            if verdict in _EXCLUDED:
                skipped += 1
                continue
            n += 1
            function = cell.get("function") or ct.parse_match(
                ct.cell_text(row.get(binding.get("match_col"))))
            stale = bool(built_at and cell.get("code_map_built_at")
                         and cell["code_map_built_at"] != built_at)
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
                "component": (((code_map or {}).get("functions") or {})
                              .get(function) or {}).get("component", ""),
                "snapshot": call_tree_snapshot(index, function),
            })
            counts[verdict] = counts.get(verdict, 0) + 1
            totals[verdict] = totals.get(verdict, 0) + 1
        chapters.append({"model": model_name, "entries": entries, "counts": counts,
                         "skipped": skipped, "has_code_map": bool(code_map),
                         "code_map_built_at": built_at})
    return {
        "project": project, "release": release, "col_name": col_name,
        "match_col": binding.get("match_col", ""), "strict": bool(binding.get("strict")),
        "has_objects": pairs is not None, "author": author, "created_at": created_at,
        "components": list(components or []),
        "chapters": chapters, "totals": totals, "entry_count": n,
    }


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
    order = [ct.PASSED, ct.ENTRY_POINT, ct.FAILED, ct.NO_MATCH, ct.NO_DATA]
    keys = [k for k in order if k in counts] + sorted(k for k in counts if k not in order)
    return " ".join(f'{_badge(k)}<b>{counts[k]}</b>' for k in keys)


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

    if not s["found"]:
        snap = ('<p class="muted">The function is not in the current call tree of '
                "this release, so there is nothing to draw.</p>")
    else:
        alias = (f' <span class="muted">(code-map name <code>{_e(s["key"])}</code>)</span>'
                 if s["key"] != entry["function"] else "")
        path_note = ("Not called by any function — it is its own root."
                     if len(s["path"]) == 1 else
                     f"Shortest chain from a root (a function nothing calls) — "
                     f"{len(s['path']) - 1} call(s); green edges are object-verified.")
        snap = (f"<h4>Call path{alias}</h4><p class=\"muted\">{path_note}</p>"
                f"{_path_svg(s['path'], s['path_verified'])}"
                f'<div class="trees"><div><h4>Called by ({s["callers_total"]}) — '
                f'{CALLER_DEPTH} levels</h4>{_tree_html(s["callers"], s["key"], "callers")}</div>'
                f'<div><h4>Calls ({s["callees_total"]}) — {CALLEE_DEPTH} levels</h4>'
                f'{_tree_html(s["callees"], s["key"], "callees")}</div></div>')

    return (f'<section class="detail" id="{entry["id"]}">'
            f'<p><a href="#{_e(_chapter_id(chapter))}" class="back">← {_e(chapter["model"])}</a>'
            f' · <a href="#top" class="back">Overview</a></p>'
            f'<h2>{_e(entry["port"] or entry["function"] or "Row " + str(entry["row"]))}</h2>'
            f'<table class="meta">{meta}</table>'
            f'<h3>Why this verdict</h3>{"".join(why)}'
            f'<h3>Call-tree snapshot</h3>{snap}</section>')


def _chapter_id(chapter: dict) -> str:
    return "m-" + re.sub(r"[^\w-]+", "-", chapter["model"]).strip("-").lower()


_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e3e3e8;--card:#f6f6f8;
--pass:#1e7e34;--pass-bg:#e3f4e7;--fail:#b3261e;--fail-bg:#fbe5e3;--entry:#8a5a00;
--entry-bg:#fdf1d6;--warn:#555;--warn-bg:#ececef;--accent:#0a66d8}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f2f2f4;--muted:#a1a1a6;
--line:#3a3a3c;--card:#2c2c2e;--pass:#7ad08f;--pass-bg:#1f3a26;--fail:#ff8a80;
--fail-bg:#47201d;--entry:#f2c46d;--entry-bg:#3d3017;--warn:#c7c7cc;--warn-bg:#3a3a3c;
--accent:#5aa2ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1060px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 8px}
h3{font-size:15px;margin:22px 0 6px}h4{font-size:13px;margin:14px 0 4px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.muted{color:var(--muted)}.ok{color:var(--pass);font-size:12px}
.head{border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:8px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
table.meta{width:auto;max-width:100%}table.meta th{width:150px}
.scroll{overflow-x:auto}.scroll table{min-width:640px}
td code{word-break:break-all}
table.facts{width:auto}table.facts th{padding-right:24px}
tr.entry{cursor:pointer}tr.entry:hover{background:var(--card)}
.v{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;
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
.chapter{margin-top:30px}.toc a{margin-right:14px}
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
    facts += [("Created by", _e(data["author"]) or "—"),
              ("Created", _e(data["created_at"]))]
    fact_rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in facts)

    toc = " ".join(
        f'<a href="#{_chapter_id(c)}">{_e(c["model"])} ({len(c["entries"])})</a>'
        for c in data["chapters"])
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
            table = (f'<div class="scroll"><table><thead><tr><th>#</th><th>Port</th>'
                     f'<th>Function</th><th>Verdict</th><th>Reason</th></tr></thead>'
                     f'<tbody>{body}</tbody></table></div>')
            details += [_detail_html(e, c, data) for e in c["entries"]]
        else:
            table = '<p class="muted">No recorded results for this model — the Test column has not been run on it.</p>'
        extra = (f' · <span class="muted">{c["skipped"]} row(s) without a valid result '
                 f'(not run / retired / deleted)</span>' if c["skipped"] else "")
        nocm = ("" if c["has_code_map"] else
                ' · <span class="muted">no code map for this release — snapshots unavailable</span>')
        chapters.append(
            f'<section class="chapter" id="{_chapter_id(c)}"><h2>{_e(c["model"])}</h2>'
            f'<p>{_counts_line(c["counts"])}{extra}{nocm}</p>{table}</section>')

    title = f'Call-tree test report — {data["release"]} — {data["col_name"]}'
    payload = json.dumps({"entries": data["entry_count"]})
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<meta name=\"generator\" content=\"Architecture Validator\">"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main id=\"top\">"
        f'<div class="overview"><div class="head"><h1>Call-tree test report</h1>'
        f'<p class="muted">{_e(data["project"])} · {_e(data["release"])} · '
        f'{_e(data["col_name"])}</p></div>'
        f'<table class="facts">{fact_rows}</table>'
        f'<h3>Summary — {data["entry_count"]} result(s)</h3><p>{_counts_line(data["totals"])}</p>'
        f'<p class="toc">{toc}</p>'
        f'<div class="filter"><input id="flt" placeholder="Filter entries…" '
        f'aria-label="Filter entries"></div>'
        f'{"".join(chapters)}</div>{"".join(details)}</main>'
        f'<script type="application/json" id="report-meta">{payload}</script>'
        f"<script>{_JS}</script></body></html>\n")
