# Pipeline demo

A complete, working example of the [Pipeline CLI](../../../docs/guide/12-pipeline-cli.md): everything a CI team needs to see how it fits into a build pipeline. It uses the synthetic VCU firmware, so it's safe to share and run anywhere. The app's own GitLab and GitHub pipelines run these same steps on Linux, Windows and macOS.

## What's here

| File | Purpose |
|---|---|
| `build_firmware.py` | Stands in for your firmware build: compiles the VCU code for an ARM Cortex-M4 |
| `make_workbook.py` | Stands in for your architecture export: writes the workbook the CLI reads |
| `validation.cfg` | The CLI's settings for this demo |
| `requirements.txt` | Tools for the two stand-in scripts (the CLI doesn't need them) |
| `example-gitlab-ci.yml` | A GitLab job to copy from |
| `example-github-workflow.yml` | A GitHub Actions workflow to copy from |

The demo's tools write `build/`, `interfaces.xlsx` and `results/` here. None of those are kept in git.

## Run it

From the repository root, install the tools and generate the inputs:

```bash
pip install -r Resources/VCU_TestProject/pipeline/requirements.txt
```

```bash
python Resources/VCU_TestProject/pipeline/build_firmware.py
```

```bash
python Resources/VCU_TestProject/pipeline/make_workbook.py
```

Then run the validation:

```bash
arch-validator run --config Resources/VCU_TestProject/pipeline/validation.cfg
```

The last line says `RESULT: PASSED`, the exit code is `0`, and `results/` holds `vcu-validation.html` and `vcu-validation.arch`.

`arch-validator` is the packaged CLI from the release page. To run from source instead, use `python src/cli/main.py`. Running from source needs `pip install -r requirements.txt` and the native parser for the object files; the packaged CLI already includes both.

## Using it for real firmware

1. Replace the two stand-in scripts with your own build and architecture export.
2. Copy `validation.cfg` and point it at your outputs. Set the column names and `object_pattern` to match them.
3. Copy the job from the example for your platform, and put the CLI on your runner.
4. If the project should be encrypted, set `encryption = env` and add the password as a masked variable or secret.

The firmware can come from any compiler: the CLI reads the ELF, source and object files without running them. The ELF and object files just have to be ELF format, which is why the demo builds for ARM on every platform rather than for the machine it runs on.

To see a failing run, remove the `_Init`/`_Step` filter in `make_workbook.py` so the workbook includes ports nothing calls. The run then ends with `RESULT: FAILED` and exit code `1`, and the report explains each failed port.
