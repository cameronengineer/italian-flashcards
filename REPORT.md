# Italian Flashcards — Code & Data Review Report

**Date:** 2026-05-15 (updated 2026-05-17)  
**Database:** `database.sqlite`  
**Scope:** All scripts (`scripts/`), the SQLite database, and pipeline configuration. `freqdic/` excluded.

---

## Database State Summary

| Table | Rows |
|---|---|
| `input_words` | 19,988 |
| `word_entries` | 1,810 (504 verbs, 1,285 nouns, 21 other) |
| `verb_forms` | 11,088 |
| `noun_phrases` | 4,772 |
| `card_items` | 16,364 |
| `anki_cards` | 16,364 |

**Media on disk:** ~8,140 images, ~20,275 audio files.  
**Generated decks:** 6 `.apkg` files in `decks/`.

---

## Recommendations

### 2. BUG — Malformed possessive phrases for vowel-initial nouns (90 affected rows)

**Severity: High**

In `scripts/5_create_noun_phrases.py`, the `POSSESSIVES` dictionary builds possessive forms using the bare `l'` article as a key but constructs phrases like `"l' mio accordo"` instead of the correct `"il mio accordo"`. This is because the possessive forms for nouns that take `l'` (e.g., `accordo`, `ufficio`, `inizio`) get the `l'` article prepended before the possessive adjective.

**Root cause:** `phrase_join("l' mio", "accordo")` → `"l' mio accordo"` because the code uses `phrase_join(poss_map[entry.definite_singular], entry.singular)` but the POSSESSIVES dict for the `l'` slot already contains the full possessive form (e.g., `"l' mio"`), which then has the noun appended with a space. The actual correct Italian is `"il mio accordo"` (nouns taking `l'` in the definite use `il/la` with possessives except for family nouns).

There are **90 malformed possessive phrases** in the database (all have `italian LIKE 'l'' %'`), producing **90 bad cards**.

**Fix:** Possessive forms for nouns whose definite singular is `l'` should use `il`/`la` article with the possessive (standard Italian: `il mio accordo`, not `l' mio accordo`). Update the `POSSESSIVES` dict entry for `"l'"` to return `"il mio"`, `"la mia"`, etc. based on grammatical gender.

---

### 3. BUG — `scripts/5_create_noun_phrases.py` idempotency guard is too broad

**Severity: Medium**

The early-exit check is:

```python
if noun_phrase_count > 0 and noun_entry_count > 0:
    print(f"Already have {noun_phrase_count} noun_phrases ... Exiting.")
    return
```

This exits as soon as *any* phrases exist, even if new noun entries have been added since the last run. The script will never generate phrases for newly added nouns without manually deleting the guard or all existing phrases.

**Fix:** Mirror the approach used in `scripts/4_create_verb_forms.py`, which queries specifically for verb entries *without* forms. The `load_entries()` function already does this correctly (filters `NOT EXISTS (SELECT 1 FROM noun_phrases WHERE ...)`). Remove the early-exit block entirely and rely on the query to find only unphrased entries.

---

### 4. DATA QUALITY — 42 proper-noun entries have no articles and generate zero cards

**Severity: Medium**

42 noun entries (all proper nouns: `adam`, `alex`, `angeles`, `barry`, etc.) have `NULL` for `definite_singular`, `definite_plural`, and `indefinite_singular`. Because `deterministic_phrases()` only generates definite phrases when `entry.definite_singular` is non-empty, these nouns produce no `noun_phrases` rows and therefore no cards.

**Options:**
- Skip proper nouns (NPR `dom_pos`) entirely in `scripts/3_create_noun_word_entries.py` — they make poor vocabulary flashcards.

---

### 5. DATA QUALITY — 100 nouns produce only 2 phrases instead of 4

**Severity: Medium**

100 nouns (mostly proper nouns and place names: `africa`, `alan`, `alice`, `america`, etc.) produce only 2 `noun_phrases` rows each — one `definite` and one variant — because they have no plural form. The definite plural and variant plural are skipped when `entry.plural` is empty.

These cards are technically usable but incomplete. This is directly related to recommendation #4 — proper nouns should likely be excluded from the noun card workflow.

---

### 7. DATA QUALITY — Nouns classified from `dom_pos=NPR` are mostly proper nouns

**Severity: Medium**

The noun entry script includes `dom_pos IN ('NOM', 'NPR')`, so proper nouns like `emma`, `alan`, `gary`, `michael`, `washington`, `chicago`, `roma` are being added as nouns. These generate articles like `"l'emma"`, `"la chicago"`, which are grammatically awkward and not useful flashcard content.

**Fix:** Either exclude `NPR` rows entirely

---

### 8. CODE QUALITY — Duplicate `load_api_key()` and `lemma_id()` functions across scripts

**Severity: Low**

Scripts `2`, `3`, `4`, and `5` each define their own `load_api_key()` function and scripts `2` and `3` define identical `lemma_id()` functions. Scripts `10` and `11` both inline the MD5 hash logic.

