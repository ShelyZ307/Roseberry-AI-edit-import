#!/bin/zsh
set -euo pipefail

# TODO: Replace this placeholder with the real published manifest URL.
MANIFEST_URL="${ROSEBERRY_AI_EDIT_IMPORT_MANIFEST_URL:-https://github.com/ShelyZ307/Roseberry-AI-edit-import/releases/latest/download/update_manifest.json}"
APP_HOME="${ROSEBERRY_AI_EDIT_IMPORT_HOME:-$HOME/Roseberry/AI Edit Import}"
APP_DIR="$APP_HOME/roseberry_ai_edit_import"
CONFIG_DIR="$HOME/Library/Application Support/Roseberry AI Tools"
CONFIG_PATH="$CONFIG_DIR/config.json"
LOG_PATH="$HOME/Desktop/roseberry_ai_tools_update_log.txt"
TMP_DIR="$(mktemp -d)"
STAMP="$(date +%Y%m%d_%H%M%S)"
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
AUTH_ARGS=()
if [ -n "$TOKEN" ]; then
  AUTH_ARGS=(-H "Authorization: Bearer $TOKEN")
fi

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG_PATH"
}

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

log "Roseberry AI Edit Import updater starting."
log "Manifest URL: $MANIFEST_URL"
log "App home: $APP_HOME"

python3 - <<PY
import json
from pathlib import Path
config_path = Path("$CONFIG_PATH")
config_path.parent.mkdir(parents=True, exist_ok=True)
if not config_path.exists():
    config_path.write_text(json.dumps({"app_home": "$APP_HOME"}, indent=2) + "\\n", encoding="utf-8")
PY

curl -fsSL "${AUTH_ARGS[@]}" "$MANIFEST_URL" -o "$TMP_DIR/update_manifest.json"

LATEST_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["latest_version"])' "$TMP_DIR/update_manifest.json")"
ZIP_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mac"]["zip_url"])' "$TMP_DIR/update_manifest.json")"
EXPECTED_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mac"]["sha256"])' "$TMP_DIR/update_manifest.json")"
LOCAL_VERSION="unknown"
if [ -f "$APP_DIR/VERSION" ]; then
  LOCAL_VERSION="$(cat "$APP_DIR/VERSION")"
fi

log "Local version: $LOCAL_VERSION"
log "Latest version: $LATEST_VERSION"

if [ "$LOCAL_VERSION" = "$LATEST_VERSION" ]; then
  log "Already up to date."
  exit 0
fi

curl -fsSL "${AUTH_ARGS[@]}" "$ZIP_URL" -o "$TMP_DIR/release.zip"
ACTUAL_SHA="$(shasum -a 256 "$TMP_DIR/release.zip" | awk '{print $1}')"
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  log "SHA256 mismatch. Expected $EXPECTED_SHA but got $ACTUAL_SHA"
  exit 1
fi

unzip -q "$TMP_DIR/release.zip" -d "$TMP_DIR/release"
NEW_PAYLOAD="$(find "$TMP_DIR/release" -type d -name roseberry_ai_edit_import | head -n 1)"
if [ -z "$NEW_PAYLOAD" ]; then
  log "Could not find roseberry_ai_edit_import folder in release ZIP."
  exit 1
fi

mkdir -p "$APP_HOME"
if [ -d "$APP_DIR" ]; then
  BACKUP="$APP_HOME/roseberry_ai_edit_import.backup.$STAMP"
  log "Backing up current app to: $BACKUP"
  cp -R "$APP_DIR" "$BACKUP"
fi

rm -rf "$APP_DIR"
cp -R "$NEW_PAYLOAD" "$APP_DIR"

python3 -m py_compile "$APP_DIR/ai_edit_import_utility_timecode_exact_json.py"
python3 -m py_compile "$APP_DIR/roseberry_ai_tools_app/run_desktop_app.py"
python3 -m py_compile "$APP_DIR/roseberry_ai_tools_app/roseberry_ai_tools/app.py"
python3 -m py_compile "$APP_DIR/roseberry_ai_tools_app/roseberry_ai_tools/backend_bridge.py"
python3 -m py_compile "$APP_DIR/roseberry_ai_tools_app/roseberry_ai_tools/excel_input_adapter.py"

python3 -m venv "$APP_DIR/roseberry_ai_tools_app/.venv"
"$APP_DIR/roseberry_ai_tools_app/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/roseberry_ai_tools_app/.venv/bin/python" -m pip install -r "$APP_DIR/roseberry_ai_tools_app/requirements.txt"

python3 - <<PY
import json
from pathlib import Path
config_path = Path("$CONFIG_PATH")
data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
data["app_home"] = "$APP_HOME"
data["version"] = "$LATEST_VERSION"
config_path.write_text(json.dumps(data, indent=2) + "\\n", encoding="utf-8")
PY

log "Update complete."
