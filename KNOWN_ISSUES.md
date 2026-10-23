# Known Issues

Known issues with enough detail to be picked up cold.

> **Status (2026-10-23): no open issues.** KI-01, KI-02 and KI-03 are fixed in the code base; the
> entries are kept below for their technical background. The only outstanding item is KI-01's
> validation against real linked TriCore/RH850 images on the work machine, tracked under the
> Windows on-machine validation issue (#4).

> **Scheduling lives in the team's internal GitLab tracker**, not here (sort by
> **Label priority** for execution order). This file keeps the technical detail;
> the issues track the work.

> The v2.x PyQt6 UI-layer issues at the bottom were superseded by the v3.0.0 rewrite.

---

## KI-01 — Native ELF parser corrupts DWARF on *relocatable* (`ET_REL`) objects — **FIXED 2026-08-16**
**Severity:** Low (no impact on supported targets) · **Owner:** this repo — tracked as
GitLab issue #1

> **FIXED in the working tree (2026-08-16).** `load_and_relocate_section` now reads the in-place
> bytes as the addend when `reloc.has_implicit_addend()` (sign-extended for 4-byte fields) and
> handles PC-relative (`RelocationKind::Relative`) relocations as `S + A - P`; the parser also
> emits the first CU's `DW_AT_producer` as a `producer` JSON field, and
> `ELFParser._native_result_is_sane` discards output matching the corruption signature (all struct
> names == producer, or symbols present but no function has parameters) and falls back to
> pyelftools — on **both** import paths (in-memory `extract_all` and the production DB-streaming
> `extract_all_streaming_to_db`), and it detects stale pre-fix native modules (no `producer` field)
> structurally, since a name collapsed to a compiler banner is never a valid C identifier.
> Regression tests: `Tests/test_native_et_rel.py` (7 tests — native correctness on the
> synthetic `.o`, native/pyelftools backend agreement, sanity-check units incl. the legacy-module
> shape, fallback integration on both paths).
> Verified: both backends produce identical structs/params/globals on the synthetic ET_REL fixture.
> **Still pending: final validation against real linked TriCore/RH850 images on the work machine
> (expected no-op — linked images carry no `.rel.debug_*` sections).**

Original problem, kept for reference:
`src/native/parser_rust/src/lib.rs` → `load_and_relocate_section` ignored the **in-place addend** of
ELF **REL** relocations (it applied `symbol_value + 0`). On a *relocatable object* this zeroed every
DWARF `DW_FORM_strp`/type offset → all struct/global names collapsed to the DWARF producer string and
function parameters were dropped. `ELFParser._try_native_extract` only fell back to pyelftools
**on an exception**, never on corrupted-but-valid output, so the app silently trusted the garbage.

**Why it is low impact:** it only triggers on relocatable objects (`ET_REL`). The supported targets
— **Aurix TriCore `.elf` and Renesas RH850 `.out`** — are *linked executables* with no
`.rel.debug_*` sections, so `load_and_relocate_section` is a pass-through and DWARF parses correctly
(confirmed: the linked `MB_CHLC_APP.elf` parses fine natively).

> **No longer blocked on the work machine (re-assessed 2026-07, re-verified 2026-08-16).**
> This was previously deferred to the enterprise Copilot because pointing a commercial AI at real
> ECU binaries is not permitted. That blocker **does not apply**: the bug reproduces with a
> synthetic fixture built from repo-owned source, so the fix *and* its regression tests can be
> developed here. Only final validation against real linked images needs the work machine — and per
> the analysis above, linked images do not exercise the buggy path at all.
>
> ```bash
> clang -c --target=armv7-none-eabi -g -O0 Tests/Resources/sample_elf_source.c -o /tmp/sample_et_rel.o
> ```
>
> Running `rust_elf_parser.parse_elf()` on the result shows the documented corruption: every struct
> name collapses to the producer string, `Point`'s fields vanish, and `add`'s parameters are dropped.

**Fix guidance:**
- In `load_and_relocate_section`, for REL relocations (implicit addend) read the existing 4/8 bytes
  at the offset and use `value = symbol_value + in_place_addend`; keep RELA (explicit addend) working.
  A correct, tested reference algorithm is `Resources/AI_Demo_WindowLift/patch_debug_relocs.py`
  (also copied in `Resources/VCU_TestProject/`).
