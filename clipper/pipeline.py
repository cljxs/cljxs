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

from clipper import campaign, captions, db, media, moments, postcopy, render, score, transcribe

MAX_ATTEMPTS = 3

# A clip probes a little longer than it was cut: the container ends on a
# frame and an audio packet, not on the second.
WHOLE_SLACK = 1.0


def folder(paths, video):
    return paths["media"] / str(video["id"])


def loudness_of(paths, video):
    """Per-second loudness, measured once and kept as loudness.json. The
    audio it is measured from is deleted after transcribing, so a video
    that reaches here without either gets its audio extracted again."""
    f = folder(paths, video)
    loud_file, audio = f / "loudness.json", f / "audio.wav"
    if loud_file.exists():
        return json.loads(loud_file.read_text())
    if not audio.exists():
        media.extract_audio(video["media_path"], audio)
    loud = media.loudness(audio)
    loud_file.write_text(json.dumps(loud))
    return loud


def stage_transcribe(conn, paths, cfg, video):
    f = folder(paths, video)
    audio = f / "audio.wav"
    if not audio.exists():
        media.extract_audio(video["media_path"], audio)
    t = transcribe.transcribe(audio, f / "transcript.json", cfg)
    loudness_of(paths, video)
    # Everything later reads transcript.json and loudness.json, never the
    # audio: 155 MB for an 81-minute episode, kept for nothing.
    audio.unlink()
    db.event(conn, "transcribe", f"{len(t['words'])} words ({t['provider']} {t['model']})",
             video_id=video["id"])
    return "transcribed"


def words_of(conn, paths, video):
    """A video's transcript words with its source's spellings applied - the
    one way every stage reads them, so what was scored is what is captioned."""
    src = conn.execute("SELECT spellings FROM sources WHERE id = ?", (video["source_id"],)).fetchone()
    words = json.loads((folder(paths, video) / "transcript.json").read_text())["words"]
    return transcribe.respell(words, json.loads(src["spellings"]) if src and src["spellings"] else [])


def already_a_clip(cfg, video):
    """A video no longer than the longest clip is one somebody already cut -
    a Twitch clip is at most 60 s - so it is used whole. Choosing its "best
    35 seconds" would cut off the setup or the payoff the person picked."""
    return video["duration"] <= cfg["max_clip_seconds"] + WHOLE_SLACK


def stage_find(conn, paths, cfg, video):
    words = words_of(conn, paths, video)
    if already_a_clip(cfg, video):
        with conn:
            conn.execute("DELETE FROM clips WHERE video_id = ?", (video["id"],))
            conn.execute("DELETE FROM candidates WHERE video_id = ?", (video["id"],))
            conn.execute(
                "INSERT INTO candidates (video_id, start, end, text, score, scorer, features, "
                "reasons, selected) VALUES (?, 0, ?, ?, 100, 'whole', '{}', ?, 1)",
                (video["id"], video["duration"], "".join(w["word"] for w in words).strip(),
                 db.as_json([f"already a clip ({video['duration']:.0f}s): used whole"])))
        db.event(conn, "find", f"{video['duration']:.0f}s - already a clip, used whole",
                 video_id=video["id"])
        return "found"
    loud = loudness_of(paths, video)
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (video["source_id"],)).fetchone()
    hunt = json.loads(src["hunt"]) if src["hunt"] else []
    sents = moments.sentences(words, cfg["sentence_pause_seconds"])
    wins = moments.windows(sents, cfg["min_clip_seconds"], cfg["max_clip_seconds"])
    scored = score.score_windows(sents, wins, words, loud, cfg, hunt)
    # At least one clip per topic on the campaign's list: the list is the
    # moments it says to make, so a smaller cap would silently drop some.
    chosen = moments.choose(scored, max(cfg["max_clips_per_video"], len(hunt)), cfg["min_score"], hunt)
    # A campaign that names its own moments for this episode is followed
    # instead: Clip's picks are kept as unselected candidates (they are what
    # learning needs) and the campaign's first batch is queued below.
    listed = campaign.entries(conn, video)
    if listed:
        chosen = []
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
    if listed:
        queued = campaign.queue(conn, paths, cfg, video, first_batch=True) or campaign.queue(
            conn, paths, cfg, video, n=cfg["max_clips_per_video"])
        db.event(conn, "find", f"the campaign lists {len(listed)} moments for this episode; queued its "
                               f"first {len(queued)}. More: clipper cuts {video['id']} N", video_id=video["id"])
        return "found"
    covered = sorted({t for c in chosen for t in c["topics"]})
    missing = [t["name"] for t in hunt if t["name"] not in covered]
    db.event(conn, "find", f"{len(sents)} sentences, {len(scored)} candidate windows, "
                           f"{len(chosen)} chosen (min score {cfg['min_score']})"
                           + (f"; campaign topics clipped: {', '.join(covered) or 'none'}" if hunt else "")
                           + (f"; not found: {', '.join(missing)}" if missing else ""),
             video_id=video["id"])
    return "found"


