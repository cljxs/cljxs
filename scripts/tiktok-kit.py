#!/usr/bin/env python3
"""
tiktok-kit.py — one ready-to-post TikTok kit a day, for one live Etsy listing.

    tiktok-kit.py make                  today's kit, if there is not one yet
    tiktok-kit.py make --again          another one today (the next listing in turn)
    tiktok-kit.py make --listing 4412345678     that listing, now
    tiktok-kit.py list [--json]         every kit, newest first
    tiktok-kit.py posted <kit>          you posted it - it leaves the to-do pile
    tiktok-kit.py posted <kit> --undo

WHAT A KIT IS. Everything needed to post by hand from the iPad, so posting
is a minute's work instead of an evening's:

    slides/01.jpg ...    a photo carousel at 1080x1920: a hook slide, the
                         listing's own photos, and a last "shop it" slide
    video.mp4            the same slides as a silent slideshow, for a video post
    caption.txt          the caption, with its hashtags
    audio.txt            what kind of sound to add, and where to find it
    <kit>.zip            all of it in one download
    kit.json             which listing, which hook, and whether it is posted

Nothing is posted from here: TikTok's posting API needs an approved developer
app and a review, and a post you have not looked at is not one you want with
the shop's name on it. The Deck's Shop page shows today's kit, plays it, and
has the download and a Posted button.

WHO IS FEATURED. A listing that has never had a kit goes first, most-saved
first; after that, whichever was featured longest ago. So every live listing
gets a turn before any gets a second one, and the order is a sort, not a
judgement.

THE WORDS ARE CODE'S, not a model's: the hook from a fixed set, the caption
from the listing's own description and tags. Free, never a claim the listing
does not make, and the same listing on the same day gives the same kit.

Kits: agents/emily/shop/tiktok/<Eastern day>-<listing id>[-n]/, the newest
KEEP_KITS kept. A kit is built in a .part folder and renamed when complete,
so a folder without .part in its name is a whole kit.

Needs ffmpeg (apt install -y ffmpeg) and a bold font, which Ubuntu's
fonts-dejavu-core provides. Standard library otherwise.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
KITS = ROOT / "agents" / "emily" / "shop" / "tiktok"

W, H = 1080, 1920           # TikTok's full-screen 9:16
MAX_PHOTOS = 5              # listing photos in the carousel, after the hook
SECONDS_PER_SLIDE = 2.5
VIDEO_MIN, VIDEO_MAX = 8, 20
KEEP_KITS = 14              # about 10 MB each; two weeks is plenty to catch up on
HASHTAGS_FIXED = ["etsyfinds", "smallbusiness"]
HASHTAGS_FROM_TAGS = 3      # TikTok's own advice is a handful, not a wall
CAPTION_MAX = 2200          # TikTok's limit for a photo post's caption
HOOK_LINE = 16              # characters per line on the hook slide
HOOK_LINES = 4
KIT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{1,15}(-\d{1,2})?$")

FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
         "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf")

# The hook - the first slide's words, which decide whether anyone swipes.
# {thing} is the product ("tote bag"), {who} who it is a gift for, read from
# the listing's own tags; a hook needing a {who} the tags do not give is
# skipped. None of them claims anything about the item itself.
HOOKS = [
    "POV: you found a {thing} nobody else has",
    "Rate this {thing} 1-10",
    "Tell me this isn't the cutest {thing}",
    "Gift idea for the {who} in your life",
    "Things I didn't know I needed: this {thing}",
    "Your sign to treat yourself to a new {thing}",
    "Would you use this {thing}? Be honest",
    "Found the perfect gift for a {who}",
]

THINGS = {"tote": "tote bag", "bag": "bag", "tee": "t-shirt", "tshirt": "t-shirt",
          "t-shirt": "t-shirt", "shirt": "shirt", "sweatshirt": "sweatshirt",
          "crewneck": "sweatshirt", "pullover": "sweatshirt", "hoodie": "hoodie",
          "sticker": "sticker", "decal": "sticker", "mug": "mug", "tumbler": "tumbler",
          "poster": "print", "print": "print", "blanket": "blanket", "pillow": "pillow",
          "beanie": "beanie", "hat": "hat", "cap": "cap", "magnet": "magnet",
          "pin": "pin", "apron": "apron", "case": "phone case"}

# What sort of sound suits the product. A style, not a song: what is trending
# changes weekly, and a named track may not be in the library a business
# account is allowed to use.
SOUNDS = {
    "tote bag": "cozy lo-fi, soft acoustic, or a 'that girl' morning-routine sound",
    "bag": "cozy lo-fi or soft acoustic",
    "sticker": "upbeat, playful pop - something bouncy for quick cuts",
    "mug": "warm coffee-shop acoustic or lo-fi",
    "print": "calm piano or ambient",
}
SOUND_DEFAULT = "upbeat but easy - trending pop at low volume, or lo-fi"

WHO_RE = re.compile(r"\bgifts? for (?:a |the |your )?([a-z][a-z ]{1,20}?)s?$|"
                    r"^([a-z][a-z ]{1,20}?)s? gifts?$", re.I)


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lv():
    return _load("listing_video", "listing-video.py")


def _store():
    return _load("store_report", "store-report.py")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ the words

def thing_of(title):
    """'tote bag', 'sticker' ... from the title, or 'find' when it names no
    product. emily-printify.py's product_word() decides the word, once."""
    word = _load("emily_printify_kit", "emily-printify.py").product_word(title)
    return THINGS.get(word or "", word or "find")


