"""Approve, reject, post.

Approving a clip in the village is the whole instruction: it records your
verdict, makes sure the clip has its title, caption and tags, and posts it to
every platform in `platforms` - each with the words composed for that
platform by postcopy.compose(), which always includes the source's required
tags and credit line.

Per platform:

* YouTube: uploaded through the Data API (youtube.py) once you have signed
  in with `youtube-login`. Until then the post waits as `needs_setup` and
  goes out on the next `publish` after you sign in.
* TikTok: TikTok's posting API needs an approved developer app, a website
  and a review (DESIGN.md §13). Until that exists the post is `manual`:
  the village gives you the file and the caption to paste, and a button to
  mark it posted so it is never posted twice.

Permission is checked again at posting time, not only at ingest: a source
deactivated since the clip was cut (a campaign ended, a licence withdrawn)
blocks the post.
"""

import fcntl
from contextlib import contextmanager

from clipper import config, db, postcopy, youtube

FINAL = ("posted",)


def _clip(conn, clip_id):
    c = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not c:
        raise ValueError(f"no clip {clip_id}")
    return c


def _source(conn, clip):
    return conn.execute("SELECT s.* FROM sources s JOIN videos v ON v.source_id = s.id "
                        "WHERE v.id = ?", (clip["video_id"],)).fetchone()


def review(conn, clip_id, verdict, reason=None):
    with conn:
        conn.execute("INSERT INTO reviews (clip_id, verdict, reason, ts) VALUES (?, ?, ?, ?)",
                     (clip_id, verdict, reason, db.now()))


def reject(conn, clip_id, reason):
    clip = _clip(conn, clip_id)
    if clip["status"] not in ("rendered",):
        raise ValueError(f"clip {clip_id} is {clip['status']} - only a clip waiting for you can be rejected")
    if not (reason or "").strip():
        raise ValueError("say why - the reason is what Clip learns from")
    review(conn, clip_id, "reject", reason.strip())
    with conn:
        conn.execute("UPDATE clips SET status = 'rejected' WHERE id = ?", (clip_id,))
    db.event(conn, "review", f"clip {clip_id} rejected: {reason.strip()}", clip_id=clip_id)


def approve(conn, cfg, clip_id, note=None, post=True, **kw):
    """Record the verdict, then post. Returns publish()'s results - or [] with
    post=False, when the caller posts separately (the village: an upload can
    outlast Safari's patience, and a dropped request once left an approval
    unrecorded, 2026-10-07)."""
    clip = _clip(conn, clip_id)
    if clip["status"] != "rendered":
        raise ValueError(f"clip {clip_id} is already {clip['status']}")
    src = _source(conn, clip)
    if not src or not src["active"]:
        raise ValueError("this clip's source is no longer active - permission withdrawn, so it "
                         "cannot be posted")
    postcopy.write(conn, cfg, clip_id)
    review(conn, clip_id, "approve", note)
    with conn:
        conn.execute("UPDATE clips SET status = 'approved' WHERE id = ?", (clip_id,))
        for p in cfg["platforms"]:
            conn.execute("INSERT OR IGNORE INTO publications (clip_id, platform, status, created_at) "
                         "VALUES (?, ?, 'queued', ?)", (clip_id, p, db.now()))
    db.event(conn, "review", f"clip {clip_id} approved", clip_id=clip_id)
    return publish(conn, cfg, clip_id, **kw) if post else []


@contextmanager
def one_at_a_time():
    """Only one publish at a time. Two approvals in quick succession start two
    background publishes, and both would find the same queued post and upload
    it twice. The second waits here, then finds it already posted. Closing
    the file releases the lock, also when the process dies mid-upload."""
    path = config.home() / "publish.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def posted_today(conn, platform):
    return conn.execute("SELECT COUNT(*) FROM publications WHERE platform = ? AND status = 'posted' "
                        "AND substr(posted_at, 1, 10) = ?", (platform, db.now()[:10])).fetchone()[0]


