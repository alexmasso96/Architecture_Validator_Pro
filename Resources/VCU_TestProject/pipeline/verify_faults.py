"""Prove the CLI catches bad parameter ports, not only that good ones pass.

Runs one validation over a workbook with one deliberately wrong port per kind of
failure and checks it fails for the right reasons.

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

    name = f"{config.stem}-faults"
    text = config.read_text(encoding="utf-8")
    text = re.sub(r"^workbook = .*$", "workbook = faults.xlsx", text, flags=re.M)
    text = re.sub(r"^name = .*$", f"name = {name}", text, flags=re.M)
    faults_config = HERE / f"{name}.cfg"
    faults_config.write_text(text, encoding="utf-8")

    result = subprocess.run(command + ["run", "--config", str(faults_config)],
                            capture_output=True, text=True)
    print(result.stdout)
    errors = []
    if result.returncode != 1:
        errors.append(f"expected exit code 1, got {result.returncode}")
    for suffix, phrases in EXPECTED.items():
        report = HERE / "results" / f"{name}-{suffix}.html"
        html = report.read_text(encoding="utf-8") if report.exists() else ""
        errors += [f"{report.name} doesn't mention '{p}'" for p in phrases if p not in html]
    for error in errors:
        print(f"FAULT CHECK FAILED: {error}")
    if not errors:
        print("FAULT CHECK PASSED")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
