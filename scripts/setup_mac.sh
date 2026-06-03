#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_HOME="${ROSEBERRY_AI_EDIT_IMPORT_HOME:-$HOME/Roseberry/AI Edit Import}"
APP_DIR="$APP_HOME/roseberry_ai_edit_import"
CONFIG_DIR="$HOME/Library/Application Support/Roseberry AI Tools"
CONFIG_PATH="$CONFIG_DIR/config.json"

echo "Roseberry AI Edit Import setup for macOS"
echo "Install folder: $APP_DIR"

mkdir -p "$APP_HOME" "$CONFIG_DIR"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$REPO_ROOT/roseberry_ai_edit_import/" "$APP_DIR/"

python3 --version
python3 -m venv "$APP_DIR/roseberry_ai_tools_app/.venv"
"$APP_DIR/roseberry_ai_tools_app/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/roseberry_ai_tools_app/.venv/bin/python" -m pip install -r "$APP_DIR/roseberry_ai_tools_app/requirements.txt"

cat > "$CONFIG_PATH" <<EOF
{
  "app_home": "$APP_HOME",
  "version": "$(cat "$REPO_ROOT/VERSION")"
}
EOF

echo
echo "Setup complete."
echo "Config: $CONFIG_PATH"
echo "Next: ./scripts/install_launcher_mac.sh"

