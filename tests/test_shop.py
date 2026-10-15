"""The Shop: tiktok-kit.py (a ready-to-post TikTok kit a day) and
store-board.py (every build's money and concerns, to approve, revise or
kill), and the page and API that show them (owner, 2026-10-13).

    python3 -m unittest tests.test_shop -b

The kit tests that render need ffmpeg and skip without it; the API tests need
node and the Deck's node_modules and skip without them.
"""

import contextlib
import importlib.util
import io
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


class OneKitAtATime(unittest.TestCase):
    """2026-10-14: the owner's `make --again` was still drawing its scenes
    when the 9:48 timer started a second build of the same listing; it
    cleared the same .part folder and the first run found a "0x0, 0.0s"
    video. Builds now take turns, and each has its own folder."""

    @classmethod
    def setUpClass(cls):
        cls.tk = load("tiktok_kit_lock", "tiktok-kit.py")

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        p = unittest.mock.patch.object(self.tk, "KITS", self.d / "tiktok")
        p.start()
        self.addCleanup(p.stop)

    def test_a_second_build_waits_then_gives_up(self):
        said, slept = [], []
        with self.tk.one_at_a_time():
            with self.assertRaises(self.tk.Busy):
                with self.tk.one_at_a_time(wait=10, sleep=slept.append, say=said.append):
                    self.fail("got the lock while another build held it")
        self.assertEqual(slept, [5, 5], "it waited before giving up")
        self.assertIn("waiting for it to finish", said[0])
        with self.tk.one_at_a_time(wait=0):
            pass                                    # free again once released

    def test_the_timer_waits_for_a_manual_build_then_finds_todays_kit(self):
        import threading
        import time as _time
        real_sleep = _time.sleep
        held, release = threading.Event(), threading.Event()

        def manual_build():
            with self.tk.one_at_a_time():
                held.set()
                release.wait(10)
                k = self.tk.KITS / "2026-10-14-1"
                k.mkdir(parents=True)
                (k / "kit.json").write_text(json.dumps({"day": "2026-10-14", "listing_id": 1,
                                                        "status": "ready", "made_utc": "x"}))
        t = threading.Thread(target=manual_build)
        t.start()
        held.wait(10)
        threading.Timer(0.3, release.set).start()
        args = type("A", (), {"again": False, "listing": None, "no_scenes": False})()
        out = io_capture()
        with unittest.mock.patch.object(self.tk.et_time, "day", return_value="2026-10-14"), \
             unittest.mock.patch.object(self.tk.time, "sleep", lambda s: real_sleep(0.05)), \
             unittest.mock.patch.object(self.tk, "_lv", return_value=type("L", (), {"ffmpeg": staticmethod(lambda: ("f", "p"))})()), \
             unittest.mock.patch.object(self.tk, "build_kit") as build, \
             unittest.mock.patch("sys.stdout", out):
            code = self.tk.cmd_make(args)
        t.join(10)
        self.assertEqual(code, 0)
        build.assert_not_called()
        self.assertIn("today's kit is made", out.getvalue())

    def test_each_build_has_its_own_folder(self):
        src = (SCRIPTS / "tiktok-kit.py").read_text()
        self.assertIn('part = KITS / f".part-{kid}-{os.getpid()}"', src)

    def test_an_unreadable_video_says_so(self):
        empty = self.d / "v.mp4"
        empty.write_bytes(b"")
        if not ff_available():
            self.skipTest("ffmpeg is not installed here")
        self.assertEqual(self.tk.video_problems(empty, 20), ["the video file could not be read (0 bytes)"])


def io_capture():
    import io
    return io.StringIO()


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


# ====================================================================== scenes

def ff_available():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def mockup(d, name="mock.png", bag="0xd9c9a6", backdrop="0xe8e8e6"):
    """A stand-in Printify mockup: a tote with straps and a printed panel on a
    plain studio backdrop."""
    p = Path(d) / name
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={backdrop}:s=600x600",
                    "-frames:v", "1", "-vf",
                    f"drawbox=x=220:y=80:w=26:h=140:color={bag}:t=fill,drawbox=x=354:y=80:w=26:h=140:color={bag}:t=fill,"
                    f"drawbox=x=220:y=80:w=160:h=20:color={bag}:t=fill,drawbox=x=130:y=190:w=340:h=360:color={bag}:t=fill,"
                    "drawbox=x=200:y=280:w=200:h=160:color=0x2f5d3a:t=fill", str(p)], check=True)
    return p


