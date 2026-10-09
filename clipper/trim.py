"""Trimming a clip somebody already cut - a Twitch clip, up to 60 s - down to
its moment: the setup, the funny or crazy thing, and the reaction.

THE MOMENT is where the clip gets clearly louder than its own usual level
and stays there for a couple of seconds: laughing, yelling, a whole room
reacting. On a stream that is nearly always where the thing happened, and it
is a measurement (loudness.json, per second) rather than a guess. With no
such burst, the strongest exclamation in the transcript ("no way", "bro",
"oh my god" - score.STRONG, the one list) stands in for it.

THE CUT starts on a whole sentence about SETUP_TARGET seconds before the
moment - never on "and"/"so"/"he" (score.DANGLING) when another start is in
reach - and ends when the noise has settled back down, at the end of the
sentence being said then. A clip with no moment at all only loses its dead
air: silence before the first word and after the last.

Everything here is arithmetic on the transcript and the loudness, so it is
free and gives the same answer twice. Believed, not yet tuned on a real
Plaqueboymax clip: the constants below are where to tune it.
"""

import statistics

from clipper import moments, score

SCORER = "trim-v1"
PEAK_DB = 6.0        # a moment is this many dB louder than the clip's own median...
PEAK_SECONDS = 2     # ...over this many seconds in a row, not one spike
SETTLED_DB = 3.0     # the reaction is over when it is back within this of the median
SETUP_TARGET = 12.0  # seconds of setup before the moment, ideally
SETUP_MIN, SETUP_MAX = 4.0, 25.0
MIN_LENGTH = 10.0    # shorter than this, it keeps more setup
LEAD, TAIL = 0.3, 0.8   # air kept before the first word and after the last
SILENT = -70.0       # quieter than this is silence, not part of "usual"


def _clock(t):
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def loud_peak(loud):
    """(second, dB over median) of the loudest PEAK_SECONDS-long stretch, or
    None when nothing stands PEAK_DB clear of the clip's usual level."""
    heard = [v for v in loud if v > SILENT]
    if len(loud) < PEAK_SECONDS or not heard:
        return None
    median = statistics.median(heard)
    best, at = None, None
    for s in range(len(loud) - PEAK_SECONDS + 1):
        level = min(loud[s:s + PEAK_SECONDS])      # sustained: its quietest second counts
        if best is None or level > best:
            best, at = level, s
    if best - median < PEAK_DB:
        return None
    return at, best - median


def word_peak(words):
    """When the last strong exclamation starts - "!", or one of score.STRONG,
    multi-word ones ("no way") included - or None."""
    hit = None
    for i, w in enumerate(words):
        ahead = "".join(x["word"] for x in words[i:i + 3]).strip().lower()
        if "!" in w["word"] or any(ahead.startswith(s) for s in score.STRONG):
            hit = w["start"]
    return hit


def settled(loud, after, median):
    """The first second after `after` from which loudness stays within
    SETTLED_DB of the median for PEAK_SECONDS seconds - the reaction over."""
    for s in range(int(after) + 1, len(loud)):
        if all(v < median + SETTLED_DB for v in loud[s:s + PEAK_SECONDS]):
            return float(s)
    return float(len(loud))


def pick(words, loud, duration, cfg=None):
    """(start, end, reasons) for one already-cut clip."""
    if not words:
        return 0.0, duration, ["no speech found: kept whole"]
    sents = moments.sentences(words, (cfg or {}).get("sentence_pause_seconds", 0.8))
    first, last = words[0]["start"], words[-1]["end"]
    found = loud_peak(loud)
    if found:
        peak, over = found
        median = statistics.median([v for v in loud if v > SILENT])
        why = f"the moment at {_clock(peak)}: {over:.0f} dB louder than the rest of the clip"
        react = settled(loud, peak + PEAK_SECONDS - 1, median)
    else:
        peak = word_peak(words)
        if peak is None:
            return (round(max(0.0, first - LEAD), 2), round(min(duration, last + TAIL), 2),
                    ["no stand-out moment: only the dead air trimmed"])
        why = f"the moment at {_clock(peak)}: the strongest reaction in what is said"
        react = peak + 2.0

    # Start: the whole sentence nearest SETUP_TARGET before the moment, a
    # dangling opener only when nothing else is in reach.
    starts = [s for s in sents if peak - SETUP_MAX <= s["start"] <= peak - SETUP_MIN]
    clean = [s for s in starts if score.first_word(s["text"]) not in score.DANGLING]
    pool = clean or starts or [s for s in sents if s["start"] <= peak] or sents[:1]
    open_on = min(pool, key=lambda s: abs(peak - SETUP_TARGET - s["start"]))

    # End: the sentence being said when the reaction settles, finished.
    close_on = next((s for s in sents if s["end"] >= react), sents[-1])
    start = max(0.0, open_on["start"] - LEAD)
    end = min(duration, max(close_on["end"] + TAIL, peak + PEAK_SECONDS))

    # Too short to land: take earlier sentences as setup until it is long enough.
    while end - start < MIN_LENGTH:
        earlier = [s for s in sents if s["start"] < start - 0.01]
        if not earlier:
            break
        start = max(0.0, earlier[-1]["start"] - LEAD)

    return round(start, 2), round(end, 2), [
        why, f"starts on the setup at {_clock(start)}",
        f"ends after the reaction at {_clock(end)} (of {_clock(duration)})"]
