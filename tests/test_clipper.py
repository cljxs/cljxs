"""Clipper, Phase 1. No dependencies: the transcriber is swapped for the
"file" provider and fed real faster-whisper output captured 2026-09-28
(tests/fixtures/clipper-words-librivox.json, a public-domain LibriVox
reading). The end-to-end test needs ffmpeg and skips without it.
"""

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from clipper import captions, cli, config, db, media, moments, pipeline, render, rights, score, transcribe  # noqa: E402

WORDS = json.loads((FIXTURES / "clipper-words-librivox.json").read_text())["words"]
CFG = dict(config.DEFAULTS)


def w(start, end, word):
    return {"start": start, "end": end, "word": word, "p": 0.9}


class Rights(unittest.TestCase):
    """Nothing gets in without a named permission and written evidence."""

    def test_the_gate(self):
        self.assertIn("unknown rights", rights.check("found-it-online", "x"))
        self.assertIn("evidence is required", rights.check("permission", "  "))
        self.assertIn("commercial", rights.check("cc-by-nc", "link", "credit"))
        self.assertIsNone(rights.check("cc-by-nc", "link", "credit", allow_noncommercial=True))
        self.assertIn("requires credit", rights.check("cc-by", "link"))
        self.assertIsNone(rights.check("cc-by", "link", "Clip from X (CC BY)"))
        self.assertIsNone(rights.check("own", "my channel"))


class Config(unittest.TestCase):
    def test_a_misspelt_setting_is_an_error_not_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"max_clip_second": 45}))
            with self.assertRaisesRegex(ValueError, "max_clip_second"):
                config.load(p)
            p.write_text(json.dumps({"max_clip_seconds": 45}))
            self.assertEqual(config.load(p)["max_clip_seconds"], 45)


class Database(unittest.TestCase):
    def test_migrations_run_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.db"
            conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
            conn.close()
            conn = db.connect(path)          # a second open must not re-run CREATE TABLE
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))

    def test_the_spend_guard_refuses_before_spending(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "c.db")
            cfg = dict(CFG, daily_budget_usd=0.10)
            db.spend_guard(conn, cfg, 0.05)
            db.record_cost(conn, "groq", "transcribe", 0.08)
            with self.assertRaisesRegex(db.OverBudget, r"\$0\.10 daily cap"):
                db.spend_guard(conn, cfg, 0.05)


class Sentences(unittest.TestCase):
    """Clips are runs of whole sentences, so these rules are the ones that
    decide whether a clip can start or stop mid-thought."""

    def setUp(self):
        self.sents = moments.sentences(WORDS, CFG["sentence_pause_seconds"])

    def test_no_word_is_lost_or_repeated(self):
        covered = [i for s in self.sents for i in range(s["first"], s["last"] + 1)]
        self.assertEqual(covered, list(range(len(WORDS))))

    def test_every_sentence_ends_on_punctuation_or_a_pause(self):
        for s in self.sents[:-1]:
            self.assertTrue(moments.ends_sentence(WORDS[s["last"]]["word"])
                            or s["pause_after"] >= CFG["sentence_pause_seconds"], s["text"])

    def test_a_pause_splits_where_whisper_wrote_no_full_stop(self):
        ws = [w(0, 0.4, " so"), w(0.5, 0.9, " anyway"), w(2.0, 2.3, " next"), w(2.4, 2.8, " thing.")]
        self.assertEqual([s["text"] for s in moments.sentences(ws, 0.8)], ["so anyway", "next thing."])

    def test_a_closing_quote_does_not_hide_the_full_stop(self):
        self.assertTrue(moments.ends_sentence(' said "no."'))
        self.assertTrue(moments.ends_sentence(" really?)"))
        self.assertFalse(moments.ends_sentence(" Mr"))


class Windows(unittest.TestCase):
    def setUp(self):
        self.sents = moments.sentences(WORDS, 0.8)

    def test_windows_are_whole_sentences_within_the_length_bounds(self):
        wins = moments.windows(self.sents, 20, 60)
        self.assertGreater(len(wins), 20)
        for i, j in wins:
            dur = self.sents[j]["end"] - self.sents[i]["start"]
            self.assertTrue(20 <= dur <= 60, (i, j, dur))

    def test_choose_never_overlaps_and_respects_the_limits(self):
        scored = score.score_windows(self.sents, moments.windows(self.sents, 20, 60), WORDS, [], CFG)
        chosen = moments.choose(scored, 4, 0)
        self.assertEqual(len(chosen), 4)
        for a in chosen:
            for b in chosen:
                if a is not b:
                    self.assertFalse(moments.overlaps(a, b))
        self.assertEqual(chosen[0]["score"], max(c["score"] for c in scored))
        self.assertEqual(moments.choose(scored, 4, 101), [], "nothing clears an impossible bar")

    def test_the_cut_never_reaches_into_the_next_word(self):
        ws = [w(10.0, 10.5, " end."), w(10.6, 11.0, " Next")]
        start, end = moments.cut_points(ws, 0, 0)
        self.assertEqual(start, 9.85)
        self.assertLessEqual(end, 10.55)
        self.assertGreaterEqual(end, 10.5)


class Scoring(unittest.TestCase):
    def one(self, text_words, loud=()):
        ws, t = [], 0.0
        for x in text_words:
            ws.append(w(t, t + 0.3, x))
            t += 0.35
        s = moments.sentences(ws, 0.8)
        return score.features(s, 0, len(s) - 1, ws, list(loud), -30.0, CFG)

    def test_an_opener_that_needs_earlier_context_scores_lower(self):
        dangling = self.one([" And", " then", " he", " left."])
        clean = self.one([" The", " captain", " left."])
        self.assertLess(dangling["standalone"], clean["standalone"])

    def test_a_question_is_a_hook(self):
        self.assertGreater(self.one([" Why", " did", " it", " fail?"])["hook"],
                           self.one([" It", " failed", " quietly."])["hook"])

    def test_loud_moments_score_energy_against_this_video_not_an_absolute(self):
        f = self.one([" Look", " at", " that!"], loud=[-20.0] * 3)   # 10 dB over the -30 median
        self.assertEqual(f["energy"], 1.0)

    def test_every_score_is_explained_by_stored_features(self):
        sents = moments.sentences(WORDS, 0.8)
        for c in score.score_windows(sents, moments.windows(sents, 20, 60), WORDS, [], CFG):
            self.assertEqual(set(c["features"]), set(score.WEIGHTS))
            self.assertTrue(0 <= c["score"] <= 100)
            self.assertEqual(c["score"], score.total(c["features"]))


class Captions(unittest.TestCase):
    def setUp(self):
        self.ass = captions.build(WORDS, 38.0, 75.0)
        self.events = [l for l in self.ass.splitlines() if l.startswith("Dialogue:")]

    @staticmethod
    def secs(t):
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    def test_timestamps(self):
        self.assertEqual(captions.ts(0), "0:00:00.00")
        self.assertEqual(captions.ts(3725.456), "1:02:05.46")

    def test_one_caption_at_a_time_all_inside_the_clip(self):
        spans = [(self.secs(e.split(",")[1]), self.secs(e.split(",")[2])) for e in self.events]
        self.assertGreater(len(spans), 30)
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            self.assertLess(s1, e1)
            self.assertLessEqual(e1, s2 + 1e-6, "two captions on screen at once")
        self.assertGreaterEqual(spans[0][0], 0)
        self.assertLessEqual(spans[-1][1], 37.0 + 1e-6)

    def test_exactly_one_word_is_lit_per_event(self):
        for e in self.events:
            self.assertEqual(e.count(captions.STYLES["bold-yellow"]["highlight"]), 1, e)

    def test_a_caption_never_runs_one_sentence_into_the_next(self):
        ws = [w(0, 0.3, " Done."), w(0.35, 0.6, " Next"), w(0.65, 0.9, " one")]
        self.assertEqual([[x["word"] for x in c] for c in captions.chunks(ws)],
                         [[" Done."], [" Next", " one"]])

    def test_braces_cannot_become_ass_override_codes(self):
        self.assertEqual(captions.clean(" {\\b1}hi", True), "(/B1)HI")


class Loudness(unittest.TestCase):
    """Parsed from real ffmpeg ebur128 output, which writes "M:-120.7" and
    "M: -16.0" depending on the number's width."""

    def test_the_real_log(self):
        text = (FIXTURES / "clipper-ebur128.log").read_text()
        frames = media.parse_ebur128(text)
        # Counted from the file, not restated: every per-frame line, and none
        # of the summary lines (whose "I:" must not be read as a reading).
        self.assertEqual(len(frames), sum(1 for l in text.splitlines() if " t: " in l))
        self.assertTrue(all(-121 <= lufs <= 0 for _, lufs in frames))
        per = media.loudness_per_second(frames)
        self.assertEqual(len(per), int(frames[-1][0]) + 1)
        self.assertEqual(per[1], max(l for t, l in frames if 1 <= t < 2))

    def test_both_spacings(self):
        self.assertEqual(media.parse_ebur128("t: 1.2  TARGET:-23 LUFS    M:-120.7 S:-1\n"
                                             "t: 1.3  TARGET:-23 LUFS    M: -16.0 S:-1"),
                         [(1.2, -120.7), (1.3, -16.0)])


class Crop(unittest.TestCase):
    def test_center_crop_boxes(self):
        self.assertEqual(render.center_crop(1920, 1080), (606, 1080, 657, 0))
        self.assertEqual(render.center_crop(1280, 720), (404, 720, 438, 0))
        self.assertEqual(render.center_crop(1080, 1920), (1080, 1920, 0, 0))
        self.assertEqual(render.center_crop(1080, 1080), (606, 1080, 237, 0))
        w_, h_, _, _ = render.center_crop(1279, 719)
        self.assertEqual((w_ % 2, h_ % 2), (0, 0), "x264 needs even dimensions")

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            render.video_filter("zoom", 1920, 1080, "a.ass", 30)


from clipper import framing  # noqa: E402

try:
    import cv2  # noqa: F401
    HAVE_CV2 = True
except ImportError:
    HAVE_CV2 = False


class FramingLayouts(unittest.TestCase):
    """Owner, 2026-10-07: "In every clip of Gil's Arena it cuts to this camera
    angle showing nothing" - the middle strip of a wide shot whose hosts sit
    at the ends of a couch. Each shot is framed on its people instead."""

    SW, SH = 1920, 1080

    def test_nobody_found_shows_the_whole_frame(self):
        self.assertEqual(framing.layout([], self.SW, self.SH)["layout"], "full")

    def test_one_face_is_centred_and_kept_inside_the_frame(self):
        got = framing.layout([(1250, 500, 240, 320)], self.SW, self.SH)
        self.assertEqual(got["layout"], "crop")
        w, h, x, y = got["box"]
        self.assertEqual((w, h), (606, 1080))
        self.assertLess(abs(x + w / 2 - 1250), 2)
        edge = framing.layout([(1880, 500, 120, 160)], self.SW, self.SH)["box"]
        self.assertEqual(edge[2] + edge[0], self.SW, "clamped at the edge, not past it")

    def test_two_close_share_a_strip_two_apart_split(self):
        self.assertEqual(framing.layout([(860, 800, 110, 140), (1060, 800, 110, 140)],
                                        self.SW, self.SH)["layout"], "crop")
        got = framing.layout([(170, 760, 120, 150), (1750, 760, 120, 150)], self.SW, self.SH)
        self.assertEqual(got["layout"], "split")
        (w1, h1, x1, y1), (w2, h2, x2, y2) = got["boxes"]
        self.assertEqual((w1, h1), (w2, h2), "both halves at the same zoom")
        self.assertTrue(x1 <= 170 <= x1 + w1 and x2 <= 1750 <= x2 + w2, "each half holds its person")
        self.assertTrue(y1 <= 760 <= y1 + h1)
        self.assertLess(h1, self.SH, "zoomed to the faces, not the whole height")

    def test_three_across_the_frame_show_it_all(self):
        self.assertEqual(framing.layout([(200, 700, 100, 130), (960, 700, 100, 130), (1700, 700, 100, 130)],
                                        self.SW, self.SH)["layout"], "full")

    def test_a_flicker_is_not_a_person(self):
        samples = [(k / 5, [(400, 500, 100, 130)]) for k in range(10)]
        samples[3] = (0.6, [(400, 500, 100, 130), (1500, 500, 100, 130)])     # once in ten
        self.assertEqual([round(p[0]) for p in framing.people(samples, self.SW)], [400])

    def test_a_flash_of_a_shot_joins_the_one_before(self):
        self.assertEqual(framing.shots([2.0, 2.3, 5.0], 8.0), [(0.0, 2.3), (2.3, 5.0), (5.0, 8.0)])
        self.assertEqual(framing.shots([], 8.0), [(0.0, 8.0)])

    def test_the_filter_cuts_each_shot_and_joins_them(self):
        plan = [{"t0": 0.0, "t1": 2.0, "layout": "split", "boxes": [(658, 584, 0, 300), (658, 584, 1262, 300)]},
                {"t0": 2.0, "t1": 4.0, "layout": "crop", "box": (606, 1080, 946, 0)},
                {"t0": 4.0, "t1": 8.0, "layout": "full"}]
        f = render.video_filter("faces", 1920, 1080, "a.ass", 30, plan=plan)
        self.assertIn("[0:v]split=3[i0][i1][i2]", f)
        self.assertIn("trim=start=2.000:end=4.000", f)
        self.assertIn("[i2]trim=start=4.000,setpts", f, "the last shot runs on to the clip's end")
        self.assertIn("concat=n=3:v=1:a=0", f)
        self.assertIn("vstack", f)
        self.assertTrue(f.endswith(",fps=30,ass=a.ass[v]"))


@unittest.skipUnless(HAVE_CV2 and shutil.which("ffmpeg"), "needs OpenCV and ffmpeg")
class FramingOnRealFaces(unittest.TestCase):
    """Four shots made from NASA crew portraits (public domain,
    tests/fixtures/faces): the couch, a close-up off centre, an empty set, two
    side by side. The empty shot is the one ffmpeg's scene score alone missed."""

    FACES = Path(__file__).parent / "fixtures" / "faces"

    @classmethod
    def setUpClass(cls):
        import numpy as np
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = Path(cls.tmp.name) / "shots.mp4"
        P = [cv2.imread(str(cls.FACES / f"person{i}.jpg")) for i in (1, 2, 3, 4)]

        def frame(bg, people):
            img = np.full((1080, 1920, 3), bg, np.uint8)
            cv2.rectangle(img, (0, 700), (1920, 1080), tuple(int(c * 0.5) for c in bg), -1)
            for p, cx, k in people:
                q = cv2.resize(p, None, fx=k, fy=k)
                h, w = q.shape[:2]
                img[1080 - h - 60:1020, int(cx - w / 2):int(cx - w / 2) + w] = q
            return img
        shots = [((40, 20, 60), [(P[0], 170, 1.0), (P[1], 1750, 1.0)]),
                 ((20, 60, 40), [(P[2], 1250, 2.4)]),
                 ((70, 70, 20), []),
                 ((30, 30, 80), [(P[3], 860, 1.1), (P[0], 1060, 1.1)])]
        w = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                              "-s", "1920x1080", "-r", "30", "-i", "-", "-f", "lavfi", "-i",
                              "sine=frequency=300:duration=8", "-shortest", "-c:v", "libx264",
                              "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.video)],
                             stdin=subprocess.PIPE)
        for bg, people in shots:
            w.stdin.write(frame(bg, people).tobytes() * 60)
        w.stdin.close()
        w.wait()
        cls.info = media.probe(cls.video)
        cls.plan = framing.plan(cls.video, 0.0, 8.0, cls.info)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_each_shot_gets_its_own_layout(self):
        got = [(s["layout"], round(s["t0"], 1), round(s["t1"], 1)) for s in self.plan]
        self.assertEqual(got, [("split", 0.0, 2.0), ("crop", 2.0, 4.0), ("full", 4.0, 6.0), ("crop", 6.0, 8.0)])

    def test_the_crops_are_on_the_people(self):
        w, _, x, _ = self.plan[1]["box"]
        self.assertLess(abs(x + w / 2 - 1250), 40, "the close-up, off centre")
        w, _, x, _ = self.plan[3]["box"]
        self.assertLess(abs(x + w / 2 - 960), 40, "between the two side by side")

    def test_it_renders_and_says_how(self):
        out = Path(self.tmp.name) / "out" / "01.mp4"
        cfg = dict(config.DEFAULTS, x264_preset="ultrafast")
        info = render.render(self.video, out, 0.0, 8.0, "[Script Info]\nScriptType: v4.00+\n", cfg)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertAlmostEqual(info["duration"], 8.0, delta=0.2)
        self.assertEqual(info["framing"], "4 shot(s): split 0-2s, crop 2-4s, full 4-6s, crop 6-8s")


