"""Deterministic Italian: everything a rule can do instead of a model.

* ``normalise_spelling`` — Wiktionary marks stress on inner vowels
  ("vàdo", "andàto"); standard spelling only writes final accents
  ("andrò", "perché").
* Articles, articulated prepositions, possessives, demonstratives.
* Compound tenses from auxiliary + participle / gerund.
* Numbers to words.
* Cognate detection (Jaro similarity after suffix rewrites — the idea from
  the old ``italiananki`` "transferable words" pipeline).
* Tokenising subtitle text into words (elisions split off).
"""

from __future__ import annotations

import re
import unicodedata

VOWELS = "aeiouàèéìíòóùú"
_STRIP = {"à": "a", "è": "e", "é": "e", "ì": "i", "í": "i", "ò": "o", "ó": "o", "ù": "u", "ú": "u"}

# ── Spelling ───────────────────────────────────────────────────────────────


#: Monosyllables that standard spelling writes with an accent.
ACCENTED_MONOSYLLABLES = {
    "è", "dà", "dì", "là", "lì", "né", "sé", "sì", "tè", "ciò", "già", "giù",
    "più", "può", "piè", "scià",
}


def _monosyllabic(word: str) -> bool:
    return len(re.findall(r"[aeiouàèéìíòóùú]+", word.lower())) <= 1


def normalise_spelling(text: str) -> str:
    """Drop stress marks that standard Italian spelling doesn't write.

    Accents survive only on a word's final letter (città, andrò, perché, è).
    Wiktionary's final "ì"/"ù"/"à"/"ò" are standard; a final acute "é" is
    standard too (perché); a final "ó"/"í"/"ú" is normalised to grave.
    """
    out = []
    for word in re.split(r"(\W+)", text):
        chars = list(word)
        letters = [i for i, ch in enumerate(chars) if ch.isalpha()]
        last = letters[-1] if letters else -1
        drop_final = _monosyllabic(word) and word.lower() not in ACCENTED_MONOSYLLABLES
        for i, ch in enumerate(chars):
            if ch in _STRIP and (i != last or drop_final):
                chars[i] = _STRIP[ch]
            elif i == last and ch in "óíú":
                chars[i] = {"ó": "ò", "í": "ì", "ú": "ù"}[ch]
        out.append("".join(chars))
    return "".join(out)


