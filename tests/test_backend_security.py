import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.agent import run_agent
from backend.app import create_app
from backend.auth import InviteStore, SessionTokens
from backend.limits import RateLimiter
from backend.security import ProtocolError, validate_start


SAFE_TOOL = {
    "name": "get_quote",
    "description": "Get a quote.",
    "input_schema": {"type": "object", "properties": {}},
}


def test_server_rejects_any_execution_tool():
    dangerous = {**SAFE_TOOL, "name": "place_trade"}
    with pytest.raises(ProtocolError, match="not allowed"):
        validate_start({"type": "start", "question": "buy it", "tools": [dangerous]})


def test_server_rejects_oversized_questions():
    with pytest.raises(ProtocolError, match="too long"):
        validate_start({"type": "start", "question": "x" * 8001, "tools": [SAFE_TOOL]})


def test_invites_are_one_time_and_sessions_are_signed(tmp_path):
    store = InviteStore(tmp_path / "invites.db")
    code = store.create()
    assert store.redeem(code)
    assert not store.redeem(code)

    sessions = SessionTokens("x" * 32)
    token = sessions.issue()
    assert sessions.verify(token)
    assert not sessions.verify(token + "tampered")


def test_rate_limit_caps_requests_and_concurrency():
    limiter = RateLimiter(requests_per_minute=2, concurrent=1)
    assert limiter.enter("session", now=0)
    assert not limiter.enter("session", now=1)
    limiter.exit("session")
    assert limiter.enter("session", now=2)
    limiter.exit("session")
    assert not limiter.enter("session", now=3)
    assert limiter.enter("session", now=61)


class FakeStream:
    def __init__(self, events, response):
        self.events = events
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def __aiter__(self):
        self._events = iter(self.events)
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration

    async def get_final_message(self):
        return self.response


class FakeMessages:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests = []

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.turns)


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.results = [{"type": "tool_result", "id": "tool-1", "content": "$123.45"}]

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_json(self):
        return self.results.pop(0)


def test_agent_requests_local_tool_then_finishes():
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    tool = SimpleNamespace(type="tool_use", id="tool-1", name="get_quote",
                           input={"symbol": "AAPL"})
    first = SimpleNamespace(content=[tool], stop_reason="tool_use", usage=usage)
    second = SimpleNamespace(content=[SimpleNamespace(type="text", text="Apple is $123.45.")],
                             stop_reason="end_turn", usage=usage)
    streams = [
        FakeStream([], first),
        FakeStream([SimpleNamespace(type="text", text="Apple is $123.45.")], second),
    ]
    client = SimpleNamespace(messages=FakeMessages(streams))
    websocket = FakeWebSocket()

    asyncio.run(run_agent(client, websocket, "Apple price?", [SAFE_TOOL], "system", "model"))

    assert {event["type"] for event in websocket.sent} >= {"reset", "tool_call", "done"}
    call = next(event for event in websocket.sent if event["type"] == "tool_call")
    assert call == {"type": "tool_call", "id": "tool-1", "name": "get_quote",
                    "input": {"symbol": "AAPL"}}
    second_request = client.messages.requests[1]
    tool_result = next(
        message for message in second_request["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
    )
    assert tool_result["content"][0]["content"] == "$123.45"


def test_authenticated_websocket_round_trip(tmp_path):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    tool = SimpleNamespace(type="tool_use", id="tool-1", name="get_quote",
                           input={"symbol": "AAPL"})
    first = SimpleNamespace(content=[tool], stop_reason="tool_use", usage=usage)
    second = SimpleNamespace(content=[SimpleNamespace(type="text", text="Apple is $123.45.")],
                             stop_reason="end_turn", usage=usage)
    claude = SimpleNamespace(messages=FakeMessages([
        FakeStream([], first),
        FakeStream([SimpleNamespace(type="text", text="Apple is $123.45.")], second),
    ]))
    app = create_app(client=claude, db_path=tmp_path / "invites.db",
                     session_secret="x" * 32)

    with TestClient(app) as client:
        code = app.state.invites.create()
        redeemed = client.post("/v1/invites/redeem", json={"code": code})
        assert redeemed.status_code == 200
        token = redeemed.json()["token"]

        with client.websocket_connect(
            "/v1/questions", headers={"Authorization": f"Bearer {token}"}
        ) as websocket:
            websocket.send_json({"type": "start", "question": "Apple price?",
                                 "tools": [SAFE_TOOL]})
            assert websocket.receive_json()["type"] == "reset"
            call = websocket.receive_json()
            assert call["type"] == "tool_call"
            websocket.send_json({"type": "tool_result", "id": call["id"],
                                 "content": "$123.45"})
            assert websocket.receive_json() == {
                "type": "text_delta", "text": "Apple is $123.45."
            }
            assert websocket.receive_json()["type"] == "usage"
            assert websocket.receive_json() == {
                "type": "done", "answer": "Apple is $123.45."
            }
