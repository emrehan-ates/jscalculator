"""
App session tokens (JWT, HS256).

Issued at login and carried in the Chainlit user's metadata. Every request
decodes it to get the session identity (username, role, team, email) and
checks it against the user store, so a disabled user, a role/team change or a
password reset (all bump token_version) invalidates old tokens immediately.

Secret: APP_JWT_SECRET (falls back to CHAINLIT_AUTH_SECRET). Lifetime:
APP_JWT_TTL_HOURS (default 12).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt

import users

ALGORITHM = "HS256"
ISSUER = "networkgpt"


class InvalidSession(Exception):
    """The token is missing, expired, tampered with or revoked."""


def _secret():
    secret = os.environ.get("APP_JWT_SECRET") or os.environ.get("CHAINLIT_AUTH_SECRET")
    if not secret or len(secret) < 32:
        raise RuntimeError("APP_JWT_SECRET (or CHAINLIT_AUTH_SECRET) must be set, at least 32 characters")
    return secret


def _ttl():
    try:
        return timedelta(hours=float(os.environ.get("APP_JWT_TTL_HOURS", "12")))
    except ValueError:
        return timedelta(hours=12)


def issue_token(user):
    """Signed token for a public user dict from users.verify_user()."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user["username"],
        "role": user["role"],
        "team": user.get("team") or "",
        "email": user.get("email") or "",
        "ver": user["token_version"],
        "iss": ISSUER,
        "iat": now,
        "exp": now + _ttl(),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM)


def verify_token(token):
    """Return the session claims, or raise InvalidSession.

    Checks signature, expiry and issuer, then that the user still exists, is
    active and has the same token_version as when the token was issued.
    """
    if not token:
        raise InvalidSession("no session token")
    try:
        claims = jwt.decode(token, _secret(), algorithms=[ALGORITHM], issuer=ISSUER,
                            options={"require": ["sub", "exp", "iat", "ver"]})
    except jwt.ExpiredSignatureError:
        raise InvalidSession("session expired")
    except jwt.PyJWTError as e:
        raise InvalidSession(f"invalid session token: {e}")
    user = users.get_user(claims["sub"])
    if user is None or user["status"] != "active":
        raise InvalidSession("user is no longer active")
    if user["token_version"] != claims["ver"]:
        raise InvalidSession("session was revoked (account changed)")
    return {"username": claims["sub"], "role": claims.get("role", "user"),
            "team": claims.get("team", ""), "email": claims.get("email", ""),
            "exp": claims["exp"]}
