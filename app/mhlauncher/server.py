"""Controls the MHServerEmu process: setup, start, graceful stop, status, console."""
import configparser
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import zipfile

from . import dotnet
from .config import BUNDLED_SERVER_DIR, LOG_DIR

SIP_FILES = ("Calligraphy.sip", "mu_cdata.sip")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# files inside the server folder that hold user data and must survive updates
PRESERVE = {"ConfigOverride.ini", "Data/Account.db", "Data/Leaderboards/Leaderboards.db",
            "Data/Web/ApiKeys.json"}

SECRET_CMDS = re.compile(r"^(!account (?:verify|create|password)\s.*\s)(\S+)$")


INLINE_SECRET = re.compile(r"(!account (?:verify \S+|password \S+|create \S+ \S+) )([^\s\]]+)")


INVOKED_SECRET = re.compile(r"(command=\[account (?:verify|password|create)\], parameters=\[)([^\]]*)\]")


def mask_secrets(cmd):
    """Hide the password argument of account commands in the visible log."""
    return SECRET_CMDS.sub(lambda m: m.group(1) + "•" * len(m.group(2)), cmd)



# ------------------------------------------------------------ port owners
class PortConflict(RuntimeError):
    """A port the server needs is already taken. .owners = [(port, pid, name, is_mhserveremu)]"""

    def __init__(self, owners):
        self.owners = owners
        parts, seen = [], {}
        for port, pid, name, is_mh in owners:
            if pid and pid in seen:
                seen[pid].append(port)
                continue
            seen[pid] = [port]
        for port, pid, name, is_mh in owners:
            if pid and seen.get(pid, [port])[0] != port:
                continue
            port = " and ".join(map(str, seen.get(pid, [port]))) if pid else port
            if is_mh:
                parts.append(f"Port{'s' if ' and ' in str(port) else ''} {port} {'are' if ' and ' in str(port) else 'is'} used by "
                             f"{'an ' + name if name and name != 'MHServerEmu' else 'another MHServerEmu'} "
                             f"that is still running (pid {pid}).")
            elif pid:
                parts.append(f"Port {port} is used by another program: {name} (pid {pid}).")
            else:
                parts.append(f"Port {port} is used by a program of another user (maybe started with sudo).")
        super().__init__(" ".join(parts))

    @property
    def mh_pids(self):
        return sorted({pid for _p, pid, _n, is_mh in self.owners if is_mh and pid})


def _listen_inodes(port):
    inodes = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    parts = line.split()
                    local, state, inode = parts[1], parts[3], parts[9]
                    if state == "0A" and int(local.rsplit(":", 1)[1], 16) == port:
                        inodes.add(inode)
        except (OSError, StopIteration, IndexError, ValueError):
            pass
    return inodes


def port_owner(port):
    """Return (pid, cmdline) of the process listening on port, (None, '') if unknown, or None if free."""
    inodes = _listen_inodes(port)
    if not inodes:
        return None
    targets = {f"socket:[{i}]" for i in inodes}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(os.path.join(fd_dir, fd)) in targets:
                        return int(pid), " ".join(_argv(pid))
                except OSError:
                    continue
        except OSError:
            continue
    return None, ""


def _argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def argv_is_mhserveremu(argv):
    """True for 'dotnet MHServerEmu.dll' or the native 'MHServerEmu' apphost (not shells/editors)."""
    if not argv:
        return False
    if os.path.basename(argv[0]) == "MHServerEmu":
        return True
    return any(os.path.basename(a) == "MHServerEmu.dll" for a in argv[1:4])


def is_mhserveremu(cmdline):
    return argv_is_mhserveremu((cmdline or "").split(" "))


def find_mhserveremu_pids(exclude=()):
    pids = []
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) not in exclude and int(pid) != os.getpid():
            if argv_is_mhserveremu(_argv(pid)):
                pids.append(int(pid))
    return pids


def _alive(pid):
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[-1][0] != "Z"
    except OSError:
        return False


