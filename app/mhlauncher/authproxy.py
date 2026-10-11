"""Built-in replacement for the Apache setup that MHServerEmu uses on Windows.

* HTTP  (port 80, fallback 8088): serves SiteConfig.xml to the game client.
* HTTPS (port 443, fixed by the client): forwards /AuthServer/... to the
  MHServerEmu web frontend on 127.0.0.1:8080.

Ports below 1024 need a one-time system change on Linux, see tools/enable-login-ports.sh.
"""
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import ASSETS_DIR, DATA_DIR

HTTPS_PORT = 443          # the client always uses 443 for auth in shipping builds
HTTP_PORT = 80
HTTP_FALLBACK_PORT = 8088

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def unprivileged_port_start():
    try:
        with open("/proc/sys/net/ipv4/ip_unprivileged_port_start") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 1024


def can_bind(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_in_use(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def describe_owner(port):
    """Return (pid, short_name, cmdline) of whoever listens on port, or None if free/unknown."""
    from .server import port_owner
    res = port_owner(port)
    if not res:
        return None
    pid, cmd = res
    if not pid:
        return (None, "a program of another user (maybe a system web server)", "")
    if "mhlauncher" in cmd:
        name = "this MH Launcher" if pid == os.getpid() else "another copy of MH Launcher"
    else:
        name = os.path.basename(cmd.split(" ")[0]) if cmd else f"pid {pid}"
    return (pid, name, cmd)


def ports_allowed():
    """True if this user may open port 443 (and 80)."""
    return os.geteuid() == 0 or unprivileged_port_start() <= HTTP_PORT


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert():
    """Return (certfile, keyfile). Uses the bundled self-signed localhost certificate."""
    cert = os.path.join(ASSETS_DIR, "server.crt")
    key = os.path.join(ASSETS_DIR, "server.key")
    return cert, key


def build_siteconfig(auth_address):
    with open(os.path.join(ASSETS_DIR, "SiteConfig.xml"), "r", encoding="utf-8") as f:
        xml = f.read()
    xml = re.sub(r'(<str name="AuthServerAddress" value=")[^"]*(")', rf"\g<1>{auth_address}\g<2>", xml)
    xml = re.sub(r'(<str name="AuthServerPort" value=")[^"]*(")', r"\g<1>443\g<2>", xml)
    return xml.encode("utf-8")


# ------------------------------------------------------------------ per-client addresses
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
# What a friend (a non-local client) may reach through the bridge. Everything else (dashboard, admin web API,
# metrics) stays local-only.
FRIEND_PATHS = (("GET", "/siteconfig.xml"), ("HEAD", "/siteconfig.xml"), ("POST", "/authserver/"),
                ("GET", "/serverstatus"))
FRIEND_REGISTER = ("POST", "/accountmanagement/create")


def _read_varint(data, i):
    shift = result = 0
    while True:
        if i >= len(data) or shift > 63:
            raise ValueError("bad varint")
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def rewrite_ticket(payload, new_host):
    """Set AuthTicket.frontendServer (field 5) in an MHServerEmu auth response to new_host.

    The response is <varint message id><varint length><AuthTicket protobuf>. The game client connects to
    frontendServer next, so each client gets the address it reached us through (localhost for the host's
    own game, the LAN / Tailscale / public address for a friend). Anything unexpected is passed through."""
    try:
        mid, i = _read_varint(payload, 0)
        ln, j = _read_varint(payload, i)
        msg = payload[j:j + ln]
        if j + ln != len(payload):
            return payload
        out, k, found = bytearray(), 0, False
        while k < len(msg):
            start = k
            key, k = _read_varint(msg, k)
            fn, wt = key >> 3, key & 7
            if wt == 0:
                _, k = _read_varint(msg, k)
            elif wt == 1:
                k += 8
            elif wt == 5:
                k += 4
            elif wt == 2:
                n, k2 = _read_varint(msg, k)
                k = k2 + n
                if fn == 5:
                    host = new_host.encode("ascii")
                    out += _varint(key) + _varint(len(host)) + host
                    found = True
                    continue
            else:
                return payload
            if k > len(msg):
                return payload
            out += msg[start:k]
        if not found:
            return payload
        return _varint(mid) + _varint(len(out)) + bytes(out)
    except (ValueError, UnicodeEncodeError):
        return payload


def login_email(body):
    """Email address from an AuthServer login request (LoginDataPB field 1), or None."""
    try:
        i = 0
        _mid, i = _read_varint(body, i)
        n, i = _read_varint(body, i)
        msg = body[i:i + n]
        k = 0
        while k < len(msg):
            key, k = _read_varint(msg, k)
            fn, wt = key >> 3, key & 7
            if wt == 2:
                ln, k = _read_varint(msg, k)
                if fn == 1:
                    return msg[k:k + ln].decode("utf-8").strip().lower()
                k += ln
            elif wt == 0:
                _, k = _read_varint(msg, k)
            elif wt == 1:
                k += 8
            elif wt == 5:
                k += 4
            else:
                return None
    except (ValueError, IndexError, UnicodeDecodeError):
        pass
    return None


def is_local_client(ip):
    return ip.startswith("127.") or ip in ("::1", "localhost") or ip.startswith("::ffff:127.")


class _Handler(BaseHTTPRequestHandler):
    server_version = "MHLauncher"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route to bridge log instead of stderr
        self.server.bridge._log(f"{self.client_address[0]} {fmt % args}")

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _request_host(self):
        """The address this client used to reach us (Host header), which is reachable for it."""
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):                      # [ipv6]:port
            host = host[1:host.find("]")] if "]" in host else ""
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        if not host or not HOST_RE.match(host):
            try:
                host = self.connection.getsockname()[0]
            except OSError:
                host = self.server.bridge.auth_address
        return host

    def _handle(self):
        bridge = self.server.bridge
        path = self.path.split("?", 1)[0]
        low = path.lower()
        ip = self.client_address[0]
        local = is_local_client(ip) or ip in bridge.local_ips
        if low.startswith("/mhl/save/"):
            return self._save_api(low[len("/mhl/save/"):], local)
        if not local:
            allowed = any(self.command == m and (low == p or (p.endswith("/") and low.startswith(p)))
                          for m, p in FRIEND_PATHS)
            if not allowed and bridge.allow_register and (self.command, low) == FRIEND_REGISTER:
                allowed = True
            if not allowed:
                return self._send(403, b"Not available to remote players")
            bridge.friends_seen[ip] = time.time()
        if low.endswith("/siteconfig.xml"):
            bridge.siteconfig_requests += 1
            return self._send(200, bridge.siteconfig_for(self._request_host()), "text/xml")
        is_auth = low.startswith("/authserver")
        if is_auth:
            bridge.auth_requests += 1
            bridge.last_auth_time = time.time()
            if not local:
                bridge.friend_logins += 1
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        if is_auth and body:
            email = login_email(body)
            if email:
                bridge.logins[email] = time.time()
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        headers["Host"] = f"{bridge.web_host}:{bridge.web_port}"
        try:
            # "localhost" may resolve to ::1 or 127.0.0.1; create_connection tries both
            conn = http.client.HTTPConnection(bridge.web_host, bridge.web_port, timeout=30)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if is_auth and resp.status == 200 and data:
                data = rewrite_ticket(data, self._request_host())
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            conn.close()
        except OSError as e:
            bridge._log(f"forward failed: {e}")
            self._send(502, b"MHServerEmu is not running")

    def _save_api(self, action, local):
        """Hero transfer between launchers (see savetransfer.py). JSON in, JSON out; needs the account's password."""
        bridge = self.server.bridge
        ip = self.client_address[0]

        def reply(code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")
        if self.command != "POST" or bridge.save_api is None:
            return reply(404, {"error": "Not available"})
        if not local and not bridge.hosting:
            return reply(403, {"error": "Your friend isn't hosting right now."})
        fails = [t for t in bridge.save_fails.get(ip, []) if time.time() - t < 300]
        if len(fails) >= 5:
            return reply(429, {"error": "Too many wrong passwords. Wait 5 minutes."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 256 * 1024 * 1024:
            return reply(413, {"error": "Save is too big"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return reply(400, {"error": "Bad request"})
        if not local:
            bridge.friends_seen[ip] = time.time()
        try:
            code, obj = bridge.save_api(action, data)
        except Exception as e:
            bridge._log(f"save api {action} failed: {e}")
            code, obj = 500, {"error": str(e)}
        if code == 401:
            bridge.save_fails[ip] = fails + [time.time()]
        bridge._log(f"{ip} save {action} -> {code}")
        reply(code, obj)

    do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = _handle


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LoginBridge:
    def __init__(self, web_host="localhost", web_port=8080):
        # .NET HttpListener matches requests by Host header, so forward with the
        # exact host name from the server's [WebFrontend] Address setting.
        self.web_host = web_host
        self.web_port = web_port
        self.https = None
        self.http = None
        self.http_port = None
        self.error = ""
        self.auth_requests = 0
        self.siteconfig_requests = 0
        self.last_auth_time = 0
        self.siteconfig = b""
        self.auth_address = "127.0.0.1"
        self.log_lines = []
        self.hosting = False
        self.allow_register = True        # friends may create an account on this server
        self.friends_seen = {}            # friend ip -> last request time
        self.local_ips = set()            # this machine's own addresses (not friends)
        self.friend_logins = 0
        self._siteconfigs = {}
        self.logins = {}                  # email -> time of the last game login through this bridge
        self.save_api = None              # set by the app: (action, data) -> (http code, dict)
        self.save_fails = {}

    def siteconfig_for(self, host):
        """SiteConfig.xml whose AuthServerAddress is the address this client used to reach us."""
        if not HOST_RE.match(host or ""):
            host = self.auth_address
        if host not in self._siteconfigs:
            if len(self._siteconfigs) > 32:
                self._siteconfigs.clear()
            self._siteconfigs[host] = build_siteconfig(host)
        return self._siteconfigs[host]

    def friends_online(self, window=120):
        now = time.time()
        return [ip for ip, t in self.friends_seen.items() if now - t < window]

    def _log(self, line):
        self.log_lines.append(line)
        del self.log_lines[:-200]

    @property
    def running(self):
        return self.https is not None

    def siteconfig_url(self):
        """Value for the client's -siteconfigurl= option (this machine's own game).

        The client expects 'host/SiteConfig.xml' (no 'http://', port 80), exactly like the
        official MHServerEmu Setup tool and Bifrost launcher use.
        """
        host = "127.0.0.1"
        if self.http_port in (None, HTTP_PORT):
            return f"{host}/SiteConfig.xml"
        return f"{host}:{self.http_port}/SiteConfig.xml"

    def self_test(self):
        """Check that the game will be able to reach SiteConfig.xml (port 80) and the
        auth server (port 443). Returns a list of problems (empty = OK)."""
        import urllib.request
        problems = []
        if self.http_port != HTTP_PORT:
            own = describe_owner(HTTP_PORT)
            if own:
                problems.append(f"Port 80 is used by {own[1]}" + (f" (pid {own[0]})" if own[0] else "") +
                                ", so the game cannot download SiteConfig.xml.")
            elif not ports_allowed():
                problems.append("Port 80 is blocked: the one-time port setup has not been done.")
            else:
                problems.append("Port 80 could not be opened, so the game cannot download SiteConfig.xml.")
        else:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/SiteConfig.xml", timeout=5) as r:
                    if b"AuthServerAddress" not in r.read():
                        problems.append("SiteConfig.xml answered with unexpected content.")
            except Exception as e:
                problems.append(f"SiteConfig.xml is not reachable on port 80 ({e}).")
        if not self.https:
            problems.append(self.error or "The HTTPS login bridge on port 443 is not running.")
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                urllib.request.urlopen(urllib.request.Request(f"https://127.0.0.1:{HTTPS_PORT}/AuthServer/"),
                                       context=ctx, timeout=5).close()
            except urllib.error.HTTPError:
                pass  # any HTTP answer means TLS + forwarding work
            except Exception as e:
                problems.append(f"The login bridge on port 443 does not answer ({e}).")
        return problems

    def start(self, lan=False, allow_register=True):
        """lan=True: host mode, reachable from other devices (friends)."""
        self.stop()
        self.error = ""
        self.hosting = bool(lan)
        self.allow_register = allow_register
        bind = "0.0.0.0" if lan else "127.0.0.1"
        self.auth_address = lan_ip() if lan else "127.0.0.1"
        self.siteconfig = build_siteconfig(self.auth_address)
        self._siteconfigs = {}
        try:
            from .network import interfaces
            self.local_ips = {ip for _n, ip in interfaces()}
        except Exception:
            self.local_ips = set()

        # HTTP for SiteConfig.xml
        for port in (HTTP_PORT, HTTP_FALLBACK_PORT):
            try:
                self.http = _Server((bind, port), _Handler)
                self.http.bridge = self
                self.http_port = port
                break
            except OSError:
                self.http = None
        if self.http:
            threading.Thread(target=self.http.serve_forever, daemon=True, name="siteconfig").start()

        # HTTPS for /AuthServer
        try:
            cert, key = ensure_cert()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:  # the 2016 client may use old TLS settings
                ctx.minimum_version = ssl.TLSVersion.TLSv1
                ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            except (ValueError, ssl.SSLError):
                pass
            ctx.load_cert_chain(cert, key)
            srv = _Server((bind, HTTPS_PORT), _Handler)
            # handshake happens lazily in the per-request thread
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
            srv.bridge = self
            self.https = srv
            threading.Thread(target=srv.serve_forever, daemon=True, name="authserver").start()
        except PermissionError:
            self.error = "Port 443 is blocked. Run the one-time 'Enable login ports' fix."
        except OSError as e:
            own = describe_owner(HTTPS_PORT)
            if own:
                self.error = (f"Port 443 is used by {own[1]}" + (f" (pid {own[0]})" if own[0] else "") + ".")
            else:
                self.error = f"Could not open port 443: {e}"
        if self.error:
            self._log(self.error)
        return self.running

    def stop(self):
        for srv in (self.https, self.http):
            if srv:
                try:
                    srv.shutdown()
                    srv.server_close()
                except Exception:
                    pass
        self.https = None
        self.http = None
