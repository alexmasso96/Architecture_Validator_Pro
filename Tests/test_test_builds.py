"""Logic-layer tests for Test Builds (#15): lock, capture, restore and the run."""
import hashlib
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Test_Builds as tb
from Application_Logic.Logic_Binary_Store import BinaryStore
from Tests.test_helpers import make_project_db

SOURCE = "void main(void)\n{\n    int x = 1;\n    int y = 2;\n}\n"


def _save_source(db, release_id, text):
    db.save_release_source_files(release_id, [("main.c", text)])


class Fixture:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.db = make_project_db(
            str(tmp_path / "p.arch"), layout=[("TC. ID", "Static Text", True)],
            models=[{"name": "A", "status": "In Work", "rows": []}],
            releases=[{"name": "R1"}, {"name": "R2"}])
        rows = self.db._conn.execute("SELECT id, name FROM releases ORDER BY id").fetchall()
        self.r1, self.r2 = rows[0][0], rows[1][0]
        self.store = BinaryStore(str(tmp_path / "p.arch.binaries"))
        self.source = tmp_path / "tree"
        self.output = tmp_path / "out"
        self.source.mkdir()
        self.output.mkdir()
        (self.source / "main.c").write_text(SOURCE)
        _save_source(self.db, self.r1, SOURCE)
        self.db.replace_tracked_artifacts(None, [
            {"path": "app.hex", "role": "Flash"},
            {"path": "app.elf", "role": "Symbols"}])

    def test_build(self, name="Watchdog", release=None, hook=True, **fields):
        tpid = self.db.create_test_project(name)
        if hook:
            self.db.add_injection(tpid, "main.c", "main", "int x = 1;", "int y = 2;",
                                  "    while(1);")
        settings = dict(build_command="make", build_cwd=str(self.source),
                        clean_command="make clean", source_dir=str(self.source),
                        output_dir=str(self.output))
        settings.update(fields)
        return self.db.create_test_build(release or self.r1, name, tpid, **settings)

    def context(self, runner, **kw):
        n = [0]

        def now():
            n[0] += 1
            return f"t{n[0]}"
        return tb.RunContext(db=self.db, store=self.store,
                             work_dir=str(self.tmp / "work"), now=now,
                             command_runner=runner, **kw)

    def run(self, build_ids, runner, **kw):
        items = tb.plan_run(self.db, build_ids)
        run_id = tb.start_run(self.db, "alex", items)
        return run_id, tb.execute_run(self.context(runner, **kw), run_id)


@pytest.fixture()
def fx(tmp_path):
    f = Fixture(tmp_path)
    yield f
    f.db.close()


def fake_build(fx, fail=(), seen=None, make_elf=True):
    """A build that compiles whatever is in the source tree into app.hex."""
    def runner(argv, cwd, terminal, on_line, stop):
        command = argv[-1]
        on_line(f"running {command}")
        if command == "make clean":
            for f in fx.output.iterdir():
                f.unlink()
            return 0
        text = (fx.source / "main.c").read_text()
        if seen is not None:
            seen.append(text)
        if any(marker in text for marker in fail) or (not text and fail):
            return 2
        if "main" in fail and "while" not in text:
            return 2
        (fx.output / "app.hex").write_text("HEX:" + text)
        if make_elf:
            (fx.output / "app.elf").write_text("ELF")
        return 0
    return runner


# -- clean-release lock --------------------------------------------------
def test_clean_lock_names_what_is_missing(fx):
    status = tb.clean_lock_status(fx.db, fx.r1, "R1")
    assert status["ok"] is False
    assert status["missing"] == [".o files"]
    assert ".o files" in status["message"] and "R1" in status["message"]
    status = tb.clean_lock_status(fx.db, fx.r2, "R2")
    assert status["missing"] == ["source", ".o files"]


# -- patterns and capture ------------------------------------------------
@pytest.mark.parametrize("rel,include,exclude,expected", [
    ("a.hex", "**/*.hex", "", True),
    ("deep/er/a.hex", "**/*.hex", "", True),
    ("a.hex", "*.hex", "", True),
    ("deep/a.hex", "*.hex", "", True),
    ("deep/a.hex", "top/*.hex", "", False),
    ("a.map", "**/*.hex", "", False),
    ("a.hex", "", "", True),
    ("tmp/a.hex", "**/*.hex", "tmp/**", False),
    ("a.HEX", "*.hex", "", True),
    ("a.hex", "*.bin; *.hex", "", True),
])
def test_patterns(rel, include, exclude, expected):
    assert tb.matches_patterns(rel, include, exclude) is expected


