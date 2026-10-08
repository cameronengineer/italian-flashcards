"""``media`` commands — ElevenLabs audio and AI images for every card, then
compressed variants for deck packaging. Voice, models and compression
settings come from ``settings.toml``; image prompts go through the shared
AI layer (``tasks.IMAGE_PROMPT``) and are cached like every other AI answer.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from contextlib import closing
from pathlib import Path

from PIL import Image
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

from ..ai import AI
from ..db import connect, managed_decks
from ..paths import (
    AUDIO_DIR, AUDIO_DIR_COMPRESSED,
    IMAGE_DIR, IMAGE_DIR_COMPRESSED,
    ELEVENLABS_KEY_FILE,
    ensure_dirs,
)
from ..pool import run_pool
from ..settings import settings
from ..tasks import IMAGE_PROMPT
from ..util import audio_filename, image_filename, load_key_file, print_banner


def _audio_texts(conn, decks: list[str] | None = None) -> list[str]:
    if decks:
        placeholders = ",".join("?" * len(decks))
        rows = conn.execute(
            f"SELECT DISTINCT audio_text FROM cards"
            f" WHERE audio_text IS NOT NULL AND audio_text != ''"
            f" AND deck IN ({placeholders})",
            decks,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT audio_text FROM cards WHERE audio_text IS NOT NULL AND audio_text != ''"
        ).fetchall()
    return sorted({r["audio_text"] for r in rows})


def _image_jobs(conn) -> list[dict]:
    """One image per distinct image_text, with one of its cards as context.

    Restricted to ``en_to_it`` rows: the prompt labels ``front_text`` as
    English and ``back_highlight`` as Italian, which is backwards on the
    ``it_to_en`` twin (SQLite's GROUP BY would otherwise pick either).
    """
    rows = conn.execute(
        """
        SELECT image_text, MIN(sort_order), front_text, front_labels,
               back_highlight, back_text, deck
        FROM cards
        WHERE image_text IS NOT NULL AND image_text != ''
          AND direction = 'en_to_it'
        GROUP BY image_text
        """
    ).fetchall()
    return [
        {
            "image_key": r["image_text"],
            "front_text": r["front_text"],
            "front_labels": r["front_labels"] or "",
            "back_highlight": r["back_highlight"],
            "back_text": r["back_text"] or "",
            "deck": r["deck"],
        }
        for r in rows
    ]


# ── Audio generation ─────────────────────────────────────────────────────────
def _gen_audio(client: ElevenLabs, text: str, dest: Path) -> None:
    cfg = settings.audio
    audio_bytes = client.text_to_speech.convert(
        voice_id=cfg.voice_id,
        text=text,
        model_id=cfg.model,
        output_format=cfg.format,
        language_code=cfg.language,
        voice_settings=VoiceSettings(
            stability=cfg.stability, similarity_boost=cfg.similarity_boost,
            style=cfg.style, speed=cfg.speed,
        ),
    )
    if not isinstance(audio_bytes, (bytes, bytearray)):
        audio_bytes = b"".join(audio_bytes)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(audio_bytes)
    tmp.replace(dest)


def generate_audio(workers: int = 10, limit: int | None = None, decks: list[str] | None = None) -> dict:
    print_banner("media: generate audio (ElevenLabs)")
    ensure_dirs()
    with closing(connect()) as conn:
        texts = _audio_texts(conn, decks=decks)
    pending_all = [
        t for t in texts
        if not ((AUDIO_DIR / audio_filename(t)).exists()
                and (AUDIO_DIR / audio_filename(t)).stat().st_size > 0)
    ]
    pending = pending_all[:limit] if limit is not None else pending_all
    done_count = len(texts) - len(pending_all)
    done_pct = (done_count / len(texts) * 100) if texts else 0
    pend_pct = (len(pending_all) / len(texts) * 100) if texts else 0
    limit_note = f"; processing {len(pending)} this run" if limit is not None and len(pending_all) > len(pending) else ""
    print(f"  {len(texts)} unique audio strings; {done_count} generated ({done_pct:.1f}%), {len(pending_all)} pending ({pend_pct:.1f}%){limit_note}.")
    if not pending:
        return {"generated": 0, "failed": 0}
    api_key = load_key_file(ELEVENLABS_KEY_FILE)
    client = ElevenLabs(api_key=api_key)

    def work(text: str) -> str:
        _gen_audio(client, text, AUDIO_DIR / audio_filename(text))
        return audio_filename(text)

    generated = failed = 0
    for _, res in run_pool(
        pending, work, workers=workers, label="audio",
        describe=lambda t: t[:60],
    ):
        if isinstance(res, Exception):
            failed += 1
        else:
            generated += 1
    print(f"  done: generated={generated}, failed={failed}")
    return {"generated": generated, "failed": failed}


def generate_audio_per_deck(per_deck: int, workers: int = 5) -> dict:
    """Up to ``per_deck`` new audio files for every managed deck.

    Spreads a small daily budget across all decks instead of draining it on
    whichever deck sorts first (replaces scripts/manual_audio_generate.sh).
    """
    with closing(connect()) as conn:
        decks = managed_decks(conn)
    totals = {"generated": 0, "failed": 0}
    for deck in decks:
        res = generate_audio(workers=workers, limit=per_deck, decks=[deck])
        for k in totals:
            totals[k] += res.get(k, 0)
    print(f"\n  all decks: generated={totals['generated']}, failed={totals['failed']}")
    return totals


# ── Image generation ─────────────────────────────────────────────────────────
def _image_task(job: dict):
    return IMAGE_PROMPT.task({
        "english": job["front_text"],
        "labels": job["front_labels"],
        "italian": job["back_highlight"],
        "details": job["back_text"] or None,
    })


def _save_image(data: bytes, dest: Path) -> None:
    # Write-then-rename so an interrupted run leaves no truncated file that
    # later runs would treat as done.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)


def generate_images(workers: int = 10, limit: int | None = None) -> dict:
    print_banner("media: generate images")
    ensure_dirs()
    with closing(connect()) as conn:
        jobs = _image_jobs(conn)
    pending_all = [
        j for j in jobs
        if not (
            (IMAGE_DIR_COMPRESSED / image_filename(j["image_key"], "jpg")).exists()
            and (IMAGE_DIR_COMPRESSED / image_filename(j["image_key"], "jpg")).stat().st_size > 0
        ) and not (
            (IMAGE_DIR / image_filename(j["image_key"])).exists()
            and (IMAGE_DIR / image_filename(j["image_key"])).stat().st_size > 0
        )
    ]
    pending = pending_all[:limit] if limit is not None else pending_all
    done_count = len(jobs) - len(pending_all)
    done_pct = (done_count / len(jobs) * 100) if jobs else 0
    pend_pct = (len(pending_all) / len(jobs) * 100) if jobs else 0
    limit_note = f"; processing {len(pending)} this run" if limit is not None and len(pending_all) > len(pending) else ""
    print(f"  {len(jobs)} unique images; {done_count} generated ({done_pct:.1f}%), {len(pending_all)} pending ({pend_pct:.1f}%){limit_note}.")
    if not pending:
        return {"generated": 0, "failed": 0}
    print_lock = threading.Lock()
    with closing(connect()) as conn:
        ai = AI(conn)  # image prompts are cached; images live on disk

        def work(job: dict) -> bool:
            dest = IMAGE_DIR / image_filename(job["image_key"])
            prompt = ai.run(_image_task(job))["prompt"].strip()
            data = ai.image(prompt) if prompt else None
            if data:
                _save_image(data, dest)
            with print_lock:
                print(f"  [{'ok' if data else 'fail'}] {job['image_key']!r}")
            return bool(data)

        generated = failed = 0
        for _, res in run_pool(
            pending, work, workers=workers, label="images",
            describe=lambda j: j["image_key"],
        ):
            if isinstance(res, Exception):
                failed += 1
            elif res:
                generated += 1
            else:
                failed += 1
    print(f"  done: generated={generated}, failed={failed}")
    return {"generated": generated, "failed": failed}


# ── Compression ──────────────────────────────────────────────────────────────
def _compress_image(src: Path) -> tuple[int, int]:
    dest = IMAGE_DIR_COMPRESSED / (src.stem + ".jpg")
    if dest.exists() and dest.stat().st_size > 0:
        return 0, 0
    # Write-then-rename: an interrupted run must not leave a truncated file
    # that every later run treats as done.
    tmp = dest.with_name(dest.stem + ".tmp.jpg")
    with Image.open(src) as img:
        img = img.convert("RGB")
        px = settings.images.compressed_max_px
        img.thumbnail((px, px), Image.LANCZOS)
        img.save(tmp, "JPEG", quality=settings.images.compressed_quality, optimize=True)
    tmp.replace(dest)
    return src.stat().st_size, dest.stat().st_size


def _compress_audio(src: Path) -> tuple[int, int]:
    dest = AUDIO_DIR_COMPRESSED / src.name
    if dest.exists() and dest.stat().st_size > 0:
        return 0, 0
    tmp = dest.with_name(dest.stem + ".tmp.mp3")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-b:a", settings.audio.compressed_bitrate, str(tmp)],
        capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.decode()[-200:])
    tmp.replace(dest)
    return src.stat().st_size, dest.stat().st_size


def compress(workers: int = 8) -> dict:
    print_banner("media: compress")
    ensure_dirs()
    out: dict = {}
    for label, srcs, work_fn in (
        ("images", [p for p in IMAGE_DIR.glob("*.png") if ".tmp" not in p.name], _compress_image),
        ("audio", [p for p in AUDIO_DIR.glob("*.mp3") if ".tmp" not in p.name], _compress_audio),
    ):
        if label == "audio" and not shutil.which("ffmpeg"):
            print("  ffmpeg not on PATH — skipping audio compression.")
            continue
        if not srcs:
            print(f"  no {label} to compress.")
            continue
        done = skipped = failed = 0
        orig = comp = 0
        for _src, res in run_pool(
            srcs, work_fn, workers=workers, label=label,
            progress_every=max(1, len(srcs) // 10),
            describe=lambda p: p.name,
        ):
            if isinstance(res, Exception):
                failed += 1
                continue
            o, c = res
            if o == 0:
                skipped += 1
            else:
                done += 1
                orig += o
                comp += c
        out[label] = {"done": done, "skipped": skipped, "failed": failed}
        if done:
            saved = (1 - comp / orig) * 100 if orig else 0
            print(
                f"  {label}: compressed {done}  "
                f"({orig/1024/1024:.1f}MB → {comp/1024/1024:.1f}MB, "
                f"{saved:.0f}% reduction)  skipped={skipped} failed={failed}"
            )
    return out


def run(workers: int = 10, limit: int | None = None) -> dict:
    out = {}
    out["audio"] = generate_audio(workers=workers, limit=limit)
    out["images"] = generate_images(workers=workers, limit=limit)
    out["compress"] = compress(workers=workers)
    return out
