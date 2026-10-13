"""Mods tab: a controller-friendly front end for MHO Extended Mod Manager."""
import os
import time

import pygame

from . import ui, client, mods
from .ui import C, text, rrect, panel
from .app import CONTENT, Modal
from .config import LOG_DIR, in_gaming_mode
from .screens import Screen

X0, Y0, CW, CH = CONTENT.x, CONTENT.y, CONTENT.w, CONTENT.h
LIST_W = 560


class ArchivePicker(Modal):
    """Pick a mod archive: newest downloads first, or browse."""
    width, height = 1000, 640
    hints = [("A", "Install"), ("B", "Cancel")]

    def __init__(self, app, on_pick):
        super().__init__(app)
        self.on_pick = on_pick
        self.items = mods.downloads_archives()
        self.index = 0
        self.focus_key = "ap_list" if self.items else "ap_browse"

    def build(self):
        r = self.rect

        def render(surf, rr, path, sel, focused):
            name = os.path.basename(path)
            age = time.time() - os.path.getmtime(path)
            when = ("today" if age < 86400 else f"{int(age // 86400)} day(s) ago")
            size = os.path.getsize(path) / 1048576
            text(surf, name, (rr.x + 18, rr.y + 8), 18, C["text"], sel, max_w=rr.w - 40)
            text(surf, f"{os.path.dirname(path).replace(os.path.expanduser('~'), '~')}  ·  {size:.1f} MB  ·  {when}",
                 (rr.x + 18, rr.y + 34), 13, C["muted"], max_w=rr.w - 40)

        def pick(i):
            self.app.close_modal(self)
            self.on_pick(self.items[i])

        def browse():
            self.app.close_modal(self)
            self.app.browse("Pick a mod (.zip, .7z, .rar) or a mod folder", os.path.expanduser("~/Downloads"),
                            mods.ARCHIVE_EXTS, self.on_pick, pick_dirs=True)
        return [
            ui.ListView("ap_list", (r.x + 30, r.y + 100, r.w - 60, r.h - 200), self.items, self.index,
                        lambda i: setattr(self, "index", i), pick, 62, render,
                        "No mod archives in Downloads yet – download one from Nexus Mods, or Browse"),
            ui.Button("ap_browse", (r.x + 30, r.bottom - 82, 300, 56), "Browse…", browse, "secondary"),
            ui.Button("ap_cancel", (r.right - 210, r.bottom - 82, 180, 56), "Cancel",
                      lambda: self.app.close_modal(self), "ghost"),
        ]

    def draw_body(self, surf):
        text(surf, "Install a mod", (self.rect.x + 30, self.rect.y + 26), 28, C["text"], True)
        text(surf, "Mods in the MHModManager / MHO Extended Mod Manager format (from Nexus Mods). Newest first.",
             (self.rect.x + 30, self.rect.y + 66), 16, C["muted"], max_w=self.rect.w - 60)


