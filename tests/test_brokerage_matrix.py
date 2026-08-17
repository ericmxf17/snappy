"""Contract coverage for every integration in SnapTrade's public directory.

These are normalized-API tests, not claims that we possess credentials for every
brokerage. SnapTrade owns each upstream login; Snappy must remain indifferent to the
institution name once SnapTrade returns an account.
"""

from types import SimpleNamespace

import main
import snaptrade_client_wrapper as st


# Public directory captured 2026-08-17. SnapTrade markets this as "35+" and currently
# lists 38 integrations across the directory's three pages.
BROKERAGES = (
    "AJ Bell",
    "Alpaca",
    "Binance",
    "BUX",
    "Chase",
    "Citi",
    "Coinbase",
    "Commsec",
    "DEGIRO",
    "E*TRADE",
    "Edward Jones",
    "Empower",
    "eToro",
    "Fidelity",
    "Interactive Brokers",
    "Kraken",
    "Moomoo",
    "PNC",
    "Public",
    "Questrade",
    "Robinhood",
    "Schwab",
    "Stake AUS",
    "tastytrade",
    "TD Direct Investing",
    "TIAA",
    "TradeStation",
    "Tradier",
    "Trading 212",
    "Transamerica",
    "Upstox",
    "US Bank",
    "Vanguard US",
    "Wealthsimple",
    "Webull Canada",
    "Webull US",
    "Wells Fargo",
    "Zerodha",
)


class Response:
    def __init__(self, body):
        self.body = body


def normalized_accounts():
    return [
        {
            "account_id": f"account-{index}",
            "name": name,
            "number": f"0000{index:04d}",
            "institution": name,
            "connection_id": f"connection-{index}",
            "is_paper": False,
            "label": f"{name} ...{index:04d}",
            "ordinal": index,
            "total_value": 110.0,
            "holdings_synced_at": None,
            "holdings_sync_hours_ago": None,
        }
        for index, name in enumerate(BROKERAGES, start=1)
    ]


def test_every_public_integration_survives_account_normalization(monkeypatch):
    raw = [
        {
            "id": f"account-{index}",
            "name": name,
            "number": f"0000{index:04d}",
            "institution_name": name,
            "brokerage_authorization": f"connection-{index}",
            "is_paper": False,
            "balance": {"total": 110.0},
        }
        for index, name in enumerate(BROKERAGES, start=1)
    ]
    client = SimpleNamespace(
        account_information=SimpleNamespace(
            list_user_accounts=lambda **kwargs: Response(raw)
        )
    )
    monkeypatch.setattr(st, "_client", client)

    accounts = st.list_accounts()

    assert len(accounts) == 38
    assert tuple(account["institution"] for account in accounts) == BROKERAGES
    assert all(account["account_id"] and account["connection_id"] for account in accounts)


def test_every_public_integration_aggregates_in_one_portfolio(monkeypatch):
    accounts = normalized_accounts()
    monkeypatch.setattr(st, "list_accounts", lambda: accounts)
    monkeypatch.setattr(
        st,
        "get_account_balance",
        lambda account_id=None: [{"currency": "USD", "cash": 100.0}],
    )
    monkeypatch.setattr(
        st,
        "get_positions",
        lambda account_id=None: [
            {
                "symbol": "AAPL",
                "description": "Apple Inc.",
                "units": 1.0,
                "price": 10.0,
                "average_purchase_price": 8.0,
                "open_pnl": 2.0,
            }
        ],
    )
    monkeypatch.setattr(st, "unsynced_fills", lambda account_id=None: [])

    book = st.get_all_holdings()

    assert book["account_count"] == 38
    assert book["total_cash"] == 3800.0
    assert book["total_holdings_value"] == 380.0
    assert book["net_worth"] == 4180.0
    assert len(book["accounts"]) == 38
    assert book["combined_holdings"][0]["units"] == 38.0
    assert book["combined_holdings"][0]["held_in"] == 38
    assert {a["institution"] for a in book["accounts"]} == set(BROKERAGES)


def test_every_public_brokerage_name_renders_without_an_alpaca_fallback(state_reset):
    app = object.__new__(main.Snappy)

    for name in BROKERAGES:
        state_reset.update(
            auth_mode="oauth",
            portfolio_status="ready",
            positions=[{"symbol": "AAPL"}],
            accounts=[{"label": name}],
            account_count=1,
        )
        assert app.view()["sub"] == f"1 holding · {name}"
