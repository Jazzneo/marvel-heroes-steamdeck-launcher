"""Move a player's heroes between two MHServerEmu servers (your solo server <-> a friend's server).

MHServerEmu keeps everything on the server in Data/Account.db (SQLite):
  Account(Id, Email, PlayerName, PasswordHash, Salt, ...)
  Player(DbGuid = Account.Id, ArchiveData, StartTarget, AOIVolume, GazillioniteBalance, LastLogoutTime, Flags)
  Avatar / TeamUp (ContainerDbGuid = player)           -> heroes and team-ups
  Item (ContainerDbGuid = player, avatar or team-up)   -> stash, inventory, equipment
  ControlledEntity (ContainerDbGuid = avatar)          -> e.g. controlled pets

A "save" here is one account's Player row plus all of its entities: every hero, item and the stash. Copying it
replaces the save of the target account (login, name and password stay). The server loads player data from the
database at every login and writes it at logout, so a save can be swapped while the server runs as long as that
player isn't in the game. A backup of the target database is always made first.
"""
import base64
import glob
import hashlib
import hmac
import os
import random
import shutil
import sqlite3
import time

FORMAT = "mhl-save-1"
ENTITY_TABLES = ("Avatar", "TeamUp", "Item", "ControlledEntity")
ENTITY_COLS = ("DbGuid", "ContainerDbGuid", "InventoryProtoGuid", "Slot", "EntityProtoGuid", "ArchiveData")
PLAYER_COLS = ("ArchiveData", "StartTarget", "AOIVolume", "GazillioniteBalance", "LastLogoutTime", "Flags")
KEEP_BACKUPS = 10

# PBKDF2-HMAC-SHA512, 210000 iterations, 64-byte key (MHServerEmu.Core CryptographyHelper)
PW_ITERATIONS = 210000
PW_KEYSIZE = 64


class SaveError(Exception):
    pass


# ------------------------------------------------------------------ database helpers
def db_path(server_dir, filename="Account.db"):
    return os.path.join(server_dir, "Data", filename)


def _connect(path, must_exist=True):
    if must_exist and not os.path.isfile(path):
        raise SaveError("No save database yet – start this server and log in once first.")
    conn = sqlite3.connect(path, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def schema_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def find_account(conn, email):
    row = conn.execute("SELECT Id, Email, PlayerName, PasswordHash, Salt FROM Account WHERE Email = ? "
                       "COLLATE NOCASE", (email.strip(),)).fetchone()
    return row


def verify_password(password, pw_hash, salt):
    calc = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), bytes(salt), PW_ITERATIONS, PW_KEYSIZE)
    return hmac.compare_digest(calc, bytes(pw_hash))


def check_login(conn, email, password):
    """Account row when email + password match, else raises SaveError."""
    acc = find_account(conn, email or "")
    if acc is None:
        raise SaveError(f"There's no account {email} on this server. Create it first.")
    if not verify_password(password or "", acc["PasswordHash"], acc["Salt"]):
        raise SaveError("Wrong email or password for this server.")
    return acc


def last_logout(conn, account_id):
    """LastLogoutTime in seconds (0 if never)."""
    row = conn.execute("SELECT LastLogoutTime FROM Player WHERE DbGuid = ?", (account_id,)).fetchone()
    return (row[0] or 0) / 1000.0 if row else 0.0


def _containers(conn, account_id):
    avatars = [r[0] for r in conn.execute("SELECT DbGuid FROM Avatar WHERE ContainerDbGuid = ?", (account_id,))]
    teamups = [r[0] for r in conn.execute("SELECT DbGuid FROM TeamUp WHERE ContainerDbGuid = ?", (account_id,))]
    return avatars, teamups


def _in(ids):
    return "(" + ",".join("?" * len(ids)) + ")"


def summary(conn, account_id):
    """{'heroes': n, 'teamups': n, 'items': n, 'played': bool, 'last_logout': seconds}"""
    avatars, teamups = _containers(conn, account_id)
    holders = [account_id] + avatars + teamups
    items = conn.execute(f"SELECT COUNT(*) FROM Item WHERE ContainerDbGuid IN {_in(holders)}", holders).fetchone()[0]
    played = conn.execute("SELECT 1 FROM Player WHERE DbGuid = ?", (account_id,)).fetchone() is not None
    return {"heroes": len(avatars), "teamups": len(teamups), "items": items, "played": played,
            "last_logout": last_logout(conn, account_id)}


def describe(s):
    if not s or not s.get("played"):
        return "no heroes yet"
    h = s["heroes"]
    return f"{h} hero{'es' if h != 1 else ''}, {s['items']} item{'s' if s['items'] != 1 else ''}"


# ------------------------------------------------------------------ export / import
def _b64(v):
    return base64.b64encode(v).decode("ascii") if isinstance(v, (bytes, bytearray, memoryview)) else v


def _unb64(v):
    return base64.b64decode(v) if isinstance(v, str) else v


def _row(r, cols):
    return {c: _b64(r[c]) for c in cols}