class ModsScreen(Screen):
    name, title = "mods", "Mods"
    alt_hint = "Remove"
    accept_hints = {"md_list": "On / Off"}

    def __init__(self, app):
        super().__init__(app)
        self.lib = None
        self.index = 0
        self.loaded_at = 0
        self.live_log = []
        self.gui_proc = None

    # ----------------------------------------------------------- helpers
    @property
    def default_focus(self):
        if not mods.is_ready():
            return "md_setup"
        return "md_list" if self.lib and self.lib.mods else "md_install"

    def reload(self):
        self.lib = mods.Library() if os.path.isdir(mods.LIBRARY) else None
        self.loaded_at = time.time()
        if self.lib:
            self.index = max(0, min(self.index, len(self.lib.mods) - 1))

    def update(self):
        if self.app.screen is self and time.time() - self.loaded_at > 5 and not self.app.modals:
            self.reload()
        if self.gui_proc and self.gui_proc.poll() is not None:
            self.gui_proc = None
            self.reload()
            self.app.toast("Mod Manager closed – list refreshed", "info")

    def selected(self):
        if self.lib and self.lib.mods:
            return self.lib.mods[max(0, min(self.index, len(self.lib.mods) - 1))]
        return None

    def badge(self):
        return C["yellow"] if self.s.get("mods_dirty") else None

    def _runner(self):
        return client.pick_runner(self.app.runners_cache(), self.s.get("runner"))

    def _game_root(self):
        return self.s.get("client_root")

    def _dirty(self, v=True):
        self.s.set("mods_dirty", v)

    def _busy_guard(self):
        if self.app.game_running:
            self.app.dialog("Close the game first", "Mods can only be installed or applied while Marvel Heroes is closed.")
            return True
        if not self._game_root():
            self.app.go("client")
            self.app.toast("Pick your Marvel Heroes game first.", "warn")
            return True
        return False

    def _log_line(self, line):
        self.live_log.append(line)
        del self.live_log[:-200]

    # ----------------------------------------------------------- actions
    def setup(self, then=None):
        if self._busy_guard():
            return
        app = self.app
        runner = self._runner()

        def work(p):
            mods.setup(self.s, runner, self._game_root(), p)
            return mods.installed_version()

        def done(ver):
            self.reload()
            app.toast(f"MHO Extended Mod Manager {ver} is ready", "ok")
            if then:
                then()
        app.run_task("Setting up the Mod Manager", work, done)

    def update_tool(self):
        def work(p):
            old = mods.installed_version()
            new = mods.install_extmm(p)
            return old, new
        self.app.run_task("Updating the Mod Manager", work,
                          lambda r: self.app.toast(f"Mod Manager {r[1]}" + (" (was " + r[0] + ")" if r[0] and r[0] != r[1] else " – up to date"), "ok"))

    def install(self):
        if self._busy_guard():
            return
        self.app.open_modal(ArchivePicker(self.app, self._install_path))

    def _install_path(self, path, replace=False):
        app = self.app
        runner = self._runner()
        self.live_log = []

        def work(p):
            p(None, "Installing " + os.path.basename(path.rstrip("/")) + "…")
            before = mods.Library().folder_ids()
            args = ["--install", client.to_windows_path(path)] + (["--replace"] if replace else [])
            code, out = mods.run_cli(self.s, runner, self._game_root(), args, on_line=self._log_line)
            lib = mods.Library()
            new = sorted(lib.folder_ids() - before)
            for mid in new:                      # installed mods come in disabled: turn them on
                m = lib.find(mid)
                if m:
                    m.enabled = True
            if new:
                lib.save()
            return code, out, new

        def done(res):
            code, out, new = res
            self.reload()
            if new:
                self._dirty()
                self.index = 0
                names = ", ".join(new)
                app.toast(f"Installed {names}. It's on – press Apply changes (or just Play).", "ok", 7)
            elif "already installed" in out:
                app.dialog("Already installed", "A mod with this name is already in your list. Update it with this "
                           "file? It keeps its place in the list and its on/off setting.",
                           [("Cancel", None, "ghost"), ("Update it", lambda: self._install_path(path, True), "primary")])
            elif "Updated '" in out:
                self._dirty()
                app.toast("Mod updated. Press Apply changes (or just Play).", "ok", 6)
            else:
                tail = "\n".join([l for l in out.splitlines() if not l.startswith("MHO Extended Mod Manager v")][-8:])
                app.dialog("Couldn't install it", tail or f"The Mod Manager stopped with code {code}. "
                           f"See {os.path.join(LOG_DIR, 'modmanager.log')}.", width=1000)
        app.run_task("Installing mod", work, done, show_log=True, log_fn=lambda: self.live_log)

    def apply_blocking(self, progress, on_line=None):
        """Worker thread: make the game match the mod list. Returns (ok, summary)."""
        from . import modules
        root = self._game_root()
        # Modules patch some of the files the Mod Manager rebuilds (icons, text): step back so it finds them the way
        # it left them, then put the module changes back on top of its new files.
        modules.release_all(root, progress)
        progress(None, "Applying mod changes…")
        try:
            code, out = mods.run_cli(self.s, self._runner(), root, ["--apply"], on_line=on_line)
        finally:
            modules.apply(root, progress)
        lines = [l for l in out.splitlines() if not l.startswith("MHO Extended Mod Manager v")]
        summary = [l for l in lines if "file(s)" in l or "Applied" in l or "Stopped" in l or "close it first" in l]
        if code == 0:
            self.s.set("mods_dirty", False)
        return code == 0, "\n".join(summary[-3:] or lines[-6:])

    def apply(self):
        if self._busy_guard():
            return
        self.live_log = []
        app = self.app

        def done(res):
            ok, summary = res
            if ok:
                app.toast("Mods applied. " + (summary.splitlines()[-1] if summary else ""), "ok", 7)
            else:
                app.dialog("Apply stopped", (summary or "See the log.") +
                           f"\n\nFull log: {os.path.join(LOG_DIR, 'modmanager.log')}", width=1000)
        app.run_task("Applying mod changes", lambda p: self.apply_blocking(p, self._log_line), done,
                     show_log=True, log_fn=lambda: self.live_log)

    def toggle(self, i=None):
        m = self.lib.mods[i if i is not None else self.index] if self.lib and self.lib.mods else None
        if not m:
            return
        if m.error:
            self.app.toast(f"{m.name}: {m.error}", "error")
            return
        self.lib.set_enabled(m, not m.enabled)
        self._dirty()

    def move(self, delta=None, top=None):
        m = self.selected()
        if not m:
            return
        ok = self.lib.move(m, delta) if delta else self.lib.move_to(m, top)
        if ok:
            self.index = self.lib.mods.index(m)
            if m.enabled:
                self._dirty()
        elif m.lock:
            self.app.toast("This mod is locked in place (set in the full Mod Manager).", "info")

    def remove(self, i=None):
        m = self.lib.mods[i if i is not None else self.index] if self.lib and self.lib.mods else None
        if not m or self._busy_guard():
            return
        app = self.app
        self.live_log = []

        def work(p):
            applied = False
            # The Mod Manager only lets a mod go once none of its files are in the game any more, so turn it off
            # and apply first - also when it was already off but the change wasn't applied yet.
            if m.enabled or self.s.get("mods_dirty"):
                if m.enabled:
                    self.lib.set_enabled(m, False)
                ok, summary = self.apply_blocking(p, self._log_line)
                if not ok:
                    raise RuntimeError("Couldn't take the mod out of the game first:\n" + (summary or "see the log") +
                                       f"\n\nLog: {os.path.join(LOG_DIR, 'modmanager.log')}")
                applied = True
            p(None, "Checking it can be removed…")
            code, out = mods.run_cli(self.s, self._runner(), self._game_root(),
                                     ["--remove", m.id, "--dry-run"], on_line=self._log_line)
            if code != 0:
                msg = "\n".join(out.splitlines()[-8:]).strip()
                if msg or not applied:
                    raise RuntimeError((msg or f"The Mod Manager stopped with code {code} without saying why.") +
                                       f"\n\nLog: {os.path.join(LOG_DIR, 'modmanager.log')}")
                # no message, but Apply just succeeded with the mod off: nothing of it is left in the game
                mods.mlog(f"--remove --dry-run gave exit {code} without output; removing after a good apply")
            lib = mods.Library()
            target = lib.find(m.id)
            return lib.remove_folder(target) if target else None

        def done(dest):
            self.reload()
            app.toast(f"Removed {m.name}" + (f" (kept in {os.path.dirname(dest)})" if dest else ""), "ok", 6)
        app.dialog("Remove mod?", f"{m.name} will be taken out of the game (if it's on) and removed from your list. "
                   "Its files are kept in the launcher's 'removed' folder, just in case.",
                   [("Cancel", None, "ghost"),
                    ("Remove", lambda: app.run_task("Removing mod", work, done, show_log=True,
                                                    log_fn=lambda: self.live_log), "danger")])

    def friend_mods(self):
        """Copy the mods of the friend you join (Friends tab address), so you see the game like they do."""
        if self._busy_guard():
            return
        app = self.app
        host = (self.s.get("join_host") or "").strip()
        if not host:
            app.dialog("Whose mods?", "Enter your friend's address on the Friends tab first (the friend must be "
                       "hosting with launcher 1.5.0 or newer).")
            return
        from . import savetransfer

        def work(p):
            p(None, f"Asking {host} for their mods…")
            host_mods = savetransfer.remote(host, "list", {}, timeout=30, base="/mhl/mods/").get("mods") or []
            need, enable, total = mods.compare_with(host_mods)
            return host_mods, need, enable, total

        def done(res):
            if isinstance(res, Exception):
                app.dialog("Couldn't get your friend's mods", str(res), width=900)
                return
            host_mods, need, enable, total = res
            if not host_mods:
                app.dialog("No mods", "Your friend doesn't have any mods turned on.")
                return
            if not need and not enable:
                app.toast(f"You already have all {len(host_mods)} of your friend's mods turned on", "ok", 6)
                return
            names = [h["name"] for h in need + enable]
            play = next(sc for sc in app.screens if sc.name == "play")
            offer = {"host_mods": host_mods, "need": need, "enable": enable, "total": total}
            app.dialog("Get your friend's mods?",
                       f"{len(names)} mod(s): {', '.join(names[:5])}{' …' if len(names) > 5 else ''}"
                       f" ({mods.size_text(total)} to copy). They're turned on with your friend's order, above your "
                       "own mods. Your own mods stay.",
                       [("Cancel", None, "ghost"),
                        ("Get mods", lambda: play._get_friend_mods(host, offer, then_play=False), "primary")],
                       width=960)

        def guarded(p):
            try:
                return work(p)
            except savetransfer.SaveError as e:
                return e
        app.run_task("Friend's mods", guarded, done)

    def open_full(self):
        if self._busy_guard():
            return
        runner = self._runner()

        def go():
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                mods.init_library(self._game_root())
                self.gui_proc = mods.open_gui(self.s, runner, os.path.join(LOG_DIR, "modmanager-gui.log"))
                self.app.toast("Opening MHO Extended Mod Manager… (first start can take a moment)", "ok", 6)
            except Exception as e:
                self.app.dialog("Couldn't open it", str(e))
        if in_gaming_mode():
            self.app.dialog("Full Mod Manager", "The full Mod Manager is a Windows desktop app made for mouse and "
                            "keyboard (use the trackpad and STEAM + X for the keyboard). Everything you need for "
                            "everyday use is right here on this tab. Open it anyway?",
                            [("Cancel", None, "ghost"), ("Open it", go, "primary")])
        else:
            go()

    def on_button(self, b):
        if b == "x":
            self.toggle()

    # ----------------------------------------------------------- build / draw
    def build(self):
        if self.lib is None and os.path.isdir(mods.LIBRARY):
            self.reload()
        ws = []
        if not mods.is_ready():
            ws.append(ui.Button("md_setup", (X0 + 40, Y0 + 400, 380, 70), "Set up Mod Manager", self.setup,
                                "primary", size=24))
            return ws
        dirty = bool(self.s.get("mods_dirty"))
        bw = (CW - 4 * 12) // 5
        ws.append(ui.Button("md_install", (X0, Y0, bw, 58), "Install mod…", self.install, "primary", size=18))
        ws.append(ui.Button("md_apply", (X0 + (bw + 12), Y0, bw, 58),
                            "Apply changes" + (" •" if dirty else ""), self.apply,
                            "success" if dirty else "secondary", size=18))
        ws.append(ui.Button("md_friend", (X0 + 2 * (bw + 12), Y0, bw, 58), "Friend's mods", self.friend_mods,
                            "secondary", size=18))
        ws.append(ui.Button("md_full", (X0 + 3 * (bw + 12), Y0, bw, 58), "Full Mod Manager", self.open_full,
                            "ghost", size=17))
        ws.append(ui.Button("md_update", (X0 + 4 * (bw + 12), Y0, bw, 58), "Update tool", self.update_tool,
                            "ghost", size=17))
        items = self.lib.mods if self.lib else []

        def render(surf, r, m, sel, focused):
            on = m.enabled
            cx, cy = r.x + 26, r.centery
            pygame.draw.circle(surf, C["green"] if on else C["line"], (cx, cy), 11)
            if on:
                pygame.draw.lines(surf, C["bg"], False, [(cx - 5, cy), (cx - 1, cy + 4), (cx + 6, cy - 5)], 3)
            text(surf, m.name, (r.x + 50, r.y + 9), 18, C["text"] if on else C["muted"], True, max_w=r.w - 130)
            sub = " · ".join(x for x in [("by " + m.author) if m.author else "", ("v" + m.version) if m.version else "",
                                          ", ".join(m.kinds)] if x)
            if m.error:
                sub = "⚠ " + m.error
            text(surf, sub, (r.x + 50, r.y + 35), 13, C["red"] if m.error else C["dim"], max_w=r.w - 130)
            pos = f"#{self.lib.mods.index(m) + 1}" + (" · locked" if m.lock else "")
            text(surf, pos, (r.right - 16, r.centery), 15, C["muted"], False, "midright")
        ws.append(ui.ListView("md_list", (X0, Y0 + 100, LIST_W, CH - 100), items, self.index,
                              lambda i: setattr(self, "index", i), self.toggle, 64, render,
                              "No mods yet – press Install mod…", on_alt=self.remove))
        m = self.selected()
        if m:
            rx, rw = X0 + LIST_W + 18, CW - LIST_W - 18
            by = CONTENT.bottom - 3 * 60 + 8
            hw = (rw - 12) // 2
            ws.append(ui.Button("md_up", (rx, by, hw, 50), "▲ Higher", lambda: self.move(-1), "secondary", size=17,
                                enabled=not m.lock))
            ws.append(ui.Button("md_down", (rx + hw + 12, by, hw, 50), "▼ Lower", lambda: self.move(1), "secondary",
                                size=17, enabled=not m.lock))
            ws.append(ui.Button("md_top", (rx, by + 60, hw, 50), "To top", lambda: self.move(top=True), "ghost",
                                size=17, enabled=not m.lock))
            ws.append(ui.Button("md_bottom", (rx + hw + 12, by + 60, hw, 50), "To bottom",
                                lambda: self.move(top=False), "ghost", size=17, enabled=not m.lock))
            ws.append(ui.Button("md_toggle", (rx, by + 120, hw, 50), "Turn off" if m.enabled else "Turn on",
                                self.toggle, "secondary" if m.enabled else "success", size=17))
            ws.append(ui.Button("md_remove", (rx + hw + 12, by + 120, hw, 50), "Remove", self.remove, "danger",
                                size=17))
        return ws

    def draw(self, surf):
        if not mods.is_ready():
            box = pygame.Rect(X0, Y0, CW, CH)
            panel(surf, box, C["panel"], 20)
            text(surf, "MOD MANAGER", (box.x + 40, box.y + 34), 14, C["muted"], True)
            text(surf, "Install costumes, icons and more with one button", (box.x + 40, box.y + 60), 28, C["text"], True)
            ui.text_block(surf,
                          "The Mods tab uses MHO Extended Mod Manager by leeper48 (free, open source, MIT) to install "
                          "and apply mods safely: every game file is rebuilt from a verified original, and turning a "
                          "mod off puts the original back. Mods from Nexus Mods in the MHModManager format work.\n\n"
                          "Setup runs once and downloads:\n"
                          "•  MHO Extended Mod Manager (about 10 MB, checked against its published SHA-256)\n"
                          "•  Microsoft's .NET 8 Desktop Runtime for Windows (about 55 MB), into the game's Proton prefix\n\n"
                          "Nothing in your game folder changes until you turn a mod on and apply it.",
                          (box.x + 40, box.y + 112), box.w - 80, 17, C["muted"])
            text(surf, "github.com/leeper48/MHO-UPK-Tools", (box.x + 40, box.bottom - 40), 15, C["dim"])
            return
        lib = self.lib
        n = len(lib.mods) if lib else 0
        on = sum(1 for m in lib.mods if m.enabled) if lib else 0
        info = f"{n} mod(s) · {on} on · Mod Manager {mods.installed_version() or '?'}"
        if self.s.get("mods_dirty"):
            info += "  ·  changes not applied yet"
        text(surf, info, (X0 + 4, Y0 + 72), 15, C["yellow"] if self.s.get("mods_dirty") else C["dim"])
        m = self.selected()
        rx, rw = X0 + LIST_W + 18, CW - LIST_W - 18
        box = pygame.Rect(rx, Y0 + 100, rw, CH - 100 - 3 * 60 - 4)
        panel(surf, box, C["panel"], 16)
        if not m:
            ui.text_block(surf, "Download mods from Nexus Mods (Marvel Heroes Omega), then press Install mod… "
                          "Newest downloads are listed first.", (box.x + 20, box.y + 20), box.w - 40, 16, C["muted"])
            return
        y = box.y + 18
        for line in ui.wrap(m.name, 22, box.w - 40, True)[:2]:
            text(surf, line, (box.x + 20, y), 22, C["text"], True)
            y += 30
        meta = " · ".join(x for x in [m.author and "by " + m.author, m.version and "v" + m.version] if x)
        if meta:
            text(surf, meta, (box.x + 20, y), 15, C["muted"], max_w=box.w - 40)
            y += 24
        chip_x = box.x + 20
        for k in m.kinds:
            w = ui.font(13, True).size(k)[0] + 20
            rrect(surf, C["panel2"], pygame.Rect(chip_x, y + 4, w, 24), 12)
            text(surf, k, (chip_x + w // 2, y + 16), 13, C["text"], True, "center")
            chip_x += w + 8
        y += 38
        text(surf, m.summary(), (box.x + 20, y), 14, C["dim"], max_w=box.w - 40)
        y += 26
        state = ("ON" if m.enabled else "OFF") + (f" · locked at the {m.lock}" if m.lock else "")
        text(surf, state, (box.x + 20, y), 15, C["green"] if m.enabled else C["dim"], True)
        y += 28
        if lib and m.enabled:
            for other, cnt, wins in lib.conflicts_for(m)[:3]:
                msg = (f"Wins over {other.name}" if wins else f"Overridden by {other.name}") + f" ({cnt})"
                text(surf, ("▲ " if wins else "▼ ") + msg, (box.x + 20, y), 14,
                     C["green"] if wins else C["yellow"], max_w=box.w - 40)
                y += 22
        if m.notes and y < box.bottom - 40:
            clip = surf.get_clip()
            surf.set_clip(pygame.Rect(box.x, y, box.w, box.bottom - y - 10))
            ui.text_block(surf, m.notes[:400], (box.x + 20, y + 6), box.w - 40, 14, C["muted"], spacing=4)
            surf.set_clip(clip)
