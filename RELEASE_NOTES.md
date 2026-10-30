# v3.1.0 — Pipeline CLI, Import Profiles, object-file enrichment, parser & stability fixes

## ⚙️ New: Pipeline CLI
- **Run the full validation in a build pipeline** with one command, on GitLab or
  GitHub: `arch-validator run --config validation.cfg`. See the
  [Pipeline CLI guide](docs/guide/12-pipeline-cli.md).
- **Input is one generated Excel workbook**: a sheet per architecture model plus
  a main functions sheet. Paths and settings live in a `.cfg` file.
- **Clear result**: exit code 0 passed, 1 test failed, 2 setup or tool error. The
  HTML report and `.arch` are written either way when a test fails.
- **Packaged for Windows, Linux and macOS** as a console build that needs no
  Python and no desktop components.
- **A complete demo** in `Resources/VCU_TestProject/pipeline`: compiled test
  firmware, a generated workbook, a `.cfg` file and sample pipeline files.

## 🧭 Clearer call-tree results
- **Not Matched** replaces *No Match*, and also covers a matched name that isn't
  in the call tree (previously *Failed*): nothing could be traced for that port.
  Results from older runs report the same way.

## 🏷️ Generic import naming
- The architecture import now uses generic names throughout. The built-in
  profiles auto-select files named `INTERFACE_EXPORT_*.xls*`, so rename existing
  export files or pick the profile by hand.

## 🪟 Resizable Preferences
- Drag the bottom-right corner of the Preferences window to resize it. It opens
  larger, remembers its size, and Import Profile names are shown in full.

## 📥 New: Import Profiles
- **Import any table format without code.** Build a reusable profile from
  declarative rules (filter, derive if/else, split cell, expand to rows,
  partition into models, clean / fill merged cells) plus a column mapping, in
  **Preferences → Import Profiles**.
- **Live preview** against a sample file read once into memory: per-rule row
  counts, a target-shaped preview of what each table column will receive, and a
  paged *show all rows* view for cross-checking.
- **Append** new rows or **match & update** existing rows by a key (composite keys
  supported), e.g. filling one test-case ID into every function row of an interface.
- **Diff before write**: every import shows exactly what will change before you
  apply it. Profiles are auto-selected by filename pattern.
- The built-in interface export profiles reproduce the existing interface import exactly;
  the existing Import wizard is unchanged.

## 🔒 New: locked imported cells (TC. ID)
- Cells filled by an import in a locked column show 🔒, can't be edited by hand,
  and are **never overwritten by later imports**; a differing value is flagged in
  the import diff. Unlock is an explicit right-click action, recorded in history.

## 🗺️ New: object-file (`.o`) enrichment for the Code Map
- Import a build-output folder of `.o` files per release (Code Map → **Objects**).
  Call edges confirmed by the compiler's relocations are drawn solid; edges seen
  only in the source (e.g. inside a disabled `#ifdef`) are drawn dashed. Works
  without disassembly, so it also covers RH850.
- **Inlined functions** (code that runs but has no symbol) are recovered from the
  debug info and offered in the match picker tagged *inlined*.
- LTO objects are detected and skipped with a clear reason.

## ✅ New: Call-tree Test columns
- A per-release table column that checks whether each port's matched function
  is actually called: **Passed** (callers listed), **Entry point** (not called
  but calls others: OS/scheduler/startup), or **Failed** (never called, or not
  in the call tree). Bound to a chosen Match column, so projects with several
  Match columns pick what each test checks.
- Uses the release's call tree plus, when imported, the `.o` files, so calls
  from assembly start-up code count as real callers; optional *verified calls
  only* mode.
- Run manually with ▶; results are recorded with their evidence, marked ⟳
  stale after a call-tree rebuild, overridable by a tester until the release is
  baselined (the tool verdict is kept), and logged in the history.

## 🐛 Fixed (table)
- Renaming a release **Result** column no longer loses its link to the release.

## 🐛 Fixed (Renesas RH850 images)
- **Functions from Renesas CC-RH / V850 builds are no longer dropped.** These
  toolchains prefix every C symbol with `_` (`Bms_Init` → `_Bms_Init`), which
  made every function look compiler-internal: nothing was imported and all
  function matches stayed empty. The convention is now detected per file and
  one leading `_` is removed, so names match the architecture, the C source and
  the debug info. Parameters attach again, and the native parser is no longer
  mistaken for corrupted output (no slow fallback).
- **`.out`, `.abs` and `.axf` images** are offered in every file picker next to
  `.elf`. The Import wizard recognises an ELF by its content, so an image with
  an unusual or missing extension is imported as a release instead of being
  read as a spreadsheet.
- An import that finds **no functions** now says so instead of reporting success.

## 🤖 New: self-hosted AI (Ollama and vLLM)
- Point AI Generation and AI Chat at your own **Ollama** or **vLLM** server
  (Preferences → AI Settings) so source code never leaves your infrastructure.
- Models are listed live from the server; **Save & test** checks the connection.
- Ollama uses its native API so the **context size** can be set (its OpenAI-style
  endpoint silently truncates prompts to a small default).
- Connects directly by default (a LAN/local server isn't behind the corporate
  proxy); native tool calling is opt-in, with the text fallback otherwise.

## 🔍 Parser
- **Relocatable objects parse correctly** with the native parser (KI-01): ELF REL
  relocations' in-place addends are now applied. Output is sanity-checked and falls
  back to `pyelftools` if it ever looks corrupted, on every import path.
- **Backend provenance** (KI-02): the Release Manager shows **⚙ native** /
  **⚙ python** per release; `ARCH_PARSER_BACKEND=python|rust` forces a backend.
  A failed native-module load is logged with its real reason.

