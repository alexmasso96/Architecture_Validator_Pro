# 3. Importing Architecture

[← The Workspace](02-validation-workspace.md) · **Importing Architecture** · [Next: Releases & Baselines →](04-releases-and-baselines.md)

---

Rather than type ports in by hand, you can import them from a spreadsheet or an architecture-tool export. The importer auto-detects the format and routes it to the right flow.

Open the **Import** toolbar icon in the title bar and pick your file. The import wizard analyses it, shows you a preview, and creates the models and ports. For any other table format, use an [Import Profile](#import-profiles-any-other-table-format) via the **⇫ Profile** icon.

## Supported formats

### Firmware images (symbols)
A linked firmware image is an ELF file, whatever its extension: `.elf`, `.out`, `.abs` (Renesas CC-RH) or `.axf` (Arm/Keil) are all offered in the file pickers, and the Import wizard recognises an ELF by its content, so an image with an unusual or missing extension is imported as a new release too. A `.json` symbol cache exported by the app works the same way.

Some toolchains, notably **Renesas CC-RH / V850 for RH850**, put an underscore in front of every C symbol (`Bms_Init` is stored as `_Bms_Init`). The app detects this per file and removes the one leading underscore, so symbols match your architecture names and the C source; the import result mentions it when it happens. If an import finds no functions at all (for example a stripped image without a symbol table), the result says so.

### Excel / CSV
Standard spreadsheets are supported (`.xlsx`, `.xls`, `.csv`). In the classic **sheet-per-model** layout, each worksheet becomes its own architecture model and its rows become ports — handy when you maintain your architecture in Excel.

### InterfaceExport path-based exports
Exports from IBM InterfaceExport, where the architecture is encoded as hierarchical paths, are **detected automatically**. The tool reads the path structure, works out which model each port belongs to, and pulls out the operations associated with each interface. The wizard then lets you:

- **Preview** the models and ports that will be created before committing.
- **Choose which models** to import.
- **Map the relevant columns** (such as the required-interface and operations columns) when they can't be inferred.

A large InterfaceExport export becomes a populated, model-organised project in a couple of clicks instead of a manual re-keying exercise. Because a InterfaceExport export embeds many packages in one path column, the split into per-model row sets happens server-side in a single step.

## Import Profiles (any other table format)

For exports the built-in flows don't know — a DOORS Next Gen test-case sheet, a supplier's interface list, a differently shaped InterfaceExport export — create an **Import Profile** once and reuse it. A profile is a list of simple rules plus a column mapping. There is no scripting: every rule is a small form.

Open **Preferences → Import Profiles**. Built-in profiles (the InterfaceExport flows) are read-only; **Duplicate** one to customize it, or click **+ New**.

### Rules

Rules run top to bottom; each card shows how many rows went in and came out.

| Rule | What it does | Example |
|---|---|---|
| **filter** | Keep or drop rows matching all/any conditions (`contains`, `equals`, `starts with`, `ends with`, `regex`, `is empty`, …) | keep rows whose path contains `P10_SW_Arch_Public` |
| **derive** | Create a column from if / else-if / else conditions | *if* `Required Interface` is not empty → `with TC`, *else* → `without TC` |
| **split cell** | Split a cell by a delimiter and keep one segment (negative = from the end) | path `A::B::Motor::…` split by `::`, segment 2 → `Motor` |
| **expand** | One row per part of a multi-value cell | an Operations cell with one operation per line |
| **partition** | One model per distinct value of a column | split the export into one model per component |
| **clean** | Trim, change case, find/replace, or **fill down** (merged Excel cells) | fill a merged group column downward |

### Live preview

Click **Load sample…** and pick an example of the file this profile is for. The file is **read once into memory**; after that, every preview refresh is computed in memory with no further file access, so the preview can stay live even on machines with strict endpoint security. (Untick **live** and use **Refresh** if you prefer.)

- The preview shows the first ~10 rows **in the shape of your table**: the actual target columns and what each will receive. Columns marked 🔒 will be locked on import.
- **Show all filtered rows…** opens the complete filtered result (paged) so you can cross-check it against what you know the export should contain.
- Large files are sampled up to their first 1,000 rows; the counters say so when that happens.

### Mapping and write mode

Map each source column to a table column (or skip it). Tick **🔒 lock imported cells** for columns the tool should own, such as **TC. ID**.

- **Append new rows** — imported rows become new ports (split into models by a *partition* rule, if present).
- **Match & update existing rows** — imported rows are matched to existing rows by one or more **key columns** (e.g. *Interface*), and the mapped values are written into **every** matching row. This is how one test-case ID per interface is filled into all of that interface's function rows.

Give the profile a **filename pattern** (e.g. `DNG_*.csv`) and it is selected automatically when you import a matching file.

### Running an import

Click the **⇫ Profile** icon in the title bar, pick the file, and confirm the profile (pre-selected when the filename matches). **Preview changes** computes the full diff **without writing anything**:

- *Append*: the models that will receive rows, and which of them are new.
- *Update*: how many cells and rows will change (with an old → new table), table rows the import doesn't touch, and imported keys that match nothing.
- ⚠ **Locked cells** whose imported value differs are listed but **never changed** — under ASPICE an issued ID is stable, so a difference is something to investigate, not overwrite.
- ⚠ **Duplicate keys** carrying different values in the import are reported (the first occurrence is used).

**Apply import** writes the changes and records them in the history.

### Locked cells

Imported cells in locked columns show a 🔒 in the Workspace and can't be edited by hand. If one genuinely needs to change, right-click it → **Unlock imported cell…**. The value stays, the lock is removed, and the unlock is recorded in the history.

## After importing

Imported ports land as rows in the [Workspace](02-validation-workspace.md). From there the fuzzy matcher proposes symbol mappings against the loaded ELF release, and you review them as normal.

> **Tip:** Import is additive and model-aware — re-importing an updated export tops up the right models rather than flattening everything into one.

---

[← The Workspace](02-validation-workspace.md) · [Guide home](README.md) · [Next: Releases & Baselines →](04-releases-and-baselines.md)
