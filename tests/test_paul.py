#!/usr/bin/env python3
"""
Paul - every rule that stands between a website agent and a real business.

Paul has not shipped a bug yet; it has not run yet. So these tests name the
failure each one prevents instead of an incident, and each was checked the
repo's way - by putting the failure back and watching the test go red.

Nothing here touches the network. The business's site, Vercel and the
reference sites are all fakes handed in where paul.py takes them.

    python3 -m unittest tests.test_paul -v
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


paul = load("paul", "paul.py")
import remember  # noqa: E402

SENDER = {"area": "Austin, TX", "name": "Sam Owner", "studio": "Sam Studio",
          "email": "sam@studio.test", "phone": "512 000 1111",
          "address": "PO Box 12, Austin, TX 78701", "price": "350"}

# Findable as well as right (2026-10-16): a title naming the business and its
# town, a description a result can show whole, Open Graph, the business as
# schema.org JSON-LD and a visible FAQ marked up as FAQPage (seo_problems).
SEO_HEAD = """<meta name="description" content="Rosa's Tailoring in Austin: hems, suits and wedding dress alterations, sewn by hand on E 7th St.">
<meta property="og:title" content="Rosa's Tailoring"><meta property="og:description" content="Alterations in Austin.">
<meta property="og:image" content="img/hero.jpg">
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "ProfessionalService",
 "name": "Rosa's Tailoring", "telephone": "+1-512-444-0199",
 "address": {"@type": "PostalAddress", "streetAddress": "1801 E 7th St", "addressLocality": "Austin",
             "addressRegion": "TX", "postalCode": "78702"},
 "openingHoursSpecification": [{"@type": "OpeningHoursSpecification",
   "dayOfWeek": ["Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"], "opens": "10:00", "closes": "18:00"}]}</script>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
 {"@type": "Question", "name": "Where is the shop?", "acceptedAnswer": {"@type": "Answer", "text": "1801 E 7th St, Austin."}},
 {"@type": "Question", "name": "When are you open?", "acceptedAnswer": {"@type": "Answer", "text": "Tuesday to Saturday, 10am to 6pm."}},
 {"@type": "Question", "name": "How do I book a fitting?", "acceptedAnswer": {"@type": "Answer", "text": "Call (512) 444-0199."}}]}</script>"""
SEO_FAQ = """<section><h2>Questions</h2><h3>Where is the shop?</h3><p>1801 E 7th St, Austin.</p>
<h3>When are you open?</h3><p>Tuesday to Saturday, 10am to 6pm.</p>
<h3>How do I book a fitting?</h3><p>Call (512) 444-0199.</p></section>"""

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rosa's Tailoring | Alterations in Austin</title><link rel="stylesheet" href="style.css">
""" + SEO_HEAD + """
<link rel="icon" href="img/icon.svg"></head><body>
<header class="hero" style="background-image:url('img/hero.jpg')">
<h1>Rosa's Tailoring</h1><p>Hems, suits and wedding dresses, by hand since 1998.</p>
<a class="cta" href="tel:+15124440199">Call (512) 444-0199</a></header>
<main><p>1801 E 7th St, Austin, TX 78702</p><p>Tue-Sat 10am-6pm</p>
<a href="https://maps.google.com/?q=1801+E+7th+St">Directions</a>""" + SEO_FAQ + """</main>
</body></html>"""

DRAFT = """Channel: email
To: rosa@rosas.test
Subject: A website for Rosa's Tailoring

Hi Rosa,

I'm a designer here in Austin. I couldn't find a website for your shop, so I
built one to show you what it could look like, with your hours and a photo
of the work. It's here: [PREVIEW URL]

If you like it, it's yours for a flat $350, one time: your own photos and
wording, one round of changes, and set up on your own web address, with no
monthly fees. The only cost after that is about $15 a year to renew the name
with the registrar. The photos are placeholders until you send me yours. You can
change the pictures and colours any time, and later add a button to request a quote.
"""


# A Commons photo as `paul.py stock get` records it (Hallmark's library, real).
HERO_PAGE = "https://commons.wikimedia.org/wiki/File:Tailor_at_work.jpg"
HERO_CREDIT = {"img/hero.jpg": {"title": "Tailor at work.jpg", "page": HERO_PAGE, "author": "Some Photographer",
                                "license": "CC BY-SA 4.0",
                                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/"}}


def target(**over):
    t = {"status": "built", "name": "Rosa's Tailoring", "category": "tailor", "kind": "trade",
         "address": "1801 E 7th St, Austin, TX 78702", "phone": "(512) 444-0199",
         "hours": "Tue-Sat 10am-6pm", "current_site": "none",
         "problems": [{"code": "no-site", "evidence": "search and the Maps listing show none"}],
         "channel": "email", "contact": "rosa@rosas.test",
         "sources": {k: "https://maps.google.com/?cid=1"
                     for k in ("name", "address", "phone", "hours", "contact")},
         "photos": [{"file": "img/hero.jpg", "source": HERO_PAGE, "kind": "stock"}],
         "claims": [{"text": "by hand since 1998", "source": "https://maps.google.com/?cid=1"}],
         "identity": [{"detail": "Rosa has sewn by hand since 1998",
                       "source": "https://facebook.com/rosastailoring/about", "on_page": "by hand since 1998"},
                      {"detail": "known for wedding dresses",
                       "source": "https://facebook.com/rosastailoring/photos", "on_page": "wedding dresses"}],
         "layout": "full-bleed photo hero", "notes": "test"}
    t.update(over)
    return t


def offline(url, timeout=20, limit=400_000):
    return {"ok": False, "status": None, "url": url, "headers": {}, "body": "", "error": "offline"}


def page(body, status=200, url="https://x.test/", headers=None):
    return {"ok": True, "status": status, "url": url, "headers": headers or {}, "body": body,
            "error": ""}


class FakeVercel:
    """Answers the five calls paul.py makes, and remembers them."""

    def __init__(self, delete_status=204, end_state="READY"):
        self.calls, self.name = [], None
        self.delete_status, self.end_state = delete_status, end_state

    def __call__(self, method, path, token, body=None, data=None, headers=None, team=None,
                 timeout=60):
        self.calls.append(types.SimpleNamespace(method=method, path=path, body=body, data=data,
                                                headers=headers or {}))
        if path == "/v2/files":
            return 200, {}
        if method == "POST" and path.startswith("/v13/deployments"):
            self.name = body["name"]
            return 200, {"id": "dpl_1", "readyState": "QUEUED"}
        if path.startswith("/v13/deployments/"):
            return 200, {"id": "dpl_1", "readyState": self.end_state, "aliasAssigned": True,
                         "alias": [f"{self.name}-team.vercel.app", f"{self.name}.vercel.app"]}
        if method == "DELETE":
            return self.delete_status, {}
        if path == "/v2/user":
            return 200, {"user": {"username": "sam", "defaultTeamId": "team_1"}}
        if path.startswith("/v2/teams/"):
            return 200, {"billing": {"plan": "hobby"}}
        return 404, {"error": {"message": "unexpected call"}}


class PaulCase(unittest.TestCase):
    """A throwaway ecosystem with Paul registered and the sender set."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = (paul.ROOT, paul.find_browser, paul.budget_left, paul.vercel_token)
        paul.ROOT = self.root
        paul.find_browser = lambda: None
        paul.budget_left = lambda: None
        paul.vercel_token = lambda: "test-token"
        a = paul.A()
        a.mkdir(parents=True)
        (a / "AGENTS.md").write_text("# seeded\n")
        shutil.copyfile(ROOT / "agents" / "paul" / "MEMORY.seed.md", a / "MEMORY.seed.md")
        with open(os.devnull, "w") as dn, _quiet(dn):
            paul.cmd_init(None)
        paul.write_json(paul.A("state", "sender.json"), SENDER)

    def tearDown(self):
        paul.ROOT, paul.find_browser, paul.budget_left, paul.vercel_token = self._saved
        self.tmp.cleanup()

    def work(self, t=None, index=INDEX, draft=DRAFT, last_run=True, cycle_mode="live"):
        w = paul.A("work")
        (w / "site" / "img").mkdir(parents=True, exist_ok=True)
        (w / "site" / "index.html").write_text(index)
        (w / "site" / "style.css").write_text("body{margin:0}")
        (w / "site" / "img" / "hero.jpg").write_bytes(b"\xff\xd8" + b"0" * 5000)
        (w / "site" / paul.CREDITS).write_text(json.dumps(HERO_CREDIT))
        (w / "site" / "img" / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (w / "target.json").write_text(json.dumps(t or target()))
        (w / "draft.md").write_text(draft)
        paul.save_cycle({"started": int(time.time()) - 5, "mode": cycle_mode,
                         "day": paul.today(), "passes": 0})
        if last_run:
            paul.A("state", "last-run.txt").write_text("2026-10-04 | built: Rosa's Tailoring\n")
        return w

    def args(self, **kw):
        return types.SimpleNamespace(**kw)

    def run_quiet(self, fn, *a, **kw):
        import io
        buf = io.StringIO()
        with _quiet(buf):
            rc = fn(*a, **kw)
        return rc, buf.getvalue()


class _quiet:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        self.old = sys.stdout
        sys.stdout = self.stream

    def __exit__(self, *exc):
        sys.stdout = self.old


def live_get(sites_dir, header=True, status=200):
    """Serves whatever was deployed, the way Vercel would - including the
    X-Robots-Tag header from vercel.json, unless told to drop it."""
    def get(url, timeout=20, limit=400_000):
        u = urllib.parse.urlsplit(url)
        if not (u.hostname or "").endswith(".vercel.app"):
            return offline(url)
        site = next(p for p in Path(sites_dir).iterdir() if not p.name.startswith("_"))
        f = site / (urllib.parse.unquote(u.path.lstrip("/")) or "index.html")
        if not f.is_file():
            return page("", 404, url)
        h = {"x-robots-tag": "noindex, nofollow"} if header else {}
        return page(f.read_text(errors="replace"), status, url, h)
    return get


# ---------------------------------------------------------------------------

class RealOrNothing(unittest.TestCase):
    """An invented business, or a real one with an invented phone number, is
    the failure that would end this whole idea on its first send."""

    def test_a_fact_without_a_source_is_refused(self):
        t = target()
        t["sources"].pop("phone")
        self.assertTrue(any("sources.phone" in e for e in paul.validate_target(t)))

    def test_a_reference_sites_555_number_is_refused_as_fiction(self):
        errs = paul.validate_target(target(phone="(512) 555-0188"))
        self.assertTrue(any("fiction" in e for e in errs), errs)

    def test_no_documented_problem_means_no_target(self):
        self.assertTrue(any("problems" in e for e in paul.validate_target(target(problems=[]))))

    def test_a_vibe_is_not_a_problem_code(self):
        errs = paul.validate_target(target(problems=[{"code": "dated", "evidence": "looks old"}]))
        self.assertTrue(any("not one of" in e for e in errs), errs)

    def test_email_channel_needs_an_email(self):
        errs = paul.validate_target(target(contact="https://facebook.com/rosas"))
        self.assertTrue(any("not an email" in e for e in errs), errs)

    def test_no_target_needs_only_a_reason(self):
        self.assertEqual(paul.validate_target({"status": "no-target", "why": "nine checked"}), [])
        self.assertTrue(paul.validate_target({"status": "no-target"}))


class ClaimsAboutTheirSiteAreTested(unittest.TestCase):
    """Paul says what is wrong with a business's current site; code fetches it
    and fails the claims it can disprove. Pitching "your site has no HTTPS"
    to someone whose site has HTTPS is the fastest way to be ignored."""

    def test_no_https_is_contradicted_when_https_loads(self):
        def get(url, **kw):
            return page("<html></html>", 200, url)
        rows, _ = paul.verify_problems(target(current_site="rosas.test",
                                              problems=[{"code": "no-https", "evidence": "x"}]), get)
        self.assertEqual(rows[0][1], "contradicted")

    def test_no_https_is_confirmed_when_only_http_answers(self):
        def get(url, **kw):
            return offline(url) if url.startswith("https") else page("<html></html>", 200, url)
        rows, _ = paul.verify_problems(target(current_site="rosas.test",
                                              problems=[{"code": "no-https", "evidence": "x"}]), get)
        self.assertEqual(rows[0][1], "confirmed")

    def test_dead_domain_is_confirmed_when_nothing_answers(self):
        rows, _ = paul.verify_problems(target(current_site="rosas.test",
                                              problems=[{"code": "dead-domain", "evidence": "x"}]),
                                       offline)
        self.assertEqual(rows[0][1], "confirmed")

    def test_no_site_is_contradicted_by_a_url(self):
        rows, _ = paul.verify_problems(target(current_site="https://rosas.test"), offline)
        self.assertEqual(rows[0][1], "contradicted")

    def test_not_mobile_is_confirmed_without_a_viewport(self):
        def get(url, **kw):
            return page("<html><head></head><body><table></table></body></html>", 200, url)
        rows, _ = paul.verify_problems(target(current_site="https://rosas.test",
                                              problems=[{"code": "not-mobile", "evidence": "x"}]), get)
        self.assertEqual(rows[0][1], "confirmed")

    def test_a_contradiction_fails_the_check(self):
        with tempfile.TemporaryDirectory() as d:
            errs, _n, _i = paul.run_checks(target(current_site="https://rosas.test"), offline,
                                           work=d)
        self.assertTrue(any("problem no-site" in e for e in errs), errs)


class SiteRules(PaulCase):
    """The build rules a reader can check without taste: local images with a
    known source, a working call link, the real address, no demo data."""

    def errors(self, t=None, index=INDEX, old=None):
        self.work(t, index)
        errs, _ = paul.check_site(paul.A("work", "site"), t or target(), old)
        return errs

    def test_the_sample_site_passes(self):
        self.assertEqual(self.errors(), [])

    def test_a_hotlinked_image_is_refused(self):
        errs = self.errors(index=INDEX.replace("img/hero.jpg", "https://scontent.fbcdn.net/x.jpg"))
        self.assertTrue(any("hotlinked" in e for e in errs), errs)

    def test_reference_demo_data_is_refused(self):
        errs = self.errors(index=INDEX.replace("since 1998", "4,200+ jobs done, call (512) 555-0188"))
        self.assertTrue(any("reference site" in e for e in errs), errs)
        self.assertTrue(any("fiction" in e for e in errs), errs)

    def test_meta_copy_is_refused(self):
        errs = self.errors(index=INDEX.replace("by hand since 1998", "a real photo of our shop"))
        self.assertTrue(any("real photo" in e for e in errs), errs)

    def test_a_photo_with_no_source_is_refused(self):
        errs = self.errors(t=target(photos=[]))
        self.assertTrue(any("no entry" in e for e in errs), errs)

    def test_stock_only_from_commons_through_stock_get(self):
        # Owner, 2026-10-26: "Switch to Wikipedia" - Unsplash's and Pexels'
        # terms bar the scripts Paul fetched their photos with.
        for src in ("https://images.unsplash.com/photo-1", "https://images.example/x.jpg"):
            t = target(photos=[{"file": "img/hero.jpg", "source": src, "kind": "stock"}])
            self.assertTrue(any("Wikimedia Commons only" in e for e in self.errors(t=t)), src)
        t = target(photos=[{"file": "img/hero.jpg", "source": "https://commons.wikimedia.org/wiki/File:Other.jpg",
                            "kind": "stock"}])
        self.assertTrue(any("fetch it with paul.py stock get" in e for e in self.errors(t=t)),
                        "a Commons link Paul typed is not one code fetched and credited")

    def test_no_call_link_is_refused(self):
        errs = self.errors(index=INDEX.replace('href="tel:+15124440199"', 'href="#"'))
        self.assertTrue(any("call link" in e for e in errs), errs)

    def test_a_downgrade_from_their_photos_is_refused(self):
        bare = INDEX.replace(" style=\"background-image:url('img/hero.jpg')\"", "")
        old = page('<img src="/shop.jpg"><img src="/team.png">')
        errs = self.errors(t=target(photos=[]), index=bare, old=old)
        self.assertTrue(any("downgrade" in e for e in errs), errs)

    def test_the_street_is_checked_by_its_name_not_its_direction(self):
        # "1801 E 7th St": checking the word after the number checked "e".
        errs = self.errors(index=INDEX.replace("1801 E 7th St", "1801 E Elm St"))
        self.assertTrue(any("street address" in e for e in errs), errs)

    def test_a_missing_local_file_is_refused(self):
        errs = self.errors(index=INDEX.replace("style.css", "styles/main.css"))
        self.assertTrue(any("does not exist" in e for e in errs), errs)


class DraftRules(unittest.TestCase):
    """The pitch must read like a person wrote it, and code must be able to
    put the link in it."""

    def errs(self, text):
        return paul.check_draft(text, target())[0]

    def test_the_sample_draft_passes(self):
        self.assertEqual(self.errs(DRAFT), [])

    def test_no_link_placeholder_is_refused(self):
        self.assertTrue(any("[PREVIEW URL]" in e for e in self.errs(DRAFT.replace("[PREVIEW URL]", "here"))))

    def test_a_list_is_refused(self):
        self.assertTrue(any("no lists" in e for e in
                            self.errs(DRAFT + "\n- fast\n- mobile\n- modern\n")))

    def test_reciting_their_address_is_refused(self):
        self.assertTrue(any("address" in e for e in
                            self.errs(DRAFT.replace("in Austin", "near 1801 E 7th St"))))

    def test_a_leftover_placeholder_is_refused(self):
        self.assertTrue(any("[Your Name]" in e for e in self.errs(DRAFT + "\n[Your Name]\n")))

    def test_email_gets_opt_out_and_mailing_address_phone_gets_neither(self):
        head, body = paul.parse_draft(DRAFT)
        email = paul.compose_draft(head, body, SENDER, dry=False)
        self.assertIn(paul.OPT_OUT, email)
        self.assertIn(SENDER["address"], email)
        head["channel"] = "phone"
        call = paul.compose_draft(head, body, SENDER, dry=False)
        self.assertNotIn(SENDER["address"], call)

    def test_a_dry_draft_says_so_first(self):
        head, body = paul.parse_draft(DRAFT)
        self.assertTrue(paul.compose_draft(head, body, SENDER, dry=True).startswith("DRY RUN"))


class PreviewSafeguards(unittest.TestCase):
    """A preview that a search engine indexes, or a customer mistakes for the
    business's real site, is impersonation whatever was intended."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = Path(self.tmp.name)
        (self.site / "index.html").write_text(INDEX)
        (self.site / "menu.html").write_text("<html><head></head><body>menu</body></html>")
        (self.site / "sitemap.xml").write_text("<urlset/>")

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_page_marked_once_even_when_applied_twice(self):
        for _ in range(2):
            paul.apply_safeguards(self.site, "Rosa's Tailoring", "Sam Studio")
        for name in ("index.html", "menu.html"):
            html = (self.site / name).read_text()
            self.assertEqual(html.count('name="robots"'), 1, name)
            self.assertEqual(html.count(paul.CONCEPT_ID), 1, name)
            self.assertIn("Not the official site of Rosa", html)

    def test_header_robots_and_no_sitemap(self):
        paul.apply_safeguards(self.site, "Rosa's Tailoring", "Sam Studio")
        self.assertIn("Disallow: /", (self.site / "robots.txt").read_text())
        hdr = json.loads((self.site / "vercel.json").read_text())["headers"][0]["headers"][0]
        self.assertEqual(hdr["key"], "X-Robots-Tag")
        self.assertIn("noindex", hdr["value"])
        self.assertFalse((self.site / "sitemap.xml").exists())

    def test_the_slug_is_never_the_bare_business_name(self):
        s = paul.make_slug("Rosa's Tailoring, LLC")
        self.assertRegex(s, r"^rosas-tailoring-concept-[a-z0-9]{6}$")
        self.assertNotEqual(paul.make_slug("Rosa's Tailoring"), s)


class NeverPitchTwice(PaulCase):
    """contacted.csv is the only memory of who was pitched. Trimming it, or a
    second spelling of the name, is how a business gets the same pitch twice."""

    def test_the_same_business_is_found_by_another_spelling_or_its_phone(self):
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"business": "Rosa's Tailoring, LLC", "phone": "512-444-0199",
                         "status": "sent"})
        self.assertIsNotNone(paul.seen_row("rosas tailoring"))
        self.assertIsNotNone(paul.seen_row("Rosa Alterations", "+1 (512) 444 0199"))
        self.assertIsNone(paul.seen_row("Bob's Barbers", "512 999 0000"))

    def test_finish_refuses_a_business_already_pitched(self):
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"business": "Rosa's Tailoring", "phone": "5124440199", "status": "sent"})
        self.work()
        api = FakeVercel()
        rc, _ = self.run_quiet(paul.cmd_finish, self.args(dry=False), get=offline, api=api,
                               sleep=lambda s: None)
        self.assertEqual(rc, 1)
        self.assertEqual(api.calls, [], "it deployed a site for a business already pitched")
        self.assertEqual(len(paul.contacted()), 1)

    def test_marking_and_excluding_never_drop_a_row(self):
        for n in range(3):
            paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                            {"slug": f"s{n}", "business": f"Biz {n}", "status": "drafted"})
        self.run_quiet(paul.cmd_mark, self.args(who="s1", status="sent"))
        self.run_quiet(paul.cmd_exclude, self.args(business="Never Me", phone="5125550000"))
        rows = paul.contacted()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1]["status"], "sent")
        self.assertEqual(rows[3]["status"], "do-not-contact")


class Teardown(PaulCase):
    """Previews come down after 30 days with no reply, at once on a no, and
    stay up for a reply - and the record of them is never deleted."""

    def dep(self, slug, teardown, status="live"):
        paul.append_row(paul.A("state", "deployments.csv"), paul.DEPLOY_FIELDS,
                        {"slug": slug, "preview_url": f"https://{slug}.vercel.app",
                         "business": slug, "deployed": "2026-09-01", "teardown": teardown,
                         "status": status})

    def contact(self, slug, status):
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"slug": slug, "business": slug, "status": status})

    def test_the_policy(self):
        d = {"status": "live", "teardown": "2026-10-01"}
        self.assertIsNotNone(paul.due_reason(d, "sent", "2026-10-02"))
        self.assertIsNotNone(paul.due_reason(d, "drafted", "2026-10-02"))
        self.assertIsNone(paul.due_reason(d, "sent", "2026-10-01"))
        self.assertIsNone(paul.due_reason(d, "replied", "2026-12-01"))
        self.assertIsNone(paul.due_reason(d, "won", "2026-12-01"))
        early = {"status": "live", "teardown": "2026-12-31"}
        self.assertIsNotNone(paul.due_reason(early, "declined", "2026-10-02"))
        self.assertIsNone(paul.due_reason({"status": "removed", "teardown": "2026-01-01"},
                                          "sent", "2026-10-02"))

    def test_due_previews_are_deleted_by_name_and_kept_on_record(self):
        self.dep("old-concept-aaaaaa", "2026-01-01")
        self.dep("new-concept-bbbbbb", "2999-01-01")
        self.contact("old-concept-aaaaaa", "sent")
        self.contact("new-concept-bbbbbb", "sent")
        api = FakeVercel()
        rc, _ = self.run_quiet(paul.teardown, api=api)
        self.assertEqual(rc, 0)
        self.assertEqual([(c.method, c.path) for c in api.calls],
                         [("DELETE", "/v9/projects/old-concept-aaaaaa")])
        rows = paul.deployments()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "removed")
        self.assertEqual(rows[1]["status"], "live")

    def test_marking_declined_takes_it_down_now(self):
        self.dep("rosa-concept-cccccc", "2999-01-01")
        self.contact("rosa-concept-cccccc", "sent")
        api, orig = FakeVercel(), paul.vercel
        paul.vercel = api
        try:
            self.run_quiet(paul.cmd_mark, self.args(who="rosa-concept-cccccc", status="declined"))
        finally:
            paul.vercel = orig
        self.assertEqual([(c.method, c.path) for c in api.calls],
                         [("DELETE", "/v9/projects/rosa-concept-cccccc")])
        self.assertEqual(paul.deployments()[0]["status"], "removed")

    def test_a_failed_delete_stays_live_on_record(self):
        self.dep("old-concept-dddddd", "2026-01-01")
        rc, _ = self.run_quiet(paul.teardown, api=FakeVercel(delete_status=500))
        self.assertEqual(rc, 1)
        self.assertEqual(paul.deployments()[0]["status"], "live")