def picture(d, name, spec, size="1080x1920"):
    p = Path(d) / name
    src = spec if "=" in spec else f"color=c={spec}:s={size}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-frames:v", "1",
                    "-vf", f"scale={size.replace('x', ':')}", str(p)], check=True)
    return p


class SceneBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = load("scene_test", "scene.py")

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def ff(self):
        if not ff_available():
            self.skipTest("ffmpeg is not installed here")


class TheProductKeepsItsColour(SceneBase):
    """Matched in full to a dim orange table, a cream tote came out khaki
    grey (2026-10-13) - lit like the scene, and not the bag a buyer would
    receive. The product is only ever nudged."""

    def test_no_scene_moves_the_product_far(self):
        product = {"p95": 230.0, "mean": 180.0, "tint_r": 0.0, "tint_b": 0.0, "neutral_share": 0.3}
        for near, whole, tr, tb in ((60, 70, 0.4, -0.5), (250, 255, -0.4, 0.5), (140, 141, None, None)):
            scene = {"near_p95": near, "p95": whole, "tint_r": tr, "tint_b": tb, "side": 0.0}
            m = self.sc.match(scene, product)
            self.assertGreaterEqual(m["gain"], self.sc.GAIN_MIN)
            self.assertLessEqual(m["gain"], self.sc.GAIN_MAX)
            for ch in "rgb":
                self.assertLessEqual(abs(m[ch] / m["gain"] - 1), self.sc.TINT_MAX + 1e-9, (scene, m))

    def test_the_shadow_falls_away_from_the_light(self):
        product = {"p95": 200.0, "tint_r": None, "tint_b": None}
        lit_right = {"near_p95": 200, "p95": 200, "tint_r": None, "tint_b": None, "side": 0.3}
        self.assertLess(self.sc.match(lit_right, product)["shadow_dx"], 0)
        lit_left = dict(lit_right, side=-0.3)
        self.assertGreater(self.sc.match(lit_left, product)["shadow_dx"], 0)


