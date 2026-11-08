"""Sign-in, sessions and the current-user dependency.

One shared demo password (only its argon2 hash is configured), one login per customer. After sign-in the API sets an httpOnly,
SameSite=Lax cookie holding a signed token. The server also keeps the list of live sessions, so signing out (or a restart)
really ends a session instead of just asking the browser to forget it. Ownership of data is enforced in api/deps.get_profile:
this module only says WHO is asking.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
import pandas as pd
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import config
from .data.loader import load_profiles
from .data.models import Summary

router = APIRouter(prefix="/api", tags=["auth"])
COOKIE = "gs_session"
BAD_LOGIN = "Email or password is incorrect"      # the same message for an unknown email and a wrong password
MAX_FAILURES, WINDOW_SECONDS, MAX_FAILURES_PER_IP = 5, 60.0, 20

_hasher = PasswordHasher()


@dataclass(frozen=True)
class User:
    customer_id: str
    email: str
    first_name: str
    last_name: str


@lru_cache(maxsize=1)
def _users() -> tuple[dict[str, User], dict[str, User]]:
    """(by email, by customer id), from data/users.parquet (scripts/gen_users.py)."""
    path = config.DATA_DIR / "users.parquet"
    if not path.exists():
        raise RuntimeError("data/users.parquet is missing: run scripts/gen_users.py")
    users = [User(**r) for r in pd.read_parquet(path).astype(str).to_dict("records")]
    return {u.email.lower(): u for u in users}, {u.customer_id: u for u in users}


def user_by_id(customer_id: str) -> User | None:
    return _users()[1].get(customer_id)


# ------------------------------------------------------------------ sessions
_LIVE: dict[str, tuple[str, float]] = {}          # token id -> (customer id, expiry). In memory: a restart signs everyone out.
_LOCK = threading.Lock()


def _issue(customer_id: str) -> tuple[str, int]:
    s = config.auth_settings()
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=s.session_hours)
    jti = secrets.token_urlsafe(16)
    token = jwt.encode({"sub": customer_id, "jti": jti, "iat": now, "exp": now + ttl}, s.session_secret, algorithm="HS256")
    with _LOCK:
        for k in [k for k, (_, exp) in _LIVE.items() if exp < time.time()]:      # drop expired sessions
            del _LIVE[k]
        _LIVE[jti] = (customer_id, (now + ttl).timestamp())
    return token, int(ttl.total_seconds())


def _read(token: str | None) -> tuple[User, str] | None:
    if not token:
        return None
    try:
        claims = jwt.decode(token, config.auth_settings().session_secret, algorithms=["HS256"], options={"require": ["exp", "sub", "jti"]})
    except jwt.PyJWTError:
        return None
    live = _LIVE.get(claims["jti"])
    user = user_by_id(claims["sub"])
    if live is None or live[0] != claims["sub"] or user is None:
        return None
    return user, claims["jti"]


def current_user(request: Request) -> User:
    """Who is signed in. 401 when the cookie is missing, forged, expired or from a session that was signed out."""
    found = _read(request.cookies.get(COOKIE))
    if found is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return found[0]


# ------------------------------------------------------------------ rate limit (in memory; failures only)
_FAILS: dict[str, deque] = defaultdict(deque)


def reset_rate_limits() -> None:
    _FAILS.clear()


def _recent(key: str) -> deque:
    q, cutoff = _FAILS[key], time.monotonic() - WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        _FAILS.pop(key, None)
    return q


def _blocked(ip: str, email: str) -> bool:
    return len(_recent(f"{ip}|{email}")) >= MAX_FAILURES or len(_recent(ip)) >= MAX_FAILURES_PER_IP


def _fail(ip: str, email: str) -> None:
    now = time.monotonic()
    _FAILS[f"{ip}|{email}"].append(now)
    _FAILS[ip].append(now)


# ------------------------------------------------------------------ routes
class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class LoginResponse(BaseModel):
    customer_id: str
    first_name: str


class Me(BaseModel):
    customer_id: str
    email: str
    first_name: str
    last_name: str
    demo_mode: bool
    as_of_date: str
    summary: Summary | None


class DemoPersona(BaseModel):
    customer_id: str
    email: str
    first_name: str
    last_name: str


def _cookie_args() -> dict:
    return dict(key=COOKIE, httponly=True, samesite="lax", secure=config.auth_settings().cookie_secure, path="/")


@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response, background: BackgroundTasks) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    email = body.email.strip().lower()
    if _blocked(ip, email):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in a minute.", headers={"Retry-After": str(int(WINDOW_SECONDS))})
    try:                                               # always verify, so an unknown email takes as long as a wrong password
        password_ok = _hasher.verify(config.auth_settings().demo_password_hash, body.password)
    except (VerificationError, InvalidHashError):
        password_ok = False
    user = _users()[0].get(email)
    if not (password_ok and user):
        _fail(ip, email)
        raise HTTPException(status_code=401, detail=BAD_LOGIN)
    token, max_age = _issue(user.customer_id)
    response.set_cookie(value=token, max_age=max_age, **_cookie_args())
    if (profile := load_profiles().get(user.customer_id)) is not None:
        from .agent import orchestrator                  # imported here: the agent package depends on the API layer, which imports this module
        background.add_task(orchestrator.warm, profile)  # starts the dashboard check-up now; the response does not wait for it
    return LoginResponse(customer_id=user.customer_id, first_name=user.first_name)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    found = _read(request.cookies.get(COOKIE))
    if found:
        with _LOCK:
            _LIVE.pop(found[1], None)                  # the token is dead even if someone kept a copy
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=config.auth_settings().cookie_secure)


@router.get("/me", response_model=Me)
def me(user: User = Depends(current_user)) -> Me:
    profile = load_profiles().get(user.customer_id)
    return Me(customer_id=user.customer_id, email=user.email, first_name=user.first_name, last_name=user.last_name,
              demo_mode=config.auth_settings().demo_mode, as_of_date=config.AS_OF_DATE.isoformat(),
              summary=profile.summary if profile else None)


@router.get("/auth/demo-personas", response_model=list[DemoPersona])
def demo_personas() -> list[DemoPersona]:
    """The featured customers, for the demo login buttons. Only exists in demo mode, and never includes a password."""
    if not config.auth_settings().demo_mode:
        raise HTTPException(status_code=404, detail="not found")
    out = []
    for cid in config.FEATURED_PERSONAS:
        u = user_by_id(cid)
        if u:
            out.append(DemoPersona(customer_id=u.customer_id, email=u.email, first_name=u.first_name, last_name=u.last_name))
    return out
