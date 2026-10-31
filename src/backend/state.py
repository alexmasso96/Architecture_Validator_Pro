"""
AppState — headless project lifecycle (plan §3.2 / §3.3).

The Qt app drove the logic layer through a god-object ``main_window``. The
worker has no widgets, so AppState owns the equivalent state directly: the open
``ProjectDatabase`` plus the Qt-free ``ArchitectureManager`` / ``ReleaseManager``,
the project path, the edit mode, and the file-lock state. Routers read and
mutate the DB through here.

View-only is enforced server-side with ``PRAGMA query_only=ON`` (so a read-only
session physically cannot write), in addition to the edit lock.
"""
from __future__ import annotations

import atexit
import base64
import logging
import os
import shutil
import tempfile
import threading
import uuid
from typing import Optional

from Application_Logic.Logic_Database import ProjectDatabase, is_network_path
from Application_Logic.Logic_Architecture_Models import ArchitectureManager
from Application_Logic.Logic_Release_Manager import ReleaseManager
from Application_Logic.Logic_Security import SecurityManager
from Application_Logic.Logic_File_Locking import (
    FileLockManager, LOCK_HEARTBEAT_INTERVAL_SECONDS,
)
from Application_Logic import Logic_Crypto as crypto

from .events import EventBus

logger = logging.getLogger(__name__)

MODE_VIEW = "view"          # read-only, no lock
MODE_EXCLUSIVE = "exclusive"  # holds the edit lock, read-write


class ProjectError(RuntimeError):
    """Raised for expected project-lifecycle failures (surfaced as 4xx)."""


