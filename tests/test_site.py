"""The public pages Google's app review and YouTube's API audit link to
(docs/, served by GitHub Pages). They are small, but they make promises, so
the promises that code can check are checked here."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PAGES = ("index.html", "privacy.html", "terms.html")


class PublicSite(unittest.TestCase):
    def page(self, name):
        return (DOCS / name).read_text()

    def test_no_email_address_is_published(self):
        # The owner's email stays off a public page; the contact is GitHub.
        for name in PAGES:
            self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", self.page(name)), name)

    def test_youtube_api_policy_links(self):
        # YouTube's API Services policies require a client to point users at
        # the YouTube Terms of Service and the Google Privacy Policy.
        for name in ("privacy.html", "terms.html"):
            html = self.page(name)
            self.assertIn("https://www.youtube.com/t/terms", html, name)
            self.assertIn("https://policies.google.com/privacy", html, name)
        self.assertIn("https://myaccount.google.com/permissions", self.page("privacy.html"),
                      "how to revoke access")

    def test_the_policy_names_the_model_provider_clip_actually_calls(self):
        # postcopy.py sends a clip's transcript to OpenRouter. A privacy
        # policy that forgot that would be the one untrue page here.
        uses = (ROOT / "clipper" / "postcopy.py").read_text()
        self.assertIn("OR_URL", uses)
        self.assertIn("OpenRouter", self.page("privacy.html"))

    def test_the_youtube_badge_is_official_and_clickable(self):
        # The audit asks for YouTube branding on the home page. The branding
        # guidelines allow the "developed with YouTube" logo, unmodified, and
        # require it to be clickable and to link back to YouTube.
        html = self.page("index.html")
        self.assertRegex(html, r'<a class="yt" href="https://www\.youtube\.com"[^>]*>\s*<picture>')
        for f in ("developed-with-youtube-dark.png", "developed-with-youtube-light.png"):
            self.assertIn(f, html)
            self.assertEqual((DOCS / f).read_bytes()[:8], b"\x89PNG\r\n\x1a\n", f)

    def test_every_page_links_the_others_and_is_served_as_written(self):
        for name in PAGES:
            html = self.page(name)
            for other in ("privacy.html", "terms.html"):
                self.assertIn(f'href="{other}"', html, name)
            self.assertIn('name="viewport"', html, "readable on a phone")
        self.assertTrue((DOCS / ".nojekyll").exists())
