"""Mod manager integration: MHO Extended Mod Manager (leeper48/MHO-UPK-Tools) driven from the launcher.

The launcher shows and edits the mod library itself (it is plain JSON in MHModManager's format), and lets the real
MHO Extended Mod Manager do everything that touches game files (--install, --apply, --remove checks) through its
command line, run with the same Proton / Wine prefix as the game. The full Windows window (editor, Nexus features)
can be opened too; it shares the same library.

Library layout (MHModManager format):  <library>/mods/<folder>/manifest.json,  <library>/state.json
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from . import client
from .config import DATA_DIR, LOG_DIR

REPO = "leeper48/MHO-UPK-Tools"
FALLBACK_VERSION = "0.37.221"            # newest release when this launcher was made
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=15"
DOWNLOAD_BASE = f"https://github.com/{REPO}/releases/download"
WIN_DESKTOP_RUNTIME = "https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-x64.zip"
WIN_BASE_RUNTIME = "https://aka.ms/dotnet/8.0/dotnet-runtime-win-x64.zip"   # dotnet.exe + Microsoft.NETCore.App
WIN_DESKTOP_RUNTIME_EXE = "https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-x64.exe"

ROOT = os.path.join(DATA_DIR, "modmanager")
APP_DIR = os.path.join(ROOT, "app")                # extracted release ("MHO Extended Mod Manager" folder inside)
HOME_DIR = os.path.join(ROOT, "home")              # MHO_EXTMM_HOME: the tool's settings + undo records
LIBRARY = os.path.join(ROOT, "library")            # mods/ and state.json
REMOVED = os.path.join(ROOT, "removed")            # removed mods are moved here (not deleted)
EXE_NAME = "MHO_Ext_ModManager.exe"
ARCHIVE_EXTS = (".zip", ".7z", ".rar")
UA = {"User-Agent": "MHLauncher (github.com/Jazzneo/marvel-heroes-steamdeck-launcher)"}


# ============================================================== setup / install
def exe_path():
    for dirpath, _dirs, files in os.walk(APP_DIR):
        if EXE_NAME in files:
            return os.path.join(dirpath, EXE_NAME)
    return None


def installed_version():
    try:
        with open(os.path.join(APP_DIR, "version.txt")) as f:
            return f.read().strip()
    except OSError:
        return None


def win_dotnet_dir():
    return os.path.join(client.wine_prefix(), "drive_c", "Program Files", "dotnet")


def runtime_ok():
    d = os.path.join(win_dotnet_dir(), "shared")
    def has(fw):
        p = os.path.join(d, fw)
        return os.path.isdir(p) and any(v.startswith("8.") for v in os.listdir(p))
    return has("Microsoft.NETCore.App") and has("Microsoft.WindowsDesktop.App") and \
        os.path.isfile(os.path.join(win_dotnet_dir(), "dotnet.exe"))


def is_ready():
    return bool(exe_path()) and runtime_ok() and os.path.isfile(os.path.join(LIBRARY, "state.json"))


def _download(url, dest, progress=None, label="Downloading", share=(0.0, 1.0)):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress and total:
                f = share[0] + (share[1] - share[0]) * done / total
                progress(f, f"{label}… {done // 1048576} / {total // 1048576} MB")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_release():
    """(version, zip_url, sha_url) of the newest published Mod Manager release."""
    try:
        req = urllib.request.Request(RELEASES_API, headers=dict(UA, Accept="application/vnd.github+json"))
        with urllib.request.urlopen(req, timeout=20) as r:
            releases = json.loads(r.read().decode())
        best = None
        for rel in releases:
            m = re.match(r"^extmm-v(\d+(?:\.\d+)+)$", rel.get("tag_name", ""))
            if not m or rel.get("draft") or rel.get("prerelease"):
                continue
            ver = m.group(1)
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            z = f"MHO_Ext_ModManager-{ver}.zip"
            if z in assets and z + ".sha256" in assets:
                key = tuple(int(x) for x in ver.split("."))
                if best is None or key > best[0]:
                    best = (key, ver, assets[z], assets[z + ".sha256"])
        if best:
            return best[1], best[2], best[3]
    except Exception as e:
        print("[mods] release check failed, using known version:", e)
    v = FALLBACK_VERSION
    base = f"{DOWNLOAD_BASE}/extmm-v{v}/MHO_Ext_ModManager-{v}.zip"
    return v, base, base + ".sha256"


def install_extmm(progress=None):
    """Download, verify (SHA-256) and unpack the newest Mod Manager. Returns the version."""
    ver, zurl, surl = latest_release()
    os.makedirs(ROOT, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=ROOT)
    try:
        zpath = os.path.join(tmp, "extmm.zip")
        if progress:
            progress(None, f"Downloading MHO Extended Mod Manager {ver}…")
        _download(zurl, zpath, progress, f"Mod Manager {ver}", (0.0, 0.25))
        spath = os.path.join(tmp, "extmm.sha256")
        _download(surl, spath)
        want = open(spath).read().split()[0].strip().lower()
        if _sha256(zpath) != want:
            raise RuntimeError("The Mod Manager download did not match its published SHA-256 checksum. Try again later.")
        staging = os.path.join(tmp, "app")
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                dest = os.path.realpath(os.path.join(staging, info.filename))
                if not dest.startswith(os.path.realpath(staging) + os.sep):
                    continue
                z.extract(info, staging)
        with open(os.path.join(staging, "version.txt"), "w") as f:
            f.write(ver)
        shutil.rmtree(APP_DIR, ignore_errors=True)
        os.replace(staging, APP_DIR)
        return ver
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_in_prefix(settings, runner, exe, args, extra_env=None, timeout=600, cwd=None, on_line=None):
    """Run a Windows program in the prefix; streams output lines to on_line. Returns (code, output)."""
    cmd, env = client.build_command(settings, runner, exe, args, extra_env)
    env.setdefault("WINEDEBUG", "-all")
    env.setdefault("PROTON_LOG", "0")
    env.pop("LD_PRELOAD", None)          # Steam overlay: not for background tools
    proc = subprocess.Popen(cmd, env=env, cwd=cwd or ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                            start_new_session=True)
    out = []
    deadline = time.time() + timeout
    for line in proc.stdout:
        line = line.replace("\r", "").rstrip("\n")
        out.append(line)
        if on_line and line.strip() and not NOISE.match(line.strip()):
            on_line(line.strip())
        if time.time() > deadline:
            proc.kill()
            out.append("(stopped: took too long)")
            break
    code = proc.wait()
    return code, "\n".join(out)


def ensure_prefix(settings, runner, progress=None):
    """Make sure the Wine prefix exists (Proton creates it on first run)."""
    if os.path.isdir(os.path.join(client.wine_prefix(), "drive_c", "windows")):
        return
    if progress:
        progress(None, "Preparing the Proton/Wine prefix (first time, about a minute)…")
    os.makedirs(ROOT, exist_ok=True)
    _run_in_prefix(settings, runner, "cmd", ["/c", "exit"], timeout=600)
    if not os.path.isdir(os.path.join(client.wine_prefix(), "drive_c")):
        raise RuntimeError("Could not create the Proton/Wine prefix. Start the game once, then try again.")


def mlog(msg):
    """A line in logs/modmanager.log (setup steps are logged too, not only the tool's output)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "modmanager.log"), "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def runtime_report():
    d = win_dotnet_dir()
    parts = [f"dotnet.exe: {'yes' if os.path.isfile(os.path.join(d, 'dotnet.exe')) else 'no'}"]
    for fw in ("Microsoft.NETCore.App", "Microsoft.WindowsDesktop.App"):
        p = os.path.join(d, "shared", fw)
        parts.append(f"{fw}: {', '.join(sorted(os.listdir(p))) if os.path.isdir(p) else 'missing'}")
    return "; ".join(parts)


def _unzip_into(zpath, dest):
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            out = os.path.realpath(os.path.join(dest, info.filename))
            if out.startswith(os.path.realpath(dest) + os.sep):
                z.extract(info, dest)


def install_runtime(settings=None, runner=None, progress=None):
    """Windows .NET 8 runtimes in the prefix's C:\\Program Files\\dotnet.

    The Windows Desktop runtime package holds only Microsoft.WindowsDesktop.App; dotnet.exe and the base
    Microsoft.NETCore.App come in the separate .NET Runtime package, so both are unpacked into the same folder.
    If the zips can't be used, Microsoft's Windows Desktop installer (which brings both) runs silently in the prefix."""
    os.makedirs(ROOT, exist_ok=True)
    dest = win_dotnet_dir()
    os.makedirs(dest, exist_ok=True)
    mlog(f"runtime: installing into {dest}; before: {runtime_report()}")
    tmp = tempfile.mkdtemp(dir=ROOT)
    try:
        try:
            for url, label, share in ((WIN_BASE_RUNTIME, ".NET 8 Runtime for Windows", (0.3, 0.55)),
                                      (WIN_DESKTOP_RUNTIME, ".NET 8 Desktop Runtime for Windows", (0.55, 0.85))):
                zpath = os.path.join(tmp, os.path.basename(url))
                _download(url, zpath, progress, label, share)
                mlog(f"runtime: downloaded {url} ({os.path.getsize(zpath) // 1048576} MB)")
                if progress:
                    progress(share[1], f"Unpacking {label}…")
                _unzip_into(zpath, dest)
                os.remove(zpath)
            mlog(f"runtime: after zips: {runtime_report()}")
        except (urllib.error.URLError, zipfile.BadZipFile, OSError) as e:
            mlog(f"runtime: zip route failed: {e!r}")
        if not runtime_ok() and settings is not None:
            epath = os.path.join(tmp, "windowsdesktop-runtime-win-x64.exe")
            _download(WIN_DESKTOP_RUNTIME_EXE, epath, progress, ".NET 8 Desktop Runtime installer", (0.3, 0.8))
            if progress:
                progress(None, "Installing .NET runtime into the prefix (can take a few minutes)…")
            code, out = _run_in_prefix(settings, runner, epath, ["/install", "/quiet", "/norestart"], timeout=1200)
            mlog(f"runtime: installer exit {code}\n{out[-3000:]}")
            mlog(f"runtime: after installer: {runtime_report()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not runtime_ok():
        raise RuntimeError("The Windows .NET 8 runtime could not be set up in the game's Proton prefix.\n\n"
                           f"Found: {runtime_report()}\n\nDetails: {os.path.join(LOG_DIR, 'modmanager.log')}")


def init_library(game_root):
    os.makedirs(os.path.join(LIBRARY, "mods"), exist_ok=True)
    os.makedirs(HOME_DIR, exist_ok=True)
    os.makedirs(REMOVED, exist_ok=True)
    state = os.path.join(LIBRARY, "state.json")
    if not os.path.isfile(state):
        _write_json(state, {"EnabledMods": [], "ModOrder": [], "ActiveLanguage": "eng"})
    # the full Mod Manager window uses the same game folder and library
    sfile = os.path.join(HOME_DIR, "settings.json")
    try:
        with open(sfile, encoding="utf-8-sig") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    if game_root:
        s["GameRoot"] = client.to_windows_path(game_root)
    s["Library"] = client.to_windows_path(LIBRARY)
    s.setdefault("UpdateCheckAsked", True)   # the launcher installs updates (Mods → Update Mod Manager)
    s.setdefault("CheckUpdates", False)
    _write_json(sfile, s)


def setup(settings, runner, game_root, progress=None):
    """Everything the Mods tab needs. Safe to run again (only does what's missing)."""
    if runner is None or runner.kind == "custom":
        raise RuntimeError("The mod manager needs Proton or Wine. Pick one on the Game Client tab.")
    if not exe_path():
        install_extmm(progress)
    ensure_prefix(settings, runner, progress)
    if not runtime_ok():
        if progress:
            progress(0.3, "Downloading .NET 8 Desktop Runtime for Windows (~55 MB, one time)…")
        install_runtime(settings, runner, progress)
    init_library(game_root)
    if progress:
        progress(1.0, "Mod Manager ready")


def tool_env():
    return {
        "MHO_EXTMM_HOME": client.to_windows_path(HOME_DIR),
        "DOTNET_ROOT": r"C:\Program Files\dotnet",
        "DOTNET_ROOT_X64": r"C:\Program Files\dotnet",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
    }


NOISE = re.compile(r"^(fsync|esync|ntsync|wine|ProtonFixes|Proton:|steam\.exe|pressure-vessel|\d+:(err|fixme|warn):|"
                   r"WARNING: |ERROR: ld\.so|Setting up|umu)", re.I)


def run_cli(settings, runner, game_root, args, timeout=1800, on_line=None):
    """Run the Mod Manager's command line. Returns (exit_code, cleaned_output)."""
    exe = exe_path()
    if not exe:
        raise RuntimeError("The mod manager is not set up yet.")
    full = ["--library", client.to_windows_path(LIBRARY)]
    if game_root:
        full += ["--game", client.to_windows_path(game_root)]
    code, out = _run_in_prefix(settings, runner, exe, full + list(args), tool_env(), timeout,
                               cwd=os.path.dirname(exe), on_line=on_line)
    lines = [l.rstrip() for l in out.replace("\r", "").split("\n")]
    clean = "\n".join(l for l in lines if l.strip() and not NOISE.match(l.strip()))
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "modmanager.log"), "a", encoding="utf-8") as f:
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(args)}  (exit {code})\n{out}\n")
    return code, clean