class HoldWithoutWaking(PaulCase):
    """A woken model that has nothing it may do still costs money to say so.
    Every one of these is decided by code before the wake."""

    def test_a_dry_cycle_needs_only_the_sender_and_registration(self):
        self.assertIsNone(paul.hold_reason(dry=True))

    def test_no_sender_holds(self):
        paul.A("state", "sender.json").unlink()
        self.assertIn("sender", paul.hold_reason(dry=True))

    def approved(self):
        # Since 2026-10-13 a build is of a prospect the owner approved; the
        # live gates below are a build's, not a scouting cycle's.
        paul.save_prospects([dict(id=1, name="Rosa's Tailoring", status="approved",
                                  decided="2026-10-13T12:00:00Z")])

    def test_live_waits_for_go_live(self):
        self.approved()
        self.assertIn("go-live", paul.hold_reason(dry=False))

    def test_scouting_needs_no_go_live(self):
        # Finding prospects publishes nothing and contacts nobody.
        self.assertIsNone(paul.hold_reason(dry=False))

    def test_five_unsent_pitches_hold(self):
        self.approved()
        paul.write_json(paul.A("state", "live.json"), {"since": "2026-10-01"})
        paul.find_browser = lambda: "/usr/bin/true"
        for n in range(paul.MAX_WAITING):
            paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                            {"slug": f"s{n}", "business": f"B{n}", "status": "drafted"})
        self.assertIn("waiting for you", paul.hold_reason(dry=False))

    def test_too_little_budget_holds(self):
        paul.budget_left = lambda: 0.10
        self.assertIn("allowance", paul.hold_reason(dry=True))

    def test_on_the_claude_plan_openrouter_money_does_not_hold_him(self):
        # Owner, 2026-10-15: every agent on the Claude plan. Paul's run then
        # spends nothing on OpenRouter; holding him for its allowance would
        # stop every cycle for money he was not going to spend.
        (self.root / "tasks").mkdir(exist_ok=True)
        (self.root / "tasks" / "engines.json").write_text(
            json.dumps({"agents": {"paul": {"engine": "claude", "model": "sonnet"}}}))
        paul.budget_left = lambda: 0.10
        self.assertIsNone(paul.hold_reason(dry=True))

    def test_a_hold_is_one_memory_line_not_one_a_day(self):
        self.approved()
        for _ in range(3):
            self.run_quiet(paul.cmd_should_run, self.args(dry=False, record=True))
        mem = paul.A("MEMORY.md").read_text()
        self.assertEqual(mem.count("held - "), 1, mem)
        self.assertIn("held by code", paul.A("state", "last-run.txt").read_text())


class MemoryArchive(unittest.TestCase):
    """Trimming MEMORY.md at 2KB keeps every wake cheap; for Paul the trimmed
    lines move to memory-archive.md instead of disappearing."""

    def test_trimmed_lines_land_in_the_archive(self):
        with tempfile.TemporaryDirectory() as d:
            arc = Path(d) / "memory-archive.md"
            for n in range(60):
                remember.remember(d, f"cycle {n:02d} " + "x" * 40, archive=arc)
            kept = (Path(d) / "MEMORY.md").read_text()
            self.assertLessEqual(len(kept.encode()), remember.MAX_BYTES)
            gone = arc.read_text()
            for n in range(60):
                self.assertTrue(f"cycle {n:02d}" in kept or f"cycle {n:02d}" in gone, n)

    def test_without_an_archive_nothing_changes_for_the_others(self):
        with tempfile.TemporaryDirectory() as d:
            for n in range(60):
                _p, trimmed = remember.remember(d, f"cycle {n:02d} " + "x" * 40)
            self.assertGreater(trimmed, 0)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["MEMORY.md"])


class Finish(PaulCase):
    """Code files what Paul left: the dry path touches nothing outside the
    droplet, and the live path ships only a preview it has watched load."""

    def test_dry_never_deploys_and_never_records_a_contact(self):
        self.work(cycle_mode="dry")
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline, api=api)
        self.assertEqual(rc, 0, out)
        self.assertEqual(api.calls, [])
        self.assertEqual(paul.contacted(), [])
        slug = next(p.name for p in paul.A("sites").iterdir())
        self.assertIn("DRY RUN", paul.A("outbox", slug, "draft.md").read_text())
        rep = next(paul.A("reports").glob("*.md")).read_text()
        self.assertIn("NOT TAKEN - no browser", rep, "a screenshot was claimed with no browser")
        self.assertFalse(paul.A("work").exists())

    def test_live_deploys_verifies_and_records(self):
        self.work()
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=False),
                                 get=live_get(paul.A("sites")), api=api, sleep=lambda s: None)
        self.assertEqual(rc, 0, out)
        slug = api.name
        self.assertRegex(slug, r"-concept-[a-z0-9]{6}$")
        uploaded = {c.headers.get("x-vercel-digest") for c in api.calls if c.path == "/v2/files"}
        deploy = next(c for c in api.calls if c.path.startswith("/v13/deployments?"))
        self.assertEqual(deploy.body["target"], "production")
        names = {f["file"] for f in deploy.body["files"]}
        self.assertTrue({"index.html", "robots.txt", "vercel.json", "img/hero.jpg"} <= names, names)
        self.assertEqual(uploaded, {f["sha"] for f in deploy.body["files"]})
        url = f"https://{slug}.vercel.app"
        draft = paul.A("outbox", slug, "draft.md").read_text()
        self.assertIn(url, draft)
        self.assertNotIn("[PREVIEW URL]", draft)
        c, d = paul.contacted()[0], paul.deployments()[0]
        self.assertEqual((c["status"], c["preview_url"]), ("drafted", url))
        self.assertEqual((d["status"], d["teardown"]), ("live", paul.day_plus(paul.today(), 30)))
        self.assertIn(url, paul.A("MEMORY.md").read_text())

    def test_a_preview_without_the_noindex_header_is_taken_down(self):
        self.work()
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=False),
                                 get=live_get(paul.A("sites"), header=False), api=api,
                                 sleep=lambda s: None)
        self.assertEqual(rc, 1)
        self.assertIn(("DELETE", f"/v9/projects/{api.name}"),
                      [(c.method, c.path) for c in api.calls])
        self.assertEqual(paul.contacted(), [], "a pitch was recorded for a broken preview")
        self.assertTrue(paul.A("outbox", "_failed", api.name, "draft.md").is_file())
        self.assertEqual(paul.deployments()[0]["status"], "failed")

    def test_a_preview_behind_a_login_says_so(self):
        self.work()
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=False),
                                 get=live_get(paul.A("sites"), status=401), api=FakeVercel(),
                                 sleep=lambda s: None)
        self.assertEqual(rc, 1)
        self.assertIn("401", out)

    def test_an_interrupted_cycle_keeps_its_work_for_the_next(self):
        self.work(last_run=False)
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=False), get=offline,
                                 api=FakeVercel())
        self.assertEqual(rc, 1)
        self.assertTrue(paul.A("work", "target.json").is_file())
        rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=False), get=offline)
        self.assertIn("unfinished build for \"Rosa's Tailoring\"", facts)

    def test_built_with_no_target_json_is_a_failure_not_a_quiet_day(self):
        self.work()
        paul.A("work", "target.json").unlink()
        rc, _ = self.run_quiet(paul.cmd_finish, self.args(dry=False), get=offline,
                               api=FakeVercel())
        self.assertEqual(rc, 1)
        self.assertIn("wrote no valid work/target.json", paul.A("MEMORY.md").read_text())
        self.assertTrue(paul.A("work", "site", "index.html").is_file(), "the work was thrown away")

    def test_no_target_is_a_pass(self):
        paul.save_cycle({"started": int(time.time()) - 5, "mode": "live", "passes": 0})
        paul.A("work").mkdir(parents=True, exist_ok=True)
        paul.A("work", "target.json").write_text(json.dumps({"status": "no-target",
                                                             "why": "nine checked"}))
        paul.A("state", "last-run.txt").write_text("no target - nine checked\n")
        rc, _ = self.run_quiet(paul.cmd_finish, self.args(dry=False), get=offline,
                               api=FakeVercel())
        self.assertEqual(rc, 0)
        self.assertIn("no target - nine checked", paul.A("MEMORY.md").read_text())


class Check(PaulCase):
    """Paul's own sign-off. Two failed passes and it is told to stop: a model
    that keeps polishing is a model that keeps billing."""

    def test_two_failed_passes_then_stop(self):
        self.work(t=target(photos=[]))
        _rc, first = self.run_quiet(paul.cmd_check, self.args(), get=offline)
        _rc, second = self.run_quiet(paul.cmd_check, self.args(), get=offline)
        self.assertIn("pass 1 of 2", first)
        self.assertIn("Out of passes", second)
        self.assertEqual(paul.load_cycle()["passes"], 2)

    def test_a_target_that_does_not_parse_is_called_broken_not_missing(self):
        self.work()
        paul.A("work", "target.json").write_text('{"status": "built", "name": ')
        rc, out = self.run_quiet(paul.cmd_check, self.args(), get=offline)
        self.assertEqual(rc, 1)
        self.assertIn("BROKEN", out)

    def test_a_line_from_just_before_the_cycle_is_not_this_cycles(self):
        # Found by running paul-cycle.sh end to end: a dry run started less
        # than a second after a hold, and check signed off on the hold's line
        # because the start time had been cut to whole seconds.
        self.work(last_run=True)
        base = int(time.time()) - 10
        os.utime(paul.A("state", "last-run.txt"), (base + 0.2, base + 0.2))
        cyc = paul.load_cycle()
        cyc["started"] = base + 0.7
        paul.save_cycle(cyc)
        _rc, out = self.run_quiet(paul.cmd_check, self.args(), get=offline)
        self.assertIn("MEMORY: MISSING", out)

    def test_a_passing_check_with_no_last_run_line_is_not_finished(self):
        self.work(last_run=False)
        rc, out = self.run_quiet(paul.cmd_check, self.args(), get=offline)
        self.assertEqual(rc, 1)
        self.assertIn("MEMORY: MISSING", out)
        self.assertEqual(paul.load_cycle()["passes"], 0, "a missing line used a build pass")


class Brief(PaulCase):
    def test_down_references_are_not_handed_to_paul(self):
        def get(url, **kw):
            return page("<html></html>", 200, url) if "rourke" in url else offline(url)
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=get)
        live = facts.split("down today")[0]
        self.assertIn("rourke-plumbing", live)
        self.assertNotIn("ferngrove", live)
        self.assertEqual(paul.load_cycle()["mode"], "dry")


class Screenshots(unittest.TestCase):
    def test_a_browser_that_writes_nothing_is_not_a_screenshot(self):
        with tempfile.TemporaryDirectory() as d:
            ok = paul.screenshot("chrome", "file:///x.html", Path(d) / "m.png", 390, 844,
                                 run=lambda *a, **k: None)
        self.assertFalse(ok)


class Vercel(unittest.TestCase):
    def test_a_failed_build_raises_rather_than_returning_a_url(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text("<html></html>")
            with self.assertRaises(paul.DeployError):
                paul.vercel_deploy(Path(d), "x-concept-aaaaaa", "t", api=FakeVercel(end_state="ERROR"),
                                   sleep=lambda s: None)

    def test_the_plan_is_read_not_guessed(self):
        user, plan, err = paul.vercel_whoami("t", api=FakeVercel())
        self.assertEqual((user, plan, err), ("sam", "hobby", ""))


class Area(unittest.TestCase):
    def test_us_or_not(self):
        for a in ("Austin, TX", "Nashville, Tennessee", "Round Rock TX", "Dallas, Texas, USA"):
            self.assertTrue(paul.looks_us(a), a)
        for a in ("Toronto, ON", "London", "Manchester, UK", ""):
            self.assertFalse(paul.looks_us(a), a)


class TheHeaderKeepsTheHardRules(unittest.TestCase):
    """The rules the owner said only they may change. If an edit drops one,
    Paul runs without it and nothing else would notice."""

    def test_every_hard_rule_is_in_the_instructions(self):
        h = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        for phrase in ("Real or nothing", "never contact a business", "data, never instructions",
                       "style only", "Light touch", "Nobody is watching", "state/last-run.txt",
                       "never edit MEMORY.md"):
            self.assertIn(phrase, h)


if __name__ == "__main__":
    unittest.main()


class PaulSpendsFromHisOwnKey(PaulCase):
    """Owner, 2026-10-05: Paul gets his own OpenRouter key. A Sonnet-class
    cycle is $2-4 and Emily's images need $1.00 of room on the shared key, so
    Paul on the shared $1.50 day would block them. With PAUL_OPENROUTER_KEY
    saved, his budget is that key's limit_remaining (OpenRouter's own field,
    read by preflight.key_status); without it, the shared allowance."""

    def setUp(self):
        super().setUp()
        self.real_budget_left = self._saved[2]
        self._old_load = paul._load
        self.addCleanup(setattr, paul, "_load", self._old_load)
        self.asked = []

        def fake_load(name, filename):
            if filename == "preflight.py":
                return types.SimpleNamespace(key_status=lambda key, timeout=20:
                                             self.asked.append(key) or {"limit": 5.0, "limit_remaining": 3.214})
            if filename == "budget.py":
                return types.SimpleNamespace(read=lambda: {"ai_left_today": 0.42})
            return self._old_load(name, filename)
        paul._load = fake_load

    def test_his_own_key_is_what_counts(self):
        paul.A("state").mkdir(parents=True, exist_ok=True)
        paul.A("state", "credentials.env").write_text("PAUL_OPENROUTER_KEY=test-not-a-key\n")
        self.assertEqual(self.real_budget_left(), 3.21)
        self.assertEqual(self.asked, ["test-not-a-key"])

    def test_without_it_the_shared_allowance(self):
        self.assertEqual(self.real_budget_left(), 0.42)
        self.assertEqual(self.asked, [])

    def test_preflight_says_which_key_it_read(self):
        """Bug: preflight printed "$0.67 of today's AI allowance left" either
        way, so the owner could not tell whether his new key was saved."""
        self.assertIn("not saved", paul.budget_source())
        paul.A("state").mkdir(parents=True, exist_ok=True)
        paul.A("state", "credentials.env").write_text("PAUL_OPENROUTER_KEY=test-not-a-key\n")
        self.assertEqual(paul.budget_source(), "Paul's own key")


class PreflightNamesTheSearchProvider(PaulCase):
    """Bug, owner's droplet 2026-10-05: once Brave was configured, preflight
    asked openclaw for tools.web.search - now an object - and printed its
    first line, "{". It must ask for the provider, a single value."""

    def test_the_line_names_a_provider(self):
        asked = []
        old = paul._openclaw_get
        self.addCleanup(setattr, paul, "_openclaw_get", old)
        paul._openclaw_get = lambda key: asked.append(key) or (
            "brave" if key.endswith(".provider") else "{")
        paul.vercel_token = lambda: None
        _, out = self.run_quiet(paul.cmd_preflight, None)
        line = next(l for l in out.splitlines() if "web search" in l)
        self.assertIn("brave", line)
        self.assertNotIn("tools.web.search", asked)

    def test_on_the_plan_it_says_so_and_the_budget_is_not_his(self):
        # Owner's droplet, 2026-10-15: on the Claude plan, preflight still
        # said "a cycle needs $0.50 to start" and asked for a web search
        # provider - both about an engine Paul no longer wakes on.
        old = (paul._openclaw_get, paul.on_the_plan, paul.budget_left)
        self.addCleanup(lambda: (setattr(paul, "_openclaw_get", old[0]), setattr(paul, "on_the_plan", old[1]),
                                 setattr(paul, "budget_left", old[2])))
        paul._openclaw_get = lambda key: "brave"
        paul.vercel_token = lambda: None
        paul.budget_left = lambda: 2.0
        for plan in (True, False):
            paul.on_the_plan = lambda plan=plan: plan
            _, out = self.run_quiet(paul.cmd_preflight, None)
            budget = next(l for l in out.splitlines() if "budget" in l and "$" in l)
            self.assertEqual("Claude plan" in out, plan)
            self.assertEqual("not used while he is on the plan" in budget, plan, budget)
            self.assertEqual("needs $" in budget, not plan, budget)


FIX = ROOT / "tests" / "fixtures"


def doh(name):
    """get() that answers the MX lookup with a real captured dns.google reply."""
    body = (FIX / name).read_text()
    return lambda url, timeout=20, limit=400_000: page(body, url=url)


class NotVibecodedNotSued(SiteRules):
    """Owner, 2026-10-05: two checklists (Yates) - what makes a site read as
    AI-made, and what gets a small business sued. Milkbox, the first dry
    cycle, had em dashes and stock phrasing; these are the parts code can
    see, so Paul cannot just say he followed them."""

    def has(self, errs, word):
        self.assertTrue(any(word in e for e in errs), f"no error mentioning {word!r}: {errs}")

    def test_em_dashes_in_the_page(self):
        self.has(self.errors(index=INDEX.replace("by hand since", "by hand — since")),
                 "em dash")

    def test_emoji_icons(self):
        self.has(self.errors(index=INDEX.replace("<h1>", "<h1>✂️ ")), "emoji")

    def test_stock_ai_phrasing(self):
        self.has(self.errors(index=INDEX.replace("Hems, suits", "Nestled in East Austin. Hems, suits")),
                 "stock AI phrasing")

    def test_a_photo_without_alt_text(self):
        self.has(self.errors(index=INDEX.replace("</main>", '<img src="img/hero.jpg"></main>')),
                 "alt text")

    def test_alt_text_satisfies_it(self):
        errs = self.errors(index=INDEX.replace(
            "</main>", '<img src="img/hero.jpg" alt="Rosa pinning a hem"></main>'))
        self.assertFalse([e for e in errs if "alt text" in e], errs)

    def test_no_favicon(self):
        self.has(self.errors(index=INDEX.replace('<link rel="icon" href="img/icon.svg">', "")),
                 "favicon")

    def test_embeds_scripts_and_forms(self):
        errs = self.errors(index=INDEX.replace("</main>", (
            '<iframe src="https://www.google.com/maps/embed?pb=1"></iframe>'
            '<script src="https://www.googletagmanager.com/gtag/js"></script>'
            '<form><input name="email"></form></main>')))
        self.has(errs, "embedded")
        self.has(errs, "outside script")
        self.has(errs, "a form")

    def test_google_fonts_are_allowed(self):
        errs = self.errors(index=INDEX.replace(
            '<link rel="icon"', '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
                                '<link rel="icon"'))
        self.assertEqual(errs, [])

    def test_other_outside_stylesheets_are_refused(self):
        self.has(self.errors(index=INDEX.replace(
            '<link rel="icon"', '<link rel="stylesheet" href="https://cdn.tailwindcss.com/x.css">'
                                '<link rel="icon"')), "outside stylesheet")

    def test_a_custom_cursor(self):
        self.has(self.errors(index=INDEX.replace("</head>", "<style>body{cursor:none}</style></head>")),
                 "cursor")

    def test_the_clean_site_still_passes(self):
        self.assertEqual(self.errors(), [])


class ThePitchIsClean(PaulCase):
    """Milkbox's pitch (2026-10-05) used em dashes and signed itself "Best,
    Kollin" above the signature code adds - the name appeared twice."""

    def errs(self, draft):
        return paul.check_draft(draft, target())[0]

    def test_em_dash(self):
        self.assertTrue(any("em dash" in e for e in
                            self.errs(DRAFT.replace("I'm a designer", "I'm a designer —"))))

    def test_signing_off(self):
        for tail in ("\nBest,\nSam\n", "\nThanks!\n", "\nSam Owner\n"):
            self.assertTrue(any("signs itself off" in e for e in self.errs(DRAFT + tail)), tail)

    def test_the_sample_pitch_passes(self):
        self.assertEqual(self.errs(DRAFT), [])


class AnEmailThatCanArrive(PaulCase):
    """Milkbox (2026-10-05): the pitch went to andrea@ a domain that no longer
    exists, found on an AI travel site. Real dns.google replies, captured
    the same day: no such domain, a null MX, and gmail's."""

    def test_no_such_domain(self):
        self.assertEqual(paul.mail_verdict("a@gone.test", doh("dns-google-mx-nxdomain.json"))[0], "none")

    def test_a_null_mx_takes_no_mail(self):
        self.assertEqual(paul.mail_verdict("a@x.test", doh("dns-google-mx-null.json"))[0], "none")

    def test_a_real_mail_server(self):
        self.assertEqual(paul.mail_verdict("a@x.test", doh("dns-google-mx-gmail.json"))[0], "yes")

    def test_no_answer_is_not_proof(self):
        self.assertEqual(paul.mail_verdict("a@x.test", offline)[0], "unknown")

    def test_check_refuses_a_bouncing_pitch(self):
        self.work()
        errs, _n, _i = paul.run_checks(target(), doh("dns-google-mx-nxdomain.json"))
        self.assertTrue(any("would bounce" in e for e in errs), errs)


def _a_browser():
    for b in [os.environ.get("PAUL_BROWSER", "")] + [shutil.which(n) or "" for n in paul.BROWSERS] \
            + ["/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]:
        if b and os.access(b, os.X_OK):
            return b
    return None


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
class TextAVisitorCanRead(unittest.TestCase):
    def test_text_below_the_first_screen_is_measured_on_a_smooth_scrolling_page(self):
        # The template has html{scroll-behavior:smooth}; until 2026-10-21 the
        # probe looked before the scroll arrived and skipped this text unsaid.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text(
                '<!doctype html><html><head><style>html{scroll-behavior:smooth}body{margin:0;background:#fff}'
                '</style></head><body><p>Top</p><div style="height:3000px"></div>'
                '<p style="color:#ddd">Hard to read below the fold</p><div style="height:3000px"></div></body></html>')
            got = paul.contrast_problems(d, _a_browser())
        self.assertEqual([(t, need) for _p, t, _r, need in got], [("Hard to read below the fold", 4.5)])

    """Milkbox's "Call us" (2026-10-05): brown text on a brown button,
    inherited from a{color}, in both screenshots - Paul read them and missed
    it. Measured in a real browser. White text on a photo must NOT be
    flagged: it cannot be judged from colours, and a false alarm costs Paul
    one of his two passes."""

    PAGE = """<!doctype html><html><head><style>
    a{color:#6b3a1f} .btn{background:#6b3a1f;padding:10px} .btn.ok{color:#fff}
    .hero{position:relative;height:200px} .hero img{position:absolute;inset:0;width:100%;height:100%}
    .hero h2{position:relative;color:#fff} .pale{color:#d8d0c8} .gap{height:1500px}
    </style></head><body><a class="btn" href="tel:1">Call us</a> <a class="btn ok" href="#m">Menu</a>
    <div class="hero"><img src="x.jpg" alt="bread"><h2>Over a photo</h2></div><div class="gap"></div>
    <p class="pale">Pale text below the fold</p><p>Plain text</p></body></html>"""

    def test_unreadable_text_is_found_and_photos_are_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text(self.PAGE)
            bad = paul.contrast_problems(d, _a_browser())
            self.assertEqual(sorted(p for p in os.listdir(d)), ["index.html"], "the probe copy was left behind")
        self.assertIsNotNone(bad, "the browser gave no answer")
        texts = [t for _p, t, _r, _n in bad]
        self.assertIn("Call us", texts)
        self.assertIn("Pale text below the fold", texts)
        self.assertNotIn("Menu", texts)
        self.assertNotIn("Over a photo", texts)
        self.assertNotIn("Plain text", texts)


class TheOfferIsTheOwners(PaulCase):
    """Owner, 2026-10-06: $350 one time, stated in the first message with
    what it covers. Milkbox's pitch, written before there was an offer, gave
    the site away ("yours to keep, no strings attached")."""

    def errs(self, draft):
        return paul.check_draft(draft, target())[0]

    def has(self, draft, word):
        errs = self.errs(draft)
        self.assertTrue(any(word in e for e in errs), f"{word!r} not in {errs}")

    def test_no_price(self):
        self.has(DRAFT.replace("$350", "a flat fee"), "must state the price")

    def test_a_different_price(self):
        self.has(DRAFT.replace("$350", "$300"), "must state the price")
        self.has(DRAFT.replace("one time:", "one time (normally $500):"), "the only price")

    def test_giving_it_away(self):
        self.has(DRAFT + "\nOr keep it, no strings attached.\n", "offers it free")

    def test_what_it_covers(self):
        self.has(DRAFT.replace("with no\nmonthly fees", "simple"), "monthly")
        self.has(DRAFT.replace("set up on your own web address", "set up"), "web address")

    def test_the_price_is_set_with_a_command(self):
        rc, _ = self.run_quiet(paul.cmd_sender, types.SimpleNamespace(
            **{k: None for k in paul.SENDER_FIELDS} | {"price": "$ 450"}))
        self.assertEqual(rc, 0)
        self.assertEqual(paul.load_sender()[0]["price"], "450")
        rc, _ = self.run_quiet(paul.cmd_sender, types.SimpleNamespace(
            **{k: None for k in paul.SENDER_FIELDS} | {"price": "a lot"}))
        self.assertEqual(rc, 2)
        self.assertEqual(paul.load_sender()[0]["price"], "450")

    def test_no_price_set_means_no_dry_cycle(self):
        s = dict(SENDER)
        del s["price"]
        paul.write_json(paul.A("state", "sender.json"), s)
        self.assertIn("price", paul.hold_reason(True) or "")


class CheckDoesNotRemeasureAnUnchangedSite(unittest.TestCase):
    """Droplet, 2026-10-06: with the contrast probe, check took 12.3 s on
    one CPU - long enough for openclaw to background it - and a Gemini
    cycle spent 70 tool calls running and polling commands. Chrome is not
    launched again for a site that has not changed since it was measured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = Path(self.tmp.name) / "site"
        self.site.mkdir()
        (self.site / "index.html").write_text("<html><body><p>hi</p></body></html>")
        self.memo = Path(self.tmp.name) / "shots" / ".measured.json"
        self.launches = 0

    def tearDown(self):
        self.tmp.cleanup()

    def fake_chrome(self, cmd, **kw):
        self.launches += 1
        return types.SimpleNamespace(stdout='<pre id="paul-contrast">{"checked":1,"fails":[]}</pre>')

    def test_measured_once_until_the_site_changes(self):
        for _ in range(3):
            self.assertEqual(paul.measured_contrast(self.site, "chrome", self.memo, run=self.fake_chrome), [])
        self.assertEqual(self.launches, 1)
        time.sleep(0.01)
        (self.site / "index.html").write_text("<html><body><p>changed</p></body></html>")
        paul.measured_contrast(self.site, "chrome", self.memo, run=self.fake_chrome)
        self.assertEqual(self.launches, 2)

    def test_no_answer_is_asked_again(self):
        silent = lambda cmd, **kw: (setattr(self, "launches", self.launches + 1)
                                    or types.SimpleNamespace(stdout=""))
        self.assertIsNone(paul.measured_contrast(self.site, "chrome", self.memo, run=silent))
        self.assertIsNone(paul.measured_contrast(self.site, "chrome", self.memo, run=silent))
        self.assertEqual(self.launches, 2)

    def test_screenshots_are_not_retaken(self):
        calls = []
        old = paul.shoot
        self.addCleanup(setattr, paul, "shoot", old)

        def fake_shoot(url, folder, browser=None):
            calls.append(url)
            Path(folder).mkdir(parents=True, exist_ok=True)
            out = []
            for n, _w, _h in paul.SHOTS:
                (Path(folder) / f"{n}.png").write_bytes(b"png" * 500)
                out.append((n, Path(folder) / f"{n}.png"))
            return out
        paul.shoot = fake_shoot
        shots = Path(self.tmp.name) / "shots"
        paul.fresh_shots(self.site / "index.html", shots)
        paul.fresh_shots(self.site / "index.html", shots)
        self.assertEqual(len(calls), 1)
        time.sleep(0.01)
        (self.site / "index.html").write_text("<html><body>new</body></html>")
        paul.fresh_shots(self.site / "index.html", shots)
        self.assertEqual(len(calls), 2)


class StockPhotosAreSaidToBePlaceholders(PaulCase):
    """Owner, 2026-10-06: College Hill Barbers and Milkbox were all-stock -
    their own photos were behind Facebook's login. A concept may use stock,
    but the pitch must say the photos are placeholders."""

    def errs(self, draft, t):
        return paul.check_draft(draft, t)[0]

    def test_all_stock_without_saying_so(self):
        draft = DRAFT.replace(" The photos are placeholders until you send me yours.", "")
        self.assertTrue(any("placeholder" in e for e in self.errs(draft, target())))

    def test_saying_so_passes(self):
        self.assertEqual(self.errs(DRAFT, target()), [])

    def test_their_own_photo_needs_no_line(self):
        draft = DRAFT.replace(" The photos are placeholders until you send me yours.", "")
        t = target(photos=[{"file": "img/hero.jpg", "source": "https://facebook.com/x/photo",
                            "kind": "business"}])
        self.assertFalse([e for e in self.errs(draft, t) if "placeholder" in e])



class EveryClaimHasASource(SiteRules):
    """College Hill Barbers (2026-10-06) said "Certified Stylists ... stay
    trained in the latest techniques" - in no source Paul read. Claim words
    on the page need an entry in target.json "claims" with the URL."""

    def has(self, errs, word):
        self.assertTrue(any(word in e for e in errs), f"{word!r} not in {errs}")

    def test_an_unsourced_claim(self):
        self.has(self.errors(index=INDEX.replace("<main>", "<main><p>Certified tailors.</p>")),
                 '"Certified" is a claim')

    def test_a_sourced_claim_passes(self):
        t = target(claims=target()["claims"] + [{"text": "certified tailors",
                                                 "source": "https://rosas.test/about"}])
        self.assertEqual(self.errors(t=t, index=INDEX.replace("<main>", "<main><p>Certified tailors.</p>")), [])

    def test_a_claim_needs_its_url(self):
        t = target(claims=[{"text": "by hand since 1998", "source": "their sign"}])
        self.has(self.errors(t=t), "needs the URL")

    def test_a_founding_year(self):
        self.has(self.errors(t=target(claims=[], identity=[])), '"since 1998" is a claim')

    def test_ordinary_copy_is_not_a_claim(self):
        self.assertEqual(self.errors(index=INDEX.replace(
            "<main>", "<main><p>Walk-ins welcome. Bring the dress, we will pin it.</p>")), [])


TEMPLATE = ROOT / "agents" / "paul" / "templates" / "local"


def fill_template(site, t):
    """Every {{SLOT}} in a copy of the site template, filled the way Paul is
    told to: real facts in, images and links pointing at files that exist."""
    import re
    shutil.copytree(TEMPLATE, site, dirs_exist_ok=True)
    street, rest = t["address"].split(", ", 1)
    values = {"NAME": t["name"], "PHONE": t["phone"], "PHONE_TEL": "+1" + paul.norm_phone(t["phone"]),
              "ADDRESS_LINE1": street, "ADDRESS_LINE2": rest, "TOWN": "Austin, Texas",
              "DESCRIPTION": "Hollow Oak Coffee in Austin: coffee and pastries on E 7th St, open six days a week."}
    def one(m):
        k = m.group(1)
        if k in values:
            return values[k]
        if k.endswith("_IMG"):
            return "icon.svg"
        if k.endswith("_URL"):
            return "https://maps.google.com/?q=1801+E+7th+St"
        return "$4.00" if k.endswith("PRICE") else "Coffee and pastries"
    page = site / "index.html"
    page.write_text(re.sub(r"\{\{([A-Z0-9_]+)\}\}", one, page.read_text()))


class TheTemplateIsTheBarNotTheMold(unittest.TestCase):
    """templates/local shows Paul what good looks like. The owner, 2026-10-08:
    "I don't want to use the same premade template style website for every
    site ... unique to that business using things from their facebook or their
    history." So the example must itself meet every rule Paul is held to -
    Paul studies it - and a site built on it is refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / "site"
        self.t = target(name="Hollow Oak Coffee", category="cafe", kind="food", photos=[], claims=[],
                        identity=[])

    def test_the_example_meets_every_rule_but_being_itself(self):
        fill_template(self.site, self.t)
        errs, info = paul.check_site(self.site, self.t)
        self.assertEqual([e for e in errs if "identity" not in e and "templates/local" not in e], [])
        self.assertEqual(info["pages"], 1)

    def test_a_site_built_on_it_is_refused(self):
        fill_template(self.site, self.t)
        errs, _ = paul.check_site(self.site, self.t)
        self.assertTrue(any("this site is built on templates/local" in e for e in errs), errs)

    def test_a_slot_left_unfilled_is_refused_by_name(self):
        fill_template(self.site, self.t)
        page = self.site / "index.html"
        page.write_text(page.read_text().replace('alt="Coffee and pastries"', 'alt="{{HERO_ALT}}"', 1))
        errs, _ = paul.check_site(self.site, self.t)
        self.assertTrue(any("template slots left unfilled: {{HERO_ALT}}" in e for e in errs), errs)

    def test_comments_never_reach_a_visitor(self):
        fill_template(self.site, self.t)
        self.assertIn("target.json", (self.site / "index.html").read_text(), "the template does carry them")
        paul.apply_safeguards(self.site, self.t["name"], "Sam Studio")
        html = (self.site / "index.html").read_text()
        self.assertNotIn("<!--", html)
        self.assertNotIn("target.json", html)
        self.assertIn('class="intro"', html, "only the comments went")

    def test_the_header_calls_it_the_bar_and_asks_for_their_own_details(self):
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertNotIn("cp -r templates/local", header, "no longer copied")
        self.assertIn("The quality bar: `templates/local/`", header)
        self.assertIn('"identity"', header)
        self.assertIn("Facebook", header)
        self.assertTrue((TEMPLATE / "img" / "icon.svg").is_file())

    def test_the_intro_never_plays_for_people_who_ask_for_less_motion(self):
        css = (TEMPLATE / "style.css").read_text()
        reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1].split("}", 1)[0]
        self.assertIn(".intro { display: none;", reduced)
        self.assertIn(".seen .intro { display: none; }", css, "once per visit")


