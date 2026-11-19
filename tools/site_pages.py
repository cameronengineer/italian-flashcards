"""Public deck guides and consistent search metadata, generated without AI calls."""
import html
import json
import re
from xml.etree.ElementTree import Element, SubElement, tostring

ORIGIN = 'https://italianflashcards.cameronengineer.com'
CILS_HUB = '/italian-cils-anki-decks/'
VERB_HUB = '/italian-verb-conjugation-flashcards/'
CREATOR = {'@type': 'Person', '@id': ORIGIN + '/#creator', 'name': 'Cameron', 'url': 'https://blog.cameronengineer.com/',
           'sameAs': ['https://github.com/cameronengineer']}
# Search snippets are cut at about this many characters.
DESCRIPTION_LIMIT = 160
FAQ = [
 ('Are these Italian Anki decks free?', 'Yes. The decks can be downloaded without an account. Pictures and recordings are included in each .apkg file. Keep the decks non-commercial and retain the source credits.'),
 ('Which Italian vocabulary deck should I choose?', 'Choose a CILS level that fits your study, or a topic such as numbers or film vocabulary. The level decks include vocabulary from the levels below them, so downloading all four will repeat material.'),
 ('How do I import a deck into Anki?', 'Download the .apkg file, then open it with Anki. On a computer you can also use File → Import. Review the import summary before confirming, especially if you already use another deck from this collection.'),
 ('Do the cards include Italian pronunciation?', 'The downloadable packages include recordings and pictures. The voices and illustrations are AI-generated. The sample cards on this site let you listen before downloading.'),
 ('Can I practise a particular Italian verb tense?', 'Yes. Verb forms are organised into separate tense decks, including presente, passato prossimo, imperfetto, futuro semplice and both conditional tenses. Imperative decks are separated by person.'),
 ('Are the cards a complete Italian course?', 'No. These are study materials made for my own learning, rather than a complete course or an official CILS resource. AI-generated text can contain mistakes; use a dictionary or grammar reference when something seems unusual.'),
]
# English names of the tense decks, for titles and tables.
TENSES = {
 'presente': 'Present Tense',
 'passato_prossimo': 'Past Tense',
 'imperfetto': 'Imperfect Tense',
 'futuro_semplice': 'Future Tense',
 'presente_progressivo': 'Present Progressive',
 'imperativo_con_tu': 'Informal Imperative',
 'imperativo_con_lei': 'Formal Imperative',
 'imperativo_con_noi': '“Let’s” Imperative',
 'imperativo_con_voi': 'Plural Imperative',
 'condizionale_presente': 'Conditional',
 'condizionale_passato': 'Past Conditional',
}
LEVELS = {'a1': 'beginner', 'a2': 'elementary', 'b1': 'intermediate', 'b2': 'upper-intermediate'}
# Hand-written search text for the other decks: (h1, <title>, description, what it teaches).
OTHER = {
 'italian_basics_numbers': (
  'Italian Numbers Anki Deck', 'Italian Numbers Anki Deck · Free Flashcards with Audio',
  'Free Anki deck for Italian numbers, with extra practice on the easily confused teens and tens, such as undici. {cards} cards with audio, in both directions.',
  'Italian numbers'),
 'italian_core_fluent_forever_625': (
  'Italian Fluent Forever 625 Anki Deck', 'Italian Fluent Forever 625 Anki Deck · Free, with Audio',
  'Free Italian Anki deck based on Gabriel Wyner’s Fluent Forever 625 word list: family, food, home, body and time, with pictures and audio. {cards} cards.',
  'Core Italian vocabulary'),
 'italian_frequency': (
  'Most Common Italian Words Anki Deck', 'Most Common Italian Words Anki Deck · Frequency List',
  'Free Anki deck of the most common Italian verbs and nouns, ranked by how often they occur in film and TV subtitles (SUBTLEX-IT). {notes} words with audio.',
  'High-frequency Italian vocabulary'),
 'italian_noun_phrases': (
  'Italian Noun Phrases Anki Deck: Articles and Prepositions', 'Italian Articles & Prepositions Anki Deck · Noun Phrases',
  'Free Anki deck for Italian articles, possessives and prepositions with nouns: il, la, un, del, nel, mio. {notes} English → Italian phrases with audio.',
  'Italian articles, possessives and prepositions'),
 'italian_cognates': (
  'Italian Cognates Anki Deck', 'Italian–English Cognates Anki Deck · Free with Audio',
  'Free Anki deck of Italian words related to English ones, such as generoso = generous. {notes} cards with pictures and audio.',
  'Italian–English cognates'),
}