def _set(conn, pub_id, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE publications SET {cols} WHERE id = ?", (*fields.values(), pub_id))


def publish(conn, cfg, clip_id=None, upload=None):
    """Post whatever is approved and not yet out. One clip, or all of them.
    `upload` replaces youtube.upload in tests."""
    with one_at_a_time():
        return _publish(conn, cfg, clip_id, upload)


def _publish(conn, cfg, clip_id=None, upload=None):
    q = ("SELECT p.* FROM publications p JOIN clips c ON c.id = p.clip_id WHERE c.status IN "
         "('approved', 'posted') AND p.status NOT IN ('posted', 'manual')")
    rows = conn.execute(q + (" AND p.clip_id = ?" if clip_id else "") + " ORDER BY p.id",
                        (clip_id,) if clip_id else ()).fetchall()
    results = []
    for pub in rows:
        clip = _clip(conn, pub["clip_id"])
        src = _source(conn, clip)
        if not src["active"]:
            _set(conn, pub["id"], status="failed", detail="source deactivated - permission withdrawn")
            results.append((pub["platform"], "failed", "source deactivated"))
            continue
        copy_row = postcopy.write(conn, cfg, clip["id"])
        words = postcopy.compose(copy_row, src)
        if pub["platform"] == "tiktok":
            _set(conn, pub["id"], status="manual",
                 detail="TikTok posting needs an approved TikTok app - post this one from the village: "
                        "Download, then paste the caption")
            results.append(("tiktok", "manual", words["tiktok"]))
            continue
        if pub["platform"] != "youtube":
            _set(conn, pub["id"], status="failed", detail=f"no poster for {pub['platform']}")
            continue
        if posted_today(conn, "youtube") >= int(cfg["max_daily_uploads"]):
            _set(conn, pub["id"], status="queued",
                 detail=f"today's {cfg['max_daily_uploads']} YouTube posts are done - goes out tomorrow")
            results.append(("youtube", "queued", "daily cap"))
            continue
        try:
            done = (upload or youtube.upload)(clip["path"], words["youtube"], cfg["youtube_privacy"],
                                              cfg["youtube_category"])
        except youtube.NotSetUp as e:
            _set(conn, pub["id"], status="needs_setup", detail=str(e))
            results.append(("youtube", "needs_setup", str(e)))
            continue
        except Exception as e:
            attempts = pub["attempts"] + 1
            _set(conn, pub["id"], status="failed", attempts=attempts, detail=str(e)[:500])
            db.event(conn, "publish", f"youtube upload of clip {clip['id']} failed: {e}", level="error",
                     clip_id=clip["id"])
            results.append(("youtube", "failed", str(e)))
            continue
        note = "" if done["privacy"] == cfg["youtube_privacy"] else (
            f" - YouTube set it {done['privacy']}: uploads stay private until the Google Cloud "
            f"project passes YouTube's API audit")
        _set(conn, pub["id"], status="posted", remote_id=done["id"], url=done["url"],
             privacy=done["privacy"], posted_at=db.now(), attempts=pub["attempts"] + 1,
             detail=f"posted {done['privacy']}{note}")
        db.record_cost(conn, "youtube-upload", "upload", 0.0, units=100, note=f"clip {clip['id']}")
        db.event(conn, "publish", f"clip {clip['id']} posted to YouTube: {done['url']} ({done['privacy']})",
                 clip_id=clip["id"])
        results.append(("youtube", "posted", done["url"]))
    for cid in {r["clip_id"] for r in rows}:
        _settle(conn, cid)
    return results


def _settle(conn, clip_id):
    """A clip is 'posted' once every platform really is. A TikTok post waiting
    for you to paste it is not posted, and the clip does not say it is."""
    left = conn.execute("SELECT COUNT(*) FROM publications WHERE clip_id = ? AND status != 'posted'",
                        (clip_id,)).fetchone()[0]
    if not left:
        with conn:
            conn.execute("UPDATE clips SET status = 'posted' WHERE id = ?", (clip_id,))


def mark_posted(conn, clip_id, platform, url=None):
    """You posted it by hand (TikTok, for now). Recorded so it is never posted
    twice and so its numbers can be collected later."""
    pub = conn.execute("SELECT * FROM publications WHERE clip_id = ? AND platform = ?",
                       (clip_id, platform)).fetchone()
    if not pub:
        raise ValueError(f"clip {clip_id} was never approved for {platform}")
    if pub["status"] == "posted":
        raise ValueError(f"clip {clip_id} is already marked posted on {platform}")
    _set(conn, pub["id"], status="posted", url=url, posted_at=db.now(), detail="posted by hand")
    _settle(conn, clip_id)
