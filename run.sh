#!/bin/bash
set -e

source .venv/bin/activate

# Backup the database before making any changes
DB="database.sqlite"
BACKUP="database.backup.$(date +%Y%m%d_%H%M%S).sqlite"
if [ -f "$DB" ]; then
    cp "$DB" "$BACKUP"
    echo "Backup created: $BACKUP"
fi

# Deduplicate backup files: hash every database.backup.*.sqlite, keep the oldest
# copy among any group of identical files, and remove the newer duplicates.
echo "Deduplicating database backups..."
python3 - <<'PYEOF'
import hashlib, os, glob

backups = sorted(glob.glob("database.backup.*.sqlite"), key=os.path.getmtime)
seen = {}  # sha256 -> (path, mtime)
for f in backups:
    with open(f, "rb") as fh:
        h = hashlib.sha256(fh.read()).hexdigest()
    mtime = os.path.getmtime(f)
    if h not in seen:
        seen[h] = (f, mtime)
        print(f"  Kept:    {f}")
    else:
        os.remove(f)
        print(f"  Removed duplicate (newer): {f}")
PYEOF

python scripts/1_import_subtlex_it.py
python scripts/2_import_input_words.py --workers 30
python scripts/2b_import_numbers.py
python scripts/3_create_verb_word_entries.py --workers 30
python scripts/4_create_noun_word_entries.py --workers 30
python scripts/5_create_verb_forms.py --workers 30
python scripts/6_create_noun_phrases.py --workers 30
python scripts/7_create_input_word_card_items.py
python scripts/7b_create_avere_card_items.py
python scripts/8_create_card_items_nouns_verbs.py
python scripts/9_create_anki_cards.py
python scripts/10_sort_anki_cards.py
python scripts/11_randomize_anki_cards.py
python scripts/12_generate_images.py
python scripts/13_generate_audio.py --limit 500
python scripts/15_compress_media.py
python scripts/16_create_decks.py