def slug(stem):
    return stem.removeprefix('italian_').replace('_', '-')


def deck_path(stem):
    return '/decks/italian-' + slug(stem) + '-anki-deck/'


def tense(stem):
    for prefix in ('italian_verbs_learning_', 'italian_verbs_more_tenses_'):
        if stem.startswith(prefix):
            return stem.removeprefix(prefix)
    return None


def heading_case(text):
    """'Passato prossimo · tu' → 'Passato Prossimo (tu)'; words in brackets keep their case."""
    text = re.sub(r' · (\w+)$', r' (\1)', text)
    return ' '.join(w if w.startswith('(') else w[:1].upper() + w[1:] for w in text.split())


def phrase(name):
    """A deck name inside a sentence: 'Imperativo · Lei' → 'imperativo with Lei'."""
    text = name.replace(' · ', ' with ')
    return text[:1].lower() + text[1:]


def fit(*sentences, limit=DESCRIPTION_LIMIT):
    """The first sentence, then each later one that still fits in a search snippet."""
    out = sentences[0]
    for sentence in sentences[1:]:
        if sentence and len(out) + 1 + len(sentence) <= limit:
            out += ' ' + sentence
    return out


def example(info):
    """A form of vedere (or the first verb) from a tense deck, as (Italian, English)."""
    conj = info.get('conjugations') or {}
    if conj:
        row = next(iter(conj.values()))[0]
        return row['italian'], row['english'].split(' / ')[0]
    verbs = info.get('verbs') or []
    verb = next((v for v in verbs if v['infinitive'] == 'vedere'), verbs[0] if verbs else None)
    return (verb['form'], verb['prompt']) if verb and 'form' in verb else None


def seo(stem, name, info, new_at):
    """(h1, <title>, meta description, what it teaches, parent hub as (label, path) or None)."""
    notes, cards = info['notes'], info['cards']
    if stem.startswith('italian_cils_'):
        level = stem.rsplit('_', 1)[-1]
        up = level.upper()
        lower = {'a1': '', 'a2': ', plus all A1 words', 'b1': ', plus all A1–A2 words', 'b2': ', plus all A1–B1 words'}[level]
        h1 = f'CILS {up} Italian Vocabulary Anki Deck'
        desc = fit(f'Free CILS {up} Anki deck: {new_at[level]:,} {LEVELS[level]} Italian words{lower}, with audio and pictures.',
                   'See real cards and the word list first.')
        return h1, h1 + ' · Free, with Audio', desc, f'Italian vocabulary for CILS {up}', ('CILS levels', CILS_HUB)
    t = tense(stem)
    if t:
        english = TENSES[t]
        h1 = f'Italian {heading_case(name)} Anki Deck'
        title = f'{h1} · {english} Flashcards'
        if len(title) > 60:
            title = f'{h1} · {english}'
        verbs = len(info.get('verbs', []))
        size = f'{verbs} common verbs' if notes == verbs else f'{notes:,} forms of {verbs} common verbs'
        ex = example(info)
        desc = fit(f'Free Anki deck for the Italian {phrase(name)} ({english.lower()}): {size}, with audio and pictures.',
                   f'Example: {ex[0]} = “{ex[1]}”.' if ex else '', 'See a real card first.')
        return h1, title, desc, f'Italian {phrase(name)} conjugation', ('Verb conjugation', VERB_HUB)
    if stem in OTHER:
        h1, title, desc, teaches = OTHER[stem]
        return h1, title, desc.format(notes=f'{notes:,}', cards=f'{cards:,}'), teaches, None
    # Film decks keep the wording they were published with.
    h1 = f'Italian {name} Anki Deck'
    return h1, h1, None, f'Italian vocabulary from {name}', None


def faq_html():
    return '<section id="faq"><h2>Questions about the decks</h2>' + ''.join(
        f'<details><summary>{html.escape(q)}</summary><p>{html.escape(a)}</p></details>' for q, a in FAQ
    ) + '</section>'


def crumbs_html(crumbs):
    """Visible breadcrumb: home, then (label, path) pairs; the last is this page."""
    e = html.escape
    parts = ['<a href="/">Italian flashcards</a>'] + [f'<a href="{p}">{e(n)}</a>' for n, p in crumbs[:-1]]
    return '<p class="crumbs">' + ' / '.join(parts + [e(crumbs[-1][0])]) + '</p>'


