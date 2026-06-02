"""OpenRouter helpers + canonical JSON schemas used across modes.

A thin layer over ``urllib.request`` that:

  * Caches successful responses by content hash in ``ai_cache`` so re-runs are
    instant.
  * Retries on transient HTTP errors with linear backoff and honours
    ``Retry-After`` on 429.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any

from .util import md5_hex

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "~google/gemini-flash-latest"


class OpenRouterError(RuntimeError):
    """Raised when an OpenRouter request fails after all retries."""


def _cache_key(prompt: str, schema_name: str, model: str) -> str:
    return md5_hex(f"{model}::{schema_name}::{prompt}")


def cache_get(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        "SELECT response_json FROM ai_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(conn: sqlite3.Connection, key: str, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ai_cache (cache_key, response_json) VALUES (?, ?)",
        (key, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


def request_structured(
    *,
    prompt: str,
    schema_name: str,
    schema: dict,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 60,
    max_retries: int = 3,
    retry_delay: int = 5,
) -> dict:
    """Call OpenRouter with strict structured output and return the parsed JSON."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        },
    }
    data = json.dumps(payload).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                OPENROUTER_URL,
                data=data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "Italian Flashcards",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            content = json.loads(body)["choices"][0]["message"]["content"]
            return json.loads(content)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            wait = retry_delay * attempt
            if exc.code == 429 and exc.headers:
                ra = exc.headers.get("Retry-After")
                if ra:
                    try:
                        wait = min(float(ra), 60.0)
                    except ValueError:
                        pass
            if attempt < max_retries:
                time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay * attempt)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay)
    raise OpenRouterError(
        f"OpenRouter request failed after {max_retries} attempts: {last_exc}"
    )


def request_chat(
    *,
    messages: list[dict],
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 60,
    max_retries: int = 3,
    retry_delay: int = 5,
) -> str:
    """Plain (non-structured) chat call. Returns the raw assistant message text."""
    payload_obj = {"model": model, "messages": messages}
    data = json.dumps(payload_obj).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                OPENROUTER_URL,
                data=data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "Italian Flashcards",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay * attempt)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay * attempt)
    raise OpenRouterError(
        f"OpenRouter chat failed after {max_retries} attempts: {last_exc}"
    )


def cached_structured(
    *,
    conn: sqlite3.Connection,
    db_lock: Any,  # threading.Lock
    prompt: str,
    schema_name: str,
    schema: dict,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 60,
) -> dict:
    """Like ``request_structured`` but reads/writes the ``ai_cache`` table."""
    key = _cache_key(prompt, schema_name, model)
    with db_lock:
        hit = cache_get(conn, key)
    if hit is not None:
        return hit
    result = request_structured(
        prompt=prompt, schema_name=schema_name, schema=schema,
        api_key=api_key, model=model, timeout=timeout,
    )
    with db_lock:
        cache_put(conn, key, result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Canonical JSON schemas (re-used by gloss / verb / noun modes)
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_GLOSS = {
    "type": "object",
    "properties": {
        "english": {"type": "string"},
        "disambiguation": {"type": "string"},
        "usage_note": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "valid": {"type": "boolean"},
    },
    "required": ["english", "disambiguation", "usage_note", "confidence", "valid"],
    "additionalProperties": False,
}

SCHEMA_VERB = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "lemma": {"type": "string"},
        "english": {"type": "string"},
        "disambiguation": {"type": "string"},
        "usage_note": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "infinitive": {"type": "string"},
        "auxiliary": {"type": "string", "enum": ["avere", "essere", "both", "unknown"]},
        "past_participle": {"type": "string"},
        "is_reflexive": {"type": "boolean"},
    },
    "required": [
        "valid", "lemma", "english", "disambiguation", "usage_note",
        "confidence", "infinitive", "auxiliary", "past_participle", "is_reflexive",
    ],
    "additionalProperties": False,
}

SCHEMA_NOUN = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "lemma": {"type": "string"},
        "english": {"type": "string"},
        "disambiguation": {"type": "string"},
        "usage_note": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "singular": {"type": "string"},
        "singular_english": {"type": "string"},
        "plural": {"type": "string"},
        "plural_english": {"type": "string"},
        "gender": {"type": "string", "enum": ["masculine", "feminine", "both", "unknown"]},
        "definite_singular": {"type": "string"},
        "definite_plural": {"type": "string"},
        "indefinite_singular": {"type": "string"},
    },
    "required": [
        "valid", "lemma", "english", "disambiguation", "usage_note", "confidence",
        "singular", "singular_english", "plural", "plural_english", "gender",
        "definite_singular", "definite_plural", "indefinite_singular",
    ],
    "additionalProperties": False,
}

SCHEMA_VERB_FORMS = {
    "type": "object",
    "properties": {
        "forms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tense": {
                        "type": "string",
                        "enum": [
                            "presente",
                            "presente_progressivo",
                            "passato_prossimo",
                            "imperfetto",
                            "futuro_semplice",
                            "imperativo",
                            "condizionale_presente",
                            "condizionale_passato",
                        ],
                    },
                    "person": {
                        "type": "string",
                        "enum": ["io", "tu", "lui_lei", "noi", "voi", "loro", "Lei"],
                    },
                    "italian": {"type": "string"},
                    "english": {"type": "string"},
                    "usage_note": {"type": "string"},
                    "labels": {"type": "string"},
                },
                "required": ["tense", "person", "italian", "english", "usage_note", "labels"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["forms"],
    "additionalProperties": False,
}

SCHEMA_NOUN_PHRASES = {
    "type": "object",
    "properties": {
        "phrases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase_type": {
                        "type": "string",
                        "enum": ["definite", "indefinite", "articulated_preposition", "demonstrative", "possessive"],
                    },
                    "number": {"type": "string", "enum": ["singular", "plural"]},
                    "preposition": {"type": "string"},
                    "italian": {"type": "string"},
                    "english": {"type": "string"},
                    "usage_note": {"type": "string"},
                    "labels": {"type": "string"},
                },
                "required": ["phrase_type", "number", "preposition", "italian", "english", "usage_note", "labels"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["phrases"],
    "additionalProperties": False,
}


def merge_english(item: dict) -> str:
    """Combine ``english`` with optional ``disambiguation`` and ``usage_note``."""
    english = (item.get("english") or "").strip()
    if not english:
        return ""
    dis = (item.get("disambiguation") or "").strip()
    if dis:
        english = f"{english} ({dis})"
    note = (item.get("usage_note") or "").strip()
    if note:
        english = f"{english} [{note}]"
    return english
