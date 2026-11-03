"""The Sync Worker's tray icon (#16). Stays quiet unless a project needs the user."""
from __future__ import annotations

import logging
import sys
import threading
import time

from Application_Logic import Logic_Sync_Autostart as autostart
from Application_Logic.Logic_Sync_Worker import SyncStore, request_sync_now, summarize

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 5
FRAME_SECONDS = 0.2
SPIN_FRAMES = 8
# A push can take a moment, so the icon keeps turning long enough to be noticed.
MIN_SPIN_SECONDS = 2
ICON_SIZE = 64
COLOR_OK = (74, 144, 226, 255)
COLOR_ATTENTION = (230, 150, 30, 255)


def icon_image(attention: bool):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = COLOR_ATTENTION if attention else COLOR_OK
    # Two arrows chasing each other: the usual shape for sync.
    box = (10, 10, ICON_SIZE - 10, ICON_SIZE - 10)
    draw.arc(box, 200, 340, fill=color, width=7)
    draw.arc(box, 20, 160, fill=color, width=7)
    draw.polygon([(48, 4), (62, 18), (44, 22)], fill=color)
    draw.polygon([(16, 60), (2, 46), (20, 42)], fill=color)
    return image


def spin_frames(image) -> list:
    """The icon at evenly spaced angles, for turning while data moves."""
    return [image.rotate(-360 * i / SPIN_FRAMES) for i in range(SPIN_FRAMES)]


def on_main_thread(fn, *args) -> None:
    """macOS kills a process that changes its menu bar from another thread."""
    if sys.platform == "darwin":
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn, *args)
    else:
        fn(*args)


def run(store: SyncStore, stop: threading.Event, worker) -> None:
    """Runs the icon until the user quits. Blocks, so it needs the main thread."""
    import pystray

    from desktop.sync_worker import WINDOW_FLAG, command, start_detached

    def open_window(tray, *_args) -> None:
        try:
            start_detached(WINDOW_FLAG)
        except OSError as e:
            logger.exception("Could not open the Sync Worker window")
            tray.notify(f"The window couldn't open: {e}", "Sync Worker")

    def sync_now(*_args) -> None:
        request_sync_now(store)
        worker.wake()

    def toggle_login(*_args) -> None:
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable(command())

    def quit_applet(tray, *_args) -> None:
        stop.set()
        worker.wake()
        tray.stop()

    summary = summarize(store)
    menu = pystray.Menu(
        pystray.MenuItem(lambda _item: summarize(store)["title"], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Sync Worker…", open_window, default=True),
        pystray.MenuItem("Sync now", sync_now),
        pystray.MenuItem("Start at login", toggle_login,
                         checked=lambda _item: autostart.is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_applet),
    )
    tray = pystray.Icon("architecture-validator-sync", icon_image(summary["attention"]),
                        "Architecture Validator Sync", menu)

    def checked(result: str) -> None:
        # "Sync now" says what it found, even when that's nothing.
        try:
            on_main_thread(tray.notify, result, "Sync Worker")
        except Exception:  # noqa: BLE001 — a desktop without notifications
            logger.info("Sync now: %s", result)

    worker.on_checked = checked

    def show(icon) -> None:
        icon.visible = True

    def set_icon(icon, image) -> None:
        icon.icon = image

    def update(icon, current: dict) -> None:
        icon.title = current["title"]
        icon.update_menu()

    def refresh(icon) -> None:
        on_main_thread(show, icon)
        attention = summary["attention"]
        rest = icon_image(attention)
        frames = spin_frames(rest)
        frame = 0
        spin_until = next_summary = 0.0
        was_spinning = was_active = False
        while not stop.wait(FRAME_SECONDS):
            now = time.monotonic()
            active = worker.active
            if active:
                spin_until = now + MIN_SPIN_SECONDS
            spinning = now < spin_until
            if now >= next_summary or active != was_active:
                was_active = active
                next_summary = now + REFRESH_SECONDS
                current = summarize(store)
                if current["attention"] != attention:
                    attention = current["attention"]
                    rest = icon_image(attention)
                    frames = spin_frames(rest)
                    on_main_thread(set_icon, icon, rest)
                on_main_thread(update, icon, current)
            if spinning:
                frame = (frame + 1) % SPIN_FRAMES
                on_main_thread(set_icon, icon, frames[frame])
            elif was_spinning:
                on_main_thread(set_icon, icon, rest)
            was_spinning = spinning

    if sys.platform == "darwin":
        # Menu bar only: no Dock icon for a background applet.
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
    tray.run(setup=refresh)
