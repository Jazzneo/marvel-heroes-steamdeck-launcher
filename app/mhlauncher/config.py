"""Paths and persistent launcher settings."""
import json
import os
import threading

APP_NAME = "MHLauncher"
APP_VERSION = "1.5.1"

# Folder that contains MHLauncher.sh (two levels above this file: app/mhlauncher/config.py)
BUNDLE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ASSETS_DIR = os.path.join(BUNDLE_DIR, "assets")
BUNDLED_SERVER_DIR = os.path.join(BUNDLE_DIR, "server")


def _xdg(var, fallback):
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), fallback)


CONFIG_DIR = os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), APP_NAME)
DATA_DIR = os.path.join(_xdg("XDG_DATA_HOME", ".local/share"), APP_NAME)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

DEFAULTS = {
    "server_dir": os.path.join(DATA_DIR, "MHServerEmu"),
    "client_root": "",          # folder that contains UnrealEngine3/ and Data/
    "client_exe": "",           # full path to MarvelHeroesOmega.exe
    "runner": "",               # runner id, see runners.py ("" = auto)
    "custom_runner_cmd": "",    # used when runner == "custom"; {exe} and {args} placeholders
    "accounts": [],             # [{"email","player","password"}]
    "active_account": "",       # email
    "remember_passwords": True,
    "auto_login": True,         # pass -emailaddress/-password to the client
    "auto_start_server": True,  # start server when pressing Play
    "stop_server_on_exit": True,
    "deck_resolution": True,    # -ResX=1280 -ResY=800
    "skip_movies": True,        # -nostartupmovies
    "extra_client_args": "",
    "lan_mode": False,          # host mode: friends can join this server (Friends tab)
    "host_kind": "tailscale",   # how friends reach you: tailscale / lan / zerotier / internet
    "public_ip": "",            # internet hosting: router's public address (UPnP)
    "upnp_open": False,         # router ports opened by the launcher
    "friends_can_register": True,
    "fw_added_ports": [],       # firewall ports the launcher opened for friends (closed again on request)
    "fw_added_tailscale": False,
    "fw_added_other": [],       # zone rules like "nm-shared/80" (Deck-to-Deck hotspot zone)
    "fw_watchdog": False,       # the opened rules are runtime-only and watched (close without a password)
    "fw_close_on_server_stop": True,
    "hotspot_ssid": "",         # Deck to Deck: name of this Deck's hotspot (MHL-…)
    "hotspot_password": "",     # 8 digits, made on first use
    "join_mode": False,         # PLAY joins a friend's server instead of your own
    "join_host": "",
    "join_email": "",
    "join_player": "",
    "join_password": "",
    "server_options": {},       # ConfigOverride.ini values {"Section.Key": "value"}
    "fullscreen": None,         # None = auto (fullscreen in Gaming Mode)
    "mods_dirty": False,
    "share_mods": True,         # friends can copy my turned-on mods while I host
    "friend_mods_skip": "",     # "Not now" for this exact set of a friend's mods        # mod list changed since the last Apply
}


class Settings:
    def __init__(self):
        self._lock = threading.RLock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.data.update(stored)
        except FileNotFoundError:
            pass
        except Exception as e:  # corrupted file: keep defaults, back it up
            try:
                os.replace(SETTINGS_FILE, SETTINGS_FILE + ".broken")
            except OSError:
                pass
            print(f"[settings] could not read settings: {e}")

    def save(self):
        with self._lock:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            data = dict(self.data)
            if not data.get("remember_passwords", True):
                data["accounts"] = [dict(a, password="") for a in data.get("accounts", [])]
            # settings may contain passwords -> owner-only permissions
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, SETTINGS_FILE)

    def get(self, key, default=None):
        with self._lock:
            return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value, save=True):
        with self._lock:
            self.data[key] = value
            if save:
                self.save()

    # ---- accounts -------------------------------------------------------
    def accounts(self):
        return list(self.get("accounts") or [])

    def find_account(self, email):
        email = (email or "").lower()
        for a in self.accounts():
            if a.get("email", "").lower() == email:
                return a
        return None

    def upsert_account(self, email, player="", password=""):
        email = email.strip().lower()
        accs = self.accounts()
        for a in accs:
            if a.get("email", "").lower() == email:
                if player:
                    a["player"] = player
                if password:
                    a["password"] = password
                break
        else:
            accs.append({"email": email, "player": player, "password": password})
        self.set("accounts", accs, save=False)
        self.set("active_account", email)

    def remove_account(self, email):
        email = email.lower()
        accs = [a for a in self.accounts() if a.get("email", "").lower() != email]
        self.set("accounts", accs, save=False)
        if self.get("active_account", "").lower() == email:
            self.set("active_account", accs[0]["email"] if accs else "", save=False)
        self.save()

    def active_account(self):
        return self.find_account(self.get("active_account"))


def in_gaming_mode():
    """True when running inside Steam Deck Gaming Mode (gamescope session)."""
    env = os.environ
    return bool(
        env.get("SteamGamepadUI")
        or env.get("GAMESCOPE_WAYLAND_DISPLAY")
        or env.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope"
    )


def is_steam_deck():
    if os.environ.get("SteamDeck") == "1":
        return True
    try:
        with open("/sys/devices/virtual/dmi/id/board_vendor") as f:
            vendor = f.read().strip()
        with open("/sys/devices/virtual/dmi/id/product_name") as f:
            product = f.read().strip()
        return vendor == "Valve" and product in ("Jupiter", "Galileo")
    except OSError:
        return False
