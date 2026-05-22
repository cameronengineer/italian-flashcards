#!/usr/bin/env python3
"""Compress media files for Anki deck building.

Reads originals from media/images/ and media/audio/ and writes compressed
versions to media/images_compressed/ and media/audio_compressed/.

Images:  PNG → JPEG, max 512×512, quality 75. Produces ~90% size reduction.
Audio:   MP3 re-encoded to 48kbps mono via ffmpeg (must be on PATH).

Originals in media/images/ and media/audio/ are never modified.
16_create_decks.py reads from the compressed folders preferentially,
falling back to originals if compressed versions don't exist.

Based on ../italiananki/builder/3_compress_media.py
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]

IMAGES_IN = PROJECT_ROOT / "media" / "images"
IMAGES_OUT = PROJECT_ROOT / "media" / "images_compressed"
AUDIO_IN = PROJECT_ROOT / "media" / "audio"
AUDIO_OUT = PROJECT_ROOT / "media" / "audio_compressed"

# Configuration
IMAGE_MAX_PX = 512  # max width or height in pixels
IMAGE_QUALITY = 75  # JPEG quality (0-100)
AUDIO_BITRATE = "48k"  # target MP3 bitrate


def compress_images() -> None:
    """Compress PNG images to JPEG format with size limits."""
    IMAGES_OUT.mkdir(parents=True, exist_ok=True)

    sources = list(IMAGES_IN.glob("*.png"))
    if not sources:
        print("No PNG files found in media/images/")
        return

    skipped = done = failed = 0
    original_total = compressed_total = 0
    total = len(sources)
    PROGRESS_EVERY = max(1, total // 20)  # print every ~5%

    print(f"Compressing {total} images → {IMAGES_OUT}")

    for i, src in enumerate(sources, start=1):
        dest = IMAGES_OUT / (src.stem + ".jpg")
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue

        try:
            with Image.open(src) as img:
                img = img.convert("RGB")
                img.thumbnail((IMAGE_MAX_PX, IMAGE_MAX_PX), Image.LANCZOS)
                img.save(dest, "JPEG", quality=IMAGE_QUALITY, optimize=True)

            original_total += src.stat().st_size
            compressed_total += dest.stat().st_size
            done += 1

            if done % PROGRESS_EVERY == 0:
                pct = i / total * 100
                print(f"  [{i}/{total}] {pct:.0f}%  —  {compressed_total/1024/1024:.1f}MB written so far")

        except Exception as exc:
            print(f"  [error] {src.name}: {exc}")
            failed += 1

    if done:
        ratio = (1 - compressed_total / original_total) * 100
        print(
            f"  Done:    {done} compressed  ({original_total/1024/1024:.1f}MB → "
            f"{compressed_total/1024/1024:.1f}MB, {ratio:.0f}% reduction)"
        )
    if skipped:
        print(f"  Skipped: {skipped} already exist")
    if failed:
        print(f"  Failed:  {failed}")


def compress_audio() -> None:
    """Compress MP3 audio to 48kbps mono using ffmpeg."""
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH — skipping audio compression.")
        print("Install with: brew install ffmpeg")
        return

    AUDIO_OUT.mkdir(parents=True, exist_ok=True)

    sources = list(AUDIO_IN.glob("*.mp3"))
    if not sources:
        print("No MP3 files found in media/audio/")
        return

    skipped = done = failed = 0
    original_total = compressed_total = 0
    total = len(sources)
    PROGRESS_EVERY = max(1, total // 20)  # print every ~5%

    print(f"Compressing {total} audio files → {AUDIO_OUT}")

    for i, src in enumerate(sources, start=1):
        dest = AUDIO_OUT / src.name
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue

        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(src),
                    "-ac",
                    "1",  # mono
                    "-b:a",
                    AUDIO_BITRATE,
                    str(dest),
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode()[-200:])

            original_total += src.stat().st_size
            compressed_total += dest.stat().st_size
            done += 1

            if done % PROGRESS_EVERY == 0:
                pct = i / total * 100
                print(f"  [{i}/{total}] {pct:.0f}%  —  {compressed_total/1024/1024:.1f}MB written so far")

        except Exception as exc:
            print(f"  [error] {src.name}: {exc}")
            failed += 1

    if done:
        ratio = (1 - compressed_total / original_total) * 100
        print(
            f"  Done:    {done} compressed  ({original_total/1024/1024:.1f}MB → "
            f"{compressed_total/1024/1024:.1f}MB, {ratio:.0f}% reduction)"
        )
    if skipped:
        print(f"  Skipped: {skipped} already exist")
    if failed:
        print(f"  Failed:  {failed}")


def print_banner() -> None:
    title = "20 Compress media"
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def main() -> None:
    print_banner()
    parser = argparse.ArgumentParser(
        description="Compress media files (images and audio) for Anki decks."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--images", action="store_true", help="Compress images only")
    group.add_argument("--audio", action="store_true", help="Compress audio only")
    args = parser.parse_args()

    if args.images:
        compress_images()
    elif args.audio:
        compress_audio()
    else:
        compress_images()
        print()
        compress_audio()


if __name__ == "__main__":
    main()
