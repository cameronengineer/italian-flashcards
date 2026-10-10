"""Read-only asset lookup and validation. Existing media is never removed."""

from functools import lru_cache
from pathlib import Path
import hashlib
import json

from PIL import Image
from . import paths
from .util import sha256_file


@lru_cache(maxsize=32768)
def _valid_image(path: str, size: int, modified: int) -> bool:
    if size <= 0:
        return False
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def valid_image(path: Path) -> bool:
    try:
        st = path.stat()
        return _valid_image(str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return False


def locate(name: str) -> Path | None:
    if Path(name).name != name:
        raise ValueError("Media filename must not contain a path")
    for directory in (
        paths.IMAGE_DIR_COMPRESSED,
        paths.AUDIO_DIR_COMPRESSED,
        paths.AUDIO_DIR,
        paths.IMAGE_DIR,
    ):
        p = directory / name
        if p.is_file() and p.stat().st_size > 0:
            if p.suffix.lower() in (".png", ".jpg", ".jpeg") and not valid_image(p):
                continue
            return p
    return None


def image_for(key: str | None) -> Path | None:
    """Use the preferred variant, retaining a usable legacy JPEG as fallback."""
    from .util import image_filename, md5_hex

    if not key:
        return None
    names = (image_filename(key, "jpg"), image_filename(key, "png"), md5_hex(key.strip()) + ".jpg")
    for name in dict.fromkeys(names):
        if path := locate(name):
            return path
    return None


def record(conn, path: Path, *, subject: str, kind: str, spec: dict, source="generated"):
    encoded = json.dumps(spec, sort_keys=True)
    conn.execute(
        """INSERT INTO asset_manifest(filename,subject,kind,spec_hash,checksum,metadata)
        VALUES(?,?,?,?,?,?) ON CONFLICT(filename) DO UPDATE SET checksum=excluded.checksum,metadata=excluded.metadata""",
        (
            path.name,
            subject,
            kind,
            hashlib.sha256(encoded.encode()).hexdigest(),
            sha256_file(path),
            json.dumps({"spec": spec, "source": source, "bytes": path.stat().st_size}),
        ),
    )
