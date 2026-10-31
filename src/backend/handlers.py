"""
Job handlers (plan §3.1). Each adapts a pure ``Logic_*`` job function to the
``(params, progress, cancel_event) -> result`` contract and is registered with
the JobManager under its kind.

The pure jobs already take a ``progress_cb`` and (where relevant) open their own
SQLite connection on the worker thread — that own-connection rule is what keeps
a build crash-free while the main connection is busy (see build_code_map_job /
run_release_diff). Handlers therefore stay thin.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from contextlib import contextmanager
from typing import Callable

from Application_Logic.Logic_Change_Log_Tab import run_release_diff
from Application_Logic.Logic_Code_Map_Tab import build_code_map_job

from .jobs import JobManager
from .state import AppState, ProjectError


def register_handlers(jobs: JobManager, state: AppState) -> None:
    jobs.register("_demo", _demo_handler)
    jobs.register("release_diff", _make_release_diff(state))
    jobs.register("build_code_map", _make_build_code_map(state))
    jobs.register("fuzzy_rematch", _make_fuzzy_rematch(state))
    jobs.register("build_mind_map", _make_build_mind_map(state))
    jobs.register("generate_tests", _make_generate_tests(state))
    jobs.register("parse_elf", _make_parse_elf(state))
    jobs.register("import_symbols", _make_import_symbols(state))
    jobs.register("import_source", _make_import_source(state))
    jobs.register("import_objects", _make_import_objects(state))
    jobs.register("run_calltree_tests", _make_run_calltree_tests(state))
    jobs.register("test_build_run", _make_test_build_run(state))
    jobs.register("mapped_switch", _make_mapped_switch(state))


# ----------------------------------------------------------------------
# Shared: a worker-thread-owned DB connection to the open project (the
# crash-safe pattern — never write the main connection from a job thread).
# ----------------------------------------------------------------------
@contextmanager
def _worker_db(state: AppState):
    from Application_Logic.Logic_Database import ProjectDatabase
    main = state.require_open()
    wdb = ProjectDatabase()
    wdb.open(main.db_path, create_schema=False, apply_journal=False)
    wdb.set_block_cipher(state.block_cipher())
    try:
        yield wdb
    finally:
        try:
            wdb.close()
        except Exception:  # noqa: BLE001
            pass


def _resolve_model_ids(state: AppState, params: dict, wdb) -> list[int]:
    ids = params.get("model_ids")
    if ids:
        return list(ids)
    mid = state.require_arch().active_model_id
    if mid is None:
        raise ProjectError("No active model and no model_ids given.")
    return [mid]


def _extract_ports(wdb, layout, model_id: int) -> list[dict]:
    """[{name, operation}] from a model's rows, deduped — headless port pull
    (the Qt _extract_ports). Port col = first 'Port Search'; ops col from meta."""
    port_col = next((c[0] for c in layout if c[1] == "Port Search"), None)
    ops_col = wdb.get_meta("operations_column_name")
    ports, seen = [], set()
    for r in wdb.get_model_rows(model_id):
        name = (r.get(port_col, {}) or {}).get("text", "").strip() if port_col else ""
        op = (r.get(ops_col, {}) or {}).get("text", "").strip() if ops_col else ""
        if not name:
            continue
        key = (name, op)
        if key in seen:
            continue
        seen.add(key)
        ports.append({"name": name, "operation": op})
    return ports


# ----------------------------------------------------------------------
# _demo — a dependency-free job for exercising the lifecycle/SSE/cancel path
# in tests without ELF or project fixtures.
# ----------------------------------------------------------------------
def _demo_handler(params: dict, progress: Callable[..., None],
                  cancel: threading.Event):
    steps = int(params.get("steps", 3))
    delay = float(params.get("delay", 0.0))
    # `burn` seconds of GIL-holding CPU work per step — used by the freeze probe
    # (scripts/freeze_probe.py) to load the worker process and prove the UI
    # process stays responsive across the process boundary.
    burn = float(params.get("burn", 0.0))
    done = 0
    for i in range(steps):
        if cancel.is_set():
            break
        done = i + 1
        progress(f"step {done}/{steps}", percent=100.0 * done / steps)
        if burn:
            end = time.monotonic() + burn
            x = 0.0
            while time.monotonic() < end:
                for _ in range(20000):
                    x += math.sqrt(123.456)  # CPU, holds the GIL
                if cancel.is_set():
                    break
        elif delay:
            time.sleep(delay)
    return {"steps_completed": done, "echo": params.get("echo")}


# ----------------------------------------------------------------------
def _make_release_diff(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        db = state.require_open()
        cur = params.get("current_release_id")
        prev = params.get("previous_release_id")
        if cur is None or prev is None:
            raise ProjectError("current_release_id and previous_release_id are required.")
        model_id = params.get("model_id")
        if model_id is None:
            model_id = state.require_arch().active_model_id
        if model_id is None:
            raise ProjectError("model_id is required (no active model).")

        progress("Computing release diff…")
        diff_hash, diffs = run_release_diff(db.db_path, cur, prev,
                                            cipher=state.block_cipher())

        # Persist on the worker's OWN connection (the crash-safe pattern: the
        # main connection stays untouched from this thread). The Change Log then
        # reads the rows back through the main connection.
        progress("Saving diffs…")
        from Application_Logic.Logic_Database import ProjectDatabase
        wdb = ProjectDatabase()
        try:
            wdb.open(db.db_path, create_schema=False, apply_journal=False)
            wdb.set_block_cipher(state.block_cipher())
            wdb.save_code_diffs(model_id, diff_hash, diffs)
            wdb.set_model_diff_hash(model_id, diff_hash, release_id=cur)
            wdb.set_meta("ai_last_diff_hash", diff_hash)
            wdb.commit()
        finally:
            wdb.close()
        return {"diff_hash": diff_hash, "file_count": len(diffs), "model_id": model_id}
    return handler


# ----------------------------------------------------------------------
# After source is imported into the ACTIVE release, its #define macros are
# new match candidates — re-match every model so Match cells filled before
# the source existed pick them up (hand-picked cells are kept, see
# rematch_rows). Never fails the import it follows: the source is stored
# either way, and Re-match Symbols remains available.
# ----------------------------------------------------------------------
def _rematch_source_models(state: AppState, release_id, progress, cancel) -> None:
    active = (state.release_manager.get_active_release()
              if state.release_manager is not None else None)
    if (active is None or active.id != release_id or active.is_baseline
            or not state.can_edit or state.lock_lost or cancel.is_set()):
        return
    model_ids = [model.id for model in state.require_arch().models
                 if model.id is not None and not model.is_deleted]
    if not model_ids:
        return
    try:
        _make_fuzzy_rematch(state)({"model_ids": model_ids}, progress, cancel)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("Re-match after source import failed: %s", e)
        progress(f"Source imported, but re-matching failed ({e}) — "
                 "run Re-match Symbols.")


# ----------------------------------------------------------------------
def _make_build_code_map(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        db = state.require_open()
        # The map belongs to the release; model_id is accepted for older
        # clients but no longer needed.
        model_id = params.get("model_id")
        code_map = build_code_map_job(
            db.db_path,
            params.get("elf_hash"),
            params.get("elf_path"),
            params.get("source_dir"),
            model_id,
            params.get("release_id"),
            progress_cb=lambda msg: progress(msg),
            cipher=state.block_cipher(),
        )
        # The build may have imported source into the DB (an explicitly chosen
        # folder) — tell clients so the ✓ Source badge / Code Map source status
        # refresh without a manual reload.
        state.invalidate_matchers()   # the build may have imported source
        if params.get("source_dir"):
            _rematch_source_models(state, params.get("release_id"), progress, cancel)
        state.bus.publish("db-changed",
                          {"reason": "code-map-built",
                           "release_id": params.get("release_id")})
        return {"functions": len(code_map.get("functions", {}))}
    return handler


# ----------------------------------------------------------------------
# fuzzy_rematch — re-run symbol matching for a model's rows against the
# active release's symbol files (every component), writing "Name (NN%)" into each (Match) cell.
# ----------------------------------------------------------------------
def _make_fuzzy_rematch(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        from Application_Logic.Logic_Symbol_Matcher import (
            SymbolMatcher, search_specs_from_layout, rematch_rows,
        )
        state.require_edit()
        # The active release's symbol files — all components, one match pool.
        hashes = [params["elf_hash"]] if params.get("elf_hash") else state.active_elf_hashes()
        main_db = state.require_open()
        active_rid = main_db.get_active_release_id()
        if not hashes and not (active_rid is not None
                               and main_db.has_release_source(active_rid)):
            raise ProjectError("No ELF imported for the active release.")
        with _worker_db(state) as wdb:
            for h in hashes:
                if not wdb.has_elf(h):
                    raise ProjectError(f"No ELF in project for hash {h}.")
            from Application_Logic.Logic_Source_Macros import release_match_extras
            matcher = SymbolMatcher(None, db=wdb, elf_hash=hashes,
                                    macros=release_match_extras(wdb, active_rid))
            specs = search_specs_from_layout(wdb.load_column_layout())
            model_ids = _resolve_model_ids(state, params, wdb)
            total = 0
            for i, mid in enumerate(model_ids, 1):
                if cancel.is_set():
                    break
                progress(f"Re-matching model {i}/{len(model_ids)}…")
                rows = wdb.get_model_rows(mid)
                changed = rematch_rows(rows, specs, matcher)
                if changed:
                    wdb.save_model_rows(mid, rows)
                total += changed
            wdb.commit()
        return {"cells_changed": total, "models": len(model_ids)}
    return handler


# ----------------------------------------------------------------------
# build_mind_map — own-connection wrapper around run_mindmap_job (which takes
# a live db and writes through it).
# ----------------------------------------------------------------------
def _make_build_mind_map(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        from Application_Logic.Logic_AI_Chat import run_mindmap_job
        from Application_Logic.Logic_AI_Context import META_REQUIREMENTS
        from Application_Logic.Logic_Source_Store import release_source_provider
        state.require_edit()
        with _worker_db(state) as wdb:
            current_rid = params.get("current_release_id")
            if current_rid is None:
                current_rid = wdb.get_active_release_id()
            if current_rid is None:
                raise ProjectError("No current release for the mind map.")
            previous_rid = params.get("previous_release_id")

            current_source = release_source_provider(wdb, current_rid)
            if current_source is None:
                raise ProjectError("The current release has no source imported.")
            previous_source = (release_source_provider(wdb, previous_rid)
                               if previous_rid is not None else None)

            raw_reqs = wdb.get_meta(META_REQUIREMENTS)
            try:
                reqs = json.loads(raw_reqs) if raw_reqs else []
            except (ValueError, TypeError):
                reqs = []

            layout = wdb.load_column_layout()
            names = {m["id"]: m["name"] for m in wdb.get_all_models()}
            model_ids = _resolve_model_ids(state, params, wdb)
            mm_jobs = [(mid, names.get(mid, str(mid)), _extract_ports(wdb, layout, mid), reqs)
                       for mid in model_ids]

            built = run_mindmap_job(mm_jobs, current_source, previous_source, wdb,
                                    release_id=current_rid, progress_cb=progress)
        return {"maps_built": built, "models": len(model_ids)}
    return handler


# ----------------------------------------------------------------------
# generate_tests — AI low-level test generation from an HLT design file.
# Network + writes <Model>_LowLevel.md.
# ----------------------------------------------------------------------
def _make_generate_tests(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        from Application_Logic import Logic_AI_Context as ctx
        from Application_Logic.Logic_AI_Generation import run_generation_job
        from Application_Logic.Logic_Source_Store import release_source_provider
        state.require_open()
        project_path = state.project_path
        provider_id = params.get("provider_id")
        model = params.get("model")
        hlt_path = params.get("hlt_path")
        if not (provider_id and model and hlt_path):
            raise ProjectError("provider_id, model, and hlt_path are required.")

        parsed = ctx.parse_hlt_file(hlt_path)
        cases = parsed["test_cases"]
        wanted = params.get("test_case_ids")
        if wanted is not None:
            wanted = set(wanted)
            cases = [c for c in cases if c["id"] in wanted]
        if not cases:
            raise ProjectError("No test cases selected.")

        with _worker_db(state) as wdb:
            rules = ctx.get_rules(wdb)
            prompt = ctx.get_prompt(wdb)
            rid = params.get("current_release_id")
            if rid is None:
                rid = wdb.get_active_release_id()
            source = release_source_provider(wdb, rid) if rid is not None else None
            output_dir = ctx.hlt_output_dir(project_path)
            path = run_generation_job(
                provider_id, model, rules, prompt, source, output_dir,
                parsed["model_name"], parsed["title"], cases,
                progress_cb=progress,
                case_done_cb=lambda tc_id, _t: progress(f"Generated {tc_id}"),
                stop_check=cancel.is_set,
            )
        return {"output_path": path, "cases": len(cases)}
    return handler


# ----------------------------------------------------------------------
# parse_elf — import an ELF into a release: parse + stream symbols to the DB,
# then key the release to the new ELF hash.
# ----------------------------------------------------------------------
def _make_parse_elf(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        result = _import_symbol_file(state, params, progress, kind="elf")
        result.pop("kind", None)
        return result
    return handler


MAX_COMPONENT_NAME = 64


def _import_symbol_file(state: AppState, params: dict, progress: Callable[..., None],
                        kind: str) -> dict:
    """Parse one symbol file into the project and attach it to a release.

    ``component`` given → that component's file (added, or replaced in
    place). Omitted → the release's primary (first) component, or the
    default component when the release has none yet — the single-ELF
    behavior every older caller relies on. Logged in the change history."""
    import os
    from Application_Logic.Logic_Database import DEFAULT_SYMBOL_COMPONENT
    from Application_Logic.Logic_File_Locking import FileLockManager
    from Application_Logic.Logic_New_Project import ElfImportTask
    state.require_edit()
    file_path = params.get("file_path")
    release_id = params.get("release_id")
    if not file_path or release_id is None:
        raise ProjectError("file_path and release_id are required.")
    component = (params.get("component") or "").strip() or None
    if component is not None and len(component) > MAX_COMPONENT_NAME:
        raise ProjectError(f"Component name is too long (max {MAX_COMPONENT_NAME}).")
    with _worker_db(state) as wdb:
        if wdb.is_release_frozen(release_id):
            raise ProjectError("That release is a frozen baseline — its symbol "
                               "files cannot change.")
        task = ElfImportTask(project_db=wdb, db_path=None)
        if kind == "elf":
            progress("Parsing ELF…")
            parser = task.import_elf(file_path)
        else:
            progress("Loading symbol cache…")
            parser = task.import_json(file_path)
        elf_hash = getattr(parser, "md5_hash", None) or getattr(parser, "_active_elf_hash", None)
        before = wdb.get_release_symbol_files(release_id)
        if component is None:
            component = before[0]["component"] if before else DEFAULT_SYMBOL_COMPONENT
        old = next((f for f in before if f["component"] == component), None)
        try:
            action = wdb.set_release_symbol_file(release_id, component, elf_hash,
                                                 file_path)
        except ValueError as e:
            raise ProjectError(str(e)) from e
        if old and old["elf_hash"] != elf_hash:
            wdb.drop_elf_if_unreferenced(old["elf_hash"])
        fn_count = len(wdb.get_function_names(elf_hash)) if elf_hash else 0
        # Names this file shares with the release's other components (they
        # merge into one entry — the first component in the list wins).
        mine = set(wdb.get_function_names(elf_hash)) if elf_hash else set()
        others = [f["elf_hash"] for f in wdb.get_release_symbol_files(release_id)
                  if f["component"] != component]
        shared = len(mine & set(wdb.get_function_names(others))) if others else 0
        desc = (f"Symbol file for component '{component}' {action}: "
                f"{os.path.basename(file_path)} ({fn_count} functions)")
        _log_release_history(wdb, state, release_id, desc,
                             FileLockManager.get_username())
        wdb.commit()

    # Reflect the new primary ELF on the in-memory release so the symbols
    # endpoint's active_elf_hash() sees it without a reopen.
    if state.release_manager is not None:
        state.release_manager.refresh_elf_mirror(release_id)
    state.invalidate_matchers()
    state.bus.publish("db-changed", {"reason": "symbol-files-changed",
                                     "release_id": release_id})
    return {"kind": kind, "elf_hash": elf_hash, "functions": fn_count,
            "release_id": release_id, "component": component, "action": action,
            "shared_functions": shared,
            "symbol_prefix_stripped": bool(getattr(parser, "symbol_prefix_stripped", False))}


def _log_release_history(db, state: AppState, release_id: int, desc: str,
                         user: str) -> None:
    """History entry for a release-level change, logged against that release
    and — when another release is active — the active one too, so it shows
    on the timeline the user is looking at (as baseline freezes do)."""
    targets = [release_id]
    active = db.get_active_release_id()
    if active is not None and active != release_id:
        targets.append(active)
    for rid in targets:
        try:
            db.add_history_entry(description=desc, model_name="", username=user,
                                 release_id=rid)
        except Exception:  # noqa: BLE001 — history must not fail the change
            pass


# ----------------------------------------------------------------------
# import_source — import C/H source from a local folder into the DB, keyed to a
# release (#2E). Stored gzip-compressed so the source travels with the .arch and
# the code map / test injection can read it without the original tree present.
# ----------------------------------------------------------------------
def _make_import_source(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        from Application_Logic.Logic_Source_Store import (
            FilesystemSourceProvider, iter_source_roots,
        )
        state.require_edit()
        release_id = params.get("release_id")
        source_dir = params.get("source_dir")
        # ``source_roots`` = explicitly listed folders (the CLI): several roots,
        # no built-in skip list, and a file count per root in the result.
        source_roots = params.get("source_roots")
        if release_id is None or not (source_dir or source_roots):
            raise ProjectError("release_id and source_dir are required.")
        root_counts: dict = {}
        files = (iter_source_roots(source_roots, params.get("source_exclude") or (),
                                   root_counts)
                 if source_roots else FilesystemSourceProvider(source_dir).iter_text())

        with _worker_db(state) as wdb:
            progress("Scanning source folder…")
            # save_release_source_files calls progress(rel_path, idx, total_or_None)
            # per file — surface it as the job's per-file message (total is None,
            # so the bar stays indeterminate).
            count = wdb.save_release_source_files(
                release_id,
                files,
                progress=lambda rel_path, idx, _total: progress(
                    f"Importing {rel_path}"),
            )
            wdb.commit()
            # The source's #define macros are match candidates: list them now
            # (cached in the project) so the first symbol search stays fast.
            progress("Indexing #define macros…")
            from Application_Logic.Logic_Source_Macros import release_macros
            release_macros(wdb, release_id)
            wdb.commit()
        state.invalidate_matchers()
        _rematch_source_models(state, release_id, progress, cancel)

        # Tell every client the source set changed so the Release Manager's
        # ✓ Source badge + the workspace refresh without a manual reload.
        state.bus.publish("db-changed",
                          {"reason": "source-imported", "release_id": release_id})
        return {"files": count, "release_id": release_id, "roots": root_counts}
    return handler


# ----------------------------------------------------------------------
# import_objects — #6: scan a build-output folder of .o files and store the
# enrichment dataset (compiler-verified call edges and data references, inlined
# functions, file attribution) keyed to a release. Requires the native parser: the relocation
# and inlined-subroutine extraction only exists in rust_elf_parser.
# ----------------------------------------------------------------------
def _make_import_objects(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        import fnmatch
        import hashlib
        import json as _json
        from pathlib import Path

        from core import elf_parser as ep

        state.require_edit()
        release_id = params.get("release_id")
        obj_dir = params.get("obj_dir")
        include_glob = params.get("include_glob") or "**/*.o"
        exclude_globs = [g.strip() for g in
                         (params.get("exclude_globs") or "").split(",") if g.strip()]
        if release_id is None or not obj_dir:
            raise ProjectError("release_id and obj_dir are required.")
        if not ep.RUST_PARSER_AVAILABLE:
            raise ProjectError(
                "Object enrichment needs the native Rust parser "
                "(rust_elf_parser), which is not available in this build.")
        import rust_elf_parser

        root = Path(obj_dir)
        if not root.is_dir():
            raise ProjectError(f"Not a folder: {obj_dir}")

        progress("Scanning object folder…")
        paths = sorted(p for p in root.glob(include_glob) if p.is_file())
        paths = [p for p in paths
                 if not any(fnmatch.fnmatch(str(p.relative_to(root)), g)
                            for g in exclude_globs)]
        if not paths:
            raise ProjectError(
                f"No object files matched {include_glob!r} under {obj_dir}.")

        objects, edges, inlined, data_refs = [], [], [], []
        for idx, p in enumerate(paths, 1):
            if cancel.is_set():
                raise ProjectError("Import cancelled.")
            rel = str(p.relative_to(root))
            progress(f"Parsing {rel} ({idx}/{len(paths)})")
            # One read per file (EDR-friendly): hash the bytes we read and
            # hand the parser the same path right after (its mmap is the
            # second and last file access).
            try:
                content = p.read_bytes()
                content_hash = hashlib.md5(content).hexdigest()
                del content
                data = _json.loads(rust_elf_parser.parse_elf(str(p)))
                # Renesas-style "_name" symbols → plain C names, so the edges
                # line up with the code map and the ELF's normalized names.
                ep.normalize_native_output(data)
            except ValueError as e:
                status = ("lto" if "LTO object" in str(e)
                          else f"error: {str(e)[:200]}")
                objects.append((rel, "", "", status))
                continue
            except Exception as e:
                objects.append((rel, "", "", f"error: {str(e)[:200]}"))
                continue

            cu = data.get("cu_files") or [""]
            objects.append((rel, cu[0], content_hash, "ok"))
            for e in data.get("call_edges") or []:
                edges.append((e["from"], e["to"], rel))
            for r in data.get("data_refs") or []:
                data_refs.append((r["from"], r["to"], rel))
            for i in data.get("inlined_functions") or []:
                inlined.append((i["name"], i["caller"], rel))

        # Synthetic dataset hash: order-independent over per-file hashes, so
        # re-import detection works without re-parsing.
        lines = sorted(f"{o[0]}:{o[2]}" for o in objects)
        dataset_hash = hashlib.md5("\n".join(lines).encode()).hexdigest()

        with _worker_db(state) as wdb:
            progress("Writing object dataset…")
            wdb.save_release_object_config(
                release_id, str(root), include_glob,
                params.get("exclude_globs") or "")
            wdb.replace_release_objects(
                release_id, objects, edges, inlined, dataset_hash, data_refs)
            wdb.commit()

        state.bus.publish("db-changed",
                          {"reason": "objects-imported", "release_id": release_id})
        ok = sum(1 for o in objects if o[3] == "ok")
        return {"release_id": release_id, "objects": ok,
                "failed": len(objects) - ok,
                "edges": len({(e[0], e[1]) for e in edges}),
                "data_refs": len({(r[0], r[1]) for r in data_refs}),
                "inlined": len(set(inlined)), "dataset_hash": dataset_hash}
    return handler


# ----------------------------------------------------------------------
# run_calltree_tests — evaluate one call-tree Test column for one or more
# models against the bound release's code map (+ its .o dataset). Manual only:
# the user starts it, so a large model's run never surprises an EDR machine.
# ----------------------------------------------------------------------
def _make_run_calltree_tests(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        import datetime as _dt
        from Application_Logic import Logic_CallTree_Report as rep
        from Application_Logic import Logic_CallTree_Test as ct
        from Application_Logic.Logic_File_Locking import FileLockManager
        from backend.routers.architecture import _release_flags, _test_bindings

        state.require_table_edit()
        col_name = params.get("col_name")
        main_db = state.require_open()
        binding = _test_bindings(main_db).get(col_name or "")
        if binding is None:
            raise ProjectError(f"No call-tree Test column named {col_name!r}.")
        rel, is_active, is_baselined = _release_flags(state.release_manager,
                                                      binding["release_id"])
        if rel is None:
            raise ProjectError("The Test column's release no longer exists.")
        if not is_active:
            raise ProjectError(f"Activate release '{rel.name}' to run this Test "
                               "column (its rows are that release's ports).")
        if is_baselined:
            raise ProjectError(f"Release '{rel.name}' is baselined — its test "
                               "results are frozen.")

        tested_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        user = FileLockManager.get_username()
        summary = []
        with _worker_db(state) as wdb:
            layout = wdb.load_column_layout()
            port_state_col = next((n for (n, t, _v, _w) in layout
                                   if t == "PortStateColumn"), None)
            object_pairs = None
            if wdb.has_release_objects(binding["release_id"]):
                object_pairs = set(wdb.get_release_object_edges(binding["release_id"]))
                for inl in wdb.get_release_inlined_functions(binding["release_id"]):
                    object_pairs.add((inl["caller"], inl["name"]))
            model_ids = _resolve_model_ids(state, params, wdb)
            names = {m.id: m.name for m in state.require_arch().models}
            # A variable column checks parameter ports: it starts from the
            # row's function, not from a main function.
            variable_test = ct.variable_test_spec(layout, binding["match_col"])
            if variable_test is not None:
                hashes = [f["elf_hash"] for f in
                          wdb.get_release_symbol_files(binding["release_id"])]
                variables = set(wdb.get_variable_names(hashes)) if hashes else set()
                # Extern data is an undefined target, so it sits with the call edges.
                data_refs = wdb.get_release_object_data_refs(binding["release_id"])
                if data_refs:
                    data_refs |= {p for p in object_pairs or () if p[1] in variables}
                variable_test.update(
                    search_col=rep.port_name_column(layout, binding["match_col"]),
                    min_score=params.get("min_variable_score", ct.DEFAULT_VARIABLE_SCORE),
                    globals=variables, object_refs=data_refs)
            # Every model needs its main function(s): the test checks that
            # they reach each port's function (the UI asks before running).
            mains = ct.parse_mains(wdb.get_meta(ct.MAINS_KEY, "{}"))
            unset = ([] if variable_test is not None else
                     [names.get(mid, str(mid)) for mid in model_ids if not mains.get(mid)])
            # ``allow_missing_mains`` (the CLI): keep testing the other models;
            # a model without a main function reports No Data on its rows.
            if unset and not params.get("allow_missing_mains"):
                raise ProjectError("Set the main function of "
                                   + ", ".join(unset) + " before running the test.")
            for i, mid in enumerate(model_ids, 1):
                if cancel.is_set():
                    break
                progress(f"Testing {names.get(mid, mid)} ({i}/{len(model_ids)})…")
                code_map = wdb.get_model_code_map(mid, release_id=binding["release_id"])
                rows = wdb.get_model_rows(mid)
                counts = ct.run_on_rows(rows, col_name, binding, code_map, object_pairs,
                                        port_state_col=port_state_col,
                                        tested_at=tested_at,
                                        mains=None if variable_test else mains.get(mid, []),
                                        min_match_score=params.get("min_match_score"),
                                        variable_test=variable_test)
                wdb.save_model_rows(mid, rows)
                summary.append({"model_id": mid, "model": names.get(mid, ""),
                                "counts": counts, "has_code_map": bool(code_map)})
                desc = (f"Call-tree test '{col_name}' run: "
                        + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
                try:
                    wdb.add_history_entry(description=desc,
                                          model_name=names.get(mid, ""),
                                          username=user,
                                          release_id=binding["release_id"],
                                          model_id=mid)
                except Exception:  # noqa: BLE001 — history must not fail the run
                    pass
            wdb.commit()

        state.bus.publish("db-changed", {"reason": "calltree-tests-run",
                                         "col_name": col_name})
        return {"col_name": col_name, "tested_at": tested_at, "models": summary,
                "has_objects": object_pairs is not None}
    return handler


def _detect_import_kind(file_path: str) -> str:
    """'elf' or 'json' from the file — magic bytes first, then extension.

    The new-project Import flow is type-agnostic: the UI hands us a file and we
    pick the right importer (ELF binary vs exported JSON symbol cache).
    """
    import os
    try:
        with open(file_path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        raise ProjectError(f"Cannot read import file: {e}") from e
    if magic[:4] == b"\x7fELF":
        return "elf"
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".json":
        return "json"
    if ext in (".elf", ".out", ".abs", ".axf"):
        return "elf"
    raise ProjectError("Unsupported import file — expected an ELF image "
                       "(.elf/.out/.abs/.axf) or a .json symbol cache.")


def _make_import_symbols(state: AppState):
    """Auto-detecting symbol import (the new-project 'Import' option, and
    adding a component file to a release). Detects ELF vs JSON and dispatches
    to the matching ElfImportTask path, then attaches the file to the release
    — as ``component`` when given, else as its primary file."""
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        file_path = params.get("file_path")
        if not file_path or params.get("release_id") is None:
            raise ProjectError("file_path and release_id are required.")
        kind = _detect_import_kind(file_path)
        return _import_symbol_file(state, params, progress, kind=kind)
    return handler


# ----------------------------------------------------------------------
# test_build_run — #15: an overnight series of test builds, one after another.
# Holds the session's edit lock for the whole run. With ``resume_run_id`` it
# picks up a run that a stopped app left unfinished.
# ----------------------------------------------------------------------
def _make_test_build_run(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        from Application_Logic import Logic_Test_Builds as tb
        from Application_Logic.Logic_Sleep_Guard import prevent_sleep

        from .routers.prefs import app_data_dir
        from .routers.test_builds import binary_store

        state.require_edit()
        state.begin_build_run()
        try:
            with _worker_db(state) as wdb, prevent_sleep():
                store = binary_store(state)
                run_id = params.get("resume_run_id")
                if run_id is None:
                    run_id = _start_build_run(state, wdb, store, params)
                state.active_run_id = run_id

                def on_line(item: dict, line: str) -> None:
                    state.bus.publish("build", {
                        "build_id": f"run-{run_id}", "event": "log",
                        "build_name": item["build_name"], "line": line})

                result = tb.execute_run(tb.RunContext(
                    db=wdb, store=store, now=wdb._now_iso, on_line=on_line,
                    work_dir=str(app_data_dir() / "build_runs"
                                 / wdb.get_meta("project_uuid", "") / str(run_id)),
                    progress=progress, cancel=cancel,
                    lock_lost=lambda: state.lock_lost), run_id)
        finally:
            state.end_build_run()
        state.publish_now()
        state.bus.publish("db-changed", {"reason": "test-build-run-finished"})
        return result
    return handler


def _start_build_run(state: AppState, wdb, store, params: dict) -> int:
    from Application_Logic import Logic_Test_Builds as tb
    from Application_Logic.Logic_File_Locking import FileLockManager

    build_ids = params.get("build_ids") or []
    if not build_ids:
        raise ProjectError("Choose at least one test build.")
    overridden = tuple(params.get("continue_anyway") or ())
    names = {r.id: r.name for r in state.require_releases().releases}
    report = tb.preflight(wdb, store, build_ids, overridden, names)
    if report["problems"]:
        raise ProjectError("Pre-flight found problems: " + "; ".join(
            p["problem"] for p in report["problems"]))
    user = FileLockManager.get_username()
    for lock in report["locks"]:
        if not lock["ok"]:
            wdb.add_clean_lock_override(lock["release_id"], lock["missing"],
                                        params.get("continue_reason", ""), user)
    return tb.start_run(wdb, user, tb.plan_run(wdb, build_ids))


# ----------------------------------------------------------------------
# mapped_switch — #15: replace the mapped folder's files with a captured build.
# Reads the store and writes to the user's drive, so it needs no edit lock.
# ----------------------------------------------------------------------
def _make_mapped_switch(state: AppState):
    def handler(params: dict, progress: Callable[..., None], cancel: threading.Event):
        from Application_Logic import Logic_Mapped_Release as mapped
        from Application_Logic.Logic_Test_Builds import CAPTURE_COMPLETE

        from .routers.test_builds import binary_store, mapped_folder, mapped_identity

        with _worker_db(state) as wdb:
            folder = mapped_folder(state)
            if not folder:
                raise ProjectError("Choose the mapped folder first.")
            build = wdb.get_test_build(params.get("build_id"))
            if build is None:
                raise ProjectError("No such test build.")
            active = state.require_releases().get_active_release()
            if active is None or build["release_id"] != active.id:
                raise ProjectError("This build belongs to another release. Each "
                                   "release needs its own builds.")
            if build["capture_status"] != CAPTURE_COMPLETE:
                raise ProjectError(f"{build['name']} has no complete capture.")
            manifest = wdb.get_build_manifest(build["id"])
            try:
                result = mapped.switch(
                    folder, binary_store(state), manifest, mapped_identity(state, build),
                    allow_outside_changes=bool(params.get("allow_outside_changes")),
                    progress=progress)
            except mapped.SwitchError as e:
                detail = f": {', '.join(e.files)}" if e.files else ""
                raise ProjectError(f"{e}{detail}") from e
        return {**result, "build_name": build["name"]}
    return handler
