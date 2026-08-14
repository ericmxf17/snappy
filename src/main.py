"""Snappy — ask your brokerage account a question out loud.

Hold Right ⌥ and talk. It transcribes locally, asks Claude (which pulls live data
from SnapTrade and searches the web), and writes the answer into a floating glass
panel — the headline first, then the arithmetic, the sources, and the API trace.

You speak; it writes. There is deliberately no text-to-speech: a laptop mic beside a
laptop speaker is an echo path, and it cost us a string of bugs — including Snappy
recording its own voice and talking itself out of its own trade.

Run with:  ./venv/bin/python main.py
"""

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser

import AppKit
import objc
import rumps
from Foundation import NSObject

import audio
import access
import auth
import backend_client
import config
import hotkey
import snaptrade_client_wrapper as st
import state
import trading
import transcribe
import ui
from assistant import answer

# The Snappy mark: five bars that read as a waveform and as a bar chart at once. All
# three states are the SAME glyph at different amplitudes — quiet, hot, flat — so the
# icon never changes identity or width as the state flips. See assets/mark.py.
#
# These are TEMPLATE images (black + alpha), so macOS tints them for the light or dark
# menubar itself. A coloured icon would look pasted on in one of the two.
if getattr(sys, "frozen", False):
    # Packaged by py2app (see setup.py): assets are loose files under Contents/Resources,
    # not next to this script, which py2app has zipped up into the app instead.
    ROOT = os.environ.get("RESOURCEPATH", "")
    ASSETS = os.path.join(ROOT, "assets")
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/ -> repo root
    ASSETS = os.path.join(ROOT, "assets")

_instance_lock = None


def acquire_instance_lock(path=None):
    """Allow only one Snappy process across source and every packaged copy."""
    global _instance_lock
    if _instance_lock is not None:
        return True
    path = path or os.path.expanduser(
        "~/Library/Application Support/Snappy/instance.lock"
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    _instance_lock = handle
    return True

MARKS = {
    "idle": "menubar-idle@2x.png",
    "listening": "menubar-listening@2x.png",
    "thinking": "menubar-thinking@2x.png",
}

# If the assets are ever missing, fall back to Apple's stock symbols rather than
# showing nothing at all — a menubar app you can't see is a menubar app you can't quit.
SYMBOLS = {
    "idle": "waveform",
    "listening": "waveform.circle.fill",
    "thinking": "ellipsis.circle",
}

ACCESSIBILITY_PANE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)
SNAPTRADE_DASHBOARD = "https://dashboard.snaptrade.com/"

# Recordings that end when you stop talking. A held ⌥ is the exception — that one
# sends on release.
#
# There used to be a third trigger, "confirm": Snappy opened the mic BY ITSELF when a
# trade was proposed. Every recording it started was a recording nobody asked for, and
# it was the source of nearly every trading bug this app has had. It is gone. Snappy
# only ever listens because the user made it listen.
AUTOSTOP_TRIGGERS = ("click",)


def answers_pending_order(text):
    """Is this an ANSWER to an order on the table, rather than a new question?

    Only an unambiguous yes or no counts. "What's the risk?" is still a question, and
    anything unclear — silence, a garbled transcript, a follow-up — leaves the order
    standing. Treating "unclear" as "cancel" once destroyed a trade the user wanted
    while they were reaching for the Confirm button.
    """
    return bool(trading.pending()) and (
        trading.is_confirmation(text) or trading.is_cancellation(text)
    )



# Snappy has no voice. It listens, and it answers on the panel.
#
# Text-to-speech is gone deliberately, and it took a whole class of bugs with it:
# a laptop mic sitting beside a laptop speaker is an echo path, and the confirmation
# recording kept capturing Snappy's own read-back — transcribing "say confirm to
# place the trade", deciding that wasn't a yes, and talking itself out of its own
# trade. No speech, no echo, no speech-lock races, no settle delays.
def notify(text):
    """Say something to the user. On screen, where it can be re-read."""
    print(f"→ {text}")
    state.notify(text)


