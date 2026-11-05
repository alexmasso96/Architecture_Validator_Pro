# -*- mode: python ; coding: utf-8 -*-
#
# Desktop shell (pywebview + FastAPI worker). Entry = src/desktop/main.py.
# This is the only build spec now — the legacy PyQt spec was removed at the
# Phase 4 cutover when the PyQt UI was deleted.
#
# The same folder also holds the Sync Worker's own program,
# ArchitectureValidatorSync (entry = src/desktop/sync_main.py). It runs all day,
# so it's built from its own small analysis instead of carrying the whole app,
# and Task Manager shows it under its own name. Both share the folder's DLLs.
#
# Onedir (COLLECT) is deliberate: a onefile build re-extracts every launch,
# which re-triggers the EDR small-file scanning penalty this whole migration
# exists to avoid.
#
# NOTE: this spec has not been validated by an actual build here — PyInstaller
# specs always need a round of iteration on the real target OS (and the Windows
# WebView2 runtime check). Build + smoke-test on macOS and an EDR Windows box
# before relying on it (plan §5, §9 risk row "WebView2 missing").
import os
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all

block_cipher = None
console_build = bool(os.environ.get('ARCH_BUILD_CONSOLE'))

# Made from Media/icon/icon.svg and icon_sync.svg by Media/icon/render_icon.py.
ICON_DIR = os.path.join('Media', 'icon')
ICON_ICO = os.path.join(ICON_DIR, 'app.ico')
ICON_ICNS = os.path.join(ICON_DIR, 'app.icns')
ICON_PNG = os.path.join(ICON_DIR, 'icon_1024.png')
exe_icon = ICON_ICO if sys.platform == 'win32' else (ICON_ICNS if sys.platform == 'darwin' else None)
# The Sync Worker's executable: the app icon with a sync badge. macOS shows only
# the bundle's icon, so it needs no .icns of its own.
sync_exe_icon = os.path.join(ICON_DIR, 'app_sync.ico') if sys.platform == 'win32' else exe_icon

PRODUCT_NAME = 'Architecture Validator Pro'
with open(os.path.join('src', 'frontend', 'package.json'), encoding='utf-8') as _f:
    APP_VERSION = __import__('json').load(_f)['version']


def version_info(exe_name, description):
    """The Windows version resource. Task Manager shows its FileDescription as the
    process name, so without it both programs would just be their file names."""
    if sys.platform != 'win32':
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
        VarStruct, VSVersionInfo)
    numbers = tuple(int(p) for p in APP_VERSION.split('.')[:3]) + (0,)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable('040904B0', [
                StringStruct('ProductName', PRODUCT_NAME),
                StringStruct('FileDescription', description),
                StringStruct('FileVersion', APP_VERSION),
                StringStruct('ProductVersion', APP_VERSION),
                StringStruct('InternalName', exe_name),
                StringStruct('OriginalFilename', f'{exe_name}.exe'),
            ])]),
            VarFileInfo([VarStruct('Translation', [1033, 1200])]),
        ])

# Bundle the built SPA at <bundle>/frontend/dist so backend.static.frontend_dist()
# finds it under sys._MEIPASS. Run `npm run build` in src/frontend/ BEFORE
# packaging. Source path is src/frontend/dist; bundle dest stays frontend/dist.
datas = [
    ('src/frontend/dist', 'frontend/dist'),
    (ICON_PNG, ICON_DIR),
]
# Which build this is, for in-app updates (src/desktop/build_info.py). CI writes
# it with scripts/write_build_info.py; a local build without it can't tell which
# commit it is, so it always offers the newest build of its branch.
if os.path.isfile('build_info.json'):
    datas.append(('build_info.json', '.'))
# uvicorn/webview import their backends lazily — collect them so the frozen app
# can find the event loop, HTTP/WS protocols, and the platform webview backend.
datas += collect_data_files('webview')

binaries = []

