"""The Phase 1 scorer: features a script can compute exactly, weighted.

It is a baseline, not the product. It cannot tell a funny story from a dull
one - that needs a language model (Phase 2). What it CAN do reliably is
reject what no model should be paid to look at: clips that open on "and so
he", that stop mid-thought, that mumble, that run long. So in Phase 2 it
becomes the pre-filter that decides which few windows reach the model.

Every feature is kept on the candidate row, 0..1, with the weights and the
scorer version. That is what makes the weights learnable in Phase 5 - a score
without its parts can only be trusted or not.
"""

import re
import statistics

SCORER = "heuristic-v1"

WEIGHTS = {
    "hook": 0.22,        # does the first sentence make you stay?
    "standalone": 0.18,  # does it start without needing what came before?
    "ending": 0.14,      # does it land, or stop?
    "energy": 0.14,      # louder than this video usually is - laughter, raised voices
    "emotion": 0.10,     # exclamations, strong words
    "info": 0.08,        # numbers, reasons, how-to
    "pace": 0.08,        # neither mumbled nor rushed
    "length": 0.06,      # near the target length
}

# A clip opening on one of these is answering something the viewer never heard.
DANGLING = {"and", "but", "so", "because", "which", "that", "it", "this", "they", "he",
            "she", "him", "her", "them", "also", "then", "or", "plus", "anyway",
            "those", "these", "there", "who", "where",
            # Seen opening the unpunctuated runs of a real podcast mid-story:
            # "like in the first quarter ... they started chanting".
            "like"}
HOOK_PHRASES = ("you won't believe", "the truth", "nobody", "no one", "never", "secret",
                "the problem", "the reason", "here's", "here is", "what if", "the worst",
                "the best", "i was wrong", "stop", "biggest", "mistake", "why ", "how ",
                "the first time", "let me tell you", "i can't believe", "crazy", "insane")
STRONG = ("amazing", "insane", "crazy", "terrible", "incredible", "love", "hate", "shocked",
          "unbelievable", "wow", "oh my god", "no way", "hilarious", "furious", "scared",
          "haha", "[laughter]", "(laughs)")
INFO = ("because", "the reason", "how to", "step", "percent", "%", "$", "study", "data",
        "first", "second", "third", "tip", "mistake", "learned")
NUMBER = re.compile(r"\d")


def _clamp(x):
    return max(0.0, min(1.0, x))


def first_word(text):
    m = re.match(r"\W*([A-Za-z']+)", text)
    return m.group(1).lower() if m else ""


def features(sents, i, j, words, loud, video_loud_median, cfg):
    first, last = sents[i], sents[j]
    text = " ".join(s["text"] for s in sents[i:j + 1])
    low = text.lower()
    opener = first["text"].lower()
    n_words = last["last"] - first["first"] + 1
    dur = last["end"] - first["start"]

    hook = 0.0
    if opener.rstrip(" \"'").endswith("?"):
        hook += 0.6
    if any(p in opener for p in HOOK_PHRASES):
        hook += 0.5
    if len(opener.split()) <= 12:
        hook += 0.2
    if NUMBER.search(opener):
        hook += 0.2

    standalone = 0.25 if first_word(first["text"]) in DANGLING else 1.0

    ending = 0.6 if last["text"].rstrip(" \"'”’)").endswith(tuple(".?!")) else 0.2
    if last.get("pause_after") is None or last["pause_after"] >= 0.6:
        ending += 0.4

    energy = 0.0
    if loud:
        secs = loud[int(first["start"]):int(last["end"]) + 1]
        if secs:
            peak = sorted(secs)[int(0.9 * (len(secs) - 1))]      # the clip's loud end, not one spike
            energy = _clamp((peak - video_loud_median) / 10.0)   # +10 dB over typical = full marks

    per100 = 100.0 / max(n_words, 1)
    emotion = _clamp((text.count("!") * 2 + sum(low.count(s) for s in STRONG) * 3) * per100 / 6)
    info = _clamp((len(NUMBER.findall(text)) + sum(low.count(s) for s in INFO) * 2) * per100 / 8)

    wps = n_words / max(dur, 0.1)
    pace = 1.0 if 2.2 <= wps <= 3.8 else _clamp(1 - min(abs(wps - 2.2), abs(wps - 3.8)) / 1.5)

    target = float(cfg["target_clip_seconds"])
    length = _clamp(1 - abs(dur - target) / target)

    return {"hook": _clamp(hook), "standalone": standalone, "ending": _clamp(ending),
            "energy": energy, "emotion": emotion, "info": info, "pace": pace, "length": length}


def total(feats, weights=WEIGHTS):
    return round(100 * sum(weights[k] * feats[k] for k in weights) / sum(weights.values()), 1)


REASONS = {
    "hook": "strong opening line",
    "standalone": "makes sense without the video before it",
    "ending": "ends cleanly on a pause",
    "energy": "louder than the rest of the video (laughter, raised voices)",
    "emotion": "emotional language",
    "info": "concrete information (numbers, reasons, steps)",
    "pace": "comfortable speaking pace",
    "length": "close to the target length",
}
WARNINGS = {
    "standalone": "opens mid-thought (\"and\", \"so\", \"he\" ...)",
    "ending": "may stop before the thought finishes",
    "pace": "very slow or very fast speech",
}