class WhereTheProductGoes(SceneBase):
    """Laid flat in a top-down scene, it lies on the surface wherever it goes.
    The first try stood a tote at eye level and it hung in front of the table."""

    def test_flat_products_are_centred_a_mug_stands(self):
        x, y, pw, ph = self.sc.placement(500, 600, "tote bag")
        self.assertEqual(x, (self.sc.W - pw) // 2)
        self.assertAlmostEqual(y + ph / 2, self.sc.H * self.sc.FLAT_CENTRE, delta=2)
        x, y, pw, ph = self.sc.placement(500, 600, "mug")
        self.assertEqual(y + ph, int(self.sc.H * self.sc.STAND_BASE))

    def test_a_tall_product_is_capped(self):
        _x, _y, pw, ph = self.sc.placement(200, 900, "tote bag")
        self.assertLessEqual(ph, self.sc.H * self.sc.MAX_HEIGHT_SHARE + 2)

    def test_the_prompts_ask_for_the_right_view(self):
        self.assertIn("top-down", self.sc.prompt_for("tote bag", 0))
        self.assertIn("very edges", self.sc.prompt_for("tote bag", 0))
        self.assertIn("eye-level", self.sc.prompt_for("mug", 0))
        self.assertNotEqual(self.sc.prompt_for("tote bag", 0), self.sc.prompt_for("tote bag", 1))
        for t in ("tote bag", "mug", "sticker", "find"):
            self.assertIn("no people", self.sc.prompt_for(t, 0))


class TheFreeScene(SceneBase):
    """The free service answers refusals as JSON - sometimes with a 200 - and
    rate-limits by IP; one request in two was refused when asked quickly."""

    class Answer:
        def __init__(self, body, kind):
            self.body, self.headers = body, {"Content-Type": kind}

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(self, answers, seen):
        def go(req, timeout=None):
            seen.append(req)
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return go

    def test_a_json_refusal_and_a_429_are_retried_then_the_picture_kept(self):
        import urllib.error
        seen, waits = [], []
        answers = [self.Answer(b'{"error":"rate limited"}', "image/jpeg"),
                   urllib.error.HTTPError("u", 429, "Too Many", {}, None),
                   self.Answer(b"\xff\xd8\xff jpeg", "image/jpeg")]
        out = self.d / "bg.img"
        with unittest.mock.patch.object(self.sc, "credentials_key", return_value=None):
            got = self.sc.fetch_background("a table", 7, out, self.opener(answers, seen), waits.append)
        self.assertEqual(got, "free")
        self.assertEqual(out.read_bytes(), b"\xff\xd8\xff jpeg")
        self.assertEqual(waits, [self.sc.FETCH_WAIT] * 2)
        self.assertTrue(seen[0].full_url.startswith("https://image.pollinations.ai/prompt/a%20table?"))
        self.assertIn("seed=7", seen[0].full_url)

    def test_every_try_refused_says_so(self):
        answers = [self.Answer(b"{}", "application/json") for _ in range(self.sc.FETCH_TRIES)]
        with unittest.mock.patch.object(self.sc, "credentials_key", return_value=None):
            with self.assertRaises(self.sc.SceneError) as ctx:
                self.sc.fetch_background("x", 1, self.d / "o", self.opener(answers, []), lambda s: None)
        self.assertIn("refused 3 times", str(ctx.exception))
        self.assertFalse((self.d / "o").exists())

    def test_a_key_goes_in_a_header_never_the_url(self):
        seen = []
        with unittest.mock.patch.object(self.sc, "credentials_key", return_value="sk_test_value"):
            self.sc.fetch_background("x", 1, self.d / "o",
                                     self.opener([self.Answer(b"\xff\xd8", "image/jpeg")], seen), lambda s: None)
        self.assertNotIn("sk_test_value", seen[0].full_url)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer sk_test_value")

    def test_the_mark_is_cropped_and_a_tiny_scene_refused(self):
        self.ff()
        # The free picture carries its mark in the bottom-right corner.
        src = picture(self.d, "free.png", "color=c=0x806040:s=576x1024,drawbox=x=0:y=990:w=576:h=34:color=red:t=fill",
                      "576x1024")
        out = self.d / "bg.png"
        self.sc.prepare_background(src, out, keyed=False)
        self.assertEqual(self.sc.probe_size(out), (1080, 1920))
        px = self.sc.raw(out, 54, 96)
        last_row = px[-54 * 3:]
        reds = sum(1 for j in range(0, len(last_row), 3) if last_row[j] > 180 and last_row[j + 1] < 80)
        self.assertEqual(reds, 0, "the marked strip is gone")
        with self.assertRaises(self.sc.SceneError):
            self.sc.prepare_background(picture(self.d, "tiny.png", "0x806040", "300x500"), self.d / "t.png", False)


class TheLightingChecker(SceneBase):
    """Each way a pasted product gives itself away, measured on the finished
    picture."""

    def scene_from(self, bg, box):
        return self.sc.scene_light(self.sc.raw(bg, self.sc.W // 2, self.sc.H // 2), box)

    def composite(self, bg_spec, product="0xd9c9a6", rim=None):
        """A finished picture: a product rectangle on a scene, optionally
        with a light rim round it. Returns (path, scene, mask)."""
        box = (290, 660, 500, 600)
        x, y, w, h = box
        draw = f"drawbox=x={x}:y={y}:w={w}:h={h}:color={product}:t=fill"
        if rim:
            draw = f"drawbox=x={x}:y={y}:w={w}:h={h}:color={rim}:t=fill,drawbox=x={x + 4}:y={y + 4}:w={w - 8}:h={h - 8}:color={product}:t=fill"
        bg = picture(self.d, "bg.png", bg_spec)
        out = self.d / "c.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(bg), "-vf", draw, str(out)], check=True)
        scene = self.scene_from(bg, box)
        mask = {(cx, cy) for cx in range(x // 2, (x + w) // 2) for cy in range(y // 2, (y + h) // 2)}
        return out, scene, mask

    def test_a_calm_scene_and_a_matching_product_pass(self):
        self.ff()
        out, scene, mask = self.composite("0xb08a60")
        checks = self.sc.check(out, scene, mask, 0.0)
        self.assertTrue(all(c["ok"] for c in checks.values()), checks)

    def test_a_studio_lit_product_in_a_dim_room_glows(self):
        self.ff()
        out, scene, mask = self.composite("0x5a5048", product="0xffffff")
        checks = self.sc.check(out, scene, mask, 0.0)
        self.assertFalse(checks["glow"]["ok"])
        self.assertIn("pasted on", checks["glow"]["said"])

    def test_a_light_rim_is_backdrop_left_on_the_outline(self):
        self.ff()
        out, scene, mask = self.composite("0x806040", product="0x4a3a30", rim="0xe8e8e6")
        checks = self.sc.check(out, scene, mask, 0.0)
        self.assertFalse(checks["edges"]["ok"], checks["edges"])

    def test_backdrop_left_inside_fails(self):
        self.ff()
        out, scene, mask = self.composite("0xb08a60")
        self.assertFalse(self.sc.check(out, scene, mask, 0.10)["backdrop"]["ok"])

    def test_a_cluttered_or_dark_scene_fails(self):
        self.ff()
        out, scene, mask = self.composite("testsrc2=s=1080x1920")
        self.assertIn("cluttered", self.sc.check(out, scene, mask, 0.0)["background"]["said"])
        out, scene, mask = self.composite("0x151515")
        self.assertIn("too dark", self.sc.check(out, scene, mask, 0.0)["background"]["said"])

    def test_tint_is_judged_only_on_white_and_grey(self):
        r, b, share = self.sc.tint([(200, 200, 200), (210, 200, 190), (250, 10, 10)])
        self.assertAlmostEqual(share, 2 / 3)
        self.assertGreater(r, 0)
        self.assertLess(b, 0)
        self.assertEqual(self.sc.tint([(250, 10, 10)])[2], 0.0)


class AWholeScene(SceneBase):

    def test_the_real_product_set_into_a_scene(self):
        self.ff()
        mock = mockup(self.d)
        wood = picture(self.d, "wood.jpg", "color=c=0xb07a48:s=576x1024", "576x1024")
        out = self.d / "scene.jpg"
        fetch = lambda prompt, seed, o: (shutil.copy(wood, o), "free")[1]
        rep = self.sc.free_scene(mock, "tote bag", out, 0, 1, self.d, fetch=fetch)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(self.sc.probe_size(out), (1080, 1920))
        self.assertEqual(set(rep["checks"]), {"glow", "colour", "edges", "backdrop", "background"})
        # The design itself is still there, untouched: the panel's green.
        px = self.sc.raw(out, 108, 192)
        greens = sum(1 for j in range(0, len(px), 3) if px[j + 1] > px[j] + 25 and px[j + 1] > px[j + 2] + 10)
        self.assertGreater(greens, 50)

    def test_a_product_too_small_to_enlarge_is_refused(self):
        self.ff()
        small = picture(self.d, "small.png",
                        "color=c=0xe8e8e6:s=600x600,drawbox=x=240:y=240:w=110:h=110:color=0x2f5d3a:t=fill", "600x600")
        with self.assertRaises(self.sc.CutoutError) as ctx:
            self.sc.cutout(small, self.d)
        self.assertIn("too little product", str(ctx.exception))

    def test_a_photo_with_no_plain_backdrop_is_a_cutout_error(self):
        self.ff()
        busy = picture(self.d, "busy.png", "testsrc2=s=600x600", "600x600")
        with self.assertRaises(self.sc.CutoutError):
            self.sc.cutout(busy, self.d)


class PaidScenesSpendOnlyWhatIsAllowed(SceneBase):
    """Two rules before any paid call: the kits' own cap, and leaving Emily
    her share of the day's AI budget as OpenRouter counts it."""

    CAPS = {"tiktok_daily_ai": 0.30, "tiktok_leave_for_emily": 0.50}

    def test_the_kits_own_cap(self):
        ok, why = self.sc.allowance(0.28, 0.053, lambda: {"ai_left_today": 1.2}, self.CAPS)
        self.assertFalse(ok)
        self.assertIn("scene allowance is spent", why)
        self.assertTrue(self.sc.allowance(0.20, 0.053, lambda: {"ai_left_today": 1.2}, self.CAPS)[0])

    def test_emilys_share_is_left_alone(self):
        ok, why = self.sc.allowance(0.0, 0.053, lambda: {"ai_left_today": 0.52}, self.CAPS)
        self.assertFalse(ok)
        self.assertIn("kept for Emily's art", why)

    def test_an_unreadable_budget_is_a_no(self):
        def boom():
            raise RuntimeError("network down")
        self.assertFalse(self.sc.allowance(0.0, 0.05, boom, self.CAPS)[0])
        self.assertFalse(self.sc.allowance(0.0, 0.05, lambda: {"ai_left_today": None}, self.CAPS)[0])

    def test_the_caps_live_in_limits_json(self):
        got = self.sc.limits(ROOT / "tasks" / "limits.json")
        self.assertEqual(got, {"tiktok_daily_ai": 0.30, "tiktok_leave_for_emily": 0.50})  # owner's file, 2026-10-14
        bad = self.d / "limits.json"
        bad.write_text(json.dumps({"tiktok_daily_ai": "lots", "tiktok_leave_for_emily": -1}))
        self.assertEqual(self.sc.limits(bad), self.sc.LIMITS_DEFAULTS)
        self.assertEqual(self.sc.limits(self.d / "none.json"), self.sc.LIMITS_DEFAULTS)

    def test_mode(self):
        for env, want in (({}, "paid"), ({"TIKTOK_SCENES": "free"}, "free"),
                          ({"TIKTOK_SCENES": "OFF"}, "off"), ({"TIKTOK_SCENES": "nonsense"}, "paid")):
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                if not env:
                    os.environ.pop("TIKTOK_SCENES", None)
                self.assertEqual(self.sc.mode(), want)


class TheDesignCheck(SceneBase):
    """A redrawn print can come back changed. The vision model answers
    questions; code decides what counts as a problem."""

    GOOD = {"product_visible": True, "same_design": True, "words_a": "FERN", "words_b": "Fern!",
            "differences": [], "added_text": False}

    def test_a_faithful_redraw_passes(self):
        self.assertEqual(self.sc.design_problems(self.GOOD), [], "case and punctuation are not a change")

    def test_each_kind_of_change_fails(self):
        for change, said in ((dict(same_design=False), "design was changed"),
                             (dict(words_b="FEM"), "lettering changed"),
                             (dict(product_visible=False), "not clearly shown"),
                             (dict(differences=["the leaves are blue"]), "the leaves are blue"),
                             (dict(added_text=True), "words were added")):
            got = " | ".join(self.sc.design_problems(dict(self.GOOD, **change)))
            self.assertIn(said, got, change)

    def test_an_unread_answer_is_a_failure(self):
        self.assertTrue(self.sc.design_problems(None))
        partial = {k: v for k, v in self.GOOD.items() if k != "same_design"}
        self.assertIn("did not answer same_design", self.sc.design_problems(partial)[0])
        self.assertTrue(self.sc.design_problems(dict(self.GOOD, same_design="yes")),
                        "only a real true counts")

    def test_the_request_shows_both_pictures_and_reads_a_fenced_reply(self):
        a = self.d / "a.jpg"
        b = self.d / "b.jpg"
        a.write_bytes(b"\xff\xd8A")
        b.write_bytes(b"\xff\xd8B")
        sent = []

        def post(req):
            sent.append(json.loads(req.data))
            return {"choices": [{"message": {"content": "```json\n" + json.dumps(self.GOOD) + "\n```"}}],
                    "usage": {"cost": 0.0012}}
        problems, cost = self.sc.design_check(a, b, "k", post=post)
        self.assertEqual((problems, cost), ([], 0.0012))
        parts = sent[0]["messages"][0]["content"]
        urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(u.startswith("data:image/jpeg;base64,") for u in urls))


class APaidScene(SceneBase):
    """The product's photo drawn into a scene, compared with the photo, and
    paid for only within the allowance."""

    def setUp(self):
        super().setUp()
        if not ff_available():
            self.skipTest("ffmpeg is not installed here")
        self.photo = picture(self.d, "photo.png", "0xd9c9a6", "800x800")
        self.room = lambda: {"ai_left_today": 1.20}
        self.caps = {"tiktok_daily_ai": 0.30, "tiktok_leave_for_emily": 0.50}

    def drawer(self, sizes):
        calls = []

        def generate(path, prompt, images):
            calls.append((prompt, images))
            picture(self.d, "drawn.png", "0xb07a48", sizes[min(len(calls), len(sizes)) - 1])
            shutil.copy(self.d / "drawn.png", path)
            return 1, {"cost": 0.039}
        return generate, calls

    def run_scene(self, generate, check, room=None):
        return self.sc.paid_scene(self.photo, "tote bag", self.d / "scene.jpg", 0, self.d, 0.0, key="k",
                                  generate=generate, check_design=check, budget_read=room or self.room,
                                  caps=self.caps)

    def test_a_good_scene(self):
        gen, calls = self.drawer(["768x1344"])
        rep = self.run_scene(gen, lambda ref, pic: ([], 0.001))
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["cost"], 0.04)
        self.assertEqual(self.sc.probe_size(self.d / "scene.jpg"), (1080, 1920))
        prompt, images = calls[0]
        self.assertIn("THIS EXACT tote bag", prompt)
        self.assertEqual(images[0][0], "image/jpeg", "the model is shown the real photo")

    def test_a_changed_design_gets_one_more_try_then_fails(self):
        gen, calls = self.drawer(["768x1344"])
        rep = self.run_scene(gen, lambda ref, pic: (["the lettering changed"], 0.001))
        self.assertEqual(len(calls), self.sc.PAID_TRIES)
        self.assertFalse(rep["ok"])
        self.assertIn("lettering changed", rep["why"])
        self.assertAlmostEqual(rep["cost"], 0.08, places=3, msg="both tries are counted")

    def test_a_wide_picture_is_tried_again(self):
        gen, calls = self.drawer(["1344x768", "768x1344"])
        rep = self.run_scene(gen, lambda ref, pic: ([], 0.001))
        self.assertTrue(rep["ok"])
        self.assertEqual(len(calls), 2)

    def test_nothing_is_spent_without_room(self):
        gen, calls = self.drawer(["768x1344"])
        with self.assertRaises(self.sc.SpendError):
            self.run_scene(gen, lambda ref, pic: ([], 0.001), room=lambda: {"ai_left_today": 0.40})
        self.assertEqual(calls, [], "refused before the call, not after")

    def test_emily_assets_sends_the_photo_with_the_prompt(self):
        ea = load("emily_assets_scene_test", "emily-assets.py")
        sent = []
        png = (self.d / "photo.png").read_bytes()

        def post(req):
            sent.append(json.loads(req.data))
            return {"choices": [{"message": {"images": [{"image_url": {
                "url": "data:image/png;base64," + __import__("base64").b64encode(png).decode()}}]}}],
                "usage": {"cost": 0.039}}
        with unittest.mock.patch.object(ea, "post", post):
            n, usage = ea.generate(self.d / "out.png", "draw it", "k", model="m", direction="",
                                   aspect="9:16", images=[("image/jpeg", b"\xff\xd8x")])
        req = sent[0]
        content = req["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertNotIn(ea.PRINT_DIRECTION[:40], content[0]["text"], "no print-file direction on a scene")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(req["image_config"], {"aspect_ratio": "9:16"})
        self.assertEqual(usage["cost"], 0.039)
        # Emily's own art request is unchanged: a plain string.
        with unittest.mock.patch.object(ea, "post", post):
            ea.generate(self.d / "out2.png", "draw it", "k", model="m", product="tote")
        self.assertIsInstance(sent[1]["messages"][0]["content"], str)


class KitsWithScenes(unittest.TestCase):
    """The kit holds AI scenes for the owner's eye before it can be posted."""

    @classmethod
    def setUpClass(cls):
        cls.tk = load("tiktok_kit_scenes", "tiktok-kit.py")
        cls.sc = cls.tk._scene()

    def setUp(self):
        if not ff_available() or not self.tk.font():
            self.skipTest("ffmpeg or a bold font is not installed here")
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        p = unittest.mock.patch.object(self.tk, "KITS", self.d / "tiktok")
        p.start()
        self.addCleanup(p.stop)
        self.photos = [picture(self.d, "a.png", "0xd9c9a6", "800x800"), picture(self.d, "b.png", "0x2f5d3a", "800x800")]

    def maker(self, fail=None):
        calls = []

        def make(photo, thing, out, n, seed, scratch, **kw):
            calls.append(Path(photo).name)
            if fail and fail(len(calls), Path(photo).name):
                raise fail(len(calls), Path(photo).name)
            picture(self.d, Path(out).name, "0xb07a48")
            shutil.copy(self.d / Path(out).name, out)
            return {"ok": True, "why": None, "checks": {"glow": {"ok": True, "said": "fine"}},
                    "prompt": "a table", "source": "free"}
        return make, calls

    def test_scenes_go_first_and_hold_the_kit_for_review(self):
        make, _ = self.maker()
        waits = []
        kit = self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, maker=make, wait=waits.append,
                                how="free")
        self.assertEqual(kit["status"], "needs_review")
        self.assertEqual(kit["ai_slides"], ["01.jpg", "02.jpg", "03.jpg"], "the hook is on a scene too")
        self.assertEqual([s["slide"] for s in kit["scenes"]], ["02.jpg", "03.jpg"])
        self.assertEqual(kit["slides"], 1 + 2 + 2 + 1)
        self.assertEqual(waits, [self.tk.SCENE_SPACING], "the free service is not asked twice in a row")
        paid_waits = []
        self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, maker=make, wait=paid_waits.append,
                          how="paid")
        self.assertEqual(paid_waits, [], "the paid service is not made to wait")
        self.assertIn("AI-generated content", (self.tk.KITS / kit["id"] / "audio.txt").read_text())
        with self.assertRaises(ValueError):
            self.tk.set_posted(kit["id"])
        self.assertEqual(self.tk.approve(kit["id"])["status"], "ready")
        self.assertEqual(self.tk.set_posted(kit["id"])["status"], "posted")

    def test_todays_allowance_running_out_stops_the_rest(self):
        make, calls = self.maker(fail=lambda n, name: self.sc.SpendError("today's scene allowance is spent")
                                 if n == 2 else None)
        kit = self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, maker=make,
                                wait=lambda s: None, scenes=3, how="paid")
        self.assertEqual(len(calls), 2, "no third scene is asked for once the money is gone")
        self.assertEqual(len(kit["scenes"]), 1)
        self.assertEqual(kit["scenes_dropped"], ["today's scene allowance is spent"])

    def test_what_scenes_cost_is_kept_and_counted_for_the_day(self):
        def make(photo, thing, out, n, seed, scratch, spent_today=0.0, how=None):
            seen.append(spent_today)
            picture(self.d, "s.png", "0xb07a48")
            shutil.copy(self.d / "s.png", out)
            return {"ok": True, "why": None, "checks": {}, "prompt": "p", "source": "paid", "cost": 0.04}
        seen = []
        kit = self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, maker=make,
                                wait=lambda s: None, spent=0.10, how="paid")
        self.assertEqual(seen, [0.10, 0.14], "each scene knows what the day has spent so far")
        self.assertEqual(kit["scene_cost"], 0.08)
        self.assertEqual(self.tk.spent_today([kit, {"day": "2026-10-12", "scene_cost": 9}], "2026-10-13"), 0.08)

    def test_a_photo_that_will_not_cut_out_is_skipped_for_the_next(self):
        make, calls = self.maker(fail=lambda n, name: self.sc.CutoutError("no backdrop") if name == "a.png" else None)
        kit = self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, maker=make, wait=lambda s: None,
                                how="free")
        self.assertEqual(calls[:2], ["a.png", "b.png"])
        self.assertEqual(len(kit["scenes"]), 2)
        self.assertIn("a.png: no backdrop", kit["scenes_dropped"][0])

    def test_a_failed_scene_costs_only_itself(self):
        make, _ = self.maker(fail=lambda n, name: self.sc.SceneError("refused 3 times") if n == 1 else None)
        kit = self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, maker=make, wait=lambda s: None)
        self.assertEqual(len(kit["scenes"]), 1)
        self.assertEqual(kit["scenes_dropped"], ["refused 3 times"])

    def test_no_scenes_is_the_plain_kit(self):
        kit = self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, scenes=0)
        self.assertEqual((kit["status"], kit["ai_slides"]), ("ready", []))
        self.assertNotIn("AI-generated", (self.tk.KITS / kit["id"] / "audio.txt").read_text())

    def test_dropping_a_slide_rebuilds_the_video_and_zip(self):
        make, _ = self.maker()
        kit = self.tk.build_kit(ROW, self.photos, "2026-10-13", "Shop on Etsy", 0, maker=make, wait=lambda s: None)
        k = self.tk.KITS / kit["id"]
        after = self.tk.drop_slide(kit["id"], "02.jpg")
        self.assertEqual(after["slides"], kit["slides"] - 1)
        self.assertEqual(sorted(p.name for p in (k / "slides").iterdir()),
                         [f"{n:02d}.jpg" for n in range(1, kit["slides"])])
        self.assertEqual(after["ai_slides"], ["01.jpg", "02.jpg"], "the old 03 is now 02")
        self.assertEqual([s["slide"] for s in after["scenes"]], ["02.jpg"])
        names = zipfile.ZipFile(k / f"{kit['id']}.zip").namelist()
        self.assertEqual(len([n for n in names if "/photos/" in n]), after["slides"])
        secs, _w, _h, _a = self.tk._lv().probe(k / "video.mp4")
        self.assertAlmostEqual(secs, after["video_seconds"], delta=0.25)
        # Dropping every AI slide makes it a plain kit again.
        self.tk.drop_slide(kit["id"], "01.jpg")
        last = self.tk.drop_slide(kit["id"], "01.jpg")
        self.assertEqual((last["ai_slides"], last["status"]), ([], "ready"))
        self.assertNotIn("AI-generated", (k / "audio.txt").read_text())

    def test_a_kit_keeps_two_slides_and_names_are_checked(self):
        kit = self.tk.build_kit(ROW, self.photos[:1], "2026-10-13", "Shop on Etsy", 0, scenes=0)
        self.tk.drop_slide(kit["id"], "01.jpg")
        with self.assertRaises(ValueError):
            self.tk.drop_slide(kit["id"], "01.jpg")
        with self.assertRaises(ValueError):
            self.tk.drop_slide(kit["id"], "../kit.json")


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
        for f in (SCRIPTS / "tiktok-kit.py", SCRIPTS / "store-board.py", SCRIPTS / "scene.py", API / "shop.js"):
            src = f.read_text().lower()
            self.assertNotIn("open-api.tiktok.com", src)
            self.assertNotIn("/publish.json", src)
            self.assertNotIn("publishing_succeeded", src)


