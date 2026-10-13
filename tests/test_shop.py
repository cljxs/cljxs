"""The Shop: tiktok-kit.py (a ready-to-post TikTok kit a day) and
store-board.py (every build's money and concerns, to approve, revise or
kill), and the page and API that show them (owner, 2026-10-13).

    python3 -m unittest tests.test_shop -b

The kit tests that render need ffmpeg and skip without it; the API tests need
node and the Deck's node_modules and skip without them.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
API = ROOT / "mission-control-api"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def still(d, name, size="900x900", color="teal"):
    p = Path(d) / name
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:s={size}", "-frames:v", "1", str(p)], check=True)
    return p


ROW = {"listing_id": 4412345678, "title": "Vintage Fern Tote Bag | Botanical Herbarium Canvas Tote",
       "url": "https://www.etsy.com/listing/4412345678/vintage-fern-tote",
       "tags": ["fern tote bag", "gift for book lovers", "botanical tote", "cottagecore bag",
                "library bag"],
       "description": "A sturdy canvas tote printed with antique fern plates. Holds a week of books.",
       "favs": 4, "views": 80}


# ====================================================================== kits

class KitWords(unittest.TestCase):
    """The hook, caption and hashtags are code's - free, and never saying
    more than the listing does."""

    @classmethod
    def setUpClass(cls):
        cls.tk = load("tiktok_kit_words", "tiktok-kit.py")

    def test_the_product_word_is_emily_printifys(self):
        # One fact, one place: the word comes from product_word(), so a
        # sweatshirt is never read as a shirt here and a sweatshirt there.
        self.assertEqual(self.tk.thing_of("Vintage Fern Tote Bag"), "tote bag")
        self.assertEqual(self.tk.thing_of("Crewneck Sweatshirt for Hikers"), "sweatshirt")
        self.assertEqual(self.tk.thing_of("Kiss-Cut Sticker, Mushroom"), "sticker")
        self.assertEqual(self.tk.thing_of("A Lovely Thing"), "find")

    def test_who_its_a_gift_for_comes_from_the_tags(self):
        self.assertEqual(self.tk.who_of(["fern tote", "gift for book lovers"]), "book lover")
        self.assertEqual(self.tk.who_of(["teacher gift"]), "teacher")
        self.assertEqual(self.tk.who_of(["gifts for the gardener"]), "gardener")
        # "the her in your life" is not a hook
        self.assertIsNone(self.tk.who_of(["gift for her", "tote bag"]))
        self.assertIsNone(self.tk.who_of([]))

    def test_consecutive_kits_never_share_a_hook(self):
        hooks = [self.tk.hook_for(ROW, n) for n in range(len(self.tk.HOOKS))]
        for a, b in zip(hooks, hooks[1:]):
            self.assertNotEqual(a, b)
        self.assertTrue(any("book lover" in h for h in hooks))

    def test_a_hook_needing_a_who_is_skipped_when_the_tags_name_nobody(self):
        row = dict(ROW, tags=["fern tote bag"])
        for n in range(20):
            h = self.tk.hook_for(row, n)
            self.assertNotIn("{", h)
            self.assertNotRegex(h, r"for the\s+in|for a\s*$", h)

    def test_every_hook_fits_the_slide(self):
        # wrap() raises on a hook that would not fit; a longest-case product
        # and gift word must still fit every template.
        row = dict(ROW, title="Phone Case", tags=["gift for coffee lovers"])
        for n in range(len(self.tk.HOOKS)):
            lines = self.tk.wrap(self.tk.hook_for(row, n))
            self.assertTrue(all(len(x) <= self.tk.HOOK_LINE or " " not in x for x in lines))

    def test_wrap_refuses_a_hook_that_does_not_fit(self):
        self.assertEqual(self.tk.wrap("Rate this tote bag 1-10"), ["Rate this tote", "bag 1-10"])
        with self.assertRaises(ValueError):
            self.tk.wrap("word " * 30)

    def test_hashtags_are_a_handful_and_clean(self):
        tags = self.tk.hashtags(["Fern Tote Bag", "gift for book lovers", "fern-tote-bag", "ab",
                                 "botanical", "library bag"])
        self.assertEqual(tags[:2], ["#etsyfinds", "#smallbusiness"])
        self.assertEqual(len(tags), 2 + self.tk.HASHTAGS_FROM_TAGS)
        self.assertEqual(len(set(tags)), len(tags), "a tag that cleans to the same word is one hashtag")
        for t in tags:
            self.assertRegex(t, r"^#[a-z0-9]{3,24}$")

    def test_the_caption_is_the_listings_own_words(self):
        cap = self.tk.caption_for(ROW, "Rate this tote bag 1-10", "VintageLoom Treasures on Etsy")
        self.assertTrue(cap.startswith("Rate this tote bag 1-10"))
        self.assertIn("A sturdy canvas tote printed with antique fern plates.", cap)
        self.assertNotIn("Holds a week", cap, "one sentence, not the description")
        self.assertIn("Link in bio - VintageLoom Treasures on Etsy", cap)
        self.assertIn("#etsyfinds", cap)
        self.assertLessEqual(len(cap), self.tk.CAPTION_MAX)

    def test_the_sound_note_names_the_commercial_library(self):
        # A business account that uses a sound outside it can be muted.
        text = self.tk.audio_for("tote bag")
        self.assertIn("Commercial Music", text)
        self.assertIn("silent on purpose", text)


class WhoIsFeaturedNext(unittest.TestCase):
    """Every live listing gets a turn before any gets a second."""

    @classmethod
    def setUpClass(cls):
        cls.tk = load("tiktok_kit_next", "tiktok-kit.py")

    def rows(self):
        return [{"listing_id": 1, "url": "u1", "favs": 1, "views": 5},
                {"listing_id": 2, "url": "u2", "favs": 9, "views": 1},
                {"listing_id": 3, "url": "u3", "favs": 9, "views": 50},
                {"listing_id": 4, "url": None, "favs": 99, "views": 999}]

    def test_never_featured_first_most_saved_first(self):
        self.assertEqual(self.tk.next_listing(self.rows(), [])["listing_id"], 3)

    def test_then_the_one_featured_longest_ago(self):
        kits = [{"listing_id": 3, "day": "2026-10-10"}, {"listing_id": 2, "day": "2026-10-11"},
                {"listing_id": 1, "day": "2026-10-12"}, {"listing_id": 3, "day": "2026-10-13"}]
        self.assertEqual(self.tk.next_listing(self.rows(), kits)["listing_id"], 2,
                         "3's latest kit is the 13th, not the 10th")

    def test_a_listing_without_a_link_is_never_chosen(self):
        self.assertNotEqual(self.tk.next_listing(self.rows(), [])["listing_id"], 4)
        self.assertIsNone(self.tk.next_listing([{"listing_id": 4, "url": None}], []))
        self.assertIsNone(self.tk.next_listing([], []))


class KitOnDisk(unittest.TestCase):
    """A real kit: slides, video, words and zip, made whole or not at all."""

    @classmethod
    def setUpClass(cls):
        cls.tk = load("tiktok_kit_disk", "tiktok-kit.py")
        cls.lv = cls.tk._lv()

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.kits = self.d / "tiktok"
        p = unittest.mock.patch.object(self.tk, "KITS", self.kits)
        p.start()
        self.addCleanup(p.stop)

    def ff(self):
        if not all(self.lv.ffmpeg()):
            self.skipTest("ffmpeg is not installed here")
        if not self.tk.font():
            self.skipTest("no bold font here")

    def test_a_whole_kit(self):
        self.ff()
        photos = [still(self.d, "a.png", "1200x1200"), still(self.d, "b.png", "900x1200", "orange"),
                  still(self.d, "c.png", "1500x1000", "navy")]
        # Turn 2 is "Tell me this isn't the cutest ..." - an apostrophe, which
        # in ffmpeg's filter syntax ends a quoted string. It goes through a
        # textfile, so the hook renders instead of breaking the command.
        kit = self.tk.build_kit(ROW, photos, "2026-10-13", "VintageLoom Treasures on Etsy", 2)
        self.assertIn("isn't", kit["hook"])
        k = self.kits / kit["id"]
        slides = sorted((k / "slides").iterdir())
        self.assertEqual(len(slides), 3 + 2, "hook + each photo + the shop slide")
        for s in slides:
            _secs, w, h, _a = self.lv.probe(s)
            self.assertEqual((w, h), (1080, 1920), s.name)
        secs, w, h, audio = self.lv.probe(k / "video.mp4")
        self.assertEqual((w, h, audio), (1080, 1920, False))
        self.assertAlmostEqual(secs, kit["video_seconds"], delta=0.25)
        names = set(zipfile.ZipFile(k / f"{kit['id']}.zip").namelist())
        self.assertEqual(names, {f"{kit['id']}/photos/{s.name}" for s in slides}
                         | {f"{kit['id']}/{n}" for n in ("video.mp4", "caption.txt", "audio.txt")})
        saved = json.loads((k / "kit.json").read_text())
        self.assertEqual((saved["status"], saved["listing_id"], saved["slides"]), ("ready", 4412345678, 5))
        self.assertEqual([x["id"] for x in self.tk.read_kits()], [kit["id"]])
        self.assertFalse(list(self.kits.glob(".part-*")))

    def test_a_kit_that_goes_wrong_leaves_nothing(self):
        self.ff()
        photos = [still(self.d, "a.png")]
        with unittest.mock.patch.object(self.tk, "video_problems", return_value=["900x900, not 1080x1920"]):
            with self.assertRaises(RuntimeError):
                self.tk.build_kit(ROW, photos, "2026-10-13", "Shop on Etsy", 0)
        self.assertEqual(list(self.kits.iterdir()), [], "no kit and no .part folder")

    def test_the_slideshow_length_is_held_to_tiktoks_short_range(self):
        self.assertEqual(self.tk.video_seconds(1), self.tk.VIDEO_MIN)
        self.assertEqual(self.tk.video_seconds(5), 12)
        self.assertEqual(self.tk.video_seconds(40), self.tk.VIDEO_MAX)

    def fake(self, kid, day, made, status="ready"):
        k = self.kits / kid
        k.mkdir(parents=True)
        (k / "kit.json").write_text(json.dumps({"day": day, "listing_id": 1, "status": status,
                                                "made_utc": made}))

    def test_posted_and_undo(self):
        self.fake("2026-10-13-1", "2026-10-13", "2026-10-13 14:00:00")
        self.assertEqual(self.tk.set_posted("2026-10-13-1")["status"], "posted")
        self.assertIsNotNone(json.loads((self.kits / "2026-10-13-1" / "kit.json").read_text())["posted_utc"])
        self.assertEqual(self.tk.set_posted("2026-10-13-1", False)["status"], "ready")
        with self.assertRaises(ValueError):
            self.tk.set_posted("../../etc")

    def test_only_the_newest_kits_are_kept(self):
        for n in range(5):
            self.fake(f"2026-10-0{n + 1}-1", f"2026-10-0{n + 1}", f"2026-10-0{n + 1} 14:00:00")
        (self.kits / ".part-2026-10-09-1").mkdir()
        gone = self.tk.prune(keep=3)
        self.assertEqual(sorted(gone), ["2026-10-01-1", "2026-10-02-1"])
        self.assertEqual(sorted(p.name for p in self.kits.iterdir()),
                         ["2026-10-03-1", "2026-10-04-1", "2026-10-05-1"])

    def test_a_second_listing_on_one_day_gets_its_own_folder(self):
        self.fake("2026-10-13-1", "2026-10-13", "x")
        self.assertEqual(self.tk.kit_id("2026-10-13", 1), "2026-10-13-1-2")
        self.assertRegex(self.tk.kit_id("2026-10-13", 1), self.tk.KIT_RE)

    def test_one_a_day_the_timer_does_not_make_a_second(self):
        self.fake("2026-10-13-1", "2026-10-13", "2026-10-13 14:00:00")
        args = type("A", (), {"again": False, "listing": None})()
        with unittest.mock.patch.object(self.tk.et_time, "day", return_value="2026-10-13"), \
             unittest.mock.patch.object(self.tk, "build_kit") as build, \
             unittest.mock.patch.object(self.tk, "_lv", return_value=type("L", (), {"ffmpeg": staticmethod(lambda: ("f", "p"))})()), \
             unittest.mock.patch("sys.stdout"):
            self.assertEqual(self.tk.cmd_make(args), 0)
        build.assert_not_called()


class TheSlideshowIsListingVideosOwn(unittest.TestCase):
    """listing-video.py's zoom-and-crossfade makes both videos; the size is a
    parameter, not a second copy that would drift."""

    def test_tall_and_square_from_one_command(self):
        lv = load("listing_video_size", "listing-video.py")
        tall = lv.command(["a.jpg", "b.jpg"], "o.mp4", 10, size=(1080, 1920))
        graph = tall[tall.index("-filter_complex") + 1]
        self.assertIn("s=1080x1920", graph)
        self.assertIn("scale=2160:3840", graph)
        square = lv.command(["a.jpg", "b.jpg"], "o.mp4", 10)
        self.assertIn("s=1080x1080", square[square.index("-filter_complex") + 1])
        self.assertEqual(square[square.index("-crf") + 1], "20", "Etsy's video is unchanged")

    def test_the_kit_does_not_restate_the_zoom(self):
        src = (SCRIPTS / "tiktok-kit.py").read_text()
        self.assertNotIn("zoompan", src)
        self.assertNotIn("xfade", src)


# ====================================================================== board

class BoardBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sb = load("store_board_test", "store-board.py")
        cls.ep = cls.sb._ep()
        cls.ea = load("emily_assets_test_board", "emily-assets.py")

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.builds = self.d / "builds"
        self.builds.mkdir()
        self.catalog = self.d / "printify-catalog.json"
        for target, name, value in ((self.sb, "BUILDS", self.builds), (self.ep, "CATALOG", self.catalog)):
            p = unittest.mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cat({})

    def cat(self, cat):
        self.catalog.write_text(json.dumps(cat))

    def build(self, slug, listing=None, build=None, art=(1600, 1600), order=None):
        d = self.builds / slug
        d.mkdir()
        if listing is not None:
            (d / "listing.json").write_text(listing if isinstance(listing, str) else json.dumps(listing))
        if build is not None:
            (d / "build.json").write_text(json.dumps(build))
        if order is not None:
            (d / "order.json").write_text(json.dumps(order))
        if art:
            w, h = art
            self.ea.write_png(d / "design.png", w, h, [bytes([20, 90, 30] * w) for _ in range(h)])
        return d

    def card(self, slug):
        return next(c for c in self.sb.board()["cards"] if c["slug"] == slug)


GOOD_LISTING = {
    "title": "Vintage Fern Tote Bag, Botanical Herbarium Canvas Tote for Book Lovers and Gardeners",
    "product_type": "tote",
    "tags": ["fern tote bag", "botanical tote", "herbarium tote bag", "vintage fern bag",
             "canvas tote bag", "book lover tote", "gardener tote bag", "fern print bag",
             "botanical gift", "library tote bag", "cottagecore tote", "plant lover tote",
             "gift for gardener"],
    "description": ("Vintage fern tote bag printed with antique botanical herbarium plates. " * 8),
}
TOTE = {"blueprint_title": "Tote Bag (AOP)", "variant_ids": [1, 2],
        "variant_titles": ['15" x 16"', '18" x 18"'], "costs": {"1": 14.5, "2": 16.2},
        "prices": {"1": 3200, "2": 3600}}


class TheMoney(BoardBase):
    """Per sale, from emily-printify.py's one fee table."""

    def test_the_parts_add_up_to_what_the_buyer_paid(self):
        fees = dict(self.ep.DEFAULT_FEES, ship_cost=4.59, ship_charged=2.0)
        b = self.sb.breakdown(24.0, 9.5, fees)
        self.assertAlmostEqual(b["price"] + b["ship_charged"],
                               b["etsy"] + b["making"] + b["postage"] + b["kept"], delta=0.011)
        self.assertAlmostEqual(b["kept"], self.ep.net_of(24.0, 9.5, fees, 4.59, 2.0), places=2)

    def test_the_sticker_that_loses_money_says_so(self):
        # The numbers in net_of()'s own docstring: Printify's Kiss-Cut at $1.42
        # to print and $4.59 to post, sold at $3.99 with free shipping, loses
        # $2.85 a sale.
        self.cat({"sticker": {"blueprint_title": "Kiss-Cut Stickers", "variant_ids": [9],
                              "costs": {"9": 1.42}}, "_fees": {"ship_cost": 4.59}})
        self.build("cheap-sticker", dict(GOOD_LISTING, product_type="sticker"),
                   {"status": "ready_for_review", "price_low": 3.99, "price_high": 3.99})
        c = self.card("cheap-sticker")
        self.assertEqual(c["verdict"], "LOSS")
        self.assertEqual(c["money"]["worst"]["kept"], -2.85)
        self.assertIn("breaks even at $7.14", c["concerns"][0]["text"])

    def test_catalogue_prices_per_variant_come_first(self):
        rows, source = self.sb.variant_rows(TOTE, {"price_low": 1.0})
        self.assertEqual(source, "catalogue")
        self.assertEqual(rows, [('15" x 16"', 32.0, 14.5), ('18" x 18"', 36.0, 16.2)])

    def test_without_them_printifys_range_low_with_cheapest_high_with_dearest(self):
        entry = dict(TOTE, prices={})
        rows, source = self.sb.variant_rows(entry, {"price_low": 30.0, "price_high": 34.0})
        self.assertEqual(source, "printify")
        self.assertEqual(rows, [("lowest price", 30.0, 14.5), ("highest price", 34.0, 16.2)])
        self.assertEqual(self.sb.variant_rows(entry, {}), ([], None))

    def test_the_worst_variant_is_the_one_shown(self):
        self.cat({"tote": TOTE})
        self.build("fern-tote", GOOD_LISTING, {"status": "ready_for_review"})
        m = self.card("fern-tote")["money"]
        self.assertLessEqual(m["worst"]["kept"], m["best"]["kept"])
        self.assertEqual(m["variants"], 2)


