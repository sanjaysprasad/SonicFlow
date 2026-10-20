@echo off
setlocal enabledelayedexpansion
title SonicFlow Desktop App

cd /d "%~dp0"

:: 1. Detect Python
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_BIN=py -3"
    goto :CHECK_DEPS
)

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_BIN=python"
    goto :CHECK_DEPS
)

echo ========================================================
echo   [ERROR] Python is not detected on your system.
echo ========================================================
echo Please install Python 3.11 or newer from:
echo   https://www.python.org/downloads/
echo.
echo IMPORTANT: During setup, check "Add Python to PATH"!
echo.
pause
exit /b 1

:CHECK_DEPS
:: 2. Launch SonicFlow
echo Starting SonicFlow Desktop App...
%PY_BIN% app.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [Notice] The application closed with an exit code: %ERRORLEVEL%
    echo If dependencies are missing, run: %PY_BIN% -m pip install -r requirements.txt
    echo.
    pause
)
