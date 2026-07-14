"""Sign in to SnapTrade with OAuth, so nobody ever pastes an API key.

SnapTrade Personal OAuth: an authorization-code flow with PKCE, as a public native client.
Snappy registers itself once, opens the consent page in your browser, catches the redirect
on a loopback port, and swaps the code for tokens. The tokens live in the macOS Keychain —
not in .env, not on disk, not in this repo.

    launch  ->  "Sign in with SnapTrade"  ->  browser  ->  done

WHAT THIS CANNOT DO: TRADE.

    POST /oauth/register/  {"scope": "trade"}       -> 400  "scope must be 'read'."
    POST /oauth/register/  {"scope": "read trade"}  -> 400  "scope must be 'read'."

The server enforces it at registration, and the discovery document agrees:
`"scopes_supported": ["read"]`. So OAuth gets you every READ path — net worth, holdings,
the cross-account view — and trading stays on Personal API keys until a write scope exists.
See config.mode(). The moment SnapTrade ships one, the fallback below deletes itself.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
import webbrowser

import requests

API_BASE = "https://api.snaptrade.com"
DISCOVERY = f"{API_BASE}/.well-known/oauth-authorization-server"

# The server demands an EXACT redirect_uri match, port included — so no ephemeral ports.
# We register a few fixed ones and bind whichever is free when the user signs in.
PORTS = (8765, 8919, 9137)

SERVICE = "Snappy"          # macOS Keychain service name
ACCOUNT = "snaptrade-oauth"

_meta = None                # discovery document, fetched once


class AuthError(Exception):
    pass


# --------------------------------------------------------------------------- keychain
#
# The macOS Keychain, via the `security` CLI. It's the same store Safari uses for your
# passwords: encrypted at rest, unlocked with your login, and readable only by you.
#
# A refresh token is a long-lived key to somebody's brokerage accounts. It does not go in
# .env (which people paste into issues), and it does not go in a dotfile (which people
# back up to Dropbox). It goes where macOS keeps secrets.


def _keychain_write(data):
    subprocess.run(
        ["security", "add-generic-password", "-U",  # -U: update if it already exists
         "-s", SERVICE, "-a", ACCOUNT, "-w", json.dumps(data)],
        check=True, capture_output=True,
    )


def _keychain_read():
    r = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {}


def _keychain_delete():
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT],
        capture_output=True,
    )


# --------------------------------------------------------------------------- discovery


def _discover():
    """Endpoints come from the server, not from constants I typed in.

    Prod currently serves authorize on dashboard.snaptrade.com and token/register on
    api.snaptrade.com — a split that would be easy to hardcode wrong, and that they are
    explicitly still moving around ("a very new addition we're still experimenting with").
    """
    global _meta
    if _meta:
        return _meta
    r = requests.get(DISCOVERY, timeout=15)
    r.raise_for_status()
    d = r.json()
    _meta = {
        "authorize": d["authorization_endpoint"],
        "token": d["token_endpoint"],
        "register": d["registration_endpoint"],
        "scopes": d.get("scopes_supported", []),
    }
    return _meta


def scopes_supported():
    """What the server will actually grant. Today: ['read']."""
    try:
        return _discover()["scopes"]
    except Exception:
        return []


# --------------------------------------------------------------- client registration


def _client_id():
    """Register once, then reuse. The id is public — it's the secret-less half of OAuth."""
    saved = _keychain_read()
    if saved.get("client_id"):
        return saved["client_id"]

    r = requests.post(
        _discover()["register"],
        json={
            "client_name": "Snappy",
            "redirect_uris": [f"http://127.0.0.1:{p}/callback" for p in PORTS],
            "token_endpoint_auth_method": "none",   # public client: there IS no secret
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "read",                        # the only scope the server allows
        },
        timeout=20,
    )
    if not r.ok:
        raise AuthError(f"couldn't register with SnapTrade ({r.status_code}): {r.text[:200]}")

    client_id = r.json()["client_id"]
    _keychain_write({**saved, "client_id": client_id})
    return client_id