class TheConcerns(BoardBase):

    def levels(self, slug):
        return {(x["level"], x["text"]) for x in self.card(slug)["concerns"]}

    def texts(self, slug):
        return " | ".join(x["text"] for x in self.card(slug)["concerns"])

    def test_a_clean_build_is_good(self):
        self.cat({"tote": TOTE, "_fees": {"ship_cost": 0.0}})
        self.build("fern-tote", GOOD_LISTING, {"status": "ready_for_review"})
        self.assertEqual(self.card("fern-tote")["concerns"], [])
        self.assertEqual(self.card("fern-tote")["verdict"], "GOOD")

    def test_no_cost_recorded_names_the_command(self):
        self.cat({"tote": dict(TOTE, costs={})})
        self.build("fern-tote", GOOD_LISTING, {})
        self.assertIn("emily-printify.py costs --product tote", self.texts("fern-tote"))
        self.assertEqual(self.card("fern-tote")["verdict"], "FIX")

    def test_the_drafters_placeholder_price_is_caught(self):
        # The placeholder is emily-printify's PLACEHOLDER_CENTS, read from it.
        cents = self.ep.PLACEHOLDER_CENTS
        self.cat({"sticker": {"blueprint_title": "Kiss-Cut Stickers", "variant_ids": [9],
                              "costs": {"9": 1.0}}})
        self.build("s", dict(GOOD_LISTING, product_type="sticker"),
                   {"price_low": cents / 100, "price_high": cents / 100})
        self.assertIn(f"${cents / 100:.2f} placeholder", self.texts("s"))

    def test_a_thin_margin_is_a_check(self):
        self.cat({"tote": dict(TOTE, prices={"1": 2000, "2": 2400})})
        self.build("thin", GOOD_LISTING, {})
        self.assertIn("thin: keeps", self.texts("thin"))
        self.assertEqual(self.card("thin")["verdict"], "CHECK")

    def test_art_missing_placeholder_or_soft(self):
        self.cat({"tote": dict(TOTE, print_sizes={"1": [4500, 4800], "2": [5400, 5400]})})
        self.build("none", GOOD_LISTING, {}, art=None)
        self.assertIn("no design.png", self.texts("none"))
        d = self.build("ph", GOOD_LISTING, {}, art=None)
        self.ea.write_png(d / "design.png", 64, 64, [bytes(64 * 3)] * 64, mark=self.ea.PLACEHOLDER_MARK)
        self.assertIn("the art is the placeholder", self.texts("ph"))
        self.build("small", GOOD_LISTING, {}, art=(1024, 1024))
        # 1024 px across Printify's 5400 px (18 in at 300 DPI) is ~57 DPI.
        self.assertIn("about 57 DPI across a 18x18 in print area", self.texts("small"))

    def test_the_listing_audit_is_store_reports_own(self):
        # The board and `store-report.py audit` say the same thing about a
        # listing, because the board calls the audit rather than restating it.
        listing = dict(GOOD_LISTING, tags=["tote"] * 3)
        self.cat({"tote": TOTE})
        self.build("few-tags", listing, {})
        sr = load("store_report_board_test", "store-report.py")
        for kind, msg in sr.audit_listing(listing):
            self.assertIn(({"LIMIT": "FIX"}.get(kind, "CHECK"), msg), self.levels("few-tags"))

    def test_a_broken_listing_json_is_said(self):
        self.cat({"tote": TOTE})
        self.build("broken", '{"title": "x",', {"product": "tote"})
        self.assertIn("listing.json will not parse", self.texts("broken"))

    def test_why_there_is_no_draft_is_shown(self):
        self.cat({"tote": TOTE})
        self.build("blocked", GOOD_LISTING, {"draft_blocked": {"reason": "not drafting: art is a photo"}})
        self.assertIn("no Printify draft: not drafting: art is a photo", self.texts("blocked"))

    def test_worst_first(self):
        self.cat({"tote": TOTE, "sticker": {"blueprint_title": "Kiss-Cut Stickers", "variant_ids": [9],
                                             "costs": {"9": 1.42}}, "_fees": {"ship_cost": 4.59}})
        self.build("a-good", GOOD_LISTING, {})
        self.build("b-live", GOOD_LISTING, {"published": True})
        self.build("c-loss", dict(GOOD_LISTING, product_type="sticker"), {"price_low": 3.99})
        self.build("d-fix", dict(GOOD_LISTING, tags=[]), {})
        order = [c["verdict"] for c in self.sb.board()["cards"]]
        self.assertEqual(order, sorted(order, key=["LOSS", "FIX", "CHECK", "GOOD", "LIVE"].index))
        self.assertEqual(order[0], "LOSS")
        self.assertEqual(order[-1], "LIVE")

    def test_free_postage_is_flagged_once_for_the_board(self):
        self.assertTrue(any("Postage is recorded as $0" in n for n in self.sb.board()["notes"]))
        self.cat({"_fees": {"ship_cost": 4.0}})
        self.assertEqual(self.sb.board()["notes"], [])


