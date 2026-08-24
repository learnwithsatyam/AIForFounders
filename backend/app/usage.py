"""Usage recording — one row per question, in the Postgres you already have.

Why the database and not logs: Fly logs are ephemeral and the counters in
limits.py live in process memory, so both reset every deploy. Anything you want
to look at next month has to be durable, and Neon is already open with a warm
pool, so this costs one INSERT and no new infrastructure.

Two rules this module holds to:

  1. Recording can never break answering. Every write is fire-and-forget and
     every failure is swallowed with a log line. An analytics table is not
     worth a 500 in front of a reader.
  2. Recording can never slow the stream. The write is scheduled after the
     answer has finished streaming, never awaited inside the response.

Privacy: IP addresses are salted and hashed, never stored raw. Unsalted, an
IPv4 hash is trivially reversible — the whole space is 2^32 — so the salt is
what makes the column non-identifying rather than decorative.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("aiforfounders")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id            bigserial PRIMARY KEY,
    ts            timestamptz  NOT NULL DEFAULT now(),
    ip_hash       text         NOT NULL,
    question      text         NOT NULL,
    standalone    text,
    chapters      text[],
    top_distance  real,
    answer_chars  integer,
    ttft_ms       integer,
    total_ms      integer,
    outcome       text         NOT NULL,
    turn          integer
);
CREATE INDEX IF NOT EXISTS usage_ts_idx ON usage (ts DESC);

-- Added after the table shipped, so IF NOT EXISTS rather than a rewrite: the
-- rows already recorded stay, with NULLs where nothing was measured.
ALTER TABLE usage ADD COLUMN IF NOT EXISTS prompt_tokens integer;
ALTER TABLE usage ADD COLUMN IF NOT EXISTS output_tokens integer;
ALTER TABLE usage ADD COLUMN IF NOT EXISTS model         text;
ALTER TABLE usage ADD COLUMN IF NOT EXISTS condense_ms   integer;
ALTER TABLE usage ADD COLUMN IF NOT EXISTS retrieve_ms   integer;
"""

INSERT = """
INSERT INTO usage (ip_hash, question, standalone, chapters, top_distance,
                   answer_chars, ttft_ms, total_ms, outcome, turn,
                   prompt_tokens, output_tokens, model, condense_ms, retrieve_ms,
                   user_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
"""


@dataclass
class Event:
    """What one question produced. Assembled as the answer streams."""

    ip: str
    question: str
    turn: int
    standalone: str | None = None
    chapters: list[str] | None = None
    top_distance: float | None = None
    answer_chars: int = 0
    ttft_ms: int | None = None
    total_ms: int | None = None
    # ok | no_hits | rate_limited | too_long | disconnected | error
    #
    # Pessimistic by default, flipped to "ok" only once the answer has actually
    # finished. A reader who closes the tab before the first token never
    # reaches the loop that would notice, so defaulting to "ok" silently
    # recorded abandoned questions as successful ones.
    outcome: str = "disconnected"

    # Gemini's own token counts, not an estimate from character counts. The
    # prompt side dominates — eight excerpts go into every question — so this
    # is the number that actually explains the bill.
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None

    # Where the wait goes. Time to first token is the app's weakest number, and
    # without this split there is no way to know whether it is the condense
    # call, the vector query, or Gemini itself.
    condense_ms: int | None = None
    retrieve_ms: int | None = None

    # Null for anonymous readers, who remain the default. ip_hash is still
    # recorded either way, so a signed-in reader's rows are not orphaned if
    # they later delete the account.
    user_id: int | None = None


class Recorder:
    def __init__(self, pool: AsyncConnectionPool, salt: str, enabled: bool = True) -> None:
        self.pool = pool
        self.enabled = enabled
        self._salt = salt.encode()
        # asyncio keeps only weak references to tasks, so a fire-and-forget
        # task can be garbage collected mid-flight. Holding them here until
        # they finish is what stops writes from vanishing under load.
        self._pending: set[asyncio.Task] = set()

    async def ensure_table(self) -> None:
        if not self.enabled:
            return
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(SCHEMA)
            log.info("usage recording on")
        except Exception:
            log.exception("could not create usage table — recording disabled")
            self.enabled = False

    def _hash(self, ip: str) -> str:
        return hashlib.sha256(self._salt + ip.encode()).hexdigest()[:32]

    def submit(self, event: Event) -> None:
        """Schedule a write. Returns immediately; never raises."""
        if not self.enabled:
            return
        task = asyncio.create_task(self._write(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _write(self, e: Event) -> None:
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(INSERT, (
                    self._hash(e.ip),
                    e.question[:2000],
                    e.standalone[:2000] if e.standalone else None,
                    e.chapters,
                    e.top_distance,
                    e.answer_chars,
                    e.ttft_ms,
                    e.total_ms,
                    e.outcome,
                    e.turn,
                    e.prompt_tokens,
                    e.output_tokens,
                    e.model,
                    e.condense_ms,
                    e.retrieve_ms,
                    e.user_id,
                ))
        except Exception:
            # Deliberately swallowed: the reader already has their answer.
            log.exception("usage write failed")

    async def drain(self, timeout: float = 5.0) -> None:
        """Let in-flight writes finish on shutdown, rather than losing them."""
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=timeout)
