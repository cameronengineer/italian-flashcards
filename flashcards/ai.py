"""The one way this project talks to an LLM.

Every text / JSON AI call — card enrichment, conjugation, fun facts, image
prompts, card audits, sentence practice — goes through :class:`AI` and runs
on **Claude via ``claude -p``** (your Claude subscription):

* **One prompt shape**: a shared system preamble plus a task prompt built
  by :mod:`flashcards.tasks` (instruction → context → rules → input JSON).
* **Strict JSON-schema output** (``--json-schema``) for structured tasks;
  plain text otherwise.
* **One cache**: ``ai_cache``, keyed by model + task + messages. Cached
  results are free on re-runs; ``refresh=True`` forces a new answer.
* **Waits instead of failing** when Claude is unavailable (usage limit
  reached, logged out, network down): all AI work pauses, one call retries
  every ``[ai] unavailable_retry_minutes``, and everything resumes when it
  succeeds. Errors specific to one request (invalid output, a timeout) fail
  just that item.
* **Bounded concurrency** (``[ai] concurrency``) however many pipeline
  workers call in, so ``claude -p`` processes never pile up.

Image generation is the one exception: :meth:`AI.image` calls OpenRouter
(``[images] model``), which has no Claude equivalent.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Iterator, TypeVar

from .paths import OPENROUTER_KEY_FILE
from .pool import STOP, run_pool
from .settings import settings
from .util import load_key_file, md5_hex

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

T = TypeVar("T")


class AIError(RuntimeError):
    """This request failed; other requests may still succeed."""


class AIUnavailable(AIError):
    """Claude can't serve anything right now (usage limit, auth, network)."""


@dataclass(frozen=True)
class Task:
    """One AI request. Build these with ``flashcards.tasks`` specs."""

    name: str
    system: str
    prompt: str
    schema: dict | None = None  # None → free-text answer
    timeout: int | None = None
    cache: bool = True

    @property
    def model(self) -> str:
        return settings.ai.model_for(self.name)

    def messages(self) -> list[dict]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.prompt},
        ]

    def cache_key(self) -> str:
        return md5_hex(f"{self.model}::{self.name}::{json.dumps(self.messages(), ensure_ascii=False)}")


# ── Error classification ──────────────────────────────────────────────────

_UNAVAILABLE_STATUS = {401, 402, 403, 408, 429, 500, 502, 503, 504, 529}
_UNAVAILABLE_HINTS = (
    "usage limit", "rate limit", "limit reached", "hit your limit", "quota",
    "credit", "billing", "overloaded", "too many requests",
    "log in", "login", "logged out", "not logged", "authenticat", "unauthorized",
    "oauth", "invalid api key", "subscription",
    "network", "connection", "econnre", "enotfound", "etimedout", "fetch failed",
    "service unavailable", "internal server error", "bad gateway",
)


def _classify(message: str, status: int | None = None) -> AIError:
    low = message.lower()
    if status in _UNAVAILABLE_STATUS or (status or 0) >= 500:
        return AIUnavailable(message)
    if "schema" in low or "structured output" in low:
        return AIError(message)
    if any(h in low for h in _UNAVAILABLE_HINTS):
        return AIUnavailable(message)
    return AIError(message)


# ── Claude (text / JSON) ──────────────────────────────────────────────────


def _claude_code(task: Task) -> Any:
    """Run one task through ``claude -p`` (your Claude subscription)."""
    cfg = settings.ai
    cmd = [
        cfg.claude_cli, "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--tools", "",
        "--model", task.model,
        "--effort", cfg.effort,
        "--system-prompt", task.system,
    ]
    if task.schema is not None:
        cmd += ["--json-schema", json.dumps(task.schema)]
    try:
        proc = subprocess.run(
            cmd, input=task.prompt, capture_output=True, text=True,
            timeout=task.timeout or cfg.timeout,
            cwd=tempfile.gettempdir(),  # no project CLAUDE.md / settings
        )
    except FileNotFoundError as exc:
        raise AIError(f"Claude Code CLI not found ({cfg.claude_cli!r}); install it or set [ai] claude_cli") from exc
    except subprocess.TimeoutExpired as exc:
        raise AIError(f"{task.name}: no answer within {exc.timeout}s") from exc
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        raise _classify(f"claude -p failed: {detail[-400:]}")
    if data.get("is_error") or data.get("subtype") != "success":
        detail = str(data.get("result") or data.get("subtype") or "unknown error")
        raise _classify(f"claude -p: {detail[:400]}", data.get("api_error_status"))
    if task.schema is None:
        return (data.get("result") or "").strip()
    out = data.get("structured_output")
    if isinstance(out, dict):
        return out
    try:
        return json.loads(data.get("result") or "")
    except json.JSONDecodeError as exc:
        raise AIError(f"{task.name}: no structured output in Claude's answer") from exc


# ── OpenRouter (images only) ──────────────────────────────────────────────

_key_lock = threading.Lock()
_api_key: str | None = None


def _openrouter_key() -> str:
    global _api_key
    with _key_lock:
        if _api_key is None:
            _api_key = load_key_file(OPENROUTER_KEY_FILE)
        return _api_key


