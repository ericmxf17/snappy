"""Shared state between the worker thread and the UI.

AppKit is not thread-safe, so the worker never touches the panel directly. It
mutates this dict; a timer on the main thread notices `dirty` and pushes an
update into the WebView.
"""

import threading

_lock = threading.Lock()

STATE = {
    "status": "idle",  # idle | listening | thinking | searching
    "level": 0.0,  # live mic amplitude, 0..1, drives the waveform
    "question": "",  # transcript of what was just asked
    "answer": "",  # grows as Claude streams
    "calls": [],  # tool calls for the question in flight
    "sources": [],  # web pages cited in the answer
    "history": [],  # newest first: {question, answer, calls, sources}
    "total_value": None,
    "cash": None,
    "positions": [],
    "hotkey_ok": True,  # False → the panel explains how to grant Accessibility
    "dirty": True,
}


def update(**kwargs):
    with _lock:
        STATE.update(kwargs)
        STATE["dirty"] = True


def set_level(level):
    # Deliberately does NOT set `dirty` — the waveform is pushed on its own fast
    # timer, and flagging every audio callback would redraw the whole panel at
    # the mic's callback rate.
    with _lock:
        STATE["level"] = level


def record_call(name, ms, detail=""):
    """Note a tool call so the panel can show what actually ran."""
    with _lock:
        STATE["calls"].append({"name": name, "ms": ms, "detail": detail})
        STATE["dirty"] = True


def append_answer(text):
    with _lock:
        STATE["answer"] += text
        STATE["dirty"] = True


def add_sources(sources):
    with _lock:
        seen = {s["url"] for s in STATE["sources"]}
        STATE["sources"].extend(s for s in sources if s["url"] not in seen)
        STATE["dirty"] = True


def start_question(question=""):
    with _lock:
        STATE.update(question=question, answer="", calls=[], sources=[], dirty=True)


def finish_question():
    with _lock:
        if STATE["question"] or STATE["answer"]:
            STATE["history"].insert(
                0,
                {
                    "question": STATE["question"],
                    "answer": STATE["answer"],
                    "calls": list(STATE["calls"]),
                    "sources": list(STATE["sources"]),
                },
            )
        STATE.update(question="", answer="", calls=[], sources=[], dirty=True)


def snapshot():
    """A copy for the UI thread, with the dirty flag cleared."""
    with _lock:
        STATE["dirty"] = False
        return {
            **STATE,
            "calls": list(STATE["calls"]),
            "sources": list(STATE["sources"]),
            "positions": list(STATE["positions"]),
            "history": list(STATE["history"]),
        }


def is_dirty():
    with _lock:
        return STATE["dirty"]
