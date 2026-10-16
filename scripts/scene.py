#!/usr/bin/env python3
"""
scene.py — the real product, cut out of its mockup, set into a free
AI-drawn scene, and checked for whether the light matches.

    scene.py try photo.jpg --thing "tote bag"        two scenes, with the checker's report
    scene.py try photo.jpg --thing mug --n 1 --out /tmp/scenes
    scene.py try photo.jpg --how free                 the free kind instead of the paid

Two kinds. PAID (the default since 2026-10-14) shows the image model the
product's photo and has it draw that product into a scene; a vision model
then checks the design survived. FREE, below, pastes the cut-out product on
a free AI background - the owner tried it and it "looks terrible", so it is
kept only as `--how free` / TIKTOK_SCENES=free.

WHY THIS SHAPE (owner, 2026-10-13: "Let's try the free option first"). An AI
that redraws the product garbles its print - the lettering, the pattern - and
the ones that can be shown the product cost money. So the AI here draws only
the EMPTY scene, a cafe table or a bookshelf, and never sees the product. The
product is the listing's own photo with its studio backdrop cut away by
knockout.py (the one cutter), pasted in by ffmpeg. Its design cannot be
wrong because nothing redrew it.

What that cannot do: a hand holding the bag, a person wearing the shirt.
Drawing fingers around a strap means drawing over the product, which is the
paid, garbling kind. A pasted product sits on a surface; that is the whole
range of this.

THE FREE SCENE. Pollinations, no account, no key (checked 2026-10-13): it
answered 576x1024 with a "pollinations.ai" mark in the bottom-right corner,
and one request in two was refused upstream as rate-limited. So the mark is
cropped off, the picture is enlarged and softened - a product photo's
background is out of focus anyway - and a refusal is retried after a wait.
With a free Pollinations key (POLLINATIONS_KEY in Emily's credentials.env)
the request goes to the keyed endpoint instead. That path is BELIEVED, not
verified: no key existed here to try it.

THE LIGHTING CHECKER. A pasted product gives itself away in four ways, and
each is measured on the finished picture, not on what the code meant to do:

    glow        the product brighter than the brightest lit thing near it -
                a studio-lit item in a dim room
    colour      its white and grey parts a different tint from the scene's
                (cool studio light in a warm cafe); skipped when the product
                has no neutral parts to judge by
    edges       a light rim round the product - the studio backdrop left on
                its outline
    backdrop    patches of the studio backdrop left inside it, such as
                between a tote's straps
    background  the scene behind it too busy, too dark or blown out

Before checking, the product is NUDGED towards the scene - a little
brighter or darker, a touch of its tint - and its shadow thrown away from
the brighter side. Only nudged: the product's colour is what the buyer
receives, so a scene that would need more fails the checker instead. A
scene that fails any check is not used, and the kit says why.

The scenes are top-down flat lays (a mug stands on a counter): the surface
fills the frame, so the product lies on it wherever it goes.

Needs ffmpeg. Standard library otherwise.
"""

import argparse
import base64
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

W, H = 1080, 1920
GRID = 2                    # the checker reads the picture at half size: 540x960

# The free scene.
FREE_URL = "https://image.pollinations.ai/prompt/{prompt}?width=1080&height=1920&model=flux&nologo=true&seed={seed}"
KEYED_URL = "https://gen.pollinations.ai/image/{prompt}?width=1080&height=1920&model=flux&seed={seed}"
MARK_CROP = 0.06            # the bottom share of the free picture that carries the mark
FETCH_TRIES = 3
FETCH_WAIT = 20             # seconds between tries; the anonymous limit is per IP
MIN_SCENE_SIDE = 500
BLUR = 5                    # how soft the scene is behind the product

# The product.
MAX_CUT_SIDE = 900          # the mockup is cut at this size - knockout is pure Python
MIN_PRODUCT_SHARE = 0.06    # of the mockup; less is too few pixels to enlarge, or the product was cut away
MAX_PRODUCT_SHARE = 0.92    # more and there was no backdrop to cut
# THE PRODUCT'S COLOUR IS NOT NEGOTIABLE. Matched in full to a dim orange
# table, a cream tote came out khaki grey (2026-10-13) - lit like the scene,
# and not the bag a buyer would receive. So it is only nudged; a scene that
# needs more than this fails the checker and is not used.
GAIN_MIN, GAIN_MAX = 0.85, 1.05     # brightness
TINT_SHARE = 0.35                   # of the scene's tint the product takes on
TINT_MAX = 0.06                     # most any one channel moves beyond the brightness

# How the product sits in the scene, and how large. Most products are laid
# FLAT and shot from above: in a top-down scene the surface fills the frame,
# so a pasted product lies on it wherever it goes. An eye-level scene needs
# the product's base exactly on the table's edge, which code cannot find -
# the first try (2026-10-13) hung a tote in front of a cafe table. A mug's
# mockup is a side view, so a mug stands on a counter that fills the bottom.
PLACE = {"tote bag": (0.62, "flat"), "bag": (0.62, "flat"), "mug": (0.46, "stand"),
         "sticker": (0.40, "flat"), "t-shirt": (0.80, "flat"), "shirt": (0.80, "flat"),
         "sweatshirt": (0.82, "flat"), "hoodie": (0.82, "flat"), "print": (0.66, "flat"),
         "pillow": (0.66, "flat"), "blanket": (0.84, "flat")}
