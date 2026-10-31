import os
import json
import socket
import sys
import datetime
import errno
import getpass
import logging
import math
import threading
import time
import uuid

logger = logging.getLogger(__name__)

LOCK_HEARTBEAT_INTERVAL_SECONDS = 60
LOCK_STALE_THRESHOLD_SECONDS = 600
LOCK_IO_ATTEMPTS = 3
LOCK_IO_RETRY_SECONDS = 0.2
# Replacing a file on an SMB share isn't atomic for other machines: it was seen
# vanishing for up to 317 ms during a live lock's heartbeat. It has to stay gone
# for longer than that before a lock counts as released.
LOCK_GONE_CHECKS = 6
LOCK_GONE_CHECK_SECONDS = 0.25
# A rename or delete fails while another machine has the file open: the share
# reports EBUSY on macOS and a sharing violation on Windows.
_BUSY_ERRNOS = (errno.EBUSY, errno.EACCES, errno.EPERM, errno.ETXTBSY)
_BUSY_ATTEMPTS = 10
_BUSY_RETRY_SECONDS = 0.2

# lock file -> (its contents, time.monotonic() when those contents were first seen)
_observed_locks: dict = {}
_observed_locks_guard = threading.Lock()

def retry_while_busy(action, *args):
    """Runs a rename or delete, waiting out another machine that has the file open."""
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            return action(*args)
        except OSError as e:
            if e.errno not in _BUSY_ERRNOS or attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(_BUSY_RETRY_SECONDS)


