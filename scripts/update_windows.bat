@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM TODO: Replace this placeholder with the real published manifest URL.
if "%ROSEBERRY_AI_EDIT_IMPORT_MANIFEST_URL%"=="" (
  set "MANIFEST_URL=https://github.com/ShelyZ307/roseberry-ai-edit-import/releases/latest/download/update_manifest.json"
) else (
  set "MANIFEST_URL=%ROSEBERRY_AI_EDIT_IMPORT_MANIFEST_URL%"
)

if "%ROSEBERRY_AI_EDIT_IMPORT_HOME%"=="" (
  set "APP_HOME=%LOCALAPPDATA%\Roseberry\AI Edit Import"
) else (
  set "APP_HOME=%ROSEBERRY_AI_EDIT_IMPORT_HOME%"
)

set "APP_DIR=%APP_HOME%\roseberry_ai_edit_import"
set "CONFIG_DIR=%APPDATA%\Roseberry AI Tools"
set "CONFIG_PATH=%CONFIG_DIR%\config.json"
set "LOG_PATH=%USERPROFILE%\Desktop\roseberry_ai_tools_update_log.txt"
set "TMP_DIR=%TEMP%\roseberry_ai_edit_import_update_%RANDOM%%RANDOM%"

mkdir "%TMP_DIR%" >nul 2>nul
echo [%DATE% %TIME%] Roseberry AI Edit Import updater starting. > "%LOG_PATH%"
echo [%DATE% %TIME%] Manifest URL: %MANIFEST_URL% >> "%LOG_PATH%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%MANIFEST_URL%' -OutFile '%TMP_DIR%\update_manifest.json'"
if %ERRORLEVEL% NEQ 0 (
  echo Failed to download update manifest. See %LOG_PATH%
  pause
  exit /b 1
)

for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Content '%TMP_DIR%\update_manifest.json' | ConvertFrom-Json).latest_version"`) do set "LATEST_VERSION=%%v"
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Content '%TMP_DIR%\update_manifest.json' | ConvertFrom-Json).windows.zip_url"`) do set "ZIP_URL=%%v"
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Content '%TMP_DIR%\update_manifest.json' | ConvertFrom-Json).windows.sha256"`) do set "EXPECTED_SHA=%%v"

set "LOCAL_VERSION=unknown"
if exist "%APP_DIR%\VERSION" set /p LOCAL_VERSION=<"%APP_DIR%\VERSION"

echo [%DATE% %TIME%] Local version: %LOCAL_VERSION% >> "%LOG_PATH%"
echo [%DATE% %TIME%] Latest version: %LATEST_VERSION% >> "%LOG_PATH%"

if "%LOCAL_VERSION%"=="%LATEST_VERSION%" (
  echo Already up to date.
  echo [%DATE% %TIME%] Already up to date. >> "%LOG_PATH%"
  pause
  exit /b 0
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%ZIP_URL%' -OutFile '%TMP_DIR%\release.zip'"
if %ERRORLEVEL% NEQ 0 (
  echo Failed to download release ZIP. See %LOG_PATH%
  pause
  exit /b 1
)

for /f "usebackq delims=" %%h in (`powershell -NoProfile -Command "(Get-FileHash '%TMP_DIR%\release.zip' -Algorithm SHA256).Hash.ToLower()"`) do set "ACTUAL_SHA=%%h"
if not "%ACTUAL_SHA%"=="%EXPECTED_SHA%" (
  echo SHA256 mismatch. See %LOG_PATH%
  echo Expected %EXPECTED_SHA%, got %ACTUAL_SHA% >> "%LOG_PATH%"
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%TMP_DIR%\release.zip' -DestinationPath '%TMP_DIR%\release' -Force"
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "Get-ChildItem -Path '%TMP_DIR%\release' -Directory -Recurse -Filter roseberry_ai_edit_import | Select-Object -First 1 -ExpandProperty FullName"`) do set "NEW_PAYLOAD=%%d"

if "%NEW_PAYLOAD%"=="" (
  echo Could not find roseberry_ai_edit_import folder in release ZIP. See %LOG_PATH%
  pause
  exit /b 1
)

if not exist "%APP_HOME%" mkdir "%APP_HOME%"
if exist "%APP_DIR%" (
  set "BACKUP=%APP_HOME%\roseberry_ai_edit_import.backup.%DATE:/=-%_%TIME::=-%"
  xcopy "%APP_DIR%" "!BACKUP!\" /E /I /Y >nul
)

if exist "%APP_DIR%" rmdir /s /q "%APP_DIR%"
xcopy "%NEW_PAYLOAD%" "%APP_DIR%\" /E /I /Y >nul

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

%PY% -m py_compile "%APP_DIR%\ai_edit_import_utility_timecode_exact_json.py"
%PY% -m py_compile "%APP_DIR%\roseberry_ai_tools_app\run_desktop_app.py"
%PY% -m py_compile "%APP_DIR%\roseberry_ai_tools_app\roseberry_ai_tools\app.py"
%PY% -m py_compile "%APP_DIR%\roseberry_ai_tools_app\roseberry_ai_tools\backend_bridge.py"
%PY% -m py_compile "%APP_DIR%\roseberry_ai_tools_app\roseberry_ai_tools\excel_input_adapter.py"

%PY% -m venv "%APP_DIR%\roseberry_ai_tools_app\.venv"
"%APP_DIR%\roseberry_ai_tools_app\.venv\Scripts\python.exe" -m pip install --upgrade pip
"%APP_DIR%\roseberry_ai_tools_app\.venv\Scripts\python.exe" -m pip install -r "%APP_DIR%\roseberry_ai_tools_app\requirements.txt"

if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
(
  echo {
  echo   "app_home": "%APP_HOME:\=\\%",
  echo   "version": "%LATEST_VERSION%"
  echo }
) > "%CONFIG_PATH%"

echo [%DATE% %TIME%] Update complete. >> "%LOG_PATH%"
echo Update complete.
pause
