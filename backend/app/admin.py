"""Admin dashboard — login, and everything the usage table knows.

Auth is a signed session cookie rather than HTTP Basic: Basic gives you a
browser dialog you cannot style and cannot log out of. The cookie carries only
an expiry and an HMAC of it, so there is nothing in it worth stealing and it
cannot be forged without the server secret.

Three things this module refuses to do:

  * Exist when unconfigured. With no ADMIN_PASSWORD set, every admin route
    returns 404 — not 403, which would confirm there is something here. A
    dashboard with a default password is worse than no dashboard.
  * Compare passwords with ==. That leaks length and prefix through timing.
  * Let the login endpoint be hammered. Failed attempts are counted per IP.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from psycopg_pool import AsyncConnectionPool

from .limits import LoginThrottle

log = logging.getLogger("aiforfounders")

COOKIE = "af_admin"
SESSION_HOURS = 12
LOGIN_MAX_TRIES = 8          # per IP, per hourly bucket
DASHBOARD = Path(__file__).resolve().parent / "dashboard.html"


class Login(BaseModel):
    password: str


class Admin:
    def __init__(self, pool: AsyncConnectionPool, password: str, secret: str) -> None:
        self.pool = pool
        self._password = password
        # Folding the password into the session secret means changing the
        # password invalidates every existing session, for free.
        self._secret = hashlib.sha256((secret + password).encode()).digest()
        # Failed attempts live in Postgres, not memory: a brute-force guard
        # that forgets on restart is one deploy away from useless.
        self.throttle = LoginThrottle(pool, LOGIN_MAX_TRIES)

    @property
    def enabled(self) -> bool:
        return bool(self._password)

    # --- sessions ---------------------------------------------------------

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expiry = str(int(time.time()) + SESSION_HOURS * 3600)
        return f"{expiry}.{self._sign(expiry)}"

    def valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        expiry, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(expiry)):
            return False
        try:
            return int(expiry) > time.time()
        except ValueError:
            return False

    # --- login ------------------------------------------------------------

    async def check_password(self, ip: str, attempt: str) -> bool:
        # compare_digest, not ==: a short-circuiting comparison leaks how much
        # of the password was right through how long the response took.
        if hmac.compare_digest(attempt, self._password):
            await self.throttle.clear(ip)
            return True
        await self.throttle.record_failure(ip)
        return False


def build_router(admin: Admin, client_ip) -> APIRouter:
    router = APIRouter()

    def require(token: str | None) -> None:
        if not admin.enabled:
            raise HTTPException(status_code=404)
        if not admin.valid(token):
            raise HTTPException(status_code=401, detail="not signed in")

    @router.get("/admin", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        if not admin.enabled:
            raise HTTPException(status_code=404)
        # The page renders its own sign-in form; it holds no data until an
        # authenticated fetch succeeds.
        return HTMLResponse(DASHBOARD.read_text())

    @router.get("/api/admin/session")
    async def session(af_admin: str | None = Cookie(default=None)) -> dict:
        if not admin.enabled:
            raise HTTPException(status_code=404)
        return {"signed_in": admin.valid(af_admin)}

    @router.post("/api/admin/login")
    async def login(request: Request) -> Response:
        # The body is parsed by hand rather than declared as a model parameter.
        # FastAPI validates declared bodies *before* the handler runs, so a
        # malformed POST would answer 422 while admin is disabled — confirming
        # the route exists. Checking `enabled` first keeps it a flat 404.
        if not admin.enabled:
            raise HTTPException(status_code=404)

        try:
            body = Login(**await request.json())
        except Exception:
            raise HTTPException(status_code=400, detail="Bad request.") from None

        ip = client_ip(request)
        if await admin.throttle.throttled(ip):
            log.warning("admin login throttled for %s", ip[:16])
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

        if not await admin.check_password(ip, body.password):
            log.warning("failed admin login from %s", ip[:16])
            raise HTTPException(status_code=401, detail="Wrong password.")

        resp = JSONResponse({"signed_in": True})
        resp.set_cookie(
            COOKIE, admin.issue(),
            max_age=SESSION_HOURS * 3600,
            httponly=True,      # JS cannot read it, so XSS cannot lift the session
            secure=True,        # never sent over plain http
            samesite="strict",  # not attached to cross-site requests at all
            path="/",
        )
        return resp

    @router.post("/api/admin/logout")
    async def logout() -> Response:
        resp = JSONResponse({"signed_in": False})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.get("/api/admin/stats")
    async def stats(days: int = 7, af_admin: str | None = Cookie(default=None)) -> dict:
        require(af_admin)
        days = max(1, min(days, 365))
        async with admin.pool.connection() as conn, conn.cursor() as cur:
            return await collect(cur, days)

    return router


# --------------------------------------------------------------------------
# queries — one round trip per panel, all scoped to the same window
# --------------------------------------------------------------------------

async def collect(cur, days: int) -> dict:
    window = "ts > now() - make_interval(days => %s)"

    await cur.execute(
        f"""SELECT count(*), count(DISTINCT ip_hash),
                   count(*) FILTER (WHERE turn > 1),
                   count(*) FILTER (WHERE outcome = 'ok')
            FROM usage WHERE {window};""",
        (days,),
    )
    total, readers, followups, answered = await cur.fetchone()

    # Percentiles must come from rows that actually produced a token, or the
    # "full answer" median can land below the time-to-first-token median.
    await cur.execute(
        f"""SELECT percentile_disc(0.5)  WITHIN GROUP (ORDER BY ttft_ms),
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms),
                   percentile_disc(0.5)  WITHIN GROUP (ORDER BY total_ms),
                   avg(answer_chars)::int
            FROM usage WHERE {window} AND ttft_ms IS NOT NULL;""",
        (days,),
    )
    ttft50, ttft95, total50, chars = await cur.fetchone()

    await cur.execute(
        f"SELECT outcome, count(*) FROM usage WHERE {window} GROUP BY outcome ORDER BY 2 DESC;",
        (days,),
    )
    outcomes = [{"outcome": o, "n": n} for o, n in await cur.fetchall()]

    # Tokens, and where the wait goes. Both are null for rows recorded before
    # these columns existed, so every aggregate has to tolerate NULL.
    await cur.execute(
        f"""SELECT coalesce(sum(prompt_tokens), 0), coalesce(sum(output_tokens), 0),
                   count(*) FILTER (WHERE prompt_tokens IS NOT NULL),
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY condense_ms),
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY retrieve_ms)
            FROM usage WHERE {window};""",
        (days,),
    )
    tok_in, tok_out, priced_rows, condense50, retrieve50 = await cur.fetchone()

    # generate_series so quiet days are zeroes rather than gaps — a missing day
    # would otherwise read as a shorter axis instead of no traffic.
    await cur.execute(
        """SELECT d::date::text, coalesce(c, 0) FROM generate_series(
               date_trunc('day', now()) - make_interval(days => %s - 1),
               date_trunc('day', now()), '1 day') AS d
           LEFT JOIN (
               SELECT date_trunc('day', ts) AS day, count(*) AS c
               FROM usage WHERE ts > now() - make_interval(days => %s)
               GROUP BY 1
           ) t ON t.day = d
           ORDER BY d;""",
        (days, days),
    )
    by_day = [{"day": d, "n": n} for d, n in await cur.fetchall()]

    await cur.execute(
        f"""SELECT ch, count(*) FROM usage, unnest(chapters) AS ch
            WHERE {window} GROUP BY ch ORDER BY 2 DESC LIMIT 15;""",
        (days,),
    )
    chapters = [{"chapter": c, "n": n} for c, n in await cur.fetchall()]

    await cur.execute(
        f"""SELECT to_char(ts, 'Mon DD HH24:MI'), question, outcome, turn, ttft_ms
            FROM usage WHERE {window} ORDER BY ts DESC LIMIT 60;""",
        (days,),
    )
    recent = [
        {"when": w, "question": q, "outcome": o, "turn": t, "ttft_ms": f}
        for w, q, o, t, f in await cur.fetchall()
    ]

    # Accounts. Total is all-time — signups are a cumulative asset, not
    # something to read only inside the selected window — while `new` and the
    # activity counts respect it.
    await cur.execute(
        """SELECT count(*),
                  count(*) FILTER (WHERE created_at > now() - make_interval(days => %s)),
                  count(*) FILTER (WHERE last_seen_at > now() - make_interval(days => %s))
           FROM users;""",
        (days, days),
    )
    users_total, users_new, users_active = await cur.fetchone()

    # LEFT JOIN with the window in the ON clause, not WHERE: a reader who
    # signed up and has not asked anything yet still needs to appear, and a
    # WHERE would filter those rows away.
    await cur.execute(
        """SELECT u.email, u.name, count(x.id),
                  to_char(max(x.ts), 'Mon DD HH24:MI'),
                  to_char(u.created_at, 'Mon DD')
           FROM users u
           LEFT JOIN usage x
             ON x.user_id = u.id AND x.ts > now() - make_interval(days => %s)
           GROUP BY u.id, u.email, u.name, u.created_at
           ORDER BY count(x.id) DESC, u.created_at DESC
           LIMIT 25;""",
        (days,),
    )
    people = [
        {"email": e, "name": n, "questions": q, "last": last, "joined": joined}
        for e, n, q, last, joined in await cur.fetchall()
    ]

    # Outright misses plus near-misses: a high top_distance means nothing in
    # the book was really close, even though something was returned.
    await cur.execute(
        f"""SELECT to_char(ts, 'Mon DD'), question, outcome, top_distance
            FROM usage
            WHERE {window} AND (outcome = 'no_hits' OR top_distance > 0.45)
            ORDER BY top_distance DESC NULLS FIRST LIMIT 40;""",
        (days,),
    )
    misses = [
        {"when": w, "question": q, "outcome": o, "distance": d}
        for w, q, o, d in await cur.fetchall()
    ]

    # Cost is only reported when prices are configured. An invented rate would
    # look authoritative and be wrong.
    from .config import settings as _s
    priced = _s.price_in_per_mtok > 0 or _s.price_out_per_mtok > 0
    cost = (
        (tok_in / 1e6) * _s.price_in_per_mtok + (tok_out / 1e6) * _s.price_out_per_mtok
        if priced else None
    )

    return {
        "days": days,
        "total": total,
        "readers": readers,
        "tokens_in": tok_in,
        "tokens_out": tok_out,
        "priced_rows": priced_rows,
        "cost_usd": cost,
        "condense_p50": condense50,
        "retrieve_p50": retrieve50,
        "users_total": users_total,
        "users_new": users_new,
        "users_active": users_active,
        "people": people,
        "followups": followups,
        "answered": answered,
        "ttft_p50": ttft50,
        "ttft_p95": ttft95,
        "total_p50": total50,
        "avg_chars": chars,
        "outcomes": outcomes,
        "by_day": by_day,
        "chapters": chapters,
        "recent": recent,
        "misses": misses,
    }