class FileLockManager:
    @staticmethod
    def get_username():
        """Returns the current OS username cross-platform.

        Prefers getpass.getuser() (which honours $LOGNAME/$USER/$USERNAME and
        falls back to the pwd database) over os.getlogin(): on some macOS launch
        contexts os.getlogin() returns "root" even for a normal user, which would
        mislabel both the edit-lock owner and the change-history actor.
        """
        try:
            name = getpass.getuser()
            if name:
                return name
        except Exception:
            pass
        try:
            return os.getlogin()
        except Exception:
            return "unknown"

    @staticmethod
    def is_lock_stale(lock_data: dict) -> bool:
        """Checks if a lock's last_seen timestamp exceeds the stale threshold.

        Only meaningful for a lock written on this host. Another machine's
        timestamp comes from another clock, so check_lock uses quiet_seconds.
        """
        last_seen_str = lock_data.get("last_seen")
        if not last_seen_str:
            return True
        try:
            if last_seen_str.endswith('Z'):
                last_seen_str = last_seen_str[:-1] + '+00:00'
            last_seen = datetime.datetime.fromisoformat(last_seen_str)
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            age = (now - last_seen).total_seconds()
            return age > LOCK_STALE_THRESHOLD_SECONDS
        except Exception:
            return True

    @staticmethod
    def is_process_alive(pid: int, hostname: str, lock_data: dict = None) -> bool:
        """Checks if the given PID is active on the host machine."""
        current_hostname = socket.gethostname()
        if hostname != current_hostname:
            # Different machine: we cannot check process status across machines, so assume it is alive
            return True

        if pid <= 0:
            return False

        # Determine raw PID liveness on this machine.
        if sys.platform != "win32":
            try:
                os.kill(pid, 0)
                alive = True
            except OSError as err:
                import errno
                if err.errno == errno.ESRCH:
                    alive = False
                else:
                    # EPERM means we don't have permission to signal but it exists
                    alive = True
        else:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    exit_code = ctypes.c_ulong()
                    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    kernel32.CloseHandle(handle)
                    alive = exit_code.value == 259
                else:
                    last_error = kernel32.GetLastError()
                    alive = last_error == 5  # Access Denied
            except Exception:
                try:
                    import subprocess
                    # Run tasklist command to check if PID exists
                    out = subprocess.check_output(f'tasklist /FI "PID eq {pid}"', shell=True, text=True)
                    alive = str(pid) in out
                except Exception:
                    alive = True

        # Same host: PID liveness alone is not enough — the OS can recycle a dead
        # owner's PID, which would keep the project locked forever. A live owner
        # refreshes the lock heartbeat (~every 60s); if the heartbeat is stale
        # beyond the threshold, treat the lock as dead even though *some* process
        # now happens to hold that PID. Only applied when we have the lock data to
        # check — a bare PID probe (lock_data=None) keeps the raw liveness answer.
        if alive and lock_data is not None and FileLockManager.is_lock_stale(lock_data):
            return False
        return alive

    @staticmethod
    def get_lock_file_path(project_path: str) -> str:
        """Returns the absolute path to the sidecar lock file for the project."""
        return project_path + ".lock"

    @staticmethod
    def quiet_seconds(lock_file: str, contents) -> float:
        """Seconds this process has watched the lock without its contents changing.

        Measured only on this machine's monotonic clock, so a difference between
        the owner's clock and ours can't make a live lock look stale. Watching
        starts at the first read and is lost when the app restarts.
        """
        now = time.monotonic()
        with _observed_locks_guard:
            seen = _observed_locks.get(lock_file)
            if seen is None or seen[0] != contents:
                _observed_locks[lock_file] = (contents, now)
                return 0.0
            return now - seen[1]

    @staticmethod
    def seed_observation(lock_file: str, contents, age_seconds: float) -> None:
        """Takes on a watch another process started on this machine. Bytes that
        were already there ``age_seconds`` ago haven't had a heartbeat since."""
        started = time.monotonic() - max(age_seconds, 0.0)
        with _observed_locks_guard:
            seen = _observed_locks.get(lock_file)
            if seen is None or seen[0] != contents or started < seen[1]:
                _observed_locks[lock_file] = (contents, started)

    @staticmethod
    def _forget_observation(lock_file: str) -> None:
        with _observed_locks_guard:
            _observed_locks.pop(lock_file, None)

    @staticmethod
    def _read_lock_file(lock_file: str) -> tuple:
        """Returns (data, contents); data is None if the file stays unreadable.

        A failed read is retried: it can be a half-written file or a brief
        network error, and neither means the lock is free. FileNotFoundError
        is passed on, because a missing file does.
        """
        contents = None
        for attempt in range(LOCK_IO_ATTEMPTS):
            if attempt:
                time.sleep(LOCK_IO_RETRY_SECONDS)
            try:
                with open(lock_file, 'r') as f:
                    contents = f.read()
                data = json.loads(contents)
                if isinstance(data, dict):
                    return data, contents
            except FileNotFoundError:
                raise
            except Exception:
                pass
        return None, contents

    @staticmethod
    def _write_lock_file(lock_file: str, lock_data: dict) -> None:
        """Writes via temp + rename, so a reader never sees a half-written lock.

        The rename fails while another machine has the file open for reading, so
        it's retried.
        """
        temp_lock = f"{lock_file}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_lock, 'w') as f:
                json.dump(lock_data, f, indent=4)
            retry_while_busy(os.replace, temp_lock, lock_file)
        finally:
            try:
                if os.path.exists(temp_lock):
                    os.remove(temp_lock)
            except Exception:
                pass

    @staticmethod
    def _really_gone(lock_file: str, token) -> bool:
        """Whether a lock that looks missing has been released. Only a lock that
        may be in use needs this: a shared project's, or one this process has seen."""
        with _observed_locks_guard:
            seen = lock_file in _observed_locks
        if token is None and not seen:
            return True
        for _ in range(LOCK_GONE_CHECKS):
            time.sleep(LOCK_GONE_CHECK_SECONDS)
            if os.path.exists(lock_file):
                return False
        return True

    @staticmethod
    def _owned(data: dict, token, pid: int, hostname: str) -> bool:
        """A lock taken with a token belongs to that token, whichever process has
        it. Any other lock belongs to the process that took it."""
        if data.get("token") is not None:
            return data["token"] == token
        return data.get("pid") == pid and data.get("hostname") == hostname

    @staticmethod
    def _refusal_message(lock_status: dict) -> str:
        """The reason acquire_lock gives when a lock is held or can't be read."""
        minutes_left = math.ceil(
            (LOCK_STALE_THRESHOLD_SECONDS - lock_status.get("quiet_seconds", 0.0)) / 60)
        retry_hint = (f"try again in about {max(minutes_left, 1)} minutes "
                      "and keep this app open until then")
        if lock_status["status"] == "unreadable":
            return f"The project's lock file can't be read. If this continues, {retry_hint}."
        message = (f"Project is locked by {lock_status['user']} on "
                   f"{lock_status['hostname']} since {lock_status['timestamp']}")
        if lock_status.get("remote"):
            message += f". If that session has stopped, {retry_hint}."
        return message

    @staticmethod
    def check_lock(project_path: str, token: str = None) -> dict:
        """
        Checks lock status. With ``token``, any process holding the same token
        counts as the owner, so the Sync Worker can finish a push and release the
        lock after the app closes. Returns a dictionary:
        {
            "status": "unlocked" | "locked_by_me" | "locked_by_other" | "unreadable",
            "user": str,
            "hostname": str,
            "timestamp": str,
            "pid": int
        }
        A lock from another machine also has "remote" and "quiet_seconds", and
        goes stale after LOCK_STALE_THRESHOLD_SECONDS without changing.
        "unreadable" means a lock file exists but can't be parsed: it's treated
        as held until it has been quiet for the same time.
        """
        lock_file = FileLockManager.get_lock_file_path(project_path)
        try:
            if not os.path.exists(lock_file):
                raise FileNotFoundError(lock_file)
            data, contents = FileLockManager._read_lock_file(lock_file)
        except FileNotFoundError:
            if FileLockManager._really_gone(lock_file, token):
                FileLockManager._forget_observation(lock_file)
                return {"status": "unlocked"}
            try:
                data, contents = FileLockManager._read_lock_file(lock_file)
            except FileNotFoundError:
                data, contents = None, None

        if data is None:
            # Watched apart from the lock's real contents: one bad read over a
            # share must not restart the watch of a lock that was read fine.
            quiet = FileLockManager.quiet_seconds(f"{lock_file}#unreadable", contents)
            if quiet > LOCK_STALE_THRESHOLD_SECONDS:
                return {"status": "unlocked"}
            return {
                "status": "unreadable",
                "user": "unknown",
                "hostname": "unknown",
                "timestamp": "",
                "pid": 0,
                "quiet_seconds": quiet
            }

        user = data.get("user", "unknown")
        hostname = data.get("hostname", "unknown")
        timestamp = data.get("timestamp", "")
        pid = data.get("pid", 0)
        
        # Check if it is locked by the CURRENT process
        current_pid = os.getpid()
        current_hostname = socket.gethostname()
        
        if FileLockManager._owned(data, token, current_pid, current_hostname):
            return {
                "status": "locked_by_me",
                "user": user,
                "hostname": hostname,
                "timestamp": timestamp,
                "pid": pid
            }
            
        if hostname != current_hostname:
            quiet = FileLockManager.quiet_seconds(lock_file, contents)
            if quiet > LOCK_STALE_THRESHOLD_SECONDS:
                return {"status": "unlocked"}
            return {
                "status": "locked_by_other",
                "user": user,
                "hostname": hostname,
                "timestamp": timestamp,
                "pid": pid,
                "remote": True,
                "quiet_seconds": quiet
            }

        # Check if the process is alive
        if FileLockManager.is_process_alive(pid, hostname, lock_data=data):
            return {
                "status": "locked_by_other",
                "user": user,
                "hostname": hostname,
                "timestamp": timestamp,
                "pid": pid
            }
        else:
            # Stale lock: process is dead. Treat as unlocked.
            return {"status": "unlocked"}

    @staticmethod
    def acquire_lock(project_path: str, token: str = None) -> tuple[bool, str]:
        """
        Attempts to acquire the lock.
        Returns (success, message).
        If lock is held by someone else, returns (False, error_msg).
        If acquired or already held by us, returns (True, success_msg).
        """
        if not project_path or not os.path.exists(project_path):
            return False, "Invalid project path."
            
        lock_status = FileLockManager.check_lock(project_path, token)
        if lock_status["status"] in ("locked_by_other", "unreadable"):
            return False, FileLockManager._refusal_message(lock_status)

        lock_file = FileLockManager.get_lock_file_path(project_path)
        _now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        lock_data = {
            "user": FileLockManager.get_username(),
            "hostname": socket.gethostname(),
            "timestamp": _now,
            "last_seen": _now,
            "pid": os.getpid()
        }
        if token is not None:
            lock_data["token"] = token

        if lock_status["status"] == "locked_by_me":
            # We already own the lock — safe to overwrite via temp+replace.
            try:
                FileLockManager._write_lock_file(lock_file, lock_data)
                return True, "Lock acquired successfully."
            except Exception as e:
                return False, f"Failed to write lock file: {str(e)}"

        # Lock is unlocked — use O_CREAT|O_EXCL for an atomic create.
        # This prevents the TOCTOU race where two processes both see "unlocked"
        # and both succeed with temp+replace.
        for _attempt in range(2):
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if sys.platform == "win32":
                    flags |= os.O_BINARY
                fd = os.open(lock_file, flags, 0o644)
                with os.fdopen(fd, 'w') as f:
                    json.dump(lock_data, f, indent=4)
                return True, "Lock acquired successfully."
            except FileExistsError:
                # Another process beat us — re-read and check staleness.
                recheck = FileLockManager.check_lock(project_path, token)
                if recheck["status"] == "unlocked":
                    # Stale lock was just cleaned up by check_lock; retry once.
                    try:
                        os.unlink(lock_file)
                    except OSError:
                        pass
                    continue
                # Genuinely held by someone else.
                return False, FileLockManager._refusal_message(recheck)
            except Exception as e:
                return False, f"Failed to write lock file: {str(e)}"
        return False, "Failed to acquire lock after retry."

    @staticmethod
    def release_lock(project_path: str, token: str = None) -> bool:
        """
        Releases the lock file if it is held by us (or if it is stale).
        Returns True if released, False otherwise.
        """
        if not project_path or not os.path.exists(project_path):
            return False
            
        lock_file = FileLockManager.get_lock_file_path(project_path)
        if not os.path.exists(lock_file):
            return True
            
        lock_status = FileLockManager.check_lock(project_path, token)
        # Release if locked by me or if it is stale (unlocked status)
        if lock_status["status"] in ("locked_by_me", "unlocked"):
            try:
                retry_while_busy(os.remove, lock_file)
                return True
            except Exception:
                return False
        return False

    @staticmethod
    def write_heartbeat(project_path: str, token: str = None):
        """Updates the last_seen timestamp in the lock file to current time if held by us."""
        if not project_path:
            return
        lock_file = FileLockManager.get_lock_file_path(project_path)
        if not os.path.exists(lock_file):
            return
        try:
            data, _ = FileLockManager._read_lock_file(lock_file)
            if data is None:
                return
            if FileLockManager._owned(data, token, os.getpid(), socket.gethostname()):
                data["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                FileLockManager._write_lock_file(lock_file, data)
        except Exception as e:
            logger.exception("Failed to write lock heartbeat")

