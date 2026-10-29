#!/usr/bin/env python3
"""
ace-study.py - what Ace's own record says about where profit could come
from: a read-only report, the first step toward a second strategy to run
beside him.

    python3 scripts/ace-study.py            the report, in words
    python3 scripts/ace-study.py --json     the same numbers

Owner, 2026-10-29: "find another strategy to run alongside ace to compare
them. What counts as better is more profit." This reads his records - it
writes nothing, wakes no model, moves no money - and answers, in order:

  1. What there is: bets, blind estimates, prices, results, and how far
     back each reaches (price history is kept 30 days, results 40).
  2. His profit, honestly: the range it could have landed in by luck, and
     what the closing prices say his bets were worth - profit = that + luck.
  3. His forecasts, on every game he estimated blind (not just his bets):
     accuracy against the market, whether his chances are squeezed toward
     50-50, and the one test that matters most - does his number carry
     anything the market's does not (a logistic regression; its weight on
     his disagreement is also the pull toward the market the data supports).
  4. Where the money went: his bets by sport, favourite or underdog, edge,
     price, timing and the way the price moved after.
  5. The no-AI bettors on the same games (ace-baselines.py).
  6. Candidate rules replayed on every game he estimated, at the price when
     he estimated. These are IDEAS found on this data: a rule chosen because
     it did well here has to prove itself on games after today.

Every price, close and result is read through the scripts that own them
(ace-book, ace-clv, ace-judge, ace-baselines). Standard library only.
"""

import importlib.util
import json
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
STATE = ROOT / "agents" / "ace" / "state"

STAKE = 100.0
BOOT = 2000                    # resamples for each range
SEED = 20261029                # the same ranges every run
MIN_FIT = 30                   # games before the regression is run
CAL_BINS = ((0, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 101))
FAR_APART_PTS = 30             # a blind estimate this far from the market is listed by name


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mods():
    book = _load("ace_book_study", "ace-book.py")
    clv = _load("ace_clv_study", "ace-clv.py")
    judge = _load("ace_judge_study", "ace-judge.py")
    base = _load("ace_baselines_study", "ace-baselines.py")
    for m in (book, clv):
        m.STATE = STATE
    book.BETS, book.BANK, book.RESULTS = STATE / "bets.jsonl", STATE / "bankroll.json", STATE / "results.json"
    clv.LINES, clv.JOURNAL = STATE / "lines.json", STATE / "judgements.jsonl"
    return book, clv, judge, base


# ------------------------------------------------------------------ statistics

