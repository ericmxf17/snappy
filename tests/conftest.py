"""Test setup.

The tests never touch the network, the microphone, the Anthropic API, or the
SnapTrade API. Anything that would leave the machine is faked, so the suite runs
offline, in about a second, and costs nothing.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

# config.py reads these at import and raises if they're missing. Set fakes BEFORE
# anything imports it, so the suite runs on a machine with no .env at all (a fresh
# clone, or CI).
os.environ.setdefault("SNAPTRADE_CLIENT_ID", "PERS-TEST")
os.environ.setdefault("SNAPTRADE_CONSUMER_KEY", "test-consumer-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")

# Pin the auth mode. Otherwise the suite asks the macOS Keychain whether this developer
# happens to be signed in to SnapTrade OAuth — real, machine-wide, mutable state — and the
# wrapper would silently swap the SDK for the read-only bearer client halfway through a
# trading test. Tests must not depend on the machine they run on.
os.environ["SNAPPY_AUTH_MODE"] = "keys"

import pytest  # noqa: E402


class Body:
    """Stands in for an SDK response, which wraps its payload in `.body`."""

    def __init__(self, body):
        self.body = body


@pytest.fixture(autouse=True)
def _no_cache_between_tests():
    """The wrapper memoises its reads for 20s. Tests run in milliseconds.

    Without this, one test's fake portfolio is still cached when the next one asks —
    so a test that stubs an empty account gets served the previous test's $30,000.
    The cache is doing exactly its job; it just must not span tests.
    """
    import snaptrade_client_wrapper as st

    st.invalidate()
    yield
    st.invalidate()


@pytest.fixture
def state_reset():
    """state.STATE is module-level and shared; give each test a clean one."""
    import state

    before = {k: (v.copy() if isinstance(v, list) else v) for k, v in state.STATE.items()}
    yield state
    state.STATE.clear()
    state.STATE.update(before)