PLACE_DEFAULT = (0.60, "flat")
FLAT_CENTRE = 0.52           # of the frame's height
STAND_BASE = 0.86
MAX_HEIGHT_SHARE = 0.56

# The scenes. Props sit at the EDGES; the middle is the product's.
FLAT = ["a rustic wooden table, a latte and a few dried flowers at the very edges",
        "a cream knitted blanket, an open book and reading glasses at the very edges",
        "light oak floorboards, a few autumn leaves scattered at the very edges",
        "crumpled white linen, a small potted plant at one corner",
        "a red and white picnic blanket on grass, strawberries at one edge"]
SCENES = {
    "mug": ["an empty wooden kitchen counter filling the lower half, a window with soft morning light behind",
            "an empty cafe table top filling the lower half, a blurred cozy cafe behind"],
    "sticker": ["a pastel desk, a notebook and pens at the very edges",
                "a clean wooden desk, a closed laptop and a plant leaf at the very edges"],
}
SCENES_DEFAULT = FLAT
STYLE = {"flat": ("top-down flat lay photography, overhead view looking straight down, the whole "
                  "middle of the frame is plain empty surface, photorealistic, vertical photo, soft "
                  "natural light, no people, no hands, no text, no logos, no bags, no products"),
         "stand": ("eye-level product photography background, photorealistic, vertical photo, the "
                   "front of the surface is empty, shallow depth of field, soft natural light, no "
                   "people, no hands, no text, no logos, no products")}

# The checker.
# Glow: a light-coloured thing is legitimately brighter than a dark surface
# under the same light, so the product may be
# this much brighter than what is near it, or as bright as the brightest
# thing anywhere in the scene. Beyond both, it was lit by a different light.
GLOW_NEAR = 1.45            # cream canvas on mid-brown wood is about 1.4x as bright
GLOW_SCENE = 1.05
GLOW_SLACK = 6              # in luma levels
DARK_SHARE = 0.30           # product mean below this share of its surroundings reads as a hole
TINT_TOLERANCE = 0.10       # log-ratio difference in red/green or blue/green
NEUTRAL_SAT = 0.12          # a pixel this unsaturated is white/grey - it shows the light's tint
NEUTRAL_MIN_SHARE = 0.03    # fewer neutral pixels than this and the tint check is skipped
HALO_LEVELS = 18            # a rim this much lighter than both sides is backdrop left on the edge
BACKDROP_TOLERANCE = 20     # per channel: "this is still the studio backdrop's colour"
BACKDROP_MAX_SHARE = 0.03
BUSY_STDEV = 52             # luma spread behind the product above this is clutter
SCENE_DARK, SCENE_BRIGHT = 45, 225


class SceneError(Exception):
    pass


class CutoutError(SceneError):
    """This photo of the product has no plain backdrop to cut it from - try
    another photo, not another scene."""


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ffmpeg(*args, timeout=180):
    r = subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", *map(str, args)],
                       capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise SceneError(f"ffmpeg: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.stdout


def raw(path, w, h, fmt="rgb24"):
    """The picture's pixels at exactly w x h, as bytes."""
    return ffmpeg("-i", path, "-vf", f"scale={w}:{h}:flags=area", "-f", "rawvideo",
                  "-pix_fmt", fmt, "-")


def luma(r, g, b):
    return 0.299 * r + 0.587 * g + 0.114 * b


def pct(values, q):
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))]


# ------------------------------------------------------------------ the scene

def mode_of(thing):
    return PLACE.get(thing, PLACE_DEFAULT)[1]


def prompt_for(thing, n):
    places = SCENES.get(thing) or SCENES_DEFAULT
    return f"{places[n % len(places)]}, {STYLE[mode_of(thing)]}"


def credentials_key():
    """POLLINATIONS_KEY from the environment or Emily's credentials.env,
    through emily-assets.py's reader - the one parser for that file."""
    if os.environ.get("POLLINATIONS_KEY"):
        return os.environ["POLLINATIONS_KEY"].strip()
    try:
        ea = _load("emily_assets_scene", "emily-assets.py")
        path = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent)) / "agents" / "emily" / "state" / "credentials.env"
        return (ea.read_env_file(path).get("POLLINATIONS_KEY") or "").strip() or None
    except Exception:
        return None