def _post(payload: dict, *, timeout: int) -> dict:
    """POST to OpenRouter (images only), retrying transient errors."""
    cfg = settings.images
    data = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    status: int | None = None
    attempt = 0
    for attempt in range(1, cfg.max_retries + 1):
        wait = cfg.retry_delay * attempt
        try:
            req = urllib.request.Request(
                OPENROUTER_URL,
                data=data,
                headers={
                    "Authorization": f"Bearer {_openrouter_key()}",
                    "Content-Type": "application/json",
                    "X-Title": "Italian Flashcards",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if "error" in body and not body.get("choices"):
                raise AIError(f"OpenRouter error: {body['error']}")
            return body
        except urllib.error.HTTPError as exc:
            last, status = exc, exc.code
            if not (exc.code in (408, 429) or exc.code >= 500):
                break
            if exc.code == 429 and exc.headers and exc.headers.get("Retry-After"):
                try:
                    wait = min(float(exc.headers["Retry-After"]), 60.0)
                except ValueError:
                    pass
        except (urllib.error.URLError, TimeoutError) as exc:
            last, status = AIUnavailable(f"network error: {exc}"), None
        except (json.JSONDecodeError, AIError) as exc:
            last = exc
        if attempt < cfg.max_retries:
            time.sleep(wait)
    message = f"OpenRouter request failed after {attempt} attempt(s): {last}"
    if isinstance(last, AIUnavailable):
        raise AIUnavailable(message)
    raise _classify(message, status)


# ── Availability gate + concurrency ───────────────────────────────────────


class _Gate:
    """Pauses every caller while Claude is unavailable.

    The first caller to see an outage becomes the prober: it retries its own
    request every ``unavailable_retry_minutes`` and reopens the gate when it
    succeeds. Everyone else blocks until then, so an outage costs one retry
    per interval rather than one per worker.
    """

    def __init__(self) -> None:
        self.open = threading.Event()
        self.open.set()
        self.lock = threading.Lock()

    def call(self, fn: Callable[[], Any]) -> Any:
        while True:
            while not self.open.wait(timeout=1):
                if STOP.is_set():
                    raise AIError("interrupted")
            try:
                return fn()
            except AIUnavailable as exc:
                with self.lock:
                    prober = self.open.is_set()
                    if prober:
                        self.open.clear()
                if prober:
                    return self._probe(fn, exc)
                # someone else is probing — wait for the gate to reopen

    def _probe(self, fn: Callable[[], Any], exc: AIUnavailable) -> Any:
        interval = max(0.1, settings.ai.unavailable_retry_minutes) * 60
        print(f"\n  ⏸  Claude unavailable: {exc}\n"
              f"     Pausing all AI work; retrying every {interval / 60:g} min "
              f"(Ctrl-C to stop).", flush=True)
        try:
            while True:
                nxt = datetime.now() + timedelta(seconds=interval)
                print(f"     next attempt at {nxt:%H:%M}", flush=True)
                if STOP.wait(interval):
                    raise AIError("interrupted while waiting for Claude")
                try:
                    result = fn()
                except AIUnavailable as again:
                    print(f"     still unavailable: {str(again)[:160]}", flush=True)
                    continue
                print("  ▶  Claude available again — resuming.", flush=True)
                return result
        finally:
            self.open.set()


_gate = _Gate()
_slots = threading.BoundedSemaphore(max(1, settings.ai.concurrency))


def _call(task: Task) -> Any:
    def once() -> Any:
        with _slots:
            return _claude_code(task)

    return _gate.call(once)


# ── Client ────────────────────────────────────────────────────────────────


class AI:
    """AI client bound to a DB connection (for the cache).

    ``conn`` may be None for callers that never cache (e.g. practice).
    ``lock`` serialises cache access when the connection is shared by a
    thread pool.
    """

    def __init__(self, conn: sqlite3.Connection | None = None, lock: Any = None):
        self.conn = conn
        self.lock = lock or threading.Lock()

    def _cache_get(self, key: str) -> Any | None:
        if self.conn is None:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT response_json FROM ai_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_put(self, key: str, value: Any) -> None:
        if self.conn is None:
            return
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO ai_cache (cache_key, response_json) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.conn.commit()

    def run(self, task: Task, *, refresh: bool = False) -> Any:
        """Run one task. Structured tasks return a dict, others a string."""
        use_cache = task.cache and self.conn is not None
        key = task.cache_key() if use_cache else ""
        if use_cache and not refresh:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        result = _call(task)
        if use_cache:
            self._cache_put(key, result)
        return result

    def run_many(
        self,
        items: Iterable[T],
        make_task: Callable[[T], Task],
        *,
        workers: int,
        label: str,
        describe: Callable[[T], str] | None = None,
        refresh: bool = False,
        progress_every: int = 50,
    ) -> Iterator[tuple[T, Any]]:
        """Run a task per item in parallel; yields ``(item, result | Exception)``."""
        yield from run_pool(
            list(items),
            lambda item: self.run(make_task(item), refresh=refresh),
            workers=workers,
            label=label,
            describe=describe,
            progress_every=progress_every,
        )

    def image(self, prompt: str) -> bytes | None:
        """Generate one image via OpenRouter (never cached — the file is the cache)."""
        payload = {
            "model": settings.images.model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image"],
        }
        body = _post(payload, timeout=120)
        try:
            url = body["choices"][0]["message"]["images"][0]["image_url"]["url"]
        except (KeyError, IndexError, TypeError):
            return None
        if not url.startswith("data:image/"):
            return None
        return base64.b64decode(url.split(",", 1)[1])