def stop_foreign_server(pid, timeout=40):
    """Stop an MHServerEmu we did not start. Tries a clean '!server shutdown' through its stdin
    (works when stdin is a pipe, e.g. a leftover from this launcher), then SIGTERM, then SIGKILL."""
    graceful = False
    try:
        if os.readlink(f"/proc/{pid}/fd/0").startswith("pipe:"):
            fd = os.open(f"/proc/{pid}/fd/0", os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"!server shutdown\n")
            graceful = True
            deadline = time.time() + timeout
            while _alive(pid) and time.time() < deadline:
                time.sleep(1)
                try:
                    os.write(fd, b"\n")
                except OSError:
                    break
            os.close(fd)
    except OSError:
        pass
    for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, 3)):
        if not _alive(pid):
            break
        try:
            os.kill(pid, sig)
        except OSError:
            break
        t = time.time()
        while _alive(pid) and time.time() - t < wait:
            time.sleep(0.2)
    return not _alive(pid), graceful


FATAL_HINTS = [
    (re.compile(r"cannot bind", re.I),
     "A network port the server needs is already in use (usually an old server that is still running)."),
    (re.compile(r"\.sip are missing|mu_cdata\.sip and/or", re.I),
     "Game data is missing. Pick your Marvel Heroes client on the Game Client tab again."),
]


STOPPED, STARTING, ONLINE, STOPPING, CRASHED = "Stopped", "Starting", "Online", "Stopping", "Crashed"

# Options exposed in the launcher's Server screen: (Section.Key, label, default)
SERVER_TOGGLES = [
    ("CustomGameOptions.AutoUnlockAvatars", "Unlock all heroes", "false"),
    ("CustomGameOptions.AutoUnlockTeamUps", "Unlock all team-ups", "false"),
    ("CustomGameOptions.DisableInstancedLoot", "Free-for-all loot", "false"),
    ("CustomGameOptions.DisableMovementPowerChargeCost", "Free movement powers", "false"),
    ("GameData.LoadAllPrototypes", "Preload all data (slower start, less lag)", "false"),
    ("PlayerManager.AllowClientVersionMismatch", "Allow other client versions", "false"),
]


