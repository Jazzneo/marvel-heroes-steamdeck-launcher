"""Main application: window, input, modals, background tasks."""
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback

import pygame

from . import ui
from .ui import C, W, H, text, rrect, panel, glyph
from .authproxy import LoginBridge, ports_allowed, lan_ip
from .config import Settings, in_gaming_mode, APP_VERSION, LOG_DIR, ASSETS_DIR
from .server import ServerManager, PortConflict, stop_foreign_server, find_mhserveremu_pids

class BridgeBlocked(RuntimeError):
    """The login bridge cannot use ports 80/443. .servers = detected web servers [(pid, name, unit)]."""

    def __init__(self, message, servers=()):
        super().__init__(message)
        self.servers = list(servers)


SIDEBAR_W = 268
CONTENT = pygame.Rect(SIDEBAR_W + 24, 96, W - SIDEBAR_W - 48, H - 96 - 64)


# =================================================================== modals
class Modal:
    hints = [("A", "Select"), ("B", "Back")]
    width, height = 760, 420

    def __init__(self, app):
        self.app = app
        self.focus_key = None
        self.rect = pygame.Rect(0, 0, self.width, self.height)
        self.rect.center = (W // 2, H // 2)

    def build(self):
        return []

    def draw_body(self, surf):
        pass

    def back(self):
        self.app.close_modal(self)

    def on_button(self, b):
        return False

    def on_event(self, ev):
        return False


class Dialog(Modal):
    def __init__(self, app, title, message, buttons, on_back=None, width=760):
        self.width = width
        lines = ui.wrap(message, 19, width - 80)
        self.height = 190 + len(lines) * 27
        super().__init__(app)
        self.title, self.message, self.buttons, self.on_back = title, message, buttons, on_back
        # controller focus starts on the main (primary) action, e.g. "Fix it for me"
        prim = [i for i, b in enumerate(buttons) if b[2] == "primary"]
        self.focus_key = f"dlg{prim[-1] if prim else len(buttons) - 1}"

    def build(self):
        ws = []
        n = len(self.buttons)
        bw = min(260, (self.rect.w - 60 - (n - 1) * 16) // n)
        x = self.rect.right - 30 - n * bw - (n - 1) * 16
        for i, (label, cb, style) in enumerate(self.buttons):
            def click(cb=cb):
                self.app.close_modal(self)
                if cb:
                    cb()
            ws.append(ui.Button(f"dlg{i}", (x + i * (bw + 16), self.rect.bottom - 86, bw, 58), label,
                                click, style))
        return ws

    def draw_body(self, surf):
        text(surf, self.title, (self.rect.x + 36, self.rect.y + 30), 28, C["text"], True)
        ui.text_block(surf, self.message, (self.rect.x + 36, self.rect.y + 80), self.rect.w - 72, 19,
                      C["muted"])

    def back(self):
        self.app.close_modal(self)
        if self.on_back:
            self.on_back()


class Progress(Modal):
    hints = []

    def __init__(self, app, title, show_log=False, cancel=None):
        self.height = 470 if show_log else 260
        super().__init__(app)
        self.title, self.show_log, self.cancel = title, show_log, cancel
        self.status, self.fraction = "Working…", None

    def build(self):
        if self.cancel:
            return [ui.Button("pcancel", (self.rect.right - 210, self.rect.bottom - 80, 180, 54),
                              "Cancel", self.cancel, "ghost")]
        return []

    def back(self):
        if self.cancel:
            self.cancel()

    def draw_body(self, surf):
        text(surf, self.title, (self.rect.x + 36, self.rect.y + 30), 28, C["text"], True)
        text(surf, self.status, (self.rect.x + 36, self.rect.y + 82), 19, C["muted"], max_w=self.rect.w - 72)
        bar = pygame.Rect(self.rect.x + 36, self.rect.y + 124, self.rect.w - 72, 12)
        rrect(surf, C["bg2"], bar, 6)
        if self.fraction is None:
            t = time.time() * 1.3 % 1
            seg = pygame.Rect(0, bar.y, bar.w // 4, bar.h)
            seg.x = bar.x + int((bar.w - seg.w) * (0.5 - 0.5 * __import__("math").cos(t * 6.283)))
            rrect(surf, C["accent"], seg, 6)
        else:
            f = bar.copy()
            f.w = max(12, int(bar.w * max(0.0, min(1.0, self.fraction))))
            rrect(surf, C["accent"], f, 6)
        if self.show_log:
            box = pygame.Rect(self.rect.x + 36, self.rect.y + 156, self.rect.w - 72,
                              self.rect.h - 156 - (100 if self.cancel else 36))
            rrect(surf, C["bg"], box, 10)
            lines = self.app.server.log[-9:]
            y = box.y + 10
            for ln in lines:
                text(surf, ln, (box.x + 12, y), 14, C["dim"], max_w=box.w - 24)
                y += 20


class Keyboard(Modal):
    """On-screen keyboard usable with D-pad + A (also accepts a real keyboard)."""
    width, height = 1000, 560
    ROWS = ["1234567890", "qwertyuiop", "asdfghjkl@", "zxcvbnm._-"]
    hints = [("A", "Type"), ("X", "Delete"), ("Y", "Shift"), ("START", "Done"), ("B", "Cancel")]

    def __init__(self, app, label, value, on_done, password=False, on_submit=None):
        super().__init__(app)
        self.rect.centery = H // 2 + 20
        self.label, self.value, self.original = label, value or "", value or ""
        self.on_done, self.password, self.on_submit = on_done, password, on_submit
        self.shift, self.reveal = False, False
        self.focus_key = "k1:0"
        pygame.key.start_text_input()

    def close(self, accept):
        pygame.key.stop_text_input()
        self.app.close_modal(self)
        if accept:
            self.on_done(self.value)
            if self.on_submit:
                self.on_submit()

    def back(self):
        self.close(False)

    def type(self, ch):
        if len(self.value) < 64:
            self.value += ch.upper() if self.shift else ch
            if self.shift and ch.isalpha():
                self.shift = False

    def build(self):
        ws = []
        kx, ky, kw, kh, gap = self.rect.x + 30, self.rect.y + 150, 86, 62, 8
        for r, row in enumerate(self.ROWS):
            for c, ch in enumerate(row):
                label = ch.upper() if self.shift else ch
                ws.append(ui.Button(f"k{r}:{c}", (kx + c * (kw + gap), ky + r * (kh + gap), kw, kh),
                                    label, lambda ch=ch: self.type(ch), "secondary", size=24))
        y = ky + 4 * (kh + gap)
        ws.append(ui.Button("kshift", (kx, y, 180, kh), "⇧ Shift" if not self.shift else "⇧ SHIFT",
                            lambda: setattr(self, "shift", not self.shift),
                            "primary" if self.shift else "ghost", size=20))
        ws.append(ui.Button("kspace", (kx + 188, y, 300, kh), "Space", lambda: self.type(" "), "ghost", size=20))
        ws.append(ui.Button("kdel", (kx + 496, y, 170, kh), "⌫ Delete",
                            lambda: setattr(self, "value", self.value[:-1]), "ghost", size=20))
        ws.append(ui.Button("kdone", (kx + 674, y, 266, kh), "Done", lambda: self.close(True), "primary", size=22))
        if self.password:
            ws.append(ui.Button("kreveal", (self.rect.right - 150, self.rect.y + 66, 120, 52),
                                "Hide" if self.reveal else "Show",
                                lambda: setattr(self, "reveal", not self.reveal), "ghost", size=18))
        return ws

    def draw_body(self, surf):
        text(surf, self.label, (self.rect.x + 30, self.rect.y + 26), 22, C["muted"], True)
        box = pygame.Rect(self.rect.x + 30, self.rect.y + 66, self.rect.w - 60 - (130 if self.password else 0), 56)
        rrect(surf, C["bg2"], box, 12)
        rrect(surf, C["accent"], box, 12, 2)
        shown = self.value if (not self.password or self.reveal) else "•" * len(self.value)
        r = text(surf, shown, (box.x + 18, box.centery), 24, C["text"], False, "midleft", box.w - 40)
        if int(time.time() * 2) % 2 == 0:
            x = r.right + 3 if shown else box.x + 18
            pygame.draw.line(surf, C["text"], (x, box.y + 14), (x, box.bottom - 14), 2)

    def on_button(self, b):
        if b == "x":
            self.value = self.value[:-1]
        elif b == "y":
            self.shift = not self.shift
        elif b == "start":
            self.close(True)
        else:
            return False
        return True

    def on_event(self, ev):
        if ev.type == pygame.TEXTINPUT:
            for ch in ev.text:
                if ch.isprintable() and len(self.value) < 64:
                    self.value += ch
            return True
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_BACKSPACE:
                self.value = self.value[:-1]
                return True
            if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                # Enter on a real keyboard = done, unless keyboard focus is on a key button
                self.close(True)
                return True
            if ev.key == pygame.K_ESCAPE:
                self.close(False)
                return True
            if ev.unicode and ev.unicode.isprintable() and not (ev.mod & pygame.KMOD_CTRL):
                return True  # handled by TEXTINPUT; don't treat letters as navigation
            if ev.key == pygame.K_v and ev.mod & pygame.KMOD_CTRL:
                try:
                    clip = pygame.scrap.get_text() if hasattr(pygame, "scrap") else ""
                    self.value += (clip or "").strip()[:64 - len(self.value)]
                except Exception:
                    pass
                return True
        return False


class FileBrowser(Modal):
    width, height = 1100, 700
    hints = [("A", "Open"), ("B", "Up / Close"), ("Y", "Select folder")]

    def __init__(self, app, title, start, exts, on_pick, pick_dirs=False):
        super().__init__(app)
        self.title, self.exts, self.on_pick, self.pick_dirs = title, exts, on_pick, pick_dirs
        self.index = 0
        self.path = start if start and os.path.isdir(start) else os.path.expanduser("~")
        self.show_hidden = False
        self.entries = []
        self.load()
        self.focus_key = "fb_list"

    def load(self):
        try:
            names = os.listdir(self.path)
        except OSError as e:
            self.app.toast(f"Cannot open folder: {e.strerror}", "error")
            names = []
        dirs, files = [], []
        for n in names:
            if n.startswith(".") and not self.show_hidden:
                continue
            p = os.path.join(self.path, n)
            if os.path.isdir(p):
                dirs.append(n)
            elif not self.exts or n.lower().endswith(self.exts):
                files.append(n)
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        self.entries = [("..", True)] if os.path.dirname(self.path) != self.path else []
        self.entries += [(d, True) for d in dirs] + [(f, False) for f in files]
        self.index = 0

    def go(self, path):
        if os.path.isdir(path):
            old = self.path
            self.path = os.path.realpath(path)
            self.load()
            # when going up, select the folder we came from
            if os.path.dirname(old) == self.path:
                for i, (n, _d) in enumerate(self.entries):
                    if n == os.path.basename(old):
                        self.index = i

    def open_index(self, i):
        name, is_dir = self.entries[i]
        p = os.path.join(self.path, name)
        if is_dir:
            self.go(os.path.dirname(self.path) if name == ".." else p)
        else:
            self.app.close_modal(self)
            self.on_pick(p)

    def back(self):
        parent = os.path.dirname(self.path)
        if parent != self.path and self.path != os.path.expanduser("~"):
            self.go(parent)
        else:
            self.app.close_modal(self)

    def pick_folder(self):
        self.app.close_modal(self)
        self.on_pick(self.path)

    def on_button(self, b):
        if b == "y" and self.pick_dirs:
            self.pick_folder()
            return True
        return False

    def build(self):
        from .client import steam_libraries
        r = self.rect
        ws = []
        places = [("Home", os.path.expanduser("~"))]
        libs = steam_libraries()
        if libs:
            places.append(("Steam games", os.path.join(libs[0], "steamapps", "common")))
        sd = [p for p in ("/run/media/deck", "/run/media/" + os.environ.get("USER", "deck"), "/run/media")
              if os.path.isdir(p)]
        if sd:
            places.append(("SD card / USB", sd[0]))
        places.append(("Downloads", os.path.expanduser("~/Downloads")))
        places.append(("Games", os.path.expanduser("~/Games")))
        y = r.y + 100
        for i, (label, p) in enumerate(places):
            ws.append(ui.Button(f"fb_place{i}", (r.x + 24, y, 230, 56), label,
                                lambda p=p: self.go(p), "ghost", enabled=os.path.isdir(p), size=19,
                                align="left"))
            y += 64
        ws.append(ui.Toggle("fb_hidden", (r.x + 24, y + 6, 230, 56), "Hidden files", self.show_hidden,
                            lambda v: (setattr(self, "show_hidden", v), self.load())))

        def render(surf, rr, item, sel, focused):
            name, is_dir = item
            icon = "▸" if is_dir else "◆"
            col = C["gold"] if (not is_dir and name.lower().endswith(".exe")) else C["text"]
            text(surf, icon, (rr.x + 18, rr.centery), 16, C["muted"] if is_dir else col, False, "midleft")
            text(surf, name + ("/" if is_dir and name != ".." else ""), (rr.x + 44, rr.centery), 19,
                 col if sel or not is_dir else C["muted"] if name == ".." else C["text"], sel, "midleft",
                 rr.w - 60)

        ws.append(ui.ListView("fb_list", (r.x + 276, r.y + 100, r.w - 300, r.h - 200), self.entries,
                              self.index, lambda i: setattr(self, "index", i), self.open_index, 52,
                              render, "Empty folder"))
        bx = r.right - 24
        ws.append(ui.Button("fb_cancel", (bx - 180, r.bottom - 82, 180, 56), "Cancel",
                            lambda: self.app.close_modal(self), "ghost"))
        if self.pick_dirs:
            ws.append(ui.Button("fb_pickdir", (bx - 180 - 16 - 300, r.bottom - 82, 300, 56),
                                "Use this folder", self.pick_folder, "primary"))
        return ws

    def draw_body(self, surf):
        text(surf, self.title, (self.rect.x + 24, self.rect.y + 24), 26, C["text"], True)
        text(surf, self.path, (self.rect.x + 24, self.rect.y + 64), 16, C["muted"], max_w=self.rect.w - 48)


# ===================================================================== app
class App:
    def __init__(self, screenshot_mode=False):
        self.settings = Settings()
        self.server = ServerManager(self.settings)
        host, port = self.server.web_endpoint()
        self.bridge = LoginBridge(host, port)
        self.screenshot_mode = screenshot_mode
        self.modals = []
        self.toasts = []
        self.focus_key = None
        self._mainq = []
        self._mainq_lock = threading.Lock()
        self.game_proc = None
        self.game_started = 0
        self.touch_activates = True
        self.running = True
        self.held = None
        self.held_next = 0
        self.axis_dir = None
        self.controllers = []
        self.last_mouse = (0, 0)
        self.quitting = False

        from . import screens
        self.screens = [screens.PlayScreen(self), screens.AccountsScreen(self), screens.ServerScreen(self),
                        screens.ClientScreen(self), screens.SettingsScreen(self)]
        self.screen_i = 0
        # first run: install the server that ships with the launcher
        try:
            self.server.install_bundled()
        except Exception as e:
            self.toast(f"Could not install bundled server: {e}", "error")

    # --------------------------------------------------------- helpers
    def runners_cache(self):
        if getattr(self, "_runners", None) is None:
            from .client import detect_runners
            self._runners = detect_runners()
        return self._runners

    def refresh_runners(self):
        self._runners = None
        n = len([r for r in self.runners_cache() if r.kind != "custom"])
        self.toast(f"Found {n} Proton/Wine option(s)" if n else "No Proton or Wine found. Install Proton "
                   "from Steam (Library → Tools) or GE-Proton.", "ok" if n else "warn", 6)

    @property
    def screen(self):
        return self.screens[self.screen_i]

    def go(self, index_or_name):
        if isinstance(index_or_name, str):
            for i, s in enumerate(self.screens):
                if s.name == index_or_name:
                    index_or_name = i
        self.screen_i = index_or_name
        self.focus_key = self.screen.default_focus

    def call_soon(self, fn):
        with self._mainq_lock:
            self._mainq.append(fn)

    def toast(self, msg, kind="info", secs=4.5):
        self.toasts.append([msg, kind, time.time() + secs])
        del self.toasts[:-3]

    def open_modal(self, m):
        m.prev_focus = self.focus_key
        self.modals.append(m)
        self.focus_key = m.focus_key

    def close_modal(self, m):
        if m in self.modals:
            self.modals.remove(m)
            self.focus_key = getattr(m, "prev_focus", None)

    def dialog(self, title, message, buttons=None, on_back=None, width=760):
        buttons = buttons or [("OK", None, "primary")]
        self.open_modal(Dialog(self, title, message, buttons, on_back, width))

    def open_keyboard(self, label, value, on_done, password=False, on_submit=None):
        self.open_modal(Keyboard(self, label, value, on_done, password, on_submit))

    def browse(self, title, start, exts, on_pick, pick_dirs=False):
        self.open_modal(FileBrowser(self, title, start, exts, on_pick, pick_dirs))

    def run_task(self, title, fn, on_done=None, show_log=False, cancellable=False, quiet=False):
        """Run fn(progress) in a thread with a progress modal; on_done(result) on the main thread."""
        cancel_ev = threading.Event()
        modal = None
        if not quiet:
            modal = Progress(self, title, show_log, cancel=(lambda: cancel_ev.set()) if cancellable else None)
            self.open_modal(modal)

        def progress(frac, status=None):
            if modal:
                modal.fraction = frac
                if status:
                    modal.status = status
        progress.cancelled = cancel_ev

        def worker():
            try:
                res, err = fn(progress), None
            except Exception as e:
                traceback.print_exc()
                res, err = None, e

            def finish():
                if modal:
                    self.close_modal(modal)
                if isinstance(err, PortConflict) and err.mh_pids:
                    retry = lambda: self.run_task(title, fn, on_done, show_log, cancellable, quiet)
                    self.offer_stop_leftover(err.mh_pids, then=retry, reason=str(err))
                elif isinstance(err, BridgeBlocked):
                    retry = lambda: self.run_task(title, fn, on_done, show_log, cancellable, quiet)
                    self.offer_port_fix(str(err), err.servers, then=retry)
                elif isinstance(err, PortConflict):
                    self.dialog("Port already in use", str(err) + "\n\nClose that program (or reboot) and try again.")
                elif err is not None:
                    self.dialog("Something went wrong", str(err) or err.__class__.__name__)
                elif on_done:
                    on_done(res)
            self.call_soon(finish)
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------- server / bridge
    def server_lan_ip(self):
        return lan_ip() if self.settings.get("lan_mode") else None

    def start_server_blocking(self, progress, wait_online=True, timeout=300):
        """Worker-thread helper: start bridge + server and optionally wait until online."""
        from . import dotnet
        if not dotnet.find_dotnet()[0]:
            progress(None, "Installing .NET 8 runtime (one time)…")
            dotnet.install(lambda f, s: progress(f, s))
        client_root = self.settings.get("client_root")
        if client_root and not self.server.game_data_ok():
            progress(None, "Copying game data from your client…")
            self.server.sync_game_data(client_root)
        if not self.bridge.running or self.bridge.http_port != 80:
            self.bridge.start(lan=self.settings.get("lan_mode"))
        if not self.server.running:
            progress(None, "Starting MHServerEmu…")
            if not self.server.start(self.server_lan_ip()):
                raise RuntimeError(self.server.message)
            # the server may have moved to another web port (e.g. Steam holds 8080)
            self.bridge.web_host, self.bridge.web_port = self.server.web_endpoint()
        if not wait_online:
            return True
        t0 = time.time()
        while time.time() - t0 < timeout:
            if getattr(progress, "cancelled", None) and progress.cancelled.is_set():
                return False
            st = self.server.state
            if st == "Online":
                return True
            if self.server.fatal:
                # give the reader a moment to collect the rest of the error, then report it
                time.sleep(0.5)
                raise RuntimeError(self.server.fatal_message())
            if not self.server.running:
                raise RuntimeError(self.server.message or "Server stopped while starting. See the Server tab log.")
            last = self.server.log[-1] if self.server.log else ""
            progress(None, f"Starting server… {int(time.time() - t0)}s   {last[-70:]}")
            time.sleep(0.3)
        raise RuntimeError("Server did not come online in time. See the Server tab log.")

    def offer_port_fix(self, message, servers=(), then=None):
        """Explain a port problem and offer a one-click fix (one password prompt via pkexec)."""
        from . import integration
        servers = list(servers)
        names = sorted({n for _p, n, _u in servers})
        msg = message
        if names:
            msg += (f"\n\nFound {', '.join(names)} running and using the web ports. "
                    f"'Fix it for me' will stop {'it' if len(names) == 1 else 'them'} and turn off "
                    f"{'its' if len(names) == 1 else 'their'} autostart, then open the ports for the launcher.")
        else:
            msg += "\n\n'Fix it for me' opens ports 80/443 for the launcher (one time)."

        def fix():
            def work(p):
                p(None, "Waiting for your password…")
                ok, info = integration.try_enable_ports_graphically(servers)
                if ok:
                    p(None, "Checking ports…")
                    time.sleep(1.5)
                    self.bridge.stop()
                return ok, info

            def done(res):
                ok, info = res
                if ok:
                    self.toast("Ports fixed" + (f" – {', '.join(names)} stopped" if names else ""), "ok", 6)
                    if then:
                        then()
                else:
                    self.dialog("Couldn't fix it automatically",
                                f"{info}\n\n" + integration.manual_fix_instructions(servers), width=1000)
            self.run_task("Fixing login ports", work, done)
        self.dialog("Login ports blocked" if names else "Login ports need a one-time setup", msg,
                    [("Cancel", None, "ghost"), ("Fix it for me", fix, "primary")], width=960)

    def offer_stop_leftover(self, pids, then=None, reason=None):
        """Ask to shut down MHServerEmu processes this launcher did not start (e.g. left over)."""
        msg = (reason + "\n\n" if reason else
               f"An MHServerEmu server is already running in the background (pid {', '.join(map(str, pids))}). ")
        msg += ("It was probably left over from an earlier session. The launcher can close it "
                "(cleanly, so progress is saved) and then continue.")

        def stop():
            def work(p):
                for pid in pids:
                    p(None, f"Shutting down old server (pid {pid})…")
                    ok, _graceful = stop_foreign_server(pid)
                    if not ok:
                        raise RuntimeError(f"Could not stop process {pid}. Try rebooting, or run: kill -9 {pid}")
                time.sleep(1)  # let the OS release the ports
                return True
            self.run_task("Stopping old server", work,
                          lambda _r: (self.toast("Old server stopped", "ok"), then and then()))
        self.dialog("Old copy still running", msg,
                    [("Cancel", None, "ghost"), ("Stop it" + (" & continue" if then else ""), stop, "primary")],
                    width=900)

    def toggle_server(self):
        if self.server.running:
            self.run_task("Stopping server", lambda p: (p(None, "Saving players and shutting down…"),
                                                        self.server.stop(), self.bridge.stop()),
                          lambda _r: self.toast("Server stopped", "ok"), show_log=True)
        else:
            def done(ok):
                if ok:
                    self.toast("Server is online", "ok")
                    if self.bridge.error:
                        self.toast(self.bridge.error, "warn", 8)
            self.run_task("Starting server", lambda p: self.start_server_blocking(p), done,
                          show_log=True, cancellable=True)

    # ----------------------------------------------------------- game
    @property
    def game_running(self):
        return self.game_proc is not None and self.game_proc.poll() is None

    def stop_game(self):
        if self.game_running:
            import signal
            try:
                os.killpg(self.game_proc.pid, signal.SIGTERM)
            except Exception:
                pass

    # ----------------------------------------------------------- quit
    def request_quit(self):
        if self.quitting:
            return
        if self.server.running and self.settings.get("stop_server_on_exit"):
            msg = "The server will be shut down safely (player data is saved)."
            if self.game_running:
                msg = "The game is still running. " + msg
            self.dialog("Quit launcher?", msg, [("Cancel", None, "ghost"), ("Quit", self._quit_now, "danger")])
        elif self.server.running:
            self.dialog("Quit launcher?", "The server keeps running only while the launcher is open, "
                        "so it will be shut down safely now.",
                        [("Cancel", None, "ghost"), ("Quit", self._quit_now, "danger")])
        else:
            self._quit_now()

    def _quit_now(self):
        self.quitting = True

        def work(p):
            if self.server.running:
                p(None, "Saving players and shutting down the server…")
                self.server.stop()
            self.bridge.stop()
        self.run_task("Closing", work, lambda _r: setattr(self, "running", False), show_log=True)

    # ---------------------------------------------------------- input
    def _widgets(self):
        if self.modals:
            return self.modals[-1].build()
        return self.sidebar_widgets() + self.screen.build()

    def _focused(self, widgets):
        for w in widgets:
            if w.key == self.focus_key and w.focusable:
                return w
        # default focus
        target = self.modals[-1].focus_key if self.modals else self.screen.default_focus
        for w in widgets:
            if w.key == target and w.focusable:
                self.focus_key = w.key
                return w
        for w in widgets:
            if w.focusable and w.group == "content" and w.enabled:
                self.focus_key = w.key
                return w
        for w in widgets:
            if w.focusable:
                self.focus_key = w.key
                return w
        return None

    def action(self, a):
        widgets = self._widgets()
        cur = self._focused(widgets)
        top = self.modals[-1] if self.modals else None
        if a in ("up", "down", "left", "right"):
            if cur and cur.nav(self, a):
                return
            if cur is None:
                return
            if not top and cur.group == "content":
                cands = [w for w in widgets if w.group == "content"]
                nxt = ui.spatial_next(cands, cur, a)
                if nxt is None and a == "left":
                    self.focus_key = f"nav{self.screen_i}"
                    return
            elif not top and cur.group == "nav" and a == "right":
                self.focus_key = self.screen.last_focus or self.screen.default_focus
                if not any(w.key == self.focus_key for w in widgets):
                    self.focus_key = self.screen.default_focus
                return
            elif not top and cur.group == "nav":
                cands = [w for w in widgets if w.group == "nav"]
                nxt = ui.spatial_next(cands, cur, a)
            else:
                nxt = ui.spatial_next(widgets, cur, a)
            if nxt:
                self.focus_key = nxt.key
                if nxt.group == "content" and not top:
                    self.screen.last_focus = nxt.key
        elif a == "accept":
            if cur and cur.enabled:
                cur.activate(self)
        elif a == "back":
            if top:
                top.back()
            elif cur and cur.group == "content":
                self.focus_key = f"nav{self.screen_i}"
            elif self.screen_i != 0:
                self.go(0)
        elif a in ("x", "y"):
            if top and top.on_button(a):
                return
            if cur and cur.alt(self, a):
                return
            if not top:
                self.screen.on_button(a)
        elif a in ("lb", "rb"):
            if top:
                top.on_button(a)
                return
            n = len(self.screens)
            self.go((self.screen_i + (1 if a == "rb" else -1)) % n)
        elif a == "start":
            if top:
                top.on_button("start")
            else:
                self.screens[0].play()
        elif a == "select":
            if not top:
                self.request_quit()

    def _scroll(self, amount, pos=None):
        widgets = self._widgets()
        if pos:
            for w in widgets:
                if w.rect.collidepoint(pos) and w.scroll(self, amount):
                    return
        cur = self._focused(widgets)
        if cur and cur.scroll(self, amount):
            return
        if not self.modals:
            self.screen.scroll(amount)

    def handle_event(self, ev):
        top = self.modals[-1] if self.modals else None
        if ev.type == pygame.QUIT:
            self.request_quit()
            return
        if top and top.on_event(ev):
            return
        if ev.type == pygame.KEYDOWN:
            km = {pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left", pygame.K_RIGHT: "right",
                  pygame.K_RETURN: "accept", pygame.K_KP_ENTER: "accept", pygame.K_SPACE: "accept",
                  pygame.K_ESCAPE: "back", pygame.K_BACKSPACE: "back", pygame.K_PAGEUP: "lb",
                  pygame.K_PAGEDOWN: "rb", pygame.K_F5: "start", pygame.K_DELETE: "y",
                  pygame.K_TAB: "down"}
            if ev.key == pygame.K_TAB and ev.mod & pygame.KMOD_SHIFT:
                self.action("up")
            elif ev.key == pygame.K_q and ev.mod & pygame.KMOD_CTRL:
                self.request_quit()
            elif ev.key in km:
                self.action(km[ev.key])
        elif ev.type == pygame.CONTROLLERBUTTONDOWN:
            bm = {pygame.CONTROLLER_BUTTON_A: "accept", pygame.CONTROLLER_BUTTON_B: "back",
                  pygame.CONTROLLER_BUTTON_X: "x", pygame.CONTROLLER_BUTTON_Y: "y",
                  pygame.CONTROLLER_BUTTON_LEFTSHOULDER: "lb", pygame.CONTROLLER_BUTTON_RIGHTSHOULDER: "rb",
                  pygame.CONTROLLER_BUTTON_START: "start", pygame.CONTROLLER_BUTTON_BACK: "select",
                  pygame.CONTROLLER_BUTTON_DPAD_UP: "up", pygame.CONTROLLER_BUTTON_DPAD_DOWN: "down",
                  pygame.CONTROLLER_BUTTON_DPAD_LEFT: "left", pygame.CONTROLLER_BUTTON_DPAD_RIGHT: "right"}
            a = bm.get(ev.button)
            if a:
                self.action(a)
                if a in ("up", "down", "left", "right"):
                    self.held, self.held_next = a, time.time() + 0.38
        elif ev.type == pygame.CONTROLLERBUTTONUP:
            self.held = None
        elif ev.type == pygame.CONTROLLERAXISMOTION:
            if ev.axis in (pygame.CONTROLLER_AXIS_LEFTX, pygame.CONTROLLER_AXIS_LEFTY):
                self._stick()
            elif ev.axis == pygame.CONTROLLER_AXIS_TRIGGERRIGHT and ev.value > 20000:
                self._scroll(5)
            elif ev.axis == pygame.CONTROLLER_AXIS_TRIGGERLEFT and ev.value > 20000:
                self._scroll(-5)
        elif ev.type == pygame.CONTROLLERDEVICEADDED:
            self._open_controllers()
        elif ev.type == pygame.JOYHATMOTION and not self._has_gamecontroller(ev.instance_id):
            x, y = ev.value
            a = "left" if x < 0 else "right" if x > 0 else "up" if y > 0 else "down" if y < 0 else None
            if a:
                self.action(a)
        elif ev.type == pygame.JOYBUTTONDOWN and not self._has_gamecontroller(ev.instance_id):
            jm = {0: "accept", 1: "back", 2: "x", 3: "y", 4: "lb", 5: "rb", 6: "select", 7: "start"}
            if ev.button in jm:
                self.action(jm[ev.button])
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            for w in self._widgets():
                if w.rect.collidepoint(ev.pos) and w.focusable:
                    self.focus_key = w.key
                    if w.group == "content" and not self.modals:
                        self.screen.last_focus = w.key
                    if w.enabled:
                        w.click(self, ev.pos)
                    break
        elif ev.type == pygame.MOUSEWHEEL:
            self._scroll(-ev.y * 2, pygame.mouse.get_pos())
        elif ev.type == pygame.MOUSEMOTION:
            if abs(ev.pos[0] - self.last_mouse[0]) + abs(ev.pos[1] - self.last_mouse[1]) > 6:
                self.last_mouse = ev.pos
                for w in self._widgets():
                    if w.rect.collidepoint(ev.pos) and w.focusable and not isinstance(w, ui.ListView):
                        self.focus_key = w.key
                        break

    def _has_gamecontroller(self, instance_id):
        return any(getattr(c, "instance_id", None) == instance_id for c in self.controllers)

    def _open_controllers(self):
        try:
            from pygame._sdl2 import controller as sdlc
            sdlc.init()
            for i in range(sdlc.get_count()):
                if sdlc.is_controller(i):
                    c = sdlc.Controller(i)
                    if c not in self.controllers:
                        self.controllers.append(c)
                        try:
                            c.instance_id = c.as_joystick().get_instance_id()
                        except Exception:
                            pass
        except Exception as e:
            print("controller init failed:", e)
        pygame.joystick.init()
        for i in range(pygame.joystick.get_count()):
            try:
                pygame.joystick.Joystick(i).init()
            except Exception:
                pass

    def _stick(self):
        x = y = 0
        for c in self.controllers:
            try:
                x = c.get_axis(pygame.CONTROLLER_AXIS_LEFTX) / 32767
                y = c.get_axis(pygame.CONTROLLER_AXIS_LEFTY) / 32767
                if abs(x) > 0.55 or abs(y) > 0.55:
                    break
            except Exception:
                pass
        d = None
        if max(abs(x), abs(y)) > 0.55:
            d = ("right" if x > 0 else "left") if abs(x) > abs(y) else ("down" if y > 0 else "up")
        if d != self.axis_dir:
            self.axis_dir = d
            if d:
                self.action(d)
                self.held, self.held_next = d, time.time() + 0.38
            else:
                self.held = None

    # ----------------------------------------------------------- draw
    def sidebar_widgets(self):
        ws = []
        y = 150
        for i, s in enumerate(self.screens):
            ws.append(ui.NavItem(f"nav{i}", (16, y, SIDEBAR_W - 32, 58), s.title,
                                 lambda i=i: self.go(i), i == self.screen_i, s.badge()))
            y += 66
        ws.append(ui.NavItem("navquit", (16, H - 64 - 72, SIDEBAR_W - 32, 54), "Quit",
                             self.request_quit, False))
        return ws

    def draw_logo(self, surf):
        # original emblem: a hexagon with an "MH" monogram
        cx, cy = 52, 62
        import math
        pts = [(cx + 26 * math.cos(math.radians(60 * k + 30)), cy + 26 * math.sin(math.radians(60 * k + 30)))
               for k in range(6)]
        pygame.draw.polygon(surf, C["accent"], pts)
        pygame.draw.polygon(surf, C["accent_hi"], pts, 2)
        text(surf, "MH", (cx, cy + 1), 20, C["text"], True, "center")
        text(surf, "MH Launcher", (90, 40), 25, C["text"], True)
        text(surf, "for MHServerEmu", (91, 72), 15, C["muted"])

    def draw_topbar(self, surf):
        text(surf, self.screen.title, (CONTENT.x, 36), 34, C["text"], True)
        # server pill
        st = self.server.state
        col = {"Online": C["green"], "Starting": C["yellow"], "Stopping": C["yellow"],
               "Crashed": C["red"]}.get(st, C["dim"])
        label = f"Server {st.lower()}"
        if st == "Online":
            label += f" · {self.server.players_online()} players"
        w = ui.font(17, True).size(label)[0] + 56
        pill = pygame.Rect(W - 24 - w, 34, w, 40)
        rrect(surf, C["panel"], pill, 20)
        rrect(surf, C["line"], pill, 20, 1)
        ui.status_dot(surf, col, (pill.x + 22, pill.centery), 6, st in ("Starting", "Stopping"))
        text(surf, label, (pill.x + 38, pill.centery), 17, C["text"], True, "midleft")
        if self.game_running:
            g = "Game running"
            gw = ui.font(17, True).size(g)[0] + 56
            gp = pygame.Rect(pill.x - 12 - gw, 34, gw, 40)
            rrect(surf, (38, 26, 30), gp, 20)
            rrect(surf, C["accent_dk"], gp, 20, 1)
            ui.status_dot(surf, C["accent"], (gp.x + 22, gp.centery), 6, True)
            text(surf, g, (gp.x + 38, gp.centery), 17, C["text"], True, "midleft")

    def draw_hints(self, surf, focused):
        bar = pygame.Rect(0, H - 52, W, 52)
        pygame.draw.rect(surf, C["bg2"], bar)
        pygame.draw.line(surf, C["line"], (0, bar.y), (W, bar.y))
        if self.modals:
            hints = list(self.modals[-1].hints)
        else:
            hints = [("A", "Select"), ("B", "Back")]
            if isinstance(focused, (ui.Toggle, ui.Selector)):
                hints.append(("DPAD", "Change"))
            if isinstance(focused, ui.ListView) and focused.on_alt:
                hints.append(("Y", self.screen.alt_hint))
            hints += [("LB", ""), ("RB", "Switch tab"), ("START", "Play"), ("SELECT", "Quit")]
        x = W - 24
        for g, label in reversed(hints):
            if label:
                r = text(surf, label, (x, bar.centery), 16, C["muted"], False, "midright")
                x = r.x - 10
            hw = glyph(surf, g, (x - 14, bar.centery))
            x -= hw * 2 + (14 if label else 4)
        text(surf, f"v{APP_VERSION}", (24, bar.centery), 14, C["dim"], False, "midleft")

    def draw_toasts(self, surf):
        now = time.time()
        self.toasts = [t for t in self.toasts if t[2] > now]
        y = H - 70
        for msg, kind, _exp in reversed(self.toasts):
            col = {"ok": C["green"], "warn": C["yellow"], "error": C["red"]}.get(kind, C["blue"])
            lines = ui.wrap(msg, 17, 420)
            h = 26 + 24 * len(lines)
            r = pygame.Rect(W - 24 - 470, y - h, 470, h)
            rrect(surf, C["panel2"], r, 12)
            rrect(surf, col, pygame.Rect(r.x, r.y, 6, r.h), 3)
            yy = r.y + 13
            for ln in lines:
                text(surf, ln, (r.x + 24, yy), 17, C["text"])
                yy += 24
            y = r.y - 10

    def draw(self, surf):
        surf.fill(C["bg"])
        # background flourish
        pygame.draw.circle(surf, (24, 14, 18), (W + 60, -80), 420)
        pygame.draw.rect(surf, C["bg2"], (0, 0, SIDEBAR_W, H))
        pygame.draw.line(surf, C["line"], (SIDEBAR_W, 0), (SIDEBAR_W, H))
        self.draw_logo(surf)
        base = self.sidebar_widgets() + self.screen.build()
        focused = None if self.modals else self._focused(base)
        self.draw_topbar(surf)
        self.screen.draw(surf)
        for w in base:
            w.draw(surf, w is focused)
        if self.modals:
            dim = pygame.Surface((W, H), pygame.SRCALPHA)
            dim.fill((5, 6, 9, 200))
            surf.blit(dim, (0, 0))
            m = self.modals[-1]
            panel(surf, m.rect, C["panel"], 20)
            m.draw_body(surf)
            mw = m.build()
            mf = self._focused(mw)
            for w in mw:
                w.draw(surf, w is mf)
            focused = mf
        self.draw_hints(surf, focused)
        self.draw_toasts(surf)

    # ------------------------------------------------------------ loop
    def tick(self):
        with self._mainq_lock:
            q, self._mainq = self._mainq, []
        for fn in q:
            try:
                fn()
            except Exception:
                traceback.print_exc()
        if self.held and time.time() >= self.held_next:
            self.action(self.held)
            self.held_next = time.time() + 0.09
        if self.game_proc is not None and self.game_proc.poll() is not None:
            code = self.game_proc.returncode
            self.game_proc = None
            if time.time() - self.game_started < 15 and code not in (0, None):
                self.dialog("The game closed right away",
                            f"Exit code {code}. Check the client log at {os.path.join(LOG_DIR, 'client.log')} "
                            "or try a different Proton version on the Client tab.")
            else:
                self.toast("Game closed", "info")
        for s in self.screens:
            s.update()

    def run(self):
        os.environ.setdefault("SDL_VIDEO_X11_WMCLASS", "MHLauncher")
        pygame.display.init()
        pygame.font.init()
        pygame.display.set_caption("MH Launcher")
        icon_path = os.path.join(ASSETS_DIR, "icon.png")
        if os.path.isfile(icon_path):
            try:
                pygame.display.set_icon(pygame.image.load(icon_path))
            except Exception:
                pass
        fs = self.settings.get("fullscreen")
        if fs is None:
            fs = in_gaming_mode()
        flags = pygame.SCALED | (pygame.FULLSCREEN if fs else pygame.RESIZABLE)
        try:
            screen = pygame.display.set_mode((W, H), flags, vsync=1)
        except pygame.error:
            screen = pygame.display.set_mode((W, H), flags)
        pygame.key.set_repeat(380, 70)
        pygame.key.stop_text_input()
        self._open_controllers()
        self.go(0)
        leftovers = find_mhserveremu_pids()
        if leftovers:
            self.offer_stop_leftover(leftovers)
        clock = pygame.time.Clock()
        while self.running:
            for ev in pygame.event.get():
                try:
                    self.handle_event(ev)
                except Exception:
                    traceback.print_exc()
            self.tick()
            self.draw(screen)
            pygame.display.flip()
            # save battery: 30 fps when nothing is animating
            clock.tick(60 if (self.modals or self.held) else 30)
        self.bridge.stop()
        if self.server.running:
            self.server.stop()
        pygame.quit()

