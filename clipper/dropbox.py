"""The Dropbox pickup: every new video in a source's shared Dropbox folder
is downloaded and ingested, so you drop clips into one folder instead of
sending a link for each.

HOW, from Dropbox's own API spec (dropbox-api-spec, read 2026-09-30):
files/list_folder takes a shared_link and lists that folder (one level -
"only non-recursive mode is supported for shared link"), and
sharing/get_shared_link_file downloads one file in it. Both accept APP
authentication - the app's key and secret - so there is no sign-in: a
shared link is already public to whoever holds it, and the app only reads
what the link shows.

SETTING UP, ONCE: a Dropbox app (App folder access, with the
files.metadata.read and sharing.read permissions ticked), its key and secret
stored with set-credential.py as DROPBOX_APP_KEY and DROPBOX_APP_SECRET, and
the folder's share link on its source:

    python3 -m clipper source rules plaqueboymax --dropbox-folder LINK

A file is picked up once, by Dropbox's id and content hash - re-uploading
the same name with new content is a new pickup. A file that is not a video
is noted once and left alone. Nothing in the folder is changed or deleted.

Believed, not yet verified: there was no Dropbox app to test against when
this was written. The first `clipper pickup` is the check.
"""

import base64
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from clipper import db, ingest, youtube

RPC = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
VIDEO = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


class NotSetUp(RuntimeError):
    pass


def _auth():
    c = youtube.creds()
    key, secret = c.get("DROPBOX_APP_KEY"), c.get("DROPBOX_APP_SECRET")
    if not key or not secret:
        raise NotSetUp("no DROPBOX_APP_KEY / DROPBOX_APP_SECRET - see clipper/README.md, "
                       "'Dropbox folder pickup'")
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


def _why(raw):
    try:
        d = json.loads(raw)
        return str(d.get("error_summary") or d.get("error") or d)[:300]
    except ValueError:
        return (raw or b"")[:300].decode("utf-8", "replace")


def _rpc(route, body, auth, urlopen=urllib.request.urlopen):
    req = urllib.request.Request(f"{RPC}/{route}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dropbox {route}: {e.code} {_why(e.read())}")


def list_videos(link, auth, rpc=_rpc):
    """Every file in the shared folder (not its subfolders), as Dropbox's
    metadata dicts."""
    d = rpc("files/list_folder", {"path": "", "shared_link": {"url": link}, "limit": 2000}, auth)
    files = list(d["entries"])
    while d.get("has_more"):
        d = rpc("files/list_folder/continue", {"cursor": d["cursor"]}, auth)
        files += d["entries"]
    return [f for f in files if f.get(".tag") == "file"]


def download(link, name, dest, auth, size=0, urlopen=urllib.request.urlopen):
    """One file of the shared folder to `dest`, checked for room first."""
    ingest.need_space(dest.parent, size)
    # No body and so no Content-Type: a download-style route refuses the
    # form type urllib would add for an empty body.
    req = urllib.request.Request(f"{CONTENT}/sharing/get_shared_link_file", method="POST", headers={
        "Authorization": auth, "Dropbox-API-Arg": json.dumps({"url": link, "path": "/" + name})})
    try:
        with urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except urllib.error.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"Dropbox download of {name}: {e.code} {_why(e.read())}")
    return dest


def _key(f):
    """What makes a file the same file: Dropbox's id and its content hash
    (with the path, size and time for a listing that leaves them out)."""
    return (f.get("id") or f.get("path_lower") or f["name"],
            f.get("content_hash") or f"{f.get('size')}:{f.get('server_modified')}")


def _note(conn, key, f, status, detail, video_id=None):
    """Record what came of a file. An event only when that changed, so a
    file that keeps failing the same way is one line, not one a run."""
    old = conn.execute("SELECT status, detail FROM pickups WHERE file_id = ? AND content_hash = ?",
                       key).fetchone()
    with conn:
        conn.execute("INSERT OR REPLACE INTO pickups (file_id, content_hash, path, size, status, video_id, "
                     "detail, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (*key, f["name"], f.get("size"), status, video_id, detail, db.now()))
    if not old or (old["status"], old["detail"]) != (status, detail):
        db.event(conn, "pickup", f"{f['name']}: {status} - {detail}",
                 level="info" if status == "ingested" else "warn", video_id=video_id)


def pickup(conn, paths, cfg, auth=None, lister=list_videos, fetch=download):
    """Pick up every new video in every source's Dropbox folder. Returns
    {"new": [video ids], "skipped": n, "failed": n}."""
    out = {"new": [], "skipped": 0, "failed": 0}
    sources = conn.execute("SELECT * FROM sources WHERE active = 1 AND dropbox_folder IS NOT NULL "
                           "ORDER BY id").fetchall()
    if not sources:
        return out
    auth = auth or _auth()
    staging = paths["media"] / "incoming"
    staging.mkdir(parents=True, exist_ok=True)
    for src in sources:
        for f in lister(src["dropbox_folder"], auth):
            key = _key(f)
            seen = conn.execute("SELECT status FROM pickups WHERE file_id = ? AND content_hash = ?",
                                key).fetchone()
            if seen and seen["status"] in ("ingested", "refused"):
                out["skipped"] += 1
                continue
            suffix = Path(f["name"]).suffix.lower()
            if suffix not in VIDEO:
                _note(conn, key, f, "refused", f"not a video ({', '.join(sorted(VIDEO))})")
                continue
            tmp = staging / f"dropbox{suffix}"
            try:
                fetch(src["dropbox_folder"], f["name"], tmp, auth, f.get("size") or 0)
                row, new = ingest.ingest(conn, paths, src["name"], str(tmp), title=Path(f["name"]).stem,
                                         move=True)
            except (ValueError, OSError, RuntimeError) as exc:
                tmp.unlink(missing_ok=True)
                out["failed"] += 1
                _note(conn, key, f, "failed", str(exc)[:300])
                continue
            if new:
                with conn:
                    conn.execute("UPDATE videos SET origin = ? WHERE id = ?",
                                 (f"dropbox: {src['name']}/{f['name']}", row["id"]))
                out["new"].append(row["id"])
            _note(conn, key, f, "ingested", f"video {row['id']}" + ("" if new else " (already had it)"),
                  row["id"])
    return out
