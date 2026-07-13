"""Bugs that actually happened. Each of these shipped once.

They're grouped here on purpose: every one was silent — nothing crashed, nothing
logged, the app just did the wrong thing convincingly.
"""

import re

import pytest

import state
import tools
import transcribe


# --- Whisper's prompt bled into the transcript -----------------------------
# Passing the vocabulary as a comma-separated keyword list made Whisper parrot it
# straight back: "buy five shares of Apple" came out as "buy, buy, buy, shares, of
# Apple." The prompt has to read as prose.

def test_hint_prompt_is_prose_not_a_keyword_list():
    transcribe.set_hints(["AAPL", "NVDA"])
    prompt = transcribe._prompt

    assert prompt.endswith("."), "must be a sentence — Whisper parrots back a list"
    assert ", ," not in prompt
    # A keyword list is mostly commas. Prose is mostly words.
    assert prompt.count(",") < len(prompt.split()) / 3


def test_hints_include_the_tickers_you_hold():
    transcribe.set_hints(["NVDA", "TSLA"])
    assert "NVDA" in transcribe._prompt
    assert "TSLA" in transcribe._prompt


def test_no_holdings_still_produces_a_valid_prompt():
    transcribe.set_hints([])
    assert transcribe._prompt == transcribe.CONTEXT
    assert "buy or sell" in transcribe._prompt   # the buy/by fix must survive


def test_snappy_has_no_voice():
    """Text-to-speech is gone, and it must stay gone.

    A laptop mic sitting beside a laptop speaker is an echo path. It cost a string
    of bugs — the worst being the confirmation mic recording Snappy's OWN read-back,
    transcribing "say confirm to place the trade", deciding that wasn't a yes, and
    talking Snappy out of its own trade. If `say` comes back, so do those.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "src"
    # transcribe.py is exempt: its self-test uses `say` to GENERATE a test WAV.
    # That's input, not output.
    for name in ("main.py", "assistant.py", "trading.py", "ui.py", "tools.py"):
        source = (src / name).read_text()
        # This test greps SOURCE, so a wrong path would make it pass by finding
        # nothing rather than fail. Prove the file is actually there.
        assert source, f"{name} not found under src/ — this test was checking nothing"
        assert '["say"' not in source, f"{name} shells out to `say`"
        assert "['say'" not in source, f"{name} shells out to `say`"


def test_transcribe_can_be_asked_for_no_prompt_at_all():
    """The confirmation step MUST be able to transcribe with no vocabulary priming.

    Whisper parrots its prompt back when the audio is mostly silence — and a
    confirmation window is mostly silence. The normal prompt ends "...or say
    confirm or cancel", i.e. the exact words the trade authorisation matches on.
    A hallucinated "cancel" silently killed orders; a hallucinated "confirm" would
    have placed one nobody asked for.
    """
    import inspect
    sig = inspect.signature(transcribe.transcribe)
    assert "prompt" in sig.parameters, "run_confirm relies on passing prompt=''"


def test_prompt_primes_the_trading_verbs():
    """"buy" and "by" are homophones. Without priming, Whisper drops the verb:
    "buy five shares of Apple" -> "BY five shares of Apple". A trade command
    silently losing its verb is not an acceptable failure."""
    transcribe.set_hints([])
    assert re.search(r"\bbuy\b", transcribe._prompt)
    assert re.search(r"\bsell\b", transcribe._prompt)


# --- A tool error crashed the answer --------------------------------------
# run_tool must return errors as TEXT, so Claude can explain them out loud, rather
# than raising and killing the whole question.

def test_tool_errors_come_back_as_text(state_reset):
    def boom():
        raise RuntimeError("brokerage connection expired")

    tools.DISPATCH["_boom"] = boom
    try:
        result = tools.run_tool("_boom", {})
    finally:
        del tools.DISPATCH["_boom"]

    assert isinstance(result, str)
    assert "brokerage connection expired" in result


def test_a_failed_tool_still_shows_up_in_the_trace(state_reset):
    """The panel's trace is how you see what happened. A failure must appear."""
    tools.DISPATCH["_boom"] = lambda: (_ for _ in ()).throw(ValueError("nope"))
    try:
        tools.run_tool("_boom", {})
    finally:
        del tools.DISPATCH["_boom"]

    assert [c["name"] for c in state.STATE["calls"]] == ["_boom"]


# --- The panel got stuck on "connecting…" ---------------------------------
# snapshot() clears the dirty flag. If the UI builds a snapshot before the page can
# receive it, the update vanishes and is never retried — the panel sits on its
# placeholder forever.

def test_snapshot_clears_dirty(state_reset):
    state.update(status="listening")
    assert state.is_dirty()

    state.snapshot()
    assert not state.is_dirty(), "snapshot must clear dirty, or the UI spins"


def test_snapshot_is_a_copy_not_a_live_reference(state_reset):
    """The UI thread reads the snapshot while workers keep mutating STATE. If the
    snapshot shared a list, the panel could render a half-written update."""
    state.record_call("get_quote", 12)
    snap = state.snapshot()

    state.record_call("web_search", 900)          # worker keeps going
    assert len(snap["calls"]) == 1, "snapshot must not see later mutations"


def test_mic_level_does_not_mark_state_dirty(state_reset):
    """The waveform has its own 20fps push. If set_level dirtied the state, every
    audio callback would trigger a full panel re-render."""
    state.snapshot()                              # clear
    state.set_level(0.7)
    assert not state.is_dirty()
    assert state.STATE["level"] == 0.7


def test_a_new_question_clears_the_previous_answer(state_reset):
    """Narration and stale answers must not bleed into the next question."""
    state.update(question="old?", answer="old answer")
    state.record_call("get_quote", 5)

    state.start_question("new?")

    assert state.STATE["question"] == "new?"
    assert state.STATE["answer"] == ""
    assert state.STATE["calls"] == []


def test_finishing_a_question_files_it_into_history(state_reset):
    state.start_question("what's my balance?")
    state.append_answer("A hundred thousand.")
    state.finish_question()

    assert state.STATE["history"][0]["question"] == "what's my balance?"
    assert state.STATE["history"][0]["answer"] == "A hundred thousand."
    assert state.STATE["question"] == ""          # cleared for the next one


def test_the_drag_strip_does_not_override_hit_testing():
    """The drag strip must not define hitTest_. This killed EVERY click in the panel.

    The strip is the topmost view, so AppKit asks it about every click anywhere in
    the window. My override called isMousePoint_inRect_ — a method NSView does not
    have — so it raised inside an ObjC callback on every single hit test. Hit testing
    collapsed and nothing in the panel was clickable: not Confirm, not Dismiss, not
    the close box.

    NSView's DEFAULT hitTest_ already claims only points inside the view's own frame,
    which is exactly what a header drag handle wants. The override was never needed.
    """
    import ui
    assert "hitTest_" not in vars(ui._DragStrip), (
        "_DragStrip overrides hitTest_ again — that override took out every click "
        "in the panel the last time it existed"
    )