def export_save(conn, account):
    """The whole save of an account as a JSON-able dict."""
    aid = account["Id"]
    player = conn.execute("SELECT * FROM Player WHERE DbGuid = ?", (aid,)).fetchone()
    if player is None:
        raise SaveError(f"{account['PlayerName']} has no heroes yet on that server (never played there).")
    avatars, teamups = _containers(conn, aid)
    holders = [aid] + avatars + teamups
    ents = {}
    q = f"SELECT {', '.join(ENTITY_COLS)} FROM {{t}} WHERE ContainerDbGuid IN {{ids}}"
    ents["Avatar"] = [_row(r, ENTITY_COLS) for r in conn.execute(q.format(t="Avatar", ids="(?)"), (aid,))]
    ents["TeamUp"] = [_row(r, ENTITY_COLS) for r in conn.execute(q.format(t="TeamUp", ids="(?)"), (aid,))]
    ents["Item"] = [_row(r, ENTITY_COLS) for r in conn.execute(q.format(t="Item", ids=_in(holders)), holders)]
    ents["ControlledEntity"] = ([_row(r, ENTITY_COLS) for r in conn.execute(
        q.format(t="ControlledEntity", ids=_in(avatars)), avatars)] if avatars else [])
    return {
        "format": FORMAT,
        "schema": schema_version(conn),
        "exported_at": time.time(),
        "player_name": account["PlayerName"],
        "account_id": aid,
        "player": _row(player, PLAYER_COLS),
        "entities": ents,
    }


def payload_summary(payload):
    e = payload.get("entities", {})
    return {"heroes": len(e.get("Avatar", [])), "teamups": len(e.get("TeamUp", [])),
            "items": len(e.get("Item", [])), "played": True}


def _new_entity_id(seq=[0]):
    """Fresh persistent-entity id in MHServerEmu's snowflake format (type 5), with machine id 0xFFF so it can't
    clash with ids the server generates itself (machine id 0)."""
    seq[0] = (seq[0] + 1) & 0xFFFF
    return (5 << 60) | (0xFFF << 48) | ((int(time.time()) & 0xFFFFFFFF) << 16) | ((seq[0] + random.randint(0, 0x7FFF))
                                                                                & 0xFFFF)


def check_payload(conn, payload):
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or "entities" not in payload:
        raise SaveError("That isn't a save from MH Launcher.")
    if payload.get("schema") != schema_version(conn):
        raise SaveError("The two servers are different MHServerEmu versions (save format "
                        f"{payload.get('schema')} vs {schema_version(conn)}). Update both launchers/servers to the "
                        "same version first.")


def import_save(conn, account, payload):
    """Replace the save of `account` with `payload`. Runs in one transaction. Returns the new summary."""
    check_payload(conn, payload)
    aid = account["Id"]
    old_pid = payload["account_id"]
    ents = payload["entities"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. remove the target's current save
        avatars, teamups = _containers(conn, aid)
        holders = [aid] + avatars + teamups
        conn.execute(f"DELETE FROM Item WHERE ContainerDbGuid IN {_in(holders)}", holders)
        if avatars:
            conn.execute(f"DELETE FROM ControlledEntity WHERE ContainerDbGuid IN {_in(avatars)}", avatars)
        conn.execute("DELETE FROM Avatar WHERE ContainerDbGuid = ?", (aid,))
        conn.execute("DELETE FROM TeamUp WHERE ContainerDbGuid = ?", (aid,))
        # 2. player row
        p = payload["player"]
        conn.execute("INSERT OR IGNORE INTO Player (DbGuid) VALUES (?)", (aid,))
        conn.execute("UPDATE Player SET ArchiveData=?, StartTarget=?, AOIVolume=?, GazillioniteBalance=?, "
                     "LastLogoutTime=?, Flags=? WHERE DbGuid=?",
                     (_unb64(p["ArchiveData"]), p["StartTarget"], p["AOIVolume"], p["GazillioniteBalance"],
                      p["LastLogoutTime"], p["Flags"] or 0, aid))
        # 3. entities: same ids unless an id is already used by someone else on this server
        def taken(i):
            return any(conn.execute(f"SELECT 1 FROM {t} WHERE DbGuid = ?", (i,)).fetchone() for t in ENTITY_TABLES)
        remap = {old_pid: aid}
        for t in ENTITY_TABLES:
            for r in ents.get(t, []):
                if taken(r["DbGuid"]) or r["DbGuid"] in remap:
                    new = _new_entity_id()
                    while taken(new):
                        new = _new_entity_id()
                    remap[r["DbGuid"]] = new
        for t in ("Avatar", "TeamUp", "ControlledEntity", "Item"):
            for r in ents.get(t, []):
                conn.execute(f"INSERT INTO {t} ({', '.join(ENTITY_COLS)}) VALUES (?, ?, ?, ?, ?, ?)",
                             (remap.get(r["DbGuid"], r["DbGuid"]), remap.get(r["ContainerDbGuid"], r["ContainerDbGuid"]),
                              r["InventoryProtoGuid"], r["Slot"], r["EntityProtoGuid"], _unb64(r["ArchiveData"])))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary(conn, aid)


# ------------------------------------------------------------------ backups
def backup(db_file, backup_dir, label):
    """Consistent copy of the database (works while the server has it open). Returns the backup path."""
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, f"Account-{time.strftime('%Y%m%d-%H%M%S')}-{label}.db")
    src = sqlite3.connect(db_file, timeout=20)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    old = sorted(glob.glob(os.path.join(backup_dir, "Account-*.db")))
    for f in old[:-KEEP_BACKUPS]:
        try:
            os.remove(f)
        except OSError:
            pass
    return dest


