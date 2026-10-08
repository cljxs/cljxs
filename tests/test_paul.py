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
import shutil
import sys
import tempfile
import time
import types
import unittest
import urllib.parse
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

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rosa's Tailoring</title><link rel="stylesheet" href="style.css">
<link rel="icon" href="img/icon.svg"></head><body>
<header class="hero" style="background-image:url('img/hero.jpg')">
<h1>Rosa's Tailoring</h1><p>Hems, suits and wedding dresses, by hand since 1998.</p>
<a class="cta" href="tel:+15124440199">Call (512) 444-0199</a></header>
<main><p>1801 E 7th St, Austin, TX 78702</p><p>Tue-Sat 10am-6pm</p>
<a href="https://maps.google.com/?q=1801+E+7th+St">Directions</a></main>
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
monthly fees. The photos are placeholders until you send me yours.
"""


def target(**over):
    t = {"status": "built", "name": "Rosa's Tailoring", "category": "tailor", "kind": "trade",
         "address": "1801 E 7th St, Austin, TX 78702", "phone": "(512) 444-0199",
         "hours": "Tue-Sat 10am-6pm", "current_site": "none",
         "problems": [{"code": "no-site", "evidence": "search and the Maps listing show none"}],
         "channel": "email", "contact": "rosa@rosas.test",
         "sources": {k: "https://maps.google.com/?cid=1"
                     for k in ("name", "address", "phone", "hours", "contact")},
         "photos": [{"file": "img/hero.jpg", "source": "https://images.unsplash.com/photo-1",
                     "kind": "stock"}],
         "claims": [{"text": "by hand since 1998", "source": "https://maps.google.com/?cid=1"}],
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
        (w / "site" / "style.css").write_text("/* Paul's site template (test fixture) */ body{margin:0}")
        (w / "site" / "img" / "hero.jpg").write_bytes(b"\xff\xd8" + b"0" * 5000)
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

    def test_stock_only_from_unsplash_or_pexels(self):
        t = target(photos=[{"file": "img/hero.jpg", "source": "https://images.example/x.jpg",
                            "kind": "stock"}])
        self.assertTrue(any("Unsplash or Pexels" in e for e in self.errors(t=t)))

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

    def test_live_waits_for_go_live(self):
        self.assertIn("go-live", paul.hold_reason(dry=False))

    def test_five_unsent_pitches_hold(self):
        paul.write_json(paul.A("state", "live.json"), {"since": "2026-10-01"})
        paul.find_browser = lambda: "/usr/bin/true"
        for n in range(paul.MAX_WAITING):
            paul.append_row(paul.A("state", "contacted.csv"), paul.CONTACTED_FIELDS,
                            {"slug": f"s{n}", "business": f"B{n}", "status": "drafted"})
        self.assertIn("waiting for you", paul.hold_reason(dry=False))

    def test_too_little_budget_holds(self):
        paul.budget_left = lambda: 0.10
        self.assertIn("allowance", paul.hold_reason(dry=True))

    def test_a_hold_is_one_memory_line_not_one_a_day(self):
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
        self.has(self.errors(t=target(claims=[])), '"since 1998" is a claim')

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
              "ADDRESS_LINE1": street, "ADDRESS_LINE2": rest, "TOWN": "Austin, Texas"}
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


class TheCafeTemplate(unittest.TestCase):
    """The owner's chosen look for restaurants and cafes (2026-10-08): light,
    warm, a mark that draws itself once, rows that scroll sideways. Paul fills
    it rather than designing from a blank page - so the template itself must
    pass every rule Paul is held to, or every cafe site starts out refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / "site"
        self.t = target(name="Hollow Oak Coffee", category="cafe", kind="food", photos=[], claims=[])

    def test_a_filled_template_passes_every_site_check(self):
        fill_template(self.site, self.t)
        errs, info = paul.check_site(self.site, self.t)
        self.assertEqual(errs, [])
        self.assertEqual(info["pages"], 1)

    def test_a_slot_left_unfilled_is_refused_by_name(self):
        fill_template(self.site, self.t)
        page = self.site / "index.html"
        page.write_text(page.read_text().replace('alt="Coffee and pastries"', 'alt="{{HERO_ALT}}"', 1))
        errs, _ = paul.check_site(self.site, self.t)
        self.assertTrue(any("template slots left unfilled: {{HERO_ALT}}" in e for e in errs), errs)

    def test_the_template_as_shipped_is_refused(self):
        shutil.copytree(TEMPLATE, self.site)
        errs, _ = paul.check_site(self.site, self.t)
        self.assertTrue(any("template slots left unfilled" in e for e in errs), errs)

    def test_its_notes_to_paul_never_reach_a_visitor(self):
        fill_template(self.site, self.t)
        self.assertIn("target.json", (self.site / "index.html").read_text(), "the template does carry them")
        paul.apply_safeguards(self.site, self.t["name"], "Sam Studio")
        html = (self.site / "index.html").read_text()
        self.assertNotIn("<!--", html)
        self.assertNotIn("target.json", html)
        self.assertIn('class="intro"', html, "only the comments went")

    def test_the_header_sends_food_businesses_to_it_and_names_marks_that_exist(self):
        header = (ROOT / "agents" / "paul" / "_paul-agents-header.md").read_text()
        self.assertIn("cp -r templates/local/. work/site/", header)
        for mark in ("cup", "fork-knife", "whisk", "wheat", "bowl", "pizza", "scissors", "barber-pole",
                     "comb", "wrench", "hammer", "house", "leaf", "paw", "car", "bag"):
            self.assertIn(mark, header)
            self.assertTrue((TEMPLATE / "img" / "marks" / f"{mark}.svg").is_file(), mark)
        self.assertTrue((TEMPLATE / "img" / "icon.svg").is_file(), "a favicon is required")

    def test_the_intro_never_plays_for_people_who_ask_for_less_motion(self):
        css = (TEMPLATE / "style.css").read_text()
        reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1].split("}", 1)[0]
        self.assertIn(".intro { display: none;", reduced)
        self.assertIn(".seen .intro { display: none; }", css, "once per visit")


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

    def test_a_site_designed_from_blank_is_refused_whatever_the_kind(self):
        site = self.root / "blank"
        site.mkdir()
        (site / "index.html").write_text(INDEX)
        (site / "style.css").write_text("body{margin:0}")
        (site / "img").mkdir()
        (site / "img" / "icon.svg").write_text("<svg/>")
        (site / "img" / "hero.jpg").write_bytes(b"\xff\xd8" + b"0" * 5000)
        for kind in ("trade", "food"):
            errs, _ = paul.check_site(site, target(kind=kind))
            self.assertTrue(any("cp -r templates/local/. work/site/" in e for e in errs), (kind, errs))
