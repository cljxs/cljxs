"""A watermark handed out flattened onto white, made see-through again.

Curious Mike's "YT: @mpj" is only available as a picture on a white canvas
(1568x523, no alpha). Overlaid as it is, it is a white box on every clip; typed
out again, it breaks the campaign's "use the supplied file" rule. So this keeps
the supplied pixels and removes only the canvas:

* the letters are the light areas the white background cannot reach without
  crossing the darker glow ring around them - found by flood-filling from the
  edges over pixels at least THRESHOLD bright. They keep their own colour,
  fully opaque;
* everything else - canvas and glow - becomes black at the opacity that makes
  it look exactly as it did: over white, alpha = 1 - brightness/255 gives
  back the original grey. So over a white background the result IS the
  supplied file (checked by a test, within 1 level), and over video the glow
  is a soft shadow, which is what it was drawn to be.

The empty canvas around the mark is trimmed off (pixels under 1% opaque);
nothing of the mark itself is.

THRESHOLD was found on the real file: at 236 the enclosed regions are exactly
the nine glyph parts (Y, T, two colon dots, @, m, p, j and its dot); at 225
and below the fill leaks through thin places in the ring and the letters
vanish. A different campaign's file is checked the same way - no letters
found is refused - but look at the first clip.
"""

import subprocess
from collections import deque

THRESHOLD = 236
TRIM_ALPHA = 2          # of 255: under 1% opaque counts as empty canvas
PAD = 4


def _decode(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return w, h, raw


def _encode(rgba, w, h, dest):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                    "-s", f"{w}x{h}", "-i", "-", "-frames:v", "1", str(dest)],
                   input=bytes(rgba), capture_output=True, check=True)


def cut_out_white(src, dest):
    """Write a see-through copy of a watermark flattened onto white.
    Returns (width, height, (x, y)): the size written and where that box sat
    in the original. Raises ValueError when the file is not a mark on a
    white canvas."""
    w, h, raw = _decode(src)
    g = [min(raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]) for i in range(w * h)]
    edge = [i for i in range(w)] + [(h - 1) * w + i for i in range(w)] + \
           [y * w for y in range(h)] + [y * w + w - 1 for y in range(h)]
    if min(g[i] for i in edge) < 250:
        raise ValueError("the watermark's edges are not white - this is for a mark drawn on a "
                         "white canvas, and this one is not")
    outside = bytearray(w * h)
    q = deque(edge)
    while q:
        i = q.popleft()
        if outside[i] or g[i] < THRESHOLD:
            continue
        outside[i] = 1
        x = i % w
        if x > 0:
            q.append(i - 1)
        if x < w - 1:
            q.append(i + 1)
        if i >= w:
            q.append(i - w)
        if i < w * (h - 1):
            q.append(i + w)
    rgba = bytearray(w * h * 4)
    letters = 0
    for i in range(w * h):
        if not outside[i] and g[i] >= THRESHOLD:
            rgba[i * 4:i * 4 + 4] = bytes((raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2], 255))
            letters += 1
        else:
            rgba[i * 4 + 3] = 255 - g[i]          # black; over white it is g again
    if letters < 100:
        raise ValueError("no lettering found inside the watermark's glow - Clip cannot cut "
                         "this one out; ask the campaign for a transparent PNG")
    xs = [i % w for i in range(w * h) if rgba[i * 4 + 3] > TRIM_ALPHA]
    ys = [i // w for i in range(w * h) if rgba[i * 4 + 3] > TRIM_ALPHA]
    x0, x1 = max(0, min(xs) - PAD), min(w - 1, max(xs) + PAD)
    y0, y1 = max(0, min(ys) - PAD), min(h - 1, max(ys) + PAD)
    tw, th = x1 - x0 + 1, y1 - y0 + 1
    out = bytearray()
    for y in range(y0, y1 + 1):
        out += rgba[(y * w + x0) * 4:(y * w + x1 + 1) * 4]
    _encode(out, tw, th, dest)
    return tw, th, (x0, y0)
