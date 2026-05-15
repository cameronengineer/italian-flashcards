#!/usr/bin/env python3
"""Migrate legacy noun images to new MD5-hashed filenames.

Legacy images were generated with article prefixes (e.g., "la casa", "le case").
This script finds those images by looking for the singular/plural forms and
renames them to match the image_text column value (typically just the singular noun).

For each noun:
1. Get singular and plural forms
2. Check if image files exist for those forms (via MD5 hash)
3. If found, rename to match the image_text hash (which is typically the singular)

This consolidates multiple article variants into a single canonical image.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "database.sqlite"
IMAGES_DIR = PROJECT_ROOT / "media" / "images"


def image_hash(text: str) -> str:
    """Return MD5 hash of text for image filename."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def find_image_file(hash_value: str) -> Path | None:
    """Find image file by MD5 hash (tries .png and .jpg)."""
    for ext in ["png", "jpg"]:
        path = IMAGES_DIR / f"{hash_value}.{ext}"
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def generate_article_variations(singular: str, plural: str) -> list[str]:
    """Generate possible article+noun combinations for legacy image search.
    
    Includes singular and plural forms with various Italian articles:
    - Definite: il, lo, l', la, i, gli, le
    """
    articles_singular = ["il", "lo", "l'", "la"]
    articles_plural = ["i", "gli", "le"]
    
    variations = [
        singular,  # bare singular
        plural,    # bare plural
    ]
    
    # Singular with articles
    for article in articles_singular:
        variations.append(f"{article} {singular}")
        variations.append(f"{article}{singular}")  # also try without space for l'
    
    # Plural with articles
    for article in articles_plural:
        variations.append(f"{article} {plural}")
        variations.append(f"{article}{plural}")  # try without space
    
    return variations


def print_banner() -> None:
    title = "99 Migrate legacy images"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Migrate legacy noun images to MD5-hashed filenames matching image_text values."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without making changes.",
    )
    args = parser.parse_args()

    if not IMAGES_DIR.exists():
        print(f"Images directory not found: {IMAGES_DIR}")
        return

    with sqlite3.connect(args.db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        # Get all nouns with their forms
        nouns = connection.execute(
            """
            SELECT
                we.id,
                we.singular,
                we.plural,
                GROUP_CONCAT(DISTINCT ac.image_text) as image_texts
            FROM word_entries we
            LEFT JOIN noun_phrases np ON we.id = np.word_entry_id
            LEFT JOIN card_items ci ON ci.source_type = 'noun_phrase' AND ci.source_id = np.id
            LEFT JOIN anki_cards ac ON ac.card_item_id = ci.id
            WHERE we.word_type = 'noun'
            GROUP BY we.id
            ORDER BY we.singular
            """
        ).fetchall()

        print(f"Found {len(nouns)} nouns.\n")

        migrated = 0
        skipped = 0
        failed = 0

        for noun in nouns:
            singular = noun["singular"]
            plural = noun["plural"]
            image_texts = noun["image_texts"]

            if not image_texts:
                skipped += 1
                continue

            # The target hash is typically the first image_text (usually the singular)
            image_text_list = [t.strip() for t in image_texts.split(",")]
            target_text = image_text_list[0]  # canonical image_text
            target_hash = image_hash(target_text)

            # Check if target already exists
            if find_image_file(target_hash):
                # Already migrated
                skipped += 1
                continue

            # Try to find legacy images by singular/plural with articles
            variations = generate_article_variations(singular, plural)
            legacy_hashes = [image_hash(var) for var in variations]

            source_path = None
            source_hash = None
            for var, legacy_hash in zip(variations, legacy_hashes):
                path = find_image_file(legacy_hash)
                if path:
                    source_path = path
                    source_hash = legacy_hash
                    break

            if not source_path:
                # No legacy image found
                skipped += 1
                continue

            # Determine target extension based on source
            target_ext = source_path.suffix
            target_path = IMAGES_DIR / f"{target_hash}{target_ext}"

            if args.dry_run:
                print(
                    f"[dry-run] {singular:20} → {source_path.name:40} renaming to {target_path.name}"
                )
            else:
                try:
                    shutil.move(str(source_path), str(target_path))
                    print(
                        f"[migrated] {singular:20} → {source_path.name:40} → {target_path.name}"
                    )
                    migrated += 1
                except Exception as e:
                    print(f"[error] {singular:20} → {source_path.name}: {e}")
                    failed += 1

        print(
            f"\nFinished."
            f"\n  Migrated : {migrated}"
            f"\n  Skipped  : {skipped}"
            f"\n  Failed   : {failed}"
            f"\n  Images dir: {IMAGES_DIR}"
        )


if __name__ == "__main__":
    main()
