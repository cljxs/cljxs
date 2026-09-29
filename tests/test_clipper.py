"""Clipper, Phase 1. No dependencies: the transcriber is swapped for the
"file" provider and fed real faster-whisper output captured 2026-09-28
(tests/fixtures/clipper-words-librivox.json, a public-domain LibriVox
reading). The end-to-end test needs ffmpeg and skips without it.
"""

import contextlib
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

from clipper import captions, cli, config, db, media, moments, pipeline, render, rights, score  # noqa: E402

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
