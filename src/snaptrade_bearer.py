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
import snaptrade_mcp as mcp

BASE = "https://api.snaptrade.com/api/v1"
TIMEOUT = 60  # SnapTrade stalls reads for 20-30s at random; see wrapper.prime()
_mcp_only = False


class ReadOnly(Exception):
    """Raised when something tries to trade over an OAuth session."""


class _Response:
    """Mimics the SDK's response object, which carries the payload in `.body`."""

    def __init__(self, body):
        self.body = body


def _get(path, params=None, *, mcp_tool=None, mcp_args=None):
    return _call("GET", path, params=params, mcp_tool=mcp_tool, mcp_args=mcp_args)


def _post(path, params=None, json_body=None):
    return _call("POST", path, params=params, json_body=json_body)


def _call(method, path, params=None, json_body=None, *, mcp_tool=None, mcp_args=None):
    global _mcp_only
    if _mcp_only and mcp_tool:
        return _Response(mcp.call(mcp_tool, **(mcp_args or {})))
    access = auth.token()
    if not access:
        raise auth.AuthError("Not signed in to SnapTrade.")
    r = requests.request(
        method, BASE + path,
        headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
        params=params, json=json_body, timeout=TIMEOUT,
    )
    if r.status_code == 401:
        try:
            error = r.json()
        except (ValueError, TypeError):
            error = {}
        if str(error.get("code")) == "1083":
            if not mcp_tool:
                raise auth.OAuthClientRestricted("This read is unavailable through SnapTrade MCP.")
            _mcp_only = True
            return _Response(mcp.call(mcp_tool, **(mcp_args or {})))
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
        global _mcp_only
        if not _mcp_only:
            try:
                return _get("/accounts")
            except auth.OAuthClientRestricted:
                _mcp_only = True
        accounts = []
        authorizations = mcp.items(mcp.call("Connections_listBrokerageAuthorizations"))
        for authorization in authorizations:
            authorization_id = authorization.get("id") if isinstance(authorization, dict) else None
            if authorization_id:
                accounts.extend(mcp.items(mcp.call(
                    "Connections_listBrokerageAuthorizationAccounts",
                    authorization_id=authorization_id,
                )))
        return _Response(accounts)

    def get_user_account_balance(self, account_id, **_):
        return _get(
            f"/accounts/{account_id}/balances",
            mcp_tool="AccountInformation_getUserAccountBalance",
            mcp_args={"account_id": account_id},
        )

    def get_all_account_positions(self, account_id, **_):
        # The legacy /positions endpoint now returns 410 for newer users. This is
        # SnapTrade's unified replacement (equities, options, crypto, futures).
        return _get(
            f"/accounts/{account_id}/positions/all",
            mcp_tool="AccountInformation_getAllAccountPositions",
            mcp_args={"account_id": account_id},
        )

    def get_user_account_orders(self, account_id, **kw):
        params = {}
        # The SDK spells it `state`; keep the same door so the wrapper needs no special case.
        if kw.get("state"):
            params["state"] = kw["state"]
        return _get(
            f"/accounts/{account_id}/orders", params=params,
            mcp_tool="AccountInformation_getUserAccountOrdersV2",
            mcp_args={"account_id": account_id, "state": kw.get("state")},
        )

    def get_account_activities(self, account_id, **_):
        return _get(
            f"/accounts/{account_id}/activities",
            mcp_tool="AccountInformation_getAccountActivities",
            mcp_args={"account_id": account_id},
        )

    def get_account_balance_history(self, account_id, **_):
        return _get(
            f"/accounts/{account_id}/balanceHistory",
            mcp_tool="AccountInformation_getAccountBalanceHistory",
            mcp_args={"account_id": account_id},
        )

    def get_user_account_return_rates(self, account_id, **_):
        return _get(f"/accounts/{account_id}/returnRates")


class _ReferenceData:
    def symbol_search_user_account(self, account_id, substring, **_):
        # POST, not GET — the substring travels in the body.
        return _post(f"/accounts/{account_id}/symbols", json_body={"substring": substring})

    def list_all_brokerages(self, **_):
        return _get("/brokerages")


class _Connections:
    def list_brokerage_authorizations(self, **_):
        return _get(
            "/authorizations",
            mcp_tool="Connections_listBrokerageAuthorizations",
        )


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
        self.connections = _Connections()
        self.reference_data = _ReferenceData()
        self.trading = _Trading()
