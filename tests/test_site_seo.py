"""Public guides retain canonical URLs, crawlable links and reproducible output."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from html.parser import HTMLParser

from tools import build_site


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.canonicals = []
        self.links = []
        self.headings = 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs["href"])
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag == "h1":
            self.headings += 1


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
            self.assertEqual(len(urls), len(data["decks"]) + 4)
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
            self.assertIn("Italian A1 Anki Deck", a1)
            self.assertIn("il caffè", a1)
            self.assertIn("960 study cards", a1)
            self.assertIn(
                "Sitemap: https://italianflashcards.cameronengineer.com/sitemap.xml",
                (root / "robots.txt").read_text(),
            )
            for row in data["decks"]["italian_cils_a1"]["preview"]:
                import html

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


class DeckCoverageTests(unittest.TestCase):
    def test_missing_and_unlisted_decks_are_named(self):
        listed = [stem for _g, _t, _b, items in build_site.GROUPS for stem, _n, _d in items]
        missing, unlisted = build_site.coverage(listed[1:] + ["italian_new_list"])
        self.assertEqual(missing, [listed[0]])
        self.assertEqual(unlisted, ["italian_new_list"])
