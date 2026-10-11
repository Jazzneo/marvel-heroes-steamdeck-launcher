"""Steam library shortcut with Marvel Heroes Omega artwork.

Steam keeps non-Steam shortcuts in userdata/<user>/config/shortcuts.vdf (binary VDF) and custom artwork in
userdata/<user>/config/grid/:
    <appid>p.<ext>      tall cover (600x900)
    <appid>.<ext>       wide banner (920x430)
    <appid>_hero.<ext>  big background at the top of the game page (1920x620)
    <appid>_logo.png    logo drawn on top of the background
The icon is a field of the shortcut itself.

Artwork sources, best first:
 1. Steam's own cache of the official Marvel Heroes Omega art (app 226320) - there when you have the game in
    your Steam library (appcache/librarycache).
 2. Steam's image servers (the game left the store in 2017, so newer sizes may not exist there).
 3. The launcher's own MH Launcher artwork (assets/steam), for anything still missing.
The official art is never shipped with the launcher; it is only copied from your own Steam on your own machine.
"""
import glob
import os
import shutil
import struct
import time
import urllib.request
import zlib

from .config import ASSETS_DIR, DATA_DIR

MHO_APPID = 226320
GAME_NAME = "Marvel Heroes Omega"
ART_DIR = os.path.join(DATA_DIR, "steam-art")
KINDS = ("capsule", "wide", "hero", "logo", "icon")
GRID_NAME = {"capsule": "{id}p", "wide": "{id}", "hero": "{id}_hero", "logo": "{id}_logo"}
CDN_BASES = ("https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{a}/",
             "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{a}/",
             "https://cdn.cloudflare.steamstatic.com/steam/apps/{a}/",
             "https://cdn.akamai.steamstatic.com/steam/apps/{a}/",
             "https://steamcdn-a.akamaihd.net/steam/apps/{a}/")
CDN_FILES = {"capsule": ("library_600x900_2x.jpg", "library_600x900.jpg"),
             "wide": ("header.jpg",),
             "hero": ("library_hero.jpg",),
             "logo": ("logo.png",)}
FALLBACK = {"capsule": "capsule.png", "wide": "wide.png", "hero": "hero.png", "logo": "logo.png"}


# ------------------------------------------------------------------ Steam folders
def steam_roots():
    seen, out = set(), []
    for p in ("~/.steam/steam", "~/.local/share/Steam", "~/.steam/root",
              "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
              "~/.var/app/com.valvesoftware.Steam/data/Steam"):
        p = os.path.expanduser(p)
        if os.path.isdir(os.path.join(p, "userdata")) or os.path.isdir(os.path.join(p, "appcache")):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                out.append(real)
    return out


def user_dirs():
    """Steam user folders (userdata/<account id>), most recently used first."""
    out = []
    for root in steam_roots():
        for d in glob.glob(os.path.join(root, "userdata", "*")):
            if os.path.basename(d).isdigit() and os.path.basename(d) != "0" and os.path.isdir(d):
                out.append(d)
    return sorted(out, key=lambda d: -os.path.getmtime(d))


def steam_running():
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/comm") as f:
                    if f.read().strip() in ("steam", "steamwebhelper"):
                        return True
            except OSError:
                pass
    return False


# ------------------------------------------------------------------ binary VDF (shortcuts.vdf)
def vdf_load(data):
    def read_str(i):
        j = data.index(b"\0", i)
        return data[i:j].decode("utf-8", "replace"), j + 1

    def read_map(i):
        out = {}
        while True:
            t = data[i]
            i += 1
            if t == 0x08:
                return out, i
            key, i = read_str(i)
            if t == 0x00:
                out[key], i = read_map(i)
            elif t == 0x01:
                out[key], i = read_str(i)
            elif t == 0x02:
                out[key] = struct.unpack_from("<i", data, i)[0]
                i += 4
            elif t == 0x07:
                out[key] = ("u64", struct.unpack_from("<Q", data, i)[0])
                i += 8
            elif t == 0x03:
                out[key] = ("f32", struct.unpack_from("<f", data, i)[0])
                i += 4
            else:
                raise ValueError(f"unknown VDF type {t:#x}")
    root, _ = read_map(0)
    return root


