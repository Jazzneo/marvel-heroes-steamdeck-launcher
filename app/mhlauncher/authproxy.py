"""Built-in replacement for the Apache setup that MHServerEmu uses on Windows.

* HTTP  (port 80, fallback 8088): serves SiteConfig.xml to the game client.
* HTTPS (port 443, fixed by the client): forwards /AuthServer/... to the
  MHServerEmu web frontend on 127.0.0.1:8080.

Ports below 1024 need a one-time system change on Linux, see tools/enable-login-ports.sh.
"""
import http.client
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

    def _handle(self):
        bridge = self.server.bridge
        path = self.path.split("?", 1)[0]
        if path.lower().endswith("/siteconfig.xml"):
            bridge.siteconfig_requests += 1
            return self._send(200, bridge.siteconfig, "text/xml")
        if path.lower().startswith("/authserver"):
            bridge.auth_requests += 1
            bridge.last_auth_time = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        headers["Host"] = f"{bridge.web_host}:{bridge.web_port}"
        try:
            # "localhost" may resolve to ::1 or 127.0.0.1; create_connection tries both
            conn = http.client.HTTPConnection(bridge.web_host, bridge.web_port, timeout=30)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
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

    def _log(self, line):
        self.log_lines.append(line)
        del self.log_lines[:-200]

    @property
    def running(self):
        return self.https is not None

    def siteconfig_url(self):
        """Value for the client's -siteconfigurl= option.

        The client expects 'host/SiteConfig.xml' (no 'http://', port 80), exactly like the
        official MHServerEmu Setup tool and Bifrost launcher use.
        """
        host = self.auth_address
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

    def start(self, lan=False):
        self.stop()
        self.error = ""
        bind = "0.0.0.0" if lan else "127.0.0.1"
        self.auth_address = lan_ip() if lan else "127.0.0.1"
        self.siteconfig = build_siteconfig(self.auth_address)

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
