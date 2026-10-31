"""
Branch coverage for FileLockManager beyond the happy-path test_file_locking.py:
staleness detection, cross-machine process checks, heartbeat, and error paths.
"""
import os
import sys
import json
import socket
import datetime
import tempfile

sys.path.append(os.path.abspath("src"))

from Application_Logic import Logic_File_Locking
from Application_Logic.Logic_File_Locking import (
    FileLockManager,
    LOCK_STALE_THRESHOLD_SECONDS,
)


def _iso(dt):
    return dt.isoformat()


class FakeClock:
    """Stands in for time.monotonic, so watching a lock doesn't take ten minutes."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        monkeypatch.setattr(Logic_File_Locking.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(Logic_File_Locking.time, "sleep", lambda _s: None)

    def advance(self, seconds):
        self.now += seconds


def _write_lock(proj, last_seen, hostname="other-host"):
    with open(FileLockManager.get_lock_file_path(proj), "w") as f:
        json.dump({"user": "someone", "hostname": hostname, "timestamp": "t",
                   "last_seen": last_seen, "pid": 4242}, f)


def _project(tmp):
    proj = os.path.join(tmp, "p.arch")
    open(proj, "w").close()
    return proj


# --------------------------------------------------------------------------
# get_username
# --------------------------------------------------------------------------

def test_get_username_returns_nonempty():
    assert FileLockManager.get_username()


# --------------------------------------------------------------------------
# is_lock_stale
# --------------------------------------------------------------------------

def test_is_lock_stale_missing_last_seen():
    assert FileLockManager.is_lock_stale({}) is True


def test_is_lock_stale_fresh():
    now = datetime.datetime.now(datetime.timezone.utc)
    assert FileLockManager.is_lock_stale({"last_seen": _iso(now)}) is False


def test_is_lock_stale_old():
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=LOCK_STALE_THRESHOLD_SECONDS + 60
    )
    assert FileLockManager.is_lock_stale({"last_seen": _iso(old)}) is True


def test_is_lock_stale_z_suffix_and_naive():
    # 'Z' suffix is normalized; naive datetime is treated as UTC
    fresh_naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()
    assert FileLockManager.is_lock_stale({"last_seen": fresh_naive}) is False
    assert FileLockManager.is_lock_stale({"last_seen": "2000-01-01T00:00:00Z"}) is True


def test_is_lock_stale_invalid_timestamp():
    assert FileLockManager.is_lock_stale({"last_seen": "not-a-date"}) is True


# --------------------------------------------------------------------------
# is_process_alive
# --------------------------------------------------------------------------

def test_is_process_alive_other_machine_is_assumed_alive():
    stale = {"last_seen": "2000-01-01T00:00:00+00:00"}
    assert FileLockManager.is_process_alive(123, "some-other-host", stale) is True
    assert FileLockManager.is_process_alive(123, "some-other-host") is True


def test_is_process_alive_local_dead_and_alive():
    host = socket.gethostname()
    assert FileLockManager.is_process_alive(0, host) is False
    assert FileLockManager.is_process_alive(999999, host) is False
    assert FileLockManager.is_process_alive(os.getpid(), host) is True


def test_is_process_alive_local_pid_reuse_with_stale_heartbeat():
    """A live PID on this host whose lock heartbeat is stale must read as dead —
    the OS may have recycled a dead owner's PID. A fresh heartbeat keeps it alive."""
    host = socket.gethostname()
    fresh = {"last_seen": _iso(datetime.datetime.now(datetime.timezone.utc))}
    stale = {"last_seen": "2000-01-01T00:00:00+00:00"}
    # Current PID is definitely alive; staleness of the lock decides.
    assert FileLockManager.is_process_alive(os.getpid(), host, fresh) is True
    assert FileLockManager.is_process_alive(os.getpid(), host, stale) is False


# --------------------------------------------------------------------------
# check_lock error paths
# --------------------------------------------------------------------------

def test_check_lock_corrupt_file_is_held_until_quiet(monkeypatch):
    clock = FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        with open(FileLockManager.get_lock_file_path(proj), "w") as f:
            f.write("{ not valid json")
        assert FileLockManager.check_lock(proj)["status"] == "unreadable"
        ok, msg = FileLockManager.acquire_lock(proj)
        assert ok is False
        assert "can't be read" in msg
        assert os.path.exists(FileLockManager.get_lock_file_path(proj))

        clock.advance(LOCK_STALE_THRESHOLD_SECONDS + 1)
        assert FileLockManager.check_lock(proj)["status"] == "unlocked"
        assert FileLockManager.acquire_lock(proj)[0] is True
        FileLockManager.release_lock(proj)


