# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir build of the pipeline command line (src/cli/main.py):
# console executable `arch-validator`, no window, no web frontend. Output:
# dist/arch-validator/. Run it on the OS it targets; PyInstaller does not
# cross-compile. The desktop app stays in ArchitectureValidatorDesktop.spec.
hiddenimports = [
    'elftools', 'elftools.elf.elffile',
    'pandas', 'openpyxl', 'rapidfuzz', 'capstone', 'bcrypt',
    'rust_elf_parser', 'cpp_demangle', 'cryptography',
]

a = Analysis(
    ['src/cli/main.py'],
    pathex=['.', 'src'],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt6', 'PyQt6.sip', 'webview', 'clr', 'clr_loader', 'pythonnet',
              'tkinter', 'uvicorn'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='arch-validator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='arch-validator',
)
