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
            "x264_preset": "ultrafast", "max_clip_seconds": 30}))
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
        self.assertEqual(searched[0], "Tao Reads", "the permitted creator is searched first")
        self.assertEqual(r["matched"], 1)
        snap = report.snapshot(self.conn, self.paths, self.cfg)["spotter"]
        states = {t["creator"]: t["state"] for t in snap["trending"]}
        self.assertEqual(states, {"Tao Reads": "matched", "Mega Streamer": "no_permission"})
        units = sum(trends.UNITS[ep] for ep, _ in calls)
        self.assertEqual(trends.units_today(self.conn), units, "every call is on the ledger")
        self.assertEqual(r["units"], units)

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
        bad = self.conn.execute("SELECT id FROM trending_clips WHERE creator_id = ?", (BIG,)).fetchone()[0]
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
