"""Compile the VCU test firmware for an ARM Cortex-M4, as a real build would.

Stands in for the firmware build step of a pipeline. Uses Zig's C compiler
(`pip install -r requirements.txt`), which runs the same on Linux, Windows and
macOS and needs no ARM toolchain. Writes to build/ next to this file:

    build/obj/<module>.o    one object file per C file
    build/vcu.elf           the linked image

    python build_firmware.py
"""
from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "releases" / "R5.0" / "src"
BUILD = HERE / "build"
OBJECTS = BUILD / "obj"
IMAGE = BUILD / "vcu.elf"

ZIG_CC = [sys.executable, "-m", "ziglang", "cc"]
TARGET = ["-target", "thumb-freestanding-eabi", "-mcpu=cortex_m4"]
CFLAGS = ["-g", "-gdwarf-4", "-O1", "-fno-inline", "-ffreestanding", "-fno-builtin",
          "-Wno-unused", f"-I{SOURCE}"]
LDFLAGS = ["-nostdlib", "-Wl,--entry=Vcu_InitAll"]


def compile_file(source: Path) -> Path:
    name = "_".join(source.relative_to(SOURCE).with_suffix(".o").parts)
    output = OBJECTS / name
    subprocess.run(ZIG_CC + TARGET + CFLAGS + ["-c", str(source), "-o", str(output)],
                   check=True)
    return output


def main() -> None:
    OBJECTS.mkdir(parents=True, exist_ok=True)
    sources = sorted(SOURCE.rglob("*.c"))
    with ThreadPoolExecutor() as pool:
        objects = list(pool.map(compile_file, sources))
    subprocess.run(ZIG_CC + TARGET + LDFLAGS + ["-o", str(IMAGE)]
                   + [str(o) for o in objects], check=True)
    print(f"{IMAGE}: {len(objects)} objects, {IMAGE.stat().st_size} bytes")


if __name__ == "__main__":
    main()