class WhatIsOnlyTrueOfThem(SiteRules):
    """A site is theirs when it shows things only true of them - from their
    Facebook, their history - each sourced, each visibly on the page."""

    def identity_errs(self, rows):
        return [e for e in self.errors(target(identity=rows)) if "identity" in e or "only true" in e]

    def test_the_sample_site_with_two_sourced_details_passes(self):
        self.assertEqual(self.identity_errs(target()["identity"]), [])

    def test_fewer_than_two_is_refused_and_says_below_bar(self):
        errs = self.identity_errs(target()["identity"][:1])
        self.assertTrue(any("1 detail(s) only true of this business" in e and "below-bar" in e for e in errs), errs)

    def test_a_detail_with_no_source_is_refused(self):
        rows = [dict(target()["identity"][0], source=""), target()["identity"][1]]
        self.assertTrue(any("needs the URL it came from" in e for e in self.identity_errs(rows)))

    def test_a_detail_not_shown_on_the_page_is_refused(self):
        rows = [dict(target()["identity"][0], on_page="three generations of tailors"), target()["identity"][1]]
        self.assertTrue(any('"three generations of tailors" is not on the page' in e
                            for e in self.identity_errs(rows)))

    def test_a_detail_backs_its_own_claim(self):
        # "since 1998" is a claim word; sourced as identity, it needs no second entry.
        errs = self.errors(target(claims=[]))
        self.assertFalse([e for e in errs if "is a claim with no source" in e], errs)


class TheFocusIsEnforcedByCode(PaulCase):
    """"Restaurants/cafes first" (owner, 2026-10-08). Asking the model for a
    cafe is a request; refusing a plumber at the check is a rule. And a food
    business's site must come from the template, not a blank page - the
    template is the quality bar the owner chose."""

    def test_set_shown_and_cleared(self):
        rc, said = self.run_quiet(paul.cmd_focus, self.args(kind="food", clear=False))
        self.assertEqual((rc, paul.focus_kind()), (0, "food"))
        self.assertIn("restaurant, cafe", said)
        rc, _ = self.run_quiet(paul.cmd_focus, self.args(kind="bakery", clear=False))
        self.assertEqual((rc, paul.focus_kind()), (2, "food"), "not a kind: refused, unchanged")
        self.run_quiet(paul.cmd_focus, self.args(kind=None, clear=True))
        self.assertIsNone(paul.focus_kind())

    def test_another_kind_is_refused_while_focused(self):
        self.assertFalse([e for e in paul.validate_target(target()) if "focus" in e], "no focus, any kind")
        self.run_quiet(paul.cmd_focus, self.args(kind="food", clear=False))
        errs = paul.validate_target(target())
        self.assertTrue(any('the focus is "food"' in e for e in errs), errs)
        self.assertFalse([e for e in paul.validate_target(target(kind="food")) if "focus" in e])
        self.assertEqual(paul.validate_target({"status": "no-target", "why": "no cafe without a site"}), [],
                         "none good enough is still a valid day")

    def test_the_brief_says_so(self):
        get = lambda url, **kw: offline(url)
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=get)
        self.assertNotIn("FOCUS", facts)
        self.run_quiet(paul.cmd_focus, self.args(kind="food", clear=False))
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=get)
        self.assertIn("FOCUS        a restaurant, cafe, bakery, coffee shop or food truck only", facts)


class TheVillageSeesWhatHeBuilt(PaulCase):
    """The owner works from an iPad, where agents/paul/sites/ cannot be opened.
    `deck` is everything the village's Paul panel shows: newest build first,
    whether the site and each screenshot exist, what makes it theirs, the
    pitch - read from the files finish() wrote, never restated."""

    def test_builds_newest_first_with_what_exists(self):
        slug = "rosas-tailoring-concept-abc123"
        paul.log_build({"day": "2026-10-07", "mode": "dry", "outcome": "below-bar", "slug": "", "name": "Joe's Diner"})
        paul.log_build({"day": "2026-10-08", "mode": "dry", "outcome": "built", "slug": slug,
                        "name": "Rosa's Tailoring", "kind": "trade", "layout": "x"})
        site, box = paul.A("sites", slug), paul.A("outbox", slug)
        site.mkdir(parents=True)
        (site / "index.html").write_text(INDEX)
        box.mkdir(parents=True)
        (box / "mobile.png").write_bytes(b"\x89PNG")
        paul.write_json(box / "target.json", target())
        (box / "draft.md").write_text("Channel: email\n\nHi Rosa")
        rc, out = self.run_quiet(paul.cmd_deck, self.args())
        d = json.loads(out)
        self.assertEqual([b["name"] for b in d["builds"]], ["Rosa's Tailoring", "Joe's Diner"])
        rosa, joe = d["builds"]
        self.assertEqual((rosa["site"], rosa["shots"], joe["site"]), (True, ["mobile"], False),
                         "no desktop.png on disk, so none is offered")
        self.assertEqual([i["detail"] for i in rosa["identity"]],
                         [i["detail"] for i in target()["identity"]])
        self.assertIn("Hi Rosa", rosa["draft"])
        self.assertFalse(d["go_live"])

    def test_nothing_built_is_an_empty_list_not_an_error(self):
        rc, out = self.run_quiet(paul.cmd_deck, self.args())
        self.assertEqual((rc, json.loads(out)["builds"]), (0, []))


HEADER_THAT_WRAPS = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;font:16px Georgia} header{display:flex;justify-content:space-between;align-items:center;
padding:14px 18px} .name{font-size:22px} .btn{background:#8b2a2a;color:#fff;padding:10px 16px;
font:700 13px sans-serif;letter-spacing:.12em;text-transform:uppercase} .ok{display:inline-block;padding:8px;
background:#222;color:#fff}</style></head><body><header><div class="name">College Hill Barbers</div>
<a class="btn" href="tel:3192779972">Call (319) 277-9972</a></header><a class="ok" href="#">Find our shop</a>
</body></html>"""


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
def _png(w, h):
    """A plain w x h PNG, written byte by byte - no image library needed."""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes([120, 90, 60]) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
class PhotosAsAVisitorSeesThem(unittest.TestCase):
    """Royal Edge (2026-10-21), the owner: "it overstretched the image". A
    photo blown up past the file, or pulled out of its shape, passed every
    check. Measured now in the page itself, below the fold too."""

    def site(self, d, body):
        d = Path(d)
        (d / "img").mkdir()
        (d / "img" / "big.png").write_bytes(_png(800, 800))
        (d / "img" / "small.png").write_bytes(_png(200, 300))
        (d / "img" / "wide.png").write_bytes(_png(800, 400))
        (d / "index.html").write_text('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                                      '<style>body{margin:0}img{display:block}</style></head><body>' + body
                                      + '</body></html>')

    def test_blown_up_and_stretched_are_caught_and_a_fitting_photo_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            self.site(d, '<img src="img/big.png" style="width:400px;height:400px;object-fit:cover" alt="a">'
                         '<img src="img/small.png" style="width:400px;height:600px;object-fit:cover" alt="b">'
                         '<img src="img/wide.png" style="width:400px;height:400px" alt="c">'
                         '<div style="height:9000px"></div>'
                         '<img loading="lazy" src="img/small.png" style="width:500px;height:750px;object-fit:cover" alt="d">')
            got = paul.image_problems(d, _a_browser())
            self.assertEqual(sorted(os.listdir(d)), ["img", "index.html"], "the probe was left behind")
        self.assertEqual([(src, kind) for _p, src, kind, _d in got],
                         [("img/small.png", "enlarged"), ("img/wide.png", "stretched"), ("img/wide.png", "enlarged"),
                          ("img/small.png", "enlarged")],
                         "below the fold, lazy, is measured too")
        self.assertIn("shown at 400x600 from a 200x300 photo; a phone or iPad screen needs 1.5x that, "
                      "so at most 133px wide", got[0][3])

    def test_a_photo_with_the_detail_for_a_sharp_screen_passes(self):
        with tempfile.TemporaryDirectory() as d:
            self.site(d, '<img src="img/big.png" style="width:100%;max-width:533px;height:auto" alt="a">'
                         '<img src="img/wide.png" style="width:260px;height:260px;object-fit:cover" alt="b">')
            self.assertEqual(paul.image_problems(d, _a_browser()), [])

    def test_shown_at_its_own_size_is_soft_on_a_sharp_screen(self):
        # Royal Edge's About photos (owner, 2026-10-21, on the iPad: "all
        # over stretched and look bad") passed at 1x: shown about as wide as
        # the file. An iPad draws two dots per pixel.
        with tempfile.TemporaryDirectory() as d:
            self.site(d, '<img src="img/big.png" style="width:100%;max-width:800px;height:auto" alt="a">')
            [(_p, src, kind, detail)] = paul.image_problems(d, _a_browser())
        self.assertEqual((src, kind), ("img/big.png", "enlarged"))
        self.assertIn("at most 533px wide", detail)


class APhoneSeesWhatAPhoneSees(unittest.TestCase):
    """College Hill Barbers (2026-10-06): on a phone the header's "CALL (319)
    277-9972" broke onto two lines, in the screenshot Paul read, and passed.
    Two bugs under it. No check measured a phone at all; and headless Chrome
    will not lay out narrower than 500px, so a "390px" screenshot was a
    500px page with its right edge cut off (measured 2026-10-13). Both are
    now drawn inside an iframe of exactly the phone's width."""

    def test_a_button_that_wraps_is_caught_and_its_fix_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text(HEADER_THAT_WRAPS)
            got = paul.phone_problems(d, _a_browser())
            self.assertEqual(sorted(os.listdir(d)), ["index.html"], "the probe files were left behind")
            self.assertEqual(got["wraps"], [("CALL (319) 277-9972", 2)])
            self.assertFalse(got["wide"])
            fixed = HEADER_THAT_WRAPS.replace("Call (319) 277-9972", "Call").replace(".btn{", ".btn{white-space:nowrap;")
            (Path(d) / "index.html").write_text(fixed)
            self.assertEqual(paul.phone_problems(d, _a_browser())["wraps"], [])

    # Royal Edge (2026-10-21): "not built - the phone-width check reported
    # "Royal Edge" breaking onto 2 lines even after nowrap". It was one line;
    # every rect at its own height counted as a line - a crown beside the
    # name, the two words in two sizes. The real wraps must still be caught.
    LOGO = ('<!doctype html><html><head><meta name="viewport" content="width=device-width"><style>'
            'body{margin:0;font-family:Georgia,serif}a{white-space:nowrap}</style></head><body>%s</body></html>')

    def phone(self, markup):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text(self.LOGO % markup)
            return paul.phone_problems(d, _a_browser())["wraps"]

    def test_a_one_line_logo_is_one_line(self):
        for markup in (
                '<a href="#"><svg width="28" height="28" viewBox="0 0 10 10"><path d="M0 8L2 2 5 6 8 2 10 8Z"/></svg> Royal Edge</a>',
                '<a href="#"><span style="font-size:30px">Royal</span> <span style="font-size:14px">EDGE</span></a>',
                '<a href="#" style="display:flex;align-items:center;gap:8px"><span style="font-size:26px">&#9819;</span>'
                '<span style="font-size:20px">Royal Edge</span></a>',
                '<a href="#"><span style="display:block;font-size:26px">Royal Edge</span>'
                '<span style="display:block;font-size:11px">BARBERSHOP</span></a>'):
            self.assertEqual(self.phone(markup), [], markup)

    def test_a_real_wrap_is_still_caught(self):
        for markup in (
                '<a class="btn" href="#" style="display:inline-block;white-space:normal;width:120px;font-size:22px">'
                '<span>Call</span> <b>(319) 427-5796</b></a>',
                '<a class="btn" href="#" style="display:inline-block;white-space:normal;width:120px;font-size:22px;'
                'line-height:.85">Call (319) 427-5796</a>'):
            self.assertEqual(self.phone(markup), [("Call (319) 427-5796", 2)], markup)

    def test_a_page_wider_than_a_phone_is_caught(self):
        # 450px fits Chrome's 500px floor: only a true 390px layout sees it.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text(
                '<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                '</head><body style="margin:0"><div style="width:450px;height:9px"></div></body></html>')
            self.assertTrue(paul.phone_problems(d, _a_browser())["wide"])

    def test_the_phone_screenshot_is_a_390px_page_not_a_cropped_500px_one(self):
        # A full-width bar with a red right edge: at a true 390px the red is
        # in the picture; on a cropped 500px layout it is 110px off it.
        sys.path.insert(0, str(ROOT / "scripts"))
        import knockout
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text(
                '<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                '<style>body{margin:0}div{height:40px;border-right:10px solid #f00;box-sizing:border-box}'
                '</style></head><body><div></div></body></html>')
            out = Path(d) / "m.png"
            self.assertTrue(paul.screenshot(_a_browser(), (Path(d) / "index.html").as_uri(), out, 390, 844))
            w, h, px = knockout.decode(out)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["index.html", "m.png"])
        i = (20 * w + 385) * 4
        self.assertEqual(w, 390)
        self.assertGreater(px[i], 200, "the red right edge is in the picture")
        self.assertLess(px[i + 1], 60)

    def test_check_refuses_it(self):
        t = target()
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            (w / "site").mkdir()
            (w / "site" / "index.html").write_text(HEADER_THAT_WRAPS)
            saved = paul.find_browser
            paul.find_browser = _a_browser
            try:
                errs, _n, _i = paul.run_checks(t, offline, work=w)
            finally:
                paul.find_browser = saved
        self.assertTrue(any('"CALL (319) 277-9972" breaks onto 2 lines' in e for e in errs), errs)


class TheOwnerSendsASiteBack(PaulCase):
    """Owner, 2026-10-13: "get Paul up and running again to fix this barbershop
    website." A cycle picks a new business; `revise` puts a finished site
    back in work/ - the folder a cycle resumes - with REVISE.md, so the next
    cycle fixes that one instead."""

    def built(self, name="College Hill Barbers", slug="college-hill-barbers-concept-abc123"):
        site, box = paul.A("sites", slug), paul.A("outbox", slug)
        site.mkdir(parents=True)
        box.mkdir(parents=True)
        (site / "index.html").write_text(INDEX)
        paul.apply_safeguards(site, name, "Sam's Studio")
        paul.write_json(box / "target.json", target(name=name))
        (box / "draft.md").write_text("DRY RUN - not for sending.\n\nChannel: email\nTo: x@y.test\n\nHello.\n")
        return slug

    def test_it_goes_back_to_work_with_the_notes_and_without_the_safeguards(self):
        slug = self.built()
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who="college hill", notes=["fix", "the", "header"]))
        self.assertEqual(rc, 0, out)
        w = paul.A("work")
        self.assertIn("fix the header", (w / "REVISE.md").read_text())
        self.assertEqual(paul.read_json(w / "target.json")["name"], "College Hill Barbers")
        page = (w / "site" / "index.html").read_text()
        self.assertNotIn(paul.CONCEPT_ID, page, "code adds the notice again at finish")
        self.assertNotIn('name="robots"', page)
        self.assertFalse((w / "site" / "robots.txt").exists())
        self.assertEqual((w / "draft.md").read_text(), "Channel: email\nTo: x@y.test\n\nHello.\n",
                         "the pitch rides along, without the dry-run line")
        self.assertTrue((paul.A("sites", slug) / "index.html").is_file(), "the old build stays")
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn('"College Hill Barbers", sent back by the owner', facts)
        self.assertIn("read work/REVISE.md", facts)

    def test_an_old_build_is_not_set_aside_as_abandoned(self):
        # The copied site keeps its old file times; RESUME_DAYS must not
        # read a 7-day-old build sent back today as an abandoned one.
        self.built()
        old = time.time() - 7 * 86400
        for f in paul.A("sites").rglob("*"):
            os.utime(f, (old, old))
        self.run_quiet(paul.cmd_revise, self.args(who="College Hill", notes=["x"]))
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("sent back by the owner", facts)
        self.assertTrue(paul.A("work", "REVISE.md").is_file())

    def test_it_will_not_overwrite_a_build_in_progress(self):
        self.built()
        self.work()
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who="College Hill", notes=["x"]))
        self.assertEqual(rc, 1)
        self.assertIn("unfinished build", out)
        self.assertFalse(paul.A("work", "REVISE.md").exists())

    def test_an_unknown_or_ambiguous_name_is_refused(self):
        self.built()
        self.built(name="College Hill Bakery", slug="college-hill-bakery-concept-def456")
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who="Nobody", notes=["x"]))
        self.assertEqual(rc, 1)
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who="College Hill", notes=["x"]))
        self.assertEqual(rc, 1)
        self.assertIn("more than one", out)
        rc, _ = self.run_quiet(paul.cmd_revise, self.args(who="Bakery", notes=["x"]))
        self.assertEqual(rc, 0)

    def test_paul_is_told_what_to_do_with_it(self):
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("work/REVISE.md", header)


