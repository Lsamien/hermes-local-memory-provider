#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_PROFILES="${HERMES_PROFILES:-}" # comma separated, e.g. "yaoer,zhuer"
FORCE_CONFIG_OVERWRITE="${FORCE_CONFIG_OVERWRITE:-1}"

render_config() {
  local src="$1"
  local dst="$2"
  local home="$3"
  python3 - <<'PY' "$src" "$dst" "$home"
from pathlib import Path
import sys

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
home = sys.argv[3]
text = src.read_text(encoding="utf-8").replace("${HERMES_HOME}", home)
dst.write_text(text, encoding="utf-8")
PY
}

install_into_home() {
  local target_home="$1"
  mkdir -p "$target_home/plugins/local_memory"
  mkdir -p "$target_home/memory"
  mkdir -p "$target_home/memory/graphiti"
  mkdir -p "$target_home/memory/reflector"
  mkdir -p "$target_home/tools"
  mkdir -p "$target_home/docs"

  cp "$ROOT_DIR/plugins/local_memory/"*.py "$target_home/plugins/local_memory/"
  cp "$ROOT_DIR/plugins/local_memory/plugin.yaml" "$target_home/plugins/local_memory/"
  cp "$ROOT_DIR/memory/__init__.py" "$target_home/memory/__init__.py"
  cp "$ROOT_DIR/memory/graphiti/"*.py "$target_home/memory/graphiti/"
  cp "$ROOT_DIR/memory/reflector/"*.py "$target_home/memory/reflector/"
  cp "$ROOT_DIR/tools/upgrade_check.py" "$target_home/tools/upgrade_check.py"
  cp "$ROOT_DIR/docs/rollback-local-memory.md" "$target_home/docs/rollback-local-memory.md"

  local cfg="$target_home/plugins/local_memory/config.yaml"
  if [[ -f "$cfg" ]]; then
    cp "$cfg" "${cfg}.bak.$(date +%Y%m%d_%H%M%S)"
  fi
  if [[ "$FORCE_CONFIG_OVERWRITE" == "1" || ! -f "$cfg" ]]; then
    render_config "$ROOT_DIR/examples/config.yaml" "$cfg" "$target_home"
  fi

  chmod +x "$target_home/tools/upgrade_check.py"
}

configure_provider() {
  local maybe_profile="$1"
  local target_home="$2"
  local cfg_path="$target_home/plugins/local_memory/config.yaml"
  if [[ -n "$maybe_profile" ]]; then
    hermes --profile "$maybe_profile" config set memory.provider local_memory || true
    hermes --profile "$maybe_profile" config set memory.local_memory.config_path "$cfg_path" || true
  else
    hermes config set memory.provider local_memory || true
    hermes config set memory.local_memory.config_path "$cfg_path" || true
  fi
}

install_into_home "$HERMES_HOME"
configure_provider "" "$HERMES_HOME"
echo "Installed local_memory into default home: $HERMES_HOME"

if [[ -n "$HERMES_PROFILES" ]]; then
  IFS=',' read -r -a profiles <<< "$HERMES_PROFILES"
  for p in "${profiles[@]}"; do
    p="$(echo "$p" | xargs)"
    [[ -z "$p" ]] && continue
    profile_home="$HERMES_HOME/profiles/$p"
    mkdir -p "$profile_home"
    install_into_home "$profile_home"
    configure_provider "$p" "$profile_home"
    echo "Installed local_memory into profile '$p': $profile_home"
  done
fi

echo "Done."
