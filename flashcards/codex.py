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
import time
import json
import re
from pathlib import Path

from PIL import Image

from .ai import AIError, AIUnavailable, _Gate, _classify
from .paths import IMAGE_DIR
from .settings import settings
from .util import md5_hex
from .assets import valid_image
from .runtime import run_process
from .pool import STOP

CODEX = "codex"
_gate = _Gate("Codex")

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
        style = STYLE.replace(
            "no text, letters, numbers, captions,",
            f"the numeral {meaning} drawn as a friendly graphic is the only text allowed; no words, captions,",
        )
    elif pos == "letter":
        subject = f"the letter {meaning} of the alphabet"
        style = STYLE.replace(
            "no text, letters, numbers, captions,",
            f"the single letter {meaning} drawn as a friendly graphic is the only text allowed; no words, captions,",
        )
    else:
        subject = f"the meaning described by this JSON data: {json.dumps({'italian': italian, 'meaning': meaning, 'pos': pos}, ensure_ascii=False)}"
        if pos in ("conj", "prep", "pron", "article", "adv", "intj", "phrase"):
            subject += " (an abstract idea: show a simple scene or symbol that suggests it)"
    return (
        "Use your image generation tool to create exactly one image for a language-learning "
        f"flashcard. Subject: {subject}. {style} Save the image as card.png in the current "
        "directory (copy the generated file there if your tool saved it elsewhere). Do not "
        "create or modify any other files."
    )


_PATH = re.compile(r"(/[^\s\"'`<>|]+\.(?:png|jpe?g|webp))", re.I)


def _reported_image(output: str, since: float) -> Path | None:
    """The image this run reported saving (its own output names the file), so
    concurrent runs sharing ~/.codex/generated_images never swap images."""
    for raw in reversed(_PATH.findall(output)):
        p = Path(raw)
        try:
            if p.is_file() and p.stat().st_mtime >= since and valid_image(p):
                return p
        except OSError:
            continue
    return None


def _run(prompt: str, dest: Path, timeout: int) -> bool:
    started = time.time() - 1
    with tempfile.TemporaryDirectory(prefix="codex-img-") as tmp:
        cmd = [
            CODEX,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-C",
            tmp,
            "--sandbox",
            "workspace-write",
            prompt,
        ]
        try:
            proc = run_process(cmd, timeout=timeout, stop=STOP)
        except FileNotFoundError as exc:
            raise AIError("Codex CLI not installed (npm i -g @openai/codex)") from exc
        except subprocess.TimeoutExpired as exc:
            raise AIError(f"codex exec: no image within {exc.timeout}s") from exc
        out = (proc.stdout or "") + (proc.stderr or "")
        img = Path(tmp) / "card.png"
        if proc.returncode == 0 and not valid_image(img):
            img = _reported_image(out, started) or img
        if proc.returncode != 0 or not valid_image(img):
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


def generate(
    image_key: str, italian: str, meaning: str, pos: str | None, *, timeout: int | None = None
) -> Path:
    """Generate the image for ``image_key`` (skips if it already exists)."""
    dest = IMAGE_DIR / f"{md5_hex(image_key.strip())}.png"
    if dest.exists():
        if not valid_image(dest):
            raise AIError(f"Existing image is invalid; preserved for recovery: {dest}")
        return dest
    prompt = image_prompt(italian, meaning, pos)

    _gate.call(lambda: _run(prompt, dest, timeout or settings.images.codex_timeout))
    return dest


__all__ = ["generate", "logged_in", "image_prompt", "AIUnavailable"]
