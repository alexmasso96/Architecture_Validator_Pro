# Pipeline CLI

Run the architecture validation automatically, for example after a nightly firmware build, with no window and no manual steps. One command creates a fresh project, imports the build's outputs, tests every architecture model, and returns a pass or fail result to the pipeline.

```bash
arch-validator run --config validation.cfg
```

---

## What a run does

1. Reads the architecture workbook and checks the setup.
2. Creates a new project and one release.
3. Imports the symbol files (`.elf`, `.out`, `.abs`, `.axf`).
4. Imports the source and object files.
5. Builds the code map.
6. Creates the architecture models and matches their ports to symbols.
7. Tests every model from its main functions.
8. Writes `<name>.html` and `<name>.arch` to the output folder.

The run always finishes every model, so one bad port doesn't hide the rest of the result.

## Exit codes

A pipeline only looks at the exit code, so each result has its own:

| Code | Meaning | What to do |
|---|---|---|
| `0` | Passed | Nothing |
| `1` | Architecture test failed | Open the HTML report |
| `2` | Setup or tool error, no valid result | Read the `ERROR:` line in the job log |

## The architecture workbook

The pipeline generates one `.xlsx` file, usually from an architecture interface export:

- **Each sheet is one architecture model.** The sheet name is the model name, and the first row holds the column titles.
- **One sheet lists the main functions**, by default called `Main Functions`. Its first column is the model name and its second column is a main function. Repeat a model on another row to add another main function, up to 10 per model.

| Model | Main Function |
|---|---|
| BodyControl | Bcm_MainTask |
| BodyControl | Bcm_InitAll |
| Powertrain | Pwt_MainTask |

