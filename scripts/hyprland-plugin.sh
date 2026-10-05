#!/usr/bin/env bash
# cybr-proof: build/load the cua Hyprland compositor plugin, rebuilding when Hyprland's ABI changed.
#   hyprland-plugin.sh ensure   # load (build first if missing or stale) — safe to run at every start
#   hyprland-plugin.sh build    # force rebuild
#   hyprland-plugin.sh status
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

SRC_GLOB="$HOME/.local/opt/cua-hypr/cua-hyprland-plugin-*"
SRC=$(ls -d $SRC_GLOB/ 2>/dev/null | sed 's:/$::' | sort -V | tail -1 || true)
[[ -n "$SRC" ]] || { echo "cua-hyprland-plugin source not found under ~/.local/opt/cua-hypr (run scripts/install.sh)"; exit 1; }
SO="$SRC/build/cua-hyprland-plugin.so"
STAMP="$SRC/build/.hyprland-version"
HYPR_VER=$(hyprctl version -j 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tag") or d.get("commit"))' 2>/dev/null || pkg-config --modversion hyprland)

build() {
  command -v cmake >/dev/null || uv tool install cmake >/dev/null
  command -v ninja >/dev/null || uv tool install ninja >/dev/null
  cmake -S "$SRC" -B "$SRC/build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCUA_HYPRLAND_INPUT=ON -DBUILD_TESTING=OFF >/dev/null
  cmake --build "$SRC/build" >/dev/null
  echo "$HYPR_VER" > "$STAMP"
  echo "built $SO for Hyprland $HYPR_VER"
}

loaded() { hyprctl plugin list 2>/dev/null | grep -q cua-hyprland-plugin; }
ready()  { hyprctl 'cua:status' 2>/dev/null | grep -q 'transport: ready'; }

case "${1:-ensure}" in
  build) build ;;
  status) hyprctl 'cua:status' 2>/dev/null || echo "not loaded" ;;
  ensure)
    if loaded && ready; then echo "cua hyprland plugin ready"; exit 0; fi
    if [[ ! -x "$SO" || "$(cat "$STAMP" 2>/dev/null)" != "$HYPR_VER" ]]; then
      if loaded; then
        # the running copy predates this check: stamp it rather than replacing a working plugin mid-session
        ready && { echo "$HYPR_VER" > "$STAMP"; echo "cua hyprland plugin ready"; exit 0; }
      fi
      build
    fi
    if loaded && ! ready; then
      # replacement load after an unload: the plugin keeps a seat-lifetime marker for the desktop session;
      # it documents that a same-user process may clear it (trusted-local guard, not a security boundary)
      hyprctl plugin unload "$SO" >/dev/null 2>&1 || true; sleep 1
      inst="${XDG_RUNTIME_DIR:-/run/user/$UID}/hypr/${HYPRLAND_INSTANCE_SIGNATURE:-}"
      rmdir "$inst/cua-input-seat-lifetime" 2>/dev/null || true
      rm -f "$inst"/cua-input-v3*.sock "$inst"/cua-inject-v2.sock 2>/dev/null || true
    fi
    if ! loaded; then
      out=$(hyprctl plugin load "$SO" 2>&1) || true
      echo "$out" | grep -qi 'ok' || { echo "load failed: $out"; [[ "$out" == *mismatch* ]] && { build; hyprctl plugin load "$SO"; }; }
    fi
    ready || { hyprctl reload >/dev/null 2>&1 || true; sleep 1; }
    ready && echo "cua hyprland plugin ready" || { echo "plugin loaded but transport not ready:"; hyprctl 'cua:status' 2>/dev/null | sed -n 2,5p; exit 2; }
    ;;
  *) echo "usage: $0 ensure|build|status"; exit 1 ;;
esac
