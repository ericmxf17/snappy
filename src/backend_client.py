"""Thin client for the hosted agent; every brokerage tool still runs locally."""

import json
import subprocess
import urllib.parse

import requests
from websockets.sync.client import connect

import config
import state
from tools import TOOLS, run_tool

SERVICE = "Snappy"
ACCOUNT = "hosted-agent-session"


class BackendError(RuntimeError):
    pass


def _read_token():
    result = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _write_token(token):
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE,
         "-a", ACCOUNT, "-w", token],
        check=True, capture_output=True,
    )


def signed_in():
    return bool(_read_token())


def sign_out():
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT],
        capture_output=True,
    )


def redeem_invite(code):
    url = urllib.parse.urljoin(config.BACKEND_URL.rstrip("/") + "/", "v1/invites/redeem")
    response = requests.post(url, json={"code": code}, timeout=20)
    if not response.ok:
        raise BackendError("The Snappy invite is invalid or has already been used.")
    token = response.json()["token"]
    _write_token(token)
    return token


def _websocket_url(base):
    parsed = urllib.parse.urlparse(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunparse((scheme, parsed.netloc, "/v1/questions", "", "", ""))


def answer(question, on_text=None, on_reset=None):
    token = _read_token()
    if not token and config.BACKEND_INVITE:
        token = redeem_invite(config.BACKEND_INVITE)
    if not token:
        raise BackendError("Snappy needs a hosted-service invite before it can answer.")

    chunks = []
    with connect(
        _websocket_url(config.BACKEND_URL),
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=15, close_timeout=5,
    ) as socket:
        socket.send(json.dumps({"type": "start", "question": question, "tools": TOOLS}))
        for raw in socket:
            event = json.loads(raw)
            kind = event.get("type")
            if kind == "text_delta":
                chunks.append(event["text"])
                if on_text:
                    on_text(event["text"])
            elif kind == "reset":
                chunks.clear()
                if on_reset:
                    on_reset()
            elif kind == "tool_call":
                socket.send(json.dumps({
                    "type": "tool_result", "id": event["id"],
                    "content": run_tool(event["name"], event.get("input") or {}),
                }))
            elif kind == "trace":
                state.record_call(event["name"], event.get("milliseconds", 0),
                                  detail=event.get("detail"))
            elif kind == "sources":
                state.add_sources(event.get("items") or [])
            elif kind == "error":
                raise BackendError(event.get("message") or "Hosted agent failed.")
            elif kind == "done":
                return event.get("answer") or "".join(chunks).strip()
    raise BackendError("The hosted agent disconnected before answering.")
