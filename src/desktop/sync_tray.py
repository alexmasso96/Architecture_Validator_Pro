"""The Sync Worker's tray icon (#16). Stays quiet unless a project needs the user."""
from __future__ import annotations

import sys
import threading

from Application_Logic import Logic_Sync_Autostart as autostart
from Application_Logic.Logic_Sync_Worker import SyncStore, request_sync_now, summarize

REFRESH_SECONDS = 5
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


def on_main_thread(fn, *args) -> None:
    """macOS kills a process that changes its menu bar from another thread."""
    if sys.platform == "darwin":
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn, *args)
    else:
        fn(*args)


def run(store: SyncStore, stop: threading.Event) -> None:
    """Runs the icon until the user quits. Blocks, so it needs the main thread."""
    import pystray

    from desktop.sync_worker import WINDOW_FLAG, command, start_detached

    def open_window(*_args) -> None:
        start_detached(WINDOW_FLAG)

    def sync_now(*_args) -> None:
        request_sync_now(store)

    def toggle_login(*_args) -> None:
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable(command())

    def quit_applet(tray, *_args) -> None:
        stop.set()
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

    def show(icon) -> None:
        icon.visible = True

    def update(icon, current: dict, image) -> None:
        icon.title = current["title"]
        if image is not None:
            icon.icon = image
        icon.update_menu()

    def refresh(icon) -> None:
        on_main_thread(show, icon)
        attention = summary["attention"]
        while not stop.wait(REFRESH_SECONDS):
            current = summarize(store)
            changed = current["attention"] != attention
            attention = current["attention"]
            on_main_thread(update, icon, current, icon_image(attention) if changed else None)

    if sys.platform == "darwin":
        # Menu bar only: no Dock icon for a background applet.
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
    tray.run(setup=refresh)
