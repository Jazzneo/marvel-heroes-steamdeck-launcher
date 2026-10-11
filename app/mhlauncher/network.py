"""Networking for playing with friends: addresses, router ports (UPnP) and the firewall."""
import ipaddress
import urllib.error
import re
import shutil
import socket
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .authproxy import lan_ip

# Ports a friend's game needs on the host: SiteConfig (80), login (443), game frontend (4306 by default).
FRIEND_PORTS = (80, 443, 4306)


# ============================================================== addresses
def interfaces():
    """[(ifname, ipv4)] for interfaces with an IPv4 address (no loopback). Asks the kernel directly
    (SIOCGIFADDR), so it works without the 'ip' tool."""
    import fcntl
    import struct
    out = []
    try:
        names = [n for _i, n in socket.if_nameindex()]
    except OSError:
        names = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            try:
                res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))  # SIOCGIFADDR
                ip = socket.inet_ntoa(res[20:24])
            except OSError:
                continue
            if not ip.startswith("127."):
                out.append((name, ip))
    finally:
        s.close()
    return out


def _cgnat(ip):
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def tailscale_ip(ask_cli=True):
    for name, ip in interfaces():
        if name.startswith("tailscale") or (name.startswith("ts") and _cgnat(ip)):
            return ip
    if ask_cli and shutil.which("tailscale"):
        try:
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                                timeout=2).stdout.strip().splitlines()
            if ip and re.match(r"^[\d.]+$", ip[0]):
                return ip[0]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def zerotier_ip():
    for name, ip in interfaces():
        if name.startswith("zt"):
            return ip
    return None


def address_options(snap=None):
    """[(kind, label, address_or_None, help)] for the Friends tab (from the cached snapshot)."""
    snap = snap or snapshot()
    lan, ts, zt = snap["lan"], snap["tailscale"], snap["zerotier"]
    return [
        ("tailscale", f"Tailscale  {ts}" if ts else "Tailscale (not running)", ts,
         "Free virtual network. Both of you install Tailscale and join the same tailnet (share your machine). "
         "Works over the internet, no router setup."),
        ("lan", f"Same Wi-Fi / home network  {lan}", None if lan.startswith("127.") else lan,
         "Your friend is on the same network as you."),
        ("zerotier", f"ZeroTier  {zt}" if zt else "ZeroTier (not running)", zt,
         "Free virtual network like Tailscale: both join the same ZeroTier network."),
        ("internet", "Internet (open router ports with UPnP)", None,
         "The launcher asks your router to forward ports 80, 443 and 4306. Needs UPnP on the router and no "
         "CGNAT from your provider."),
    ]


# ============================================================== cached snapshot
# Asking firewall-cmd / tailscale means starting programs (100+ ms each). The UI redraws up to 60 times a
# second, so it only ever reads this snapshot; a background thread refreshes it.
import threading
import time as _time

_snap = {"at": 0.0, "lan": "127.0.0.1", "tailscale": None, "zerotier": None,
         "fw": (None, False), "fw_open": None}
_snap_lock = threading.Lock()
_refreshing = threading.Event()


def refresh_snapshot():
    """Full refresh (starts programs; call from a background thread)."""
    new = {"lan": lan_ip(), "tailscale": tailscale_ip(ask_cli=True), "zerotier": zerotier_ip()}
    fw = firewall()
    new["fw"] = fw
    new["fw_open"] = firewall_ports_open(fw=fw) if fw[1] else True
    new["at"] = _time.time()
    with _snap_lock:
        _snap.update(new)
    return dict(_snap)


def _refresh_bg():
    try:
        refresh_snapshot()
    except Exception as e:
        print("[network] refresh failed:", e)
    finally:
        _refreshing.clear()


def mark_firewall_open(is_open):
    """Update the cached firewall state right after the launcher changed it."""
    with _snap_lock:
        _snap["fw_open"] = is_open


