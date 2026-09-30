"""
llm.py -- one small LLM client for the whole app (drafting, enrichment, fit judging).

Providers: groq | openai | gemini, all called over plain HTTPS (no SDKs).

  LLM_PROVIDER            default provider for everything          (default: groq)
  LLM_PROVIDER_DRAFT      override for message drafting
  LLM_PROVIDER_ENRICH     override for contact extraction
  LLM_PROVIDER_FILTER     override for fit judging
  GROQ_API_KEY / GROQ_MODEL        (default model: openai/gpt-oss-20b)
  OPENAI_API_KEY / OPENAI_MODEL    (default model: gpt-4o-mini)
  GEMINI_API_KEY / GEMINI_MODEL    (default model: gemini-2.5-flash)

Model names change often -- if a provider starts returning 404, set the
*_MODEL variable; no code change needed.
"""
import json
import re
import time

import requests

import config

DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-20b",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}
KEY_VARS = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}


class LLMError(RuntimeError):
    pass


def provider_for(task):
    p = config.env(f"LLM_PROVIDER_{task.upper()}") or config.env("LLM_PROVIDER", "groq")
    return p.strip().lower()


def model_for(provider):
    return config.env(f"{provider.upper()}_MODEL") or DEFAULT_MODELS.get(provider, "")


def is_configured(task="draft"):
    p = provider_for(task)
    return p in KEY_VARS and bool(config.env(KEY_VARS[p]))


def status():
    """Which tasks can run right now -- shown in Settings."""
    out = {}
    for task in ("draft", "enrich", "filter"):
        p = provider_for(task)
        out[task] = {"provider": p, "model": model_for(p), "ready": is_configured(task)}
    return out


def complete(prompt, task="draft", system=None, max_tokens=700, temperature=0.7, timeout=60):
    """Send one prompt, return the reply text."""
    provider = provider_for(task)
    if provider not in KEY_VARS:
        raise LLMError(f"Unknown LLM provider '{provider}' (use groq, openai or gemini)")
    key = config.env(KEY_VARS[provider])
    if not key:
        raise LLMError(f"{KEY_VARS[provider]} is not set")
    model = model_for(provider)

    last_err = None
    for attempt in range(3):
        try:
            if provider == "gemini":
                body = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
                }
                if system:
                    body["systemInstruction"] = {"parts": [{"text": system}]}
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key}, json=body, timeout=timeout,
                )
            else:
                url = ("https://api.groq.com/openai/v1/chat/completions" if provider == "groq"
                       else "https://api.openai.com/v1/chat/completions")
                messages = ([{"role": "system", "content": system}] if system else []) + \
                           [{"role": "user", "content": prompt}]
                body = {"model": model, "messages": messages,
                        "max_tokens": max_tokens, "temperature": temperature}
                # gpt-oss models spend part of max_tokens on hidden reasoning; keep it short
                if provider == "groq" and "gpt-oss" in model:
                    body["reasoning_effort"] = config.env("GROQ_REASONING_EFFORT", "low")
                resp = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                                     json=body, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise LLMError(f"{provider} returned {resp.status_code}")
            if resp.status_code >= 400:
                raise LLMError(f"{provider} error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if provider == "gemini":
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
            else:
                text = data["choices"][0]["message"]["content"] or ""
            text = text.strip()
            if not text:
                raise LLMError(f"{provider} returned an empty reply")
            return text
        except (requests.RequestException, KeyError, IndexError, ValueError, LLMError) as e:
            last_err = e
            if isinstance(e, LLMError) and "error 4" in str(e) and "429" not in str(e):
                break  # a 4xx other than rate-limit won't fix itself
            time.sleep(2 * (attempt + 1))
    raise LLMError(str(last_err))


def extract_json(text):
    """Pull the first JSON object out of an LLM reply (handles code fences and
    chatter around it). Raises ValueError if none can be parsed."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(t[start:end + 1])
