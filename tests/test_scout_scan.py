"""Run a scan from Scout's house (owner, 2026-10-16: "Can you add a run scan
option inside scouts house"). scout.js starts the unit the 06:30 timer
starts, never a second one while it runs, and reads back its state.

    python3 -m unittest tests.test_scout_scan -b

systemctl is stood in for by a script on PATH that records each call and
plays a oneshot unit: "activating" from start until it is told to stop.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "mission-control-api"
SCRIPTS = ROOT / "scripts"
NODE = shutil.which("node")


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(phrase, supply, reviews, selling, established=None, tags=()):
    """A measured phrase as market-scan.py saves it."""
    r = {"phrase": phrase, "supply": supply, "heat": 0.05, "pull": 0.01, "price": 20.0, "match": 0.9,
         "returned": 25, "heat_n": 25, "pull_n": 25, "score": 0.02, "tags": list(tags),
         "reviews": reviews, "selling": selling, "sales_n": 10, "sales_days": 365}
    if established is not None:
        r["established"] = established
    return r


@unittest.skipUnless(NODE and (API / "node_modules" / "express").is_dir(), "node or the Deck's modules missing")
class TheScanButton(unittest.TestCase):

    def test_start_once_and_say_so(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "bin").mkdir()
            calls, flag = d / "calls", d / "running"
            fake = d / "bin" / "systemctl"
            fake.write_text(f"""#!/bin/sh
echo "$*" >> {calls}
case "$1" in
  is-active) if [ -f {flag} ]; then echo activating; else echo inactive; exit 3; fi ;;
  start) touch {flag} ;;
