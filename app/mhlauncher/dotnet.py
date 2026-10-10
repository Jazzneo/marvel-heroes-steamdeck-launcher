"""Find or install the .NET runtime that MHServerEmu needs (8.0 or newer)."""
import glob
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

from .config import DATA_DIR

LOCAL_DOTNET = os.path.join(DATA_DIR, "dotnet")
DOWNLOAD_URL = "https://aka.ms/dotnet/8.0/dotnet-runtime-linux-x64.tar.gz"
MIN_MAJOR = 8


def _candidates():
    seen = []
    for path in (
        os.path.join(LOCAL_DOTNET, "dotnet"),
        os.environ.get("DOTNET_ROOT") and os.path.join(os.environ["DOTNET_ROOT"], "dotnet"),
        os.path.expanduser("~/.dotnet/dotnet"),
        shutil.which("dotnet"),
        "/usr/share/dotnet/dotnet",
        "/usr/lib/dotnet/dotnet",
        "/opt/dotnet/dotnet",
    ):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            real = os.path.realpath(path)
            if real not in seen:
                seen.append(real)
                yield path


def runtime_versions(dotnet_exe):
    """Versions of Microsoft.NETCore.App installed next to a dotnet executable."""
    root = os.path.dirname(os.path.realpath(dotnet_exe))
    versions = []
    for d in glob.glob(os.path.join(root, "shared", "Microsoft.NETCore.App", "*")):
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", os.path.basename(d))
        if m:
            versions.append(tuple(int(x) for x in m.groups()))
    if not versions:  # fall back to asking dotnet itself
        try:
            out = subprocess.run([dotnet_exe, "--list-runtimes"], capture_output=True,
                                 text=True, timeout=15).stdout
            for m in re.finditer(r"Microsoft\.NETCore\.App (\d+)\.(\d+)\.(\d+)", out):
                versions.append(tuple(int(x) for x in m.groups()))
        except Exception:
            pass
    return sorted(versions)


def find_dotnet():
    """Return (path_to_dotnet, version_tuple) for a usable runtime, or (None, None)."""
    for exe in _candidates():
        vers = [v for v in runtime_versions(exe) if v[0] >= MIN_MAJOR]
        if vers:
            # prefer an exact 8.x match, otherwise newest (roll-forward is enabled)
            eight = [v for v in vers if v[0] == MIN_MAJOR]
            return exe, (eight or vers)[-1]
    return None, None


def has_icu():
    pats = ["/usr/lib/libicuuc.so*", "/usr/lib64/libicuuc.so*",
            "/usr/lib/x86_64-linux-gnu/libicuuc.so*", "/lib/x86_64-linux-gnu/libicuuc.so*"]
    return any(glob.glob(p) for p in pats)


def server_env(dotnet_exe):
    env = dict(os.environ)
    env["DOTNET_ROOT"] = os.path.dirname(os.path.realpath(dotnet_exe))
    env["DOTNET_ROLL_FORWARD"] = "LatestMajor"
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["DOTNET_NOLOGO"] = "1"
    if not has_icu():
        env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
    # Steam injects these into games; they do not belong in the server process
    env.pop("LD_PRELOAD", None)
    return env


def install(progress=None):
    """Download the latest .NET 8 runtime into the launcher data folder (no root needed).

    progress(fraction_or_None, text) is called from this (worker) thread.
    """
    def report(frac, text):
        if progress:
            progress(frac, text)

    os.makedirs(DATA_DIR, exist_ok=True)
    report(None, "Connecting to Microsoft download server…")
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", dir=DATA_DIR)
    os.close(tmp_fd)
    try:
        req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "MHLauncher"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_path, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    report(done / total * 0.9, f"Downloading .NET 8… {done // 1048576} / {total // 1048576} MB")
        report(0.92, "Extracting…")
        staging = LOCAL_DOTNET + ".new"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        with tarfile.open(tmp_path, "r:gz") as tar:
            try:
                tar.extractall(staging, filter="data")
            except TypeError:  # Python < 3.12
                tar.extractall(staging)
        shutil.rmtree(LOCAL_DOTNET, ignore_errors=True)
        os.replace(staging, LOCAL_DOTNET)
        exe = os.path.join(LOCAL_DOTNET, "dotnet")
        os.chmod(exe, 0o755)
        report(1.0, ".NET runtime installed")
        return exe
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
