"""Render the README screenshots (docs/screenshots) from the real launcher UI, in a demo state.

Everything outside the launcher is a stand-in: a fake game folder, a fake server that is "online", a friend
connected over Tailscale, a few mods. Nothing real is started.

    runtime/python/bin/python3 tools/make-screenshots.py docs/screenshots
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEMO = tempfile.mkdtemp(prefix="mhl-shots-")
# paths show up on screen, so use a Steam Deck-like home folder when we're allowed to (cleaned up at the end)
HOME = "/home/deck" if not os.path.exists("/home/deck") and os.access("/home", os.W_OK) else os.path.join(DEMO, "home")
os.environ.update(HOME=HOME, XDG_DATA_HOME=os.path.join(HOME, ".local/share"),
                  XDG_CONFIG_HOME=os.path.join(HOME, ".config"), SDL_VIDEODRIVER="dummy",
                  SDL_AUDIODRIVER="dummy")
sys.path.insert(0, os.path.join(REPO, "app"))


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ----------------------------------------------------------------- fake world on disk
GAME = os.path.join(HOME, "Games/Marvel Heroes")
touch(os.path.join(GAME, "UnrealEngine3/Binaries/Win64/MarvelHeroesOmega.exe"))
for f in ("Calligraphy.sip", "mu_cdata.sip"):
    touch(os.path.join(GAME, "Data/Game", f))
touch(os.path.join(HOME, ".local/share/Steam/compatibilitytools.d/GE-Proton9-20/proton"))
for n in ("Spider-Man_Classic_Suit.zip", "HD_Icons_Pack.7z"):
    touch(os.path.join(HOME, "Downloads", n), os.urandom(3 * 1024 * 1024))

import pygame  # noqa: E402
from mhlauncher import (app as A, authproxy, client, dotnet, friendscreen, mods, network,  # noqa: E402
                        savetransfer, screens)
from mhlauncher.config import DATA_DIR  # noqa: E402

SERVER = os.path.join(DATA_DIR, "MHServerEmu")
touch(os.path.join(SERVER, "MHServerEmu.dll"))
for f in ("Calligraphy.sip", "mu_cdata.sip"):
    touch(os.path.join(SERVER, "Data/Game", f))

# save database with a few accounts (schema from MHServerEmu's own initialize script if available)
db = os.path.join(SERVER, "Data", "Account.db")
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE Account (Id INTEGER PRIMARY KEY, Email TEXT, PlayerName TEXT, PasswordHash BLOB, Salt BLOB,
                      UserLevel INTEGER, Flags INTEGER);
CREATE TABLE Player (DbGuid INTEGER PRIMARY KEY, ArchiveData BLOB, StartTarget INTEGER, AOIVolume INTEGER,
                     GazillioniteBalance INTEGER, LastLogoutTime INTEGER, Flags INTEGER NOT NULL DEFAULT 0);
CREATE TABLE Avatar (DbGuid INTEGER PRIMARY KEY, ContainerDbGuid INTEGER, InventoryProtoGuid INTEGER, Slot INTEGER,
                     EntityProtoGuid INTEGER, ArchiveData BLOB);
CREATE TABLE TeamUp (DbGuid INTEGER PRIMARY KEY, ContainerDbGuid INTEGER, InventoryProtoGuid INTEGER, Slot INTEGER,
                     EntityProtoGuid INTEGER, ArchiveData BLOB);
CREATE TABLE Item (DbGuid INTEGER PRIMARY KEY, ContainerDbGuid INTEGER, InventoryProtoGuid INTEGER, Slot INTEGER,
                   EntityProtoGuid INTEGER, ArchiveData BLOB);
CREATE TABLE ControlledEntity (DbGuid INTEGER PRIMARY KEY, ContainerDbGuid INTEGER, InventoryProtoGuid INTEGER,
                               Slot INTEGER, EntityProtoGuid INTEGER, ArchiveData BLOB);
PRAGMA user_version=7;
""")
for i, (email, name, g) in enumerate((("hero@deck.local", "DeckHero", 42500), ("buddy@deck.local", "WebSlinger", 9000),
                                      ("sis@deck.local", "StormRider", -1)), 1):
    c.execute("INSERT INTO Account VALUES (?,?,?,?,?,0,0)", (i, email, name, b"x", b"y"))
    c.execute("INSERT INTO Player (DbGuid, GazillioniteBalance, LastLogoutTime) VALUES (?,?,?)", (i, g, 1))
    for k in range(3 if i == 1 else 1):
        c.execute("INSERT INTO Avatar VALUES (?,?,0,?,0,?)", (i * 100 + k, i, k, b"a"))
