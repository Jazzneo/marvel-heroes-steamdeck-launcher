"""Desktop / Steam integration and the one-time port setup."""
import os
import shutil
import subprocess

from .config import BUNDLE_DIR, ASSETS_DIR, in_gaming_mode

DESKTOP_FILE = os.path.expanduser("~/.local/share/applications/mhlauncher.desktop")
PORT_SCRIPT = os.path.join(BUNDLE_DIR, "tools", "enable-login-ports.sh")
LAUNCH_SCRIPT = os.path.join(BUNDLE_DIR, "MHLauncher.sh")


def write_desktop_file():
    os.makedirs(os.path.dirname(DESKTOP_FILE), exist_ok=True)
    icon = os.path.join(ASSETS_DIR, "icon.png")
    with open(DESKTOP_FILE, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\n"
                "Type=Application\n"
                "Name=MH Launcher\n"
                "Comment=Marvel Heroes launcher for MHServerEmu\n"
                f"Exec=\"{LAUNCH_SCRIPT}\"\n"
                f"Path={BUNDLE_DIR}\n"
                f"Icon={icon}\n"
                "Terminal=false\n"
                "Categories=Game;\n")
    os.chmod(DESKTOP_FILE, 0o755)
    return DESKTOP_FILE


def add_to_steam():
    """Returns (ok, message)."""
    desktop = write_desktop_file()
    tool = shutil.which("steamos-add-to-steam")
    if tool:
        try:
            subprocess.Popen([tool, desktop], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True, ("Sent to Steam. Find 'MH Launcher' in your Library under Non-Steam "
                          "(Steam must be running).")
        except OSError as e:
            return False, f"steamos-add-to-steam failed: {e}"
    return False, ("Created a menu entry. To add it to Steam: open Steam → Games → "
                   "'Add a Non-Steam Game to My Library…' → pick 'MH Launcher' "
                   f"(or browse to {LAUNCH_SCRIPT}).")


# Web servers that commonly grab ports 80/443 and block the login bridge
KNOWN_WEB_SERVERS = ("nginx", "apache2", "httpd", "caddy", "lighttpd", "traefik", "haproxy")


def _unit_of(pid):
    """systemd service a process belongs to (e.g. 'nginx.service'), or None."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            for line in f:
                for part in line.strip().split("/"):
                    if part.endswith(".service") and not part.startswith("user@"):
                        return part
    except OSError:
        pass
    return None


def find_blocking_web_servers():
    """Running web servers (any user, incl. root) as [(pid, name, unit_or_None)].

    /proc/<pid>/comm and /proc/<pid>/cgroup are readable for everyone, so this works
    even when nginx runs as root and we cannot see which process owns the socket."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in KNOWN_WEB_SERVERS:
            found.append((int(pid), comm, _unit_of(pid)))
    return found


def build_fix_script(enable_ports=True, servers=()):
    """Shell script (run as root) that stops blocking web servers and opens ports 80/443."""
    lines = ["set +e"]
    units = sorted({u for _p, _n, u in servers if u})
    for u in units:
        # stop now and keep it from starting at boot; re-enable with: systemctl enable --now <unit>
        lines.append(f"systemctl disable --now '{u}'")
    loose = sorted({p for p, _n, u in servers if not u})
    if loose:
        lines.append("kill " + " ".join(map(str, loose)) + " 2>/dev/null; sleep 1")
        lines.append("kill -9 " + " ".join(map(str, loose)) + " 2>/dev/null")
    if enable_ports:
        lines.append("mkdir -p /etc/sysctl.d")
        lines.append("echo net.ipv4.ip_unprivileged_port_start=80 > /etc/sysctl.d/60-mhlauncher.conf")
        lines.append("sysctl -w net.ipv4.ip_unprivileged_port_start=80")
    lines.append("exit 0")
    return "\n".join(lines)


def run_as_admin(script, timeout=180):
    """Run a root script through pkexec (shows the system password dialog). Returns (ok, message)."""
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return False, "pkexec is not available on this system."
    try:
        r = subprocess.run([pkexec, "/bin/sh", "-c", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "The password prompt timed out."
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, "Done."
    if r.returncode in (126, 127):
        return False, "The password prompt was cancelled or no password dialog is available here."
    return False, (r.stderr or r.stdout or f"exit code {r.returncode}").strip()[-300:]


def try_enable_ports_graphically(servers=()):
    """Open ports 80/443 and stop blocking web servers in one admin prompt. Returns (ok, message)."""
    return run_as_admin(build_fix_script(True, servers))


def manual_fix_instructions(servers=()):
    lines = []
    for u in sorted({u for _p, _n, u in servers if u}):
        lines.append(f"sudo systemctl disable --now {u}")
    loose = sorted({p for p, _n, u in servers if not u})
    if loose:
        lines.append("sudo kill " + " ".join(map(str, loose)))
    lines.append("echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/60-mhlauncher.conf")
    lines.append("sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80")
    return ("The automatic fix could not ask for your password here (this happens in Gaming Mode on some "
            "systems). Switch to Desktop Mode and press Play there once, or open Konsole and paste:\n\n"
            + "\n".join(lines) + "\n\nOn Steam Deck, run 'passwd' first if you never set a password.")


def manual_port_instructions():
    return ("The game client always logs in through ports 80 and 443, and Linux only allows "
            "programs to use ports below 1024 after a one-time change.\n\n"
            "Switch to Desktop Mode, open Konsole and paste these two lines:\n\n"
            "echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/60-mhlauncher.conf\n"
            "sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80\n\n"
            "On Steam Deck, if you never set a password, run 'passwd' first. You only have to do this once.")
