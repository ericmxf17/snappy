"""A SnapTrade client that authenticates with an OAuth bearer token.

The official SDK signs every request with HMAC over your clientId/consumerKey. There is no
way to hand it a bearer token, so OAuth needs its own transport — SnapBar hit the same wall
and did the same thing.

It is DUCK-TYPED TO THE SDK ON PURPOSE. Same call shapes, same `.body` on the response:

    client.account_information.list_user_accounts(**USER)
    client.trading.get_user_account_quotes(account_id=..., symbols=..., use_ticker=True)

so snaptrade_client_wrapper doesn't care which one it's holding. `**USER` is accepted and
ignored — a bearer token already says who you are, so there is no user_id/user_secret to
send. That's the whole point of the OAuth flow: nobody pastes a key.

READS ONLY, and not by choice — SnapTrade grants Personal OAuth the `read` scope and
nothing else (see auth.py). Every trading method here raises rather than pretending. A
client that silently can't trade is worse than one that says so.
"""

import requests

import auth

BASE = "https://api.snaptrade.com/api/v1"
TIMEOUT = 60  # SnapTrade stalls reads for 20-30s at random; see wrapper.prime()


class ReadOnly(Exception):
    """Raised when something tries to trade over an OAuth session."""


class _Response:
    """Mimics the SDK's response object, which carries the payload in `.body`."""

    def __init__(self, body):
        self.body = body


def _get(path, params=None):
    return _call("GET", path, params=params)


def _post(path, params=None, json_body=None):
    return _call("POST", path, params=params, json_body=json_body)


def _call(method, path, params=None, json_body=None):
    access = auth.token()
    if not access:
        raise auth.AuthError("Not signed in to SnapTrade.")
    r = requests.request(
        method, BASE + path,
        headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
        params=params, json=json_body, timeout=TIMEOUT,
    )
    if r.status_code == 401:
        raise auth.AuthError("SnapTrade sign-in expired. Sign in again.")
    if r.status_code == 403:
        # The likeliest cause by far, given the scope is read-only.
        raise ReadOnly(
            f"SnapTrade refused this over OAuth ({r.status_code}). Personal OAuth is "
            f"read-only; trading needs Personal API keys."
        )
    r.raise_for_status()
    return _Response(r.json() if r.content else None)


class _AccountInformation:
    def list_user_accounts(self, **_):
        return _get("/accounts")

    def get_user_account_balance(self, account_id, **_):
        return _get(f"/accounts/{account_id}/balances")

    def get_all_account_positions(self, account_id, **_):
        return _get(f"/accounts/{account_id}/positions")

    def get_user_account_orders(self, account_id, **kw):
        params = {}
        # The SDK spells it `state`; keep the same door so the wrapper needs no special case.
        if kw.get("state"):
            params["state"] = kw["state"]
        return _get(f"/accounts/{account_id}/orders", params=params)

    def get_account_activities(self, account_id, **_):
        return _get(f"/accounts/{account_id}/activities")

    def get_account_balance_history(self, account_id, **_):
        return _get(f"/accounts/{account_id}/balanceHistory")

    def get_user_account_return_rates(self, account_id, **_):
        return _get(f"/accounts/{account_id}/returnRates")


class _ReferenceData:
    def symbol_search_user_account(self, account_id, substring, **_):
        # POST, not GET — the substring travels in the body.
        return _post(f"/accounts/{account_id}/symbols", json_body={"substring": substring})

    def list_all_brokerages(self, **_):
        return _get("/brokerages")


class _Trading:
    """Quotes are a read. Everything that moves money is not, and cannot work here."""

    def get_user_account_quotes(self, account_id, symbols, use_ticker=True, **_):
        return _get(
            f"/accounts/{account_id}/quotes",
            params={"symbols": symbols, "use_ticker": str(bool(use_ticker)).lower()},
        )

    def _refuse(self, what):
        raise ReadOnly(
            f"Can't {what} over an OAuth sign-in — SnapTrade Personal OAuth only grants "
            f"the 'read' scope (POST /oauth/register/ with scope=trade returns 400, "
            f"\"scope must be 'read'.\"). Add Personal API keys to enable trading."
        )

    def get_order_impact(self, **_):
        self._refuse("preview a trade")

    def place_order(self, **_):
        self._refuse("place an order")

    def place_force_order(self, **_):
        self._refuse("place an order")

    def cancel_user_account_order(self, **_):
        self._refuse("cancel an order")


class BearerClient:
    """Stands in for `SnapTrade(...)` when the user signed in with OAuth."""

    def __init__(self):
        self.account_information = _AccountInformation()
        self.reference_data = _ReferenceData()
        self.trading = _Trading()
