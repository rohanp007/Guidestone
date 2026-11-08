"""Test setup. The auth settings are read from the environment, so set them before the app is imported."""
from __future__ import annotations

import os

from argon2 import PasswordHasher

from helpers import TEST_PASSWORD

os.environ["SESSION_SECRET"] = "test-secret-" + "x" * 40
os.environ["DEMO_PASSWORD_HASH"] = PasswordHasher().hash(TEST_PASSWORD)
os.environ["DEMO_MODE"] = "true"
os.environ["COOKIE_SECURE"] = "false"

import pytest                                    # noqa: E402

from app import auth                             # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    auth.reset_rate_limits()
    yield
    auth.reset_rate_limits()


@pytest.fixture(autouse=True)
def _hermetic_llm_env(monkeypatch):
    """The developer's .env points the agents at real servers; tests must see the built-in defaults."""
    for name in [k for k in os.environ if k.startswith("LLM_")]:
        monkeypatch.delenv(name)
