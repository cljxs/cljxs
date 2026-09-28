"""Getting a permitted video in: from a file on the droplet, or an https URL
the rights holder gave you (a campaign's raw-footage link, your own storage).

What this deliberately does not do is download from YouTube. YouTube's Terms
of Service forbid downloading except through a download link YouTube itself
shows, whatever the video's licence - see DESIGN.md, "Content rights". Your
own uploads can be downloaded from YouTube Studio; a campaign usually hands
out source files. Both arrive here as a file or a URL.
"""

import hashlib
import shutil
import urllib.request
from pathlib import Path

from clipper import db, media, rights


def add_source(conn, cfg, name, rights_kind, evidence, url=None, attribution=None):
    why = rights.check(rights_kind, evidence, attribution, cfg["allow_noncommercial"])
    if why:
        raise ValueError(why)
    with conn:
        conn.execute("INSERT INTO sources (name, url, rights, evidence, attribution, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (name, url, rights_kind, evidence.strip(), attribution, db.now()))
    return conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()


def sha256(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            h.update(chunk)
    return h.hexdigest()


def need_space(folder, nbytes):
    """Refuse before copying rather than fill the droplet's disk: every agent
    on it writes to the same one. Twice the file: the source plus its audio,
    clips and temporaries."""
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if nbytes and free < 2 * nbytes:
        raise RuntimeError(f"not enough disk: {free / 1e9:.1f} GB free, need about "
                           f"{2 * nbytes / 1e9:.1f} GB for a {nbytes / 1e9:.1f} GB video")


def fetch(url, dest):
    if not url.startswith("https://"):
        raise ValueError("only https:// URLs - a plain http download can be altered on the way")
    req = urllib.request.Request(url, headers={"User-Agent": "clipper/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        need_space(dest.parent, int(r.headers.get("Content-Length") or 0))
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    return dest


def ingest(conn, paths, source_name, origin, title=None, move=False):
    """Register a video. Returns (row, is_new). The same bytes twice is the
    same video: it is recognised by hash and not processed again."""
    src = conn.execute("SELECT * FROM sources WHERE name = ? AND active = 1",
                       (source_name,)).fetchone()
    if not src:
        raise ValueError(f"no active source named {source_name!r} - add it first with "
                         f"`python3 -m clipper source add` and the evidence you may use it")
    staging = paths["media"] / "incoming"
    staging.mkdir(parents=True, exist_ok=True)
    if origin.startswith(("http://", "https://")):
        suffix = Path(origin.split("?")[0]).suffix or ".mp4"
        tmp = fetch(origin, staging / f"download{suffix}")
    else:
        path = Path(origin).expanduser()
        if not path.is_file():
            raise FileNotFoundError(origin)
        need_space(staging, path.stat().st_size)
        tmp = staging / f"copy{path.suffix}"
        (shutil.move if move else shutil.copy2)(str(path), str(tmp))
    digest = sha256(tmp)
    seen = conn.execute("SELECT * FROM videos WHERE sha256 = ?", (digest,)).fetchone()
    if seen:
        tmp.unlink()
        return seen, False
    info = media.probe(tmp)
    if not (info["has_video"] and info["has_audio"]):
        tmp.unlink()
        raise ValueError(f"{origin} needs both video and audio (video={info['has_video']}, "
                         f"audio={info['has_audio']})")
    with conn:
        cur = conn.execute(
            "INSERT INTO videos (source_id, title, origin, sha256, media_path, duration, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, '', ?, ?, ?)",
            (src["id"], title or Path(origin.split("?")[0]).stem, origin, digest,
             info["duration"], db.now(), db.now()))
        vid = cur.lastrowid
        folder = paths["media"] / str(vid)
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / f"source{tmp.suffix}"
        tmp.replace(final)
        conn.execute("UPDATE videos SET media_path = ? WHERE id = ?", (str(final), vid))
    db.event(conn, "ingest", f"ingested {origin} ({info['duration'] / 60:.1f} min)", video_id=vid)
    return conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone(), True
