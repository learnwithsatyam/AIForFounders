"""Reader accounts — signup, login, sessions.

Optional by design: anonymous readers keep working exactly as before. Signing
in raises your rate limit and attaches your questions to a person instead of an
IP hash, which is what makes "who is actually using this" answerable.

Decisions worth knowing about:

  * **scrypt, from the standard library.** A real memory-hard KDF, so no new
    dependency and no home-made hashing. Each password gets its own 16-byte
    salt; the parameters are stored alongside the hash so they can be raised
    later without invalidating existing accounts.

  * **Sessions in a table, not a signed cookie.** A stateless cookie cannot be
    revoked, and revocation is the whole point of a "sign out everywhere"
    button and of locking out a stolen laptop. It also gives you login history
    for free. Only the *hash* of the token is stored, so a database leak does
    not hand over live sessions.

  * **Login never says which half was wrong.** "No such email" tells an
    attacker exactly which addresses are worth attacking. Signup has to admit
    an address is taken — there is no way to offer a usable signup form
    otherwise — so that path is rate limited instead.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("aiforfounders")

COOKIE = "af_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
MAX_PASSWORD = 200          # scrypt on a huge input is a free CPU burn otherwise
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# n=2**14 keeps a login around a tenth of a second on a shared core, which is
# slow enough to be expensive to attack and fast enough not to be felt.
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            bigserial PRIMARY KEY,
    email         text        NOT NULL UNIQUE,
    password_hash text        NOT NULL,
    name          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz,
    disabled      boolean     NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash text        PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    ip_hash    text,
    user_agent text
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions (expires_at);

ALTER TABLE usage ADD COLUMN IF NOT EXISTS user_id bigint;
CREATE INDEX IF NOT EXISTS usage_user_idx ON usage (user_id);
"""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str | None


# --------------------------------------------------------------------------
# password hashing
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    # Parameters travel with the hash so they can be raised later without
    # locking anyone out of an account created under the old ones.
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, want = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(want) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


# A hash of a password nobody has. Verifying against it when the email is
# unknown keeps the failed-login response the same length of time either way,
# so the endpoint cannot be used to enumerate who has an account.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def normalise_email(raw: str) -> str:
    return raw.strip().lower()


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

class Auth:
    def __init__(self, pool: AsyncConnectionPool, ip_salt: str) -> None:
        self.pool = pool
        self._salt = ip_salt.encode()

    async def ensure_tables(self) -> None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(SCHEMA)

    def _hash_ip(self, ip: str) -> str:
        return hashlib.sha256(self._salt + ip.encode()).hexdigest()[:32]

    @staticmethod
    def _token_hash(token: str) -> str:
        # sha256 with no salt is right here: the token is already 32 bytes of
        # entropy, so there is nothing to brute force and nothing to slow down.
        return hashlib.sha256(token.encode()).hexdigest()

    async def create_user(self, email: str, password: str, name: str | None) -> User:
        email = normalise_email(email)
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM users WHERE email = %s;", (email,))
            if await cur.fetchone():
                raise HTTPException(400, "That email already has an account. Try signing in.")
            await cur.execute(
                "INSERT INTO users (email, password_hash, name) VALUES (%s, %s, %s) "
                "RETURNING id, email, name;",
                (email, hash_password(password), (name or "").strip() or None),
            )
            row = await cur.fetchone()
        return User(*row)

    async def authenticate(self, email: str, password: str) -> User | None:
        email = normalise_email(email)
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT id, email, name, password_hash, disabled FROM users WHERE email = %s;",
                (email,),
            )
            row = await cur.fetchone()

        if row is None:
            verify_password(password, _DUMMY)   # keep the timing flat
            return None
        uid, mail, name, stored, disabled = row
        if disabled or not verify_password(password, stored):
            return None
        return User(uid, mail, name)

    async def start_session(self, user: User, ip: str, agent: str | None) -> str:
        token = secrets.token_urlsafe(32)
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, ip_hash, user_agent) "
                "VALUES (%s, %s, now() + make_interval(days => %s), %s, %s);",
                (self._token_hash(token), user.id, SESSION_DAYS,
                 self._hash_ip(ip), (agent or "")[:300] or None),
            )
            await cur.execute("UPDATE users SET last_seen_at = now() WHERE id = %s;", (user.id,))
            # Opportunistic tidy-up; expired rows are dead weight, not a leak.
            await cur.execute("DELETE FROM sessions WHERE expires_at < now();")
        return token

    async def user_for(self, token: str | None) -> User | None:
        if not token:
            return None
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT u.id, u.email, u.name FROM sessions s "
                    "JOIN users u ON u.id = s.user_id "
                    "WHERE s.token_hash = %s AND s.expires_at > now() AND NOT u.disabled;",
                    (self._token_hash(token),),
                )
                row = await cur.fetchone()
        except Exception:
            # A database blip must not sign everyone out mid-conversation; the
            # request simply proceeds as anonymous.
            log.exception("session lookup failed")
            return None
        return User(*row) if row else None

    async def end_session(self, token: str | None) -> None:
        if not token:
            return
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM sessions WHERE token_hash = %s;",
                              (self._token_hash(token),))


