#!/usr/bin/env python3
"""
knockout.py <in.png> <out.png> — make the background transparent.

    python3 scripts/knockout.py design.png design-cutout.png

A sticker is die-cut, so an opaque square is fine. A garment is not: an opaque
file prints the background as a visible rectangle around the art - a white box
on a black tee, a faint panel on a white one. Nothing Emily generates today
has an alpha channel. The placeholder writes PNG colour type 2 (RGB), and the
image model returns an opaque image, so this is the one step between Emily's
art and anything wearable.

It fills INWARD from the border rather than thresholding the whole image. A
global "remove everything near-white" rule also removes the white inside the
design - eyes, glints, highlights - and leaves holes you only notice on the
printed garment. Only background actually connected to the edge is removed.

It refuses rather than guesses. If almost nothing was removed there was no
flat background to find; if almost everything was, the art itself matched the
background. Both produce a file that looks plausible and prints wrong, which
is the failure this repo keeps paying for, so neither is written.

Standard library only: zlib does the compression, the rest is here.
"""

import struct
import sys
import zlib
from collections import deque
from pathlib import Path

DEFAULT_TOLERANCE = 32      # per-channel distance that still counts as background
MIN_REMOVED_PCT = 2.0       # below this, no background was found
BORDER_MIN_PCT = 70.0       # the border must actually BE one colour
MAX_REMOVED_PCT = 92.0      # above this, the art matched the background

# After the background is gone, whatever the model drew outside the design is
# still there. A compass emblem came back with two pen-stroke scratches beside
# it: not background, so the flood left them, and they went onto a hoodie.
#
# A stroke like that is a tiny fraction of the artwork. Anything under this
# share of the remaining opaque pixels is a speck, not a design element.
SPECK_MAX_PCT = 1.5

# ...but only when there IS one main subject. A design that is genuinely two
# or three parts - an icon over a word, a pair of crests - has no speck to
# find, and guessing at one would delete half the art. If the largest piece
# does not hold at least this much, nothing is touched and the caller is told.
DOMINANT_MIN_PCT = 80.0


class PngError(Exception):
    pass


# --------------------------------------------------------------------------
# decode
# --------------------------------------------------------------------------

def _chunks(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        kind = "JPEG" if data[:2] == b"\xff\xd8" else "not a PNG"
        raise PngError(f"{kind}. This reads PNG only - the image model can return "
                       f"either, and a JPEG has no alpha channel to write into.")
    i = 8
    while i < len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        tag = data[i + 4:i + 8]
        yield tag, data[i + 8:i + 8 + length]
        i += 8 + length + 4          # +4 for the CRC we do not check


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(path):
    """-> (width, height, pixels) where pixels is a bytearray of RGBA."""
    data = Path(path).read_bytes()
    idat, palette, trns, ihdr = bytearray(), None, None, None
    for tag, body in _chunks(data):
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = body
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if not ihdr:
        raise PngError("no IHDR chunk - the file is truncated or not a PNG")

    w, h, depth, colour, comp, filt, interlace = ihdr
    if depth != 8:
        raise PngError(f"{depth}-bit PNG; this handles 8-bit, which is what every "
                       f"image model here returns")
    if interlace:
        raise PngError("interlaced PNG; not supported (and not something the model emits)")

    # samples per pixel by colour type: 0 grey, 2 RGB, 3 palette, 4 grey+A, 6 RGBA
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        raise PngError(f"unknown PNG colour type {colour}")
    if colour == 3 and palette is None:
        raise PngError("palette PNG with no PLTE chunk")

    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(w * h * 4)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        if ft:
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                v = line[x]
                if ft == 1:   line[x] = (v + a) & 0xFF
                elif ft == 2: line[x] = (v + b) & 0xFF
                elif ft == 3: line[x] = (v + ((a + b) >> 1)) & 0xFF
                elif ft == 4: line[x] = (v + _paeth(a, b, c)) & 0xFF
                else: raise PngError(f"unknown filter type {ft} on row {y}")
        prev = line

        o = y * w * 4
        for x in range(w):
            s = x * channels
            if colour == 2:
                r, g, b_, al = line[s], line[s + 1], line[s + 2], 255
            elif colour == 6:
                r, g, b_, al = line[s], line[s + 1], line[s + 2], line[s + 3]
            elif colour == 0:
                r = g = b_ = line[s]; al = 255
            elif colour == 4:
                r = g = b_ = line[s]; al = line[s + 1]
            else:                                   # palette
                idx = line[s]
                r, g, b_ = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
                al = trns[idx] if trns and idx < len(trns) else 255
            j = o + x * 4
            out[j] = r; out[j + 1] = g; out[j + 2] = b_; out[j + 3] = al
    return w, h, out


# --------------------------------------------------------------------------
# encode
# --------------------------------------------------------------------------

def encode(path, w, h, px):
    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(h):
        raw.append(0)                              # filter: none
        raw += px[y * w * 4:(y + 1) * w * 4]
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))   # 6 = RGBA
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    Path(path).write_bytes(png)
    return len(png)


