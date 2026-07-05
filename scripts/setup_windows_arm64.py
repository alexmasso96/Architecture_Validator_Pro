"""
One-shot environment fixup for developing/packaging on native Windows-on-ARM.

Run *inside the project venv* after ``pip install -r requirements.txt``:

    python scripts/setup_windows_arm64.py

Why this exists
---------------
On a native ARM64 Python, pythonnet cannot use .NET Framework (clr_loader
ships no ARM64 netfx shim — pythonnet/pythonnet#2523), so the desktop shell
routes it to CoreCLR (see ``desktop/main.py:_configure_dotnet_runtime``).
Two pywebview payloads are .NET Framework-only and must be adjusted in
site-packages; both changes are picked up automatically by PyInstaller when
building the ARM64 bundle from this venv:

1. ``webview/lib/Microsoft.Web.WebView2.{Core,WinForms}.dll`` are the net462
   builds; their ``WebView2`` control references ``System.Windows.Forms.
   ContextMenu``, a type removed in modern .NET → TypeLoadException. The
   WebView2 NuGet package ships CoreCLR-compatible builds under
   ``lib_manual/netcoreapp3.0``; this script swaps them in (same SDK version
   as the ones pywebview bundled, originals backed up next to them).

2. ``webview/platforms/winforms.py``'s ``OpenFolderDialog`` reflects into
   .NET Framework-internal types (``FileDialogNative+IFileDialog``) at import
   time → AttributeError on CoreCLR. The patch makes that reflection lazy and
   falls back to ``FolderBrowserDialog``, which on modern .NET already *is*
   the vista-style picker.

Prerequisite on every ARM64 machine (dev or target): the ARM64 **.NET Desktop
Runtime 8.0+** — https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-arm64.exe
(the app's launch preflight checks for it and links there too).
"""
from __future__ import annotations

import io
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

MARKER = "_vista_reflection_available"

FOLDER_DIALOG_OLD = """\
class OpenFolderDialog:
    foldersFilter = 'Folders|\\n'
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialogNative+IFileDialog'
    )
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
    onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
    getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
    setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
    fosPickFoldersBitFlag = (
        windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
        .GetField('FOS_PICKFOLDERS')
        .GetValue(None)
    )

    vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialog+VistaDialogEvents'
    ).GetConstructor(flags, None, [FileDialogType], [])
    adviseMethodInfo = iFileDialogType.GetMethod('Advise')
    unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
    showMethodInfo = iFileDialogType.GetMethod('Show')

    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
"""

FOLDER_DIALOG_NEW = """\
class OpenFolderDialog:
    foldersFilter = 'Folders|\\n'
    # The reflection below reaches into .NET Framework-internal WinForms types
    # (FileDialogNative+IFileDialog etc.) that were removed/renamed in .NET
    # (Core) WinForms. Under CoreCLR (e.g. Windows-on-ARM, where the netfx
    # loader is unavailable) fall back to FolderBrowserDialog, which on modern
    # .NET already *is* the vista-style IFileDialog picker.
    _vista_reflection_available = False
    try:
        flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
        windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
        iFileDialogType = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialogNative+IFileDialog'
        )
        OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
        FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
        createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
        onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
        getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
        setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
        fosPickFoldersBitFlag = (
            windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
            .GetField('FOS_PICKFOLDERS')
            .GetValue(None)
        )

        vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialog+VistaDialogEvents'
        ).GetConstructor(flags, None, [FileDialogType], [])
        adviseMethodInfo = iFileDialogType.GetMethod('Advise')
        unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
        showMethodInfo = iFileDialogType.GetMethod('Show')
        _vista_reflection_available = True
    except Exception:
        logger.debug('FileDialogNative reflection unavailable (CoreCLR); '
                     'falling back to FolderBrowserDialog')

    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
        if not cls._vista_reflection_available:
            dialog = WinForms.FolderBrowserDialog()
            if initialDirectory:
                dialog.InitialDirectory = initialDirectory
            if title:
                dialog.Description = title
            try:
                dialog.Multiselect = allow_multiple
            except Exception:
                pass  # Multiselect requires .NET 8+; single-select otherwise
            result = dialog.ShowDialog(parent) if parent else dialog.ShowDialog()
            if result == WinForms.DialogResult.OK:
                try:
                    return tuple(dialog.SelectedPaths)
                except Exception:
                    return (dialog.SelectedPath,)
            return None
"""


