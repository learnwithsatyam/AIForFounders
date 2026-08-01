"""FastAPI app — one endpoint, exactly as API_CONTRACT.md specifies.

    fastapi dev app/main.py        # development, auto-reload
    fastapi run app/main.py        # production
    uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import settings
from .limits import Limiter
from .rag import Engine, citations

log = logging.getLogger("aiforfounders")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

engine = Engine(settings)
limiter = Limiter(
    per_hour=settings.rate_per_hour, per_day_global=settings.rate_per_day
)


# --- request model ---------------------------------------------------------

class Message(BaseModel):
    role: str
    content: str = Field(min_length=1)

    @field_validator("role", mode="after")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in {"user", "assistant"}:
            raise ValueError("role must be 'user' or 'assistant'")
        return v


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)

    @field_validator("messages", mode="after")
    @classmethod
    def _ends_with_user(cls, v: list[Message]) -> list[Message]:
        if v[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return v


# --- sse helpers -----------------------------------------------------------

def sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def client_ip(request: Request) -> str:
    # Behind a proxy (Render, Railway, Fly, nginx) the real address is here.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- lifespan --------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await engine.start()
    log.info("ready — %d chapters, k=%d", len(engine.chapters), settings.top_k)
    yield
    await engine.stop()


app = FastAPI(title="AIForFounders — Ask the Book", lifespan=lifespan)


# --- the one endpoint ------------------------------------------------------

@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    messages = [m.model_dump() for m in body.messages]
    question = messages[-1]["content"]

    async def stream() -> AsyncIterator[str]:
        if len(question) > settings.max_question_chars:
            yield sse({
                "type": "error",
                "message": f"Questions are limited to {settings.max_question_chars} characters.",
            })
            return

        refusal = limiter.check(client_ip(request))
        if refusal:
            yield sse({"type": "error", "message": refusal})
            return

        try:
            standalone = await engine.condense(messages)
            hits = await engine.retrieve(standalone)

            if not hits:
                yield sse({
                    "type": "delta",
                    "text": "I could not find anything in the book about that.",
                })
                yield sse({"type": "done"})
                return

            log.info(
                "q=%r -> %r | top=%s %.4f",
                question[:60], standalone[:60],
                hits[0].chapter[:28], hits[0].distance,
            )

            async for text in engine.answer(hits, messages):
                # The user hit Stop; the client is gone. Stop paying for tokens.
                if await request.is_disconnected():
                    log.info("client disconnected mid-answer")
                    return
                yield sse({"type": "delta", "text": text})

            yield sse({"type": "citations", "citations": citations(hits)})
            yield sse({"type": "done"})

        except Exception:
            log.exception("chat failed")
            yield sse({
                "type": "error",
                "message": "Something went wrong answering that. Please try again.",
            })

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "chapters": len(engine.chapters),
        "chat_model": settings.chat_model,
        "top_k": settings.top_k,
        **limiter.stats(),
    }


# --- static frontend -------------------------------------------------------
# Mounted last so it never shadows /api/*. Only if you have run `npm run build`.

if settings.frontend_dist.is_dir():
    app.mount(
        "/", StaticFiles(directory=settings.frontend_dist, html=True), name="ui"
    )
    log.info("serving UI from %s", settings.frontend_dist)
else:
    log.info("no frontend build at %s — API only", settings.frontend_dist)