class TryAgainInTheGallery(unittest.TestCase):
    """Owner, 2026-10-15: "a rerun button for designs when they don't work
    because of not having enough money at that time". emily-finish.py --retry
    does whichever step a build is missing - and never a listing on sale."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("emily_finish_retry", SCRIPTS / "emily-finish.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "side-eye-cat"
        self.d.mkdir()
        self.m.find_build = lambda raw: self.d
        self.calls = []
        self.m.redo = lambda raw, product=None, keep_art=False: self.calls.append(keep_art) or 0

    def build(self, **kw):
        (self.d / "build.json").write_text(json.dumps(dict({"status": "ready_local"}, **kw)))

    def art(self, placeholder=False):
        mark = b"emily-assets placeholder - not artwork" if placeholder else b""
        (self.d / "design.png").write_bytes(b"\x89PNG\r\n\x1a\n" + mark + b"\0" * 64)

    def run_quiet(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.m.retry("side-eye-cat")

    def test_no_art_is_drawn_again(self):
        self.build()
        self.assertEqual(self.run_quiet(), 0)
        self.assertEqual(self.calls, [False], "a fresh drawing, then the draft")

    def test_placeholder_art_is_drawn_again(self):
        self.build()
        self.art(placeholder=True)
        self.run_quiet()
        self.assertEqual(self.calls, [False])

    def test_real_art_without_a_draft_is_only_drafted(self):
        # The Side-Eye Cat: real art, LOCAL ONLY. Drawing it again would
        # cost money and change art that was fine.
        self.build()
        self.art()
        self.run_quiet()
        self.assertEqual(self.calls, [True])

    def test_a_finished_build_is_left_alone(self):
        self.build(printify_product_id="abc123")
        self.art()
        self.run_quiet()
        self.assertEqual(self.calls, [])

    def test_a_listing_on_sale_is_never_touched(self):
        self.build(published=True)
        self.assertEqual(self.run_quiet(), 1)
        self.assertEqual(self.calls, [])

    def test_a_draft_that_runs_out_of_time_says_so_in_words(self):
        # The first real Try again ran out its 300 s and put a traceback on
        # the card. Now: a sentence, with the last step it reached.
        self.build()
        self.art()

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(a[0], k.get("timeout"),
                                            output="checking art\nuploading design.png (2100 KB) ...\n")
        with unittest.mock.patch.object(self.m.subprocess, "run", slow):
            self.assertEqual(self.run_quiet_draft(), 0)
        why = json.loads((self.d / "build.json").read_text())["draft_blocked"]["reason"]
        self.assertIn("still running after 15 minutes", why)
        self.assertIn("uploading design.png", why)
        self.assertNotIn("Traceback", why)

    def run_quiet_draft(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.m.draft(self.d)

    def test_the_gallery_offers_it_and_the_api_runs_it(self):
        html = (API / "public" / "village.html").read_text()
        self.assertIn("retryBox(b)", html.split("async function loadEmilyBuilds", 1)[1])
        js = (API / "emily.js").read_text()
        route = js.split("'/api/emily/builds/:slug/retry'", 1)[1].split("app.post(", 1)[0]
        self.assertIn("'--retry', slug", route)
        self.assertIn("SLUG_RE.test", route)


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
                "approve:await go('POST','/api/shop/tiktok/2026-10-13-42/approve',{}),"
                "baddrop:await go('POST','/api/shop/tiktok/2026-10-13-42/drop',{slide:'../kit.json'}),"
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
            self.assertEqual(out["approve"][0], 200)
            self.assertEqual(out["baddrop"][0], 400)
            self.assertTrue(json.loads(out["board"][3])["available"])
            self.assertEqual(out["badslug"][0], 400)


if __name__ == "__main__":
    unittest.main()
