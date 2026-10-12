@echo off
REM ===================================================================
REM  Job Radar - double-click launcher
REM
REM  Opens the terminal menu from THIS file's own directory, which is
REM  what makes the project's relative paths (private\state.json,
REM  sources.json, .env) resolve no matter where the shortcut is run
REM  from.
REM
REM  Paths with spaces: %~dp0 expands to this file's directory WITH a
REM  trailing backslash, and every use of it is quoted. The directory
REM  this project actually lives in contains several spaces, so an
REM  unquoted path would fail immediately.
REM
REM  Errors stay on screen: the window is never closed automatically,
REM  and a failure prints the exit code and pauses.
REM ===================================================================

setlocal
title Job Radar

REM Trailing backslash is stripped so "%PROJECT_DIR%\file" is well formed.
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

cd /d "%PROJECT_DIR%" || (
    echo [ERROR] Could not change directory to:
    echo         "%PROJECT_DIR%"
    goto :failed
)

if not exist "%PROJECT_DIR%\menu.py" (
    echo [ERROR] menu.py was not found in:
    echo         "%PROJECT_DIR%"
    echo         Keep this launcher inside the project folder.
    goto :failed
)

REM Prefer the project's own virtual environment when one exists, then the
REM Windows launcher, then a plain python on PATH. Checked in that order so a
REM project venv always wins over a different interpreter on PATH.
set "PYTHON_CMD="
if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" set "PYTHON_CMD=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not defined PYTHON_CMD if exist "%PROJECT_DIR%\venv\Scripts\python.exe" set "PYTHON_CMD=%PROJECT_DIR%\venv\Scripts\python.exe"

if not defined PYTHON_CMD (
    where py >nul 2>&1 && set "PYTHON_CMD=py"
)
if not defined PYTHON_CMD (
    where python >nul 2>&1 && set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo [ERROR] No Python interpreter found.
    echo         Looked for: .venv\Scripts\python.exe, venv\Scripts\python.exe, py, python
    echo         Install Python 3.10+ or create a virtual environment in this folder.
    goto :failed
)

REM Real posting text contains Unicode the default console codepage cannot
REM print; without this a valid run can die on an encoding error.
set "PYTHONIOENCODING=utf-8"

echo Starting Job Radar from:
echo   "%PROJECT_DIR%"
echo Using interpreter: %PYTHON_CMD%
echo.

"%PYTHON_CMD%" "%PROJECT_DIR%\menu.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Job Radar exited with code %EXIT_CODE%.
    echo         The message above this line is the real cause.
    goto :failed
)

echo.
echo Job Radar closed normally.
pause
endlocal
exit /b 0

:failed
echo.
pause
endlocal
exit /b 1