def open_gui(settings, runner, log_path):
    """Open the full Mod Manager window (non-blocking)."""
    exe = exe_path()
    if not exe:
        raise RuntimeError("The mod manager is not set up yet.")
    cmd, env = client.build_command(settings, runner, exe, [], tool_env())
    env.setdefault("WINEDEBUG", "-all")
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    return subprocess.Popen(cmd, env=env, cwd=os.path.dirname(exe), stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)


# ============================================================== library (native)
def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _ci_get(d, key, default=None):
    """JSON from MHModManager uses PascalCase; read keys case-insensitively."""
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return default


class Mod:
    def __init__(self, folder):
        self.folder = folder
        self.id = os.path.basename(folder)          # folder name: what state.json refers to
        self.name = self.id
        self.author = self.version = ""
        self.notes = ""
        self.packages, self.textures, self.languages, self.audio = [], [], [], []
        self.tags = []
        self.error = None
        self.enabled = False
        self.lock = None                             # "top" / "bottom" (set in the full Mod Manager)
        try:
            with open(os.path.join(folder, "manifest.json"), encoding="utf-8-sig") as f:
                m = json.load(f)
        except FileNotFoundError:
            self.error = "no manifest.json"
            return
        except (OSError, ValueError) as e:
            self.error = f"manifest.json: {e}"
            return
        self.name = _ci_get(m, "Name") or self.id
        self.author = _ci_get(m, "Author") or ""
        self.version = _ci_get(m, "Version") or ""
        self.notes = _ci_get(m, "Notes") or _ci_get(m, "Description") or ""
        self.packages = list(_ci_get(m, "UpkReplacements", []) or [])
        for key, kind in (("Replacements", "icon"), ("AchievementReplacements", "achievement"),
                          ("StoreReplacements", "store")):
            for r in _ci_get(m, key, []) or []:
                self.textures.append((kind, _ci_get(r, "TextureName", "")))
        for r in _ci_get(m, "ExtraIconReplacements", []) or []:
            self.textures.append(("extra:" + (_ci_get(r, "Package", "") or ""), _ci_get(r, "TextureName", "")))
        self.languages = list(_ci_get(m, "Languages", []) or [])
        self.audio = list(_ci_get(m, "AudioPacks", []) or [])
        self.tags = list(_ci_get(m, "Tags", []) or [])

    @property
    def kinds(self):
        k = []
        if self.packages:
            k.append("Packages")
        if self.textures:
            k.append("Icons")
        if self.languages:
            k.append("Text")
        if self.audio:
            k.append("Sounds")
        return k

    def claims(self):
        for p in self.packages:
            yield "package:" + p.lower()
        for kind, tex in self.textures:
            yield f"{kind}:{tex.lower()}"

    def summary(self):
        parts = []
        if self.packages:
            parts.append(f"{len(self.packages)} package(s)")
        if self.textures:
            parts.append(f"{len(self.textures)} image(s)")
        if self.languages:
            parts.append("text: " + ", ".join(self.languages))
        if self.audio:
            parts.append(f"{len(self.audio)} sound pack(s)")
        return " · ".join(parts) or "empty"