class AppState:
    def __init__(self, bus: EventBus,
                 heartbeat_interval: float = LOCK_HEARTBEAT_INTERVAL_SECONDS) -> None:
        self.bus = bus
        self._lock = threading.RLock()
        self._heartbeat_interval = heartbeat_interval

        self.db: Optional[ProjectDatabase] = None
        self.project_path: Optional[str] = None
        self.mode: Optional[str] = None
        self.arch_manager: Optional[ArchitectureManager] = None
        self.release_manager: Optional[ReleaseManager] = None
        self.master_password_hash: Optional[str] = None
        self.lock_info: dict = {}
        self.lock_lost: bool = False
        self._matchers: dict = {}   # ((hash, …), release_id) -> SymbolMatcher (name-list cache)
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop: Optional[threading.Event] = None
        self._build_run_active: bool = False
        self.active_run_id: Optional[int] = None

        # Sync (#16). With a tracked project the session opens the local copy,
        # and the lock and binary store stay on the shared path. ``sync_hold``
        # names who a pull is coming from while reads are stopped.
        self.sync_entry: Optional[dict] = None
        self.sync_token: Optional[str] = None
        self.sync_hold: Optional[str] = None
        self._sync_bridge = None

        # Per-block at-rest encryption (master-password protected projects). The
        # .arch is a PLAINTEXT SQLite file opened directly — only sensitive content
        # columns are encrypted, each under a per-category key held in
        # ``_block_cipher`` for the session. ``_encrypted`` means "has a block
        # cipher". The temp-file machinery below is used ONLY by the one-time
        # legacy whole-file (ARCHENC1) migration.
        self._db_file: Optional[str] = None
        self._encrypted: bool = False
        self._password: Optional[str] = None
        self._block_cipher = None
        self._temp_dir: Optional[str] = None
        self._atexit_registered: bool = False

    def begin_build_run(self) -> None:
        """Marks a build run as started. The project stays open until it ends,
        because the run holds the edit lock."""
        with self._lock:
            if self._build_run_active:
                raise ProjectError("A build run is already in progress.")
            self._build_run_active = True

    def end_build_run(self) -> None:
        with self._lock:
            self._build_run_active = False
            self.active_run_id = None

    def _refuse_during_build_run(self) -> None:
        if self._build_run_active:
            raise ProjectError("A build run is in progress. Cancel it first.")

    @property
    def shared_path(self) -> Optional[str]:
        """Where the project lives for other users: the lock and the binary store
        belong here."""
        return self.sync_entry["remote"] if self.sync_entry else self.project_path

    def sync_store(self):
        from Application_Logic.Logic_Sync_Worker import SyncStore
        from .routers.prefs import app_data_dir
        return SyncStore(str(app_data_dir() / "sync"))

    def block_cipher(self):
        """The session's per-block content cipher (or None). Read by worker
        threads to set_block_cipher on their own connections. Immutable once set,
        so safe to share across threads."""
        return self._block_cipher

    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.db is not None and self.db.is_open

    @property
    def can_edit(self) -> bool:
        return self.mode == MODE_EXCLUSIVE

    def require_open(self) -> ProjectDatabase:
        if not self.is_open:
            raise ProjectError("No project is open.")
        return self.db  # type: ignore[return-value]

    def require_edit(self) -> ProjectDatabase:
        db = self.require_open()
        if self.lock_lost:
            raise ProjectError("Edit lock was lost (taken over by another session). "
                               "Reopen the project to regain exclusive edit.")
        if not self.can_edit:
            raise ProjectError("Project is open in view-only mode.")
        return db

    def require_table_edit(self) -> ProjectDatabase:
        """Like require_edit, but also rejects writes while a baseline snapshot is
        the active release (a loaded baseline is read-only). Branch/freeze use
        plain require_edit so they still work from a baseline."""
        db = self.require_edit()
        rm = self.release_manager
        active_r = rm.get_active_release() if rm is not None else None
        if active_r is not None and active_r.is_baseline:
            raise ProjectError("Active release is a read-only baseline — "
                               "switch to a release to edit the table.")
        return db

    def require_arch(self) -> ArchitectureManager:
        self.require_open()
        if self.arch_manager is None:
            raise ProjectError("No architecture manager.")
        return self.arch_manager

    def model_index_by_id(self, model_id: int) -> int:
        """Index of a model in the manager's list, or raise ProjectError."""
        mgr = self.require_arch()
        for i, m in enumerate(mgr.models):
            if m.id == model_id:
                return i
        raise ProjectError(f"No such model: {model_id}")

    def require_releases(self) -> ReleaseManager:
        self.require_open()
        if self.release_manager is None:
            raise ProjectError("No release manager.")
        return self.release_manager

    def release_index_by_id(self, release_id: int) -> int:
        rm = self.require_releases()
        for i, r in enumerate(rm.releases):
            if r.id == release_id:
                return i
        raise ProjectError(f"No such release: {release_id}")

    def active_elf_hash(self) -> Optional[str]:
        """Primary ELF hash of the active release (its first component symbol
        file), or None when no symbol file is imported."""
        rm = self.release_manager
        if rm is None:
            return None
        active = rm.get_active_release()
        return active.elf_hash if active else None

    def active_elf_hashes(self) -> list:
        """Every component symbol file's hash of the active release, in
        priority order — the union symbol search and matching run against."""
        rm = self.release_manager
        active = rm.get_active_release() if rm is not None else None
        if active is None or active.id is None or not self.is_open:
            return []
        hashes = self.db.get_release_elf_hashes(active.id)
        if not hashes and active.elf_hash:
            hashes = [active.elf_hash]
        return hashes

    def get_symbol_matcher(self, elf_hash, release_id: Optional[int] = None):
        """A name-list-only SymbolMatcher for one ELF hash — or a list of them
        (a release's component files, matched as one pool) — cached per session.
        With ``release_id``, that release's source-side names join the pool
        too: its #define macros and its Code Maps' call-tree names (neither
        reaches the ELF).

        The DB-backed matcher loads only symbol *name* strings (not full objects)
        and never touches the parser, so we build it with ``parser=None``.
        Call ``invalidate_matchers()`` when a release's symbols or source change.
        """
        from Application_Logic.Logic_Database import elf_hash_list
        from Application_Logic.Logic_Source_Macros import release_match_extras
        from Application_Logic.Logic_Symbol_Matcher import SymbolMatcher
        db = self.require_open()
        hashes = elf_hash_list(elf_hash)
        for h in hashes:
            if not db.has_elf(h):
                raise ProjectError(f"No ELF in project for hash {h}.")
        key = (tuple(hashes), release_id)
        if key not in self._matchers:
            macros = (release_match_extras(db, release_id)
                      if release_id is not None else None)
            self._matchers[key] = SymbolMatcher(None, db=db, elf_hash=hashes,
                                                macros=macros)
        return self._matchers[key]

    def invalidate_matchers(self) -> None:
        self._matchers.clear()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def new_project(self, path: str, password: Optional[str] = None) -> dict:
        """Create a fresh .arch at ``path`` and open it exclusive-edit.

        With a real ``password`` the project uses per-block content encryption:
        the .arch is a plaintext SQLite file (opened directly) whose sensitive
        columns are encrypted under per-category keys derived from the password.
        No password (or a test-bypass password) → fully plaintext SQLite.
        """
        with self._lock:
            self._refuse_during_build_run()
            self._close_locked()
            if os.path.exists(path):
                raise ProjectError(f"File already exists: {path}")

            if is_network_path(path):
                return self._new_shared_project(path, password)

            db, cipher, encrypted = self._create_project_db(path, password)
            acquired, msg = FileLockManager.acquire_lock(path)
            if not acquired:
                db.close()
                raise ProjectError(f"Could not acquire edit lock: {msg}")

            self._db_file = path
            self._encrypted = encrypted
            self._password = password
            self._block_cipher = cipher
            self._wire(db, path, MODE_EXCLUSIVE)
            self.bus.publish("db-changed", {"reason": "new"})
            return self.status()

    def _create_project_db(self, path: str, password: Optional[str]) -> tuple:
        encrypted = not crypto.bypasses_encryption(password)
        db = ProjectDatabase()
        db.open(path)               # plaintext SQLite directly at path
        cipher = None
        if encrypted:
            cipher = self._init_block_encryption(db, password)
            db.set_block_cipher(cipher)
        # Seed the default table layout so a brand-new project opens with the
        # same basic columns the PyQt6 app provided (Logic_Column_Layout).
        from Application_Logic.Logic_Column_Layout import DEFAULT_COLUMN_LAYOUT
        db.save_column_layout(DEFAULT_COLUMN_LAYOUT)
        db.commit()
        return db, cipher, encrypted

    def _new_shared_project(self, path: str, password: Optional[str]) -> dict:
        """Creates the project locally and publishes it, because SQLite can't be
        trusted to write to a share. It's tracked with no extra steps."""
        from Application_Logic import Logic_Sync_Engine as engine

        store = self.sync_store()
        folder = os.path.join(store.root, f"new-{uuid.uuid4().hex}")
        os.makedirs(folder)
        try:
            local = os.path.join(folder, os.path.basename(path))
            db, _cipher, _encrypted = self._create_project_db(local, password)
            db.close()
            user = FileLockManager.get_username()
            engine.publish(local, path, user)
            store.add(path, user)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        return self.open_project(path, MODE_EXCLUSIVE, password)

    def open_project(self, path: str, mode: str = MODE_EXCLUSIVE,
                     password: Optional[str] = None) -> dict:
        """Open an existing .arch. ``mode`` is 'exclusive' or 'view'.

        Plaintext (legacy/dev) projects open directly. Encrypted projects need
        the master password — without it, ``crypto.PasswordRequired`` is raised;
        a wrong password raises ``crypto.PasswordInvalid`` (mapped to 401/403).
        """
        if mode not in (MODE_VIEW, MODE_EXCLUSIVE):
            raise ProjectError(f"Unknown mode: {mode}")
        with self._lock:
            self._refuse_during_build_run()
            self._close_locked()
            if not os.path.exists(path) and self.sync_store().find(path) is None:
                raise ProjectError(f"No such file: {path}")

            local = self._sync_open(path, mode)
            if local is not None:
                path = local

            # Resolve the per-block cipher (and migrate a legacy whole-file
            # ARCHENC1 project to per-block in place). Raises PasswordRequired /
            # PasswordInvalid for the existing 401/403 mapping — before we lock.
            cipher = self._resolve_cipher_on_open(path, password)
            encrypted = cipher is not None

            if mode == MODE_EXCLUSIVE and self.sync_entry is None:
                acquired, msg = FileLockManager.acquire_lock(path)
                if not acquired:
                    # Fall back to view-only with the contended-lock detail.
                    self.lock_info = FileLockManager.check_lock(path)
                    raise ProjectError(f"Locked by another session: {msg}")

            db = ProjectDatabase()
            db.open(path)
            # Attach the cipher BEFORE _wire (registry loads read encrypted rows)
            # and before read-only (reads still need to decrypt in view mode).
            if cipher is not None:
                db.set_block_cipher(cipher)
            if mode == MODE_VIEW:
                db.set_read_only(True)   # PRAGMA query_only=ON
            self._db_file = path
            self._encrypted = encrypted
            self._password = password
            self._block_cipher = cipher
            self._wire(db, path, mode)
            self._sync_session_started()
            self.bus.publish("db-changed", {"reason": "open"})
            return self.status()

    def flush_active_model_to_release(self) -> None:
        """Sync the active model's working rows (architecture_rows) into the
        active release's release_rows.

        The React table edits architecture_rows directly; release_rows is the
        per-release snapshot the lineage/baselines read. Keeping them in sync on
        save (and on release switch) is what makes activating a release actually
        change the table — see activate_release. No-op for a frozen baseline
        (write-protected) or a read-only DB.
        """
        if self.db is None or getattr(self.db, "read_only", False):
            return
        mgr_a = self.arch_manager
        rm = self.release_manager
        if mgr_a is None or rm is None:
            return
        model = mgr_a.get_active_model()
        rel = rm.get_active_release()
        if model is None or model.id is None or rel is None or rel.id is None:
            return
        if rel.is_baseline:
            return  # frozen snapshot — never overwrite
        self.db.save_release_rows(rel.id, self.db.get_model_rows(model.id))

    def save_project(self) -> dict:
        """Persist pending work: commit, checkpoint.

        In the worker the .arch *is* the live DB — routers mutate it directly —
        so 'save' is a durability barrier (commit + WAL checkpoint), not a
        table-flush like the Qt app. We do still flush the active model's working
        rows into the active release snapshot so release_rows stays authoritative.
        """
        with self._lock:
            db = self.require_edit()
            try:
                self.flush_active_model_to_release()
                db.commit()
                try:
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:  # noqa: BLE001 — checkpoint best-effort
                    pass
                db.commit()
                # No whole-file re-encryption: content is already encrypted at the
                # column level, so the .arch on disk is durable after the commit.
            except Exception as e:  # noqa: BLE001
                raise ProjectError(f"Save failed: {e}") from e
            self._request_push()
            self.bus.publish("db-changed", {"reason": "save"})
            return self.status()

    def close_project(self) -> dict:
        with self._lock:
            self._refuse_during_build_run()
            self._close_locked()
            self.bus.publish("db-changed", {"reason": "close"})
            return self.status()

    def shutdown_project(self) -> None:
        """Closes the project as the worker exits, even during a build run. An
        unfinished run is offered for resuming on the next start."""
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            active_model = None
            active_release = None
            model_count = release_count = 0
            if self.arch_manager is not None:
                active = self.arch_manager.get_active_model()
                active_model = active.name if active else None
                model_count = len([m for m in self.arch_manager.models if not m.is_deleted])
            active_release_is_baseline = False
            if self.release_manager is not None:
                active_r = self.release_manager.get_active_release()
                active_release = active_r.name if active_r else None
                active_release_is_baseline = bool(active_r and active_r.is_baseline)
                release_count = len(self.release_manager.releases)
            return {
                "open": self.is_open,
                "path": self.shared_path,
                "mode": self.mode,
                # A loaded baseline is a read-only snapshot: edits are blocked even
                # in an exclusive-edit project, so fold it into can_edit.
                "can_edit": self.can_edit and not active_release_is_baseline,
                "active_model": active_model,
                "active_release": active_release,
                "active_release_is_baseline": active_release_is_baseline,
                "model_count": model_count,
                "release_count": release_count,
                "lock_info": self.lock_info,
                "lock_lost": self.lock_lost,
                "encrypted": self._encrypted,
                "tracked": self.sync_entry is not None,
            }

    # ------------------------------------------------------------------
    # internals (call with self._lock held)
    # ------------------------------------------------------------------
    def _wire(self, db: ProjectDatabase, path: str, mode: str) -> None:
        self.db = db
        self.project_path = path
        self.mode = mode
        self.arch_manager = ArchitectureManager(path)
        self.arch_manager.set_db(db)
        self.arch_manager.load_registry()
        self.release_manager = ReleaseManager(path)
        self.release_manager.set_db(db)
        self.release_manager.load_registry()
        self.master_password_hash = db.get_meta("master_password_hash")
        self.lock_lost = False
        if mode == MODE_EXCLUSIVE:
            self.lock_info = {"held": True, "by": FileLockManager.get_username()}
            self._start_heartbeat(self.shared_path)
        else:
            self.lock_info = FileLockManager.check_lock(self.shared_path, self.sync_token)

    # ------------------------------------------------------------------
    # Per-block encryption helpers (call with self._lock held)
    # ------------------------------------------------------------------
    def _init_block_encryption(self, db: ProjectDatabase, password: str):
        """Generate a salt + cipher for a NEW encrypted project and stamp the
        scheme/salt/canary/master-hash into project_meta. Returns the cipher.
        Called before set_block_cipher, so the meta writes land plaintext (the
        canary is already cipher output; the salt/scheme/hash must stay plain)."""
        from Application_Logic.Logic_Block_Crypto import BlockCipher, ENC_SCHEME
        salt = BlockCipher.new_salt()
        cipher = BlockCipher.from_password(password, salt)
        db.set_meta("enc_scheme", ENC_SCHEME)
        db.set_meta("enc_kdf_salt", base64.urlsafe_b64encode(salt).decode("ascii"))
        db.set_meta("enc_canary", cipher.make_canary())
        db.set_meta("master_password_hash", SecurityManager.hash_password(password))
        return cipher

    def _resolve_cipher_on_open(self, path: str, password: Optional[str]):
        """Resolve the per-block cipher for an existing file, migrating a legacy
        whole-file (ARCHENC1) project in place. Returns the cipher, or None for a
        plaintext/test-bypass project. Raises PasswordRequired/PasswordInvalid."""
        from Application_Logic.Logic_Block_Crypto import BlockCipher, ENC_SCHEME
        if crypto.is_encrypted_file(path):
            if not password:
                raise crypto.PasswordRequired("Master password required.")
            return self._migrate_legacy_encrypted(path, password)
        if not crypto.is_plaintext_sqlite(path):
            raise ProjectError("Unrecognized project file format.")
        scheme, salt_b64, canary = self._peek_enc_meta(path)
        if scheme != ENC_SCHEME:
            return None  # plaintext / test-bypass project — no cipher
        if not password:
            raise crypto.PasswordRequired("Master password required.")
        if not salt_b64:
            raise ProjectError("Encrypted project is missing its key salt.")
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        cipher = BlockCipher.from_password(password, salt)
        cipher.verify_canary(canary)   # raises PasswordInvalid on wrong password
        return cipher

    @staticmethod
    def _peek_enc_meta(path: str):
        """Read (enc_scheme, enc_kdf_salt, enc_canary) from a plaintext SQLite
        without going through the full open path. Returns (None, None, None) for
        a project that predates per-block encryption."""
        import sqlite3
        conn = sqlite3.connect(path)
        try:
            def g(k):
                try:
                    row = conn.execute(
                        "SELECT value FROM project_meta WHERE key=?", (k,)).fetchone()
                    return row[0] if row else None
                except sqlite3.Error:
                    return None
            return g("enc_scheme"), g("enc_kdf_salt"), g("enc_canary")
        finally:
            conn.close()

    def _migrate_legacy_encrypted(self, path: str, password: str):
        """One-time: decrypt a legacy ARCHENC1 whole-file project to a temp DB,
        encrypt all content columns per-block, then write the plaintext SQLite
        back over ``path``. Returns the cipher."""
        from Application_Logic.Logic_Block_Crypto import migrate_to_per_block
        from Application_Logic.Logic_Database import ENCRYPTED_META_KEYS
        db_file = self._provision_db_file(path, encrypted=True)
        crypto.decrypt_file(path, db_file, password)   # PasswordInvalid on wrong pw
        db = ProjectDatabase()
        db.open(db_file)               # create_schema=True runs any v→4 upgrade
        try:
            cipher = migrate_to_per_block(db, password, meta_keys=ENCRYPTED_META_KEYS)
            db.commit()
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.commit()
            except Exception:  # noqa: BLE001 — checkpoint best-effort
                pass
        finally:
            db.close()
        tmp = path + ".mig.tmp"
        shutil.copyfile(db_file, tmp)
        os.replace(tmp, path)          # atomic replace of the ARCHENC1 blob
        self._purge_temp()
        return cipher

    def _provision_db_file(self, project_path: str, encrypted: bool) -> str:
        """A private temp file for the one-time legacy-migration decrypt."""
        if not self._atexit_registered:
            atexit.register(self._purge_temp)   # crash-safety net for the temp dir
            self._atexit_registered = True
        self._temp_dir = tempfile.mkdtemp(prefix="archsess_")
        try:
            os.chmod(self._temp_dir, 0o700)
        except OSError:
            pass
        return os.path.join(self._temp_dir, "project.db")

    def _purge_temp(self) -> None:
        """Best-effort shred + remove the session temp dir (decrypted DB)."""
        tmp = self._temp_dir
        if not tmp:
            return
        try:
            for name in os.listdir(tmp):
                fp = os.path.join(tmp, name)
                try:
                    size = os.path.getsize(fp)
                    with open(fp, "r+b") as f:
                        f.write(b"\x00" * size)
                        f.flush()
                        os.fsync(f.fileno())
                except OSError:
                    pass
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        self._temp_dir = None

    # ------------------------------------------------------------------
    # Lock heartbeat (plan §3.3)
    # ------------------------------------------------------------------
    def _start_heartbeat(self, path: str) -> None:
        self._stop_heartbeat()
        stop = threading.Event()
        self._hb_stop = stop
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, args=(path, stop),
            name="lock-heartbeat", daemon=True)
        self._hb_thread.start()

    def _stop_heartbeat(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()
        thread = self._hb_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._hb_thread = None
        self._hb_stop = None

    def _heartbeat_loop(self, path: str, stop: threading.Event) -> None:
        """Refresh the lock's last_seen on a timer; if we no longer own it, flag
        the session lock-lost and emit a `lock` SSE event so the UI drops to
        view-only. Runs on its own thread and never touches the DB connection
        (sqlite connections are thread-affine) — it only reads/writes the lock file.
        """
        while not stop.wait(self._heartbeat_interval):
            # A share that has dropped out isn't a takeover. The session stays
            # open, and the lock is checked again once the share is back.
            if not os.path.isdir(os.path.dirname(path) or "."):
                continue
            try:
                status = FileLockManager.check_lock(path, self.sync_token)
            except Exception:  # noqa: BLE001 — lock file race; try again next tick
                continue
            if status.get("status") == "unreadable":  # not a takeover; try again next tick
                continue
            if status.get("status") != "locked_by_me":
                self._on_lock_lost(status)
                return
            FileLockManager.write_heartbeat(path, self.sync_token)

    # ------------------------------------------------------------------
    # Sync (#16) (call with self._lock held unless noted)
    # ------------------------------------------------------------------
    def _sync_open(self, path: str, mode: str) -> Optional[str]:
        """Prepares a tracked project and returns its local copy, or None for a
        project that isn't shared. A project with a manifest beside it is tracked
        here too, so nobody edits the shared copy behind the others' backs. If the
        share can't be reached, an existing local copy still opens to view."""
        from Application_Logic import Logic_Sync_Engine as engine

        store = self.sync_store()
        user = FileLockManager.get_username()
        entry = store.find(path)
        try:
            if entry is None:
                if engine.read_manifest(path) is None:
                    return None
                entry = store.add(path, user)
        except (engine.SyncError, OSError):
            return None

        token = uuid.uuid4().hex
        reachable = os.path.isfile(entry["remote"])
        if mode == MODE_EXCLUSIVE:
            if not reachable:
                raise ProjectError("The shared copy can't be reached, so the project "
                                   "can only be opened to view.")
            self._use_worker_lock_watch(store, entry)
            acquired, msg = FileLockManager.acquire_lock(entry["remote"], token)
            if not acquired:
                self.lock_info = FileLockManager.check_lock(entry["remote"])
                raise ProjectError(f"Locked by another session: {msg}")
        try:
            if not os.path.exists(entry["local"]):
                if not reachable:
                    raise ProjectError("The shared copy can't be reached, and there's "
                                       "no local copy yet.")
                engine.clone(entry["remote"], entry["local"])
            elif reachable:
                local = engine.local_state(entry["local"])
                manifest = engine.read_manifest(entry["remote"])
                if manifest and manifest["revision"] > local["base"]:
                    if local["dirty"]:
                        engine.save_conflict_copy(entry["local"], entry["remote"], user)
                        engine.clone(entry["remote"], entry["local"])
                    else:
                        engine.pull(entry["local"], entry["remote"])
        except engine.SyncError as e:
            if mode == MODE_EXCLUSIVE:
                FileLockManager.release_lock(entry["remote"], token)
                raise ProjectError(f"The project couldn't be brought up to date: {e}") from e
            logger.warning("Opening the local copy without syncing: %s", e)
        except ProjectError:
            if mode == MODE_EXCLUSIVE:
                FileLockManager.release_lock(entry["remote"], token)
            raise
        self.sync_entry = entry
        self.sync_token = token
        return entry["local"]

    def _use_worker_lock_watch(self, store, entry: dict) -> None:
        """The always-running worker may have watched this lock for hours, so a
        stopped session's lock doesn't need another 10 minutes here."""
        from Application_Logic.Logic_Sync_Worker import lock_age_seconds

        lock_file = FileLockManager.get_lock_file_path(entry["remote"])
        try:
            with open(lock_file, "r") as f:
                contents = f.read()
        except OSError:
            return
        age = lock_age_seconds(store, entry, contents)
        if age is not None:
            FileLockManager.seed_observation(lock_file, contents, age)

    def _sync_session_started(self) -> None:
        from Application_Logic.Logic_Sync_Worker import SESSION_FILE, write_json
        from .sync_bridge import SyncBridge

        if self.sync_entry is None:
            return
        store = self.sync_store()
        write_json(store.control(self.sync_entry, SESSION_FILE),
                   {"pid": os.getpid(), "mode": self.mode})
        self._sync_bridge = SyncBridge(self, store, self.sync_entry)
        self._sync_bridge.start()
        self.ensure_sync_worker()

    def ensure_sync_worker(self) -> None:
        from Application_Logic.Logic_Sync_Worker import worker_running
        from desktop.sync_worker import reap_stopped, start_detached

        if os.environ.get("ARCH_SYNC_WORKER") == "off":
            return
        reap_stopped()
        if worker_running(self.sync_store()):
            return
        try:
            start_detached()
        except OSError:
            logger.warning("Could not start the Sync Worker", exc_info=True)

    def _request_push(self, closing: bool = False) -> None:
        """Asks the worker to publish. With ``closing``, it also releases the lock
        once the last push has finished."""
        from Application_Logic.Logic_Sync_Worker import PUSH_REQUEST, write_json

        if self.sync_entry is None or self.mode != MODE_EXCLUSIVE or self.lock_lost:
            return
        store = self.sync_store()
        write_json(store.control(self.sync_entry, PUSH_REQUEST),
                   {"token": self.sync_token, "release_lock": closing})
        self.ensure_sync_worker()

    def publish_now(self) -> None:
        """Publishes work finished in the background, without waiting for Save."""
        with self._lock:
            self._request_push()

    def _sync_session_ended(self) -> None:
        from Application_Logic.Logic_Sync_Worker import SESSION_FILE, remove_file

        store = self.sync_store()
        entry = self.sync_entry
        if self._sync_bridge is not None:
            self._sync_bridge.stop()
            self._sync_bridge = None
        try:
            self._request_push(closing=True)
            remove_file(store.control(entry, SESSION_FILE))
            self.ensure_sync_worker()
        except OSError:
            logger.warning("Error ending the sync session", exc_info=True)
        self.sync_entry = None
        self.sync_token = None
        self.sync_hold = None

    def reload_after_pull(self) -> None:
        """Re-reads what the session caches from the database, because a pull
        changed it underneath. Runs on the bridge thread."""
        with self._lock:
            if self.is_open:
                self.arch_manager.load_registry()
                self.release_manager.load_registry()
                self.invalidate_matchers()
        self.bus.publish("db-changed", {"reason": "sync-pull"})

    def on_sync_stopped(self, status: dict) -> None:
        """Called on the bridge thread: this session's edits can't be published,
        so it drops to view-only."""
        self._on_lock_lost({"status": "sync_stopped", "message": status.get("message", "")})

    def _on_lock_lost(self, info: dict) -> None:
        # Minimal, thread-safe: flip a flag + publish. require_edit() then refuses
        # writes (409) and status() reports lock_lost; we do NOT mutate the DB
        # connection or managers from this thread.
        self.lock_lost = True
        self.lock_info = info
        self.bus.publish("lock", {"lost": True, "info": info})

    def _close_locked(self) -> None:
        self._stop_heartbeat()
        self.lock_lost = False
        if self.db is not None and self.db.is_open:
            try:
                self.db.close()
            except Exception:  # noqa: BLE001
                logger.warning("Error closing DB", exc_info=True)
        if self.sync_entry is not None:
            self._sync_session_ended()
        elif self.project_path and self.mode == MODE_EXCLUSIVE:
            try:
                FileLockManager.release_lock(self.project_path)
            except Exception:  # noqa: BLE001
                logger.warning("Error releasing lock", exc_info=True)
        self._purge_temp()
        self.db = None
        self.project_path = None
        self.mode = None
        self.arch_manager = None
        self.release_manager = None
        self.master_password_hash = None
        self.lock_info = {}
        self._matchers = {}
        self._db_file = None
        self._encrypted = False
        self._password = None
        self._block_cipher = None