def swap_webview2_assemblies(lib_dir: Path) -> None:
    core = lib_dir / "Microsoft.Web.WebView2.Core.dll"
    winforms = lib_dir / "Microsoft.Web.WebView2.WinForms.dll"
    backup = lib_dir / "netfx_backup"
    if (backup / core.name).exists():
        print("WebView2 assemblies already swapped — skipping.")
        return

    # Match the SDK version pywebview bundled so Core/WinForms/Loader agree.
    try:
        import ctypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(core), None)
        data = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(str(core), 0, size, data)
        buf = ctypes.c_void_p()
        length = ctypes.c_uint()
        ctypes.windll.version.VerQueryValueW(
            data, "\\", ctypes.byref(buf), ctypes.byref(length)
        )

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32), ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32), ("dwFileVersionLS", ctypes.c_uint32),
                ("dwProductVersionMS", ctypes.c_uint32), ("dwProductVersionLS", ctypes.c_uint32),
            ]

        info = ctypes.cast(buf, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        version = (
            f"{info.dwFileVersionMS >> 16}.{info.dwFileVersionMS & 0xFFFF}."
            f"{info.dwFileVersionLS >> 16}.{info.dwFileVersionLS & 0xFFFF}"
        )
    except Exception as exc:
        raise SystemExit(f"Could not read WebView2 SDK version from {core}: {exc}")

    url = f"https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/{version}"
    print(f"Downloading WebView2 SDK {version} from nuget.org ...")
    with urllib.request.urlopen(url) as resp:
        pkg = zipfile.ZipFile(io.BytesIO(resp.read()))

    backup.mkdir(exist_ok=True)
    for dll in (core, winforms):
        shutil.copy2(dll, backup / dll.name)
        with pkg.open(f"lib_manual/netcoreapp3.0/{dll.name}") as src, open(dll, "wb") as dst:
            shutil.copyfileobj(src, dst)
        print(f"Swapped {dll.name} -> netcoreapp3.0 build (netfx original in {backup.name}/)")


def patch_winforms(winforms_py: Path) -> None:
    content = winforms_py.read_text(encoding="utf-8")
    if MARKER in content:
        print("winforms.py already patched — skipping.")
        return
    if FOLDER_DIALOG_OLD not in content:
        raise SystemExit(
            f"Unexpected OpenFolderDialog implementation in {winforms_py}; "
            "pywebview version changed — re-derive this patch (or check whether "
            "upstream now supports CoreCLR natively and drop it)."
        )
    winforms_py.write_text(
        content.replace(FOLDER_DIALOG_OLD, FOLDER_DIALOG_NEW), encoding="utf-8"
    )
    print(f"Patched OpenFolderDialog in {winforms_py}")


def main() -> None:
    if sys.platform != "win32" or platform.machine().upper() != "ARM64":
        raise SystemExit("This fixup is only needed on native Windows-on-ARM Python.")
    try:
        import webview
    except ImportError:
        raise SystemExit("pywebview is not installed in this environment.")

    pkg_dir = Path(webview.__file__).parent
    swap_webview2_assemblies(pkg_dir / "lib")
    patch_winforms(pkg_dir / "platforms" / "winforms.py")
    print("Done. Ensure the ARM64 .NET Desktop Runtime 8.0+ is installed:")
    print("  https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-arm64.exe")


if __name__ == "__main__":
    main()
