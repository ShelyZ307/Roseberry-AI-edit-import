@echo off
setlocal

set "REPO_ROOT=%~dp0.."
set "TARGET_DIR=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\Roseberry AI Tools"
set "SOURCE=%REPO_ROOT%\launchers\windows\Roseberry AI Edit Import.py"
set "TARGET=%TARGET_DIR%\Roseberry AI Edit Import.py"

if not exist "%SOURCE%" (
  echo Launcher not found:
  echo %SOURCE%
  pause
  exit /b 1
)

if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

if exist "%TARGET%" (
  copy "%TARGET%" "%TARGET%.bak.%DATE:/=-%_%TIME::=-%" >nul
)

copy "%SOURCE%" "%TARGET%" >nul
if %ERRORLEVEL% NEQ 0 (
  echo Failed to install Resolve launcher.
  pause
  exit /b 1
)

echo Installed:
echo %TARGET%
echo.
echo Restart DaVinci Resolve, then open:
echo Workspace ^> Scripts ^> Utility ^> Roseberry AI Tools ^> Roseberry AI Edit Import
pause