def describe_fill(filled):
    """Describe a placed order. MUST NOT RAISE.

    This ran inside the try/except that reports trade failures, so when it threw a
    TypeError on a null field it was caught and reported as "the order didn't go
    through — nothing was placed". The order HAD gone through. A formatting bug must
    never be able to impersonate a failed trade, so every field here is optional.
    """
    try:
        if filled.get("kind") == "cancel_all":
            done, failed = filled.get("cancelled", []), filled.get("failed", [])
            line = f"Cancelled {len(done)} order{'' if len(done) == 1 else 's'}."
            if failed:
                line += (
                    f" {len(failed)} couldn't be cancelled — they may have already "
                    "filled. Check your brokerage."
                )
            return line

        if filled.get("kind") == "cancel":
            units = filled.get("units")
            size = f"{units:g} " if isinstance(units, (int, float)) else ""
            return (
                f"Cancelled the {(filled.get('action') or '').lower()} order for "
                f"{size}{filled.get('symbol') or 'that symbol'}."
            )

        status = (filled.get("status") or "").upper()
        settled = status in ("EXECUTED", "FILLED", "COMPLETE", "COMPLETED")

        # "Bought" is a claim about what you now own. A market order placed while the
        # exchange is closed sits PENDING until the next open and has bought nothing —
        # so an unfilled order is described as SUBMITTED, not as a purchase.
        action = "Bought" if filled.get("action") == "BUY" else "Sold"
        verb = action if settled else f"Submitted: {'buy' if filled.get('action') == 'BUY' else 'sell'}"
        parts = [verb]

        units = filled.get("units")
        parts.append(f"{units:g} shares of" if isinstance(units, (int, float)) else "shares of")
        symbol = str(filled.get("symbol") or "the symbol")
        # Name the company, not just the ticker. SnapTrade's symbol search ranks
        # "Apple Hospitality REIT" above "Apple Inc.", so a wrong ticker is a live
        # possibility — and "AZQ (Amazon.com CDR)" looks wrong in a way "AZQ" doesn't.
        company = filled.get("description")
        parts.append(f"{symbol} ({company})" if company and company != symbol else symbol)

        price = filled.get("price")
        if isinstance(price, (int, float)):
            parts.append(f"at about ${price:,.2f}")
        elif isinstance(filled.get("estimated_cost"), (int, float)):
            parts.append(f"for about ${filled['estimated_cost']:,.2f}")

        # Say WHERE it went. With several accounts, "Bought 5 NVDA" leaves out the one
        # detail the user needs to notice a mistake.
        account = filled.get("account_label")
        if account:
            parts.append(f"in {account}")

        line = " ".join(parts) + "."
        if settled:
            return line
        return (
            f"{line} Still {status.lower() or 'pending'} — it hasn't filled yet. "
            "Market orders placed while the exchange is closed fill at the next open."
        )
    except Exception:  # belt and braces: a wording bug is not a trading failure
        return "Order submitted. Check your brokerage for the fill."


class _StatusTarget(NSObject):
    """Receives clicks on the menubar button.

    rumps attaches a menu to the status item, and a status item WITH a menu never
    sends its button's action — the click just opens the menu. So the menu is
    detached and re-attached only for a right-click, which is what makes a plain
    left-click able to start recording.
    """

    def initWithApp_(self, app):
        # objc.super, not Python's — an ObjC subclass initialiser has to go
        # through the ObjC init chain or the object comes back half-built.
        self = objc.super(_StatusTarget, self).init()
        self.app = app
        return self

    def click_(self, sender):
        event = AppKit.NSApp.currentEvent()
        right = event.type() == AppKit.NSEventTypeRightMouseUp or (
            event.modifierFlags() & AppKit.NSEventModifierFlagControl
        )
        if right:
            self.app.popup_menu()
        else:
            self.app.left_click()


def _show_context_menu(menu, event, view):
    AppKit.NSMenu.popUpContextMenu_withEvent_forView_(menu, event, view)


def _current_event():
    return AppKit.NSApp.currentEvent()


