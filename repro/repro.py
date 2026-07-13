"""Standalone reproduction of the SnapTrade findings. Depends on NOTHING from Snappy.

This file imports the official `snaptrade-python-sdk` and nothing else. No wrapper, no
parser, no application code — so nothing here can be an artefact of how I built my app.
That distinction matters: I once "found" a SnapTrade sync bug that turned out to be my
own parser reading the wrong payload shape, and I'm not going to make that mistake in
front of you twice.

Run it against your own account:

    pip install snaptrade-python-sdk
    export SNAPTRADE_CLIENT_ID=...      # your Personal PERS-... pair
    export SNAPTRADE_CONSUMER_KEY=...
    python repro.py

(On a Personal account, user_id and user_secret are both the literal string "personal";
override SNAPTRADE_USER_ID / SNAPTRADE_USER_SECRET if yours differ.)

It only READS. It places no orders and changes nothing.
"""

import os
import statistics
import time

from snaptrade_client import SnapTrade

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)
USER = {
    "user_id": os.environ.get("SNAPTRADE_USER_ID", "personal"),
    "user_secret": os.environ.get("SNAPTRADE_USER_SECRET", "personal"),
}

accounts = client.account_information.list_user_accounts(**USER).body
if not accounts:
    raise SystemExit("No accounts connected.")
print(f"{len(accounts)} account(s) connected\n")


def rule(n, title):
    print(f"\n{'=' * 74}\n  FINDING {n}: {title}\n{'=' * 74}")


# ---------------------------------------------------------------------------------
rule(1, "Symbol search ranks a 2x SHORT ETF above the company (SAFETY)")

for query in ("nvidia", "apple"):
    results = client.reference_data.symbol_search_user_account(
        account_id=accounts[0]["id"], substring=query, **USER
    ).body
    print(f'\n  symbol_search_user_account(substring="{query}") ->')
    for i, r in enumerate(results[:6], 1):
        sym = r.get("symbol") or "?"
        desc = (r.get("description") or "")[:52]
        print(f"    {i}. {sym:6} {desc}")

print(
    "\n  An LLM or a voice UI takes result #1. For \"nvidia\" that is a 2x INVERSE ETF,"
    "\n  so \"buy me some Nvidia\" SHORTS Nvidia at leverage. It fills cleanly."
)

# ---------------------------------------------------------------------------------
rule(2, "The SDK exposes methods that are 410 Gone")

for label, call in (
    ("account_information.get_all_user_holdings",
     lambda: client.account_information.get_all_user_holdings(**USER)),
    ("transactions_and_reporting.get_activities",
     lambda: client.transactions_and_reporting.get_activities(**USER)),
    ("account_information.get_user_account_return_rates",
     lambda: client.account_information.get_user_account_return_rates(
         account_id=accounts[0]["id"], **USER)),
):
    try:
        call()
        print(f"  {label:52} OK")
    except Exception as e:
        status = getattr(e, "status", None) or "?"
        print(f"  {label:52} HTTP {status}")

print(
    "\n  These are importable, typed, and autocompleted. get_all_user_holdings is the"
    "\n  first method you reach for when writing a multi-brokerage app."
)

# ---------------------------------------------------------------------------------
rule(3, "Reads randomly stall for 20-30 seconds")

print("\n  Six cold rounds. Watch WHERE the stall lands: it moves between balance and")
print("  positions, and between accounts. It is not one slow endpoint, and it is not")
print("  one slow account — but the frequency differs enormously between accounts.\n")

worst = {}
for r in range(6):
    row = []
    for i, a in enumerate(accounts, 1):
        aid = a["id"]
        t0 = time.time()
        client.account_information.get_user_account_balance(account_id=aid, **USER)
        bal = time.time() - t0

        t0 = time.time()
        client.account_information.get_all_account_positions(account_id=aid, **USER)
        pos = time.time() - t0

        row.append(f"acct{i}: balance={bal:6.1f}s  positions={pos:6.1f}s")
        worst.setdefault(i, []).append(max(bal, pos))
    print("    " + "  |  ".join(row))

print()
for i, times in worst.items():
    num = (accounts[i - 1].get("number") or "")[-4:]
    print(
        f"    acct{i} (...{num}):  median {statistics.median(times):5.1f}s   "
        f"worst {max(times):5.1f}s"
    )

print(
    "\n  A balance call returns cash. It does not read positions. So a 25s balance call"
    "\n  cannot be explained by how many holdings the account has."
    "\n"
    "\n  For a voice assistant this is not slow, it is BROKEN: a 27-second pause after a"
    "\n  spoken question is indistinguishable from a hang. And caching cannot help — a"
    "\n  COLD read is precisely what stalls."
)

# ---------------------------------------------------------------------------------
rule(4, "The CLI cannot create a trade-enabled connection")
print(
    """
  $ npx @snaptrade/snaptrade-cli@0.1.38 connect --help

    Usage: snaptrade connect [options]
    Options:
      --broker <slug>  Brokerage slug to connect
      -h, --help       display help for command

  No --connection-type. Every connection the CLI makes is READ-ONLY, and nothing in
  its output says so — the trades just fail later. connection_type="trade" has to be
  passed to the API directly.
"""
)
print("=" * 74)