def plain(text: str) -> str:
    """Lowercase, accents removed — for matching only, never for display."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


# ── Articles & friends ─────────────────────────────────────────────────────

_LO_START = re.compile(r"^(s[^aeiouàèéìòù]|z|gn|ps|pn|x|y|i[aeiou])", re.I)


def _starts_vowel(word: str) -> bool:
    return bool(word) and word[0].lower() in VOWELS + "h"


def definite_article(word: str, gender: str, plural: bool = False) -> str:
    """'il', 'lo', "l'", 'la', 'i', 'gli', 'le'."""
    if gender.startswith("f"):
        if plural:
            return "le"
        return "l'" if _starts_vowel(word) else "la"
    if plural:
        return "gli" if (_starts_vowel(word) or _LO_START.match(word)) else "i"
    if _starts_vowel(word):
        return "l'"
    return "lo" if _LO_START.match(word) else "il"


def indefinite_article(word: str, gender: str) -> str:
    if gender.startswith("f"):
        return "un'" if _starts_vowel(word) else "una"
    return "uno" if _LO_START.match(word) else "un"


def with_article(article: str, word: str) -> str:
    return f"{article}{word}" if article.endswith("'") else f"{article} {word}"


_PREP_BASE = {"di": "de", "a": "a", "da": "da", "in": "ne", "su": "su"}


def articulated(prep: str, article: str) -> str:
    """di + il = del, a + gli = agli, in + l' = nell' …"""
    base = _PREP_BASE[prep]
    if article == "il":
        return base + "l"
    if article in ("i", "gli"):
        return base + article  # dei, degli, agli, negli, sugli
    return base + "l" + article  # dello, dell', della, delle


def articulated_phrase(prep: str, word: str, gender: str, plural: bool = False) -> str:
    art = definite_article(word, gender, plural)
    return with_article(articulated(prep, art), word)


_POSSESSIVE = {
    "mio": ("mio", "mia", "miei", "mie"),
    "tuo": ("tuo", "tua", "tuoi", "tue"),
    "suo": ("suo", "sua", "suoi", "sue"),
    "nostro": ("nostro", "nostra", "nostri", "nostre"),
    "vostro": ("vostro", "vostra", "vostri", "vostre"),
    "loro": ("loro", "loro", "loro", "loro"),
}
FAMILY = {
    "padre", "madre", "fratello", "sorella", "figlio", "figlia", "marito", "moglie",
    "zio", "zia", "cugino", "cugina", "nonno", "nonna", "nipote", "suocero",
    "suocera", "cognato", "cognata", "genero", "nuora",
}


def possessive_phrase(owner: str, word: str, gender: str, plural: bool = False) -> str:
    fem = gender.startswith("f")
    form = _POSSESSIVE[owner][(2 if plural else 0) + (1 if fem else 0)]
    # Singular family nouns take no article (mio padre) — except with loro.
    if not plural and word in FAMILY and owner != "loro":
        return f"{form} {word}"
    art = definite_article(form, gender, plural)
    return with_article(art, f"{form} {word}")


def demonstrative_phrase(which: str, word: str, gender: str, plural: bool = False) -> str:
    if which == "questo":
        fem = gender.startswith("f")
        if not plural and _starts_vowel(word):
            return f"quest'{word}"
        return f"{('queste' if fem else 'questi') if plural else ('questa' if fem else 'questo')} {word}"
    art = definite_article(word, gender, plural)  # quello follows the article
    form = {"il": "quel", "lo": "quello", "l'": "quell'", "la": "quella",
            "i": "quei", "gli": "quegli", "le": "quelle"}[art]
    return with_article(form, word)


# ── Verbs ──────────────────────────────────────────────────────────────────

PERSONS = ("io", "tu", "lui_lei", "noi", "voi", "loro")
AVERE = {"presente": ("ho", "hai", "ha", "abbiamo", "avete", "hanno"),
         "condizionale": ("avrei", "avresti", "avrebbe", "avremmo", "avreste", "avrebbero")}
ESSERE = {"presente": ("sono", "sei", "è", "siamo", "siete", "sono"),
          "condizionale": ("sarei", "saresti", "sarebbe", "saremmo", "sareste", "sarebbero")}
STARE = ("sto", "stai", "sta", "stiamo", "state", "stanno")
REFLEXIVE = ("mi", "ti", "si", "ci", "vi", "si")


def compound(aux: str, mood: str, participle: str, person: str, reflexive: bool = False) -> tuple[str, str]:
    """(display, audio) for passato prossimo / condizionale passato.

    With essere the participle agrees: display shows both genders
    ("sono andato/a"), audio says the masculine ("sono andato").
    """
    i = PERSONS.index(person)
    use_essere = reflexive or aux == "essere"
    aux_form = (ESSERE if use_essere else AVERE)[mood][i]
    if use_essere and participle.endswith("o"):
        stem = participle[:-1]
        display_pp = f"{stem}o/a" if i < 3 else f"{stem}i/e"
        audio_pp = participle if i < 3 else stem + "i"
    else:
        display_pp = audio_pp = participle
    pre = f"{REFLEXIVE[i]} " if reflexive else ""
    return f"{pre}{aux_form} {display_pp}", f"{pre}{aux_form} {audio_pp}"


def progressive(gerund: str, person: str, reflexive: bool = False) -> str:
    i = PERSONS.index(person)
    pre = f"{REFLEXIVE[i]} " if reflexive else ""
    return f"{pre}{STARE[i]} {gerund}"


def strip_clitic(form: str, reflexive: bool) -> str:
    """lavatosi → lavato, lavandosi → lavando (Wiktionary attaches 'si')."""
    if reflexive and form.endswith("si") and len(form) > 4:
        return form[:-2]
    return form


def conjugation_class(infinitive: str, presente_io: str | None) -> str:
    inf = infinitive.removesuffix("si")
    if inf.endswith("are"):
        return "-are"
    if inf.endswith("ere"):
        return "-ere"
    if inf.endswith("ire"):
        return "-ire (isc)" if (presente_io or "").endswith("isco") else "-ire"
    return "other"


# ── Numbers ────────────────────────────────────────────────────────────────

_UNITS = ["zero", "uno", "due", "tre", "quattro", "cinque", "sei", "sette", "otto", "nove",
          "dieci", "undici", "dodici", "tredici", "quattordici", "quindici", "sedici",
          "diciassette", "diciotto", "diciannove"]
_TENS = ["", "", "venti", "trenta", "quaranta", "cinquanta", "sessanta", "settanta",
         "ottanta", "novanta"]


def _below_1000(n: int) -> str:
    if n < 20:
        return _UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        word = _TENS[tens]
        if unit in (1, 8):
            word = word[:-1]  # ventuno, ventotto
        return word + (_UNITS[unit] if unit else "")
    hundreds, rest = divmod(n, 100)
    head = "cento" if hundreds == 1 else _UNITS[hundreds] + "cento"
    if not rest:
        return head
    tail = _below_1000(rest)
    if tail.startswith("ott"):  # centottanta, centotto
        head = head[:-1]
    return head + tail


def number_words(n: int) -> str:
    """Italian cardinal in words: 23 → 'ventitré', 1021 → 'milleventuno'."""
    if n < 0:
        return "meno " + number_words(-n)
    if n < 1000:
        words = _below_1000(n)
    elif n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        head = "mille" if thousands == 1 else _below_1000(thousands) + "mila"
        words = head + (_below_1000(rest) if rest else "")
    else:
        millions, rest = divmod(n, 1_000_000)
        head = "un milione" if millions == 1 else number_words(millions) + " milioni"
        words = head + (" " + number_words(rest) if rest else "")
    if n > 3 and words.endswith("tre"):
        words = words[:-1] + "é"  # ventitré, centotré
    return words


# ── Cognates ───────────────────────────────────────────────────────────────


def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(la, lb) // 2 - 1
    am, bm = [False] * la, [False] * lb
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - window), min(lb, i + window + 1)):
            if not bm[j] and b[j] == ch:
                am[i] = bm[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    t = k = 0
    for i in range(la):
        if am[i]:
            while not bm[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    return (matches / la + matches / lb + (matches - t / 2) / matches) / 3


#: Italian suffix → English suffix. Order matters: longer first.
COGNATE_RULES: list[tuple[str, str, str]] = [
    ("zione", "tion", "-zione = -tion"), ("sione", "sion", "-sione = -sion"),
    ("ità", "ity", "-ità = -ity"), ("tà", "ty", "-tà = -ty"),
    ("logia", "logy", "-logia = -logy"), ("grafia", "graphy", "-grafia = -graphy"),
    ("ismo", "ism", "-ismo = -ism"), ("ista", "ist", "-ista = -ist"),
    ("mente", "ly", "-mente = -ly"), ("bile", "ble", "-bile = -ble"),
    ("enza", "ence", "-enza = -ence"), ("anza", "ance", "-anza = -ance"),
    ("ente", "ent", "-ente = -ent"), ("ante", "ant", "-ante = -ant"),
    ("oso", "ous", "-oso = -ous"), ("osa", "ous", "-oso = -ous"),
    ("ico", "ic", "-ico = -ic"), ("ica", "ic", "-ico = -ic"),
    ("ivo", "ive", "-ivo = -ive"), ("iva", "ive", "-ivo = -ive"),
    ("ario", "ary", "-ario = -ary"), ("orio", "ory", "-orio = -ory"),
    ("ura", "ure", "-ura = -ure"), ("ale", "al", "-ale = -al"),
    ("ore", "or", "-ore = -or"), ("are", "ate", "-are = -ate (verbs)"),
]


def cognate(italian: str, english: str) -> tuple[float, str | None]:
    """(similarity, rule) between an Italian lemma and an English gloss word.

    Similarity is the best Jaro score with or without a suffix rewrite; the
    rule is returned only when the rewrite is what made them match.
    """
    it, en = plain(italian), plain(english)
    base = jaro(it, en)
    best, rule = base, None
    for it_suf, en_suf, name in COGNATE_RULES:
        if it.endswith(plain(it_suf)):
            score = jaro(it[: -len(plain(it_suf))] + en_suf, en)
            if score > best + 0.02:
                best, rule = score, name
    return best, rule


# ── Tokenising subtitle text ───────────────────────────────────────────────

_TOKEN = re.compile(r"[a-zA-ZàèéìíòóùúÀÈÉÌÒÙ]+'?|[0-9]+", re.U)


def tokens(text: str) -> list[str]:
    """Words with elided prefixes split off: "c'è" → ["c'", "è"]."""
    return _TOKEN.findall(text.replace("’", "'"))
