"""App-wide settings."""
import json
import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]                # <repo>
DATA_DIR = ROOT / "data"
AS_OF_DATE = date(2026, 10, 31)                           # last day of the generated history
FEATURED_PERSONAS: list[str] = ["C002", "C067", "C095", "C010", "C059"]                         # customer ids pinned for the demo

ASSUMPTIONS_PATH = Path(__file__).with_name("assumptions.json")

load_dotenv(ROOT / ".env")                                # real environment variables win over the file


@lru_cache(maxsize=1)
def assumptions() -> dict[str, dict]:
    """Forecast assumptions, each {value, source, as_of}. Edit assumptions.json, not code."""
    return json.loads(ASSUMPTIONS_PATH.read_text())


@dataclass(frozen=True)
class AuthSettings:
    session_secret: str          # signs session tokens; SESSION_SECRET in .env
    demo_password_hash: str      # argon2 hash of the shared demo password; DEMO_PASSWORD_HASH in .env (never the password itself)
    demo_mode: bool              # DEMO_MODE=true: persona buttons on the login page, the customer list, /debug
    cookie_secure: bool          # COOKIE_SECURE=true when served over HTTPS
    session_hours: float         # SESSION_HOURS, default 8


@lru_cache(maxsize=1)
def auth_settings() -> AuthSettings:
    secret, pw_hash = os.environ.get("SESSION_SECRET", ""), os.environ.get("DEMO_PASSWORD_HASH", "")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must be set to at least 32 random characters (see .env.example)")
    if not pw_hash.startswith("$argon2"):
        raise RuntimeError("DEMO_PASSWORD_HASH must be an argon2 hash (run scripts/hash_password.py)")
    flag = lambda name: os.environ.get(name, "").strip().lower() in ("1", "true", "yes")
    return AuthSettings(secret, pw_hash, flag("DEMO_MODE"), flag("COOKIE_SECURE"), float(os.environ.get("SESSION_HOURS", "8")))
