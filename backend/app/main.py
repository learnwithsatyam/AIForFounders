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
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .admin import Admin, build_router
from .config import settings
from .limits import Limiter
from .rag import Engine, citations
from .usage import Event, Recorder

log = logging.getLogger("aiforfounders")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

engine = Engine(settings)
limiter = Limiter(
    per_hour=settings.rate_per_hour, per_day_global=settings.rate_per_day
)
recorder = Recorder(
    engine.pool,
    salt=settings.usage_salt or settings.database_url,
    enabled=settings.usage_enabled,
)
admin = Admin(
    engine.pool,
    password=settings.admin_password,
    secret=settings.database_url,
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
    await recorder.ensure_table()
    log.info("ready — %d chapters, k=%d", len(engine.chapters), settings.top_k)
    yield
    # Let writes that are still in flight land before the pool closes under them.
    await recorder.drain()
    await engine.stop()


app = FastAPI(title="AIForFounders — Ask the Book", lifespan=lifespan)


# --- the one endpoint ------------------------------------------------------

@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    messages = [m.model_dump() for m in body.messages]
    question = messages[-1]["content"]
    event = Event(ip=client_ip(request), question=question, turn=len(messages))

    async def stream() -> AsyncIterator[str]:
        started = perf_counter()
        try:
            if len(question) > settings.max_question_chars:
                event.outcome = "too_long"
                yield sse({
                    "type": "error",
                    "message": f"Questions are limited to {settings.max_question_chars} characters.",
                })
                return

            refusal = limiter.check(event.ip)
            if refusal:
                event.outcome = "rate_limited"
                yield sse({"type": "error", "message": refusal})
                return

            try:
                standalone = await engine.condense(messages)
                event.standalone = standalone
                hits = await engine.retrieve(standalone)

                if not hits:
                    event.outcome = "no_hits"
                    yield sse({
                        "type": "delta",
                        "text": "I could not find anything in the book about that.",
                    })
                    yield sse({"type": "done"})
                    return

                # Distinct chapters in rank order — this is the column that
                # answers "which parts of the book do readers actually need".
                event.chapters = list(dict.fromkeys(h.chapter for h in hits))
                event.top_distance = hits[0].distance

                log.info(
                    "q=%r -> %r | top=%s %.4f",
                    question[:60], standalone[:60],
                    hits[0].chapter[:28], hits[0].distance,
                )

                async for text in engine.answer(hits, messages):
                    # The user hit Stop; the client is gone. Stop paying for tokens.
                    if await request.is_disconnected():
                        event.outcome = "disconnected"
                        log.info("client disconnected mid-answer")
                        return
                    if event.ttft_ms is None:
                        event.ttft_ms = int((perf_counter() - started) * 1000)
                    event.answer_chars += len(text)
                    yield sse({"type": "delta", "text": text})

                yield sse({"type": "citations", "citations": citations(hits)})
                yield sse({"type": "done"})
                event.outcome = "ok"  # only now is it genuinely a success

            except Exception:
                event.outcome = "error"
                log.exception("chat failed")
                yield sse({
                    "type": "error",
                    "message": "Something went wrong answering that. Please try again.",
                })
        finally:
            # Runs on every path including client disconnect, where Python
            # throws GeneratorExit in at the yield above. submit() is
            # deliberately synchronous — awaiting here would be unsafe while
            # the generator is being closed, and would delay the response.
            event.total_ms = int((perf_counter() - started) * 1000)
            recorder.submit(event)

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
        # Whether ADMIN_PASSWORD reached the process — a boolean, never the
        # value. Without this, a missing secret and a missing deploy both look
        # like an identical 404 on /admin, which is impossible to tell apart
        # from outside the machine.
        "admin_enabled": admin.enabled,
        "usage_enabled": recorder.enabled,
        **limiter.stats(),
    }


# --- admin dashboard -------------------------------------------------------
# Registered before the static mount so /admin resolves here rather than being
# swallowed by the SPA. Every route 404s when ADMIN_PASSWORD is unset.

app.include_router(build_router(admin, client_ip))

if admin.enabled:
    log.info("admin dashboard at /admin")
else:
    log.info("no ADMIN_PASSWORD set — admin dashboard disabled")


# --- static frontend -------------------------------------------------------
# Mounted last so it never shadows /api/*. Only if you have run `npm run build`.

if settings.frontend_dist.is_dir():
    app.mount(
        "/", StaticFiles(directory=settings.frontend_dist, html=True), name="ui"
    )
    log.info("serving UI from %s", settings.frontend_dist)
else:
    log.info("no frontend build at %s — API only", settings.frontend_dist)
