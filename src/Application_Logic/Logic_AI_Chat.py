"""
Advanced AI Chat — mind-map / chat jobs and markdown rendering (pure logic).

Phase 0 (pywebview migration): Qt-free. The Qt tab controller lives in
UI/tab_ai_chat.py and runs these jobs on worker threads; after Phase 1 the
FastAPI job manager runs `run_mindmap_job` as the `build_mind_map` job and
the `ai` router streams `run_chat_job`. The heavy logic lives in
Logic_AI_Context / Logic_AI_Providers / Logic_AI_Tools (unit-tested).
"""
import datetime
import json
import logging
import os

from Application_Logic import Logic_AI_Providers as providers
from Application_Logic import Logic_AI_Context as ctx

logger = logging.getLogger(__name__)

_META_LAST_DIFF = "ai_last_diff_hash"

import re as _re
import html as _html


def _md_inline(s: str) -> str:
    """Inline markdown → HTML: `code`, **bold**, *italic*. HTML-escaped first."""
    codes = []
    s = _re.sub(r"`([^`]+)`", lambda m: codes.append(m.group(1)) or f"\x01{len(codes)-1}\x01", s)
    s = _html.escape(s)
    s = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = _re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    s = _re.sub(r"(?<![\*\w])\*([^*]+?)\*(?![\*\w])", r"<i>\1</i>", s)
    for i, c in enumerate(codes):
        s = s.replace(f"\x01{i}\x01",
                      f"<code style='background-color:#1b1b1b;'>{_html.escape(c)}</code>")
    return s


def md_to_html(md: str) -> str:
    """Minimal, dependency-free markdown → Qt-rich-text HTML for AI chat bubbles.
    Handles fenced code blocks, headings, bold/italic, inline code, and bullet/
    numbered lists. Output inherits the bubble's (light) text colour."""
    md = md or ""
    code_blocks = []
    md = _re.sub(r"```[^\n]*\n?(.*?)```",
                 lambda m: code_blocks.append(m.group(1).rstrip("\n")) or f"\x00C{len(code_blocks)-1}\x00",
                 md, flags=_re.S)
    out, list_mode = [], None
    for raw in md.split("\n"):
        ul = _re.match(r"^\s*[-*+]\s+(.*)$", raw)
        ol = _re.match(r"^\s*\d+\.\s+(.*)$", raw)
        if ul or ol:
            kind = "ul" if ul else "ol"
            if list_mode != kind:
                if list_mode:
                    out.append(f"</{list_mode}>")
                out.append(f"<{kind}>")
                list_mode = kind
            out.append(f"<li>{_md_inline((ul or ol).group(1))}</li>")
            continue
        if list_mode:
            out.append(f"</{list_mode}>")
            list_mode = None
        h = _re.match(r"^\s*#{1,6}\s+(.*)$", raw)
        if h:
            out.append(f"<b>{_md_inline(h.group(1))}</b><br>")
        elif raw.strip() == "":
            out.append("<br>")
        else:
            out.append(_md_inline(raw) + "<br>")
    if list_mode:
        out.append(f"</{list_mode}>")
    res = "".join(out)
    for i, b in enumerate(code_blocks):
        res = res.replace(
            f"\x00C{i}\x00",
            f"<pre style='background-color:#1b1b1b; padding:6px;'>{_html.escape(b)}</pre>")
    return res


