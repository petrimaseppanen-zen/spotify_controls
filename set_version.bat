@echo off
setlocal

if "%~1"=="" (
  echo Usage: set_version.bat MAJOR.MINOR.PATCH
  echo Example: set_version.bat 1.0.1
  exit /b 1
)

set "VERSION=%~1"
echo %VERSION%>VERSION.txt
echo Version updated to %VERSION%

endlocal
