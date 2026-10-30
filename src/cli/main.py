"""
Headless command line for build pipelines: one ``.cfg`` file, one full run.

    arch-validator run --config validation.cfg

Creates a fresh project from the architecture workbook, imports the build's
symbols, source and objects, builds the code map, tests every model, and writes
``<name>.arch`` + ``<name>.html`` to the output folder.

Exit code: 0 = passed, 1 = architecture test failed, 2 = setup or tool error
(no valid result), so a broken pipeline never looks like bad firmware.

Runs the same job handlers as the desktop app, in this process: no web server
and no window. Never logs the password.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

# Same source-root rule as desktop/worker.py: this file is src/cli/main.py.
SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2


class _Jobs:
    """The JobManager's ``register`` contract, run synchronously: a pipeline
    step has nothing to do but wait for the one before it."""

    def __init__(self, verbose: bool) -> None:
        self._handlers: dict = {}
        self._verbose = verbose

    def register(self, kind: str, handler) -> None:
        self._handlers[kind] = handler

    def _progress(self, message: str, percent=None) -> None:
        if self._verbose:
            _say(f"    {message}")

    def run(self, kind: str, **params) -> dict:
        return self._handlers[kind](params, self._progress, threading.Event())


def _say(message: str) -> None:
    print(message, flush=True)


def _replace_output(path: str) -> None:
    """Remove this run's previous output so a kept workspace can run again."""
    for leftover in (path, path + ".lock", path + "-wal", path + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)


def _import_inputs(cfg, jobs: _Jobs, release_id: int) -> None:
    from Application_Logic.Logic_Pipeline import PipelineError

    for component, path in cfg.symbols:
        _say(f"Importing symbols: {component} ({os.path.basename(path)})")
        result = jobs.run("import_symbols", file_path=path, release_id=release_id,
                          component=component)
        _say(f"    {result['functions']} functions")
    if cfg.source:
        _say("Importing source")
        result = jobs.run("import_source", release_id=release_id,
                          source_roots=cfg.source, source_exclude=cfg.source_exclude)
        for root, count in result["roots"].items():
            _say(f"    {count} files from {root}")
        empty = [root for root, count in result["roots"].items() if not count]
        if empty:
            raise PipelineError("No source files found in: " + ", ".join(empty))
    if cfg.objects:
        _say("Importing objects")
        result = jobs.run("import_objects", release_id=release_id, obj_dir=cfg.objects,
                          include_glob=cfg.object_pattern)
        _say(f"    {result['objects']} objects, {result['failed']} failed")
    _say("Building code map")
    result = jobs.run("build_code_map", release_id=release_id)
    _say(f"    {result['functions']} functions")


def _create_models(cfg, workbook, state) -> dict:
    """Layout, models, rows and main functions from the workbook; {name: id}."""
    from Application_Logic import Logic_CallTree_Test as ct
    from Application_Logic.Logic_Column_Layout import validate_layout
    from Application_Logic.Logic_Pipeline import PipelineError, build_layout

    db, mgr = state.db, state.arch_manager
    layout = build_layout(workbook.columns)
    try:
        validate_layout(layout)
    except ValueError as e:
        raise PipelineError(str(e)) from e
    db.save_column_layout(layout)
    mgr.hard_delete_model(0)      # the placeholder every new project starts with
    ids = {}
    for name, rows in workbook.models.items():
        ids[name] = mgr.create_model(name, "In Work").id
        db.save_model_rows(ids[name], rows)
    db.set_meta(ct.MAINS_KEY, ct.dump_mains(
        {ids[name]: mains for name, mains in workbook.mains.items()}))
    mgr.set_active_model(0)
    db.commit()
    return ids


def _add_test_column(cfg, state, release_id: int) -> str:
    from Application_Logic import Logic_CallTree_Test as ct
    from Application_Logic.Logic_Pipeline import TEST_MATCH_COLUMN

    db = state.db
    name = ct.default_column_name(cfg.release, TEST_MATCH_COLUMN)
    db.save_column_layout(db.load_column_layout() + [(name, ct.TEST_COLUMN_TYPE, True, 110)])
    db.set_meta(ct.BINDINGS_KEY, ct.dump_bindings(
        {name: {"release_id": release_id, "match_col": TEST_MATCH_COLUMN,
                "strict": cfg.strict}}))
    db.commit()
    return name


def _print_results(models: list, gate: dict) -> None:
    for model in models:
        counts = ", ".join(f"{n} {v}" for v, n in sorted(model["counts"].items()))
        _say(f"    {model['model']}: {counts or 'no ports'}")
    for reason in gate["reasons"]:
        _say(f"    {reason}")


def run_pipeline(cfg, verbose: bool = False) -> int:
    from Application_Logic.Logic_Pipeline import evaluate_gate, read_workbook
    from backend.events import EventBus
    from backend.handlers import register_handlers
    from backend.routers.architecture import write_calltree_report
    from backend.state import AppState

    workbook = read_workbook(cfg)
    without_mains = [m for m in workbook.models if not workbook.mains.get(m)]
    os.makedirs(cfg.output_folder, exist_ok=True)
    arch_path = os.path.join(cfg.output_folder, f"{cfg.name}.arch")
    report_path = os.path.join(cfg.output_folder, f"{cfg.name}.html")
    _replace_output(arch_path)

    state = AppState(EventBus())
    jobs = _Jobs(verbose)
    register_handlers(jobs, state)
    try:
        state.new_project(arch_path, cfg.password)
        release = state.release_manager.create_release(cfg.release)
        state.release_manager.set_active_release(state.release_index_by_id(release.id))

        _import_inputs(cfg, jobs, release.id)

        _say(f"Loading {len(workbook.models)} models")
        ids = _create_models(cfg, workbook, state)
        jobs.run("fuzzy_rematch", model_ids=list(ids.values()))
        col_name = _add_test_column(cfg, state, release.id)

        for model in without_mains:
            _say(f"    {model} has no main function: its ports report No Data")
        _say(f"Testing (minimum match score {cfg.min_match_score}%)")
        result = jobs.run("run_calltree_tests", col_name=col_name,
                          model_ids=list(ids.values()), allow_missing_mains=True,
                          min_match_score=cfg.min_match_score)
        gate = evaluate_gate({m["model"]: m["counts"] for m in result["models"]},
                             cfg.max_unmatched)
        _print_results(result["models"], gate)

        write_calltree_report(state, col_name, report_path)
        state.save_project()
    finally:
        state.close_project()

    _say(f"Report: {report_path}")
    _say(f"Project: {arch_path}")
    _say("RESULT: PASSED" if gate["passed"] else "RESULT: FAILED")
    return EXIT_PASSED if gate["passed"] else EXIT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arch-validator",
        description="Run the architecture validation in a build pipeline.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a full validation from a .cfg file.")
    run.add_argument("--config", required=True, help="Path to the .cfg file.")
    run.add_argument("--verbose", action="store_true", help="Show every step's progress.")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    # A Windows console's code page must not crash the run on a model's name.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    from Application_Logic.Logic_Crypto import PasswordInvalid, PasswordRequired
    from Application_Logic.Logic_Pipeline import PipelineError, load_config
    from backend.state import ProjectError

    try:
        return run_pipeline(load_config(args.config), args.verbose)
    except (PipelineError, ProjectError, PasswordInvalid, PasswordRequired) as e:
        _say(f"ERROR: {e}")
    except Exception as e:  # noqa: BLE001 — every failure must reach the pipeline log
        if args.verbose:
            logging.exception("Run failed")
        _say(f"ERROR: {type(e).__name__}: {e}")
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