# --------------------------------------------------------------------------
# the knockout
# --------------------------------------------------------------------------

def border_pixels(w, h):
    for x in range(w):
        yield x, 0
        yield x, h - 1
    for y in range(1, h - 1):
        yield 0, y
        yield w - 1, y


def border_agreement(w, h, px, bg, tolerance):
    """What fraction of the border is within tolerance of bg.

    "How much got removed" is not enough on its own. A gradient has no flat
    background at all, yet the most common border colour still has a band
    around it, so a percentage-removed check passes and writes a file with
    ragged chunks missing - plausible, and wrong, which is the failure this
    script exists to refuse. If the border is not one colour, there is nothing
    here to knock out.
    """
    br, bg_, bb = bg
    total = hit = 0
    for x, y in border_pixels(w, h):
        j = (y * w + x) * 4
        total += 1
        if (abs(px[j] - br) <= tolerance and abs(px[j + 1] - bg_) <= tolerance
                and abs(px[j + 2] - bb) <= tolerance):
            hit += 1
    return 100.0 * hit / max(total, 1)


def background_colour(w, h, px):
    """The most common colour around the border.

    Sampling the whole border rather than one corner: art that bleeds into a
    corner would otherwise be taken for the background and erased.
    """
    counts = {}
    for x in range(w):
        for y in (0, h - 1):
            j = (y * w + x) * 4
            k = (px[j], px[j + 1], px[j + 2])
            counts[k] = counts.get(k, 0) + 1
    for y in range(h):
        for x in (0, w - 1):
            j = (y * w + x) * 4
            k = (px[j], px[j + 1], px[j + 2])
            counts[k] = counts.get(k, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def knockout(w, h, px, tolerance=DEFAULT_TOLERANCE, bg=None):
    """Flood-fill the background from the border. Returns (removed, bg)."""
    bg = bg or background_colour(w, h, px)
    br, bg_, bb = bg

    def matches(j):
        return (abs(px[j] - br) <= tolerance
                and abs(px[j + 1] - bg_) <= tolerance
                and abs(px[j + 2] - bb) <= tolerance)

    seen = bytearray(w * h)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            q.append((x, y))

    removed = 0
    while q:
        x, y = q.popleft()
        i = y * w + x
        if seen[i]:
            continue
        j = i * 4
        if not matches(j):
            continue
        seen[i] = 1
        px[j + 3] = 0
        removed += 1
        if x > 0: q.append((x - 1, y))
        if x < w - 1: q.append((x + 1, y))
        if y > 0: q.append((x, y - 1))
        if y < h - 1: q.append((x, y + 1))
    return removed, bg


def components(w, h, px):
    """Connected runs of opaque pixels, largest first, as [(size, [indices])].

    Four-connected, same as the flood above - a diagonal touch is not a join,
    and treating it as one merges a speck into the art it sits beside.
    """
    seen = bytearray(w * h)
    out = []
    for start in range(w * h):
        if seen[start] or px[start * 4 + 3] == 0:
            continue
        members = []
        q = deque([start])
        seen[start] = 1
        while q:
            i = q.popleft()
            members.append(i)
            x, y = i % w, i // w
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    n = ny * w + nx
                    if not seen[n] and px[n * 4 + 3] != 0:
                        seen[n] = 1
                        q.append(n)
        out.append((len(members), members))
    out.sort(key=lambda c: -c[0])
    return out


def despeckle(w, h, px, max_pct=SPECK_MAX_PCT, dominant_pct=DOMINANT_MIN_PCT):
    """Erase stray marks left beside the artwork. Returns (pixels, count, why).

    `why` is always a sentence, because "nothing was removed" has two very
    different causes - there was nothing to remove, or this design has several
    real parts and removing the smaller ones would have been vandalism - and
    the caller prints whichever it was.
    """
    parts = components(w, h, px)
    if len(parts) <= 1:
        return 0, 0, "one connected piece - nothing beside the art"

    total = sum(size for size, _ in parts)
    biggest = parts[0][0]
    if total == 0 or biggest * 100.0 / total < dominant_pct:
        return 0, 0, (f"{len(parts)} pieces and no dominant one "
                      f"({biggest * 100.0 / total:.0f}% largest) - this looks "
                      f"like a multi-part design, so nothing was removed")

    wiped = wiped_parts = 0
    for size, members in parts[1:]:
        if size * 100.0 / total > max_pct:
            continue
        for i in members:
            px[i * 4 + 3] = 0
        wiped += size
        wiped_parts += 1
    if not wiped_parts:
        return 0, 0, f"{len(parts)} pieces, none small enough to be a speck"
    return wiped, wiped_parts, (f"removed {wiped_parts} stray mark(s), "
                                f"{wiped:,} px ({wiped * 100.0 / total:.2f}% "
                                f"of the artwork)")


def main():
    if len(sys.argv) < 3:
        print("usage: knockout.py <in.png> <out.png> [--tolerance N] [--keep-specks]", file=sys.stderr)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    tol = DEFAULT_TOLERANCE
    if "--tolerance" in sys.argv:
        tol = int(sys.argv[sys.argv.index("--tolerance") + 1])

    try:
        w, h, px = decode(src)
    except PngError as exc:
        print(f"knockout: {exc}", file=sys.stderr)
        return 2

    bg = background_colour(w, h, px)
    agree = border_agreement(w, h, px, bg, tol)
    removed, _ = knockout(w, h, px, tol, bg)
    pct = 100.0 * removed / (w * h)
    print(f"{src}: {w}x{h}, background rgb{bg}, border {agree:.0f}% that colour, "
          f"removed {removed:,} px ({pct:.1f}%)")

    if agree < BORDER_MIN_PCT:
        print(f"knockout: only {agree:.0f}% of the border is one colour - this art has "
              f"no flat background to remove. Nothing written.\n"
              f"  Removing part of it would leave ragged edges that print. Ask for "
              f"art on a plain, even background.", file=sys.stderr)
        return 1
    if pct < MIN_REMOVED_PCT:
        print(f"knockout: only {pct:.1f}% was removed - there is no flat background "
              f"here to knock out. Nothing written.\n"
              f"  Art for a garment needs a plain, even background. Ask for one in "
              f"the prompt, or raise --tolerance if it is merely uneven.", file=sys.stderr)
        return 1
    if pct > MAX_REMOVED_PCT:
        print(f"knockout: {pct:.1f}% was removed - the artwork itself matched the "
              f"background, so what is left is not a design. Nothing written.\n"
              f"  Lower --tolerance, or ask for art that contrasts with its "
              f"background.", file=sys.stderr)
        return 1

    # Only after the refusals above: despeckling a file that was never going
    # to be written is work for nothing, and despeckling one whose background
    # was not found would be measuring specks against noise.
    if "--keep-specks" not in sys.argv:
        wiped, parts, why = despeckle(w, h, px)
        print(f"despeckle: {why}")

    n = encode(dst, w, h, px)
    print(f"wrote {dst} ({n:,} bytes, RGBA)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
