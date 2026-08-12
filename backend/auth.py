"""One-time invite redemption and signed hosted-service sessions."""

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SessionTokens:
    def __init__(self, secret, lifetime=30 * 24 * 60 * 60):
        if len(secret) < 32:
            raise ValueError("SNAPPY_SESSION_SECRET must be at least 32 characters")
        self.secret = secret.encode()
        self.lifetime = lifetime

    def issue(self):
        payload = _b64(json.dumps({
            "sub": secrets.token_urlsafe(16),
            "exp": int(time.time()) + self.lifetime,
        }, separators=(",", ":")).encode())
        signature = _b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{signature}"

    def verify(self, token):
        try:
            payload, signature = token.split(".", 1)
            expected = _b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return False
            data = json.loads(_unb64(payload))
            return int(data["exp"]) > int(time.time()) and bool(data["sub"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False


class InviteStore:
    def __init__(self, path):
        self.path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS invites ("
                "code_hash TEXT PRIMARY KEY, created_at INTEGER NOT NULL, used_at INTEGER)"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _hash(code):
        return hashlib.sha256(code.encode()).hexdigest()

    def create(self):
        code = secrets.token_urlsafe(24)
        with self._connect() as db:
            db.execute(
                "INSERT INTO invites(code_hash, created_at) VALUES (?, ?)",
                (self._hash(code), int(time.time())),
            )
        return code

    def redeem(self, code):
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE invites SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                (int(time.time()), self._hash(code)),
            )
            return cursor.rowcount == 1
