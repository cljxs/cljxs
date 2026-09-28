"""The stages, as a state machine on videos.stage:

    ingested -> transcribed -> found -> done
                      (any stage, 3 failures) -> failed

Each stage reads the files the previous one wrote and is safe to run twice,
so a crash, a reboot or a timeout costs the stage in progress and nothing
before it. The transcript in particular is cached on disk and never redone.
"""

import fcntl
import json
import traceback
from pathlib import Path

from clipper import captions, db, media, moments, render, score, transcribe

MAX_ATTEMPTS = 3


def folder(paths, video):
    return paths["media"] / str(video["id"])


def stage_transcribe(conn, paths, cfg, video):
    f = folder(paths, video)
    audio = f / "audio.wav"
    if not audio.exists():
        media.extract_audio(video["media_path"], audio)
    t = transcribe.transcribe(audio, f / "transcript.json", cfg)
    db.event(conn, "transcribe", f"{len(t['words'])} words ({t['provider']} {t['model']})",
             video_id=video["id"])
    return "transcribed"


def stage_find(conn, paths, cfg, video):
    f = folder(paths, video)
    words = json.loads((f / "transcript.json").read_text())["words"]
    loud_file = f / "loudness.json"
    if loud_file.exists():
        loud = json.loads(loud_file.read_text())
    else:
        loud = media.loudness(f / "audio.wav")
        loud_file.write_text(json.dumps(loud))
    sents = moments.sentences(words, cfg["sentence_pause_seconds"])
    wins = moments.windows(sents, cfg["min_clip_seconds"], cfg["max_clip_seconds"])
    scored = score.score_windows(sents, wins, words, loud, cfg)
    chosen = moments.choose(scored, cfg["max_clips_per_video"], cfg["min_score"])
    picked = {(c["i"], c["j"]) for c in chosen}
    with conn:
        conn.execute("DELETE FROM clips WHERE video_id = ?", (video["id"],))
        conn.execute("DELETE FROM candidates WHERE video_id = ?", (video["id"],))
        for c in scored:
            first, last = sents[c["i"]]["first"], sents[c["j"]]["last"]
            start, end = moments.cut_points(words, first, last)
            conn.execute(
                "INSERT INTO candidates (video_id, start, end, text, score, scorer, features, "
                "reasons, selected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (video["id"], start, end, c["text"], c["score"], score.SCORER,
                 db.as_json(c["features"]), db.as_json(c["reasons"]),
                 1 if (c["i"], c["j"]) in picked else 0))
    db.event(conn, "find", f"{len(sents)} sentences, {len(scored)} candidate windows, "
                           f"{len(chosen)} chosen (min score {cfg['min_score']})",
             video_id=video["id"])
    return "found"


def stage_render(conn, paths, cfg, video):
    f = folder(paths, video)
    words = json.loads((f / "transcript.json").read_text())["words"]
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (video["source_id"],)).fetchone()
    picked = conn.execute("SELECT * FROM candidates WHERE video_id = ? AND selected = 1 "
                          "ORDER BY score DESC", (video["id"],)).fetchall()
    out_dir = paths["clips"] / str(video["id"])
    for rank, c in enumerate(picked, start=1):
        if conn.execute("SELECT 1 FROM clips WHERE candidate_id = ?", (c["id"],)).fetchone():
            continue
        out = out_dir / f"{rank:02d}.mp4"
        ass = captions.build(words, c["start"], c["end"], cfg["caption_style"],
                             cfg["caption_uppercase"], cfg["caption_max_words"],
                             cfg["caption_max_chars"])
        info = render.render(video["media_path"], out, c["start"], c["end"], ass, cfg)
        meta = {"source": src["name"], "rights": src["rights"], "evidence": src["evidence"],
                "attribution": src["attribution"], "video_title": video["title"],
                "origin": video["origin"], "start": c["start"], "end": c["end"],
                "score": c["score"], "scorer": c["scorer"],
                "features": json.loads(c["features"]), "reasons": json.loads(c["reasons"]),
                "text": c["text"], "width": info["width"], "height": info["height"],
                "duration": info["duration"]}
        out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
        with conn:
            conn.execute("INSERT INTO clips (video_id, candidate_id, rank, path, start, end, score, "
                         "meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (video["id"], c["id"], rank, str(out), c["start"], c["end"], c["score"],
                          db.as_json(meta), db.now()))
        db.event(conn, "render", f"clip {rank}: {c['start']:.1f}-{c['end']:.1f}s, score {c['score']}",
                 video_id=video["id"])
    write_summary(conn, out_dir, video)
    return "done"


def clock(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def write_summary(conn, out_dir, video):
    """clips/<video>/README.md - Phase 1's review screen, until the Deck has one."""
    rows = conn.execute("SELECT * FROM clips WHERE video_id = ? ORDER BY rank", (video["id"],)).fetchall()
    out = [f"# Clips from: {video['title']}", "", f"Source file: {video['origin']}", ""]
    if not rows:
        out.append("No moment scored above the minimum. Lower min_score, or this video "
                   "may not have one.")
    for r in rows:
        m = json.loads(r["meta"])
        out += [f"## CLIP #{r['rank']} - score {r['score']:.0f}/100",
                f"- file: {Path(r['path']).name}",
                f"- timestamp: {clock(r['start'])}-{clock(r['end'])} ({r['end'] - r['start']:.0f}s)",
                f"- why: {'; '.join(m['reasons']) or 'no single strong feature'}",
                f"- says: \"{m['text'][:280]}{'...' if len(m['text']) > 280 else ''}\"", ""]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "README.md").write_text("\n".join(out) + "\n")


STAGES = {"ingested": stage_transcribe, "transcribed": stage_find, "found": stage_render}


def advance(conn, paths, cfg, video):
    """Run one video forward until done or a stage fails."""
    while video["stage"] in STAGES:
        stage = video["stage"]
        try:
            nxt = STAGES[stage](conn, paths, cfg, video)
            with conn:
                conn.execute("UPDATE videos SET stage = ?, error = NULL, attempts = 0, "
                             "updated_at = ? WHERE id = ?", (nxt, db.now(), video["id"]))
        except Exception as exc:
            attempts = video["attempts"] + 1
            dead = attempts >= MAX_ATTEMPTS
            with conn:
                conn.execute("UPDATE videos SET attempts = ?, error = ?, stage = ?, updated_at = ? "
                             "WHERE id = ?", (attempts, f"{stage}: {exc}"[:1000],
                                              "failed" if dead else stage, db.now(), video["id"]))
            db.event(conn, stage, f"{exc}\n{traceback.format_exc(limit=3)}"[:2000], level="error",
                     video_id=video["id"])
            return False
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (video["id"],)).fetchone()
    return video["stage"] == "done"


class Busy(RuntimeError):
    pass


def run(conn, paths, cfg, video_id=None):
    """Advance every unfinished video. One run at a time: two transcribers on
    a one-CPU droplet is two slow ones and an out-of-memory kill."""
    paths["lock"].parent.mkdir(parents=True, exist_ok=True)
    with open(paths["lock"], "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another Clipper run is in progress")
        q = "SELECT * FROM videos WHERE stage NOT IN ('done', 'failed')"
        args = ()
        if video_id is not None:
            q, args = "SELECT * FROM videos WHERE id = ?", (video_id,)
        results = {}
        for v in conn.execute(q + " ORDER BY id", args).fetchall():
            results[v["id"]] = advance(conn, paths, cfg, v)
        return results
