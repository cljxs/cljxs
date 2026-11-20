"""Where each story, argument and bit starts and ends - read by a model.

Owner, 2026-10-21: "these clips cut off in the middle of someone's story or
train of thought ... I need complete thoughts". The scorer (score.py) only
measures: a strong first line, a pause at the end, loudness. Nothing in it
can tell that a host is leading up to a story, or that the argument is not
over. So the transcript is read, a stretch at a time, by Sonnet on the
owner's Claude plan (wake.py ask - "clip" in tasks/engines.json; no cost
per read), which marks where each complete moment starts and ends, by
sentence number.

The model only marks. Code decides what is usable (check): the numbers
must be sentences of the stretch it was shown, the moment 20-90 seconds
(story_max_seconds), and the cut is still made on whole sentences, because
a mark is a pair of sentence numbers. Then (apply) a marked moment is a
candidate with a bonus for how good the model rated it, and a window that
cuts through a marked moment - starts or ends inside it without holding it
whole - is skipped, the way the avoid list is: that is the cut-off clip.

When the plan will not answer (no login, usage limit), the video is
clipped the way it was before, and the find event says so. Never falls
back to a paid model: the read is long, and this is a nice-to-have on top
of a scorer that works on its own.
"""

import json

from clipper import postcopy

READER = "story-v1"
AGENT = "clip"                 # its engine and model: tasks/engines.json
STRETCH = 15 * 60.0            # seconds of transcript in one question
OVERLAP = 3 * 60.0             # shown twice, so a moment on the edge is whole in one stretch
ASK_TIMEOUT = 240
PER_STRETCH = 8                # the most moments one stretch may give
KINDS = ("story", "argument", "hot take", "funny")
# Points on top of the scorer's: 10 + 2 per rating point, so a 9/10 story
# (+28) outranks a plain window that happens to score well, and a hunt
# topic inside it (+25, score.TOPIC_BONUS) still decides between stories.
BONUS_BASE, BONUS_PER = 10.0, 2.0

PROMPT = """You are the editor of a sports podcast's short clips (YouTube Shorts,
TikTok). Below is part of an episode's transcript, one numbered sentence a
line, with the time it starts. Mark the best moments to post, each as ONE
COMPLETE THOUGHT a viewer can follow with no other context:

- story: from where the speaker starts it - the lead-up ("yo, one time in
  Atlanta...", "let me tell you about...") - to its payoff, and the laugh or
  reaction right after.
- argument: the question or claim that starts it, both sides, and where it
  lands (someone wins, concedes, or it turns into a joke).
- hot take: the claim and the speaker's reasons, to the end of their point.
- funny: the setup, the punchline, and the laugh.

Never start a moment in the middle of a story or a point, and never end it
before the payoff. If a moment begins before this excerpt or runs past its
end, leave it out - it is shown whole in the next excerpt. Each moment is
FROM_S to TO_S seconds long. At most PER moments; fewer, better ones beat
filler; none is a fine answer.
WANTED
Reply with JSON only:
{"moments": [{"first": 12, "last": 31, "kind": "story", "rating": 8,
              "title": "Teague's first night in Atlanta", "why": "builds to the punchline"}]}
first/last: sentence numbers from the transcript. kind: story, argument,
hot take or funny. rating: 1-10, how likely it is to do well as a short.
title: 3-8 words, plain. why: one short line.

Show: SOURCE
Episode: TITLE

TRANSCRIPT"""


def clock(t):
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def stretches(sents, length=STRETCH, overlap=OVERLAP):
    """[(first, last)] sentence index ranges, each about `length` seconds,
    each starting `length - overlap` after the one before."""
    if not sents:
        return []
    out, t, end = [], sents[0]["start"], sents[-1]["end"]
    while True:
        idx = [k for k, s in enumerate(sents) if t <= s["start"] < t + length]
        if idx:
            out.append((idx[0], idx[-1]))
        if t + length >= end:
            return out
        t += length - overlap


def question(sents, first, last, cfg, source="", title="", hunt=None):
    wanted = ""
    if hunt:
        wanted = ("\nThe owner most wants moments about: " + "; ".join(t["name"] for t in hunt)
                  + ". Mark them when they come up, as complete thoughts too.\n")
    lines = "\n".join(f"{k} [{clock(sents[k]['start'])}] {sents[k]['text']}" for k in range(first, last + 1))
    return (PROMPT.replace("FROM_S", str(int(cfg["min_clip_seconds"])))
            .replace("TO_S", str(int(cfg["story_max_seconds"])))
            .replace("PER", str(PER_STRETCH)).replace("WANTED", wanted)
            .replace("SOURCE", source or "").replace("TITLE", title or "")
            .replace("TRANSCRIPT", lines))


def read_json(text):
    """The JSON object in a reply - the same reading as every other model
    reply here (emily-assets.py read_proof: only the braces are trusted)."""
    return postcopy._script("emily_assets_stories", "emily-assets.py").read_proof(text)


