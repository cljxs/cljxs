"""What Clip keeps on disk, and letting go of what no longer earns its space.

Owner, 2026-10-20: "I want to delete the clips because I'm assuming they take
up unnecessary storage space". A clip's video is needed until it is out: to
watch, approve, download for TikTok and fix subtitles. Once it is posted
everywhere (YouTube, and Mark posted for TikTok) or rejected, nothing here
reads it again, so its video files go and its record stays - the post links,
the words, the review - so it is never posted twice and its history is kept.

The source videos are the large files: a whole show, kept so more moments can
be cut from it. Those go only when asked (`clipper cleanup --sources --yes`),
and only for a video with nothing left waiting.
"""

from pathlib import Path

# Beside 05.mp4: its copy without subtitles, the grid's still, the subtitle
# file, a half-written render. 05.json stays - it is the clip's record.
CLIP_FILES = (".mp4", ".clean.mp4", ".poster.jpg", ".ass", ".part.mp4", ".clean.part.mp4")
OPEN = ("rendered", "approved")


def clip_files(path):
    """The files a clip's video occupies, those that exist."""
    path = Path(path)
    stem = path.with_suffix("")
    return [f for f in (Path(f"{stem}{ext}") for ext in CLIP_FILES) if f.is_file()]


def drop_clip_files(clip):
    """Delete a finished clip's video files. Returns the bytes freed."""
    freed = 0
    for f in clip_files(clip["path"]):
        freed += f.stat().st_size
        f.unlink(missing_ok=True)
    return freed


def _size(files):
    return sum(f.stat().st_size for f in files)


def finished_clips(conn):
    return conn.execute("SELECT * FROM clips WHERE status IN ('posted', 'rejected')").fetchall()


def finished_sources(conn):
    """Videos done being cut: finished rendering, nothing waiting for you, and
    the source still on disk. A failed video is left - it may be retried."""
    out = []
    for v in conn.execute("SELECT * FROM videos WHERE stage = 'done'"):
        waiting = conn.execute("SELECT COUNT(*) FROM clips WHERE video_id = ? AND status IN (?, ?)",
                               (v["id"], *OPEN)).fetchone()[0]
        src = Path(v["media_path"] or "")
        if not waiting and v["media_path"] and src.is_file():
            out.append((v, src))
    return out


def report(conn, paths):
    """{what: (count, bytes)} - where the space is going."""
    clips = [c for c in conn.execute("SELECT * FROM clips").fetchall()]
    waiting = [f for c in clips if c["status"] in OPEN for f in clip_files(c["path"])]
    done = [f for c in clips if c["status"] not in OPEN for f in clip_files(c["path"])]
    sources = [Path(v["media_path"]) for v in conn.execute("SELECT media_path FROM videos")
               if v["media_path"] and Path(v["media_path"]).is_file()]
    freeable = [src for _v, src in finished_sources(conn)]
    return {"clips waiting for you": (len(waiting), _size(waiting)),
            "finished clips' videos (posted or rejected)": (len(done), _size(done)),
            "source videos": (len(sources), _size(sources)),
            "source videos with nothing left waiting": (len(freeable), _size(freeable))}


def cleanup(conn, sources=False, apply=False):
    """(lines, bytes) - what goes, and with apply=True, gone. Finished clips'
    videos always; source videos only with sources=True."""
    lines, total = [], 0
    for c in finished_clips(conn):
        files = clip_files(c["path"])
        if files:
            size = _size(files)
            total += size
            lines.append(f"clip {c['id']} ({c['status']}): {len(files)} file(s), {mb(size)}")
            if apply:
                drop_clip_files(c)
    if sources:
        for v, src in finished_sources(conn):
            size = src.stat().st_size
            total += size
            lines.append(f"video {v['id']} source ({v['title']}): {mb(size)}")
            if apply:
                # Nothing is recorded: whether the source is here is the file
                # itself, and cutting from it again says so (pipeline.cut_clip).
                src.unlink(missing_ok=True)
    return lines, total


def mb(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"
