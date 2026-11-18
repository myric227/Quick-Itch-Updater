@echo off
setlocal
pushd "%~dp0" || exit /b 1
if not exist ".venv\Scripts\pythonw.exe" (
    echo Quick Itch Updater's virtual environment is missing.
    echo See README.md for setup instructions, then double-click this file again.
    pause
    popd
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -B -m quick_itch
popd
