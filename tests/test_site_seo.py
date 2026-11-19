"""Public guides retain canonical URLs, crawlable links and reproducible output."""

import html
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from html.parser import HTMLParser

from tools import build_site, site_pages

ORIGIN = "https://italianflashcards.cameronengineer.com"


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.canonicals = []
        self.links = []
        self.headings = 0
        self.icons = []
        self.images = []
        self.meta = {}
        self.jsonld = []
        self.title = ""
        self._in = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs["href"])
        if tag == "link" and attrs.get("rel") == "icon":
            self.icons.append(attrs["href"])
        if tag == "meta":
            self.meta[attrs.get("name") or attrs.get("property")] = attrs.get("content")
        if tag == "img":
            self.images.append(attrs)
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag == "h1":
            self.headings += 1
        if tag == "title" or (tag == "script" and attrs.get("type") == "application/ld+json"):
            self._in = tag

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "script":
            self.jsonld.append(json.loads(data))

    def handle_endtag(self, tag):
        self._in = None

    def graph(self, kind):
        return [node for block in self.jsonld for node in block["@graph"] if node["@type"] == kind]


class SiteSEOTests(unittest.TestCase):
    def test_guides_canonicals_sitemap_and_repeat_build(self):
        data = json.loads((build_site.TOOLS_DIR / "site_data.json").read_text())
        with tempfile.TemporaryDirectory() as folder, patch.object(build_site, "SITE_DIR", Path(folder)):
            root = Path(folder)
            build_site.render_page(data)
            urls = ElementTree.parse(root / "sitemap.xml").findall(
                "{http://www.sitemaps.org/schemas/sitemap/0.9}url/"
                "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
            self.assertEqual(len(urls), len(data["decks"]) + 5)
            self.assertIn(ORIGIN + site_pages.CILS_HUB, [loc.text for loc in urls])
            for loc in urls:
                self.assertTrue(loc.text.startswith("https://italianflashcards.cameronengineer.com/"))
                relative = loc.text.removeprefix("https://italianflashcards.cameronengineer.com")
                target = root / relative.strip("/") / "index.html"
                page = Page(target.read_text())
                self.assertEqual(page.canonicals, [loc.text])
                self.assertEqual(page.headings, 1)
                for link in page.links:
                    if link.startswith("/") and not link.startswith("//"):
                        path = link.split("#")[0]
                        self.assertTrue(
                            (root / path.strip("/") / "index.html").exists()
                            or (build_site.TOOLS_DIR.parent / "site" / path.strip("/")).is_file(),
                            link,
                        )
            a1 = (root / "decks/italian-cils-a1-anki-deck/index.html").read_text()
            self.assertIn("<h1>CILS A1 Italian Vocabulary Anki Deck</h1>", a1)
            self.assertIn("il caffè", a1)
            self.assertIn("960 study cards", a1)
            self.assertIn(
                "Sitemap: https://italianflashcards.cameronengineer.com/sitemap.xml",
                (root / "robots.txt").read_text(),
            )
            for row in data["decks"]["italian_cils_a1"]["preview"]:
                self.assertIn(html.escape(row["italian"]), a1)
                self.assertIn(html.escape(row["english"]), a1)
            guide = (root / "italian-anki-decks-for-beginners/index.html").read_text()
            self.assertIn("A1 vocabulary or Fluent Forever?", guide)
            self.assertIn("/how-to-study-italian-with-anki/", guide)
            self.assertIn('id="compare-levels"', guide)
            error_page = (root / "404.html").read_text()
            self.assertIn('content="noindex, follow"', error_page)
            self.assertNotIn('rel="canonical"', error_page)
            self.assertNotIn("404.html", (root / "sitemap.xml").read_text())
            conditional = (
                root / "decks/italian-verbs-more-tenses-condizionale-passato-anki-deck/index.html"
            ).read_text()
            self.assertIn("Practising the condizionale passato", conditional)
            self.assertIn("treccani.it", conditional)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            build_site.render_page(data)
            self.assertEqual(
                before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            )

    def test_search_snippets_schema_and_hubs(self):
        data = json.loads((build_site.TOOLS_DIR / "site_data.json").read_text())
        with tempfile.TemporaryDirectory() as folder, patch.object(build_site, "SITE_DIR", Path(folder)):
            root = Path(folder)
            build_site.render_page(data)
            locs = [
                loc.text for loc in ElementTree.parse(root / "sitemap.xml").iter(
                    "{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            ]
            pages = {loc: Page((root / loc.removeprefix(ORIGIN).strip("/") / "index.html").read_text())
                     for loc in locs}
            titles, descriptions = [], []
            for loc, page in pages.items():
                title, description = html.unescape(page.title), page.meta["description"]
                titles.append(title)
                descriptions.append(description)
                self.assertLessEqual(len(title), 60, loc)
                self.assertLessEqual(len(description), site_pages.DESCRIPTION_LIMIT, loc)
                self.assertEqual(page.meta["og:url"], loc)
                self.assertTrue(page.meta["og:image"].startswith(ORIGIN + "/samples/"), loc)
                self.assertEqual(page.icons, ["/favicon.ico"], loc)
                self.assertEqual(len(page.jsonld), 1, loc)
                self.assertEqual([n["url"] for n in page.graph("WebPage") + page.graph("CollectionPage")], [loc])
                if loc != ORIGIN + "/":
                    (crumbs,) = page.graph("BreadcrumbList")
                    self.assertEqual(crumbs["itemListElement"][0]["item"], ORIGIN + "/")
                    self.assertEqual(crumbs["itemListElement"][-1]["item"], loc)
            self.assertEqual(len(set(titles)), len(titles))
            self.assertEqual(len(set(descriptions)), len(descriptions))

            # The favicon every page links to is small and committed (the animated PNG is ~1 MB).
            icon = build_site.TOOLS_DIR.parent / "site" / "favicon.ico"
            self.assertEqual(icon.read_bytes()[:4], b"\x00\x00\x01\x00")
            self.assertLess(icon.stat().st_size, 50_000)

            # CILS level pages lead with "CILS" and say what they teach.
            for level in ("a1", "a2", "b1", "b2"):
                page = pages[f"{ORIGIN}/decks/italian-cils-{level}-anki-deck/"]
                self.assertTrue(html.unescape(page.title).startswith(f"CILS {level.upper()} "))
                self.assertIn(f"CILS {level.upper()}", page.meta["description"])
                (deck,) = page.graph("LearningResource")
                self.assertEqual(deck["educationalLevel"], f"CEFR {level.upper()}")
                self.assertTrue(deck["isAccessibleForFree"])
                self.assertEqual(deck["encoding"]["contentUrl"],
                                 build_site.DOWNLOAD_BASE + data["decks"][f"italian_cils_{level}"]["file"])
                crumbs = page.graph("BreadcrumbList")[0]["itemListElement"]
                self.assertEqual(crumbs[1]["item"], ORIGIN + site_pages.CILS_HUB)
            b1 = (root / "decks/italian-cils-b1-anki-deck/index.html").read_text()
            self.assertIn("Words new at B1", b1)

            # The CILS hub is linked from the homepage and links every level.
            hub = pages[ORIGIN + site_pages.CILS_HUB]
            for level in ("a1", "a2", "b1", "b2"):
                self.assertIn(f"/decks/italian-cils-{level}-anki-deck/", hub.links)
            self.assertIn(site_pages.CILS_HUB, pages[ORIGIN + "/"].links)
            self.assertIn(site_pages.CILS_HUB, pages[ORIGIN + "/italian-anki-decks-for-beginners/"].links)
            self.assertEqual(len(hub.graph("ItemList")[0]["itemListElement"]), 4)

            # Tense pages show a full conjugation and the verbs they cover.
            imperfetto = (root / "decks/italian-verbs-learning-imperfetto-anki-deck/index.html").read_text()
            for form in ("vedevo", "vedevi", "vedeva", "vedevamo", "vedevate", "vedevano"):
                self.assertIn(f'<td lang="it">{form}</td>', imperfetto)
            self.assertLess(imperfetto.index(">vedevo<"), imperfetto.index(">vedevano<"))
            self.assertIn('<span lang="it">parlare</span>', imperfetto)
            imperative = (root / "decks/italian-verbs-learning-imperativo-con-tu-anki-deck/index.html").read_text()
            for verb in data["decks"]["italian_verbs_learning_imperativo_con_tu"]["verbs"]:
                self.assertIn(f'<td lang="it">{html.escape(verb["form"])}</td>', imperative)
            verbs_hub = pages[ORIGIN + site_pages.VERB_HUB]
            self.assertEqual(len(verbs_hub.graph("ItemList")[0]["itemListElement"]),
                             sum(stem.startswith("italian_verbs_") for stem in data["decks"]))

            # Film decks are published like the others: their own search text, the film as an
            # entity, and links between the two films and the subtitle-frequency deck.
            films = {"benvenuti-al-sud": "Benvenuti al Sud", "le-otto-montagne": "Le otto montagne"}
            for slug, film in films.items():
                page = pages[f"{ORIGIN}/decks/italian-movies-{slug}-anki-deck/"]
                self.assertTrue(html.unescape(page.title).startswith(f"{film} Anki Deck"))
                self.assertIn(film, page.meta["description"])
                (deck,) = page.graph("LearningResource")
                self.assertIn(film, [a["name"] for a in deck["about"] if a["@type"] == "Movie"])
                other = next(s for s in films if s != slug)
                self.assertIn(f"/decks/italian-movies-{other}-anki-deck/", page.links)
                self.assertIn("/decks/italian-frequency-anki-deck/", page.links)
            frequency = pages[f"{ORIGIN}/decks/italian-frequency-anki-deck/"]
            for slug in films:
                self.assertIn(f"/decks/italian-movies-{slug}-anki-deck/", frequency.links)

            # Only the first homepage sample loads its picture eagerly; the others are hidden.
            loading = [img.get("loading") for img in pages[ORIGIN + "/"].images]
            self.assertEqual(loading[0], None)
            self.assertTrue(loading[1:] and all(x == "lazy" for x in loading[1:]))


def _apkg(path: Path, notes: list[tuple[list[str], str]]) -> None:
    """A minimal package: just the notes table that ``_preview`` reads."""
    with tempfile.TemporaryDirectory() as tmp:
        db = sqlite3.connect(Path(tmp) / "collection.anki2")
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, flds TEXT, tags TEXT, sfld TEXT)")
        for i, (fields, tags) in enumerate(notes, 1):
            db.execute("INSERT INTO notes VALUES (?,?,?,?)", (i, "\x1f".join(fields), f" {tags} ", fields[0]))
        db.commit()
        db.close()
        with zipfile.ZipFile(path, "w") as z:
            z.write(Path(tmp) / "collection.anki2", "collection.anki2")


class PreviewTests(unittest.TestCase):
    def test_cils_preview_keeps_only_words_new_at_the_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "italian_cils_a2.apkg"
            _apkg(path, [(["casa", "house"], "list::cils-a1 list::cils-a2"),
                         (["abbracciare", "to hug"], "list::cils-a2"),
                         (["zaino", "<b>backpack</b>"], "list::cils-a2")])
            self.assertEqual(build_site._preview(path)["preview"],
                             [{"italian": "abbracciare", "english": "to hug"},
                              {"italian": "zaino", "english": "backpack"}])

    def test_tense_deck_lists_verbs_and_orders_persons(self):
        persons = ["they see", "I see", "you all see", "he/she sees", "you see", "we see"]
        forms = ["vedono", "vedo", "vedete", "vede", "vedi", "vediamo"]
        notes = [([f, p, "", "", "vedere — to see"], "verb::vedere") for f, p in zip(forms, persons, strict=True)]
        notes.append((["accade", "it happens", "", "", "accadere — to happen"], "verb::accadere"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "italian_verbs_learning_presente.apkg"
            _apkg(path, notes)
            out = build_site._preview(path)
        self.assertEqual([v["infinitive"] for v in out["verbs"]], ["accadere", "vedere"])
        self.assertEqual(out["verbs"][0], {"infinitive": "accadere", "english": "to happen",
                                           "form": "accade", "prompt": "it happens"})
        self.assertEqual([r["italian"] for r in out["conjugations"]["vedere"]],
                         ["vedo", "vedi", "vede", "vediamo", "vedete", "vedono"])
        self.assertNotIn("parlare", out["conjugations"])

    def test_spread_is_even_and_keeps_both_ends(self):
        self.assertEqual(build_site._spread(list(range(10)), 4), [0, 3, 6, 9])
        self.assertEqual(build_site._spread([1, 2], 5), [1, 2])


class DeckCoverageTests(unittest.TestCase):
    def test_missing_and_unlisted_decks_are_named(self):
        listed = [stem for _g, _t, _b, items in build_site.GROUPS for stem, _n, _d in items]
        missing, unlisted = build_site.coverage(listed[1:] + ["italian_new_list"])
        self.assertEqual(missing, [listed[0]])
        self.assertEqual(unlisted, ["italian_new_list"])
