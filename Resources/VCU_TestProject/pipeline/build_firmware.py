"""Compile the VCU test firmware, as a real build would.

Stands in for the firmware build step of a pipeline. Writes to build/<target>/
next to this file:

    build/<target>/obj/<module>.o    one object file per C file
    build/<target>/vcu.elf           the linked image

Targets:

    arm      ARM Cortex-M4, with Zig's C compiler (pip install -r requirements.txt).
             Runs the same on Linux, Windows and macOS.
    rh850    Renesas RH850, with the GCC built by build_rh850_toolchain.sh (Linux).

    python build_firmware.py arm
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "releases" / "R5.0" / "src"
RH850_GCC = HERE / "rh850-toolchain" / "bin" / "v850-elf-gcc"

COMMON_CFLAGS = ["-g", "-gdwarf-4", "-O1", "-fno-inline", "-ffreestanding", "-fno-builtin",
                 "-Wno-unused", f"-I{SOURCE}"]
ARM_TARGET = ["-target", "thumb-freestanding-eabi", "-mcpu=cortex_m4"]
RH850_TARGET = ["-mv850e3v5", "-mrh850-abi"]

TARGETS = {
    "arm": {"cc": [sys.executable, "-m", "ziglang", "cc"], "cpu": ARM_TARGET,
            "link": ["-nostdlib", "-Wl,--entry=Vcu_InitAll"], "libs": []},
    # GCC for this target prefixes C symbols with an underscore, as Renesas does.
    "rh850": {"cc": [str(RH850_GCC)], "cpu": RH850_TARGET,
              "link": ["-nostdlib", "-Wl,--entry=_Vcu_InitAll"], "libs": ["-lgcc"]},
}


def compile_file(source: Path, target: dict, objects: Path) -> Path:
    output = objects / "_".join(source.relative_to(SOURCE).with_suffix(".o").parts)
    subprocess.run(target["cc"] + target["cpu"] + COMMON_CFLAGS
                   + ["-c", str(source), "-o", str(output)], check=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", choices=TARGETS)
    name = parser.parse_args().target
    target = TARGETS[name]
    build = HERE / "build" / name
    objects = build / "obj"
    image = build / "vcu.elf"
    objects.mkdir(parents=True, exist_ok=True)

    sources = sorted(SOURCE.rglob("*.c"))
    with ThreadPoolExecutor() as pool:
        outputs = list(pool.map(lambda s: compile_file(s, target, objects), sources))
    subprocess.run(target["cc"] + target["cpu"] + target["link"] + ["-o", str(image)]
                   + [str(o) for o in outputs] + target["libs"], check=True)
    print(f"{image}: {len(outputs)} objects, {image.stat().st_size} bytes")


if __name__ == "__main__":
    main()