def stage_render(conn, paths, cfg, video):
    words = words_of(conn, paths, video)
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (video["source_id"],)).fetchone()
    # ties broken by the order they were queued: a campaign's moments all score
    # 100, and its own order ("start with these 6") is the order they are made
    picked = conn.execute("SELECT * FROM candidates WHERE video_id = ? AND selected = 1 "
                          "ORDER BY score DESC, id", (video["id"],)).fetchall()
    out_dir = paths["clips"] / str(video["id"])
    for c in picked:
        if conn.execute("SELECT 1 FROM clips WHERE candidate_id = ?", (c["id"],)).fetchone():
            continue
        # The next free number, not the position in this list: a clip added
        # later (a remake of a trending moment) would otherwise be handed a
        # number already on disk and overwrite that clip's file.
        rank = conn.execute("SELECT COALESCE(MAX(rank), 0) + 1 FROM clips WHERE video_id = ?",
                            (video["id"],)).fetchone()[0]
        out = out_dir / f"{rank:02d}.mp4"
        cut = conn.execute("SELECT * FROM cutlist WHERE id = ?", (c["cut_id"],)).fetchone() if c["cut_id"] else None
        scfg = render.source_cfg(cfg, src)
        hook = cut["hook"] if cut and cut["hook"] else None
        top = render.hook_top(render.check_watermark(src["watermark"], scfg) if src["watermark"] else None) \
            if hook else captions.HOOK_TOP
        ass = captions.build(words, c["start"], c["end"], cfg["caption_style"],
                             cfg["caption_uppercase"], cfg["caption_max_words"],
                             cfg["caption_max_chars"], hook=hook, hook_top=top)
        info = render.render(video["media_path"], out, c["start"], c["end"], ass,
                             scfg, watermark=src["watermark"])
        meta = {"source": src["name"], "rights": src["rights"], "evidence": src["evidence"],
                "watermark": Path(src["watermark"]).name if src["watermark"] else None,
                "hook": hook, "campaign_key": cut["key"] if cut else None,
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
        # Its title, caption and tags, drafted now so they can be read before
        # approving. Never fails the render: postcopy falls back to a plain
        # draft on its own, and anything else is a warning, not a lost clip.
        try:
            clip_id = conn.execute("SELECT id FROM clips WHERE candidate_id = ?", (c["id"],)).fetchone()[0]
            postcopy.write(conn, cfg, clip_id)
        except Exception as exc:
            db.event(conn, "copy", f"no draft for clip {rank}: {exc}", level="warn", video_id=video["id"])
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


def refind(conn, video_id):
    """Send a video back to be searched again - after a campaign's list is set,
    say. Refused once it has clips: finding again deletes and renumbers them,
    and an approved or posted clip must keep its number and its file."""
    v = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not v:
        raise ValueError(f"no video {video_id}")
    n = conn.execute("SELECT COUNT(*) FROM clips WHERE video_id = ?", (video_id,)).fetchone()[0]
    if n:
        raise ValueError(f"video {video_id} already has {n} clip(s); finding again would renumber "
                         f"them. Use `remake` for one more moment instead")
    if not (v["stage"] in ("found", "done") or (v["stage"] == "failed"
                                                and (v["error"] or "").startswith("found"))):
        raise ValueError(f"video {video_id} is at {v['stage']} - it has not been searched yet, "
                         f"`run` will search it with the current list")
    with conn:
        conn.execute("UPDATE videos SET stage = 'transcribed', attempts = 0, error = NULL, "
                     "updated_at = ? WHERE id = ?", (db.now(), video_id))
    return conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()


class Busy(RuntimeError):
    pass


def forget(conn, paths, video_id, including_posted=False):
    """Remove a video and everything made from it - a test run, a wrong
    upload - so its clips stop appearing for review. Clips, candidates,
    drafts, verdicts and publication records go, with the files on disk.
    What was spent stays in the ledger and the event log keeps its history:
    those are records of what happened, not of what is waiting.

    A clip that went out is refused unless asked for, because this is the
    only record Clip has of that post - and forgetting it here does not take
    it down there. Returns (title, clips removed, [posted urls])."""
    import shutil
    v = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not v:
        raise ValueError(f"no video {video_id}")
    posted = [p["url"] or f"clip {p['rank']} on {p['platform']}" for p in conn.execute(
        "SELECT c.rank, p.platform, p.url FROM publications p JOIN clips c ON c.id = p.clip_id "
        "WHERE c.video_id = ? AND p.status = 'posted'", (video_id,))]
    if posted and not including_posted:
        raise ValueError(f"video {video_id} has {len(posted)} posted clip(s): {', '.join(posted)}. "
                         f"Forgetting them does not take them down. Add --including-posted to go ahead")
    paths["lock"].parent.mkdir(parents=True, exist_ok=True)
    with open(paths["lock"], "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("a Clipper run is in progress - forget it when the run has finished")
        clips = "SELECT id FROM clips WHERE video_id = ?"
        n = conn.execute("SELECT COUNT(*) FROM clips WHERE video_id = ?", (video_id,)).fetchone()[0]
        with conn:
            for table in ("publications", "reviews", "post_copy"):
                conn.execute(f"DELETE FROM {table} WHERE clip_id IN ({clips})", (video_id,))
            conn.execute(f"UPDATE trending_clips SET remade_clip_id = NULL WHERE remade_clip_id IN ({clips})",
                         (video_id,))
            conn.execute("UPDATE trending_clips SET match_video_id = NULL, match_start = NULL, "
                         "match_end = NULL, match_score = NULL, match_text = NULL WHERE match_video_id = ?",
                         (video_id,))
            conn.execute("DELETE FROM clips WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM candidates WHERE video_id = ?", (video_id,))
            # The Dropbox file stays marked as picked up: forgetting a test
            # video must not make the next pickup bring it straight back.
            conn.execute("UPDATE pickups SET video_id = NULL, detail = 'forgotten' WHERE video_id = ?",
                         (video_id,))
            conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        for folder in (paths["media"] / str(video_id), paths["clips"] / str(video_id)):
            shutil.rmtree(folder, ignore_errors=True)
    db.event(conn, "forget", f"forgot video {video_id} ({v['title']}): {n} clip(s)"
             + (f", {len(posted)} of them posted" if posted else ""))
    return v["title"], n, posted


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