class FramingFallsBackToTheCentre(unittest.TestCase):
    def test_no_opencv_is_a_centre_crop_said_out_loud(self):
        real = framing.plan

        def missing(*a, **k):
            raise framing.FramingUnavailable("OpenCV is not installed")
        framing.plan = missing
        try:
            calls = []
            real_run, real_probe = media.run, media.probe
            media.run = lambda cmd, cwd=None: calls.append(cmd)
            media.probe = lambda p: {"width": 1920, "height": 1080, "has_video": True, "duration": 8.0}
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "01.part.mp4").write_bytes(b"x")
                info = render.render("in.mp4", Path(d) / "01.mp4", 0.0, 8.0, "", dict(config.DEFAULTS))
        finally:
            framing.plan, media.run, media.probe = real, real_run, real_probe
        self.assertEqual(info["framing"], "centre crop - OpenCV is not installed")
        vf = calls[0][calls[0].index("-filter_complex") + 1]
        self.assertIn("crop=606:1080:657:0", vf)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class EndToEnd(unittest.TestCase):
    """The whole Phase 1 path through the real CLI: rights gate, ingest,
    duplicate refusal, transcribe (from the real fixture), find, render, and
    a failure that is retried rather than lost."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.old = os.environ.get("CLIPPER_HOME")
        os.environ["CLIPPER_HOME"] = str(self.home)
        self.addCleanup(self.restore)
        self.home.mkdir()
        # Small and fast so CI can afford it: 60 s of video, one clip, ultrafast.
        (self.home / "config.json").write_text(json.dumps({
            "transcriber": "file", "max_clips_per_video": 1, "min_score": 0,
            "x264_preset": "ultrafast", "max_clip_seconds": 30,
            # Framing has its own tests (FramingOnRealFaces); on these it found
            # no faces in a test pattern and added minutes to the suite.
            "crop_mode": "center",
            # On the droplet the OpenRouter key is real: a test must never
            # spend it. Drafting by model is tested with a stand-in call.
            "copy_model": None}))
        self.video = Path(self.tmp.name) / "talk.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000",
                        "-t", "60", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        str(self.video)], check=True)
        self.words = [x for x in WORDS if x["end"] <= 59.0]

    def restore(self):
        if self.old is None:
            os.environ.pop("CLIPPER_HOME", None)
        else:
            os.environ["CLIPPER_HOME"] = self.old

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def give_words(self, vid):
        folder = self.home / "media" / str(vid)
        (folder / "audio.wav.words.json").write_text(json.dumps({
            "provider": "file", "model": "fixture", "language": "en", "duration": 60,
            "words": self.words}))

    def test_permitted_video_in_captioned_short_out(self):
        code, said = self.cli("ingest", "nobody", str(self.video))
        self.assertEqual(code, 2)
        self.assertIn("no active source", said)

        code, said = self.cli("source", "add", "pd", "--rights", "public-domain")
        self.assertEqual(code, 2, "no evidence, no source")
        self.assertEqual(self.cli("source", "add", "pd", "--rights", "public-domain",
                                  "--evidence", "LibriVox, public domain")[0], 0)
        self.assertEqual(self.cli("ingest", "pd", str(self.video), "--title", "Talk")[0], 0)
        code, said = self.cli("ingest", "pd", str(self.video))
        self.assertIn("already have it: video 1", said)

        self.give_words(1)
        code, said = self.cli("run")
        self.assertEqual(code, 0, said)
        self.assertIn("video 1 done: 1 clip(s)", said)

        clip = self.home / "clips" / "1" / "01.mp4"
        info = media.probe(clip)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertTrue(info["has_audio"])
        meta = json.loads(clip.with_suffix(".json").read_text())
        self.assertEqual(meta["rights"], "public-domain")
        self.assertAlmostEqual(info["duration"], meta["end"] - meta["start"], delta=0.2)
        self.assertIn("CLIP #1", (self.home / "clips" / "1" / "README.md").read_text())

        conn = db.connect(self.home / "clipper.db")
        n_cands = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertGreater(n_cands, 1, "the losers are kept too - they are what learning needs")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM candidates WHERE selected = 1").fetchone()[0], 1)
        self.assertEqual(self.cli("run")[1].strip(), "nothing pending")

    def test_a_failing_stage_is_retried_then_parked_then_retryable(self):
        self.cli("source", "add", "pd", "--rights", "public-domain", "--evidence", "x")
        self.cli("ingest", "pd", str(self.video))
        conn = db.connect(self.home / "clipper.db")
        for attempt in range(1, pipeline.MAX_ATTEMPTS + 1):
            code, said = self.cli("run")      # no words file: the transcriber fails
            self.assertEqual(code, 1)
        v = conn.execute("SELECT * FROM videos WHERE id = 1").fetchone()
        self.assertEqual(v["stage"], "failed")
        self.assertTrue(v["error"].startswith("ingested:"), v["error"])
        self.assertTrue((self.home / "media" / "1" / "audio.wav").exists(),
                        "the audio made before the failure is kept, not redone")
        self.assertIn("back at ingested", self.cli("retry", "1")[1])
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- Spotter

from datetime import datetime, timezone  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

from clipper import report, trends  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
TAO = "UC" + "a" * 22          # the creator Clip has permission for
BIG = "UC" + "b" * 22          # a bigger creator Clip has no permission for
TINY = "UC" + "c" * 22         # charting, but under the subscriber floor


def fake_youtube(search_title="He suddenly came to a grove of blossoming peach trees"):
    """The Data API's documented response shapes - counts are strings, a
    chart can be missing for a category. NOT captured from the live API (no
    key on this machine); the first real `spot` on the droplet is the check."""
    calls = []

    def fetch(url):
        ep = url.split("/v3/", 1)[1].split("?", 1)[0]
        q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
        calls.append((ep, q))
        vid = lambda i, ch, title, views, t: {"id": i, "snippet": {
            "channelId": ch, "channelTitle": {TAO: "Tao Reads", BIG: "Mega Streamer",
                                              TINY: "Small One"}.get(ch, "ClipGuy"),
            "title": title, "description": "", "publishedAt": t}, "statistics": {"viewCount": views}}
        if ep == "videos" and q.get("chart"):
            if q["videoCategoryId"] == "17":
                return {"error": {"code": 404, "message": "chart not found",
                                  "errors": [{"reason": "videoChartNotFound"}]}}
            return {"items": [vid("t1", TAO, "a", "240000", "2026-09-28T00:00:00Z"),
                              vid("t2", BIG, "b", "1200000", "2026-09-28T06:00:00Z"),
                              vid("t3", TINY, "c", "999999", "2026-09-28T11:00:00Z")]}
        if ep == "channels":
            stats = {TAO: "2000000", BIG: "30000000", TINY: "40000"}
            return {"items": [{"id": i, "snippet": {"customUrl": "@x"},
                               "statistics": {"subscriberCount": stats[i], "viewCount": "1"}}
                              for i in q["id"].split(",")]}
        if ep == "search":
            name = q["q"]
            return {"items": [{"id": {"kind": "youtube#video", "videoId": "s-" + name[:3]}}]}
        if ep == "videos" and q.get("id"):
            title = search_title if q["id"] == "s-Tao" else "insane moment on stream"
            return {"items": [vid(q["id"], "UC" + "z" * 22, title, "50000", "2026-09-28T02:00:00Z")]}
        raise AssertionError(url)
    return fetch, calls


class SpotterPure(unittest.TestCase):
    def test_counts_arrive_as_strings_and_hidden_ones_are_none(self):
        self.assertEqual(trends.as_int("1234"), 1234)
        self.assertIsNone(trends.as_int(None))

    def test_views_per_hour(self):
        self.assertEqual(trends.views_per_hour(1200, "2026-09-28T00:00:00Z", NOW), 100.0)
        self.assertEqual(trends.views_per_hour(50, "2026-09-28T11:59:00Z", NOW), 50.0,
                         "under an hour old counts as one hour, not a huge rate")

    def test_creators_are_ranked_by_heat_above_the_floor(self):
        v = lambda ch, views, t: {"snippet": {"channelId": ch, "channelTitle": ch, "publishedAt": t},
                                  "statistics": {"viewCount": views}}
        chans = {TAO: {"statistics": {"subscriberCount": "2000000"}},
                 BIG: {"statistics": {"subscriberCount": "30000000"}},
                 TINY: {"statistics": {"subscriberCount": "40000"}},
                 "UChidden": {"statistics": {"hiddenSubscriberCount": True, "subscriberCount": "9000000"}}}
        ranked = trends.rank_creators(
            [v(TAO, "240000", "2026-09-28T00:00:00Z"), v(TAO, "240000", "2026-09-28T00:00:00Z"),
             v(BIG, "120000", "2026-09-28T00:00:00Z"), v(TINY, "9999999", "2026-09-28T11:00:00Z"),
             v("UChidden", "9999999", "2026-09-28T11:00:00Z")], chans, NOW, 500000)
        self.assertEqual([c["channel_id"] for c in ranked], [TAO, BIG],
                         "two fast videos beat one; small and hidden-count channels are left out")
        self.assertEqual(ranked[0]["heat"], 40000.0)
        self.assertEqual(ranked[0]["trending_videos"], 2)


class SpotterMatching(unittest.TestCase):
    """Against the real LibriVox transcript."""

    def setUp(self):
        self.sents = moments.sentences(WORDS, 0.8)

    def test_a_trending_title_finds_its_moment(self):
        sc, i, j = trends.match("He came to a grove of blossoming peach trees 🌸 #shorts", self.sents)
        self.assertGreaterEqual(sc, 0.5)
        self.assertIn("grove of blossoming peach trees", " ".join(s["text"] for s in self.sents[i:j + 1]))

    def test_an_unrelated_title_does_not(self):
        sc, i, j = trends.match("insane clutch play on stream, chat goes wild", self.sents)
        self.assertLess(sc, 0.5)

    def test_the_creators_own_name_is_not_evidence(self):
        # Their name is in every clip title about them; matching on it would
        # "find" any moment where anyone says it.
        sc, *_ = trends.match("LibriVox recording", self.sents, drop={"librivox", "recording"})
        self.assertEqual(sc, 0.0)

    def test_widen_keeps_the_moment_and_the_bounds(self):
        sc, i, j = trends.match("grove of blossoming peach trees", self.sents)
        a, b = trends.widen(self.sents, i, j, 20, 35, 60)
        self.assertLessEqual(a, i)
        self.assertGreaterEqual(b, j)
        dur = self.sents[b]["end"] - self.sents[a]["start"]
        self.assertTrue(20 <= dur <= 60, dur)


class SpotterRun(EndToEnd):
    """Research, a match in permitted footage, and a remake, through the CLI
    and the Deck snapshot. Inherits EndToEnd's 60 s video and settings."""

    test_permitted_video_in_captioned_short_out = None
    test_a_failing_stage_is_retried_then_parked_then_retryable = None

    def ready(self):
        self.cli("source", "add", "tao", "--rights", "permission", "--evidence", "campaign page",
                 "--channel", TAO)
        self.cli("ingest", "tao", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        self.conn = db.connect(self.home / "clipper.db")
        self.cfg = config.load()
        self.paths = config.paths()

    def spot(self, **kw):
        fetch, calls = fake_youtube(**kw)
        yt = trends.YouTube(self.conn, self.cfg, key="k", fetch=fetch)
        return trends.run(self.conn, self.paths, self.cfg, yt=yt, now=NOW), calls

    def test_research_matches_a_trending_moment_in_permitted_footage(self):
        self.ready()
        r, calls = self.spot()
        self.assertEqual(r["creators"], 2)
        searched = [q["q"] for ep, q in calls if ep == "search"]
        self.assertEqual(searched, ["Tao Reads"],
                         "only the permitted creator is searched - the others are leads")
        self.assertEqual(r["matched"], 1)
        snap = report.snapshot(self.conn, self.paths, self.cfg)["spotter"]
        states = {t["creator"]: t["state"] for t in snap["trending"]}
        self.assertEqual(states, {"Tao Reads": "matched"})
        leads = {c["title"]: c["permitted"] for c in snap["creators"]}
        self.assertEqual(leads, {"Tao Reads": True, "Mega Streamer": False})
        units = sum(trends.UNITS[ep] for ep, _ in calls)
        self.assertEqual(trends.units_today(self.conn), units, "every call is on the ledger")
        self.assertEqual(r["units"], units)

    def test_with_no_permitted_creator_nothing_is_searched(self):
        self.cli("source", "add", "pd", "--rights", "public-domain", "--evidence", "x")
        self.conn = db.connect(self.home / "clipper.db")
        self.cfg, self.paths = config.load(), config.paths()
        r, calls = self.spot()
        self.assertFalse(any(ep == "search" for ep, _ in calls), "no quota spent on leads")
        self.assertIn("no permitted creators", " ".join(r["note"]))
        self.assertEqual(r["creators"], 2, "the leads are still listed")

    def test_a_permitted_creator_off_the_charts_is_still_researched(self):
        self.cli("source", "add", "quiet", "--rights", "permission", "--evidence", "x",
                 "--channel", "UC" + "q" * 22)
        self.conn = db.connect(self.home / "clipper.db")
        self.cfg, self.paths = config.load(), config.paths()
        fetch, calls = fake_youtube()
        real = fetch

        def with_quiet(url):
            if "/v3/channels" in url and "UC" + "q" * 22 in url:
                return {"items": [{"id": "UC" + "q" * 22, "snippet": {"title": "Quiet Creator"},
                                   "statistics": {"subscriberCount": "90000"}}]}
            return real(url)
        yt = trends.YouTube(self.conn, self.cfg, key="k", fetch=with_quiet)
        trends.run(self.conn, self.paths, self.cfg, yt=yt, now=NOW)
        self.assertEqual([q["q"] for ep, q in calls if ep == "search"], ["Quiet Creator"])

    def test_the_unit_cap_stops_before_the_call(self):
        self.ready()
        self.cfg["spot_daily_units"] = 105      # 5 charts + 1 channel lookup = 6; a 100-unit search would make 106
        r, calls = self.spot()
        self.assertFalse(any(ep == "search" for ep, _ in calls))
        self.assertIn("units today", " ".join(r["note"]))

    def test_a_bad_key_is_an_error_not_an_empty_result(self):
        self.ready()
        yt = trends.YouTube(self.conn, self.cfg, key="k", fetch=lambda url: {"error": {
            "code": 400, "message": "API key not valid", "errors": [{"reason": "keyInvalid"}]}})
        with self.assertRaises(trends.ApiError):
            trends.run(self.conn, self.paths, self.cfg, yt=yt, now=NOW)

    def test_remake_cuts_our_own_version_and_never_overwrites_a_clip(self):
        self.ready()
        self.spot()
        first = self.home / "clips" / "1" / "01.mp4"
        before = first.stat().st_mtime_ns
        # A trending clip of a creator with no permission (as an older,
        # search-everyone Spotter would have stored) is still refused.
        with self.conn:
            bad = self.conn.execute(
                "INSERT INTO trending_clips (yt_id, creator_id, title, seen_at) VALUES "
                "('old', ?, 'He suddenly came to a grove of blossoming peach trees', ?)",
                (BIG, db.now())).lastrowid
        code, said = self.cli("remake", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("not found in any footage Clip has permission for", said)

        tid = self.conn.execute("SELECT id FROM trending_clips WHERE creator_id = ?", (TAO,)).fetchone()[0]
        code, said = self.cli("remake", str(tid))
        self.assertEqual(code, 0, said)
        clip = self.conn.execute("SELECT * FROM clips ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(clip["rank"], 2, "the next free number, not a number already on disk")
        self.assertEqual(first.stat().st_mtime_ns, before, "clip 01 untouched")
        meta = json.loads(clip["meta"])
        self.assertTrue(meta["reasons"][0].startswith("same moment as a trending clip"))
        self.assertIn("grove", meta["text"])
        self.assertEqual(media.probe(clip["path"])["height"], 1920)
        self.assertIn("already remade", self.cli("remake", str(tid))[1])


class ClipCredentials(unittest.TestCase):
    def test_set_credential_writes_clips_key_beside_its_data(self):
        spec = importlib.util.spec_from_file_location("set_credential_clip", ROOT / "scripts" / "set-credential.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("CLIPPER_HOME")
            os.environ["CLIPPER_HOME"] = d
            try:
                self.assertEqual(m.ELSEWHERE["clip"](), Path(d) / "credentials.env")
                m.write(m.ELSEWHERE["clip"](), "YOUTUBE_API_KEY", "abc123")
                self.assertEqual(trends.api_key(), "abc123")
            finally:
                if old is None:
                    os.environ.pop("CLIPPER_HOME", None)
                else:
                    os.environ["CLIPPER_HOME"] = old

    def test_a_handle_is_not_a_channel_id(self):
        from clipper import ingest
        with self.assertRaisesRegex(ValueError, "not a channel id"):
            ingest.check_channel("@taoreads")
        ingest.check_channel(TAO)


# ---------------------------------------------------------------- posting

from clipper import postcopy, publish, youtube  # noqa: E402

CLIP_TEXT = "Once, while following a stream, he forgot how far he had gone. It lined both banks for 300 paces."


class PostCopyRules(unittest.TestCase):
    def test_a_title_may_not_invent_a_number(self):
        with self.assertRaisesRegex(ValueError, "says 5"):
            postcopy.check_draft({"title": "5 reasons he got lost", "caption": "x"}, CLIP_TEXT)
        t, c, tags = postcopy.check_draft({"title": "300 paces of peach blossom", "caption": "x",
                                           "hashtags": ["#Peach Blossom", "story", "🌸", "story", "a",
                                                        "one", "two", "three", "four"]}, CLIP_TEXT)
        self.assertEqual(t, "300 paces of peach blossom")
        self.assertEqual(tags, ["PeachBlossom", "story", "one", "two", "three"],
                         "cleaned, deduplicated, single letters and emoji dropped, five at most")

    def test_long_text_is_cut_at_a_word(self):
        t = postcopy.cut("word " * 40, 30)
        self.assertLessEqual(len(t), 30)
        self.assertTrue(t.endswith("word…"))

    def test_the_plain_draft_is_the_first_sentence(self):
        self.assertEqual(postcopy.template(CLIP_TEXT)[0],
                         "Once, while following a stream, he forgot how far he had gone.")

    def test_every_post_carries_the_sources_tags_and_credit(self):
        row = {"title": "T", "caption": "C", "hashtags": json.dumps(["story", "Clipping"])}
        src = {"post_tags": "#clipping #TaoReads", "credit": "Clip from @taoreads",
               "attribution": "CC BY Tao"}
        w = postcopy.compose(row, src)
        self.assertEqual(w["tags"], ["clipping", "TaoReads", "story"], "required first, no repeats")
        for text in (w["youtube"]["description"], w["tiktok"]):
            self.assertIn("Clip from @taoreads", text)
            self.assertIn("CC BY Tao", text)
            self.assertIn("#clipping #TaoReads #story", text)
        self.assertIn("#Shorts", w["youtube"]["description"])
        self.assertEqual(w["youtube"]["tags"], ["clipping", "TaoReads", "story"])

    def test_platform_limits(self):
        row = {"title": "T", "caption": "C" * 300, "hashtags": json.dumps(["t" + str(i) * 20 for i in range(40)])}
        src = {"post_tags": None, "credit": "x" * 3000, "attribution": None}
        w = postcopy.compose(row, src)
        self.assertLessEqual(len(w["tiktok"]), postcopy.TIKTOK_MAX)
        self.assertLessEqual(sum(len(t) + 1 for t in w["youtube"]["tags"]), postcopy.YT_TAGS_MAX)


class Posting(EndToEnd):
    """Approve -> the words -> each platform, on a real rendered clip."""

    test_permitted_video_in_captioned_short_out = None
    test_a_failing_stage_is_retried_then_parked_then_retryable = None

    def ready(self):
        self.cli("source", "add", "tao", "--rights", "permission", "--evidence", "campaign page")
        self.cli("source", "rules", "tao", "--tags", "#clipping #TaoReads", "--credit", "Clip from @taoreads")
        self.cli("ingest", "tao", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        self.conn = db.connect(self.home / "clipper.db")
        self.cfg = config.load()
        self.clip = self.conn.execute("SELECT * FROM clips").fetchone()
        self.sent = []

    def upload(self, path, meta, privacy, category):
        self.sent.append((path, meta, privacy, category))
        return {"id": "abc123", "url": "https://youtube.com/shorts/abc123", "privacy": "private"}

    def pubs(self):
        return {r["platform"]: r for r in self.conn.execute(
            "SELECT * FROM publications WHERE clip_id = ?", (self.clip["id"],))}

    def test_the_render_drafts_its_words_without_a_model_in_tests(self):
        self.ready()
        row = self.conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (self.clip["id"],)).fetchone()
        self.assertEqual(row["generator"], "template", "copy_model is null here: no key is ever spent")

    def test_a_model_draft_is_checked_and_costed(self):
        self.ready()
        cfg = dict(self.cfg, copy_model="openai/gpt-5-mini")
        good = lambda text, src, key, slug: ({"title": "He forgot how far he had gone", "caption": "A stream, a grove.",
                                              "hashtags": ["story", "#peach"]}, 0.0012)
        row = postcopy.write(self.conn, cfg, self.clip["id"], redo=True, call=good, key="k", left=0.5)
        self.assertEqual(row["generator"], "llm:openai/gpt-5-mini")
        self.assertEqual(json.loads(row["hashtags"]), ["story", "peach"])
        self.assertAlmostEqual(db.spent_today(self.conn), 0.0012)

        liar = lambda *a: ({"title": "7 secrets of the grove", "caption": "x"}, 0.001)
        row = postcopy.write(self.conn, cfg, self.clip["id"], redo=True, call=liar, key="k", left=0.5)
        self.assertEqual(row["generator"], "template", "an invented number falls back, never posts")

        # A stand-in that raises would prove nothing: write() catches every
        # error and falls back. It records instead, and the list must stay empty.
        calls = []
        never = lambda *a: calls.append(a) or ({"title": "x", "caption": "x"}, 0.0)
        row = postcopy.write(self.conn, cfg, self.clip["id"], redo=True, call=never, key="k", left=0.01)
        self.assertEqual((row["generator"], calls), ("template", []), "shared allowance spent: no call")
        row = postcopy.write(self.conn, dict(cfg, copy_model=None), self.clip["id"], redo=True,
                             call=never, key="k", left=0.5)
        self.assertEqual((row["generator"], calls), ("template", []), "switched off: no call, even with a key")

    def test_your_edit_is_never_overwritten(self):
        self.ready()
        postcopy.edit(self.conn, self.clip["id"], title="My title", hashtags="#mine")
        row = postcopy.write(self.conn, self.cfg, self.clip["id"])
        self.assertEqual((row["title"], row["generator"], json.loads(row["hashtags"])),
                         ("My title", "owner", ["mine"]))

    def test_approve_posts_with_the_composed_words(self):
        self.ready()
        results = publish.approve(self.conn, self.cfg, self.clip["id"], upload=self.upload)
        self.assertEqual(len(self.sent), 1)
        path, meta, privacy, category = self.sent[0]
        self.assertEqual(path, self.clip["path"])
        self.assertIn("Clip from @taoreads", meta["description"])
        self.assertEqual(meta["tags"][:2], ["clipping", "TaoReads"])
        self.assertEqual((privacy, category), ("public", "24"))
        p = self.pubs()
        self.assertEqual((p["youtube"]["status"], p["youtube"]["url"]), ("posted", "https://youtube.com/shorts/abc123"))
        self.assertIn("private until the Google Cloud project passes", p["youtube"]["detail"],
                      "asked public, YouTube kept it private: the page must say so")
        self.assertEqual(p["tiktok"]["status"], "manual")
        clip = lambda: self.conn.execute("SELECT status FROM clips WHERE id = ?", (self.clip["id"],)).fetchone()[0]
        self.assertEqual(clip(), "approved", "TikTok is still waiting for you, so not 'posted'")
        publish.mark_posted(self.conn, self.clip["id"], "tiktok")
        self.assertEqual(clip(), "posted")
        self.assertEqual(self.conn.execute("SELECT verdict FROM reviews").fetchone()[0], "approve")
        with self.assertRaisesRegex(ValueError, "already"):
            publish.approve(self.conn, self.cfg, self.clip["id"], upload=self.upload)
        self.assertEqual(len(self.sent), 1, "never posted twice")

    def test_approve_later_records_now_and_posts_on_the_next_publish(self):
        # 2026-10-07: Approve waited for the upload, Safari gave up first
        # ("Load failed") and the approval was never recorded. --later answers
        # at once; the village then runs `publish` in the background.
        self.ready()
        code, said = self.cli("approve", str(self.clip["id"]), "--later")
        self.assertEqual(code, 0, said)
        self.assertIn("posting in the background", said)
        self.assertEqual(self.sent, [], "--later must not upload in the request")
        status = self.conn.execute("SELECT status FROM clips WHERE id = ?", (self.clip["id"],)).fetchone()[0]
        self.assertEqual((status, self.pubs()["youtube"]["status"]), ("approved", "queued"))
        publish.publish(self.conn, self.cfg, upload=self.upload)
        self.assertEqual((len(self.sent), self.pubs()["youtube"]["status"]), (1, "posted"))
        # `clipper clips` shows the id approve takes, and where each post stands.
        said = self.cli("clips")[1]
        self.assertIn(f"CLIP {self.clip['id']} (v", said)
        self.assertIn("post: youtube posted - https://youtube.com/shorts/abc123", said)
        self.assertIn("post: tiktok manual - TikTok posting needs", said)

    def test_publish_holds_the_lock_while_it_uploads(self):
        # Two quick approvals start two background publishes; without the lock
        # both find the same queued post and upload it twice.
        self.ready()
        held = []
        def check(*a):
            with open(config.home() / "publish.lock", "w") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                    fcntl.flock(f, fcntl.LOCK_UN)
                except BlockingIOError:
                    held.append(True)
            return self.upload(*a)
        publish.approve(self.conn, self.cfg, self.clip["id"], upload=check)
        self.assertEqual(held, [True], "another publish could have started this upload too")
        with open(config.home() / "publish.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)      # released afterwards

    def test_not_signed_in_waits_then_goes_out(self):
        self.ready()
        def not_yet(*a):
            raise youtube.NotSetUp("YouTube is not set up")
        publish.approve(self.conn, self.cfg, self.clip["id"], upload=not_yet)
        self.assertEqual(self.pubs()["youtube"]["status"], "needs_setup")
        publish.publish(self.conn, self.cfg, upload=self.upload)
        self.assertEqual(self.pubs()["youtube"]["status"], "posted")

    def test_the_daily_cap_holds_a_post_for_tomorrow(self):
        self.ready()
        publish.approve(self.conn, dict(self.cfg, max_daily_uploads=0), self.clip["id"], upload=self.upload)
        self.assertEqual(self.pubs()["youtube"]["status"], "queued")
        self.assertEqual(self.sent, [])

    def test_permission_withdrawn_blocks_the_post(self):
        self.ready()
        with self.conn:
            self.conn.execute("UPDATE sources SET active = 0")
        with self.assertRaisesRegex(ValueError, "permission withdrawn"):
            publish.approve(self.conn, self.cfg, self.clip["id"], upload=self.upload)
        self.assertEqual(self.sent, [])

    def test_reject_needs_a_reason_and_keeps_it(self):
        self.ready()
        with self.assertRaisesRegex(ValueError, "say why"):
            publish.reject(self.conn, self.clip["id"], " ")
        publish.reject(self.conn, self.clip["id"], "starts mid-joke")
        self.assertEqual(self.conn.execute("SELECT verdict, reason FROM reviews").fetchone()[:],
                         ("reject", "starts mid-joke"))
        with self.assertRaisesRegex(ValueError, "rejected"):
            publish.approve(self.conn, self.cfg, self.clip["id"], upload=self.upload)


class YouTubeSignInAndUpload(unittest.TestCase):
    """Against Google's documented request/reply shapes - no Google account
    was available to capture real ones. The first youtube-login is the check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = os.environ.get("CLIPPER_HOME")
        os.environ["CLIPPER_HOME"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("CLIPPER_HOME", None) if self.old is None
                        else os.environ.__setitem__("CLIPPER_HOME", self.old))
        (Path(self.tmp.name) / "credentials.env").write_text(
            "YOUTUBE_CLIENT_ID=cid.apps.googleusercontent.com\nYOUTUBE_CLIENT_SECRET=sec\n")

    def test_the_device_flow_waits_slows_down_and_saves_the_token(self):
        replies = iter([(200, {"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 5,
                               "expires_in": 1800, "verification_url": "https://www.google.com/device"}),
                        (428, {"error": "authorization_pending"}),
                        (428, {"error": "slow_down"}),
                        (200, {"access_token": "at", "refresh_token": "rt-123"})])
        posted, slept, said = [], [], []
        ok = youtube.login(post=lambda url, f: (posted.append((url, f)), next(replies))[1],
                           sleep=slept.append, say=said.append)
        self.assertTrue(ok)
        self.assertEqual(posted[0][1]["scope"], "https://www.googleapis.com/auth/youtube")
        self.assertEqual(posted[1][1]["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")
        self.assertEqual(slept, [5, 5, 10], "slow_down adds five seconds")
        self.assertIn("ABCD-EFGH", said[0])
        self.assertEqual(youtube.creds()["YOUTUBE_REFRESH_TOKEN"], "rt-123")
        self.assertEqual(oct((Path(self.tmp.name) / "credentials.env").stat().st_mode & 0o777), "0o600")
        self.assertEqual(youtube.creds()["YOUTUBE_CLIENT_SECRET"], "sec", "other keys kept")

    def test_declining_says_so(self):
        replies = iter([(200, {"device_code": "dc", "user_code": "X", "interval": 1}),
                        (403, {"error": "access_denied"})])
        with self.assertRaisesRegex(RuntimeError, "declined"):
            youtube.login(post=lambda u, f: next(replies), sleep=lambda s: None, say=lambda s: None)

    def test_a_revoked_sign_in_asks_for_a_new_one(self):
        with open(Path(self.tmp.name) / "credentials.env", "a") as f:
            f.write("YOUTUBE_REFRESH_TOKEN=old\n")
        with self.assertRaisesRegex(youtube.NotSetUp, "youtube-login"):
            youtube.access_token(post=lambda u, f: (400, {"error": "invalid_grant"}))

    def test_upload_is_the_resumable_two_step(self):
        video = Path(self.tmp.name) / "c.mp4"
        video.write_bytes(b"x" * 1234)
        seen = []

        def send(req, timeout):
            seen.append(req)
            if req.get_method() == "POST":
                return 200, {"Location": "https://upload.example/session1"}, b""
            return 200, {}, json.dumps({"id": "vid9", "status": {"privacyStatus": "private"}}).encode()
        out = youtube.upload(video, {"title": "T", "description": "D", "tags": ["a"]}, "public", "24",
                             token="tok", send=send)
        self.assertEqual(out, {"id": "vid9", "url": "https://youtube.com/shorts/vid9", "privacy": "private"})
        first = json.loads(seen[0].data)
        self.assertEqual(first["snippet"]["title"], "T")
        self.assertEqual(first["status"]["privacyStatus"], "public")
        self.assertEqual(seen[0].get_header("X-upload-content-length"), "1234")
        self.assertEqual(seen[1].full_url, "https://upload.example/session1")

    def test_a_refused_upload_is_an_error_with_googles_words(self):
        video = Path(self.tmp.name) / "c.mp4"
        video.write_bytes(b"x")
        refuse = lambda req, t: (403, {}, json.dumps({"error": {"message": "quotaExceeded"}}).encode())
        with self.assertRaisesRegex(RuntimeError, "quotaExceeded"):
            youtube.upload(video, {"title": "T", "description": "D", "tags": []}, "public", "24",
                           token="tok", send=refuse)


# ---------------------------------------------------------------- Curious Mike's rules

from clipper import ingest  # noqa: E402


def pixel(video, t, x, y):
    """(r, g, b) of one pixel of one frame, straight from ffmpeg."""
    # 2x2 at even co-ordinates: a yuv420p frame has one colour sample per 2x2
    # block, and a 1x1 crop of it has no colour at all (ffmpeg refuses it).
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                        "-frames:v", "1", "-vf", f"crop=2:2:{x // 2 * 2}:{y // 2 * 2}", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    return tuple(r.stdout[:3])


def close(a, b, tol=40):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


MAGENTA = (255, 0, 255)


def half_magenta_png(path, w=400, h=100):
    """Left half solid magenta, right half fully transparent: shows both that
    the mark is drawn and that its transparency is kept (a lost alpha channel
    turns the right half black - a changed watermark)."""
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"color=c=magenta:s={w}x{h},format=rgba",
                    "-vf", f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(X,{w // 2}),255,0)'",
                    "-frames:v", "1", str(path)], check=True)
    return path


class WatermarkPlace(unittest.TestCase):
    """Rule 6: the mark goes where no overlay covers it, at its own shape."""

    def test_centred_at_its_own_size(self):
        w, h, x, y = render.watermark_box(400, 100, 300, 600)
        self.assertEqual((w, h, y), (400, 100, 300))
        self.assertEqual(x + w / 2, render.W / 2, "centred - never a corner")

    def test_too_wide_is_shrunk_evenly_never_stretched(self):
        w, h, x, y = render.watermark_box(1200, 300, 300, 600)
        self.assertEqual((w, h), (600, 150))
        self.assertLessEqual(x + w, render.RIGHT_CLEAR, "clear of TikTok's buttons")

    def test_refuses_the_status_bar_and_the_captions(self):
        with self.assertRaisesRegex(ValueError, "status bar"):
            render.watermark_box(400, 100, 100, 600)
        with self.assertRaisesRegex(ValueError, "into the captions"):
            render.watermark_box(400, 100, render.CAPTION_TOP - 50, 600)
        with self.assertRaisesRegex(ValueError, "buttons"):
            render.watermark_box(1000, 50, 300, 2000)

    def test_the_caption_band_comes_from_the_captions(self):
        # Captions sit MARGIN_V up from the bottom; the band must start above
        # that by at least one line of the largest style, so a mark placed just
        # above it is not under a word.
        self.assertLess(render.CAPTION_TOP, render.H - captions.MARGIN_V
                        - max(s["size"] for s in captions.STYLES.values()))
        render.watermark_box(400, 100, config.DEFAULTS["watermark_top"],
                             config.DEFAULTS["watermark_max_width"])   # the default spot is legal


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class WatermarkRender(unittest.TestCase):
    """The mark is in the rendered file, first frame to last, unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.src = d / "src.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "color=c=0x303030:s=640x360:r=30",
                        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000",
                        "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        str(cls.src)], check=True)
        cls.png = half_magenta_png(d / "mark.png")
        cls.cfg = dict(CFG, x264_preset="ultrafast")
        cls.out = render.render(cls.src, d / "out" / "01.mp4", 2.0, 6.0, captions.build([], 2.0, 6.0), cls.cfg, watermark=cls.png)
        cls.plain = render.render(cls.src, d / "plain" / "01.mp4", 2.0, 6.0, captions.build([], 2.0, 6.0), cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_on_screen_for_the_whole_clip(self):
        w, h, x, y = render.watermark_box(400, 100, self.cfg["watermark_top"],
                                          self.cfg["watermark_max_width"])
        clip = Path(self.tmp.name) / "out" / "01.mp4"
        for t in (0.0, 1.9, self.out["duration"] - 0.1):
            self.assertTrue(close(pixel(clip, t, x + w // 4, y + h // 2), MAGENTA),
                            f"watermark missing at {t:.1f}s")

    def test_its_transparency_is_kept(self):
        w, h, x, y = render.watermark_box(400, 100, self.cfg["watermark_top"],
                                          self.cfg["watermark_max_width"])
        here = (x + 3 * w // 4, y + h // 2)
        self.assertTrue(close(pixel(Path(self.tmp.name) / "out" / "01.mp4", 1.0, *here),
                              pixel(Path(self.tmp.name) / "plain" / "01.mp4", 1.0, *here), tol=12),
                        "the transparent half shows the video, not a black box")

    def test_the_image_does_not_shorten_the_clip(self):
        # -t read as the image's length (an -i after it) or the image ending
        # the overlay would cut the clip to one frame.
        self.assertAlmostEqual(self.out["duration"], 4.0, delta=0.15)
        self.assertAlmostEqual(self.out["duration"], self.plain["duration"], delta=0.05)

    def test_only_the_png_the_campaign_supplied(self):
        jpg = Path(self.tmp.name) / "mark.jpg"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(self.png), str(jpg)], check=True)
        with self.assertRaisesRegex(ValueError, "PNG"):
            render.check_watermark(jpg, self.cfg)
        with self.assertRaisesRegex(media.MediaError, "missing"):
            render.check_watermark(Path(self.tmp.name) / "gone.png", self.cfg)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class WatermarkEverywhere(EndToEnd):
    """Through the CLI: a source's watermark is on every clip it makes, and a
    missing watermark stops the render rather than make an unpaid clip."""

    def setUp(self):
        super().setUp()
        self.png = half_magenta_png(Path(self.tmp.name) / "yt-mpj.png")
        self.cli("source", "add", "mike", "--rights", "permission", "--evidence", "campaign page")

    def test_every_clip_carries_it(self):
        code, said = self.cli("source", "rules", "mike", "--watermark", str(self.png))
        self.assertEqual(code, 0, said)
        self.assertIn("watermark kept: 400x100", said)
        self.png.unlink()                      # the source keeps its own copy
        self.cli("ingest", "mike", str(self.video))
        self.give_words(1)
        code, said = self.cli("run")
        self.assertEqual(code, 0, said)
        clip = self.home / "clips" / "1" / "01.mp4"
        self.assertEqual(json.loads(clip.with_suffix(".json").read_text())["watermark"], "mike.png")
        w, h, x, y = render.watermark_box(400, 100, 300, 600)
        self.assertTrue(close(pixel(clip, 0.5, x + 50, y + 50), MAGENTA))

    def test_a_lost_watermark_stops_the_render(self):
        self.cli("source", "rules", "mike", "--watermark", str(self.png))
        (self.home / "watermarks" / "mike.png").unlink()
        self.cli("ingest", "mike", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 1)
        self.assertFalse((self.home / "clips" / "1" / "01.mp4").exists(), "no clip without the mark")
        err = db.connect(self.home / "clipper.db").execute("SELECT error FROM videos").fetchone()[0]
        self.assertIn("watermark", err)

    def test_a_bad_file_is_refused_when_set_not_hours_later(self):
        bad = Path(self.tmp.name) / "notes.txt"
        bad.write_text("YT: @mpj")
        code, said = self.cli("source", "rules", "mike", "--watermark", str(bad))
        self.assertEqual(code, 2)
        self.assertFalse(list((self.home / "watermarks").glob("*")), "nothing half-kept")


class DropboxLinks(unittest.TestCase):
    # The shape of a Dropbox share link as copied from the app in 2026: a
    # /scl/fi/ path, an rlkey, an st, and dl=0 (the preview page, not the file).
    LINK = ("https://www.dropbox.com/scl/fi/jx035jv80zkn4oh835who/CURIOUS-MIKE-TRAE-YOUNG.mp4"
            "?rlkey=abc123def456&st=xyz789&dl=0")

    def test_a_share_link_becomes_the_file(self):
        got = urllib.parse.urlsplit(ingest.direct(self.LINK))
        q = dict(urllib.parse.parse_qsl(got.query))
        self.assertEqual(q["dl"], "1")
        self.assertEqual((q["rlkey"], q["st"]), ("abc123def456", "xyz789"), "the key is kept")
        self.assertEqual(got.path, "/scl/fi/jx035jv80zkn4oh835who/CURIOUS-MIKE-TRAE-YOUNG.mp4")
        self.assertEqual(ingest.direct(self.LINK.replace("&dl=0", ""))[-4:], "dl=1")

    def test_a_folder_link_is_refused(self):
        with self.assertRaisesRegex(ValueError, "folder"):
            ingest.direct("https://www.dropbox.com/scl/fo/abc/def?rlkey=x&dl=0")

    def test_other_links_are_left_alone(self):
        u = "https://example.com/video.mp4?dl=0"
        self.assertEqual(ingest.direct(u), u)


class CaptionsAskAQuestion(unittest.TestCase):
    """Rule 2: comments count, and a question is what gets them."""

    def test_a_draft_without_a_question_gets_one(self):
        _, c, _ = postcopy.check_draft({"title": "Peach blossoms", "caption": "He got lost."}, CLIP_TEXT)
        self.assertEqual(c, "He got lost. " + postcopy.QUESTION)
        self.assertTrue(postcopy.template(CLIP_TEXT)[1].endswith("?"))

    def test_a_question_already_there_is_left_alone(self):
        _, c, _ = postcopy.check_draft({"title": "T", "caption": "Would you have kept walking?"}, CLIP_TEXT)
        self.assertEqual(c, "Would you have kept walking?")

    def test_still_within_the_limit(self):
        _, c, _ = postcopy.check_draft({"title": "T", "caption": "word " * 100}, CLIP_TEXT)
        self.assertLessEqual(len(c), postcopy.CAPTION_MAX)
        self.assertTrue(c.endswith(postcopy.QUESTION))


class WatermarkMustBeSeeThrough(unittest.TestCase):
    # Captured 2026-09-29 from the Curious Mike guidelines page on Notion:
    # "YT: @mpj" in off-white with a grey glow, on a solid white 1568x523
    # canvas, no alpha. Overlaid as it is, it is a white box over the video.
    FLAT = FIXTURES / "clipper-watermark-notion-flattened.png"

    def test_the_flattened_campaign_image_is_refused(self):
        self.assertFalse(render.has_transparency(self.FLAT))
        with self.assertRaisesRegex(ValueError, "transparent"):
            render.check_watermark(self.FLAT, CFG)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_real_alpha_channel_is_recognised(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(render.has_transparency(half_magenta_png(Path(d) / "m.png")))
            pal = Path(d) / "pal.png"     # palette PNG, transparency in a tRNS chunk
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(Path(d) / "m.png"),
                            "-vf", "split[a][b];[a]palettegen=reserve_transparent=1[p];[b][p]paletteuse",
                            "-frames:v", "1", str(pal)], check=True)
            self.assertTrue(render.has_transparency(pal))
            render.check_watermark(pal, CFG)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("openssl"), "needs ffmpeg and openssl")
class RemoteFootage(EndToEnd):
    """ingest --remote: a video too big for the disk (a 27 GB 4K episode on a
    droplet with 44 GB free) is left on the rights holder's server, read over
    https with its certificate checked, and never copied."""

    def setUp(self):
        super().setUp()
        import http.server
        import ssl
        import threading
        d = Path(self.tmp.name)
        self.cert = d / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(d / "key.pem"), "-out", str(self.cert)],
                       check=True, capture_output=True)
        serve = str(d)

        test = self

        class Quiet(http.server.SimpleHTTPRequestHandler):
            """Answers Range requests the way Dropbox does (206 and the slice);
            the standard handler ignores them and sends everything."""
            def __init__(self, *a, **k):
                super().__init__(*a, directory=serve, **k)

            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                if not (rng and test.ranges):
                    return super().do_GET()
                path = Path(self.translate_path(self.path))
                size = path.stat().st_size
                a, _, b = rng.split("=", 1)[1].partition("-")
                a, b = int(a), min(int(b) if b else size - 1, size - 1)
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
                self.send_header("Content-Length", str(b - a + 1))
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(a)
                    try:
                        self.wfile.write(f.read(b - a + 1))
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self.ranges = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, d / "key.pem")
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}/talk.mp4?rlkey=k&st=one&dl=0"
        old = os.environ.get("SSL_CERT_FILE")
        self.trust(self.cert)
        self.addCleanup(lambda: self.trust(old))
        self.cli("source", "add", "mike", "--rights", "permission", "--evidence", "campaign page")

    @staticmethod
    def trust(cert):
        """Trust only this certificate for the rest of the process. From
        Python 3.12 urllib's shared opener builds its TLS context once and
        keeps it, so changing SSL_CERT_FILE alone left the previous test's
        certificate in force: CI failed test_clipped_without_a_copy with
        "self-signed certificate" straight after the untrusted-certificate
        test. Each Clip command on the droplet is a fresh process, so this
        is the tests' problem, not Clip's; dropping the opener fixes it."""
        if cert:
            os.environ["SSL_CERT_FILE"] = str(cert)
        else:
            os.environ.pop("SSL_CERT_FILE", None)
        urllib.request.install_opener(None)

    def test_clipped_without_a_copy(self):
        code, said = self.cli("ingest", "mike", self.url, "--remote")
        self.assertEqual(code, 0, said)
        self.assertIn("left in place", said)
        v = db.connect(self.home / "clipper.db").execute("SELECT * FROM videos").fetchone()
        self.assertEqual(v["media_path"], self.url, "kept as a link (dl=1 is for Dropbox hosts only)")
        self.assertEqual(list((self.home / "media" / "1").iterdir()), [], "nothing downloaded")
        code, said = self.cli("ingest", "mike", self.url.replace("st=one", "st=two"), "--remote")
        self.assertIn("already have it", said, "a re-copied link is the same video")
        self.give_words(1)
        code, said = self.cli("run")
        self.assertEqual(code, 0, said)
        clip = self.home / "clips" / "1" / "01.mp4"
        meta = json.loads(clip.with_suffix(".json").read_text())
        self.assertAlmostEqual(media.probe(clip)["duration"], meta["end"] - meta["start"], delta=0.2)
        self.assertFalse((self.home / "media" / "1" / "source.mp4").exists())

    def test_an_untrusted_certificate_is_refused(self):
        other = Path(self.tmp.name) / "other.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=someone-else", "-keyout", str(Path(self.tmp.name) / "k2.pem"),
                        "-out", str(other)], check=True, capture_output=True)
        self.trust(other)
        # ffmpeg itself, not only the range check before it: ffmpeg checks no
        # certificate unless told to, and every clip is read through it.
        with self.assertRaises(media.MediaError):
            media.probe(self.url)
        code, said = self.cli("ingest", "mike", self.url, "--remote")
        self.assertEqual(code, 2, said)
        self.assertEqual(db.connect(self.home / "clipper.db").execute(
            "SELECT COUNT(*) FROM videos").fetchone()[0], 0)

    def test_every_read_of_a_clip_checks_the_certificate(self):
        # The probe before a render checks it too, so a render command that
        # dropped the check would still pass every other test here.
        info = {"width": 640, "height": 360}
        cmd = render.command(self.url, "o.mp4", 1, 5, info, "o.ass", CFG)
        before = cmd[:cmd.index(self.url)]
        self.assertEqual(before[before.index("-tls_verify") + 1], "1")
        self.assertIn("-ca_file", before)
        local = render.command("talk.mp4", "o.mp4", 1, 5, info, "o.ass", CFG)
        self.assertIn(str(Path("talk.mp4").resolve()), local,
                      "a local path is made absolute - ffmpeg runs in the clip's folder")

    def test_a_server_that_ignores_ranges_is_refused(self):
        self.ranges = False
        code, said = self.cli("ingest", "mike", self.url, "--remote")
        self.assertEqual(code, 2, said)
        self.assertIn("whole file", said)

    def test_only_https(self):
        code, said = self.cli("ingest", "mike", self.url.replace("https", "http"), "--remote")
        self.assertEqual(code, 2)
        self.assertIn("https", said)

    def test_a_too_big_download_says_how_to_leave_it_in_place(self):
        with self.assertRaisesRegex(RuntimeError, "--remote"):
            ingest.need_space(self.home, 10 ** 15, ingest.REMOTE_HINT)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class WatermarkCutOutOfWhite(unittest.TestCase):
    """The campaign's own pixels, minus the white canvas - not a retyped copy,
    which its rules forbid, and not a white box over the clip."""
    FLAT = WatermarkMustBeSeeThrough.FLAT

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "cut.png"

    def rgb(self, path, over, w, h):
        vf = f"color=c={over}:s={w}x{h},format=rgb24[bg];[bg][0:v]overlay=format=rgb,format=rgb24"
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-filter_complex", vf,
                            "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True, check=True)
        return r.stdout

    def test_over_white_it_is_the_supplied_file(self):
        from clipper import cutout
        w, h, (x0, y0) = cutout.cut_out_white(self.FLAT, self.out)
        self.assertTrue(render.has_transparency(self.out))
        mine = self.rgb(self.out, "white", w, h)
        orig = subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.FLAT), "-vf",
                               f"crop={w}:{h}:{x0}:{y0},format=rgb24", "-f", "rawvideo", "-"],
                              capture_output=True, check=True).stdout
        self.assertEqual(len(mine), len(orig))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(mine, orig)), 1,
                             "no pixel of the mark changed: over white it is the original")

    def test_the_letters_are_solid_and_the_canvas_is_gone(self):
        from clipper import cutout
        w, h, _ = cutout.cut_out_white(self.FLAT, self.out)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.out), "-f", "rawvideo",
                              "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout
        alpha = raw[3::4]
        self.assertGreater(sum(1 for a in alpha if a == 255), 20000,
                           "the nine glyph parts (about 21,000 pixels) stay fully opaque")
        for corner in (0, w - 1, (h - 1) * w, h * w - 1):
            self.assertLessEqual(alpha[corner], cutout.TRIM_ALPHA + 1)
        self.assertLess((w, h), (1568, 523), "the empty canvas is trimmed")
        render.watermark_box(w, h, 880, 600)   # fits low-middle, clear of the captions

    def test_a_mark_not_on_white_is_refused(self):
        from clipper import cutout
        with self.assertRaisesRegex(ValueError, "not white"):
            cutout.cut_out_white(half_magenta_png(Path(self.tmp.name) / "m.png"), self.out)
        blank = Path(self.tmp.name) / "blank.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=300x100",
                        "-frames:v", "1", str(blank)], check=True)
        with self.assertRaisesRegex(ValueError, "no lettering"):
            cutout.cut_out_white(blank, self.out)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class WatermarkPerSource(EndToEnd):
    def setUp(self):
        super().setUp()
        self.cli("source", "add", "mike", "--rights", "permission", "--evidence", "campaign page")

    def test_the_flattened_campaign_file_through_the_cli(self):
        flat = str(WatermarkMustBeSeeThrough.FLAT)
        code, said = self.cli("source", "rules", "mike", "--watermark", flat)
        self.assertEqual(code, 2)
        self.assertIn("--cut-out-white", said, "the refusal says what to do")
        code, said = self.cli("source", "rules", "mike", "--watermark", flat, "--cut-out-white",
                              "--watermark-top", "880")
        self.assertEqual(code, 0, said)
        self.assertIn("watermark kept: 543x162, centred at 268,880", said)
        self.assertTrue(render.has_transparency(self.home / "watermarks" / "mike.png"))
        self.assertEqual(sorted(p.name for p in (self.home / "watermarks").iterdir()), ["mike.png"])

    def test_replacing_a_flattened_watermark_along_with_its_position(self):
        # The droplet's real order of events: the Notion image was stored by
        # the first version (before transparency was checked), then replaced
        # with --cut-out-white and --watermark-top in one command - which was
        # refused, because the position was checked against the old file.
        old = self.home / "watermarks" / "mike.png"
        old.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(WatermarkMustBeSeeThrough.FLAT, old)
        with db.connect(self.home / "clipper.db") as conn:
            conn.execute("UPDATE sources SET watermark = ? WHERE name = 'mike'", (str(old),))
        code, said = self.cli("source", "rules", "mike", "--watermark",
                              str(WatermarkMustBeSeeThrough.FLAT), "--cut-out-white",
                              "--watermark-top", "880")
        self.assertEqual(code, 0, said)
        self.assertIn("centred at 268,880", said)
        s = db.connect(self.home / "clipper.db").execute("SELECT * FROM sources").fetchone()
        self.assertEqual(s["watermark_top"], 880)
        self.assertTrue(render.has_transparency(s["watermark"]))

    def test_a_position_under_the_captions_is_refused(self):
        self.cli("source", "rules", "mike", "--watermark", str(WatermarkMustBeSeeThrough.FLAT),
                 "--cut-out-white")
        code, said = self.cli("source", "rules", "mike", "--watermark-top", "1100")
        self.assertEqual(code, 2)
        self.assertIn("captions", said)
        top = db.connect(self.home / "clipper.db").execute(
            "SELECT watermark_top FROM sources").fetchone()[0]
        self.assertIsNone(top, "a refused position is not kept")

    def test_clips_use_the_sources_own_position(self):
        png = half_magenta_png(Path(self.tmp.name) / "m.png")
        self.assertEqual(self.cli("source", "rules", "mike", "--watermark", str(png),
                                  "--watermark-top", "700")[0], 0)
        self.cli("ingest", "mike", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        clip = self.home / "clips" / "1" / "01.mp4"
        w, h, x, y = render.watermark_box(400, 100, 700, 600)
        self.assertTrue(close(pixel(clip, 0.5, x + 50, y + 50), MAGENTA), "at 700, not the default 300")


# ---------------------------------------------------------------- a campaign's own list

LONG_RUN = json.loads((FIXTURES / "clipper-long-run-timings.json").read_text())["words"]


class LongUnpunctuatedRuns(unittest.TestCase):
    """Whisper wrote 39 of the Trae Young episode's 80 minutes as runs with no
    full stop and (mostly) no gap between words; one ran 525 s. A window is
    whole sentences, so none of it could be clipped."""

    def test_the_real_525_second_run_becomes_clippable(self):
        sents = moments.sentences(LONG_RUN, 0.8)
        self.assertGreater(len(sents), 17)
        self.assertLessEqual(max(s["end"] - s["start"] for s in sents), moments.LONGEST)
        self.assertTrue(all(s["last"] - s["first"] + 1 >= moments.MIN_PIECE_WORDS for s in sents))
        self.assertEqual([k for s in sents for k in range(s["first"], s["last"] + 1)],
                         list(range(len(LONG_RUN))), "every word once, in order")
        wins = moments.windows(sents, 20, 60)
        covered = {int(t) for i, j in wins for t in range(int(sents[i]["start"]), int(sents[j]["end"]))}
        self.assertGreater(len(covered), 0.95 * (LONG_RUN[-1]["end"] - LONG_RUN[0]["start"]))

    def test_a_real_point_eight_second_pause_is_a_pause(self):
        # The run's one 0.8 s gap is 0.79999999999972 in floating point.
        k = max(range(len(LONG_RUN) - 1), key=lambda k: LONG_RUN[k + 1]["start"] - LONG_RUN[k]["end"])
        self.assertLess(LONG_RUN[k + 1]["start"] - LONG_RUN[k]["end"], 0.8)
        sents = moments.sentences(LONG_RUN, 0.8, longest=10 ** 6)     # pauses only
        self.assertIn(k, [s["last"] for s in sents])

    def test_with_no_gaps_it_splits_near_the_middle(self):
        # The real runs are 99% zero gaps: a tie must not cut three words off
        # the front, over and over, into a string of useless fragments.
        ws = [w(t, t + 1.0, "x") for t in range(0, 80)]
        pieces = moments.split_long(ws, list(range(80)), 50)
        self.assertEqual(len(pieces), 2, [len(p) for p in pieces])
        self.assertLessEqual(abs(len(pieces[0]) - len(pieces[1])), 2, [len(p) for p in pieces])

    def test_splits_at_the_biggest_gap(self):
        ws = [w(t, t + 0.5, "x") for t in range(0, 40)]
        ws[25] = w(25.4, 25.9, "x")                      # 0.9 s after word 24, the one gap
        pieces = moments.split_long(ws, list(range(40)), 30)
        self.assertEqual(pieces[0][-1], 24)


class CampaignTopics(unittest.TestCase):
    HUNT = "Knicks: knicks, nicks, chant; Pat Beverley: pat bev, beverly; dunk"

    def test_parse_keeps_the_campaigns_order(self):
        h = score.parse_hunt(self.HUNT)
        self.assertEqual([t["name"] for t in h], ["Knicks", "Pat Beverley", "dunk"])
        self.assertEqual(h[1]["terms"], ["pat bev", "beverly"])

    def test_whole_words_with_endings(self):
        h = score.parse_hunt(self.HUNT + "; AI: ai")
        self.assertEqual(score.topics("they started chanting at the Knicks' game", h), ["Knicks"])
        self.assertEqual(score.topics("he was dunking on people", h), ["dunk"])
        self.assertEqual(score.topics("I said it to Nick's brother", h), [], "nick is not nicks; ai not in said")
        self.assertEqual(score.topics("the beef with you and pat bev", h), ["Pat Beverley"])
        self.assertEqual(score.topics("the knicks won", score.parse_hunt("Nick: nicks")), [],
                         "a term inside a longer word is not a mention")

    def words_saying(self, text, at=0.0, step=1.0):
        return [w(at + k * step, at + k * step + 0.9, " " + t + ("." if k % 6 == 5 else ""))
                for k, t in enumerate(text.split())]

    def test_a_mention_at_the_very_end_does_not_count(self):
        # The real case: the best "Knicks" window ended just after "the nicks",
        # before the story. 30 words, the topic word last.
        ws = self.words_saying(" ".join(["talk"] * 29 + ["knicks"]))
        sents = moments.sentences(ws, 0.8)
        got = score.score_windows(sents, [(0, len(sents) - 1)], ws, [], CFG, score.parse_hunt(self.HUNT))
        self.assertEqual(got[0]["topics"], [])
        ws = self.words_saying(" ".join(["talk"] * 18 + ["knicks"] + ["talk"] * 11))
        sents = moments.sentences(ws, 0.8)
        got = score.score_windows(sents, [(0, len(sents) - 1)], ws, [], CFG, score.parse_hunt(self.HUNT))
        self.assertEqual(got[0]["topics"], [], "60% of the way in: the clip ends on its setup")
        ws = self.words_saying(" ".join(["talk"] * 5 + ["knicks"] + ["talk"] * 24))
        sents = moments.sentences(ws, 0.8)
        got = score.score_windows(sents, [(0, len(sents) - 1)], ws, [], CFG, score.parse_hunt(self.HUNT))
        self.assertEqual(got[0]["topics"], ["Knicks"])
        plain = score.score_windows(sents, [(0, len(sents) - 1)], ws, [], CFG)[0]["score"]
        self.assertEqual(got[0]["score"], min(100.0, round(plain + score.TOPIC_BONUS, 1)))
        self.assertIn("on the campaign's list: Knicks", got[0]["reasons"][0])

    def test_a_clip_opening_on_like_is_mid_thought(self):
        # The real case: "like in the first quarter ... they started chanting"
        # won over the window that opened on the setup, until "like" counted.
        for opener, standalone in (("like", 0.25), ("my", 1.0)):
            ws = self.words_saying(opener + " first playoff series was the knicks and they chanted")
            sents = moments.sentences(ws, 0.8)
            f = score.score_windows(sents, [(0, len(sents) - 1)], ws, [], CFG)[0]["features"]
            self.assertEqual(f["standalone"], standalone, opener)

    def test_every_topic_gets_a_clip_before_the_rest_go_by_score(self):
        h = score.parse_hunt(self.HUNT)
        c = lambda s, e, sc, t: {"start": s, "end": e, "score": sc, "topics": t}
        scored = [c(0, 30, 99, ["Knicks"]), c(40, 70, 98, ["Knicks"]), c(80, 110, 97, ["Knicks"]),
                  c(120, 150, 60, ["dunk"]), c(160, 190, 90, []), c(125, 155, 95, ["Knicks"])]
        got = moments.choose(scored, 3, 40, h)
        self.assertEqual(sorted((x["start"] for x in got)), [0, 40, 120],
                         "Knicks' best, then dunk's best (not a fourth Knicks), then by score")
        self.assertEqual([x["start"] for x in moments.choose(scored, 3, 40)], [0, 40, 80],
                         "without a list: score alone")


class NeverClipped(unittest.TestCase):
    """Owner, 2026-10-07, before the first Gil's Arena episode: how do we tell
    Clip what it should or shouldn't clip? A sponsor read - on a sports show
    often a betting app - is never a clip, and the owner can name more."""

    def words_saying(self, text):
        return [w(k * 1.0, k * 1.0 + 0.9, " " + t + ("." if k % 6 == 5 else ""))
                for k, t in enumerate(text.split())]

    def scored(self, text, avoid=()):
        ws = self.words_saying(text)
        sents = moments.sentences(ws, 0.8)
        return score.score_windows(sents, [(0, len(sents) - 1)], ws, [], CFG,
                                   score.parse_hunt("Knicks: knicks"), avoid)[0]

    def test_a_sponsor_read_scores_zero_even_on_a_topic(self):
        got = self.scored("the knicks are rolling and this episode is brought to you by our friends "
                          "so use code GIL for your first deposit match today")
        self.assertEqual((got["score"], got["topics"]), (0.0, []))
        self.assertIn("(a sponsor read)", got["reasons"][0])
        self.assertEqual(moments.choose([got], 3, 1, score.parse_hunt("Knicks: knicks")), [],
                         "not even as the topic's pick")

    def test_talking_about_a_sportsbook_is_not_an_ad(self):
        got = self.scored("fanduel had the knicks as big favourites and everybody lost money on that one")
        self.assertGreater(got["score"], 0)

    def test_the_owners_own_list(self):
        avoid = score.parse_avoid("politics, ex-wife; betting odds")
        self.assertEqual(avoid, ["politics", "ex-wife", "betting odds"])
        got = self.scored("we are not doing politics on this show but the knicks need a center", avoid)
        self.assertEqual(got["score"], 0.0)
        self.assertIn("'politics' (on the avoid list)", got["reasons"][0])
        self.assertIsNone(score.avoided("the knicks were better between the two halves", ["bet"]),
                          "whole words: better and between are not bet")
        self.assertEqual(score.avoided("he bets on the knicks", ["bet"]), "bet", "an ending still counts")


class Spellings(unittest.TestCase):
    RULES = transcribe.parse_spellings("Trae Young: try young; Trae: tray; Knicks: nicks; "
                                       "chanting F Trae Young: chin fluck try young")

    def test_longest_first(self):
        self.assertEqual(self.RULES[0], [["chin", "fluck", "try", "young"], "chanting F Trae Young"])

    def test_the_real_mishearing_is_fixed_in_step_with_the_audio(self):
        # From the episode at 67:50: "...and like they started chin fluck try young you know"
        ws = [w(0.0, 0.3, " they"), w(0.3, 0.6, " started"), w(0.6, 0.9, " chin"),
              w(0.9, 1.2, " fluck"), w(1.2, 1.5, " try"), w(1.5, 2.0, " young."), w(2.0, 2.3, " you")]
        got = transcribe.respell(ws, self.RULES)
        self.assertEqual("".join(x["word"] for x in got), " they started chanting F Trae Young. you")
        new = got[2:6]
        self.assertEqual((new[0]["start"], new[-1]["end"]), (0.6, 2.0), "the same span of audio")
        self.assertTrue(moments.ends_sentence(new[-1]["word"]), "the full stop moves with it")

    def test_only_whole_listed_phrases(self):
        ws = [w(0, 1, " I'll"), w(1, 2, " try"), w(2, 3, " it,"), w(3, 4, " Tray,"), w(4, 5, " NICKS")]
        self.assertEqual("".join(x["word"] for x in transcribe.respell(ws, self.RULES)),
                         " I'll try it, Trae, Knicks")


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class SourceRulesEndToEnd(EndToEnd):
    """Was a second class named CampaignListEndToEnd, so the later one of that
    name replaced it and none of these ran (found 2026-10-07)."""
    def setUp(self):
        super().setUp()
        self.cli("source", "add", "tao", "--rights", "public-domain", "--evidence", "LibriVox")

    def test_spellings_reach_the_burned_in_captions(self):
        # "the" is in every clip of the fixture; respelling it proves the
        # captions and the scored text both come through the spellings.
        self.assertEqual(self.cli("source", "rules", "tao", "--spell", "Thee: the")[0], 0)
        self.cli("ingest", "tao", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        ass = (self.home / "clips" / "1" / "01.ass").read_text()
        text = json.loads((self.home / "clips" / "1" / "01.json").read_text())["text"]
        self.assertIn("THEE", ass)
        self.assertNotRegex(ass, r"[ }]THE[ {]|[ }]THE$")
        self.assertIn("Thee", text)
        self.assertNotRegex(text, r"\bthe\b")

    def test_one_clip_per_topic_even_over_the_cap(self):
        # max_clips_per_video is 1 here; the list names two moments.
        self.cli("source", "rules", "tao", "--hunt", "LibriVox: librivox; Fisherman: fisherman")
        self.cli("ingest", "tao", str(self.video))
        self.give_words(1)
        code, said = self.cli("run")
        self.assertEqual(code, 0, said)
        self.assertIn("2 clip(s)", said)
        ev = db.connect(self.home / "clipper.db").execute(
            "SELECT msg FROM events WHERE stage = 'find'").fetchone()[0]
        self.assertIn("campaign topics clipped: Fisherman, LibriVox", ev)
        self.assertNotIn("not found", ev)

    def test_an_avoid_list_is_kept_and_cleared(self):
        code, said = self.cli("source", "rules", "tao", "--avoid", "Politics, fisherman")
        self.assertEqual(code, 0, said)
        self.assertIn("never clipping: politics, fisherman", said)
        conn = db.connect(self.home / "clipper.db")
        self.assertEqual(json.loads(conn.execute("SELECT avoid FROM sources").fetchone()[0]),
                         ["politics", "fisherman"])
        self.cli("ingest", "tao", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        rows = conn.execute("SELECT text, score, selected FROM candidates").fetchall()
        named = [r for r in rows if "fisherman" in r[0].lower()]
        self.assertTrue(named, "the fixture must mention it, or this tests nothing")
        self.assertTrue(all(r[1] == 0 and not r[2] for r in named), "an avoided moment is never picked")
        self.assertEqual(self.cli("source", "rules", "tao", "--avoid", "")[0], 0)
        self.assertIsNone(conn.execute("SELECT avoid FROM sources").fetchone()[0])

    def test_a_list_set_after_searching_is_used_by_refind(self):
        self.cli("ingest", "tao", str(self.video))
        self.give_words(1)
        conn = db.connect(self.home / "clipper.db")
        pipeline.advance(conn, config.paths(), config.load(), conn.execute("SELECT * FROM videos").fetchone())
        # (the render made a clip: refind must refuse, it would renumber it)
        code, said = self.cli("refind", "1")
        self.assertEqual(code, 2)
        self.assertIn("renumber", said)
        with conn:
            conn.execute("DELETE FROM post_copy")
            conn.execute("DELETE FROM clips")
            conn.execute("UPDATE videos SET stage = 'found'")
        self.assertEqual(self.cli("source", "rules", "tao", "--hunt", "Blossom: blossom, blossoms")[0], 0)
        self.assertEqual(self.cli("refind", "1")[0], 0)
        self.assertEqual(conn.execute("SELECT stage FROM videos").fetchone()[0], "transcribed")
        self.assertEqual(self.cli("run")[0], 0)
        ev = conn.execute("SELECT msg FROM events WHERE stage = 'find' ORDER BY id DESC").fetchone()[0]
        self.assertIn("campaign topics clipped: Blossom", ev)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class TranscribedInPieces(unittest.TestCase):
    """The whole 81-minute episode at once peaked at 4.9 GB and the droplet's
    kernel killed the run. Pieces of whisper_chunk_seconds, cut in quiet."""

    def test_cuts_land_in_the_quiet_and_cover_everything(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "a.wav"
            quiet = [(9.1, 9.5), (20.6, 21.0), (30.2, 30.6)]
            gate = "*".join(f"(1-between(t,{a},{b}))" for a, b in quiet)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            f"aevalsrc='0.5*sin(2*PI*220*t)*{gate}':s=16000:d=35",
                            "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True)
            b = transcribe.chunks(wav, 10, search=2)
            self.assertEqual(b[0][0], 0)
            self.assertEqual(b[-1][1], 35 * 16000)
            self.assertTrue(all(x[1] == y[0] for x, y in zip(b, b[1:])), "no gap, no overlap")
            for (_, cut), (a, z) in zip(b, quiet):
                self.assertTrue(a <= cut / 16000 <= z, f"cut at {cut / 16000:.2f}s, not in {a}-{z}")

    def test_only_the_wav_extract_audio_writes(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "s.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-ac", "2",
                            "-c:a", "pcm_s16le", str(wav)], check=True)
            with self.assertRaisesRegex(ValueError, "16-bit mono"):
                transcribe.chunks(wav, 10)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class AudioIsNotKept(EndToEnd):
    """The audio is only for transcribing and measuring loudness: 155 MB for
    the 81-minute Trae Young episode, and nothing reads it afterwards."""

    def test_gone_once_transcribed_and_measured(self):
        self.cli("source", "add", "pd", "--rights", "public-domain", "--evidence", "LibriVox")
        self.cli("ingest", "pd", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        f = self.home / "media" / "1"
        self.assertFalse((f / "audio.wav").exists())
        self.assertTrue((f / "transcript.json").exists() and (f / "loudness.json").exists())
        loud = json.loads((f / "loudness.json").read_text())
        self.assertGreater(len(loud), 50, "measured before the audio went")

    def test_a_video_from_before_this_is_measured_again(self):
        # Transcribed by an older Clip: no loudness.json, and no audio either.
        self.cli("source", "add", "pd", "--rights", "public-domain", "--evidence", "LibriVox")
        self.cli("ingest", "pd", str(self.video))
        self.give_words(1)
        conn = db.connect(self.home / "clipper.db")
        pipeline.advance(conn, config.paths(), config.load(), conn.execute("SELECT * FROM videos").fetchone())
        f = self.home / "media" / "1"
        (f / "loudness.json").unlink()
        with conn:
            conn.execute("DELETE FROM post_copy")
            conn.execute("DELETE FROM clips")
            conn.execute("UPDATE videos SET stage = 'transcribed'")
        self.assertEqual(self.cli("run")[0], 0)
        self.assertTrue((f / "loudness.json").exists())


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class ForgetATestVideo(EndToEnd):
    """2026-09-30: the LibriVox story from setup kept its 5 clips in the
    village beside the 7 real Curious Mike ones. `forget` removes a video
    and everything made from it, and nothing else."""

    # The base end-to-end tests expect an empty Clip; they run in their own class.
    test_permitted_video_in_captioned_short_out = None
    test_a_failing_stage_is_retried_then_parked_then_retryable = None

    def two_videos(self):
        self.cli("source", "add", "setup-test", "--rights", "public-domain", "--evidence", "LibriVox")
        self.cli("source", "add", "mike", "--rights", "permission", "--evidence", "campaign page")
        self.real = Path(self.tmp.name) / "episode.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=16000",
                        "-t", "60", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        str(self.real)], check=True)
        self.cli("ingest", "setup-test", str(self.video), "--title", "story")   # video 1, the test
        self.cli("ingest", "mike", str(self.real), "--title", "Trae Young")  # video 2, the real one
        self.give_words(1); self.give_words(2)
        code, said = self.cli("run")
        self.assertEqual(code, 0, said)
        self.conn = db.connect(self.home / "clipper.db")
        with self.conn:
            self.conn.execute("INSERT INTO trending_clips (yt_id, creator_id, title, seen_at, match_video_id, "
                              "match_start, match_end) VALUES ('t1', 'UC', 'x', ?, 1, 1.0, 20.0)", (db.now(),))
            db.record_cost(self.conn, "openrouter", "copy", 0.002, video_id=1)

    def count(self, sql, *a):
        return self.conn.execute(sql, a).fetchone()[0]

    def test_the_test_video_goes_and_the_real_one_stays(self):
        self.two_videos()
        before = self.count("SELECT COUNT(*) FROM clips WHERE video_id = 2")
        self.assertGreater(before, 0)
        code, said = self.cli("forget", "1")
        self.assertEqual(code, 0, said)
        self.assertIn("forgot video 1 (story)", said)
        self.assertEqual(self.count("SELECT COUNT(*) FROM clips WHERE video_id = 1"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM candidates WHERE video_id = 1"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM videos WHERE id = 1"), 0)
        self.assertFalse((self.home / "clips" / "1").exists())
        self.assertFalse((self.home / "media" / "1").exists())
        # the real video is untouched, on disk and in what the village shows
        self.assertEqual(self.count("SELECT COUNT(*) FROM clips WHERE video_id = 2"), before)
        self.assertTrue((self.home / "clips" / "2" / "01.mp4").exists())
        deck = report.snapshot(self.conn, config.paths(), config.load())
        self.assertEqual({c["video_id"] for c in deck["clips"]}, {2})
        # history stays: what was spent, and a note of what was forgotten
        self.assertEqual(self.count("SELECT COUNT(*) FROM costs WHERE video_id = 1"), 1)
        self.assertIn("forgot video 1", self.conn.execute(
            "SELECT msg FROM events WHERE stage = 'forget'").fetchone()[0])
        # Spotter no longer points at footage that is gone
        self.assertIsNone(self.count("SELECT match_video_id FROM trending_clips WHERE yt_id = 't1'"))

    def test_a_posted_clip_is_not_forgotten_by_accident(self):
        self.two_videos()
        cid = self.count("SELECT id FROM clips WHERE video_id = 1 ORDER BY rank LIMIT 1")
        with self.conn:
            self.conn.execute("INSERT INTO publications (clip_id, platform, status, url, created_at) "
                              "VALUES (?, 'youtube', 'posted', 'https://youtu.be/test1', ?)", (cid, db.now()))
        code, said = self.cli("forget", "1")
        self.assertEqual(code, 2)
        self.assertIn("https://youtu.be/test1", said)
        self.assertIn("--including-posted", said)
        self.assertGreater(self.count("SELECT COUNT(*) FROM clips WHERE video_id = 1"), 0, "nothing removed")
        code, said = self.cli("forget", "1", "--including-posted")
        self.assertEqual(code, 0, said)
        self.assertIn("still online", said)
        self.assertEqual(self.count("SELECT COUNT(*) FROM publications"), 0)

    def test_not_while_a_run_is_going(self):
        self.two_videos()
        import fcntl
        with open(config.paths()["lock"], "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, said = self.cli("forget", "1")
        self.assertEqual(code, 2)
        self.assertIn("in progress", said)
        self.assertGreater(self.count("SELECT COUNT(*) FROM clips WHERE video_id = 1"), 0)


# ---------------------------------------------------------------- a campaign's own clip list

from clipper import campaign  # noqa: E402

NOTION = json.loads((FIXTURES / "clipper-campaign-notion.json").read_text())


class CampaignListParsing(unittest.TestCase):
    """The real page's structure, both episodes' lists (text blanked)."""

    def setUp(self):
        self.eps = campaign.parse(NOTION["blocks"], NOTION["page"])

    def test_both_episodes_by_their_youtube_links(self):
        self.assertEqual(sorted(self.eps), ["mx2XmltF8ZE", "uf0q07QagUs"])
        for ep in self.eps.values():
            self.assertEqual(len(ep["entries"]), 50)
            self.assertEqual(len({e["key"] for e in ep["entries"]}), 50)

    def test_the_campaigns_start_here_order_comes_first(self):
        trae = self.eps["uf0q07QagUs"]["entries"]
        self.assertEqual([e["first_batch"] for e in trae].count(True), 10)
        self.assertEqual(trae[0]["key"], "C45", "the Knicks one - 'start there'")
        self.assertEqual((trae[0]["start"], trae[0]["end"]), (4032.0, 4101.0))   # 01:07:12-01:08:21
        chandler = self.eps["mx2XmltF8ZE"]["entries"]
        self.assertEqual([e["key"] for e in chandler[:6]], ["2", "4", "3", "9", "1", "13"])
        self.assertTrue(all(e["first_batch"] for e in chandler[:6]))
        self.assertFalse(any(e["first_batch"] for e in chandler[6:]))
        rest = [int(e["key"]) for e in chandler[6:]]
        self.assertEqual(rest, sorted(rest), "after the first batch, in the list's own numbering")

    def test_times_hooks_captions_and_directions_by_column_name(self):
        c13 = next(e for e in self.eps["mx2XmltF8ZE"]["entries"] if e["key"] == "13")
        self.assertEqual((c13["start"], c13["end"]), (4067.0, 4095.0))        # "01:07:47 - 01:08:15"
        self.assertEqual((c13["hook"], c13["caption"], c13["direction"]), ("hook 13", "caption 13", "how 13"))
        self.assertTrue(all(e["direction"] for e in self.eps["mx2XmltF8ZE"]["entries"]))

    def test_keys_and_page_links(self):
        self.assertEqual([campaign.norm_key(k) for k in ("#02", "02", "2", "c45", " C45 ")],
                         ["2", "2", "2", "C45", "C45"])
        pid = "3e2f2311-2cb0-81d0-aa31-d058376acc1d"
        for url in ("https://x.notion.site/3e2f23112cb081d0aa31d058376acc1d",
                    "https://x.notion.site/Curious-Mike-Guidelines-3e2f23112cb081d0aa31d058376acc1d?pvs=4",
                    f"https://x.notion.site/{pid}"):
            self.assertEqual(campaign.page_id(url), pid)
        with self.assertRaises(ValueError):
            campaign.page_id("https://x.notion.site/guidelines")


class CampaignSnapping(unittest.TestCase):
    def test_rough_times_land_on_sentence_edges(self):
        sents = moments.sentences(WORDS, 0.8)
        target = next(k for k, s in enumerate(sents) if s["end"] - s["start"] > 5)   # long enough to be rough about
        s0, s1 = sents[target]["start"], sents[target + 1]["end"]
        i, j = campaign.snap(WORDS, sents, s0 + 1.3, s1 - 1.1)      # a campaign's whole seconds
        self.assertEqual((i, j), (target, target + 1))
        self.assertLessEqual(sents[i]["start"], s0 + 1.3)
        self.assertGreaterEqual(sents[j]["end"], s1 - 1.1, "never trimmed short of what it names")


class HookPlacement(unittest.TestCase):
    def test_under_the_status_bar_or_below_a_high_watermark(self):
        self.assertEqual(render.hook_top(None), captions.HOOK_TOP)
        self.assertEqual(render.hook_top((543, 162, 268, 880)), captions.HOOK_TOP, "a low mark is no bother")
        self.assertEqual(render.hook_top((400, 100, 340, 300)), 300 + 100 + 20)
        with self.assertRaisesRegex(ValueError, "no room"):
            render.hook_top((400, 700, 340, 300))       # a mark so tall the hook is pushed into the captions

    def test_the_hook_is_on_screen_the_whole_clip_and_wraps(self):
        ass = captions.build(WORDS, 10, 30, hook="He spent hours rehearsing what could go wrong.", hook_top=250)
        line = next(l for l in ass.splitlines() if l.startswith("Dialogue: 1,") and ",Hook," in l)
        self.assertTrue(line.startswith("Dialogue: 1,0:00:00.00,0:00:20.00,Hook,"))
        self.assertIn("{\\q0}He spent hours", line, "its own case, and wrapped")
        self.assertIn("Style: Hook,", ass)
        self.assertRegex(ass, r"Style: Hook,[^\n]*,8,90,90,250,1")
        self.assertNotIn(",Hook,", captions.build(WORDS, 10, 30))


def mini_list(episode, rows, first=()):
    """A page shaped like the campaign's: a section, a clip table, a priority table."""
    cols = ["a", "b", "c", "d"]
    blocks, n = {}, [0]

    def blk(kind, **kw):
        n[0] += 1
        i = f"b{n[0]}"
        blocks[i] = dict(id=i, type=kind, **kw)
        return i

    def row(*cells):
        return blk("table_row", properties={c: v for c, v in zip(cols, cells)})

    def link(t, s):
        return [[t, [["a", f"https://youtu.be/{episode}?t={int(s)}"]]]]

    clip = blk("table", format={"table_block_column_order": cols}, content=[
        row([["ID"]], [["Timestamp"]], [["Hook (on screen)"]], [["Caption (in the post)"]])] + [
        row([[k]], link(f"{int(a) // 60:02d}:{int(a) % 60:02d}-{int(b) // 60:02d}:{int(b) % 60:02d}", a),
            [[hook]], [[cap]]) for k, a, b, hook, cap in rows])
    prio = blk("table", format={"table_block_column_order": cols[:3]}, content=[
        row([["#"]], [["ID"]], [["Timestamp"]])] + [row([[str(k + 1)]], [[key]], [["0:00-0:01"]]) for k, key in enumerate(first)])
    sec = blk("sub_header", properties={"title": [["the clip list"]]}, content=[prio, clip])
    blocks["page"] = {"id": "page", "type": "page", "content": [sec]}
    return "page", blocks


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class CampaignListEndToEnd(EndToEnd):
    """Ingest an episode the campaign has listed: its first batch is cut, with
    its hooks on screen and its captions posted word for word - nothing of
    Clip's own choosing - and `cuts` queues more."""

    test_permitted_video_in_captioned_short_out = None
    test_a_failing_stage_is_retried_then_parked_then_retryable = None

    def setUp(self):
        super().setUp()
        self.cli("source", "add", "mike", "--rights", "permission", "--evidence", "campaign page")
        sents = moments.sentences(self.words, 0.8)
        # three listed moments, whole-second times as a campaign writes them
        rows = [("C1", 1.5, 24.8, "The first hook", "The campaign's first caption."),
                ("C2", 29.7, 49.3, "The second hook", "Its second caption, word for word."),
                ("C3", 39.0, 58.2, "The third hook", "A third caption.")]
        conn = db.connect(self.home / "clipper.db")
        got = campaign.import_list(conn, "mike", "https://x.notion.site/page",
                                   fetched=mini_list("AAAAAAAAAAA", rows, first=["C2", "C1"]))
        self.assertEqual(got["AAAAAAAAAAA"][1:], (3, 2))
        self.conn = conn
        del sents

    def test_the_first_batch_with_hooks_and_the_campaigns_words(self):
        code, said = self.cli("ingest", "mike", str(self.video), "--episode", "https://youtu.be/AAAAAAAAAAA")
        self.assertEqual(code, 0, said)
        self.assertIn("the campaign lists 3 moments", said)
        self.give_words(1)
        code, said = self.cli("run")
        self.assertEqual(code, 0, said)
        clips = self.conn.execute("SELECT * FROM clips ORDER BY rank").fetchall()
        self.assertEqual([json.loads(c["meta"])["campaign_key"] for c in clips], ["C2", "C1"],
                         "the campaign's first batch, in its order - none of Clip's own picks")
        c1 = clips[0]
        self.assertAlmostEqual(c1["start"], 29.7, delta=0.5)
        ass = Path(c1["path"]).with_suffix(".ass").read_text()
        self.assertIn("{\\q0}The second hook", ass)
        post = self.conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (c1["id"],)).fetchone()
        self.assertEqual((post["title"], post["caption"], post["generator"]),
                         ("The second hook", "Its second caption, word for word.", "campaign"))
        self.assertNotIn(postcopy.QUESTION, post["caption"])
        # more, one at a time, then the list is used up
        code, said = self.cli("cuts", "1", "1")
        self.assertEqual(code, 0, said)
        self.assertIn("#C3", said)
        self.assertEqual(self.cli("run")[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0], 3)
        self.assertIn("has been cut", self.cli("cuts", "1", "5")[1])

    def test_cuts_needs_a_transcript_and_a_list(self):
        self.cli("ingest", "mike", str(self.video))
        code, said = self.cli("cuts", "1")
        self.assertEqual(code, 2)
        self.assertIn("no campaign list", said)
        code, said = self.cli("cuts", "1", "--episode", "AAAAAAAAAAA")
        self.assertEqual(code, 2)
        self.assertIn("not transcribed yet", said)


# ---------------------------------------------------------------- Twitch: the chat watcher

from clipper import dropbox, twitch  # noqa: E402

CHAT_LINES = [l.split("\t", 1) for l in (FIXTURES / "twitch-irc-chat.txt").read_text().splitlines()
              if l and not l.startswith("#")]
# Real timings from three minutes of a busy chat (#forsen, 2026-09-30).
CHAT = [(float(t), twitch.parse(line)[1]) for t, line in CHAT_LINES
        if twitch.parse(line)[0] == "PRIVMSG"]


class ChatLines(unittest.TestCase):
    """Every line of a real anonymous session, as Twitch sent it."""

    def test_the_real_session_is_1023_messages_and_nothing_else(self):
        cmds = [twitch.parse(line)[0] for _, line in CHAT_LINES]
        self.assertEqual(cmds.count("PRIVMSG"), 1023)
        self.assertEqual(len(CHAT), 1023)
        self.assertEqual(CHAT[0][1], "message 1")
        self.assertEqual(CHAT[-1][1], "message 1023")
        # the welcome, JOIN and NAMES lines are not chat
        self.assertEqual(set(cmds) - {"PRIVMSG"}, {"001", "002", "003", "004", "375", "372", "376",
                                                    "JOIN", "353", "366"})

    def test_a_message_keeps_everything_after_the_first_colon(self):
        # Bug guarded: splitting on every " :" would cut "look at this :)"
        line = CHAT_LINES[-1][1].replace("message 1023", "LUL look :) at : this")
        self.assertEqual(twitch.parse(line), ("PRIVMSG", "LUL look :) at : this"))

    def test_ping_and_reconnect_and_tags(self):
        # Twitch's documented forms: a missed PONG drops the connection.
        self.assertEqual(twitch.parse("PING :tmi.twitch.tv\r\n"), ("PING", "tmi.twitch.tv"))
        self.assertEqual(twitch.parse(":tmi.twitch.tv RECONNECT")[0], "RECONNECT")
        tagged = "@badge-info=;color=#0000FF;display-name=x :x!x@x.tmi.twitch.tv PRIVMSG #bar :hi there"
        self.assertEqual(twitch.parse(tagged), ("PRIVMSG", "hi there"))


def burst(at, n, seconds=5.0, text="KEKW"):
    return [(at + i * seconds / n, text) for i in range(n)]


class ChatSpikes(unittest.TestCase):

    def feed(self, msgs, ratio=3.0, min_rate=2.0, every=1.0):
        """Feed messages in time order; return the times a spike was seen."""
        meter, hits, next_look = twitch.Chat(ratio, min_rate), [], 0.0
        for t, text in sorted(msgs):
            meter.add(t, text)
            if t >= next_look:
                next_look = t + every
                if meter.spike(t):
                    hits.append(t)
        return hits

    def test_a_busy_chat_being_busy_is_not_a_moment(self):
        # Bug guarded: a fixed messages-per-second bar would clip a big
        # channel all stream long. 5-7 a second is simply how this chat is.
        self.assertEqual(self.feed(CHAT), [])

    def test_chat_exploding_is(self):
        hits = self.feed(CHAT + burst(150, 100))
        self.assertTrue(hits, "100 messages in 5 s over a usual ~5/s")
        # seen while the burst is within the last NOW seconds, and only then
        self.assertTrue(all(150 <= t <= 155 + twitch.Chat.NOW for t in hits), hits)

    def test_nothing_fires_before_there_is_a_usual(self):
        # Bug guarded: joining mid-stream, the first seconds of chat measured
        # against nothing look like an explosion.
        self.assertEqual(self.feed(CHAT + burst(30, 100)), [])

    def test_a_small_chat_needs_the_minimum_rate(self):
        quiet = [(t, "hi") for t in range(0, 300, 10)]          # one message every 10 s
        self.assertEqual(self.feed(quiet + burst(200, 8, 10.0)), [],
                         "0.8 a second is 8x usual, but not a moment")
        self.assertTrue(self.feed(quiet + burst(200, 40, 10.0)))

    def test_an_earlier_burst_does_not_raise_the_bar(self):
        # Bug guarded: a mean would count the first explosion into "usual"
        # and miss the second one a minute later; the median does not.
        hits = self.feed(CHAT + burst(130, 100) + burst(170, 100))
        self.assertTrue(any(130 <= t <= 140 for t in hits))
        self.assertTrue(any(170 <= t <= 180 for t in hits))

    def test_what_chat_was_saying_counts_each_word_once_a_message(self):
        meter = twitch.Chat()
        for t, text in [(0, "LUL LUL LUL"), (1, "LUL W"), (2, "W"), (3, "OMEGALUL")]:
            meter.add(t, text)
        self.assertEqual(meter.words(3.5, 2), [("LUL", 2), ("W", 2)])


class FakeTwitch:
    """Twitch's API as its reference documents the replies."""

    def __init__(self, live_until=10 ** 9, refuse=None):
        self.live_until, self.refuse, self.now = live_until, refuse, 0.0
        self.made, self.asked_ids = [], []

    def stream(self, login):
        return ({"user_id": "4444", "user_login": login, "title": "stream", "started_at": "2026-09-30T20:00:00Z"}
                if self.now < self.live_until else None)

    def create_clip(self, bid, duration):
        if self.refuse:
            raise twitch.TwitchError(403, self.refuse)
        cid = f"Clip{len(self.made) + 1}"
        self.made.append((self.now, bid, duration))
        return {"id": cid, "edit_url": f"https://clips.twitch.tv/{cid}/edit"}

    def clips(self, **params):
        if "id" in params:
            self.asked_ids.append((self.now, list(params["id"])))
            return [{"id": i, "url": f"https://clips.twitch.tv/{i}"} for i in params["id"]]
        return [
            {"id": "Top", "view_count": 9000, "creator_id": "7", "creator_name": "fan", "title": "the moment",
             "video_id": "v1", "vod_offset": 3723, "duration": 30, "created_at": "2099-01-01T00:00:00Z"},
            {"id": "Same", "view_count": 50, "creator_id": "8", "creator_name": "fan2", "title": "same moment",
             "video_id": "v1", "vod_offset": 3730, "duration": 30, "created_at": "2099-01-01T00:00:00Z"},
            {"id": "Mine", "view_count": 800, "creator_id": "me", "creator_name": "you", "title": "yours",
             "video_id": "v1", "vod_offset": 100, "duration": 60, "created_at": "2099-01-01T00:00:00Z"},
            {"id": "NoVod", "view_count": 700, "creator_id": "9", "creator_name": "x", "title": "no vod",
             "video_id": "", "vod_offset": None, "duration": 30, "created_at": "2099-01-01T00:00:00Z"}]

    def me(self):
        return {"id": "me"}

    def videos(self, user_id):
        # the past broadcast of this stream, and an older one
        return [{"id": "v900", "created_at": "2026-09-30T20:00:09Z"},
                {"id": "v800", "created_at": "2026-09-28T21:00:00Z"}]

    def user(self, login):
        return {"id": "4444", "login": login}


class Watcher(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.connect(Path(self.tmp.name) / "c.db")
        self.cfg = dict(CFG)
        with self.conn:
            self.conn.execute("INSERT INTO sources (name, rights, evidence, created_at, twitch) "
                              "VALUES ('pbm', 'permission', 'campaign', ?, 'plaqueboymax')", (db.now(),))
        self.src = twitch.watched(self.conn)[0]

    def run_watch(self, msgs, api, tick_until=None):
        """The real chat, with ticks every second like the IRC reader's."""
        end = tick_until or (max(t for t, _ in msgs) + 1)
        stream = sorted(msgs + [(float(t), None) for t in range(int(end) + 1)], key=lambda m: m[0])

        def chat(login):
            for t, text in stream:
                api.now = t
                yield t, text
        return twitch.watch(self.conn, self.cfg, self.src, api=api, chat=chat,
                            now=lambda: api.now, say=lambda s: None)

    def test_offline_asks_once_and_leaves(self):
        api = FakeTwitch(live_until=-1)
        self.assertEqual(self.run_watch(CHAT, api), {"live": False, "clips": 0})
        self.assertEqual(api.made, [])

    def test_one_explosion_is_one_clip_asked_for_after_the_delay(self):
        api = FakeTwitch()
        r = self.run_watch(CHAT + burst(150, 100), api, tick_until=400)
        self.assertEqual(r["clips"], 1, "the burst lasts seconds; the cooldown makes it one clip")
        at, bid, duration = api.made[0]
        self.assertEqual((bid, duration), ("4444", 60))
        # chat first reads as exploding at ~155 s (ChatSpikes), when the burst is complete
        self.assertGreaterEqual(at, 155 + self.cfg["twitch_clip_delay_seconds"],
                                "asked after the delay, so the reaction is in it")
        row = self.conn.execute("SELECT * FROM twitch_clips").fetchone()
        self.assertEqual(row["status"], "made", "confirmed once Twitch listed it")
        asked_at, ids = api.asked_ids[0]
        self.assertEqual(ids, ["Clip1"])
        self.assertTrue(at + twitch.CONFIRM_AFTER <= asked_at <= at + twitch.CONFIRM_AFTER + 11,
                        "confirmed during the stream, so the Deck says so the same night")
        self.assertEqual(row["url"], "https://clips.twitch.tv/Clip1")
        self.assertEqual(json.loads(row["chat_words"])[0][0], "KEKW")
        self.assertGreater(row["chat_rate"], 3 * row["chat_usual"])

    def test_the_cooldown_and_the_cap(self):
        api = FakeTwitch()
        bursts = [m for k in range(6) for m in burst(130 + 40 * k, 150)]
        # bursts every 40 s: a 60 s cooldown alone would allow three clips
        self.cfg.update(twitch_cooldown_seconds=60, twitch_max_clips_per_stream=2)
        r = self.run_watch(CHAT + bursts, api, tick_until=400)
        self.assertEqual(r["clips"], 2)
        self.assertGreaterEqual(api.made[1][0] - api.made[0][0], 60)
        self.cfg.update(twitch_max_clips_per_stream=8)
        with self.conn:
            self.conn.execute("DELETE FROM twitch_clips")
        self.assertEqual(self.run_watch(CHAT + bursts, FakeTwitch(), tick_until=400)["clips"], 3)

    def test_every_moment_is_clipped_and_the_biggest_jumps_are_best(self):
        # 2026-10-01: the first night clipped the first 8 moments over the bar,
        # reached the limit two hours in, and was blind for the last four.
        # Now every one is clipped and the biggest jumps are marked best.
        loop = [(t + 180 * k, m) for k in range(9) for t, m in CHAT]      # 27 minutes of real chat
        # Ten explosions - more than the old limit of 8. Each starts the same
        # (150 messages in 5 s, which crosses the bar) and the real reaction
        # comes 10 s later, after the clip is asked for: only measuring on
        # past the crossing can tell the big ones from the small.
        sizes = [60, 300, 120, 220, 90, 260, 150, 70, 110, 80]
        times = [150 + 130 * i for i in range(len(sizes))]
        msgs = loop + [m for at, n in zip(times, sizes)
                       for m in burst(at, 150) + burst(at + 10, n)]
        self.cfg.update(twitch_cooldown_seconds=100, twitch_keep_best=3)
        api = FakeTwitch()
        r = self.run_watch(msgs, api, tick_until=1600)
        self.assertEqual(r["clips"], len(sizes), "all ten over the bar, not the first few")

        def burst_of(clip_id):
            at = api.made[int(clip_id[4:]) - 1][0]
            return max(i for i, b in enumerate(times) if b <= at)
        ranked = twitch.best_of(self.conn.execute("SELECT * FROM twitch_clips").fetchall(), 3)
        self.assertEqual(sorted(sizes[burst_of(c["id"])] for c in ranked if c["best"]), [220, 260, 300])
        state = report.twitch_state(self.conn, self.cfg)
        self.assertEqual([(st["made"], st["best"]) for st in state["streams"]], [(10, 3)])
        self.assertEqual(sum(c["best"] for c in state["clips"]), 3)
        self.assertEqual([c["best"] for c in state["clips"]][:3], [True] * 3, "best first")
        last = self.conn.execute("SELECT msg FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("best 3", last)

    def clip_row(self, cid, created, started="2026-09-30T22:40:12Z"):
        with self.conn:
            self.conn.execute("INSERT INTO twitch_clips (id, channel, status, chat_rate, chat_usual, "
                              "stream_started_at, created_at) VALUES (?, 'plaqueboymax', 'made', 9, 3, ?, ?)",
                              (cid, started, created))

    def test_each_clip_links_to_its_minute_of_the_past_broadcast(self):
        # 2026-10-01: 7 of the first night's 8 clips were gone by morning. The
        # moment in the past broadcast is there to clip by hand, and stays.
        self.clip_row("GrotesqueTallHyena", "2026-10-01 00:06:36")
        api = FakeTwitch()
        api.videos = lambda uid: [{"id": "2581", "created_at": "2026-09-30T22:40:20Z"},
                                  {"id": "2570", "created_at": "2026-09-29T21:00:00Z"}]
        self.assertEqual(twitch.link_to_broadcast(self.conn, api, "plaqueboymax", "4444", self.cfg), 1)
        row = self.conn.execute("SELECT * FROM twitch_clips").fetchone()
        # asked for at 1:26:16 into the broadcast; the watcher's minute began 60 s before
        self.assertEqual((row["vod_id"], row["vod_offset"]), ("2581", 5116))
        clip = report.twitch_state(self.conn, self.cfg)["clips"][0]
        self.assertEqual(clip["vod_link"], "https://www.twitch.tv/videos/2581?t=1h25m16s")

    def test_another_streams_broadcast_is_not_used(self):
        self.clip_row("Lonely", "2026-10-01 00:06:36")
        api = FakeTwitch()
        api.videos = lambda uid: [{"id": "2570", "created_at": "2026-09-29T21:00:00Z"}]
        self.assertEqual(twitch.link_to_broadcast(self.conn, api, "plaqueboymax", "4444", self.cfg), 0)
        self.assertIsNone(report.twitch_state(self.conn, self.cfg)["clips"][0]["vod_link"])

    def test_the_watch_links_its_clips_when_the_stream_ends(self):
        api = FakeTwitch(live_until=320)
        self.run_watch(CHAT + burst(150, 100), api, tick_until=2000)
        row = self.conn.execute("SELECT * FROM twitch_clips").fetchone()
        self.assertEqual(row["vod_id"], "v900")
        self.assertGreaterEqual(row["vod_offset"], 0)

    def test_moments_links_last_nights_clips_too(self):
        self.clip_row("FromLastNight", "2026-09-30 20:30:00", started="2026-09-30T20:00:00Z")
        with contextlib.redirect_stdout(io.StringIO()):
            twitch.moments(self.conn, self.cfg, api=FakeTwitch(), now=lambda: 1790800000.0)
        row = self.conn.execute("SELECT * FROM twitch_clips WHERE id = 'FromLastNight'").fetchone()
        self.assertEqual((row["vod_id"], row["vod_offset"]), ("v900", 30 * 60 - 9 - 60))

    def test_ranked_by_the_jump_not_the_raw_speed(self):
        # Late in a stream more people watch: 20/s against a usual 10/s is a
        # smaller moment than 12/s against a usual 3/s.
        rows = [{"id": "late", "status": "made", "chat_rate": 20.0, "chat_usual": 10.0},
                {"id": "early", "status": "made", "chat_rate": 12.0, "chat_usual": 3.0},
                {"id": "never", "status": "failed", "chat_rate": 50.0, "chat_usual": 1.0}]
        ranked = twitch.best_of(rows, 1)
        self.assertEqual([(c["id"], c["best"], c["jump"]) for c in ranked],
                         [("early", True, 4.0), ("late", False, 2.0)])

    def test_a_refusal_is_said_not_swallowed(self):
        api = FakeTwitch(refuse="The broadcaster has restricted the ability to capture clips to followers")
        self.run_watch(CHAT + burst(150, 100), api)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM twitch_clips").fetchone()[0], 0)
        msg = self.conn.execute("SELECT msg FROM events WHERE level = 'warn'").fetchone()[0]
        self.assertIn("followers", msg)
        self.assertIn("follow the channel", msg)

    def test_the_stream_ending_ends_the_watch_and_lists_past_moments(self):
        api = FakeTwitch(live_until=320)
        r = self.run_watch(CHAT + burst(150, 100), api, tick_until=2000)
        self.assertTrue(r["live"])
        self.assertLess(api.now, 320 + twitch.LIVE_CHECK + 1, "stopped at the next live check")
        ids = [m["id"] for m in report.twitch_state(self.conn, self.cfg)["moments"]]
        self.assertEqual(ids, ["Top"], "own clip, same moment twice, and no-broadcast clips left out")

    def test_a_moment_links_to_its_second_of_the_broadcast(self):
        self.assertEqual(twitch.vod_link("v1", 3723), "https://www.twitch.tv/videos/v1?t=1h02m03s")
        self.assertEqual(twitch.vod_link("v1", 59), "https://www.twitch.tv/videos/v1?t=0h00m59s")


class TwitchSignIn(unittest.TestCase):
    """Against Twitch's documented device-flow shapes; there was no Twitch
    app to test with. The first twitch-login is the check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = os.environ.get("CLIPPER_HOME")
        os.environ["CLIPPER_HOME"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("CLIPPER_HOME", None) if self.old is None
                        else os.environ.__setitem__("CLIPPER_HOME", self.old))
        self.env = Path(self.tmp.name) / "credentials.env"
        self.env.write_text("TWITCH_CLIENT_ID=abc123\n")

    def test_the_device_flow_waits_and_keeps_both_tokens(self):
        replies = iter([(200, {"device_code": "dc", "user_code": "ABCDEFGH", "interval": 5, "expires_in": 1800,
                               "verification_uri": "https://www.twitch.tv/activate?public=true&device-code=ABCDEFGH"}),
                        (400, {"status": 400, "message": "authorization_pending"}),
                        (200, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 14400})])
        posted, said = [], []
        twitch.login(post=lambda u, f: (posted.append(f), next(replies))[1], sleep=lambda s: None,
                     say=said.append, now=lambda: 1000.0)
        self.assertEqual(posted[0], {"client_id": "abc123", "scopes": "clips:edit"})
        self.assertNotIn("client_secret", posted[1], "a public app has no secret")
        c = twitch.creds()
        self.assertEqual((c["TWITCH_REFRESH_TOKEN"], c["TWITCH_ACCESS_TOKEN"], c["TWITCH_ACCESS_EXPIRES"]),
                         ("rt1", "at1", "15400"))
        self.assertEqual(oct(self.env.stat().st_mode & 0o777), "0o600")

    def test_a_fresh_token_is_reused_and_a_stale_one_refreshed_and_rotated(self):
        # Bug guarded: Twitch's refresh tokens are one-time use. Refreshing
        # on every call, or keeping the old refresh token after one, signs
        # Clip out within a day.
        self.env.write_text("TWITCH_CLIENT_ID=abc123\nTWITCH_REFRESH_TOKEN=rt1\n"
                            "TWITCH_ACCESS_TOKEN=at1\nTWITCH_ACCESS_EXPIRES=5000\n")
        calls = []
        post = lambda u, f: (calls.append(f), (200, {"access_token": "at2", "refresh_token": "rt2",
                                                     "expires_in": 14400}))[1]
        self.assertEqual(twitch.access_token(post=post, now=lambda: 1000.0), "at1")
        self.assertEqual(calls, [], "four minutes left is still a token")
        self.assertEqual(twitch.access_token(post=post, now=lambda: 4800.0), "at2")
        self.assertEqual(calls[0]["refresh_token"], "rt1")
        self.assertEqual(twitch.creds()["TWITCH_REFRESH_TOKEN"], "rt2")
        self.assertEqual(twitch.access_token(post=post, now=lambda: 4800.0), "at2")
        self.assertEqual(len(calls), 1)

    def test_a_spent_refresh_token_asks_for_a_new_sign_in(self):
        self.env.write_text("TWITCH_CLIENT_ID=abc123\nTWITCH_REFRESH_TOKEN=old\n")
        with self.assertRaisesRegex(twitch.NotSetUp, "twitch-login"):
            twitch.access_token(post=lambda u, f: (400, {"status": 400, "message": "Invalid refresh token"}))


class TwitchChannelOnASource(unittest.TestCase):

    def test_logins_and_links_both_work_and_nonsense_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = db.connect(Path(tmp.name) / "c.db")
        with conn:
            conn.execute("INSERT INTO sources (name, rights, evidence, created_at) "
                         "VALUES ('pbm', 'permission', 'x', ?)", (db.now(),))
        with contextlib.redirect_stdout(io.StringIO()):
            for given in ("plaqueboymax", "PlaqueBoyMax", "https://www.twitch.tv/plaqueboymax",
                          "twitch.tv/plaqueboymax/", "@plaqueboymax"):
                cli.set_pickup(conn, "pbm", given, None)
                self.assertEqual(twitch.watched(conn)[0]["twitch"], "plaqueboymax", given)
            with self.assertRaises(ValueError):
                cli.set_pickup(conn, "pbm", "https://www.youtube.com/@plaqueboymax", None)
            with self.assertRaisesRegex(ValueError, "folder"):
                cli.set_pickup(conn, "pbm", None, "https://www.dropbox.com/scl/fi/abc/clip.mp4?dl=0")
            cli.set_pickup(conn, "pbm", "", None)
        self.assertEqual(twitch.watched(conn), [])


class TheTimerClipsAVideoAddedByHand(EndToEnd):
    """2026-10-08: the Ant-Man & LaMelo episode was added with `ingest` and sat
    unclipped for a day. The 15-minute timer (`pickup --run`) ran the pipeline
    only when Dropbox brought a new file, and nothing told anyone to run it."""

    test_permitted_video_in_captioned_short_out = None
    test_a_failing_stage_is_retried_then_parked_then_retryable = None

    def test_a_waiting_video_is_run_with_nothing_new_in_dropbox(self):
        from unittest import mock
        self.cli("source", "add", "tao", "--rights", "permission", "--evidence", "campaign page")
        code, said = self.cli("ingest", "tao", str(self.video))
        self.assertIn("within 15 minutes", said, "the owner is told it will happen")
        ran = []
        nothing = {"new": [], "skipped": 0, "failed": 0}
        with mock.patch.object(cli.dropbox, "pickup", return_value=nothing), \
             mock.patch.object(cli.pipeline, "run", side_effect=lambda *a, **k: ran.append(1)):
            code, said = self.cli("pickup", "--run")
        self.assertEqual((code, ran), (0, [1]), said)
        self.assertIn("video 1: ingested", said)
        with mock.patch.object(cli.dropbox, "pickup", return_value=nothing), \
             mock.patch.object(cli.pipeline, "run", side_effect=lambda *a, **k: ran.append(2)):
            conn = db.connect(self.home / "clipper.db")
            with conn:
                conn.execute("UPDATE videos SET stage = 'done'")
            self.cli("pickup", "--run")
        self.assertEqual(ran, [1], "nothing waiting: no run")


# ---------------------------------------------------------------- the Dropbox folder pickup

@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class DropboxPickup(EndToEnd):
    """A folder of clips in, one video each, rendered whole."""

    test_permitted_video_in_captioned_short_out = None
    test_a_failing_stage_is_retried_then_parked_then_retryable = None
    FOLDER = "https://www.dropbox.com/scl/fo/abc123/AAAxyz?rlkey=k1&dl=0"

    def setUp(self):
        super().setUp()
        self.cli("source", "add", "pbm", "--rights", "permission", "--evidence", "campaign page")
        self.assertEqual(self.cli("source", "rules", "pbm", "--dropbox-folder", self.FOLDER)[0], 0)
        self.conn = db.connect(self.home / "clipper.db")
        # Twitch clips are at most 60 s; this one is 25.
        self.clip = Path(self.tmp.name) / "twitch-clip.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                        "-t", "25", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        str(self.clip)], check=True)
        self.folder = [{".tag": "file", "name": "PBM laughing.mp4", "id": "id:1", "content_hash": "h1",
                        "size": self.clip.stat().st_size}]
        self.fetched = []

    def pick(self):
        def fetch(link, name, dest, auth, size):
            self.fetched.append((link, name))
            shutil.copy(self.clip if name.endswith(".mp4") else __file__, dest)
        return dropbox.pickup(self.conn, config.paths(), config.load(), auth="Basic x",
                              lister=lambda link, auth: list(self.folder), fetch=fetch)

    def test_a_new_clip_becomes_one_video_and_is_rendered_whole(self):
        r = self.pick()
        self.assertEqual(r["new"], [1])
        self.assertEqual(self.fetched, [(self.FOLDER, "PBM laughing.mp4")])
        v = self.conn.execute("SELECT v.*, s.name AS source FROM videos v JOIN sources s ON s.id = v.source_id").fetchone()
        self.assertEqual((v["source"], v["title"], v["origin"]),
                         ("pbm", "PBM laughing", "dropbox: pbm/PBM laughing.mp4"))
        self.words = [x for x in WORDS if x["end"] <= 24.0]
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        # Bug guarded: the moment finder would pick its "best 20-30 s" of a
        # clip somebody already cut, as if it were an episode. A clip is one
        # candidate, cut by trim.py around its own moment.
        cands = self.conn.execute("SELECT * FROM candidates").fetchall()
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual((c["selected"], c["scorer"]), (1, trim.SCORER))
        self.assertTrue(0 <= c["start"] < c["end"] <= v["duration"] + 0.01)
        info = media.probe(self.home / "clips" / "1" / "01.mp4")
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertAlmostEqual(info["duration"], c["end"] - c["start"], delta=0.3)

    def test_with_trimming_off_a_clip_is_kept_whole(self):
        cfg = json.loads((self.home / "config.json").read_text())
        (self.home / "config.json").write_text(json.dumps(dict(cfg, trim_clips=False)))
        self.pick()
        self.words = [x for x in WORDS if x["end"] <= 24.0]
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        c = self.conn.execute("SELECT * FROM candidates").fetchone()
        self.assertEqual((c["start"], c["scorer"]), (0, "whole"))
        self.assertAlmostEqual(media.probe(self.home / "clips" / "1" / "01.mp4")["duration"], 25, delta=0.3)

    def test_each_file_once_and_new_content_again(self):
        self.pick()
        self.assertEqual(self.pick()["new"], [], "the same file is not picked up twice")
        self.assertEqual(len(self.fetched), 1, "and not even downloaded again")
        # the same name re-uploaded with different content is new
        self.folder[0]["content_hash"] = "h2"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i", "sine=frequency=550",
                        "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        str(self.clip)], check=True)
        self.assertEqual(self.pick()["new"], [2])

    def test_not_a_video_is_noted_once(self):
        self.folder.append({".tag": "file", "name": "notes.txt", "id": "id:2", "content_hash": "h3", "size": 5})
        self.pick(); self.pick()
        self.assertEqual([n for _, n in self.fetched], ["PBM laughing.mp4"], "the text file is never fetched")
        notes = [r[0] for r in self.conn.execute("SELECT msg FROM events WHERE stage = 'pickup'")]
        self.assertEqual(sum("notes.txt" in m for m in notes), 1, notes)

    def test_a_failure_is_retried_but_said_once(self):
        # Bug guarded: a file that cannot be ingested (the disk is full) must
        # be tried again next time, without one warning every 15 minutes.
        real = ingest.need_space

        def full(folder, nbytes, hint=""):
            raise RuntimeError("not enough disk: 0.1 GB free")
        ingest.need_space = full
        self.addCleanup(setattr, ingest, "need_space", real)
        self.assertEqual(self.pick()["failed"], 1)
        self.assertEqual(self.pick()["failed"], 1)
        warns = self.conn.execute("SELECT COUNT(*) FROM events WHERE stage = 'pickup'").fetchone()[0]
        self.assertEqual(warns, 1)
        ingest.need_space = real
        self.assertEqual(self.pick()["new"], [1])

    def test_forgetting_a_picked_up_video_does_not_bring_it_back(self):
        self.pick()
        self.assertEqual(self.cli("forget", "1")[0], 0)
        self.assertEqual(self.pick()["new"], [])
        self.assertEqual(self.conn.execute("SELECT detail FROM pickups").fetchone()[0], "forgotten")

    def test_a_long_video_is_still_searched_for_moments(self):
        # Only a clip is used whole: the 60 s test video is longer than
        # this config's 30 s clips, so it is searched as before.
        self.cli("ingest", "pbm", str(self.video))
        self.give_words(1)
        self.assertEqual(self.cli("run")[0], 0)
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates WHERE scorer = 'whole'")
                         .fetchone()[0], 0)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class ChatReaderOverTls(unittest.TestCase):
    """irc_messages against a local TLS server replaying the real session:
    lines split across reads, a PING that must be answered, and Twitch's
    RECONNECT. The droplet reaches irc.chat.twitch.tv:6697 itself; this
    session could not, so that one connection is believed, not verified."""

    def setUp(self):
        import ssl
        import threading
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        cert = d / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(d / "key.pem"), "-out", str(cert)], check=True, capture_output=True)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, d / "key.pem")
        self.client_ctx = ssl.create_default_context(cafile=str(cert))
        import socket as _socket
        self.listener = _socket.create_server(("127.0.0.1", 0))
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        self.heard = []          # what the client sent, per connection
        lines = [l for _, l in CHAT_LINES[:20]]        # the welcome and the first 10 messages
        more = [l for _, l in CHAT_LINES[20:23]]       # three more, after the reconnect

        def read_until(conn, word):
            got = b""
            while word not in got:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                got += chunk
            return got.decode()

        def serve():
            first = self.listener.accept()[0]
            threading.Thread(target=serve_again, daemon=True).start()
            with server_ctx.wrap_socket(first, server_side=True) as c:
                self.heard.append(read_until(c, b"JOIN"))
                blob = ("\r\n".join(lines) + "\r\n").encode()
                for i in range(0, len(blob), 37):             # lines split across reads
                    c.sendall(blob[i:i + 37])
                c.sendall(b"PING :tmi.twitch.tv\r\n")
                self.heard.append(read_until(c, b"PONG"))
                c.sendall(b":tmi.twitch.tv RECONNECT\r\n")
                import time as _t
                _t.sleep(3)          # Twitch hangs up soon after; Clip must not wait for it

        def serve_again():
            with server_ctx.wrap_socket(self.listener.accept()[0], server_side=True) as c:
                self.heard.append(read_until(c, b"JOIN"))
                c.sendall(("\r\n".join(more) + "\r\n").encode())
                import time as _t
                _t.sleep(3)
        threading.Thread(target=serve, daemon=True).start()

    def test_messages_ping_and_reconnect(self):
        import time as _t
        got, slept, deadline = [], [], _t.time() + 10
        for t, text in twitch.irc_messages("PlaqueBoyMax", tick=0.2, say=lambda s: None,
                                           host="127.0.0.1", port=self.port, context=self.client_ctx,
                                           sleep=slept.append):
            if text is not None:
                got.append((_t.time(), text))
            if len(got) >= 13 or _t.time() > deadline:
                break
        self.assertEqual([m for _, m in got], [f"message {n}" for n in range(1, 14)])
        self.assertLess(got[10][0] - got[9][0], 2, "reconnected when asked, not when hung up on")
        self.assertIn("PASS SCHMOOPIIE", self.heard[0])
        self.assertRegex(self.heard[0], r"NICK justinfan\d+\r\nJOIN #plaqueboymax\r\n")
        self.assertIn("PONG :tmi.twitch.tv", self.heard[1], "an unanswered PING drops the connection")
        self.assertIn("JOIN #plaqueboymax", self.heard[2], "joined again after RECONNECT")
        self.assertEqual(slept, [1])


def has_webp_encoder():
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True) \
        if shutil.which("ffmpeg") else None
    return bool(r and "libwebp" in r.stdout)


@unittest.skipUnless(has_webp_encoder(), "needs ffmpeg with a WebP encoder to make the test files")
class WatermarkSavedAsWebP(EndToEnd):
    """2026-09-30: the plaqueboymax watermark, held and saved in Safari on
    the iPad, arrived as image.webp - clipping.net serves its images as
    WebP - and Clip refused it for not being a PNG."""

    test_permitted_video_in_captioned_short_out = None
    test_a_failing_stage_is_retried_then_parked_then_retryable = None

    def setUp(self):
        super().setUp()
        self.cli("source", "add", "pbm", "--rights", "permission", "--evidence", "campaign page")
        d = Path(self.tmp.name)
        self.alpha, self.flat = d / "image.webp", d / "flat.webp"
        # a white mark on a see-through canvas, and the same on solid white
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=black@0.0:s=400x100,format=rgba,drawbox=x=20:y=20:w=200:h=50:color=white@1:t=fill",
                        "-frames:v", "1", "-c:v", "libwebp", str(self.alpha)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=white:s=400x100,drawbox=x=20:y=20:w=200:h=50:color=black:t=fill",
                        "-frames:v", "1", "-c:v", "libwebp", str(self.flat)], check=True)

    def kept(self):
        conn = db.connect(self.home / "clipper.db")
        return Path(conn.execute("SELECT watermark FROM sources WHERE name = 'pbm'").fetchone()[0])

    def test_a_webp_with_transparency_is_kept_as_a_png(self):
        code, said = self.cli("source", "rules", "pbm", "--watermark", str(self.alpha))
        self.assertEqual(code, 0, said)
        self.assertIn("watermark kept: 400x100", said)
        png = self.kept()
        self.assertEqual(png.read_bytes()[:8], render.PNG_SIGNATURE)
        self.assertTrue(render.has_transparency(png))

    def test_a_flat_webp_is_still_refused(self):
        # Converting must not invent transparency: a flat image made RGBA
        # would pass the check and burn a white box into every clip.
        code, said = self.cli("source", "rules", "pbm", "--watermark", str(self.flat))
        self.assertEqual(code, 2)
        self.assertIn("transparent", said)

    def test_not_an_image_says_so(self):
        text = Path(self.tmp.name) / "image.webp.txt"
        text.write_text("not a picture")
        code, said = self.cli("source", "rules", "pbm", "--watermark", str(text))
        self.assertEqual(code, 2)
        self.assertIn("not an image Clip can read", said)
        self.assertEqual(list((self.home / "watermarks").glob("*")), [], "nothing half-written left behind")


# ---------------------------------------------------------------- trimming a clip to its moment

from clipper import trim  # noqa: E402


def say(t, text):
    """A sentence as transcript words, 0.35 s a word."""
    return [{"start": round(t + i * 0.35, 2), "end": round(t + i * 0.35 + 0.3, 2), "word": " " + w, "p": 0.9}
            for i, w in enumerate(text.split())]


STREAM = [(3.2, "Okay chat listen to this."), (6.5, "So he walks in the studio."),
          (10.0, "Nobody knew who he was."), (13.5, "And then he grabs the mic."),
          (17.0, "He starts rapping over the beat."), (20.5, "Bro what is he doing!"),
          (22.5, "No way no way."), (24.5, "Oh my god."), (27.5, "That was crazy."),
          (31.0, "Anyway back to the beats."), (35.0, "Let me load the next one."),
          (39.0, "This one is hard."), (42.0, "Okay.")]
CALM = [(3.2, "Okay chat listen to this."), (10.0, "Here is the next beat."),
        (20.0, "It has a nice bassline."), (30.0, "Let me turn it up."), (42.0, "Okay.")]


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class TrimToTheMoment(unittest.TestCase):
    """"Can we figure out a way for Clip to trim the videos correctly? Based
    around the funny or crazy thing that happened?" (2026-10-01)

    A 50 s clip as a stream sounds: dead air, talking, a burst of yelling,
    talking, dead air. The loudness is measured by the real ffmpeg path
    (media.loudness) from generated audio; the words are written to match."""

    def loudness(self, burst=(22, 26)):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "a.wav"
            expr = (f"if(between(t,3,45),1,0)*if(between(t,{burst[0]},{burst[1]}),0.6,0.03)"
                    f"*sin(2*PI*220*t)") if burst else "if(between(t,3,45),0.03,0)*sin(2*PI*220*t)"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            f"aevalsrc='{expr}':s=16000:d=50", str(wav)], check=True)
            return media.loudness(wav)

    def words(self, script):
        return [w for t, s in script for w in say(t, s)]

    def test_setup_moment_and_reaction(self):
        start, end, why = trim.pick(self.words(STREAM), self.loudness(), 50.0, CFG)
        self.assertEqual((start, end), (9.7, 29.3))
        self.assertIn("the moment at 0:22", why[0])
        # it opens on a whole sentence and keeps "That was crazy."
        self.assertEqual(start, round(10.0 - trim.LEAD, 2))
        self.assertEqual(end, round(28.5 + trim.TAIL, 2))

    def test_it_does_not_open_on_and_or_so(self):
        # The moment at 25: the sentence nearest 12 s before it is "And then
        # he grabs the mic." - a clip that opens on "And" opens mid-thought.
        start, end, why = trim.pick(self.words(STREAM), self.loudness((25, 29)), 50.0, CFG)
        self.assertEqual(start, round(10.0 - trim.LEAD, 2))

    def test_no_moment_only_loses_the_dead_air(self):
        start, end, why = trim.pick(self.words(CALM), self.loudness(None), 50.0, CFG)
        self.assertEqual((start, end), (round(3.2 - trim.LEAD, 2), round(42.3 + trim.TAIL, 2)))
        self.assertIn("only the dead air", why[0])

    def test_without_a_loud_burst_the_strongest_words_stand_in(self):
        start, end, why = trim.pick(self.words(STREAM), self.loudness(None), 50.0, CFG)
        self.assertIn("strongest reaction in what is said", why[0])
        self.assertLess(end, 42.3, "not just the dead air: cut around 'That was crazy'")

    def test_too_short_takes_more_setup(self):
        words = self.words([(5.0, "Watch this guy right here."), (25.5, "Go."), (27.0, "Yes.")])
        start, end, why = trim.pick(words, self.loudness((30, 32)), 50.0, CFG)
        self.assertGreaterEqual(end - start, trim.MIN_LENGTH)
        self.assertEqual(start, round(5.0 - trim.LEAD, 2))

    def test_no_words_is_kept_whole(self):
        self.assertEqual(trim.pick([], self.loudness(), 50.0, CFG)[:2], (0.0, 50.0))
