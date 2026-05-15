#!/usr/bin/env python3
"""Generate images for flashcards using riverflow-v2-fast model."""

from __future__ import annotations

import argparse
import base64
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from common import DEFAULT_DB_PATH, load_api_key, image_filename

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_FILE = PROJECT_ROOT / ".openrouter"
OUTPUT_DIR = PROJECT_ROOT / "media" / "images"

PROMPT_MODEL = "~google/gemini-flash-latest"          # text AI for prompt generation (repo default)
IMAGE_MODEL = "black-forest-labs/flux.2-klein-4b"      # image generation — better quality than riverflow

LIMIT = None
MAX_RETRIES = 2
RETRY_SLEEP = 5.0
WORKERS = 10


def collect_entries(connection: sqlite3.Connection) -> list[dict]:
    """
    Collect unique image entries from anki_cards.
    Deduplicates by image_text to avoid generating the same image multiple times.
    Only keeps one representative card per unique image_text for prompt generation.
    """
    rows = connection.execute(
        """
        SELECT
            image_text,
            front_text,
            front_labels,
            back_highlight,
            back_text,
            deck
        FROM anki_cards
        WHERE image_text IS NOT NULL AND image_text != ''
        ORDER BY image_text, id
        """
    ).fetchall()
    
    # Deduplicate by image_text, keeping only the first occurrence
    seen = set()
    entries = []
    for row in rows:
        image_text = row["image_text"]
        if image_text not in seen:
            seen.add(image_text)
            entries.append({
                "image_key": image_text,
                "front_text": row["front_text"],
                "front_labels": row["front_labels"] or "",
                "back_highlight": row["back_highlight"],
                "back_text": row["back_text"] or "",
                "deck": row["deck"],
            })
    
    return entries


def generate_prompt(api_key: str, entry: dict) -> str | None:
    """
    Phase 1: Use text AI to generate a precise image prompt from the full flashcard context.
    Given the flashcard data, create a specific visual concept that reflects the Italian meaning.
    Returns the prompt string, or None on failure.
    """
    image_key = entry["image_key"]
    front_text = entry["front_text"]
    front_labels = entry["front_labels"]
    back_highlight = entry["back_highlight"]
    back_text = entry["back_text"]
    
    back_text_line = f"\n- Italian infinitive: {back_text}" if back_text else ""
    
    user_content = (
        f"- English: {front_text}\n"
        f"- Type / context: {front_labels}\n"
        f"- Italian: {back_highlight}"
        f"{back_text_line}\n\n"
        f"Write the image generation prompt."
    )
    
    system_content = (
        "You generate image prompts for Italian language flashcard illustrations. "
        "Given a flashcard's data, write a single specific image generation prompt "
        "(2–3 sentences) for a flat design, minimalist icon-style illustration. "
        "The Italian word/phrase takes precedence over the English when the English "
        "is ambiguous — the image must accurately represent the Italian meaning. "
        "The image must be simple, clear, and suitable for a language learner. "
        "STRICTLY NO TEXT, letters, numbers, or labels in the image. "
        "Respond with only the image prompt, nothing else."
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
            print(f"  [warn] HTTP {exc.response.status_code}: {exc}")
        except Exception as exc:
            print(f"  [warn] Unexpected error generating prompt: {exc}")

        if attempt <= MAX_RETRIES:
            time.sleep(RETRY_SLEEP)
    
    return None


def generate_image(api_key: str, prompt: str, output_path: Path) -> bool:
    """
    Phase 2: Call OpenRouter with FLUX.2 Klein to generate an image from the prompt.
    Writes PNG to output_path. Returns True on success.
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
            print(f"  [warn] HTTP {exc.response.status_code}: {exc}")
        except Exception as exc:
            print(f"  [warn] Unexpected error generating image: {exc}")

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
    title = "12 Generate images"
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
