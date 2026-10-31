"""
The app's side of sync (#16): follows the Sync Worker's files while a tracked
project is open, so the session stops reads during a pull and the UI stays current.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from Application_Logic.Logic_Sync_Worker import (
    HOLD_ACK, HOLD_FILE, STATE_BLOCKED, STATE_CONFLICT, SyncStore, read_json, remove_file,
    write_json,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.3
# Reads never stay stopped for long, even if the worker dies mid-pull. A pull is
# one transaction, so a reader only ever sees all of a revision or none of it.
HOLD_LIMIT_SECONDS = 120


class SyncBridge:
    def __init__(self, state, store: SyncStore, entry: dict) -> None:
        self.state = state
        self.store = store
        self.entry = entry
        self._stop = threading.Event()
        self._last_status: dict = {}
        self._hold_started = 0.0
        self._thread = threading.Thread(target=self._loop, name="sync-bridge", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.step()
            except Exception:  # noqa: BLE001 — try again next tick
                logger.warning("Sync bridge step failed", exc_info=True)

    def step(self) -> None:
        hold_path = self.store.control(self.entry, HOLD_FILE)
        hold = read_json(hold_path)
        if (hold is not None and self.state.sync_hold is not None
                and time.monotonic() - self._hold_started > HOLD_LIMIT_SECONDS):
            remove_file(hold_path)
            hold = None
        if hold is not None and self.state.sync_hold is None:
            self.state.sync_hold = hold.get("by", "")
            self._hold_started = time.monotonic()
            write_json(self.store.control(self.entry, HOLD_ACK), True)
            self.state.bus.publish("sync", {"state": "pulling", "by": self.state.sync_hold})
        elif hold is None and self.state.sync_hold is not None:
            self.state.reload_after_pull()
            self.state.sync_hold = None
            self.state.bus.publish("sync", {"state": "synced"})
        status = self.store.status(self.entry)
        if status != self._last_status:
            self._last_status = status
            self.state.bus.publish("sync", status)
            if (status.get("state") in (STATE_CONFLICT, STATE_BLOCKED)
                    and self.state.can_edit and not self.state.lock_lost):
                self.state.on_sync_stopped(status)
