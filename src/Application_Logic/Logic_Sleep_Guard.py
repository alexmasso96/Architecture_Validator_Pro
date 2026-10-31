"""Keeps the machine awake during an overnight build run (#15)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def prevent_sleep():
    """Best effort: a machine that can't be held awake still runs the builds."""
    helper = None
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        elif sys.platform == "darwin":
            helper = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        else:
            helper = subprocess.Popen(
                ["systemd-inhibit", "--what=sleep", "--why=Overnight test builds",
                 "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, AttributeError):
        logger.warning("Could not prevent sleep for the build run", exc_info=True)
    try:
        yield
    finally:
        if helper is not None:
            helper.terminate()
        elif sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            except (OSError, AttributeError):
                pass
