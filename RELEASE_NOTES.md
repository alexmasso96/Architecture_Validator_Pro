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

# v3.1.1 — Parameter port tests, compiler-verified variable use, no size limits

## 🔌 New: parameter port tests
- **Test ports that are a parameter, not a function.** Put `required` or
  `provided` in a Variable Search column's title and bind a Test column to its
  Match column. Required values must come into the function, as a parameter or a
  read; provided values must go out, returned, written or through an output
  pointer. Helpers the function calls count too.
- **Separate match threshold** for parameters (70%), because generated names rarely
  match exactly. In the CLI: `parameter_columns` and `min_parameter_score`.
- **Object files show variable use.** A variable hidden behind a macro or used from
  assembly now passes when the compiler's relocations show the function references
  it, marked *direction not confirmed* because a relocation can't tell a read from
  a write. Parameter tests also run with object files and no source. Import object
  files again to get this.

## 📏 No more size limits in the source analysis
- **Functions over 5000 lines are no longer dropped.** They disappeared from the
  Code Map with all their calls, which matters for generated step functions.
- **No limit on the variables per function.** The Code Map also records `++`/`--`
  and plain global writes as writes, and sees `return g_value;`.
- Rebuild the Code Map to get this.

## 🐛 Fixed (ARM Thumb objects)
- **A call in the first instruction of a Thumb function** was credited to the
  function before it, so Cortex-M object files could verify the wrong call.
