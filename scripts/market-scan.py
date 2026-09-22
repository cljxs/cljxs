#!/usr/bin/env python3
"""
market-scan.py — put the two halves together.

    market-scan.py phrase fall sticker      one phrase, both sources
    market-scan.py scan fall sticker        expand, then score the candidates

trend-probe.py says what people SEARCH for. etsy-probe.py says what is
already SOLD. The number that matters is neither one:

    'fall sticker'  109,099 active listings, and its top results have
                    1, 14 and 0 favourites

Enormous supply meeting demand that has already been served. Demand alone
would have called that phrase a winner. Supply alone would have called it a
dead end without noticing that people are hunting it in nine different
shops. You need both.

NO INVENTED THRESHOLDS. There is no rule here that says 'under 2,000
listings is good', because nothing here knows that. One phrase on its own
has nothing to compare against, and this says so rather than dressing a
guess up as a verdict. `scan` ranks candidates AGAINST EACH OTHER, which
needs no absolute number and is the only honest comparison available.

EVERY FIELD IS CHECKED BEFORE IT IS USED. Etsy returns 59 fields and this
reads four of them. A timestamp in milliseconds, a null view count, a
missing divisor - each would produce a confident, wrong number rather than
an error. Anything that fails its check is dropped from the score and named
in the output, because a score that quietly lost a component is worse than
no score.

Standard library only.
"""

import importlib.util
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Both probes, imported. The rate-limit handling, the credential parsing, the
# intent labels and the trademark list all live in one place each.
ep = _load("etsy_probe", "etsy-probe.py")
tp = _load("trend_probe", "trend-probe.py")

# Etsy launched in 2005. A creation timestamp outside this window is not a
# creation timestamp - most likely milliseconds, which would make every
# listing about three weeks old and every favs/day figure enormous.
EPOCH_FLOOR = 1104537600          # 2005-01-01
CANDIDATES = 12                   # Etsy calls per scan. 5 QPS, 5000 a day.


def age_days(row, now=None):
    """How long this listing has been up, or None if the field cannot be
    trusted."""
    ts = row.get("original_creation_timestamp") or row.get("creation_timestamp")
    if not isinstance(ts, (int, float)):
        return None
    now = now if now is not None else time.time()
    if not (EPOCH_FLOOR <= ts <= now + 86400):
        return None
    return max((now - ts) / 86400.0, 1.0)


def favs_per_day(row, now=None):
    """The demand signal that is a RATE.

    A five-year-old listing with 100 favourites is not doing better than a
    two-month-old one with 60; it has had thirty times as long. Raw
    num_favorers rewards age, which on Etsy means rewarding whoever got there
    first - the opposite of what a researcher looking for a way in wants.
    """
    favs = row.get("num_favorers")
    days = age_days(row, now)
    if days is None or not isinstance(favs, int) or isinstance(favs, bool) \
            or favs < 0:
        return None
    return favs / days


def pull(row):
    """Favourites per view: of the people who looked, how many saved it.

    Independent of age and of how much traffic Etsy sent, so it measures the
    LISTING - the photo, the title, the price - rather than its luck.
    """
    views, favs = row.get("views"), row.get("num_favorers")
    for v in (views, favs):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return None
    return (favs / views) if views else None


def price_of(row):
    """Etsy sends price as {amount, divisor, currency_code}, in minor units."""
    p = row.get("price")
    if not isinstance(p, dict):
        return None
    amount, divisor = p.get("amount"), p.get("divisor")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    if not isinstance(divisor, (int, float)) or not divisor:
        return None
    return amount / divisor


STOPWORDS = {"for", "the", "a", "an", "and", "of", "with", "in", "to"}


def title_match(rows, phrase):
    """What fraction of the returned listings actually contain the phrase.

    A low supply count has two completely different meanings and the number
    alone cannot tell them apart:

      - a thin market, which is the opportunity we are hunting, or
      - a phrase nobody uses, so Etsy matched loosely and the count is the
        size of some OTHER market.

    'fall sticker emojis' returned 110 listings. That is either the best
    find in the sweep or an artefact of an odd phrasing, and treating it as
    the first without checking is how a scorer produces a confident
    recommendation for a market that does not exist.
    """
    want = {tp.stem(w) for w in re.findall(r"[a-z']+", (phrase or "").lower())
            if w not in STOPWORDS}
    if not want or not rows:
        return None
    hits = 0
    for r in rows:
        title = str(r.get("title") or "").lower()
        have = {tp.stem(w) for w in re.findall(r"[a-z']+", title)}
        if want <= have:
            hits += 1
    return hits / len(rows)


