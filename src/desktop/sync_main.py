"""
The Sync Worker's own program (#16): ``ArchitectureValidatorSync`` in a frozen
build, next to the app's executable.

It loads only what syncing needs, so it doesn't carry the app's web server,
parsers and window in memory all day, and Task Manager shows it under its own
name. With ``--sync-window`` it's the worker's window instead, and with
``--detach`` it starts itself again without that flag and exits, so the copy that
stays isn't the child of whoever started it.

The app's executable still takes ``--sync-worker`` and ``--sync-window``, because
a login item made before the split names it. It hands them on to this program.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from desktop import sync_worker  # noqa: E402


def main(argv: list | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    flags = [a for a in argv if a != sync_worker.DETACH_FLAG]
    if len(flags) != len(argv) or _started_as_the_app():
        sync_worker.start_detached(*flags)
        return
    if sync_worker.WINDOW_FLAG in flags:
        from desktop.main import _open_sync_window
        _open_sync_window()
    else:
        sync_worker.main()


def _started_as_the_app() -> bool:
    """Whether a frozen build started the app's executable with the worker's flags,
    when the worker has a program of its own."""
    return (getattr(sys, "frozen", False)
            and Path(sync_worker.program()[0]) != Path(sys.executable))


if __name__ == "__main__":
    main()