def word_list(rows):
    """Italian words with their English, in columns."""
    e = html.escape
    return '<ul class="word-list">' + ''.join(
        f'<li><span lang="it">{e(it)}</span> <small>{e(en)}</small></li>' for it, en in rows) + '</ul>'


def metadata(page, title, description, path, image='il-caffe'):
    e = html.escape
    url = ORIGIN + path
    page = re.sub(r'<title>.*?</title>', '<title>' + e(title) + '</title>', page)
    for key, value in [('description', description), ('og:title', title), ('og:description', description)]:
        attr = 'property' if key.startswith('og:') else 'name'
        page = re.sub(rf'<meta {attr}="{key}" content="[^"]*">', f'<meta {attr}="{key}" content="{e(value, quote=True)}">', page)
    page = re.sub(r'<meta property="og:image" content="[^"]*">', f'<meta property="og:image" content="{ORIGIN}/samples/{image}.jpg">', page)
    extra = f'''<link rel="canonical" href="{url}">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:url" content="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Italian Flashcards">
<meta property="og:locale" content="en_GB">
<meta property="og:image:width" content="512">
<meta property="og:image:height" content="384">
<meta property="og:image:alt" content="An illustration from the Italian flashcard collection">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title, quote=True)}">
<meta name="twitter:description" content="{e(description, quote=True)}">
<meta name="twitter:image" content="{ORIGIN}/samples/{image}.jpg">
<meta name="twitter:image:alt" content="An illustration from the Italian flashcard collection">
<script>
// Render static-site rules cannot match a hostname. Preserve the destination
// path, query and fragment when directing the legacy hostname to our domain.
if (location.hostname === 'italian-flashcards-lyh2.onrender.com') {{
 location.replace('{ORIGIN}' + location.pathname + location.search + location.hash);
}}
</script>
'''
    return page.replace('</head>', extra + '</head>')


def schema(page, title, path, items=None, crumbs=None, description=None, nodes=()):
    """JSON-LD: creator, site, this page, its breadcrumb, any list of decks, and ``nodes``."""
    url = ORIGIN + path
    node = {'@type': 'CollectionPage' if items else 'WebPage', '@id': url + '#page', 'url': url, 'name': title,
            'inLanguage': 'en', 'author': {'@id': ORIGIN + '/#creator'}, 'isPartOf': {'@id': ORIGIN + '/#website'}}
    if description:
        node['description'] = description
    graph = [
        CREATOR,
        {'@type': 'WebSite', '@id': ORIGIN + '/#website', 'url': ORIGIN + '/', 'name': 'Italian Flashcards',
         'inLanguage': 'en', 'publisher': {'@id': ORIGIN + '/#creator'}},
        node,
    ]
    if path != '/':
        trail = [('Italian Flashcards', '/')] + (crumbs or [(title, path)])
        node['breadcrumb'] = {'@id': url + '#breadcrumb'}
        graph.append({'@type': 'BreadcrumbList', '@id': url + '#breadcrumb', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'name': name, 'item': ORIGIN + link}
            for i, (name, link) in enumerate(trail, 1)
        ]})
    if items:
        graph.append({'@type': 'ItemList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'name': name, 'url': ORIGIN + link}
            for i, (name, link) in enumerate(items, 1)
        ]})
    if nodes:
        node['mainEntity'] = {'@id': nodes[0]['@id']}
        graph.extend(nodes)
    data = json.dumps({'@context': 'https://schema.org', '@graph': graph}, ensure_ascii=False).replace('<', '\\u003c')
    return page.replace('</head>', '<script type="application/ld+json">' + data + '</script>\n</head>')


