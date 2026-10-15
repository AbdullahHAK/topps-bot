@echo off
REM Builds dist\ToppsBot.exe (single file). Run from the project folder.
if not exist .venv (
    python -m venv .venv || exit /b 1
)
.venv\Scripts\python -m pip install -q -r requirements.txt || exit /b 1
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name ToppsBot ^
    --collect-all playwright ^
    run.py || exit /b 1
echo.
echo Built dist\ToppsBot.exe