def snapshot(max_age=15.0):
    """Cached network facts; never blocks on outside programs. Refreshes in the background when stale."""
    with _snap_lock:
        first = _snap["at"] == 0.0
        if first:   # quick values right away (no programs started)
            _snap.update(lan=lan_ip(), tailscale=tailscale_ip(ask_cli=False), zerotier=zerotier_ip(),
                         at=0.001)
        stale = _time.time() - _snap["at"] > max_age
        data = dict(_snap)
    if (first or stale) and not _refreshing.is_set():
        _refreshing.set()
        threading.Thread(target=_refresh_bg, daemon=True, name="net-refresh").start()
    return data


# ============================================================== UPnP (router port forwarding)
SSDP_ADDR = ("239.255.255.250", 1900)
IGD_TYPES = ("urn:schemas-upnp-org:device:InternetGatewayDevice:2",
             "urn:schemas-upnp-org:device:InternetGatewayDevice:1")
WAN_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2",
                "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")


class UPnPError(Exception):
    pass


def discover_igd(timeout=3.0):
    """Return the router's description URL (LOCATION) or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    try:
        for st in IGD_TYPES + ("ssdp:all",):
            msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                   f"MX: 2\r\nST: {st}\r\n\r\n").encode()
            s.sendto(msg, SSDP_ADDR)
        while True:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                return None
            text = data.decode(errors="replace")
            if "InternetGatewayDevice" in text or "WANIPConnection" in text or "WANPPPConnection" in text:
                m = re.search(r"^location:\s*(\S+)", text, re.I | re.M)
                if m:
                    return m.group(1)
    except OSError:
        return None
    finally:
        s.close()


def _control_url(location):
    with urllib.request.urlopen(location, timeout=5) as r:
        xml = r.read()
    root = ET.fromstring(xml)
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    base = root.findtext("d:URLBase", default="", namespaces=ns) or location
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        stype = svc.findtext("d:serviceType", default="", namespaces=ns)
        if stype in WAN_SERVICES:
            ctrl = svc.findtext("d:controlURL", default="", namespaces=ns)
            return urllib.parse.urljoin(base, ctrl), stype
    raise UPnPError("The router does not offer port forwarding through UPnP.")


def _soap(url, stype, action, args):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{stype}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(url, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{stype}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        m = re.search(r"<errorDescription>(.*?)</errorDescription>", detail)
        raise UPnPError(f"Router refused {action}: {m.group(1) if m else e.code}")


class Router:
    def __init__(self, location=None):
        location = location or discover_igd()
        if not location:
            raise UPnPError("No UPnP router found. Turn on UPnP in your router's settings, or use Tailscale.")
        self.url, self.stype = _control_url(location)

    def external_ip(self):
        xml = _soap(self.url, self.stype, "GetExternalIPAddress", [])
        m = re.search(r"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>", xml)
        return m.group(1).strip() if m else None

    def add(self, port, internal_ip, desc="MH Launcher"):
        _soap(self.url, self.stype, "AddPortMapping", [
            ("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", "TCP"),
            ("NewInternalPort", port), ("NewInternalClient", internal_ip), ("NewEnabled", 1),
            ("NewPortMappingDescription", desc), ("NewLeaseDuration", 0)])

    def remove(self, port):
        try:
            _soap(self.url, self.stype, "DeletePortMapping", [
                ("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", "TCP")])
        except (UPnPError, OSError):
            pass


def open_router_ports(ports=FRIEND_PORTS):
    """Forward ports to this machine. Returns (public_ip, warning_or_None)."""
    r = Router()
    me = lan_ip()
    for p in ports:
        r.add(p, me)
    ip = r.external_ip()
    warning = None
    try:
        nonpublic = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "0.0.0.0/8")
        if ip and any(ipaddress.ip_address(ip) in ipaddress.ip_network(n) for n in nonpublic):
            warning = (f"Your router's internet address ({ip}) is not public: your provider uses CGNAT, so friends "
                       "can't reach it. Use Tailscale instead.")
    except ValueError:
        pass
    return ip, warning


def close_router_ports(ports=FRIEND_PORTS):
    try:
        r = Router()
    except (UPnPError, OSError):
        return
    for p in ports:
        r.remove(p)


# ============================================================== firewall
def firewall():
    """('firewalld' | 'ufw' | None, active_bool)."""
    if shutil.which("firewall-cmd"):
        try:
            state = subprocess.run(["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5)
            return "firewalld", state.returncode == 0 and "running" in state.stdout
        except (OSError, subprocess.SubprocessError):
            return "firewalld", False
    if shutil.which("ufw"):
        try:
            st = subprocess.run(["systemctl", "is-active", "ufw"], capture_output=True, text=True, timeout=5)
            return "ufw", st.stdout.strip() == "active"
        except (OSError, subprocess.SubprocessError):
            return "ufw", False
    return None, False


def firewall_ports_open(ports=FRIEND_PORTS, fw=None):
    """True / False when it can tell, None when it can't (querying may need admin rights)."""
    kind, active = fw or firewall()
    if not active:
        return True
    if kind == "firewalld":
        try:
            for p in ports:
                r = subprocess.run(["firewall-cmd", f"--query-port={p}/tcp"], capture_output=True,
                                   text=True, timeout=5)
                if r.returncode not in (0, 1):
                    return None
                if r.returncode == 1:
                    return False
            return True
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def firewall_script(ports=FRIEND_PORTS, fw=None):
    """Shell lines (run as root) that let friends through the firewall, or [] if none is active.

    Each rule is only added when it isn't there yet, and every rule this adds is printed as 'ADDED:<what>',
    so closing later removes exactly those (rules you had before stay)."""
    kind, active = fw or firewall()
    if not active:
        return []
    if kind == "firewalld":
        lines = [f"firewall-cmd --permanent --query-port={p}/tcp >/dev/null 2>&1 || "
                 f"{{ firewall-cmd --permanent --add-port={p}/tcp >/dev/null && echo ADDED:{p}; }}" for p in ports]
        # Tailscale's interface: trusted, like Tailscale recommends
        lines.append("if ip link show tailscale0 >/dev/null 2>&1 || [ -d /sys/class/net/tailscale0 ]; then "
                     "firewall-cmd --permanent --zone=trusted --query-interface=tailscale0 >/dev/null 2>&1 || "
                     "{ firewall-cmd --permanent --zone=trusted --add-interface=tailscale0 >/dev/null && "
                     "echo ADDED:tailscale0; }; fi")
        lines.append("firewall-cmd --reload >/dev/null")
        return lines
    if kind == "ufw":
        return [f"ufw status | grep -qE '^{p}/tcp +ALLOW' || {{ ufw allow {p}/tcp comment 'MH Launcher' "
                f">/dev/null && echo ADDED:{p}; }}" for p in ports]
    return []


