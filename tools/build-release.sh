#!/usr/bin/env bash
# Builds the ready-to-run package: launcher + portable Python (with pygame-ce) + MHServerEmu.
#
#   tools/build-release.sh <MHServerEmu-nightly-…-Release-linux-x64.zip> [version]
#
# Output: dist/MHLauncher-linux-x64.tar.xz
set -euo pipefail

SERVER_ZIP="${1:?usage: $0 <MHServerEmu-linux-x64.zip> [version]}"
VERSION="${2:-dev}"
PBS_TAG="20250918"
PY_VER="3.12.11"
PYGAME_VER="2.5.6"

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
WORK="$ROOT/build"
OUT="$WORK/MHLauncher"
rm -rf "$WORK" && mkdir -p "$OUT" "$ROOT/dist"

echo "==> Copying launcher"
cp -r "$ROOT/MHLauncher.sh" "$ROOT/README.md" "$ROOT/LICENSE" "$ROOT/app" "$ROOT/assets" "$ROOT/tools" "$OUT/"
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
rm -f "$OUT/tools/build-release.sh"

echo "==> Portable Python $PY_VER (python-build-standalone $PBS_TAG)"
PBS="cpython-${PY_VER}+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
curl -fL --retry 3 -o "$WORK/python.tar.gz" \
  "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS}"
mkdir -p "$OUT/runtime" && tar -xzf "$WORK/python.tar.gz" -C "$OUT/runtime"
PY="$OUT/runtime/python/bin/python3"

echo "==> pygame-ce $PYGAME_VER"
WHL="pygame_ce-${PYGAME_VER}-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
curl -fL --retry 3 -o "$WORK/$WHL" \
  "https://github.com/pygame-community/pygame-ce/releases/download/${PYGAME_VER}/${WHL}"
"$PY" -m pip install --no-index --no-warn-script-location "$WORK/$WHL" >/dev/null

echo "==> Trimming runtime"
R="$OUT/runtime/python"
rm -rf "$R"/lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data} \
       "$R"/lib/libtk* "$R"/lib/libtcl* "$R"/lib/tcl8* "$R"/lib/tk8.6 "$R"/lib/itcl* "$R"/lib/thread* \
       "$R"/lib/python3.12/lib-dynload/_tkinter* "$R/share" "$R/include" "$R"/bin/pip* \
       "$R"/lib/python3.12/site-packages/pip "$R"/lib/python3.12/site-packages/pygame/{examples,tests,docs}
find "$R" -name __pycache__ -prune -exec rm -rf {} +

echo "==> MHServerEmu from $(basename "$SERVER_ZIP")"
mkdir -p "$OUT/server"
unzip -q "$SERVER_ZIP" -d "$OUT/server"
# releases sometimes wrap everything in one folder
if [ ! -f "$OUT/server/MHServerEmu.dll" ]; then
  inner="$(dirname "$(find "$OUT/server" -name MHServerEmu.dll | head -1)")"
  mv "$inner"/* "$OUT/server/" && rmdir "$inner" 2>/dev/null || true
fi
chmod +x "$OUT/server/MHServerEmu" 2>/dev/null || true
curl -fsSL -o "$OUT/server/LICENSE.txt" https://raw.githubusercontent.com/Crypto137/MHServerEmu/master/LICENSE \
  || echo "warning: could not fetch MHServerEmu LICENSE (AGPL-3.0) – add it manually"

echo "==> Packing"
echo "$VERSION" > "$OUT/VERSION"
tar -C "$WORK" -cJf "$ROOT/dist/MHLauncher-linux-x64.tar.xz" MHLauncher
ls -lh "$ROOT/dist/MHLauncher-linux-x64.tar.xz"
