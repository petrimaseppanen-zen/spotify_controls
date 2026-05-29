@echo off
setlocal

set "PY=.\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Virtual environment python not found: %PY%
  echo Create it first: py -3.12 -m venv .venv
  exit /b 1
)

echo Installing/updating PyInstaller...
"%PY%" -m pip install --upgrade pyinstaller
if errorlevel 1 exit /b 1

echo Building SpotifyMiniController.exe...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name SpotifyMiniController spot.py
if errorlevel 1 exit /b 1

echo.
echo Build complete: dist\SpotifyMiniController.exe
endlocal
