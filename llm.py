"""Optional LLM layer + token/cost log.

Any OpenAI-compatible endpoint works. Default: Groq free tier running the open-source
Llama 3.1 8B model. Configure with environment variables:

    LLM_API_KEY (or GROQ_API_KEY)   - no key => the assistant runs fully offline on rules
    LLM_BASE_URL                    - default https://api.groq.com/openai/v1
    LLM_MODEL                       - default llama-3.1-8b-instant
    LLM_PRICE_IN_PER_M / LLM_PRICE_OUT_PER_M - USD per 1M tokens, used for the cost estimate

temperature=0 for every call, and every response is cached on disk keyed by the exact request,
so a repeat run on the same input makes zero new calls.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.1-8b-instant"
# Groq's published on-demand rate for llama-3.1-8b-instant at the time of writing (USD / 1M tokens).
# Re-check https://groq.com/pricing before quoting it; the free tier itself costs nothing.
DEFAULT_PRICE_IN = 0.05
DEFAULT_PRICE_OUT = 0.08


def estimate_tokens(text: str) -> int:
    """Rough rule of thumb (~4 characters per token). Used only for labelled estimates."""
    return math.ceil(len(text) / 4)


class TokenLog:
    def __init__(self):
        self.calls: list[dict] = []

    def add(self, purpose: str, input_tokens: int, output_tokens: int, cached: bool):
        self.calls.append(dict(purpose=purpose, input_tokens=input_tokens, output_tokens=output_tokens, cached=cached))

    def summary(self, price_in: float, price_out: float) -> dict:
        live = [c for c in self.calls if not c["cached"]]
        tin, tout = sum(c["input_tokens"] for c in live), sum(c["output_tokens"] for c in live)
        return dict(llm_calls_made=len(live), cache_hits=len(self.calls) - len(live), input_tokens=tin,
                    output_tokens=tout, est_cost_usd=round(tin / 1e6 * price_in + tout / 1e6 * price_out, 6))


class LLMClient:
    def __init__(self, cache_dir: Path, log: TokenLog, use_cache: bool = True):
        self.api_key = os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY")
        self.base_url = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.price_in = float(os.environ.get("LLM_PRICE_IN_PER_M", DEFAULT_PRICE_IN))
        self.price_out = float(os.environ.get("LLM_PRICE_OUT_PER_M", DEFAULT_PRICE_OUT))
        self.log, self.use_cache = log, use_cache
        self.cache_path = Path(cache_dir) / "llm_cache.json"
        self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.exists() else {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, purpose: str, json_mode: bool = False) -> str:
        payload = {"model": self.model, "temperature": 0,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if self.use_cache and key in self.cache:
            self.log.add(purpose, 0, 0, cached=True)
            return self.cache[key]

        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json",
                                              "User-Agent": "freight-cost-assistant/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"LLM HTTP {exc.code}: {exc.read()[:200]!r}") from exc
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        self.log.add(purpose, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), cached=False)
        self.cache[key] = text
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")
        return text
