#!/usr/bin/env bash
# cybr-proof installer — idempotent, update-proof. Re-run any time (after `hermes update`, Hyprland update, new profile).
#   scripts/install.sh            # default profile + every profile under $HERMES_HOME/profiles
#   scripts/install.sh --no-hypr  # skip the Hyprland/cua-driver part (macOS, X11, or you don't want it)
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
REPO=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${HERMES_HOME:-$HOME/.hermes}
# profiles are laid out as <root>/profiles/<name>; if HERMES_HOME points at a profile, climb to the root
[[ $(basename "$(dirname "$ROOT")") == profiles ]] && ROOT=$(dirname "$(dirname "$ROOT")")
DO_HYPR=1; [[ "${1:-}" == "--no-hypr" ]] && DO_HYPR=0
CUA_VER=${CUA_VER:-0.33.3}

homes=("$ROOT")
for p in "$ROOT"/profiles/*/; do [[ -f "$p/config.yaml" ]] && homes+=("${p%/}"); done

install_into() {
  local home=$1 name
  name=$(basename "$home"); [[ "$home" == "$ROOT" ]] && name=default
  mkdir -p "$home/plugins"
  ln -sfn "$REPO" "$home/plugins/cybr-proof"
  ln -sfn "$REPO/contrib/cua-driver-shim" "$home/plugins/cua-driver-shim"
  local prof=(); [[ "$name" != default ]] && prof=(--profile "$name")
  hermes "${prof[@]}" plugins enable cybr-proof >/dev/null 2>&1 || true
  hermes "${prof[@]}" plugins enable cua-driver-shim >/dev/null 2>&1 || true
  if [[ $DO_HYPR == 1 && -n "${WAYLAND_DISPLAY:-}" ]]; then
    hermes "${prof[@]}" config set computer_use.native_wayland true >/dev/null 2>&1 || true
  fi
  echo "  ✓ profile $name: plugins linked + enabled"
}

echo "cybr-proof → $ROOT"
for h in "${homes[@]}"; do install_into "$h"; done

if [[ $DO_HYPR == 1 && "$(uname -s)" == Linux && -n "${WAYLAND_DISPLAY:-}" ]] && command -v hyprctl >/dev/null; then
  echo "Hyprland detected — cua-driver $CUA_VER + compositor plugin"
  DRV_DIR=$HOME/.local/opt/cua-driver-$CUA_VER
  if [[ ! -x "$DRV_DIR/cua-driver-rs-$CUA_VER-linux-x86_64/cua-driver" ]]; then
    mkdir -p "$DRV_DIR" && cd "$DRV_DIR"
    gh release download "cua-driver-rs-v$CUA_VER" -R trycua/cua -p "cua-driver-rs-$CUA_VER-linux-x86_64.tar.gz" --clobber
    tar xzf "cua-driver-rs-$CUA_VER-linux-x86_64.tar.gz"; chmod +x "cua-driver-rs-$CUA_VER-linux-x86_64/cua-driver"
    "cua-driver-rs-$CUA_VER-linux-x86_64/cua-driver" telemetry disable >/dev/null 2>&1 || true
  fi
  echo "  ✓ cua-driver $CUA_VER at $DRV_DIR"
  if ! ls -d "$HOME"/.local/opt/cua-hypr/cua-hyprland-plugin-* >/dev/null 2>&1; then
    mkdir -p "$HOME/.local/opt/cua-hypr" && cd "$HOME/.local/opt/cua-hypr"
    gh release download "cua-driver-rs-v$CUA_VER" -R trycua/cua -p "cua-hyprland-plugin-$CUA_VER-*[0-9a-f].tar.gz" --clobber
    tar xzf cua-hyprland-plugin-*.tar.gz
  fi
  HCONF=$HOME/.config/hypr
  grep -q 'plugin = { cua = { enabled = true } }' "$HCONF/hyprland.lua" 2>/dev/null || \
    printf '\n-- Cua computer-use driver: Hyprland input/discovery plugin transport (cybr-proof).\nhl.config({ plugin = { cua = { enabled = true } } })\n' >> "$HCONF/hyprland.lua"
  # one autostart line, pointing at the self-rebuilding loader (not a fixed .so path)
  sed -i '/cua-hyprland-plugin.so\|hyprland-plugin.sh/d;/Cua computer-use Hyprland input plugin/d;/cmake -S ~\/.local\/opt\/cua-hypr/d' "$HCONF/autostart.lua" 2>/dev/null || true
  printf '\n-- Cua computer-use Hyprland input plugin (cybr-proof); rebuilds itself after Hyprland updates.\no.exec_on_start("bash %s/scripts/hyprland-plugin.sh ensure")\n' "$REPO" >> "$HCONF/autostart.lua"
  bash "$REPO/scripts/hyprland-plugin.sh" ensure
fi

echo
echo "done. Restart the Hermes desktop app / gateway so the plugins load."
echo "verify: hermes proof --help ; hyprctl cua:status ; cybr_proof(action='status') in chat"
