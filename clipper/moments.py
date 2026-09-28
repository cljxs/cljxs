"""From words to candidate clips.

A clip starts at the start of a sentence and ends at the end of one - never
mid-sentence. That single rule does most of what "avoid awkward cuts" means,
and it is enforced here by construction rather than checked afterwards: a
window is a run of whole sentences, so there is no way to produce one that
starts or stops inside a sentence.
"""

TERMINAL = ".?!"
CLOSERS = "\"'”’)]"


def ends_sentence(word):
    w = word.strip().rstrip(CLOSERS)
    return bool(w) and w[-1] in TERMINAL


def sentences(words, pause=0.8):
    """Group words into sentences: split after . ? ! or at a pause of
    `pause` seconds, whichever comes first. Whisper drops a lot of
    punctuation in fast speech, and a long pause is a sentence end a
    listener hears even when none is written."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else None
        if nxt is None or ends_sentence(w["word"]) or gap >= pause:
            first, last = words[cur[0]], words[cur[-1]]
            out.append({"first": cur[0], "last": cur[-1], "start": first["start"],
                        "end": last["end"],
                        "text": "".join(words[j]["word"] for j in cur).strip(),
                        "pause_after": gap})
            cur = []
    return out


def windows(sents, min_len, max_len):
    """Every run of whole sentences lasting min_len..max_len seconds, as
    (first_sentence, last_sentence) index pairs. A single sentence longer than
    max_len is not a clip - there is nowhere to cut it."""
    out = []
    for i, s in enumerate(sents):
        for j in range(i, len(sents)):
            dur = sents[j]["end"] - s["start"]
            if dur > max_len:
                break
            if dur >= min_len:
                out.append((i, j))
    return out


def overlaps(a, b):
    return a["start"] < b["end"] and b["start"] < a["end"]


def choose(scored, max_clips, min_score):
    """Best first, never two that overlap. A clip overlapping a better one is
    the same moment twice, and posting both is a duplicate."""
    chosen = []
    for c in sorted(scored, key=lambda c: c["score"], reverse=True):
        if c["score"] < min_score or len(chosen) >= max_clips:
            break
        if not any(overlaps(c, k) for k in chosen):
            chosen.append(c)
    return chosen


def cut_points(words, first, last, lead=0.15, tail=0.35):
    """Where the file is actually cut: a breath before the first word and a
    beat after the last, but never into the next word - a clip that ends on
    the first syllable of the next sentence sounds chopped."""
    start = max(0.0, words[first]["start"] - lead)
    end = words[last]["end"] + tail
    if last + 1 < len(words):
        end = min(end, words[last + 1]["start"] - 0.05)
    return round(start, 3), round(max(end, words[last]["end"]), 3)