To test parameter ports as well, add columns with `required` or `provided` in the title and list them under `parameter_columns`. See [Parameter ports](#parameter-ports).

The rules are strict because nobody reviews the file during a run:

- A main functions row that names no sheet stops the run with exit code 2. Excel limits sheet names to 31 characters, so check long model names.
- A model with no main function row keeps the run going. Its ports report `No Data`, which fails the run.
- A sheet needs the column named by `function_column`.
- Columns titled `TC. ID` and `Port State` fill those table columns. Setting a port's state to `Retired` or `Deleted` leaves it out of the result.
- Any other extra column appears in the project as plain text.

## The `.cfg` file

```ini
[project]
name = my-ecu
release = ${CI_COMMIT_SHORT_SHA}

[inputs]
workbook = architecture/interfaces.xlsx
main_sheet = Main Functions
symbols = APP=${BUILD_DIR}/app.out
          FBL=${BUILD_DIR}/fbl.out
source = src
         ${BUILD_DIR}/generated
source_exclude = src/test_stubs
objects = ${BUILD_DIR}/obj
object_pattern = **/*.obj

[columns]
function_column = Function
port_column = Port Name
variable_column = Parameter
parameter_columns = Required Parameter
    Provided Parameter

[test]
min_match_score = 90
min_parameter_score = 70
max_unmatched = 0
strict = false

[security]
encryption = env
password_env = ARCH_PASSWORD

[output]
folder = results
```

Relative paths start from the `.cfg` file's folder. `${NAME}` reads an environment variable, and an unset variable stops the run. Lists go on separate, indented lines, and comments need their own line.

| Setting | Default | Meaning |
|---|---|---|
| `project.name` | `.cfg` file name | Name of the output files |
| `project.release` | `pipeline` | Release name in the project and report |
| `inputs.workbook` | Required | The architecture workbook |
| `inputs.main_sheet` | `Main Functions` | Sheet holding the main functions |
| `inputs.symbols` | Required | One `COMPONENT=path` per line, or a single path |
| `inputs.source` | None | Folders or files to import |
| `inputs.source_exclude` | None | Folders or files to leave out |
| `inputs.objects` | None | Folder holding the object files |
| `inputs.object_pattern` | `**/*.o` | Which object files to import |
| `columns.function_column` | `Mapped Func` | Workbook column holding the function to test |
| `columns.port_column` | `Input Port` | Workbook column holding the port name |
| `columns.variable_column` | `Mapped Parameter` | Workbook column holding the variable name |
| `columns.parameter_columns` | None | Workbook columns holding parameter ports, one per line; each title needs `required` or `provided` |
| `test.min_match_score` | `90` | Lowest score that counts as a function match |
| `test.min_parameter_score` | `70` | Lowest score that counts as a parameter match |
| `test.max_unmatched` | `0` | How many `Not Matched` ports the run may have and still pass; `-1` means no limit |
| `test.strict` | `false` | Count compiler-verified calls only (needs object files) |
| `security.encryption` | `none` | `none`, `config` or `env` |
| `security.password` | None | The password, for `encryption = config` |
| `security.password_env` | `ARCH_PASSWORD` | The variable holding it, for `encryption = env` |
| `output.folder` | Required | Where the report and project are written |

The output folder's previous `<name>.arch` is replaced on every run.

### Source paths

The folders you list are imported in full. The app's built-in skip list, which covers folders such as `build`, `out` and `tools`, doesn't apply to them.

List the real source folders, not the whole repository. A test stub that calls a port would count as a real caller and could turn a `Failed` into a false `Passed`. Use `source_exclude` for anything inside a listed folder that isn't firmware. A listed folder with no source files stops the run with exit code 2.

Importing object files needs the native parser, which the packaged CLI includes.

### Match threshold

Each port's function is matched to the closest symbol, with a score out of 100:

- At or above `min_match_score`, it counts as a match and is tested.
- Below it, the port reports `Not Matched`, and the report names the closest symbol and its score.

A port also reports `Not Matched` when its cell is empty, or when the matched name isn't in the call tree, so nothing could be traced for it.

### Parameter ports

Each column in `parameter_columns` gets its own test and report, `<name>-<column>.html`, next to the main `<name>.html`. The title sets the direction:

| Title has | Passes when the row's function, or a function it calls, ... |
|---|---|
| `required` | has the variable as a parameter, or reads it |
| `provided` | returns it, writes it, or stores it through an output pointer |

Using it the wrong way round fails the run with "Wrong direction". A parameter that matches nothing above `min_parameter_score` is `Not Matched` and counts towards `max_unmatched`. That threshold is lower than the function one because generated variable names rarely match exactly. A row with nothing in that column has no parameter to test, so it's ignored.

The direction comes from the source, so list `source`. With `objects` listed too, the compiler's data relocations name every variable a function references:

| The run has | An access the source shows | An access the source doesn't show, such as a macro or assembly |
|---|---|---|
| `source` and `objects` | Tested, and the report says the object file confirms it | `Passed`, and the report says the direction isn't confirmed |
| `source` only | Tested | `Failed` |
| `objects` only | `Passed`, direction not confirmed | `Passed`, direction not confirmed |
| Neither | `No Data` | `No Data` |

A relocation shows that a function uses a variable, not whether it reads or writes it, so a wrong direction is only caught when the source shows the access. A variable reached only through a pointer stored elsewhere, such as in a table of pointers, isn't seen either way. Relocations are read from the object files, because a linked image no longer has them.

### Size limits

The C source analysis has no limit on file size, function length, or the number of variables in a function, so a large generated function is analysed in full. The limits that remain are:

| Limit | Value | What happens |
|---|---|---|
| Main functions per model | 10 | Extra rows are ignored |
| Functions followed for one parameter port | 5000, closest first | A `Failed` result says the check was cut short |
| Callers listed for one port in the report | 25 | The report still shows the total |

### How the result is decided

| Result | Effect |
|---|---|
| `Passed`, `Entry point` | OK |
| `No Result` (retired or deleted port, or no parameter in that column) | Ignored |
| `Not Matched` | Fails the run once there are more than `max_unmatched`, never with `-1` |
| `Failed`, `No Data` | Always fails the run |

The run always finishes every model, whatever `max_unmatched` is set to: the setting only decides the final result. Set it to accept a small number of missing symbols, or to `-1` to accept any number. They still appear in the report. A run where no port could be tested always fails, even with `-1`.

### Password options

| `encryption` | Where the password comes from | Use it for |
|---|---|---|
| `none` | No password, `.arch` is not encrypted | Non-sensitive projects |
| `config` | `password` in the `.cfg` file | Files kept outside version control |
| `env` | The variable named by `password_env` | Pipelines, using a masked secret |

An empty password always stops the run with exit code 2, so a missing secret can't change how the file is stored.

> ⚠️ **The HTML report is never encrypted.** It contains function names and call trees, so treat it with the same care as the build outputs. A password in a `.cfg` file is readable by anyone who can read that file.

## Installing

Download `architecture-validator-cli-<os>-<arch>.zip` from the release page and unzip it on the runner. It needs no Python and no desktop components. Use the build made for the runner's operating system. The Linux build may not start on a Linux version older than the one it was built on; if that happens, run it from source with Python on that runner:

```bash
pip install -r requirements.txt
```

```bash
python src/cli/main.py run --config validation.cfg
```

## Adding it to a pipeline

Add a job after the firmware build. The job's exit code is the validation result, and the report must be kept even when the job fails.

**GitLab**, with `ARCH_PASSWORD` set as a masked CI/CD variable:

```yaml
architecture_validation:
  stage: test
  needs: [build]
  script:
    - arch-validator run --config validation.cfg
  artifacts:
    when: always
    paths:
      - results/
```

**GitHub**, with `ARCH_PASSWORD` set as a repository secret:

```yaml
architecture-validation:
  needs: build
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/download-artifact@v4
      with:
        name: firmware-build
    - name: Validate architecture
      env:
        ARCH_PASSWORD: ${{ secrets.ARCH_PASSWORD }}
      run: arch-validator run --config validation.cfg
    - uses: actions/upload-artifact@v4
      if: always()
      with:
        name: architecture-validation
        path: results/
```

The CLI reads the build's outputs without running them, so the job doesn't need the compiler and can use a different runner from the build.

Scheduled runs on GitHub always use the default branch's latest commit.

## Working demo

[`Resources/VCU_TestProject/pipeline`](../../Resources/VCU_TestProject/pipeline/README.md) has a complete example to run or copy from: a test firmware compiled for ARM and Renesas RH850, a generated workbook, `.cfg` files, and sample GitLab and GitHub pipeline files. The app's own pipelines run these same steps, and test both targets on Linux, Windows and macOS.

## Report details

The report's operator details and logo come from the app's preferences. To set them in a pipeline, point the `ARCH_PREFS_FILE` environment variable at a saved preferences file.
