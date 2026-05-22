#!/usr/bin/env python3
"""Import all generated .apkg files into Anki via AnkiConnect.

Iterates over every .apkg in the decks/ directory and calls importPackage for
each one. After all imports are done it calls reloadCollection so Anki picks up
all changes immediately.

Requires:
  - Anki running with the AnkiConnect add-on enabled (default port 8765)

Usage:
  python scripts/22_import_anki_decks.py
  python scripts/22_import_anki_decks.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_CONNECT_VERSION = 6

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DECKS_DIR = PROJECT_ROOT / "decks"


def invoke(action: str, **params: Any) -> Any:
    payload = json.dumps(
        {"action": action, "version": ANKI_CONNECT_VERSION, "params": params}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(ANKI_CONNECT_URL, payload, timeout=60) as resp:
            result = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach AnkiConnect. Make sure Anki is running with AnkiConnect enabled."
        ) from exc
    if len(result) != 2 or "error" not in result or "result" not in result:
        raise RuntimeError(f"Unexpected AnkiConnect response: {result!r}")
    if result["error"] is not None:
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


def print_banner() -> None:
    title = "22 Import Anki decks via AnkiConnect"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> int:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Import all .apkg files from decks/ into Anki via AnkiConnect."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List which files would be imported without actually importing them.",
    )
    args = parser.parse_args()

    apkg_files = sorted(DECKS_DIR.glob("*.apkg"))
    if not apkg_files:
        print(f"No .apkg files found in {DECKS_DIR}")
        return 0

    if args.dry_run:
        print("DRY RUN — no changes will be made to Anki.\n")

    imported = 0
    failed = 0

    for apkg in apkg_files:
        abs_path = str(apkg.resolve())
        if args.dry_run:
            print(f"  would import: {apkg.name}")
            imported += 1
            continue
        try:
            result = invoke("importPackage", path=abs_path)
            if result:
                print(f"  imported:  {apkg.name}")
                imported += 1
            else:
                print(f"  FAILED:    {apkg.name}  (importPackage returned false)")
                failed += 1
        except RuntimeError as exc:
            print(f"  ERROR:     {apkg.name}  — {exc}", file=sys.stderr)
            failed += 1

    if not args.dry_run and imported > 0:
        try:
            invoke("reloadCollection")
            print(f"\nCollection reloaded.")
        except RuntimeError as exc:
            print(f"\nWARN: reloadCollection failed — {exc}", file=sys.stderr)

    status = "would import" if args.dry_run else "imported"
    print(f"\nDone. {imported} {status}, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