## 🐛 Fixed (crash audit)
- `bad parameter or other API misuse` crash from concurrent requests sharing a
  prepared statement; transactions on the shared connection are now serialized.
- Several reads of **encrypted** project data skipped decryption (Code Map crash
  with `Expecting value`, typedef structs silently missing, broken ELF cache
  export). A failed cache export no longer deletes an older good cache.
- Closing a project during a request, or a corrupt encrypted value, now returns a
  clear **409** message instead of an unexplained server error.

---

# v3.0.2 — Hotfix: Windows blank/white window

A follow-up Windows fix. On some machines the app opened to a **blank white
window** (and only worked when "Run as administrator" was used). macOS and Linux
are unaffected.

## 🐛 Fixed
- **No more blank white window on Windows.** The embedded WebView2 control stores
  its browser profile in a "user data folder"; left unset it lands next to the
  `.exe`, which fails to initialise when the app runs from a read-only or
  permission-restricted location (Program Files, a VM shared folder, a locked-down
  extract) — leaving the window white unless launched as administrator. The app
  now puts that folder in a guaranteed per-user-writable location
  (`%LOCALAPPDATA%\ArchitectureValidator\WebView2`), so it works as a normal user.

---

# v3.0.1 — Hotfix: Windows launch crash

A maintenance release that fixes the Windows desktop build failing to start. No
functional changes to the app itself — **macOS and Linux behave exactly as in
3.0.0**; if 3.0.0 already runs for you, 3.0.1 only adds the Windows fix and better
diagnostics.

## 🐛 Fixed
- **Windows app no longer crashes on launch.** The native window backend
  (pywebview → winforms → pythonnet/.NET) failed to initialise on several
  machines — symptoms ranged from a silent exit, to a blank white window, to
  `Failed to resolve Python.Runtime.Loader.Initialize`. The build now ships
  pythonnet and its `clr_loader` .NET bootstrap shims with a pinned, matched
  version so the runtime loads correctly.
- **Crashes are no longer silent.** If the app fails to start, a full traceback is
  now written to `%LOCALAPPDATA%\ArchitectureValidator\crash.log`.
- **Missing-prerequisite check.** On Windows the app now verifies the WebView2
  Runtime and .NET Framework are present at launch, and — if either is missing —
  shows a dialog linking straight to Microsoft's download page instead of
  failing cryptically.

## 📦 Linux packaging
- Linux now ships proper distribution packages — **`.deb`, `.rpm`, and
  `.flatpak`** — instead of a portable `.tar.gz` archive.

## ℹ️ Windows prerequisites
The native window needs two Microsoft runtimes on the target machine:
- **Microsoft Edge WebView2 Evergreen Runtime** (match the CPU architecture — a
  missing/wrong-arch runtime shows a blank white window; install the ARM64
  runtime on Windows-on-ARM devices).
- **.NET Framework 4.7.2 or newer.**

---

# v3.0.0 — Desktop Rewrite: React + pywebview, Test Injection & Single-File Projects

The biggest release yet: the entire desktop app has been **rewritten**. The PyQt6
UI is gone, replaced by a **React single-page app running in a native pywebview
window** over a **local FastAPI worker**. The Qt-free Python logic core and the
native Rust ELF parser carry straight over, so every validation, release, AI, and
code-map capability is intact — now behind a faster, more responsive interface
that physically cannot freeze (the heavy work runs in a separate process and
streams progress over Server-Sent Events).

## 🏗 New architecture (no behaviour you relied on was dropped)
- **React SPA + pywebview shell + FastAPI worker.** The UI talks to the worker
  over `127.0.0.1` with a per-session bearer token; the worker owns all project
  state and drives the existing `Application_Logic` layer directly.
- **Every heavy operation is a cancellable background job** with uniform progress,
  cancellation, and error reporting streamed over a single `/api/events` SSE.
- **View-only is enforced server-side** (`PRAGMA query_only=ON`) — read-only
  sessions physically cannot write, not just a greyed-out button.

## 💉 New: Source-level Test Injection
A new view to **splice test code into production C source without editing the
originals**. Hooks anchor to the *text* of the surrounding lines (not brittle line
numbers), so they re-find their spot when upstream source shifts. Import helper
`.c/.h` files alongside, then export build-ready code — *Modified files only* or a
full *Reconstructed* tree — leaving your originals untouched.

## 🎓 New: Interactive in-app tutorials
Every view now has a **click-through interactive walkthrough** on a simulated
screen (nothing in your real projects is touched). Open **Preferences → Tutorials**
for Workspace, Code Map, Change Log, Test Design, AI Generation, AI Chat, and Test
Injection.

## 💾 Single-file projects + per-block encryption
- A project is now **one portable `.arch` SQLite file** — no per-project folder
  required.
- Encryption moved from whole-file to **per-block (per-category) content
  encryption**: the `.arch` is a plaintext SQLite container and only sensitive
  content columns are encrypted, each under its own key. Open and save are fast
  (no decrypt-to-temp / re-encrypt-the-whole-file). **Legacy whole-file
  (`ARCHENC1`) projects auto-migrate on first open.**

## ⚙️ Packaging
- Packaged as a **PyInstaller onedir desktop bundle** (Windows/macOS/Linux) from
  `ArchitectureValidatorDesktop.spec`, which builds the React SPA and the Rust
  parser wheel first. CI gained the Node.js setup + frontend build steps.

> **Upgrade note:** opening a project from v2.x re-stamps it into the v3 storage
> format on first open. Keep a backup of important projects before upgrading.
