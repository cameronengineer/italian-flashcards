"""Public deck guides and consistent search metadata, generated without AI calls."""
import html
import json
import re
from xml.etree.ElementTree import Element, SubElement, tostring

ORIGIN = 'https://italianflashcards.cameronengineer.com'
FAQ = [
 ('Are these Italian Anki decks free?', 'Yes. The decks can be downloaded without an account. Pictures and recordings are included in each .apkg file. Keep the decks non-commercial and retain the source credits.'),
 ('Which Italian vocabulary deck should I choose?', 'Choose a CILS level that fits your study, or a topic such as numbers or film vocabulary. The level decks include vocabulary from the levels below them, so downloading all four will repeat material.'),
 ('How do I import a deck into Anki?', 'Download the .apkg file, then open it with Anki. On a computer you can also use File → Import. Review the import summary before confirming, especially if you already use another deck from this collection.'),
 ('Do the cards include Italian pronunciation?', 'The downloadable packages include recordings and pictures. The voices and illustrations are AI-generated. The sample cards on this site let you listen before downloading.'),
 ('Can I practise a particular Italian verb tense?', 'Yes. Verb forms are organised into separate tense decks, including presente, passato prossimo, imperfetto, futuro semplice and both conditional tenses. Imperative decks are separated by person.'),
 ('Are the cards a complete Italian course?', 'No. These are study materials made for my own learning, rather than a complete course or an official CILS resource. AI-generated text can contain mistakes; use a dictionary or grammar reference when something seems unusual.'),
]


def slug(stem):
    return stem.removeprefix('italian_').replace('_', '-')


def deck_path(stem):
    return '/decks/italian-' + slug(stem) + '-anki-deck/'


def title_for(stem, name):
    if stem.startswith('italian_cils_'):
        return f'Italian {stem.rsplit("_", 1)[-1].upper()} Anki Deck'
    return f'Italian {name} Anki Deck'


def faq_html():
    return '<section id="faq"><h2>Questions about the decks</h2>' + ''.join(
        f'<details><summary>{html.escape(q)}</summary><p>{html.escape(a)}</p></details>' for q, a in FAQ
    ) + '</section>'


def metadata(page, title, description, path, image='il-caffe'):
    e = html.escape
    url = ORIGIN + path
    page = re.sub(r'<title>.*?</title>', '<title>' + e(title) + '</title>', page)
    for key, value in [('description', description), ('og:title', title), ('og:description', description)]:
        attr = 'property' if key.startswith('og:') else 'name'
        page = re.sub(rf'<meta {attr}="{key}" content="[^"]*">', f'<meta {attr}="{key}" content="{e(value, quote=True)}">', page)
    page = re.sub(r'<meta property="og:image" content="[^"]*">', f'<meta property="og:image" content="{ORIGIN}/samples/{image}.jpg">', page)
    page = page.replace('href="favicon.png"', 'href="/favicon.png"')
    extra = f'''<link rel="canonical" href="{url}">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:url" content="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Italian Flashcards">
<meta property="og:image:alt" content="An illustration from the Italian flashcard collection">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title, quote=True)}">
<meta name="twitter:description" content="{e(description, quote=True)}">
<meta name="twitter:image" content="{ORIGIN}/samples/{image}.jpg">
<script>
// Render static-site rules cannot match a hostname. Preserve the destination
// path, query and fragment when directing the legacy hostname to our domain.
if (location.hostname === 'italian-flashcards-lyh2.onrender.com') {{
 location.replace('{ORIGIN}' + location.pathname + location.search + location.hash);
}}
</script>
'''
    return page.replace('</head>', extra + '</head>')


