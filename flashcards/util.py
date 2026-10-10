"""Hashing, identifier, and small utility helpers."""

from __future__ import annotations

import hashlib
import re


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def media_hash(text: str) -> str:
    """Content-addressed media filename hash. Stable across DB rebuilds."""
    return md5_hex(text.strip())


def audio_filename(text: str) -> str:
    from dataclasses import asdict
    import json
    from .settings import settings, AudioSettings

    spec = asdict(settings.audio)
    baseline = asdict(AudioSettings())
    spec.pop("compressed_bitrate")
    baseline.pop("compressed_bitrate")
    key = text if spec == baseline else text.strip() + "::tts::" + json.dumps(spec, sort_keys=True)
    return f"{media_hash(key)}.mp3"


def compressed_audio_name(name: str) -> str:
    from .settings import settings

    bitrate = settings.audio.compressed_bitrate
    return name if bitrate == "48k" else name.removesuffix(".mp3") + f".{bitrate}.mp3"


def image_compression_suffix() -> str:
    from .settings import settings

    cfg = settings.images
    return (
        ""
        if (cfg.compressed_max_px, cfg.compressed_quality) == (512, 75)
        else f".{cfg.compressed_max_px}q{cfg.compressed_quality}"
    )


def image_filename(key: str, ext: str = "png") -> str:
    return f"{media_hash(key)}{image_compression_suffix() if ext == 'jpg' else ''}.{ext}"


def slugify(text: str) -> str:
    """Filesystem-safe slug from a deck name (e.g. 'Italian - Verbs' → 'italian_verbs')."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "deck"


def print_banner(title: str) -> None:
    line = "-" * len(title)
    print(f"\n{line}\n{title}\n{line}", flush=True)


def load_key_file(path) -> str:
    p = path
    if not p.exists():
        raise FileNotFoundError(f"API key file not found: {p}\nCreate it with: echo 'your-key-here' > {p}")
    key = p.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"API key file is empty: {p}")
    return key


def table(headers: list[str], rows: list[list], *, total: list | None = None) -> str:
    """A boxed plain-text table (used by every reporting command)."""
    cells = [[str(c) for c in r] for r in rows]
    foot = [str(c) for c in total] if total else None
    widths = [len(h) for h in headers]
    for r in cells + ([foot] if foot else []):
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def line(char: str = "-") -> str:
        return "+" + "+".join(char * (w + 2) for w in widths) + "+"

    def fmt(r: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths, strict=False)) + " |"

    out = [line(), fmt(headers), line("=")] + [fmt(r) for r in cells]
    if foot:
        out += [line(), fmt(foot)]
    out.append(line())
    return "\n".join(out)
