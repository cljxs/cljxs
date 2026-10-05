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
          "address": "PO Box 12, Austin, TX 78701"}

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rosa's Tailoring</title><link rel="stylesheet" href="style.css"></head><body>
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

If you like it, I can put it live under your own name for a flat fee.
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
