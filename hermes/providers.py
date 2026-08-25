"""Unified chat interface across every backend Hermes supports.

Raw HTTP via urllib on purpose: Hermes ships with zero dependencies so the
installer can never fail on a broken wheel. Each adapter normalises to:

    {"text": str, "tokens_in": int, "tokens_out": int, "cost": float, "latency_ms": int}

Model lists are fetched live from each provider where an endpoint exists, so
they never go stale; SEED lists are the offline fallback.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import db

TIMEOUT = 300


class ProviderError(RuntimeError):
    pass


# Cost per 1M tokens (input, output). 0 means free / local / unknown.
PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
}

# Anthropic removed sampling params on these — sending temperature returns 400.
NO_TEMPERATURE = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                  "claude-sonnet-5", "claude-fable-5", "claude-mythos-5")

PROVIDERS = {
    "ollama": {
        "label": "Ollama",
        "tier": "local",
        "blurb": "Runs entirely on your own machine. Free, offline, private.",
        "needs_key": False,
        "base": "http://localhost:11434",
        "seed": ["qwen2.5:latest", "llama3.1:latest"],
        "signup": "https://ollama.com/download",
    },
    "groq": {
        "label": "Groq",
        "tier": "free",
        "blurb": "Free API key, very fast inference, generous free tier.",
        "needs_key": True,
        "base": "https://api.groq.com/openai/v1",
        "seed": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "signup": "https://console.groq.com/keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "tier": "free",
        "blurb": "Free API key with a daily quota. Strong all-round model.",
        "needs_key": True,
        "base": "https://generativelanguage.googleapis.com/v1beta",
        "seed": ["gemini-2.0-flash"],
        "signup": "https://aistudio.google.com/apikey",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "tier": "paid",
        "blurb": "Highest quality for hard, multi-step work. Bring your own key.",
        "needs_key": True,
        "base": "https://api.anthropic.com/v1",
        "seed": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
        "signup": "https://console.anthropic.com/settings/keys",
    },
    "openai": {
        "label": "OpenAI",
        "tier": "paid",
        "blurb": "Bring your own key. Broad model selection.",
        "needs_key": True,
        "base": "https://api.openai.com/v1",
        "seed": ["gpt-4o", "gpt-4o-mini"],
        "signup": "https://platform.openai.com/api-keys",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "tier": "custom",
        "blurb": "Any OpenAI-compatible endpoint: LM Studio, vLLM, OpenRouter, Together.",
        "needs_key": True,
        "base": "",
        "seed": [],
        "signup": "",
    },
}


def _post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:600]
        raise ProviderError(f"HTTP {e.code} from {url.split('?')[0]} — {detail}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"Cannot reach {url.split('?')[0]} — {e.reason}") from None


def _get(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _cost(model: str, tin: int, tout: int) -> float:
    rate = PRICING.get(model)
    if not rate:
        base = model.split(":")[0]
        rate = PRICING.get(base)
    if not rate:
        return 0.0
    return (tin / 1_000_000) * rate[0] + (tout / 1_000_000) * rate[1]


def base_url(provider: str) -> str:
    if provider == "custom":
        return db.setting("custom.base_url", "").rstrip("/")
    return PROVIDERS[provider]["base"]


# ---------------------------------------------------------------- adapters

def _chat_ollama(model, system, messages, temperature, max_tokens):
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    data = _post(f"{base_url('ollama')}/api/chat", {
        "model": model, "messages": msgs, "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }, {})
    return (data.get("message", {}).get("content", ""),
            data.get("prompt_eval_count", 0), data.get("eval_count", 0))


def _chat_openai_style(provider, model, system, messages, temperature, max_tokens):
    key = db.get_key(provider)
    if not key:
        raise ProviderError(f"No API key set for {PROVIDERS[provider]['label']}. Add one in Settings.")
    url = base_url(provider)
    if not url:
        raise ProviderError("Custom provider has no base URL. Set one in Settings.")
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    data = _post(f"{url}/chat/completions", {
        "model": model, "messages": msgs,
        "temperature": temperature, "max_tokens": max_tokens,
    }, {"Authorization": f"Bearer {key}"})
    usage = data.get("usage") or {}
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError(f"Empty response from {provider}: {json.dumps(data)[:300]}")
    return (choices[0].get("message", {}).get("content") or "",
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def _chat_anthropic(model, system, messages, temperature, max_tokens):
    key = db.get_key("anthropic")
    if not key:
        raise ProviderError("No Anthropic API key set. Add one in Settings.")
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        payload["system"] = system
    # Sampling params are rejected with a 400 on the current Claude models.
    if not any(model.startswith(m) for m in NO_TEMPERATURE):
        payload["temperature"] = temperature
    data = _post(f"{base_url('anthropic')}/messages", payload, {
        "x-api-key": key, "anthropic-version": "2023-06-01",
    })
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage") or {}
    return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _chat_gemini(model, system, messages, temperature, max_tokens):
    key = db.get_key("gemini")
    if not key:
        raise ProviderError("No Gemini API key set. Add one in Settings.")
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in messages]
    payload = {"contents": contents,
               "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}
    data = _post(f"{base_url('gemini')}/models/{model}:generateContent?key={key}", payload, {})
    cands = data.get("candidates") or []
    text = ""
    if cands:
        text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
    usage = data.get("usageMetadata") or {}
    return text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)


def chat(provider: str, model: str, messages: list[dict], system: str = "",
         temperature: float = 0.7, max_tokens: int = 4096) -> dict:
    """Send one turn. Returns normalised text + usage."""
    if provider not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{provider}'")
    t0 = time.time()
    if provider == "ollama":
        text, tin, tout = _chat_ollama(model, system, messages, temperature, max_tokens)
    elif provider == "anthropic":
        text, tin, tout = _chat_anthropic(model, system, messages, temperature, max_tokens)
    elif provider == "gemini":
        text, tin, tout = _chat_gemini(model, system, messages, temperature, max_tokens)
    else:
        text, tin, tout = _chat_openai_style(provider, model, system, messages, temperature, max_tokens)
    return {
        "text": text or "",
        "tokens_in": tin or 0,
        "tokens_out": tout or 0,
        "cost": _cost(model, tin or 0, tout or 0),
        "latency_ms": int((time.time() - t0) * 1000),
    }


# ---------------------------------------------------------------- discovery

def list_models(provider: str) -> list[str]:
    """Live model list where the provider exposes one, else the seed list."""
    spec = PROVIDERS.get(provider)
    if not spec:
        return []
    try:
        if provider == "ollama":
            data = _get(f"{base_url('ollama')}/api/tags", {})
            return sorted(m["name"] for m in data.get("models", []))
        key = db.get_key(provider)
        if not key:
            return spec["seed"]
        if provider == "anthropic":
            data = _get(f"{base_url('anthropic')}/models?limit=100",
                        {"x-api-key": key, "anthropic-version": "2023-06-01"})
            return [m["id"] for m in data.get("data", [])]
        if provider == "gemini":
            data = _get(f"{base_url('gemini')}/models?key={key}", {})
            return sorted(m["name"].replace("models/", "") for m in data.get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", []))
        url = base_url(provider)
        if not url:
            return spec["seed"]
        data = _get(f"{url}/models", {"Authorization": f"Bearer {key}"})
        return sorted(m["id"] for m in data.get("data", []))
    except Exception:
        return spec["seed"]


def status(provider: str) -> dict:
    """Is this backend usable right now, and why not?"""
    spec = PROVIDERS[provider]
    if provider == "ollama":
        try:
            data = _get(f"{base_url('ollama')}/api/tags", {}, timeout=4)
            n = len(data.get("models", []))
            if n == 0:
                return {"ok": False, "detail": "Running, but no models pulled yet."}
            return {"ok": True, "detail": f"Running locally · {n} model{'s' if n != 1 else ''} ready"}
        except Exception:
            return {"ok": False, "detail": "Not running. Start it with: ollama serve"}
    if provider == "custom" and not base_url("custom"):
        return {"ok": False, "detail": "No base URL configured."}
    if spec["needs_key"] and not db.get_key(provider):
        return {"ok": False, "detail": "No API key set."}
    return {"ok": True, "detail": "Key configured"}


def catalogue() -> list[dict]:
    out = []
    for pid, spec in PROVIDERS.items():
        st = status(pid)
        out.append({
            "id": pid, "label": spec["label"], "tier": spec["tier"],
            "blurb": spec["blurb"], "needs_key": spec["needs_key"],
            "signup": spec["signup"], "ok": st["ok"], "detail": st["detail"],
            "has_key": bool(db.get_key(pid)) if spec["needs_key"] else True,
        })
    return out
