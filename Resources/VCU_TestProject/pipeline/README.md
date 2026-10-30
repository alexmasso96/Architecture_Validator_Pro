# Pipeline demo

A complete, working example of the [Pipeline CLI](../../../docs/guide/12-pipeline-cli.md): everything a CI team needs to see how it fits into a build pipeline. It uses the synthetic VCU firmware, built for both ARM and Renesas RH850, so it's safe to share and run anywhere. The app's own GitLab and GitHub pipelines run these same steps.

## What's here

| File | Purpose |
|---|---|
| `build_firmware.py` | Stands in for your firmware build: compiles the VCU code for `arm` or `rh850` |
| `build_rh850_toolchain.sh` | Builds the free GCC compiler for RH850, needed only for the `rh850` target |
| `make_workbook.py` | Stands in for your architecture export: writes the workbook the CLI reads |
| `validation-arm.cfg`, `validation-rh850.cfg` | The CLI's settings for each target |
| `requirements.txt` | Tools for the two stand-in scripts (the CLI doesn't need them) |
| `example-gitlab-ci.yml` | A GitLab job to copy from |
| `example-github-workflow.yml` | A GitHub Actions workflow to copy from |

The demo's tools write `build/`, `rh850-toolchain/`, `interfaces.xlsx` and `results/` here. None of those are kept in git.

## Run it

From the repository root, install the tools and generate the inputs:

```bash
pip install -r Resources/VCU_TestProject/pipeline/requirements.txt
```

```bash
python Resources/VCU_TestProject/pipeline/build_firmware.py arm
```

```bash
python Resources/VCU_TestProject/pipeline/make_workbook.py
```

Then run the validation:

```bash
arch-validator run --config Resources/VCU_TestProject/pipeline/validation-arm.cfg
```

The last line says `RESULT: PASSED`, the exit code is `0`, and `results/` holds `vcu-arm.html` and `vcu-arm.arch`.

`arch-validator` is the packaged CLI from the release page. To run from source instead, use `python src/cli/main.py`. Running from source needs `pip install -r requirements.txt` and the native parser for the object files; the packaged CLI already includes both.

## Renesas RH850

The same firmware also builds for RH850, using the GCC `v850` target with the RH850 ABI. Nobody packages this compiler, so the first step builds it from source, which takes a few minutes. It needs Linux and the packages listed at the top of the script:

```bash
Resources/VCU_TestProject/pipeline/build_rh850_toolchain.sh
```

```bash
python Resources/VCU_TestProject/pipeline/build_firmware.py rh850
```

```bash
arch-validator run --config Resources/VCU_TestProject/pipeline/validation-rh850.cfg
```

Pipelines should cache `rh850-toolchain/` so they build it only once. Only the compile step needs Linux: the CLI can read the RH850 output on any platform, which is how the app's own GitHub pipeline tests it on Windows and macOS.

This build uses the same ABI, ELF format and `_name` symbols as Renesas firmware, but a different compiler from CC-RH or GHS. Run your real project too before relying on the result.

## Using it for real firmware

1. Replace the two stand-in scripts with your own build and architecture export.
2. Copy a `validation-*.cfg` file and point it at your outputs. Set the column names and `object_pattern` to match them.
3. Copy the job from the example for your platform, and put the CLI on your runner.
4. If the project should be encrypted, set `encryption = env` and add the password as a masked variable or secret.

The firmware can come from any compiler: the CLI reads the ELF, source and object files without running them. The image and object files just have to be ELF format, which is why the demo builds for ARM and RH850 rather than for the machine it runs on.

To see a failing run, remove the `_Init`/`_Step` filter in `make_workbook.py` so the workbook includes ports nothing calls. The run then ends with `RESULT: FAILED` and exit code `1`, and the report explains each failed port.
