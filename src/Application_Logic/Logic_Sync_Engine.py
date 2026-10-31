"""
Sync engine (#16)
=================
Keeps a local copy of a project and its shared copy on a NAS in step, one section
at a time. Both are ordinary ``.arch`` files. Next to the shared copy, a small
manifest (``Project.arch.sync``) holds the revision of the project and of each
section, so polling never has to open the database.

Stored column values are copied as they are, so sync never needs the master
password and never handles plaintext.

Only one user can edit at a time, so a push needs no merging: it's refused if the
shared copy has moved on from the revision the local copy was based on, and the
worker keeps the local work as a conflict copy.

The shared file is opened without SQLite's file locks, which can't be relied on
over a network share: on a Linux SMB mount they stop every commit. The edit lock
already allows one writer. Readers are protected by the manifest instead: a writer
marks it ``writing`` first and clears it when done, and a reader compares it before
and after, throwing away anything that overlapped a write. SQLite's journal still
rolls back a write that was interrupted.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from .Logic_Database import DB_SCHEMA_VERSION
from .Logic_File_Locking import retry_while_busy
from . import Logic_Sync_Sections as sections

MANIFEST_SUFFIX = ".sync"
BUSY_TIMEOUT_MS = 30000
NO_LOCK_VFS = "win32-none" if sys.platform == "win32" else "unix-none"
LOCAL_VFS = "win32" if sys.platform == "win32" else "unix"
WAL_FORMAT = 2    # byte 18 of a SQLite file's header

META_BASE = "sync_base_revision"
META_REVISION = "sync_revision"
META_LAST_PUSH = "sync_last_push"
META_PENDING_PUSH = "sync_pending_push"

DIRTY_NO = 0
DIRTY_YES = 1
DIRTY_PUSHING = 2


class SyncError(RuntimeError):
    """Sync can't go ahead right now. It's safe to try again later."""


class SyncConflict(SyncError):
    """The shared copy moved on, so a push from this base would lose work."""


class SyncBlocked(SyncError):
    """Sync stopped for this project until a person acts."""


def manifest_path(remote: str) -> str:
    return remote + MANIFEST_SUFFIX


def read_manifest(remote: str) -> Optional[dict]:
    """The manifest, or None if there isn't one. An unreachable share raises."""
    try:
        with open(manifest_path(remote), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if not os.path.isdir(os.path.dirname(remote) or "."):
            raise SyncError(f"The shared folder can't be reached: {remote}") from None
        return None
    except ValueError as e:
        raise SyncError(f"The sync manifest can't be read: {e}") from e
    except OSError as e:
        raise SyncError(f"The shared folder can't be reached: {e}") from e


def _write_manifest(remote: str, manifest: dict) -> None:
    path = manifest_path(remote)
    temp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
        retry_while_busy(os.replace, temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def sqlite_uri(path: str, **params) -> str:
    """A ``file:`` URI for a path, including Windows drive and UNC paths."""
    plain = os.path.abspath(path).replace("\\", "/")
    if not plain.startswith("/"):
        plain = "/" + plain
    query = urllib.parse.urlencode(params)
    return f"file://{urllib.parse.quote(plain, safe='/:')}" + (f"?{query}" if query else "")


def _shared_uri(path: str, **params) -> str:
    return sqlite_uri(path, vfs=NO_LOCK_VFS, **params)


def _connect(path: str, shared: bool = False) -> sqlite3.Connection:
    """A connection for sync. ``shared`` opens a file on the share without locks."""
    uri = _shared_uri(path) if shared else sqlite_uri(path)
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
                           isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # A section's rows arrive in table order, and parents can be in other sections.
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _meta(conn, key: str, default=None, db: str = "main"):
    row = conn.execute(f"SELECT value FROM {db}.project_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO project_meta (key, value) VALUES (?, ?)",
                 (key, str(value)))


def _manifest_for(conn, revision: int, username: str) -> dict:
    rows = conn.execute("SELECT scope, section, revision FROM sync_sections").fetchall()
    return {"project_uuid": _meta(conn, "project_uuid", ""),
            "schema_version": int(_meta(conn, "schema_version", 0)),
            "revision": revision, "username": username,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sections": {f"{r[0]}|{r[1]}": r[2] for r in rows}}


def _stable(remote: str, before: dict) -> None:
    """Raises unless the shared copy stayed as ``before`` throughout a read."""
    after = read_manifest(remote)
    if (after is None or after.get("writing")
            or after.get("revision") != before.get("revision")):
        raise SyncError("The shared copy changed during the read. Trying again shortly.")


def _begin_write(remote: str, manifest: dict, username: str) -> None:
    _write_manifest(remote, {**manifest, "writing": username})


def clear_interrupted_write(remote: str) -> None:
    """Finishes off a writer that stopped, once nobody holds the edit lock. Opening
    the file for writing rolls the journal back, and readers can then carry on."""
    manifest = read_manifest(remote)
    if not manifest or not manifest.get("writing"):
        return
    conn = _connect(remote, shared=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        revision = int(_meta(conn, META_REVISION, 0))
        fresh = _manifest_for(conn, revision, manifest.get("username", ""))
    finally:
        conn.close()
    _write_manifest(remote, fresh)


def _check_schema(manifest: dict) -> None:
    if manifest.get("schema_version") != DB_SCHEMA_VERSION:
        raise SyncBlocked("App versions disagree on the project format. Update the "
                          "app on every PC.")


# ----------------------------------------------------------------------
# Applying a section
# ----------------------------------------------------------------------
def _columns(conn, table: str, db: str) -> tuple:
    info = conn.execute(f"PRAGMA {db}.table_info({table})").fetchall()
    names = [r[1] for r in info]
    keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
    return names, keys or ["rowid"]


def _sync_table(conn, table: sections.Table, scope_id, src: str = "src") -> tuple:
    """Makes ``main``'s rows of this table match ``src`` within the scope. Rows are
    compared by primary key, and by content_hash where the table has one.
    Returns ``(rows_changed, bytes_sent)``."""
    name = table.name
    columns, keys = _columns(conn, name, "main")
    src_columns = set(_columns(conn, name, src)[0])
    columns = [c for c in columns if c in src_columns]
    compare = ["content_hash"] if "content_hash" in columns else [
        c for c in columns if c not in keys]
    join = " AND ".join(f"d.{c} IS s.{c}" for c in keys)
    same = " AND ".join(f"d.{c} IS s.{c}" for c in [*keys, *compare])
    params = () if scope_id is None else (scope_id,)

    src_filter = (table.filter.format(a="s", db=src) if table.filter else "1=1")
    dst_filter = (table.filter.format(a="d", db="main") if table.filter else "1=1")
    changed = (f"FROM {src}.{name} s WHERE {src_filter} AND NOT EXISTS "
               f"(SELECT 1 FROM main.{name} d WHERE {same})")

    sizes = " + ".join(f"COALESCE(LENGTH(CAST(s.{c} AS BLOB)), 0)" for c in columns)
    count, size = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({sizes}), 0) {changed}", params).fetchone()
    removed = conn.execute(
        f"DELETE FROM main.{name} AS d WHERE {dst_filter} AND NOT EXISTS "
        f"(SELECT 1 FROM {src}.{name} s WHERE {join})", params).rowcount
    if count:
        written = list(columns) + (["rowid"] if keys == ["rowid"] else [])
        listing = ", ".join(written)
        selected = ", ".join(f"s.{c}" for c in written)
        conn.execute(f"INSERT OR REPLACE INTO main.{name} ({listing}) "
                     f"SELECT {selected} {changed}", params)
    return count + removed, size


def _sync_section(conn, section: sections.Section, scope_id, src: str = "src") -> tuple:
    rows = sent = 0
    for table in section.tables:
        table_rows, table_bytes = _sync_table(conn, table, scope_id, src)
        rows += table_rows
        sent += table_bytes
    return rows, sent


def _sync_elf(conn, src: str = "src") -> tuple:
    """ELF data is keyed by hash, so only hashes one side lacks are transferred."""
    theirs = {r[0] for r in conn.execute(f"SELECT elf_hash FROM {src}.{sections.ELF_INDEX}")}
    ours = {r[0] for r in conn.execute(f"SELECT elf_hash FROM main.{sections.ELF_INDEX}")}
    rows = sent = 0
    for table in (*sections.ELF_TABLES, sections.ELF_INDEX):
        columns = _columns(conn, table, "main")[0]
        listing = ", ".join(c for c in columns if c in set(_columns(conn, table, src)[0]))
        for elf_hash in theirs - ours:
            rows += conn.execute(
                f"INSERT OR REPLACE INTO main.{table} ({listing}) "
                f"SELECT {listing} FROM {src}.{table} WHERE elf_hash=?",
                (elf_hash,)).rowcount
        for elf_hash in ours - theirs:
            rows += conn.execute(f"DELETE FROM main.{table} WHERE elf_hash=?",
                                 (elf_hash,)).rowcount
    return rows, sent


def _copy_sync_log(conn, src: str = "src") -> None:
    conn.execute(f"INSERT OR IGNORE INTO main.sync_log SELECT * FROM {src}.sync_log "
                 f"WHERE id > (SELECT COALESCE(MAX(id), 0) FROM main.sync_log)")


# ----------------------------------------------------------------------
# First copies
# ----------------------------------------------------------------------
def _backup(source: str, target: str) -> None:
    with sqlite3.connect(source, timeout=BUSY_TIMEOUT_MS / 1000) as src:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()


def _copy_to_share(local: str, target: str, prepare=None) -> None:
    """Writes a local project to the share as one plain file, ending in a rename.
    The journal mode is set here, because WAL can't work on a share."""
    staged = f"{local}.{uuid.uuid4().hex}.stage"
    uploading = f"{target}.{uuid.uuid4().hex}.tmp"
    try:
        _backup(local, staged)
        conn = _connect(staged)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            if prepare is not None:
                prepare(conn)
        finally:
            conn.close()
        shutil.copyfile(staged, uploading)
        retry_while_busy(os.replace, uploading, target)
    finally:
        for path in (staged, uploading):
            if os.path.exists(path):
                os.remove(path)


def publish(local: str, remote: str, username: str) -> dict:
    """Puts a project on the share for the first time."""
    if os.path.exists(remote):
        raise SyncBlocked(f"A file already exists at {remote}.")
    made: dict = {}

    def prepare(conn) -> None:
        sections.install_triggers(conn)
        _set_meta(conn, META_REVISION, 1)
        conn.execute("DELETE FROM sync_sections")
        made.update(_manifest_for(conn, 1, username))

    _copy_to_share(local, remote, prepare)
    _write_manifest(remote, made)
    return made


def _leave_wal_mode(remote: str) -> None:
    """A copied project can still be in WAL mode, which a share can't use. The
    change happens on a local copy, because SQLite can't make it on the share."""
    with open(remote, "rb") as f:
        header = f.read(20)
    if len(header) < 20 or header[18] != WAL_FORMAT:
        return
    with tempfile.TemporaryDirectory() as folder:
        local = os.path.join(folder, os.path.basename(remote))
        shutil.copyfile(remote, local)
        conn = _connect(local)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        uploading = f"{remote}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(local, uploading)
            retry_while_busy(os.replace, uploading, remote)
        finally:
            if os.path.exists(uploading):
                os.remove(uploading)


def adopt_remote(remote: str, username: str) -> dict:
    """Prepares a database that was put on the share by hand. Nothing in it moves,
    so it stays a normal ``.arch``."""
    _leave_wal_mode(remote)
    conn = _connect(remote, shared=True)
    try:
        sections.install_triggers(conn)
        revision = int(_meta(conn, META_REVISION, 0)) or 1
        _set_meta(conn, META_REVISION, revision)
        manifest = _manifest_for(conn, revision, username)
    finally:
        conn.close()
    _write_manifest(remote, manifest)
    return manifest


def clone(remote: str, local: str, only_if_missing: bool = False) -> None:
    """The first download: a full copy to a temp file, then an atomic swap. With
    ``only_if_missing``, a copy that appeared meanwhile is left alone, because the
    app may already have it open."""
    os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
    temp = f"{local}.{uuid.uuid4().hex}.tmp"
    before = read_manifest(remote)
    if before is None:
        raise SyncError("The shared copy has no sync manifest.")
    if before.get("writing"):
        raise SyncError(f"{before['writing']} is publishing. Trying again shortly.")
    try:
        shutil.copyfile(remote, temp)
        _stable(remote, before)
        conn = _connect(temp)
        try:
            sections.install_triggers(conn)
            _set_meta(conn, META_BASE, _meta(conn, META_REVISION, 1))
            conn.execute("UPDATE sync_sections SET dirty=0")
        finally:
            conn.close()
        if only_if_missing and os.path.exists(local):
            return
        for suffix in ("-wal", "-shm"):
            if os.path.exists(local + suffix):
                os.remove(local + suffix)
        os.replace(temp, local)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


# ----------------------------------------------------------------------
# Push
# ----------------------------------------------------------------------
def local_state(local: str) -> dict:
    conn = _connect(local)
    try:
        dirty = conn.execute("SELECT COUNT(*) FROM sync_sections WHERE dirty != 0").fetchone()[0]
        return {"base": int(_meta(conn, META_BASE, 0)), "dirty": dirty,
                "pending": _meta(conn, META_PENDING_PUSH)}
    finally:
        conn.close()


def _elf_differs(conn) -> bool:
    theirs = {r[0] for r in conn.execute(f"SELECT elf_hash FROM src.{sections.ELF_INDEX}")}
    ours = {r[0] for r in conn.execute(f"SELECT elf_hash FROM main.{sections.ELF_INDEX}")}
    return theirs != ours


def _names(conn, db: str) -> dict:
    return dict(conn.execute(f"SELECT id, name FROM {db}.releases").fetchall())


def _finish_push(local_conn, remote: str, revision: int, username: str) -> None:
    """The steps after the shared database has committed. Also how an interrupted
    push completes, because the shared copy already holds the new revision."""
    remote_conn = _connect(remote, shared=True)
    try:
        _write_manifest(remote, _manifest_for(remote_conn, revision, username))
    finally:
        remote_conn.close()
    local_conn.execute("ATTACH DATABASE ? AS src", (_shared_uri(remote, mode="ro"),))
    try:
        local_conn.execute("BEGIN IMMEDIATE")
        local_conn.execute("UPDATE sync_sections SET revision=?, dirty=0 WHERE dirty=?",
                           (revision, DIRTY_PUSHING))
        _set_meta(local_conn, META_BASE, revision)
        local_conn.execute("DELETE FROM project_meta WHERE key=?", (META_PENDING_PUSH,))
        _copy_sync_log(local_conn)
        local_conn.execute("COMMIT")
    except Exception:
        local_conn.execute("ROLLBACK")
        raise
    finally:
        local_conn.execute("DETACH DATABASE src")


def push(local: str, remote: str, username: str,
         still_ours: Callable[[], bool] = lambda: True) -> dict:
    """Sends the dirty sections. Returns the sections, rows and bytes sent.

    An interrupted push rolls back through SQLite's journal, the dirty flags
    survive, and the next call tries again.
    """
    try:
        return _push(local, remote, username, still_ours)
    except sqlite3.DatabaseError as e:
        raise SyncError(f"The push didn't finish: {e}") from e


def _push(local: str, remote: str, username: str, still_ours: Callable[[], bool]) -> dict:
    # The manifest comes first: an unreachable share also has no readable lock,
    # and that's an outage to wait out, not a lost lock.
    manifest = read_manifest(remote)
    if manifest is None:
        raise SyncError("The shared copy has no sync manifest.")
    _check_schema(manifest)
    if not still_ours():
        raise SyncConflict("The edit lock is no longer ours.")

    conn = _connect(local)
    try:
        base = int(_meta(conn, META_BASE, 0))
        pending = _meta(conn, META_PENDING_PUSH)
        if pending:
            remote_check = _connect(remote, shared=True)
            try:
                last = _meta(remote_check, META_LAST_PUSH)
                revision = int(_meta(remote_check, META_REVISION, 0))
            finally:
                remote_check.close()
            if last == pending:
                _finish_push(conn, remote, revision, username)
                base = revision
                manifest = read_manifest(remote)
            else:
                conn.execute("DELETE FROM project_meta WHERE key=?", (META_PENDING_PUSH,))
        if manifest["revision"] != base:
            raise SyncConflict(
                f"The shared copy is at revision {manifest['revision']}, "
                f"and this copy is based on {base}.")

        # Edits made during the push set dirty back to 1, so they aren't lost.
        conn.execute("UPDATE sync_sections SET dirty=? WHERE dirty=?",
                     (DIRTY_PUSHING, DIRTY_YES))
        work = conn.execute("SELECT scope, section FROM sync_sections WHERE dirty=?",
                            (DIRTY_PUSHING,)).fetchall()
    finally:
        conn.close()

    remote_conn = _connect(remote, shared=True)
    report: list = []
    new_revision = base + 1
    push_id = uuid.uuid4().hex
    committed = False
    try:
        # An attached file would otherwise inherit the share's no-lock access.
        remote_conn.execute("ATTACH DATABASE ? AS src", (sqlite_uri(local, vfs=LOCAL_VFS),))
        if not work and not _elf_differs(remote_conn):
            return {"revision": base, "sections": [], "rows": 0, "bytes": 0}
        local_conn = _connect(local)
        try:
            _set_meta(local_conn, META_PENDING_PUSH, push_id)
        finally:
            local_conn.close()
        _begin_write(remote, manifest, username)

        names = {**_names(remote_conn, "main"), **_names(remote_conn, "src")}
        # Deferred, so only the shared copy is written and the app isn't locked out.
        remote_conn.execute("BEGIN")
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            host = socket.gethostname()
            if _elf_differs(remote_conn):
                rows, sent = _sync_elf(remote_conn)
                work_rows = [("project", sections.ELF_SECTION, rows, sent)]
            else:
                work_rows = []
            for scope, section_name in work:
                section = sections.section_for(scope, section_name)
                scope_id = sections.parse_scope(scope)[1]
                rows, sent = _sync_section(remote_conn, section, scope_id)
                work_rows.append((scope, section_name, rows, sent))
            for scope, section_name, rows, sent in work_rows:
                kind, scope_id = sections.parse_scope(scope)
                remote_conn.execute(
                    "INSERT INTO sync_sections (scope, section, revision, dirty) "
                    "VALUES (?, ?, ?, 0) ON CONFLICT(scope, section) "
                    "DO UPDATE SET revision=excluded.revision, dirty=0",
                    (scope, section_name, new_revision))
                if rows:  # A section can be written without really changing.
                    remote_conn.execute(
                        "INSERT INTO sync_log (revision, timestamp, username, hostname, "
                        "release_id, release_name, model_id, section, action, rows_changed) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'push', ?)",
                        (new_revision, now, username, host,
                         scope_id if kind == sections.SCOPE_RELEASE else None,
                         names.get(scope_id, "") if kind == sections.SCOPE_RELEASE else "",
                         scope_id if kind == sections.SCOPE_MODEL else None,
                         section_name, rows))
                report.append({"scope": scope, "section": section_name,
                               "rows": rows, "bytes": sent})
            # Triggers on the shared copy just marked everything we wrote.
            remote_conn.execute("UPDATE sync_sections SET dirty=0")
            _set_meta(remote_conn, META_REVISION, new_revision)
            _set_meta(remote_conn, META_LAST_PUSH, push_id)
            remote_conn.execute("COMMIT")
            committed = True
        except Exception:
            if remote_conn.in_transaction:
                remote_conn.execute("ROLLBACK")
            raise
    finally:
        try:
            if not committed and read_manifest(remote).get("writing"):
                _write_manifest(remote, manifest)
        except (SyncError, AttributeError):
            pass
        remote_conn.close()

    conn = _connect(local)
    try:
        # ELF-only pushes change the shared copy's section list, not ours.
        _finish_push(conn, remote, new_revision, username)
    finally:
        conn.close()
    return {"revision": new_revision, "sections": report,
            "rows": sum(r["rows"] for r in report),
            "bytes": sum(r["bytes"] for r in report)}


def restore_dirty_flags(local: str) -> None:
    """Puts back flags left in the pushing state by a failed push."""
    conn = _connect(local)
    try:
        conn.execute("UPDATE sync_sections SET dirty=? WHERE dirty=?",
                     (DIRTY_YES, DIRTY_PUSHING))
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Pull
# ----------------------------------------------------------------------
def pull(local: str, remote: str) -> dict:
    """Brings changed sections from the shared copy, in one local transaction, so
    readers see all of a revision or none of it. Only for a copy with no local
    work: the caller keeps dirty copies as conflict copies first."""
    manifest = read_manifest(remote)
    if manifest is None:
        raise SyncError("The shared copy has no sync manifest.")
    _check_schema(manifest)

    if manifest.get("writing"):
        raise SyncError(f"{manifest['writing']} is publishing. Trying again shortly.")

    conn = _connect(local)
    report: list = []
    try:
        conn.execute("ATTACH DATABASE ? AS src", (_shared_uri(remote, mode="ro"),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            base = int(_meta(conn, META_BASE, 0))
            revision = int(_meta(conn, META_REVISION, 0, db="src"))
            if revision <= base:
                conn.execute("ROLLBACK")
                return {"revision": base, "sections": [], "rows": 0}
            ours = {(r[0], r[1]): r[2] for r in conn.execute(
                "SELECT scope, section, revision FROM main.sync_sections")}
            theirs = conn.execute(
                "SELECT scope, section, revision FROM src.sync_sections").fetchall()
            for scope, section_name, section_revision in theirs:
                if section_revision <= ours.get((scope, section_name), 0):
                    continue
                if section_name == sections.ELF_SECTION:
                    rows = _sync_elf(conn)[0]
                else:
                    section = sections.section_for(scope, section_name)
                    rows = _sync_section(conn, section, sections.parse_scope(scope)[1])[0]
                report.append({"scope": scope, "section": section_name, "rows": rows})
            conn.execute("DELETE FROM main.sync_sections")
            conn.execute("INSERT INTO main.sync_sections SELECT scope, section, revision, 0 "
                         "FROM src.sync_sections")
            _set_meta(conn, META_BASE, revision)
            _copy_sync_log(conn)
            _stable(remote, manifest)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.DatabaseError as e:
        raise SyncError(f"The pull didn't finish: {e}") from e
    finally:
        conn.close()
    return {"revision": revision, "sections": report,
            "rows": sum(r["rows"] for r in report), "by": manifest.get("username", "")}


# ----------------------------------------------------------------------
# Problems
# ----------------------------------------------------------------------
def save_conflict_copy(local: str, remote: str, username: str) -> str:
    """Keeps local work that can't be pushed, and never merges it. The copy goes
    next to the shared project, or stays local if the share refuses it."""
    stem = os.path.splitext(os.path.basename(remote))[0]
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    name = f"{stem} (conflict {username} {stamp}).arch"
    for folder in (os.path.dirname(remote), os.path.dirname(local)):
        target = os.path.join(folder, name)
        try:
            _copy_to_share(local, target)
            return target
        except (OSError, sqlite3.Error):
            continue
    raise SyncBlocked("The conflict copy couldn't be saved.")


def project_uuid_of(remote: str) -> Optional[str]:
    conn = _connect(remote, shared=True)
    try:
        return _meta(conn, "project_uuid")
    finally:
        conn.close()


def remote_is_healthy(remote: str) -> bool:
    try:
        conn = _connect(remote, shared=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        return bool(row) and row[0] == "ok"
    except sqlite3.Error:
        return False


def republish(local: str, remote: str, username: str) -> dict:
    """Replaces a damaged shared copy from the latest complete local copy."""
    damaged = f"{remote}.damaged-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    if os.path.exists(remote):
        shutil.move(remote, damaged)
    conn = _connect(local)
    try:
        base = int(_meta(conn, META_BASE, 0))
        conn.execute("DELETE FROM project_meta WHERE key=?", (META_PENDING_PUSH,))
    finally:
        conn.close()
    manifest = publish(local, remote, username)
    conn = _connect(remote, shared=True)
    try:
        revision = base + 1
        _set_meta(conn, META_REVISION, revision)
        manifest = _manifest_for(conn, revision, username)
    finally:
        conn.close()
    _write_manifest(remote, manifest)
    conn = _connect(local)
    try:
        _set_meta(conn, META_BASE, revision)
        conn.execute("UPDATE sync_sections SET dirty=0")
    finally:
        conn.close()
    return manifest
