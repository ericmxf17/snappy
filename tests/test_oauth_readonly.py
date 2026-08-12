"""OAuth is read-only, and Snappy must fail CLOSED when it is.

SnapTrade grants Personal OAuth the 'read' scope and refuses every other one at
registration:

    POST /oauth/register/ {"scope": "trade"}  ->  400  "scope must be 'read'."

So an OAuth session cannot place an order — the question is only whether Snappy notices
before or after the user says "confirm". These tests pin down that it notices first, and
that it says something a person can act on.
"""

import socket
import threading
import time

import pytest

import snaptrade_bearer as bearer
import auth
import snaptrade_client_wrapper as st
import trading


@pytest.fixture
def oauth(monkeypatch):
    """Pretend the user signed in with OAuth instead of pasting keys."""
    monkeypatch.setattr(st, "can_trade", lambda: False)
    monkeypatch.setattr(st, "mode", lambda: "oauth")
    trading.cancel()  # no pending order bleeding in from another test
    yield
    trading.cancel()


def test_propose_refuses_before_asking_which_account(oauth, monkeypatch):
    """The refusal must come FIRST, not after the account picker.

    propose() raises AccountNeeded when the user has several accounts and didn't name
    one. If the read-only check ran after that, an OAuth user would be shown a picker,
    make a choice, and only then be told the trade was never possible. Never ask someone
    for a decision you already know you will refuse to act on.
    """
    def boom(*a, **k):
        raise AssertionError("list_accounts() was called — it got past the guard")

    monkeypatch.setattr(st, "list_accounts", boom)

    with pytest.raises(trading.TradeRefused) as e:
        trading.propose("BUY", "NVDA", 1)

    assert "read-only" in str(e.value).lower()
    assert trading.pending() is None, "a refused trade must leave nothing armed"


def test_the_refusal_tells_you_how_to_fix_it(oauth):
    """A guard that says 'no' without saying 'why' or 'so what do I do' is a dead end."""
    with pytest.raises(trading.TradeRefused) as e:
        trading.propose("BUY", "NVDA", 1)

    msg = str(e.value)
    assert "read" in msg.lower(), "must name the scope that's missing"
    assert "keys" in msg.lower(), "must say how to actually enable trading"


def test_check_account_refuses_too(oauth, monkeypatch):
    """Defence in depth: the guard chain refuses even if propose() were bypassed."""
    monkeypatch.setattr(
        st, "resolve_account",
        lambda hint=None: (_ for _ in ()).throw(AssertionError("got past the guard")),
    )
    with pytest.raises(trading.TradeRefused):
        trading.check_account("alpaca")


def test_bearer_client_cannot_trade_at_the_transport_layer():
    """The last line: even holding the client directly, the trading calls refuse.

    This is the control that survives someone deleting a guard in trading.py by mistake —
    the transport itself has no way to place an order.
    """
    from snaptrade_bearer import BearerClient, ReadOnly

    client = BearerClient()
    for call in (
        lambda: client.trading.get_order_impact(account_id="a", action="BUY"),
        lambda: client.trading.place_order(trade_id="t"),
        lambda: client.trading.place_force_order(account_id="a"),
        lambda: client.trading.cancel_user_account_order(account_id="a"),
    ):
        with pytest.raises(ReadOnly):
            call()


def test_bearer_client_uses_unified_positions_endpoint(monkeypatch):
    """OAuth must not call the legacy endpoint, which returns 410 for newer users."""
    paths = []
    monkeypatch.setattr(
        bearer, "_get",
        lambda path, params=None: paths.append(path) or bearer._Response({"results": []}),
    )

    bearer.BearerClient().account_information.get_all_account_positions("account-id")

    assert paths == ["/accounts/account-id/positions/all"]


def test_bearer_client_lists_connections(monkeypatch):
    """Connection-health tools need the same client surface in OAuth and keys mode."""
    paths = []
    monkeypatch.setattr(
        bearer, "_get",
        lambda path, params=None: paths.append(path) or bearer._Response([]),
    )

    response = bearer.BearerClient().connections.list_brokerage_authorizations()

    assert paths == ["/authorizations"]
    assert response.body == []


def test_keys_mode_can_still_trade(monkeypatch):
    """The read-only guard must not have quietly disabled trading for key users."""
    monkeypatch.setattr(st, "can_trade", lambda: True)
    monkeypatch.setattr(st, "mode", lambda: "keys")

    # It should get PAST the read-only guard and fail on something else entirely
    # (no accounts stubbed here) — anything but the OAuth refusal.
    monkeypatch.setattr(st, "list_accounts", lambda: [])
    with pytest.raises(trading.TradeRefused) as e:
        trading.propose("BUY", "NVDA", 1)

    assert "read-only" not in str(e.value).lower()


def test_oauth_callback_shutdown_does_not_wait_for_browser_connection(monkeypatch):
    """Safari may keep the success-page request alive after rendering it."""
    # This is an HTTP-server lifecycle test, not a registered-redirect test. An
    # ephemeral port prevents collisions with Snappy, another test process, or a
    # managed environment that reserves the production callback ports.
    monkeypatch.setattr(auth, "PORTS", (0,))
    release = threading.Event()
    original = auth._Catcher.do_GET

    def held_open(handler):
        original(handler)
        release.wait(2)

    monkeypatch.setattr(auth._Catcher, "do_GET", held_open)
    server, port = auth._listen()
    auth._Catcher.query = None
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    client.sendall(b"GET /callback?code=ok&state=ok HTTP/1.1\r\nHost: localhost\r\n\r\n")

    deadline = time.time() + 1
    while auth._Catcher.query is None and time.time() < deadline:
        time.sleep(0.01)
    assert auth._Catcher.query == {"code": "ok", "state": "ok"}

    started = time.monotonic()
    server.shutdown()
    elapsed = time.monotonic() - started
    release.set()
    client.close()
    server.server_close()

    assert elapsed < 1, "callback cleanup blocked token exchange"
