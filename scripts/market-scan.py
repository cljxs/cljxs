#!/usr/bin/env python3
"""
market-scan.py — put the two halves together.

    market-scan.py phrase fall sticker      one phrase, both sources
    market-scan.py scan fall sticker        expand, then score the candidates
    market-scan.py scan laptop sticker --save   ...and file it for Scout

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
from datetime import datetime, timezone
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
SCANS = ROOT / "agents" / "scout" / "state" / "scans"
LOOSE = 0.5                       # below this share of matching titles,
                                  # a phrase is not comparable at all


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


def median_n(values):
    """(median, how many rows it was computed from).

    The count is not decoration. A scan of 'cozy fall sweater' reported
    favs/view 0.0195 and favs/day 0.000 for the same listings, which is
    impossible unless the two medians were taken over DIFFERENT rows - some
    row had a view count but no usable timestamp, so it counted towards one
    and not the other.

    `dropped` only ever caught a metric that was missing entirely. A median
    of three rows out of twenty-five looked exactly like a median of all
    twenty-five, which is the same defect one level down.
    """
    vals = [v for v in values if v is not None]
    return (statistics.median(vals) if vals else None), len(vals)


def measure(data, now=None, phrase=""):
    """Everything computable about one keyword's Etsy results, plus what was
    not computable and why."""
    rows = data.get("results") or []
    count = data.get("count")
    heat, heat_n = median_n(favs_per_day(r, now) for r in rows)
    pull_, pull_n = median_n(pull(r) for r in rows)
    price, price_n = median_n(price_of(r) for r in rows)
    age, _age_n = median_n(age_days(r, now) for r in rows)
    out = {
        "supply": count if isinstance(count, int) else None,
        "returned": len(rows),
        "heat": heat, "heat_n": heat_n,
        "pull": pull_, "pull_n": pull_n,
        "price": price, "price_n": price_n,
        "age": age,
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
        # KEYED BY STEM, not by the phrase. Etsy stems its own search:
        # 'cozy fall sweatshirt' and 'cozy fall sweatshirts' both came back
        # with exactly 137,321 listings, and crewneck/crewnecks with 71,176
        # and 71,177. Two of ten calls bought the same answer twice, and a
        # near-duplicate also takes a row in the ranking that a distinct
        # phrase could have had.
        key = frozenset(tp.stem(t) for t in re.findall(r"[a-z']+", w.lower()))
        if key and key not in seen:
            seen.add(key)
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

    # LOOSELY MATCHED PHRASES COME OUT OF THE RANKING ENTIRELY.
    #
    # 'nail sticker japan' ranked third on 273 listings and the best
    # favourites-per-view in the table - with 4% match. One of the
    # twenty-five listings Etsy returned actually contained the phrase; the
    # rest were generic nail stickers it fell back to. Its numbers describe
    # a market nobody asked about.
    #
    # The warning about it printed BELOW the table, while the row itself sat
    # near the top. Anyone reading a ranking reads the top of it. A number
    # that cannot be compared must not be sorted alongside numbers that can.
    loose = [(c, m) for c, m in usable
             if m.get("match") is not None and m["match"] < LOOSE]
    ranked = [cm for cm in usable if cm not in loose]
    if not ranked:
        print(f"\n  Every phrase came back loosely matched - Etsy returned "
              f"listings that do\n  not contain the phrase asked about. "
              f"There is nothing here to rank.",
              file=sys.stderr)
        ranked, loose = usable, []

    print(f"\n  RANKED - most demand per unit of competition first\n")
    # The match column is ALWAYS shown, never only when it trips a
    # threshold. A warning that appears below 50% and is silent above it
    # cannot be told apart from a warning that is broken - the absence means
    # 'all fine' and 'the check never ran' equally, and the reader cannot
    # distinguish them. A number every row is unambiguous.
    print(f"  {'phrase':<32}{'supply':>9}{'favs/day':>10}{'n':>4}"
          f"{'favs/view':>11}{'n':>4}{'match':>7}{'score':>9}")
    for cand, m in ranked:
        match = m.get("match")
        shown = f"{match * 100:>5.0f}%" if match is not None else "    ?"
        print(f"  {cand[:31]:<32}{m['supply']:>9,}{m['heat']:>10.3f}"
              f"{m['heat_n']:>4}"
              f"{(m['pull'] if m['pull'] is not None else 0):>11.4f}"
              f"{m['pull_n']:>4}{shown:>7}{opportunity(m):>9.4f}")

    thin = [(c, m) for c, m in ranked
            if min(m["heat_n"], m["pull_n"]) < m["returned"]]
    if thin:
        # No count in this sentence. It used to say 'of the 25 returned
        # listings', taken from the FIRST row - and 'nail sticker company'
        # returned 17, not 25. The per-row numbers below carry it correctly;
        # the header was the only thing generalising from one phrase.
        print(f"\n  The n columns are how many of the listings Etsy returned "
              f"each median\n  was actually computed from. Where they differ, "
              f"the two numbers\n  describe different listings and should not "
              f"be read against each other:")
        for c, m in thin:
            print(f"      {c:<32}favs/day from {m['heat_n']}, "
                  f"favs/view from {m['pull_n']}, of {m['returned']}")

    best, bm = ranked[0]
    dead = [(c, m) for c, m in ranked if opportunity(m) <= 0]
    alive = [(c, m) for c, m in ranked if opportunity(m) > 0]

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

    if loose:
        print(f"\n  LEFT OUT OF THE RANKING ({len(loose)}) - fewer than half "
              f"the listings Etsy\n  returned contain the phrase at all, so "
              f"their numbers describe some\n  other market and cannot be "
              f"compared with the rows above:")
        for c, m in loose:
            print(f"      {c:<32}{m['match'] * 100:>3.0f}% match, "
                  f"{m['supply']:>8,} listings, would have scored "
                  f"{opportunity(m):.4f}")
    else:
        print(f"\n  match is the share of returned listings whose title "
              f"really contains\n  the phrase. All {len(ranked)} are at or "
              f"above {LOOSE:.0%}, so every supply count\n  above is the "
              f"market that was actually asked about.")

    if "--save" in sys.argv:
        save_scan(phrase, ranked, loose)

    print(f"\n  A comparison between "
          f"{'these ' + str(len(ranked)) + ' phrases' if len(ranked) > 1 else 'one phrase'}"
          f" and nothing more.\n  It is not a sales forecast.")
    skipped = len(scored) - len(usable)
    if skipped:
        print(f"\n  {skipped} phrase(s) left out of the ranking: a field the "
              f"score needs was\n  missing or malformed, and none was "
              f"guessed at.")
    return 0


def slug(phrase):
    return re.sub(r"[^a-z0-9]+", "-", (phrase or "").lower()).strip("-") or "scan"


def save_scan(seed, ranked, loose):
    """Write the ranking where Scout can read it.

    THE POINT OF THIS FILE. Scout proposes ideas out of the model's own head
    today, and an agent asked 'what is selling' will produce a confident,
    detailed, plausible answer that is fiction. Fiction with numbers on it
    gets acted on.

    So Scout never types a supply figure. It names a phrase, and
    scout-ideas.py copies the numbers out of here. A number that is never
    typed cannot be invented - the same reason ace-judge.py and
    emily-listing.py exist.

    Loosely matched phrases are saved too, in their own list, so a proposal
    naming one can be refused with the reason rather than silently missed.
    """
    SCANS.mkdir(parents=True, exist_ok=True)
    doc = {
        "seed": seed,
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": [{"phrase": c, "supply": m["supply"], "heat": m["heat"],
                  "pull": m["pull"], "price": m["price"], "match": m["match"],
                  "returned": m["returned"], "heat_n": m["heat_n"],
                  "pull_n": m["pull_n"], "score": opportunity(m)}
                 for c, m in ranked],
        "excluded": [{"phrase": c, "match": m["match"], "supply": m["supply"],
                      "why": "fewer than half the listings Etsy returned "
                             "contain this phrase"}
                     for c, m in loose],
    }
    path = SCANS / f"{slug(seed)}.json"
    path.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"\n  saved {len(doc['rows'])} measured phrase(s) to "
          f"agents/scout/state/scans/{path.name}")
    print(f"  Scout can now propose any of them by name, and nothing else.")
    return path


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("phrase", "scan") or len(sys.argv) < 3:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    key, why = ep.api_key()
    if not key:
        print(why, file=sys.stderr)
        return 2
    words = [a for a in sys.argv[2:] if not a.startswith("--")]
    return (cmd_phrase if cmd == "phrase" else cmd_scan)(key, words)


if __name__ == "__main__":
    sys.exit(main())