class Snappy(rumps.App):
    def __init__(self):
        super().__init__("Snappy", quit_button="Quit")
        self.recording = False
        self.trigger = None  # "hold" → release sends; "click" → silence sends
        self.panel_ready = False
        self.wired = False
        self.icon_state = None
        self.marks = {}  # status -> NSImage, loaded once
        self.target = None  # strong ref: PyObjC won't retain the click target
        # rumps installs its own Mach SIGINT hook inside run(). Replace it afterward;
        # that hook can leave a source-launched app alive after Ctrl+C.
        rumps.events.before_start.register(self.enable_terminal_quit)
        self.menu = ["Ask Snappy", "Show panel", None, "Sign in with SnapTrade", None]
        state.update(auth_mode=st.mode(), oauth_available=auth.signed_in())

        # The panel can't be built here: rumps hasn't created NSApplication yet,
        # and a window made before AppKit is ready never gets displayed. Build it
        # on the first timer tick, once the run loop is live.
        rumps.Timer(self.tick, 0.15).start()
        rumps.Timer(self.tick_level, 0.05).start()  # waveform wants ~20fps
        threading.Thread(target=self.refresh_portfolio, daemon=True).start()
        threading.Thread(target=self.keep_fresh, daemon=True).start()
        # Load Whisper off the main thread so launch is instant and the first
        # question doesn't pay for the model load either.
        threading.Thread(target=transcribe.warm, daemon=True).start()

    # --- setup, once the run loop exists -----------------------------------

    def wire(self):
        """Take over the status-item click, and start listening for ⌥."""
        ui.set_on_ask(self.ask_text)
        ui.set_on_trade(self.confirm_from_panel, self.cancel_from_panel)
        ui.set_on_account(self.account_from_panel)
        ui.set_on_signin(self.start_oauth_signin)
        ui.set_on_access(self.change_access)

        item = self._nsapp.nsstatusitem
        self.menu_ref = item.menu()  # keep it; we re-attach it for right-clicks
        item.setMenu_(None)

        self.target = _StatusTarget.alloc().initWithApp_(self)
        button = item.button()
        button.setTarget_(self.target)
        button.setAction_("click:")
        button.sendActionOn_(
            AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
        )

        trusted = hotkey.is_trusted()
        state.update(hotkey_ok=trusted)
        if trusted:
            hotkey.start(self.hold_start, self.hold_end)
        else:
            # Don't leave a dead key with no explanation — the panel says how to
            # fix it, and clicking the icon works regardless.
            print("⌥ hotkey OFF — Accessibility not granted. See the panel.")

    def popup_menu(self):
        item = self._nsapp.nsstatusitem
        # Show a real blocking context menu. Temporarily attaching the menu and then
        # immediately detaching it races menu selection, including the Quit item.
        _show_context_menu(self.menu_ref, _current_event(), item.button())

    def enable_terminal_quit(self):
        """Make Ctrl+C reliably terminate a source-launched menubar app."""
        signal.signal(signal.SIGINT, lambda *_: rumps.quit_application())

    # --- UI ----------------------------------------------------------------
    # AppKit must only be touched from the main thread. Worker threads mutate
    # `state`; these timers run on the main thread and push it into the panel.

    def tick(self, _):
        if not self.panel_ready:
            ui.create()
            self.panel_ready = True
            return
        if not self.wired:
            self.wire()
            self.wired = True

        self.set_icon(state.STATE["status"])

        # The menu item name follows whether we're actually signed in — same item,
        # same callback, just a different label. See MenuItem's docstring: the title
        # can change at runtime and the lookup key stays whatever it was at creation.
        item = self.menu.get("Sign in with SnapTrade")
        if item is not None:
            current_mode = state.STATE.get("auth_mode")
            if current_mode == "oauth":
                label = "Sign out of SnapTrade"
            elif state.STATE.get("oauth_available"):
                label = "Use read-only OAuth"
            else:
                label = "Sign in with SnapTrade"
            if item.title != label:
                item.title = label

        # Snappy used to OPEN THE MIC here, by itself, the moment a trade was
        # proposed. That one behaviour caused nearly every trading bug this app has
        # had: the mic that never closed (so the order aged out underneath it),
        # silence read as a cancellation (killing trades the user wanted), Snappy
        # recording its own read-back and refusing its own trade, and "I didn't catch
        # a yes" firing the instant the button appeared.
        #
        # It is gone. A confirmation card is not a reason to switch on someone's
        # microphone. There are three ways to say yes — the button, the text box, and
        # holding ⌥ and saying "confirm" — and all three are things the USER starts.

        # Don't touch the dirty flag until the page can actually receive the
        # update — building the snapshot clears it, and a push into a half-loaded
        # page silently vanishes, leaving the panel stuck on its placeholder.
        if ui.is_ready() and state.is_dirty():
            ui.push(self.view())

    def tick_level(self, _):
        if not self.recording:
            return
        ui.set_level(state.STATE["level"])
        # A held key sends on release. Every other recording — including the one
        # that listens for "confirm" — ends when you stop talking. Leaving "confirm"
        # out of this meant that mic never closed at all: it stayed open until the
        # order's 90s TTL quietly expired underneath it.
        if self.trigger in AUTOSTOP_TRIGGERS and audio.should_autostop():
            self.stop()

    def view(self):
        """State snapshot plus the bits only the panel cares about."""
        s = state.snapshot()
        positions = s["positions"]
        count = s.get("account_count") or 1
        where = f"{count} accounts" if count > 1 else "Alpaca Paper"

        if positions:
            sub = f"{len(positions)} holding{'s' if len(positions) > 1 else ''} · {where}"
        elif s["cash"]:
            sub = f"100% cash · {where}"
        else:
            sub = where

        # Shares the brokerage has filled that SnapTrade hasn't synced. Saying nothing
        # would leave the headline number quietly understated with no hint why.
        stale = s.get("unsynced") or []
        if stale:
            symbols = ", ".join(sorted({g["symbol"] for g in stale}))
            sub += f" · {symbols} filled, not yet synced"

        return {**s, "sub": sub}

    def _mark(self, status):
        """The Snappy mark for a status, loaded once and kept."""
        if status in self.marks:
            return self.marks[status]

        path = os.path.join(ASSETS, MARKS.get(status, MARKS["idle"]))
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
        if image is not None:
            # The file is 36px — the @2x rep. Declaring it 18pt tells macOS this is a
            # retina image for an 18pt slot, so it uses the full resolution instead of
            # drawing a 36pt icon that overflows the menubar.
            image.setSize_(AppKit.NSMakeSize(18, 18))
        else:
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                SYMBOLS.get(status, SYMBOLS["idle"]), "Snappy"
            )
        if image is not None:
            image.setTemplate_(True)  # let macOS tint it for the light/dark menubar

        self.marks[status] = image
        return image

    def set_icon(self, status):
        if status == self.icon_state:
            return
        self.icon_state = status
        image = self._mark(status)
        if image is None:  # nothing to draw — keep the text title rather than vanish
            return
        button = self._nsapp.nsstatusitem.button()
        button.setImage_(image)
        button.setTitle_("")

    def refresh_portfolio(self):
        """The headline number: net worth across EVERY account, not just the first one.

        This used to call get_portfolio_summary() with no account, which quietly means
        "the first account". So the panel showed $100,000 for someone whose net worth was
        $150,000 across two brokerages — a multi-brokerage app displaying a single
        brokerage, which is the exact failure the whole project claims to fix.
        """
        if st.mode() is None:
            return
        try:
            book = st.get_all_holdings()
            connections = st.list_connections()
            by_connection = {c["connection_id"]: c for c in connections}
            state.update(
                total_value=book["net_worth"],
                cash=book["total_cash"],
                holdings_value=book["total_holdings_value"],
                positions=book["combined_holdings"],
                account_count=book["account_count"],
                # Per-account contents, for the expandable Accounts card. The combined
                # view answers "what do I own"; this one answers "and where is it".
                accounts=[
                    {
                        "account_id": a["account_id"],
                        "connection_id": a.get("connection_id"),
                        "connection_type": (
                            by_connection.get(a.get("connection_id"), {}).get("type") or "read"
                        ),
                        "label": a["label"],
                        "cash": a.get("cash"),
                        "holdings_value": a.get("holdings_value"),
                        "total_value": (a.get("total_value") or {}).get("amount"),
                        "positions": a.get("positions") or [],
                        "unsynced_fills": a.get("unsynced_fills") or [],
                        "error": a.get("error"),
                    }
                    for a in book["accounts"]
                ],
                connections=connections,
                connection_health=[
                    {key: value for key, value in c.items() if key != "created"}
                    for c in connections
                ],
                unsynced=[
                    gap
                    for a in book["accounts"]
                    for gap in (a.get("unsynced_fills") or [])
                ],
                # The panel counts up from this, so "live" is something the user can
                # SEE rather than something we assert. A number that never moves and
                # carries no timestamp is indistinguishable from a number that is stale.
                updated_at=time.time(),
            )
            # Prime the transcriber with the tickers you actually hold. Whisper guesses
            # from context, so knowing you own NVDA is the difference between hearing
            # "Nvidia" and hearing "and video".
            transcribe.set_hints([p["symbol"] for p in book["combined_holdings"]])
        except st.NoAccountsError:
            return
        except auth.OAuthClientRestricted as e:
            # A browser consent page is not proof of API access. Dynamic registrations
            # can mint MCP-only tokens that direct REST endpoints reject with code 1083.
            auth.sign_out()
            st.connect()
            state.update(
                signing_in=False, auth_mode=st.mode(), oauth_available=False,
                total_value=None, cash=None, holdings_value=None,
                positions=[], accounts=[], connections=[],
                notice=str(e),
            )
            print("portfolio refresh failed:", e)
        except Exception as e:
            print("portfolio refresh failed:", e)

    def keep_fresh(self):
        """Re-read the portfolio every 30s, so the panel isn't quoting a stale number.

        It used to refresh only at startup, after an answer, and after a trade — so the
        balance on screen could be an hour old and look perfectly current. A number with
        no timestamp that never changes is indistinguishable from a number that is right.

        This thread also EATS SNAPTRADE'S LATENCY so the user never does. st.prime()
        force-refreshes every account concurrently; SnapTrade randomly stalls a single
        account for 23-28 seconds (see snaptrade_client_wrapper.prime), and that stall
        used to land in the middle of a question — Claude asked for the portfolio, and
        you sat watching a spinner for half a minute. Now it lands here, on a background
        thread, where nobody is waiting for it.

        Primed FIRST, then read: refresh_portfolio() then hits a warm cache and returns
        immediately, so the panel and the model both get fresh data without paying for it.
        """
        while True:
            try:
                st.prime()
            except Exception as e:
                # A failed prime is not worth taking the refresher down for — the cache
                # keeps its last good values and the next tick tries again.
                print("prime failed:", e)
            self.refresh_portfolio()
            # Sleep LAST, not first. Sleeping first left the cache cold for the opening
            # 30 seconds — precisely when someone launches the app and asks it something.
            time.sleep(30)

    # --- triggers ----------------------------------------------------------

    def left_click(self):
        """Click the icon: show or hide the panel. It never opens the mic.

        Recording is always a deliberate act — hold ⌥, or pick "Ask Snappy" from
        the right-click menu. A click that silently starts listening is a nasty
        surprise, and more so now that Snappy can place trades.
        """
        if ui.is_visible():
            ui.hide()
        else:
            ui.show()

    def hold_start(self):
        if not self.recording:
            self.start("hold")

    def hold_end(self, was_tap):
        if not self.recording:
            return
        if was_tap:
            # Too short to be speech. They want hands-free, not a 200ms clip —
            # leave the mic open and let silence end it, same as a click.
            self.trigger = "click"
        else:
            self.stop()

    @rumps.clicked("Ask Snappy")
    def menu_ask(self, _):
        self.stop() if self.recording else self.start("click")

    @rumps.clicked("Show panel")
    def show_panel(self, _):
        ui.show()

    @rumps.clicked("Sign in with SnapTrade")
    def toggle_snaptrade_auth(self, _):
        if st.mode() == "oauth":
            self.do_signout()
        elif auth.signed_in():
            access.set_preferred_mode("oauth")
            st.connect(force="oauth")
            state.update(auth_mode="oauth", oauth_available=True)
            notify("Using read-only OAuth.")
            threading.Thread(target=self.refresh_portfolio, daemon=True).start()
        else:
            self.start_oauth_signin()

    @rumps.clicked("Connect Snappy service")
    def connect_hosted_service(self, _):
        if not config.BACKEND_URL:
            notify("This build has no hosted service configured.")
            return
        if backend_client.signed_in():
            backend_client.sign_out()
            notify("Disconnected from the Snappy service.")
            return
        response = rumps.Window(
            title="Connect Snappy",
            message="Enter your one-time invite code.",
            default_text="",
            ok="Connect",
            cancel="Cancel",
        ).run()
        if response.clicked and response.text.strip():
            threading.Thread(
                target=self._redeem_backend_invite,
                args=(response.text.strip(),),
                daemon=True,
            ).start()

    def _redeem_backend_invite(self, code):
        try:
            backend_client.redeem_invite(code)
        except Exception as exc:
            print("ERROR connecting hosted service:", exc)
            notify(str(exc))
            return
        notify("Connected to the Snappy service.")

    # --- SnapTrade sign-in ---------------------------------------------------
    # One click, in the panel or the menu, either drives the same flow: open the
    # browser, wait for consent, come back signed in. No key is ever typed here.

    def start_oauth_signin(self):
        if state.STATE.get("signing_in"):
            return
        state.update(signing_in=True, notice="Opening your browser to sign in with SnapTrade…")
        ui.show()
        threading.Thread(target=self._do_signin, daemon=True).start()

    def _do_signin(self):
        try:
            auth.sign_in()
            # Validate the bearer token against the API before claiming success. The
            # OAuth token endpoint also serves MCP-only clients, whose tokens cannot
            # read /accounts or /authorizations directly.
            st.connect(force="oauth")
            st.list_connections()
        except auth.OAuthClientRestricted as e:
            auth.sign_out()
            st.connect()
            state.update(
                signing_in=False, auth_mode=st.mode(), oauth_available=False,
                notice=str(e),
            )
            notify(str(e))
            return
        except auth.AuthError as e:
            state.update(signing_in=False)
            notify(f"Sign-in failed: {e}")
            return
        except Exception as e:
            print("ERROR signing in:", e)
            state.update(signing_in=False)
            notify("Sign-in failed — see the terminal for details.")
            return

        access.set_preferred_mode("oauth")
        st.connect(force="oauth")
        state.update(signing_in=False, auth_mode=st.mode(), oauth_available=True)
        notify("Connected to SnapTrade.")
        self.refresh_portfolio()

    def do_signout(self):
        auth.sign_out()
        st.connect()
        state.update(
            auth_mode=st.mode(),
            oauth_available=False,
            total_value=None, cash=None, holdings_value=None,
            positions=[], accounts=[], account_count=1,
        )
        notify("Signed out of SnapTrade.")

    # --- brokerage permissions --------------------------------------------

    def change_access(self, target, connection_id):
        """Open SnapTrade's reauthorization flow for this exact connection."""
        if state.STATE.get("permission_changing"):
            return

        # Permission changes must use keys explicitly supplied for the currently signed-in
        # Personal account. Development keys from `.env` may belong to a different account
        # and must never be silently paired with an OAuth connection ID.
        if target == "trade" and not access.saved_personal_keys():
            if state.STATE.get("key_setup_connection") != connection_id:
                webbrowser.open(SNAPTRADE_DASHBOARD)
                state.update(
                    key_setup_connection=connection_id,
                    notice=(
                        "SnapTrade opened. Create or view your Personal API key, "
                        "then return and click Continue setup."
                    ),
                )
                return
            client = rumps.Window(
                title="Enable full permission",
                message=(
                    "SnapTrade OAuth is read-only. Enter your Personal Client ID; "
                    "it will be stored in the macOS Keychain."
                ),
                ok="Next", cancel="Cancel", dimensions=(360, 24),
            ).run()
            if not client.clicked or not client.text.strip():
                return
            secret = rumps.Window(
                title="Consumer Key",
                message="Enter your Personal Consumer Key. It is stored only in Keychain.",
                ok="Continue", cancel="Cancel", dimensions=(360, 24), secure=True,
            ).run()
            if not secret.clicked or not secret.text.strip():
                return
            try:
                access.save_personal_keys(client.text, secret.text)
                state.update(key_setup_connection=None)
            except Exception as exc:
                notify(f"Couldn't save SnapTrade credentials: {exc}")
                return

        previous = st.mode()
        trading.cancel()
        state.update(
            permission_changing=connection_id,
            notice="Opening SnapTrade to change this connection's permission…",
        )
        threading.Thread(
            target=self._change_access,
            args=(target, connection_id, previous),
            daemon=True,
        ).start()

    def _change_access(self, target, connection_id, previous):
        try:
            # Signed portal requests require the Personal key even when the requested
            # end state is read-only. OAuth cannot create or modify connections.
            access.set_preferred_mode("keys")
            st.connect(force="keys")
            current = next(
                (c for c in st.list_connections() if c["connection_id"] == connection_id),
                None,
            )
            if current is None:
                access.forget_personal_keys()
                state.update(key_setup_connection=None)
                raise RuntimeError(
                    "Those Personal API keys don't match the signed-in SnapTrade account. "
                    "Click Read only again and enter keys from this account."
                )

            if (current.get("type") or "read").lower() != target:
                webbrowser.open(st.connection_permission_url(connection_id, target))
                deadline = time.time() + 300
                while time.time() < deadline:
                    time.sleep(5)
                    current = next(
                        (c for c in st.list_connections()
                         if c["connection_id"] == connection_id),
                        None,
                    )
                    if current and (current.get("type") or "read").lower() == target:
                        break
                else:
                    raise RuntimeError("Permission change timed out; complete it and try again.")

            if target == "read" and auth.signed_in():
                access.set_preferred_mode("oauth")
                st.connect(force="oauth")
            state.update(
                auth_mode=st.mode(), oauth_available=auth.signed_in(),
                permission_changing=None,
                notice=("Full permission enabled." if target == "trade"
                        else "Read-only permission enabled."),
            )
            self.refresh_portfolio()
        except Exception as exc:
            print("ERROR changing permission:", exc)
            if previous in ("oauth", "keys"):
                try:
                    access.set_preferred_mode(previous)
                    st.connect(force=previous)
                except Exception:
                    pass
            state.update(
                permission_changing=None, auth_mode=st.mode(),
                oauth_available=auth.signed_in(),
            )
            notify(f"Couldn't change permission: {exc}")

    # --- recording ---------------------------------------------------------

    def start(self, trigger):
        try:
            audio.start_recording()
        except Exception as e:
            print("ERROR starting mic:", e)
            notify("I couldn't access the microphone. Check System Settings.")
            return
        self.recording = True
        self.trigger = trigger
        # Clearing state would wipe a pending order right off the screen, and the
        # thing the user is most likely to say into an open mic while a confirmation
        # card is up is "confirm".
        if not trading.pending():
            state.start_question()
        state.update(status="listening")
        ui.show()

    def stop(self):
        self.recording = False
        self.trigger = None
        state.update(status="thinking", level=0.0)

        heard = audio.heard_speech()
        wav = audio.stop_recording()
        if wav is None or not heard:
            # Silence is neither consent nor cancellation. If an order is on the
            # table it STAYS on the table — say nothing, place nothing, destroy
            # nothing.
            state.update(status="confirming" if trading.pending() else "idle")
            if not trading.pending():
                notify("I didn't hear anything.")
            return

        threading.Thread(target=self.run_voice, args=(wav,), daemon=True).start()

    def ask_text(self, question):
        """A question typed into the panel — no mic, no Whisper.

        If an order is waiting, a typed "confirm" or "cancel" is an ANSWER to it, not
        a new question. Sending it to Claude instead was a real bug: the model has no
        tool that can place or cancel an order and is deliberately never told one is
        pending, so it replied "I don't have a pending trade proposal" — while the
        confirm card sat right there on screen. The voice path and the buttons both
        route through resolve_trade; this door was the one left unwired.

        Only an unambiguous yes/no is intercepted. "What's the risk?" is still a
        question, and anything unclear leaves the order standing rather than killing
        a trade the user wanted.
        """
        if self.recording:
            self.stop()

        if answers_pending_order(question):
            state.update(question=question)   # don't wipe the order off the screen
        else:
            state.start_question(question)
            state.update(status="thinking")
            ui.show()

        threading.Thread(target=self.route, args=(question,), daemon=True).start()

    # --- answering (worker thread — never touches AppKit) -------------------

    def route(self, text):
        """Is this an ANSWER to the order on the table, or a new question?

        The single fork for everything the user says or types. Spoken and typed input
        used to take different paths here, and each path had to remember the gate
        independently — the typed one forgot, and "confirm" went to a model that has
        no tool to place an order and is deliberately never told one is pending.
        One fork, so there is only one thing to get right.
        """
        if answers_pending_order(text):
            self.resolve_trade(trading.is_confirmation(text), text)
            return
        self.answer(text)

    def run_voice(self, wav):
        # No vocabulary priming while an order is pending. The usual prompt ends
        # "...or say confirm or cancel", and Whisper parrots its prompt when the audio
        # is mostly silence — which would forge the one word that moves money.
        priming = "" if trading.pending() else None
        try:
            question = transcribe.transcribe(wav, prompt=priming)
        except Exception as e:
            print("ERROR transcribing:", e)
            return
        print(f"heard: {question!r}")
        state.update(question=question)
        self.route(question)

    def answer(self, question):
        try:
            reply = answer(
                question,
                on_text=state.append_answer,
                # Claude narrates before calling tools ("let me look that up").
                # That isn't the answer, so drop it from the panel...
                on_reset=lambda: state.update(answer=""),
            )
            print(f"reply: {reply!r}")
        except Exception as e:
            print("ERROR:", e)
            reply = "Sorry, something went wrong."
            state.update(answer=reply)

        state.update(status="answered")
        self.refresh_portfolio()

        # Claude may have asked to trade without saying WHERE. It is not allowed to
        # choose, and neither are we — the panel offers the accounts and the user
        # clicks one. A click is the one input that cannot be mis-heard.
        choice = trading.choosing()
        if choice:
            state.update(choose=choice, status="choosing")
            return

        # Claude may have PROPOSED a trade. It cannot place one. Show the card and
        # WAIT — the user decides when to answer, and how. Snappy does not open the
        # microphone to hurry them along.
        order = trading.pending()
        if order:
            state.update(pending=order, status="confirming")
            return

        state.finish_question()
        state.update(status="idle")

    # --- confirming a trade -------------------------------------------------
    # The model is not involved in any of this. It proposed an order; whether that
    # order executes is decided here, by a regex, from what the user actually said.

    def resolve_trade(self, confirmed, heard=""):
        """Place the pending order, or don't. Called from a worker or the bridge.

        Three outcomes, not two. A clear yes places it; a clear no cancels it; and
        ANYTHING ELSE — silence, a garbled transcript, Snappy hearing its own voice
        — leaves the order standing. Treating "unclear" as "cancel" destroyed trades
        the user actually wanted, including while they reached for the Confirm button.
        """
        if not confirmed and not trading.is_cancellation(heard):
            state.update(status="confirming")
            notify("Still waiting — say “confirm”, or use the button.")
            return

        if not confirmed:
            trading.cancel()
            state.update(pending=None, status="answered")
            notify("Cancelled — nothing was placed.")
        else:
            try:
                filled = trading.confirm()
            except trading.TradeRefused as e:
                # A guard said no. The guards all run BEFORE the order is sent, so
                # this genuinely means nothing was placed.
                state.update(pending=None, status="answered")
                notify(str(e))
            except Exception as e:
                # The order was sent and something went wrong. We do NOT know whether
                # it reached the brokerage, so we must not say it didn't — this
                # handler once announced "Nothing was placed" about an order that had
                # in fact filled. Never assert a fact about someone's money that you
                # have not checked.
                print("ERROR placing order:", e)
                state.update(pending=None, status="answered")
                notify(
                    "I couldn't read back the result of that order. It may have gone "
                    "through — check your brokerage before trying again."
                )
            else:
                # Clear the card. Every FAILURE path above cleared it and the success
                # path didn't, so a trade that actually worked left its own confirm
                # button sitting on screen — looking exactly like nothing had happened,
                # and inviting the user to press it again.
                state.update(pending=None, status="answered")
                notify(describe_fill(filled))

        self.refresh_portfolio()
        state.finish_question()
        state.update(status="idle")

    def abort_recording(self):
        """Close the mic WITHOUT resolving anything.

        The button and the voice path both end in resolve_trade, so a click while
        the mic is still listening must not also fire the voice path — that would
        resolve the same order twice, racing a "confirm" against an "I'll wait".
        """
        if not self.recording:
            return
        self.recording = False
        self.trigger = None
        audio.stop_recording()
        state.update(level=0.0)

    def confirm_from_panel(self):
        print(f"confirm button: pending={trading.pending() is not None}")
        self.abort_recording()
        threading.Thread(
            target=self.resolve_trade, args=(True, "confirm"), daemon=True
        ).start()

    def cancel_from_panel(self):
        print(f"cancel button: pending={trading.pending() is not None}")
        self.abort_recording()
        trading.clear_choice()          # dismissing also drops an unpicked account
        state.update(choose=None)
        threading.Thread(
            target=self.resolve_trade, args=(False, "cancel"), daemon=True
        ).start()

    def account_from_panel(self, account_id):
        """The user picked an account. Price the trade in it and show the confirm card.

        This is a second gate, not a shortcut past the first: choose_account() runs the
        full guard chain, and the order still has to be confirmed afterwards. Picking
        WHERE is not the same as saying YES.
        """
        print(f"account picked: {account_id}")
        self.abort_recording()
        state.update(choose=None, status="thinking")
        threading.Thread(target=self._price_in, args=(account_id,), daemon=True).start()

    def _price_in(self, account_id):
        try:
            order = trading.choose_account(account_id)
        except (trading.TradeRefused, st.AmbiguousAccount) as e:
            state.update(status="answered")
            notify(str(e))
            return
        except Exception as e:
            print("ERROR pricing the order:", e)
            state.update(status="answered")
            notify("I couldn't price that order. Nothing was placed.")
            return
        state.update(pending=order, status="confirming")


def _build():
    """Which commit is actually running.

    Python doesn't hot-reload, so an app left open after an edit is running the old
    code — which has now sent us chasing three bugs that were already fixed. Print
    it, and stop guessing.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=ROOT,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=ROOT,
        ).stdout.strip()
        return f"{sha}{'+dirty' if dirty else ''}" if sha else "packaged"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    if not acquire_instance_lock():
        print("Snappy is already running. Quit the existing copy first.")
        raise SystemExit(1)
    print(f"Snappy — build {_build()}, model {config.CLAUDE_MODEL}")
    print("Hold right ⌥ to talk. Right-click the icon to quit.\n")
    Snappy().run()