class TheActions(BoardBase):

    def test_approve_and_undo(self):
        self.build("fern-tote", GOOD_LISTING, {"status": "ready_for_review", "printify_url": "https://p/1"})
        self.sb.approve("fern-tote")
        c = self.card("fern-tote")
        self.assertIsNotNone(c["approved_utc"])
        self.assertEqual(self.sb.board()["approved"], 1)
        self.sb.approve("fern-tote", undo=True)
        self.assertIsNone(self.card("fern-tote")["approved_utc"])

    def test_approve_never_overwrites_a_build_json_it_cannot_read(self):
        d = self.build("bad", GOOD_LISTING)
        (d / "build.json").write_text("{not json")
        with self.assertRaises(ValueError):
            self.sb.approve("bad")
        self.assertEqual((d / "build.json").read_text(), "{not json")

    def test_names_are_checked(self):
        for bad in ("../x", "Bad Slug", ""):
            with self.assertRaises(ValueError):
                self.sb.approve(bad)
        with self.assertRaises(FileNotFoundError):
            self.sb.approve("no-such-build")

    class Ran:
        def __init__(self, code=0, out="drafted", after=None):
            self.code, self.out, self.after, self.cmd = code, out, after, None

        def __call__(self, cmd):
            self.cmd = cmd
            if self.after:
                self.after()
            return type("R", (), {"returncode": self.code, "stdout": self.out, "stderr": ""})()

    def test_a_note_reaches_the_brief_and_replaces_the_last_one(self):
        d = self.build("fern-tote", GOOD_LISTING, {"approved_utc": "x"},
                       order={"product": "tote", "name": "Fern Tote", "brief": "Antique ferns."})
        run = self.Ran()
        ok, _ = self.sb.revise("fern-tote", "bigger lettering", runner=run)
        self.assertTrue(ok)
        self.assertEqual(run.cmd[-2:], ["--redo", "fern-tote"])
        self.sb.revise("fern-tote", "warmer colours", runner=run)
        brief = json.loads((d / "order.json").read_text())["brief"]
        self.assertEqual(brief, "Antique ferns.\n\nOwner's note: warmer colours")
        b = json.loads((d / "build.json").read_text())
        self.assertNotIn("approved_utc", b, "a build sent back is no longer approved")
        self.assertTrue(b["revision"]["ok"])
        self.assertEqual((d / "revision.log").read_text(), "drafted\n")

    def test_keep_art_is_a_redraft(self):
        self.build("fern-tote", GOOD_LISTING, {})
        run = self.Ran()
        self.sb.revise("fern-tote", keep_art=True, runner=run)
        self.assertEqual(run.cmd[-2:], ["--redraft", "fern-tote"])

    def test_a_refused_draft_is_a_failed_revision_even_at_exit_0(self):
        # emily-finish exits 0 when the drafter refuses and records why on
        # build.json - the outcome is read from there.
        d = self.build("fern-tote", GOOD_LISTING, {})

        def refuse():
            b = json.loads((d / "build.json").read_text())
            b["draft_blocked"] = {"reason": "not drafting: the artwork is wrong"}
            (d / "build.json").write_text(json.dumps(b))
        ok, _ = self.sb.revise("fern-tote", runner=self.Ran(0, "emily-finish: could not", refuse))
        self.assertFalse(ok)
        rev = json.loads((d / "build.json").read_text())["revision"]
        self.assertEqual((rev["ok"], rev["said"]), (False, "not drafting: the artwork is wrong"))

    def test_a_live_listing_is_never_sent_back(self):
        self.build("live", GOOD_LISTING, {"published": True})
        run = self.Ran()
        with self.assertRaises(ValueError):
            self.sb.revise("live", "x", runner=run)
        self.assertIsNone(run.cmd)