**Fix:** Extract shared utilities into a `scripts/common.py` module:

```python
# scripts/common.py
import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def load_api_key(path: Path) -> str: ...
def media_hash(text: str) -> str: ...
def lemma_id(lemma: str) -> str: ...
def audio_filename(text: str) -> str: ...
def image_filename(text: str, ext: str = "jpg") -> str: ...
```

---

### 10. CODE QUALITY — Silent error swallowing in `scripts/10_generate_images.py`

**Severity: Medium**

Both `generate_prompt()` and `generate_image()` catch all exceptions with `pass`:

```python
except requests.HTTPError as exc:
    pass  # Silent fail, will retry
except Exception as exc:
    pass  # Will retry
```

This makes it impossible to diagnose API failures. The commented-out `exc` variable is never logged.

**Fix:** Log the exception, at minimum:

```python
except requests.HTTPError as exc:
    print(f"  [warn] HTTP {exc.response.status_code}: {exc}")
except Exception as exc:
    print(f"  [warn] Unexpected error: {exc}")
```

---

### 12. SCHEMA — `card_items` has no `UNIQUE` constraint to prevent duplicates on re-runs

**Severity: Medium**

`card_items` uses `INSERT` (not `INSERT OR IGNORE`) and has no unique constraint on `(source_type, source_id)`. Script `6_create_card_items.py` guards against duplicates with a `NOT EXISTS` subquery, which works correctly. However, if that guard were ever removed or bypassed, duplicate card items would silently accumulate.

**Fix:** Add a unique constraint to the schema:

```sql
UNIQUE(source_type, source_id)
```

This makes the table self-protecting and allows `INSERT OR IGNORE` to replace the `NOT EXISTS` pattern.

---

### 15. PIPELINE — Script `12_create_decks.py` referenced in `13_compress_media.py` docstring but does not exist

**Severity: Low**

The docstring in `scripts/13_compress_media.py` says:

```
12_create_decks.py reads from the compressed folders preferentially,
```

There is no `12_create_decks.py`. The actual deck creation script is `14_create_decks.py`. This is a stale reference from a renumbering of scripts.

**Fix:** Update the docstring to reference `14_create_decks.py`.

---

## Issues Found in Second Audit Pass (2026-05-17)

### A. BUG — `POSSESSIVES` dict produces wrong articles for `gli`-plural and `lo`-singular nouns

**Severity: High — affects 59 card_items / 118 anki_cards**

In `scripts/6_create_noun_phrases.py`, the `POSSESSIVES` dict (line 40–47) has two bugs:

1. **`gli` plural key:** Produces `gli miei accordi`, `gli tuoi spiriti`, etc. — wrong. The correct Italian is `i miei accordi`, `i tuoi spiriti`. When the definite plural is `gli`, the possessive article must change to `i` (not remain `gli`).
2. **`lo` singular key:** Produces `lo mio spirito`, `lo tuo scherzo`, etc. — wrong. The correct Italian is `il mio spirito`, `il tuo scherzo`. When the definite singular is `lo`, the possessive article must change to `il` (not remain `lo`).

**Affected nouns:** 141 nouns have `definite_plural='gli'`; 31 have both `definite_singular='lo'` and `definite_plural='gli'`. Exception: `saio` (lo/i) has correct noun_phrases since `i` maps correctly.

**Current wrong dict entries (all 6 possessives):**
```python
"mio": { ..., "gli": "gli miei", "lo": "lo mio", ... }
# should be:
"mio": { ..., "gli": "i miei",   "lo": "il mio",  ... }
```

**Fix:** Change every `"gli": "gli X"` entry to `"gli": "i X"` and every `"lo": "lo X"` entry to `"lo": "il X"` for all 6 possessives. Then delete all affected `noun_phrases` rows (phrase_type='possessive' for all gli/lo nouns) and re-run `scripts/6_create_noun_phrases.py`.

---

### B. BUG — `accedere` has wrong auxiliary and misspelled past participle

**Severity: High — 6 verb_form rows, 12 anki_cards wrong**

`word_entries` row for `accedere` has:
- `auxiliary = 'essere'` — wrong; `accedere` takes `avere`
- `past_participle = 'asceduto'` — wrong; correct is `acceduto`

All 6 passato prossimo verb_forms read `sono asceduto/a`, `sei asceduto/a`, etc. — both the auxiliary and the past participle are wrong.

**Fix:**
```sql
UPDATE word_entries SET auxiliary='avere', past_participle='acceduto' WHERE lemma='accedere';
DELETE FROM verb_forms WHERE word_entry_id = (SELECT id FROM word_entries WHERE lemma='accedere');
```
Then re-run `scripts/4_create_verb_forms.py` (or insert correct forms directly) and re-run `scripts/8_create_card_items_nouns_verbs.py` + `scripts/9_create_anki_cards.py`.

---

### C. BUG — `assomigliare` uses `essere` auxiliary (should be `avere`)

**Severity: Medium — 6 verb_form rows wrong**