def check(doc, sents, first, last, cfg):
    """([usable marks], [why each other one was dropped]). A mark is
    {i, j, kind, rating, title, why} with i..j sentence indices."""
    good, dropped = [], []
    for m in (doc or {}).get("moments") or []:
        if not isinstance(m, dict):
            continue
        try:
            i, j, rating = int(m.get("first")), int(m.get("last")), int(m.get("rating") or 0)
        except (TypeError, ValueError):
            dropped.append("not sentence numbers")
            continue
        if not (first <= i <= j <= last):
            dropped.append(f"{i}-{j} is outside the excerpt ({first}-{last})")
            continue
        dur = sents[j]["end"] - sents[i]["start"]
        if not (cfg["min_clip_seconds"] <= dur <= cfg["story_max_seconds"]):
            dropped.append(f"{i}-{j} is {dur:.0f}s")
            continue
        kind = str(m.get("kind") or "").lower().strip()
        good.append({"i": i, "j": j, "kind": kind if kind in KINDS else "story",
                     "rating": max(1, min(10, rating or 5)),
                     "title": " ".join(str(m.get("title") or "").split())[:80],
                     "why": " ".join(str(m.get("why") or "").split())[:160]})
    return good, dropped


def overlaps(a, b):
    return a["i"] <= b["j"] and b["i"] <= a["j"]


def merge(marks):
    """One mark per moment: the same story seen in two overlapping stretches
    is kept once - the better rated, then the longer."""
    out = []
    for m in sorted(marks, key=lambda m: (m["rating"], m["j"] - m["i"]), reverse=True):
        if not any(overlaps(m, k) for k in out):
            out.append(m)
    return sorted(out, key=lambda m: m["i"])


def _ask(q):
    return postcopy._script("wake_clip", "wake.py").ask(AGENT, q, ASK_TIMEOUT)


def read(sents, cfg, source="", title="", hunt=None, ask=None, cache=None):
    """(marks, note). marks is None when the transcript could not be read;
    note says how it went, for the find event. `cache`, a Path, keeps the
    marks: a refind with the same sentences asks nothing again."""
    if not cfg.get("story_reading"):
        return None, "story reading is off"
    key = [len(sents), round(sents[0]["start"], 2), round(sents[-1]["end"], 2)] if sents else []
    if cache and cache.is_file():
        try:
            kept = json.loads(cache.read_text())
            if kept.get("key") == key and kept.get("reader") == READER:
                return kept["marks"], f"{len(kept['marks'])} complete moments marked (kept from before)"
        except (ValueError, KeyError):
            pass
    ask = ask or _ask
    marks, dropped, failed = [], [], []
    parts = stretches(sents)
    for first, last in parts:
        ok, text = ask(question(sents, first, last, cfg, source, title, hunt))
        if not ok:
            failed.append(text[:160])
            # The plan said no (a login, a limit): the next stretch will too.
            if len(failed) == 1 and not marks:
                return None, f"the transcript could not be read ({text[:160]}) - clipped by score alone"
            continue
        good, bad = check(read_json(text), sents, first, last, cfg)
        marks += good
        dropped += bad
    marks = merge(marks)
    note = (f"{len(marks)} complete moments marked in {len(parts)} stretch(es)"
            + (f"; {len(dropped)} marks dropped ({'; '.join(dropped[:3])})" if dropped else "")
            + (f"; {len(failed)} stretch(es) not read ({failed[0]})" if failed else ""))
    if cache and not failed:
        cache.write_text(json.dumps({"reader": READER, "key": key, "marks": marks}, indent=1))
    return marks, note


def cuts_into(c, marks):
    """The mark a window (i..j) cuts through - overlapping it without holding
    it whole - or None."""
    for m in marks:
        if overlaps(c, m) and not (c["i"] <= m["i"] and m["j"] <= c["j"]):
            return m
    return None


def apply(scored, marks):
    """Marks on the scorer's candidates, in place: a marked moment gets its
    bonus and says what it is; a window that cuts into one is skipped at 0
    with the reason, like an avoided word. Windows the avoid list skipped
    stay skipped."""
    by = {(m["i"], m["j"]): m for m in marks}
    for c in scored:
        if c.get("avoided"):
            continue
        m = by.get((c["i"], c["j"]))
        if m:
            c["score"] = min(100.0, round(c["score"] + BONUS_BASE + BONUS_PER * m["rating"], 1))
            c["reasons"] = [f"a complete {m['kind']} ({m['rating']}/10): {m['title'] or m['why']}"] + c["reasons"]
            c["story"] = m
            continue
        cut = cuts_into(c, marks)
        if cut:
            c["score"], c["topics"], c["laughs"] = 0.0, [], 0
            c["reasons"] = [f"skipped: cuts into a {cut['kind']} ({cut['title'] or 'marked'}, "
                            f"sentences {cut['i']}-{cut['j']})"]
    return scored
