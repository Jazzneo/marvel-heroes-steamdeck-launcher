"""Friends tab: host your server for friends, or join a friend's server."""
import os
import shutil
import subprocess
import threading
import time

import pygame

from . import ui, network, integration, accounts, hotspot
from .ui import C, text, rrect, panel
from .app import CONTENT
from .authproxy import ports_allowed
from .config import DATA_DIR

# While this file exists (and the launcher runs), the firewall stays open for friends. Deleting it makes the
# root watchdog close the firewall again within ~2 seconds, without asking for a password.
FIREWALL_FLAG = os.path.join(DATA_DIR, "hosting-firewall.flag")


def watchdog_running():
    """Is a firewall watchdog (started by an earlier open) still alive? /proc cmdlines are world-readable."""
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if b"mhl-firewall-watch" in f.read():
                        return True
            except OSError:
                pass
    return False


from .screens import Screen, check_row

X0, Y0, CW, CH = CONTENT.x, CONTENT.y, CONTENT.w, CONTENT.h
COL = (CW - 18) // 2
LX, RX = X0, X0 + COL + 18


def copy_to_clipboard(value):
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=value, text=True, timeout=3)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


class FriendsScreen(Screen):
    name, title = "friends", "Friends"
    default_focus = "fr_host"

    def __init__(self, app):
        super().__init__(app)
        self.options = []
        self.options_at = 0
        self.fw_state = None
        self.join_msg = ("", "info")
        self.status_cache = None
        self._refresh_lock = threading.Lock()
        self.ts_wait_until = 0          # waiting for a Tailscale login to finish

    def badge(self):
        if self.s.get("lan_mode"):
            return C["green"]
        if self.s.get("join_mode"):
            return C["blue"]
        return None

    # --------------------------------------------------------------- state
    def update(self):
        waiting = time.time() < self.ts_wait_until
        every = 3 if waiting else 6
        if (self.app.screen is self or waiting) and time.time() - self.options_at > every:
            self.options_at = time.time()
            threading.Thread(target=self._refresh, daemon=True).start()
        if waiting and network.snapshot(max_age=1e9)["tailscale"]:
            self.ts_wait_until = 0
            self.app.toast(f"Tailscale connected: {network.snapshot()['tailscale']}", "ok", 8)

    def _refresh(self):
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            snap = network.refresh_snapshot()
            self.options = network.address_options(snap)
            self.fw_state = snap["fw_open"] if self.s.get("lan_mode") else None
        finally:
            self._refresh_lock.release()

    def kinds(self):
        if not self.options:
            self.options = network.address_options(network.snapshot())
        return self.options

    def invite(self):
        return self.app.host_address() if self.s.get("lan_mode") else None

    # --------------------------------------------------------------- hosting
    def set_hosting(self, on):
        app = self.app
        self.s.set("lan_mode", bool(on))
        if on:
            self.s.set("join_mode", False)
            kind = self.s.get("host_kind")
            if kind == "tailscale" and not network.snapshot()["tailscale"]:
                app.dialog("Tailscale isn't running", "Install Tailscale (Bazzite has it built in; on Steam Deck use "
                           "the Tailscale Flatpak or tailscale.com/download), log in, and share this machine with "
                           "your friend. Or pick another way on the Friends tab.")
            self._after_host_change()
            if kind == "direct":
                self.start_hotspot()
                return
            self.open_access(silent_if_ok=True)
            if kind == "internet":
                self.open_router()
        else:
            if self.s.get("upnp_open"):
                threading.Thread(target=lambda: (network.close_router_ports(), self.s.set("upnp_open", False)),
                                 daemon=True).start()
            self._after_host_change()
            self.close_firewall(reason="hosting turned off")
            if network.snapshot().get("hotspot"):
                self.stop_hotspot()

    def _after_host_change(self):
        """Hosting changes how the server listens: restart it if it's running."""
        app = self.app
        if not app.server.running:
            return

        def work(p):
            p(None, "Restarting the server for friends…" if self.s.get("lan_mode") else "Restarting the server…")
            app.server.stop()
            app.bridge.stop()
            return app.start_server_blocking(p)
        app.run_task("Restarting server", work,
                     lambda ok: ok and app.toast("Server restarted" + (" – friends can join now" if self.s.get(
                         "lan_mode") else ""), "ok"), show_log=True)

    def open_access(self, silent_if_ok=False):
        """One admin prompt: ports 80/443 for the launcher (if needed) and the firewall for friends."""
        app = self.app
        need_ports = not ports_allowed()
        snap = network.snapshot()
        iface = snap["hotspot"][0] if snap.get("hotspot") else None
        fw_lines = network.firewall_script(fw=snap["fw"], launcher_pid=os.getpid(), flag_path=FIREWALL_FLAG,
                                           iface=iface)
        fw_ok = snap["fw_open"] if fw_lines else True
        if not need_ports and (not fw_lines or fw_ok is True):
            if not silent_if_ok:
                app.toast("Ports and firewall are already open for friends", "ok")
            return
        lines = ["set +e"]
        if need_ports:
            lines += integration.build_fix_script(True, []).splitlines()[1:-1]
        lines += fw_lines
        lines.append("exit 0")
        kind, _active = snap["fw"]
        what = []
        if need_ports:
            what.append("open ports 80/443 for the launcher (one time)")
        if fw_lines:
            what.append(f"open ports {', '.join(map(str, network.FRIEND_PORTS))} in your firewall ({kind}) while you "
                        "host – it closes again by itself when you stop hosting, the server stops or the launcher "
                        "closes")

        def go():
            def work(p):
                p(None, "Waiting for your password…")
                os.makedirs(os.path.dirname(FIREWALL_FLAG), exist_ok=True)
                with open(FIREWALL_FLAG, "w") as f:
                    f.write(str(os.getpid()))
                return integration.run_as_admin("\n".join(lines))

            def done(res):
                ok, info = res
                if ok:
                    ports, ts, zoned = network.parse_added(info)
                    if ports or ts or zoned:
                        self.s.set("fw_added_ports", sorted(set(self.s.get("fw_added_ports") or []) | set(ports)),
                                   save=False)
                        self.s.set("fw_added_other", sorted(set(self.s.get("fw_added_other") or []) | set(zoned)),
                                   save=False)
                        self.s.set("fw_added_tailscale", bool(self.s.get("fw_added_tailscale") or ts), save=False)
                        self.s.set("fw_watchdog", "WATCHDOG" in info)
                    self.fw_state = True
                    network.mark_firewall_open(True)
                    threading.Thread(target=network.refresh_snapshot, daemon=True).start()
                    app.toast("Friends can reach your server now", "ok")
                else:
                    try:
                        os.remove(FIREWALL_FLAG)
                    except OSError:
                        pass
                    app.dialog("Couldn't open it automatically", f"{info}\n\nThe firewall was left closed.",
                               width=900)
            app.run_task("Opening for friends", work, done)
        app.dialog("Let friends in", "To host, the launcher needs to " + " and ".join(what) +
                   ". You'll be asked for your password once.",
                   [("Not now", None, "ghost"), ("Open it", go, "primary")], width=900)

    def firewall_opened_by_us(self):
        return bool(self.s.get("fw_added_ports") or self.s.get("fw_added_tailscale") or self.s.get("fw_added_other"))

    def close_firewall(self, then=None, reason=None, quiet=False):
        """Close what the launcher opened for friends.

        Normal case: delete the hosting flag; the root watchdog removes the rules within ~2 s (no password).
        Rules from launcher versions before 1.2.4 (permanent, no watchdog) need one password prompt."""
        app = self.app
        if not self.firewall_opened_by_us():
            if then:
                then()
            return
        if self.s.get("fw_watchdog"):
            try:
                os.remove(FIREWALL_FLAG)
            except OSError:
                pass

            def work(p):
                p(None, "Closing the firewall…")
                time.sleep(3)           # the watchdog checks every 2 s
                return True

            def done(_r):
                self._forget_firewall()
                if not quiet:
                    app.toast("Firewall closed" + (f" ({reason})" if reason else "") +
                              ". Your own games keep working.", "ok", 6)
                if then:
                    then()
            if quiet:
                threading.Thread(target=lambda: (time.sleep(3), app.call_soon(lambda: done(None))),
                                 daemon=True).start()
            else:
                app.run_task("Closing the firewall", work, done)
            return
        # rules opened by an older launcher version: permanent, so remove them as admin
        ports = list(self.s.get("fw_added_ports") or [])
        ts = bool(self.s.get("fw_added_tailscale"))
        lines = network.firewall_close_script(ports, ts, fw=network.snapshot()["fw"])
        if not lines:
            self._forget_firewall()
            if then:
                then()
            return

        def work(p):
            p(None, "Waiting for your password…")
            return integration.run_as_admin("set +e\n" + "\n".join(lines) + "\nfirewall-cmd --reload "
                                            ">/dev/null 2>&1\nexit 0")

        def done(res):
            ok, info = res
            if ok:
                self._forget_firewall()
                app.toast("Firewall closed again. Your own games keep working.", "ok", 6)
            else:
                app.dialog("Couldn't close it automatically", f"{info}\n\nOpen Konsole and run:\n\n" +
                           "\n".join("sudo " + l.replace(" >/dev/null 2>&1", "") for l in lines) +
                           "\nsudo firewall-cmd --reload", width=1000)
            if then:
                then()
        app.run_task("Closing the firewall", work, done)

    def _forget_firewall(self):
        self.s.set("fw_added_other", [], save=False)
        self.s.set("fw_added_ports", [], save=False)
        self.s.set("fw_added_tailscale", False, save=False)
        self.s.set("fw_watchdog", False)
        self.fw_state = False
        network.mark_firewall_open(False)
        threading.Thread(target=network.refresh_snapshot, daemon=True).start()

    def startup_cleanup(self):
        """At launcher start: the watchdog of an earlier run has closed everything (its launcher is gone)."""
        title = "Firewall still open from an older version"
        body = "An earlier version of the launcher left your firewall open for friends"
        if self.s.get("fw_watchdog"):
            try:
                os.remove(FIREWALL_FLAG)
            except OSError:
                pass
            # firewalld rules are runtime-only (a reboot clears them too). ufw stores its rules on disk, so if the
            # PC was switched off while hosting, the watchdog never got to remove them -> close them now.
            if network.firewall()[0] == "ufw" and not watchdog_running():
                self.s.set("fw_watchdog", False)
                title = "Firewall still open"
                body = "The PC was switched off while you were hosting, so your firewall is still open for friends"
            else:
                self.s.set("fw_added_ports", [], save=False)
                self.s.set("fw_added_other", [], save=False)
                self.s.set("fw_added_tailscale", False, save=False)
                self.s.set("fw_watchdog", False)
                self._cleanup_hotspot_leftovers()
                return
        self._cleanup_hotspot_leftovers()
        if self.firewall_opened_by_us():
            # permanent rules (launcher v1.2.0-1.2.3, or ufw after a power-off): offer to close them once
            self.app.call_soon(lambda: self.app.dialog(
                title, body + " (ports " + ", ".join(map(str, self.s.get("fw_added_ports") or [])) +
                "). Close it now? From now on the launcher closes it by itself.",
                [("Later", None, "ghost"), ("Close it", self.close_firewall, "primary")], width=940))

    def open_router(self):
        app = self.app

        def work(p):
            p(None, "Asking your router to forward ports 80, 443 and 4306 (UPnP)…")
            return network.open_router_ports()

        def done(res):
            ip, warning = res
            self.s.set("public_ip", ip or "")
            self.s.set("upnp_open", True)
            if warning:
                app.dialog("Router ports opened, but…", warning)
            else:
                app.toast(f"Router ports open. Your internet address: {ip}", "ok", 8)
        app.run_task("Opening router ports", work, done)

    def set_kind(self, i):
        opts = self.kinds()
        kind = opts[i][0]
        old = self.s.get("host_kind")
        self.s.set("host_kind", kind)
        if old == "direct" and kind != "direct" and network.snapshot().get("hotspot"):
            self.stop_hotspot()
        if self.s.get("lan_mode") and kind == "internet" and not self.s.get("upnp_open"):
            self.open_router()

    # --------------------------------------------------------------- Deck to Deck
    def _cleanup_hotspot_leftovers(self):
        """A crashed launcher may have left the hotspot on: switch Wi-Fi back to normal (in the background)."""
        def work():
            try:
                left = hotspot.cleanup_leftovers()
            except Exception as e:
                print("[hotspot] cleanup failed:", e)
                return
            if left:
                network.mark_hotspot(None, None)
                self.app.call_soon(lambda: self.app.toast("Turned off a Deck-to-Deck hotspot left from last time",
                                                          "info", 6))
        threading.Thread(target=work, daemon=True).start()

    def hotspot_creds(self):
        ssid = self.s.get("hotspot_ssid")
        pw = self.s.get("hotspot_password")
        if not ssid:
            acc = self.s.active_account() or {}
            ssid = hotspot.make_ssid(acc.get("player") or "")
            self.s.set("hotspot_ssid", ssid, save=False)
        if not pw or len(pw) < 8:
            pw = hotspot.make_password()
            self.s.set("hotspot_password", pw, save=False)
        self.s.save()
        return ssid, pw

    def start_hotspot(self, confirm=True):
        app = self.app
        if not hotspot.available():
            app.dialog("Deck to Deck isn't available", "This system doesn't use NetworkManager (nmcli), which the "
                       "launcher needs to make a Wi-Fi hotspot. Pick another way on the Friends tab.", width=900)
            return
        if confirm:
            app.dialog("Start Deck-to-Deck hotspot?",
                       "This Deck's Wi-Fi turns into its own small network for your friend's Deck. While it's on, "
                       "this Deck has no internet (the game and server don't need it). Your usual Wi-Fi comes back "
                       "when you stop hosting or close the launcher.",
                       [("Cancel", None, "ghost"), ("Start hotspot", lambda: self.start_hotspot(False), "primary")],
                       width=940)
            return
        ssid, pw = self.hotspot_creds()

        def work(p):
            p(None, f"Starting Wi-Fi hotspot {ssid}…")
            return hotspot.start_host(ssid, pw)

        def done(res):
            dev, ip = res
            network.mark_hotspot((dev, ip), None)
            self.options = network.address_options(network.snapshot())
            if not self.s.get("lan_mode"):
                self.s.set("lan_mode", True)
                self.s.set("join_mode", False)
                self._after_host_change()
            app.dialog("Hotspot is on",
                       f"On your friend's Deck: Friends tab → Find Deck, pick {ssid} and type the password "
                       f"{pw}.\n\nThen they create an account on your server (once) and press PLAY.", width=900)
            threading.Thread(target=network.refresh_snapshot, daemon=True).start()
            self.open_access(silent_if_ok=True)
        app.run_task("Deck to Deck", work, done)

    def stop_hotspot(self):
        def work(p):
            p(None, "Turning off the hotspot and reconnecting your usual Wi-Fi…")
            hotspot.stop_host()

        def done(_r):
            network.mark_hotspot(None, network.snapshot().get("joined"))
            self.options = network.address_options(network.snapshot())
            threading.Thread(target=network.refresh_snapshot, daemon=True).start()
            self.app.toast("Hotspot off – your usual Wi-Fi reconnects by itself", "ok", 6)
        self.app.run_task("Deck to Deck", work, done)

    def new_hotspot_password(self):
        self.s.set("hotspot_password", hotspot.make_password())
        if network.snapshot().get("hotspot"):
            self.start_hotspot(confirm=False)     # restart with the new password

    def find_deck(self):
        app = self.app
        if network.snapshot().get("joined"):
            return self.leave_deck()
        if not hotspot.available():
            self.join_msg = ("Finding a Deck needs NetworkManager (nmcli), which this system doesn't have.", "error")
            return

        def work(p):
            p(None, "Looking for a Deck hotspot nearby…")
            return hotspot.scan()

        def done(found):
            if not found:
                self.join_msg = ("No Deck hotspot found. On the other Deck: Friends tab → Host for friends → "
                                 "'Deck to Deck (own Wi-Fi hotspot)'. Keep the Decks close, then press Find Deck "
                                 "again.", "error")
                return
            buttons = [(f"{ssid[len(hotspot.SSID_PREFIX):]}  {sig}%", (lambda ssid=ssid: self._ask_password(ssid)),
                        "primary" if i == 0 else "secondary") for i, (ssid, sig) in enumerate(found[:3])]
            app.dialog("Pick your friend's Deck",
                       "Found " + ", ".join(s for s, _ in found[:3]) + ". Your friend sees the name and password on "
                       "their Friends tab.", [("Cancel", None, "ghost")] + buttons, width=1060)
        app.run_task("Find Deck", work, done)

    def _ask_password(self, ssid):
        self.app.open_keyboard(f"Wi-Fi password for {ssid}", "", lambda pw: self._join_deck(ssid, pw), password=False)

    def _join_deck(self, ssid, pw):
        app = self.app
        if len(pw) < 8:
            self.join_msg = ("The hotspot password has 8 digits – check your friend's Friends tab.", "error")
            return

        def work(p):
            p(None, f"Connecting to {ssid}…")
            try:
                host = hotspot.join(ssid, pw)
            except hotspot.HotspotError as e:
                return None, [str(e)]
            p(None, "Checking your friend's server…")
            problems = network.check_host(host)
            return host, problems

        def done(res):
            host, problems = res
            if host is None:
                self.join_msg = (f"Couldn't join {ssid}: {problems[0]} Press Find Deck to try again.", "error")
                return
            network.mark_hotspot(network.snapshot().get("hotspot"), ssid)
            self.s.set("join_host", host)
            if self.s.get("lan_mode"):
                self.set_hosting(False)
            self.s.set("join_mode", True)
            if problems:
                self.join_msg = (f"Connected to {ssid}, but the server didn't answer yet: " + problems[0] +
                                 " Make sure your friend's server is ON, then press Test connection.", "info")
            else:
                self.join_msg = (f"Connected to {ssid}! Create an account on your friend's server once (fill in "
                                 "the fields and press Create account there), then press PLAY.", "ok")
        app.run_task("Joining Deck", work, done)

    def leave_deck(self):
        def work(p):
            p(None, "Leaving the hotspot and reconnecting your usual Wi-Fi…")
            hotspot.leave()

        def done(_r):
            network.mark_hotspot(network.snapshot().get("hotspot"), None)
            threading.Thread(target=network.refresh_snapshot, daemon=True).start()
            self.join_msg = ("Left your friend's hotspot. Your usual Wi-Fi reconnects by itself.", "info")
        self.app.run_task("Leaving Deck", work, done)

    def setup_tailscale(self):
        app = self.app
        if not network.tailscale_installed():
            app.dialog("Install Tailscale first",
                       "Tailscale isn't installed on this system. Install it from tailscale.com/download "
                       "(Bazzite includes it; for SteamOS see tailscale.com/download/linux/steamos), then press "
                       "Set up Tailscale again. Your friend needs Tailscale too.", width=900)
            return
        import getpass
        user = getpass.getuser()

        def work(p):
            p(None, "Checking Tailscale…")
            state = network.tailscale_state()
            if state == "Running":
                network.refresh_snapshot()
                return ("running", None)
            admin_done = False
            if state == "daemon-off":
                p(None, "Turning on Tailscale (password prompt)…")
                ok, info = integration.run_as_admin(network.tailscale_admin_script(user))
                if not ok:
                    raise RuntimeError("Couldn't turn on Tailscale: " + info)
                admin_done = True
            p(None, "Starting Tailscale login…")
            try:
                url, out = network.tailscale_login()
            except PermissionError:
                if admin_done:
                    raise RuntimeError("Tailscale refused the login even after setup. Run 'sudo tailscale up' once "
                                       "in a terminal.")
                p(None, "Allowing the launcher to use Tailscale (password prompt)…")
                ok, info = integration.run_as_admin(network.tailscale_admin_script(user))
                if not ok:
                    raise RuntimeError("Couldn't set up Tailscale: " + info)
                url, out = network.tailscale_login()
            return ("login", url)

        def done(res):
            what, url = res
            if what == "running":
                self.options_at = 0
                app.toast("Tailscale is already running", "ok")
                return
            self.ts_wait_until = time.time() + 600
            if url:
                opened = network.open_url(url)
                app.dialog("Log in to Tailscale",
                           ("A browser window opened. " if opened else "") + "Log in to Tailscale there (free "
                           f"account), then come back – your address appears here by itself.\n\nLogin page: {url}"
                           "\n\nThen let your friend in: invite them to your tailnet, or share this machine "
                           "with them from login.tailscale.com/admin/machines.", width=1000)
            else:
                app.toast("Tailscale is starting – your address appears in a moment.", "ok", 6)
        app.run_task("Setting up Tailscale", work, done)

    def copy_invite(self):
        if self.s.get("host_kind") == "direct":
            ssid, pw = self.hotspot_creds()
            addr = f"Wi-Fi {ssid}, password {pw}"
            if copy_to_clipboard(addr):
                self.app.toast(f"Copied – {addr}", "ok")
            else:
                self.app.toast(addr, "info", 8)
            return
        addr = self.invite()
        if not addr:
            return
        if copy_to_clipboard(addr):
            self.app.toast(f"Copied {addr} – send it to your friend", "ok")
        else:
            self.app.toast(f"Send your friend this address: {addr}", "info", 8)

    # --------------------------------------------------------------- joining
    def set_join(self, on):
        self.s.set("join_mode", bool(on))
        if on and self.s.get("lan_mode"):
            self.set_hosting(False)
        if not on and network.snapshot().get("joined"):
            self.leave_deck()
        self.app.toast("PLAY now joins your friend's server" if on else "PLAY uses your own server again", "info")

    def setf(self, key):
        def _set(v):
            self.s.set(key, v.strip() if key != "join_password" else v)
        return _set

    def test(self):
        host = (self.s.get("join_host") or "").strip()
        if not host:
            self.join_msg = ("Enter your friend's address first.", "error")
            return

        def work(p):
            p(None, f"Contacting {host}…")
            problems = network.check_host(host)
            status = None if problems else network.friend_server_status(host)
            return problems, status

        def done(res):
            problems, status = res
            if problems:
                self.join_msg = ("\n".join(problems), "error")
            else:
                players = (status or {}).get("PlayerManagerPlayers")
                self.join_msg = (f"Connected! Your friend's server is online" +
                                 (f" with {players} player(s)." if players is not None else "."), "ok")
        self.app.run_task("Testing connection", work, done)

    def create_remote(self):
        host = (self.s.get("join_host") or "").strip()
        email, player, pw = self.s.get("join_email"), self.s.get("join_player"), self.s.get("join_password")
        if not host:
            self.join_msg = ("Enter your friend's address first.", "error")
            return
        err = accounts.validate(email or "", player or "", pw or "")
        if err:
            self.join_msg = (err, "error")
            return

        def done(res):
            ok, msg = res
            self.join_msg = ((f"Account {player} created on your friend's server. Turn on 'Play on my friend's "
                              "server' and press PLAY.") if ok else msg, "ok" if ok else "error")
        self.app.run_task("Creating account", lambda p: network.create_account_on(host, email, player, pw), done)

    # --------------------------------------------------------------- UI
    def build(self):
        ws = []
        host_on = bool(self.s.get("lan_mode"))
        w = COL - 40
        ws.append(ui.Toggle("fr_host", (LX + 20, Y0 + 66, w, 62), "Host for friends", host_on, self.set_hosting,
                            sub="Your server, open to your friends"))
        opts = self.kinds()
        kinds = [o[0] for o in opts]
        cur = self.s.get("host_kind")
        ws.append(ui.Selector("fr_kind", (LX + 20, Y0 + 142, w, 70), "How your friend reaches you",
                              [o[1] for o in opts], kinds.index(cur) if cur in kinds else 0, self.set_kind))
        if cur == "tailscale" and not network.snapshot()["tailscale"]:
            ws.append(ui.Button("fr_ts", (LX + 20, Y0 + 220, w, 50), "Set up Tailscale", self.setup_tailscale,
                                "primary", size=18))
        elif cur == "direct":
            hw = (w - 12) // 2
            if network.snapshot().get("hotspot"):
                ws.append(ui.Button("fr_ts", (LX + 20, Y0 + 220, hw, 50), "Stop hotspot", self.stop_hotspot,
                                    "danger", size=17))
            else:
                ws.append(ui.Button("fr_ts", (LX + 20, Y0 + 220, hw, 50), "Start hotspot", self.start_hotspot,
                                    "primary", size=17))
            ws.append(ui.Button("fr_newpw", (LX + 20 + hw + 12, Y0 + 220, hw, 50), "New password",
                                self.new_hotspot_password, "ghost", size=17))
        if host_on:
            ws.append(ui.Button("fr_copy", (LX + COL - 20 - 120, Y0 + 292, 120, 50), "Copy", self.copy_invite,
                                "secondary", size=18, enabled=bool(self.invite()) or cur == "direct"))
        bw = (w - 12) // 2
        if self.firewall_opened_by_us():
            ws.append(ui.Button("fr_open", (LX + 20, CONTENT.bottom - 66, bw, 50), "Close firewall",
                                self.close_firewall, "danger", size=17))
        else:
            ws.append(ui.Button("fr_open", (LX + 20, CONTENT.bottom - 66, bw, 50), "Open firewall",
                                lambda: self.open_access(False), "ghost", size=17))
        ws.append(ui.Button("fr_router", (LX + 20 + bw + 12, CONTENT.bottom - 66, bw, 50), "Open router (UPnP)",
                            self.open_router, "ghost", size=17))
        ws.append(ui.Toggle("fr_reg", (LX + 20, CONTENT.bottom - 126, w, 52), "Friends can create accounts",
                            bool(self.s.get("friends_can_register")),
                            lambda v: (self.s.set("friends_can_register", v),
                                       setattr(self.app.bridge, "allow_register", v))))
        # join column
        ws.append(ui.Toggle("fr_join", (RX + 20, Y0 + 66, w, 62), "Play on my friend's server",
                            bool(self.s.get("join_mode")), self.set_join, sub="PLAY joins them instead"))
        fw_ = 150
        ws.append(ui.TextField("fr_jhost", (RX + 20, Y0 + 142, w - fw_ - 12, 66), "Friend's address",
                               self.s.get("join_host"), self.setf("join_host"), placeholder="e.g. 100.101.102.103"))
        joined = network.snapshot().get("joined")
        ws.append(ui.Button("fr_jfind", (RX + 20 + w - fw_, Y0 + 142, fw_, 66), "Leave Deck" if joined else "Find Deck",
                            self.find_deck, "danger" if joined else "secondary", size=17))
        ws.append(ui.TextField("fr_jemail", (RX + 20, Y0 + 220, w, 66), "Your email on their server",
                               self.s.get("join_email"), self.setf("join_email"), placeholder="me@deck.local"))
        ws.append(ui.TextField("fr_jplayer", (RX + 20, Y0 + 298, bw, 66), "Player name", self.s.get("join_player"),
                               self.setf("join_player"), placeholder="DeckHero"))
        ws.append(ui.TextField("fr_jpw", (RX + 20 + bw + 12, Y0 + 298, bw, 66), "Password",
                               self.s.get("join_password"), self.setf("join_password"), password=True))
        ws.append(ui.Button("fr_test", (RX + 20, Y0 + 378, bw, 56), "Test connection", self.test, "secondary",
                            size=17))
        ws.append(ui.Button("fr_jcreate", (RX + 20 + bw + 12, Y0 + 378, bw, 56), "Create account there",
                            self.create_remote, "primary", size=17))
        return ws

    def draw(self, surf):
        app, br = self.app, self.app.bridge
        for x, title in ((LX, "HOST · YOUR FRIENDS JOIN YOU"), (RX, "JOIN · PLAY ON A FRIEND'S SERVER")):
            panel(surf, pygame.Rect(x, Y0, COL, CH), C["panel"], 18)
            text(surf, title, (x + 20, Y0 + 22), 14, C["muted"], True)
        # host column details
        opts = self.kinds()
        cur = next((o for o in opts if o[0] == self.s.get("host_kind")), opts[0])
        show_help = not (cur[0] == "tailscale" and not network.snapshot()["tailscale"]) and cur[0] != "direct"
        clip = surf.get_clip()
        surf.set_clip(pygame.Rect(LX, Y0 + 216, COL, 58))
        if show_help:
            ui.text_block(surf, cur[3], (LX + 22, Y0 + 220), COL - 44, 14, C["dim"], spacing=3)
        surf.set_clip(clip)
        box = pygame.Rect(LX + 20, Y0 + 276, COL - 40, 82)
        rrect(surf, C["bg2"], box, 14)
        if cur[0] == "direct":
            ssid, pw = self.s.get("hotspot_ssid"), self.s.get("hotspot_password")
            on = bool(network.snapshot().get("hotspot"))
            text(surf, "HOTSPOT ON – TELL YOUR FRIEND" if on else "HOTSPOT OFF", (box.x + 16, box.y + 10), 12,
                 C["green"] if on else C["muted"], True)
            col = C["gold"] if on else C["dim"]
            if ssid and pw:
                text(surf, f"Wi-Fi  {ssid}", (box.x + 16, box.y + 28), 20, col, True, max_w=box.w - 160)
                text(surf, f"Password  {pw}", (box.x + 16, box.y + 53), 20, col, True, max_w=box.w - 160)
            else:
                text(surf, "Press 'Start hotspot'", (box.x + 16, box.y + 36), 22, C["dim"], True, max_w=box.w - 160)
        elif self.s.get("lan_mode"):
            addr = self.invite()
            text(surf, "YOUR ADDRESS – SEND IT TO YOUR FRIEND", (box.x + 16, box.y + 12), 12, C["muted"], True)
            text(surf, addr or "not available", (box.x + 16, box.y + 32), 30, C["gold"] if addr else C["yellow"],
                 True, max_w=box.w - 160)
        else:
            text(surf, "Turn on 'Host for friends' to get an address to share.", (box.x + 16, box.centery), 15,
                 C["dim"], False, "midleft", box.w - 32)
        y = Y0 + 374
        srv_on = app.server.state == "Online"
        rows = [] if self.s.get("lan_mode") else [
            (True if srv_on else None, "Server", "Online" if srv_on else "Turns on when you press PLAY or Server → ON"),
        ]
        if self.s.get("lan_mode"):
            snap = network.snapshot()
            fw = self.fw_state if self.fw_state is not None else snap["fw_open"]
            kind, active = snap["fw"]
            rows.append((True if (not active or fw) else (None if fw is None else False), "Firewall",
                         "No firewall active" if not active else (
                             "Open while hosting – closes by itself when you stop" if (fw and self.firewall_opened_by_us())
                             else "Open for friends" if fw else
                                                                    "Press 'Open firewall'" if fw is False else
                                                                    "Can't check – press 'Open firewall' once")))
            if self.s.get("host_kind") == "direct":
                hs = snap.get("hotspot")
                rows.insert(0, (True if hs else None, "Hotspot", f"On · {hs[1]}" if hs else "Press 'Start hotspot'"))
            if self.s.get("host_kind") == "internet":
                rows.append((True if self.s.get("upnp_open") else None, "Router",
                             f"Ports open · {self.s.get('public_ip')}" if self.s.get("upnp_open")
                             else "Press 'Open router (UPnP)'"))
            n = len(br.friends_online())
            rows.append((True if n else None, "Friends", f"{n} connected" if n else "Nobody connected yet"))
        for ok, label, detail in rows[:3]:
            check_row(surf, LX + 22, y, ok, label, detail, COL - 44)
            y += 46
        # join column message
        msg, kind = self.join_msg
        mbox = pygame.Rect(RX + 20, Y0 + 452, COL - 40, CH - 452 - 20)
        if msg:
            col = {"ok": C["green"], "error": C["red"]}.get(kind, C["blue"])
            rrect(surf, C["bg2"], mbox, 14)
            rrect(surf, col, pygame.Rect(mbox.x, mbox.y, 6, mbox.h), 3)
            clip = surf.get_clip()
            surf.set_clip(mbox)
            ui.text_block(surf, msg, (mbox.x + 20, mbox.y + 14), mbox.w - 36, 15, C["text"], spacing=3)
            surf.set_clip(clip)
        else:
            ui.text_block(surf, "Ask your friend for their address (the gold one on their Friends tab), or press "
                          "Find Deck when they host Deck to Deck. Create an account on their server once, turn on "
                          "'Play on my friend's server', then press PLAY. Your mods stay on your side.",
                          (mbox.x + 4, mbox.y + 10), mbox.w - 8, 14, C["dim"], spacing=3)
