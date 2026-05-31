"""``media`` command — generate ElevenLabs audio + AI images for every card,
then produce compressed variants for deck packaging.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

from ..db import connect
from ..openrouter import request_chat
from ..paths import (
    AUDIO_DIR, AUDIO_DIR_COMPRESSED,
    IMAGE_DIR, IMAGE_DIR_COMPRESSED,
    ELEVENLABS_KEY_FILE, OPENROUTER_KEY_FILE,
    ensure_dirs,
)
from ..pool import run_pool
from ..util import audio_filename, image_filename, load_key_file, print_banner

# ── Audio ────────────────────────────────────────────────────────────────────
VOICE_ID = "HuK8QKF35exsCh2e7fLT"
ELEVENLABS_MODEL = "eleven_multilingual_v2"
ELEVENLABS_FORMAT = "mp3_44100_128"
LANGUAGE_CODE = "it"
VOICE_SETTINGS = VoiceSettings(stability=0.5, similarity_boost=1.0, style=1.0, speed=0.7)

# ── Image ────────────────────────────────────────────────────────────────────
PROMPT_MODEL = "~google/gemini-flash-latest"
IMAGE_MODEL = "sourceful/riverflow-v2-fast"

# ── Compression ──────────────────────────────────────────────────────────────
IMAGE_MAX_PX = 512
IMAGE_QUALITY = 75
AUDIO_BITRATE = "48k"


def _audio_texts(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT audio_text FROM cards WHERE audio_text IS NOT NULL AND audio_text != ''"
    ).fetchall()
    return sorted({r["audio_text"] for r in rows})


def _image_jobs(conn) -> list[dict]:
    """One image per distinct image_text. Use the most-frequent card as context."""
    rows = conn.execute(
        """
        SELECT image_text, front_text, front_labels, back_highlight, back_text, deck
        FROM cards
        WHERE image_text IS NOT NULL AND image_text != ''
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
    audio_bytes = client.text_to_speech.convert(
        voice_id=VOICE_ID,
        text=text,
        model_id=ELEVENLABS_MODEL,
        output_format=ELEVENLABS_FORMAT,
        language_code=LANGUAGE_CODE,
        voice_settings=VOICE_SETTINGS,
    )
    if not isinstance(audio_bytes, (bytes, bytearray)):
        audio_bytes = b"".join(audio_bytes)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(audio_bytes)
    tmp.replace(dest)


def generate_audio(workers: int = 10, limit: int | None = None) -> dict:
    print_banner("media: generate audio (ElevenLabs)")
    ensure_dirs()
    with connect() as conn:
        texts = _audio_texts(conn)
    pending = [
        t for t in texts
        if not ((AUDIO_DIR / audio_filename(t)).exists()
                and (AUDIO_DIR / audio_filename(t)).stat().st_size > 0)
    ]
    if limit is not None:
        pending = pending[:limit]
    print(f"  {len(texts)} unique audio strings; {len(pending)} pending.")
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


# ── Image generation ─────────────────────────────────────────────────────────
def _visual_prompt(api_key: str, entry: dict) -> str | None:
    user_content = (
        f"- English: {entry['front_text']}\n"
        f"- Type / context: {entry['front_labels']}\n"
        f"- Italian: {entry['back_highlight']}"
        + (f"\n- Italian infinitive: {entry['back_text']}" if entry["back_text"] else "")
        + "\n\nWrite the image generation prompt."
    )
    system_content = (
        "You generate image prompts for Italian language flashcard illustrations. "
        "Given a flashcard's data, write a single specific image generation prompt "
        "(2–3 sentences) for a flat design, minimalist icon-style illustration. "
        "The Italian word/phrase takes precedence when English is ambiguous. "
        "Simple, clear, suitable for a language learner. "
        "STRICTLY NO TEXT, letters, numbers, or labels in the image. "
        "Respond with only the visual concept prompt, nothing else."
    )
    try:
        return request_chat(
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            api_key=api_key,
            model=PROMPT_MODEL,
            timeout=60,
        ).strip() or None
    except Exception as exc:  # noqa: BLE001
        print(f"  [prompt-fail] {entry['image_key']!r}: {exc}")
        return None


def _gen_image(api_key: str, prompt: str, dest: Path) -> bool:
    payload = {
        "model": IMAGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Italian Flashcards",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
        result = json.loads(body)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        print(f"  [image-fail] {dest.name}: {exc}")
        return False
    images = (result.get("choices") or [{}])[0].get("message", {}).get("images")
    if not images:
        return False
    url = images[0]["image_url"]["url"]
    if not url.startswith("data:image/"):
        return False
    _, encoded = url.split(",", 1)
    # Write atomically so an interrupted run (SIGINT, disk full) leaves either
    # no file or a complete file. Otherwise generate_images' "skip if file
    # exists and is non-empty" check would treat a partial PNG as done.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(base64.b64decode(encoded))
    tmp.replace(dest)
    return True


def generate_images(workers: int = 10, limit: int | None = None) -> dict:
    print_banner("media: generate images")
    ensure_dirs()
    with connect() as conn:
        jobs = _image_jobs(conn)
    pending = [
        j for j in jobs
        if not (IMAGE_DIR / image_filename(j["image_key"])).exists()
        or (IMAGE_DIR / image_filename(j["image_key"])).stat().st_size == 0
    ]
    if limit is not None:
        pending = pending[:limit]
    print(f"  {len(jobs)} unique images; {len(pending)} pending.")
    if not pending:
        return {"generated": 0, "failed": 0}
    api_key = load_key_file(OPENROUTER_KEY_FILE)
    print_lock = threading.Lock()

    def work(job: dict) -> bool:
        dest = IMAGE_DIR / image_filename(job["image_key"])
        vp = _visual_prompt(api_key, job)
        if not vp:
            return False
        ok = _gen_image(api_key, vp, dest)
        with print_lock:
            tag = "ok" if ok else "fail"
            print(f"  [{tag}] {job['image_key']!r}")
        return ok

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
    with Image.open(src) as img:
        img = img.convert("RGB")
        img.thumbnail((IMAGE_MAX_PX, IMAGE_MAX_PX), Image.LANCZOS)
        img.save(dest, "JPEG", quality=IMAGE_QUALITY, optimize=True)
    return src.stat().st_size, dest.stat().st_size


def _compress_audio(src: Path) -> tuple[int, int]:
    dest = AUDIO_DIR_COMPRESSED / src.name
    if dest.exists() and dest.stat().st_size > 0:
        return 0, 0
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-b:a", AUDIO_BITRATE, str(dest)],
        capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode()[-200:])
    return src.stat().st_size, dest.stat().st_size


def compress(workers: int = 8) -> dict:
    print_banner("media: compress")
    ensure_dirs()
    out: dict = {}
    for label, srcs, work_fn in (
        ("images", list(IMAGE_DIR.glob("*.png")), _compress_image),
        ("audio", list(AUDIO_DIR.glob("*.mp3")), _compress_audio),
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
