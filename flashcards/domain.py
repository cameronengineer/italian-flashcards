"""Input contracts shared by readers and application services."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ListDef:
    id: str
    kind: str
    title: str
    deck: str
    path: Path | None
    type: str | None = None
    pos: str | None = None
    facts: bool = True
    recognition_only: bool = False
    extras: dict = field(default_factory=dict)


@dataclass
class Item:
    raw: str
    lemma: str
    pos: str | None
    english: str = ""
    gender: str | None = None
    display: str | None = None
    context: dict = field(default_factory=dict)