# ====================================================================== wiring

class TheShopIsWiredIn(unittest.TestCase):

    def test_the_kit_timer_is_not_emilys_and_runs_after_furys_fetch(self):
        # Emily has no timer (deploy.sh and test_parsers hold that), so the
        # kit's units are tiktok-*. The kit is chosen from today's snapshot,
        # which Fury's run fetches - so the kit runs after it.
        timer = (ROOT / "deploy" / "tiktok-kit.timer").read_text()
        self.assertFalse(list((ROOT / "deploy").glob("emily-*.timer")))
        kit = re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d) America/New_York", timer)
        fury = re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d) America/Chicago",
                         (ROOT / "deploy" / "fury-cycle.timer").read_text())
        self.assertTrue(kit and fury)
        fury_et = (int(fury[1]) + 1) * 60 + int(fury[2])
        self.assertGreater(int(kit[1]) * 60 + int(kit[2]), fury_et)
        self.assertIn("store-report.py fetch", (ROOT / "deploy" / "fury-cycle.service").read_text())
        self.assertIn("scripts/tiktok-kit.py make", (ROOT / "deploy" / "tiktok-kit.service").read_text())

    def test_the_page_is_reachable_from_the_village_and_the_deck(self):
        village = (API / "public" / "village.html").read_text()
        self.assertIn("shop: '/shop?embed=1'", village)
        self.assertIn("'shop-close'", village)
        self.assertIn("openFrame('shop')", village)
        self.assertIn('href="/shop"', (API / "public" / "dashboard.html").read_text())
        server = (API / "server.js").read_text()
        self.assertIn("app.get('/shop'", server)
        self.assertIn("shopRoutes.register(app)", server)
        self.assertIn("type: 'shop-close'", (API / "public" / "shop.html").read_text())

    def test_the_page_works_nothing_out(self):
        # The money is store-board.py's. A page that computed a fee would be
        # a second opinion on what a sale earns.
        page = (API / "public" / "shop.html").read_text()
        for word in ("0.065", "0.03", "transaction_pct", "net_of"):
            self.assertNotIn(word, page)

    def test_kill_is_emilys_existing_remove(self):
        page = (API / "public" / "shop.html").read_text()
        self.assertIn("/api/emily/builds/${enc(d.kill)}", page)
        self.assertNotIn("remove", (API / "shop.js").read_text().split("module.exports")[0]
                         .replace("emily-build.py remove", ""))

    def test_nothing_posts_or_publishes(self):
        for f in (SCRIPTS / "tiktok-kit.py", SCRIPTS / "store-board.py", API / "shop.js"):
            src = f.read_text().lower()
            self.assertNotIn("open-api.tiktok.com", src)
            self.assertNotIn("/publish.json", src)
            self.assertNotIn("publishing_succeeded", src)


