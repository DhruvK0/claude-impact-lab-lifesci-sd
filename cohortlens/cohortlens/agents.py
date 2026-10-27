"""Agent tasks, structured outputs and the backends that can fill each seat.

OpenRouterBackend - the team's OpenRouter account (default model Qwen3.7 Plus), JSON-schema
                    structured outputs, per-seat model/effort. Default when OPENROUTER_API_KEY is set.
ClaudeBackend   - Claude API directly with structured outputs (Pydantic), per-seat model/effort.
OfflineBackend  - deterministic rule-based stand-ins (cohortlens.offline_agents) that
                  receive exactly the same payload. Used for CI, demos without an API
                  key and as a reproducible reference. It is not an LLM: persona and
                  model perturbations are reported as not applicable on it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .audit import AuditLog
from .config import agent_models
from .personas import PERSONAS
from .schemas import (
    ClaimContent, CohortDecisionCode, CritiqueContent, ResponseItem, RoutingCode,
)


# --------------------------------------------------------------------------- structured outputs

class CapturedValue(BaseModel):
    record_id: str
    field: str
    value: float | None = Field(default=None, description="Numeric value exactly as in the source; null if missing or non-numeric")
    code: str | None = Field(default=None, description="Text value exactly as in the source (genotype, AE term, urinalysis result)")
    unit: str | None = None


class DataIssue(BaseModel):
    record_id: str | None = None
    field: str | None = None
    issue: str


class CaptureOutput(BaseModel):
    values: list[CapturedValue]
    data_issues: list[DataIssue] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    critiques: list[CritiqueContent] = Field(default_factory=list)


class ResponseOutput(BaseModel):
    items: list[ResponseItem] = Field(default_factory=list)
    corrected_values: list[CapturedValue] = Field(default_factory=list)
    revised_routing: RoutingCode | None = None
    revised_cohort_decision: CohortDecisionCode | None = None


class NarrativeOutput(BaseModel):
    narrative: str
    claims: list[ClaimContent] = Field(default_factory=list)


class DSMBOutput(BaseModel):
    proposed_routing: RoutingCode
    rationale: str
    findings: list[ClaimContent] = Field(default_factory=list)


class CohortOutput(BaseModel):
    decision: CohortDecisionCode
    rationale: str
    findings: list[ClaimContent] = Field(default_factory=list)


# --------------------------------------------------------------------------- task

@dataclass
class AgentTask:
    role: str               # e.g. "s1_capture", "s3_review"
    agent_id: str           # persona seat
    stage: str
    instruction: str
    payload: dict[str, Any]
    output_model: type[BaseModel]
    framing: str | None = None          # A2 persona pollution (prepended to the request)
    system_suffix: str | None = None    # A3 system-prompt tamper
    model_override: str | None = None   # A3 model swap
    effort_override: str | None = None  # A3 effort change
    meta: dict[str, Any] = field(default_factory=dict)


class AgentError(RuntimeError):
    pass


def _render_user(task: AgentTask) -> str:
    parts = []
    if task.framing:
        parts.append(task.framing)
    parts.append(f"<task stage=\"{task.stage}\">\n{task.instruction}\n</task>")
    for key, val in task.payload.items():
        tag = "source_records" if key in ("records", "source_documents") else key
        parts.append(f"<{tag}>\n{json.dumps(val, indent=1, default=str)}\n</{tag}>")
    return "\n\n".join(parts)


class Backend:
    name = "base"
    calls = 0

    def run(self, task: AgentTask) -> BaseModel:  # pragma: no cover - interface
        raise NotImplementedError

    def model_for(self, agent_id: str) -> str:
        return self.name


class OfflineBackend(Backend):
    name = "offline"

    def __init__(self, audit: AuditLog | None = None):
        self.audit = audit
        self._lock = threading.Lock()
        self.calls = 0

    def run(self, task: AgentTask) -> BaseModel:
        from . import offline_agents
        fn = getattr(offline_agents, task.role)
        out = fn(task)
        with self._lock:
            self.calls += 1
        if self.audit:
            self.audit.append("agent_call", {
                "backend": self.name, "role": task.role, "agent_id": task.agent_id, "stage": task.stage,
                "request_hash": self.audit.digest(_render_user(task)), "response_hash": self.audit.digest(out.model_dump_json()),
            }, run_id=task.meta.get("run_id"))
        return out

    def model_for(self, agent_id: str) -> str:
        return "offline-deterministic"


class ClaudeBackend(Backend):
    name = "claude"

    def __init__(self, audit: AuditLog | None = None):
        import anthropic  # imported lazily so the offline backend has no SDK dependency
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.cfg = agent_models()
        self.audit = audit
        self._lock = threading.Lock()
        self.calls = 0

    def model_for(self, agent_id: str) -> str:
        return self.cfg["agents"].get(agent_id, self.cfg["default"])["model"]

    def run(self, task: AgentTask) -> BaseModel:
        seat = self.cfg["agents"].get(task.agent_id, self.cfg["default"])
        model = task.model_override or seat["model"]
        effort = task.effort_override or seat["effort"]
        system = PERSONAS[task.agent_id].system_prompt + (f"\n\n{task.system_suffix}" if task.system_suffix else "")
        user = _render_user(task)
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=self.cfg.get("max_tokens", 16000),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=task.output_model,
            output_config={"effort": effort},
        )
        if model in self.cfg.get("server_side_fallback_models", []):
            # On a safety-classifier decline the API re-runs the request on Anthropic's recommended fallback model.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        t0 = time.time()
        a = self.anthropic
        try:
            resp = self.client.beta.messages.parse(**kwargs)
        except a.BadRequestError as e:
            raise AgentError(f"{task.agent_id}: bad request: {e.message}") from e
        except a.RateLimitError as e:
            raise AgentError(f"{task.agent_id}: rate limited") from e
        except a.APIStatusError as e:
            raise AgentError(f"{task.agent_id}: API error {e.status_code}") from e
        except a.APIConnectionError as e:
            raise AgentError(f"{task.agent_id}: connection error") from e
        with self._lock:
            self.calls += 1
        if self.audit:
            self.audit.append("agent_call", {
                "backend": self.name, "role": task.role, "agent_id": task.agent_id, "stage": task.stage,
                "model": model, "served_by": resp.model, "effort": effort, "request_id": getattr(resp, "_request_id", None),
                "stop_reason": resp.stop_reason, "latency_s": round(time.time() - t0, 2),
                "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens,
                          "cache_read": getattr(resp.usage, "cache_read_input_tokens", None)},
                "system_hash": self.audit.digest(system), "request_hash": self.audit.digest(user),
                "response_hash": self.audit.digest(resp.model_dump_json()),
            }, run_id=task.meta.get("run_id"))
        if resp.stop_reason == "refusal":
            raise AgentError(f"{task.agent_id}: model declined the request")
        if resp.stop_reason == "max_tokens":
            raise AgentError(f"{task.agent_id}: output truncated at max_tokens")
        if resp.parsed_output is None:
            raise AgentError(f"{task.agent_id}: no structured output returned")
        return resp.parsed_output


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON Schema tightened for strict structured outputs: closed objects, every property required."""
    schema = model.model_json_schema()

    def fix(node):
        if isinstance(node, dict):
            if isinstance(node.get("title"), str):
                node.pop("title")
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for k, v in node.items():
                if k == "properties":
                    for prop in v.values():
                        fix(prop)
                else:
                    fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
    fix(schema)
    return schema


