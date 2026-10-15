"""Modules tab: install, turn on/off and remove modules (new heroes and other big game changes). See modules.py."""
import os
import time

import pygame

from . import ui, modules
from .ui import C, text, rrect, panel
from .app import CONTENT, Modal
from .config import LOG_DIR, DATA_DIR
from .screens import Screen

X0, Y0, CW, CH = CONTENT.x, CONTENT.y, CONTENT.w, CONTENT.h
LIST_W = 560


class ModulePicker(Modal):
    """Pick a module archive: newest downloads first, or browse."""
    width, height = 1000, 640
    hints = [("A", "Install"), ("B", "Cancel")]

    def __init__(self, app, on_pick):
        super().__init__(app)
        self.on_pick = on_pick
        self.items = modules.downloads_archives()
        self.index = 0
        self.focus_key = "mp_list" if self.items else "mp_browse"

    def build(self):
        r = self.rect

        def render(surf, rr, path, sel, focused):
            age = time.time() - os.path.getmtime(path)
            when = "today" if age < 86400 else f"{int(age // 86400)} day(s) ago"
            size = os.path.getsize(path) / 1048576
            text(surf, os.path.basename(path), (rr.x + 18, rr.y + 8), 18, C["text"], sel, max_w=rr.w - 40)
            text(surf, f"{os.path.dirname(path).replace(os.path.expanduser('~'), '~')}  ·  {size:.1f} MB  ·  {when}",
                 (rr.x + 18, rr.y + 34), 13, C["muted"], max_w=rr.w - 40)

        def pick(i):
            self.app.close_modal(self)
            self.on_pick(self.items[i])

        def browse():
            self.app.close_modal(self)
            self.app.browse("Pick a module (.zip) or a module folder", os.path.expanduser("~/Downloads"),
                            modules.ARCHIVE_EXTS, self.on_pick, pick_dirs=True)
        return [
            ui.ListView("mp_list", (r.x + 30, r.y + 100, r.w - 60, r.h - 200), self.items, self.index,
                        lambda i: setattr(self, "index", i), pick, 62, render,
                        "No modules in Downloads yet – or press Browse"),
            ui.Button("mp_browse", (r.x + 30, r.bottom - 82, 300, 56), "Browse…", browse, "secondary"),
            ui.Button("mp_cancel", (r.right - 210, r.bottom - 82, 180, 56), "Cancel",
                      lambda: self.app.close_modal(self), "ghost"),
        ]

    def draw_body(self, surf):
        text(surf, "Install a module", (self.rect.x + 30, self.rect.y + 26), 28, C["text"], True)
        text(surf, "MH Launcher modules (a .zip with a module.json inside). Newest first.",
             (self.rect.x + 30, self.rect.y + 66), 16, C["muted"], max_w=self.rect.w - 60)


