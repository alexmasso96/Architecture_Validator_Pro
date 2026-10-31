"""Logic-layer tests for the sync engine (#16): sections, push, pull and failures."""
import base64
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_Sync_Engine as engine
from Application_Logic import Logic_Sync_Sections as sections
from Application_Logic.Logic_Block_Crypto import BlockCipher
from Application_Logic.Logic_Database import ProjectDatabase
from Tests.test_helpers import make_project_db


class Team:
    """A shared project and two users' local copies."""

    def __init__(self, tmp):
        self.tmp = tmp
        origin = str(tmp / "origin.arch")
        db = make_project_db(
            origin, layout=[("TC. ID", "Static Text", True)],
            models=[{"name": "A", "status": "In Work", "rows": []}],
            releases=[{"name": "R1"}, {"name": "R2"}])
        db.close()
        (tmp / "nas").mkdir()
        self.remote = str(tmp / "nas" / "Project.arch")
        self.a = str(tmp / "a" / "Project.arch")
        self.b = str(tmp / "b" / "Project.arch")
        engine.publish(origin, self.remote, "alex")
        engine.clone(self.remote, self.a)
        engine.clone(self.remote, self.b)
        ids = self.query(self.a, "SELECT id FROM releases ORDER BY id")
        self.r1, self.r2 = ids[0][0], ids[1][0]

    @staticmethod
    def query(path, sql, *params):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    @staticmethod
    def write(path, sql, *params):
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def set_code_map(self, path, release_id, text):
        self.write(path, "INSERT OR REPLACE INTO release_code_maps "
                         "(release_id, code_map_json) VALUES (?, ?)", release_id, text)

    def code_map(self, path, release_id):
        rows = self.query(path, "SELECT code_map_json FROM release_code_maps "
                                "WHERE release_id=?", release_id)
        return rows[0][0] if rows else None


@pytest.fixture()
def team(tmp_path):
    return Team(tmp_path)


@pytest.mark.parametrize("path,params,expected", [
    ("/nas/My Project.arch", {}, "file:///nas/My%20Project.arch"),
    ("/nas/a.arch", {"mode": "ro"}, "file:///nas/a.arch?mode=ro"),
])
def test_sqlite_uri(path, params, expected):
    assert engine.sqlite_uri(path, **params) == expected


def test_sqlite_uri_for_windows_paths(monkeypatch):
    monkeypatch.setattr(engine.os.path, "abspath", lambda p: p)
    assert engine.sqlite_uri("C:\\work\\a.arch") == "file:///C:/work/a.arch"
    assert engine.sqlite_uri("\\\\nas\\share\\a.arch") == "file:////nas/share/a.arch"


def test_share_is_used_without_sqlite_file_locks(team, monkeypatch):
    """A Linux SMB mount can't complete SQLite's locks, so nothing may depend on them."""
    uris = []
    real = sqlite3.connect

    def spying(target, *args, **kwargs):
        uris.append(str(target))
        return real(target, *args, **kwargs)
    monkeypatch.setattr(engine.sqlite3, "connect", spying)
    team.set_code_map(team.a, team.r1, "m")
    engine.push(team.a, team.remote, "alex")
    engine.pull(team.b, team.remote)
    on_share = [u for u in uris if "/nas/" in u]
    assert on_share and all("vfs=" in u for u in on_share)


def test_push_marks_the_manifest_while_writing(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "m")
    seen = {}
    real = engine._sync_section

    def spying(*args, **kwargs):
        seen["manifest"] = engine.read_manifest(team.remote)
        return real(*args, **kwargs)
    monkeypatch.setattr(engine, "_sync_section", spying)
    engine.push(team.a, team.remote, "alex")
    assert seen["manifest"]["writing"] == "alex"
    assert "writing" not in engine.read_manifest(team.remote)


