@echo off
setlocal

set "REPO_ROOT=%~dp0.."
if "%ROSEBERRY_AI_EDIT_IMPORT_HOME%"=="" (
  set "APP_HOME=%LOCALAPPDATA%\Roseberry\AI Edit Import"
) else (
  set "APP_HOME=%ROSEBERRY_AI_EDIT_IMPORT_HOME%"
)
set "APP_DIR=%APP_HOME%\roseberry_ai_edit_import"
set "CONFIG_DIR=%APPDATA%\Roseberry AI Tools"
set "CONFIG_PATH=%CONFIG_DIR%\config.json"

echo Roseberry AI Edit Import setup for Windows
echo Install folder: %APP_DIR%

if not exist "%APP_HOME%" mkdir "%APP_HOME%"
if exist "%APP_DIR%" rmdir /s /q "%APP_DIR%"
xcopy "%REPO_ROOT%\roseberry_ai_edit_import" "%APP_DIR%\" /E /I /Y /EXCLUDE:%REPO_ROOT%\scripts\windows_copy_excludes.txt
if %ERRORLEVEL% NEQ 0 exit /b 1

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

%PY% --version
if %ERRORLEVEL% NEQ 0 (
  echo Python 3 was not found. Install Python 3.10 or newer, then rerun this script.
  pause
  exit /b 1
)

%PY% -m venv "%APP_DIR%\roseberry_ai_tools_app\.venv"
"%APP_DIR%\roseberry_ai_tools_app\.venv\Scripts\python.exe" -m pip install --upgrade pip
"%APP_DIR%\roseberry_ai_tools_app\.venv\Scripts\python.exe" -m pip install -r "%APP_DIR%\roseberry_ai_tools_app\requirements.txt"

if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
(
  echo {
  echo   "app_home": "%APP_HOME:\=\\%",
  echo   "version": "0.2.0"
  echo }
) > "%CONFIG_PATH%"

echo.
echo Setup complete.
echo Config: %CONFIG_PATH%
echo Next: scripts\install_launcher_windows.bat
pause

