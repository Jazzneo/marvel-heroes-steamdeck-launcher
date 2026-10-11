"""Deck-to-Deck direct play: one Deck makes its own Wi-Fi hotspot, the other joins it (no router, no internet).

True ad-hoc (IBSS) Wi-Fi isn't supported by the Steam Deck's Wi-Fi drivers, so this uses an access-point
hotspot from NetworkManager, the same thing the KDE network menu's 'Hotspot' does. Only `nmcli` is needed;
it's part of SteamOS, Bazzite and most desktop distros.

The host's Deck gets the address 10.42.0.1 on the hotspot (NetworkManager 'shared' mode); the joining Deck
uses that address as the friend's server. While a hotspot is on (or joined), the Wi-Fi isn't connected to
your normal network, so there is no internet; stopping/leaving reconnects to your usual Wi-Fi by itself.
"""
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import time

HOST_CON = "MHLauncher-Hotspot"
JOIN_CON = "MHLauncher-Join"
SSID_PREFIX = "MHL-"
DEFAULT_HOST_IP = "10.42.0.1"


class HotspotError(Exception):
    pass


def available():
    return bool(shutil.which("nmcli"))


def _nmcli(args, timeout=40, admin_fallback=True):
    """Run nmcli; returns stdout. On 'not authorized' retries once through the admin password prompt."""
    if not available():
        raise HotspotError("NetworkManager (nmcli) isn't available on this system.")
    try:
        r = subprocess.run(["nmcli"] + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HotspotError("NetworkManager didn't answer in time.")
    if r.returncode == 0:
        return r.stdout
    err = (r.stderr or r.stdout or "").strip()
    if admin_fallback and re.search(r"(?i)not authori[sz]ed|insufficient privileges|permission denied", err):
        from . import integration
        ok, out = integration.run_as_admin("nmcli " + " ".join(shlex.quote(a) for a in args), timeout=timeout + 120)
        if ok:
            return out
        err = out
    raise HotspotError(_friendly(err))


def _friendly(err):
    e = err.lower()
    if "secrets were required" in e or "802-11-wireless-security.psk" in e or "invalid passphrase" in e:
        return "Wrong Wi-Fi password for that Deck's hotspot."
    if "no network with ssid" in e:
        return "That hotspot isn't in range any more. Move the Decks closer and search again."
    if "does not support ap mode" in e or "ap mode is not supported" in e:
        return "This Wi-Fi card can't make a hotspot. Let the other Deck host instead."
    return err.splitlines()[-1][-300:] if err else "NetworkManager reported an error."


def _split(line):
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def wifi_device():
    """Name of the first Wi-Fi interface (e.g. 'wlan0') or None."""
    try:
        out = _nmcli(["-t", "-f", "DEVICE,TYPE,STATE", "device"], timeout=8, admin_fallback=False)
    except HotspotError:
        return None
    for line in out.splitlines():
        p = _split(line)
        if len(p) >= 3 and p[1] == "wifi" and p[2] != "unavailable":
            return p[0]
    return None


def active_connections():
    """{connection name: device} of active connections."""
    try:
        out = _nmcli(["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"], timeout=8, admin_fallback=False)
    except HotspotError:
        return {}
    res = {}
    for line in out.splitlines():
        p = _split(line)
        if len(p) >= 2:
            res[p[0]] = p[1]
    return res


def _dev_value(dev, field):
    try:
        out = _nmcli(["-g", field, "device", "show", dev], timeout=8, admin_fallback=False)
    except HotspotError:
        return ""
    return out.strip().split("|")[0].strip()


def device_ip(dev):
    """IPv4 address of a device straight from the kernel (no programs started)."""
    from .network import interfaces
    for name, ip in interfaces():
        if name == dev:
            return ip
    return None


# ------------------------------------------------------------------ hosting
def make_password():
    """8 digits: WPA2 needs at least 8 characters, and digits are quickest to type with a controller."""
    return "".join(random.choice("23456789") for _ in range(8))


def make_ssid(name):
    base = re.sub(r"[^A-Za-z0-9_-]", "", name or "") or socket.gethostname().split(".")[0] or "Deck"
    return (SSID_PREFIX + base)[:32]


def start_host(ssid, password):
    """Turn this machine's Wi-Fi into a hotspot. Returns (device, ip)."""
    dev = wifi_device()
    if not dev:
        raise HotspotError("No Wi-Fi found. Deck-to-Deck needs Wi-Fi turned on (not Airplane mode).")
    if HOST_CON in _all_cons():
        _nmcli(["connection", "delete", HOST_CON], timeout=10, admin_fallback=False)
    base = ["device", "wifi", "hotspot", "ifname", dev, "con-name", HOST_CON, "ssid", ssid, "password", password]
    try:
        _nmcli(base + ["band", "bg"])        # 2.4 GHz: works on every Wi-Fi card and has the best range
    except HotspotError as e:
        if "band" not in str(e).lower() and "channel" not in str(e).lower():
            raise
        _nmcli(base)
    try:   # never come back by itself after a reboot
        _nmcli(["connection", "modify", HOST_CON, "connection.autoconnect", "no"], timeout=10, admin_fallback=False)
    except HotspotError:
        pass
    ip = None
    for _ in range(20):
        ip = device_ip(dev)
        if ip:
            break
        time.sleep(0.5)
    return dev, ip or DEFAULT_HOST_IP


def stop_host():
    """Turn the hotspot off; NetworkManager reconnects to your usual Wi-Fi by itself."""
    if HOST_CON in _all_cons():
        try:
            _nmcli(["connection", "delete", HOST_CON], timeout=15)
        except HotspotError:
            _nmcli(["connection", "down", HOST_CON], timeout=15)


def host_status():
    """(device, ip) when our hotspot is on, else None."""
    dev = active_connections().get(HOST_CON)
    if not dev:
        return None
    return dev, device_ip(dev) or DEFAULT_HOST_IP


# ------------------------------------------------------------------ joining
def scan(rescan=True):
    """Nearby launcher hotspots: [(ssid, signal_percent)] strongest first."""
    args = ["-t", "-f", "SSID,SIGNAL", "device", "wifi", "list"]
    if rescan:
        args += ["--rescan", "yes"]
    try:
        out = _nmcli(args, timeout=30, admin_fallback=False)
    except HotspotError:
        if not rescan:
            raise
        out = _nmcli(args[:-2], timeout=15, admin_fallback=False)   # scanning refused right now: use the last list
    best = {}
    for line in out.splitlines():
        p = _split(line)
        if len(p) >= 2 and p[0].startswith(SSID_PREFIX):
            try:
                sig = int(p[1])
            except ValueError:
                sig = 0
            best[p[0]] = max(sig, best.get(p[0], 0))
    return sorted(best.items(), key=lambda kv: -kv[1])


def join(ssid, password):
    """Connect to another Deck's hotspot. Returns the host's address (its server)."""
    dev = wifi_device()
    if not dev:
        raise HotspotError("No Wi-Fi found. Turn Wi-Fi on (not Airplane mode).")
    if JOIN_CON in _all_cons():
        _nmcli(["connection", "delete", JOIN_CON], timeout=10, admin_fallback=False)
    try:
        _nmcli(["device", "wifi", "connect", ssid, "password", password, "ifname", dev, "name", JOIN_CON],
               timeout=60)
    except HotspotError:
        if JOIN_CON in _all_cons():     # don't keep a profile with the wrong password
            try:
                _nmcli(["connection", "delete", JOIN_CON], timeout=10, admin_fallback=False)
            except HotspotError:
                pass
        raise
    try:   # don't jump onto this hotspot by itself later; your usual Wi-Fi stays the normal one
        _nmcli(["connection", "modify", JOIN_CON, "connection.autoconnect", "no"], timeout=10, admin_fallback=False)
    except HotspotError:
        pass
    gw = ""
    for _ in range(20):
        gw = _dev_value(dev, "IP4.GATEWAY")
        if gw:
            break
        time.sleep(0.5)
    return gw or DEFAULT_HOST_IP


def leave():
    if JOIN_CON in _all_cons():
        try:
            _nmcli(["connection", "delete", JOIN_CON], timeout=15)
        except HotspotError:
            _nmcli(["connection", "down", JOIN_CON], timeout=15)


def joined():
    """Name of the joined hotspot (SSID) or None."""
    dev = active_connections().get(JOIN_CON)
    if not dev:
        return None
    try:
        out = _nmcli(["-g", "802-11-wireless.ssid", "connection", "show", JOIN_CON], timeout=8, admin_fallback=False)
        return out.strip() or "hotspot"
    except HotspotError:
        return "hotspot"


def _all_cons():
    try:
        out = _nmcli(["-t", "-f", "NAME", "connection", "show"], timeout=8, admin_fallback=False)
    except HotspotError:
        return set()
    return {_split(l)[0] for l in out.splitlines() if l}


def cleanup_leftovers():
    """After a crash: remove a hotspot / joined connection the launcher left behind (restores normal Wi-Fi)."""
    if not available():
        return []
    left = [c for c in (HOST_CON, JOIN_CON) if c in _all_cons()]
    for c in left:
        try:
            _nmcli(["connection", "delete", c], timeout=15)
        except HotspotError:
            pass
    return left
