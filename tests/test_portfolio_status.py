"""Portfolio loading must work without Claude and must never hide OAuth failures."""

import main
import ui


def app_without_appkit():
    """Use Snappy's portfolio methods without constructing a macOS application."""
    return object.__new__(main.Snappy)


def test_no_granted_accounts_is_actionable_not_an_empty_portfolio(
    monkeypatch, state_reset
):
    monkeypatch.setattr(main.st, "mode", lambda: "oauth")
    monkeypatch.setattr(
        main.st,
        "get_all_holdings",
        lambda: (_ for _ in ()).throw(main.st.NoAccountsError("none granted")),
    )
    monkeypatch.setattr(
        main.st,
        "list_connections",
        lambda: [{"connection_id": "alpaca-1", "brokerage": "Alpaca", "type": "read"}],
    )

    app = app_without_appkit()
    app.refresh_portfolio()

    assert state_reset.STATE["portfolio_status"] == "no_accounts"
    assert state_reset.STATE["account_count"] == 0
    assert state_reset.STATE["connections"][0]["connection_id"] == "alpaca-1"
    assert "Choose accounts again" in state_reset.STATE["portfolio_error"]
    assert app.view()["sub"] == "No accounts granted"


def test_portfolio_transport_error_is_visible_and_preserves_last_good_data(
    monkeypatch, state_reset
):
    state_reset.update(
        auth_mode="oauth",
        total_value=1250.0,
        cash=250.0,
        holdings_value=1000.0,
        positions=[{"symbol": "AAPL"}],
        accounts=[{"label": "Alpaca", "positions": []}],
        account_count=1,
        portfolio_status="ready",
    )
    monkeypatch.setattr(main.st, "mode", lambda: "oauth")
    monkeypatch.setattr(
        main.st,
        "get_all_holdings",
        lambda: (_ for _ in ()).throw(RuntimeError("private transport detail")),
    )

    app = app_without_appkit()
    app.refresh_portfolio()

    assert state_reset.STATE["portfolio_status"] == "error"
    assert state_reset.STATE["total_value"] == 1250.0
    assert state_reset.STATE["positions"] == [{"symbol": "AAPL"}]
    assert "private transport detail" not in state_reset.STATE["portfolio_error"]
    assert app.view()["sub"] == "1 holding · Alpaca"


def test_successful_portfolio_read_is_model_independent(monkeypatch, state_reset):
    book = {
        "net_worth": 4200.0,
        "total_cash": 200.0,
        "total_holdings_value": 4000.0,
        "combined_holdings": [{"symbol": "MSFT"}],
        "account_count": 1,
        "accounts": [
            {
                "account_id": "account-1",
                "connection_id": "connection-1",
                "label": "Fidelity",
                "cash": 200.0,
                "holdings_value": 4000.0,
                "total_value": {"amount": 4200.0},
                "positions": [{"symbol": "MSFT"}],
            }
        ],
    }
    monkeypatch.setattr(main.st, "mode", lambda: "oauth")
    monkeypatch.setattr(main.st, "get_all_holdings", lambda: book)
    monkeypatch.setattr(
        main.st,
        "list_connections",
        lambda: [{"connection_id": "connection-1", "type": "read"}],
    )
    monkeypatch.setattr(main.transcribe, "set_hints", lambda symbols: None)

    app = app_without_appkit()
    app.refresh_portfolio()

    assert state_reset.STATE["portfolio_status"] == "ready"
    assert state_reset.STATE["total_value"] == 4200.0
    assert app.view()["sub"] == "1 holding · Fidelity"


def test_retry_invalidates_cached_empty_accounts(monkeypatch, state_reset):
    calls = []

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    app = app_without_appkit()
    state_reset.update(portfolio_status="error")
    monkeypatch.setattr(main.st, "mode", lambda: "oauth")
    monkeypatch.setattr(main.st, "invalidate", lambda: calls.append("invalidate"))
    monkeypatch.setattr(app, "refresh_portfolio", lambda: calls.append("refresh"))
    monkeypatch.setattr(main.threading, "Thread", ImmediateThread)

    app.retry_portfolio()

    assert calls == ["invalidate", "refresh"]
    assert state_reset.STATE["portfolio_status"] == "loading"


def test_panel_exposes_distinct_portfolio_recovery_actions():
    source = open(ui.PAGE, encoding="utf-8").read()

    assert "No accounts granted" in source
    assert 'id="btnChooseAccounts"' in source
    assert 'id="btnPortfolioRefresh"' in source
    assert "type: 'refresh'" in source
    assert "portfolio_status === 'ready'" in source
