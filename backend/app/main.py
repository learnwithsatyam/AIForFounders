"""FastAPI app — one endpoint, exactly as API_CONTRACT.md specifies.

    fastapi dev app/main.py        # development, auto-reload
    fastapi run app/main.py        # production
    uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import asyncio
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
from .auth import Auth
from .auth import build_router as build_auth_router
from .config import settings
from .limits import Limiter, LoginThrottle
from .rag import Engine, citations
from .usage import Event, Recorder

log = logging.getLogger("aiforfounders")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

engine = Engine(settings)
limiter = Limiter(
    engine.pool,
    per_hour=settings.rate_per_hour,
    per_day_global=settings.rate_per_day,
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
auth = Auth(engine.pool, ip_salt=settings.usage_salt or settings.database_url)
# Signup and login are both worth guessing at, so they share one per-IP budget
# rather than each getting their own.
auth_throttle = LoginThrottle(engine.pool, max_tries=settings.auth_tries_per_hour)


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

    # Every one of these touches the database, and none of them is worth the
    # whole app for. A Neon outage used to take the process down here, which
    # meant the frontend and the admin dashboard went with it despite needing
    # no database. Now a failure is logged and startup continues; the tables
    # are created on the next boot that can reach Postgres.
    async def _setup(label: str, coro) -> bool:
        try:
            # Bounded separately from the pool's own timeout: three setup calls
            # each waiting the full connect timeout would delay the first
            # health check past the point where Fly gives up on the machine.
            await asyncio.wait_for(coro, timeout=6.0)
            return True
        except Exception:
            log.warning("%s unavailable at startup — continuing without it", label)
            return False

    db_ready = await _setup("usage table", recorder.ensure_table())
    db_ready &= await _setup("rate limit table", limiter.ensure_table())
    # After the usage table: it adds a user_id column to it.
    db_ready &= await _setup("auth tables", auth.ensure_tables())

    if db_ready:
        log.info("ready — k=%d", settings.top_k)
    else:
        log.warning("started WITHOUT a working database — questions will fail, "
                    "static pages and /api/health still serve")
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

    # Resolved before the stream opens: an anonymous reader simply gets None
    # here and everything downstream behaves exactly as it did before accounts
    # existed.
    user = await auth.user_for(request.cookies.get("af_session"))

    event = Event(
        ip=client_ip(request),
        question=question,
        turn=len(messages),
        user_id=user.id if user else None,
    )

    async def stream() -> AsyncIterator[str]:
        started = perf_counter()
        # Declared before the first early return: the finally below reads it on
        # every path, including a request refused before any model was called.
        token_stats: dict[str, int] = {}
        try:
            if len(question) > settings.max_question_chars:
                event.outcome = "too_long"
                yield sse({
                    "type": "error",
                    "message": f"Questions are limited to {settings.max_question_chars} characters.",
                })
                return

            refusal = await limiter.check(
                event.ip,
                user_id=event.user_id,
                user_per_hour=settings.rate_per_hour_user,
            )
            if refusal:
                event.outcome = "rate_limited"
                yield sse({"type": "error", "message": refusal})
                return

            # Token counts come back through token_stats rather than as return
            # values, so the streaming signature stays a plain iterator of text.
            event.model = settings.chat_model

            try:
                mark = perf_counter()
                standalone = await engine.condense(messages, token_stats)
                event.condense_ms = int((perf_counter() - mark) * 1000)
                event.standalone = standalone

                mark = perf_counter()
                hits = await engine.retrieve(standalone)
                event.retrieve_ms = int((perf_counter() - mark) * 1000)

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

                async for text in engine.answer(hits, messages, token_stats):
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
            # condense reports a running total per call; the answer stream
            # repeats its own running total, so they add rather than nest.
            prompt = token_stats.get("prompt_tokens", 0) + token_stats.get("answer_prompt_tokens", 0)
            output = token_stats.get("output_tokens", 0) + token_stats.get("answer_output_tokens", 0)
            if prompt or output:
                event.prompt_tokens, event.output_tokens = prompt, output
            recorder.submit(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health() -> dict[str, object]:
    # Bounded well under the 5s health-check timeout in fly.toml. This endpoint
    # is what Fly polls for liveness, so it must answer quickly whether or not
    # Postgres does — a slow probe here gets the machine pulled from rotation,
    # which would take the frontend down over a database problem.
    try:
        counts = await asyncio.wait_for(limiter.stats(), timeout=2.0)
        db_ok = counts.get("questions_today", -1) >= 0
    except Exception:
        counts, db_ok = {"questions_today": -1, "tracked_ips": -1}, False

    return {
        "ok": True,
        "chapters": len(engine.chapters),   # 0 until the first question loads them
        "chat_model": settings.chat_model,
        "top_k": settings.top_k,
        # Whether ADMIN_PASSWORD reached the process — a boolean, never the
        # value. Without this, a missing secret and a missing deploy both look
        # like an identical 404 on /admin, which is impossible to tell apart
        # from outside the machine.
        "admin_enabled": admin.enabled,
        "usage_enabled": recorder.enabled,
        # Reported, not fatal. Fly's health check hits this endpoint, so
        # raising here over an unreachable database would pull the machine out
        # of rotation and take the static frontend down with it.
        "db_ok": db_ok,
        # Read from Postgres, so these survive a deploy instead of reporting
        # zero every time the machine restarts.
        **counts,
    }


# --- admin dashboard -------------------------------------------------------
# Registered before the static mount so /admin resolves here rather than being
# swallowed by the SPA. Every route 404s when ADMIN_PASSWORD is unset.

app.include_router(build_auth_router(auth, auth_throttle, client_ip))
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
