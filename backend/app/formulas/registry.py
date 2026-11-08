"""Registry of named formulas: the only way a formula is ever looked up and run (by the API and, later, the agents).

Each formula declares an args model; its JSON schema is what the debug page builds forms from and what agents see as the tool schema.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Formula:
    name: str
    fn: Callable                 # fn(profile, **args)
    args: type[BaseModel]
    result: type[BaseModel] | None
    category: str
    description: str


FORMULAS: dict[str, Formula] = {}


def register(name: str, *, args: type[BaseModel] = NoArgs, result: type[BaseModel] | None = None, category: str = "general"):
    def deco(fn: Callable) -> Callable:
        doc = (inspect.getdoc(fn) or "").split("\n\n")[0]
        FORMULAS[name] = Formula(name, fn, args, result, category, " ".join(doc.split()))
        return fn

    return deco
