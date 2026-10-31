"""Logic-layer tests for mapped release switching (#15)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Mapped_Release as mr
from Application_Logic.Logic_Binary_Store import BinaryStore, sha256_file


class Bench:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.store = BinaryStore(str(tmp_path / "store"))
        self.folder = tmp_path / "mapped"
        self.folder.mkdir()
        (tmp_path / "in").mkdir()

    def manifest(self, files, role="Flash"):
        entries = []
        for rel_path, data in files.items():
            path = self.tmp / "in" / "f"
            path.write_bytes(data)
            saved = self.store.put_file(str(path))
            entries.append({"rel_path": rel_path, "role": role, **saved})
        return entries

    def switch(self, entries, name="Build", **kw):
        return mr.switch(str(self.folder), self.store, entries,
                         {"build_name": name, "release_name": "R1"}, **kw)

    def snapshot(self):
        out = {}
        for root, _dirs, names in os.walk(self.folder):
            for name in names:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, self.folder).replace(os.sep, "/")
                if not rel.startswith(".arch_"):
                    out[rel] = open(path, "rb").read()
        return out


@pytest.fixture()
def bench(tmp_path):
    return Bench(tmp_path)


MAIN = {"app.hex": b"main-hex", "app.elf": b"elf", "maps/app.map": b"map"}
WATCHDOG = {"app.hex": b"watchdog-hex", "app.elf": b"elf", "wd.sym": b"sym"}


def test_switching_there_and_back_is_byte_identical(bench):
    main = bench.manifest(MAIN)
    watchdog = bench.manifest(WATCHDOG)
    bench.switch(main, "Main")
    original = bench.snapshot()
    bench.switch(watchdog, "Watchdog")
    assert bench.snapshot() == WATCHDOG
    bench.switch(main, "Main")
    assert bench.snapshot() == original == MAIN


def test_only_changed_files_are_transferred(bench, monkeypatch):
    bench.switch(bench.manifest(MAIN), "Main")
    downloads = []
    real = bench.store.read_to
    monkeypatch.setattr(bench.store, "read_to",
                        lambda sha, target: (downloads.append(target), real(sha, target)))
    result = bench.switch(bench.manifest(WATCHDOG), "Watchdog")
    assert len(downloads) == result["transferred"] == 2
    assert result["unchanged"] == 1
    assert result["removed"] == 1


def test_state_file_records_the_build_and_journal_and_temp_are_gone(bench):
    bench.switch(bench.manifest(MAIN), "Main")
    state = mr.read_state(str(bench.folder))
    assert state["build_name"] == "Main"
    assert state["files"]["app.hex"] == bench.manifest(MAIN)[0]["sha256"]
    assert sorted(p.name for p in bench.folder.iterdir()) == [
        ".arch_mapped.json", "app.elf", "app.hex", "maps"]


def test_file_changed_outside_the_app_isnt_overwritten_without_asking(bench):
    bench.switch(bench.manifest(MAIN), "Main")
    (bench.folder / "app.hex").write_bytes(b"manual rebuild")
    with pytest.raises(mr.SwitchError) as e:
        bench.switch(bench.manifest(WATCHDOG), "Watchdog")
    assert e.value.files == ["app.hex"]
    assert (bench.folder / "app.hex").read_bytes() == b"manual rebuild"
    bench.switch(bench.manifest(WATCHDOG), "Watchdog", allow_outside_changes=True)
    assert (bench.folder / "app.hex").read_bytes() == b"watchdog-hex"


def test_first_switch_asks_about_files_already_in_the_folder(bench):
    (bench.folder / "app.hex").write_bytes(b"built by hand")
    with pytest.raises(mr.SwitchError):
        bench.switch(bench.manifest(MAIN), "Main")


def test_files_the_app_didnt_place_are_left_alone(bench):
    (bench.folder / "debugger.ini").write_bytes(b"settings")
    main = bench.manifest(MAIN)
    bench.switch(main, "Main")
    bench.switch(bench.manifest(WATCHDOG), "Watchdog")
    assert (bench.folder / "debugger.ini").read_bytes() == b"settings"


def test_dropped_nas_during_download_leaves_the_folder_unchanged(bench, monkeypatch):
    main = bench.manifest(MAIN)
    bench.switch(main, "Main")
    before = bench.snapshot()
    watchdog = bench.manifest(WATCHDOG)
    calls = []
    real = bench.store.read_to

    def drops(sha, target):
        calls.append(sha)
        if len(calls) == 2:
            raise OSError("network name no longer available")
        real(sha, target)
    monkeypatch.setattr(bench.store, "read_to", drops)
    with pytest.raises(mr.SwitchError, match="wasn't changed"):
        bench.switch(watchdog, "Watchdog")
    assert bench.snapshot() == before
    assert mr.interrupted_switch(str(bench.folder)) is None
    assert not (bench.folder / mr.TEMP_DIR).exists()


def test_held_file_is_named_and_nothing_changes(bench):
    main = bench.manifest(MAIN)
    bench.switch(main, "Main")
    before = bench.snapshot()
    with pytest.raises(mr.SwitchError) as e:
        bench.switch(bench.manifest(WATCHDOG), "Watchdog",
                     held=lambda p: p.endswith("app.hex"))
    assert e.value.files == ["app.hex"]
    assert "debugger" in str(e.value)
    assert bench.snapshot() == before


def test_rename_failure_midway_undoes_the_switch(bench, monkeypatch):
    main = bench.manifest(MAIN)
    bench.switch(main, "Main")
    before = bench.snapshot()
    watchdog = bench.manifest(WATCHDOG)
    real = os.replace
    calls = []

    def fails_once(src, dst):
        calls.append(dst)
        if len(calls) == 3:
            raise PermissionError("locked just now")
        real(src, dst)
    monkeypatch.setattr(mr.os, "replace", fails_once)
    with pytest.raises(mr.SwitchError, match="undone"):
        bench.switch(watchdog, "Watchdog")
    monkeypatch.setattr(mr.os, "replace", real)
    assert bench.snapshot() == before
    assert mr.interrupted_switch(str(bench.folder)) is None


class Killed(BaseException):
    """Like a killed process: nothing after this point runs."""


def _kill_during_swap(bench, monkeypatch, entries, after):
    real = os.replace
    calls = []

    def killed(src, dst):
        calls.append(dst)
        if len(calls) > after:
            raise Killed()
        real(src, dst)
    monkeypatch.setattr(mr.os, "replace", killed)
    with pytest.raises(Killed):
        bench.switch(entries, "Watchdog")
    monkeypatch.setattr(mr.os, "replace", real)


@pytest.mark.parametrize("after", [1, 2, 3, 4, 5])
def test_killed_switch_can_be_finished(bench, monkeypatch, after):
    bench.switch(bench.manifest(MAIN), "Main")
    _kill_during_swap(bench, monkeypatch, bench.manifest(WATCHDOG), after)
    assert mr.interrupted_switch(str(bench.folder))["target"] == "Watchdog"
    mr.recover(str(bench.folder), mr.RECOVER_FINISH)
    assert bench.snapshot() == WATCHDOG
    assert mr.read_state(str(bench.folder))["build_name"] == "Watchdog"
    assert mr.interrupted_switch(str(bench.folder)) is None


@pytest.mark.parametrize("after", [1, 2, 3, 4, 5])
def test_killed_switch_can_go_back(bench, monkeypatch, after):
    bench.switch(bench.manifest(MAIN), "Main")
    before = bench.snapshot()
    _kill_during_swap(bench, monkeypatch, bench.manifest(WATCHDOG), after)
    mr.recover(str(bench.folder), mr.RECOVER_BACK)
    assert bench.snapshot() == before
    assert mr.read_state(str(bench.folder))["build_name"] == "Main"


def test_new_switch_is_refused_until_an_interrupted_one_is_recovered(bench, monkeypatch):
    bench.switch(bench.manifest(MAIN), "Main")
    _kill_during_swap(bench, monkeypatch, bench.manifest(WATCHDOG), 1)
    with pytest.raises(mr.SwitchError, match="interrupted"):
        bench.switch(bench.manifest(MAIN), "Main")


def test_objects_stay_out_of_the_mapped_folder(bench):
    entries = bench.manifest(MAIN) + bench.manifest({"a.o": b"obj"}, role="Objects")
    bench.switch(entries, "Main")
    assert "a.o" not in bench.snapshot()
