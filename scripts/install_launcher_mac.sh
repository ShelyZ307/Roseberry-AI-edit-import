#!/bin/zsh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/Roseberry AI Tools"
SOURCE="$REPO_ROOT/launchers/mac/Roseberry AI Edit Import.py"
TARGET="$TARGET_DIR/Roseberry AI Edit Import.py"
STAMP="$(date +%Y%m%d_%H%M%S)"

if [ ! -f "$SOURCE" ]; then
  echo "Launcher not found: $SOURCE"
  exit 1
fi

mkdir -p "$TARGET_DIR"
if [ -f "$TARGET" ]; then
  cp "$TARGET" "$TARGET.bak.$STAMP"
fi

cp "$SOURCE" "$TARGET"
chmod 644 "$TARGET"

echo "Installed Resolve launcher:"
echo "$TARGET"
echo
echo "Restart DaVinci Resolve, then open:"
echo "Workspace > Scripts > Utility > Roseberry AI Tools > Roseberry AI Edit Import"

