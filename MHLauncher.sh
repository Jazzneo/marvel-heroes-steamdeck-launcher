#!/usr/bin/env bash
# MH Launcher – Linux / Steam Deck launcher for MHServerEmu (Marvel Heroes)
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LOGDIR="${XDG_DATA_HOME:-$HOME/.local/share}/MHLauncher/logs"
mkdir -p "$LOGDIR"

PY="$DIR/runtime/python/bin/python3"
if [ ! -x "$PY" ]; then
    if command -v python3 >/dev/null 2>&1 && python3 -c "import pygame" >/dev/null 2>&1; then
        PY="python3"
    else
        MSG="MH Launcher's bundled Python runtime is missing. Re-extract the full MHLauncher-linux-x64.tar.xz archive."
        echo "$MSG" >&2
        (command -v zenity >/dev/null && zenity --error --text="$MSG") || \
        (command -v kdialog >/dev/null && kdialog --error "$MSG") || true
        exit 1
    fi
fi

chmod +x "$DIR/tools/"*.sh 2>/dev/null
cd "$DIR/app" || exit 1
# Keep a log when started from Steam / a desktop icon (no terminal attached)
if [ -t 1 ]; then
    exec "$PY" -s -m mhlauncher "$@"
else
    exec "$PY" -s -m mhlauncher "$@" >"$LOGDIR/launcher.log" 2>&1
fi
