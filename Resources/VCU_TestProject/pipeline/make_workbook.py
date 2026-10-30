"""Generate the architecture workbook the CLI reads, from the VCU test corpus.

Stands in for the step of a real pipeline that exports the architecture. Writes
interfaces.xlsx next to this file: one sheet per architecture model plus the
main functions sheet, keeping only ports the firmware really calls, so
both validation-*.cfg files pass.

    python make_workbook.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
PORTS = HERE.parent / "releases" / "R5.0" / "architecture_ports.csv"
OUTPUT = HERE / "interfaces.xlsx"

COLUMNS = ["Port Name", "Operations", "Direction", "Return Type"]
MAIN_FUNCTIONS = ("Vcu_InitAll", "Vcu_Cyclic10ms")


def main() -> None:
    models: dict[str, list[dict]] = {}
    with PORTS.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            path = row["Full path name"]
            if ("P10_SW_Arch_Public" in path
                    and row["Operations"].endswith(("_Init", "_Step"))):
                models.setdefault(path.split("::")[2], []).append(row)

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in models.items():
        sheet = workbook.create_sheet(name)
        sheet.append(COLUMNS)
        for row in rows:
            sheet.append([row[c] for c in COLUMNS])
    mains = workbook.create_sheet("Main Functions")
    mains.append(["Model", "Main Function"])
    for name in models:
        for function in MAIN_FUNCTIONS:
            mains.append([name, function])
    workbook.save(OUTPUT)
    print(f"{OUTPUT}: {len(models)} models")


if __name__ == "__main__":
    main()