def test_check_lock_retries_a_half_written_file(monkeypatch):
    """A lock caught mid-write reads normally once the write has finished."""
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        lock_file = FileLockManager.get_lock_file_path(proj)
        open(lock_file, "w").close()

        def finish_write(_seconds):
            _write_lock(proj, "2999-01-01T00:00:00+00:00")
        monkeypatch.setattr(Logic_File_Locking.time, "sleep", finish_write)

        status = FileLockManager.check_lock(proj)
        assert status["status"] == "locked_by_other"
        assert status["user"] == "someone"


def test_check_lock_retries_a_read_error(monkeypatch):
    FakeClock(monkeypatch)
    real_open = open
    failures = []

    def flaky_open(path, *args, **kwargs):
        if str(path).endswith(".lock") and not failures:
            failures.append(path)
            raise OSError("network name no longer available")
        return real_open(path, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2999-01-01T00:00:00+00:00")
        monkeypatch.setattr("builtins.open", flaky_open)
        assert FileLockManager.check_lock(proj)["status"] == "locked_by_other"
        assert failures


# --------------------------------------------------------------------------
# staleness of a lock from another machine
# --------------------------------------------------------------------------

def test_remote_lock_clock_difference_does_not_make_it_stale(monkeypatch):
    """The owner's clock being far behind ours must not free a live lock."""
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2000-01-01T00:00:00+00:00")
        assert FileLockManager.check_lock(proj)["status"] == "locked_by_other"
        assert FileLockManager.acquire_lock(proj)[0] is False


def test_remote_lock_goes_stale_only_after_watching_it_unchanged(monkeypatch):
    clock = FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2000-01-01T00:00:00+00:00")
        assert FileLockManager.check_lock(proj)["quiet_seconds"] == 0.0

        clock.advance(LOCK_STALE_THRESHOLD_SECONDS)
        status = FileLockManager.check_lock(proj)
        assert status["status"] == "locked_by_other"
        assert status["quiet_seconds"] == LOCK_STALE_THRESHOLD_SECONDS

        clock.advance(1)
        assert FileLockManager.check_lock(proj)["status"] == "unlocked"
        assert FileLockManager.acquire_lock(proj)[0] is True
        assert FileLockManager.check_lock(proj)["status"] == "locked_by_me"
        FileLockManager.release_lock(proj)


def test_remote_heartbeat_restarts_the_watch(monkeypatch):
    clock = FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2000-01-01T00:00:00+00:00")
        FileLockManager.check_lock(proj)
        clock.advance(LOCK_STALE_THRESHOLD_SECONDS - 1)
        _write_lock(proj, "2000-01-01T00:01:00+00:00")
        assert FileLockManager.check_lock(proj)["quiet_seconds"] == 0.0

        clock.advance(LOCK_STALE_THRESHOLD_SECONDS - 1)
        assert FileLockManager.check_lock(proj)["status"] == "locked_by_other"


def test_seeded_watch_counts_time_seen_by_another_process(monkeypatch):
    clock = FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2000-01-01T00:00:00+00:00")
        lock_file = FileLockManager.get_lock_file_path(proj)
        contents = open(lock_file).read()
        FileLockManager.seed_observation(lock_file, contents, LOCK_STALE_THRESHOLD_SECONDS + 1)
        assert FileLockManager.check_lock(proj)["status"] == "unlocked"


def test_seeded_watch_of_other_bytes_proves_nothing(monkeypatch):
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2000-01-01T00:00:00+00:00")
        lock_file = FileLockManager.get_lock_file_path(proj)
        FileLockManager.seed_observation(lock_file, "older bytes", 10000)
        assert FileLockManager.check_lock(proj)["status"] == "locked_by_other"


def test_unreadable_moment_doesnt_restart_the_watch_of_a_lock(monkeypatch):
    clock = FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2000-01-01T00:00:00+00:00")
        lock_file = FileLockManager.get_lock_file_path(proj)
        contents = open(lock_file).read()
        FileLockManager.check_lock(proj)
        clock.advance(300)
        open(lock_file, "w").close()
        assert FileLockManager.check_lock(proj)["status"] == "unreadable"
        with open(lock_file, "w") as f:
            f.write(contents)
        clock.advance(301)
        assert FileLockManager.check_lock(proj)["status"] == "unlocked"


def test_refused_remote_lock_says_when_to_retry(monkeypatch):
    clock = FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2000-01-01T00:00:00+00:00")
        assert "about 10 minutes" in FileLockManager.acquire_lock(proj)[1]
        clock.advance(240)
        ok, msg = FileLockManager.acquire_lock(proj)
        assert ok is False
        assert "locked by someone on other-host" in msg.lower()
        assert "about 6 minutes" in msg


def test_same_host_lock_still_uses_its_timestamp(monkeypatch):
    """One machine means one clock, so a dead owner frees the lock at once."""
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2999-01-01T00:00:00+00:00", hostname=socket.gethostname())
        assert FileLockManager.check_lock(proj)["status"] == "unlocked"


# --------------------------------------------------------------------------
# acquire_lock / release_lock edge cases
# --------------------------------------------------------------------------

def test_acquire_lock_invalid_path():
    ok, msg = FileLockManager.acquire_lock("/no/such/project.arch")
    assert ok is False
    assert "Invalid" in msg


def test_acquire_lock_locked_by_other():
    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "p.arch")
        open(proj, "w").close()
        # Write a lock owned by a live process (parent) so it reads as locked_by_other
        lock_data = {
            "user": "someone",
            "hostname": socket.gethostname(),
            "timestamp": "t",
            "last_seen": _iso(datetime.datetime.now(datetime.timezone.utc)),
            "pid": os.getppid(),
        }
        with open(FileLockManager.get_lock_file_path(proj), "w") as f:
            json.dump(lock_data, f)
        ok, msg = FileLockManager.acquire_lock(proj)
        assert ok is False
        assert "locked by" in msg.lower()


