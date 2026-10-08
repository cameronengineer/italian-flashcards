"""Images through Codex CLI on your ChatGPT plan (no API key, no per-image bill).

``codex exec`` runs non-interactively in a scratch directory with its
built-in image generation; we ask it to save one PNG there, then copy it
into ``media/images/<md5(image_key)>.png``. Plan limits and outages pause
image work (the same wait-and-retry gate as Claude); text AI is unaffected.
Existing images are never overwritten or deleted.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

from .ai import AIError, AIUnavailable, _Gate, _classify
from .paths import IMAGE_DIR
from .settings import settings
from .util import md5_hex

CODEX = "codex"
_gate = _Gate("Codex")
_slots = threading.BoundedSemaphore(max(1, settings.images.codex_concurrency))
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")

STYLE = (
    "Flat design, minimalist, icon-like illustration with soft colours on a plain light "
    "background, square format. Original artwork only: no text, letters, numbers, captions, "
    "logos, brands, flags with writing, or recognisable copyrighted characters."
)


def logged_in() -> bool:
    if not shutil.which(CODEX):
        return False
    try:
        out = subprocess.run([CODEX, "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (out.stdout + out.stderr).lower()
    return out.returncode == 0 and "not logged in" not in text


def image_prompt(italian: str, meaning: str, pos: str | None) -> str:
    style = STYLE
    if pos == "num":
        subject = f"the number {meaning}"
        style = STYLE.replace("no text, letters, numbers, captions,",
                              f"the numeral {meaning} drawn as a friendly graphic is the only text allowed; no words, captions,")
    elif pos == "letter":
        subject = f"the letter {meaning} of the alphabet"
        style = STYLE.replace("no text, letters, numbers, captions,",
                              f"the single letter {meaning} drawn as a friendly graphic is the only text allowed; no words, captions,")
    else:
        subject = f"the meaning of the Italian {pos or 'word'} '{italian}' — {meaning}"
        if pos in ("conj", "prep", "pron", "article", "adv", "intj", "phrase"):
            subject += " (an abstract idea: show a simple scene or symbol that suggests it)"
    return (
        "Use your image generation tool to create exactly one image for a language-learning "
        f"flashcard. Subject: {subject}. {style} Save the image as card.png in the current "
        "directory. Do not create or modify any other files and do not run other commands."
    )


def _newest_image(*dirs: Path, since: float) -> Path | None:
    found = []
    for d in dirs:
        if d.exists():
            found += [p for p in d.rglob("*") if p.suffix.lower() in _IMG_EXT and p.stat().st_mtime >= since]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _run(prompt: str, dest: Path, timeout: int) -> bool:
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="codex-img-") as tmp:
        cmd = [CODEX, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
               "-C", tmp, "--sandbox", "workspace-write", prompt]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise AIError("Codex CLI not installed (npm i -g @openai/codex)") from exc
        except subprocess.TimeoutExpired as exc:
            raise AIError(f"codex exec: no image within {exc.timeout}s") from exc
        out = (proc.stdout or "") + (proc.stderr or "")
        img = _newest_image(Path(tmp), Path.home() / ".codex" / "generated_images", since=started)
        if img is None:
            if proc.returncode != 0 or "error" in out.lower():
                raise _classify(f"codex exec: {out.strip()[-400:]}")
            raise AIError(f"codex exec produced no image: {out.strip()[-200:]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(".tmp.png")
        with Image.open(img) as im:
            im.convert("RGB").save(tmp_dest, "PNG")
        if dest.exists():  # never overwrite an existing image
            tmp_dest.unlink(missing_ok=True)
            return True
        tmp_dest.replace(dest)
        return True


def generate(image_key: str, italian: str, meaning: str, pos: str | None, *, timeout: int | None = None) -> Path:
    """Generate the image for ``image_key`` (skips if it already exists)."""
    dest = IMAGE_DIR / f"{md5_hex(image_key.strip())}.png"
    if dest.exists():
        return dest
    prompt = image_prompt(italian, meaning, pos)

    def once() -> bool:
        with _slots:
            return _run(prompt, dest, timeout or settings.images.codex_timeout)

    _gate.call(once)
    return dest


__all__ = ["generate", "logged_in", "image_prompt", "AIUnavailable"]
