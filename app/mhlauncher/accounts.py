"""Account creation / sign-in against a running MHServerEmu."""
import json
import re
import urllib.request

RESULT_TEXT = {
    0: "Account created.",
    1: "The server could not create the account.",
    2: "Database error on the server.",
    3: "That email address is not valid.",
    4: "That email is already used by another account.",
    5: "No account with that email.",
    6: "Hero names may only contain up to 16 letters and numbers.",
    7: "That player name is already taken.",
    8: "Password must be 3 to 64 characters long.",
}

NAME_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")


def validate(email, player, password, confirm=None):
    email = email.strip()
    at, dot = email.find("@"), email.rfind(".")
    if at < 1 or dot < at or len(email) - dot - 1 < 2 or " " in email:
        return "Enter a valid email (it does not need to be real, e.g. me@deck.local)."
    if player is not None and not NAME_RE.match(player):
        return "Player name: 1–16 letters or numbers, no spaces."
    if not 3 <= len(password) <= 64:
        return "Password must be 3 to 64 characters long."
    if " " in password:
        return "Password cannot contain spaces."
    if confirm is not None and confirm != password:
        return "Passwords do not match."
    return None


def create_account(server, email, player, password):
    """Returns (ok, message)."""
    host, port = server.web_endpoint()
    body = json.dumps({"Email": email.strip().lower(), "PlayerName": player,
                       "Password": password}).encode()
    req = urllib.request.Request(f"http://{host}:{port}/AccountManagement/Create", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode() or "{}").get("Result", 1)
    except Exception:
        # web API unavailable -> fall back to the server console command
        m = server.command(f"!account create {email.strip().lower()} {player} {password}",
                           r"(Successfully created account|Failed to create account)[^\n]*", timeout=8)
        if m and m.group(1).startswith("Successfully"):
            return True, RESULT_TEXT[0]
        return False, (m.group(0) if m else "Server did not answer. Is it online?")
    return result == 0, RESULT_TEXT.get(result, f"Unknown error (code {result}).")


def verify_login(server, email, password):
    """Check credentials through the server console. Returns (ok_or_None, message).

    None means the check could not be performed (server offline / no answer).
    """
    if not server.running:
        return None, "Server is offline – credentials will be checked by the game."
    m = server.command(f"!account verify {email.strip().lower()} {password}",
                       r"Account credentials are (NOT )?valid(?:: (\w+))?", timeout=8)
    if not m:
        return None, "Server did not answer the login check."
    if m.group(1):
        reason = m.group(2) or ""
        friendly = {"EmailNotVerified": "account not verified", "AccountBanned": "account banned",
                    "IncorrectUsernameOrPassword401": "wrong email or password",
                    "IncorrectUsernameOrPassword403": "wrong email or password"}.get(reason, reason)
        return False, f"Login failed: {friendly or 'wrong email or password'}."
    return True, "Signed in."


def make_admin(server, email):
    m = server.command(f"!account userlevel {email.strip().lower()} 2",
                       r"(Successfully set user level|Failed to set user level|Failed to parse user level)",
                       timeout=6)
    return bool(m and m.group(1).startswith("Successfully"))
