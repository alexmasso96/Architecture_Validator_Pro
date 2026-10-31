"""
Test Builds (#15)
=================
A Test Build is a test project attached to one release, built for testing on a
real ECU. Saving its binaries per release is its only link to that release: it
never touches the release's symbols, Code Map, call graph or test results, and
it's never compared with Main. It's judged only on whether its build succeeded,
because the code in it may deliberately hang or violate the MPU.

This module holds the logic with no app state: the clean-release lock, hooked
file rendering, artifact capture, pre-flight checks, the source snapshot that
keeps one build's hooks out of the next, and the sequential run itself.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import Logic_Code_Injection as injection
from .Logic_Binary_Store import BinaryStore, StoreError
from .Logic_Source_Store import release_source_provider

logger = logging.getLogger(__name__)

ROLE_SYMBOLS = "Symbols"
ROLE_FLASH = "Flash"
ROLE_SOURCE = "Source"
ROLE_OBJECTS = "Objects"
ROLES = (ROLE_SYMBOLS, ROLE_FLASH, ROLE_SOURCE, ROLE_OBJECTS)

# Source files are kept apart from build output in a capture's relative paths.
SOURCE_PREFIX = "source/"

CAPTURE_NONE = "none"
CAPTURE_COMPLETE = "complete"
CAPTURE_INCOMPLETE = "incomplete"

ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_PASSED = "passed"
ITEM_FAILED = "failed"
ITEM_INCOMPLETE = "incomplete"
ITEM_SKIPPED = "skipped"
ITEM_CANCELLED = "cancelled"

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_CANCELLED = "cancelled"

SOURCE_JOURNAL = "source_journal.json"
# A run needs room for this many times the last build's size before it starts.
SPACE_MARGIN = 1.2

_STOP_POLL_SECONDS = 0.5
_KILL_WAIT_SECONDS = 10


# ----------------------------------------------------------------------
# Clean-release lock
# ----------------------------------------------------------------------
def clean_lock_status(db, release_id: int, release_name: str = "") -> dict:
    """Whether the release's clean source and .o files are imported.

    Architecture testing needs them from clean software, so they must exist
    before injected code does. Missing data is reported and the caller decides:
    the user can continue anyway, and importing later is never blocked.
    """
    missing = []
    if not db.has_release_source(release_id):
        missing.append("source")
    if not db.has_release_objects(release_id):
        missing.append(".o files")
    label = release_name or f"release {release_id}"
    message = ""
    if missing:
        message = (f"No {' or '.join(missing)} imported for {label}. "
                   "Import them before changing the code.")
    return {"ok": not missing, "missing": missing, "message": message,
            "release_id": release_id, "release_name": release_name}


# ----------------------------------------------------------------------
# Hooked source
# ----------------------------------------------------------------------
def render_hooked_files(db, provider, test_project_id: int,
                        mode: str = "modified") -> tuple:
    """Returns ``({rel_path: text}, conflicts)`` for a test project on the
    release ``provider`` reads from. The release doesn't need to be active."""
    by_file: dict = {}
    for inj in db.list_injections(test_project_id):
        by_file.setdefault(inj["src_file_path"], []).append(inj)

    files: dict = {}
    conflicts: list = []

    def emit(rel_path: str) -> None:
        text = provider.read_file(rel_path) if provider is not None else None
        if text is None:
            conflicts.extend({"src_file_path": rel_path, "injection_id": h["id"]}
                             for h in by_file.get(rel_path, ()))
            return
        hooks = by_file.get(rel_path)
        if hooks:
            text, results = injection.apply_injections(text, hooks)
            conflicts.extend(
                {"src_file_path": rel_path, "injection_id": r["injection_id"]}
                for r in results if not r["applied"])
        files[rel_path] = text

    if mode == "modified":
        for rel_path in by_file:
            emit(rel_path)
    else:
        for sf in provider.list_files():
            emit(sf.rel_path)

    for f in db.list_test_project_files(test_project_id):
        text = db.read_test_project_file(test_project_id, f["rel_path"])
        if text is not None:
            files[f["rel_path"]] = text
    return files, conflicts