def schema(page, title, path, items=None):
    url = ORIGIN + path
    graph = [
        {'@type': 'Person', '@id': ORIGIN + '/#creator', 'name': 'Cameron', 'url': 'https://blog.cameronengineer.com/'},
        {'@type': 'WebSite', '@id': ORIGIN + '/#website', 'url': ORIGIN + '/', 'name': 'Italian Flashcards', 'inLanguage': 'en'},
        {'@type': 'CollectionPage' if items else 'WebPage', '@id': url + '#page', 'url': url, 'name': title,
         'inLanguage': 'en', 'author': {'@id': ORIGIN + '/#creator'}, 'isPartOf': {'@id': ORIGIN + '/#website'}},
    ]
    if path != '/':
        graph.append({'@type': 'BreadcrumbList', 'itemListElement': [
            {'@type': 'ListItem', 'position': 1, 'name': 'Italian Flashcards', 'item': ORIGIN + '/'},
            {'@type': 'ListItem', 'position': 2, 'name': title, 'item': url},
        ]})
    if items:
        graph.append({'@type': 'ItemList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'name': name, 'url': ORIGIN + link}
            for i, (name, link) in enumerate(items, 1)
        ]})
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
    entries = [(stem, name, desc.format(**{f'new_{k}': f'{v:,}' for k, v in data['stats']['new_at'].items()}))
               for _gid, _title, _blurb, rows in api.GROUPS for stem, name, desc in rows]
    # All guide links are ordinary HTML links and remain available without JS.
    for stem, name, _desc in entries:
        homepage = homepage.replace(f'<b>{e(name)}</b>', f'<b><a class="deck-title" href="{deck_path(stem)}">{e(name)}</a></b>')
    homepage = homepage.replace('Italian, a little<br>at a time.', 'Italian flashcards<br>for Anki.')
    homepage = homepage.replace('These are the flashcards I made for studying Italian.', 'Free Italian Anki flashcards for English speakers, made for my own Italian study.')
    homepage = homepage.replace('Shared freely for use in', 'Made by <a href="https://blog.cameronengineer.com/">Cameron</a>. Shared freely for use in')
    homepage = homepage.replace('\n</main>', '<section id="study-guides"><h2>Choosing and using Italian flashcards</h2><p>New to the collection? These guides explain the differences between the decks and how to use them alongside your other Italian study.</p><ul><li><a href="/italian-anki-decks-for-beginners/">Italian Anki decks for beginners: choosing a first deck</a></li><li><a href="/how-to-study-italian-with-anki/">How to study Italian with Anki flashcards</a></li></ul></section>\n</main>')
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
.guide .sample { margin:25px 0; }
.guide .side:first-child .word { padding-top:16px; }
.guide .side audio { max-width:100%; }
.guide ol { margin:20px 0; }
.preview-table { width:100%; border-collapse:collapse; margin:20px 0; font-size:15px; table-layout:fixed; overflow-wrap:anywhere; }
.preview-table th,.preview-table td { text-align:left; border-bottom:1px solid var(--line); padding:12px 10px; vertical-align:top; }
.preview-table th { color:var(--ink); }
.guide .download { display:inline-block; border:1px solid var(--green); padding:10px 18px; border-radius:6px; margin:14px 0; }
</style>''')
    base = homepage
    home_title = 'Free Italian Flashcards for Anki · Audio & Pictures'
    home_desc = 'Free Italian Anki flashcards with audio and pictures. Download decks for A1–B2 vocabulary, numbers and verb conjugation, with real card previews.'
    homepage = schema(metadata(homepage, home_title, home_desc, '/'), home_title, '/', [(name, deck_path(stem)) for stem, name, _ in entries])
    homepage = homepage.replace('<h3>Verbs, one tense at a time</h3>', '<h3><a href="/italian-verb-conjugation-flashcards/">Italian verb conjugation</a></h3>')
    (site / 'index.html').write_text(homepage)
    urls = ['/']
    # Reuse the common shell without homepage-only interactive scripts.
    head = base[:base.index('<main id="main">')]
    head = head.replace('href="#"', 'href="/"').replace('href="#cards"', 'href="/#cards"').replace('href="#decks"', 'href="/#decks"').replace('href="#about"', 'href="/#about"')
    tail = base[base.index('<footer>'):base.index('<script>', base.index('<footer>'))]
    tail = tail.replace('Italian · A personal collection, shared freely.', 'Italian · By <a href="https://blog.cameronengineer.com/">Cameron</a>. A personal collection, shared freely.')
    def write(path, title, desc, content, image='il-caffe'):
        page = head + '<main id="main" class="guide">' + content + '</main>' + tail + '</body></html>'
        page = schema(metadata(page, title, desc, path, image), title, path)
        target = site / path.strip('/') / 'index.html'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page)
        urls.append(path)
    sample_map = {s['deck']: s for s in data['samples']}
    for stem, name, desc in entries:
        info = data['decks'][stem]
        title = title_for(stem, name)
        sample = sample_map.get(stem)
        explanation = 'These cards ask for the Italian verb form from an English prompt. Each download focuses on one tense or imperative person.' if stem.startswith('italian_verbs_') else 'Vocabulary cards practise recognition and recall. Related meanings may share a word, and some vocabulary repeats across the collection.'
        if stem == 'italian_noun_phrases':
            explanation = 'These English → Italian drills practise articles, possessives and prepositions together with nouns, rather than isolated translations.'
        if stem == 'italian_cognates':
            explanation = 'These cards practise connections between Italian and English words. Similar spelling can help recall, but the card’s meaning still matters.'
        if stem.startswith('italian_movies_'):
            explanation = 'These recognition cards ask for the English meaning of Italian vocabulary from the film. Example subtitle lines include a word-for-word translation; they are study notes, not a substitute for watching the film.'
        related = [(other, label) for other, label, _ in entries if other != stem and (other.startswith('italian_cils_') if stem.startswith('italian_cils_') else other.startswith('italian_verbs_') if stem.startswith('italian_verbs_') else other in ('italian_basics_numbers', 'italian_core_fluent_forever_625'))]
        body = f'<p><a href="/">Italian flashcards</a> / {e(name)}</p><h1>{e(title)}</h1><p>{e(desc)}</p><h2>What this deck covers</h2><p>{e(explanation)}</p><p>{info["notes"]:,} notes · {info["cards"]:,} study cards · {api._size(info["bytes"])} · Anki .apkg file</p>'
        body += f'<a class="download" href="{api.DOWNLOAD_BASE}{e(info["file"])}">Download {e(name)} ({api._size(info["bytes"])})</a>'
        if sample:
            body += '<h2>A card from this deck</h2>' + api._sample_html(sample).replace('src="samples/', 'src="/samples/').replace('href="samples/', 'href="/samples/')
        study_note = notes_for(stem)
        if study_note:
            heading, note = study_note
            body += f'<h2>{e(heading)}</h2><p>{e(note)}</p>'
            if 'imperativo' in stem:
                body += '<p>Grammar reference: <a href="https://www.treccani.it/enciclopedia/imperativo_%28Enciclopedia-dell%27Italiano%29/">Treccani on the imperativo</a> (in Italian).</p>'
            elif 'condizionale' in stem:
                body += '<p>Grammar reference: <a href="https://www.treccani.it/enciclopedia/condizionale_%28La-grammatica-italiana%29/">Treccani on the condizionale</a> (in Italian).</p>'
        preview = info.get('preview', [])
        if preview:
            body += '<h2>Examples from the deck</h2><p>A short preview of the downloadable material. These are individual prompts, not a complete vocabulary list or a lesson order.</p><table class="preview-table"><thead><tr><th scope="col">Italian</th><th scope="col">English prompt or meaning</th></tr></thead><tbody>'
            body += ''.join(f'<tr><td lang="it">{e(row["italian"])}</td><td>{e(row["english"])}</td></tr>' for row in preview)
            body += '</tbody></table>'
        body += '<p>For help choosing material, see <a href="/italian-anki-decks-for-beginners/">Italian Anki decks for beginners</a>. For review and import advice, see <a href="/how-to-study-italian-with-anki/">how to study Italian with Anki</a>.</p>'
        body += '<h2>How to use this deck</h2><ol><li>Install <a href="https://apps.ankiweb.net/">Anki</a> for your device.</li><li>Download the .apkg file above. Pictures and recordings are included.</li><li>Open the file with Anki, or choose File → Import on a computer. Check the import summary if you already have related decks.</li><li>Choose this deck in Anki and set a daily new-card limit that suits your study.</li></ol>'
        if stem.startswith('italian_cils_'):
            body += '<h2>Choosing a CILS level</h2><p>The A1, A2, B1 and B2 decks follow CILS vocabulary lists. Higher levels include words from lower levels, so choose one level to avoid repeats. This is an independent study resource, not an official CILS deck or a complete exam course.</p>'
        body += '<h2>Sources and limitations</h2><p>I made these cards for my own Italian study. Dictionary data comes from Wiktionary via Kaikki; the frequency deck uses SUBTLEX-IT. Text, pictures and voices use AI and may contain mistakes. Keep source credits when sharing and use the decks non-commercially. <a href="/#about">Read about the material</a>.</p>'
        if related:
            body += '<h2>Related decks</h2><ul>' + ''.join(f'<li><a href="{deck_path(other)}">{e(label)}</a></li>' for other, label in related) + '</ul>'
        write(deck_path(stem), title, f'{desc} Download {info["cards"]:,} Italian Anki flashcards with pictures and audio. Includes a real example and import instructions.', body, sample['slug'] if sample else 'il-caffe')
    verbs = [(stem,name,desc) for stem,name,desc in entries if stem.startswith('italian_verbs_')]
    body = '<p><a href="/">Italian flashcards</a></p><h1>Italian Verb Conjugation Flashcards</h1><p>Separate Anki decks for Italian verb tenses and imperative persons, with English prompts, Italian answers, pictures and recordings. Choose the tense you are studying rather than importing every form at once.</p><h2>Tenses and moods</h2><ul>'
    body += ''.join(f'<li><a href="{deck_path(stem)}">{e(name)}</a> — {e(desc)}</li>' for stem,name,desc in verbs) + '</ul>'
    body += '<h2>How the practice works</h2><p>A prompt such as “I see” asks you to recall vedo. Reveal the answer, listen to the recording, and grade your recall in Anki. These drills supplement grammar study: they do not explain every use of each tense.</p>'
    body += api._sample_html(sample_map['italian_verbs_learning_presente']).replace('src="samples/', 'src="/samples/').replace('href="samples/', 'href="/samples/')
    body += '<h2>Importing a tense deck</h2><p>Open any tense page above, download its .apkg file and import it into Anki. Media is included. Start with the tense you are learning and add others as your course or reading introduces them.</p><p>The cards are AI-assisted study materials and may contain errors. <a href="/#about">Sources and limitations</a>.</p>'
    write('/italian-verb-conjugation-flashcards/', 'Italian Verb Conjugation Flashcards for Anki', 'Practise Italian verb conjugation with free Anki decks for present, past, future, conditional and imperative forms. See examples and choose a tense.', body, 'vedo')
    try:
        from .study_guides import GUIDES
    except ImportError:
        from study_guides import GUIDES
    for path, title, description, content in GUIDES:
        body = '<p><a href="/">Italian flashcards</a> / Study guide</p><h1>' + e(title) + '</h1><p>By <a href="https://blog.cameronengineer.com/">Cameron</a>, creator of this collection.</p>' + content
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
