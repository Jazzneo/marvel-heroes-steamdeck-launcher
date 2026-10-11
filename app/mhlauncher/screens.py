"""The launcher's pages: Play, Accounts, Server, Client, Settings."""
import os
import threading
import time

import pygame

from . import ui, accounts, client, dotnet, integration
from .ui import C, text, rrect, panel
from .app import CONTENT
from .authproxy import ports_allowed, lan_ip
from .config import LOG_DIR, in_gaming_mode
from .server import SERVER_TOGGLES

X0, Y0, CW, CH = CONTENT.x, CONTENT.y, CONTENT.w, CONTENT.h


def fmt_uptime(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def check_row(surf, x, y, ok, label, detail, w=420):
    col = C["green"] if ok is True else C["yellow"] if ok is None else C["red"]
    pygame.draw.circle(surf, col, (x + 11, y + 13), 11)
    text(surf, "✓" if ok else "!", (x + 11, y + 14), 14, C["bg"], True, "center")
    text(surf, label, (x + 32, y + 2), 18, C["text"], True, max_w=w - 40)
    text(surf, detail, (x + 32, y + 26), 15, C["muted"], max_w=w - 40)


class Screen:
    name = title = ""
    default_focus = None
    alt_hint = ""

    def __init__(self, app):
        self.app = app
        self.last_focus = None

    @property
    def s(self):
        return self.app.settings

    def build(self):
        return []

    def draw(self, surf):
        pass

    def update(self):
        pass

    def scroll(self, amount):
        pass

    def on_button(self, b):
        pass

    def badge(self):
        return None


# ===================================================================== PLAY
class PlayScreen(Screen):
    name, title = "play", "Play"
    default_focus = "play"

    def __init__(self, app):
        super().__init__(app)
        self._client_info = None
        self._client_info_for = None
        self._dotnet = (None, None)
        self._dotnet_checked = 0

    # cached checks (cheap to call every frame)
    def client_info(self):
        path = self.s.get("client_exe") or self.s.get("client_root")
        if path != self._client_info_for:
            self._client_info_for = path
            self._client_info = client.inspect_client(path) if path else None
        return self._client_info

    def dotnet_info(self):
        if time.time() - self._dotnet_checked > 5:
            self._dotnet = dotnet.find_dotnet()
            self._dotnet_checked = time.time()
        return self._dotnet

    def refresh(self):
        self._client_info_for = None
        self._dotnet_checked = 0

    def checks(self):
        ci = self.client_info()
        acc = self.s.active_account()
        dn, ver = self.dotnet_info()
        srv = self.app.server
        return [
            ("client", ci is not None and ci["ok"], "Game client",
             (f"Version {ci['version'] or '?'}" if ci and ci["ok"] else
              (ci["problems"][0] if ci else "Not selected yet")), "client"),
            ("account", acc is not None or not self.s.get("auto_login"), "Account",
             (f"{acc.get('player') or acc['email']}" if acc else "Sign in or create one"), "accounts"),
            ("dotnet", True if dn else None, ".NET 8 runtime",
             (f"{'.'.join(map(str, ver))} found" if dn else "Will be installed automatically"), "settings"),
            ("gamedata", True if srv.game_data_ok() else (None if ci and ci["ok"] else False), "Server game data",
             "Calligraphy.sip + mu_cdata.sip ready" if srv.game_data_ok() else "Copied from your client on start",
             "client"),
            ("ports", ports_allowed() or None, "Login ports (80/443)",
             "Ready" if ports_allowed() else "Needs a one-time setup", "settings"),
        ]

    def play(self):
        app = self.app
        if app.modals:
            return
        if app.game_running:
            app.toast("The game is already running.", "info")
            return
        ci = self.client_info()
        if not ci or not ci["ok"]:
            app.go("client")
            app.toast(ci["problems"][0] if ci else "First, pick your Marvel Heroes game.", "warn")
            return
        if self.s.get("join_mode"):
            return self._join_go()
        acc = self.s.active_account()
        if self.s.get("auto_login") and (not acc or not acc.get("password")):
            app.go("accounts")
            app.toast("Sign in or create an account first.", "warn")
            return
        if not ports_allowed():
            app.offer_port_fix("The game logs in through ports 80 and 443, which need a one-time setup. "
                               "The launcher can do it for you – you will be asked for your password once.",
                               servers=integration.find_blocking_web_servers(), then=self._play_go)
            return
        self._play_go()

    def _play_go(self):
        app = self.app
        ci = self.client_info()
        acc = self.s.active_account()

        def work(progress):
            try:
                app.server.sync_game_data(ci["root"])
            except FileNotFoundError as e:
                raise RuntimeError(str(e))
            if not app.start_server_blocking(progress):
                return None
            progress(None, "Checking the login bridge (ports 80 / 443)…")
            problems = app.bridge.self_test()
            if problems:
                from .authproxy import describe_owner, ports_allowed
                from .server import PortConflict
                stale = []
                for port in (80, 443):
                    own = describe_owner(port)
                    if own and own[0] and own[0] != os.getpid() and own[1] == "another copy of MH Launcher":
                        stale.append((port, own[0], "MH Launcher (old copy)", True))
                if stale:
                    raise PortConflict(stale)
                from .app import BridgeBlocked
                raise BridgeBlocked("The game would not be able to log in:\n\n• " + "\n• ".join(problems),
                                    integration.find_blocking_web_servers())
            from . import mods as modlib
            if self.s.get("mods_dirty") and modlib.is_ready():
                ms = next(sc for sc in app.screens if sc.name == "mods")
                ok_m, summary = ms.apply_blocking(progress)
                if not ok_m:
                    raise RuntimeError("Your mod changes couldn't be applied, so the game wasn't started:\n\n" +
                                       summary + "\n\nCheck the Mods tab (or turn the new mod off) and try again.")
            if acc and self.s.get("auto_login"):
                progress(None, "Checking your login…")
                ok, msg = accounts.verify_login(app.server, acc["email"], acc.get("password", ""))
                if ok is False:
                    raise RuntimeError(msg + " Update the password on the Accounts tab.")
            progress(None, "Launching Marvel Heroes…")
            return True

        def done(ok):
            if ok:
                self.launch_client()
        app.run_task("Getting ready to play", work, done, show_log=True, cancellable=True)

    def _join_go(self):
        """PLAY in join mode: no local server, connect to a friend's server."""
        app = self.app
        host = (self.s.get("join_host") or "").strip()
        if not host:
            app.go("friends")
            app.toast("Enter your friend's address on the Friends tab first.", "warn")
            return
        if self.s.get("auto_login") and not (self.s.get("join_email") and self.s.get("join_password")):
            app.go("friends")
            app.toast("Enter the email and password of your account on your friend's server.", "warn")
            return
        from . import network

        def work(progress):
            progress(None, f"Connecting to {host}…")
            problems = network.check_host(host)
            if problems:
                raise RuntimeError("Can't reach your friend's server:\n\n• " + "\n• ".join(problems) +
                                   "\n\nCheck that their server is on and hosting, that the address is right, "
                                   "and (Tailscale/ZeroTier) that both of you are connected.")
            from . import mods as modlib
            if self.s.get("mods_dirty") and modlib.is_ready():
                ms = next(sc for sc in app.screens if sc.name == "mods")
                ok_m, summary = ms.apply_blocking(progress)
                if not ok_m:
                    raise RuntimeError("Your mod changes couldn't be applied:\n\n" + summary)
            progress(None, "Launching Marvel Heroes…")
            return True

        def done(ok):
            if ok:
                self.launch_client(siteconfig=f"{host}/SiteConfig.xml",
                                   account={"email": self.s.get("join_email"),
                                            "password": self.s.get("join_password")})
        app.run_task(f"Joining {host}", work, done, cancellable=True)

    def launch_client(self, siteconfig=None, account=None):
        app = self.app
        ci = self.client_info()
        runners = client.detect_runners()
        runner = client.pick_runner(runners, self.s.get("runner"))
        args = client.build_args(self.s, siteconfig or app.bridge.siteconfig_url(),
                                 account if account is not None else self.s.active_account())
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            app.game_proc = client.launch(self.s, runner, ci["exe"], args, os.path.join(LOG_DIR, "client.log"))
            app.game_started = time.time()
            app.toast(f"Starting the game with {runner.name}. First launch can take a minute.", "ok", 7)
        except Exception as e:
            app.dialog("Could not start the game", str(e))

    def build(self):
        app = self.app
        ws = []
        hero = pygame.Rect(X0, Y0, CW, 236)
        if app.game_running:
            ws.append(ui.Button("play", (hero.right - 330, hero.y + 52, 300, 96), "STOP GAME",
                                app.stop_game, "danger", sub="Game is running", size=28))
        else:
            ws.append(ui.Button("play", (hero.right - 330, hero.y + 52, 300, 96), "PLAY", self.play,
                                "primary", sub=("Server is online" if app.server.state == "Online" else "Starts server + game"),
                                size=34))
        # cards
        cw = (CW - 32) // 3
        cy = Y0 + 236 + 18
        srv = app.server
        on = srv.running
        ws.append(ui.Button("card_server", (X0 + 20, cy + 112, cw - 40, 52),
                            "Turn server off" if on else "Turn server on", app.toggle_server,
                            "danger" if on else "success", size=19,
                            enabled=srv.state not in ("Stopping",)))
        ws.append(ui.Button("card_account", (X0 + cw + 16 + 20, cy + 112, cw - 40, 52), "Change account",
                            lambda: app.go("accounts"), "secondary", size=19))
        ws.append(ui.Button("card_client", (X0 + 2 * (cw + 16) + 20, cy + 112, cw - 40, 52), "Change game",
                            lambda: app.go("client"), "secondary", size=19))
        # checklist "fix" buttons
        y = cy + 190 + 18 + 46
        colw = (CW - 24) // 3
        for i, (key, ok, label, detail, target) in enumerate(self.checks()):
            col, row = i % 3, i // 3
            bx = X0 + 20 + col * colw + colw - 76
            by = y + row * 58 - 2
            if ok is not True:
                ws.append(ui.Button(f"fix_{key}", (bx, by, 64, 36), "Fix",
                                    lambda t=target: app.go(t), "secondary", size=15))
        return ws

    def draw(self, surf):
        app = self.app
        hero = pygame.Rect(X0, Y0, CW, 236)
        panel(surf, hero, C["panel"], 22)
        # decorative diagonal stripes
        stripe = pygame.Surface(hero.size, pygame.SRCALPHA)
        for i in range(-4, 14):
            x = i * 70
            pygame.draw.polygon(stripe, (226, 36, 41, 14), [(x, hero.h), (x + 30, hero.h), (x + 230, 0), (x + 200, 0)])
        surf.blit(stripe, hero.topleft)
        text(surf, "MARVEL HEROES", (hero.x + 36, hero.y + 34), 44, C["text"], True)
        text(surf, "Offline / private server  ·  client 1.52.0.1700  ·  MHServerEmu",
             (hero.x + 38, hero.y + 96), 17, C["muted"])
        acc = self.s.active_account()
        if self.s.get("join_mode"):
            text(surf, "Joining your friend's server", (hero.x + 38, hero.y + 146), 15, C["muted"])
            text(surf, self.s.get("join_host") or "(no address yet)", (hero.x + 38, hero.y + 166), 26,
                 C["blue"], True, max_w=hero.w - 440)
            text(surf, self.s.get("join_email") or "", (hero.x + 38, hero.y + 200), 15, C["dim"],
                 max_w=hero.w - 440)
        elif acc:
            text(surf, "Playing as", (hero.x + 38, hero.y + 146), 15, C["muted"])
            text(surf, acc.get("player") or acc["email"], (hero.x + 38, hero.y + 166), 26, C["gold"], True,
                 max_w=hero.w - 440)
            text(surf, acc["email"], (hero.x + 38, hero.y + 200), 15, C["dim"], max_w=hero.w - 440)
        else:
            text(surf, "No account selected yet", (hero.x + 38, hero.y + 160), 22, C["yellow"], True)
        # cards
        cw = (CW - 32) // 3
        cy = Y0 + 236 + 18
        srv = app.server
        for i, (title, line1, line2, dot) in enumerate([
            ("Server", srv.state, (f"Up {fmt_uptime(srv.uptime())} · {srv.players_online()} players"
                                   if srv.state == "Online" else srv.message or "Not running"),
             {"Online": C["green"], "Starting": C["yellow"], "Stopping": C["yellow"],
              "Crashed": C["red"]}.get(srv.state, C["dim"])),
            ("Account", (acc.get("player") or acc["email"]) if acc else "None",
             f"{len(self.s.accounts())} saved account(s)", C["green"] if acc else C["yellow"]),
            ("Game client", self._client_line(), self._client_line2(),
             C["green"] if (self.client_info() or {}).get("ok") else C["yellow"]),
        ]):
            r = pygame.Rect(X0 + i * (cw + 16), cy, cw, 190)
            panel(surf, r, C["panel"], 18)
            text(surf, title.upper(), (r.x + 20, r.y + 18), 14, C["muted"], True)
            ui.status_dot(surf, dot, (r.right - 26, r.y + 26), 6, srv.state == "Starting" and i == 0)
            text(surf, line1, (r.x + 20, r.y + 44), 24, C["text"], True, max_w=r.w - 40)
            text(surf, line2, (r.x + 20, r.y + 80), 15, C["muted"], max_w=r.w - 40)
        # checklist
        y = cy + 190 + 18
        box = pygame.Rect(X0, y, CW, CONTENT.bottom - y)
        panel(surf, box, C["panel"], 18)
        text(surf, "READY CHECK", (box.x + 24, box.y + 16), 14, C["muted"], True)
        colw = (CW - 24) // 3
        for i, (key, ok, label, detail, target) in enumerate(self.checks()):
            col, row = i % 3, i // 3
            check_row(surf, box.x + 20 + col * colw, y + 46 + row * 58, ok, label, detail,
                      colw - (84 if ok is not True else 10))

    def _client_line(self):
        ci = self.client_info()
        if not ci:
            return "Not selected"
        return f"v{ci['version']}" if ci["version"] else ("Found" if ci["exe"] else "Problem")

    def _client_line2(self):
        ci = self.client_info()
        if not ci:
            return "Pick MarvelHeroesOmega.exe"
        r = client.pick_runner(self.app.runners_cache(), self.s.get("runner"))
        return f"Runs with {r.name}" if r else "No Proton/Wine found"

    def badge(self):
        return C["accent"] if self.app.game_running else None


# ================================================================= ACCOUNTS
class AccountsScreen(Screen):
    name, title = "accounts", "Accounts"
    alt_hint = "Remove"

    @property
    def default_focus(self):
        return "acc_list" if self.s.accounts() else "f_email"

    def __init__(self, app):
        super().__init__(app)
        self.mode = "create" if not self.s.accounts() else "signin"
        self.index = 0
        self.f = {"email": "", "player": "", "password": "", "confirm": ""}
        self.admin = True
        self.message = ("", "info")

    def setf(self, k):
        def _set(v):
            self.f[k] = v.strip() if k in ("email", "player") else v
        return _set

    def items(self):
        return self.s.accounts()

    def select(self, i):
        accs = self.items()
        if 0 <= i < len(accs):
            self.s.set("active_account", accs[i]["email"])
            self.app.toast(f"Now playing as {accs[i].get('player') or accs[i]['email']}", "ok")

    def remove(self, i):
        accs = self.items()
        if not (0 <= i < len(accs)):
            return
        email = accs[i]["email"]

        def do():
            self.s.remove_account(email)
            self.index = max(0, self.index - 1)
            self.app.toast("Removed from this launcher (the account still exists on the server).", "info")
        self.app.dialog("Remove saved account?",
                        f"{email} will be removed from the launcher's list. Your heroes stay on the server.",
                        [("Cancel", None, "ghost"), ("Remove", do, "danger")])

    def on_button(self, b):
        if b == "y":
            self.remove(self.index)

    def sign_in(self):
        email, pw = self.f["email"].lower(), self.f["password"]
        err = accounts.validate(email, None, pw)
        if err:
            self.message = (err, "error")
            return
        app = self.app

        def work(progress):
            progress(None, "Checking with the server…")
            return accounts.verify_login(app.server, email, pw)

        def done(res):
            ok, msg = res
            if ok is False:
                self.message = (msg, "error")
                return
            existing = self.s.find_account(email)
            self.s.upsert_account(email, existing.get("player", "") if existing else "", pw)
            self.f["password"] = ""
            self.message = (msg if ok else "Saved. " + msg, "ok")
            app.toast(f"Signed in as {email}", "ok")
        if app.server.state == "Online":
            app.run_task("Signing in", work, done)
        else:
            done((None, "The server is off, so the password will be checked when you play."))

    def create(self):
        email, player, pw = self.f["email"].lower(), self.f["player"], self.f["password"]
        err = accounts.validate(email, player, pw, self.f["confirm"])
        if err:
            self.message = (err, "error")
            return
        app = self.app
        make_admin = self.admin

        def work(progress):
            if app.server.state != "Online":
                if not self.s.get("client_root"):
                    raise RuntimeError("Pick your Marvel Heroes game on the Client tab first – "
                                       "the server needs its data files to start.")
                if not app.start_server_blocking(progress):
                    return None
            progress(None, "Creating account…")
            ok, msg = accounts.create_account(app.server, email, player, pw)
            admin_ok = None
            if ok and make_admin:
                progress(None, "Giving admin rights…")
                admin_ok = accounts.make_admin(app.server, email)
            return ok, msg, admin_ok

        def done(res):
            if not res:
                return
            ok, msg, admin_ok = res
            if not ok:
                self.message = (msg, "error")
                return
            self.s.upsert_account(email, player, pw)
            self.f = {"email": "", "player": "", "password": "", "confirm": ""}
            self.message = (f"Account {player} created" + (" with admin rights." if admin_ok else "."), "ok")
            app.toast(f"Welcome, {player}! Press PLAY to start.", "ok", 6)
            self.mode = "signin"
            self.index = len(self.s.accounts()) - 1
        app.run_task("Creating account", work, done, show_log=True)

    def build(self):
        ws = []
        lw = 380
        accs = self.items()
        active = (self.s.get("active_account") or "").lower()

        def render(surf, r, a, sel, focused):
            is_active = a["email"].lower() == active
            text(surf, a.get("player") or "(no player name)", (r.x + 20, r.y + 9), 19,
                 C["text"], True, max_w=r.w - 120)
            text(surf, a["email"], (r.x + 20, r.y + 36), 14, C["muted"], max_w=r.w - 120)
            if is_active:
                chip = pygame.Rect(r.right - 92, r.centery - 13, 78, 26)
                rrect(surf, C["accent"], chip, 13)
                text(surf, "ACTIVE", chip.center, 13, C["text"], True, "center")
            if not a.get("password"):
                text(surf, "no password saved", (r.right - 14, r.bottom - 8), 12, C["yellow"], False,
                     "bottomright")
        ws.append(ui.ListView("acc_list", (X0, Y0 + 44, lw, CH - 44), accs, self.index,
                              lambda i: setattr(self, "index", i), self.select, 70, render,
                              "No accounts yet – create one →", on_alt=self.remove))
        rx, rw = X0 + lw + 24, CW - lw - 24
        ws.append(ui.Button("tab_signin", (rx, Y0, rw // 2 - 6, 52), "Sign in",
                            lambda: setattr(self, "mode", "signin"),
                            "primary" if self.mode == "signin" else "ghost", size=20))
        ws.append(ui.Button("tab_create", (rx + rw // 2 + 6, Y0, rw // 2 - 6, 52), "Create account",
                            lambda: setattr(self, "mode", "create"),
                            "primary" if self.mode == "create" else "ghost", size=20))
        y = Y0 + 84
        fh, gap = 70, 14
        ws.append(ui.TextField("f_email", (rx, y, rw, fh), "Email (any made-up address works)",
                               self.f["email"], self.setf("email"), placeholder="me@deck.local"))
        y += fh + gap
        if self.mode == "create":
            ws.append(ui.TextField("f_player", (rx, y, rw, fh), "Player name (letters & numbers, max 16)",
                                   self.f["player"], self.setf("player"), placeholder="DeckHero"))
            y += fh + gap
            ws.append(ui.TextField("f_pw", (rx, y, rw // 2 - 6, fh), "Password", self.f["password"],
                                   self.setf("password"), password=True, placeholder="3–64 characters"))
            ws.append(ui.TextField("f_confirm", (rx + rw // 2 + 6, y, rw // 2 - 6, fh), "Confirm password",
                                   self.f["confirm"], self.setf("confirm"), password=True,
                                   placeholder="repeat", on_submit=None))
            y += fh + gap
            ws.append(ui.Toggle("f_admin", (rx, y, rw, 62), "Give admin rights",
                                self.admin, lambda v: setattr(self, "admin", v),
                                sub="Lets you use server commands like !server shutdown in chat"))
            y += 62 + gap + 6
            ws.append(ui.Button("f_submit", (rx, y, rw, 66), "Create account", self.create, "primary",
                                size=24))
        else:
            ws.append(ui.TextField("f_pw", (rx, y, rw, fh), "Password", self.f["password"],
                                   self.setf("password"), password=True, on_submit=None))
            y += fh + gap + 6
            ws.append(ui.Button("f_submit", (rx, y, rw, 66), "Sign in", self.sign_in, "primary", size=24))
        return ws

    def draw(self, surf):
        text(surf, "SAVED ACCOUNTS", (X0 + 4, Y0 + 12), 14, C["muted"], True)
        rx, rw = X0 + 404, CW - 404
        msg, kind = self.message
        y = CONTENT.bottom - 96
        box = pygame.Rect(rx, y, rw, 96)
        if msg:
            col = {"ok": C["green"], "error": C["red"]}.get(kind, C["blue"])
            rrect(surf, C["panel"], box, 14)
            rrect(surf, col, pygame.Rect(box.x, box.y, 6, box.h), 3)
            ui.text_block(surf, msg, (box.x + 22, box.y + 16), box.w - 40, 17, C["text"])
        else:
            tip = ("Accounts live on your own server. The email does not have to be real."
                   if self.mode == "create" else
                   "Sign in with an account that already exists on this server. "
                   "The launcher logs you into the game automatically.")
            ui.text_block(surf, tip, (box.x + 4, box.y + 16), box.w - 10, 16, C["dim"])


# =================================================================== SERVER
class ServerScreen(Screen):
    name, title = "server", "Server"
    default_focus = "srv_power"

    def __init__(self, app):
        super().__init__(app)
        self.scroll_off = 0
        self.cmd = ""

    def scroll(self, amount):
        self.scroll_off = max(0, min(len(self.app.server.log) - 5, self.scroll_off - amount))

    def send_cmd(self):
        c = self.cmd.strip()
        if not c:
            return
        if not self.app.server.running:
            self.app.toast("Server is not running.", "warn")
            return
        if not c.startswith("!"):
            c = "!" + c
        self.app.server.send(c)
        self.cmd = ""
        self.scroll_off = 0

    def badge(self):
        st = self.app.server.state
        return {"Online": C["green"], "Starting": C["yellow"], "Crashed": C["red"]}.get(st)

    def build(self):
        app, srv = self.app, self.app.server
        ws = []
        on = srv.running
        ws.append(ui.Button("srv_power", (X0, Y0, 300, 150), "TURN OFF" if on else "TURN ON", app.toggle_server,
                            "danger" if on else "success", size=30,
                            sub="Saves players, then stops" if on else "Start MHServerEmu",
                            enabled=srv.state != "Stopping"))
        lw = CW - 380 - 24
        ws.append(ui.TextField("srv_cmd", (X0, CONTENT.bottom - 70, lw, 70),
                               "Console command (e.g. !commands)", self.cmd,
                               lambda v: setattr(self, "cmd", v), placeholder="!account info",
                               on_submit=self.send_cmd))
        tx = X0 + lw + 24
        y = Y0 + 174
        opts = list(SERVER_TOGGLES)
        for full, label, default in opts:
            val = srv.option(full, default).lower() == "true"
            ws.append(ui.Toggle(f"opt_{full}", (tx, y, 380, 56), label, val,
                                lambda v, full=full: self._set_opt(full, v)))
            y += 62
        ws.append(ui.Toggle("opt_lan", (tx, y, 380, 56), "Host for friends (Friends tab)",
                            bool(self.s.get("lan_mode")), self._set_lan))
        return ws

    def _set_opt(self, full, v):
        self.app.server.set_option(full, "true" if v else "false")
        if self.app.server.running:
            self.app.toast("Saved. Restart the server to apply.", "info")

    def _set_lan(self, v):
        next(sc for sc in self.app.screens if sc.name == "friends").set_hosting(v)

    def draw(self, surf):
        app, srv, br = self.app, self.app.server, self.app.bridge
        info = pygame.Rect(X0 + 324, Y0, CW - 324, 150)
        panel(surf, info, C["panel"], 18)
        rows = [
            ("Status", srv.state),
            ("Uptime", fmt_uptime(srv.uptime()) if srv.running else "—"),
            ("Players", str(srv.players_online()) if srv.state == "Online" else "—"),
            ("Login bridge", ("Running" + (f" · {br.auth_requests} logins" if br.auth_requests else ""))
             if br.running else (br.error or "Off")),
            ("Login ports", "Ready" if ports_allowed() else "Need one-time setup (Settings)"),
            ("Friends", (f"hosting on {app.host_address()} · {len(br.friends_online())} connected")
             if self.s.get("lan_mode") else "not hosting (Friends tab)"),
        ]
        for i, (k, v) in enumerate(rows):
            cx = info.x + 24 + (i % 2) * (info.w // 2)
            cy = info.y + 18 + (i // 2) * 42
            text(surf, k.upper(), (cx, cy), 12, C["muted"], True)
            text(surf, v, (cx, cy + 16), 17, C["text"], True, max_w=info.w // 2 - 40)
        # log
        lw = CW - 380 - 24
        box = pygame.Rect(X0, Y0 + 174, lw, CH - 174 - 84)
        rrect(surf, (9, 10, 14), box, 14)
        rrect(surf, C["line"], box, 14, 1)
        text(surf, "SERVER LOG", (box.x + 16, box.y + 12), 12, C["muted"], True)
        if self.scroll_off:
            text(surf, f"↑ {self.scroll_off} lines up  (RT to go down)", (box.right - 16, box.y + 12), 12,
                 C["yellow"], False, "topright")
        lh = 19
        n = (box.h - 44) // lh
        log = srv.log
        end = max(0, len(log) - self.scroll_off)
        lines = log[max(0, end - n):end]
        y = box.y + 36
        clip = surf.get_clip()
        surf.set_clip(box.inflate(-8, -8))
        for ln in lines:
            col = C["red"] if (" ERROR" in ln or "FATAL" in ln or "Exception" in ln) else \
                C["yellow"] if " WARN" in ln else C["blue"] if ln.startswith(">") else (190, 196, 210)
            text(surf, ln, (box.x + 14, y), 14, col, max_w=box.w - 28)
            y += lh
        if not lines:
            text(surf, "The log appears here when the server runs.", box.center, 16, C["dim"], False, "center")
        surf.set_clip(clip)
        text(surf, "OPTIONS", (X0 + lw + 28, Y0 + 158), 12, C["muted"], True)


# =================================================================== CLIENT
class ClientScreen(Screen):
    name, title = "client", "Game Client"
    default_focus = "cl_scan"

    def __init__(self, app):
        super().__init__(app)
        self.scan_results = None

    def badge(self):
        ci = self.app.screens[0].client_info()
        return None if ci and ci["ok"] else C["yellow"]

    def set_client(self, path):
        info = client.inspect_client(path)
        if not info["root"] or not info["exe"]:
            self.app.dialog("That doesn't look like Marvel Heroes", "\n".join(info["problems"]) +
                            "\n\nPick MarvelHeroesOmega.exe (inside UnrealEngine3/Binaries/Win64) "
                            "or the 'Marvel Heroes' folder.")
            return
        self.s.set("client_root", info["root"], save=False)
        self.s.set("client_exe", info["exe"])
        self.app.screens[0].refresh()
        if info["sip"] and self.app.server.installed():
            try:
                copied = self.app.server.sync_game_data(info["root"])
                if copied:
                    self.app.toast("Copied game data to the server: " + ", ".join(copied), "ok")
            except Exception as e:
                self.app.toast(f"Could not copy game data: {e}", "error")
        if info["ok"]:
            self.app.toast(f"Marvel Heroes {info['version'] or ''} found!", "ok")
        else:
            self.app.dialog("Game found, but…", "\n".join(info["problems"]))

    def scan(self):
        app = self.app

        def work(progress):
            def p(path):
                progress(None, "Searching " + path[-80:])
            return client.scan_for_clients(stop_event=progress.cancelled, progress=p)

        def done(found):
            if not found:
                app.dialog("Game not found", "Couldn't find MarvelHeroesOmega.exe in your Steam libraries, "
                           "home folder or SD card. Use Browse to pick it yourself.",
                           [("Browse…", self.browse, "primary")])
            elif len(found) == 1:
                self.set_client(found[0])
            else:
                app.dialog("Several copies found", "Pick one:",
                           [(os.path.basename(f.rstrip("/")) + " – " + os.path.dirname(f)[-24:],
                             (lambda f=f: self.set_client(f)), "secondary") for f in found[:3]], width=1000)
        app.run_task("Looking for Marvel Heroes", work, done, cancellable=True)

    def browse(self):
        start = self.s.get("client_root") or os.path.expanduser("~")
        self.app.browse("Pick MarvelHeroesOmega.exe (or the game folder)", start, (".exe",),
                        self.set_client, pick_dirs=True)

    def build(self):
        ws = []
        ws.append(ui.Button("cl_scan", (X0, Y0 + 196, (CW - 16) // 2, 64), "Find my game automatically",
                            self.scan, "primary", size=21))
        ws.append(ui.Button("cl_browse", (X0 + (CW + 16) // 2, Y0 + 196, (CW - 16) // 2, 64),
                            "Browse for EXE / folder…", self.browse, "secondary", size=21))
        runners = self.app.runners_cache()
        names = [r.name for r in runners]
        ids = [r.id for r in runners]
        cur = self.s.get("runner")
        auto = client.pick_runner(runners, "")
        names = [f"Automatic ({auto.name})" if auto else "Automatic (none found)"] + names
        ids = [""] + ids
        idx = ids.index(cur) if cur in ids else 0
        y = Y0 + 290
        half = (CW - 16) // 2
        ws.append(ui.Selector("cl_runner", (X0, y, half, 70), "Run the Windows game with", names, idx,
                              lambda i: self.s.set("runner", ids[i])))
        ws.append(ui.Button("cl_rescan", (X0 + half + 16, y, half, 70), "Refresh Proton / Wine list",
                            self.app.refresh_runners, "ghost", size=19))
        y += 86
        if cur == "custom":
            ws.append(ui.TextField("cl_custom", (X0, y, CW, 70), "Custom command ({exe} and {args} are replaced)",
                                   self.s.get("custom_runner_cmd"), lambda v: self.s.set("custom_runner_cmd", v),
                                   placeholder="lutris-wine {exe} {args}"))
            y += 86
        tw = (CW - 16) // 2
        ws.append(ui.Toggle("cl_autologin", (X0, y, tw, 62), "Log in automatically", self.s.get("auto_login"),
                            lambda v: self.s.set("auto_login", v), sub="Uses the active account"))
        ws.append(ui.Toggle("cl_deck", (X0 + tw + 16, y, tw, 62), "Steam Deck resolution",
                            self.s.get("deck_resolution"), lambda v: self.s.set("deck_resolution", v),
                            sub="1280 × 800"))
        y += 76
        ws.append(ui.Toggle("cl_movies", (X0, y, tw, 62), "Skip intro movies", self.s.get("skip_movies"),
                            lambda v: self.s.set("skip_movies", v)))
        ws.append(ui.TextField("cl_args", (X0 + tw + 16, y - 4, tw, 70), "Extra launch options",
                               self.s.get("extra_client_args"), lambda v: self.s.set("extra_client_args", v),
                               placeholder="e.g. -nosound"))
        return ws

    def draw(self, surf):
        ci = self.app.screens[0].client_info()
        box = pygame.Rect(X0, Y0, CW, 176)
        panel(surf, box, C["panel"], 18)
        text(surf, "MARVEL HEROES CLIENT", (box.x + 24, box.y + 18), 14, C["muted"], True)
        if not ci:
            text(surf, "No game selected", (box.x + 24, box.y + 46), 26, C["text"], True)
            ui.text_block(surf, "You need the Windows version 1.52.0.1700 of Marvel Heroes (the final Steam "
                          "release). Press 'Find my game' to search your Steam library, home folder and SD card, "
                          "or browse to MarvelHeroesOmega.exe.", (box.x + 24, box.y + 88), box.w - 48, 16,
                          C["muted"])
            return
        ok = ci["ok"]
        text(surf, ("Ready  ·  v" + (ci["version"] or "?")) if ok else "Needs attention", (box.x + 24, box.y + 44),
             26, C["green"] if ok else C["yellow"], True)
        text(surf, "Folder:  " + (ci["root"] or "—"), (box.x + 24, box.y + 88), 16, C["text"], max_w=box.w - 48)
        exe = ci["exe"] or "—"
        if ci["root"] and exe.startswith(ci["root"]):
            exe = exe[len(ci["root"]) + 1:]
        text(surf, "Program:  " + exe + ("  (64-bit)" if ci["exe"] and client.is_64bit_exe(ci["exe"]) else ""),
             (box.x + 24, box.y + 114), 16, C["text"], max_w=box.w - 48)
        if ci["problems"]:
            text(surf, "⚠ " + ci["problems"][0], (box.x + 24, box.y + 142), 16, C["yellow"], max_w=box.w - 48)
        else:
            text(surf, "Game data files present · launch options: -robocopy -nosteam -siteconfigurl=…",
                 (box.x + 24, box.y + 142), 15, C["muted"], max_w=box.w - 48)


# ================================================================= SETTINGS
class SettingsScreen(Screen):
    name, title = "settings", "Settings"
    default_focus = "set_dotnet"

    @staticmethod
    def fix_ports(app):
        app.offer_port_fix("Open ports 80 and 443 for the login bridge (one time) and stop any web server "
                           "that is blocking them. You will be asked for your password once.",
                           servers=integration.find_blocking_web_servers())

    def install_dotnet(self):
        def work(progress):
            return dotnet.install(lambda f, s: progress(f, s))
        self.app.run_task("Installing .NET 8 runtime", work, lambda _r: (self.app.screens[0].refresh(),
                                                                          self.app.toast(".NET installed", "ok")))

    def import_zip(self):
        def picked(path):
            if self.app.server.running:
                self.app.dialog("Stop the server first", "Turn the server off before updating it.")
                return
            self.app.run_task("Updating MHServerEmu", lambda p: self.app.server.import_zip(path, p),
                              lambda _r: self.app.toast("Server updated. Your accounts were kept.", "ok"))
        self.app.browse("Pick an MHServerEmu release .zip (linux-x64)", os.path.expanduser("~/Downloads"),
                        (".zip",), picked)

    def add_steam(self):
        ok, msg = integration.add_to_steam()
        if ok:
            self.app.toast(msg, "ok", 8)
        else:
            self.app.dialog("Add to Steam", msg, width=900)

    def build(self):
        ws = []
        bw = 250
        bx = X0 + CW - bw - 20
        y = Y0 + 20
        dn, _v = self.app.screens[0].dotnet_info()
        ws.append(ui.Button("set_dotnet", (bx, y, bw, 52), "Reinstall .NET" if dn else "Install .NET",
                            self.install_dotnet, "ghost" if dn else "primary", size=18))
        y += 76
        ws.append(ui.Button("set_ports", (bx, y, bw, 52), "Check again" if ports_allowed() else "Enable ports",
                            (lambda: self.app.toast("Ports are ready", "ok")) if ports_allowed()
                            else (lambda: SettingsScreen.fix_ports(self.app)),
                            "ghost" if ports_allowed() else "primary", size=18))
        y += 76
        ws.append(ui.Button("set_zip", (bx, y, bw, 52), "Import server .zip", self.import_zip, "ghost", size=18))
        y += 76
        ws.append(ui.Button("set_steam", (bx, y, bw, 52), "Add to Steam", self.add_steam, "ghost", size=18))
        y = Y0 + 336
        tw = (CW - 16) // 2
        toggles = [
            ("set_autostart", "Start server when I press Play", "auto_start_server", None),
            ("set_stopexit", "Stop server when the launcher closes", "stop_server_on_exit", None),
            ("set_remember", "Remember passwords", "remember_passwords", "Stored in ~/.config (owner only)"),
            ("set_fullscreen", "Fullscreen launcher", "fullscreen", "Applies next start"),
        ]
        for i, (key, label, skey, sub) in enumerate(toggles):
            val = self.s.get(skey)
            if skey == "fullscreen" and val is None:
                val = in_gaming_mode()
            ws.append(ui.Toggle(key, (X0 + (i % 2) * (tw + 16), y + (i // 2) * 74, tw, 62), label, bool(val),
                                lambda v, skey=skey: self._set(skey, v), sub=sub))
        return ws

    def _set(self, key, v):
        self.s.set(key, v)
        if key == "remember_passwords":
            self.s.save()

    def draw(self, surf):
        srv = self.app.server
        dn, ver = self.app.screens[0].dotnet_info()
        box = pygame.Rect(X0, Y0, CW, 316)
        panel(surf, box, C["panel"], 18)
        dll = srv.dll_path()
        built = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(dll))) if os.path.isfile(dll) else None
        rows = [
            (True if dn else None, ".NET runtime",
             f"{'.'.join(map(str, ver))} at {os.path.dirname(dn)}" if dn else "Not installed – downloads ~30 MB, no admin needed"),
            (ports_allowed() or None, "Login ports 80 / 443",
             "Ready" if ports_allowed() else "One-time setup needed so the game can log in"),
            (srv.installed() or False, "MHServerEmu",
             f"Installed in {srv.dir}" + (f" (build {built})" if built else "") if srv.installed()
             else "Missing – import a release zip"),
            (None, "Steam", "Adds this launcher to your Steam library for Gaming Mode"),
        ]
        for i, (ok, label, detail) in enumerate(rows):
            check_row(surf, box.x + 24, box.y + 28 + i * 76, ok, label, detail, box.w - 320)
        text(surf, f"Logs: {LOG_DIR}", (X0 + 4, CONTENT.bottom - 22), 14, C["dim"], max_w=CW)