class TheShopApi(unittest.TestCase):
    """shop.js against a real root: kits listed with their words, files served
    by name only, Posted written by the script."""

    NODE = shutil.which("node")

    def test_routes(self):
        if not (self.NODE and (API / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").symlink_to(SCRIPTS)
            k = root / "agents" / "emily" / "shop" / "tiktok" / "2026-10-13-42"
            (k / "slides").mkdir(parents=True)
            (k / "slides" / "01.jpg").write_bytes(b"\xff\xd8jpeg")
            (k / "video.mp4").write_bytes(b"v" * 64)
            (k / "caption.txt").write_text("Rate this tote 1-10\n")
            (k / "audio.txt").write_text("cozy\n")
            (k / "2026-10-13-42.zip").write_bytes(b"PK")
            (k / "kit.json").write_text(json.dumps({"day": "2026-10-13", "listing_id": 42,
                                                    "status": "ready", "slides": 1, "made_utc": "x"}))
            (root / "agents" / "emily" / "builds").mkdir(parents=True)
            js = (
                f"const express=require({json.dumps(str(API / 'node_modules' / 'express'))});"
                f"const shop=require({json.dumps(str(API / 'shop.js'))});"
                "const http=require('http');const app=express();app.use(express.json());shop.register(app);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const go=(m,p,b)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:m,"
                "headers:{'Content-Type':'application/json'}},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>"
                "ok([r.statusCode,r.headers['content-type']||'',r.headers['content-disposition']||'',s]));});"
                "q.end(b?JSON.stringify(b):undefined);});"
                "const out={list:await go('GET','/api/shop/tiktok'),"
                "slide:await go('GET','/api/shop/tiktok/2026-10-13-42/slide/01.jpg'),"
                "zip:await go('GET','/api/shop/tiktok/2026-10-13-42/file/kit.zip'),"
                "kitjson:await go('GET','/api/shop/tiktok/2026-10-13-42/file/kit.json'),"
                "climb:await go('GET','/api/shop/tiktok/..%2F..%2Fx/file/video.mp4'),"
                "badslide:await go('GET','/api/shop/tiktok/2026-10-13-42/slide/..%2Fkit.json'),"
                "posted:await go('POST','/api/shop/tiktok/2026-10-13-42/posted',{}),"
                "board:await go('GET','/api/shop/board'),"
                "badslug:await go('POST','/api/shop/board/Bad%20Slug/approve',{})};"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, ECOSYSTEM_ROOT=d))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            listed = json.loads(out["list"][3])
            self.assertTrue(listed["available"], listed)
            self.assertEqual(listed["kits"][0]["caption"], "Rate this tote 1-10\n")
            self.assertEqual(out["slide"][:2], [200, "image/jpeg"])
            self.assertIn('filename="2026-10-13-42.zip"', out["zip"][2])
            self.assertEqual(out["kitjson"][0], 400, "only the kit's named files are served")
            self.assertEqual(out["climb"][0], 404)
            self.assertEqual(out["badslide"][0], 400)
            self.assertEqual(out["posted"][0], 200)
            self.assertEqual(json.loads((k / "kit.json").read_text())["status"], "posted")
            self.assertTrue(json.loads(out["board"][3])["available"])
            self.assertEqual(out["badslug"][0], 400)


if __name__ == "__main__":
    unittest.main()