# ------------------------------------------------------------------- loopback catcher


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Catches the single redirect the browser makes back to us."""

    query = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/callback"):
            self.send_response(404)
            self.end_headers()
            return

        _Catcher.query = dict(urllib.parse.parse_qsl(parsed.query))
        ok = "code" in _Catcher.query and "error" not in _Catcher.query

        title = "Snappy is connected" if ok else "Sign-in didn't complete"
        sub = ("You can close this tab and go back to Snappy."
               if ok else "Go back to Snappy and try again.")
        body = f"""<!doctype html><meta charset="utf-8"><title>Snappy</title>
        <style>body{{font:15px -apple-system,system-ui,sans-serif;margin:0;height:100vh;
        display:flex;align-items:center;justify-content:center;background:#0f1216;color:#f7f5f0}}
        .t{{font-size:19px;font-weight:600;margin-bottom:6px}}.m{{color:#9aa0a6}}
        .c{{text-align:center}}.b{{color:#fab038;font-size:26px;margin-bottom:14px}}</style>
        <div class=c><div class=b>▁▄█▂▆</div><div class=t>{title}</div><div class=m>{sub}</div></div>"""
        raw = body.encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass  # don't spray the console with request logs


def _listen():
    """Bind the first free registered port. Returns (server, port)."""
    for port in PORTS:
        try:
            server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
            return server, port
        except OSError:
            continue  # in use — try the next one we registered
    raise AuthError(
        f"couldn't open a callback port ({', '.join(map(str, PORTS))}) — "
        "something else on this Mac is using all of them."
    )


# --------------------------------------------------------------------------- the flow


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _exchange(form):
    """POST the token endpoint and store what comes back.

    THE REFRESH TOKEN ROTATES. Every refresh mints a new one and retires the old — so the
    response must be persisted every single time. Drop it once and the user is silently
    signed out the next time their access token expires, with no way to tell them why.
    """
    r = requests.post(
        _discover()["token"], data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20,
    )
    if not r.ok:
        detail = r.text[:200]
        hint = ""
        if r.status_code in (400, 401, 403, 404) or "not enabled" in detail.lower():
            hint = ("\n\nSnapTrade Personal OAuth is gated per-user while it's in preview. "
                    "If this says the flow isn't enabled, your dashboard.snaptrade.com "
                    "account needs to be whitelisted for it.")
        raise AuthError(f"SnapTrade rejected the sign-in ({r.status_code}): {detail}{hint}")

    t = r.json()
    saved = _keychain_read()
    _keychain_write({
        **saved,
        "access_token": t["access_token"],
        # Fall back to the one we just sent only if the server declined to rotate it.
        "refresh_token": t.get("refresh_token") or form.get("refresh_token", ""),
        "expires_at": time.time() + int(t.get("expires_in", 36000)),
    })
    return t["access_token"]


def sign_in(timeout=300):
    """Open the browser, wait for consent, come back with tokens. Blocking."""
    client_id = _client_id()
    server, port = _listen()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(16))

    url = _discover()["authorize"] + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    _Catcher.query = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    webbrowser.open(url)

    deadline = time.time() + timeout
    try:
        while _Catcher.query is None:
            if time.time() > deadline:
                raise AuthError("timed out waiting for the browser sign-in.")
            time.sleep(0.15)
        q = _Catcher.query
    finally:
        server.shutdown()
        server.server_close()

    if "error" in q:
        raise AuthError(f"sign-in was denied: {q.get('error_description') or q['error']}")
    # Without this check, anyone who can get your browser to hit the loopback port could
    # feed us an authorization code of their choosing.
    if q.get("state") != state:
        raise AuthError("state mismatch — sign-in aborted for safety.")
    if not q.get("code"):
        raise AuthError("SnapTrade returned no authorization code.")

    # The code expires in about a minute. Spend it now.
    return _exchange({
        "grant_type": "authorization_code",
        "code": q["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })


def token():
    """A valid access token, refreshed if it's about to lapse. None if signed out."""
    saved = _keychain_read()
    if not saved.get("access_token"):
        return None

    # 60s of slack: a token that expires mid-request is a token that expired.
    if time.time() < saved.get("expires_at", 0) - 60:
        return saved["access_token"]

    if not saved.get("refresh_token"):
        return None
    try:
        return _exchange({
            "grant_type": "refresh_token",
            "refresh_token": saved["refresh_token"],
            "client_id": saved.get("client_id") or _client_id(),
        })
    except AuthError:
        # The refresh token is dead (revoked, rotated past, or expired). Make the user
        # sign in again rather than limping along pretending to be authenticated.
        return None


def signed_in():
    return bool(_keychain_read().get("access_token"))


def sign_out():
    """Forget the tokens. Keeps nothing — the next sign-in re-registers cleanly."""
    _keychain_delete()
    global _meta
    _meta = None
