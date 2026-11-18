import os
from pathlib import Path
import platform
import runpy
import struct
import sys

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)


if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"} or struct.calcsize("P") != 8:
    raise SystemExit("Build Quick Itch Updater with 64-bit Python on Windows x64.")

project = Path(SPECPATH)
package = project / "quick_itch"
version = runpy.run_path(str(package / "__init__.py"))["__version__"]
version_parts = [int(part) for part in version.split(".")]
windows_version = tuple((version_parts + [0] * 4)[:4])
product = "Quick Itch Updater"
name = f"Quick-Itch-Updater-{version}-Windows-x64"
icon = str(package / "quick-itch-updater.ico")
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=windows_version, prodvers=windows_version, mask=0x3F, flags=0,
                      OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("FileDescription", product),
            StringStruct("FileVersion", version),
            StringStruct("OriginalFilename", name + ".exe"),
            StringStruct("ProductName", product),
            StringStruct("ProductVersion", version),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

# Limit DLL discovery to the Python installation and Windows. The Qt hooks add
# their own wheel directories during analysis.
windows = Path(os.environ["SystemRoot"])
python = Path(sys.base_prefix)
original_path = os.environ.get("PATH", "")
os.environ["PATH"] = os.pathsep.join(map(str, (python, python / "DLLs", windows / "System32", windows)))
try:
    app = Analysis(
        [str(package / "__main__.py")],
        pathex=[str(project)],
        datas=[(icon, "quick_itch")],
    )
finally:
    os.environ["PATH"] = original_path
archive = PYZ(app.pure)
exe = EXE(
    archive, app.scripts, app.binaries, app.datas, [],
    name=name,
    console=False,
    icon=icon,
    version=version_info,
    upx=False,
)
