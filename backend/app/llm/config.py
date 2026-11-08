"""Per-agent LLM settings, read from the environment (.env).

For each agent in AGENTS, LLM_<AGENT>_{BACKEND,BASE_URL,MODEL,REASONING,TIMEOUT,TEMPERATURE,MAX_TOKENS} override the defaults below.
LLM_BASE_URL (the fallback for an agent with no base URL of its own) and LLM_API_KEY point at OpenAI-compatible servers.

Defaults: every agent, the advisor included, runs on the llama-server at :1235 (nemotron-nano-4b-nothink, reasoning disabled in the
server). The advisor was measured both ways (scripts/advisor_latency.py): reasoning-off answers in about 4 s (p90 6 s) with the same
first-try pass rate as reasoning-on, which takes about 15 s (p90 25 s).

Documented option: the advisor with reasoning on, on LM Studio. Put this in .env (no code change):
    LLM_ADVISOR_BACKEND=lmstudio
    LLM_ADVISOR_BASE_URL=http://localhost:1234/v1
    LLM_ADVISOR_MODEL=nvidia/nemotron-3-nano-4b
    LLM_ADVISOR_REASONING=true
    LLM_ADVISOR_TIMEOUT=60            # and ADVISOR_BUDGET_S=30 (the default 12 s budget would serve the template most of the time)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .. import config as _app_config      # noqa: F401  (loads .env)

AGENTS = ("orchestrator", "researcher", "presenter", "advisor")
BACKENDS = ("lmstudio", "llamacpp", "hosted")
LMSTUDIO_URL = "http://localhost:1234/v1"
LLAMACPP_URL = "http://localhost:1235/v1"
# agent -> (backend, base url, model, reasoning, temperature, max_tokens)
DEFAULTS = {
    "orchestrator": ("llamacpp", LLAMACPP_URL, "nemotron-nano-4b-nothink", False, 0.0, 512),
    "researcher": ("llamacpp", LLAMACPP_URL, "nemotron-nano-4b-nothink", False, 0.2, 512),
    "presenter": ("llamacpp", LLAMACPP_URL, "nemotron-nano-4b-nothink", False, 0.0, 512),
    "advisor": ("llamacpp", LLAMACPP_URL, "nemotron-nano-4b-nothink", False, 0.3, 1024),
}


ADVISOR_BUDGET_S = 12.0      # default; ADVISOR_BUDGET_S in the environment overrides it


def advisor_budget_s() -> float:
    """Seconds the advisor may take (model calls and retry together) before the template result is served instead."""
    raw = os.environ.get("ADVISOR_BUDGET_S", "").strip()
    return float(raw) if raw else ADVISOR_BUDGET_S


@dataclass(frozen=True)
class AgentConfig:
    agent: str
    backend: str          # "lmstudio" / "llamacpp" (local, OpenAI-compatible) or "hosted" (not implemented yet)
    base_url: str
    model: str
    reasoning: bool       # sent to the model as a /think or /no_think flag in the system prompt
    timeout: float        # seconds per request
    temperature: float
    max_tokens: int

    @property
    def reasoning_flag(self) -> str:
        return "/think" if self.reasoning else "/no_think"

    @property
    def supports_required_tool_choice(self) -> bool:
        return self.backend != "llamacpp"         # tool_choice "required" hangs llama-server's grammar mode


def api_key() -> str:
    return os.environ.get("LLM_API_KEY", "lm-studio")     # local servers ignore it, the SDK insists on one


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def agent_config(agent: str) -> AgentConfig:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}; expected one of {AGENTS}")
    d_backend, d_url, d_model, d_reasoning, d_temp, d_max = DEFAULTS[agent]
    key = lambda name: f"LLM_{agent.upper()}_{name}"
    env = lambda name, default: os.environ.get(key(name), "").strip() or default
    reasoning = _flag(os.environ[key("REASONING")]) if os.environ.get(key("REASONING"), "").strip() else d_reasoning
    backend = env("BACKEND", d_backend)
    if backend not in BACKENDS:
        raise ValueError(f"{key('BACKEND')}={backend!r}; expected one of {BACKENDS}")
    # the agent's own URL, else LLM_BASE_URL, else the built-in default for the backend it is on
    fallback = os.environ.get("LLM_BASE_URL", "").strip() or (d_url if backend == d_backend else LMSTUDIO_URL)
    return AgentConfig(agent, backend, env("BASE_URL", fallback).rstrip("/"), env("MODEL", d_model), reasoning,
                       float(env("TIMEOUT", "120" if reasoning else "60")), float(env("TEMPERATURE", str(d_temp))),
                       int(env("MAX_TOKENS", str(d_max))))
