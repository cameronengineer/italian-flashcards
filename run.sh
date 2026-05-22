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

# Deduplicate backup files: keep the oldest copy of any identical database
# backup, remove newer duplicates. Uses shasum (built into macOS).
# Written without associative arrays to stay compatible with bash 3.2 (macOS default).
echo "Deduplicating database backups..."
if ls database.backup.*.sqlite 1>/dev/null 2>&1; then
    _seen_hashes_file=$(mktemp)
    while IFS= read -r _backup; do
        _hash=$(shasum -a 256 "$_backup" | awk '{print $1}')
        if grep -qF "$_hash" "$_seen_hashes_file" 2>/dev/null; then
            rm "$_backup"
            echo "  Removed duplicate: $_backup"
        else
            echo "$_hash" >> "$_seen_hashes_file"
            echo "  Kept:    $_backup"
        fi
    done < <(ls -rt database.backup.*.sqlite)  # -rt = oldest first
    rm -f "$_seen_hashes_file"
    unset _seen_hashes_file _backup _hash
fi

python scripts/01_import_subtlex_it.py
python scripts/02_import_input_words.py --workers 30
python scripts/03_import_numbers.py
python scripts/04_italki_import_verbs.py --workers 10
python scripts/05_italki_import_expressions.py --workers 10
python scripts/06_italki_create_verb_forms.py --workers 10
python scripts/07_create_verb_word_entries.py --workers 30
python scripts/08_create_noun_word_entries.py --workers 30
python scripts/09_create_verb_forms.py --workers 30
python scripts/10_create_noun_phrases.py --workers 30
python scripts/11_create_input_word_card_items.py
python scripts/12_create_avere_card_items.py
python scripts/13_create_card_items_nouns_verbs.py
python scripts/14_italki_create_card_items.py
python scripts/15_create_anki_cards.py
python scripts/16_sort_anki_cards.py
python scripts/17_randomize_anki_cards.py
python scripts/18_generate_images.py
python scripts/19_generate_audio.py --limit 500
python scripts/20_compress_media.py
python scripts/21_create_decks.py
python scripts/22_import_anki_decks.py
python scripts/23_delete_orphan_anki_cards.py
python scripts/24_reorder_anki_cards.py
