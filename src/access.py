"""Local storage for SnapTrade Personal-key access and the user's chosen mode.

OAuth tokens and Personal keys are separate credentials. OAuth remains read-only;
Personal keys can request a trade-enabled brokerage connection. Secrets live in the
macOS Keychain, never in panel state or the hosted backend.
"""

import json
import subprocess

import config

SERVICE = "Snappy"
KEY_ACCOUNT = "snaptrade-personal-api"
MODE_ACCOUNT = "snaptrade-access-mode"


def _read(account):
    result = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _write(account, value):
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE,
         "-a", account, "-w", value],
        check=True, capture_output=True,
    )


def _delete(account):
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
        capture_output=True,
    )


def personal_keys():
    saved_keys = saved_personal_keys()
    if saved_keys:
        return saved_keys
    if config.SNAPTRADE_CLIENT_ID and config.SNAPTRADE_CONSUMER_KEY:
        return config.SNAPTRADE_CLIENT_ID, config.SNAPTRADE_CONSUMER_KEY
    return None


def saved_personal_keys():
    """Personal keys explicitly entered in Snappy, excluding development `.env` keys."""
    saved = _read(KEY_ACCOUNT)
    if saved:
        try:
            data = json.loads(saved)
            if data.get("client_id") and data.get("consumer_key"):
                return data["client_id"], data["consumer_key"]
        except json.JSONDecodeError:
            pass
    return None


def save_personal_keys(client_id, consumer_key):
    client_id, consumer_key = client_id.strip(), consumer_key.strip()
    if not client_id or not consumer_key:
        raise ValueError("Both the Personal Client ID and Consumer Key are required.")
    _write(KEY_ACCOUNT, json.dumps({
        "client_id": client_id,
        "consumer_key": consumer_key,
    }))


def preferred_mode():
    mode = _read(MODE_ACCOUNT)
    return mode if mode in ("oauth", "keys") else None


def set_preferred_mode(mode):
    if mode not in ("oauth", "keys"):
        raise ValueError("invalid access mode")
    _write(MODE_ACCOUNT, mode)


def clear():
    """Remove Personal credentials and the saved mode selection."""
    _delete(KEY_ACCOUNT)
    _delete(MODE_ACCOUNT)


def forget_personal_keys():
    """Forget UI-entered keys without touching the OAuth session or development `.env`."""
    _delete(KEY_ACCOUNT)