c.commit()
c.close()

# mod library
LIB = mods.LIBRARY
for mid, name, author, ver, files in (
        ("SpiderManClassic", "Spider-Man Classic Suit", "webhead", "1.2", ["Spiderman_Costume.upk"]),
        ("HDIcons", "HD Power Icons", "pixelpusher", "2.0", []),
        ("BetterLoading", "Cleaner Loading Screens", "deckfan", "1.0", ["LoadingScreens.upk"]),
        ("EnglishFixes", "English Text Fixes", "typo-hunter", "0.9", [])):
    m = {"Name": name, "Author": author, "Version": ver, "UpkReplacements": files}
    if mid == "HDIcons":
        m["Replacements"] = [{"TextureName": f"icon_{k}"} for k in range(24)]
    if mid == "EnglishFixes":
        m["Languages"] = ["eng"]
    touch(os.path.join(LIB, "mods", mid, "manifest.json"), json.dumps(m).encode())
touch(os.path.join(LIB, "state.json"), json.dumps({
    "EnabledMods": ["SpiderManClassic", "HDIcons", "BetterLoading"],
    "ModOrder": ["SpiderManClassic", "HDIcons", "BetterLoading", "EnglishFixes"], "ActiveLanguage": "eng"}).encode())

# ----------------------------------------------------------------- stand-ins for the outside world
client.pe_file_version = lambda exe: client.EXPECTED_VERSION
dotnet.find_dotnet = lambda: ("/usr/lib/dotnet/dotnet", (8, 0, 20))
authproxy.ports_allowed = friendscreen.ports_allowed = screens.ports_allowed = lambda: True
mods.is_ready = lambda: True
mods.installed_version = lambda: "0.37.221"
network.firewall = lambda: ("firewalld", True)
network.refresh_snapshot = lambda: network.snapshot(max_age=1e9)
network._snap.update(at=time.time() + 1e9, lan="192.168.1.42", tailscale="100.101.102.103", zerotier=None,
                     fw=("firewalld", True), fw_open=True, hotspot=None, joined=None)


class FakeProc:
    pid = 4242

    def poll(self):
        return None


app = A.App(screenshot_mode=True)
pygame.display.init()
pygame.font.init()
scr = pygame.display.set_mode((1280, 800))
s = app.settings
s.set("server_dir", SERVER)
s.set("client_root", GAME)
s.set("client_exe", os.path.join(GAME, "UnrealEngine3/Binaries/Win64/MarvelHeroesOmega.exe"))
s.set("runner", f"proton:{os.path.join(HOME, '.local/share/Steam/compatibilitytools.d/GE-Proton9-20')}")
s.upsert_account("buddy@deck.local", "WebSlinger", "secret123")
s.upsert_account("hero@deck.local", "DeckHero", "secret123")
s.set("fullscreen", False)
s.set("server_options", {"MTXStore.GazillioniteBalanceForNewAccounts": "10000"})

srv = app.server
srv.proc = FakeProc()
srv.state = "Online"
srv.started_at = time.time() - 3725
srv.status = {"PlayerManagerPlayers": 2, "StartupTime": 0, "CurrentTime": 3725}
srv.log = [
    "[Info] [ServerApp] MHServerEmu v1.0.3 (nightly 20261010) starting…",
    "[Info] [GameDatabase] Loaded 63,284 prototypes in 3.1 s",
    "[Info] [SQLiteDBManager] Found existing database file with schema version 7 (current = 7)",
    "[Info] [WebFrontendService] Listening on http://localhost:8080/",
    "[Info] [FrontendServer] Listening on 0.0.0.0:4306",
    "[Info] [ServerManager] Server is ready",
    "[Info] [PlayerManagerService] Login request from hero@deck.local",
    "[Info] [PlayerHandle] Loaded player data for account [DeckHero]",
    "[Info] [GameInstance] Player [DeckHero] entered region Avengers Tower",
    "[Info] [PlayerManagerService] Login request from buddy@deck.local",
    "[Info] [PlayerHandle] Loaded player data for account [WebSlinger]",
    "[Info] [GameInstance] Player [WebSlinger] entered region Avengers Tower",
    "> !server status",
    "[Info] [CommandManager] Players online: 2 · Games: 1 · Uptime: 01:02:05",
]
app.bridge.https = object()
app.bridge.auth_requests = 6
app.bridge.friends_seen = {"100.88.12.7": time.time() + 1e9}


