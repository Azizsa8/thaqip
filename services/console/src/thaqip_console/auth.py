"""Authentication for the Thaqip console.

Before this module, tenant identity was a client-supplied ``X-Thaqip-Tenant``
header with a default fallback: anyone who could reach the API was whichever
tenant they claimed to be, and an unauthenticated request was served the
default tenant's private cost and bid data. Identity now requires a credential.

Two credential kinds, both resolving to exactly one tenant:

* **User session** — username + password, verified with scrypt, exchanged for
  an opaque cookie token. Only the token's hash is stored, so reading the
  database does not yield a usable session, and sessions are revocable.
* **Service token** — ``THAQIP_API_TOKEN`` in the environment, presented as
  ``Authorization: Bearer <token>``, for the ingestion workers and test
  batteries. Compared with ``secrets.compare_digest``.

No dependency beyond the standard library: scrypt, secrets and hmac are enough,
and every added dependency in an auth path is a supply-chain question.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import HTTPException, Request

COOKIE_NAME = "thaqip_session"
SESSION_TTL = timedelta(days=int(os.environ.get("THAQIP_SESSION_DAYS", "14")))

# scrypt parameters. n=2**15 keeps a single verification near ~50-100ms on this
# class of hardware: slow enough to make offline cracking expensive, fast enough
# that a login does not feel broken.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
# OpenSSL caps scrypt memory at 32 MiB by default; n=2**15,r=8 needs ~32 MiB and
# trips it. Raise the cap rather than weakening the work factor.
_MAXMEM = 96 * 1024 * 1024

SERVICE_TOKEN = os.environ.get("THAQIP_API_TOKEN") or ""
SERVICE_TENANT_SLUG = os.environ.get("THAQIP_API_TOKEN_TENANT", "default")


# --- password hashing -------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=_DKLEN, maxmem=_MAXMEM)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Returns False on any malformed hash rather
    than raising, so a corrupt row cannot become an authentication bypass."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(hash_b64)),
            maxmem=_MAXMEM)
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:  # noqa: BLE001 - a bad hash is a failed login, never a pass
        return False


# --- sessions ---------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(pool: asyncpg.Pool, user_id: int, *, user_agent: str = "",
                         ip: str = "") -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + SESSION_TTL
    await pool.execute(
        """INSERT INTO sessions (user_id, token_hash, expires_at, user_agent, ip)
           VALUES ($1,$2,$3,$4,$5)""",
        user_id, _token_hash(token), expires, user_agent[:300], ip[:80])
    return token, expires


async def revoke_session(pool: asyncpg.Pool, token: str) -> None:
    await pool.execute(
        "UPDATE sessions SET revoked_at=now() WHERE token_hash=$1 AND revoked_at IS NULL",
        _token_hash(token))


async def session_principal(pool: asyncpg.Pool, token: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        """SELECT u.id AS user_id, u.username, u.role, u.tenant_id, t.slug AS tenant_slug
           FROM sessions s
           JOIN users u ON u.id = s.user_id
           JOIN tenants t ON t.id = u.tenant_id
           WHERE s.token_hash=$1 AND s.revoked_at IS NULL AND s.expires_at > now()
             AND u.active""",
        _token_hash(token))
    if row is None:
        return None
    return {**dict(row), "auth": "session"}


async def authenticate(pool: asyncpg.Pool, username: str, password: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        """SELECT u.id, u.username, u.password_hash, u.role, u.tenant_id, u.active,
                  t.slug AS tenant_slug
           FROM users u JOIN tenants t ON t.id = u.tenant_id
           WHERE lower(u.username)=lower($1)""",
        username)
    # Hash a dummy password when the user is unknown so a missing account and a
    # wrong password take comparable time and cannot be told apart by timing.
    stored = row["password_hash"] if row else hash_password("timing-equalizer")
    ok = verify_password(password, stored)
    if not row or not ok or not row["active"]:
        return None
    return {"user_id": row["id"], "username": row["username"], "role": row["role"],
            "tenant_id": row["tenant_id"], "tenant_slug": row["tenant_slug"],
            "auth": "password"}


async def log_auth_event(pool: asyncpg.Pool, *, event: str, username: str | None = None,
                         user_id: int | None = None, tenant_id: int | None = None,
                         detail: str = "", ip: str = "") -> None:
    await pool.execute(
        """INSERT INTO auth_events (username, user_id, tenant_id, event, detail, ip)
           VALUES ($1,$2,$3,$4,$5,$6)""",
        (username or "")[:120] or None, user_id, tenant_id, event, detail[:300], ip[:80])


# --- request principal ------------------------------------------------------

#: Set to "cloudflare" only when the console is reachable solely through a
#: Cloudflare Tunnel. Then every request arrives from the cloudflared container
#: and the real visitor is in CF-Connecting-IP; without this, the per-IP login
#: throttle would treat all users as one address and one attacker could lock
#: everyone out. Never enable it when the port is reachable directly: the
#: header would then be attacker-controlled.
TRUSTED_PROXY = os.environ.get("THAQIP_TRUSTED_PROXY", "").strip().lower()

#: Force the Secure cookie flag. Behind a tunnel the app sees plain HTTP even
#: though the visitor is on HTTPS, so the scheme cannot be trusted to decide.
COOKIE_SECURE = os.environ.get("THAQIP_COOKIE_SECURE", "") == "1"


def client_ip(request: Request) -> str:
    if TRUSTED_PROXY == "cloudflare":
        forwarded = (request.headers.get("cf-connecting-ip") or "").strip()
        if forwarded:
            return forwarded[:64]
    return (request.client.host if request.client else "") or ""


def cookie_secure(request: Request) -> bool:
    return COOKIE_SECURE or request.url.scheme == "https"


async def principal(request: Request) -> dict[str, Any] | None:
    """Resolve the caller from a credential, or None. Never falls back to a
    default identity — that fallback was the vulnerability."""
    pool: asyncpg.Pool = request.app.state.pool

    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        presented = auth_header[7:].strip()
        if SERVICE_TOKEN and secrets.compare_digest(presented, SERVICE_TOKEN):
            tid = await pool.fetchval(
                "SELECT id FROM tenants WHERE slug=$1", SERVICE_TENANT_SLUG)
            if tid is None:
                return None
            return {"user_id": None, "username": "service", "role": "service",
                    "tenant_id": int(tid), "tenant_slug": SERVICE_TENANT_SLUG,
                    "auth": "service_token"}
        return None

    token = request.cookies.get(COOKIE_NAME)
    if token:
        return await session_principal(pool, token)
    return None


async def require_principal(request: Request) -> dict[str, Any]:
    who = await principal(request)
    if who is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "authentication_required",
                    "message_ar": "يلزم تسجيل الدخول للوصول إلى هذه البيانات.",
                    "login": "/api/auth/login"},
            headers={"WWW-Authenticate": "Bearer"})
    return who
