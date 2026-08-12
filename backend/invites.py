"""Create a one-time invite: python -m backend.invites."""

import os

from backend.auth import InviteStore


if __name__ == "__main__":
    store = InviteStore(os.environ.get("SNAPPY_INVITE_DB", "snappy-invites.db"))
    print(store.create())
