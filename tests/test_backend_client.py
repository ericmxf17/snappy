import json

import backend_client


class FakeSocket:
    def __init__(self, events):
        self.events = [json.dumps(event) for event in events]
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def __iter__(self):
        return iter(self.events)

    def send(self, payload):
        self.sent.append(json.loads(payload))


def test_hosted_client_executes_requested_tool_locally(monkeypatch):
    socket = FakeSocket([
        {"type": "reset"},
        {"type": "tool_call", "id": "one", "name": "get_quote",
         "input": {"symbol": "AAPL"}},
        {"type": "text_delta", "text": "Apple is $123.45."},
        {"type": "done", "answer": "Apple is $123.45."},
    ])
    monkeypatch.setattr(backend_client, "_read_token", lambda: "session")
    monkeypatch.setattr(backend_client, "connect", lambda *a, **k: socket)
    monkeypatch.setattr(backend_client, "run_tool", lambda name, data: "$123.45")
    monkeypatch.setattr(backend_client.config, "BACKEND_URL", "https://agent.example")
    resets = []

    result = backend_client.answer("Apple price?", on_reset=lambda: resets.append(True))

    assert result == "Apple is $123.45."
    assert resets == [True]
    assert socket.sent[1] == {"type": "tool_result", "id": "one", "content": "$123.45"}


def test_https_backend_uses_secure_websocket():
    assert backend_client._websocket_url("https://agent.example/base") == (
        "wss://agent.example/v1/questions"
    )
