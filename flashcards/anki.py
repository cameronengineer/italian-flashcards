"""AnkiConnect HTTP client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6


def invoke(action: str, *, timeout: int = 60, **params: Any) -> Any:
    payload = json.dumps(
        {"action": action, "version": ANKI_CONNECT_VERSION, "params": params}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(ANKI_CONNECT_URL, payload, timeout=timeout) as resp:
            result = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach AnkiConnect. Make sure Anki is running with "
            "the AnkiConnect add-on enabled (port 8765)."
        ) from exc
    if "error" not in result or "result" not in result:
        raise RuntimeError(f"Unexpected AnkiConnect response: {result!r}")
    if result["error"] is not None:
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]
