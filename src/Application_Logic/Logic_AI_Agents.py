"""
AI Chat agents and rules, set up the way VS Code's Copilot sets them up.

VS Code keeps them as Markdown files in the repository:

  * ``.github/agents/<name>.agent.md`` (formerly ``.github/chatmodes/
    <name>.chatmode.md``) — a custom agent: YAML front matter (description,
    tools, model) and a body of instructions.
  * ``.github/copilot-instructions.md`` and ``AGENTS.md`` — rules applied to
    every request.
  * ``.github/instructions/<name>.instructions.md`` — rules with an ``applyTo``
    glob, applied when the conversation is about matching files.

Here they live in the project (project_meta, so the team shares them and they
travel with the .arch), next to a few built-in agents. They can be imported
from a checkout's ``.github`` folder and exported back to the same files.
Pure logic: no Qt, no FastAPI.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Dict, Iterable, List, Optional

META_AGENTS = "ai_agents"
META_INSTRUCTIONS = "ai_instructions"

# The read-only chat tools (Logic_AI_Tools.default_tools) by name.
ALL_TOOLS = ("read_file", "list_files", "search_code", "get_mind_map",
             "get_requirements", "get_diff", "get_function", "get_call_graph")

# VS Code tool names (and tool sets) mapped onto ours, so an imported
# .agent.md keeps the spirit of its tool list. Unknown names are ignored.
_VSCODE_TOOLS = {
    "codebase": ("search_code", "list_files", "read_file", "get_function", "get_call_graph"),
    "search": ("search_code", "list_files"),
    "textsearch": ("search_code",),
    "filesearch": ("list_files",),
    "readfile": ("read_file",),
    "read": ("read_file", "list_files"),
    "usages": ("get_call_graph", "get_function"),
    "findtestfiles": ("list_files",),
    "changes": ("get_diff",),
    "problems": (),
    "fetch": (),
}

# How a reply should point at code, so the chat can turn the references into
# links that open the code (see the frontend's chat renderer).
CITATION_RULES = """When you refer to code:
- Write function, variable and macro names in backticks, exactly as in the source: `Motor_Init`.
- Write file references as `path/to/file.c:123` (path as the tools return it, then the line).
- Quote code in fenced blocks with a language tag, e.g. ```c.
- Use the tools to look at the real code before answering; never guess at a function's body."""

BUILTIN_AGENTS: List[Dict] = [
    {
        "id": "builtin:ask",
        "name": "Ask",
        "description": "Answers questions about the architecture and the code.",
        "instructions": ("Answer questions about the project's architecture and source code. "
                         "Look the code up with the tools first, then answer concisely, "
                         "pointing at the functions and lines that support the answer."),
        "tools": None,
        "model": "",
        "source": "builtin",
    },
    {
        "id": "builtin:test-engineer",
        "name": "Test Engineer",
        "description": "Designs test cases from the code and the interfaces.",
        "instructions": (
            "You are an embedded-software test engineer. For the code asked about, "
            "find the inputs, outputs, conditions and boundary values by reading the "
            "functions and their callers, then propose test cases: preconditions, "
            "stimulus, expected result, and the requirement or port they cover. "
            "Prefer a table. Point out branches no test reaches."),
        "tools": None,
        "model": "",
        "source": "builtin",
    },
    {
        "id": "builtin:reviewer",
        "name": "Code Reviewer",
        "description": "Reviews code for defects, MISRA-style risks and missing checks.",
        "instructions": (
            "Review the code asked about as a senior embedded reviewer. Read it with the "
            "tools, then list concrete findings — wrong conditions, unchecked inputs, "
            "overflow, race conditions on shared data, MISRA C concerns — each with the "
            "file:line and why it matters. Say plainly when you found nothing."),
        "tools": None,
        "model": "",
        "source": "builtin",
    },
]

DEFAULT_AGENT_ID = "builtin:ask"


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------
_FM_RE = re.compile(r"\A﻿?---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in _split_list(inner)]
    return v


def _split_list(s: str) -> List[str]:
    """Split an inline YAML list on commas outside quotes."""
    out, buf, quote = [], "", ""
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf += ch
        elif ch == ",":
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return [x.strip() for x in out]


def parse_front_matter(text: str):
    """(front-matter dict, body) of a Markdown file. Handles the YAML VS Code's
    agent/instruction files use: scalars, quoted strings, inline lists and
    block lists. Anything fancier is kept as a plain string."""
    text = (text or "").replace("\r\n", "\n")
    m = _FM_RE.match(text)
    if not m:
        return {}, text.strip()
    meta: Dict = {}
    key = None
    for line in m.group(1).split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", line) or re.match(r"^-\s+(.*)$", line)
        if item and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []  # replaces the None of an empty "key:"
            meta[key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            # An empty value may open a block list ("- item" lines follow).
            meta[key] = _scalar(kv.group(2)) if kv.group(2).strip() else None
    for k, v in list(meta.items()):
        if v is None:
            meta[k] = ""
    return meta, text[m.end():].strip()


def _yaml_value(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(f"'{x}'" for x in v) + "]"
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}'\"]|^\s|\s$", s):
        return "'" + s.replace("'", "''") + "'"
    return s


def _front_matter(fields: Dict) -> str:
    lines = [f"{k}: {_yaml_value(v)}" for k, v in fields.items() if v not in (None, "")]
    return "---\n" + "\n".join(lines) + "\n---\n" if lines else ""


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "agent"


def _stem(filename: str) -> str:
    base = os.path.basename(filename or "")
    for suffix in (".agent.md", ".chatmode.md", ".instructions.md", ".prompt.md", ".md"):
        if base.lower().endswith(suffix):
            return base[: -len(suffix)]
    return base


def map_tools(names) -> Optional[List[str]]:
    """An agent's tool list → our tool names. None = every tool (the key was
    absent, or none of the listed tools exist here — all ours are read-only)."""
    if names is None or names == "":
        return None
    if isinstance(names, str):
        names = [names]
    out: List[str] = []
    for n in names:
        key = str(n).strip()
        if key in ALL_TOOLS:
            cand = (key,)
        else:
            low = key.lower().replace("_", "").replace("-", "")
            # Tool-set prefixes ("search/codebase", "#codebase") → last part.
            low = low.lstrip("#").split("/")[-1]
            cand = _VSCODE_TOOLS.get(low, ())
        for t in cand:
            if t not in out:
                out.append(t)
    return out or None


def parse_agent_md(text: str, filename: str = "") -> Dict:
    """A VS Code ``.agent.md`` / ``.chatmode.md`` file → an agent dict."""
    meta, body = parse_front_matter(text)
    name = str(meta.get("name") or "").strip() or _stem(filename) or "Agent"
    tools_raw = meta.get("tools") if "tools" in meta else None
    return {
        "id": "",
        "name": name,
        "description": str(meta.get("description") or "").strip(),
        "instructions": body,
        "tools": map_tools(tools_raw),
        "model": str(meta.get("model") or "").strip() if not isinstance(meta.get("model"), list)
        else str((meta.get("model") or [""])[0]),
        "source": "project",
    }


def agent_to_md(agent: Dict) -> str:
    """An agent → ``.agent.md`` text VS Code reads."""
    fields = {"name": agent.get("name", ""), "description": agent.get("description", "")}
    if agent.get("tools"):
        fields["tools"] = list(agent["tools"])
    if agent.get("model"):
        fields["model"] = agent["model"]
    return _front_matter(fields) + "\n" + (agent.get("instructions") or "").strip() + "\n"


def parse_instructions_md(text: str, filename: str = "") -> Dict:
    """A ``*.instructions.md`` / ``copilot-instructions.md`` / ``AGENTS.md`` file
    → a rule: {name, apply_to, body}. No applyTo = applies to every request."""
    meta, body = parse_front_matter(text)
    apply_to = meta.get("applyTo") or meta.get("apply_to") or ""
    if isinstance(apply_to, list):
        apply_to = ",".join(str(x) for x in apply_to)
    return {
        "id": "",
        "name": str(meta.get("name") or "").strip() or _stem(filename) or "Rules",
        "description": str(meta.get("description") or "").strip(),
        "apply_to": str(apply_to).strip(),
        "body": body,
        "enabled": True,
    }


def instructions_to_md(rule: Dict) -> str:
    fields = {"description": rule.get("description", ""), "applyTo": rule.get("apply_to", "")}
    return _front_matter(fields) + "\n" + (rule.get("body") or "").strip() + "\n"


def rule_applies(rule: Dict, files: Iterable[str]) -> bool:
    """Does a rule apply, given the files the conversation is about? A rule
    without applyTo (or ``**``) always applies; otherwise one of its globs
    (comma-separated) must match one of the files."""
    if not rule.get("enabled", True):
        return False
    globs = [g.strip() for g in (rule.get("apply_to") or "").split(",") if g.strip()]
    if not globs or any(g in ("**", "**/*", "*") for g in globs):
        return True
    for f in files or ():
        path = (f or "").replace("\\", "/").lstrip("/")
        for g in globs:
            g2 = g.lstrip("/")
            if fnmatch.fnmatch(path, g2) or fnmatch.fnmatch(os.path.basename(path), g2) \
                    or (g2.startswith("**/") and fnmatch.fnmatch(path, g2[3:])):
                return True
    return False


# ---------------------------------------------------------------------------
# Import from a checkout
# ---------------------------------------------------------------------------
_MAX_IMPORT_FILES = 200
_MAX_IMPORT_BYTES = 512_000
_SKIP_DIRS = {".git", "node_modules", "build", "dist", "out", ".venv", "__pycache__"}


def import_from_folder(folder: str) -> Dict:
    """Agents and rules found in a checkout, VS Code's layout:
    .github/agents/*.agent.md, .github/chatmodes/*.chatmode.md,
    .github/copilot-instructions.md, .github/instructions/*.instructions.md,
    AGENTS.md — at the folder's root or below it. Pointing at the .github
    folder itself works too. Nothing is saved."""
    agents: List[Dict] = []
    rules: List[Dict] = []
    if not folder or not os.path.isdir(folder):
        raise FileNotFoundError(f"No such folder: {folder}")
    seen = 0
    for dirpath, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for n in sorted(names):
            low = n.lower()
            kind = None
            if low.endswith((".agent.md", ".chatmode.md")):
                kind = "agent"
            elif low.endswith(".instructions.md") or low in ("agents.md", "copilot-instructions.md"):
                kind = "rule"
            if kind is None:
                continue
            path = os.path.join(dirpath, n)
            try:
                if os.path.getsize(path) > _MAX_IMPORT_BYTES:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if kind == "agent":
                a = parse_agent_md(text, n)
                a["file"] = rel
                agents.append(a)
            else:
                r = parse_instructions_md(text, n)
                if low == "copilot-instructions.md":
                    r["name"] = "copilot-instructions"
                r["file"] = rel
                rules.append(r)
            seen += 1
            if seen >= _MAX_IMPORT_FILES:
                return {"agents": agents, "rules": rules, "truncated": True}
    return {"agents": agents, "rules": rules, "truncated": False}


# ---------------------------------------------------------------------------
# Project storage
# ---------------------------------------------------------------------------
def _load_list(db, key: str) -> List[Dict]:
    if db is None:
        return []
    try:
        raw = db.get_meta(key)
    except Exception:  # noqa: BLE001 — a closed/odd DB just has none
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _clean_agent(a: Dict, taken: set) -> Dict:
    name = str(a.get("name") or "Agent").strip()[:80] or "Agent"
    aid = str(a.get("id") or "")
    if not aid.startswith("project:") or aid in taken:
        base = "project:" + _slug(name)
        aid, i = base, 2
        while aid in taken:
            aid, i = f"{base}-{i}", i + 1
    taken.add(aid)
    tools = a.get("tools")
    if tools is not None:
        tools = [t for t in tools if t in ALL_TOOLS] or None
    return {"id": aid, "name": name,
            "description": str(a.get("description") or "")[:500],
            "instructions": str(a.get("instructions") or ""),
            "tools": tools, "model": str(a.get("model") or "")[:120],
            "source": "project"}


def _clean_rule(r: Dict, taken: set) -> Dict:
    name = str(r.get("name") or "Rules").strip()[:80] or "Rules"
    rid = str(r.get("id") or "")
    if not rid.startswith("rule:") or rid in taken:
        base = "rule:" + _slug(name)
        rid, i = base, 2
        while rid in taken:
            rid, i = f"{base}-{i}", i + 1
    taken.add(rid)
    return {"id": rid, "name": name,
            "description": str(r.get("description") or "")[:500],
            "apply_to": str(r.get("apply_to") or "")[:500],
            "body": str(r.get("body") or ""),
            "enabled": bool(r.get("enabled", True))}


def project_agents(db) -> List[Dict]:
    return _load_list(db, META_AGENTS)


def all_agents(db) -> List[Dict]:
    """Built-in agents, then the project's."""
    return [dict(a) for a in BUILTIN_AGENTS] + project_agents(db)


def project_rules(db) -> List[Dict]:
    return _load_list(db, META_INSTRUCTIONS)


def save_project_agents(db, agents: List[Dict]) -> List[Dict]:
    taken: set = set()
    clean = [_clean_agent(a, taken) for a in agents if isinstance(a, dict)
             and a.get("source", "project") != "builtin"]
    db.set_meta(META_AGENTS, json.dumps(clean))
    return clean


def save_project_rules(db, rules: List[Dict]) -> List[Dict]:
    taken: set = set()
    clean = [_clean_rule(r, taken) for r in rules if isinstance(r, dict)]
    db.set_meta(META_INSTRUCTIONS, json.dumps(clean))
    return clean


def find_agent(db, agent_id: Optional[str]) -> Dict:
    agents = all_agents(db)
    return next((a for a in agents if a.get("id") == agent_id), agents[0])


def compose_system_prompt(base_rules: str, agent: Optional[Dict], rules: List[Dict],
                          files: Iterable[str] = (), extra: str = "") -> str:
    """The chat's system prompt: the project's chat rules, the agent's
    instructions, the rules that apply to the files in view, how to cite code,
    then ``extra`` (the mind map)."""
    files = list(files or ())
    parts = [base_rules.strip()] if base_rules and base_rules.strip() else []
    if agent and (agent.get("instructions") or "").strip():
        parts.append(f"# AGENT: {agent.get('name', '')}\n{agent['instructions'].strip()}")
    applied = [r for r in rules or () if rule_applies(r, files) and (r.get("body") or "").strip()]
    for r in applied:
        scope = f" (applies to {r['apply_to']})" if r.get("apply_to") else ""
        parts.append(f"# RULES: {r.get('name', '')}{scope}\n{r['body'].strip()}")
    parts.append("# CITING CODE\n" + CITATION_RULES)
    if files:
        parts.append("# FILES IN VIEW\n" + "\n".join(f"- {f}" for f in files[:20]))
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def tools_for_agent(agent: Optional[Dict], all_tools: List) -> List:
    """The agent's tools out of ``all_tools`` (Tool objects); all of them when
    the agent names none."""
    names = (agent or {}).get("tools")
    if not names:
        return list(all_tools)
    keep = set(names)
    return [t for t in all_tools if t.name in keep]
