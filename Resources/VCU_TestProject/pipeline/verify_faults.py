"""Prove the CLI catches bad parameter ports, not only that good ones pass.

Runs one validation over a workbook with one deliberately wrong port per kind of
failure and checks it fails for the right reasons. A second run leaves the source
out: object files alone still show which variables a function references, but not
which way, so the wrong directions pass and say the direction isn't confirmed.

    python verify_faults.py <validation cfg> <CLI command...>
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
COLUMNS = ["Port Name", "Operations", "Required Parameter", "Provided Parameter"]
FAULTS = [
    # Written, but the port says it's read.
    ["wrong_required", "Bcm_Lighting_Init", "g_bcm_lighting_state.phase", None],
    # Only read, but the port says it's provided.
    ["wrong_provided", "Bcm_Lighting_Step", None, "g_bcm_lighting_cfg.max_current_ma"],
    # A real variable the function has nothing to do with.
    ["unrelated", "Bcm_Lighting_Init", "g_bcm_doors_state", None],
    # A name that looks nothing like the code.
    ["unknown", "Bcm_Lighting_Init", "Zzqq Nothing Like This", None],
]
EXPECTED = {
    "required-parameter": ["Wrong direction", "does not use", "Not Matched"],
    "provided-parameter": ["Wrong direction"],
}
OBJECTS_ONLY = "in its object file"
EXPECTED_WITHOUT_SOURCE = {
    "required-parameter": [OBJECTS_ONLY, "does not use", "Not Matched"],
    "provided-parameter": [OBJECTS_ONLY],
}


def check(command: list, name: str, text: str, expected: dict) -> list:
    """The problems with one run that should fail for the expected reasons."""
    config = HERE / f"{name}.cfg"
    config.write_text(re.sub(r"^name = .*$", f"name = {name}", text, flags=re.M),
                      encoding="utf-8")
    result = subprocess.run(command + ["run", "--config", str(config)],
                            capture_output=True, text=True)
    print(result.stdout)
    errors = []
    if result.returncode != 1:
        errors.append(f"{name}: expected exit code 1, got {result.returncode}")
    for suffix, phrases in expected.items():
        report = HERE / "results" / f"{name}-{suffix}.html"
        html = report.read_text(encoding="utf-8") if report.exists() else ""
        errors += [f"{report.name} doesn't mention '{p}'" for p in phrases if p not in html]
    return errors


def main() -> int:
    config, command = Path(sys.argv[1]).resolve(), sys.argv[2:]
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Faults"
    sheet.append(COLUMNS)
    for row in FAULTS:
        sheet.append(row)
    mains = workbook.create_sheet("Main Functions")
    mains.append(["Model", "Main Function"])
    mains.append(["Faults", "Vcu_InitAll"])
    mains.append(["Faults", "Vcu_Cyclic10ms"])
    workbook.save(HERE / "faults.xlsx")

    text = config.read_text(encoding="utf-8")
    text = re.sub(r"^workbook = .*$", "workbook = faults.xlsx", text, flags=re.M)
    errors = check(command, f"{config.stem}-faults", text, EXPECTED)
    errors += check(command, f"{config.stem}-objects",
                    re.sub(r"^source = .*\n", "", text, flags=re.M), EXPECTED_WITHOUT_SOURCE)
    for error in errors:
        print(f"FAULT CHECK FAILED: {error}")
    if not errors:
        print("FAULT CHECK PASSED")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
