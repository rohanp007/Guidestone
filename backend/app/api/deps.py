"""Shared dependencies. Every route that takes a profile goes through get_profile: it is where ownership is enforced."""
from __future__ import annotations

from fastapi import Depends, HTTPException

from ..auth import User, current_user
from ..data.loader import load_profiles
from ..data.models import Profile


def get_profile(id: str, user: User = Depends(current_user)) -> Profile:
    """The signed-in customer's own profile. Any other id is 403, including ids that do not exist, so the response never reveals
    which customers exist."""
    if id != user.customer_id:
        raise HTTPException(status_code=403, detail="forbidden")
    profile = load_profiles().get(id)
    if profile is None:
        raise HTTPException(status_code=403, detail="forbidden")
    return profile