esac
""")
            fake.chmod(0o755)
            scans = d / "agents" / "scout" / "state" / "scans"
            scans.mkdir(parents=True)
            (scans / "s.json").write_text("{}")
            (scans.parent / "last-scan.json").write_text(json.dumps({"scanned": [], "new_niches": ["x mug"]}))
            js = (
                f"const express=require({json.dumps(str(API / 'node_modules' / 'express'))});"
                f"const scout=require({json.dumps(str(API / 'scout.js'))});"
                "const http=require('http');const app=express();app.use(express.json());scout.register(app,0);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const go=(m,p)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:m},r=>{"
                "let s='';r.on('data',c=>s+=c);r.on('end',()=>ok([r.statusCode,JSON.parse(s)]));});q.end();});"
                "const out={idle:await go('GET','/api/scout/scan'),start:await go('POST','/api/scout/scan'),"
                "busy:await go('POST','/api/scout/scan'),state:await go('GET','/api/scout/scan')};"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, ECOSYSTEM_ROOT=str(d), PATH=f"{d / 'bin'}:{os.environ['PATH']}"))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["idle"][0], 200)
            self.assertFalse(out["idle"][1]["running"])
            self.assertTrue(out["idle"][1]["last_scan_utc"], "the newest scan file's time")
            self.assertEqual(out["idle"][1]["result"]["new_niches"], ["x mug"], "what the last run found")
            self.assertEqual((out["start"][0], out["start"][1]["running"]), (202, True))
            self.assertEqual(out["busy"][0], 409, "a second press while it runs starts nothing")
            self.assertTrue(out["state"][1]["running"])
            starts = [l for l in calls.read_text().splitlines() if l.startswith("start")]
            self.assertEqual(starts, ["start --no-block scout-niches.service"], "the timer's own unit, once")

    def test_the_house_has_the_button(self):
        page = (API / "public" / "village.html").read_text()
        self.assertIn('<div id="scoutScan"></div>', page)
        self.assertIn("fetch('/api/scout/scan', { method: 'POST' })", page)
        self.assertIn("ExecStart=/usr/bin/python3 /root/ecosystem/scripts/niche-scan.py run",
                      (ROOT / "deploy" / "scout-niches.service").read_text(), "the button runs what the timer runs")


class TheScanAlwaysMeasuresSomething(unittest.TestCase):
    """2026-10-28, the owner: "I recently ran a scan that returned no
    results." A run where every niche was under a week old measured nothing,
    and the list stopped growing at 80 niches, dead ones included - so it
    went round the same markets. Now: new niches first, the oldest re-measured
    early when nothing is due, a niche with nothing selling twice retired to
    make room, and what the run did written for the Deck."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.m = load("niche_scan_t", "niche-scan.py")
        state = self.tmp / "state"
        self.m.NICHES, self.m.SCANS, self.m.LAST = state / "niches.json", state / "scans", state / "last-scan.json"
        self.m.SCANS.mkdir(parents=True)
        self.m.STARTER = ["frog sticker", "dog mom mug"]
        self.m.gaps = lambda bps=None: []
        self.m.log = lambda msg: None
        self.selling = {}                       # seed -> its rows sell?
        self.tags = ["cute frog lover"]         # what the selling listings tag
        self.scanned = []

        def scan(seed):
            self.scanned.append(seed)
            sells = self.selling.get(seed, True)
            doc = {"seed": seed, "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "rows": [row(seed, 5000, 30 if sells else 0, 6 if sells else 0, tags=self.tags)]}
            (self.m.SCANS / f"{self.m.slug(seed)}.json").write_text(json.dumps(doc))
            return 0, ["saved"]
        self.m.scan = scan

    def run_scan(self, n=2, force=False):
        self.scanned.clear()
        return self.m.cmd_run(type("A", (), {"n": n, "force": force})())

    def test_a_run_with_nothing_due_still_measures_the_oldest(self):
        self.tags = []                          # nothing new to find either
        self.run_scan()
        first = list(self.scanned)
        self.run_scan()
        self.assertTrue(self.scanned, "the second press measured something")
        last = json.loads(self.m.LAST.read_text())
        self.assertIn("re-measured the oldest early", last["note"])
        self.assertEqual(len(last["scanned"]), len(self.scanned))
        self.assertTrue(first)

    def test_new_niches_from_selling_tags_are_found_and_reported(self):
        self.run_scan()
        last = json.loads(self.m.LAST.read_text())
        self.assertTrue(last["new_niches"], "the selling tag became a niche")
        self.assertEqual(last["scanned"][0]["best"]["sold"], 6)

    def test_a_niche_selling_nothing_twice_is_retired_and_never_comes_back(self):
        self.selling["dog mom mug"] = False
        self.run_scan()
        self.run_scan(n=5, force=True)
        d = self.m.load()
        self.assertIn("dog mom mug", d.get("retired") or {})
        self.assertNotIn("dog mom mug", [s for s, _src, _age in self.m.niches(d)])
        self.assertIn("dog mom mug", json.loads(self.m.LAST.read_text())["retired"])
        # A selling listing elsewhere tags it: still not brought back.
        (self.m.SCANS / "mugs.json").write_text(json.dumps({"seed": "funny mug", "rows": [
            row("funny mug", 900, 20, 5, tags=["dog mom mug"])]}))
        self.assertNotIn("dog mom mug", self.m.discover(d), "not re-added from a tag")

    def test_one_dead_scan_is_not_enough(self):
        self.selling["dog mom mug"] = False
        self.run_scan()
        self.assertNotIn("dog mom mug", self.m.load().get("retired") or {})

    def test_the_house_shows_what_the_run_did(self):
        page = (API / "public" / "village.html").read_text()
        self.assertIn("${scanResult(s.result)}", page)
        self.assertIn("readFileSync(LAST, 'utf8')", (API / "scout.js").read_text())


class TheOpening(unittest.TestCase):
    """Owner, 2026-10-28: "maximize traffic to my store". Scout ranked by
    money - reviews x price - and went into the biggest markets, where a new
    shop is not seen. It now ranks by opening: sales per decade of
    competition, less where page one is held by shops with years of reviews,
    more for a specific phrase."""

    def setUp(self):
        self.m = load("scout_ideas_open", "scout-ideas.py")

    def test_an_open_specific_market_beats_a_big_held_one(self):
        big = row("funny dad shirt", 300000, 377, 9, established=9)
        open_ = row("dad joke fishing shirt", 6000, 40, 6, established=1)
        self.assertGreater(self.m.opening("dad joke fishing shirt", open_), self.m.opening("funny dad shirt", big))

    def test_page_one_held_by_big_shops_counts_against_a_market(self):
        r = row("frog sticker", 80000, 120, 8)
        self.assertGreater(self.m.opening("frog sticker", dict(r, established=0)),
                           self.m.opening("frog sticker", dict(r, established=8)))
        self.assertEqual(self.m.opening("frog sticker", r), self.m.opening("frog sticker", dict(r, established=0)),
                         "an old scan that does not say is not counted against it")

    def test_nothing_sold_is_no_opening(self):
        self.assertEqual(self.m.opening("x sticker", row("x sticker", 100, 0, 0)), 0.0)

    def test_the_scan_records_how_held_page_one_is(self):
        ms = load("market_scan_held", "market-scan.py")
        now = 1_800_000_000
        reply = lambda ever: ({"count": ever, "results": [{"create_timestamp": now - 86400}]}, {}, None)
        got = ms.sales("k", [1, 2], now=now, call=lambda p, k: reply(80 if "/1/" in p else 3))
        self.assertEqual(got["established"], 1, "one of two top listings has 50+ reviews ever")


class YourShopShapesTheIdeas(unittest.TestCase):
    """Owner, 2026-10-28: Scout never looked at the shop it was proposing for.
    On 2026-10-27, 20 of its 35 live listings were pattern totes. A product or
    design style that is already a big share of the shop is held: more of it
    is refused while other markets are open, and one design of a style a run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        scans = self.tmp / "agents" / "scout" / "state" / "scans"
        store = self.tmp / "agents" / "emily" / "state" / "store"
        scans.mkdir(parents=True)
        store.mkdir(parents=True)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for seed, rows in {"tote": [row("library tote bag", 9000, 30, 5, 2, tags=["book bag", "reading tote"])],
                           "shirt": [row("dad joke fishing shirt", 6000, 40, 6, 1)],
                           "sticker": [row("judging frog sticker", 3000, 25, 5, 0), row("frog sticker", 80000, 120, 8, 7)]}.items():
            (scans / f"{seed}.json").write_text(json.dumps({"seed": seed, "scanned_at": now, "rows": rows, "excluded": []}))
        listings = ([{"listing_id": i, "title": f"Geometric Pattern Tote Bag {i}", "views": 3, "favs": 0} for i in range(20)]
                    + [{"listing_id": 100 + i, "title": f"Cute Cat Sticker {i}", "views": 1, "favs": 0} for i in range(9)]
                    + [{"listing_id": 200 + i, "title": f"Funny Mom Mug {i}", "views": 2, "favs": 0} for i in range(6)])
        (store / "2026-10-28.json").write_text(json.dumps({"day": "2026-10-28", "listings": listings}))
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.tmp))
        self.drafts = self.tmp / "agents" / "scout" / "state" / "drafts.txt"

    def ideas(self, *cmd):
        return subprocess.run(["python3", str(SCRIPTS / "scout-ideas.py"), *cmd], capture_output=True,
                              text=True, env=self.env, timeout=120)

    def test_the_brief_shows_the_shop_and_what_is_held(self):
        out = self.ideas("brief").stdout
        self.assertIn("YOUR SHOP (Etsy, 2026-10-28): 35 live listings", out)
        self.assertIn("tote: 20 listings, 3.0 views each", out)
        self.assertIn("HELD: tote - 20 of the shop's 35 live listings already are", out)
        self.assertIn("HELD: pattern", out)
        self.assertIn("top sellers tag it:", out)
        self.assertLess(out.index("judging frog sticker"), out.index("frog sticker  ("), "best opening first")

    def test_more_of_a_held_product_or_style_is_refused_and_the_rest_filed(self):
        self.drafts.write_text("library tote bag | Quiet Reader Tote | tote | library regulars\n"
                               "dad joke fishing shirt | Reel Dad Jokes | tshirt | lettering for lake dads\n"
                               "judging frog sticker | Unimpressed Frog | sticker | sarcastic frog\n"
                               "frog sticker | Frog Plaid Pattern | sticker | repeating plaid frogs\n")
        r = self.ideas("intake")
        self.assertIn("2 filed, 2 refused", r.stdout)
        self.assertIn("the shop has enough tote", r.stderr)
        self.assertIn("a pattern design", r.stderr)

    def test_a_focus_is_the_owner_choosing_and_is_not_held_back(self):
        self.ideas("focus", "tote")
        self.drafts.write_text("library tote bag | Quiet Reader Tote | tote | library regulars\n")
        self.assertIn("1 filed", self.ideas("intake").stdout)

    def test_one_design_of_a_style_a_run(self):
        m = load("scout_ideas_style", "scout-ideas.py")
        self.assertEqual(m.style_of("Retro Gingham Tote"), "pattern")
        self.assertIsNone(m.style_of("Unimpressed Frog"))


class EmilyTitlesTheSearch(unittest.TestCase):
    """Owner, 2026-10-28: traffic. Scout's idea names the phrase shoppers type;
    a listing that never says it cannot be found for it. Emily's title must
    start with it and it must be a tag - checked by emily-listing.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.d = self.tmp / "reel-dad"
        self.d.mkdir()
        self.m = load("emily_listing_search", "emily-listing.py")

    def problems(self, title, tags, phrase="judging frog sticker"):
        (self.d / "evidence.json").write_text(json.dumps({"phrase": phrase}))
        return self.m.search_problems(self.d, {"title": title, "tags": tags})

    def test_the_title_starts_with_the_phrase_and_it_is_a_tag(self):
        self.assertEqual(self.problems("Judging Frog Stickers | Unimpressed Frog", ["judging frog sticker"]), [],
                         "a plural is the same search")
        said = self.problems("Unimpressed Frog | Sarcastic Sticker for Laptops", ["funny frog"])
        self.assertEqual(len(said), 2)
        self.assertIn("must start with 'judging frog sticker'", said[0])

    def test_a_phrase_too_long_for_a_tag_is_only_asked_of_the_title(self):
        self.assertEqual(self.problems("Dad Joke Fishing Shirt | Reel Dad", ["fishing dad"],
                                       phrase="dad joke fishing shirt"), [])

    def test_a_build_with_no_evidence_is_not_checked(self):
        self.assertEqual(self.m.search_problems(self.d, {"title": "Anything", "tags": []}), [])

    def test_the_command_refuses_before_writing(self):
        src = (SCRIPTS / "emily-listing.py").read_text()
        self.assertIn("stitch_problems(listing) + search_problems(d, listing)", src)


class ScoutThinksOnSonnet(unittest.TestCase):
    def test_engines(self):
        e = json.loads((ROOT / "tasks" / "engines.json").read_text())["agents"]["scout"]
        self.assertEqual((e["engine"], e["model"]), ("claude", "sonnet"))


if __name__ == "__main__":
    unittest.main()
