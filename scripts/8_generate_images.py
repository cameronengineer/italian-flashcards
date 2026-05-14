#!/usr/bin/env python3
"""Generate images for flashcards using riverflow-v2-fast model."""

from __future__ import annotations

import argparse
import base64
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
OUTPUT_DIR = PROJECT_ROOT / "media" / "images"

PROMPT_MODEL = "~google/gemini-flash-latest"
IMAGE_MODEL = "sourceful/riverflow-v2-fast"

LIMIT = None
MAX_RETRIES = 2
RETRY_SLEEP = 5.0
WORKERS = 10


def load_api_key(path: Path) -> str:
    """Load OpenRouter API key from file."""
    if not path.exists():
        raise FileNotFoundError(
            f"API key file not found: {path}\n"
            f"Create it with: echo 'your-key-here' > {path}"
        )
    return path.read_text(encoding="utf-8").strip()


def image_filename(key: str) -> str:
    """Return the MD5 hash of the image key as a PNG filename."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return f"{digest}.png"


def collect_entries(connection: sqlite3.Connection) -> list[dict]:
    """
    Collect unique image entries from anki_cards.
    Deduplicates by image_text to avoid generating the same image multiple times.
    """
    rows = connection.execute(
        """
        SELECT DISTINCT
            image_text,
            front_text,
            back_highlight,
            deck
        FROM anki_cards
        WHERE image_text IS NOT NULL AND image_text != ''
        ORDER BY image_text
        """
    ).fetchall()
    
    entries = []
    for row in rows:
        entries.append({
            "image_key": row["image_text"],
            "front_text": row["front_text"],
            "back_highlight": row["back_highlight"],
            "deck": row["deck"],
        })
    
    return entries


def generate_prompt(api_key: str, entry: dict) -> str | None:
    """
    Phase 1: Use text AI to generate a good image prompt from the base word/phrase.
    Given the Italian word and its English meaning, create a specific visual concept.
    Returns the prompt string, or None on failure.
    """
    image_key = entry["image_key"]
    front_text = entry["front_text"]
    back_highlight = entry["back_highlight"]
    
    user_content = (
        f"English meaning: {front_text}\n"
        f"Italian word/phrase to illustrate: {image_key}\n\n"
        f"Create a visual concept prompt."
    )
    
    system_content = (
        "You generate visual concept prompts for Italian language flashcards. "
        "Given an Italian word or phrase and its English meaning, create a single, "
        "specific visual concept (2–3 sentences) that represents the Italian meaning clearly. "
        "The image should be a flat design, minimalist icon-style illustration. "
        "Focus on the Italian meaning, not the English — use the English only to verify meaning. "
        "The image must be simple, clear, and immediately recognizable for a language learner. "
        "STRICTLY NO TEXT, letters, numbers, or labels in the image. "
        "Respond with only the visual concept prompt, nothing else."
    )
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": PROMPT_MODEL,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
    }
    
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            result = response.json()
            prompt = result["choices"][0]["message"]["content"].strip()
            if prompt:
                return prompt
            return None
        
        except requests.HTTPError as exc:
            pass  # Silent fail, will retry
        except Exception as exc:
            pass  # Silent fail, will retry
        
        if attempt <= MAX_RETRIES:
            time.sleep(RETRY_SLEEP)
    
    return None


def generate_image(api_key: str, prompt: str, output_path: Path) -> bool:
    """
    Phase 2: Call OpenRouter with riverflow-v2-fast to generate an image.
    Returns True on success.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Italian Flashcards",
    }
    payload = {
        "model": IMAGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
    }
    
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            result = response.json()
            
            if not result.get("choices"):
                return False
            
            message = result["choices"][0]["message"]
            images = message.get("images")
            
            if not images:
                return False
            
            image_url = images[0]["image_url"]["url"]
            if not image_url.startswith("data:image/"):
                return False
            
            _, encoded = image_url.split(",", 1)
            output_path.write_bytes(base64.b64decode(encoded))
            return True
        
        except requests.HTTPError as exc:
            pass  # Silent fail, will retry
        except Exception as exc:
            pass  # Silent fail, will retry
        
        if attempt <= MAX_RETRIES:
            time.sleep(RETRY_SLEEP)
    
    return False


def run_task(
    api_key: str,
    idx: int,
    total: int,
    entry: dict,
    output_path: Path,
    print_lock: threading.Lock,
) -> bool:
    """Worker task: generate visual prompt then the image itself."""
    key = entry["image_key"]
    
    with print_lock:
        print(f"[{idx}/{total}] \"{key}\"")
    
    # Phase 1: AI generates a visual concept prompt from the base word
    visual_prompt = generate_prompt(api_key, entry)
    if not visual_prompt:
        with print_lock:
            print(f"  [fail] [{idx}/{total}] could not generate visual prompt")
        return False
    
    # Phase 2: Image AI generates the image from the visual prompt
    success = generate_image(api_key, visual_prompt, output_path)
    
    with print_lock:
        if success:
            print(f"  [ok]   [{idx}/{total}] {output_path.name}")
        else:
            print(f"  [fail] [{idx}/{total}] image generation failed")
    
    return success


def print_banner() -> None:
    title = "8 Generate images"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(description="Generate flashcard images.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=LIMIT, help="Max images to generate")
    args = parser.parse_args()
    
    api_key = load_api_key(API_KEY_FILE)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        entries = collect_entries(connection)
    
    if not entries:
        print("No entries found. Nothing to do.")
        return
    
    # Filter to only entries that need generating
    to_generate = [
        entry
        for entry in entries
        if not (OUTPUT_DIR / image_filename(entry["image_key"])).exists()
        or (OUTPUT_DIR / image_filename(entry["image_key"])).stat().st_size == 0
    ]
    
    if args.limit is not None:
        to_generate = to_generate[:args.limit]
    
    total_entries = len(entries)
    total_to_generate = len(to_generate)
    skipped = total_entries - total_to_generate
    
    print(f"Found {total_entries} unique image key(s). {skipped} already exist, {total_to_generate} to generate.\n")
    print("=" * 80)
    
    if total_to_generate == 0:
        print("All images already exist. Nothing to do.")
        return
    
    print(f"Running with {WORKERS} parallel workers.\n")
    
    generated = 0
    failed = 0
    print_lock = threading.Lock()
    
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(
                run_task,
                api_key,
                idx,
                total_to_generate,
                entry,
                OUTPUT_DIR / image_filename(entry["image_key"]),
                print_lock,
            ): entry["image_key"]
            for idx, entry in enumerate(to_generate, start=1)
        }
        
        for future in as_completed(futures):
            try:
                success = future.result()
            except Exception as exc:
                key = futures[future]
                print(f"  [exception] {key}: {exc}")
                success = False
            
            if success:
                generated += 1
            else:
                failed += 1
    
    print("\n" + "=" * 80)
    print(
        f"\nFinished."
        f"\n  Generated                 : {generated}"
        f"\n  Skipped (already existed) : {skipped}"
        f"\n  Failed                    : {failed}"
        f"\n  Output dir                : {OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()
