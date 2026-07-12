"""The ⌥ key's tap-vs-hold rule, and the threading contract the UI depends on."""

import threading

import hotkey
import state


class Event:
    """A fake NSEvent carrying just the modifier bits."""

    def __init__(self, right_option):
        self._flags = hotkey.RIGHT_OPTION if right_option else 0

    def modifierFlags(self):
        return self._flags


def wire(monkeypatch, clock):
    """Reset the module and capture what it calls."""
    calls = []
    monkeypatch.setattr(hotkey, "_held", False)
    monkeypatch.setattr(hotkey, "_pressed_at", 0.0)
    monkeypatch.setattr(hotkey, "_on_press", lambda: calls.append("press"))
    monkeypatch.setattr(hotkey, "_on_release", lambda tap: calls.append(f"release:{tap}"))
    monkeypatch.setattr(hotkey.time, "monotonic", lambda: clock[0])
    return calls


def test_holding_the_key_is_push_to_talk(monkeypatch):
    clock = [0.0]
    calls = wire(monkeypatch, clock)

    hotkey._handle(Event(True))
    clock[0] = 3.0                       # spoke for three seconds
    hotkey._handle(Event(False))

    assert calls == ["press", "release:False"]   # False = a hold, so send now


def test_tapping_the_key_is_hands_free(monkeypatch):
    """A tap would otherwise record ~100ms of nothing and be thrown away as
    "I didn't hear anything" — a baffling failure. A tap means: leave the mic
    open and let silence end it."""
    clock = [0.0]
    calls = wire(monkeypatch, clock)

    hotkey._handle(Event(True))
    clock[0] = 0.1                       # a quick tap
    hotkey._handle(Event(False))

    assert calls == ["press", "release:True"]    # True = a tap


def test_the_tap_hold_boundary(monkeypatch):
    clock = [0.0]
    calls = wire(monkeypatch, clock)

    hotkey._handle(Event(True))
    clock[0] = hotkey.TAP_SECONDS + 0.01
    hotkey._handle(Event(False))

    assert calls[-1] == "release:False"


def test_other_modifiers_are_ignored(monkeypatch):
    """Shift, Command, and LEFT Option must not start a recording — otherwise
    every ⌘C in another app would fire the mic."""
    clock = [0.0]
    calls = wire(monkeypatch, clock)

    hotkey._handle(Event(False))         # some other modifier changed
    assert calls == []


def test_key_repeat_does_not_start_two_recordings(monkeypatch):
    """macOS sends repeated flagsChanged events while a key is held."""
    clock = [0.0]
    calls = wire(monkeypatch, clock)

    hotkey._handle(Event(True))
    hotkey._handle(Event(True))          # still down
    hotkey._handle(Event(True))

    assert calls == ["press"]            # exactly one


# --- the threading contract ------------------------------------------------
# Workers mutate state; a main-thread timer reads it. Every threading bug in this
# app came from breaking that. state must be safe under concurrent writers.

def test_state_survives_concurrent_writers(state_reset):
    def worker(n):
        for i in range(200):
            state.record_call(f"tool{n}", i)
            state.append_answer("x")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(state.STATE["calls"]) == 8 * 200
    assert len(state.STATE["answer"]) == 8 * 200


def test_snapshot_never_tears_under_a_writer(state_reset):
    """The UI thread must never see a half-written list.

    The writer is bounded: an unbounded one grows `calls` without limit while the
    reader copies the whole list each time, which is quadratic and runs forever.
    (I wrote that version first. It hung.)
    """
    def writer():
        for _ in range(500):
            state.record_call("get_quote", 1)

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(200):
            snap = state.snapshot()
            # A torn read would show up as a non-dict entry or a missing key.
            assert all(isinstance(c, dict) and "name" in c for c in snap["calls"])
    finally:
        t.join()
