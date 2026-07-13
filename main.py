"""Snappy — ask your brokerage account a question out loud.

Hold Right ⌥ and talk. It transcribes locally, asks Claude (which pulls live data
from SnapTrade and searches the web), and writes the answer into a floating glass
panel — the headline first, then the arithmetic, the sources, and the API trace.

You speak; it writes. There is deliberately no text-to-speech: a laptop mic beside a
laptop speaker is an echo path, and it cost us a string of bugs — including Snappy
recording its own voice and talking itself out of its own trade.

Run with:  ./venv/bin/python main.py
"""

import os
import subprocess
import threading

import AppKit
import objc
import rumps
from Foundation import NSObject

import audio
import config
import hotkey
import snaptrade_client_wrapper as st
import state
import trading
import transcribe
import ui
from assistant import answer

# SF Symbols render as template images: vector, monochrome, and they pick up the
# menubar's tint automatically — unlike an emoji, which looks pasted on.
SYMBOLS = {
    "idle": "waveform",
    "listening": "waveform.circle.fill",
    "thinking": "ellipsis.circle",
}

ACCESSIBILITY_PANE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

# Recordings that end when you stop talking. A held ⌥ is the exception — that one
# sends on release. "confirm" MUST be in here: it was left out once, and that mic
# then never closed at all, so every proposed order aged out under a misleading
# "that order expired".
AUTOSTOP_TRIGGERS = ("click", "confirm")



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


class Snappy(rumps.App):
    def __init__(self):
        super().__init__("Snappy", quit_button="Quit")
        self.recording = False
        self.trigger = None  # "hold" → release sends; "click" → silence sends
        self.panel_ready = False
        self.wired = False
        self.icon_state = None
        self.target = None  # strong ref: PyObjC won't retain the click target
        self.want_confirm = False  # a worker asks the main thread to reopen the mic

        self.menu = ["Ask Snappy", "Show panel", None]

        # The panel can't be built here: rumps hasn't created NSApplication yet,
        # and a window made before AppKit is ready never gets displayed. Build it
        # on the first timer tick, once the run loop is live.
        rumps.Timer(self.tick, 0.15).start()
        rumps.Timer(self.tick_level, 0.05).start()  # waveform wants ~20fps
        threading.Thread(target=self.refresh_portfolio, daemon=True).start()
        # Load Whisper off the main thread so launch is instant and the first
        # question doesn't pay for the model load either.
        threading.Thread(target=transcribe.warm, daemon=True).start()

    # --- setup, once the run loop exists -----------------------------------

    def wire(self):
        """Take over the status-item click, and start listening for ⌥."""
        ui.set_on_ask(self.ask_text)
        ui.set_on_trade(self.confirm_from_panel, self.cancel_from_panel)

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
        item.setMenu_(self.menu_ref)
        item.button().performClick_(None)
        item.setMenu_(None)  # back to click-to-talk

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

        # A worker proposed a trade and wants the mic reopened to hear a yes. Only
        # the main thread may do that — starting a recording shows the panel.
        #
        if self.want_confirm and not self.recording:
            self.want_confirm = False
            self.start("confirm")

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
        if positions:
            sub = f"{len(positions)} holding{'s' if len(positions) > 1 else ''} · Alpaca Paper"
        elif s["cash"]:
            sub = "100% cash · Alpaca Paper"
        else:
            sub = "Alpaca Paper"
        return {**s, "sub": sub}

    def set_icon(self, status):
        if status == self.icon_state:
            return
        self.icon_state = status
        name = SYMBOLS.get(status, SYMBOLS["idle"])
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, "Snappy"
        )
        if image is None:  # symbol unavailable on this macOS — keep the text title
            return
        image.setTemplate_(True)
        button = self._nsapp.nsstatusitem.button()
        button.setImage_(image)
        button.setTitle_("")

    def refresh_portfolio(self):
        try:
            p = st.get_portfolio_summary()
            state.update(
                total_value=p["total_portfolio_value"],
                cash=p["cash"],
                positions=p["positions"],
            )
            # Prime the transcriber with the tickers you actually hold. Whisper
            # guesses from context, so knowing you own NVDA is the difference
            # between hearing "Nvidia" and hearing "and video".
            transcribe.set_hints([pos["symbol"] for pos in p["positions"]])
        except Exception as e:
            print("portfolio refresh failed:", e)

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
        # A confirmation isn't a new question — clearing state here would wipe the
        # order that's being confirmed right off the screen.
        if trigger != "confirm":
            state.start_question()
        state.update(status="listening")
        ui.show()

    def stop(self):
        was = self.trigger
        self.recording = False
        self.trigger = None
        state.update(status="thinking", level=0.0)

        heard = audio.heard_speech()
        wav = audio.stop_recording()
        if wav is None or not heard:
            state.update(status="idle")
            if was == "confirm":
                # Silence is not consent. Say nothing, place nothing.
                self.resolve_trade(confirmed=False, heard="")
            else:
                notify("I didn't hear anything.")
            return

        target = self.run_confirm if was == "confirm" else self.run_voice
        threading.Thread(target=target, args=(wav,), daemon=True).start()

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

        if trading.pending():
            if trading.is_confirmation(question) or trading.is_cancellation(question):
                state.update(question=question)
                threading.Thread(
                    target=self.resolve_trade,
                    args=(trading.is_confirmation(question), question),
                    daemon=True,
                ).start()
                return

        state.start_question(question)
        state.update(status="thinking")
        ui.show()
        threading.Thread(target=self.answer, args=(question,), daemon=True).start()

    # --- answering (worker thread — never touches AppKit) -------------------

    def run_voice(self, wav):
        try:
            question = transcribe.transcribe(wav)
        except Exception as e:
            print("ERROR transcribing:", e)
            return
        print(f"heard: {question!r}")
        state.update(question=question)
        self.answer(question)

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

        # Claude may have PROPOSED a trade. It cannot place one. If there's an order
        # waiting, reopen the mic and listen for a yes — but ask the main thread to
        # do it, because starting a recording touches the panel and AppKit is
        # main-thread only.
        order = trading.pending()
        if order:
            state.update(pending=order, status="confirming")
            self.want_confirm = True
            return

        state.finish_question()
        state.update(status="idle")

    # --- confirming a trade -------------------------------------------------
    # The model is not involved in any of this. It proposed an order; whether that
    # order executes is decided here, by a regex, from what the user actually said.

    def run_confirm(self, wav):
        try:
            # NO vocabulary prompt here. The usual one ends "...or say confirm or
            # cancel", and Whisper parrots its prompt when the audio is mostly
            # silence — which a confirmation window is. That hallucinated the very
            # words this step authorises on.
            said = transcribe.transcribe(wav, prompt="")
        except Exception as e:
            print("ERROR transcribing confirmation:", e)
            said = ""
        print(f"confirmation heard: {said!r}")
        self.resolve_trade(trading.is_confirmation(said), said)

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
        threading.Thread(
            target=self.resolve_trade, args=(False, "cancel"), daemon=True
        ).start()


def _build():
    """Which commit is actually running.

    Python doesn't hot-reload, so an app left open after an edit is running the old
    code — which has now sent us chasing three bugs that were already fixed. Print
    it, and stop guessing.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)),
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)),
        ).stdout.strip()
        return f"{sha}{'+dirty' if dirty else ''}"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    print(f"Snappy — build {_build()}, model {config.CLAUDE_MODEL}")
    print("Hold right ⌥ to talk. Right-click the icon to quit.\n")
    Snappy().run()
