#!/usr/bin/env python3
"""
scene.py — the real product, cut out of its mockup, set into a free
AI-drawn scene, and checked for whether the light matches.

    scene.py try photo.jpg --thing "tote bag"        two scenes, with the checker's report
    scene.py try photo.jpg --thing mug --n 1 --out /tmp/scenes

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


def make_scene(photo, thing, out, n, seed, scratch, fetch=fetch_background):
    """One scene picture at `out` (1080x1920 jpg). Returns its report:
    {ok, why, checks, prompt, source, match}. A scene that fails a check is
    still written, so the owner can see why; `ok` says whether to use it."""
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


def cmd_try(a):
    out = Path(a.out or tempfile.mkdtemp(prefix="scenes-"))
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for n in range(a.n):
        with tempfile.TemporaryDirectory() as scratch:
            target = out / f"scene-{n + 1}.jpg"
            try:
                rep = make_scene(a.photo, a.thing, target, n, a.seed + n, scratch)
            except SceneError as exc:
                print(f"scene {n + 1}: FAILED - {exc}")
                bad += 1
                continue
        bad += not rep["ok"]
        print(f"scene {n + 1}: {'PASS' if rep['ok'] else 'FAIL'}  {target}")
        for k, v in rep["checks"].items():
            print(f"    {'ok ' if v['ok'] else 'BAD'} {k:<11}{v['said']}")
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
    t.set_defaults(fn=cmd_try)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