def who_of(tags):
    """Who the listing is a gift for, from a tag like 'gift for book lovers'
    or 'teacher gift' - or None. Singular: 'book lover'."""
    for t in tags or []:
        m = WHO_RE.search(str(t).strip())
        if m:
            who = (m.group(1) or m.group(2) or "").strip().lower()
            if who and who not in ("her", "him", "them", "you", "me", "us"):
                return who
    return None


def hook_for(row, turn):
    """The hook for this listing on this turn of the rotation. `turn` is how
    many kits came before, so consecutive kits never share a hook."""
    thing, who = thing_of(row.get("title")), who_of(row.get("tags"))
    usable = [h for h in HOOKS if "{who}" not in h or who]
    return usable[turn % len(usable)].format(thing=thing, who=who or "")


def wrap(text, width=HOOK_LINE, lines=HOOK_LINES):
    """Greedy word wrap for the hook slide; a word longer than a line gets a
    line of its own rather than being cut. More than `lines` is an error,
    because a hook that does not fit is a hook nobody reads."""
    out, line = [], ""
    for w in str(text).split():
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    if len(out) > lines:
        raise ValueError(f"hook is {len(out)} lines at {width} characters; {lines} fit")
    return out


def hashtag(tag):
    t = re.sub(r"[^a-z0-9]", "", str(tag).lower())
    return t if 3 <= len(t) <= 24 else None


def hashtags(tags):
    out = list(HASHTAGS_FIXED)
    for t in tags or []:
        h = hashtag(t)
        if h and h not in out:
            out.append(h)
        if len(out) >= len(HASHTAGS_FIXED) + HASHTAGS_FROM_TAGS:
            break
    return ["#" + h for h in out]


def caption_for(row, hook, shop_line):
    """caption.txt: the hook, the listing's own first sentence, where to buy,
    and the hashtags. Nothing here is written by a model, so nothing in it
    says more than the listing does."""
    lead = _store().first_sentence(row.get("description"), 180)
    parts = [hook if re.search(r"[.!?]$", hook) else hook + " 👀"]
    if lead:
        parts.append(lead)
    parts.append(f"🛒 Link in bio - {shop_line}")
    parts.append(" ".join(hashtags(row.get("tags"))))
    text = "\n\n".join(parts)
    return text[:CAPTION_MAX]


def audio_for(thing):
    style = SOUNDS.get(thing, SOUND_DEFAULT)
    return "\n".join([
        f"SOUND FOR THIS POST ({thing})",
        "",
        f"Style that suits it: {style}.",
        "",
        "The video is silent on purpose. Add the sound inside TikTok (tap Add sound",
        "before posting) - a post that uses a sound is listed on that sound's page,",
        "which is free reach a sound baked into the file never gets.",
        "",
        "On a Business account TikTok only lets you use its Commercial Music",
        "Library - pick from there, or the post can be muted later.",
        "",
        "For the photo carousel: the same Add sound button, then let it auto-play.",
    ]) + "\n"


