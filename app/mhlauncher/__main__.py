import argparse
import fcntl
import os
import shutil
import signal
import subprocess
import sys

from .app import App
from .config import APP_VERSION, DATA_DIR

_lock_file = None


def single_instance():
    """Only one launcher may run, otherwise two servers would fight over the same ports."""
    global _lock_file
    os.makedirs(DATA_DIR, exist_ok=True)
    _lock_file = open(os.path.join(DATA_DIR, "launcher.lock"), "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def notify(msg):
    print(msg, file=sys.stderr)
    for cmd in (["zenity", "--info", "--text", msg], ["kdialog", "--msgbox", msg]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, stderr=subprocess.DEVNULL)
            break


def main():
    ap = argparse.ArgumentParser(prog="MHLauncher", description="Marvel Heroes launcher for MHServerEmu")
    ap.add_argument("--fullscreen", action="store_true", help="start fullscreen")
    ap.add_argument("--windowed", action="store_true", help="start in a window")
    ap.add_argument("--version", action="version", version=APP_VERSION)
    args = ap.parse_args()
    if not single_instance():
        notify("MH Launcher is already running.")
        return 1
    app = App()

    # Steam's "Exit game", logout and shutdown send SIGTERM/SIGHUP: stop the server cleanly first
    def on_term(signum, _frame):
        if getattr(on_term, "fired", False):
            return
        on_term.fired = True
        app.running = False
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_term)
    if args.fullscreen or args.windowed:
        app.settings.data["fullscreen"] = bool(args.fullscreen)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app.server.running:
            print("Stopping server…")
            app.server.stop()
        app.bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