def frames(n=4):
    for _ in range(n):
        app.tick()
        app.draw(scr)


def shot(name):
    frames()
    pygame.image.save(scr, os.path.join(OUT, name))
    print("saved", name)


def screen(name):
    app.modals.clear()
    app.go(next(i for i, x in enumerate(app.screens) if x.name == name))
    frames(2)
    return app.screen


def focus(key):
    app.focus_key = key


os.makedirs(OUT, exist_ok=True)

# Play
screen("play")
focus("play")
shot("play.png")

# Friends: hosting over Tailscale, firewall opened by the launcher, one friend connected
s.set("lan_mode", True)
s.set("host_kind", "tailscale")
s.set("fw_added_ports", [80, 4306])
s.set("fw_watchdog", True)
s.set("join_host", "100.64.0.9")
s.set("join_email", "hero@deck.local")
s.set("join_password", "secret123")
fs = screen("friends")
fs.options = network.address_options(network.snapshot())
fs.fw_state = True
focus("fr_copy")
shot("friends.png")

# Deck to Deck hotspot
network._snap.update(hotspot=("wlan0", "10.42.0.1"))
s.set("host_kind", "direct")
s.set("hotspot_ssid", "MHL-DeckHero")
s.set("hotspot_password", "48273915")
fs = screen("friends")
fs.options = network.address_options(network.snapshot())
focus("fr_ts")
shot("deck-to-deck.png")

# Move my heroes (join side)
network._snap.update(hotspot=None)
s.set("lan_mode", False)
s.set("host_kind", "tailscale")
s.set("fw_added_ports", [])
fs = screen("friends")
fs.options = network.address_options(network.snapshot())
fs.join_msg = ("", "info")
app.dialog("Move your heroes",
           "Your solo save – DeckHero: 3 heroes, 128 items\nOn your friend's server – DeckHero: 1 hero, 12 items\n\n"
           "Moving copies every hero, item and your stash, and replaces the save on the other side (a backup of it "
           "is kept). Your names, logins and passwords stay as they are.",
           [("Cancel", None, "ghost"), ("Take home", None, "secondary"), ("Bring to friend", None, "primary")],
           width=1000)
shot("move-heroes.png")

# Mods
ms = screen("mods")
ms.reload()
ms.index = 0
focus("md_list")
shot("mods.png")

# Friend's mods offer (when joining)
app.dialog("Get your friend's mods?",
           "Your friend plays with 2 mod(s) you don't have or have turned off: Spider-Man Classic Suit, HD Power Icons"
           " (46 MB to copy).\n\nMods only change what you see on your own screen, so copy them to see the game like "
           "your friend does. Your own mods stay; you can turn any of them off later on the Mods tab.",
           [("Not now", None, "ghost"), ("Get mods & play", None, "primary")], width=980)
shot("friend-mods.png")

# Install a mod
screen("mods")
from mhlauncher.modscreen import ArchivePicker  # noqa: E402
app.open_modal(ArchivePicker(app, lambda p: None))
shot("mods-install.png")

# Accounts: create + saved
acc = screen("accounts")
acc.mode = "create"
acc.f = {"email": "storm@deck.local", "player": "StormRider", "password": "secret123", "confirm": "secret123"}
focus("f_submit")
shot("create-account.png")
acc.mode = "signin"
acc.f = {"email": "", "player": "", "password": "", "confirm": ""}
acc.index = 0
focus("acc_list")
shot("accounts.png")

# Keyboard
screen("accounts")
app.open_keyboard("Player name", "Storm", lambda v: None)
app.modals[-1].focus_key = "k2:4"
shot("keyboard.png")

# Server + cash shop
screen("server")
focus("srv_g")
shot("server.png")
from mhlauncher.gshop import GShop  # noqa: E402
g = GShop(app)
app.open_modal(g)
g.give(10000)
g.focus_key = "g_10000"
shot("cash-shop.png")

# Game client, settings
screen("client")
shot("client.png")
screen("settings")
focus("set_steam")
shot("settings.png")
import shutil  # noqa: E402
if HOME == "/home/deck":
    shutil.rmtree(HOME, ignore_errors=True)
shutil.rmtree(DEMO, ignore_errors=True)