def test_acquire_lock_reacquire_when_owned():
    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "p.arch")
        open(proj, "w").close()
        assert FileLockManager.acquire_lock(proj)[0] is True
        # Already owned -> re-acquire succeeds (temp+replace path)
        ok, msg = FileLockManager.acquire_lock(proj)
        assert ok is True
        FileLockManager.release_lock(proj)


def test_release_lock_invalid_path():
    assert FileLockManager.release_lock("/no/such/project.arch") is False


def test_release_lock_no_lock_file_is_ok():
    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "p.arch")
        open(proj, "w").close()
        # No lock file present -> returns True (nothing to release)
        assert FileLockManager.release_lock(proj) is True


def test_release_lock_owned_by_other_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "p.arch")
        open(proj, "w").close()
        lock_data = {
            "user": "someone",
            "hostname": socket.gethostname(),
            "timestamp": "t",
            "last_seen": _iso(datetime.datetime.now(datetime.timezone.utc)),
            "pid": os.getppid(),
        }
        with open(FileLockManager.get_lock_file_path(proj), "w") as f:
            json.dump(lock_data, f)
        assert FileLockManager.release_lock(proj) is False


# --------------------------------------------------------------------------
# owner token
# --------------------------------------------------------------------------

def test_token_owns_a_lock_whichever_process_holds_it():
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        assert FileLockManager.acquire_lock(proj, "shared")[0] is True
        lock_file = FileLockManager.get_lock_file_path(proj)
        data = json.load(open(lock_file))
        data["pid"], data["hostname"] = 1, "other-host"
        json.dump(data, open(lock_file, "w"))
        assert FileLockManager.check_lock(proj, "shared")["status"] == "locked_by_me"
        FileLockManager.write_heartbeat(proj, "shared")
        assert FileLockManager.release_lock(proj, "shared") is True
        assert not os.path.exists(lock_file)


def test_lock_with_a_token_refuses_this_process_without_it():
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        assert FileLockManager.acquire_lock(proj, "theirs")[0] is True
        assert FileLockManager.acquire_lock(proj, "mine")[0] is False
        assert FileLockManager.release_lock(proj, "mine") is False


# --------------------------------------------------------------------------
# write_heartbeat
# --------------------------------------------------------------------------

def test_write_heartbeat_updates_last_seen_when_owned():
    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "p.arch")
        open(proj, "w").close()
        FileLockManager.acquire_lock(proj)
        lock_file = FileLockManager.get_lock_file_path(proj)
        with open(lock_file) as f:
            before = json.load(f)["last_seen"]
        # Force an older last_seen, then heartbeat should refresh it
        data = json.load(open(lock_file))
        data["last_seen"] = "2000-01-01T00:00:00+00:00"
        json.dump(data, open(lock_file, "w"))
        FileLockManager.write_heartbeat(proj)
        after = json.load(open(lock_file))["last_seen"]
        assert after != "2000-01-01T00:00:00+00:00"
        FileLockManager.release_lock(proj)


