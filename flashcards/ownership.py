"""What the pipeline owns in Anki: its tag, its generated tags, its decks.

Personal tags (and Anki's own ``leech``) are never touched by a sync.
"""

OWNER_TAG = "fc::owned"
GENERATED_PREFIXES = ("fc::", "list::", "pos::", "tense::", "verb::", "noun::")
GENERATED_TAGS = {"cognate", "verb-form", "pattern", "noun-phrase", "cognate-rule", "mistake", "adopted"}


def managed_tag(tag):
    return tag.startswith(GENERATED_PREFIXES) or tag in GENERATED_TAGS


def tags_for(existing, wanted):
    return sorted({t for t in existing if not managed_tag(t)} | set(wanted) | {OWNER_TAG})


def deck_owned(deck: str, legacy=()):
    return deck.startswith("Italian::") or deck in legacy
