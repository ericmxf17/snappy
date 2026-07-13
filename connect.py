"""Open a SnapTrade Connection Portal link with TRADING enabled.

The CLI (`snaptrade connect --broker X`) has no way to ask for a trade-enabled
connection: `connection_type` is a parameter on login_snap_trade_user, and the CLI
never passes it. So every connection it makes comes back read-only, and every order is
then refused by trading.py's guards — correctly, and confusingly.

This opens the portal with connection_type="trade".

    ./venv/bin/python connect.py                 # list connections
    ./venv/bin/python connect.py ALPACA-PAPER    # connect one, with trading
    ./venv/bin/python connect.py --drop <id>     # remove a connection

You enter your brokerage credentials in the BROWSER. They never touch this process,
this repo, or the model.
"""

import sys
import webbrowser

import snaptrade_client_wrapper as st


def show():
    print("\nConnections:")
    for c in st.list_connections():
        trade = "TRADE" if (c["type"] or "").lower() == "trade" else "read-only"
        flag = "" if trade == "TRADE" else "   <- cannot place orders"
        print(f"   {c['connection_id']}  {c['brokerage']:<16} {trade}{flag}")
    print()


def connect(broker):
    """Print (and open) a portal link that asks for a TRADE connection."""
    login = st._client.authentication.login_snap_trade_user(
        **st._USER,
        broker=broker,
        connection_type="trade",   # the whole point — the CLI can't pass this
        immediate_redirect=True,
    ).body

    url = dict(login).get("redirectURI")
    if not url:
        print("No portal URL came back:", login)
        return

    print(f"\nOpening the SnapTrade portal for {broker} with TRADING enabled.")
    print("Enter your brokerage credentials in the browser — nothing is read here.\n")
    print(url, "\n")
    webbrowser.open(url)


def drop(connection_id):
    st._client.connections.remove_brokerage_authorization(
        authorization_id=connection_id, **st._USER
    )
    print(f"Removed {connection_id}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        show()
    elif args[0] == "--drop":
        drop(args[1])
        show()
    else:
        connect(args[0])
