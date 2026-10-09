"""Where the people are in each camera shot, so a vertical clip shows them.

Owner, 2026-10-07: "In every clip of Gil's Arena it cuts to this camera
angle showing nothing." A short is 9:16 and the footage is 16:9, so a
vertical clip is a strip about a third of the frame wide. The middle third
is right for a close-up and wrong for a wide shot: Gil's Arena seats its
hosts at the two ends of a couch, and the middle of that shot is the table
between them.

So each clip is framed shot by shot:

1. One pass over the clip's stretch of footage, small (DETECT_WIDTH wide) at
   SAMPLE_FPS. A camera cut is ffmpeg's scene score OR a jump in a thumbnail
   of the picture: the scene score alone missed a cut from a close-up to an
   empty shot in testing (0.3 is its usual line; that cut scored under it).
   YuNet, a small face detector that runs on the CPU, finds the faces.
2. Each shot gets a layout from where its faces are:
   * "crop"  - one face, or several close enough to share the strip: the
               strip, centred on them.
   * "split" - two people too far apart for one strip (the couch): the top
               half on the left one, the bottom half on the right one.
   * "full"  - three or more spread out, or no face found: the whole frame
               across the middle on a blurred copy of itself. Nobody is cut
               out; it is the layout that cannot be wrong, only small.
3. render.py cuts each shot with its own layout and joins them.

Faces are counted across a shot, not one frame: a face seen in fewer than
MIN_SEEN of a shot's samples is a flicker or a poster, not a person.

Free: OpenCV and the model (MIT licence, clipper/face_detection_yunet_2023mar.onnx)
run on the droplet. Without OpenCV, plan() says so and the caller keeps the
centre crop - a missing library must not stop the clips.
"""

import statistics
import subprocess
import tempfile
from pathlib import Path

from clipper import media

MODEL = Path(__file__).resolve().parent / "face_detection_yunet_2023mar.onnx"
DETECT_WIDTH = 480          # px the footage is looked at: faces on a wide shot stay ~25 px
SAMPLE_FPS = 10             # frames looked at a second; the cut lands within 0.1 s
DETECT_EVERY = 2            # faces on every 2nd sample - 5 a second is plenty
SCENE_CUT = 0.30            # ffmpeg scene score that is a camera cut...
DIFF_CUT = 10.0             # ...or a 48x27 thumbnail changing this much (0-255) between samples
DIFF_RATIO = 4.0            # ...and this many times the clip's usual change, so gesturing is not a cut
MIN_SHOT = 0.5              # s: a shorter "shot" is a flash, joined to the one before
FACE_SCORE = 0.7            # YuNet's confidence
MIN_FACE = 0.04             # of the frame's height: smaller is a crowd or a poster
MIN_SEEN = 0.3              # share of a shot's samples a face must be in
CLUSTER_GAP = 0.10          # of the frame's width: faces closer than this are one person
SHARE = 0.85                # faces spanning under this much of a strip can share it
SPLIT_FACES = 4.5           # a split half is this many face heights tall...
SPLIT_MIN = 0.35            # ...but at least this much of the frame's height
THUMB = 24                  # px: each sighting keeps a grey thumbnail of the face this big
# A face that never changes is a picture of one: Gil's Arena's set has a mural
# of LeBron and Harden behind the couch, and a clip split the screen between
# a host and the mural (owner, 2026-10-09). A real face blinks, talks and
# moves. When a shot has a face that changes at least LIVE_MIN (mean grey
# levels between sightings), a face changing less than STILL_SHARE of it is
# dropped. A shot whose faces are ALL still - a photo on screen, a frozen
# frame - is left alone: there is nothing to tell the real ones by.
# Measured 2026-10-09 on a public-domain interview (archive.org, "News Now -
# Evelyn Gomez") with a still portrait pasted beside her: the portrait 0.0,
# people talking 5.8-8.9, a quiet listener 3.4 (44% of the speaker beside him).
# A face goes only if it is under BOTH: a share of the liveliest face, and an
# absolute ceiling - so a quiet listener beside a very animated speaker stays.
# On grainy footage blown up 2.2x a still wall sketch read 3.3, as much as the
# listener: in doubt, a face is kept, which is how framing behaved before.
LIVE_MIN = 3.0
STILL_SHARE = 0.3
STILL_MAX = 1.5