def restore(backup_file, db_file):
    """Put a backup back (server must be stopped)."""
    for ext in ("-wal", "-shm"):
        try:
            os.remove(db_file + ext)
        except OSError:
            pass
    shutil.copy2(backup_file, db_file)


# ------------------------------------------------------------------ host side (login bridge endpoint)
def make_host_api(get_db_file, bridge, backup_dir):
    """Handler for POST /mhl/save/<action> on the host's login bridge. Friends prove they own the account on this
    server with its email + password; they can only read or replace their own save."""
    def api(action, data):
        email, password = (data.get("email") or "").strip(), data.get("password") or ""
        db_file = get_db_file()
        if action not in ("info", "export", "import"):
            return 404, {"error": "Unknown action"}
        try:
            conn = _connect(db_file)
        except SaveError as e:
            return 409, {"error": str(e)}
        try:
            try:
                acc = check_login(conn, email, password)
            except SaveError as e:
                return 401, {"error": str(e)}
            if action == "info":
                return 200, {"player_name": acc["PlayerName"], "summary": summary(conn, acc["Id"]),
                             "schema": schema_version(conn)}
            seen = bridge.logins.get(email.lower(), 0)
            if seen and seen > last_logout(conn, acc["Id"]) + 1:
                return 409, {"error": f"{acc['PlayerName']} is still in the game on that server. Quit the game "
                                      "first (the server saves when you log out), then try again."}
            if action == "export":
                return 200, {"payload": export_save(conn, acc)}
            payload = data.get("payload") or {}
            check_payload(conn, payload)      # before making a backup
            bk = backup(db_file, backup_dir, "before-friend-import")
            new = import_save(conn, acc, payload)
            return 200, {"player_name": acc["PlayerName"], "summary": new, "backup": os.path.basename(bk)}
        except SaveError as e:
            return 409, {"error": str(e)}
        finally:
            conn.close()
    return api


# ------------------------------------------------------------------ friend side (talks to the host's bridge)
def remote(host, action, data, timeout=120, base="/mhl/save/", stream_to=None, progress=None):
    """POST to a friend's launcher. Uses the login bridge's HTTPS port (self-signed, so not verified: the
    password is still encrypted on the way). Returns the JSON reply or raises SaveError."""
    import json
    import ssl
    import urllib.error
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(f"https://{host.strip()}{base}{action}", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(req, timeout=timeout) as r:
            if stream_to is None:
                return json.loads(r.read() or b"{}")
            total, done = int(r.headers.get("Content-Length") or 0), 0
            with open(stream_to, "wb") as out:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done / total)
            return {"size": done}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = None
        if e.code == 404 and not msg:
            msg = ("Your friend's launcher is too old for this. They need MH Launcher " +
                   ("1.5.0" if "mods" in base else "1.4.0") + " or newer.")
        raise SaveError(msg or f"Your friend's launcher answered with error {e.code}.")
    except (urllib.error.URLError, OSError) as e:
        raise SaveError(f"Can't reach your friend's launcher ({getattr(e, 'reason', e)}). Is their server on?")


# ------------------------------------------------------------------ cash shop points (Gs)
def g_accounts(conn):
    """[{id, email, name, balance}] for every account on this server. balance: None = never logged in,
    -1 = still the starting amount for new accounts (given at the next login)."""
    out = []
    for r in conn.execute("SELECT a.Id, a.Email, a.PlayerName, p.GazillioniteBalance AS g, p.DbGuid AS pid "
                          "FROM Account a LEFT JOIN Player p ON p.DbGuid = a.Id ORDER BY a.PlayerName COLLATE NOCASE"):
        out.append({"id": r["Id"], "email": r["Email"], "name": r["PlayerName"],
                    "balance": None if r["pid"] is None else (r["g"] if r["g"] is not None else -1)})
    return out


def add_g(conn, account_id, amount, starting_balance):
    """Add (or with a negative amount, remove) Gs. The server reads the balance from here at every login.
    Returns the new balance."""
    row = conn.execute("SELECT GazillioniteBalance FROM Player WHERE DbGuid = ?", (account_id,)).fetchone()
    if row is None:
        raise SaveError("This account has never logged in, so it has no wallet yet. Log in with it once, quit, "
                        "and try again.")
    cur = row[0] if row[0] is not None and row[0] >= 0 else starting_balance
    new = max(0, min(int(cur) + int(amount), 2_000_000_000))
    conn.execute("UPDATE Player SET GazillioniteBalance = ? WHERE DbGuid = ?", (new, account_id))
    return new