def _release_code_map(db, release_id, current_source, progress_cb):
    """The release's Code Map: the stored one, or one built now from the
    release's ELF and source, or from the source alone when there's no ELF.
    One built here is stored for the release, like the Code Map tab's."""
    code_map = db.get_release_code_map(release_id)
    if code_map:
        progress_cb("Using the release's Code Map …")
        return code_map
    from Application_Logic.Logic_Code_Index import build_index
    from Application_Logic.Logic_Code_Map import ComponentParsers, build_code_map

    release = next((r for r in db.get_all_releases()
                    if not r.get("is_deleted", 0) and r.get("elf_hash")
                    and (r.get("id") == release_id if release_id is not None
                         else r.get("is_active", 0) == 1)), None)
    progress_cb("Indexing the source …")
    code_index = build_index(current_source) if current_source else None
    parser = None
    if release and release.get("elf_path") and os.path.exists(release["elf_path"]):
        progress_cb("Joining the source with the ELF …")
        try:
            from core.elf_parser import ELFParser
            files = db.get_release_symbol_files(release["id"])
            parser = ELFParser(release["elf_path"])
            parser.load_elf(release["elf_path"])
            parser.extract_all_streaming_to_db(db)
            parser = ELFParser(release["elf_path"])
            parser._db = db
            parser._active_elf_hash = release["elf_hash"]
            if len(files) > 1:
                parts = []
                for f in files:
                    cp = ELFParser(f["elf_path"] or None)
                    cp._db = db
                    cp._active_elf_hash = f["elf_hash"]
                    parts.append((f["component"], cp, None))
                parser = ComponentParsers(parts)
        except Exception as e:  # noqa: BLE001 — the source alone still makes a map
            progress_cb(f"Warning: the ELF couldn't be read ({e}); mapping the source only.")
            logger.warning("ELF unavailable for the mind map", exc_info=True)
            parser = None
    code_map = build_code_map(parser, code_index, source_root="",
                              prefer_source_calls=None if parser else True)
    db.save_release_code_map(json.dumps(code_map), release_id=release_id)
    return code_map


def run_mindmap_job(jobs, current_source, previous_source, db, release_id=None,
                    progress_cb=lambda msg: None, project_models=None):
    """Builds the mind maps (v3, Logic_Project_Map) of each job's model, and the
    project map when ``project_models`` is given. Run me on a worker thread.

    ``jobs`` is a list of (model_id, model_name, ports, requirements) tuples (the
    requirements are no longer used); ``project_models`` is [(model_id, name,
    ports)] for every model of the project. ``release_id`` pins the maps to that
    release (#2E). Returns the number of model maps built; raises on failure.
    """
    from Application_Logic import Logic_Project_Map as project_map

    diff_hash = ""
    diffs = None
    if previous_source and current_source:
        diff_hash = ctx.compute_diff_hash(current_source, previous_source)
        db.set_meta(_META_LAST_DIFF, diff_hash)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    release_name = next((r.get("name", "") for r in db.get_all_releases()
                         if r.get("id") == release_id), "")
    code_map = _release_code_map(db, release_id, current_source, progress_cb)
    source_hash = ctx.hash_source_tree(current_source) if current_source else ""

    for (mid, name, ports, _reqs) in jobs:
        progress_cb(f"Mapping model '{name}' …")
        model_diffs = []
        if diff_hash:
            cur = db._conn.execute(
                "SELECT 1 FROM ai_code_diffs WHERE model_id=? AND diff_hash=? LIMIT 1",
                (mid, diff_hash))
            if cur.fetchone():
                progress_cb(f"Using pregenerated diffs for model '{name}' …")
                model_diffs = db.get_code_diffs(mid, diff_hash)
            else:
                if diffs is None:
                    progress_cb("Computing file-by-file diffs (this reads changed files)…")
                    diffs = ctx.diff_source_folders(current_source, previous_source)
                model_diffs = diffs

        mm = project_map.build_model_map(code_map, mid, name, ports, release_name, now)
        mm["source_hash"] = source_hash
        db.save_model_mindmap(
            mid, json.dumps(mm), source_hash=source_hash,
            diff_hash=diff_hash, builder_version=mm["builder_version"],
            char_count=ctx.mind_map_char_count(mm), updated_at=now,
            release_id=release_id)
        if diff_hash and model_diffs:
            db.save_code_diffs(mid, diff_hash, model_diffs)

    if project_models:
        progress_cb("Mapping the whole project …")
        pm = project_map.build_project_map(code_map, project_models, release_name, now)
        pm["source_hash"] = source_hash
        db.save_project_map(pm, release_id=release_id)
    db.commit()
    return len(jobs)


def run_chat_job(provider_id, model, messages, tools, executor, system_prompt,
                 on_tool_call=lambda name, args: None, stop_check=lambda: False):
    """One agentic chat turn (tool loop included). Run me on a worker thread.
    Returns the assistant's final text; raises on failure (incl. AIStopped)."""
    return providers.generate_with_tools(
        provider_id, model, messages, tools,
        tool_executor=executor, system_prompt=system_prompt,
        on_tool_call=on_tool_call, stop_check=stop_check)