class FramingUnavailable(Exception):
    """OpenCV or the model is missing - the caller keeps its centre crop."""


def even(x):
    return int(x) // 2 * 2


def _detector(w, h):
    try:
        import cv2
    except ImportError as exc:
        raise FramingUnavailable("OpenCV is not installed in Clip's venv "
                                 "(clipper/.venv/bin/pip install -r clipper/requirements.txt)") from exc
    if not MODEL.is_file():
        raise FramingUnavailable(f"the face model {MODEL.name} is missing")
    return cv2, cv2.FaceDetectorYN.create(str(MODEL), "", (w, h), FACE_SCORE)


def look(src, start, end, info):
    """One pass over [start, end): ([cut times], [(t, [(cx, cy, w, h), ...]), ...]),
    times from the clip's start, boxes in the source's own pixels."""
    sw, sh = info["width"], info["height"]
    dw, dh = DETECT_WIDTH, even(DETECT_WIDTH * sh / sw)
    cv2, det = _detector(dw, dh)
    import numpy as np
    scale = sw / dw
    with tempfile.TemporaryDirectory() as tmp:
        scores = Path(tmp) / "scores.txt"
        # The scene score is printed for every frame to a file, while the
        # frames themselves come down the pipe: one decode does both.
        vf = (f"fps={SAMPLE_FPS},scale={dw}:{dh},select='gte(scene\\,0)',"
              f"metadata=print:key=lavfi.scene_score:file={scores.name}")
        p = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}",
                              *media.input_args(src), "-t", f"{end - start:.3f}", "-an",
                              "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp)
        size, faces, i, diffs, prev = dw * dh * 3, [], 0, [], None
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
            thumb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (48, 27),
                               interpolation=cv2.INTER_AREA).astype(np.float32)
            if prev is not None:
                diffs.append((i / SAMPLE_FPS, float(np.abs(thumb - prev).mean())))
            prev = thumb
            if i % DETECT_EVERY == 0:
                _, found = det.detect(frame)
                boxes = []
                grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                for f in (found if found is not None else []):
                    x, y, w, h = (float(v) * scale for v in f[:4])
                    if h >= MIN_FACE * sh:
                        fx, fy, fw, fh = (int(round(float(v))) for v in f[:4])
                        patch = grey[max(0, fy):max(0, fy) + max(1, fh), max(0, fx):max(0, fx) + max(1, fw)]
                        thumb = cv2.resize(patch, (THUMB, THUMB), interpolation=cv2.INTER_AREA) \
                            if patch.size else None
                        boxes.append((x + w / 2, y + h / 2, w, h, thumb))
                faces.append((i / SAMPLE_FPS, boxes))
            i += 1
        err = p.stderr.read().decode(errors="replace")
        if p.wait() != 0:
            raise media.MediaError(f"framing could not read the footage: {err.strip()[-300:]}")
        cuts = []
        t = None
        for line in (scores.read_text() if scores.exists() else "").splitlines():
            if "pts_time:" in line:
                t = float(line.split("pts_time:")[1].split()[0])
            elif line.startswith("lavfi.scene_score=") and t is not None:
                if float(line.split("=", 1)[1]) > SCENE_CUT and t > 0:
                    cuts.append(t)
    usual = statistics.median(d for _, d in diffs) if diffs else 0.0
    for t, d in diffs:
        if d > DIFF_CUT and d > DIFF_RATIO * usual and not any(abs(t - c) < MIN_SHOT for c in cuts):
            cuts.append(t)
    return sorted(cuts), faces


def shots(cuts, duration):
    """[(t0, t1)] from cut times; a shot shorter than MIN_SHOT joins the one before."""
    edges = [0.0] + sorted(c for c in cuts if 0 < c < duration) + [duration]
    out = []
    for a, b in zip(edges, edges[1:]):
        if out and b - a < MIN_SHOT:
            out[-1] = (out[-1][0], b)
        elif out and out[-1][1] - out[-1][0] < MIN_SHOT:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def motion(sightings):
    """How much a face changes between sightings: the median mean grey
    difference of its thumbnails, in time order. None without thumbnails."""
    thumbs = [b[4] for _t, b in sorted(sightings, key=lambda x: x[0]) if len(b) > 4 and b[4] is not None]
    if len(thumbs) < 3:
        return None
    import numpy as np
    diffs = [float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()) for a, b in zip(thumbs, thumbs[1:])]
    return statistics.median(diffs)