def by_host(sites_dir):
    """live_get, serving each preview from its own folder - two builds of one
    business are on disk during a revision, and live_get takes the first."""
    def get(url, timeout=20, limit=400_000):
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        if not host.endswith(".vercel.app"):
            return offline(url)
        f = Path(sites_dir) / host.split(".")[0] / (urllib.parse.unquote(u.path.lstrip("/")) or "index.html")
        if not f.is_file():
            return page("", 404, url)
        return page(f.read_text(errors="replace"), 200, url, {"x-robots-tag": "noindex, nofollow"})
    return get


class ARevisionShipsAndSaysSo(TheOwnerSendsASiteBack):
    """Royal Edge, 2026-10-21. Sent back to fix a photo, Paul fixed it - and
    his check refused it: "Royal Edge already in contacted.csv", its own row.
    And the owner could not tell where it had got to: "I need to be able to
    tell the process of what's going on like if it's building still or if
    it's updated and to look at the new site." """

    test_it_goes_back_to_work_with_the_notes_and_without_the_safeguards = None
    test_an_old_build_is_not_set_aside_as_abandoned = None
    test_it_will_not_overwrite_a_build_in_progress = None
    test_an_unknown_or_ambiguous_name_is_refused = None
    test_paul_is_told_what_to_do_with_it = None

    OLD = "rosas-tailoring-concept-abc123"

    def drafted(self):
        slug = self.built(name="Rosa's Tailoring", slug=self.OLD)
        paul.log_build({"day": "2026-10-20", "mode": "live", "outcome": "built", "slug": slug,
                        "name": "Rosa's Tailoring"})
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"date": "2026-10-20", "slug": slug, "business": "Rosa's Tailoring",
                         "phone": "(512) 444-0199", "preview_url": f"https://{slug}.vercel.app",
                         "status": "drafted"})
        return slug

    def deck(self):
        _rc, out = self.run_quiet(paul.cmd_deck, self.args())
        return {b["slug"]: b for b in json.loads(out)["builds"]}

    def test_a_drafted_business_revised_ships_replaces_its_row_and_says_so(self):
        old = self.drafted()
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who=old, notes=["the", "shop", "photo", "is", "a", "customer"]))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.deck()[old]["revision"]["state"], "sent")
        self.run_quiet(paul.cmd_brief, self.args(dry=False), get=offline)
        self.assertEqual(paul.revisions()[old]["state"], "building", "his cycle took it")
        self.work()
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=False), get=by_host(paul.A("sites")),
                                 api=api, sleep=lambda s: None)
        self.assertEqual(rc, 0, out)
        new = api.name
        rows = paul.contacted()
        self.assertEqual(len(rows), 1, "one business, one row - not a second pitch")
        self.assertEqual((rows[0]["slug"], rows[0]["preview_url"], rows[0]["status"]),
                         (new, f"https://{new}.vercel.app", "drafted"))
        deck = self.deck()
        self.assertEqual((deck[old]["revision"]["state"], deck[old]["revision"]["new_slug"]), ("updated", new))
        self.assertEqual(deck[new]["from_note"]["notes"], "the shop photo is a customer")

    def test_a_new_business_already_pitched_is_still_refused(self):
        self.drafted()
        self.work()
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=False), get=by_host(paul.A("sites")),
                                 api=api, sleep=lambda s: None)
        self.assertEqual(rc, 1)
        self.assertIn("already in contacted.csv", out, "only a revision passes its own row")

    def test_a_revision_that_did_not_ship_is_resumed_on_the_next_send_back(self):
        old = self.drafted()
        self.run_quiet(paul.cmd_revise, self.args(who=old, notes=["use", "the", "storefront"]))
        (paul.A("work", "site", "index.html")).write_text(INDEX.replace("</body>", "<p>PAULS FIX</p></body>"))
        paul.write_json(paul.A("work", "target.json"), {"status": "below-bar", "why": "the check refused it"})
        paul.save_cycle({"started": int(time.time()) - 5, "mode": "live", "day": paul.today(), "passes": 0})
        paul.A("state", "last-run.txt").write_text("2026-10-21 | resumed: Rosa's\n")
        self.run_quiet(paul.cmd_finish, self.args(dry=False), get=offline, api=FakeVercel(), sleep=lambda s: None)
        self.assertEqual(paul.revisions()[old]["state"], "not shipped")
        self.assertIn("the check refused it", paul.revisions()[old]["why"])
        # Made before revise.json existed (Royal Edge's): found by its REVISE.md.
        kept = next(paul.A("sites", "_below-bar").iterdir())
        (kept / "revise.json").unlink()
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who=old, notes=["finish", "your", "fix"]))
        self.assertEqual(rc, 0, out)
        self.assertIn("resuming your last revision", out)
        self.assertIn("PAULS FIX", paul.A("work", "site", "index.html").read_text(), "his work, not the old site")
        revise = paul.A("work", "REVISE.md").read_text()
        self.assertIn("finish your fix", revise)
        self.assertIn("The owner asked then: use the storefront", revise)

    def test_the_studio_shows_it_and_can_delete(self):
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        for bit in ("function revision(b)", "UPDATED FROM YOUR NOTE", "PAUL IS WORKING NOW",
                    'data-drop="${esc(b.slug)}"', "${p.status !== 'rejected' ? photos(p) : ''}"):
            self.assertIn(bit, studio)
        js = (ROOT / "mission-control-api" / "paul.js").read_text()
        self.assertIn("paul(['drop', slug], 120000)", js, "one version - never a whole business (2026-10-22)")


def prospect(**over):
    """A prospect as Paul writes one in a scouting cycle: the build's facts,
    minus the site, plus a line for the owner."""
    t = target()
    p = {k: t[k] for k in ("name", "category", "kind", "address", "phone", "hours", "current_site",
                           "problems", "channel", "contact", "sources")}
    p.update(links={"facebook": "https://facebook.com/rosastailoring"},
             why="Sewing by hand since 1998, no website of her own.")
    p.update(over)
    return p


class TheOwnerPicksPaulBuilds(PaulCase):
    """Owner, 2026-10-13 (after another ecosystem's "Paul's Studio"): Paul
    lists prospects, the owner presses Approve + build or Reject, and only an
    approved business is built. Before, every cycle chose and built in one go,
    so a site the owner would never have picked was paid for anyway. Code
    decides which kind of cycle comes next (cycle_plan)."""

    def seed(self, *rows):
        paul.save_prospects([dict(id=i + 1, found="2026-10-13", **r) for i, r in enumerate(rows)])

    # --- which cycle -------------------------------------------------------

    def test_the_plan(self):
        self.assertEqual(paul.cycle_plan(), ("scout", paul.PROSPECT_POOL))
        self.seed(*[dict(prospect(name=f"B{n}", phone=f"(512) 444-01{n}0"), status="waiting") for n in range(3)])
        self.assertEqual(paul.cycle_plan(), ("scout", paul.PROSPECT_POOL - 3))
        self.seed(*[dict(prospect(name=f"B{n}"), status="waiting") for n in range(paul.PROSPECT_POOL)])
        kind, why = paul.cycle_plan()
        self.assertEqual(kind, "hold")
        self.assertIn("waiting for your call", why)
        self.assertIn("waiting for your call", paul.hold_reason(dry=True), "no model is woken")

    def test_an_approved_one_is_built_first_oldest_first(self):
        self.seed(dict(prospect(name="Later"), status="approved", decided="2026-10-13T15:00:00Z"),
                  dict(prospect(name="Sooner"), status="approved", decided="2026-10-13T09:00:00Z"),
                  dict(prospect(name="Waiting"), status="waiting"))
        kind, p = paul.cycle_plan()
        self.assertEqual((kind, p["name"]), ("build", "Sooner"))

    def test_work_in_progress_comes_before_anything(self):
        self.seed(dict(prospect(), status="approved", decided="x"))
        self.work()
        self.assertEqual(paul.cycle_plan()[0], "resume")

    # --- the brief ---------------------------------------------------------

    def test_a_scouting_brief(self):
        self.seed(dict(prospect(name="Old One"), status="rejected"))
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn(f"SCOUT: find {paul.PROSPECT_POOL - 0} new prospect(s)", facts)
        self.assertIn("Old One | 5124440199 (prospect, rejected)", facts, "a rejected prospect is on the skip list")
        self.assertEqual(paul.load_cycle()["kind"], "scout")

    def test_a_building_brief_hands_paul_the_approved_business(self):
        self.seed(dict(prospect(), status="approved", decided="2026-10-13T09:00:00Z"))
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("BUILD: the owner approved Rosa's Tailoring", facts)
        self.assertEqual(paul.read_json(paul.A("work", "prospect.json"))["name"], "Rosa's Tailoring")
        self.assertEqual((paul.load_cycle()["kind"], paul.load_cycle()["prospect"]), ("build", 1))

    def test_an_interrupted_build_still_closes_its_prospect(self):
        self.seed(dict(prospect(), status="approved", decided="x"))
        self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)    # work/prospect.json
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("RESUME", facts)
        self.assertEqual(paul.load_cycle()["prospect"], 1, "or it is built again tomorrow")

    # --- a scouting cycle's files -------------------------------------------

    def scouted(self, rows, why=None):
        paul.save_cycle({"started": time.time() - 5, "kind": "scout", "mode": "dry", "day": paul.today()})
        paul.write_json(paul.A("work", "prospects.json"), {"prospects": rows, **({"why": why} if why else {})})
        paul.A("state", "last-run.txt").write_text("2026-10-13 | prospects: found some\n")

    def test_good_prospects_are_filed_for_the_owner_and_bad_ones_dropped(self):
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"slug": "x", "business": "Already Pitched", "phone": "(512) 444-9999", "status": "sent"})
        no_source = prospect(name="Unsourced Cafe", phone="(512) 444-0001")
        no_source["sources"] = {}
        self.scouted([prospect(), prospect(name="Already Pitched", phone="(512) 444-9999"),
                      no_source, prospect(name="Rosa's Tailoring LLC"), prospect(name="No Why", why="",
                                                                               phone="(512) 444-0002")])
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline)
        self.assertEqual(rc, 0, out)
        rows = paul.load_prospects()
        self.assertEqual([(p["id"], p["name"], p["status"]) for p in rows], [(1, "Rosa's Tailoring", "waiting")])
        self.assertIn("1 added, 4 dropped", out)
        self.assertIn("found 1 prospect(s): Rosa's Tailoring (4 dropped)", paul.A("MEMORY.md").read_text())
        self.assertFalse(paul.A("work").exists())
        self.assertTrue(next(paul.A("reports").glob("*-prospects.md")).is_file())

    def test_a_problem_their_own_site_disproves_is_dropped(self):
        # Paul says the domain is dead; it loads.
        p = prospect(current_site="https://rosas.test", problems=[{"code": "dead-domain", "evidence": "x"}])
        self.scouted([p])
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=True),
                                 get=lambda url, **kw: page("<html><body>Rosa's Tailoring</body></html>", 200, url))
        self.assertEqual(paul.load_prospects(), [])
        self.assertIn("its website says otherwise", out)

    def test_none_found_needs_a_why(self):
        self.scouted([], why="nine checked, all had good sites")
        self.assertEqual(self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline)[0], 0)
        self.scouted([])
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline)
        self.assertEqual(rc, 1)
        self.assertIn('no "why"', out)

    def test_pauls_check_reads_the_prospects_the_same_way(self):
        self.scouted([prospect(), prospect(name="X", sources={})])
        rc, out = self.run_quiet(paul.cmd_check, self.args(), get=offline)
        self.assertEqual(rc, 0, out)
        self.assertIn("1 would be filed, 1 dropped", out)

    # --- the owner's buttons -------------------------------------------------

    def test_approve_and_build_starts_a_dry_cycle_until_he_goes_live(self):
        self.seed(dict(prospect(), status="waiting"))
        ran = []
        rc, out = self.run_quiet(paul.cmd_approve, self.args(who="1", build=True),
                                 run=lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.assertEqual(rc, 0, out)
        self.assertEqual(paul.load_prospects()[0]["status"], "approved")
        self.assertEqual(ran, [["systemctl", "start", "--no-block", "paul-cycle.service"]])
        self.assertTrue(paul.A("state", "dry-next").is_file(), "not live: the build is a dry one")
        self.assertIn("a dry run", out)

    def test_live_approval_builds_live(self):
        self.seed(dict(prospect(), status="waiting"))
        paul.write_json(paul.A("state", "live.json"), {"since": "2026-10-01"})
        self.run_quiet(paul.cmd_approve, self.args(who="Rosa", build=True),
                       run=lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.assertFalse(paul.A("state", "dry-next").exists())

    def test_reject_is_for_good(self):
        self.seed(dict(prospect(), status="waiting"))
        self.assertEqual(self.run_quiet(paul.cmd_reject, self.args(who="1", why=["not", "a", "fit"]))[0], 0)
        self.assertEqual(paul.load_prospects()[0]["status"], "rejected")
        self.scouted([prospect()])
        self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline)
        self.assertEqual(len(paul.load_prospects()), 1, "never proposed again")

    # Royal Edge (2026-10-21): not built after its check failed - and the
    # check was wrong. Try again brings its last build back to finish it,
    # rather than paying for a new one.

    def set_aside(self, pid, name, age=0):
        d = paul.A("sites", "_below-bar", f"2026-10-20-{name}-{1000 + age}")
        (d / "site").mkdir(parents=True)
        (d / "site" / "index.html").write_text(f"<h1>{name}</h1>")
        (d / "draft.md").write_text("to: phone\n\nhi")
        paul.write_json(d / "prospect.json", {"id": pid, "name": name})
        os.utime(d, (time.time() - age, time.time() - age))
        return d

    def test_try_again_brings_the_last_build_back_to_finish(self):
        self.seed(dict(prospect(name="Royal Edge"), status="not built",
                       note="the phone-width check reported \"Royal Edge\" breaking onto 2 lines",
                       reference="https://rudysbarbershop.com/"))
        old = self.set_aside(1, "royal-edge", age=86400)
        last = self.set_aside(1, "royal-edge", age=0)
        other = self.set_aside(2, "someone-else", age=0)
        rc, out = self.run_quiet(paul.cmd_approve, self.args(who="1", build=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(paul.load_prospects()[0]["status"], "approved")
        self.assertIn("back in work/", out)
        self.assertEqual(paul.A("work", "site", "index.html").read_text(), "<h1>royal-edge</h1>")
        self.assertFalse(last.exists(), "the newest of its builds came back")
        self.assertTrue(old.exists() and other.exists(), "an older one, and another business's, stay")
        revise = paul.A("work", "REVISE.md").read_text()
        self.assertIn("breaking onto 2 lines", revise, "Paul is told why it stopped")
        self.assertIn("Run check first", revise)
        self.assertEqual(paul.read_json(paul.A("work", "prospect.json"))["reference"], "https://rudysbarbershop.com/")
        self.assertEqual(paul.cycle_plan()[0], "resume")
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("read work/REVISE.md first", facts)
        self.assertEqual(paul.load_cycle()["prospect"], 1, "the finish closes this prospect")

    def test_try_again_with_nothing_set_aside_builds_afresh(self):
        self.seed(dict(prospect(), status="not built", note="x"))
        rc, out = self.run_quiet(paul.cmd_approve, self.args(who="1", build=False))
        self.assertEqual((rc, paul.load_prospects()[0]["status"]), (0, "approved"))
        self.assertFalse(paul.A("work").exists())
        self.assertEqual(paul.cycle_plan()[0], "build")

    def test_the_studio_offers_try_again(self):
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        self.assertIn("p.status === 'waiting' || p.status === 'not built'", studio)
        self.assertIn("'↻ Try again'", studio)

    def test_a_built_one_cannot_be_rejected_after_the_fact(self):
        self.seed(dict(prospect(), status="built", slug="rosas-tailoring-concept-abc123"))
        rc, out = self.run_quiet(paul.cmd_reject, self.args(who="1", why=[]))
        self.assertEqual(rc, 1)
        self.assertIn("already built", out)
        self.assertEqual(paul.load_prospects()[0]["status"], "built")

    def test_a_name_that_fits_two_is_refused(self):
        self.seed(dict(prospect(name="Rosa's Tailoring"), status="waiting"),
                  dict(prospect(name="Rosa's Bakery", phone="(512) 444-0003"), status="waiting"))
        rc, out = self.run_quiet(paul.cmd_approve, self.args(who="Rosa", build=False))
        self.assertEqual(rc, 1)
        self.assertIn("more than one", out)

    # --- a build closes its prospect -----------------------------------------

    def build_cycle(self):
        self.seed(dict(prospect(), status="approved", decided="x"))
        self.work(cycle_mode="dry")
        cyc = paul.load_cycle()
        cyc.update(kind="build", prospect=1)
        paul.save_cycle(cyc)

    def test_a_built_site_marks_its_prospect_built(self):
        self.build_cycle()
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline, api=FakeVercel())
        self.assertEqual(rc, 0, out)
        p = paul.load_prospects()[0]
        self.assertEqual(p["status"], "built")
        self.assertTrue((paul.A("sites", p["slug"]) / "index.html").is_file())
        self.assertEqual(paul.cycle_plan()[0], "scout", "nothing approved is left")

    def test_one_that_could_not_be_built_says_why(self):
        self.build_cycle()
        paul.write_json(paul.A("work", "target.json"), {"status": "below-bar", "why": "they closed in August"})
        self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline, api=FakeVercel())
        p = paul.load_prospects()[0]
        self.assertEqual((p["status"], p["note"]), ("not built", "they closed in August"))

    def test_the_studio_reads_it_all_from_deck(self):
        self.seed(dict(prospect(), status="waiting"))
        _rc, out = self.run_quiet(paul.cmd_deck, self.args())
        d = json.loads(out)
        self.assertEqual(d["prospects"][0]["name"], "Rosa's Tailoring")
        self.assertEqual(d["plan"], {"kind": "scout", "detail": paul.PROSPECT_POOL - 1})
        self.assertEqual(d["studio"]["studio"], SENDER["studio"])

    def test_paul_is_told_about_all_three(self):
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        for word in ("SCOUT", "BUILD", "RESUME", "work/prospects.json", "work/prospect.json"):
            self.assertIn(word, header)


RUDYS = (ROOT / "tests" / "fixtures" / "reference-rudysbarbershop.html").read_text()


def with_reference(url, timeout=20, limit=400_000):
    if (urllib.parse.urlsplit(url).hostname or "") == "rudysbarbershop.com":
        return page(RUDYS, 200, url)
    return offline(url)


class TheOwnerNamesTheBusinessAndTheLook(PaulCase):
    """Owner, 2026-10-20: "I want Paul to build royal edges barbershop website
    using this site as a reference: https://rudysbarbershop.com/?ref=land-book.com".
    Before, Paul built only what he had scouted, after whichever reference he
    judged closest, so neither the business nor the look could be asked for."""

    REF = "https://rudysbarbershop.com/?ref=land-book.com"

    def build(self, name="Royal Edges Barbershop", ref=REF, where=None, now=False, run=None):
        kw = {"run": run} if run else {}
        return self.run_quiet(paul.cmd_build, self.args(name=name, ref=ref, where=where, now=now), **kw)

    def test_a_named_business_is_added_approved_with_the_reference_cleaned(self):
        rc, out = self.build(where="Waterloo, IA")
        self.assertEqual(rc, 0, out)
        [p] = paul.load_prospects()
        self.assertEqual((p["name"], p["status"], p["source"], p["where"]),
                         ("Royal Edges Barbershop", "approved", "owner", "Waterloo, IA"))
        self.assertEqual(p["reference"], "https://rudysbarbershop.com/",
                         "?ref=land-book.com is where the owner found it, not part of the site")
        self.assertEqual(paul.cycle_plan()[0:1], ("build",))

    def test_one_paul_already_found_is_approved_not_listed_twice(self):
        paul.save_prospects([dict(prospect(name="Royal Edges Barber Shop"), id=4, found="x", status="waiting")])
        self.build()
        [p] = paul.load_prospects()
        self.assertEqual((p["id"], p["status"], p["reference"]), (4, "approved", "https://rudysbarbershop.com/"))
        self.assertNotIn("source", p, "it keeps the facts Paul found for it")

    def test_contacted_built_and_not_a_link_are_refused(self):
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"slug": "x", "business": "Royal Edges Barbershop", "phone": "", "status": "sent"})
        self.assertEqual(self.build()[0], 1)
        self.assertEqual(paul.load_prospects(), [])
        self.assertEqual(self.build(name="Elsewhere", ref="rudys")[0], 1)
        paul.save_prospects([dict(prospect(name="Done Cuts"), id=1, found="x", status="built")])
        rc, out = self.build(name="Done Cuts")
        self.assertEqual(rc, 1)
        self.assertIn("revise", out)

    def test_now_starts_the_cycle(self):
        ran = []
        self.build(now=True, run=lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.assertEqual(ran, [["systemctl", "start", "--no-block", "paul-cycle.service"]])

    def test_the_brief_hands_paul_the_reference_and_the_research(self):
        self.build(where="Waterloo, IA")
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("BUILD: the owner approved Royal Edges Barbershop", facts)
        self.assertIn("OWNER'S REF  https://rudysbarbershop.com/", facts)
        self.assertIn("named        the owner named Royal Edges Barbershop (Waterloo, IA)", facts)
        # A prospect Paul scouted, with no reference: neither line.
        shutil.rmtree(paul.A("work"))
        paul.save_prospects([dict(prospect(), id=1, found="x", status="approved", decided="x")])
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertNotIn("OWNER'S REF", facts)
        self.assertNotIn("named ", facts)

    def problems(self, text, name="Royal Edges Barbershop"):
        return paul.reference_problems(text, {"name": name}, "https://rudysbarbershop.com/", with_reference)

    def test_its_name_and_its_sentences_are_refused_its_ideas_are_not(self):
        own = ("Royal Edges Barbershop. Fades, tapers and beard work on Main Street. "
               "Walk-ins welcome, booking is best. Freedom to wear it your way.")
        self.assertEqual(self.problems(own), ([], []))
        errs, _ = self.problems(own + " A Rudy's-style shop.")
        self.assertIn("is the reference's name", errs[0])
        errs, _ = self.problems(own + " Like Rudys Barbershop.")
        self.assertIn("is the reference's name", errs[0])
        errs, _ = self.problems(own + " We'll snip it, chop it, buzz it or trim it anyway you'd like.")
        self.assertIn("is the reference's own wording", errs[0])
        self.assertIn("well snip it chop it buzz it", errs[0])

    def test_a_reference_that_does_not_load_is_a_note_not_a_failure(self):
        errs, notes = paul.reference_problems("anything", {"name": "X"}, "https://gone.test/", offline)
        self.assertEqual(errs, [])
        self.assertIn("not checked for copying", notes[0])

    def test_pauls_check_runs_it(self):
        self.build()
        self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        copied = INDEX.replace("</body>", "<p>We’ll snip it, chop it, buzz it or trim it anyway you’d like.</p></body>")
        self.work(index=copied)
        errs, _notes, _info = paul.run_checks(target(), with_reference)
        self.assertTrue(any("reference's own wording" in e for e in errs), errs)
        self.work()
        errs, _notes, _info = paul.run_checks(target(), with_reference)
        self.assertFalse(any("reference" in e for e in errs), errs)

    def test_approve_and_build_takes_the_studios_reference(self):
        # Owner, 2026-10-20, on Royal Edge, a prospect Paul found: "I just want
        # him to reference that site when I press the approve + build button".
        paul.save_prospects([dict(prospect(name="Royal Edge"), id=7, found="x", status="waiting")])
        nothing = lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr="")
        rc, out = self.run_quiet(paul.cmd_approve, self.args(who="7", build=True, ref="https:// nope"), run=nothing)
        self.assertEqual((rc, paul.load_prospects()[0]["status"]), (1, "waiting"), out)
        rc, out = self.run_quiet(paul.cmd_approve, self.args(who="7", build=True, ref=self.REF), run=nothing)
        self.assertEqual(rc, 0, out)
        [p] = paul.load_prospects()
        self.assertEqual((p["status"], p["reference"]), ("approved", "https://rudysbarbershop.com/"))
        self.assertEqual(p["address"], prospect()["address"], "the facts Paul found stay")
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("OWNER'S REF  https://rudysbarbershop.com/", facts)
        self.assertNotIn("named ", facts, "Paul found this one; its facts are checked")

    def test_the_approve_route_passes_the_reference_and_only_a_link(self):
        node = shutil.which("node")
        api = ROOT / "mission-control-api"
        if not (node and (api / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").symlink_to(ROOT / "scripts")
            (root / "agents" / "paul" / "state").mkdir(parents=True)
            (root / "agents" / "paul" / "state" / "prospects.json").write_text(json.dumps(
                {"prospects": [dict(prospect(name="Royal Edge"), id=3, status="waiting")]}))
            js = (
                f"const express=require({json.dumps(str(api / 'node_modules' / 'express'))});"
                f"const paul=require({json.dumps(str(api / 'paul.js'))});"
                "const http=require('http');const app=express();app.use(express.json());paul.register(app);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const go=(p,b)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:'POST',"
                "headers:{'Content-Type':'application/json'}},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>"
                "ok([r.statusCode,s]));});q.end(JSON.stringify(b));});"
                "const out={bad:await go('/api/paul/prospects/3/approve',{ref:'--anyway; rm -rf /'}),"
                f"ok:await go('/api/paul/prospects/3/approve',{{ref:{json.dumps(self.REF)}}})}};"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, ECOSYSTEM_ROOT=d))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["bad"][0], 400, out["bad"])
            self.assertEqual(out["ok"][0], 200, out["ok"])
            [p] = json.loads((root / "agents" / "paul" / "state" / "prospects.json").read_text())["prospects"]
            self.assertEqual((p["status"], p["reference"]), ("approved", "https://rudysbarbershop.com/"))

    def test_the_check_refuses_a_blown_up_photo(self):
        self.work()
        saved = (paul.find_browser, paul.measured_contrast, paul.measured_phone, paul.measured_images)
        self.addCleanup(lambda: setattr(paul, "find_browser", saved[0]) or setattr(paul, "measured_contrast", saved[1])
                        or setattr(paul, "measured_phone", saved[2]) or setattr(paul, "measured_images", saved[3]))
        paul.find_browser = lambda: "chrome"
        paul.measured_contrast = lambda *a, **k: []
        paul.measured_phone = lambda *a, **k: {"wraps": [], "wide": False}
        paul.measured_images = lambda *a, **k: [("index.html", "img/shop.jpg", "enlarged",
                                                 "shown at 768x1150 from a 300x450 photo, 2.56x")]
        errs, _notes, _info = paul.run_checks(target(), offline)
        self.assertTrue(any("img/shop.jpg on index.html is blown up" in e for e in errs), errs)

    def test_the_studio_can_send_a_site_back(self):
        node = shutil.which("node")
        api = ROOT / "mission-control-api"
        if not (node and (api / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").symlink_to(ROOT / "scripts")
            slug = "royal-edge-concept-y8ehkl"
            (root / "agents" / "paul" / "sites" / slug).mkdir(parents=True)
            (root / "agents" / "paul" / "sites" / slug / "index.html").write_text("<h1>Royal Edge</h1>")
            (root / "agents" / "paul" / "outbox" / slug).mkdir(parents=True)
            (root / "agents" / "paul" / "outbox" / slug / "target.json").write_text(json.dumps(target(name="Royal Edge")))
            note = "The photo under THE SHOP is a customer, not the front windows"
            js = (
                f"const express=require({json.dumps(str(api / 'node_modules' / 'express'))});"
                f"const paul=require({json.dumps(str(api / 'paul.js'))});"
                "const http=require('http');const app=express();app.use(express.json());paul.register(app);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const go=(p,b)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:'POST',"
                "headers:{'Content-Type':'application/json'}},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>"
                "ok([r.statusCode,s]));});q.end(JSON.stringify(b));});"
                f"const out={{empty:await go('/api/paul/revise/{slug}',{{notes:' '}}),"
                "badslug:await go('/api/paul/revise/..%2Fx',{notes:'fix it'}),"
                f"badref:await go('/api/paul/revise/{slug}',{{notes:'fix it',ref:'mancave'}}),"
                f"ok:await go('/api/paul/revise/{slug}',{{notes:{json.dumps(note)},ref:'https://mancaveformen.com'}})}};"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, ECOSYSTEM_ROOT=d))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual((out["empty"][0], out["badslug"][0], out["badref"][0]), (400, 400, 400))
            self.assertEqual(out["ok"][0], 200, out["ok"])
            revise = (root / "agents" / "paul" / "work" / "REVISE.md").read_text()
            self.assertIn(note, revise, "Paul reads the owner's words")
            self.assertTrue((root / "agents" / "paul" / "work" / "site" / "index.html").is_file())
            # The new reference box (2026-10-21), as approve's is.
            self.assertEqual(json.loads((root / "agents" / "paul" / "work" / "prospect.json").read_text())["reference"],
                             "https://mancaveformen.com")
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        self.assertIn('data-revise="${esc(b.slug)}"', studio)
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("**Every caption says what is in that photo.**", header)

    def test_the_studio_shows_it_and_the_header_says_it(self):
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        self.assertIn("Your reference", studio)
        self.assertIn('data-ref="${esc(p.id)}"', studio, "a reference box on each prospect")
        self.assertIn("{ ref: refs[a.dataset.approve].trim() }", studio, "Approve + build sends it")
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("**The owner may pick the reference**", header)


PLACES = json.loads((ROOT / "tests" / "fixtures" / "google-places-searchtext.json").read_text())
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 4000
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 4000


class GoogleIsCheckedNotCopied(PaulCase):
    """Owner, 2026-10-15: Paul could not see businesses' Facebook or
    Instagram, so their real photos never reached his sites. Google's Places
    API can say what a listing holds - but its terms forbid storing its
    content beyond the place ID, so code keeps only its own verdict: the ID,
    how many photos and how many the business posted, and what disagrees."""

    def post(self, places=None):
        sent = []

        def post(body, key):
            sent.append((body, key))
            return {"places": PLACES["places"] if places is None else places}
        return post, sent

    def test_the_listing_is_found_and_only_a_verdict_is_kept(self):
        post, sent = self.post()
        g = paul.google_check(prospect(), "k", post)
        self.assertEqual(set(g), {"checked", "found", "place_id", "photos", "owner_photos", "flags"},
                         "no name, phone, address, hours or photo of Google's is stored")
        self.assertEqual((g["found"], g["place_id"]), (True, "ChIJ-test-not-a-real-place-1"))
        self.assertEqual((g["photos"], g["owner_photos"], g["flags"]), (4, 2, []))
        self.assertIn("Rosa's Tailoring", sent[0][0]["textQuery"])
        self.assertIn("Austin", sent[0][0]["textQuery"])

    def test_a_different_business_is_not_this_one(self):
        # "Rosati's Pizza" is not "Rosa's Tailoring", whatever Google ranks first.
        post, _ = self.post([PLACES["places"][1]])
        self.assertEqual(paul.google_check(prospect(), "k", post), {"checked": paul.today(), "found": False})

    def test_what_disagrees_is_flagged(self):
        place = dict(PLACES["places"][0], nationalPhoneNumber="(512) 444-7777",
                     websiteUri="https://rosas.example", businessStatus="CLOSED_TEMPORARILY")
        post, _ = self.post([place])
        flags = paul.google_check(prospect(), "k", post)["flags"]
        self.assertEqual(len(flags), 3, flags)
        self.assertTrue(any("phone" in f for f in flags))
        self.assertTrue(any("website" in f for f in flags))
        self.assertTrue(any("temporarily closed" in f for f in flags))

    def test_a_scout_files_the_verdict_and_drops_one_closed_for_good(self):
        closed = dict(PLACES["places"][0], id="ChIJ-closed", displayName={"text": "Closed Cafe"},
                      businessStatus="CLOSED_PERMANENTLY", nationalPhoneNumber="(512) 444-0001")
        paul.A("state", "credentials.env").write_text("GOOGLE_PLACES_KEY=test-not-a-key\n")
        old = paul._places_post
        self.addCleanup(setattr, paul, "_places_post", old)
        calls = []
        paul._places_post = lambda body, key, timeout=20: calls.append(body) or {
            "places": [closed] if "Closed Cafe" in body["textQuery"] else PLACES["places"]}
        # check_on_google's default is bound at definition; pass through the module
        real = paul.check_on_google
        self.addCleanup(setattr, paul, "check_on_google", real)
        paul.check_on_google = lambda p, key=None, post=None: real(p, key, paul._places_post)
        TheOwnerPicksPaulBuilds.scouted(self, [prospect(), prospect(name="Closed Cafe", phone="(512) 444-0001")])
        rc, out = self.run_quiet(paul.cmd_finish, self.args(dry=True), get=offline)
        self.assertEqual(rc, 0, out)
        rows = paul.load_prospects()
        self.assertEqual([p["name"] for p in rows], ["Rosa's Tailoring"])
        self.assertEqual(rows[0]["google"]["owner_photos"], 2)
        self.assertIn("dropped Closed Cafe: Google lists it as permanently closed", out)
        self.assertEqual(paul.places_used(), 2)

    def test_no_key_no_call_and_a_daily_cap(self):
        post, sent = self.post()
        p = paul.check_on_google(prospect(), "", post)
        self.assertNotIn("google", p)
        self.assertEqual(sent, [])
        paul.write_json(paul.A("state", "places-usage.json"), {"day": paul.today(), "n": paul.PLACES_DAILY})
        p = paul.check_on_google(prospect(), "k", post)
        self.assertIn("lookups are used", p["google"]["error"])
        self.assertEqual(sent, [], "nothing is asked past the cap")

    def test_the_key_goes_in_a_header_never_the_url(self):
        seen = []

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"places": []}'
        old = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", old)
        urllib.request.urlopen = lambda req, timeout=20: seen.append(req) or R()
        paul._places_post({"textQuery": "x"}, "test-not-a-key")
        req = seen[0]
        self.assertNotIn("test-not-a-key", req.full_url)
        self.assertEqual(req.get_header("X-goog-api-key"), "test-not-a-key")
        self.assertEqual(req.get_header("X-goog-fieldmask"), paul.PLACES_FIELDS)
        self.assertNotIn("photos.", paul.PLACES_FIELDS.replace("places.photos", ""), "no photo media is fetched")

    def test_the_studio_gets_a_maps_link_from_the_stored_id(self):
        TheOwnerPicksPaulBuilds.seed(self, dict(prospect(), status="waiting",
                                                google={"found": True, "place_id": "ChIJ-x", "photos": 3,
                                                        "owner_photos": 1, "flags": []}))
        d = json.loads(self.run_quiet(paul.cmd_deck, self.args())[1])
        self.assertIn("query_place_id=ChIJ-x", d["prospects"][0]["maps"])
        self.assertEqual(d["prospects"][0]["supplied"], [])