def test_capture_is_incomplete_when_a_tracked_file_is_missing(fx):
    build = fx.db.get_test_build(fx.test_build())
    (fx.output / "app.hex").write_text("x")
    result = tb.capture_build(fx.db, fx.store, build, "now")
    assert result["status"] == tb.CAPTURE_INCOMPLETE
    assert result["missing"] == ["app.elf"]


def test_capture_folder_with_patterns_and_build_override(fx):
    build_id = fx.test_build(hook=False)
    fx.db.replace_tracked_artifacts(build_id, [
        {"path": "bin", "is_folder": True, "include_patterns": "**/*.hex", "role": "Flash"}])
    (fx.output / "bin" / "sub").mkdir(parents=True)
    (fx.output / "bin" / "sub" / "a.hex").write_text("1")
    (fx.output / "bin" / "skip.map").write_text("2")
    build = fx.db.get_test_build(build_id)
    assert tb.capture_build(fx.db, fx.store, build, "now")["status"] == tb.CAPTURE_COMPLETE
    assert [m["rel_path"] for m in fx.db.get_build_manifest(build_id)] == ["bin/sub/a.hex"]


def test_capture_defaults_source_role_to_hooked_and_helper_files(fx):
    build_id = fx.test_build()
    build = fx.db.get_test_build(build_id)
    fx.db.import_test_project_files(build["test_project_id"], [("wd.c", "int wd;")])
    (fx.source / "wd.c").write_text("int wd;")
    (fx.output / "app.hex").write_text("x")
    (fx.output / "app.elf").write_text("y")
    tb.capture_build(fx.db, fx.store, build, "now")
    manifest = {m["rel_path"]: m["role"] for m in fx.db.get_build_manifest(build_id)}
    assert manifest["source/main.c"] == "Source"
    assert manifest["source/wd.c"] == "Source"


def test_objects_are_captured_for_main_only(fx):
    fx.db.replace_tracked_artifacts(None, [
        {"path": "app.hex", "role": "Flash"}, {"path": "a.o", "role": "Objects"}])
    (fx.output / "app.hex").write_text("x")
    (fx.output / "a.o").write_text("o")
    test = fx.db.get_test_build(fx.test_build(hook=False))
    main_id = fx.db.create_test_build(fx.r1, "Main", is_main=True,
                                      output_dir=str(fx.output))
    tb.capture_build(fx.db, fx.store, test, "now")
    tb.capture_build(fx.db, fx.store, fx.db.get_test_build(main_id), "now")
    assert "a.o" not in {m["rel_path"] for m in fx.db.get_build_manifest(test["id"])}
    assert "a.o" in {m["rel_path"] for m in fx.db.get_build_manifest(main_id)}