# ------------------------------------------------------------------ who goes next

def featured(kits):
    """{listing_id: the latest day it had a kit}."""
    out = {}
    for k in kits:
        lid, day = str(k.get("listing_id")), str(k.get("day") or "")
        if day > out.get(lid, ""):
            out[lid] = day
    return out


def next_listing(listings, kits):
    """The listing whose turn it is: never featured first (most saved, then
    most viewed), then the one featured longest ago. None if there are none."""
    seen = featured(kits)
    rows = [r for r in listings or [] if r.get("listing_id") and r.get("url")]
    if not rows:
        return None
    num = lambda v: v if isinstance(v, int) else -1
    # A listing never featured has no day, and "" sorts before every date.
    rows.sort(key=lambda r: (seen.get(str(r["listing_id"]), ""),
                             -num(r.get("favs")), -num(r.get("views")), str(r["listing_id"])))
    return rows[0]


# ------------------------------------------------------------------ the pictures

def font():
    return next((f for f in FONTS if Path(f).is_file()), None)


def _text_filters(lines, fontfile, folder, top, size, tag):
    """One drawtext per line, each centred. The words go through a file
    (textfile=) so an apostrophe or a colon in a hook is never ffmpeg syntax."""
    out, step = [], int(size * 1.22)
    for n, line in enumerate(lines):
        f = Path(folder) / f"{tag}-{n}.txt"
        f.write_text(line)
        out.append(f"drawtext=fontfile={fontfile}:textfile={f}:expansion=none:fontsize={size}"
                   f":fontcolor=white:borderw=6:bordercolor=black@0.85"
                   f":x=(w-text_w)/2:y={top + n * step}")
    return out


def slide_command(photo, out, kind, lines=(), fontfile=None, scratch=None):
    """One slide as an ffmpeg argument list. Every slide is the photo over a
    blurred, darkened copy of itself, so a square mockup fills a tall screen
    without white bars. kind: 'hook' (photo lower, words on top), 'photo',
    or 'end' (words only, over the blur)."""
    dim = {"hook": -0.28, "photo": -0.10, "end": -0.32}[kind]
    fg_w, fg_h = {"hook": (960, 960), "photo": (1040, 1600), "end": (0, 0)}[kind]
    chain = [f"[0:v]split=2[a][b]",
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
             f"gblur=sigma=38,eq=brightness={dim}[bg]"]
    if kind == "end":
        chain.append("[b]nullsink")
        last = "[bg]"
    else:
        y = "(H-h)/2" if kind == "photo" else f"{H}-h-260"
        chain.append(f"[b]scale={fg_w}:{fg_h}:force_original_aspect_ratio=decrease[fg]")
        chain.append(f"[bg][fg]overlay=(W-w)/2:{y}[c]")
        last = "[c]"
    text = []
    if lines:
        size = 96 if kind == "hook" else 84
        top = 220 if kind == "hook" else int(H / 2 - len(lines) * size * 0.61)
        text = _text_filters(lines, fontfile, scratch, top, size, Path(out).stem)
    chain.append(f"{last}{','.join(text + ['format=yuvj420p'])}[out]")
    return ["ffmpeg", "-y", "-loglevel", "error", "-i", str(photo),
            "-filter_complex", ";".join(chain), "-map", "[out]",
            "-frames:v", "1", "-q:v", "3", str(out)]


def run(cmd, timeout=300):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(r.stderr or '').strip()[-300:]}")


def video_seconds(slides):
    return max(VIDEO_MIN, min(VIDEO_MAX, round(slides * SECONDS_PER_SLIDE)))


def video_problems(path, seconds):
    """What is wrong with the finished slideshow. [] is good. Read back from
    the file by ffprobe, not assumed from the command that made it."""
    secs, w, h, audio = _lv().probe(path)
    out = []
    if (w, h) != (W, H):
        out.append(f"{w}x{h}, not {W}x{H}")
    if abs(secs - seconds) > 0.25:
        out.append(f"{secs:.1f}s long, meant to be {seconds}s")
    if audio:
        out.append("has a sound track - the sound is added in TikTok")
    return out


