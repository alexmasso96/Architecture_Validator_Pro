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
