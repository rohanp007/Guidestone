"""Registry of named formulas."""
from typing import Callable

FORMULAS: dict[str, Callable] = {}


def register(name: str):
    def deco(fn: Callable) -> Callable:
        FORMULAS[name] = fn
        return fn

    return deco