# ------------------------------------------------------------------ a kit

def read_kits():
    """Every whole kit, newest first."""
    out = []
    for d in sorted(KITS.iterdir(), reverse=True) if KITS.is_dir() else []:
        if not d.is_dir() or not KIT_RE.match(d.name):
            continue
        try:
            k = json.loads((d / "kit.json").read_text())
        except Exception:
            continue
        if isinstance(k, dict):
            k["id"] = d.name
            out.append(k)
    out.sort(key=lambda k: (str(k.get("made_utc") or ""), k["id"]), reverse=True)
    return out


def kit_id(day, listing_id):
    base = f"{day}-{int(listing_id)}"
    n = 1
    name = base
    while (KITS / name).exists():
        n += 1
        name = f"{base}-{n}"
    return name


def build_kit(row, photos, day, shop_line, turn):
    """Make a whole kit from a listing row and its downloaded photos. Returns
    the kit dict. Raises on anything that would leave a kit half-made, and
    leaves nothing behind when it does."""
    fontfile = font()
    if not fontfile:
        raise RuntimeError("no bold font found. On the droplet:  apt install -y fonts-dejavu-core")
    photos = [Path(p) for p in photos][:MAX_PHOTOS]
    if not photos:
        raise RuntimeError("the listing has no photos to make slides from")
    thing = thing_of(row.get("title"))
    hook = hook_for(row, turn)
    hook_lines = wrap(hook)
    name = re.sub(r"\s+on Etsy$", "", shop_line).strip()
    end_lines = ["Shop it on Etsy"] + (wrap(name, 18, 2) if name else []) + ["Link in bio"]

    KITS.mkdir(parents=True, exist_ok=True)
    kid = kit_id(day, row["listing_id"])
    part = KITS / f".part-{kid}"
    shutil.rmtree(part, ignore_errors=True)
    (part / "slides").mkdir(parents=True)
    try:
        with tempfile.TemporaryDirectory() as scratch:
            plan = ([("hook", photos[0], hook_lines)] + [("photo", p, ()) for p in photos]
                    + [("end", photos[0], end_lines)])
            slides = []
            for n, (kind, photo, lines) in enumerate(plan, start=1):
                out = part / "slides" / f"{n:02d}.jpg"
                run(slide_command(photo, out, kind, lines, fontfile, scratch))
                slides.append(out)
        seconds = video_seconds(len(slides))
        lv = _lv()
        cmd = lv.command(slides, part / "video.mp4", seconds, size=(W, H),
                         background="black", crf=23)
        cmd[0] = shutil.which("ffmpeg") or "ffmpeg"
        run(cmd, timeout=600)
        bad = video_problems(part / "video.mp4", seconds)
        if bad:
            raise RuntimeError("the slideshow came out wrong: " + "; ".join(bad))
        caption = caption_for(row, hook, shop_line)
        (part / "caption.txt").write_text(caption + "\n")
        (part / "audio.txt").write_text(audio_for(thing))
        with zipfile.ZipFile(part / f"{kid}.zip", "w", zipfile.ZIP_STORED) as z:
            for s in slides:
                z.write(s, f"{kid}/photos/{s.name}")
            for name in ("video.mp4", "caption.txt", "audio.txt"):
                z.write(part / name, f"{kid}/{name}")
        kit = {"day": day, "listing_id": int(row["listing_id"]),
               "title": row.get("title") or "", "url": row.get("url"),
               "hook": hook, "thing": thing, "slides": len(slides),
               "video_seconds": seconds, "hashtags": hashtags(row.get("tags")),
               "status": "ready", "made_utc": _now(), "posted_utc": None}
        (part / "kit.json").write_text(json.dumps(kit, indent=1) + "\n")
        part.rename(KITS / kid)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    kit["id"] = kid
    return kit


def prune(keep=KEEP_KITS):
    """Drop the oldest kits beyond `keep`, and any .part folder a crash left.
    Returns the ids removed."""
    gone = []
    for k in read_kits()[keep:]:
        shutil.rmtree(KITS / k["id"], ignore_errors=True)
        gone.append(k["id"])
    for d in KITS.glob(".part-*") if KITS.is_dir() else []:
        shutil.rmtree(d, ignore_errors=True)
    return gone