class ModulesScreen(Screen):
    name, title = "modules", "Modules"
    alt_hint = "Remove"
    accept_hints = {"mo_list": "On / Off"}

    def __init__(self, app):
        super().__init__(app)
        self.lib = None
        self.index = 0
        self.loaded_at = 0

    @property
    def default_focus(self):
        return "mo_list" if self.lib and self.lib.modules else "mo_install"

    def reload(self):
        self.lib = modules.Library()
        self.loaded_at = time.time()
        self.index = max(0, min(self.index, len(self.lib.modules) - 1))

    def update(self):
        if self.app.screen is self and time.time() - self.loaded_at > 5 and not self.app.modals:
            self.reload()

    def selected(self):
        if self.lib and self.lib.modules:
            return self.lib.modules[max(0, min(self.index, len(self.lib.modules) - 1))]
        return None

    def badge(self):
        return C["yellow"] if self.s.get("modules_dirty") else None

    def _game_root(self):
        return self.s.get("client_root")

    def _busy_guard(self):
        if self.app.game_running:
            self.app.dialog("Close the game first", "Modules can only be changed while Marvel Heroes is closed.")
            return True
        if not self._game_root():
            self.app.go("client")
            self.app.toast("Pick your Marvel Heroes game first.", "warn")
            return True
        return False

    # ----------------------------------------------------------- actions
    def apply_blocking(self, progress):
        """Worker thread: make the game files match the module list."""
        progress(None, "Applying modules…")
        done = modules.apply(self._game_root(), progress)
        self.s.set("modules_dirty", False)
        return done

    def apply(self):
        if self._busy_guard():
            return
        app = self.app

        def done(res):
            if isinstance(res, Exception):
                app.dialog("Couldn't apply modules", f"{res}\n\nLog: {os.path.join(LOG_DIR, 'modules.log')}", width=960)
                return
            self.reload()
            app.toast("Modules applied" + (f" ({len(res)} file(s) changed)" if res else " – nothing to change"), "ok", 6)
            for m in self.lib.modules:
                if self.lib.enabled(m) and m.waiting:
                    app.toast(f"{m.name} wasn't applied: it {m.waiting}", "warn", 10)

        def work(p):
            try:
                return self.apply_blocking(p)
            except Exception as e:           # show it in a dialog with the log path
                modules.mlog(f"apply failed: {e}")
                return e
        app.run_task("Applying modules", work, done)

    def install(self):
        if self._busy_guard():
            return
        self.app.open_modal(ModulePicker(self.app, self._install_path))

    def _install_path(self, path):
        app = self.app

        def work(p):
            p(None, "Installing " + os.path.basename(path.rstrip("/")) + "…")
            return modules.Library().install(path)

        def done(mod):
            if isinstance(mod, Exception):
                app.dialog("Couldn't install it", str(mod), width=900)
                return
            self.s.set("modules_dirty", True)
            self.reload()
            self.index = 0
            app.toast(f"Installed {mod.name} {mod.version}. It's on – press Apply (or just Play).", "ok", 7)

        def guarded(p):
            try:
                return work(p)
            except Exception as e:
                return e
        app.run_task("Installing module", guarded, done)

    def _save_backup(self, label):
        """Copy of the server's save database (Account.db) before game data goes away. Returns the path or None."""
        from . import savetransfer
        db = savetransfer.db_path(self.s.get("server_dir"))
        if not os.path.isfile(db):
            return None
        return savetransfer.backup(db, os.path.join(DATA_DIR, "save-backups"), label)

    def _confirm_data_loss(self, m, verb, then):
        """A module with game data (heroes, items, ...): the server drops what it can't load, so warn and back up."""
        if not m.m.get("calligraphy"):
            return then()
        app = self.app

        def go():
            try:
                dest = self._save_backup("before-module-" + "".join(c if c.isalnum() else "-" for c in m.id))
                if dest:
                    app.toast("Save backed up: " + os.path.basename(dest), "ok", 5)
            except Exception as e:
                app.dialog("Couldn't back up your save", f"{e}\n\nNothing was changed.")
                return
            then()
        app.dialog(f"{verb} {m.name}?",
                   f"{m.name} adds game data (heroes, powers, items). While it's out of the game, the server can't load "
                   "anything that comes from it: if you log in, heroes and items from this module are dropped from "
                   "your save, and turning the module back on won't bring them back.\n\n"
                   "A backup of your save (Account.db) is made now, in ~/.local/share/MHLauncher/save-backups/.",
                   [("Cancel", None, "ghost"), (verb, go, "danger")], width=980)

    def toggle(self, i=None):
        m = self.lib.modules[i if i is not None else self.index] if self.lib and self.lib.modules else None
        if not m:
            return
        if m.error:
            self.app.toast(f"{m.name}: {m.error}", "error")
            return

        def flip():
            self.lib.set_enabled(m, not self.lib.enabled(m))
            self.s.set("modules_dirty", True)
        if self.lib.enabled(m):
            self._confirm_data_loss(m, "Turn off", flip)
        else:
            flip()

    def move(self, delta):
        m = self.selected()
        if not m:
            return
        order = self.lib.state["order"]
        key = os.path.basename(m.folder)
        i = order.index(key)
        j = max(0, min(len(order) - 1, i + delta))
        if i != j:
            order.insert(j, order.pop(i))
            self.lib.save()
            self.reload()
            self.index = j
            if self.lib.enabled(m):
                self.s.set("modules_dirty", True)

    def remove(self, i=None):
        m = self.lib.modules[i if i is not None else self.index] if self.lib and self.lib.modules else None
        if not m or self._busy_guard():
            return
        app = self.app

        def work(p):
            lib = modules.Library()
            target = lib.find(os.path.basename(m.folder))
            if target and lib.enabled(target):
                lib.set_enabled(target, False)
            modules.apply(self._game_root(), p)          # puts its files back before it goes
            lib = modules.Library()
            target = lib.find(os.path.basename(m.folder))
            return lib.remove_folder(target) if target else None

        def done(dest):
            self.reload()
            app.toast(f"Removed {m.name} – the game files are back as they were", "ok", 6)

        def run():
            app.run_task("Removing module", work, done)
        if m.m.get("calligraphy"):
            self._confirm_data_loss(m, "Remove", run)
        else:
            app.dialog("Remove module?", f"{m.name} will be taken out of the game and removed from your list. "
                       "Every game file it changed is put back exactly as it was. Its files are kept in the "
                       "launcher's modules/removed folder, just in case.",
                       [("Cancel", None, "ghost"), ("Remove", run, "danger")])

    def on_button(self, b):
        if b == "x":
            self.toggle()

    # ----------------------------------------------------------- build / draw
    def build(self):
        if self.lib is None:
            self.reload()
        dirty = bool(self.s.get("modules_dirty"))
        bw = (CW - 2 * 12) // 3
        ws = [ui.Button("mo_install", (X0, Y0, bw, 58), "Install module…", self.install, "primary", size=18),
              ui.Button("mo_apply", (X0 + bw + 12, Y0, bw, 58), "Apply changes" + (" •" if dirty else ""), self.apply,
                        "success" if dirty else "secondary", size=18)]
        items = self.lib.modules

        def render(surf, r, m, sel, focused):
            on = self.lib.enabled(m)
            cx, cy = r.x + 26, r.centery
            pygame.draw.circle(surf, C["green"] if on else C["line"], (cx, cy), 11)
            if on:
                pygame.draw.lines(surf, C["bg"], False, [(cx - 5, cy), (cx - 1, cy + 4), (cx + 6, cy - 5)], 3)
            text(surf, m.name, (r.x + 50, r.y + 9), 18, C["text"] if on else C["muted"], True, max_w=r.w - 130)
            sub = " · ".join(x for x in [("by " + m.author) if m.author else "", ("v" + m.version) if m.version else "",
                                          ", ".join(m.kinds())] if x)
            if m.error or (on and m.waiting):
                sub = "⚠ " + (m.error or m.waiting)
            text(surf, sub, (r.x + 50, r.y + 35), 13, C["red"] if m.error else C["yellow"] if on and m.waiting
                 else C["dim"], max_w=r.w - 70)
        ws.append(ui.ListView("mo_list", (X0, Y0 + 100, LIST_W, CH - 100), items, self.index,
                              lambda i: setattr(self, "index", i), self.toggle, 64, render,
                              "No modules yet – press Install module…", on_alt=self.remove))
        m = self.selected()
        if m:
            rx, rw = X0 + LIST_W + 18, CW - LIST_W - 18
            by = CONTENT.bottom - 2 * 60 + 8
            hw = (rw - 12) // 2
            on = self.lib.enabled(m)
            ws.append(ui.Button("mo_up", (rx, by, hw, 50), "▲ Higher", lambda: self.move(-1), "secondary", size=17))
            ws.append(ui.Button("mo_down", (rx + hw + 12, by, hw, 50), "▼ Lower", lambda: self.move(1), "secondary",
                                size=17))
            ws.append(ui.Button("mo_toggle", (rx, by + 60, hw, 50), "Turn off" if on else "Turn on", self.toggle,
                                "secondary" if on else "success", size=17))
            ws.append(ui.Button("mo_remove", (rx + hw + 12, by + 60, hw, 50), "Remove", self.remove, "danger", size=17))
        return ws

    def draw(self, surf):
        lib = self.lib
        n = len(lib.modules) if lib else 0
        on = sum(1 for m in lib.modules if lib.enabled(m)) if lib else 0
        info = f"{n} module(s) · {on} on"
        if self.s.get("modules_dirty"):
            info += "  ·  changes not applied yet (Play applies them too)"
        text(surf, info, (X0 + 4, Y0 + 72), 15, C["yellow"] if self.s.get("modules_dirty") else C["dim"])
        m = self.selected()
        rx, rw = X0 + LIST_W + 18, CW - LIST_W - 18
        box = pygame.Rect(rx, Y0 + 100, rw, CH - 100 - 2 * 60 - 4)
        panel(surf, box, C["panel"], 16)
        if not m:
            ui.text_block(surf, "Modules add bigger things than mods can: new playable heroes, new powers and their "
                          "game data. They are separate from the Mods tab, and removing one puts every game file back "
                          "exactly as it was.\n\nPress Install module… and pick a module .zip.",
                          (box.x + 20, box.y + 20), box.w - 40, 16, C["muted"])
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
        for k in m.kinds():
            w = ui.font(13, True).size(k)[0] + 20
            rrect(surf, C["panel2"], pygame.Rect(chip_x, y + 4, w, 24), 12)
            text(surf, k, (chip_x + w // 2, y + 16), 13, C["text"], True, "center")
            chip_x += w + 8
        y += 38
        if not m.error:
            text(surf, f"{len(m.targets())} game file(s) · {modules_size(m)}", (box.x + 20, y), 14, C["dim"],
                 max_w=box.w - 40)
            y += 26
        state = "ON" if lib.enabled(m) else "OFF"
        text(surf, state, (box.x + 20, y), 15, C["green"] if lib.enabled(m) else C["dim"], True)
        y += 28
        if m.description and y < box.bottom - 40:
            clip = surf.get_clip()
            surf.set_clip(pygame.Rect(box.x, y, box.w, box.bottom - y - 10))
            ui.text_block(surf, m.description[:600], (box.x + 20, y + 6), box.w - 40, 14, C["muted"], spacing=4)
            surf.set_clip(clip)


def modules_size(m):
    n = m.size()
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n // 1024} KB"
