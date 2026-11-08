"""Guidestone API. Deterministic endpoints only for now; the agent endpoints come later."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from . import auth, config
from .api import formulas, meta, profiles
from .api.serialize import SafeJSONResponse
from .data import loader
from .llm import client as llm_client
from .formulas.registry import FORMULAS

log = logging.getLogger("guidestone")

# the dev frontend, plus the deployed one when FRONTEND_ORIGIN is set
ORIGINS = ["http://localhost:3000"] + ([os.environ["FRONTEND_ORIGIN"]] if os.environ.get("FRONTEND_ORIGIN") else [])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load everything before the first request, so a data problem crashes on boot instead of mid-demo."""
    config.auth_settings()               # fails now, not on the first sign-in, if the secrets are missing
    auth._users()
    loader.load_profiles()
    loader._tables()                     # transactions and statements, for the transactions endpoint
    profiles.paycheck_flags()
    yield
    await llm_client.close_clients()


_demo = config.auth_settings().demo_mode      # the interactive docs and the schema are only served in demo mode
app = FastAPI(title="Guidestone", lifespan=lifespan, default_response_class=SafeJSONResponse,
              docs_url="/docs" if _demo else None, redoc_url=None, openapi_url="/openapi.json" if _demo else None)
# cookies need an explicit origin list: browsers refuse credentialed requests to "Access-Control-Allow-Origin: *"
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_credentials=True, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"], expose_headers=["X-Compute-Ms", "X-Cache"])
for module in (auth, meta, profiles, formulas):
    app.include_router(module.router)


def openapi() -> dict:
    """The generic formula route cannot name its arguments or results, so publish every registered formula's args and result
    models in the schema. That is what lets the generated frontend types cover them."""
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for f in FORMULAS.values():
            for model, mode in ((f.args, "validation"), (f.result, "serialization")):
                if model is None: continue
                js = model.model_json_schema(mode=mode, ref_template="#/components/schemas/{model}")
                for name, definition in js.pop("$defs", {}).items(): components.setdefault(name, definition)
                components.setdefault(model.__name__, js)
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = openapi


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Anything not handled closer to the route: say nothing about the request, log only the error type."""
    log.error("unhandled %s on %s", type(exc).__name__, request.url.path.split("/")[1:3])
    return SafeJSONResponse({"detail": "internal error"}, status_code=500)
