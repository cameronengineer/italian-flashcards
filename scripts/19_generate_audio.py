#!/usr/bin/env python3
"""Generate audio files for Italian text using ElevenLabs TTS.

Reads audio_text values from anki_cards table, generates MP3 files using ElevenLabs,
and saves them with MD5-hashed filenames to media/audio/.

Based on ../italiananki/builder/1_generate_audio.py
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

from common import DEFAULT_DB_PATH, load_api_key, audio_filename

PROJECT_ROOT = Path(__file__).resolve().parents[1]

API_KEY_FILE = PROJECT_ROOT / ".elevenlabs"
OUTPUT_DIR = PROJECT_ROOT / "media" / "audio"

# ElevenLabs configuration
VOICE_ID = "HuK8QKF35exsCh2e7fLT"  # Italian female voice
MODEL_ID = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
LANGUAGE_CODE = "it"

VOICE_SETTINGS = VoiceSettings(
    stability=0.5,
    similarity_boost=1.0,
    style=1.0,
    speed=0.7,
)

MAX_RETRIES = 2
RETRY_SLEEP = 5.0
CALL_DELAY = 0.5


def collect_audio_strings(connection: sqlite3.Connection, limit_per_deck: int | None = None) -> list[str]:
    """
    Collect unique audio_text values from anki_cards, optionally limiting per deck.
    Deduplicates — each unique string is only generated once.
    Returns a list sorted for predictable output.
    
    Args:
        connection: SQLite connection
        limit_per_deck: Maximum number of cards to process per deck (None = no limit, generate all)
    """
    # Get all unique decks
    deck_rows = connection.execute(
        "SELECT DISTINCT deck FROM anki_cards ORDER BY deck"
    ).fetchall()
    
    seen_audio = set()
    audio_list = []
    
    # Process cards from each deck
    for deck_row in deck_rows:
        deck = deck_row["deck"]
        limit_clause = f"LIMIT {limit_per_deck}" if limit_per_deck else ""
        
        cards = connection.execute(
            f"""
            SELECT DISTINCT audio_text
            FROM anki_cards
            WHERE deck = ? AND audio_text IS NOT NULL AND audio_text != ''
            ORDER BY id
            {limit_clause}
            """,
            (deck,),
        ).fetchall()
        
        for card in cards:
            audio_text = card["audio_text"]
            if audio_text not in seen_audio:
                seen_audio.add(audio_text)
                audio_list.append(audio_text)
    
    return sorted(audio_list)


def generate_audio(client: ElevenLabs, text: str, output_path: Path) -> bool:
    """Call ElevenLabs TTS and write MP3 to output_path. Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            audio_bytes = client.text_to_speech.convert(
                voice_id=VOICE_ID,
                text=text,
                model_id=MODEL_ID,
                output_format=OUTPUT_FORMAT,
                language_code=LANGUAGE_CODE,
                voice_settings=VOICE_SETTINGS,
            )
            if not isinstance(audio_bytes, (bytes, bytearray)):
                audio_bytes = b"".join(audio_bytes)
            output_path.write_bytes(audio_bytes)
            return True
        except Exception as e:
            print(f"    [error] Attempt {attempt}: {e}")
            if attempt <= MAX_RETRIES:
                print(f"    Retrying in {RETRY_SLEEP}s...")
                time.sleep(RETRY_SLEEP)

    return False


def print_banner() -> None:
    title = "19 Generate audio"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Generate audio files for anki_cards using ElevenLabs TTS."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of cards per deck to process (default: None = generate all)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without calling the API."
    )
    args = parser.parse_args()

    api_key = load_api_key(API_KEY_FILE)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        
        print(f"Collecting audio strings from {args.db}...")
        if args.limit:
            print(f"(limiting to first {args.limit} cards per deck)")
        else:
            print("(generating audio for all cards)")
        audio_texts = collect_audio_strings(connection, limit_per_deck=args.limit)
    
    if not audio_texts:
        print("No audio strings found. Nothing to do.")
        return
    
    total = len(audio_texts)
    print(f"Found {total} unique audio string(s).\n")
    
    # Count how many actually need to be generated
    to_generate = 0
    for text in audio_texts:
        filename = audio_filename(text)
        output_path = OUTPUT_DIR / filename
        if not (output_path.exists() and output_path.stat().st_size > 0):
            to_generate += 1
    
    print(f"Need to generate {to_generate} file(s).\n")
    
    if args.dry_run:
        print("[dry-run] Would generate the following:")
        for i, text in enumerate(audio_texts[:10], 1):
            filename = audio_filename(text)
            output_path = OUTPUT_DIR / filename
            status = "skip" if (output_path.exists() and output_path.stat().st_size > 0) else "generate"
            print(f"  [{i}] {status:8} → {filename} for: \"{text[:50]}\"")
        if total > 10:
            print(f"  ... and {total - 10} more")
        return
    
    client = ElevenLabs(api_key=api_key)
    
    generated = 0
    skipped = 0
    failed = 0
    generation_count = 0
    
    for text in audio_texts:
        filename = audio_filename(text)
        output_path = OUTPUT_DIR / filename
        label = f"\"{text[:50]}{'...' if len(text) > 50 else ''}\""
        
        if output_path.exists() and output_path.stat().st_size > 0:
            skipped += 1
            continue
        
        generation_count += 1
        progress_label = f"[{generation_count}/{to_generate}] {label}"
        print(f"{progress_label} — generating...")
        success = generate_audio(client, text, output_path)
        if success:
            print(f"    Saved: {filename}")
            generated += 1
        else:
            print(f"    [fail] Could not generate audio for: {text}")
            failed += 1
        
        time.sleep(CALL_DELAY)
    
    print(
        f"\nFinished."
        f"\n  Generated                 : {generated}"
        f"\n  Skipped (already existed) : {skipped}"
        f"\n  Failed                    : {failed}"
        f"\n  Output dir                : {OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()