def parse_added(output):
    """What an open script added: (ports, tailscale_trusted)."""
    added = re.findall(r"ADDED:(\S+)", output or "")
    ports = sorted({int(a) for a in added if a.isdigit()})
    return ports, "tailscale0" in added


def firewall_close_script(ports, tailscale_trusted=False, fw=None):
    """Shell lines (run as root) that remove the rules the launcher added. [] if nothing to do."""
    kind, active = fw or firewall()
    if not active or not (ports or tailscale_trusted):
        return []
    if kind == "firewalld":
        lines = [f"firewall-cmd --permanent --remove-port={p}/tcp >/dev/null 2>&1" for p in ports]
        if tailscale_trusted:
            lines.append("firewall-cmd --permanent --zone=trusted --remove-interface=tailscale0 >/dev/null 2>&1")
        lines.append("firewall-cmd --reload >/dev/null")
        return lines
    if kind == "ufw":
        return [f"ufw delete allow {p}/tcp >/dev/null 2>&1" for p in ports]
    return []


# ============================================================== joining
def check_host(host, timeout=5):
    """Can we reach a friend's server? Returns a list of problems (empty = looks good)."""
    problems = []
    host = host.strip()
    try:
        with urllib.request.urlopen(f"http://{host}/SiteConfig.xml", timeout=timeout) as r:
            if b"AuthServerAddress" not in r.read():
                problems.append("Port 80 answered, but not with a Marvel Heroes SiteConfig.")
    except Exception as e:
        problems.append(f"Can't reach {host} on port 80 ({getattr(e, 'reason', e)}).")
    for port in (443,):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError as e:
            problems.append(f"Can't reach {host} on port {port} ({e.strerror or e}).")
    return problems