def vdf_dump(obj):
    out = bytearray()

    def w(m):
        for k, v in m.items():
            kb = k.encode("utf-8") + b"\0"
            if isinstance(v, dict):
                out.extend(b"\x00" + kb)
                w(v)
            elif isinstance(v, str):
                out.extend(b"\x01" + kb + v.encode("utf-8") + b"\0")
            elif isinstance(v, bool) or isinstance(v, int):
                out.extend(b"\x02" + kb + struct.pack("<i", int(v)))
            elif isinstance(v, tuple) and v[0] == "u64":
                out.extend(b"\x07" + kb + struct.pack("<Q", v[1]))
            elif isinstance(v, tuple) and v[0] == "f32":
                out.extend(b"\x03" + kb + struct.pack("<f", v[1]))
        out.append(0x08)
    w(obj)
    return bytes(out)


def shortcuts_path(user_dir):
    return os.path.join(user_dir, "config", "shortcuts.vdf")


def read_shortcuts(user_dir):
    try:
        with open(shortcuts_path(user_dir), "rb") as f:
            data = f.read()
        if not data:
            return {"shortcuts": {}}
        root = vdf_load(data)
        root.setdefault("shortcuts", {})
        return root
    except FileNotFoundError:
        return {"shortcuts": {}}


def _get(entry, key, default=None):
    for k, v in entry.items():
        if k.lower() == key.lower():
            return v
    return default


def find_shortcut(user_dir, exe_path):
    """(key, entry) of the shortcut that starts exe_path, or (None, None)."""
    target = os.path.realpath(exe_path)
    for key, e in read_shortcuts(user_dir).get("shortcuts", {}).items():
        exe = str(_get(e, "Exe", "")).strip().strip('"')
        if exe and (os.path.realpath(exe) == target or exe.endswith("/MHLauncher.sh")):
            return key, e
    return None, None


def legacy_appid(exe, name):
    """Shortcut id the way Steam and other tools make it (CRC32 of exe + name with the top bit set)."""
    return (zlib.crc32((exe + name).encode("utf-8")) & 0xFFFFFFFF) | 0x80000000


def unsigned(appid):
    return int(appid) & 0xFFFFFFFF


def add_shortcut_vdf(user_dir, exe_path, name, icon, start_dir):
    """Write a new shortcut straight into shortcuts.vdf (only while Steam is closed). Returns the appid."""
    root = read_shortcuts(user_dir)
    sc = root["shortcuts"]
    exe = f'"{exe_path}"'
    appid = legacy_appid(exe, name)
    entry = {
        "appid": struct.unpack("<i", struct.pack("<I", appid))[0],
        "AppName": name, "Exe": exe, "StartDir": f'"{start_dir}"', "icon": icon or "",
        "ShortcutPath": "", "LaunchOptions": "", "IsHidden": 0, "AllowDesktopConfig": 1, "AllowOverlay": 1,
        "OpenVR": 0, "Devkit": 0, "DevkitGameID": "", "DevkitOverrideAppID": 0, "LastPlayTime": 0,
        "FlatpakAppID": "", "tags": {},
    }
    n = 0
    while str(n) in sc:
        n += 1
    sc[str(n)] = entry
    _write_shortcuts(user_dir, root)
    return appid


def update_shortcut_vdf(user_dir, key, name=None, icon=None):
    root = read_shortcuts(user_dir)
    e = root["shortcuts"].get(key)
    if e is None:
        return False
    for field, val in (("AppName", name), ("icon", icon)):
        if val:
            for k in list(e):
                if k.lower() == field.lower():
                    del e[k]
            e[field] = val
    _write_shortcuts(user_dir, root)
    return True