def set_posted(kid, posted=True):
    if not KIT_RE.match(kid or ""):
        raise ValueError(f"{kid!r} is not a kit name")
    path = KITS / kid / "kit.json"
    kit = json.loads(path.read_text())
    kit["status"] = "posted" if posted else "ready"
    kit["posted_utc"] = _now() if posted else None
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kit, indent=1) + "\n")
    tmp.replace(path)
    return kit


# ------------------------------------------------------------------ commands

def cmd_make(a):
    lv = _lv()
    if not all(lv.ffmpeg()):
        print("ffmpeg is not installed. On the droplet:  apt install -y ffmpeg", file=sys.stderr)
        return 2
    day = et_time.day()
    kits = read_kits()
    if not a.again and not a.listing and any(k.get("day") == day for k in kits):
        today = next(k for k in kits if k.get("day") == day)
        print(f"today's kit is made: {today['id']} ({today.get('title', '')[:50]}). "
              f"Another: tiktok-kit.py make --again")
        return 0
    store = _store()
    snaps = store.snapshots()
    latest = snaps[-1] if snaps else {}
    listings = latest.get("listings") or []
    if a.listing:
        row = next((r for r in listings if str(r.get("listing_id")) == a.listing), None)
        if not row:
            print(f"listing {a.listing} is not in the latest store snapshot - "
                  f"store-report.py fetch, then try again", file=sys.stderr)
            return 2
    else:
        row = next_listing(listings, kits)
        if not row:
            print("No live listings in the store snapshot yet: store-report.py fetch",
                  file=sys.stderr)
            return 2
    ep = _load("etsy_probe", "etsy-probe.py")
    key, err = ep.api_key()
    if err:
        print(err, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as d:
        try:
            photos = lv.download(lv.listing_photos(row["listing_id"], key, ep), d, MAX_PHOTOS)
            kit = build_kit(row, photos, day, store.SHOP_LINE, len(kits))
        except Exception as exc:
            print(f"FAILED  {row['listing_id']}  {str(exc)[:300]}", file=sys.stderr)
            return 1
    gone = prune()
    print(f"made {kit['id']}: {kit['slides']} slides, a {kit['video_seconds']}s video - "
          f"{store.short(kit['title'], 50)}\n  hook: {kit['hook']}")
    if gone:
        print(f"  cleared {len(gone)} old kit(s)")
    print("In the Deck: Shop -> TikTok. Download it, post it, press Posted.")
    return 0


def cmd_list(a):
    kits = read_kits()
    if a.json:
        # The words ride along, so the page can show and copy them without
        # a second request per kit.
        for k in kits:
            for name in ("caption", "audio"):
                try:
                    k[name] = (KITS / k["id"] / f"{name}.txt").read_text()
                except OSError:
                    k[name] = None
        print(json.dumps({"kits": kits, "today": et_time.day(),
                          "to_post": sum(1 for k in kits if k.get("status") != "posted")}))
        return 0
    if not kits:
        print("no kits yet:  tiktok-kit.py make")
        return 0
    for k in kits:
        print(f"  {k['id']:<28}{k.get('status', '?'):<8}{str(k.get('title', ''))[:50]}")
    return 0


def cmd_posted(a):
    try:
        kit = set_posted(a.kit, not a.undo)
    except FileNotFoundError:
        print(f"no kit called {a.kit} - tiktok-kit.py list", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"{a.kit}: {kit['status']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="today's kit")
    m.add_argument("--again", action="store_true", help="another kit today")
    m.add_argument("--listing", help="this Etsy listing id, now")
    m.set_defaults(fn=cmd_make)
    ls = sub.add_parser("list", help="every kit")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)
    p = sub.add_parser("posted", help="mark a kit posted")
    p.add_argument("kit")
    p.add_argument("--undo", action="store_true")
    p.set_defaults(fn=cmd_posted)
    a = ap.parse_args()
    if a.cmd == "make" and a.listing and not a.listing.isdigit():
        print("a listing id is digits - it is in the listing's Etsy address", file=sys.stderr)
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