`assomigliare` (to resemble) takes `avere` in standard Italian: `ho assomigliato a mia madre`. The DB has `auxiliary='essere'`, producing `sono assomigliato/a`, which is incorrect.

**Fix:** Same pattern as accedere — update word_entry, delete verb_forms, regenerate.

---

### D. BUG — `appartenere` uses `essere` auxiliary (should be `avere`)

**Severity: Medium — 6 verb_form rows wrong**

`appartenere` (to belong) takes `avere` in modern standard Italian: `ha appartenuto a noi`. The DB has `auxiliary='essere'`.

**Fix:** Same pattern as accedere.

---

### E. DATA QUALITY — `scattare` uses `essere` (ambiguous)

**Severity: Low — technically debatable**

`scattare` is listed with `essere` in the DB. In intransitive uses meaning "to go off / spring" it can take `essere` in some registers, but `avere` is also widely used. The current entry is defensible but learners may encounter both. No fix needed unless a single auxiliary must be chosen — `avere` is more common in practice.

---

### F. DATA QUALITY — Stub proper-noun entries generating low-quality cards

**Severity: Medium**

Several noun entries are pure proper nouns or personal names with no meaningful flashcard value. Some generate cards like "the Boston → la boston" or "the Bob → il bob":

| lemma | English | card_items |
|---|---|---|
| `alice` | Alice | 2 |
| `bob` | Bob | 2 |
| `boston` | Boston | 2 |
| `charlotte` | Charlotte | 2 |
| `jack` | Jack | 2 |
| `west` | West | 2 |
| `henry` | Henry | 0 (no phrases — missing article fields) |
| `bibbia` | Bible | 2 |
| `cristo` | Christ | 4 |
| `dna` | DNA | 4 |

`bibbia` and `cristo` could be kept (religious vocabulary). `dna` and `tv` are useful common loanwords and their cards look reasonable. `alice`, `bob`, `charlotte`, `jack`, `henry`, `boston`, `west` are English proper names that appeared in the frequency corpus as common loanwords/film references; their cards ("the Alice", "the Bob") are not useful Italian vocabulary.

**Fix options:**
1. Delete the `word_entries` for `alice`, `bob`, `charlotte`, `jack`, `henry`, `boston`, `west` (and cascade-delete noun_phrases/card_items/anki_cards). These 9 entries contribute 14 low-quality cards.
2. Alternatively, keep `boston` and `west` as they appear in common Italian expressions.

---

### G. DATA QUALITY — `nozze` missing `definite_singular` and `indefinite_singular`

**Severity: Low**

`nozze` is a *pluralia tantum* (used only in plural). The DB has:
- `definite_singular = NULL` — correct (no singular form exists)
- `indefinite_singular = NULL` — correct (no singular form exists)
- `definite_plural = 'le'` ✓

Only 2 noun_phrases exist: `le nozze` (the wedding) and `le sue nozze` (his/her wedding). This is correct behaviour — there is no singular form to generate cards for. **No fix needed.**

---

### H. DATA QUALITY — Awkward `plural_english` values for uncountable or mass nouns

**Severity: Low — cosmetic**

Several nouns have `plural_english` values that read unnaturally in English because the nouns are uncountable, events, or mass nouns. The plural_english appears in possessive plural card backs (e.g. "your listenings", "your golds", "our mornings").

Affected nouns and suggested fixes:

| lemma | current `plural_english` | suggested fix |
|---|---|---|
| `ascolto` | listenings | listening sessions |
| `oro` | golds | gold pieces |
| `mattina` | mornings | mornings *(OK)* |
| `mattino` | mornings | mornings *(OK)* |
| `sera` | evenings | evenings *(OK)* |
| `serata` | evenings | evenings *(OK)* |
| `inizio` | beginnings | beginnings *(OK)* |
| `sentimento` | feelings | feelings *(OK)* |
| `buonasera` | good evenings | good evening greetings |
| `buongiorno` | good mornings | good morning greetings |
| `carter` | casings | casings *(OK)* |
| `nozze` | weddings | weddings *(OK)* |
| `significato` | meanings | meanings *(OK)* |

Genuinely awkward: `ascolto` → "listenings", `oro` → "golds", `buonasera` → "good evenings", `buongiorno` → "good mornings".

---

### I. AUXILIARY CORRECTNESS — `costare`, `bastare`, `durare`, `spettare`, `bisognare`

**Severity: Low — these are correct or defensible**

These impersonal/weather-type verbs are listed with `essere`. In standard Italian grammar:
- `costare` → takes `essere` in intransitive use (`è costato caro`) ✓
- `bastare` → takes `essere` (`è bastato`) ✓  
- `durare` → takes `essere` (`è durato`) ✓
- `spettare` → takes `essere` (`è spettato a lui`) ✓
- `bisognare` → impersonal, takes `essere` (`è bisognato`) ✓
- `convenire` → takes `essere` in impersonal use (`è convenuto`) ✓
- `fallire` → takes `essere` in intransitive "to go bankrupt" ✓

**No fix needed for these verbs.**

---