def median(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def measure(data, now=None, phrase=""):
    """Everything computable about one keyword's Etsy results, plus what was
    not computable and why."""
    rows = data.get("results") or []
    count = data.get("count")
    out = {
        "supply": count if isinstance(count, int) else None,
        "returned": len(rows),
        "heat": median(favs_per_day(r, now) for r in rows),
        "pull": median(pull(r) for r in rows),
        "price": median(price_of(r) for r in rows),
        "age": median(age_days(r, now) for r in rows),
        "match": title_match(rows, phrase),
        "dropped": [],
    }
    checks = [("supply", "count"), ("heat", "original_creation_timestamp "
               "or num_favorers"), ("pull", "views"), ("price", "price")]
    for key, field in checks:
        if out[key] is None:
            out[key] = None
            out["dropped"].append(field)
    return out


def opportunity(m):
    """Demand per decade of competition, or None if either half is missing.

    log10 because supply spans four orders of magnitude between 'fall
    sticker' and anything specific, and a linear divisor would make every
    narrow phrase win by arithmetic alone.
    """
    if m.get("heat") is None or not m.get("supply"):
        return None
    return m["heat"] / math.log10(max(m["supply"], 10))


def fetch(key, phrase):
    """(measurements, error) for one keyword."""
    import urllib.parse
    q = urllib.parse.urlencode({"keywords": phrase, "limit": 25,
                                "sort_on": "score"})
    data, headers, err = ep.call(f"/listings/active?{q}", key)
    if err:
        return None, err
    return measure(data, phrase=phrase), None


def show(phrase, m):
    def num(v, fmt, unit=""):
        return f"{format(v, fmt)}{unit}" if v is not None else "not available"
    print(f"\n  {phrase}")
    print(f"    supply       {num(m['supply'], ',d', ' active listings')}")
    print(f"    heat         {num(m['heat'], '.3f', ' favourites/day')}"
          f"   (median of the top {m['returned']})")
    print(f"    pull         {num(m['pull'], '.4f', ' favourites/view')}")
    print(f"    typical age  {num(m['age'], '.0f', ' days')}")
    print(f"    typical price{num(m['price'], '.2f', ' USD'):>9}")
    opp = opportunity(m)
    print(f"    opportunity  {num(opp, '.4f')}"
          f"   (heat per decade of competition)")
    if m["dropped"]:
        print(f"    DROPPED from the score: {', '.join(sorted(set(m['dropped'])))}"
              f"\n      - the field was missing or not the shape it should be, "
              f"so nothing\n        was guessed in its place.")


def cmd_phrase(key, words):
    phrase = " ".join(words)
    tier, what, why = tp.ipc.risky(phrase)
    if tier == "blocked":
        print(f"  BLOCKED: {what} - {why}", file=sys.stderr)
        return 2
    m, err = fetch(key, phrase)
    if err:
        print(f"failed - {err}", file=sys.stderr)
        return 1
    show(phrase, m)
    got, rel, serr = tp.suggest(phrase)
    if serr:
        print(f"\n    (Google side unavailable: {serr})")
    else:
        buyers = [w for w in got if tp.intent_of(w)[0] is None]
        print(f"\n    Google completes it {len(got)} ways, {len(buyers)} of "
              f"them by someone\n    trying to buy a physical thing.")
    print(f"\n  One phrase on its own has nothing to compare against. These "
          f"numbers mean\n  something next to other phrases' numbers, which "
          f"is what `scan` is for.")
    return 0


def pick(phrase, found):
    """Which of an expansion's results are worth an Etsy call.

    Everything hunted in more than one shop - someone guessed at a stockist,
    which is as close to proven intent as this gets - then the best-ranked
    buying phrases to fill the budget.
    """
    hunted = tp.retail_demand(found)
    first = [p for p, shops in sorted(hunted.items(), key=lambda kv: -len(kv[1]))
             if len(shops) > 1]
    rest = [w for w, _rank in sorted(found.items(), key=lambda kv: kv[1])
            if tp.bucket(phrase, w)[0] == "buying"]
    # ONE dedup, here. The earlier version also filtered `rest` against
    # `first`, which made this loop's `seen` unreachable - a second opinion
    # that could never disagree, and one no test could fail on.
    out, seen = [], set()
    for w in first + rest:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:CANDIDATES]