class ServerManager:
    def __init__(self, settings):
        self.settings = settings
        self.proc = None
        self.state = STOPPED
        self.status = {}
        self.log = []                 # recent console lines
        self.log_version = 0          # bumps whenever log changes (for UI)
        self.started_at = 0
        self.message = ""
        self.fatal = ""               # first [Fatal] line seen during startup
        self._lock = threading.Lock()
        self._waiters = []            # (regex, event, result_holder)
        self._logfile = None
        self._stop_requested = False
        self.on_online = []           # callbacks fired once each time the server comes online

    # ------------------------------------------------------------------ paths
    @property
    def dir(self):
        return os.path.expanduser(self.settings.get("server_dir"))

    def dll_path(self):
        return os.path.join(self.dir, "MHServerEmu.dll")

    def installed(self):
        return os.path.isfile(self.dll_path())

    def game_data_ok(self):
        return all(os.path.isfile(os.path.join(self.dir, "Data", "Game", f)) for f in SIP_FILES)

    def install_bundled(self):
        """First run: copy the server shipped with the launcher into the data folder."""
        if self.installed() or not os.path.isfile(os.path.join(BUNDLED_SERVER_DIR, "MHServerEmu.dll")):
            return self.installed()
        shutil.copytree(BUNDLED_SERVER_DIR, self.dir, dirs_exist_ok=True)
        self._fix_exec_bits()
        return True

    def _fix_exec_bits(self):
        exe = os.path.join(self.dir, "MHServerEmu")
        if os.path.isfile(exe):
            os.chmod(exe, 0o755)

    def import_zip(self, zip_path, progress=None):
        """Update/install server files from an MHServerEmu release zip, keeping user data."""
        if self.proc:
            raise RuntimeError("Stop the server before updating it.")
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            if not any(n.endswith("MHServerEmu.dll") for n in names):
                raise ValueError("This zip does not contain MHServerEmu.dll")
            # handle zips that wrap everything in a top-level folder
            dll = next(n for n in names if n.endswith("MHServerEmu.dll"))
            prefix = dll[: -len("MHServerEmu.dll")]
            members = [n for n in names if n.startswith(prefix) and not n.endswith("/")]
            os.makedirs(self.dir, exist_ok=True)
            for i, name in enumerate(members):
                rel = name[len(prefix):]
                if rel in PRESERVE and os.path.exists(os.path.join(self.dir, rel)):
                    continue
                dest = os.path.realpath(os.path.join(self.dir, rel))
                if not dest.startswith(os.path.realpath(self.dir) + os.sep):
                    continue  # zip-slip protection
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                if progress and i % 20 == 0:
                    progress(i / len(members), f"Extracting {rel}")
        self._fix_exec_bits()
        if progress:
            progress(1.0, "Server updated")

    def sync_game_data(self, client_root):
        """Copy Calligraphy.sip / mu_cdata.sip from the client into the server. Returns list copied."""
        copied = []
        src_dir = os.path.join(client_root, "Data", "Game")
        dst_dir = os.path.join(self.dir, "Data", "Game")
        os.makedirs(dst_dir, exist_ok=True)
        for f in SIP_FILES:
            src, dst = os.path.join(src_dir, f), os.path.join(dst_dir, f)
            if not os.path.isfile(src):
                raise FileNotFoundError(f"{f} not found in {src_dir}")
            if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src) \
                    or os.path.getmtime(dst) < os.path.getmtime(src):
                shutil.copy2(src, dst)
                copied.append(f)
        return copied

    # ------------------------------------------------------------- config
    def _ini(self, name):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        path = os.path.join(self.dir, name)
        if os.path.isfile(path):
            try:
                cp.read(path, encoding="utf-8-sig")
            except configparser.Error:
                pass
        return cp

    def config_value(self, section, key, default=""):
        for name in ("ConfigOverride.ini", "Config.ini"):
            cp = self._ini(name)
            if cp.has_option(section, key):
                return cp.get(section, key).strip()
        return default

    def web_endpoint(self):
        host = self.config_value("WebFrontend", "Address", "localhost") or "localhost"
        try:
            port = int(self.config_value("WebFrontend", "Port", "8080"))
        except ValueError:
            port = 8080
        if host in ("*", "+", "0.0.0.0"):
            host = "localhost"
        return host, port

    def frontend_port(self):
        try:
            return int(self.config_value("Frontend", "Port", "4306"))
        except ValueError:
            return 4306

    WEB_PORTS = (8080, 8580, 8680, 18080, 28080)
    FRONTEND_PORTS = (4306, 4316, 14306, 24306)

    def _port_free(self, port):
        if port_owner(port) is not None:
            return False
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def check_ports(self):
        """Pick free ports for the server.

        A leftover MHServerEmu raises PortConflict (the launcher offers to stop it).
        Any other program (e.g. Steam's steamwebhelper, which often holds 8080 on a
        Steam Deck) simply makes the server move to the next free port; the game only
        talks to the launcher's login bridge, so it never needs to know.
        """
        self.port_overrides = {}
        owners = []
        for key, current, candidates in (
                ("Frontend.Port", self.frontend_port(), self.FRONTEND_PORTS),
                ("WebFrontend.Port", self.web_endpoint()[1], self.WEB_PORTS)):
            res = port_owner(current)
            if res is None:
                continue
            pid, cmd = res
            if self.proc and pid == self.proc.pid:
                continue
            if is_mhserveremu(cmd):
                owners.append((current, pid, "MHServerEmu", True))
                continue
            alt = next((p for p in candidates if p != current and self._port_free(p)), None)
            if alt is None:
                name = cmd.split(" ")[0].rsplit("/", 1)[-1] if cmd else ""
                owners.append((current, pid, name, False))
            else:
                name = cmd.split(" ")[0].rsplit("/", 1)[-1] if cmd else "another program"
                self.port_overrides[key] = str(alt)
                self._add_line(f"[Launcher] Port {current} is used by {name}; using port {alt} instead.")
        # a leftover server may still be loading data and not listening yet
        if not owners:
            for pid in find_mhserveremu_pids(exclude=(self.proc.pid,) if self.proc else ()):
                owners.append((self.frontend_port(), pid, "MHServerEmu", True))
        if owners:
            raise PortConflict(owners)

    def write_overrides(self, lan_ip=None):
        """Write the launcher-managed values into ConfigOverride.ini."""
        cp = self._ini("ConfigOverride.ini")
        values = dict(self.settings.get("server_options") or {})
        # the launcher needs the web API for account creation and status
        values["WebFrontend.EnableWebApi"] = "true"
        values["Frontend.PublicAddress"] = lan_ip or "127.0.0.1"
        values["Frontend.BindIP"] = "0.0.0.0"
        values.update(getattr(self, "port_overrides", {}) or {})
        # file logging helps when something goes wrong in Gaming Mode
        for full, val in values.items():
            section, key = full.split(".", 1)
            if not cp.has_section(section):
                cp.add_section(section)
            cp.set(section, key, str(val))
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "ConfigOverride.ini"), "w", encoding="utf-8") as f:
            cp.write(f, space_around_delimiters=False)

    def option(self, full_key, default="false"):
        opts = self.settings.get("server_options") or {}
        if full_key in opts:
            return str(opts[full_key])
        section, key = full_key.split(".", 1)
        return self.config_value(section, key, default)

    def set_option(self, full_key, value):
        opts = dict(self.settings.get("server_options") or {})
        opts[full_key] = value
        self.settings.set("server_options", opts)

    # ------------------------------------------------------------ process
    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def preflight(self):
        """Return a list of human readable problems that prevent starting."""
        problems = []
        if not self.installed():
            problems.append("MHServerEmu files are missing (Settings → Import server zip).")
        elif not self.game_data_ok():
            problems.append("Game data missing: pick your Marvel Heroes client first (Client tab).")
        exe, _ = dotnet.find_dotnet()
        if not exe:
            problems.append(".NET 8 runtime is not installed (Settings → Install .NET).")
        return problems

    def start(self, lan_ip=None):
        with self._lock:
            if self.running:
                return True
            problems = self.preflight()
            if problems:
                self.message = problems[0]
                return False
            exe, _ = dotnet.find_dotnet()
            self.log = []
            self.check_ports()
            self.write_overrides(lan_ip)
            os.makedirs(LOG_DIR, exist_ok=True)
            self._logfile = open(os.path.join(LOG_DIR, "server.log"), "w", encoding="utf-8")
            for line in self.log:
                self._logfile.write(line + "\n")
            self.status = {}
            self._stop_requested = False
            self.fatal = ""
            # The process is spawned from the long-lived reader thread so that the
            # parent-death signal (see _spawn) only fires when the launcher itself dies.
            spawned = threading.Event()
            box = []
            threading.Thread(target=self._reader, args=([exe, "MHServerEmu.dll"], dotnet.server_env(exe),
                                                         spawned, box), daemon=True, name="srv-out").start()
            spawned.wait(15)
            if not box or isinstance(box[0], Exception):
                self.message = f"Could not start the server: {box[0] if box else 'timeout'}"
                return False
            self.proc = box[0]
            self.state = STARTING
            self.started_at = time.time()
            self.message = "Server is starting…"
            threading.Thread(target=self._poller, args=(self.proc,), daemon=True, name="srv-poll").start()
            return True

    @staticmethod
    def _child_setup():
        # new session: Ctrl+C in a terminal goes to the launcher only, which then stops the server cleanly
        os.setsid()
        # if the launcher is killed hard, take the server down with it instead of leaving
        # an orphan that blocks ports 4306/8080 (PR_SET_PDEATHSIG = 1)
        try:
            import ctypes
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
        except Exception:
            pass

    def _add_line(self, line):
        line = ANSI_RE.sub("", line.rstrip("\r\n"))
        line = INLINE_SECRET.sub(lambda m: m.group(1) + "•••", line)
        line = INVOKED_SECRET.sub(lambda m: m.group(1) + m.group(2).rsplit(" ", 1)[0] + " •••]", line)
        if not line or line.rstrip().endswith("Unknown command"):
            return  # (empty lines are sent to the console to let it exit during shutdown)
        if "[Fatal]" in line and self.state == STARTING and not self.fatal:
            self.fatal = line.split("]", 3)[-1].strip() if line.count("]") >= 3 else line
            # the server waits for a key press after a fatal error; end it so the launcher can report
            threading.Thread(target=self._end_after_fatal, args=(self.proc,), daemon=True).start()
        self.log.append(line)
        if len(self.log) > 2000:
            del self.log[:500]
        self.log_version += 1
        if self._logfile:
            try:
                self._logfile.write(line + "\n")
                self._logfile.flush()
            except Exception:
                pass
        for w in list(self._waiters):
            m = w[0].search(line)
            if m:
                w[2].append(m)
                w[1].set()

    def _reader(self, cmd, env, spawned, box):
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.dir, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                preexec_fn=self._child_setup)
            box.append(proc)
        except Exception as e:
            box.append(e)
            spawned.set()
            return
        spawned.set()
        for line in proc.stdout:
            self._add_line(line)
        code = proc.wait()
        with self._lock:
            if self.proc is proc:
                crashed = not self._stop_requested or bool(self.fatal)
                self.state = CRASHED if crashed else STOPPED
                self.message = (("Server failed to start: " + self.fatal) if self.fatal else
                                f"Server exited unexpectedly (code {code}). Check the log."
                                if crashed else "Server stopped.")
                self.proc = None
                self.status = {}
        if self._logfile:
            self._logfile.close()
            self._logfile = None

    def _poller(self, proc):
        host, port = self.web_endpoint()
        url = f"http://{host}:{port}/ServerStatus"
        while proc.poll() is None:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    self.status = json.loads(r.read().decode("utf-8") or "{}")
                if self.state == STARTING:
                    self.state = ONLINE
                    self.message = "Server is online."
                    for cb in list(self.on_online):
                        try:
                            cb()
                        except Exception as e:
                            print("on_online callback failed:", e)
            except Exception:
                pass
            time.sleep(2 if self.state == ONLINE else 1)

    def send(self, command):
        if not self.running:
            return False
        try:
            self.proc.stdin.write(command.strip() + "\n")
            self.proc.stdin.flush()
            self._add_line("> " + mask_secrets(command.strip()))
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def command(self, command, pattern, timeout=8):
        """Send a console command and wait for a log line that matches pattern."""
        rx = re.compile(pattern)
        ev = threading.Event()
        holder = []
        w = (rx, ev, holder)
        self._waiters.append(w)
        try:
            if not self.send(command):
                return None
            ev.wait(timeout)
            return holder[0] if holder else None
        finally:
            self._waiters.remove(w)

    def stop(self, timeout=45):
        """Graceful stop (saves player data). Blocks; call from a worker thread."""
        proc = self.proc
        if not proc or proc.poll() is not None:
            self.state = STOPPED
            return
        self._stop_requested = True
        self.state = STOPPING
        self.message = "Saving players and shutting down…"
        self.send("!server shutdown")
        deadline = time.time() + timeout
        next_nudge = time.time() + 2
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.25)
            # once services have shut down, the console loop exits on its next input line
            if time.time() >= next_nudge:
                next_nudge = time.time() + 2
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                except Exception:
                    pass
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(10)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass

    def _end_after_fatal(self, proc):
        time.sleep(1.5)
        if proc and proc.poll() is None:
            self._stop_requested = True
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
            except Exception:
                pass
            time.sleep(1.5)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass

    def fatal_message(self):
        if not self.fatal:
            return ""
        for rx, hint in FATAL_HINTS:
            if rx.search(self.fatal):
                return f"{hint}\n\nServer said: {self.fatal}"
        return f"The server hit a fatal error:\n{self.fatal}\n\nSee the Server tab log for details."

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self._stop_requested = True
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                pass

    # ----------------------------------------------------------- helpers
    def players_online(self):
        s = self.status or {}
        return s.get("PlayerManagerPlayers", s.get("GisPlayers", 0))

    def uptime(self):
        s = self.status or {}
        if "CurrentTime" in s and "StartupTime" in s:
            return max(0, int(s["CurrentTime"] - s["StartupTime"]))
        return int(time.time() - self.started_at) if self.running else 0
