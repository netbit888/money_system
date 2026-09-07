@echo off
chcp 65001 >nul
title Money System - Start

cd /d "d:\PythonProject\money_system"

echo ========================================
echo   Money System - One-click Start
echo   Project: %cd%
echo   Backend: app.py (http.server) - PyPy preferred
echo   Frontend: economy.html (fetch API)
echo ========================================
echo.

REM ===== 1. Check Python =====
echo [1/4] Checking Python...
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python and add to PATH.
    pause
    exit /b 1
)
python --version

REM ===== 1.5 Detect runtime: prefer PyPy for speed, fallback to CPython =====
set "PYPY_PATH=d:\PythonProject\pypy3.11-v7.3.23-win64\pypy3.exe"
set "RUN_CMD=python"
if exist "%PYPY_PATH%" (
    set "RUN_CMD=%PYPY_PATH%"
    echo Using PyPy: %PYPY_PATH%
) else (
    echo PyPy not found, using CPython (python)
)

REM ===== 2. Check port 8000 =====
echo.
echo [2/4] Checking port 8000...
set PORT_IN_USE=0
netstat -ano | findstr ":8000 " | findstr LISTENING >nul 2>nul
if not errorlevel 1 set PORT_IN_USE=1
if %PORT_IN_USE%==1 (
    echo Port 8000 already bound. Releasing...
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr LISTENING') do (
        echo Kill old process PID: %%p
        taskkill /F /PID %%p >nul 2>nul
    )
    ping -n 2 127.0.0.1 >nul
) else (
    echo Port 8000 is free.
)

REM ===== 3. Start backend API server in a new visible window =====
echo.
echo [3/4] Starting backend on port 8000...
start "MoneySystem API" cmd /k "cd /d d:\PythonProject\money_system && %RUN_CMD% app.py"

REM ===== 4. Wait and open browser =====
echo.
echo [4/4] Opening browser...
ping -n 3 127.0.0.1 >nul
start "" "http://127.0.0.1:8000/"

echo.
echo ========================================
echo   Start finished.
echo   - API window title: "MoneySystem API"
echo   - URL: http://127.0.0.1:8000/
echo   - If page shows "load failed", wait 1-2s and refresh.
echo ========================================
echo.
ping -n 4 127.0.0.1 >nul
