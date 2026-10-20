@echo off
setlocal enabledelayedexpansion
title SonicFlow Web Server

cd /d "%~dp0"

:: 1. Detect Python
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_BIN=py -3"
    goto :RUN_SERVER
)

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_BIN=python"
    goto :RUN_SERVER
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

:RUN_SERVER
echo ========================================================
echo   SonicFlow Web Server
echo   Running locally at: http://localhost:8000
echo ========================================================
echo Opening browser...
start "" "http://localhost:8000"
%PY_BIN% server.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [Notice] Server stopped with exit code: %ERRORLEVEL%
    pause
)
