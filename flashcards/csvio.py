"""Minimal CSV reader. Tolerates ``italian,english`` in either column order."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CsvRow:
    italian: str
    english: str
    index: int  # 1-based row position within the source (for stable sort)


def _detect_encoding(path: Path) -> str:
    try:
        path.read_bytes().decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def read(csv_path: Path) -> list[CsvRow]:
    """Read a CSV with header row {italian, english}. Order in header may vary."""
    rows: list[CsvRow] = []
    with csv_path.open(newline="", encoding=_detect_encoding(csv_path)) as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return rows
        fieldnames = {f.strip().lower(): f for f in reader.fieldnames}
        it_key = fieldnames.get("italian")
        en_key = fieldnames.get("english")
        if not it_key or not en_key:
            raise ValueError(
                f"{csv_path}: header must include 'italian' and 'english' "
                f"columns; got {reader.fieldnames!r}"
            )
        for i, row in enumerate(reader, start=1):
            italian = (row.get(it_key) or "").strip()
            english = (row.get(en_key) or "").strip()
            if not italian:
                continue
            rows.append(CsvRow(italian=italian, english=english, index=i))
    return rows