class OpenRouterBackend(Backend):
    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, audit: AuditLog | None = None):
        import httpx
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise AgentError("OPENROUTER_API_KEY is not set")
        self.cfg = agent_models()
        self.map = self.cfg["openrouter"]["model_map"]
        self.http = httpx.Client(timeout=self.cfg["openrouter"].get("timeout_s", 600), headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://github.com/Draider2001/impact-lab"),
            "X-Title": "CohortLens",
        })
        self.audit = audit
        self._lock = threading.Lock()
        self.calls = 0

    def model_for(self, agent_id: str) -> str:
        orc = self.cfg["openrouter"]
        return orc.get("agents", {}).get(agent_id, orc["default_model"])

    def run(self, task: AgentTask) -> BaseModel:
        seat = self.cfg["agents"].get(task.agent_id, self.cfg["default"])
        model = self.map.get(task.model_override, task.model_override) if task.model_override else self.model_for(task.agent_id)
        effort = task.effort_override or seat["effort"]
        system = PERSONAS[task.agent_id].system_prompt + (f"\n\n{task.system_suffix}" if task.system_suffix else "")
        user = _render_user(task)
        body = {
            "model": model,
            "max_tokens": self.cfg.get("max_tokens", 16000),
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                 if model.startswith("anthropic/") else system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": task.output_model.__name__, "strict": True, "schema": strict_schema(task.output_model)}},
            "reasoning": {"effort": {"xhigh": "high", "max": "high"}.get(effort, effort)},
            "provider": {"require_parameters": True},  # only route to providers that honour structured outputs
        }
        fallbacks = list(self.cfg["openrouter"].get("fallback_models", []))
        fallbacks = [m for m in fallbacks if m != model]
        if fallbacks:
            body["models"] = [model, *fallbacks]
        t0 = time.time()
        data = self._post(body, task)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        with self._lock:
            self.calls += 1
        if self.audit:
            self.audit.append("agent_call", {
                "backend": self.name, "role": task.role, "agent_id": task.agent_id, "stage": task.stage,
                "model": model, "served_by": data.get("model"), "effort": effort, "request_id": data.get("id"),
                "finish_reason": choice.get("finish_reason"), "latency_s": round(time.time() - t0, 2),
                "usage": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"),
                          "cache_read": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"), "cost": usage.get("cost")},
                "system_hash": self.audit.digest(system), "request_hash": self.audit.digest(user),
                "response_hash": self.audit.digest(json.dumps(data, sort_keys=True)),
            }, run_id=task.meta.get("run_id"))
        if msg.get("refusal"):
            raise AgentError(f"{task.agent_id}: model declined the request")
        if choice.get("finish_reason") == "length":
            raise AgentError(f"{task.agent_id}: output truncated at max_tokens")
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content:
            raise AgentError(f"{task.agent_id}: no structured output returned")
        try:
            return task.output_model.model_validate_json(content)
        except ValueError as e:
            raise AgentError(f"{task.agent_id}: output failed schema validation: {str(e)[:300]}") from e

    def _post(self, body: dict, task: AgentTask) -> dict:
        import httpx
        delay = 2.0
        for attempt in range(3):
            try:
                r = self.http.post(self.URL, json=body)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise AgentError(f"{task.agent_id}: connection error: {e}") from e
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    raise AgentError(f"{task.agent_id}: OpenRouter HTTP {r.status_code}")
                time.sleep(float(r.headers.get("retry-after", delay)))
                delay *= 2
                continue
            if r.status_code >= 400:
                raise AgentError(f"{task.agent_id}: OpenRouter HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            if "error" in data:
                raise AgentError(f"{task.agent_id}: OpenRouter error: {str(data['error'])[:300]}")
            return data
        raise AgentError(f"{task.agent_id}: OpenRouter request failed")


def make_backend(name: str, audit: AuditLog | None = None) -> Backend:
    if name == "openrouter":
        return OpenRouterBackend(audit)
    if name == "claude":
        return ClaudeBackend(audit)
    if name == "offline":
        return OfflineBackend(audit)
    raise ValueError(f"unknown backend '{name}' (use 'offline', 'openrouter' or 'claude')")