def cmd_scan(key, words):
    phrase = " ".join(words)
    print(f"expanding '{phrase}'...", flush=True)
    found, errors = tp.expand(phrase)
    if not found:
        print("nothing came back from Suggest.", file=sys.stderr)
        return 1
    if errors:
        print(f"  ({len(errors)} of 27 Suggest queries failed)")
    candidates = pick(phrase, found)
    print(f"{len(found)} searches found; asking Etsy about {len(candidates)} "
          f"of them.\n", flush=True)

    scored = []
    for i, cand in enumerate(candidates, 1):
        m, err = fetch(key, cand)
        print(f"  {i:>2}/{len(candidates)}  {cand}"
              f"{'  -> ' + err if err else ''}", flush=True)
        if m:
            scored.append((cand, m))
        time.sleep(0.25)               # 5 QPS is the published limit

    usable = [(c, m) for c, m in scored if opportunity(m) is not None]
    if not usable:
        print("\nNothing scorable came back - every result was missing a "
              "field the score\nneeds. Nothing is being guessed in their "
              "place.", file=sys.stderr)
        return 1
    usable.sort(key=lambda cm: -opportunity(cm[1]))

    print(f"\n  RANKED - most demand per unit of competition first\n")
    print(f"  {'phrase':<34}{'supply':>10}{'favs/day':>11}"
          f"{'favs/view':>11}{'score':>9}")
    for cand, m in usable:
        print(f"  {cand[:33]:<34}{m['supply']:>10,}{m['heat']:>11.3f}"
              f"{(m['pull'] if m['pull'] is not None else 0):>11.4f}"
              f"{opportunity(m):>9.4f}")

    best, bm = usable[0]
    dead = [(c, m) for c, m in usable if opportunity(m) <= 0]
    alive = [(c, m) for c, m in usable if opportunity(m) > 0]

    # The first version divided by max(score, 1e-9) and printed
    # '19508025.0x' when the bottom phrase scored exactly zero. A ratio
    # against zero is not a large number, it is not a number - and printed
    # to one decimal place it reads like a measurement.
    if len(alive) >= 2:
        worst, wm = alive[-1]
        print(f"\n  '{best}' has {opportunity(bm) / opportunity(wm):.1f}x the "
              f"demand per unit of\n  competition that '{worst}' does - the "
              f"lowest phrase here that scored at all.")
    else:
        print(f"\n  Only {len(alive)} phrase scored above zero, so there is no "
              f"ratio to report.")

    if dead:
        print(f"\n  SCORED ZERO ({len(dead)}) - not one favourite a day across "
              f"the top 25:")
        for c, m in dead:
            print(f"      {c:<34}{m['supply']:>10,} listings")
        print(f"  That is a finding, not a gap. People are listing into these "
              f"phrases\n  and nobody is saving the results.")

    loose = [(c, m) for c, m in usable
             if m.get("match") is not None and m["match"] < 0.5]
    if loose:
        print(f"\n  READ THE SUPPLY COLUMN CAREFULLY for these - fewer than "
              f"half the\n  listings Etsy returned actually contain the "
              f"phrase, so the count is\n  the size of a looser market than "
              f"the one asked about:")
        for c, m in loose:
            print(f"      {c:<34}{m['match'] * 100:>3.0f}% of results match")

    print(f"\n  A comparison between these {len(usable)} phrases and nothing "
          f"more.\n  It is not a sales forecast.")
    skipped = len(scored) - len(usable)
    if skipped:
        print(f"\n  {skipped} phrase(s) left out of the ranking: a field the "
              f"score needs was\n  missing or malformed, and none was "
              f"guessed at.")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("phrase", "scan") or len(sys.argv) < 3:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    key, why = ep.api_key()
    if not key:
        print(why, file=sys.stderr)
        return 2
    return (cmd_phrase if cmd == "phrase" else cmd_scan)(key, sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