def friend_server_status(host, timeout=5):
    """The friend's /ServerStatus (dict) or None."""
    import json
    try:
        with urllib.request.urlopen(f"http://{host}/ServerStatus", timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None


def create_account_on(host, email, player, password, timeout=10):
    """Create an account on a friend's server (through their launcher's bridge). Returns (ok, message)."""
    import json
    from .accounts import RESULT_TEXT
    body = json.dumps({"Email": email.strip().lower(), "PlayerName": player, "Password": password}).encode()
    req = urllib.request.Request(f"http://{host}/AccountManagement/Create", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read().decode() or "{}").get("Result", 1)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return False, "Your friend's launcher doesn't allow creating accounts. Ask them to turn it on."
        return False, f"Your friend's server answered with an error ({e.code})."
    except Exception as e:
        return False, f"Can't reach your friend's server: {getattr(e, 'reason', e)}"
    return result == 0, RESULT_TEXT.get(result, f"Unknown error (code {result}).")


# ============================================================== Tailscale setup
def tailscale_installed():
    return bool(shutil.which("tailscale"))


def tailscale_state():
    """'missing', 'daemon-off', or Tailscale's BackendState ('Running', 'NeedsLogin', 'Stopped', ...)."""
    import json
    if not tailscale_installed():
        return "missing"
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "daemon-off"
    try:
        return json.loads(r.stdout).get("BackendState") or "unknown"
    except ValueError:
        err = (r.stderr + r.stdout).lower()
        if "failed to connect" in err or "connection refused" in err or "no such file" in err \
                or "tailscaled" in err:
            return "daemon-off"
        return "unknown"


def tailscale_admin_script(user):
    """Root lines: start the Tailscale service (and at boot) and let this user control it without sudo."""
    return ("set -e\nsystemctl enable --now tailscaled\nfor i in 1 2 3 4 5 6 7 8 9 10; do "
            "tailscale status >/dev/null 2>&1 && break; tailscale status 2>&1 | grep -qi 'logged out\\|needs' "
            "&& break; sleep 1; done\n"
            f"tailscale set --operator={user}\nexit 0")


def tailscale_login(timeout=30):
    """Start 'tailscale up' as this user and return (login_url_or_None, output).

    'tailscale up' prints a login URL and then waits until the login is done; it keeps running in the
    background. Raises PermissionError when this user may not control Tailscale yet."""
    import threading as _th
    import time as _t
    proc = subprocess.Popen(["tailscale", "up"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    lines, found = [], {}

    def reader():
        for line in proc.stdout:
            lines.append(line.rstrip())
            m = re.search(r"https://login\.tailscale\.com/\S+", line)
            if m:
                found["url"] = m.group(0)
    _th.Thread(target=reader, daemon=True).start()
    deadline = _t.time() + timeout
    while _t.time() < deadline and "url" not in found and proc.poll() is None:
        _t.sleep(0.2)
    out = "\n".join(lines)
    if proc.poll() not in (None, 0) and ("access denied" in out.lower() or "permission" in out.lower()
                                         or "operator" in out.lower()):
        raise PermissionError(out)
    return found.get("url"), out


def open_url(url):
    for cmd in (["xdg-open", url], ["kde-open", url], ["gio", "open", url]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                return True
            except OSError:
                continue
    return False