class Library:
    def __init__(self, path=LIBRARY):
        self.path = path
        self.state = {}
        self.mods = []
        self.load()

    @property
    def state_file(self):
        return os.path.join(self.path, "state.json")

    def load(self):
        try:
            with open(self.state_file, encoding="utf-8-sig") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {"EnabledMods": [], "ModOrder": [], "ActiveLanguage": "eng"}
        mods_dir = os.path.join(self.path, "mods")
        mods = []
        if os.path.isdir(mods_dir):
            for name in os.listdir(mods_dir):
                p = os.path.join(mods_dir, name)
                if os.path.isdir(p) and not name.endswith(".tmp"):
                    mods.append(Mod(p))
        order = {n.lower(): i for i, n in reversed(list(enumerate(_ci_get(self.state, "ModOrder", []) or [])))}
        enabled = {n.lower() for n in _ci_get(self.state, "EnabledMods", []) or []}
        top = [n.lower() for n in (_ci_get(self.state, "LockedTop") or [])]
        bottom = [n.lower() for n in (_ci_get(self.state, "LockedBottom") or [])]
        mods.sort(key=lambda m: (order.get(m.id.lower(), 1 << 30), m.id.lower()))
        for m in mods:
            m.enabled = m.id.lower() in enabled
            m.lock = "top" if m.id.lower() in top else "bottom" if m.id.lower() in bottom else None
        # locked mods stay at the ends, like the full Mod Manager keeps them
        self.mods = ([m for m in mods if m.lock == "top"] + [m for m in mods if not m.lock] +
                     [m for m in mods if m.lock == "bottom"])

    def save(self):
        def setk(key, value):
            for k in list(self.state):
                if k.lower() == key.lower() and k != key:
                    del self.state[k]
            self.state[key] = value
        setk("ModOrder", [m.id for m in self.mods])
        setk("EnabledMods", [m.id for m in self.mods if m.enabled])
        _write_json(self.state_file, self.state)

    def find(self, mod_id):
        for m in self.mods:
            if m.id.lower() == mod_id.lower():
                return m
        return None

    def set_enabled(self, mod, on):
        mod.enabled = on
        self.save()

    def move(self, mod, delta):
        """Move by delta (-1 = up = higher priority). Locked mods (full Mod Manager) don't move."""
        if mod.lock:
            return False
        i = self.mods.index(mod)
        j = i + delta
        while 0 <= j < len(self.mods) and self.mods[j].lock:
            return False
        if not 0 <= j < len(self.mods):
            return False
        self.mods[i], self.mods[j] = self.mods[j], self.mods[i]
        self.save()
        return True

    def move_to(self, mod, top):
        if mod.lock:
            return False
        self.mods.remove(mod)
        if top:
            i = next((k for k, m in enumerate(self.mods) if m.lock != "top"), len(self.mods))
        else:
            i = next((k for k in range(len(self.mods) - 1, -1, -1) if self.mods[k].lock != "bottom"), -1) + 1
        self.mods.insert(i, mod)
        self.save()
        return True

    def conflicts_for(self, mod):
        """[(other_mod, count, mod_wins)] for enabled mods that change the same things."""
        mine = set(mod.claims())
        if not mine:
            return []
        out = []
        my_i = self.mods.index(mod)
        for i, other in enumerate(self.mods):
            if other is mod or not other.enabled:
                continue
            n = len(mine & set(other.claims()))
            if n:
                out.append((other, n, my_i < i))
        return out

    def remove_folder(self, mod):
        """Move a (disabled, already un-applied) mod out of the library into REMOVED."""
        os.makedirs(REMOVED, exist_ok=True)
        dest = os.path.join(REMOVED, f"{mod.id}-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.move(mod.folder, dest)
        for key in ("EnabledMods", "ModOrder", "LockedTop", "LockedBottom"):
            lst = _ci_get(self.state, key)
            if isinstance(lst, list):
                lst[:] = [n for n in lst if n.lower() != mod.id.lower()]
        self.mods.remove(mod)
        self.save()
        return dest

    def folder_ids(self):
        d = os.path.join(self.path, "mods")
        return {n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))} if os.path.isdir(d) else set()


def downloads_archives(limit=40):
    """Mod archives in common download folders, newest first."""
    seen, out = set(), []
    for d in ("~/Downloads", "~/Desktop", "~/Games"):
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name.lower().endswith(ARCHIVE_EXTS) and os.path.isfile(p) and p not in seen:
                seen.add(p)
                out.append(p)
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out[:limit]
