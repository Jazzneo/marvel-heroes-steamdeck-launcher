"""Finding, validating and launching the Marvel Heroes Windows client."""
import glob
import os
import re
import shlex
import shutil
import struct
import subprocess
import threading

from .config import DATA_DIR

EXPECTED_VERSION = "1.52.0.1700"
EXE_NAMES = ("MarvelHeroesOmega.exe",)
EXE_SUBPATHS = (
    os.path.join("UnrealEngine3", "Binaries", "Win64", "MarvelHeroesOmega.exe"),
    os.path.join("UnrealEngine3", "Binaries", "Win32", "MarvelHeroesOmega.exe"),
)


# ------------------------------------------------------------------ PE version
def pe_file_version(path):
    """Read FileVersion from a Windows executable's VS_FIXEDFILEINFO (no Wine needed)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return None
            # resources usually sit near the end; scan the whole file in chunks
            sig = struct.pack("<I", 0xFEEF04BD)
            chunk, overlap, pos = 4 << 20, 64, 0
            f.seek(0)
            while pos < size:
                f.seek(pos)
                data = f.read(chunk + overlap)
                i = data.find(sig)
                while i != -1:
                    block = data[i:i + 20]
                    if len(block) == 20:
                        _sig, _struc, ms, ls = struct.unpack("<IIII", block[:16])
                        if _struc in (0x10000, 0):
                            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
                    i = data.find(sig, i + 1)
                pos += chunk
    except OSError:
        pass
    return None


def is_64bit_exe(path):
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            off = struct.unpack("<I", f.read(4))[0]
            f.seek(off + 4)
            return struct.unpack("<H", f.read(2))[0] == 0x8664
    except (OSError, struct.error):
        return False


# ------------------------------------------------------------------ discovery
def find_client_root(path):
    """Given any file/folder the user picked, return the game root folder or None.

    The root is the folder that contains both UnrealEngine3/ and Data/Game/.
    """
    if not path:
        return None
    p = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(p):
        p = os.path.dirname(p)
    for _ in range(6):
        if os.path.isdir(os.path.join(p, "UnrealEngine3")) and os.path.isdir(os.path.join(p, "Data", "Game")):
            return p
        # user picked the parent folder (e.g. .../common) that contains "Marvel Heroes"
        for name in ("Marvel Heroes", "Marvel Heroes 2016", "MarvelHeroes", "Marvel Heroes Omega"):
            q = os.path.join(p, name)
            if os.path.isdir(os.path.join(q, "UnrealEngine3")):
                return q
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def best_exe(root):
    for sub in EXE_SUBPATHS:
        exe = os.path.join(root, sub)
        if os.path.isfile(exe):
            return exe
    return None


def inspect_client(path):
    """Return a dict describing the client at `path` (exe or folder)."""
    info = {"ok": False, "root": None, "exe": None, "version": None, "problems": [], "sip": False}
    p = os.path.abspath(os.path.expanduser(path or ""))
    exe = p if os.path.isfile(p) and p.lower().endswith(".exe") and \
        os.path.basename(p).lower() == "marvelheroesomega.exe" else None
    root = find_client_root(p)
    info["root"] = root
    if not root:
        info["problems"].append("This is not a Marvel Heroes folder (UnrealEngine3 + Data/Game not found).")
        return info
    exe = exe or best_exe(root)
    info["exe"] = exe
    if not exe:
        info["problems"].append("MarvelHeroesOmega.exe was not found under UnrealEngine3/Binaries.")
        return info
    info["version"] = pe_file_version(exe)
    info["sip"] = all(os.path.isfile(os.path.join(root, "Data", "Game", f))
                      for f in ("Calligraphy.sip", "mu_cdata.sip"))
    if not info["sip"]:
        info["problems"].append("Data/Game/Calligraphy.sip or mu_cdata.sip is missing (incomplete install?).")
    if info["version"] and info["version"] != EXPECTED_VERSION:
        info["problems"].append(f"Client version {info['version']} – this server needs {EXPECTED_VERSION}.")
    info["ok"] = not info["problems"]
    return info


def steam_roots():
    roots = []
    for p in ("~/.steam/steam", "~/.steam/root", "~/.local/share/Steam",
              "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"):
        p = os.path.realpath(os.path.expanduser(p))
        if os.path.isdir(p) and p not in roots:
            roots.append(p)
    return roots


def steam_libraries():
    libs = []
    for root in steam_roots():
        libs.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    libs.append(m.group(1).replace("\\\\", "\\"))
        except OSError:
            pass
    out = []
    for l in libs:
        l = os.path.realpath(l)
        if os.path.isdir(l) and l not in out:
            out.append(l)
    return out


def scan_locations():
    home = os.path.expanduser("~")
    locs = [os.path.join(l, "steamapps", "common") for l in steam_libraries()]
    locs += [os.path.join(home, d) for d in ("Games", "games", "Downloads", "Desktop", "Documents")]
    locs += glob.glob("/run/media/*/*") + glob.glob("/run/media/*") + glob.glob("/media/*/*") + glob.glob("/mnt/*")
    locs += [os.path.join(home, ".wine", "drive_c"), os.path.join(home, "Games", "Heroic"),
             os.path.join(home, ".local", "share", "lutris")]
    locs.append(home)
    seen, out = set(), []
    for l in locs:
        r = os.path.realpath(l)
        if os.path.isdir(r) and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def scan_for_clients(max_depth=6, stop_event=None, progress=None):
    """Search common places for MarvelHeroesOmega.exe. Returns list of game roots."""
    found = []
    skip = {"proc", "sys", "dev", "node_modules", ".cache", "shadercache", "compatdata", "__pycache__",
            ".git", "Trash", ".local/share/Trash", "MHLauncher"}
    visited = set()
    for base in scan_locations():
        base_depth = base.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            if stop_event and stop_event.is_set():
                return found
            real = os.path.realpath(dirpath)
            if real in visited:
                dirnames[:] = []
                continue
            visited.add(real)
            if progress:
                progress(dirpath)
            depth = dirpath.count("/") - base_depth
            dirnames[:] = [d for d in dirnames if d not in skip and not (d.startswith(".") and depth > 0)]
            if depth >= max_depth:
                dirnames[:] = []
            if "UnrealEngine3" in dirnames and "Data" in dirnames:
                root = find_client_root(dirpath)
                if root and best_exe(root) and root not in found:
                    found.append(root)
                dirnames[:] = []
    return found


# ------------------------------------------------------------------ runners
class Runner:
    def __init__(self, rid, name, kind, path=None):
        self.id, self.name, self.kind, self.path = rid, name, kind, path

    def __repr__(self):
        return f"Runner({self.id})"


def detect_runners():
    runners = []
    proton_dirs = []
    for lib in steam_libraries():
        proton_dirs += glob.glob(os.path.join(lib, "steamapps", "common", "Proton*"))
    for root in steam_roots():
        proton_dirs += glob.glob(os.path.join(root, "compatibilitytools.d", "*"))
    proton_dirs += glob.glob(os.path.expanduser("~/.local/share/Steam/compatibilitytools.d/*"))
    seen = set()

    def proton_key(d):
        name = os.path.basename(d)
        nums = [int(x) for x in re.findall(r"\d+", name)] or [0]
        return ("GE" in name or "ge-" in name.lower(), "Experimental" in name, nums)

    for d in sorted(set(proton_dirs), key=proton_key, reverse=True):
        script = os.path.join(d, "proton")
        real = os.path.realpath(d)
        if os.path.isfile(script) and real not in seen:
            seen.add(real)
            name = os.path.basename(d)
            if shutil.which("umu-run"):
                runners.append(Runner(f"umu:{d}", f"{name} (umu)", "umu", d))
            runners.append(Runner(f"proton:{d}", name, "proton", d))
    wine = shutil.which("wine") or shutil.which("wine64")
    if wine:
        runners.append(Runner("wine", "System Wine", "wine", wine))
    runners.append(Runner("custom", "Custom command", "custom"))
    return runners


def pick_runner(runners, wanted):
    for r in runners:
        if r.id == wanted:
            return r
    # auto: first Proton/umu, else wine
    for r in runners:
        if r.kind in ("umu", "proton", "wine"):
            return r
    return None


PREFIX_DIR = os.path.join(DATA_DIR, "prefix")


def build_args(settings, siteconfig_url, account=None):
    args = ["-robocopy", "-nosteam", f"-siteconfigurl={siteconfig_url}"]
    if settings.get("skip_movies"):
        args.append("-nostartupmovies")
    if settings.get("deck_resolution"):
        args += ["-ResX=1280", "-ResY=800"]
    if account and settings.get("auto_login") and account.get("email") and account.get("password"):
        args += [f"-emailaddress={account['email']}", f"-password={account['password']}"]
    extra = settings.get("extra_client_args") or ""
    if extra.strip():
        args += shlex.split(extra)
    return args


def build_command(settings, runner, exe, args, extra_env=None):
    """Command + environment to run a Windows program in the launcher's Proton/Wine prefix."""
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "SDL_VIDEODRIVER"):
        env.pop(k, None)
    os.makedirs(PREFIX_DIR, exist_ok=True)
    if runner is None:
        raise RuntimeError("No Proton or Wine found. Install Proton in Steam or pick a custom command.")
    if runner.kind in ("proton", "umu"):
        steam = (steam_roots() or [os.path.expanduser("~/.steam/steam")])[0]
        env["STEAM_COMPAT_DATA_PATH"] = PREFIX_DIR
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = steam
        env.setdefault("STEAM_COMPAT_APP_ID", "0")
        env.setdefault("SteamAppId", "0")
        env.setdefault("SteamGameId", "0")
        if runner.kind == "umu":
            env["PROTONPATH"] = runner.path
            env["WINEPREFIX"] = wine_prefix()
            env.setdefault("GAMEID", "umu-0")
            cmd = ["umu-run", exe] + args
        else:
            cmd = [os.path.join(runner.path, "proton"), "run", exe] + args
    elif runner.kind == "wine":
        env["WINEPREFIX"] = wine_prefix()
        env.setdefault("WINEDEBUG", "-all")
        cmd = [runner.path, exe] + args
    else:
        template = settings.get("custom_runner_cmd") or ""
        if "{exe}" not in template:
            raise RuntimeError("Custom command must contain {exe} (and optionally {args}).")
        cmd = []
        for tok in shlex.split(template):
            if tok == "{args}":
                cmd += args
            else:
                cmd.append(tok.replace("{exe}", exe))
    if extra_env:
        env.update(extra_env)
    return cmd, env


def wine_prefix():
    """The Wine prefix the game and the mod manager share (Proton keeps it in <compat data>/pfx)."""
    return os.path.join(PREFIX_DIR, "pfx")


def to_windows_path(path):
    """A Linux path as Wine sees it (drive Z: is the whole Linux file system)."""
    return "Z:" + os.path.abspath(path).replace("/", "\\")


def launch(settings, runner, exe, args, log_path):
    """Start the client. Returns the Popen object."""
    cmd, env = build_command(settings, runner, exe, args)
    workdir = os.path.dirname(exe)
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    log.write("$ " + " ".join(shlex.quote(c if "-password=" not in c else "-password=***") for c in cmd) + "\n\n")
    log.flush()
    proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=lambda: (proc.wait(), log.close()), daemon=True).start()
    return proc