# -- source snapshot -----------------------------------------------------
def test_snapshot_restores_changed_and_removes_added_files(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.c").write_text("original")
    snap = tb.SourceSnapshot(str(tmp_path / "work"))
    os.makedirs(snap.work_dir)
    snap.apply(str(source), {"a.c": "hooked", "new/dir/h.c": "helper"})
    assert (source / "a.c").read_text() == "hooked"
    assert (source / "new" / "dir" / "h.c").exists()
    snap.restore()
    assert (source / "a.c").read_text() == "original"
    assert sorted(p.name for p in source.iterdir()) == ["a.c"]
    assert snap.pending is False


def test_snapshot_restore_survives_a_stop_and_a_new_process(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.c").write_text("original")
    work = tmp_path / "work"
    os.makedirs(work)
    tb.SourceSnapshot(str(work)).apply(str(source), {"a.c": "hooked"})
    # A new object, as after a restart, finds the journal and repeats safely.
    fresh = tb.SourceSnapshot(str(work))
    assert fresh.pending is True
    fresh.restore()
    fresh.restore()
    assert (source / "a.c").read_text() == "original"


# -- the run -------------------------------------------------------------
def test_hooks_never_reach_the_next_build(fx):
    first = fx.test_build("Watchdog")
    second = fx.test_build("MPU", hook=False)
    seen = []
    fx.run([first, second], fake_build(fx, seen=seen))
    assert any("while(1);" in text for text in seen)
    assert "while(1);" not in seen[-1]


def test_run_builds_main_first_and_leaves_the_source_as_it_was(fx):
    build_id = fx.test_build()
    seen = []
    run_id, result = fx.run([build_id], fake_build(fx, seen=seen))
    run = fx.db.get_build_run(run_id)
    assert [i["build_name"] for i in run["items"]] == ["Main", "Watchdog"]
    assert [i["status"] for i in run["items"]] == ["passed", "passed"]
    assert "while(1);" not in seen[0]
    assert "while(1);" in seen[1]
    assert (fx.source / "main.c").read_text() == SOURCE
    assert result["status"] == "done"
    assert fx.db.get_main_build(fx.r1)["capture_status"] == "complete"
    assert not (fx.tmp / "work").exists()


def test_existing_main_capture_is_not_rebuilt(fx):
    build_id = fx.test_build()
    fx.run([build_id], fake_build(fx))
    run_id, _ = fx.run([build_id], fake_build(fx))
    assert [i["build_name"] for i in fx.db.get_build_run(run_id)["items"]] == ["Watchdog"]


def test_failed_build_does_not_stop_the_run(fx):
    bad = fx.test_build("Bad")
    fx.db.add_injection(fx.db.get_test_build(bad)["test_project_id"], "main.c", "main",
                        "int y = 2;", "}", "BROKEN")
    good = fx.test_build("Good", hook=False)
    run_id, _ = fx.run([bad, good], fake_build(fx, fail=("BROKEN",)))
    items = {i["build_name"]: i for i in fx.db.get_build_run(run_id)["items"]}
    assert items["Bad"]["status"] == "failed"
    assert items["Bad"]["returncode"] == 2
    assert items["Good"]["status"] == "passed"
    assert (fx.source / "main.c").read_text() == SOURCE


def test_failed_main_skips_that_releases_builds(fx):
    build_id = fx.test_build()

    def always_fails(argv, cwd, terminal, on_line, stop):
        return 0 if argv[-1] == "make clean" else 1
    run_id, _ = fx.run([build_id], always_fails)
    statuses = {i["build_name"]: i["status"] for i in fx.db.get_build_run(run_id)["items"]}
    assert statuses == {"Main": "failed", "Watchdog": "skipped"}


def test_missing_tracked_file_marks_the_item_incomplete(fx):
    build_id = fx.test_build()
    run_id, _ = fx.run([build_id], fake_build(fx, make_elf=False))
    item = fx.db.get_build_run(run_id)["items"][-1]
    assert item["status"] == "skipped"  # Main was incomplete too
    assert fx.db.get_main_build(fx.r1)["capture_status"] == "incomplete"


def test_build_log_is_saved(fx):
    run_id, _ = fx.run([fx.test_build()], fake_build(fx))
    item = fx.db.get_build_run(run_id)["items"][-1]
    log = fx.db.read_build_run_log(item["id"])
    assert "running make clean" in log and "$ make" in log


def test_hook_conflicts_are_recorded(fx):
    build_id = fx.test_build()
    tpid = fx.db.get_test_build(build_id)["test_project_id"]
    fx.db.add_injection(tpid, "main.c", "main", "no such", "line either", "X")
    run_id, _ = fx.run([build_id], fake_build(fx))
    assert len(fx.db.get_build_run(run_id)["items"][-1]["conflicts"]) == 1


def test_test_builds_leave_the_release_data_alone(fx):
    before = fx.db._conn.execute(
        "SELECT (SELECT COUNT(*) FROM release_symbol_files), "
        "(SELECT COUNT(*) FROM release_code_maps), (SELECT COUNT(*) FROM release_objects), "
        "(SELECT COUNT(*) FROM release_results), (SELECT COUNT(*) FROM elf_index)").fetchone()
    fx.run([fx.test_build()], fake_build(fx))
    after = fx.db._conn.execute(
        "SELECT (SELECT COUNT(*) FROM release_symbol_files), "
        "(SELECT COUNT(*) FROM release_code_maps), (SELECT COUNT(*) FROM release_objects), "
        "(SELECT COUNT(*) FROM release_results), (SELECT COUNT(*) FROM elf_index)").fetchone()
    assert before == after


def test_cancel_stops_the_run_and_restores_the_source(fx):
    first = fx.test_build("One")
    second = fx.test_build("Two")
    cancel = threading.Event()
    base = fake_build(fx)

    def cancels_during_one(argv, cwd, terminal, on_line, stop):
        if argv[-1] == "make" and "while" in (fx.source / "main.c").read_text():
            cancel.set()
            return -15
        return base(argv, cwd, terminal, on_line, stop)
    run_id, result = fx.run([first, second], cancels_during_one, cancel=cancel)
    run = fx.db.get_build_run(run_id)
    assert run["status"] == "cancelled"
    assert {i["build_name"]: i["status"] for i in run["items"]} == {
        "Main": "passed", "One": "cancelled", "Two": "cancelled"}
    assert (fx.source / "main.c").read_text() == SOURCE


def test_interrupted_run_resumes_from_the_first_open_build(fx):
    first = fx.test_build("One")
    second = fx.test_build("Two")
    fx.db.create_test_build(fx.r1, "Main", is_main=True, build_command="make",
                            build_cwd=str(fx.source), source_dir=str(fx.source),
                            output_dir=str(fx.output))
    items = tb.plan_run(fx.db, [first, second])
    run_id = tb.start_run(fx.db, "alex", items)

    class Killed(BaseException):
        pass
    base = fake_build(fx)

    def killed_during_two(argv, cwd, terminal, on_line, stop):
        if argv[-1] == "make" and "One" not in cwd:
            text = (fx.source / "main.c").read_text()
            builds.append(text)
            if len(builds) == 3:
                killed.append(True)
                raise Killed()
        return base(argv, cwd, terminal, on_line, stop)
    builds, killed = [], []
    # A stop that bypasses the normal restore, like a killed process.
    real_restore = tb.SourceSnapshot.restore

    def restore_until_killed(self):
        if not killed:
            real_restore(self)
    tb.SourceSnapshot.restore = restore_until_killed
    try:
        with pytest.raises(Killed):
            tb.execute_run(fx.context(killed_during_two), run_id)
    finally:
        tb.SourceSnapshot.restore = real_restore
    assert "while(1);" in (fx.source / "main.c").read_text()

    seen = []
    tb.execute_run(fx.context(fake_build(fx, seen=seen)), run_id)
    run = fx.db.get_build_run(run_id)
    assert [i["status"] for i in run["items"]] == ["passed", "passed", "passed"]
    assert len(seen) == 1
    assert (fx.source / "main.c").read_text() == SOURCE
    assert run["status"] == "done"


def test_lost_lock_stops_without_writing_and_leaves_the_run_open(fx):
    lost = threading.Event()
    build_id = fx.test_build()
    base = fake_build(fx)

    def loses_lock(argv, cwd, terminal, on_line, stop):
        lost.set()
        return base(argv, cwd, terminal, on_line, stop)
    run_id, result = fx.run([build_id], loses_lock, lock_lost=lost.is_set)
    assert result["stopped"] == "lock_lost"
    run = fx.db.get_build_run(run_id)
    assert run["status"] == "running"
    assert (fx.source / "main.c").read_text() == SOURCE


# -- pre-flight ----------------------------------------------------------
def test_preflight_lists_problems_per_build(fx):
    build_id = fx.test_build(build_command="", output_dir="")
    report = tb.preflight(fx.db, fx.store, [build_id])
    text = [p["problem"] for p in report["problems"] if p["build_name"] == "Watchdog"]
    assert any("build command" in t for t in text)
    assert any("output folder" in t for t in text)
    assert any(".o files" in t for t in text)


def test_preflight_passes_once_the_lock_is_overridden(fx):
    build_id = fx.test_build()
    report = tb.preflight(fx.db, fx.store, [build_id], overridden_releases=(fx.r1,))
    assert report["problems"] == []
    assert [i["build_name"] for i in report["items"]] == ["Main", "Watchdog"]


def test_preflight_shows_hooks_that_dont_resolve_on_a_release(fx):
    tpid = fx.db.create_test_project("Shared")
    fx.db.add_injection(tpid, "main.c", "main", "int x = 1;", "int y = 2;", "X")
    _save_source(fx.db, fx.r2, "void main(void) {}\n")
    build_id = fx.db.create_test_build(
        fx.r2, "Watchdog", tpid, build_command="make", build_cwd=str(fx.source),
        source_dir=str(fx.source), output_dir=str(fx.output))
    report = tb.preflight(fx.db, fx.store, [build_id], overridden_releases=(fx.r2,))
    assert any("don't resolve" in p["problem"] for p in report["problems"])


def test_preflight_reports_an_unreachable_store(fx, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    report = tb.preflight(fx.db, BinaryStore(str(blocker / "s")), [fx.test_build()],
                          overridden_releases=(fx.r1,))
    assert any("can't be reached" in p["problem"] for p in report["problems"])


# -- real commands -------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command")
def test_run_command_streams_output_and_returns_the_exit_code():
    lines = []
    code = tb.run_command(["bash", "-c", "echo one; echo two; exit 3"], None, "bash",
                          lines.append, threading.Event())
    assert code == 3
    assert lines == ["one", "two"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_run_command_stop_ends_the_child_processes():
    stop = threading.Event()
    lines = []

    def on_line(line):
        lines.append(line)
        stop.set()
    code = tb.run_command(["bash", "-c", "echo started; sleep 30 & wait"], None, "bash",
                          on_line, stop)
    assert code != 0