class TheOwnersPhotosComeFirst(PaulCase):
    """Owner, 2026-10-15: "Add photos" in the Studio - the owner saves a
    business's photos from Facebook, Instagram or Google on the iPad and
    adds them; Paul builds with them before any stock."""

    def setUp(self):
        super().setUp()
        TheOwnerPicksPaulBuilds.seed(self, dict(prospect(), status="approved", decided="x"))

    def test_adding_checks_what_it_is_given(self):
        self.assertEqual(paul.add_photo(1, JPEG, "facebook"), ("01.jpg", None))
        self.assertEqual(paul.add_photo(1, PNG, "instagram"), ("02.png", None))
        self.assertIn("JPEG or PNG", paul.add_photo(1, b"<html>", "facebook")[1])
        self.assertIn("where it is from", paul.add_photo(1, JPEG, "myspace")[1])
        self.assertIn("MB", paul.add_photo(1, b"\xff\xd8\xff" + b"0" * paul.SUPPLIED_MAX_BYTES, "facebook")[1])
        self.assertTrue(paul.remove_photo(1, "01.jpg"))
        self.assertFalse(paul.remove_photo(1, "01.jpg"))
        self.assertEqual(paul.add_photo(1, JPEG, "google"), ("01.jpg", None), "a gap is filled")
        self.assertEqual([(r["file"], r["from"]) for r in paul.supplied(1)], [("02.png", "instagram"), ("01.jpg", "google")])
        for _ in range(paul.MAX_SUPPLIED - 2):
            paul.add_photo(1, JPEG, "facebook")
        self.assertIn("plenty", paul.add_photo(1, JPEG, "facebook")[1])

    def test_the_build_gets_them_and_is_told(self):
        paul.add_photo(1, JPEG, "facebook")
        paul.add_photo(1, JPEG, "instagram")
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("2 of THEIR OWN photos, saved by the owner from their facebook, instagram", facts)
        self.assertIn("work/site/img/yours-01.jpg", facts)
        self.assertEqual(paul.A("work", "site", "img", "yours-02.jpg").read_bytes(), JPEG)
        _rc, again = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("RESUME", again)
        self.assertEqual(len(list(paul.A("work", "site", "img").glob("yours-*"))), 2, "copied once")

    def site(self, ref, photos):
        site = paul.A("work", "site")
        (site / "img").mkdir(parents=True, exist_ok=True)
        (site / "img" / "yours-01.jpg").write_bytes(JPEG)
        (site / "img" / "hero.jpg").write_bytes(JPEG)
        (site / "index.html").write_text(INDEX.replace("img/hero.jpg", ref))
        (site / "style.css").write_text("body{margin:0}")
        return site, target(photos=photos)

    def test_the_check_wants_them_used_and_called_theirs(self):
        site, t = self.site("img/yours-01.jpg", [{"file": "img/yours-01.jpg", "source": "saved by the owner from their Facebook",
                                                  "kind": "business"}])
        errs = paul.check_site(site, t)[0]
        self.assertFalse([e for e in errs if "yours-" in e or "source must" in e], errs)
        site, t = self.site("img/yours-01.jpg", [{"file": "img/yours-01.jpg", "source": "x", "kind": "stock"}])
        self.assertTrue(any('kind "business"' in e for e in paul.check_site(site, t)[0]))
        site, t = self.site("img/hero.jpg", target()["photos"])
        self.assertTrue(any("uses none" in e for e in paul.check_site(site, t)[0]))

    def test_paul_is_told(self):
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("work/site/img/yours-*", header)


class ThePhotoRoutes(unittest.TestCase):
    """paul.js against a real root: an upload is kept by `paul.py photo add`,
    served back by its own name only, and removed the same way."""

    NODE = shutil.which("node")
    API = ROOT / "mission-control-api"

    def test_routes(self):
        if not (self.NODE and (self.API / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").symlink_to(ROOT / "scripts")
            (root / "agents" / "paul" / "state").mkdir(parents=True)
            (root / "agents" / "paul" / "state" / "prospects.json").write_text(json.dumps(
                {"prospects": [dict(prospect(), id=3, status="waiting")]}))
            js = (
                f"const express=require({json.dumps(str(self.API / 'node_modules' / 'express'))});"
                f"const paul=require({json.dumps(str(self.API / 'paul.js'))});"
                "const http=require('http');const app=express();app.use(express.json());paul.register(app);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const go=(m,p,b,t)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:m,"
                "headers:{'Content-Type':t||'image/jpeg'}},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>"
                "ok([r.statusCode,s]));});q.end(b);});"
                "const jpg=Buffer.concat([Buffer.from([0xff,0xd8,0xff,0xe0]),Buffer.alloc(3000)]);"
                "const out={up:await go('POST','/api/paul/prospects/3/photos?from=instagram',jpg),"
                "notimg:await go('POST','/api/paul/prospects/3/photos?from=facebook',Buffer.from('<html>')),"
                "badtype:await go('POST','/api/paul/prospects/3/photos?from=facebook','x','text/plain'),"
                "badfrom:await go('POST','/api/paul/prospects/3/photos?from=myspace',jpg),"
                "badid:await go('POST','/api/paul/prospects/3x/photos',jpg),"
                "get:await go('GET','/api/paul/photo/3/01.jpg'),"
                "climb:await go('GET','/api/paul/photo/3/..%2Fprospects.json'),"
                "del:await go('DELETE','/api/paul/prospects/3/photos/01.jpg'),"
                "gone:await go('GET','/api/paul/photo/3/01.jpg')};"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, ECOSYSTEM_ROOT=d))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["up"][0], 200, out["up"])
            self.assertIn("saved 01.jpg", out["up"][1])
            self.assertEqual(out["notimg"][0], 409, "paul.py decides what is a picture")
            self.assertEqual((out["badtype"][0], out["badfrom"][0], out["badid"][0]), (400, 400, 400))
            self.assertEqual(out["get"][0], 200)
            self.assertEqual(out["climb"][0], 400)
            self.assertEqual((out["del"][0], out["gone"][0]), (200, 404))
            self.assertEqual([p.name for p in (root / "agents" / "paul" / "state").glob(".upload-*")], [],
                             "no upload is left behind")


