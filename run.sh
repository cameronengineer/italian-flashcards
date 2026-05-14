#!/bin/bash
set -e

source .venv/bin/activate

python scripts/1_import_subtlex_it.py
python scripts/2_create_verb_word_entries.py
python scripts/3_create_noun_word_entries.py
python scripts/4_create_verb_forms.py
python scripts/5_create_noun_phrases.py
python scripts/6_create_card_items.py
python scripts/7_create_anki_cards.py
# python scripts/8_generate_images.py
# python scripts/9_create_decks.py
