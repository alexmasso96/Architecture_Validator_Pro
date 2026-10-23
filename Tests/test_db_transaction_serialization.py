"""
Shared-connection transaction serialization (_TxnSerializedConnection).

The main ProjectDatabase connection is shared by every FastAPI threadpool
thread. `cached_statements=0` fixed the SQLITE_MISUSE crash class; this covers
the remaining interleaving hazard: without the transaction lock, thread A's
`with conn:` commit/rollback also commits/rolls back thread B's half-finished
writes. The proxy must make each `with` block atomic across threads while
keeping same-thread nesting and plain reads untouched.
"""
import os
import sys
import threading
import time
import tempfile

sys.path.append(os.path.abspath("src"))

from Application_Logic.Logic_Database import ProjectDatabase


def _open_db(tmp: str) -> ProjectDatabase:
    db = ProjectDatabase()
    db.open(os.path.join(tmp, "p.arch"))
    return db


def test_concurrent_with_blocks_do_not_interleave():
    with tempfile.TemporaryDirectory() as tmp:
        db = _open_db(tmp)
        order = []
        a_in_txn = threading.Event()

        def writer_a():
            # Holds the transaction open, then aborts → its insert must roll
            # back WITHOUT taking writer B's committed insert with it.
            try:
                with db._conn:
                    db.execute(
                        "INSERT INTO project_meta (key, value) VALUES ('a', '1')")
                    order.append("a-begin")
                    a_in_txn.set()
                    time.sleep(0.3)
                    order.append("a-end")
                    raise ValueError("abort a")
            except ValueError:
                pass

        def writer_b():
            a_in_txn.wait(5)
            with db._conn:
                order.append("b-begin")
                db.execute(
                    "INSERT INTO project_meta (key, value) VALUES ('b', '2')")

        ta = threading.Thread(target=writer_a)
        tb = threading.Thread(target=writer_b)
        ta.start(); tb.start()
        ta.join(5); tb.join(5)

        # B's transaction must have waited for A's block to finish.
        assert order == ["a-begin", "a-end", "b-begin"]
        # A rolled back alone; B committed alone.
        assert db.get_meta("a") is None
        assert db.get_meta("b") == "2"
        db.close()


def test_bare_commit_waits_for_open_transaction():
    # A trailing bare commit() (the routers' write pattern) must not land in
    # the middle of another thread's `with` transaction: before commit()/
    # rollback()/close() took the lock, it committed the other thread's
    # half-finished write, which then survived that thread's abort.
    with tempfile.TemporaryDirectory() as tmp:
        db = _open_db(tmp)
        a_in_txn = threading.Event()
        b_done = threading.Event()

        def writer_a():
            try:
                with db._conn:
                    db.execute(
                        "INSERT INTO project_meta (key, value) VALUES ('a', '1')")
                    a_in_txn.set()
                    # Give B time to call commit(); it must block on the lock.
                    time.sleep(0.3)
                    raise ValueError("abort a")
            except ValueError:
                pass

        def committer_b():
            a_in_txn.wait(5)
            db._conn.commit()   # must wait for A's block to finish
            b_done.set()

        ta = threading.Thread(target=writer_a)
        tb = threading.Thread(target=committer_b)
        ta.start(); tb.start()
        ta.join(5); tb.join(5)

        assert b_done.is_set(), "bare commit deadlocked behind the txn lock"
        # A aborted → its insert must be gone, not committed under it by B.
        assert db.get_meta("a") is None
        db.close()


def test_same_thread_nesting_still_works():
    # RLock: the pre-existing same-thread nested `with` pattern must not
    # deadlock (sqlite3 semantics of nested context managers are unchanged).
    with tempfile.TemporaryDirectory() as tmp:
        db = _open_db(tmp)
        with db._conn:
            db.execute("INSERT INTO project_meta (key, value) VALUES ('x', '1')")
            with db._conn:
                db.execute("INSERT INTO project_meta (key, value) VALUES ('y', '2')")
        assert db.get_meta("x") == "1" and db.get_meta("y") == "2"
        db.close()


def test_plain_reads_do_not_need_the_lock():
    # A reader thread must not block behind an open transaction on the same
    # connection (reads see the connection's own uncommitted state, as before).
    with tempfile.TemporaryDirectory() as tmp:
        db = _open_db(tmp)
        in_txn = threading.Event()
        release = threading.Event()
        read_done = threading.Event()

        def writer():
            with db._conn:
                db.execute("INSERT INTO project_meta (key, value) VALUES ('k', 'v')")
                in_txn.set()
                release.wait(5)

        def reader():
            in_txn.wait(5)
            db.execute("SELECT value FROM project_meta WHERE key='k'").fetchone()
            read_done.set()

        tw = threading.Thread(target=writer)
        tr = threading.Thread(target=reader)
        tw.start(); tr.start()
        assert read_done.wait(2), "plain read blocked behind an open transaction"
        release.set()
        tw.join(5); tr.join(5)
        db.close()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