class FoundOnGoogleAndInAIAnswers(unittest.TestCase):
    """Owner, 2026-10-16: "Include SEO (search engine optimization) as well as
    AI SEO". The preview stays out of search; the site it becomes is built
    findable - and code checks it, the same way every time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / "site"
        (self.site / "img").mkdir(parents=True)

    def errs(self, index=INDEX, t=None, more=None):
        (self.site / "index.html").write_text(index)
        for name, body in (more or {}).items():
            (self.site / name).write_text(body)
        return paul.seo_problems(self.site, t or target())

    def has(self, errs, words):
        self.assertTrue(any(words in e for e in errs), f"{words!r} not in {errs}")

    def test_the_example_is_findable(self):
        self.assertEqual(self.errs(), [])

    def test_every_page_has_a_title_a_description_and_one_h1(self):
        e = self.errs(more={"about.html": "<html><head></head><body><h1>A</h1><h1>B</h1></body></html>"})
        self.has(e, "about.html: no <title>")
        self.has(e, 'about.html: no <meta name="description">')
        self.has(e, "about.html: 2 <h1> headings")
        self.has(self.errs(INDEX.replace("Alterations in Austin", "x" * 80)), "Google cuts it off")
        self.has(self.errs(INDEX.replace('content="Rosa\'s Tailoring in Austin:', 'content="Hi."><meta name="x" content="')),
                 "the description is 3 characters")

    def test_the_home_page_names_the_business_and_its_town(self):
        self.has(self.errs(INDEX.replace("<title>Rosa's Tailoring | Alterations in Austin</title>",
                                         "<title>Alterations in Austin</title>")), "does not name the business")
        self.has(self.errs(t=target(address="1801 E 7th St, Round Rock, TX 78664")), 'say "Round Rock"')

    def test_link_previews(self):
        self.has(self.errs(INDEX.replace('<meta property="og:image" content="img/hero.jpg">', "")), "og:image")

    def test_the_business_markup_matches_what_paul_found(self):
        self.has(self.errs(INDEX.replace("ProfessionalService", "FAQPage", 1)), "no schema.org JSON-LD")
        self.has(self.errs(INDEX.replace("+1-512-444-0199", "+1-512-444-0000")), '"telephone" must be their number')
        self.has(self.errs(INDEX.replace('"streetAddress": "1801 E 7th St"', '"streetAddress": "E 7th St"')),
                 "PostalAddress with streetAddress")
        self.has(self.errs(INDEX.replace('"openingHoursSpecification"', '"hoursNote"')), "no openingHoursSpecification")
        self.has(self.errs(INDEX.replace('"name": "Rosa\'s Tailoring", "telephone"', '"name": "Bob\'s", "telephone"')),
                 '"name" is "Bob\'s"')
        self.has(self.errs(INDEX.replace('{"@context": "https://schema.org", "@type": "ProfessionalService"',
                                         '{"@context": "https://schema.org", "@type": "ProfessionalService", '
                                         '"aggregateRating": {"ratingValue": 5}')), "no ratings or reviews")
        self.has(self.errs(INDEX.replace('"@type": "ProfessionalService",', '"@type": "ProfessionalService",,')),
                 "does not parse")

    def test_ai_search_gets_a_visible_faq(self):
        self.has(self.errs(INDEX.replace(SEO_FAQ, "")), "is in the markup but not on the page")
        two = INDEX.replace(',\n {"@type": "Question", "name": "How do I book a fitting?", "acceptedAnswer": '
                            '{"@type": "Answer", "text": "Call (512) 444-0199."}}', "")
        self.has(self.errs(two), "add a short FAQ")

    def test_check_site_runs_it(self):
        body = (SCRIPTS / "paul.py").read_text().split("def check_site(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("errs += seo_problems(site, t)", body)

    def test_paul_is_told(self):
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        # The contractor sites are the owner's samples (2026-10-16): "he isn't required to use anything".
        for word in ("FAQPage", "JSON-LD", "<meta name=\"description\">", "AI search", "contractorgorilla",
                     "are **optional**", "use anything, or nothing"):
            self.assertIn(word, header)


class ThePitchSaysWhatItCostsAndWhatCanChange(PaulCase):
    """Owner, 2026-10-13: "put in the pitch that the website will have a
    yearly fee", and "that they can change the pictures or colors and
    potentially add features like booking haircuts for the barbershop".
    "No monthly fees" alone let a buyer think there was no cost at all."""

    def errs(self, draft, t=None):
        return paul.check_draft(draft, t or target())[0]

    def test_the_test_pitch_passes(self):
        self.assertEqual(self.errs(DRAFT), [])

    def test_the_yearly_fee_must_be_said(self):
        d = DRAFT.replace("about $15 a year to renew the name\nwith the registrar", "nothing")
        self.assertTrue(any("once a year" in e for e in self.errs(d)), self.errs(d))
        for ok in ("$15 yearly", "$15 per year", "an annual $15", "$15/yr"):
            with self.subTest(ok):
                self.assertFalse([e for e in self.errs(d.replace("nothing", ok)) if "once a year" in e])

    def test_the_fee_is_the_one_other_price_allowed(self):
        self.assertFalse([e for e in self.errs(DRAFT) if "remove $" in e])
        d = DRAFT.replace("about $15 a year", "about $20 a year")
        self.assertTrue(any("remove $20" in e for e in self.errs(d)), self.errs(d))

    def test_photos_and_colours_are_theirs_to_change(self):
        d = DRAFT.replace("change the pictures and colours any time", "call me any time")
        self.assertTrue(any("photos and the colours" in e for e in self.errs(d)), self.errs(d))

    def test_a_barbershop_is_offered_booking(self):
        barber = target(kind="salon", category="barbershop")
        self.assertTrue(any("online booking" in e for e in self.errs(DRAFT, barber)))
        d = DRAFT.replace("a button to request a quote", "a button to book a haircut")
        self.assertFalse([e for e in self.errs(d, barber) if "feature" in e])

    def test_paul_is_told_the_fee_and_the_ideas(self):
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn(f"about ${paul.DOMAIN_FEE} a year", facts)
        self.assertIn("online booking", facts)
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("booking haircuts, for a barbershop", header)


class TheOwnerCanRemoveABusinessForGood(PaulCase):
    """Owner, 2026-10-13: "get rid of the bakery site completely, I don't want
    to see it anymore." Every trace in his studio goes - site, draft,
    screenshots, report, build log line, prospect card, a live preview - and
    he never picks it again."""

    def make(self, name, slug, kind="food"):
        site, box = paul.A("sites", slug), paul.A("outbox", slug)
        site.mkdir(parents=True)
        box.mkdir(parents=True)
        (site / "index.html").write_text(INDEX)
        paul.write_json(box / "target.json", target(name=name, kind=kind))
        (box / "draft.md").write_text("Channel: email\nTo: x@y.test\n\nHello.\n")
        (box / "mobile.png").write_bytes(b"png")
        paul.A("reports").mkdir(exist_ok=True)
        (paul.A("reports") / f"2026-10-05-{slug}.md").write_text(f"# {name}\n")
        paul.log_build({"day": "2026-10-05", "mode": "dry", "outcome": "built", "slug": slug,
                        "name": name, "kind": kind})

    def setUp(self):
        super().setUp()
        self.make("Milkbox Bakery", "milkbox-bakery-concept-abc123")
        paul.record("DRY: Milkbox Bakery | built locally, not deployed")
        self.make("Rosa's Tailoring", "rosas-tailoring-concept-def456", kind="trade")
        paul.save_prospects([dict(prospect(name="Milkbox Bakery", phone="(512) 444-0007"), id=1,
                                  status="built", found="2026-10-05")])

    def test_without_yes_it_only_lists(self):
        rc, out = self.run_quiet(paul.cmd_remove, self.args(who="bakery", yes=False))
        self.assertEqual(rc, 0)
        self.assertIn("nothing deleted", out)
        self.assertTrue(paul.A("sites", "milkbox-bakery-concept-abc123").is_dir())

    def test_it_is_gone_everywhere_and_never_picked_again(self):
        rc, out = self.run_quiet(paul.cmd_remove, self.args(who="bakery", yes=True))
        self.assertEqual(rc, 0, out)
        for gone in (paul.A("sites", "milkbox-bakery-concept-abc123"), paul.A("outbox", "milkbox-bakery-concept-abc123"),
                     paul.A("reports", "2026-10-05-milkbox-bakery-concept-abc123.md")):
            self.assertFalse(gone.exists(), gone)
        self.assertEqual([b["name"] for b in paul.builds()], ["Rosa's Tailoring"])
        self.assertEqual(paul.load_prospects(), [])
        self.assertTrue(paul.A("sites", "rosas-tailoring-concept-def456").is_dir(), "the others stay")
        _rc, deck = self.run_quiet(paul.cmd_deck, self.args())
        self.assertNotIn("Milkbox", deck)
        self.assertEqual(paul.seen_row("Milkbox Bakery")["status"], "do-not-contact")

    def test_a_live_preview_comes_down_first(self):
        paul.append_row(paul.A("state", "deployments.csv"), paul.DEPLOY_FIELDS,
                        {"slug": "milkbox-bakery-concept-abc123", "preview_url": "https://m.vercel.app",
                         "business": "Milkbox Bakery", "deployed": "2026-10-05", "status": "live"})
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_remove, self.args(who="Milkbox", yes=True), api=api)
        self.assertEqual(rc, 0, out)
        self.assertIn(("DELETE", "/v9/projects/milkbox-bakery-concept-abc123"),
                      [(c.method, c.path.split("?")[0]) for c in api.calls])
        self.assertEqual(paul.deployments()[0]["status"], "removed")

    def test_a_name_that_fits_two_is_refused(self):
        rc, out = self.run_quiet(paul.cmd_remove, self.args(who="a", yes=True))
        self.assertEqual(rc, 1)
        self.assertIn("more than one", out)
        self.assertEqual(len(paul.builds()), 2)


class PaulIsHandedLeads(PaulCase):
    """Owner, 2026-10-19: "It seems Paul is struggling to find prospects".
    He checked six Cedar Falls businesses and listed none: a web search finds
    businesses that HAVE sites, and three with only a Facebook page he dropped
    because nothing proved they had no site. Code now searches Google's
    listings first and hands him leads; weak-but-working sites count; an
    email is not required; and the nearby towns are searched too."""

    LISTINGS = [
        {"id": "P1", "displayName": {"text": "The Parlor"}, "formattedAddress": "1 Main St, Cedar Falls, IA",
         "nationalPhoneNumber": "(319) 555-0101", "businessStatus": "OPERATIONAL"},
        {"id": "P2", "displayName": {"text": "Goodlookin Salon"}, "businessStatus": "OPERATIONAL",
         "websiteUri": "https://www.facebook.com/goodlookin"},
        {"id": "P3", "displayName": {"text": "Old Site Barbers"}, "businessStatus": "OPERATIONAL",
         "websiteUri": "http://oldsite.test"},
        {"id": "P4", "displayName": {"text": "Good Site Co"}, "businessStatus": "OPERATIONAL",
         "websiteUri": "https://good.test"},
        {"id": "P5", "displayName": {"text": "Gone Shop"}, "businessStatus": "CLOSED_PERMANENTLY"},
    ]
    GOOD = ('<html><head><meta name="viewport" content="width=device-width"><title>Good</title>'
            '<meta name="description" content="We fix things"></head><p>&copy; 2026</p></html>')
    WEAK = "<html><title>Old</title><p>&copy; 2016 Old Site</p></html>"

    def setUp(self):
        super().setUp()
        paul.write_json(paul.A("state", "sender.json"), dict(SENDER, area="Cedar Falls, IA"))
        paul.write_json(paul.A("state", "nearby.json"), ["Waterloo, IA"])
        self.asked = []

    def post(self, body, key, timeout=20, fields=None):
        self.asked.append((body["textQuery"], fields))
        return {"places": self.LISTINGS}

    def get(self, url, timeout=20, limit=0):
        if "oldsite" in url:
            return page(self.WEAK, url=url) if url.startswith("http:") else offline(url)
        return page(self.GOOD, url=url)

    def leads(self, **kw):
        return paul.find_leads("k", post=self.post, get=self.get, say=lambda m: None, **kw)

    def test_no_site_and_social_only_first_then_a_weak_site(self):
        got = self.leads()
        self.assertEqual([(l["name"], l["code"]) for l in got],
                         [("The Parlor", "no-site"), ("Goodlookin Salon", "social-only"),
                          ("Old Site Barbers", "no-https")], "closed and good sites are not leads")
        self.assertIn("outdated: the newest copyright year is 2016", got[2]["why"])
        self.assertIn("query_place_id=P1", got[0]["maps"])
        self.assertIn("places.websiteUri", self.asked[0][1])

    def test_each_scout_searches_new_ground_in_both_towns(self):
        first = paul.next_searches()
        self.assertEqual({t for _q, _k, t in first}, {"Cedar Falls, IA", "Waterloo, IA"})
        self.leads()
        self.assertTrue(set(paul.next_searches()).isdisjoint(first), "the next scout asks about other kinds")
        before = paul.next_searches()
        self.leads(remember=False)
        self.assertEqual(paul.next_searches(), before, "a preview leaves the rotation alone")

    def test_a_focus_searches_only_its_kind(self):
        paul.A("state", "focus").write_text("trade")
        self.assertTrue(all(k == "trade" for _q, k, _t in paul.next_searches(focus="trade")))

    def test_known_and_contacted_businesses_are_not_leads(self):
        paul.save_prospects([{"id": 1, "name": "The Parlor", "status": "rejected"}])
        self.assertNotIn("The Parlor", [l["name"] for l in self.leads()])

    def test_google_lookups_are_kept_back_for_the_days_prospects(self):
        paul.write_json(paul.A("state", "places-usage.json"),
                        {"day": paul.today(), "n": paul.PLACES_DAILY - paul.KEEP_FOR_PROSPECTS})
        self.assertEqual(self.leads(), [])
        self.assertEqual(self.asked, [])

    def test_nothing_of_googles_is_stored(self):
        self.leads()
        for f in paul.A("state").iterdir():
            text = f.read_text() if f.is_file() else ""
            self.assertNotIn("The Parlor", text, f.name)
            self.assertNotIn("555-0101", text, f.name)

    def test_the_scout_brief_carries_the_leads(self):
        lines = "\n".join(paul.lead_lines(self.leads()))
        self.assertIn("A listing with no website IS your", lines)
        self.assertIn("The Parlor", lines)
        self.assertIn("leads        none from Google today", "\n".join(paul.lead_lines([])))
        src = (SCRIPTS / "paul.py").read_text()
        self.assertIn("L += lead_lines(find_leads(key", src)

    def test_weak_sites_are_confirmed_from_the_page(self):
        weak = page(self.WEAK, url="https://w.test/")
        rows, _ = paul.verify_problems(
            {"current_site": "https://w.test", "problems": [{"code": "no-seo"}, {"code": "outdated"}]},
            lambda url, timeout=20, limit=0: weak)
        self.assertEqual([v for _c, v, _d in rows], ["confirmed", "confirmed"])
        good = page(self.GOOD, url="https://g.test/")
        rows, _ = paul.verify_problems(
            {"current_site": "https://g.test", "problems": [{"code": "no-seo"}, {"code": "outdated"}]},
            lambda url, timeout=20, limit=0: good)
        self.assertEqual([v for _c, v, _d in rows], ["contradicted", "contradicted"])

    def test_a_facebook_page_is_social_only(self):
        rows, _ = paul.verify_problems({"current_site": "https://facebook.com/goodlookin",
                                        "problems": [{"code": "social-only"}]}, offline)
        self.assertEqual(rows[0][1], "confirmed")
        flags = paul.google_check(prospect(current_site="none"), "k",
                                  lambda body, key: {"places": [dict(PLACES["places"][0],
                                                     websiteUri="https://facebook.com/rosas")]})["flags"]
        self.assertFalse([f for f in flags if "website" in f], "a Facebook page is not a website")

    def test_the_instructions_say_it(self):
        head = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("Start with FACTS `leads`", head)
        self.assertIn("A Google\nlisting with no website on it is your evidence for `no-site`", head)
        self.assertIn("**An email is NOT required.**", head)
        self.assertIn("stopping at six is giving up early", head)
        for code in paul.PROBLEMS:
            self.assertIn(f"    {code}", head, f"every problem code is explained ({code})")


# Royal Edge after mancaveformen.com (owner, 2026-10-21): "Get rid of the
# video since I don't have one... use the pictures I have... sample pictures
# and put 'sample' on the picture. Include a drop down menu for services and
# an about us." And in the same screenshot, the revision's computer picture
# was a bare page - black serif text on white - while its phone one looked
# right.
STYLE = ("body{margin:0;background:#111;color:#eee;font-family:Arial}header{display:flex;gap:10px;"
         "padding:12px}a{color:#fc0}h1{font-size:40px;text-transform:uppercase}p{line-height:1.6;padding:4px 16px}")


def _page(d, css, body):
    d = Path(d)
    (d / "img").mkdir(exist_ok=True)
    (d / "img" / "s.png").write_bytes(_png(600, 400))
    (d / "style.css").write_text(css)
    (d / "index.html").write_text('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                                  '<link rel="stylesheet" href="style.css"></head><body>' + body + "</body></html>")


BODY = '<header><a href="#top">Royal Edge</a><a href="#about">About</a></header><h1>Fades in Waterloo</h1><p id="about">x</p>'


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
class APageStyledAtEveryWidth(unittest.TestCase):
    """The bare computer screenshot: CSS that applies at one width only - an
    @media block never closed - passed every check, because black on white
    has fine contrast and an unstyled photo is shown at its own size."""

    def both(self, css):
        with tempfile.TemporaryDirectory() as d:
            _page(d, css, BODY)
            return paul.phone_problems(d, _a_browser())["styled"], paul.image_problems(d, _a_browser())

    def test_a_styled_page_passes(self):
        phone, desk = self.both(STYLE)
        self.assertGreaterEqual(phone, paul.STYLED_MIN)
        self.assertEqual(desk, [])

    def test_css_that_only_applies_on_a_phone_is_caught_on_a_computer(self):
        phone, desk = self.both("@media (max-width:700px){" + STYLE)
        self.assertGreaterEqual(phone, paul.STYLED_MIN, "the phone looked right - as in the Studio")
        self.assertEqual([k for _p, _s, k, _d in desk], ["unstyled"])

    def test_a_stylesheet_that_does_not_load_is_caught_at_both(self):
        with tempfile.TemporaryDirectory() as d:
            _page(d, STYLE, BODY)
            (Path(d) / "index.html").write_text((Path(d) / "index.html").read_text().replace(
                'href="style.css"', 'href="/style.css"'))
            self.assertLess(paul.phone_problems(d, _a_browser())["styled"], paul.STYLED_MIN)
            self.assertEqual([k for _p, _s, k, _d in paul.image_problems(d, _a_browser())], ["unstyled"])

    def test_check_refuses_it(self):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            _page(w / "site" if (w / "site").mkdir() is None else None, "@media (max-width:700px){" + STYLE, BODY)
            saved = paul.find_browser
            paul.find_browser = _a_browser
            try:
                errs, _n, _i = paul.run_checks(target(), offline, work=w)
            finally:
                paul.find_browser = saved
        self.assertTrue(any("index.html shows unstyled (on a computer (1280px)" in e for e in errs), errs)


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
class AMenuOpensOnATap(unittest.TestCase):
    """The Services drop-down. One that opens on hover alone cannot be opened
    on a phone or the owner's iPad; the probe taps every menu button at phone
    width and names any menu link that never shows."""

    def hidden(self, css, body):
        with tempfile.TemporaryDirectory() as d:
            _page(d, STYLE + css, body)
            got = paul.phone_problems(d, _a_browser())
            self.assertEqual(sorted(os.listdir(d)), ["img", "index.html", "style.css"])
            return got["hidden"]

    def test_a_hover_only_drop_down_is_caught(self):
        self.assertEqual(self.hidden(".dd ul{display:none}.dd:hover ul{display:block}",
                                     '<header><nav><a href="#top">Home</a><div class="dd"><span>Services</span>'
                                     '<ul><li><a href="#fades">Fades</a></li></ul></div></nav></header><p id="fades">x</p>'),
                         ["Fades"])

    def test_details_and_a_menu_button_pass(self):
        self.assertEqual(self.hidden("", '<header><nav><a href="#top">Home</a><details><summary>Services</summary>'
                                         '<a href="#fades">Fades</a></details></nav></header><p id="fades">x</p>'), [])
        # The usual phone header: the computer's links hidden, a Menu button,
        # and inside it a Services drop-down that slides open.
        self.assertEqual(self.hidden(
            ".wide{display:none}.menu{max-height:0;overflow:hidden;transition:max-height .4s}.menu.open{max-height:400px}",
            '<header><nav class="wide"><a href="#fades">Fades</a></nav>'
            '<button onclick="document.querySelector(\'.menu\').classList.toggle(\'open\')">Menu</button>'
            '<nav class="menu"><details><summary>Services</summary><a href="#fades">Fades</a><a href="#beards">Beards</a>'
            '</details><a href="#about">About us</a></nav></header><p id="fades">x</p><p id="beards">y</p><p id="about">z</p>'), [])

    def test_a_link_hidden_on_a_phone_but_tappable_elsewhere_passes(self):
        self.assertEqual(self.hidden(".book{display:none}.bar{position:fixed;bottom:0}",
                                     '<header><a href="#top">Royal Edge</a><a class="book" href="https://booksy.com/x">Book</a>'
                                     '</header><p>x</p><div class="bar"><a href="https://booksy.com/x">Book now</a></div>'), [])

    def test_what_makes_a_phone_scroll_sideways_is_named(self):
        # Royal Edge's rebuild: "the phone layout scrolls sideways" twice,
        # and nothing said what to fix. A row meant to scroll is not blamed.
        css = (".menu{position:absolute;top:0;left:0;width:300px;transform:translateX(400px)}"
               ".row{display:flex;overflow-x:auto}.row div{flex:0 0 300px;height:9px}")
        with tempfile.TemporaryDirectory() as d:
            _page(d, STYLE + css, '<header><nav class="menu main"><a href="#top">Fades</a></nav></header>'
                                  '<div class="row"><div></div><div></div></div>')
            got = paul.phone_problems(d, _a_browser())
        self.assertTrue(got["wide"])
        self.assertEqual(got["over"], [('<nav class="menu main">', 700)])

    def test_an_intro_that_covers_the_page_is_waited_out(self):
        # The quality bar's own header, under its logo intro: switching
        # animations off (the first version of the probe) left the intro over
        # every link, and read the whole menu as hidden.
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(ROOT / "agents" / "paul" / "templates" / "local", Path(d) / "s")
            self.assertEqual(paul.phone_problems(Path(d) / "s", _a_browser())["hidden"], [])


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
class SamplePhotosSaySample(unittest.TestCase):
    """"I don't have any product samples... so maybe just use sample pictures
    and put 'sample' on the picture." On the picture, measured: a caption
    under it is not on it."""

    CARD = (".card{position:relative;width:300px}.card img{width:300px;height:200px;object-fit:cover;display:block}"
            ".tag{position:absolute;top:8px;left:8px;background:#000;color:#fff;font-size:13px;padding:2px 6px}")

    def bare(self, body, samples=("img/s.png",)):
        with tempfile.TemporaryDirectory() as d:
            _page(d, STYLE + self.CARD, body)
            return [(s, k) for _p, s, k, _d in paul.image_problems(d, _a_browser(), samples=samples)]

    def test_the_word_over_the_picture_passes(self):
        self.assertEqual(self.bare('<div class="card"><img src="img/s.png" alt="a"><span class="tag">Sample</span></div>'), [])

    def test_none_or_beside_it_is_caught(self):
        for body in ('<div class="card"><img src="img/s.png" alt="a"></div>',
                     '<div class="card"><img src="img/s.png" alt="a"><p>Sample</p></div>'):
            self.assertEqual(self.bare(body), [("img/s.png", "unlabelled")], body)

    def test_a_label_that_lets_taps_through_is_on_the_picture(self):
        # Royal Edge's rebuild (2026-10-21) was refused twice with its span
        # over each photo: pointer-events:none hides an element from
        # elementsFromPoint, which the probe read as no label at all.
        self.assertEqual(self.bare('<div class="card"><img src="img/s.png" alt="a">'
                                   '<span class="tag" style="pointer-events:none">Sample</span></div>'), [])

    def test_royal_edges_own_products_pass_on_a_smooth_scrolling_page(self):
        # Paul's rules, read off work/site/style.css on the droplet
        # (2026-10-21), refused three times as "behind the photo": the
        # template's scroll-behavior:smooth made scrollIntoView a glide, and
        # the probe looked before it arrived.
        css = (":root{--gold:#c9a24a}html{scroll-behavior:smooth}.prods{display:grid;gap:18px}"
               ".prods figure{position:relative;margin:0;aspect-ratio:1/1;overflow:hidden;border-radius:6px}"
               ".prods img{width:100%;height:100%;object-fit:cover;object-position:center 70%}"
               ".sample-tag{position:absolute;left:12px;top:12px;background:#0d0d0d;color:var(--gold);"
               "border:1px solid var(--gold);padding:4px 12px;border-radius:4px;font-weight:600;"
               "letter-spacing:.08em;text-transform:uppercase;font-size:.85rem}"
               "@media (min-width:760px){.prods{grid-template-columns:repeat(3,1fr)}}.prods .sample-tag{z-index:5}")
        with tempfile.TemporaryDirectory() as d:
            _page(d, STYLE + css, '<div style="height:1500px"></div><section class="prods">'
                  + '<figure><img src="img/s.png" alt="a"><span class="sample-tag">Sample</span></figure>' * 3
                  + '</section><div style="height:1500px"></div>')
            (Path(d) / "img" / "s.png").write_bytes(_png(1000, 1000))   # a Commons download at 1000px
            got = paul.image_problems(d, _a_browser(), samples=["img/s.png"])
        self.assertEqual([x for x in got if x[2] == "unlabelled"], [])
        # Under the one rulebook (2026-10-26) the tag itself must change: tracked capitals.
        self.assertEqual({x[2] for x in got}, {"look:caps"})

    def test_paul_is_told_why(self):
        with tempfile.TemporaryDirectory() as d:
            _page(d, STYLE + self.CARD + ".card img{position:relative;z-index:1}",
                  '<div class="card"><img src="img/s.png" alt="a"><span class="tag">Sample</span></div>')
            [(_p, _s, kind, why)] = paul.image_problems(d, _a_browser(), samples=["img/s.png"])
        self.assertEqual((kind, why), ("unlabelled", paul.SAMPLE_WHY["behind"]))

    def test_only_sample_photos_need_it(self):
        self.assertEqual(self.bare('<div class="card"><img src="img/s.png" alt="a"></div>', samples=()), [])


class RoyalEdgeAfterManCave(SiteRules):
    """The rules a reader can check without a browser."""

    def test_a_sample_is_stock_and_the_pitch_says_so(self):
        t = target(photos=[{"file": "img/hero.jpg", "source": HERO_PAGE, "kind": "sample"}])
        self.assertEqual(self.errors(t), [])
        t["photos"][0]["source"] = "https://www.amazon.com/pomade.jpg"
        self.assertTrue(any("Wikimedia Commons only" in e for e in self.errors(t)))
        t["photos"][0].update(source=HERO_PAGE)
        self.assertTrue(any("product photos are samples" in e for e in paul.check_draft(DRAFT, t)[0]))
        said = DRAFT.replace("The photos are placeholders", "The product photos are samples and the rest placeholders")
        self.assertFalse([e for e in paul.check_draft(said, t)[0] if "sample" in e])

    def test_a_video_is_refused(self):
        errs = self.errors(index=INDEX.replace("<main>", '<main><video src="img/hero.jpg" autoplay muted></video>'))
        self.assertTrue(any("no video or sound" in e for e in errs), errs)

    def test_a_menu_link_needs_its_section(self):
        index = INDEX.replace("<main>", '<main><nav><a href="#fades">Fades</a><a href="#top">Top</a>'
                                        '<a href="#beards">Beards</a></nav><p id="beards">Beard trims</p>')
        errs = self.errors(index=index)
        self.assertEqual([e for e in errs if "goes nowhere" in e],
                         ['index.html links to "#fades" but nothing on that page has id="fades" - the link goes nowhere'])
        self.assertEqual(self.errors(index=index.replace('<p id="beards">', '<p id="fades"></p><p id="beards">')), [])


class TheOwnerCanSeeWhyABuildWasRefused(PaulCase):
    """Owner, 2026-10-21, of Royal Edge's rebuild refused twice: "How should
    we fix this?" `paul.py measure` runs the browser checks on the build set
    aside, with Paul's own words, and wakes no model."""

    def test_it_measures_the_newest_build_set_aside(self):
        for name, age in (("2026-10-20-old-1", 100), ("2026-10-21-royal-edge-2", 0)):
            d = paul.A("sites", "_below-bar", name)
            (d / "site").mkdir(parents=True)
            (d / "site" / "index.html").write_text("<h1>x</h1>")
            paul.write_json(d / "target.json", target(name=name))
            os.utime(d, (time.time() - age, time.time() - age))
        saved = (paul.find_browser, paul.measured_contrast, paul.measured_phone, paul.measured_images)
        self.addCleanup(lambda: [setattr(paul, k, v) for k, v in zip(
            ("find_browser", "measured_contrast", "measured_phone", "measured_images"), saved)])
        paul.find_browser = lambda: "chrome"
        paul.measured_contrast = lambda *a, **k: []
        paul.measured_images = lambda *a, **k: []
        paul.measured_phone = lambda *a, **k: {"wraps": [], "wide": True, "hidden": [], "styled": 1,
                                               "over": [('<div class="dd mega">', 540)]}
        rc, out = self.run_quiet(paul.cmd_measure, self.args(folder=None))
        self.assertEqual(rc, 1)
        self.assertIn("2026-10-21-royal-edge-2", out)
        self.assertIn('<div class="dd mega"> reaches 540px on a 390px screen', out)


