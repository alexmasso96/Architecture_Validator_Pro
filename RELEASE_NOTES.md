# v3.2.1 — Sync Worker fixes for Windows, clearer progress, update check

## 🪟 Sync Worker window
- **The window opens from the tray on Windows.** It now does the same Windows start-up as the app and has its own WebView2 profile.
- **Opening it twice brings the open window forward,** and a window that crashed can't stop the next one from opening.
- **The worker and its window keep log files,** `sync-worker.log` and `sync-window.log` in the app's `sync` data folder, so a failure can be traced.

## 🔄 Clearer progress for shared projects
- **Adding a project shows every step,** and if it fails, you see why. Before, some failures showed nothing at all.
- **Sharing the open project keeps you in it.** A progress card shows each step, the app no longer drops back to the first window, and a failed share leaves your session as it was.
- **Opening a shared project for the first time shows its progress,** including on the start screen.
- **The tray icon turns while data is moving,** and so does the sync icon in the status bar.
- **Setting up a shared project is lighter on the share,** with one write where there used to be about a hundred.

## 🔔 Update check
- **The pill can't stay on Checking.** The check uses the repository's current address, has a time limit, and tries once more if it fails.
- **If the check fails, hover over the pill to see why.**

# v3.2.0 — Test Builds, shared projects on a NAS, Sync Worker

## 🔨 New: Test Builds
- **Named builds per release**, such as `Watchdog build`, set up under Test Injection → **Test Builds**. Each has its own build and clean commands, and you choose which files, folders and patterns to keep.
- **Overnight runs.** Builds run one after another, unattended. Main is built first if the release has no clean capture, a failed build doesn't stop the rest, and your source folder is put back after every build, so hooks can't reach the next one. An interrupted run can be resumed, and the machine stays awake until the run ends.
- **Switch the mapped release.** Pick a build, and the folder your debugger points at gets it. Only files that differ are transferred, files changed outside the app are never overwritten without asking, and an interrupted switch can be finished or undone. Flash from your debugger as usual.
- **Clean-release lock.** Before code changes, the app checks that the release's clean source and `.o` files are imported. You can continue anyway, which is recorded under your name, and importing later is never blocked.
- **Binaries stay on the share**, next to the project in `Project.arch.binaries/`, with each identical file stored once. Copying a project elsewhere needs that folder too.
- Test builds are for real ECU testing only. They're never compared with Main, and they never change a release's symbols, Code Map or results.

## 🔄 New: shared projects and the Sync Worker
- **Share a project on a NAS.** You work on a local copy, and only the parts that changed travel. One user edits at a time, and everyone else can keep viewing.
- **Sync Worker.** A small tray applet that keeps shared projects current in the background, even with the app closed. Turn on **Start at login** in its menu.
- **Changes are tracked in the Sync Worker,** for example `alex → Release 2.1 → Code Map`, so they don't clutter the app or the change history.
- **Safe when things go wrong.** Work that can't be published is kept as a conflict copy and never merged. A share that drops out leaves your session open, and encrypted projects sync without the master password reaching the worker.

## 🔒 Edit lock
- **A wrong clock on another PC can no longer free a lock that's in use.**
- **A heartbeat can no longer leave a half-written lock,** which another session could have treated as free.
- **A lock left behind by a crash is free after 10 minutes of silence.** With the Sync Worker running, that needs no waiting in the app.