def test_failed_push_clears_the_mark(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "m")

    def dropped(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(engine, "_sync_section", dropped)
    with pytest.raises(engine.SyncError):
        engine.push(team.a, team.remote, "alex")
    assert "writing" not in engine.read_manifest(team.remote)


def test_pull_waits_while_someone_is_writing(team):
    team.set_code_map(team.a, team.r1, "m")
    engine.push(team.a, team.remote, "alex")
    manifest = engine.read_manifest(team.remote)
    engine._begin_write(team.remote, manifest, "alex")
    with pytest.raises(engine.SyncError, match="publishing"):
        engine.pull(team.b, team.remote)
    with pytest.raises(engine.SyncError, match="publishing"):
        engine.clone(team.remote, str(team.tmp / "new" / "Project.arch"))
    assert team.code_map(team.b, team.r1) is None


def test_pull_throws_away_a_read_that_overlapped_a_write(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "first")
    engine.push(team.a, team.remote, "alex")
    real = engine._sync_section
    done = []

    def someone_publishes_meanwhile(*args, **kwargs):
        result = real(*args, **kwargs)
        if not done:
            done.append(True)
            engine._begin_write(team.remote, engine.read_manifest(team.remote), "alex")
        return result
    monkeypatch.setattr(engine, "_sync_section", someone_publishes_meanwhile)
    with pytest.raises(engine.SyncError, match="changed during the read"):
        engine.pull(team.b, team.remote)
    assert team.code_map(team.b, team.r1) is None
    assert engine.local_state(team.b)["base"] == 1


def test_clone_throws_away_a_copy_that_overlapped_a_write(team, monkeypatch):
    real = engine.shutil.copyfile

    def someone_publishes_meanwhile(source, target):
        real(source, target)
        engine._begin_write(team.remote, engine.read_manifest(team.remote), "alex")
    monkeypatch.setattr(engine.shutil, "copyfile", someone_publishes_meanwhile)
    local = str(team.tmp / "new" / "Project.arch")
    with pytest.raises(engine.SyncError, match="changed during the read"):
        engine.clone(team.remote, local)
    assert os.listdir(os.path.dirname(local)) == []


def test_interrupted_write_is_rolled_back_and_cleared(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "safe")
    engine.push(team.a, team.remote, "alex")
    engine._begin_write(team.remote, engine.read_manifest(team.remote), "alex")
    engine.clear_interrupted_write(team.remote)
    assert "writing" not in engine.read_manifest(team.remote)
    assert engine.pull(team.b, team.remote)["revision"] == 2
    assert team.code_map(team.b, team.r1) == "safe"


def test_every_project_table_belongs_to_a_section(team):
    known = {t.name for s in sections.SECTIONS for t in s.tables}
    known |= {sections.ELF_INDEX, *sections.ELF_TABLES}
    known |= {"ui_state", "sync_sections", "sync_log"}
    tables = {r[0] for r in team.query(
        team.a, "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")}
    assert tables == known


def test_changing_one_code_map_sends_only_that_section(team):
    team.set_code_map(team.a, team.r1, "x" * 5000)
    report = engine.push(team.a, team.remote, "alex")
    assert [(r["scope"], r["section"]) for r in report["sections"]] == [
        (f"release:{team.r1}", "Code Map")]
    assert report["rows"] == 1
    assert 5000 <= report["bytes"] < 5100
    assert team.code_map(team.remote, team.r1) == "x" * 5000


def test_unchanged_project_sends_nothing(team):
    report = engine.push(team.a, team.remote, "alex")
    assert report["sections"] == []
    assert engine.read_manifest(team.remote)["revision"] == 1


def test_one_changed_source_file_sends_one_row(team):
    for name in ("a.c", "b.c"):
        team.write(team.a, "INSERT INTO release_source_files (release_id, rel_path, "
                           "content_gzip, size, ext, content_hash) VALUES (?, ?, ?, 1, '.c', ?)",
                   team.r1, name, b"old", name)
    engine.push(team.a, team.remote, "alex")
    team.write(team.a, "UPDATE release_source_files SET content_gzip=?, content_hash='new' "
                       "WHERE rel_path='a.c'", b"new")
    assert engine.push(team.a, team.remote, "alex")["rows"] == 1


def test_deletions_are_carried_across(team):
    team.set_code_map(team.a, team.r1, "m")
    engine.push(team.a, team.remote, "alex")
    team.write(team.a, "DELETE FROM release_code_maps WHERE release_id=?", team.r1)
    engine.push(team.a, team.remote, "alex")
    assert team.code_map(team.remote, team.r1) is None


def test_other_user_receives_changes_by_pulling(team):
    team.set_code_map(team.a, team.r2, "from alex")
    engine.push(team.a, team.remote, "alex")
    assert engine.read_manifest(team.remote)["revision"] == 2
    report = engine.pull(team.b, team.remote)
    assert report["revision"] == 2
    assert report["by"] == "alex"
    assert team.code_map(team.b, team.r2) == "from alex"
    assert engine.local_state(team.b)["dirty"] == 0
    assert engine.pull(team.b, team.remote)["sections"] == []


def test_edits_can_go_both_ways_in_turn(team):
    team.set_code_map(team.a, team.r1, "one")
    engine.push(team.a, team.remote, "alex")
    engine.pull(team.b, team.remote)
    team.set_code_map(team.b, team.r1, "two")
    engine.push(team.b, team.remote, "bo")
    engine.pull(team.a, team.remote)
    assert team.code_map(team.a, team.r1) == "two"


def test_sync_log_reaches_every_copy(team):
    team.set_code_map(team.a, team.r1, "m")
    engine.push(team.a, team.remote, "alex")
    engine.pull(team.b, team.remote)
    for path in (team.a, team.b, team.remote):
        rows = team.query(path, "SELECT username, release_name, section, rows_changed "
                                "FROM sync_log")
        assert rows == [("alex", "R1", "Code Map", 1)]


def test_rewriting_the_same_data_adds_nothing_to_the_log(team):
    team.set_code_map(team.a, team.r1, "same")
    engine.push(team.a, team.remote, "alex")
    team.set_code_map(team.a, team.r1, "same")
    engine.push(team.a, team.remote, "alex")
    assert len(team.query(team.remote, "SELECT id FROM sync_log")) == 1


def test_project_and_model_sections_are_named(team):
    team.write(team.a, "INSERT INTO project_meta (key, value) VALUES ('ai_prompt', 'p')")
    model = team.query(team.a, "SELECT id FROM architecture_models")[0][0]
    team.write(team.a, "INSERT OR REPLACE INTO model_metadata (model_id, key, value) "
                       "VALUES (?, 'k', 'v')", model)
    names = {(r["scope"], r["section"]) for r in
             engine.push(team.a, team.remote, "alex")["sections"]}
    assert ("project", "Settings") in names
    assert (f"model:{model}", "Architecture") in names


def test_local_meta_never_travels(team):
    team.write(team.a, "INSERT OR REPLACE INTO project_meta (key, value) "
                       "VALUES ('activity_status', 'busy')")
    assert engine.push(team.a, team.remote, "alex")["sections"] == []
    assert team.query(team.remote, "SELECT value FROM project_meta "
                                   "WHERE key='activity_status'") == []


def test_deleted_release_reaches_the_other_copy(team):
    team.set_code_map(team.a, team.r2, "m")
    engine.push(team.a, team.remote, "alex")
    engine.pull(team.b, team.remote)
    team.write(team.a, "DELETE FROM release_code_maps WHERE release_id=?", team.r2)
    team.write(team.a, "DELETE FROM releases WHERE id=?", team.r2)
    engine.push(team.a, team.remote, "alex")
    engine.pull(team.b, team.remote)
    assert team.query(team.b, "SELECT COUNT(*) FROM releases WHERE id=?", team.r2) == [(0,)]
    assert team.code_map(team.b, team.r2) is None


def test_test_build_rows_sync_with_their_release(team):
    team.write(team.a, "INSERT INTO test_builds (id, release_id, name) VALUES (9, ?, 'W')", team.r1)
    team.write(team.a, "INSERT INTO build_manifests (test_build_id, rel_path, sha256) "
                       "VALUES (9, 'a.hex', 'abc')")
    names = {r["section"] for r in engine.push(team.a, team.remote, "alex")["sections"]}
    assert names == {"Test Builds"}
    engine.pull(team.b, team.remote)
    assert team.query(team.b, "SELECT sha256 FROM build_manifests") == [("abc",)]
    team.write(team.b, "DELETE FROM test_builds WHERE id=9")
    engine.push(team.b, team.remote, "bo")
    assert team.query(team.remote, "SELECT COUNT(*) FROM build_manifests") == [(0,)]


def test_binaries_folder_is_not_touched(team):
    binaries = team.remote + ".binaries"
    os.makedirs(binaries)
    open(os.path.join(binaries, "blob"), "wb").write(b"x")
    engine.push(team.a, team.remote, "alex")
    engine.pull(team.b, team.remote)
    assert os.listdir(os.path.dirname(team.b)) == ["Project.arch"] or not os.path.exists(
        team.b + ".binaries")


def test_elf_data_transfers_only_missing_hashes(team):
    team.write(team.a, "INSERT INTO elf_index (elf_hash) VALUES ('h1')")
    report = engine.push(team.a, team.remote, "alex")
    assert [r["section"] for r in report["sections"]] == [sections.ELF_SECTION]
    engine.pull(team.b, team.remote)
    assert team.query(team.b, "SELECT elf_hash FROM elf_index") == [("h1",)]


def test_edits_made_during_a_push_stay_dirty(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "first")
    real = engine._finish_push

    def edit_then_finish(*args):
        team.set_code_map(team.a, team.r1, "second")
        real(*args)
    monkeypatch.setattr(engine, "_finish_push", edit_then_finish)
    engine.push(team.a, team.remote, "alex")
    monkeypatch.setattr(engine, "_finish_push", real)
    assert team.code_map(team.remote, team.r1) == "first"
    assert engine.local_state(team.a)["dirty"] == 1
    engine.push(team.a, team.remote, "alex")
    assert team.code_map(team.remote, team.r1) == "second"


def test_failed_push_rolls_back_and_retries(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "m")
    real = engine._sync_section

    def dropped(*args, **kwargs):
        real(*args, **kwargs)
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(engine, "_sync_section", dropped)
    with pytest.raises(engine.SyncError):
        engine.push(team.a, team.remote, "alex")
    monkeypatch.setattr(engine, "_sync_section", real)
    assert team.code_map(team.remote, team.r1) is None
    assert engine.read_manifest(team.remote)["revision"] == 1
    assert engine.remote_is_healthy(team.remote)
    engine.restore_dirty_flags(team.a)
    assert engine.push(team.a, team.remote, "alex")["revision"] == 2
    assert team.code_map(team.remote, team.r1) == "m"


def test_stop_after_the_shared_commit_completes_on_retry(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "m")
    real = engine._finish_push

    class Killed(BaseException):
        pass

    def killed(*_):
        raise Killed()
    monkeypatch.setattr(engine, "_finish_push", killed)
    with pytest.raises(Killed):
        engine.push(team.a, team.remote, "alex")
    monkeypatch.setattr(engine, "_finish_push", real)
    assert engine.read_manifest(team.remote)["revision"] == 1
    assert engine.local_state(team.a)["pending"]

    report = engine.push(team.a, team.remote, "alex")
    assert report["sections"] == []
    assert engine.read_manifest(team.remote)["revision"] == 2
    assert engine.local_state(team.a) == {"base": 2, "dirty": 0, "pending": None}
    assert len(team.query(team.remote, "SELECT id FROM sync_log")) == 1


def test_failed_pull_leaves_the_old_revision_readable(team, monkeypatch):
    team.set_code_map(team.a, team.r1, "new")
    team.write(team.a, "INSERT INTO project_meta (key, value) VALUES ('ai_prompt', 'p')")
    engine.push(team.a, team.remote, "alex")
    real = engine._sync_section
    calls = []

    def dropped(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args, **kwargs)
    monkeypatch.setattr(engine, "_sync_section", dropped)
    with pytest.raises(engine.SyncError):
        engine.pull(team.b, team.remote)
    monkeypatch.setattr(engine, "_sync_section", real)
    assert team.code_map(team.b, team.r1) is None
    assert engine.local_state(team.b)["base"] == 1
    assert engine.pull(team.b, team.remote)["revision"] == 2


def test_push_from_an_old_base_is_refused_and_work_is_kept(team):
    team.set_code_map(team.a, team.r1, "alex's")
    engine.push(team.a, team.remote, "alex")
    team.set_code_map(team.b, team.r1, "bo's")
    with pytest.raises(engine.SyncConflict):
        engine.push(team.b, team.remote, "bo")
    copy = engine.save_conflict_copy(team.b, team.remote, "bo")
    assert os.path.basename(copy).startswith("Project (conflict bo ")
    assert team.code_map(copy, team.r1) == "bo's"
    assert team.code_map(team.remote, team.r1) == "alex's"


def test_lost_lock_refuses_a_push(team):
    team.set_code_map(team.a, team.r1, "m")
    with pytest.raises(engine.SyncConflict):
        engine.push(team.a, team.remote, "alex", still_ours=lambda: False)


def test_version_mismatch_blocks_sync(team):
    path = engine.manifest_path(team.remote)
    manifest = json.load(open(path))
    manifest["schema_version"] = 999
    json.dump(manifest, open(path, "w"))
    with pytest.raises(engine.SyncBlocked, match="Update the"):
        engine.pull(team.a, team.remote)


def test_unreachable_share_is_a_retryable_error(team, tmp_path):
    with pytest.raises(engine.SyncError) as e:
        engine.read_manifest(str(tmp_path / "gone" / "Project.arch"))
    assert not isinstance(e.value, engine.SyncBlocked)


def test_damaged_remote_is_detected_and_republished(team):
    team.set_code_map(team.a, team.r1, "kept")
    engine.push(team.a, team.remote, "alex")
    with open(team.remote, "r+b") as f:
        f.seek(0)
        f.write(b"garbage" * 200)
    assert engine.remote_is_healthy(team.remote) is False
    engine.republish(team.a, team.remote, "alex")
    assert engine.remote_is_healthy(team.remote) is True
    assert team.code_map(team.remote, team.r1) == "kept"
    assert engine.read_manifest(team.remote)["revision"] == 3
    assert engine.local_state(team.a)["base"] == 3
    assert any(".damaged-" in n for n in os.listdir(os.path.dirname(team.remote)))


def test_database_put_there_by_hand_can_be_adopted(tmp_path):
    db = make_project_db(str(tmp_path / "hand.arch"), layout=[("TC. ID", "Static Text", True)],
                         models=[], releases=[{"name": "R1"}])
    db.close()
    remote = str(tmp_path / "hand.arch")
    manifest = engine.adopt_remote(remote, "alex")
    assert manifest["revision"] == 1
    local = str(tmp_path / "local" / "hand.arch")
    engine.clone(remote, local)
    assert engine.local_state(local) == {"base": 1, "dirty": 0, "pending": None}


def test_publish_refuses_to_overwrite(team):
    with pytest.raises(engine.SyncBlocked):
        engine.publish(team.a, team.remote, "alex")


def test_shared_copy_stays_a_normal_project_file(team):
    team.set_code_map(team.a, team.r1, "m")
    engine.push(team.a, team.remote, "alex")
    db = ProjectDatabase()
    db.open(team.remote)
    assert [r[0] for r in db._conn.execute("SELECT name FROM releases ORDER BY id")] == ["R1", "R2"]
    db.close()


def test_encrypted_values_sync_without_the_password(tmp_path):
    origin = str(tmp_path / "e.arch")
    cipher = BlockCipher(b"m" * 32)
    db = make_project_db(origin, layout=[("TC. ID", "Static Text", True)], models=[],
                         releases=[{"name": "R1"}])
    db.set_block_cipher(cipher)
    db.set_meta("ai_prompt", "secret prompt")
    db.close()
    remote = str(tmp_path / "shared.arch")
    engine.publish(origin, remote, "alex")
    local = str(tmp_path / "local" / "shared.arch")
    engine.clone(remote, local)
    other = str(tmp_path / "other" / "shared.arch")
    engine.clone(remote, other)

    db = ProjectDatabase()
    db.open(local)
    db.set_block_cipher(cipher)
    db.set_meta("ai_prompt", "changed secret")
    db.close()
    engine.push(local, remote, "alex")
    engine.pull(other, remote)

    stored = Team.query(other, "SELECT value FROM project_meta WHERE key='ai_prompt'")[0][0]
    assert "changed secret" not in stored and "changed secret" not in open(other, "rb").read().decode("latin1")
    db = ProjectDatabase()
    db.open(other)
    db.set_block_cipher(cipher)
    assert db.get_meta("ai_prompt") == "changed secret"
    db.close()