class ASiteSentBackWithANewLook(PaulCase):
    """"I have a new sample site for Paul to use to make Royal Edge's website."
    Send back took only notes, and lost the prospect on the way: no saved
    photos were placed in the revision, and the reference was gone."""

    def setUp(self):
        super().setUp()
        TheOwnerPicksPaulBuilds.seed(self, dict(prospect(name="Royal Edge"), status="built",
                                                slug="royal-edge-concept-abc123", source="owner",
                                                reference="https://rudysbarbershop.com/"))
        TheOwnerSendsASiteBack.built(self, name="Royal Edge", slug="royal-edge-concept-abc123")
        paul.add_photo(1, JPEG, "facebook")

    def test_the_new_reference_and_the_photos_reach_the_cycle(self):
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who="royal-edge-concept-abc123", notes=["no", "video"],
                                                            ref="https://mancaveformen.com/?utm_source=x"))
        self.assertEqual(rc, 0, out)
        self.assertIn("built after https://mancaveformen.com/", out)
        self.assertEqual(paul.read_json(paul.A("work", "prospect.json"))["reference"], "https://mancaveformen.com/")
        self.assertEqual(paul.load_prospects()[0]["reference"], "https://mancaveformen.com/")
        self.assertIn("new reference site for the look: https://mancaveformen.com/", paul.A("work", "REVISE.md").read_text())
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("OWNER'S REF  https://mancaveformen.com/", facts)
        self.assertIn("1 of THEIR OWN photos", facts)
        self.assertTrue(paul.A("work", "site", "img", "yours-01.jpg").is_file())
        self.assertNotIn("nothing about it is", facts, "a business being revised was checked when it was built")

    def test_without_one_the_old_reference_still_applies(self):
        self.run_quiet(paul.cmd_revise, self.args(who="Royal Edge", notes=["fix", "it"], ref=None))
        self.assertEqual(paul.own_reference(), "https://rudysbarbershop.com/")

    def test_not_a_link_is_refused(self):
        rc, out = self.run_quiet(paul.cmd_revise, self.args(who="Royal Edge", notes=["x"], ref="mancave"))
        self.assertEqual(rc, 1)
        self.assertFalse(paul.A("work", "REVISE.md").exists())

    def test_a_revision_that_does_not_ship_leaves_the_site_built(self):
        paul.close_prospect(1, {"outcome": "below-bar", "day": "2026-10-22", "why": "x"})
        self.assertEqual(paul.load_prospects()[0]["status"], "built")
        paul.close_prospect(1, {"outcome": "built", "day": "2026-10-22", "slug": "royal-edge-concept-new123", "mode": "live"})
        self.assertEqual(paul.load_prospects()[0]["slug"], "royal-edge-concept-new123")

    def test_the_studio_has_the_box(self):
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        self.assertIn('data-ref="s:${esc(b.slug)}"', studio)
        self.assertIn("ref ? { notes, ref } : { notes }", studio)
        deck = (ROOT / "mission-control-api" / "paul.js").read_text()
        self.assertIn("['revise', slug, ...(ref ? ['--ref', ref] : []), '--', notes]", deck)


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
class PhotosThatFitBesideTheText(unittest.TestCase):
    """Royal Edge's About (owner, 2026-10-21, on the iPad): two tall phone
    photos stacked beside two paragraphs - "all over stretched and look
    bad. They also create a bunch of blank space". The owner chose a row of
    photos under the text."""

    TEXT = ('<div class="about-text"><h2>Royal Edge on Sycamore Street</h2><p>A barbershop at 621 Sycamore St '
            'in Waterloo, Iowa.</p><p>Skin fades, beard trims, kids cuts and designs.</p></div>')
    BASE = "body{margin:0;font:18px/1.6 sans-serif}.wrap{max-width:1100px;margin:0 auto;padding:0 20px}"

    def measure(self, css, body, size=(590, 1280)):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d) / "site"
            (site / "img").mkdir(parents=True)
            for n in (1, 2, 3):
                (site / "img" / f"yours-0{n}.png").write_bytes(_png(*size))
            (site / "style.css").write_text(self.BASE + css)
            (site / "index.html").write_text('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                                             '<link rel="stylesheet" href="style.css"></head><body>' + body + "</body></html>")
            return paul.measured_images(site, _a_browser(), Path(d) / "memo.json")

    def test_the_about_as_it_was_is_caught(self):
        got = self.measure(".about{display:grid;grid-template-columns:1fr 1fr;gap:40px}"
                           ".about-photos img{width:100%;display:block;margin-bottom:20px}",
                           '<section class="wrap about">' + self.TEXT + '<div class="about-photos">'
                           '<img src="img/yours-01.png" alt="a"><img src="img/yours-02.png" alt="b"></div></section>')
        self.assertEqual(sorted({k for _p, _s, k, _d in got}), ["enlarged", "gap"])
        [gap] = [d for _p, _s, k, d in got if k == "gap"]
        self.assertIn('<div class="about-text"> ends', gap)
        self.assertIn('above <div class="about-photos"> beside it', gap)

    def test_a_row_under_the_text_passes(self):
        self.assertEqual(self.measure(
            ".row{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.row img{width:100%;aspect-ratio:1/1;"
            "object-fit:cover;display:block}@media(max-width:700px){.row{grid-template-columns:1fr}}",
            '<section class="wrap">' + self.TEXT + '<div class="row"><img src="img/yours-01.png" alt="a">'
            '<img src="img/yours-02.png" alt="b"><img src="img/yours-03.png" alt="c"></div></section>'), [])

    def test_text_that_stays_beside_the_photos_is_not_a_gap(self):
        got = self.measure(".about{display:grid;grid-template-columns:1fr 1fr;gap:40px}.about-text{position:sticky;top:0}"
                           ".about-photos img{width:300px;display:block;margin-bottom:20px}",
                           '<section class="wrap about">' + self.TEXT + '<div class="about-photos">'
                           '<img src="img/yours-01.png" alt="a"><img src="img/yours-02.png" alt="b"></div></section>',
                           size=(600, 1300))
        self.assertEqual(got, [])

    def test_a_photo_too_wide_on_an_upright_ipad_is_caught_there(self):
        # One column under 900px, as Royal Edge's CSS has: at 1280 the photo
        # is 300px, on an upright iPad the whole width.
        got = self.measure(".pic{width:300px}@media(max-width:899px){.pic{width:100%}}.pic{display:block}",
                           '<section class="wrap"><img class="pic" src="img/yours-01.png" alt="a"></section>',
                           size=(700, 700))
        [(_p, src, kind, detail)] = got
        self.assertEqual((src, kind), ("img/yours-01.png", "enlarged"))
        self.assertIn("on an iPad (820px)", detail)

    def test_the_quality_bar_has_no_gap(self):
        # Its dotted price leader is an empty span: read by its content, it
        # "ended" at the top of the page, 1671px above the price beside it.
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(ROOT / "agents" / "paul" / "templates" / "local", Path(d) / "s")
            self.assertEqual(paul.measured_images(Path(d) / "s", _a_browser(), Path(d) / "m.json"), [])


class APitchIsWrittenOnceAndCopiedOnAPhone(PaulCase):
    """Owner, 2026-10-21, with four Royal Edge pitches in Drafts: "Paul
    doesn't need to draft a pitch every time we send something back if the
    pitch isn't changing... I need to be able to copy and paste the pitch
    when I'm on my phone." """

    URL = "https://royal-edge-concept-wys8bh.vercel.app"

    def composed(self, dry=False):
        head, body = paul.parse_draft(DRAFT)
        return paul.compose_draft(head, body, SENDER, dry).replace(paul.PREVIEW_URL, self.URL)

    def test_a_sent_pitch_comes_back_as_paul_wrote_it(self):
        for dry in (False, True):
            back = paul.reusable_draft(self.composed(dry))
            self.assertIn(paul.PREVIEW_URL, back)
            self.assertNotIn(self.URL, back)
            self.assertNotIn(paul.OPT_OUT, back, "code adds the opt-out line again")
            self.assertNotIn(SENDER["name"], back.split("\n\n", 1)[1], "and the signature")
            self.assertEqual(paul.check_draft(back, target())[0], [], "Paul's check passes it as it is")
            head, body = paul.parse_draft(back)
            self.assertEqual(paul.compose_draft(head, body, SENDER, False).replace(paul.PREVIEW_URL, self.URL),
                             self.composed(False), "finishing it again gives the same pitch")

    def test_a_phone_script_keeps_every_line(self):
        text = "Channel: phone\nTo: (319) 427-5796\n\nHi, it is here: " + self.URL + ". " + paul.OPT_OUT + "\n"
        self.assertEqual(paul.reusable_draft(text),
                         "Channel: phone\nTo: (319) 427-5796\n\nHi, it is here: [PREVIEW URL]. " + paul.OPT_OUT + "\n")

    def test_sending_back_carries_it_and_says_so(self):
        TheOwnerSendsASiteBack.built(self, name="Royal Edge", slug="royal-edge-concept-wys8bh")
        paul.A("outbox", "royal-edge-concept-wys8bh", "draft.md").write_text(self.composed())
        self.run_quiet(paul.cmd_revise, self.args(who="Royal Edge", notes=["photos", "in", "a", "row"], ref=None))
        self.assertIn(paul.PREVIEW_URL, paul.A("work", "draft.md").read_text())
        self.assertIn("Leave it as it is unless", paul.A("work", "REVISE.md").read_text())
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("Your pitch is carried", header)

    def test_drafts_shows_one_per_business_with_what_to_copy(self):
        for slug, day in (("royal-edge-concept-y8ehkl", "2026-10-20"), ("royal-edge-concept-wys8bh", "2026-10-21")):
            TheOwnerSendsASiteBack.built(self, name="Royal Edge", slug=slug)
            paul.A("outbox", slug, "draft.md").write_text(self.composed().replace(self.URL, f"https://{slug}.vercel.app"))
            paul.log_build({"day": day, "mode": "live", "outcome": "built", "slug": slug, "name": "Royal Edge"})
        _rc, out = self.run_quiet(paul.cmd_deck, self.args())
        builds = json.loads(out)["builds"]
        self.assertEqual([(b["slug"], b["current"]) for b in builds],
                         [("royal-edge-concept-wys8bh", True), ("royal-edge-concept-y8ehkl", False)])
        pitch = builds[0]["pitch"]
        self.assertEqual((pitch["to"], pitch["subject"]), ("rosa@rosas.test", "A website for Rosa's Tailoring"))
        self.assertTrue(pitch["message"].startswith("Hi Rosa,"))
        self.assertIn("https://royal-edge-concept-wys8bh.vercel.app", pitch["message"])
        self.assertIn(paul.OPT_OUT, pitch["message"], "what is sent is what code composed")
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        self.assertIn("built.filter(b => b.current && b.draft", studio)
        self.assertIn('data-part="message">Copy message', studio)
        self.assertIn("document.execCommand('copy')", studio, "plain http has no navigator.clipboard")
        self.assertIn("pre{white-space:pre-wrap;overflow-wrap:anywhere;", studio, "a long link wraps on a phone")


class IsentItAndTheOlderPreviewsComeDown(PaulCase):
    """Owner, 2026-10-21: an "I sent it" button instead of typing paul.py
    mark, and a button to take down Royal Edge's older previews - four were
    up, one per revision, and only the newest is in its pitch."""

    SLUGS = ("royal-edge-concept-y8ehkl", "royal-edge-concept-pvgrms", "royal-edge-concept-wys8bh")

    def setUp(self):
        super().setUp()
        for n, slug in enumerate(self.SLUGS):
            TheOwnerSendsASiteBack.built(self, name="Royal Edge", slug=slug)
            paul.log_build({"day": f"2026-10-2{n}", "mode": "live", "outcome": "built", "slug": slug,
                            "name": "Royal Edge"})
            paul.append_row(paul.A("state", "deployments.csv"), paul.DEPLOY_FIELDS,
                            {"slug": slug, "preview_url": f"https://{slug}.vercel.app", "business": "Royal Edge",
                             "deployed": "2026-10-21", "teardown": "2026-11-20", "status": "live"})
        # One row per business: a revision's link replaces the old one.
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"slug": self.SLUGS[-1], "business": "Royal Edge", "status": "drafted"})

    def live(self):
        return [d["slug"] for d in paul.deployments() if d["status"] == "live"]

    def test_the_older_ones_come_down_and_the_newest_stays(self):
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_retire, self.args(who=self.SLUGS[-1]), api=api)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.live(), [self.SLUGS[-1]])
        self.assertEqual(sorted(c.path for c in api.calls if c.method == "DELETE"),
                         sorted(f"/v9/projects/{s}" for s in self.SLUGS[:2]))
        rc, out = self.run_quiet(paul.cmd_retire, self.args(who="Royal Edge"), api=FakeVercel())
        self.assertIn("no older preview up", out)

    def test_not_once_the_pitch_has_gone_out(self):
        self.run_quiet(paul.cmd_mark, self.args(who=self.SLUGS[-1], status="sent"))
        api = FakeVercel()
        rc, out = self.run_quiet(paul.cmd_retire, self.args(who="Royal Edge"), api=api)
        self.assertEqual(rc, 1)
        self.assertIn("may be an older one", out)
        self.assertEqual(len(self.live()), 3)
        self.assertFalse([c for c in api.calls if c.method == "DELETE"])

    def test_the_studio_shows_it(self):
        self.run_quiet(paul.cmd_retire, self.args(who="Royal Edge"), api=FakeVercel())
        paul.append_row(paul.A("state", "deployments.csv"), paul.DEPLOY_FIELDS,
                        {"slug": "x", "preview_url": "https://x.vercel.app", "business": "Royal Edge", "status": "live"})
        _rc, out = self.run_quiet(paul.cmd_deck, self.args())
        cards = {b["slug"]: b for b in json.loads(out)["builds"]}
        newest, oldest = cards[self.SLUGS[-1]], cards[self.SLUGS[0]]
        self.assertEqual((newest["current"], newest["older_live"]), (True, 1))
        self.assertEqual((oldest["current"], oldest["older_live"], oldest["preview_url"]), (False, 0, None),
                         "a preview taken down has no link")
        self.assertEqual(newest["preview_url"], f"https://{self.SLUGS[-1]}.vercel.app")
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        for s in ('data-retire="${esc(b.slug)}"', 'data-sent="${esc(b.slug)}"', "OLDER VERSION"):
            self.assertIn(s, studio)

    def test_the_buttons_through_the_deck(self):
        node = shutil.which("node")
        api = ROOT / "mission-control-api"
        if not (node and (api / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        js = (f"const express=require({json.dumps(str(api / 'node_modules' / 'express'))});"
              f"const paul=require({json.dumps(str(api / 'paul.js'))});"
              "const http=require('http');const app=express();app.use(express.json());paul.register(app);"
              "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
              "const go=p=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:'POST'},"
              "r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>ok([r.statusCode,s]));});q.end();});"
              f"const out={{bad:await go('/api/paul/sent/..%2Fx'),retire:await go('/api/paul/retire/{self.SLUGS[-1]}'),"
              f"sent:await go('/api/paul/sent/{self.SLUGS[-1]}')}};"
              "process.stdout.write(JSON.stringify(out));srv.close();});")
        root = self.root
        (root / "scripts").symlink_to(ROOT / "scripts")
        # No VERCEL_TOKEN here: retire answers, and takes nothing down.
        r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["bad"][0], 400)
        self.assertIn("no VERCEL_TOKEN", out["retire"][1])
        self.assertEqual(out["sent"][0], 200, out["sent"])
        self.assertEqual([r["status"] for r in paul.contacted()], ["sent"])


class ABusinessDeletedByMistakeIsBuiltAgain(PaulCase):
    """Owner, 2026-10-22: Delete on an older Royal Edge card took every site
    of it, and its previews - "can we recover what I had?" No backups; the
    owner chose to rebuild it with every edit made since. Delete had kept it
    in contacted.csv so Paul would never pick it, and left the photos the
    owner saved under its old prospect number."""

    NOTES = "No video. A row of your photos under About us. Sample on each product photo."

    def setUp(self):
        super().setUp()
        TheOwnerPicksPaulBuilds.seed(self, dict(prospect(name="Waiting Bakery", phone="(512) 444-0100"), status="waiting"),
                                     dict(prospect(name="Royal Edge"), status="built", slug="royal-edge-concept-pvgrms",
                                          source="owner", reference="https://rudysbarbershop.com/"),
                                     dict(prospect(name="Later Cafe", phone="(512) 444-0101"), status="waiting"))
        paul.add_photo(2, JPEG, "facebook")
        TheOwnerSendsASiteBack.built(self, name="Royal Edge", slug="royal-edge-concept-pvgrms")
        paul.log_build({"day": "2026-10-21", "mode": "live", "outcome": "built", "slug": "royal-edge-concept-pvgrms",
                        "name": "Royal Edge"})
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"date": "2026-10-21", "slug": "royal-edge-concept-pvgrms", "business": "Royal Edge",
                         "status": "drafted"})
        self.run_quiet(paul.cmd_remove, self.args(who="Royal Edge", yes=True), api=FakeVercel())

    def restore(self, **kw):
        a = dict(name="Royal Edge", ref="https://mancaveformen.com", where="Waterloo, IA", notes=self.NOTES,
                 photos=None, now=False)
        a.update(kw)
        return self.run_quiet(paul.cmd_restore, self.args(**a))

    def test_it_comes_back_with_its_photos_its_look_and_the_owners_edits(self):
        self.assertEqual([p["name"] for p in paul.load_prospects()], ["Waiting Bakery", "Later Cafe"],
                         "Delete took the card")
        rc, out = self.restore()
        self.assertEqual(rc, 0, out)
        self.assertIn("back as prospect 2, with the 1 photo(s) you saved", out)
        p = [p for p in paul.load_prospects() if p["name"] == "Royal Edge"][0]
        self.assertEqual((p["id"], p["status"], p["reference"], p["owner_notes"]),
                         (2, "approved", "https://mancaveformen.com", self.NOTES))
        self.assertFalse([r for r in paul.contacted() if r["business"] == "Royal Edge"],
                         "the row for a pitch never sent, to a preview that is gone, goes")
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertIn("BUILD: the owner approved Royal Edge", facts)
        self.assertIn("OWNER WANTS  No video. A row of your photos under About us. Sample on each", facts)
        self.assertIn("OWNER'S REF  https://mancaveformen.com", facts)
        self.assertIn("1 of THEIR OWN photos", facts)
        self.assertFalse(paul.already_pitched(target(name="Royal Edge", phone="(319) 427-5796")),
                         "his check would refuse it as already pitched")

    def test_not_when_the_pitch_went_out(self):
        rows = paul.contacted()
        rows[0]["status"] = "sent"
        paul.write_rows(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS, rows)
        rc, out = self.restore()
        self.assertEqual(rc, 1)
        self.assertIn("a pitch went out", out)
        self.assertEqual([p["name"] for p in paul.load_prospects()], ["Waiting Bakery", "Later Cafe"])

    def test_not_while_it_still_has_a_site(self):
        TheOwnerSendsASiteBack.built(self, name="Royal Edge", slug="royal-edge-concept-new123")
        paul.log_build({"day": "2026-10-22", "mode": "live", "outcome": "built", "slug": "royal-edge-concept-new123",
                        "name": "Royal Edge"})
        rc, out = self.restore()
        self.assertEqual(rc, 1)
        self.assertIn("still has a site", out)

    def test_photos_that_belong_to_a_waiting_prospect_are_not_taken(self):
        rc, out = self.restore(photos=1)
        self.assertEqual(rc, 1)
        self.assertIn("belongs to another prospect", out)



class DeleteThisVersionOnly(PaulCase):
    """Owner, 2026-10-22: Delete on an older Royal Edge card took every site
    of it, and its previews. "I want delete this version, never delete the
    whole business." """

    SLUGS = ("royal-edge-concept-y8ehkl", "royal-edge-concept-ltg0xp", "royal-edge-concept-pvgrms")

    def setUp(self):
        super().setUp()
        TheOwnerPicksPaulBuilds.seed(self, dict(prospect(name="Royal Edge"), status="built", slug=self.SLUGS[-1]))
        for n, slug in enumerate(self.SLUGS):
            TheOwnerSendsASiteBack.built(self, name="Royal Edge", slug=slug)
            paul.log_build({"day": f"2026-10-2{n}", "mode": "live", "outcome": "built", "slug": slug, "name": "Royal Edge"})
            paul.append_row(paul.A("state", "deployments.csv"), paul.DEPLOY_FIELDS,
                            {"slug": slug, "preview_url": f"https://{slug}.vercel.app", "business": "Royal Edge",
                             "deployed": "2026-10-21", "teardown": "2026-11-20", "status": "live"})
        paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                        {"slug": self.SLUGS[-1], "business": "Royal Edge", "status": "drafted",
                         "preview_url": f"https://{self.SLUGS[-1]}.vercel.app"})

    def drop(self, slug, api=None):
        return self.run_quiet(paul.cmd_drop, self.args(slug=slug), api=api or FakeVercel())

    def test_an_older_version_goes_and_the_rest_stay(self):
        api = FakeVercel()
        rc, out = self.drop(self.SLUGS[0], api)
        self.assertEqual(rc, 0, out)
        self.assertEqual([b["slug"] for b in paul.versions("Royal Edge")], list(self.SLUGS[1:]))
        self.assertEqual([c.path for c in api.calls if c.method == "DELETE"], [f"/v9/projects/{self.SLUGS[0]}"])
        for slug in self.SLUGS[1:]:
            self.assertTrue((paul.A("sites", slug) / "index.html").is_file())
            self.assertTrue(paul.A("outbox", slug, "draft.md").is_file())
        self.assertEqual([p["name"] for p in paul.load_prospects()], ["Royal Edge"], "the business stays")
        self.assertEqual(paul.contacted()[0]["slug"], self.SLUGS[-1])

    def test_never_the_last_one(self):
        self.drop(self.SLUGS[0])
        self.drop(self.SLUGS[1])
        rc, out = self.drop(self.SLUGS[2])
        self.assertEqual(rc, 1)
        self.assertIn("only version", out)
        self.assertTrue((paul.A("sites", self.SLUGS[2]) / "index.html").is_file())

    def test_never_the_one_whose_link_went_out(self):
        self.run_quiet(paul.cmd_mark, self.args(who=self.SLUGS[-1], status="sent"))
        rc, out = self.drop(self.SLUGS[-1])
        self.assertEqual(rc, 1)
        self.assertIn("whose link you sent", out)

    def test_an_unsent_pitch_moves_to_the_newest_left(self):
        rc, out = self.drop(self.SLUGS[-1])
        self.assertEqual(rc, 0, out)
        row = paul.contacted()[0]
        self.assertEqual((row["slug"], row["preview_url"]), (self.SLUGS[1], f"https://{self.SLUGS[1]}.vercel.app"))

    def test_undelete_within_30_days_and_gone_after(self):
        self.drop(self.SLUGS[0])
        rc, out = self.run_quiet(paul.cmd_undelete, self.args(slug=self.SLUGS[0]))
        self.assertEqual(rc, 0, out)
        self.assertEqual([b["slug"] for b in paul.versions("Royal Edge")], list(self.SLUGS), "back in its place")
        self.assertTrue(paul.A("outbox", self.SLUGS[0], "draft.md").is_file())
        self.drop(self.SLUGS[0])
        self.run_quiet(paul.purge_deleted, day=paul.day_plus(paul.today(), 30))
        self.assertTrue(paul.deleted_dir(self.SLUGS[0]).is_dir(), "kept for 30 days")
        self.run_quiet(paul.purge_deleted, day=paul.day_plus(paul.today(), 31))
        self.assertFalse(paul.deleted_dir(self.SLUGS[0]).exists())

    def test_the_studio_offers_it_only_beside_another_version(self):
        _rc, out = self.run_quiet(paul.cmd_deck, self.args())
        self.assertEqual({b["versions"] for b in json.loads(out)["builds"]}, {3})
        studio = (ROOT / "mission-control-api" / "public" / "paul.html").read_text()
        self.assertIn("${b.versions > 1 ? `<button class=\"btn del\" data-drop=", studio)
        self.assertNotIn("data-remove", studio)