def test_write_heartbeat_never_truncates_the_lock_in_place(monkeypatch):
    """Every moment of a heartbeat leaves a complete lock file for readers."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        FileLockManager.acquire_lock(proj)
        lock_file = FileLockManager.get_lock_file_path(proj)
        real_replace = os.replace
        seen_before_rename = []

        def spying_replace(src, dst):
            with open(lock_file) as f:
                seen_before_rename.append(json.load(f))
            real_replace(src, dst)
        monkeypatch.setattr(Logic_File_Locking.os, "replace", spying_replace)

        FileLockManager.write_heartbeat(proj)
        assert len(seen_before_rename) == 1
        assert sorted(os.listdir(tmp)) == ["p.arch", "p.arch.lock"]
        FileLockManager.release_lock(proj)


def test_write_heartbeat_retries_a_rename_blocked_by_a_reader(monkeypatch):
    """On Windows the rename fails while another machine is reading the lock."""
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        FileLockManager.acquire_lock(proj)
        real_replace = os.replace
        calls = []

        def blocked_once(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise PermissionError(13, "in use")
            real_replace(src, dst)
        monkeypatch.setattr(Logic_File_Locking.os, "replace", blocked_once)

        FileLockManager.write_heartbeat(proj)
        assert len(calls) == 2
        FileLockManager.release_lock(proj)


def test_lock_that_vanishes_during_a_rename_is_still_held(monkeypatch):
    """A share can hide a lock for a moment while its owner replaces it."""
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2999-01-01T00:00:00+00:00")
        lock_file = FileLockManager.get_lock_file_path(proj)
        assert FileLockManager.check_lock(proj)["status"] == "locked_by_other"
        contents = open(lock_file).read()
        os.remove(lock_file)

        def comes_back(_seconds):
            with open(lock_file, "w") as f:
                f.write(contents)
        monkeypatch.setattr(Logic_File_Locking.time, "sleep", comes_back)
        assert FileLockManager.check_lock(proj)["status"] == "locked_by_other"


def test_released_lock_that_stays_gone_is_unlocked(monkeypatch):
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        _write_lock(proj, "2999-01-01T00:00:00+00:00")
        FileLockManager.check_lock(proj)
        os.remove(FileLockManager.get_lock_file_path(proj))
        assert FileLockManager.check_lock(proj)["status"] == "unlocked"


def test_shared_lock_is_confirmed_gone_before_a_first_read_trusts_it(monkeypatch):
    FakeClock(monkeypatch)
    sleeps = []
    monkeypatch.setattr(Logic_File_Locking.time, "sleep", sleeps.append)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        assert FileLockManager.check_lock(proj)["status"] == "unlocked"
        assert sleeps == []
        assert FileLockManager.check_lock(proj, "token")["status"] == "unlocked"
        assert len(sleeps) == Logic_File_Locking.LOCK_GONE_CHECKS


def test_busy_rename_is_retried_as_the_share_reports_it(monkeypatch):
    """macOS reports EBUSY when another machine has the file open."""
    import errno
    FakeClock(monkeypatch)
    calls = []

    def busy_twice():
        calls.append(1)
        if len(calls) < 3:
            raise OSError(errno.EBUSY, "Resource busy")
        return "done"
    assert Logic_File_Locking.retry_while_busy(busy_twice) == "done"
    assert len(calls) == 3


def test_other_errors_arent_retried(monkeypatch):
    import errno
    FakeClock(monkeypatch)
    calls = []

    def gone():
        calls.append(1)
        raise OSError(errno.ENOENT, "No such file")
    try:
        Logic_File_Locking.retry_while_busy(gone)
    except OSError:
        pass
    assert len(calls) == 1


def test_write_heartbeat_gives_up_cleanly_when_the_rename_stays_blocked(monkeypatch):
    FakeClock(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(tmp)
        FileLockManager.acquire_lock(proj)
        lock_file = FileLockManager.get_lock_file_path(proj)
        before = open(lock_file).read()
        calls = []

        def always_blocked(src, dst):
            calls.append(src)
            raise PermissionError(13, "in use")
        monkeypatch.setattr(Logic_File_Locking.os, "replace", always_blocked)

        FileLockManager.write_heartbeat(proj)
        assert len(calls) == Logic_File_Locking._BUSY_ATTEMPTS
        assert open(lock_file).read() == before
        assert sorted(os.listdir(tmp)) == ["p.arch", "p.arch.lock"]
        FileLockManager.release_lock(proj)


def test_write_heartbeat_no_file_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "p.arch")
        open(proj, "w").close()
        # No lock file -> should not raise
        FileLockManager.write_heartbeat(proj)


def test_write_heartbeat_empty_path_is_noop():
    FileLockManager.write_heartbeat("")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
