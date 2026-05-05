#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$HERMES_HOME/plugins/local_memory"
mkdir -p "$HERMES_HOME/memory"
mkdir -p "$HERMES_HOME/memory/graphiti"
mkdir -p "$HERMES_HOME/memory/reflector"
mkdir -p "$HERMES_HOME/tools"
mkdir -p "$HERMES_HOME/docs"

cp "$ROOT_DIR/plugins/local_memory/"*.py "$HERMES_HOME/plugins/local_memory/"
cp "$ROOT_DIR/plugins/local_memory/plugin.yaml" "$HERMES_HOME/plugins/local_memory/"
cp "$ROOT_DIR/memory/__init__.py" "$HERMES_HOME/memory/__init__.py"
cp "$ROOT_DIR/memory/graphiti/"*.py "$HERMES_HOME/memory/graphiti/"
cp "$ROOT_DIR/memory/reflector/"*.py "$HERMES_HOME/memory/reflector/"
cp "$ROOT_DIR/tools/upgrade_check.py" "$HERMES_HOME/tools/upgrade_check.py"
cp "$ROOT_DIR/docs/rollback-local-memory.md" "$HERMES_HOME/docs/rollback-local-memory.md"
python3 - <<'PY' "$ROOT_DIR/examples/config.yaml" "$HERMES_HOME/plugins/local_memory/config.yaml" "$HERMES_HOME"
from pathlib import Path
import sys

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
home = sys.argv[3]
text = src.read_text(encoding="utf-8").replace("${HERMES_HOME}", home)
dst.write_text(text, encoding="utf-8")
PY
chmod +x "$HERMES_HOME/tools/upgrade_check.py"

echo "Installed to $HERMES_HOME"
