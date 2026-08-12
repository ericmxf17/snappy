"""FastAPI entrypoint for Snappy's hosted Claude service."""

import os

import anthropic
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.agent import run_agent
from backend.auth import InviteStore, SessionTokens
from backend.limits import RateLimiter
from backend.prompt import SYSTEM_PROMPT
from backend.security import ProtocolError, validate_start


class InviteRequest(BaseModel):
    code: str


def create_app(client=None, db_path=None, session_secret=None):
    app = FastAPI(title="Snappy Agent", docs_url=None, redoc_url=None)
    tokens = SessionTokens(session_secret or os.environ["SNAPPY_SESSION_SECRET"])
    invites = InviteStore(db_path or os.environ.get("SNAPPY_INVITE_DB", "snappy-invites.db"))
    claude = client or anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("SNAPPY_CLAUDE_MODEL", "claude-sonnet-5")
    limiter = RateLimiter(
        requests_per_minute=int(os.environ.get("SNAPPY_REQUESTS_PER_MINUTE", "10")),
        concurrent=int(os.environ.get("SNAPPY_CONCURRENT_REQUESTS", "2")),
    )

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/v1/invites/redeem")
    async def redeem(body: InviteRequest):
        if not invites.redeem(body.code):
            raise HTTPException(status_code=401, detail="invalid or used invite")
        return {"token": tokens.issue()}

    @app.websocket("/v1/questions")
    async def questions(websocket: WebSocket, authorization: str | None = Header(default=None)):
        bearer = authorization.removeprefix("Bearer ") if authorization else ""
        if not tokens.verify(bearer):
            await websocket.close(code=4401, reason="unauthorized")
            return
        if not limiter.enter(bearer):
            await websocket.close(code=4429, reason="rate limit exceeded")
            return
        await websocket.accept()
        try:
            question, tools = validate_start(await websocket.receive_json())
            await run_agent(claude, websocket, question, tools, SYSTEM_PROMPT, model)
        except (ProtocolError, ValueError) as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close(code=4400)
        except WebSocketDisconnect:
            return
        except Exception:
            await websocket.send_json({"type": "error", "message": "agent request failed"})
            await websocket.close(code=1011)
        finally:
            limiter.exit(bearer)

    app.state.invites = invites
    app.state.tokens = tokens
    app.state.limiter = limiter
    return app