def people(samples, sw, report=None):
    """The people in one shot's samples: [(cx, cy, w, h)] by x, each the median
    of its sightings, kept only if seen in MIN_SEEN of the samples - and, when
    the shot has a face that clearly moves, only if it moves too (a mural is
    not a person). `report`, a list, gets each face's motion."""
    if not samples:
        return []
    seen = sorted(((t, b) for t, boxes in samples for b in boxes), key=lambda tb: tb[1][0])
    groups = []
    for tb in seen:
        if groups and tb[1][0] - groups[-1][-1][1][0] <= CLUSTER_GAP * sw:
            groups[-1].append(tb)
        else:
            groups.append([tb])
    kept, gap = [], CLUSTER_GAP * sw
    for g in groups:
        lo, hi = g[0][1][0] - gap, g[-1][1][0] + gap
        frames = sum(1 for _, boxes in samples if any(lo <= x[0] <= hi for x in boxes))
        if frames >= MIN_SEEN * len(samples):
            kept.append((tuple(statistics.median(b[k] for _t, b in g) for k in range(4)), motion(g)))
    moves = [m for _p, m in kept if m is not None]
    live = max(moves) if moves else None
    if report is not None:
        report.extend(m for _p, m in kept)
    if live is not None and LIVE_MIN is not None and live >= LIVE_MIN:
        kept = [(p, m) for p, m in kept if m is None or m >= min(STILL_SHARE * live, STILL_MAX)]
    return [p for p, _m in kept]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def layout(folk, sw, sh):
    """One shot's layout from its people. See the module docstring."""
    cw = even(sh * 9 / 16)
    if not folk:
        return {"layout": "full", "why": "no face found"}
    left = min(c - w / 2 for c, _, w, _ in folk)
    right = max(c + w / 2 for c, _, w, _ in folk)
    if right - left <= SHARE * cw:
        x = clamp(even((left + right) / 2 - cw / 2), 0, sw - cw)
        return {"layout": "crop", "box": (cw, even(sh), x, 0),
                "why": f"{len(folk)} face(s), centred on them"}
    if len(folk) == 2:
        # Each half is 1080x960 (9:8). Zoomed to the faces - SPLIT_FACES face
        # heights tall, the same for both so neither person looks bigger -
        # but never tighter than SPLIT_MIN of the frame, which is all pixels.
        hh = even(clamp(SPLIT_FACES * max(f[3] for f in folk), SPLIT_MIN * sh, sh))
        hw = min(even(hh * 9 / 8), even(sw))
        halves = []
        for c, cy, _, _ in folk:
            halves.append((hw, hh, clamp(even(c - hw / 2), 0, sw - hw), clamp(even(cy - hh * 0.4), 0, sh - hh)))
        return {"layout": "split", "boxes": halves, "why": "two people too far apart for one strip"}
    return {"layout": "full", "why": f"{len(folk)} people across the frame"}


def plan(src, start, end, info):
    """[{t0, t1, layout, ...}] for the clip, times from its start. Adjacent shots
    with the same framing are one segment: a cut between two identical crops
    is invisible, and every segment is another branch of the filter graph."""
    sw, sh = info["width"], info["height"]
    duration = end - start
    cuts, faces = look(src, start, end, info)
    out = []
    for t0, t1 in shots(cuts, duration):
        seg = dict(layout(people([f for f in faces if t0 <= f[0] < t1], sw), sw, sh), t0=t0, t1=t1)
        if out and {k: v for k, v in out[-1].items() if k not in ("t0", "t1", "why")} == \
                {k: v for k, v in seg.items() if k not in ("t0", "t1", "why")}:
            out[-1]["t1"] = t1
        else:
            out.append(seg)
    return out


def summary(segments):
    """'3 shots: split 0-6s, crop 6-14s, full 14-31s' - kept on the clip, shown in the studio."""
    return f"{len(segments)} shot(s): " + ", ".join(
        f"{s['layout']} {s['t0']:.0f}-{s['t1']:.0f}s" for s in segments)