- Add an **output sanity check** in `_map_native_json` / `_try_native_extract`: if every struct name
  equals the DWARF producer string (or no function has params while symbols exist), discard the
  native result and fall back to pyelftools.
- **Regression tests** ship with this fix (`Tests/test_aspice_dataset.py` notes they land together):
  compile the synthetic `.o` above and assert correct structs/params/globals plus backend agreement.
- **Validate against a real linked TriCore `.elf` and RH850 `.out`** (both backends) on the work
  machine — the final "supported?" gate, expected to be a no-op.

**Supporting additional (non-TriCore/RH850) ECUs later** — mostly validation, small code:
symbol/DWARF extraction via `object`+`gimli` is architecture-neutral; the hard-coded
little-endian (`lib.rs:~350 RunTimeEndian::Little`) is fine for TriCore/RH850 but a **big-endian**
target needs `elf_file.is_little_endian()` detection; the Capstone disassembly fallback supports
TriCore but **not** RH850/V850 (low impact — the call graph comes from the C indexer, not disasm).

## KI-02 — No parser-backend provenance / override (IMP-8) — **FIXED 2026-08-16**
**Severity:** Low · **Owner:** with KI-01 — tracked as
GitLab issue #5

> **FIXED in the working tree (2026-08-16).** `ARCH_PARSER_BACKEND=python|rust` is honoured in
> `ELFParser.load_elf` (forcing python skips the native module entirely, including hashing);
> `elf_index.parser_backend` is exposed per release by `GET /api/releases` and `/lineage`
> (`db.get_parser_backend`), and the Release Manager shows a `⚙ native` / `⚙ python` badge with an
> explanatory tooltip. Tests: `Tests/test_parser_provenance.py` (6 tests). BUG-H's WARNING on a
> failed native import remains the log-level companion.

**Plumbing that already exists and is unused:** `elf_index.parser_backend`
(`Logic_Database.py:643-645`) is written by `register_elf` (`:1654-1660`) and returned by
`get_elf_stats` (`:1842-1863`); the only reader is `ELFParser.get_statistics`, called from tests
alone. No API route exposes it and the frontend has no badge. `ARCH_PARSER_BACKEND` currently
appears **only in documentation** — there is no implementation in any source file.

## KI-03 — `TC. ID` column is user-editable (NC-2) — **FIXED 2026-08-16**
**Severity:** Medium · tracked as
GitLab issue #7

> **FIXED.** No longer waiting on a dedicated DOORS Next Gen importer: the general **Import
> Profiles** engine (#8) reads a DNG export like any other table. Its *match & update* mode writes
> the test-case ID into every row matching a key (e.g. the interface), and columns marked 🔒 store
> the imported cells with `"origin": "import"`. Such cells are **locked**: `PATCH ports/{row}`
> refuses them (409), later imports never overwrite them (a differing value is flagged in the
> pre-import diff instead — issued ASPICE IDs are stable), and `POST ports/{row}/unlock` is the
> explicit, change-logged way to make one editable again. Lock is per cell (set by the import),
> so hand-entered IDs stay editable during the transition. Tests: `Tests/test_table_import.py`.

Original problem: the test-case identifier `TC. ID` was editable like any other free-text column,
although it is meant to be populated from DOORS Next Gen exports.

---

## Resolved in / superseded by v3.0.0

A batch of v2.x issues were tied to the **PyQt6 UI layer**, which was removed in the v3 rewrite. They
no longer apply to the React/pywebview client and are listed here only for history:

- **Architecture table lost columns for a schema-less model** (Inc-04) — the React Workspace derives
  its column set from the project-global `column_layout`, so a model with no per-model schema renders
  the full set.
- **New-project ELF flow did not return to the table** (Inc-03) — project creation now completes as a
  background job and lands directly on the Workspace.
- **Match columns accepted unvalidated free text** (NC-1) — the match picker re-queries the real
  symbol pool live; a value that is not a real symbol is flagged rather than shown as a valid match.
- **`StyledMessageBox` rendered empty on macOS** (KI-07) — the Qt styled-dialog is gone; the SPA uses
  HTML modals.
