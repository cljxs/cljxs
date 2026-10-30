"""How far along a clip's remake is, for the page that is waiting on it.

Owner, 2026-10-22: "When I click update video I need a percentage or
something showing ... progress". Update video (and Back to transcript) runs
one ffmpeg for a minute or two while the page could only say "working".

While a clip is being remade, two files sit beside it:

    NN.progress.json   what is happening (step), how long the clip is, when
                       it started - written here
    NN.progress.txt    ffmpeg's own report (-progress): out_time_us, how
                       far into the clip it has written

read() turns them into {running, step, percent}. The page asks for that
every couple of seconds (`clipper progress CLIP`); nothing is worked out in
the page. Both files go when the remake ends, however it ends.
"""

import json
import time
from pathlib import Path

# A report older than this is from a remake that was killed, not one running:
# the Deck gives a remake 30 minutes (clip.js REMAKE_TIMEOUT_MS).
STALE_SECONDS = 40 * 60


def files(clip_path):
    p = Path(clip_path)
    return p.with_name(p.stem + ".progress.json"), p.with_name(p.stem + ".progress.txt")


class Progress:
    """One remake's report. Use as a context manager: the files go at the end."""

    def __init__(self, clip_path, duration):
        self.status, self.ffmpeg = files(clip_path)
        self.duration = float(duration)
        self.started = time.time()

    def step(self, words, measured=False):
        """measured: ffmpeg reports this step, so it has a percentage."""
        self.ffmpeg.unlink(missing_ok=True)
        self.status.write_text(json.dumps({"step": words, "measured": measured, "duration": self.duration,
                                           "started": self.started}))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for f in (self.status, self.ffmpeg):
            f.unlink(missing_ok=True)
        return False


def written_seconds(text):
    """How far into the clip ffmpeg has written, from its -progress report:
    the last out_time_us (microseconds; "N/A" before the first frame)."""
    last = 0.0
    for line in (text or "").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "out_time_us":
            try:
                last = max(last, int(value.strip()) / 1e6)
            except ValueError:
                pass
    return last


def read(clip_path, now=None):
    status, ffmpeg = files(clip_path)
    try:
        s = json.loads(status.read_text())
    except (OSError, ValueError):
        return {"running": False}
    now = time.time() if now is None else now
    elapsed = now - float(s.get("started") or now)
    if elapsed > STALE_SECONDS:
        return {"running": False, "stale": True}
    percent = None
    if s.get("measured"):
        try:
            done = written_seconds(ffmpeg.read_text())
        except OSError:
            done = 0.0
        percent = int(min(99, 100 * done / s["duration"])) if s.get("duration") else None
    return {"running": True, "step": s.get("step"), "percent": percent, "seconds": int(elapsed)}