def ci(values, stat=lambda v: sum(v) / len(v), n=BOOT):
    """(low, high): the middle 90% of `stat` over resamples of `values`."""
    if len(values) < 2:
        return None
    rng = random.Random(SEED)
    got = sorted(stat([values[rng.randrange(len(values))] for _ in values]) for _ in range(n))
    return round(got[int(0.05 * n)], 2), round(got[int(0.95 * n) - 1], 2)


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def logistic(X, y, iters=50):
    """Coefficients and standard errors of a logistic regression, by
    Newton's method - small and exact enough for three columns."""
    k = len(X[0])
    b = [0.0] * k
    for _ in range(iters):
        H = [[0.0] * k for _ in range(k)]
        g = [0.0] * k
        for xi, yi in zip(X, y):
            p = 1 / (1 + math.exp(-sum(bj * xj for bj, xj in zip(b, xi))))
            w = p * (1 - p)
            for a in range(k):
                g[a] += (yi - p) * xi[a]
                for c in range(k):
                    H[a][c] += w * xi[a] * xi[c]
        step = solve(H, g)
        if step is None:
            return None
        b = [bj + sj for bj, sj in zip(b, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    inv = invert(H)
    if inv is None:
        return None
    return b, [math.sqrt(max(inv[a][a], 0)) for a in range(k)]


def solve(A, v):
    inv = invert(A)
    return None if inv is None else [sum(inv[i][j] * v[j] for j in range(len(v))) for i in range(len(v))]


def invert(A):
    n = len(A)
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            return None
        M[c], M[piv] = M[piv], M[c]
        d = M[c][c]
        M[c] = [x / d for x in M[c]]
        for r in range(n):
            if r != c:
                f = M[r][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [row[n:] for row in M]


# ------------------------------------------------------------------ 1-2: data and profit

def bets_of(book, clv, lines):
    """His settled game bets (never Ace Props), each with its close and the
    profit the closing price says it was worth."""
    out = []
    for b in book.fold(book.events())["settled"]:
        if book.is_props(b) or b.get("result") not in ("won", "lost"):
            continue
        entry = lines.get(clv.game_key(b.get("sport"), b.get("match"), b.get("starts_utc")))
        at = clv.parse_start(str(b.get("utc") or "").replace(" ", "T") + "Z")
        close = clv.closing(entry, b.get("side"), "draftkings", at)[0] if entry else None
        sharp = clv.sharp_close(entry, b.get("side"), at) if entry else None
        c = clv.clv_at(b.get("price"), sharp if sharp is not None else close)
        start = clv.parse_start(b.get("starts_utc"))
        q = b.get("sharp_pct") if b.get("sharp_pct") is not None else b.get("novig_pct")
        out.append(dict(b, clv_pct=c, worth=round(STAKE * c / 100, 2) if c is not None else None,
                        market_pct=q, close_pct=sharp if sharp is not None else close,
                        edge_pts=(round(float(b["p_used"]) - float(q), 1)
                                  if b.get("p_used") is not None and q is not None else None),
                        hours_before=(round((start - at).total_seconds() / 3600, 1) if start and at else None)))
    return sorted(out, key=lambda b: str(b.get("utc")))


def profit_section(bets):
    if not bets:
        return {"bets": 0}
    profit = [float(b["profit"]) for b in bets]
    worth = [b["worth"] for b in bets if b["worth"] is not None]
    graded = [b for b in bets if b["worth"] is not None]
    return {"bets": len(bets), "profit": round(sum(profit), 2),
            "roi_pct": round(sum(profit) / (STAKE * len(bets)) * 100, 2),
            "roi_range_pct": ci([p / STAKE * 100 for p in profit]),
            "graded": len(graded), "worth": round(sum(worth), 2) if worth else None,
            "luck": round(sum(float(b["profit"]) for b in graded) - sum(worth), 2) if worth else None,
            "avg_clv_pct": round(sum(b["clv_pct"] for b in graded) / len(graded), 2) if graded else None,
            "first": str(bets[0].get("utc"))[:10], "last": str(bets[-1].get("utc"))[:10]}


# ------------------------------------------------------------------ 3: forecasts

def forecast_section(rows):
    paired = [r for r in rows if r["then"] is not None]
    out = {"games": len(rows), "paired": len(paired)}
    if not paired:
        return out
    brier = lambda key: round(sum(((r["blind"] if key == "blind" else r[key] / 100) - r["y"]) ** 2
                                  for r in paired) / len(paired), 4)
    out.update(brier_ace=brier("blind"), brier_market=brier("then"))
    # Squeezed toward 50-50? How far his chances sit from even, against the
    # market's on the same games.
    out["ace_from_even_pts"] = round(sum(abs(r["blind"] * 100 - 50) for r in paired) / len(paired), 1)
    out["market_from_even_pts"] = round(sum(abs(r["then"] - 50) for r in paired) / len(paired), 1)
    out["calibration"] = []
    for lo, hi in CAL_BINS:
        b = [r for r in paired if lo <= r["blind"] * 100 < hi]
        if b:
            out["calibration"].append({"bin": f"{lo}-{min(hi, 100)}%", "n": len(b),
                                       "ace_said": round(sum(r["blind"] for r in b) / len(b) * 100, 1),
                                       "market_said": round(sum(r["then"] for r in b) / len(b), 1),
                                       "happened": round(sum(r["y"] for r in b) / len(b) * 100, 1)})
    # Games where he and the market were miles apart, by name - a home and
    # away read the wrong way round looks exactly like this (2026-10-29: three
    # games where he said ~20% and the market ~66%).
    out["far_apart"] = [{"game": str(r.get("key") or "").split("|")[1] if "|" in str(r.get("key") or "") else r.get("key"),
                         "sport": r.get("sport"), "starts_utc": r.get("starts_utc"), "estimated_utc": r.get("utc"),
                         "ace_home": round(r["blind"] * 100, 1), "market_home": r["then"],
                         "winner": "home" if r["y"] == 1 else "away"}
                        for r in paired if abs(r["blind"] * 100 - r["then"]) >= FAR_APART_PTS]
    if len(paired) >= MIN_FIT:
        X = [[1.0, logit(r["then"] / 100), logit(r["blind"]) - logit(r["then"] / 100)] for r in paired]
        fit = logistic(X, [r["y"] for r in paired])
        if fit:
            (a, bm, bd), (_sa, sm, sd) = fit
            out["fit"] = {"market_weight": round(bm, 2), "market_se": round(sm, 2),
                          "ace_weight": round(bd, 2), "ace_se": round(sd, 2),
                          "keeps_of_ace": round(max(0.0, min(1.0, bd)), 2)}
    return out


# ------------------------------------------------------------------ 4: where the money went

def group(bets, key):
    out = {}
    for b in bets:
        k = key(b)
        if k is None:
            continue
        out.setdefault(k, []).append(b)
    rows = {}
    for k, v in out.items():
        graded = [b for b in v if b["clv_pct"] is not None]
        half = len(v) // 2
        first, second = v[:half], v[half:]
        rows[k] = {"bets": len(v), "profit": round(sum(float(b["profit"]) for b in v), 2),
                   "roi_pct": round(sum(float(b["profit"]) for b in v) / (STAKE * len(v)) * 100, 1),
                   "avg_clv_pct": round(sum(b["clv_pct"] for b in graded) / len(graded), 2) if graded else None,
                   "same_both_halves": (len(v) >= 6 and (sum(float(b["profit"]) for b in first) > 0)
                                        == (sum(float(b["profit"]) for b in second) > 0))}
    return dict(sorted(rows.items()))


def money_section(bets):
    def price_band(b):
        p = float(b["price"])
        return "+200 or longer" if p >= 200 else "+100 to +199" if p >= 100 else "-150 to -101" if p >= -150 \
            else "shorter than -150"

    def edge_band(b):
        e = b.get("edge_pts")
        return None if e is None else "under 3 pts" if e < 3 else "3-5 pts" if e < 5 else "5+ pts"

    def timing(b):
        h = b.get("hours_before")
        return None if h is None else "under 3h" if h < 3 else "3-8h" if h < 8 else "8h+"

    def moved(b):
        c, m = b.get("close_pct"), b.get("novig_pct")
        if c is None or m is None:
            return None
        d = float(c) - float(m)
        return "moved his way" if d > 0.5 else "moved against him" if d < -0.5 else "barely moved"
    return {"by sport": group(bets, lambda b: str(b.get("sport") or "?").upper()),
            "favourite or underdog": group(bets, lambda b: None if b.get("market_pct") is None
                                           else "favourite" if float(b["market_pct"]) >= 50 else "underdog"),
            "by price": group(bets, price_band), "by edge": group(bets, edge_band),
            "by time before the game": group(bets, timing), "price after the bet": group(bets, moved)}


# ------------------------------------------------------------------ 6: candidate rules

def candidates(rows, judge, book, shrink_fit=None):
    """Rules replayed on every game he estimated: flat $100, one side a game,
    at DraftKings' price when he estimated. In-sample - ideas, not proof."""
    games = []
    for r in rows:
        snap = r.get("snap") or {}
        mh, ma = snap.get("moneyline_home"), snap.get("moneyline_away")
        if r["then"] is None or mh is None or ma is None:
            continue
        games.append({"y": r["y"], "ace_home": r["blind"] * 100, "mkt_home": float(r["then"]),
                      "price": {"home": mh, "away": ma}, "sport": r.get("sport")})

    def ace_pick(g, shrink):
        """The side his own bar (ace-judge.py) takes with this pull toward
        the market, measured against DraftKings then."""
        for side in ("home", "away"):
            q = g["mkt_home"] if side == "home" else 100 - g["mkt_home"]
            mine = g["ace_home"] if side == "home" else 100 - g["ace_home"]
            if judge.bar(mine, {"novig_pct": q, "price": g["price"][side]}, shrink)["clears"]:
                return side
        return None

    def other(side):
        return {"home": "away", "away": "home"}.get(side)

    def fav(g):
        return "home" if g["mkt_home"] >= 50 else "away"

    rules = {
        "his rule (60% pull)": lambda g: ace_pick(g, judge.SHRINK),
        "his rule, favourites only": lambda g: (lambda s: s if s and s == fav(g) else None)(ace_pick(g, judge.SHRINK)),
        "his rule, underdogs only": lambda g: (lambda s: s if s and s != fav(g) else None)(ace_pick(g, judge.SHRINK)),
        "the opposite of his rule": lambda g: other(ace_pick(g, judge.SHRINK)),
        "every favourite (30-75% only)": lambda g: fav(g) if 30 <= max(g["mkt_home"], 100 - g["mkt_home"]) <= 75 else None,
        "every underdog (25-70% only)": lambda g: other(fav(g)) if 25 <= min(g["mkt_home"], 100 - g["mkt_home"]) <= 70 else None,
    }
    if shrink_fit is not None and 0 < shrink_fit < 1:
        rules[f"his rule, {round(shrink_fit * 100)}% pull (fitted)"] = lambda g: ace_pick(g, shrink_fit)
    out = {}
    for name, rule in rules.items():
        results = []
        for g in games:
            side = rule(g)
            if side is None:
                continue
            won = (g["y"] == 1.0) == (side == "home")
            results.append(STAKE * (book.decimal_odds(g["price"][side]) - 1) if won else -STAKE)
        half = len(results) // 2
        out[name] = {"bets": len(results), "profit": round(sum(results), 2),
                     "roi_pct": round(sum(results) / (STAKE * len(results)) * 100, 1) if results else None,
                     "roi_range_pct": ci([x / STAKE * 100 for x in results]) if len(results) >= 2 else None,
                     # A rule that only made money in one half is one lucky stretch.
                     "same_both_halves": len(results) >= 20 and (sum(results[:half]) > 0) == (sum(results[half:]) > 0)
                     and sum(results) > 0}
    return {"games": len(games), "rules": out}


# ------------------------------------------------------------------ the report

def study():
    book, clv, judge, base = mods()
    lines = clv._read(clv.LINES, {})
    results = clv._read(STATE / "results.json", {})
    rows = clv.calibration_rows(STATE / "blind.jsonl", STATE / "results.json", clv.LINES)
    bets = bets_of(book, clv, lines)
    fc = forecast_section(rows)
    keep = (fc.get("fit") or {}).get("keeps_of_ace")
    starts = sorted(str(e.get("start_utc") or "")[:10] for e in lines.values() if e.get("start_utc"))
    try:
        no_ai = base.report(STATE / "baselines.jsonl")
    except Exception as exc:
        no_ai = {"error": str(exc)[:200]}
    return {
        "asof_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "data": {"settled_bets": len(bets), "blind_games_graded": len(rows), "games_with_prices": len(lines),
                 "prices_from": starts[0] if starts else None, "results_kept": len(results)},
        "profit": profit_section(bets),
        "forecasts": fc,
        "money": money_section(bets),
        "no_ai": no_ai,
        "candidates": candidates(rows, judge, book, shrink_fit=(1 - keep) if keep is not None else None),
    }


def eastern(utc):
    """'Oct 24 11:41' - when, in Eastern time (et_time.py owns the clock)."""
    t = _load("et_time_study", "et_time.py").to_eastern(str(utc or "").replace(" ", "T"))
    return t.strftime("%b %d %H:%M") if t else str(utc or "?")


def text(s):
    out = ["ACE - WHAT HIS RECORD SAYS (read-only; changes nothing)", ""]
    d, p, f = s["data"], s["profit"], s["forecasts"]
    out += ["1. THE DATA",
            f"   {d['settled_bets']} settled game bets, {d['blind_games_graded']} finished games he estimated blind, "
            f"{d['games_with_prices']} games with price history (from {d['prices_from']}).",
            "   Price history is kept 30 days and results 40 - older games drop out of this report.", ""]
    out.append("2. PROFIT, HONESTLY")
    if p["bets"]:
        r = p["roi_range_pct"]
        out += [f"   {p['bets']} bets {p['first']} to {p['last']}: ${p['profit']:+,.2f}, {p['roi_pct']:+.1f}% a bet."
                + (f" By luck alone it could have landed anywhere from {r[0]:+.1f}% to {r[1]:+.1f}% a bet." if r else "")]
        if p["worth"] is not None:
            out.append(f"   The closing prices say his {p['graded']} graded bets were worth ${p['worth']:+,.2f} "
                       f"({p['avg_clv_pct']:+.1f}% a bet); the rest, ${p['luck']:+,.2f}, was luck.")
    else:
        out.append("   No settled bets yet.")
    out += ["", "3. HIS FORECASTS (every game he estimated blind, not just his bets)"]
    if f.get("paired"):
        better = "better" if f["brier_ace"] < f["brier_market"] else "WORSE"
        out += [f"   Accuracy on {f['paired']} games (Brier: lower is better, 0.25 is a coin flip): "
                f"Ace {f['brier_ace']}, DraftKings when he estimated {f['brier_market']} - Ace is {better}.",
                f"   How far from 50-50 his chances sit: {f['ace_from_even_pts']} pts on average; the market's: "
                f"{f['market_from_even_pts']} pts." + (" His are squeezed toward even."
                                                     if f["ace_from_even_pts"] < f["market_from_even_pts"] - 1 else ""),
                "   When he said / the market said / what happened (home team):"]
        out += [f"     {c['bin']:>8}  n={c['n']:<4} Ace {c['ace_said']:5.1f}%  market {c['market_said']:5.1f}%  "
                f"happened {c['happened']:5.1f}%" for c in f["calibration"]]
        if f.get("far_apart"):
            out.append(f"   {len(f['far_apart'])} game(s) where he and the market were {FAR_APART_PTS}+ pts apart "
                       "(home team's chance; a home/away mix-up looks like this):")
            out += [f"     {str(g['starts_utc'])[:10]} {str(g['sport'] or '').upper():<4} {g['game']:<24} Ace {g['ace_home']:5.1f}%  "
                    f"market {g['market_home']:5.1f}%  {g['winner']} won  (estimated {eastern(g['estimated_utc'])} ET)"
                    for g in f["far_apart"]]
        fit = f.get("fit")
        if fit:
            sig = fit["ace_weight"] - 2 * fit["ace_se"] > 0
            out += [f"   Does his number add anything the market's does not? Weight on his disagreement: "
                    f"{fit['ace_weight']:+.2f} (give or take {fit['ace_se']:.2f}) - "
                    + ("yes, more than chance would give." if sig else "not yet distinguishable from nothing."),
                    f"   The pull toward the market this supports: {round((1 - fit['keeps_of_ace']) * 100)}% "
                    f"(his rule uses 60%)."]
        else:
            out.append(f"   The information test needs {MIN_FIT} games with a price; there are {f['paired']}.")
    else:
        out.append("   No finished game with both his estimate and a price yet.")
    out += ["", "4. WHERE THE MONEY WENT (his bets; * = same direction in both halves of the period)"]
    for name, g in s["money"].items():
        out.append(f"   {name}:")
        out += [f"     {k:<20} {v['bets']:>3} bets  ${v['profit']:+9,.2f}  {v['roi_pct']:+6.1f}% a bet"
                + (f"  close {v['avg_clv_pct']:+.1f}%" if v["avg_clv_pct"] is not None else "")
                + ("  *" if v["same_both_halves"] else "") for k, v in g.items()]
    out += ["", "5. THE NO-AI BETTORS ON THE SAME GAMES"]
    for name, r in (s["no_ai"] or {}).items():
        if isinstance(r, dict) and "profit" in r:
            out.append(f"   {name:<10} {r['settled']:>3} settled  ${r['profit']:+9,.2f}"
                       + (f"  close {r['avg_clv_pct']:+.1f}%" if r.get("avg_clv_pct") is not None else ""))
    c = s["candidates"]
    out += ["", f"6. RULES REPLAYED ON EVERY GAME HE ESTIMATED ({c['games']} with a price) - ideas, not proof",
            "   Flat $100 at DraftKings' price when he estimated, in the order the games were played.",
            f"   With {len(c['rules'])} rules tried, one usually looks good by luck alone; * = profitable in both",
            "   halves of the period. A rule picked here must still prove itself on games after today."]
    for name, r in sorted(c["rules"].items(), key=lambda kv: -(kv[1]["profit"])):
        rng = r["roi_range_pct"]
        out.append(f"   {name:<34} {r['bets']:>4} bets  ${r['profit']:+9,.2f}"
                   + (f"  {r['roi_pct']:+6.1f}% a bet" if r["roi_pct"] is not None else "")
                   + (f"  (luck range {rng[0]:+.1f}% to {rng[1]:+.1f}%)" if rng else "")
                   + ("  *" if r.get("same_both_halves") else ""))
    out += ["", "Nothing here changes a rule or places a bet."]
    return "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    s = study()
    print(json.dumps(s, indent=1) if "--json" in argv else text(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