hiddenimports = [
    'elftools', 'elftools.elf.elffile',
    'pandas', 'openpyxl', 'rapidfuzz', 'capstone', 'bcrypt',
    'rust_elf_parser', 'cpp_demangle', 'cryptography',
    # The source index's C parser, loaded only when a source is indexed.
    'tree_sitter', 'tree_sitter_c',
]
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('webview')
# The Sync Worker's tray icon: pystray picks its platform backend at runtime.
hiddenimports += collect_submodules('pystray')

# Windows: pywebview's default backend is winforms, which imports `clr`
# (pythonnet) and bootstraps the .NET runtime through `clr_loader`. PyInstaller's
# automatic analysis does NOT reliably bundle pythonnet's managed assembly
# (pythonnet/runtime/Python.Runtime.dll) together with clr_loader's native
# bootstrap shims (clr_loader/ffi/dlls/{x86,amd64}/*.dll — NO arm64 shim
# exists; see pythonnet/pythonnet#2523). When the shim and the managed assembly
# are mismatched or the shim is missing, clr_loader raises
#   "Failed to resolve Python.Runtime.Loader.Initialize from ...Python.Runtime.dll"
# and the app crashes before the window appears. collect_all() pulls the data
# files, native DLLs, and submodules for both so they ship with the correct
# layout. Guarded to win32 because pythonnet/clr_loader are not installed on the
# macOS/Linux build hosts (those use the WKWebView / GTK backends instead).
#
# ARM64 note: a bundle built with a *native ARM64* Python targets CoreCLR at
# runtime instead of .NET Framework (desktop/main.py:_configure_dotnet_runtime);
# build such bundles from a venv prepared by scripts/setup_windows_arm64.py so
# the CoreCLR-compatible WebView2 assemblies are the ones collected here.
dotnet_datas, dotnet_binaries, dotnet_hiddenimports = [], [], []
if sys.platform == 'win32':
    for _pkg in ('pythonnet', 'clr_loader'):
        _d, _b, _h = collect_all(_pkg)
        dotnet_datas += _d
        dotnet_binaries += _b
        dotnet_hiddenimports += _h
    dotnet_hiddenimports += ['clr']
datas += dotnet_datas
binaries += dotnet_binaries
hiddenimports += dotnet_hiddenimports

a = Analysis(
    ['src/desktop/main.py'],
    pathex=['.', 'src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The desktop shell never touches Qt; keep it out of the bundle.
    excludes=['PyQt6', 'PyQt6.sip'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ArchitectureValidator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console_build,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
    version=version_info('ArchitectureValidator', PRODUCT_NAME),
)

# --- The Sync Worker's program -------------------------------------------------
# Only the tray, the sync engine and, for its window, pywebview. Application_Logic
# loads its heavy modules on first use, so they stay out of this one. Its window
# borrows the Windows set-up in desktop/main.py, whose worker bootstrap names the
# web server; that only runs in the app, so the server is left out here.
sync_a = Analysis(
    ['src/desktop/sync_main.py'],
    pathex=['.', 'src'],
    binaries=dotnet_binaries,
    datas=collect_data_files('webview') + dotnet_datas,
    hiddenimports=(collect_submodules('pystray') + collect_submodules('webview')
                   + dotnet_hiddenimports),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt6', 'PyQt6.sip', 'backend.app', 'uvicorn'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

sync_pyz = PYZ(sync_a.pure, sync_a.zipped_data, cipher=block_cipher)

sync_exe = EXE(
    sync_pyz,
    sync_a.scripts,
    [],
    exclude_binaries=True,
    name='ArchitectureValidatorSync',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console_build,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=sync_exe_icon,
    version=version_info('ArchitectureValidatorSync', f'{PRODUCT_NAME} Sync'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    sync_exe,
    sync_a.binaries,
    sync_a.zipfiles,
    sync_a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ArchitectureValidator',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='ArchitectureValidator.app',
        icon=ICON_ICNS,
        bundle_identifier='com.architecture.validator.desktop',
    )