def _write_shortcuts(user_dir, root):
    path = shortcuts_path(user_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        shutil.copy2(path, path + f".mhlauncher-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(vdf_dump(root))          # ends with 08 (shortcuts) 08 (root), like Steam writes it
    os.replace(tmp, path)


# ------------------------------------------------------------------ artwork
def _kind_of(name):
    n = name.lower()
    if "600x900" in n or "library_capsule" in n:
        return "capsule"
    if "hero" in n and "blur" not in n:
        return "hero"
    if "logo" in n:
        return "logo"
    if "header" in n:
        return "wide"
    if "icon" in n:
        return "icon"
    return None


def cached_official_art():
    """{kind: path} from Steam's local cache of Marvel Heroes Omega's library art."""
    found = {}
    for root in steam_roots():
        lc = os.path.join(root, "appcache", "librarycache")
        candidates = glob.glob(os.path.join(lc, f"{MHO_APPID}_*"))           # older Steam: 226320_header.jpg
        candidates += glob.glob(os.path.join(lc, str(MHO_APPID), "**", "*"), recursive=True)   # newer: 226320/...
        for p in sorted(candidates, key=lambda p: ("2x" not in p, len(p))):
            if not os.path.isfile(p) or os.path.getsize(p) < 500:
                continue
            kind = _kind_of(os.path.basename(p))
            if kind is None and os.path.dirname(p) == os.path.join(lc, str(MHO_APPID)) and \
                    len(os.path.splitext(os.path.basename(p))[0]) == 40:
                kind = "icon"                                    # newer Steam: icon stored as <sha1>.jpg
            if kind and kind not in found:
                found[kind] = p
    return found


def _download(url, dest, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (MH Launcher)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read()
    if not ctype.startswith("image/") or len(data) < 1000:
        raise ValueError("not an image")
    with open(dest, "wb") as f:
        f.write(data)


def collect_art(progress=None):
    """Gather artwork into ART_DIR. Returns {kind: (path, source)} with source 'steam', 'web' or 'launcher'."""
    os.makedirs(ART_DIR, exist_ok=True)
    art = {}
    for kind, path in cached_official_art().items():
        dest = os.path.join(ART_DIR, kind + os.path.splitext(path)[1].lower())
        shutil.copy2(path, dest)
        art[kind] = (dest, "steam")
    for kind, files in CDN_FILES.items():
        if kind in art:
            continue
        if progress:
            progress(None, "Looking for the Marvel Heroes Omega artwork on Steam…")
        for fname in files:
            dest = os.path.join(ART_DIR, kind + os.path.splitext(fname)[1])
            for base in CDN_BASES:
                try:
                    _download(base.format(a=MHO_APPID) + fname, dest)
                    art[kind] = (dest, "web")
                    break
                except Exception:
                    continue
            if kind in art:
                break
    for kind, fname in FALLBACK.items():
        if kind not in art:
            art[kind] = (os.path.join(ASSETS_DIR, "steam", fname), "launcher")
    if "icon" not in art:
        art["icon"] = (os.path.join(ASSETS_DIR, "icon.png"), "launcher")
    return art


def install_grid(user_dir, appid, art):
    """Copy the artwork into Steam's custom-art folder for this shortcut."""
    grid = os.path.join(user_dir, "config", "grid")
    os.makedirs(grid, exist_ok=True)
    uid = unsigned(appid)
    for kind, pattern in GRID_NAME.items():
        if kind not in art:
            continue
        src = art[kind][0]
        base = pattern.format(id=uid)
        for old in glob.glob(os.path.join(grid, base + ".*")):      # only one image per slot
            os.remove(old)
        ext = ".png" if kind == "logo" else os.path.splitext(src)[1].lower() or ".png"
        shutil.copy2(src, os.path.join(grid, base + ext))


def icon_file(art):
    """A stable copy of the icon for Steam to use."""
    src = art["icon"][0]
    dest = os.path.join(ART_DIR, "shortcut-icon" + os.path.splitext(src)[1].lower())
    if os.path.abspath(src) != os.path.abspath(dest):
        os.makedirs(ART_DIR, exist_ok=True)
        shutil.copy2(src, dest)
    return dest


def wait_for_shortcut(exe_path, timeout=30):
    """After asking Steam to add the shortcut: wait until it shows up in a shortcuts.vdf. -> (user_dir, key, entry)"""
    end = time.time() + timeout
    while time.time() < end:
        for u in user_dirs():
            key, e = find_shortcut(u, exe_path)
            if e is not None:
                return u, key, e
        time.sleep(1)
    return None, None, None