def set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        COOKIE, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,   # XSS cannot read it
        secure=True,
        # Lax rather than Strict: a reader arriving from a link in an email or
        # a tweet should still be signed in when they land.
        samesite="lax",
        path="/",
    )


def build_router(auth: Auth, throttle, client_ip) -> APIRouter:
    """`throttle` is the shared LoginThrottle — signup and login are the two
    endpoints worth guessing at, so both are counted per IP."""
    router = APIRouter(prefix="/api/auth")

    def public(u: User) -> dict:
        return {"id": u.id, "email": u.email, "name": u.name}

    async def read_body(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Bad request.") from None
        return body if isinstance(body, dict) else {}

    @router.post("/signup")
    async def signup(request: Request) -> Response:
        ip = client_ip(request)
        if await throttle.throttled(ip):
            raise HTTPException(429, "Too many attempts. Try again later.")

        body = await read_body(request)
        email = normalise_email(str(body.get("email", "")))
        password = str(body.get("password", ""))

        if not EMAIL_RE.match(email):
            raise HTTPException(400, "That does not look like an email address.")
        if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
            raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters.")

        # Counted before success is known: signup is an enumeration surface,
        # because it has to tell you when an address is taken.
        await throttle.record_failure(ip)
        user = await auth.create_user(email, password, body.get("name"))

        token = await auth.start_session(user, ip, request.headers.get("user-agent"))
        resp = JSONResponse({"user": public(user)})
        set_session_cookie(resp, token)
        log.info("new account %s", email)
        return resp

    @router.post("/login")
    async def login(request: Request) -> Response:
        ip = client_ip(request)
        if await throttle.throttled(ip):
            raise HTTPException(429, "Too many attempts. Try again later.")

        body = await read_body(request)
        user = await auth.authenticate(str(body.get("email", "")), str(body.get("password", "")))
        if user is None:
            await throttle.record_failure(ip)
            # Deliberately does not distinguish unknown email from wrong
            # password — the difference is a list of who has an account.
            raise HTTPException(401, "Wrong email or password.")

        token = await auth.start_session(user, ip, request.headers.get("user-agent"))
        resp = JSONResponse({"user": public(user)})
        set_session_cookie(resp, token)
        return resp

    @router.post("/logout")
    async def logout(af_session: str | None = Cookie(default=None)) -> Response:
        await auth.end_session(af_session)
        resp = JSONResponse({"user": None})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.get("/me")
    async def me(af_session: str | None = Cookie(default=None)) -> dict:
        user = await auth.user_for(af_session)
        return {"user": public(user) if user else None}

    return router