def write_source_file(out_dir: str, rel_path: str, text: str) -> str:
    target = os.path.join(out_dir, rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(target) or out_dir, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return target


class SourceSnapshot:
    """Keeps the originals of the files a build's hooks overwrite, on local disk.

    The journal is written before the source folder changes, so a process that
    stops mid-build can still put the folder back on the next start.
    """

    def __init__(self, work_dir: str) -> None:
        self.work_dir = work_dir
        self.journal_path = os.path.join(work_dir, SOURCE_JOURNAL)
        self.originals_dir = os.path.join(work_dir, "originals")

    @property
    def pending(self) -> bool:
        return os.path.exists(self.journal_path)

    def apply(self, source_dir: str, files: dict) -> None:
        """Saves the originals, then writes ``files`` into ``source_dir``."""
        self.restore()
        os.makedirs(self.originals_dir, exist_ok=True)
        entries = []
        for number, rel_path in enumerate(sorted(files)):
            target = os.path.join(source_dir, rel_path.replace("/", os.sep))
            saved = None
            if os.path.exists(target):
                saved = str(number)
                shutil.copyfile(target, os.path.join(self.originals_dir, saved))
            entries.append({"rel_path": rel_path, "saved": saved,
                            "new_dirs": _missing_parents(source_dir, rel_path)})
        temp = f"{self.journal_path}.tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump({"source_dir": source_dir, "entries": entries}, f)
        os.replace(temp, self.journal_path)
        for rel_path, text in files.items():
            write_source_file(source_dir, rel_path, text)

    def restore(self) -> None:
        """Puts the source folder back. Safe to repeat after being interrupted."""
        if not self.pending:
            return
        with open(self.journal_path, encoding="utf-8") as f:
            journal = json.load(f)
        source_dir = journal["source_dir"]
        for entry in journal["entries"]:
            target = os.path.join(source_dir, entry["rel_path"].replace("/", os.sep))
            if entry["saved"] is not None:
                # A fresh modified time, so the next build recompiles this file.
                shutil.copyfile(os.path.join(self.originals_dir, entry["saved"]), target)
            elif os.path.exists(target):
                os.remove(target)
        for entry in journal["entries"]:
            for folder in sorted(entry["new_dirs"], key=len, reverse=True):
                try:
                    os.rmdir(os.path.join(source_dir, folder))
                except OSError:
                    pass
        os.remove(self.journal_path)
        shutil.rmtree(self.originals_dir, ignore_errors=True)


def _missing_parents(root: str, rel_path: str) -> list:
    """The parent folders of ``rel_path`` that don't exist yet under ``root``."""
    missing = []
    parent = os.path.dirname(rel_path.replace("/", os.sep))
    while parent:
        if not os.path.isdir(os.path.join(root, parent)):
            missing.append(parent)
        parent = os.path.dirname(parent)
    return missing


# ----------------------------------------------------------------------
# Tracked artifacts and capture
# ----------------------------------------------------------------------
def _split_patterns(patterns: str) -> list:
    return [p.strip().replace("\\", "/") for p in re.split(r"[;,\n]", patterns or "")
            if p.strip()]


def _pattern_regex(pattern: str):
    parts, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        else:
            j = i
            while j < len(pattern) and not pattern.startswith("**", j):
                j += 1
            # fnmatch's '*' crosses '/', which is only right for '**'.
            parts.append(fnmatch.translate(pattern[i:j])[4:-3].replace(".*", "[^/]*"))
            i = j
    return re.compile("".join(parts) + r"\Z", re.IGNORECASE)


def matches_patterns(rel_path: str, include: str, exclude: str) -> bool:
    """Whether a path relative to a tracked folder passes its patterns. A name
    without a '/' matches at any depth, so ``*.hex`` finds nested files too."""
    def hit(pattern: str) -> bool:
        regex = _pattern_regex(pattern if "/" in pattern else f"**/{pattern}")
        return regex.match(rel_path) is not None
    includes = _split_patterns(include)
    if includes and not any(hit(p) for p in includes):
        return False
    return not any(hit(p) for p in _split_patterns(exclude))


def _rel_under(path: str, root: str) -> str:
    rel = os.path.relpath(path, root) if root else os.path.basename(path)
    if rel.startswith(".."):
        rel = os.path.basename(path)
    return rel.replace(os.sep, "/")


def expand_artifacts(artifacts: list, output_dir: str, source_dir: str = "") -> tuple:
    """Resolves a tracked list to files on disk.

    Returns ``(files, missing)``: ``files`` holds dicts with ``path``, ``rel_path``
    and ``role``, and ``missing`` the tracked entries that found nothing.
    """
    files, missing = [], []
    seen = set()

    def add(path: str, rel_path: str, role: str) -> None:
        if role == ROLE_SOURCE:
            rel_path = SOURCE_PREFIX + rel_path
        if rel_path not in seen:
            seen.add(rel_path)
            files.append({"path": path, "rel_path": rel_path, "role": role})

    for artifact in artifacts:
        role = artifact.get("role", ROLE_FLASH)
        root = source_dir if role == ROLE_SOURCE else output_dir
        path = os.path.join(root, artifact["path"]) if root else artifact["path"]
        found = False
        if artifact.get("is_folder"):
            for folder, _dirs, names in os.walk(path):
                for name in names:
                    full = os.path.join(folder, name)
                    within = _rel_under(full, path)
                    if matches_patterns(within, artifact.get("include_patterns", ""),
                                        artifact.get("exclude_patterns", "")):
                        add(full, _rel_under(full, root), role)
                        found = True
        elif os.path.isfile(path):
            add(path, _rel_under(path, root), role)
            found = True
        if not found:
            missing.append(artifact["path"])
    return files, missing


def source_artifacts_for(db, build: dict) -> list:
    """The default Source role: the files the hooks modify, plus helper files."""
    if not build.get("test_project_id"):
        return []
    tpid = build["test_project_id"]
    paths = {h["src_file_path"] for h in db.list_injections(tpid)}
    paths |= {f["rel_path"] for f in db.list_test_project_files(tpid)}
    return [{"path": p, "role": ROLE_SOURCE} for p in sorted(paths)]


def capture_build(db, store: BinaryStore, build: dict, captured_at: str,
                  output_dir: Optional[str] = None) -> dict:
    """Saves a build's tracked files and writes its manifest.

    Anything missing marks the capture Incomplete, rather than quietly saving a
    partial set. Objects are kept for Main only, so a late .o import can use
    clean output.
    """
    artifacts = [a for a in db.tracked_artifacts_for_build(build["id"])
                 if build["is_main"] or a.get("role") != ROLE_OBJECTS]
    if not any(a.get("role") == ROLE_SOURCE for a in artifacts):
        artifacts = artifacts + source_artifacts_for(db, build)
    files, missing = expand_artifacts(
        artifacts, output_dir or build["output_dir"], build["source_dir"])
    entries = []
    for f in files:
        saved = store.put_file(f["path"])
        entries.append({"rel_path": f["rel_path"], "role": f["role"], **saved})
    status = CAPTURE_INCOMPLETE if missing or not entries else CAPTURE_COMPLETE
    db.replace_build_manifest(build["id"], entries, status, captured_at)
    return {"status": status, "files": len(entries), "missing": missing,
            "size": sum(e["size"] for e in entries)}


# ----------------------------------------------------------------------
# Pre-flight
# ----------------------------------------------------------------------
def plan_run(db, build_ids: list, release_names: Optional[dict] = None) -> list:
    """Orders a run: each release's missing Main capture, then its test builds.

    A Main capture that already exists isn't rebuilt. A new Main row takes its
    build settings from the release's first test build.
    """
    builds = [b for b in (db.get_test_build(i) for i in build_ids) if b is not None]
    releases: dict = {}
    for b in builds:
        releases.setdefault(b["release_id"], []).append(b)
    items = []
    for release_id, members in releases.items():
        tests = [b for b in members if not b["is_main"]]
        main = db.get_main_build(release_id)
        if main is None or main["capture_status"] != CAPTURE_COMPLETE:
            items.append({"test_build_id": main["id"] if main else None,
                          "build_name": "Main", "release_id": release_id,
                          "is_main": True,
                          "template": tests[0] if tests else None})
        items.extend({"test_build_id": b["id"], "build_name": b["name"],
                      "release_id": release_id, "is_main": False} for b in tests)
    return items


def ensure_main_build(db, release_id: int, template: Optional[dict]) -> dict:
    """The release's Main row, created from ``template`` or filled in from it."""
    settings = {}
    if template:
        settings = {k: template[k] for k in (
            "build_command", "build_cwd", "clean_command", "terminal", "wsl_distro",
            "source_dir", "output_dir")}
    main = db.get_main_build(release_id)
    if main is None:
        db.create_test_build(release_id, "Main", is_main=True, **settings)
    else:
        empty = {k: v for k, v in settings.items() if not main.get(k)}
        if empty:
            db.update_test_build(main["id"], **empty)
    return db.get_main_build(release_id)


def preflight(db, store: BinaryStore, build_ids: list, overridden_releases: tuple = (),
              release_names: Optional[dict] = None) -> dict:
    """Checks everything that can be known before a run starts.

    Returns ``problems`` per build name and release, ``warnings`` that don't stop
    a build, and the planned items. The user can drop the builds with problems
    and start the rest.
    """
    names = release_names or {}
    items = plan_run(db, build_ids)
    problems, warnings = [], []
    reachable = store.is_reachable()
    needed = 0
    locks = {}

    def problem(item: dict, text: str) -> None:
        problems.append({"test_build_id": item["test_build_id"],
                         "build_name": item["build_name"],
                         "release_id": item["release_id"], "problem": text})

    for item in items:
        release_id = item["release_id"]
        if release_id not in locks:
            locks[release_id] = clean_lock_status(db, release_id, names.get(release_id, ""))
        lock = locks[release_id]
        build = (db.get_test_build(item["test_build_id"]) if item["test_build_id"]
                 else None) or item.get("template")
        if item["is_main"] and build is None:
            problem(item, "No build settings for Main. Add a test build or set them on Main.")
            continue
        if not lock["ok"] and release_id not in overridden_releases:
            problem(item, lock["message"])
        if not build["build_command"].strip():
            problem(item, "No build command is set.")
        if not build["build_cwd"].strip():
            problem(item, "No working directory is set.")
        if not build["output_dir"].strip():
            problem(item, "No build output folder is set.")
        if not (item["is_main"] or build["source_dir"].strip()):
            problem(item, "No source folder is set.")
        if not build["clean_command"].strip():
            warnings.append({"build_name": item["build_name"],
                             "warning": "No clean command is set, so stale objects "
                                        "can end up in this build."})
        scope_id = build.get("id")
        tracked = (db.tracked_artifacts_for_build(scope_id) if scope_id
                   else db.list_tracked_artifacts(None))
        if not tracked:
            problem(item, "No tracked artifacts are defined.")
        if not item["is_main"]:
            provider = release_source_provider(db, release_id)
            hooks = db.list_injections(build["test_project_id"]) if build.get(
                "test_project_id") else []
            if hooks and provider is None:
                problem(item, "Hooks can't be applied without the release's source.")
            elif hooks:
                _, conflicts = render_hooked_files(db, provider, build["test_project_id"])
                if conflicts:
                    problem(item, f"{len(conflicts)} hook(s) don't resolve on this release.")
        if scope_id:
            needed += db.get_build_manifest_size(scope_id)

    if not reachable:
        problems.append({"test_build_id": None, "build_name": "", "release_id": None,
                         "problem": "The binary store can't be reached."})
    else:
        free = store.free_bytes()
        if free is not None and free < needed * SPACE_MARGIN:
            problems.append({"test_build_id": None, "build_name": "", "release_id": None,
                             "problem": "The binary store doesn't have enough free "
                                        "space, based on the last build size."})
    return {"items": [{k: v for k, v in it.items() if k != "template"} for it in items],
            "problems": problems, "warnings": warnings,
            "locks": list(locks.values())}


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def build_argv(terminal: str, command: str, cwd: str, distro: str) -> list:
    """Construct the platform/terminal-appropriate argv for ``command``."""
    if terminal == "cmd":
        return ["cmd.exe", "/c", command]
    if terminal == "powershell":
        return ["powershell.exe", "-NoProfile", "-Command", command]
    if terminal == "wsl":
        inner = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        argv = ["wsl.exe"]
        if distro:
            argv += ["-d", distro]
        argv += ["--", "sh", "-c", inner]
        return argv
    # bash (default)
    return ["bash", "-lc", command]


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Stops a build and the compiler processes under its shell."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass


def run_command(argv: list, cwd: Optional[str], terminal: str,
                on_line: Callable[[str], None], stop: threading.Event) -> int:
    """Runs a command to the end or until ``stop`` is set, returning its exit code."""
    popen_cwd = None if (terminal == "wsl" or not cwd) else cwd
    kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(
            argv, cwd=popen_cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace", **kwargs)
    except (OSError, ValueError) as e:
        on_line(f"Failed to launch: {e}")
        return -1

    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(_STOP_POLL_SECONDS):
            if stop.is_set():
                _terminate_tree(proc)
                return

    threading.Thread(target=watch, name="build-stop", daemon=True).start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait()
    finally:
        finished.set()


# ----------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------
@dataclass
class RunContext:
    db: object
    store: BinaryStore
    work_dir: str
    now: Callable[[], str]
    on_line: Callable[[dict, str], None] = lambda _item, _line: None
    progress: Callable[..., None] = lambda *_a, **_k: None
    cancel: threading.Event = field(default_factory=threading.Event)
    # True once the session has lost the edit lock: nothing more may be written.
    lock_lost: Callable[[], bool] = lambda: False
    command_runner: Callable = run_command


def start_run(db, username: str, items: list) -> int:
    """Records a planned run, creating any Main rows it needs."""
    rows = []
    for item in items:
        build_id = item["test_build_id"]
        if item["is_main"]:
            build_id = ensure_main_build(db, item["release_id"], item.get("template"))["id"]
        rows.append({"test_build_id": build_id, "build_name": item["build_name"],
                     "release_id": item["release_id"]})
    return db.create_build_run(username, rows)


def _run_step(ctx: RunContext, item: dict, build: dict, command: str,
              log: list) -> int:
    def on_line(line: str) -> None:
        log.append(line)
        ctx.on_line(item, line)
    argv = build_argv(build["terminal"], command, build["build_cwd"], build["wsl_distro"])
    return ctx.command_runner(argv, build["build_cwd"], build["terminal"], on_line, ctx.cancel)


def _build_one(ctx: RunContext, item: dict, snapshot: SourceSnapshot,
               failed_mains: set) -> None:
    db = ctx.db
    build = db.get_test_build(item["test_build_id"])
    if build is None:
        db.update_build_run_item(item["id"], status=ITEM_SKIPPED,
                                 message="This build was deleted.", ended_at=ctx.now())
        return
    if not build["is_main"] and build["release_id"] in failed_mains:
        db.update_build_run_item(
            item["id"], status=ITEM_SKIPPED, ended_at=ctx.now(),
            message="Main failed, so there would be nothing clean to switch back to.")
        return

    db.update_build_run_item(item["id"], status=ITEM_RUNNING, started_at=ctx.now())
    conflicts: list = []
    log: list = []

    def finish(status: str, message: str = "", returncode=None) -> None:
        if build["is_main"] and status != ITEM_PASSED:
            failed_mains.add(build["release_id"])
        db.update_build_run_item(
            item["id"], status=status, message=message, returncode=returncode,
            conflicts=json.dumps(conflicts), log="\n".join(log).encode("utf-8"),
            ended_at=ctx.now())

    if not build["is_main"] and build["test_project_id"]:
        provider = release_source_provider(db, build["release_id"])
        hooks = db.list_injections(build["test_project_id"])
        if hooks and provider is None:
            finish(ITEM_SKIPPED, "The release's source isn't imported, so its hooks "
                                 "can't be applied.")
            return
        files, conflicts = render_hooked_files(db, provider, build["test_project_id"])
        snapshot.apply(build["source_dir"], files)

    returncode = 0
    if build["clean_command"].strip():
        log.append(f"$ {build['clean_command']}")
        returncode = _run_step(ctx, item, build, build["clean_command"], log)
    if returncode == 0 and not ctx.cancel.is_set():
        log.append(f"$ {build['build_command']}")
        returncode = _run_step(ctx, item, build, build["build_command"], log)

    if ctx.cancel.is_set():
        finish(ITEM_CANCELLED, "Cancelled.", returncode)
    elif returncode != 0:
        finish(ITEM_FAILED, f"The build ended with exit code {returncode}.", returncode)
    elif ctx.lock_lost():
        # Another session owns the project now. The item stays open for a resume.
        return
    else:
        try:
            result = capture_build(db, ctx.store, build, ctx.now())
        except (StoreError, OSError) as e:
            finish(ITEM_INCOMPLETE, f"The binaries couldn't be saved: {e}", returncode)
            return
        if result["status"] == CAPTURE_COMPLETE:
            count = result["files"]
            finish(ITEM_PASSED, f"Saved {count} file{'' if count == 1 else 's'}.", returncode)
        else:
            listed = ", ".join(result["missing"]) or "no tracked files were found"
            finish(ITEM_INCOMPLETE, f"Missing: {listed}", returncode)


def execute_run(ctx: RunContext, run_id: int) -> dict:
    """Runs a recorded run's open items in order. Also used to resume one.

    A failed build doesn't stop the series. The source folder is put back before
    the first build, after each build, and however the run ends.
    """
    db = ctx.db
    os.makedirs(ctx.work_dir, exist_ok=True)
    snapshot = SourceSnapshot(ctx.work_dir)
    snapshot.restore()

    run = db.get_build_run(run_id)
    for item in run["items"]:
        if item["status"] == ITEM_RUNNING:
            db.update_build_run_item(item["id"], status=ITEM_PENDING)
    # A resumed run must still skip builds whose Main didn't finish.
    failed_mains = {
        item["release_id"] for item in run["items"]
        if item["status"] in (ITEM_FAILED, ITEM_INCOMPLETE, ITEM_SKIPPED, ITEM_CANCELLED)
        and item["build_name"] == "Main"
    }
    items = [i for i in db.get_build_run(run_id)["items"] if i["status"] == ITEM_PENDING]
    total = len(items) or 1
    for done, item in enumerate(items):
        if ctx.lock_lost():
            snapshot.restore()
            return {"run_id": run_id, "stopped": "lock_lost"}
        if ctx.cancel.is_set():
            db.update_build_run_item(item["id"], status=ITEM_CANCELLED,
                                     message="Cancelled.", ended_at=ctx.now())
            continue
        ctx.progress(f"Building {item['build_name']} ({done + 1} of {len(items)})",
                     100.0 * done / total)
        try:
            _build_one(ctx, item, snapshot, failed_mains)
        finally:
            snapshot.restore()
    if ctx.lock_lost():
        return {"run_id": run_id, "stopped": "lock_lost"}
    db.finish_build_run(run_id, RUN_CANCELLED if ctx.cancel.is_set() else RUN_DONE)
    shutil.rmtree(ctx.work_dir, ignore_errors=True)
    return {"run_id": run_id, **_summary(db.get_build_run(run_id))}


def _summary(run: dict) -> dict:
    counts: dict = {}
    for item in run["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"status": run["status"], "counts": counts}
