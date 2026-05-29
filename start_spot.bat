@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python3.12.exe"
%PYTHON% --version >nul 2>&1
if errorlevel 1 set "PYTHON=py -3.12"

echo [1/4] Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
  echo Creating .venv...
  %PYTHON% -m venv .venv
  if errorlevel 1 goto :fail
)

echo [2/4] Updating pip...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/4] Installing dependencies (numpy, soundcard, sounddevice)...
.venv\Scripts\python.exe -m pip install numpy soundcard sounddevice
if errorlevel 1 goto :fail

echo [4/4] Launching Spotify controller...
.venv\Scripts\python.exe spot.py
if errorlevel 1 (
  echo.
  echo Application exited with an error (code %ERRORLEVEL%).
  pause
)
exit /b %ERRORLEVEL%

:fail
echo.
echo Setup or launch failed.
pause
exit /b 1