def fetch_background(prompt, seed, out, opener=urllib.request.urlopen, wait=time.sleep):
    """Download one free scene to `out`. Returns 'keyed' or 'free'. Raises
    SceneError when every try was refused or came back as something other
    than a picture - the free service answers an error as JSON, sometimes
    with a 200."""
    key = credentials_key()
    url = (KEYED_URL if key else FREE_URL).format(prompt=urllib.parse.quote(prompt, safe=""), seed=int(seed))
    headers = {"User-Agent": "cljxs-scene/1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    last = "no answer"
    for attempt in range(FETCH_TRIES):
        if attempt:
            wait(FETCH_WAIT)
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=150) as r:
                body = r.read()
                kind = (r.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            continue
        except Exception as e:      # timeouts, resets - the free service has both
            last = type(e).__name__
            continue
        if not kind.startswith("image/") or body[:1] in (b"{", b"<"):
            last = f"not a picture ({kind or 'no type'}): {body[:120].decode(errors='replace')}"
            continue
        Path(out).write_bytes(body)
        return "keyed" if key else "free"
    raise SceneError(f"the free scene service refused {FETCH_TRIES} times - last: {last}")


def prepare_background(src, out, keyed):
    """Crop the free service's mark, fill 1080x1920, soften. A scene too small
    to enlarge is refused rather than smeared."""
    w, h = probe_size(src)
    if min(w, h) < MIN_SCENE_SIDE:
        raise SceneError(f"the scene came back {w}x{h} - too small to use")
    crop = "" if keyed else f"crop=iw:ih*{1 - MARK_CROP}:0:0,"
    ffmpeg("-i", src, "-vf", f"{crop}scale={W}:{H}:force_original_aspect_ratio=increase,"
                            f"crop={W}:{H},gblur=sigma={BLUR}", "-frames:v", "1", out)


def probe_size(path):
    r = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
        return w, h
    except ValueError:
        raise SceneError(f"{Path(path).name} is not a picture ffprobe can read")


# ------------------------------------------------------------------ the product

def cutout(photo, folder):
    """The product with its studio backdrop cut away, by knockout.py.
    Returns (cutout path, share of the product still the backdrop's colour,
    share of the mockup that is product)."""
    folder = Path(folder)
    src = folder / "mockup.png"
    ffmpeg("-i", photo, "-vf", f"scale='min({MAX_CUT_SIDE},iw)':-2", "-frames:v", "1", src)
    out = folder / "cutout.png"
    r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"), str(src), str(out), "--holes"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not out.is_file():
        said = (r.stderr or r.stdout).strip().splitlines()
        raise CutoutError("the product could not be cut out of this photo"
                         + (f" - {said[0][:160]}" if said else ""))
    ko = _load("knockout_scene", "knockout.py")
    w, h, px = ko.decode(src)
    backdrop = ko.background_colour(w, h, px)
    cw, ch, cpx = ko.decode(out)
    opaque = left = 0
    for i in range(0, len(cpx), 4):
        if cpx[i + 3] > 127:
            opaque += 1
            left += all(abs(cpx[i + k] - backdrop[k]) <= BACKDROP_TOLERANCE for k in range(3))
    share = opaque / float(w * h)
    if not MIN_PRODUCT_SHARE <= share <= MAX_PRODUCT_SHARE:
        raise CutoutError(f"the cut-out is {share:.0%} of the photo - "
                         + ("too little product to enlarge into a scene without blurring it, or the "
                            "product was cut away with the backdrop" if share < MIN_PRODUCT_SHARE
                            else "there was no plain backdrop to cut"))
    # Read here, on the cut-out itself, before its colours are matched to the
    # scene - after that the backdrop's grey has shifted with everything else
    # and would no longer be recognised.
    return out, left / max(opaque, 1), share


def placement(cut_w, cut_h, thing):
    """(x, y, width, height) of the product in the 1080x1920 frame."""
    share, mode = PLACE.get(thing, PLACE_DEFAULT)
    pw = W * share
    ph = pw * cut_h / cut_w
    if ph > H * MAX_HEIGHT_SHARE:
        ph = H * MAX_HEIGHT_SHARE
        pw = ph * cut_w / cut_h
    pw, ph = int(round(pw / 2) * 2), int(round(ph / 2) * 2)
    y = int(H * STAND_BASE) - ph if mode == "stand" else int(H * FLAT_CENTRE - ph / 2)
    return (W - pw) // 2, y, pw, ph


# ------------------------------------------------------------------ measuring

def region_stats(px, w, box):
    """Luma values, and the scene's brightness left vs right, inside box."""
    x0, y0, x1, y1 = box
    out = []
    for y in range(max(0, y0), y1):
        for x in range(max(0, x0), min(w, x1)):
            j = (y * w + x) * 3
            out.append(luma(px[j], px[j + 1], px[j + 2]))
    return out


def tint(pixels):
    """(log red/green, log blue/green) of the neutral pixels in an iterable of
    (r, g, b), and their share - the colour of the light, read off the
    things in the picture that have no colour of their own."""
    rs = gs = bs = 0.0
    n = total = 0
    for r, g, b in pixels:
        total += 1
        hi, lo = max(r, g, b), min(r, g, b)
        if hi < 40 or hi > 250 or (hi - lo) / hi > NEUTRAL_SAT:
            continue
        rs, gs, bs, n = rs + r, gs + g, bs + b, n + 1
    if not n or not gs:
        return None, None, 0.0
    return math.log((rs + 1) / (gs + 1)), math.log((bs + 1) / (gs + 1)), n / max(total, 1)


def scene_light(bg_px, box):
    """What the scene's light is like: its highlights near where the product
    stands, its overall brightness, its tint, which side is brighter, and
    how busy it is behind the product."""
    w, h = W // GRID, H // GRID
    x, y, pw, ph = (v // GRID for v in box)
    pad_x, pad_y = pw // 4, ph // 4
    near = region_stats(bg_px, w, (x - pad_x, y - pad_y, x + pw + pad_x, min(h, y + ph + pad_y)))
    behind = region_stats(bg_px, w, (x, y, x + pw, y + ph))
    whole = [luma(bg_px[j], bg_px[j + 1], bg_px[j + 2]) for j in range(0, len(bg_px), 3)]
    third = w // 3
    left = region_stats(bg_px, w, (0, 0, third, h))
    right = region_stats(bg_px, w, (w - third, 0, w, h))
    tr, tb, _ = tint((bg_px[j], bg_px[j + 1], bg_px[j + 2]) for j in range(0, len(bg_px), 3))
    mean = lambda v: sum(v) / len(v) if v else 0.0
    mb = mean(behind)
    spread = math.sqrt(mean([(v - mb) ** 2 for v in behind])) if behind else 0.0
    return {"near_p95": pct(near, 0.95), "near_mean": mean(near), "mean": mean(whole),
            "p95": pct(whole, 0.95), "p99": pct(whole, 0.99), "tint_r": tr, "tint_b": tb,
            "side": (mean(right) - mean(left)) / max(mean(whole), 1.0), "busy": spread}


def product_light(cut_path, pw, ph):
    """The product's own highlights and tint, from its opaque pixels."""
    w, h = max(1, pw // GRID), max(1, ph // GRID)
    px = raw(cut_path, w, h, "rgba")
    lum, rgb = [], []
    for j in range(0, len(px), 4):
        if px[j + 3] > 200:
            lum.append(luma(px[j], px[j + 1], px[j + 2]))
            rgb.append((px[j], px[j + 1], px[j + 2]))
    tr, tb, share = tint(rgb)
    return {"p95": pct(lum, 0.95), "mean": sum(lum) / len(lum) if lum else 0.0,
            "tint_r": tr, "tint_b": tb, "neutral_share": share}


def match(scene, product):
    """How to change the product so its light is the scene's: a gain on its
    brightness and a multiplier per channel. Pure arithmetic, so tested."""
    target = 0.5 * scene["near_p95"] + 0.5 * scene["p95"]
    gain = max(GAIN_MIN, min(GAIN_MAX, target / max(product["p95"], 1.0)))
    r = g = b = gain
    if scene["tint_r"] is not None:
        pr = product["tint_r"] if product["tint_r"] is not None else 0.0
        pb = product["tint_b"] if product["tint_b"] is not None else 0.0
        r *= math.exp(TINT_SHARE * (scene["tint_r"] - pr) if product["tint_r"] is not None
                      else TINT_SHARE * scene["tint_r"])
        b *= math.exp(TINT_SHARE * (scene["tint_b"] - pb) if product["tint_b"] is not None
                      else TINT_SHARE * scene["tint_b"])
    clamp = lambda v: round(max(gain * (1 - TINT_MAX), min(gain * (1 + TINT_MAX), v)), 4)
    # The shadow falls away from the brighter side, and further the stronger
    # the difference.
    dx = int(round(-max(-1.0, min(1.0, scene["side"] * 3)) * 40))
    return {"r": clamp(r), "g": clamp(g), "b": clamp(b), "gain": round(gain, 4),
            "shadow_dx": dx, "shadow_dy": 16}


# ------------------------------------------------------------------ the picture

def composite_command(bg, cut, out, box, m, mode="flat"):
    """The ffmpeg command that sets the product into the scene: a soft drop
    shadow thrown from the light, a contact shadow under its base, then the
    product, recoloured to the scene and its outline pulled in a pixel so no
    backdrop rim survives."""
    x, y, pw, ph = box
    # Lying flat, a product casts a short soft shadow and nothing under it;
    # standing, it also darkens the surface where it touches.
    contact_h = max(8, ph // 18)
    contact = 0.0 if mode == "flat" else 0.35
    drop, sigma = (0.30, 16) if mode == "flat" else (0.22, 26)
    graph = (
        f"[1:v]scale={pw}:{ph},format=rgba,"
        f"colorchannelmixer=rr={m['r']}:gg={m['g']}:bb={m['b']},split=3[p][s][c];"
        f"[p]split[p1][p2];[p1]alphaextract,erosion,gblur=sigma=0.8[a];[p2][a]alphamerge[fg];"
        f"[s]colorchannelmixer=rr=0:gg=0:bb=0:aa={drop},gblur=sigma={sigma}[sh];"
        f"[c]scale={pw}:{contact_h},colorchannelmixer=rr=0:gg=0:bb=0:aa={contact},gblur=sigma=10[ct];"
        f"[0:v][sh]overlay={x + m['shadow_dx']}:{y + m['shadow_dy']}[b1];"
        f"[b1][ct]overlay={x}:{y + ph - contact_h // 2}[b2];"
        f"[b2][fg]overlay={x}:{y},format=rgb24[out]")
    return ["-i", str(bg), "-i", str(cut), "-filter_complex", graph, "-map", "[out]",
            "-frames:v", "1", "-q:v", "3", str(out)]


def mask_of(cut_path, box):
    """Where the product is in the half-size frame: a set of (x, y)."""
    x, y, pw, ph = (v // GRID for v in box)
    a = raw(cut_path, pw, ph, "rgba")
    return {(x + i % pw, y + i // pw) for i in range(pw * ph) if a[i * 4 + 3] > 127}


def ring(cells, step):
    """Cells one step outside `cells` (step=1) or one step inside (step=-1)."""
    nb = ((1, 0), (-1, 0), (0, 1), (0, -1))
    if step > 0:
        return {(x + dx, y + dy) for x, y in cells for dx, dy in nb} - cells
    return {(x, y) for x, y in cells if any((x + dx, y + dy) not in cells for dx, dy in nb)}


def check(composite, scene, mask, backdrop_left):
    """The lighting checker, run on the finished picture. Returns
    {name: {"ok": bool, "said": str}}; every check must pass."""
    w, h = W // GRID, H // GRID
    px = raw(composite, w, h)
    at = lambda c: (lambda j: (px[j], px[j + 1], px[j + 2]))((c[1] * w + c[0]) * 3)
    inside = {c for c in mask if 0 <= c[0] < w and 0 <= c[1] < h}
    edge = ring(inside, -1)
    core = inside - edge - ring(inside - edge, -1)
    out1 = {c for c in ring(inside, 1) if 0 <= c[0] < w and 0 <= c[1] < h}
    out2 = {c for c in ring(inside | out1, 1) if 0 <= c[0] < w and 0 <= c[1] < h}
    lum = lambda cells: [luma(*at(c)) for c in cells]
    mean = lambda v: sum(v) / len(v) if v else 0.0
    result = {}

    fg = lum(core or inside)
    p95 = pct(fg, 0.95)
    ceiling = max(scene["near_p95"] * GLOW_NEAR, scene["p99"] * GLOW_SCENE) + GLOW_SLACK
    surround = mean(lum(out1 | out2))
    if p95 > ceiling:
        result["glow"] = {"ok": False, "said": f"the product ({p95:.0f}) is lit brighter than the scene "
                                               f"allows ({ceiling:.0f}) - it would look pasted on"}
    elif mean(fg) < DARK_SHARE * surround:
        result["glow"] = {"ok": False, "said": f"the product ({mean(fg):.0f}) is far darker than "
                                               f"where it stands ({surround:.0f})"}
    else:
        result["glow"] = {"ok": True, "said": f"highlights {p95:.0f}, within the scene's light ({ceiling:.0f})"}

    ftr, ftb, share = tint(at(c) for c in (core or inside))
    if scene["tint_r"] is None or ftr is None or share < NEUTRAL_MIN_SHARE:
        result["colour"] = {"ok": True, "said": "no white or grey in the product to judge the tint by - not checked"}
    else:
        off = max(abs(ftr - scene["tint_r"]), abs(ftb - scene["tint_b"]))
        result["colour"] = {"ok": off <= TINT_TOLERANCE,
                            "said": (f"its whites are tinted {off:.2f} away from the scene's light"
                                     if off > TINT_TOLERANCE else f"tint within {off:.2f} of the scene's")}

    rim, inner, outer = mean(lum(edge)), mean(lum(core)), mean(lum(out1))
    halo = rim - max(inner, outer)
    result["edges"] = {"ok": halo <= HALO_LEVELS,
                       "said": (f"a light rim round the product ({halo:.0f} levels) - backdrop left on its outline"
                                if halo > HALO_LEVELS else "no rim round the product")}

    lshare = backdrop_left
    result["backdrop"] = {"ok": lshare <= BACKDROP_MAX_SHARE,
                          "said": (f"{lshare:.0%} of the product is still the studio backdrop's colour"
                                   if lshare > BACKDROP_MAX_SHARE else "no studio backdrop left inside")}

    if scene["busy"] > BUSY_STDEV:
        result["background"] = {"ok": False, "said": f"the scene behind it is cluttered (spread {scene['busy']:.0f})"}
    elif not SCENE_DARK <= scene["mean"] <= SCENE_BRIGHT:
        result["background"] = {"ok": False, "said": f"the scene is too {'dark' if scene['mean'] < SCENE_DARK else 'bright'} "
                                                     f"({scene['mean']:.0f})"}
    else:
        result["background"] = {"ok": True, "said": "a calm, well-lit scene"}
    return result


def free_scene(photo, thing, out, n, seed, scratch, fetch=fetch_background):
    """One FREE scene picture at `out` (1080x1920 jpg): the cut-out product
    on a free AI background. Returns its report: {ok, why, checks, prompt,
    source, match}. A scene that fails a check is still written, so the
    owner can see why; `ok` says whether to use it."""
    scratch = Path(scratch)
    cut, backdrop_left, share = cutout(photo, scratch)
    ko = _load("knockout_scene_size", "knockout.py")
    box = placement(*ko.size(cut), thing)
    prompt = prompt_for(thing, n)
    raw_bg = scratch / f"scene-{n}.img"
    source = fetch(prompt, seed, raw_bg)
    bg = scratch / f"scene-{n}.png"
    prepare_background(raw_bg, bg, source == "keyed")
    scene = scene_light(raw(bg, W // GRID, H // GRID), box)
    m = match(scene, product_light(cut, box[2], box[3]))
    ffmpeg(*composite_command(bg, cut, out, box, m, mode_of(thing)))
    checks = check(out, scene, mask_of(cut, box), backdrop_left)
    failed = [k for k, v in checks.items() if not v["ok"]]
    return {"ok": not failed, "why": "; ".join(checks[k]["said"] for k in failed) or None,
            "checks": checks, "prompt": prompt, "source": source, "match": m,
            "product_share": round(share, 3)}


# ------------------------------------------------------------------ paid scenes
#
# THE PAID KIND (owner, 2026-10-14, after seeing the free ones: "it looks
# terrible"). The image model is shown the product's own photo and draws
# that product into the scene - so it can be carried on a shoulder or held
# at a market, which pasting never could. The price of that is that the
# model REDRAWS the product, and a redrawn print can come back changed. So
# every picture is compared with the real photo by a vision model, and code
# - not that model - decides from its answers whether the design survived.
#
# Through the OpenRouter key and the request emily-assets.py already builds
# (generate() with `images`), so there is no new account. The cost of each
# call is OpenRouter's own figure (usage.include), recorded on the kit.
#
# BELIEVED, not verified: no key existed where this was written. The request
# is the one emily-assets sends for art plus the image part proof() sends,
# both of which run on the droplet daily, but image-in AND image-out in one
# call had not been made from here before.

PAID_MODEL = "google/gemini-2.5-flash-image"
CHECK_MODEL = "google/gemini-2.5-flash"
PAID_TRIES = 2              # per scene: a redraw that changed the design gets one more go
IMAGE_GUESS = 0.05          # what an image is assumed to cost until OpenRouter says (believed ~$0.04)
CHECK_GUESS = 0.003
REF_SIDE = 1024             # the product photo is sent at most this size
MIN_TALL = 1.3              # height / width - a landscape picture would crop the product away
CHECK_MAX_TOKENS = 800
LIMITS_KEYS = ("tiktok_daily_ai", "tiktok_leave_for_emily")
LIMITS_DEFAULTS = {"tiktok_daily_ai": 0.30, "tiktok_leave_for_emily": 0.50}

# Faces are cropped out on purpose: an AI face is the thing viewers spot
# first and TikTok labels most strictly, and the product is the point.
# Seven each: a kit asks for five and tries two more when the design check
# refuses one (tiktok-kit.py), and a place asked for twice is a duplicate.
#
# THE TOTE (owner, 2026-10-16, on a bag drawn floating in front of a chair:
# "The bag isn't hanging off of anything. Also can we use people holding the
# bag... If not could we use the mockup pictures from printify and just
# change the background"). So: people carrying it, faces out of frame, and
# FLAT places - "flat:" - where the real mockup keeps its exact pose and
# only the plain backdrop is replaced by a surface it lies on, seen from
# above. Nothing hangs from furniture: that is what came out floating.
# Alternated, so any five in a row hold both kinds.
FLAT_LAY = "flat:"
PAID_SCENES = {
    "tote bag": ["carried on the shoulder of a woman walking down a sunny autumn street, one hand on the strap, "
                 "cropped at the shoulders so no face shows",
                 f"{FLAT_LAY} a rustic wooden table beside a latte and an open book",
                 "held out by one hand at a farmers market stall, the fingers wrapped round both straps, "
                 "flowers softly blurred behind, no face shown",
                 f"{FLAT_LAY} a red and white picnic blanket on grass beside a basket of strawberries",
                 "carried in the crook of the arm of a person browsing a bookshop, cropped at the chin so no face shows",
                 f"{FLAT_LAY} light oak floorboards with a few autumn leaves around it",
                 "slung over the shoulder of a person waiting at a cafe counter, seen from behind, no face shown"],
    "mug": ["held in two hands with cozy sweater sleeves beside a rainy window, no face shown",
            "on a kitchen counter beside a coffee pot in soft morning light",
            "on a desk next to an open notebook and a laptop, warm lamp light",
            "on a bedside table beside a stack of books and a small lamp",
            "on a sunny balcony table among potted plants",
            "on a wooden tray with cookies on a cozy knit blanket",
            "on a cafe windowsill with a pastry on a plate beside it"],
    "sticker": ["stuck on the lid of a laptop on a cafe table", "stuck on a water bottle beside a hiking trail",
                "stuck on the cover of a travel journal on a wooden desk",
                "stuck on a skateboard deck leaning against a brick wall",
                "stuck on a phone case lying on a cafe table",
                "stuck on a notebook beside a cup of coloured pencils",
                "stuck on a guitar case in a sunny room"],
    "t-shirt": ["worn by a person standing in a sunny park, cropped at the chin so no face shows",
                "folded neatly on a wooden bench beside a pair of sneakers",
                "worn by a person sitting in a cafe, cropped at the chin so no face shows",
                "laid flat on a bed beside a denim jacket and sunglasses",
                "hanging on a wooden hanger against a white brick wall",
                "worn by a person walking down a city street, cropped at the chin so no face shows",
                "folded on a shelf in a bright little boutique"],
}
PAID_SCENES["bag"] = PAID_SCENES["tote bag"]
for _k in ("shirt", "sweatshirt", "hoodie"):
    PAID_SCENES[_k] = PAID_SCENES["t-shirt"]
PAID_DEFAULT = ["on a cafe table beside a latte, seen from slightly above",
                "on a wooden shelf in a bright living room with plants",
                "held in one hand in soft daylight, no face shown",
                "on a bedside table beside a small lamp and a book",
                "on a sunny windowsill among potted plants",
                "on a wooden desk beside a notebook and a cup of coffee",
                "on a picnic blanket in a park on a sunny day"]

PAID_ASK = (
    "Here is a product photo of a {thing}. Make a new photo of THIS EXACT {thing} {scene}. "
    "Keep the product identical to the photo: the same printed design, the same colours, the same "
    "lettering spelled exactly the same, the same shape and proportions. Do not redraw, simplify, "
    "restyle, add to or remove anything printed on it, and keep its design facing the camera and fully "
    "visible. It obeys real physics: it is held, worn or resting exactly as a real {thing} would be - "
    "its weight pulling down on whatever holds it, straps actually round the hand or shoulder that holds "
    "them, touching what it rests on, with a matching shadow. Nothing floats. Hands and arms look natural. "
    "Photorealistic lifestyle photography, natural light, vertical 9:16 frame. No added text, "
    "logos, captions or watermarks anywhere in the picture.")

# The mockup with a real place behind it instead of studio white: the
# product exactly as photographed, lying on a surface, seen from straight
# above - the one pose in which a flat mockup is also physically true.
FLAT_ASK = (
    "Here is a product photo of a {thing} on a plain studio background. Keep THIS EXACT {thing} as it is "
    "in the photo - the same shape, angle, printed design, colours and lettering, not redrawn - and "
    "replace only the plain background: it lies flat on {scene}, seen from directly above, touching the "
    "surface, with a soft natural shadow where it meets it and the same soft daylight on both. "
    "Photorealistic overhead flat-lay photography, vertical 9:16 frame, the whole product in view. "
    "No added text, logos, captions or watermarks anywhere in the picture.")

DESIGN_ASK = (
    "Image A is a product photo. Image B is meant to show the SAME product in a new scene. "
    "Look only at the product. Reply with JSON only, no prose:\n"
    '{"product_visible": true or false (is the product clearly in B with its design facing the camera),\n'
    ' "same_design": true or false (is the printed design in B the same as in A - same picture, colours and layout),\n'
    ' "words_a": "every word printed on the product in A, exactly as spelled, or empty",\n'
    ' "words_b": "every word printed on the product in B, exactly as spelled, or empty",\n'
    ' "differences": ["each way the product in B differs from A - design, colours, lettering, shape"],\n'
    ' "added_text": true or false (are there words in B that are not printed on the product),\n'
    ' "physically_real": true or false (is the product in B held, worn, lying on or hanging from something '
    'the way a real one would be - not floating in the air or in front of things, straps actually round '
    'what holds them, touching what it rests on)}')


class SpendError(SceneError):
    """Today's allowance for scenes is used up - stop asking, not just this one."""


def _ea():
    return _load("emily_assets_paid", "emily-assets.py")


def limits(path=None):
    """The kit's two money settings, from tasks/limits.json beside the other
    caps: the most kits may spend on scenes a day, and what must be left of
    the day's AI budget for Emily's art."""
    path = Path(path) if path else Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent)) / "tasks" / "limits.json"
    try:
        d = json.loads(path.read_text())
    except Exception:
        d = {}
    out = {}
    for k in LIMITS_KEYS:
        v = d.get(k, LIMITS_DEFAULTS[k])
        out[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else LIMITS_DEFAULTS[k]
    return out


def allowance(spent_today, about_to, budget_read, caps):
    """(ok, why) for spending `about_to` more on scenes today. Two rules,
    both checked: the kits' own cap, and leaving Emily her share of the
    day's AI budget as OpenRouter counts it (budget.py). An unreadable
    budget is a no - this is spending, and unknown is not 'room left'."""
    if spent_today + about_to > caps["tiktok_daily_ai"] + 1e-9:
        return False, (f"today's scene allowance is spent (${spent_today:.2f} of "
                       f"${caps['tiktok_daily_ai']:.2f})")
    try:
        state = budget_read()
    except Exception as exc:
        return False, f"the AI budget could not be read ({str(exc)[:80]}) - not spending blind"
    left = state.get("ai_left_today")
    if left is None:
        return False, "OpenRouter did not say what is left today - not spending blind"
    if left - about_to < caps["tiktok_leave_for_emily"]:
        return False, (f"only ${left:.2f} of today's AI budget is left, and "
                       f"${caps['tiktok_leave_for_emily']:.2f} is kept for Emily's art")
    return True, None


def paid_prompt(thing, n):
    places = PAID_SCENES.get(thing) or PAID_DEFAULT
    place = places[n % len(places)]
    if place.startswith(FLAT_LAY):
        return FLAT_ASK.format(thing=thing, scene=place[len(FLAT_LAY):].strip())
    return PAID_ASK.format(thing=thing, scene=place)


def _letters(text):
    return "".join(c for c in str(text or "").lower() if c.isalnum())


def design_problems(reading):
    """What is wrong with the redrawn product, from the vision model's
    answers. Code decides, and an answer it cannot read is a failure - an
    unread check is not a passed one."""
    if not isinstance(reading, dict):
        return ["the design check's answer could not be read"]
    need = ("product_visible", "same_design", "words_a", "words_b", "differences", "added_text",
            "physically_real")
    missing = [k for k in need if k not in reading]
    if missing:
        return [f"the design check did not answer {', '.join(missing)}"]
    out = []
    if reading["product_visible"] is not True:
        out.append("the product is not clearly shown with its design facing the camera")
    if reading["same_design"] is not True:
        out.append("the printed design was changed")
    a, b = _letters(reading["words_a"]), _letters(reading["words_b"])
    if a != b:
        out.append(f"the lettering changed: {str(reading['words_a'])[:60]!r} became {str(reading['words_b'])[:60]!r}")
    diffs = [str(x)[:120] for x in reading["differences"] or [] if str(x).strip()] \
        if isinstance(reading["differences"], list) else [str(reading["differences"])[:120]]
    out += [f"changed: {x}" for x in diffs]
    if reading["added_text"] is True:
        out.append("words were added that are not on the product")
    if reading["physically_real"] is not True:
        out.append("it does not sit in the scene the way a real one would - floating, or its straps "
                   "holding onto nothing")
    return out


def design_check(reference, picture, key, post=None):
    """(problems, cost) - the vision model's comparison of the real photo and
    the new picture, judged by design_problems()."""
    ea = _ea()
    parts = [{"type": "text", "text": DESIGN_ASK}]
    for label, path in (("A", reference), ("B", picture)):
        data = base64.b64encode(Path(path).read_bytes()).decode()
        parts += [{"type": "text", "text": f"Image {label}:"},
                  {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}]
    body = json.dumps({"model": os.environ.get("SCENE_CHECK_MODEL") or CHECK_MODEL,
                       "messages": [{"role": "user", "content": parts}],
                       "max_tokens": CHECK_MAX_TOKENS, "usage": {"include": True}}).encode()
    req = urllib.request.Request(ea.OR_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cljxs/cljxs", "X-Title": "scene-check"})
    data = (post or ea.post)(req)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return design_problems(ea.read_proof(msg.get("content"))), ea.cost_of(data.get("usage"))


def paid_scene(photo, thing, out, n, scratch, spent_today=0.0, key=None, generate=None,
               check_design=None, budget_read=None, caps=None):
    """One PAID scene at `out` (1080x1920 jpg): the product drawn into a
    scene by the image model, then compared with its real photo. Returns
    {ok, why, checks, prompt, source, cost}. Raises SpendError when today's
    allowance would be crossed - before spending, never after."""
    scratch = Path(scratch)
    if key is None:
        key = _load("preflight_scene", "preflight.py").openrouter_key()
    if not key:
        raise SpendError("no OpenRouter key - paid scenes need the key the agents use")
    caps = caps or limits()
    if budget_read is None:
        budget_read = lambda: _load("budget_scene", "budget.py").read(key)
    ea = _ea()
    generate = generate or (lambda path, prompt, images: ea.generate(
        path, prompt, key, model=os.environ.get("SCENE_IMAGE_MODEL") or PAID_MODEL,
        direction="", aspect="9:16", images=images))
    check_design = check_design or (lambda ref, pic: design_check(ref, pic, key))

    ref = scratch / "reference.jpg"
    ffmpeg("-i", photo, "-vf", f"scale='min({REF_SIDE},iw)':-2", "-frames:v", "1", "-q:v", "3", ref)
    prompt = paid_prompt(thing, n)
    spent, last = 0.0, None
    for attempt in range(PAID_TRIES):
        ok, why = allowance(spent_today + spent, IMAGE_GUESS + CHECK_GUESS, budget_read, caps)
        if not ok:
            if attempt == 0:
                raise SpendError(why)
            break
        drawn = scratch / f"drawn-{n}-{attempt}.img"
        _bytes, usage = generate(drawn, prompt, [("image/jpeg", ref.read_bytes())])
        cost = ea.cost_of(usage)
        spent += cost if cost is not None else IMAGE_GUESS
        w, h = probe_size(drawn)
        if h / max(w, 1) < MIN_TALL:
            last = {"design": {"ok": False, "said": f"came back {w}x{h}, not a tall picture"}}
            continue
        ffmpeg("-i", drawn, "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
               "-frames:v", "1", "-q:v", "3", out)
        problems, ccost = check_design(ref, out)
        spent += ccost if ccost is not None else CHECK_GUESS
        last = {"design": {"ok": not problems,
                           "said": "; ".join(problems) if problems else "the design matches the real photo"}}
        if not problems:
            break
    failed = [v["said"] for v in (last or {}).values() if not v["ok"]]
    return {"ok": bool(last) and not failed, "why": "; ".join(failed) or (None if last else "nothing was drawn"),
            "checks": last or {}, "prompt": prompt, "source": "paid", "cost": round(spent, 4)}


MODES = ("paid", "free", "off")


def mode():
    """Which scenes kits get: TIKTOK_SCENES in the environment, else paid."""
    m = (os.environ.get("TIKTOK_SCENES") or "paid").strip().lower()
    return m if m in MODES else "paid"


def make_scene(photo, thing, out, n, seed, scratch, spent_today=0.0, how=None, **kw):
    """One scene of whichever kind `how` (or mode()) says."""
    how = how or mode()
    if how == "free":
        return free_scene(photo, thing, out, n, seed, scratch, **kw)
    if how == "paid":
        return paid_scene(photo, thing, out, n, scratch, spent_today, **kw)
    raise SceneError("scenes are switched off (TIKTOK_SCENES=off)")


def cmd_try(a):
    out = Path(a.out or tempfile.mkdtemp(prefix="scenes-"))
    out.mkdir(parents=True, exist_ok=True)
    bad, spent = 0, 0.0
    for n in range(a.n):
        with tempfile.TemporaryDirectory() as scratch:
            target = out / f"scene-{n + 1}.jpg"
            try:
                rep = make_scene(a.photo, a.thing, target, n, a.seed + n, scratch,
                                 spent_today=spent, how=a.how)
                spent += rep.get("cost") or 0.0
            except SceneError as exc:
                print(f"scene {n + 1}: FAILED - {exc}")
                bad += 1
                continue
        bad += not rep["ok"]
        print(f"scene {n + 1}: {'PASS' if rep['ok'] else 'FAIL'}  {target}")
        for k, v in rep["checks"].items():
            print(f"    {'ok ' if v['ok'] else 'BAD'} {k:<11}{v['said']}")
    if spent:
        print(f"spent ${spent:.3f} (OpenRouter's own figures where it gave them)")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("try", help="make scenes from one product photo and show the checker's report")
    t.add_argument("photo")
    t.add_argument("--thing", default="tote bag")
    t.add_argument("--n", type=int, default=2)
    t.add_argument("--seed", type=int, default=1)
    t.add_argument("--out")
    t.add_argument("--how", choices=["paid", "free"], help="which kind (default: TIKTOK_SCENES, else paid)")
    t.set_defaults(fn=cmd_try)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
