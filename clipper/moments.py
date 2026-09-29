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


# The longest a "sentence" may run before it is split at its likeliest break.
# Whisper sometimes writes minutes of fast talk with no full stop and no gap
# between words: on the Curious Mike x Trae Young episode (2026-09-29) 39 of
# 80 minutes sat in 14 such runs, up to 525 s - and a window is whole
# sentences of at most max_clip_seconds, so none of it could be clipped,
# including the campaign's best moment. Half the default longest clip, so a
# window can still start or end between the pieces.
LONGEST = 30.0
MIN_PIECE_WORDS = 3


def split_long(words, idx, longest=LONGEST):
    """Word indices of one over-long sentence, cut into pieces no longer than
    `longest`: at the biggest gap between words, the one nearest the middle
    when gaps tie (they are mostly 0.0 in these runs), never leaving a piece
    of fewer than MIN_PIECE_WORDS words."""
    if (words[idx[-1]]["end"] - words[idx[0]]["start"] <= longest
            or len(idx) < 2 * MIN_PIECE_WORDS):
        return [idx]
    mid = (len(idx) - 1) / 2
    k = max(range(MIN_PIECE_WORDS, len(idx) - MIN_PIECE_WORDS + 1),
            key=lambda k: (round(words[idx[k]]["start"] - words[idx[k - 1]]["end"], 2),
                           -abs(k - mid)))
    return split_long(words, idx[:k], longest) + split_long(words, idx[k:], longest)


def sentences(words, pause=0.8, longest=LONGEST):
    """Group words into sentences: split after . ? ! or at a pause of
    `pause` seconds, whichever comes first. Whisper drops a lot of
    punctuation in fast speech, and a long pause is a sentence end a
    listener hears even when none is written. Anything still longer than
    `longest` is split by split_long."""
    out = []
    for cur in _grouped(words, pause):
        for piece in split_long(words, cur, longest):
            first, last = words[piece[0]], words[piece[-1]]
            nxt = words[piece[-1] + 1] if piece[-1] + 1 < len(words) else None
            out.append({"first": piece[0], "last": piece[-1], "start": first["start"],
                        "end": last["end"],
                        "text": "".join(words[j]["word"] for j in piece).strip(),
                        "pause_after": (nxt["start"] - last["end"]) if nxt else None})
    return out


def _grouped(words, pause):
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        # Rounded: word times are 3-decimal floats, and 3036.62 - 3035.82 is
        # 0.7999999999997 - a real 0.8 s pause read as shorter than 0.8.
        gap = round(nxt["start"] - w["end"], 3) if nxt else None
        if nxt is None or ends_sentence(w["word"]) or gap >= pause:
            out.append(cur)
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


def choose(scored, max_clips, min_score, hunt=None):
    """Best first, never two that overlap. A clip overlapping a better one is
    the same moment twice, and posting both is a duplicate.

    With a campaign's list (hunt), each topic first gets its own best clip,
    in the list's order - otherwise the one topic talked about longest
    takes every slot - and the rest go by score. A topic's pick may not be
    one that overlaps every remaining chance of a topic after it: a long
    window mentioning two topics would otherwise cost the list a clip."""
    ranked = sorted(scored, key=lambda c: c["score"], reverse=True)
    chosen = []

    def free(c):
        return (c["score"] >= min_score and len(chosen) < max_clips
                and not any(overlaps(c, k) for k in chosen))

    def take(c):
        if free(c):
            chosen.append(c)
            return True
        return False

    names = [t["name"] for t in hunt or []]
    for n, name in enumerate(names):
        later = [[c for c in ranked if u in c.get("topics", ()) and free(c)] for u in names[n + 1:]]
        mine = [c for c in ranked if name in c.get("topics", ()) and c not in chosen]
        fair = [c for c in mine if all(not left or any(not overlaps(c, x) for x in left)
                                       for left in later)]
        for c in fair or mine:
            if take(c):
                break
    for c in ranked:
        if c not in chosen:
            take(c)
    return sorted(chosen, key=lambda c: c["score"], reverse=True)


def cut_points(words, first, last, lead=0.15, tail=0.35):
    """Where the file is actually cut: a breath before the first word and a
    beat after the last, but never into the next word - a clip that ends on
    the first syllable of the next sentence sounds chopped."""
    start = max(0.0, words[first]["start"] - lead)
    end = words[last]["end"] + tail
    if last + 1 < len(words):
        end = min(end, words[last + 1]["start"] - 0.05)
    return round(start, 3), round(max(end, words[last]["end"]), 3)