def build(data, api):
    """Called after the homepage is rendered; local deck archives are optional."""
    site = api.SITE_DIR
    e = html.escape
    try:
        from .deck_notes import notes_for
    except ImportError:
        from deck_notes import notes_for
    homepage = (site / 'index.html').read_text()
    new_at = data['stats']['new_at']
    entries = [(stem, name, desc.format(**{f'new_{k}': f'{v:,}' for k, v in new_at.items()}))
               for _gid, _title, _blurb, rows in api.GROUPS for stem, name, desc in rows]
    # All guide links are ordinary HTML links and remain available without JS.
    for stem, name, _desc in entries:
        homepage = homepage.replace(f'<b>{e(name)}</b>', f'<b><a class="deck-title" href="{deck_path(stem)}">{e(name)}</a></b>')
    homepage = homepage.replace('Italian, a little<br>at a time.', 'Free Italian flashcards<br>for Anki.')
    homepage = homepage.replace('These are the flashcards I made for studying Italian.', 'Free Italian Anki flashcards for English speakers, made for my own Italian study.')
    homepage = homepage.replace('Shared freely for use in', 'Made by <a href="https://blog.cameronengineer.com/">Cameron</a>. Shared freely for use in')
    homepage = homepage.replace('\n</main>', '<section id="study-guides"><h2>Choosing and using Italian flashcards</h2><p>New to the collection? These guides explain the differences between the decks and how to use them alongside your other Italian study.</p><ul>'
                                f'<li><a href="{CILS_HUB}">CILS vocabulary decks: A1, A2, B1 and B2 compared</a></li>'
                                f'<li><a href="{VERB_HUB}">Italian verb conjugation flashcards, one tense at a time</a></li>'
                                '<li><a href="/italian-anki-decks-for-beginners/">Italian Anki decks for beginners: choosing a first deck</a></li><li><a href="/how-to-study-italian-with-anki/">How to study Italian with Anki flashcards</a></li></ul></section>\n</main>')
    homepage = homepage.replace('\n</main>', '\n' + faq_html() + '\n</main>')
    homepage = homepage.replace('</style>', '''.decks .deck-title { border:0; padding:0; font:inherit; text-decoration:underline; text-decoration-color:var(--line); }
.decks .deck-title:hover { background:transparent; text-decoration-color:var(--green); }
details { border-bottom:1px solid var(--line); padding:18px 0; max-width:800px; }
summary { cursor:pointer; color:var(--ink); font-weight:600; }
details p { margin:12px 0 0; font-size:15px; }
footer nav { flex-wrap:wrap; margin-bottom:24px; }
.guide { max-width:800px; padding:42px 0; }
.guide h1 { font-size:clamp(36px,5vw,58px); }
.guide h2 { font-size:30px; margin:28px 0 18px; }
.guide > h3 { font-size:22px; margin:22px 0 10px; }
.guide .sample { margin:25px 0; }
.guide .side:first-child .word { padding-top:16px; }
.guide .side audio { max-width:100%; }
.guide ol { margin:20px 0; }
.crumbs { font-size:14px; color:var(--muted); }
.preview-table { width:100%; border-collapse:collapse; margin:20px 0; font-size:15px; table-layout:fixed; overflow-wrap:anywhere; }
.preview-table th,.preview-table td { text-align:left; border-bottom:1px solid var(--line); padding:12px 10px; vertical-align:top; }
.preview-table th { color:var(--ink); }
.preview-table caption { text-align:left; font:20px var(--serif); color:var(--ink); padding:6px 0; }
.word-list { columns:3 180px; column-gap:28px; padding:0; margin:18px 0; list-style:none; font-size:15px; }
.word-list li { break-inside:avoid; padding:3px 0; }
.word-list small { color:var(--muted); font-size:13px; }
.guide .download { display:inline-block; border:1px solid var(--green); padding:10px 18px; border-radius:6px; margin:14px 0; }
</style>''')
    base = homepage
    home_title = 'Free Italian Anki Decks with Audio & Pictures · A1–B2, Verbs'
    home_desc = 'Free Italian Anki decks with audio and pictures: CILS A1–B2 vocabulary, verb conjugation by tense, numbers and more. See real cards before you download.'
    homepage = schema(metadata(homepage, home_title, home_desc, '/'), home_title, '/',
                      [(name, deck_path(stem)) for stem, name, _ in entries], description=home_desc)
    homepage = homepage.replace('<h3>Words by level</h3>', f'<h3><a href="{CILS_HUB}">CILS vocabulary by level</a></h3>')
    homepage = homepage.replace('<h3>Verbs, one tense at a time</h3>', f'<h3><a href="{VERB_HUB}">Italian verb conjugation</a></h3>')
    (site / 'index.html').write_text(homepage)
    urls = ['/']
    # Reuse the common shell without homepage-only interactive scripts.
    head = base[:base.index('<main id="main">')]
    head = head.replace('href="#"', 'href="/"').replace('href="#cards"', 'href="/#cards"').replace('href="#decks"', 'href="/#decks"').replace('href="#about"', 'href="/#about"')
    tail = base[base.index('<footer>'):base.index('<script>', base.index('<footer>'))]
    tail = tail.replace('Italian · A personal collection, shared freely.', 'Italian · By <a href="https://blog.cameronengineer.com/">Cameron</a>. A personal collection, shared freely.')

    def write(path, title, desc, content, image='il-caffe', crumbs=None, items=None, nodes=()):
        page = head + '<main id="main" class="guide">' + content + '</main>' + tail + '</body></html>'
        page = schema(metadata(page, title, desc, path, image), title, path, items, crumbs, desc, nodes)
        target = site / path.strip('/') / 'index.html'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page)
        urls.append(path)

    def sample_card(sample):
        return api._sample_html(sample).replace('src="samples/', 'src="/samples/').replace('href="samples/', 'href="/samples/')

    sample_map = {s['deck']: s for s in data['samples']}
    for stem, name, desc in entries:
        info = data['decks'][stem]
        h1, title, description, teaches, parent = seo(stem, name, info, new_at)
        if description is None:
            description = f'{desc} Download {info["cards"]:,} Italian Anki flashcards with pictures and audio. Includes a real example and import instructions.'
        sample = sample_map.get(stem)
        explanation = 'These cards ask for the Italian verb form from an English prompt. Each download focuses on one tense or imperative person.' if stem.startswith('italian_verbs_') else 'Vocabulary cards practise recognition and recall. Related meanings may share a word, and some vocabulary repeats across the collection.'
        if stem == 'italian_noun_phrases':
            explanation = 'These English → Italian drills practise articles, possessives and prepositions together with nouns, rather than isolated translations.'
        if stem == 'italian_cognates':
            explanation = 'These cards practise connections between Italian and English words. Similar spelling can help recall, but the card’s meaning still matters.'
        if stem.startswith('italian_movies_'):
            explanation = 'These recognition cards ask for the English meaning of Italian vocabulary from the film. Example subtitle lines include a word-for-word translation; they are study notes, not a substitute for watching the film.'
        related = [(other, label) for other, label, _ in entries if other != stem and (other.startswith('italian_cils_') if stem.startswith('italian_cils_') else other.startswith('italian_verbs_') if stem.startswith('italian_verbs_') else other in ('italian_basics_numbers', 'italian_core_fluent_forever_625'))]
        crumbs = ([parent] if parent else []) + [(name, deck_path(stem))]
        download = api.DOWNLOAD_BASE + info['file']
        body = crumbs_html(crumbs) + f'<h1>{e(h1)}</h1><p>{e(desc)}</p><h2>What this deck covers</h2><p>{e(explanation)}</p><p>{info["notes"]:,} notes · {info["cards"]:,} study cards · {api._size(info["bytes"])} · Anki .apkg file</p>'
        body += f'<a class="download" href="{e(download)}">Download {e(name)} ({api._size(info["bytes"])})</a>'
        if sample:
            body += '<h2>A card from this deck</h2>' + sample_card(sample)
        study_note = notes_for(stem)
        if study_note:
            heading, note = study_note
            body += f'<h2>{e(heading)}</h2><p>{e(note)}</p>'
            if 'imperativo' in stem:
                body += '<p>Grammar reference: <a href="https://www.treccani.it/enciclopedia/imperativo_%28Enciclopedia-dell%27Italiano%29/">Treccani on the imperativo</a> (in Italian).</p>'
            elif 'condizionale' in stem:
                body += '<p>Grammar reference: <a href="https://www.treccani.it/enciclopedia/condizionale_%28La-grammatica-italiana%29/">Treccani on the condizionale</a> (in Italian).</p>'
        verbs = info.get('verbs') or []
        conjugations = info.get('conjugations') or {}
        meanings = {v['infinitive']: v['english'] for v in verbs}
        if conjugations:
            body += f'<h2>{e(name)}: {e(" and ".join(conjugations))} in full</h2><p>Two verbs from the deck, one card per person. The English prompt is what the card shows; the Italian is the answer.</p>'
            for verb, rows in conjugations.items():
                body += (f'<table class="preview-table"><caption><span lang="it">{e(verb)}</span> · {e(meanings.get(verb, ""))}</caption>'
                         '<thead><tr><th scope="col">English prompt</th><th scope="col">Italian</th></tr></thead><tbody>'
                         + ''.join(f'<tr><td>{e(r["english"])}</td><td lang="it">{e(r["italian"])}</td></tr>' for r in rows)
                         + '</tbody></table>')
        if verbs and all('form' in v for v in verbs):
            body += (f'<h2>All {len(verbs)} verbs in this deck</h2><p>One card per verb: the English command is the prompt, the Italian {e(phrase(name))} form is the answer.</p>'
                     '<table class="preview-table"><thead><tr><th scope="col">Verb</th><th scope="col">English prompt</th><th scope="col">Italian</th></tr></thead><tbody>'
                     + ''.join(f'<tr><td><span lang="it">{e(v["infinitive"])}</span> <small>{e(v["english"])}</small></td><td>{e(v["prompt"])}</td><td lang="it">{e(v["form"])}</td></tr>' for v in verbs)
                     + '</tbody></table>')
        elif verbs:
            body += (f'<h2>The {len(verbs)} verbs in this deck</h2><p>Each form of each verb is its own card, with an English prompt.</p>'
                     + word_list((v['infinitive'], v['english']) for v in verbs))
        preview = info.get('preview', [])
        if preview and not verbs:
            if stem.startswith('italian_cils_'):
                level = stem.rsplit('_', 1)[-1]
                intro = f'{len(preview)} of the {new_at[level]:,} words that are new at {level.upper()}, spread from A to Z.'
                if level != 'a1':
                    intro += ' The deck also includes every word from the levels below.'
                heading = f'Words new at {level.upper()}'
            else:
                intro = f'{len(preview)} of the {info["notes"]:,} entries in the deck, spread from A to Z.'
                heading = 'Examples from the deck'
            body += f'<h2>{heading}</h2><p>{intro} These are individual prompts, not a lesson order.</p><table class="preview-table"><thead><tr><th scope="col">Italian</th><th scope="col">English prompt or meaning</th></tr></thead><tbody>'
            body += ''.join(f'<tr><td lang="it">{e(row["italian"])}</td><td>{e(row["english"])}</td></tr>' for row in preview)
            body += '</tbody></table>'
        body += '<p>For help choosing material, see <a href="/italian-anki-decks-for-beginners/">Italian Anki decks for beginners</a>. For review and import advice, see <a href="/how-to-study-italian-with-anki/">how to study Italian with Anki</a>.</p>'
        body += '<h2>How to use this deck</h2><ol><li>Install <a href="https://apps.ankiweb.net/">Anki</a> for your device.</li><li>Download the .apkg file above. Pictures and recordings are included.</li><li>Open the file with Anki, or choose File → Import on a computer. Check the import summary if you already have related decks.</li><li>Choose this deck in Anki and set a daily new-card limit that suits your study.</li></ol>'
        if stem.startswith('italian_cils_'):
            body += f'<h2>Choosing a CILS level</h2><p>The A1, A2, B1 and B2 decks follow CILS vocabulary lists. Higher levels include words from lower levels, so choose one level to avoid repeats. <a href="{CILS_HUB}">Compare the four CILS levels</a>. This is an independent study resource, not an official CILS deck or a complete exam course.</p>'
        body += '<h2>Sources and limitations</h2><p>I made these cards for my own Italian study. Dictionary data comes from Wiktionary via Kaikki; the frequency deck uses SUBTLEX-IT. Text, pictures and voices use AI and may contain mistakes. Keep source credits when sharing and use the decks non-commercially. <a href="/#about">Read about the material</a>.</p>'
        if related:
            body += '<h2>Related decks</h2><ul>' + ''.join(f'<li><a href="{deck_path(other)}">{e(label)}</a></li>' for other, label in related) + '</ul>'
            if stem.startswith('italian_verbs_'):
                body += f'<p>All tenses side by side: <a href="{VERB_HUB}">Italian verb conjugation flashcards</a>.</p>'
        image = sample['slug'] if sample else 'il-caffe'
        resource = {
            '@type': 'LearningResource', '@id': ORIGIN + deck_path(stem) + '#deck', 'name': h1, 'description': description,
            'url': ORIGIN + deck_path(stem), 'inLanguage': 'en', 'about': {'@type': 'Language', 'name': 'Italian'},
            'teaches': teaches, 'learningResourceType': 'Flashcards', 'isAccessibleForFree': True,
            'author': {'@id': ORIGIN + '/#creator'}, 'image': f'{ORIGIN}/samples/{image}.jpg',
            'encoding': {'@type': 'MediaObject', 'name': info['file'], 'contentUrl': download,
                         'encodingFormat': 'Anki package (.apkg)', 'contentSize': api._size(info['bytes'])},
        }
        if stem.startswith('italian_cils_'):
            resource['educationalLevel'] = 'CEFR ' + stem.rsplit('_', 1)[-1].upper()
        write(deck_path(stem), title, description, body, image, crumbs, nodes=[resource])

    verb_decks = [(stem, name, desc) for stem, name, desc in entries if stem.startswith('italian_verbs_')]
    body = crumbs_html([('Verb conjugation', VERB_HUB)]) + '<h1>Italian Verb Conjugation Flashcards for Anki</h1><p>Separate Anki decks for Italian verb tenses and imperative persons, with English prompts, Italian answers, pictures and recordings. Choose the tense you are studying rather than importing every form at once.</p>'
    body += '<h2>Tenses and moods</h2><table class="preview-table"><thead><tr><th scope="col">Deck</th><th scope="col">In English</th><th scope="col">Example</th><th scope="col">Cards</th></tr></thead><tbody>'
    for stem, name, _desc in verb_decks:
        info = data['decks'][stem]
        ex = example(info)
        shown = f'<span lang="it">{e(ex[0])}</span> · {e(ex[1])}' if ex else ''
        body += f'<tr><th scope="row"><a href="{deck_path(stem)}">{e(name)}</a></th><td>{e(TENSES[tense(stem)])}</td><td>{shown}</td><td>{info["cards"]:,}</td></tr>'
    body += '</tbody></table>'
    body += '<h2>How the practice works</h2><p>A prompt such as “I see” asks you to recall vedo. Reveal the answer, listen to the recording, and grade your recall in Anki. These drills supplement grammar study: they do not explain every use of each tense.</p>'
    body += sample_card(sample_map['italian_verbs_learning_presente'])
    covered = {}
    for stem, _name, _desc in verb_decks:
        for v in data['decks'][stem].get('verbs') or []:
            covered.setdefault(v['infinitive'], v['english'])
    if covered:
        body += f'<h2>The {len(covered)} verbs in the decks</h2><p>Common Italian verbs, regular and irregular. Not every verb appears in every tense deck; each deck page lists its own.</p>' + word_list(sorted(covered.items()))
    body += '<h2>Importing a tense deck</h2><p>Open any tense page above, download its .apkg file and import it into Anki. Media is included. Start with the tense you are learning and add others as your course or reading introduces them.</p><p>The cards are AI-assisted study materials and may contain errors. <a href="/#about">Sources and limitations</a>.</p>'
    verb_title = 'Italian Verb Conjugation Anki Decks · Free, Tense by Tense'
    write(VERB_HUB, verb_title, 'Practise Italian verb conjugation with free Anki decks for present, past, future, conditional and imperative forms. See examples and choose a tense.',
          body, 'vedo', [('Verb conjugation', VERB_HUB)], [(name, deck_path(stem)) for stem, name, _ in verb_decks])

    levels = [(stem, name) for stem, name, _desc in entries if stem.startswith('italian_cils_')]
    body = crumbs_html([('CILS levels', CILS_HUB)]) + '<h1>CILS Italian Vocabulary Anki Decks: A1, A2, B1 and B2</h1>'
    body += '<p>Four free Anki decks of Italian vocabulary based on CILS word lists, from A1 to B2, with pictures and recordings. Each level includes the words of the levels below it, so you only need to download one.</p>'
    body += '<h2>Compare the CILS levels</h2><table class="preview-table"><thead><tr><th scope="col">Level</th><th scope="col">New at this level</th><th scope="col">Words in the deck</th><th scope="col">Cards</th><th scope="col">Download</th></tr></thead><tbody>'
    for stem, name in levels:
        info = data['decks'][stem]
        body += f'<tr><th scope="row"><a href="{deck_path(stem)}">{e(name)}</a></th><td>{new_at[stem.rsplit("_", 1)[-1]]:,}</td><td>{info["notes"]:,}</td><td>{info["cards"]:,}</td><td>{api._size(info["bytes"])}</td></tr>'
    body += '</tbody></table><p>Most words have two cards, Italian → English and English → Italian, so a deck has about twice as many cards as words.</p>'
    body += '<h2>What is CILS?</h2><p>CILS (Certificazione di Italiano come Lingua Straniera) is a certificate in Italian as a foreign language from the <a href="https://cils.unistrasi.it/">Università per Stranieri di Siena</a>. It covers all six levels of the Common European Framework, A1 to C2; the B1 and B2 exams are called UNO-B1 and DUE-B2. These decks cover vocabulary for A1 to B2.</p>'
    body += '<h2>Which level should I download?</h2><ul>' + ''.join(
        f'<li><a href="{deck_path(stem)}">{e(name)}</a>: {e(desc)}</li>' for stem, name, desc in entries if stem.startswith('italian_cils_')) + '</ul>'
    body += '<p>Choose the level you are studying now. Each deck repeats the levels below it, so moving up later means importing a larger deck that includes words you have already studied.</p>'
    body += '<h2>Preparing for CILS B1 Cittadinanza</h2><p>CILS also has a separate B1 Cittadinanza exam for people applying for Italian citizenship, with its own format. The <a href="/decks/italian-cils-b1-anki-deck/">B1 deck</a> can help with vocabulary, but it does not practise the listening, reading, writing or speaking tasks. Check the current requirements with the office handling your application.</p>'
    body += '<h2>Words new at each level</h2><p>A few of the words each level adds, taken from the decks. Each deck page shows a longer list.</p>'
    for stem, name in levels:
        rows = api._spread(data['decks'][stem].get('preview', []), 12)
        body += f'<h3><a href="{deck_path(stem)}">{e(name)}</a></h3>' + word_list((r['italian'], r['english']) for r in rows)
    body += '<h2>What the decks do not cover</h2><p>These are independent study decks, not official CILS material, and they are not affiliated with the Università per Stranieri di Siena. They practise vocabulary only: grammar, listening and writing need other study. Text, pictures and voices are AI-assisted and may contain mistakes. <a href="/#about">Sources and limitations</a>.</p>'
    body += '<p>New to Anki? Read <a href="/italian-anki-decks-for-beginners/">choosing a first Italian deck</a> and <a href="/how-to-study-italian-with-anki/">how to study Italian with Anki</a>.</p>'
    write(CILS_HUB, 'CILS Anki Decks · Free Italian Vocabulary for A1, A2, B1, B2',
          'Free Anki decks for CILS Italian vocabulary at A1, A2, B1 and B2, with audio and pictures. Compare the levels, see the new words at each and pick one.',
          body, 'il-caffe', [('CILS levels', CILS_HUB)], [(name, deck_path(stem)) for stem, name in levels])

    try:
        from .study_guides import GUIDES
    except ImportError:
        from study_guides import GUIDES
    for path, title, description, content in GUIDES:
        body = '<p class="crumbs"><a href="/">Italian flashcards</a> / Study guide</p><h1>' + e(title) + '</h1><p>By <a href="https://blog.cameronengineer.com/">Cameron</a>, creator of this collection.</p>' + content
        if path == '/italian-anki-decks-for-beginners/':
            comparison = '<h2 id="compare-levels">Compare the Italian vocabulary decks</h2><p>Current package counts. Notes are vocabulary entries; some notes produce more than one study card. Level decks overlap.</p><table class="preview-table"><thead><tr><th scope="col">Deck</th><th scope="col">Notes</th><th scope="col">Cards</th><th scope="col">Download</th></tr></thead><tbody>'
            for stem, label in [('italian_cils_a1', 'A1'), ('italian_cils_a2', 'A2'), ('italian_cils_b1', 'B1'), ('italian_cils_b2', 'B2'), ('italian_core_fluent_forever_625', 'Fluent Forever')]:
                item = data['decks'][stem]
                comparison += f'<tr><th scope="row"><a href="{deck_path(stem)}">{e(label)}</a></th><td>{item["notes"]:,}</td><td>{item["cards"]:,}</td><td>{api._size(item["bytes"])}</td></tr>'
            comparison += '</tbody></table>'
            body = body.replace('<h2>When to add numbers and verbs</h2>', comparison + '<h2>When to add numbers and verbs</h2>')
        write(path, title, description, body)
    not_found = head + '<main id="main" class="guide"><h1>That page is not here.</h1><p>The deck or guide may have moved, or the address may be mistyped.</p><p><a href="/">Browse Italian flashcards</a> or <a href="/italian-anki-decks-for-beginners/">choose a deck for beginners</a>.</p></main>' + tail + '</body></html>'
    not_found = re.sub(r'<title>.*?</title>', '<title>Page not found · Italian Flashcards</title>', not_found)
    not_found = not_found.replace('</head>', '<meta name="robots" content="noindex, follow">\n</head>')
    (site / '404.html').write_text(not_found)
    root = Element('urlset', xmlns='http://www.sitemaps.org/schemas/sitemap/0.9')
    for path in urls:
        SubElement(SubElement(root, 'url'), 'loc').text = ORIGIN + path
    (site / 'sitemap.xml').write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root))
    (site / 'robots.txt').write_text('User-agent: *\nAllow: /\n\nSitemap: ' + ORIGIN + '/sitemap.xml\n')
