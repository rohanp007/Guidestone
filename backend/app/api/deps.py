"""Shared dependencies. Every route that takes a profile goes through get_profile, so adding auth later is one change here."""
from __future__ import annotations

from fastapi import HTTPException

from ..data.loader import load_profiles
from ..data.models import Profile


def get_profile(id: str) -> Profile:
    profile = load_profiles().get(id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"profile {id} not found")
    return profile
