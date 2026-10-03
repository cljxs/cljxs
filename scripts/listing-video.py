#!/usr/bin/env python3
"""
listing-video.py — a short listing video made from the listing's own photos.

    listing-video.py 4412345678               one listing, from its Etsy photos
    listing-video.py all                      every listing in the latest store snapshot without one
    listing-video.py all --redo               ...and remake the ones that exist
    listing-video.py --images a.png b.png --out x.mp4    from files (a build, before it is listed)

WHY. Etsy says listings with video are more likely to sell, and a video
thumbnail plays in search as a shopper hovers. The shop's listings have none
because making one by hand is a chore nobody does. This makes one from the
photos the listing already has - a slow zoom over each, crossfading into the
next - with no model and no cost: ffmpeg does the work.

ETSY'S LIMITS, CHECKED ON EVERY FILE (read 2026-09-25 from Etsy Help and
sellers' guides): 5 to 15 seconds, under 100 MB, at least 500 px, and no
sound - Etsy strips audio, so none is written. A file outside those is
reported as a failure, not handed over.

Uploading is the owner's: in Etsy, Edit listing -> Photos and video. Nothing
here can change a listing on Etsy. The Deck's Emily house lists every video
with a download link, for saving on the iPad and uploading from there.

Needs ffmpeg on the droplet (Ubuntu's own package):  apt install -y ffmpeg

Videos: agents/emily/shop/videos/<listing_id>.mp4, with index.json beside
them saying which listing each is and when it was made.

Standard library only (plus the ffmpeg binary).
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
VIDEOS = ROOT / "agents" / "emily" / "shop" / "videos"
INDEX = VIDEOS / "index.json"

# Etsy's limits for a listing video.
MIN_SECONDS, MAX_SECONDS = 5, 15
MAX_BYTES = 100 * 1024 * 1024
MIN_SIDE = 500

# What this makes. Square, because Etsy's search grid is square and a square
# video is never letterboxed there.
SIDE = 1080
FPS = 30
SECONDS = 10
MAX_IMAGES = 4          # more photos in ten seconds is a slideshow nobody can read
FADE = 0.6              # seconds of crossfade between photos
ZOOM = 1.08             # how far each photo drifts in over its time on screen
BACKGROUND = "white"    # behind a photo that is not square


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ffmpeg():
    return shutil.which("ffmpeg"), shutil.which("ffprobe")


# ------------------------------------------------------------------ the video

def plan(n, seconds):
    """How long each photo is on screen, and where each crossfade starts, so
    the whole video is `seconds` long. Pure arithmetic, so it is tested."""
    if n < 1:
        raise ValueError("no photos to make a video from")
    fade = FADE if n > 1 else 0.0
    each = (seconds + (n - 1) * fade) / n
    offsets = [round(k * (each - fade), 3) for k in range(1, n)]
    return round(each, 3), fade, offsets


def command(images, out, seconds=SECONDS):
    """The ffmpeg command line - an argument list, never a shell string."""
    each, fade, offsets = plan(len(images), seconds)
    frames = int(round(each * FPS))
    step = (ZOOM - 1) / max(frames, 1)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for img in images:
        cmd += ["-i", str(img)]
    parts = []
    for i in range(len(images)):
        # Fit inside a square twice the output size, pad to square, then zoom
        # down to the output: zooming from a larger frame keeps the drift
        # smooth instead of stepping a pixel at a time.
        parts.append(
            f"[{i}:v]scale={2 * SIDE}:{2 * SIDE}:force_original_aspect_ratio=decrease,"
            f"pad={2 * SIDE}:{2 * SIDE}:(ow-iw)/2:(oh-ih)/2:color={BACKGROUND},setsar=1,"
            f"zoompan=z='min(zoom+{step:.6f},{ZOOM})':d={frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={SIDE}x{SIDE}:fps={FPS},"
            f"format=yuv420p[v{i}]")
    last = "v0"
    for k, off in enumerate(offsets, start=1):
        parts.append(f"[{last}][v{k}]xfade=transition=fade:duration={fade}:offset={off}[x{k}]")
        last = f"x{k}"
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]",
            "-t", str(seconds), "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart", str(out)]
    return cmd


def probe(path):
    """(seconds, width, height, has_audio) as ffprobe reads the file."""
    _, fp = ffmpeg()
    r = subprocess.run([fp, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height", "-of", "json",
                        str(path)], capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout or "{}")
    streams = d.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return (float((d.get("format") or {}).get("duration") or 0),
            int(video.get("width") or 0), int(video.get("height") or 0),
            any(s.get("codec_type") == "audio" for s in streams))


def problems(path):
    """Everything about a finished file that Etsy would refuse. [] is good."""
    out = []
    size = Path(path).stat().st_size
    seconds, w, h, audio = probe(path)
    if not MIN_SECONDS <= seconds <= MAX_SECONDS + 0.05:
        out.append(f"{seconds:.1f}s long; Etsy takes {MIN_SECONDS} to {MAX_SECONDS}")
    if size > MAX_BYTES:
        out.append(f"{size / 1e6:.0f} MB; Etsy takes up to {MAX_BYTES // (1024 * 1024)} MB")
    if min(w, h) < MIN_SIDE:
        out.append(f"{w}x{h}; Etsy wants at least {MIN_SIDE}px")
    if audio:
        out.append("has a sound track; Etsy strips it")
    return out


def make(images, out, seconds=SECONDS):
    """Write the video. Returns (ok, message)."""
    ff, fp = ffmpeg()
    if not ff or not fp:
        return False, "ffmpeg is not installed. On the droplet:  apt install -y ffmpeg"
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        return False, f"--seconds must be {MIN_SECONDS} to {MAX_SECONDS} (Etsy's limits)"
    images = [Path(i) for i in images][:MAX_IMAGES]
    missing = [str(i) for i in images if not i.is_file()]
    if not images or missing:
        return False, "no photos" if not images else f"missing: {', '.join(missing)}"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    cmd = command(images, tmp, seconds)
    cmd[0] = ff
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        return False, f"ffmpeg failed: {(r.stderr or '').strip()[-300:]}"
    bad = problems(tmp)
    if bad:
        tmp.unlink(missing_ok=True)
        return False, "not usable on Etsy: " + "; ".join(bad)
    tmp.replace(out)
    secs, w, h, _ = probe(out)
    return True, f"{out.name}: {secs:.1f}s, {w}x{h}, {out.stat().st_size / 1e6:.1f} MB, {len(images)} photo(s)"


# ------------------------------------------------------------------ from Etsy

def listing_photos(listing_id, key, ep):
    """The listing's photo URLs in Etsy's own order (rank), largest size."""
    data, _h, err = ep.call(f"/listings/{int(listing_id)}/images", key)
    if err:
        raise RuntimeError(f"Etsy refused the photos: {err}")
    rows = sorted((data or {}).get("results") or [], key=lambda r: r.get("rank") or 0)
    return [r.get("url_fullxfull") or r.get("url_570xN") for r in rows
            if r.get("url_fullxfull") or r.get("url_570xN")]


def download(urls, folder):
    paths = []
    for n, url in enumerate(urls[:MAX_IMAGES]):
        if not str(url).startswith("https://"):
            continue
        ext = ".png" if str(url).lower().endswith(".png") else ".jpg"
        path = Path(folder) / f"{n}{ext}"
        req = urllib.request.Request(url, headers={"User-Agent": "listing-video/1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            path.write_bytes(r.read())
        paths.append(path)
    return paths


def read_index():
    try:
        return json.loads(INDEX.read_text())
    except Exception:
        return {}


def write_index(index):
    VIDEOS.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=1) + "\n")
    tmp.replace(INDEX)


def for_listing(listing_id, title, key, ep, seconds=SECONDS):
    with tempfile.TemporaryDirectory() as d:
        photos = download(listing_photos(listing_id, key, ep), d)
        if not photos:
            return False, "the listing has no photos Etsy would hand over"
        out = VIDEOS / f"{int(listing_id)}.mp4"
        ok, msg = make(photos, out, seconds)
    if ok:
        index = read_index()
        index[str(int(listing_id))] = {
            "title": title, "photos": len(photos), "seconds": seconds,
            "made_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
        write_index(index)
    return ok, msg


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("listing", nargs="?", help="an Etsy listing id, or 'all'")
    ap.add_argument("--images", nargs="+", help="make it from these files instead")
    ap.add_argument("--out", help="where --images writes the video")
    ap.add_argument("--seconds", type=int, default=SECONDS)
    ap.add_argument("--redo", action="store_true", help="remake videos that exist")
    a = ap.parse_args()

    if a.images:
        if not a.out:
            print("--images needs --out", file=sys.stderr)
            return 2
        ok, msg = make(a.images, a.out, a.seconds)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if not a.listing:
        ap.print_usage(sys.stderr)
        return 2
    if not all(ffmpeg()):
        print("ffmpeg is not installed. On the droplet:  apt install -y ffmpeg", file=sys.stderr)
        return 2

    ep = _load("etsy_probe", "etsy-probe.py")
    key, err = ep.api_key()
    if err:
        print(err, file=sys.stderr)
        return 2
    store = _load("store_report", "store-report.py")
    snaps = store.snapshots()
    listings = (snaps[-1].get("listings") if snaps else None) or []
    titles = {str(r.get("listing_id")): r.get("title") or "" for r in listings}

    if a.listing == "all":
        if not listings:
            print("No store snapshot yet: store-report.py fetch", file=sys.stderr)
            return 2
        done = read_index()
        todo = [r for r in listings if r.get("listing_id")
                and (a.redo or str(r["listing_id"]) not in done
                     or not (VIDEOS / f"{r['listing_id']}.mp4").is_file())]
    else:
        if not a.listing.isdigit():
            print("a listing id is digits - it is in the listing's Etsy address", file=sys.stderr)
            return 2
        todo = [{"listing_id": int(a.listing), "title": titles.get(a.listing, "")}]

    failed = 0
    for r in todo:
        try:
            ok, msg = for_listing(r["listing_id"], r.get("title") or "", key, ep, a.seconds)
        except Exception as exc:
            ok, msg = False, str(exc)[:300]
        failed += not ok
        print(f"{'made' if ok else 'FAILED'}  {r['listing_id']}  {store.short(r.get('title') or '', 40)}  {msg}")
    if not todo:
        print("every listing in the snapshot already has a video (--redo to remake)")
    elif not failed:
        print(f"\nIn the Deck: Emily's house -> Listing videos. On Etsy: Edit listing -> "
              f"Photos and video -> add the file.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