# Commons' API answer for one file (formatversion=2), in the shape its docs
# give (mediawiki.org API:Imageinfo, extmetadata). The cloud session's
# address is rate-limited by Wikimedia (429); on the droplet, 2026-10-26,
# `stock find barber shop chair` read seven real answers through this parser
# (titles, sizes, licences, authors) - and showed the two bugs tested below.
def commons_page(title="File:Old barber shop interior.jpg", lic="CC BY-SA 4.0", artist='<a href="//x">Ann Lee</a>',
                 credit="Own work", mime="image/jpeg", width=4000):
    name = title.removeprefix("File:").replace(" ", "_")
    return {"pageid": 1, "ns": 6, "title": title, "imagerepository": "local",
            "imageinfo": [{"size": 900000, "width": width, "height": 3000, "mime": mime,
                           "url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{name}",
                           "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
                           "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{name}/1280px-{name}",
                           "thumbwidth": 1280, "thumbheight": 960,
                           "extmetadata": {"Artist": {"value": artist, "source": "commons-desc-page"},
                                           "LicenseShortName": {"value": lic, "source": "commons-desc-page"},
                                           "Credit": {"value": credit, "source": "commons-desc-page"},
                                           "ImageDescription": {"value": "<p>A barber shop</p>", "source": "commons-desc-page"}}}]}


class StockIsWikimediaCommonsCreditedByCode(PaulCase):
    """Owner, 2026-10-26: "Switch to Wikipedia." Paul fetched Unsplash and
    Pexels photos with scripts, which both sites' terms bar. Commons files
    are openly licensed; `stock get` records whose each one is, and code
    writes the credit - title, author, source and licence - on every page."""

    JPEG = b"\xff\xd8\xff\xe0" + b"0" * 3000

    def fake(self, pages=None, status=200):
        calls = []

        def get(url, binary=False, **_k):
            calls.append(url)
            if binary:
                return 200, self.JPEG
            return status, json.dumps({"batchcomplete": True, "query": {"pages": pages or []}})
        return get, calls

    def test_the_library_is_there_and_searchable(self):
        lib = paul.library()
        self.assertGreater(len(lib), 700)
        self.assertTrue(all(paul.FREE_LICENSE.match(p["license"]) for p in lib), "every one under a licence we may use")
        hits = paul.library_find(["bakery", "bread"])
        self.assertTrue(hits and all("bake" in (p["subject"] + " ".join(p["tags"])).lower() or "bread" in p["subject"]
                                     for p in hits[:3]), [p["subject"] for p in hits[:3]])

    def test_library_words_match_whole_and_most_of_them(self):
        # The droplet's first search: "shop" was found inside "workshop",
        # and a glassblower answered "barber shop chair".
        self.assertEqual(paul.library_find(["barber", "shop", "chair"]), [])
        self.assertTrue(all(re.search(r"\bchairs?\b", p["subject"] + " " + " ".join(p["tags"]))
                            for p in paul.library_find(["wooden", "chair"])))

    def test_an_unknown_author_is_said_once(self):
        # Commons' template renders it twice, the second hidden: the droplet
        # printed "Unknown author Unknown author" for Draper's Barber Shop.
        page = commons_page(artist='Unknown author<span style="display:none">Unknown author</span>', lic="Public domain")
        self.assertEqual(paul.commons_info([page])[0]["author"], "Unknown author")

    def test_a_library_photo_comes_at_a_standard_width_with_its_credit(self):
        p = paul.library()[0]
        get, calls = self.fake()
        self.work()
        rc, out = self.run_quiet(paul.cmd_stock, self.args(action="get", which="File:" + p["id"], width=1100), get=get)
        self.assertEqual(rc, 0, out)
        name = p["file"].rsplit("/", 1)[1]
        # The shape of the 1280px thumbnail downloaded for real on 2026-10-26.
        self.assertEqual(calls, [p["file"].replace("/commons/", "/commons/thumb/") + f"/1280px-{name}"],
                         "no API call for a library photo, and the 1280 step for an 1100px need")
        credits = json.loads(paul.A("work", "site", paul.CREDITS).read_text())
        [(rel, c)] = [(k, v) for k, v in credits.items() if k.startswith("img/stock-")]
        self.assertEqual((c["page"], c["author"], c["license"]), (p["page"], p["author"], p["license"]))
        self.assertEqual(paul.A("work", "site", rel).read_bytes(), self.JPEG)
        self.assertIn(f'"file": "{rel}", "source": "{p["page"]}", "kind": "stock"', out)

    def test_a_commons_photo_is_checked_before_it_is_taken(self):
        self.work()
        for page, why in ((commons_page(lic="CC BY-NC 4.0"), "non-commercial"),
                          (commons_page(credit="https://unsplash.com/photos/abc"), "a stock site's"),
                          (commons_page(artist=""), "no author to credit"),
                          (commons_page(mime="image/tiff"), "not a JPEG or PNG")):
            get, calls = self.fake([page])
            rc, out = self.run_quiet(paul.cmd_stock, self.args(action="get", which="File:Old barber shop interior.jpg",
                                                              width=1280), get=get)
            self.assertEqual(rc, 1, why)
            self.assertFalse([c for c in calls if "upload.wikimedia" in c], f"downloaded {why}")
        get, calls = self.fake([commons_page()])
        rc, out = self.run_quiet(paul.cmd_stock, self.args(action="get", which="https://commons.wikimedia.org/wiki/"
                                                          "File:Old_barber_shop_interior.jpg", width=1280), get=get)
        self.assertEqual(rc, 0, out)
        self.assertIn("1280px-Old_barber_shop_interior.jpg", calls[-1])
        c = json.loads(paul.A("work", "site", paul.CREDITS).read_text())["img/stock-old-barber-shop-interior.jpg"]
        self.assertEqual((c["author"], c["license_url"]), ("Ann Lee", "https://creativecommons.org/licenses/by-sa/4.0/"))

    def test_find_lists_both_and_says_when_commons_cannot_answer(self):
        get, _ = self.fake([commons_page()])
        rc, out = self.run_quiet(paul.cmd_stock, self.args(action="find", words=["bakery"], limit=3), get=get)
        self.assertIn("  lib  File:", out)
        self.assertIn("  web  File:Old barber shop interior.jpg", out)
        get, _ = self.fake(status=429)
        _rc, out = self.run_quiet(paul.cmd_stock, self.args(action="find", words=["bakery"], limit=3), get=get)
        self.assertIn("Commons answered 429", out)

    def test_the_credit_is_on_every_page_and_comes_off_for_a_revision(self):
        w = self.work()
        site = w / "site"
        (site / "about.html").write_text(INDEX)
        paul.apply_safeguards(site, "Rosa's Tailoring", "Sam Studio")
        for page in ("index.html", "about.html"):
            markup = (site / page).read_text()
            self.assertIn(f'<p id="{paul.CREDITS_ID}"', markup)
            self.assertIn(f'<a href="{HERO_PAGE}">Tailor at work.jpg</a> by Some Photographer, '
                          f'<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>', markup)
        self.assertNotIn("Photos from", paul.scan_site(site)["text"], "code's own words are not checked as Paul's copy")
        paul.strip_safeguards(site)
        self.assertNotIn(paul.CREDITS_ID, (site / "index.html").read_text())

    def test_a_photo_the_site_no_longer_shows_is_not_credited(self):
        site = self.work(index=INDEX.replace("img/hero.jpg", "img/other.jpg")) / "site"
        (site / "img" / "other.jpg").write_bytes(self.JPEG)
        self.assertIsNone(paul.credits_line(site))


# One rulebook (owner, 2026-10-26: Hallmark's best parts and Paul's, "one
# rulebook"). A clean local-business page, and each tell put into it.
LOOK_CSS = (":root{--ink:#1c1a17;--paper:#f6f2ea;--accent:#7a3b12;--line:#d9d0c2;--serif:Georgia,serif;--sans:Arial,sans-serif}"
            "html,body{overflow-x:clip}body{margin:0;background:var(--paper);color:var(--ink);font:18px/1.6 var(--sans)}"
            "header{display:flex;justify-content:space-between;align-items:center;padding:16px 32px}nav a{margin-left:20px;color:var(--ink)}"
            ".wrap{max-width:1100px;margin:0 auto;padding:0 24px}h1{font:700 56px/1.05 var(--serif);margin:24px 0 12px}"
            "h2{font:700 40px/1.1 var(--serif)}.btn{display:inline-block;background:var(--accent);color:var(--paper);padding:12px 20px;"
            "border-radius:6px;text-decoration:none;white-space:nowrap}a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}")
LOOK_BODY = ('<header><a class="brand" href="#top">Royal Edge</a><nav><a href="#services">Services</a><a href="#visit">Visit</a></nav></header>'
             '<main class="wrap"><section><h1>Fades, beards and designs in Waterloo</h1><p>A barbershop on Sycamore Street, open six days '
             'a week.</p><a class="btn" href="tel:+13194275796">Call (319) 427-5796</a></section><section id="services"><h2>Services</h2>'
             '<ul><li>Skin fade $25</li><li>Beard trim $15</li><li>Kids cut $18</li></ul></section><section id="visit"><h2>Visit</h2>'
             '<address>621 Sycamore St, Waterloo, IA</address><p>Book on Booksy or walk in.</p></section></main>')


@unittest.skipUnless(_a_browser(), "needs a headless Chrome")
class OneRulebookInTheBrowser(unittest.TestCase):

    def look(self, css="", body=LOOK_BODY, base=LOOK_CSS):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "style.css").write_text(base + css)
            (d / "index.html").write_text('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                                          '<link rel="stylesheet" href="style.css"></head><body>' + body + "</body></html>")
            return {k[5:]: det for _p, _s, k, det in paul.measured_images(d, _a_browser(), d / "m.json") or []
                    if k.startswith("look:")}

    def test_a_clean_page_passes(self):
        self.assertEqual(self.look(), {})

    def test_the_eyebrow_and_tracked_capitals_from_royal_edge(self):
        # "ABOUT US" and "621 SYCAMORE ST, WATERLOO": Paul's own template.
        got = self.look(".label{font-size:12px;letter-spacing:.14em;text-transform:uppercase}",
                        LOOK_BODY.replace("<h1>", '<p class="label">Barbershop · Waterloo · Iowa</p><h1>'))
        self.assertEqual(set(got), {"eyebrow", "caps", "dots"})
        self.assertIn('"BARBERSHOP · WATERLOO · IOWA" above "Fades, beards', got["eyebrow"])
        for css in ("nav a{text-transform:uppercase;font-size:14px}", "nav a{letter-spacing:.1em;font-size:14px}"):
            self.assertEqual(set(self.look(css)), {"caps"}, css)

    def test_an_arrow_on_a_button_and_one_word_set_apart(self):
        got = self.look("h1 em{color:var(--accent)}", LOOK_BODY.replace("Call (319) 427-5796", "Book now →")
                        .replace("designs in", "<em>designs</em> in"))
        self.assertEqual(set(got), {"arrow", "accent"})

    def test_a_removed_focus_ring_and_the_browsers_own(self):
        no_ring = LOOK_CSS.replace("a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}", "")
        self.assertEqual(self.look(base=no_ring), {}, "the browser's own outline is a focus ring")
        self.assertEqual(set(self.look("a:focus{outline:none}", base=no_ring)), {"focus"})

    def test_the_hero_on_a_laptop_screen(self):
        self.assertIn("the headline, its first sentence, the main button", self.look("main>section:first-child{padding-top:700px}")["fold"])
        header_only = LOOK_BODY.replace('<a class="btn" href="tel:+13194275796">Call (319) 427-5796</a></section>', "</section>") \
            .replace('<nav>', '<nav><a class="btn" href="tel:+13194275796">Call</a>')
        self.assertEqual(self.look(body=header_only), {}, "the header's call button is the main button")

    def test_words_per_screen_count_sentences_not_lists(self):
        long = "We cut hair with care and attention every single day of the week. " * 14
        self.assertIn("words of sentences", self.look(body=LOOK_BODY.replace("<p>Book on Booksy or walk in.</p>", f"<p>{long}</p>"))["words"])
        menu = "".join(f"<li><p>Cut number {n} with a hot towel and a line up, ${n + 10}</p></li>" for n in range(30))
        self.assertEqual(self.look(body=LOOK_BODY.replace("<ul>", "<ul>" + menu)), {}, "a price list is not prose")

    def test_gradients_blobs_nested_cards_drawings_and_empty_boxes(self):
        got = self.look(".g{background:linear-gradient(90deg,#7b2ff7,#2f80ed);height:50px}.blob{width:300px;height:300px;"
                        "filter:blur(40px);background:var(--accent)}.c{border:1px solid var(--line);border-radius:8px;padding:16px;"
                        "min-height:80px}", LOOK_BODY.replace("</main>", '<div class="g">x</div><div class="blob"></div>'
                                                                          '<div class="c">a<div class="c">b</div></div>'
                                                                          '<svg width="300" height="300"><circle cx="50" cy="50" r="40"/>'
                                                                          '<circle cx="90" cy="90" r="40"/><path d="M0 0L90 90"/></svg></main>'))
        self.assertEqual(set(got), {"purple", "blob", "nest", "empty", "drawing"})

    def test_a_price_said_once_and_said_everywhere(self):
        echo = LOOK_BODY.replace("</main>", "".join(f"<section><p>Cuts from $25 {n}</p></section>" for n in range(3)) + "</main>")
        self.assertIn("$25 in 4 sections", self.look(body=echo)["echo"])

    def test_buttons_fit_and_nothing_sticks_out_on_an_ipad_too(self):
        got = self.look(".btn{white-space:normal;width:90px}.wide{width:1000px;height:9px}", LOOK_BODY.replace("</main>", '<div class="wide"></div></main>'),
                        base=LOOK_CSS.replace("html,body{overflow-x:clip}", ""))
        self.assertIn("on an iPad (820px)", got["wide"], "820px is where it sticks out")
        self.assertIn("Call (319) 427-5796", got["wrap"])

    def test_the_quality_bar_passes_every_rule(self):
        # Its six labels in tracked capitals and its 640px drawing of a cup
        # behind the hero went (2026-10-26): the rulebook's first catch.
        with tempfile.TemporaryDirectory() as d:
            s = Path(d) / "s"
            shutil.copytree(ROOT / "agents" / "paul" / "templates" / "local", s)
            self.assertEqual(paul.measured_images(s, _a_browser(), Path(d) / "m.json"), [])
            self.assertEqual(paul.code_rules(paul.scan_site(s)["css"]), [])


class OneRulebookInTheCode(unittest.TestCase):

    def test_colours_and_faces_live_once(self):
        self.assertEqual(paul.code_rules([LOOK_CSS]), [])
        self.assertEqual(paul.code_rules(["@font-face{font-family:'Grenze';src:url(g.woff2)}"
                                          ".x{background:url('data:image/svg+xml,%23fff#a');white-space:nowrap}"]), [])
        for css in (".x{color:#fff}", ".x{box-shadow:0 1px 2px rgba(0,0,0,.2)}", ".x{border:1px solid white}",
                    ".x{font-family:Georgia,serif}", ".x{font:16px/1.5 'Inter',sans-serif}", ".x{background:oklch(90% .02 80)}"):
            self.assertTrue(any("written in place" in e for e in paul.code_rules([css])), css)

    def test_motion_has_a_calm_version(self):
        moving = "@keyframes up{from{transform:translateY(8px)}}.x{animation:up .4s}"
        self.assertTrue(any("calm version" in e for e in paul.code_rules([moving])))
        self.assertEqual(paul.code_rules([moving + "@media (prefers-reduced-motion: reduce){.x{animation:none}}"]), [])

    def test_nothing_waits_for_a_script_to_be_seen(self):
        self.assertTrue(any("until a script" in e for e in paul.code_rules([".reveal{opacity:0}"])))
        self.assertEqual(paul.code_rules([".js .reveal{opacity:0}.reveal.is-visible{opacity:1}"]), [])

    def test_stock_actions_and_buzzwords(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "index.html").write_text(INDEX.replace("Directions</a>", "Directions</a> <a href='#'>Learn more</a> "
                                                              "World-class fades."))
            errs = paul.look_and_law(paul.scan_site(d))
        self.assertTrue(any('"Learn more" as a button' in e for e in errs), errs)
        self.assertTrue(any("world-class" in e for e in errs), errs)


class AThemeIsDealtWhenThereIsNoReference(PaulCase):
    """Hallmark's deal (owner, 2026-10-26): three themes that fit the kind of
    business, none from the last three builds; the owner's reference, when
    there is one, is the look instead."""

    def setUp(self):
        super().setUp()
        TheOwnerPicksPaulBuilds.seed(self, dict(prospect(kind="salon"), status="approved", decided="x"))

    def test_three_for_the_kind_none_used_lately(self):
        for n, theme in enumerate(("loom", "sport", "darkroom")):
            paul.log_build({"day": f"2026-10-1{n}", "outcome": "built", "slug": f"s{n}", "name": "x", "theme": theme})
        for _ in range(20):
            got = paul.deal_themes("salon")
            self.assertEqual(len(set(got)), 3)
            self.assertFalse({"loom", "sport", "darkroom"} & set(got), got)
            self.assertTrue(set(got) <= {"brutal", "vellum", "overprint"} | set(paul.THEMES_FOR["other"]), got)
        for kind, themes in paul.THEMES_FOR.items():
            for t in themes:
                self.assertTrue((paul.HALLMARK / "themes" / f"{t}.md").is_file() and
                                (paul.HALLMARK / "themes" / f"{t}.jpg").is_file(), t)

    def test_the_brief_deals_once_and_check_holds_paul_to_it(self):
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        dealt = paul.read_json(paul.A("work", "themes.json"))["themes"]
        self.assertIn(f"THEMES       no reference from the owner: build in one of {', '.join(dealt)}.", facts)
        _rc, again = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertEqual(paul.read_json(paul.A("work", "themes.json"))["themes"], dealt, "a resumed build keeps its deal")
        self.work(t=target(theme="cobalt"))
        paul.write_json(paul.A("work", "themes.json"), {"themes": dealt})
        errs, _n, _i = paul.run_checks(target(theme="cobalt"), offline)
        self.assertTrue(any('"theme" must be the one you built in' in e for e in errs), errs)
        errs, _n, _i = paul.run_checks(target(theme=dealt[0]), offline)
        self.assertFalse([e for e in errs if '"theme"' in e])

    def test_the_owners_reference_is_the_look(self):
        rows = paul.load_prospects()
        rows[0]["reference"] = "https://mancaveformen.com/"
        paul.save_prospects(rows)
        _rc, facts = self.run_quiet(paul.cmd_brief, self.args(dry=True), get=offline)
        self.assertNotIn("THEMES", facts)
        self.assertFalse(paul.A("work", "themes.json").exists())
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        for bit in ("**A theme, when the owner gave no reference**", "**Build around what the visitor came to do**",
                    "**One rulebook**"):
            self.assertIn(bit, header)


class AFreshReviewOncePerBuild(PaulCase):
    """Hallmark's finish review (owner, 2026-10-26: "4 is fine do it"): when
    every check passes, a model that did not build the page reads its
    screenshots, once. Code still judges what it can measure."""

    def setUp(self):
        super().setUp()
        self.work()
        shots = paul.A("work", "shots")
        shots.mkdir(parents=True, exist_ok=True)
        for n, _w, _h in paul.SHOTS:
            (shots / f"{n}.png").write_bytes(_png(10, 10))
        saved = paul.fresh_shots
        self.addCleanup(setattr, paul, "fresh_shots", saved)
        paul.fresh_shots = lambda *_a, **_k: [(n, shots / f"{n}.png") for n, _w, _h in paul.SHOTS]
        self.asked = []

    def ask(self, answer, ok=True):
        def ask(agent, question, timeout=150, tools=(), files=()):
            self.asked.append((agent, question, tools, [Path(f).name for f in files]))
            return ok, answer
        return ask

    def test_fix_is_shown_once_and_uses_no_pass(self):
        a = self.args(ask=self.ask("fix\n1. The headline would fit any barbershop - name the pharaoh windows."))
        rc, out = self.run_quiet(paul.cmd_check, a, get=offline)
        self.assertEqual(rc, 1)
        self.assertIn("The headline would fit any barbershop", out)
        self.assertEqual(paul.load_cycle()["passes"], 0, "the review round is not a failed pass")
        (agent, q, tools, files) = self.asked[0]
        self.assertEqual((agent, tools, files), ("paul", ("Read",), ["mobile.png", "desktop.png", "index.html"]))
        self.assertIn("Rosa's Tailoring", q)
        rc, out = self.run_quiet(paul.cmd_check, a, get=offline)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.asked), 1, "one round, never a second")
        self.assertIn("CHECK PASSED", out)

    def test_ship_passes_and_a_review_that_cannot_run_never_blocks(self):
        rc, out = self.run_quiet(paul.cmd_check, self.args(ask=self.ask("ship")), get=offline)
        self.assertEqual(rc, 0, out)
        self.assertIn("REVIEW: ship", out)
        paul.A("work", paul.REVIEW).unlink()
        rc, out = self.run_quiet(paul.cmd_check, self.args(ask=self.ask("the plan is busy", ok=False)), get=offline)
        self.assertEqual(rc, 0, out)
        self.assertIn("REVIEW: not run (the plan is busy)", out)

    def test_the_reviewer_hears_what_the_owner_asked(self):
        paul.write_json(paul.A("work", "prospect.json"), {"owner_notes": "No video. A row of photos under About us."})
        self.run_quiet(paul.cmd_check, self.args(ask=self.ask("ship")), get=offline)
        self.assertIn("The owner asked: No video. A row of photos under About us.", self.asked[0][1])

    def test_it_reaches_the_report(self):
        self.assertEqual(paul.review_lines({"first": "fix", "review": "fix\n1. a thing"}),
                         ["", "## Fresh review", "", "- fix", "  1. a thing"])


class WakeAskCanBeHandedFiles(unittest.TestCase):
    """Paul's review reads screenshots; `wake.py ask` runs in an empty folder,
    so the files are copied into it by name."""

    def test_the_files_are_there_when_the_model_runs(self):
        spec = importlib.util.spec_from_file_location("wake_t", ROOT / "scripts" / "wake.py")
        wake = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wake)
        wake.setting = lambda agent, doc=None: {"engine": "claude", "model": "sonnet", "max_turns": 10}
        wake.claude_bin, wake.plan_token = (lambda: "/bin/claude"), (lambda: "tok")
        seen = {}

        def run(cmd, cwd=None, **_k):
            seen.update(files=sorted(os.listdir(cwd)), cmd=cmd)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"result": "ship"}), stderr="")
        with tempfile.TemporaryDirectory() as d:
            shot = Path(d) / "mobile.png"
            shot.write_bytes(b"png")
            ok, text = wake.ask("paul", "review it", run=run, tools=("Read",), files=[str(shot)])
        self.assertEqual((ok, text), (True, "ship"))
        self.assertIn("mobile.png", seen["files"])
        self.assertIn("Read", seen["cmd"][seen["cmd"].index("--tools") + 1])
