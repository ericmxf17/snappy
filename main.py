"""Snappy — a voice assistant for your brokerage accounts.

Hold Right ⌥ and talk, or click the menubar icon and just stop talking. It
transcribes locally, asks Claude (which pulls live data from SnapTrade and
searches the web), speaks a short answer, and shows the reasoning in a floating
glass panel.

Run with:  ./venv/bin/python main.py
"""

import subprocess
import threading

import AppKit
import objc
import rumps
from Foundation import NSObject

import audio
import hotkey
import snaptrade_client_wrapper as st
import state
import transcribe
import ui
from assistant import answer, spoken_part

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


_speaking = None  # the `say` process currently talking, if any


def say_now(text):
    """Speak, and don't return until it's done."""
    _say(text).wait()


def say_soon(text):
    """Start speaking and return immediately — the search shouldn't wait on audio."""
    _say(text)


def _say(text):
    """Never talk over ourselves: the filler line has to finish before the answer."""
    global _speaking
    if _speaking and _speaking.poll() is None:
        _speaking.wait()
    _speaking = subprocess.Popen(["say", text])
    return _speaking


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

        self.menu = ["Ask Snappy", "Show panel", None]

        # The panel can't be built here: rumps hasn't created NSApplication yet,
        # and a window made before AppKit is ready never gets displayed. Build it
        # on the first timer tick, once the run loop is live.
        rumps.Timer(self.tick, 0.15).start()
        rumps.Timer(self.tick_level, 0.05).start()  # waveform wants ~20fps
        threading.Thread(target=self.refresh_portfolio, daemon=True).start()

    # --- setup, once the run loop exists -----------------------------------

    def wire(self):
        """Take over the status-item click, and start listening for ⌥."""
        ui.set_on_ask(self.ask_text)

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

        # Don't touch the dirty flag until the page can actually receive the
        # update — building the snapshot clears it, and a push into a half-loaded
        # page silently vanishes, leaving the panel stuck on its placeholder.
        if ui.is_ready() and state.is_dirty():
            ui.push(self.view())

    def tick_level(self, _):
        if not self.recording:
            return
        ui.set_level(state.STATE["level"])
        # A held key sends on release; a click-started recording ends when you do.
        if self.trigger == "click" and audio.should_autostop():
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
        """Click the icon: stop if recording, otherwise start."""
        if self.recording:
            self.stop()
        elif state.STATE["status"] == "thinking":
            ui.show()  # already working — just bring the panel back
        else:
            self.start("click")

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
            say_now("I couldn't access the microphone. Check System Settings.")
            return
        self.recording = True
        self.trigger = trigger
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
            state.update(status="idle")
            say_now("I didn't hear anything.")
            return

        threading.Thread(target=self.run_voice, args=(wav,), daemon=True).start()

    def ask_text(self, question):
        """A question typed into the panel — no mic, no Whisper."""
        if self.recording:
            self.stop()
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
                # ...but say it out loud. A researched answer takes ten seconds or
                # more, and without this the user is listening to dead air the whole
                # time, wondering whether it heard them at all.
                on_narration=self.say_soon,
            )
            print(f"reply: {reply!r}")
        except Exception as e:
            print("ERROR:", e)
            reply = "Sorry, something went wrong."
            state.update(answer=reply)

        state.update(status="answered")
        self.refresh_portfolio()

        say_now(spoken_part(reply))  # only the first paragraph is meant to be heard

        state.finish_question()
        state.update(status="idle")

    def say_soon(self, text):
        """Speak filler without blocking the search that's already running."""
        threading.Thread(target=speak, args=(text,), daemon=True).start()


if __name__ == "__main__":
    Snappy().run()