def reasons(feats, weights=WEIGHTS):
    """Plain English, strongest contributions first, then anything worrying."""
    ranked = sorted(weights, key=lambda k: weights[k] * feats[k], reverse=True)
    out = [REASONS[k] for k in ranked if feats[k] >= 0.6][:3]
    out += ["warning: " + WARNINGS[k] for k in WARNINGS if feats[k] < 0.4]
    return out


# ------------------------------------------------------------------ a campaign's list

# Points added to a window that talks about something on the campaign's list.
# The list is the rights holder saying which moments perform ("the Knicks clip
# pulled 4.2% engagement, start there") - better evidence than any feature
# here - so it outweighs them, but a mumbled, cut-off mention still loses to
# a clean one, because the rest of the score still counts.
TOPIC_BONUS = 25.0
# A topic must come up in the clip's first half: a clip picked for a topic
# should be about it, not end on its setup. At 0.75, on the Trae Young
# episode, the "Knicks" pick ended a few seconds after "my first playoff
# series was the Knicks" - before the chant it was about - on both the
# one-piece and the chunked transcript; at 0.5 both keep the chant.
TOPIC_BY = 0.5


def parse_hunt(text):
    """'Knicks: knicks, chant; Pat Beverley: beverley, beverly; dunk' ->
    [{"name": "Knicks", "terms": ["knicks", "chant"]}, ...]. Topics are split
    by ';', terms by ','; a topic without a 'Name:' is named by its first
    term. Order is kept: it is the campaign's priority."""
    out = []
    for part in (text or "").split(";"):
        name, sep, terms = part.partition(":")
        terms = [t.strip().lower() for t in (terms if sep else name).split(",") if t.strip()]
        if terms:
            out.append({"name": name.strip() if sep else terms[0], "terms": terms})
    return out


# Never a clip: a sponsor read. Podcasts break for them mid-show - on a sports
# show often a betting app - and a clip of one is an ad nobody is paying you
# for, which TikTok may also restrict. Phrases an ad read says and a
# conversation does not, so a guest mentioning FanDuel's odds is not skipped.
AD_READ = ("promo code", "use code", "use my code", "sponsored by", "brought to you by",
           "today's sponsor", "our sponsor", "first deposit", "deposit match",
           "terms and conditions apply", "gambling problem", "1-800-gambler", "must be 21",
           "21 and older", "21+ only", "download the app today", "link in the description",
           "check them out at")


def parse_avoid(text):
    """'politics, betting odds, ex-wife' -> ["politics", "betting odds", "ex-wife"]:
    terms a source's clips stay away from, as the owner writes them."""
    return [t.strip().lower() for t in re.split(r"[,;]", text or "") if t.strip()]


def avoided(text, avoid=()):
    """The first sponsor phrase or avoided term a piece of transcript says, or
    None. Whole words, as topics() matches them."""
    low = text.lower()
    for term in list(AD_READ) + list(avoid or ()):
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?:'s|s'|s|es|ed|ing|er|')?(?![a-z0-9])", low):
            return term
    return None


def topics(text, hunt):
    """Names of the topics a piece of transcript mentions. Whole words only,
    plurals and possessives allowed: 'Knicks' matches "the Knicks'", 'dunk'
    matches "dunks", 'ai' never matches "said"."""
    low = text.lower()
    hit = []
    for t in hunt or []:
        for term in t["terms"]:
            if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?:'s|s'|s|es|ed|ing|er|')?(?![a-z0-9])", low):
                hit.append(t["name"])
                break
    return hit


def score_windows(sents, wins, words, loud, cfg, hunt=None, avoid=()):
    median = statistics.median([v for v in loud if v > -70]) if any(v > -70 for v in loud) else -30.0
    out = []
    for i, j in wins:
        f = features(sents, i, j, words, loud, median, cfg)
        text = " ".join(s["text"] for s in sents[i:j + 1])
        score, why = total(f), reasons(f)
        by = sents[i]["start"] + TOPIC_BY * (sents[j]["end"] - sents[i]["start"])
        early = "".join(words[k]["word"] for k in range(sents[i]["first"], sents[j]["last"] + 1)
                        if words[k]["start"] < by) if hunt else ""
        on_list = topics(early, hunt)
        if on_list:
            score = min(100.0, round(score + TOPIC_BONUS, 1))
            why = [f"on the campaign's list: {', '.join(on_list)}"] + why
        # Anywhere in the window, not only its first half: a clip that ends
        # on "use code GIL" is an ad as much as one that starts on it. Kept
        # as a candidate at 0, with the reason, so the studio can say why.
        bad = avoided(text, avoid)
        if bad:
            score, on_list = 0.0, []
            why = [f"skipped: says {bad!r}" + (" (a sponsor read)" if bad in AD_READ else " (on the avoid list)")]
        out.append({"i": i, "j": j, "start": sents[i]["start"], "end": sents[j]["end"],
                    "text": text, "score": score, "features": f, "reasons": why,
                    "topics": on_list})
    return out
