"""Cash shop points (Gs): give Gs to any account on your server with buttons, no console commands.

MHServerEmu keeps each account's G balance in the save database (Player.GazillioniteBalance) and reads it at
every login, so the launcher changes it there. That works whether the server is on or off, as long as that
player isn't in the game right now (otherwise the server would overwrite it when they log out).
"""
import time

from . import ui, savetransfer
from .app import Modal
from .ui import C, text, rrect

STARTING_KEY = "MTXStore.GazillioniteBalanceForNewAccounts"
AMOUNTS = (1000, 10000, 100000)


def fmt(n):
    return f"{n:,}"


class GShop(Modal):
    width, height = 980, 620
    hints = [("A", "Select"), ("B", "Close")]

    def __init__(self, app):
        super().__init__(app)
        self.accounts = []
        self.index = 0
        self.msg = ("", "info")
        self.focus_key = "g_1000"
        self.reload()
        active = (app.settings.get("active_account") or "").lower()
        for i, a in enumerate(self.accounts):
            if a["email"].lower() == active:
                self.index = i

    # ----------------------------------------------------------- data
    def starting(self):
        try:
            return max(0, int(float(self.app.server.option(STARTING_KEY, "0"))))
        except ValueError:
            return 0

    def reload(self):
        try:
            conn = savetransfer._connect(self.app.save_db_file())
            try:
                self.accounts = savetransfer.g_accounts(conn)
            finally:
                conn.close()
        except savetransfer.SaveError as e:
            self.accounts = []
            self.msg = (str(e), "error")
        except Exception as e:
            self.accounts = []
            self.msg = (f"Couldn't read the server's save database: {e}", "error")
        self.index = min(self.index, max(0, len(self.accounts) - 1))

    def current(self):
        return self.accounts[self.index] if self.accounts else None

    def in_game(self, acc, conn):
        """Is this player logged in right now? (They'd overwrite the change when logging out.)"""
        if not self.app.server.running:
            return False
        seen = self.app.bridge.logins.get(acc["email"].lower(), 0)
        return bool(seen) and seen > savetransfer.last_logout(conn, acc["id"]) + 1

    def give(self, amount):
        acc = self.current()
        if not acc:
            return
        try:
            conn = savetransfer._connect(self.app.save_db_file())
            try:
                if self.in_game(acc, conn):
                    self.msg = (f"{acc['name']} is in the game right now. Close the game "
                                "first (the server saves the wallet when you log out), then add the Gs.", "error")
                    return
                new = savetransfer.add_g(conn, acc["id"], amount, self.starting())
            finally:
                conn.close()
        except savetransfer.SaveError as e:
            self.msg = (str(e), "error")
            return
        except Exception as e:
            self.msg = (f"Couldn't change the balance: {e}", "error")
            return
        self.reload()
        verb = "Added" if amount >= 0 else "Removed"
        self.msg = (f"{verb} {fmt(abs(amount))} G {'to' if amount >= 0 else 'from'} {acc['name']}. New balance: "
                    f"{fmt(new)} G – it shows up the next time they log in.", "ok")

    def custom(self):
        def done(v):
            v = v.strip().replace(",", "").replace(" ", "")
            try:
                n = int(v)
            except ValueError:
                self.msg = ("Type a number, e.g. 5000 (or -5000 to take Gs away).", "error")
                return
            if n:
                self.give(n)
        self.app.open_keyboard("How many Gs? (use - to remove)", "", done)

    def set_starting(self):
        def done(v):
            v = v.strip().replace(",", "")
            try:
                n = max(0, int(v))
            except ValueError:
                self.msg = ("Type a number, e.g. 10000.", "error")
                return
            self.app.server.set_option(STARTING_KEY, str(n))
            self.msg = (f"New accounts now start with {fmt(n)} G" +
                        (" (restart the server to apply)." if self.app.server.running else "."), "ok")
        self.app.open_keyboard("Gs for new accounts", str(self.starting()), done)

    # ----------------------------------------------------------- UI
    def build(self):
        r = self.rect
        ws = []
        names = [f"{a['name']}  ({a['email']})" for a in self.accounts]
        ws.append(ui.Selector("g_acc", (r.x + 36, r.y + 92, r.w - 72, 70), "Account on your server", names,
                              self.index, self._pick, enabled=bool(names)))
        bw = (r.w - 72 - 3 * 14) // 4
        y = r.y + 290
        for i, amt in enumerate(AMOUNTS):
            ws.append(ui.Button(f"g_{amt}", (r.x + 36 + i * (bw + 14), y, bw, 64), f"+ {fmt(amt)} G",
                                lambda amt=amt: self.give(amt), "primary" if i == 1 else "secondary", size=20,
                                enabled=bool(self.accounts)))
        ws.append(ui.Button("g_custom", (r.x + 36 + 3 * (bw + 14), y, bw, 64), "Other amount…", self.custom,
                            "ghost", size=19, enabled=bool(self.accounts)))
        ws.append(ui.Button("g_start", (r.x + 36, r.bottom - 84, 420, 56),
                            f"New accounts start with {fmt(self.starting())} G", self.set_starting, "ghost", size=17))
        ws.append(ui.Button("g_close", (r.right - 216, r.bottom - 84, 180, 56), "Done",
                            lambda: self.app.close_modal(self), "secondary"))
        return ws

    def _pick(self, i):
        self.index = i
        self.msg = ("", "info")

    def draw_body(self, surf):
        r = self.rect
        text(surf, "Cash shop points (G)", (r.x + 36, r.y + 28), 28, C["text"], True)
        text(surf, "Gs buy heroes, costumes and more in the in-game store (Marvel Store).", (r.x + 36, r.y + 64), 15,
             C["muted"], max_w=r.w - 72)
        acc = self.current()
        box = pygame_rect(r.x + 36, r.y + 178, r.w - 72, 94)
        rrect(surf, C["bg2"], box, 14)
        if acc:
            bal = acc["balance"]
            text(surf, "BALANCE", (box.x + 20, box.y + 14), 12, C["muted"], True)
            if bal is None:
                text(surf, "No wallet yet", (box.x + 20, box.y + 36), 30, C["yellow"], True)
                text(surf, "Log in with this account once, then add Gs.", (box.right - 20, box.y + 52), 15, C["dim"],
                     False, "midright")
            else:
                shown = self.starting() if bal < 0 else bal
                text(surf, f"{fmt(shown)} G", (box.x + 20, box.y + 34), 36, C["gold"], True)
                if bal < 0:
                    text(surf, "starting amount (not used yet)", (box.right - 20, box.y + 52), 15, C["dim"], False,
                         "midright")
        else:
            text(surf, "No accounts on your server yet – create one on the Accounts tab.", (box.x + 20, box.centery),
                 17, C["dim"], False, "midleft")
        msg, kind = self.msg
        if msg:
            col = {"ok": C["green"], "error": C["red"]}.get(kind, C["blue"])
            mb = pygame_rect(r.x + 36, r.y + 372, r.w - 72, 120)
            rrect(surf, C["bg2"], mb, 12)
            rrect(surf, col, pygame_rect(mb.x, mb.y, 6, mb.h), 3)
            ui.text_block(surf, msg, (mb.x + 20, mb.y + 14), mb.w - 36, 16, C["text"], spacing=3)
        else:
            ui.text_block(surf, "Works while the server is on or off. The player must not be in the game right now; "
                          "the new balance shows up at their next login. Friends' accounts on your server are listed "
                          "too.", (r.x + 40, r.y + 380), r.w - 80, 15, C["dim"], spacing=3)


def pygame_rect(*a):
    import pygame
    return pygame.Rect(*a)
