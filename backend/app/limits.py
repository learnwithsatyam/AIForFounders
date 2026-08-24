"""Rate limiting, in Postgres so it survives a restart.

This is the thing standing between a public link and an unbounded Gemini bill,
so it cannot live in process memory. The previous version did, which meant
every deploy silently reset the daily cap to zero — the guard was strongest
right up until you shipped, and then gone.

The mechanism is one atomic upsert per check:

    INSERT … ON CONFLICT DO UPDATE SET n = n + 1 RETURNING n

Postgres does the increment and returns the post-increment value in a single
statement, so two concurrent requests can never both read "9" and both proceed.
Counting is per fixed window (an hour, a day) rather than sliding. That allows
a burst across a boundary — ten questions at 10:59 and ten more at 11:00 —
which is the accepted cost of an atomic counter that needs no locks.

Refused requests still increment. That is deliberate: hammering the endpoint
should not be free, and the window expiring is what clears it, not restraint.
"""

from __future__ import annotations

import hashlib
import logging

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("aiforfounders")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_buckets (
    scope   text        NOT NULL,
    bucket  timestamptz NOT NULL,
    n       integer     NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, bucket)
);
CREATE INDEX IF NOT EXISTS rate_buckets_bucket_idx ON rate_buckets (bucket);
"""

BUMP = """
INSERT INTO rate_buckets (scope, bucket, n)
VALUES (%s, date_trunc(%s, now()), 1)
ON CONFLICT (scope, bucket) DO UPDATE SET n = rate_buckets.n + 1
RETURNING n;
"""

# Buckets are only read inside their own window; anything older is dead weight.
SWEEP = "DELETE FROM rate_buckets WHERE bucket < now() - interval '3 days';"


def _scope(prefix: str, ip: str) -> str:
    # A counter, not a log: this table holds no questions and nothing worth
    # joining back to a person.
    return f"{prefix}:" + hashlib.sha256(ip.encode()).hexdigest()[:24]


class Limiter:
    def __init__(self, pool: AsyncConnectionPool, per_hour: int, per_day_global: int) -> None:
        self.pool = pool
        self.per_hour = per_hour
        self.per_day_global = per_day_global
        self._checks = 0

    async def ensure_table(self) -> None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(SCHEMA)

    async def _bump(self, cur, scope: str, unit: str) -> int:
        await cur.execute(BUMP, (scope, unit))
        return (await cur.fetchone())[0]

    async def check(self, ip: str, user_id: int | None = None, user_per_hour: int = 0) -> str | None:
        """None if allowed, else the message explaining the refusal.

        A signed-in reader is counted against their account rather than their
        IP, and gets the higher allowance — which is the concrete reason to
        make an account, and stops a shared office network from being one
        bucket for everyone in it.

        Fails closed. If the count cannot be read there is no way to know what
        has already been spent, and answering costs money — the condense step
        calls Gemini before anything else has a chance to fail.
        """
        scope = f"user:{user_id}" if user_id else _scope("ip", ip)
        allowance = user_per_hour if user_id and user_per_hour else self.per_hour

        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                today = await self._bump(cur, "global", "day")
                if today > self.per_day_global:
                    return ("This demo has hit its daily question limit. "
                            "Please try again tomorrow.")

                mine = await self._bump(cur, scope, "hour")
                if mine > allowance:
                    if not user_id:
                        return (f"You have reached {allowance} questions this hour. "
                                "Create a free account for more, or try again shortly.")
                    return (f"You have reached {allowance} questions this hour. "
                            "Please try again shortly.")

                # Housekeeping amortised over requests rather than a cron job.
                self._checks += 1
                if self._checks % 500 == 0:
                    await cur.execute(SWEEP)
                return None
        except Exception:
            log.exception("rate limiter unavailable — refusing rather than spending")
            return "Temporarily unavailable. Please try again in a moment."

    async def stats(self) -> dict[str, int]:
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT n FROM rate_buckets "
                    "WHERE scope = 'global' AND bucket = date_trunc('day', now());"
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) FROM rate_buckets "
                    "WHERE scope LIKE 'ip:%%' AND bucket > now() - interval '1 hour';"
                )
                ips = (await cur.fetchone())[0]
                return {"questions_today": row[0] if row else 0, "tracked_ips": ips}
        except Exception:
            log.exception("rate limiter stats failed")
            return {"questions_today": -1, "tracked_ips": -1}


class LoginThrottle:
    """Same table, same reasoning: a brute-force guard that forgets on restart
    is one `fly deploy` away from useless."""

    def __init__(self, pool: AsyncConnectionPool, max_tries: int) -> None:
        self.pool = pool
        self.max_tries = max_tries

    async def record_failure(self, ip: str) -> None:
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(BUMP, (_scope("login", ip), "hour"))
        except Exception:
            log.exception("could not record failed login")

    async def throttled(self, ip: str) -> bool:
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT coalesce(sum(n), 0) FROM rate_buckets "
                    "WHERE scope = %s AND bucket > now() - interval '1 hour';",
                    (_scope("login", ip),),
                )
                return (await cur.fetchone())[0] >= self.max_tries
        except Exception:
            # Fail closed: unable to count attempts is unable to bound them.
            log.exception("login throttle unavailable — refusing")
            return True

    async def clear(self, ip: str) -> None:
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("DELETE FROM rate_buckets WHERE scope = %s;",
                                  (_scope("login", ip),))
        except Exception:
            log.exception("could not clear login attempts")
