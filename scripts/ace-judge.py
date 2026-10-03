#!/usr/bin/env python3
"""
ace-judge.py — record a verdict on a game, without hand-writing JSON.

Ace spent 43 turns, 100 tool calls, 36 failures and $0.19 on one cycle trying
to write state/ledger.json by hand. It ended up proposing to "reformat it in a
way that may trick the system into recognizing the change". The file it
produced had `selection` set to "CHW @ CLE" - the match string - so not one of
its six rows joined to the board.

None of that is judgement. Copying a row and attaching a verdict is clerical,
and the exact-string matching that makes it fragile is invisible to whoever is
doing the copying. So the copying happens here:

    ace-judge.py list                       what is on the board, numbered
    ace-judge.py pass 3 --my-pct 58.0 --why "EV -1.2%, the bar is +3%"
    ace-judge.py pass 1-6,9 --why "no edge on the no-vig line"   one call, seven rows
    ace-judge.py rest --why "nothing cleared the EV bar"            sweeps the remainder
    ace-judge.py bet  7 --my-pct 71.0 --why "SP scratched an hour ago"   (stake is computed)
    ace-judge.py verdict "No picks - 6 judged, nothing cleared the EV bar."
    ace-judge.py mark                       close the cycle (bumps cycle_count)
    ace-judge.py show

`selection`, `match`, `price` and `novig_pct` are copied verbatim from
candidates.json by row number. They cannot be mistyped, because they are never
typed. Ace supplies only what it actually decided: the estimate and the reason.

Standard library only.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENT = ROOT / "agents" / "ace"
CANDIDATES = AGENT / "data" / "candidates.json"
LEDGER = AGENT / "state" / "ledger.json"

# Copied straight across from the fetcher's row. The join key is the first two.
CARRY = ("selection", "match", "sport", "price", "novig_pct", "starts_utc", "event_id", "side", "sharp_pct")


# What counts as a judged row, in one place.
#
# ace-verify counted every row in the ledger as judged; signoff counted only
# rows whose status is one of these. They agree today only because this script
# always sets a status - the moment a row arrives without one, the agent is
# told its ledger is both complete and empty, which is the shape of every
# expensive bug in this repo.
JUDGED_STATUSES = ("passed", "bet")


def rows_of(doc):
    """The rows of a ledger or candidates document, whatever shape it is in.

    A bare array was what crashed the verifier once: calling .get() on a list.
    """
    if isinstance(doc, list):
        return doc
    return (doc or {}).get("candidates") or []


def judged_rows(doc):
    return [r for r in rows_of(doc) if r.get("status") in JUDGED_STATUSES]


def slate_size(agent_dir=None):
    """How many games the fetcher offered this cycle, or None if unreadable.

    Zero is a real, recurring state. ace-fetch keeps only games starting within
    BET_WINDOW_HOURS and drops ones already underway, so on a Friday night -
    MLB finished, NFL not until Sunday - the slate is legitimately empty. An
    empty ledger is then correct and not evidence of anything.

    Ace was failed at 03:31 on 2026-09-18 for exactly that, having written
    "No picks - nothing cleared the bar on the no-vig line." It had done every
    part of its job.
    """
    path = (Path(agent_dir) / "data" / "candidates.json") if agent_dir else CANDIDATES
    try:
        return len(rows_of(json.loads(path.read_text())))
    except Exception:
        return None


def load_candidates():
    try:
        doc = json.loads(CANDIDATES.read_text())
    except Exception as exc:
        print(f"cannot read {CANDIDATES}: {exc}\n"
              f"Run the fetcher first:  python3 ../../scripts/ace-fetch.py", file=sys.stderr)
        sys.exit(2)
    rows = doc if isinstance(doc, list) else (doc.get("candidates") or [])
    if not rows:
        print("candidates.json has no rows - nothing to judge.", file=sys.stderr)
        sys.exit(2)
    return doc, rows


def cycle_started():
    try:
        return int((AGENT / "state" / ".cycle-started").read_text().strip())
    except Exception:
        return 0


def load_ledger():
    """The ledger for THIS cycle, always a dict with a candidates list.

    Two things it has to survive. Ace once wrote ledger.json as a bare JSON
    array, and reading that straight back crashed this script on .get(). And a
    ledger left by an earlier cycle must not be appended to - yesterday's
    passes are not this afternoon's, and the file would keep rows for games
    that have already finished.
    """
    now = et_time.eastern_now()
    fresh = {"day": et_time.day(now), "slot": et_time.slot("ace", now),
             "verdict": None, "candidates": []}
    if not LEDGER.exists():
        return fresh
    started = cycle_started()
    if started and LEDGER.stat().st_mtime < started:
        return fresh                                  # left by an earlier cycle
    try:
        doc = json.loads(LEDGER.read_text())
    except Exception:
        return fresh
    if isinstance(doc, list):                         # a bare array of rows
        fresh["candidates"] = doc
        return fresh
    if not isinstance(doc, dict):
        return fresh
    doc.setdefault("candidates", [])
    if not isinstance(doc["candidates"], list):
        doc["candidates"] = []
    doc.setdefault("day", fresh["day"])
    doc.setdefault("slot", fresh["slot"])
    # A ledger from a different slot is a different cycle's work.
    if doc.get("day") != fresh["day"] or doc.get("slot") != fresh["slot"]:
        return fresh
    return doc


def save(led):
    led["asof_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, indent=1) + "\n")


def parse_numbers(spec, total):
    """"3", "1-6", "1-6,9,12" -> [3] / [1..6] / [1..6, 9, 12].

    One judgement per tool call meant 60 candidates cost 60 round trips, and a
    cycle spent 184 calls and hit its timeout still working. Rows that share a
    reason - which is most of them, since most games simply do not clear the
    bar - should cost one call."""
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                return None, f"{part!r} is not a number or a range like 4-9"
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                return None, f"{part!r} is not a number or a range like 4-9"
    seen, uniq = set(), []
    for n in out:
        if n in seen:
            continue
        seen.add(n)
        if not 1 <= n <= total:
            return None, f"there is no candidate {n} - the list has {total}"
        uniq.append(n)
    return uniq, None


def log_for_clv(rows):
    """Every verdict goes to ace-clv.py's journal too: the ledger is replaced
    each cycle, and the estimate is only gradeable after its game closes."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("ace_clv", Path(__file__).resolve().parent / "ace-clv.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Ace's own state folder (AGENT, which the tests point elsewhere),
        # under ace-clv.py's file name - one name, one owner.
        mod.log_judgements(rows, path=AGENT / "state" / mod.JOURNAL.name)
    except Exception as exc:
        print(f"  (not logged for closing-line grading: {exc})", file=sys.stderr)


def focus_today():
    """Today's focus from ace-focus.py, or None - read there so this and the
    fetcher agree on whether today is a focus day."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ace_focus", Path(__file__).resolve().parent / "ace-focus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.today()


def other_side(rows, row):
    """The other team's row in the same game, or None. A moneyline game is two
    rows, and their no-vig chances add to 100."""
    for r in rows:
        if r.get("match") == row.get("match") and r.get("selection") != row.get("selection"):
            return r
    return None


def key(r):
    return f"{str(r.get('selection') or '').lower()}|{str(r.get('match') or '').lower()}"


def cmd_list(a):
    gate = blind_gate()
    if gate:
        print(gate, file=sys.stderr)
        return 1
    _, rows = load_candidates()
    led = load_ledger()
    done = {key(r): r for r in led.get("candidates", [])}
    print(f"{len(rows)} candidates. Judge one with:  ace-judge.py pass <n> "
          f"--my-pct <your estimate> --why \"reason\"\n")
    for i, r in enumerate(rows, 1):
        mine = done.get(key(r))
        mark = f"  [{mine['status']}]" if mine else ""
        price = r.get("price")
        price = f"{price:+d}" if isinstance(price, int) else str(price)
        sharp = f"  Pinnacle {r['sharp_pct']}%" if r.get("sharp_pct") is not None else ""
        print(f"  {i:>3}. {str(r.get('selection')):<10} {str(r.get('match')):<14} "
              f"price {price:>6}   no-vig {r.get('novig_pct')}%{sharp}{mark}")
        for inj in r.get("fresh_injuries") or []:
            if r.get("side") != "away":          # once a game, under its home row
                print(f"         news {inj.get('id')}: {inj.get('team')} {inj.get('player')} "
                      f"({inj.get('position')}) {inj.get('status')}, {inj.get('hours_old')}h ago")
    print(f"\n{len(done)} judged so far. The other {len(rows) - len(done)} will show "
          f"as unjudged on the board, which is honest - judge the ones you looked at.")
    return 0


def record(a, status):
    doc, rows = load_candidates()
    gate = blind_gate()
    if gate:
        print(gate, file=sys.stderr)
        return 1
    nums, err = parse_numbers(a.number, len(rows))
    if err:
        print(f"{err}. Run `ace-judge.py list` to see them.", file=sys.stderr)
        return 1
    if not nums:
        print("no candidates given.", file=sys.stderr)
        return 1
    if not a.why:
        print("--why is required. The reason IS the row - a verdict with no reason "
              "tells the user nothing about why you passed.", file=sys.stderr)
        return 1
    # A FOCUS DAY (ace-focus.py): the slate is only as big as what gets
    # studied, so every game is judged on its own, with an estimate. One
    # estimate covers both sides of a game - the other team's row is filled in
    # below at 100 minus it - so this costs one call a game, as sweeping did.
    focus = focus_today()
    if focus and len(nums) > 1:
        print(f"focus day ({focus['sport']}, {focus['games']} games): judge one game at a "
              f"time with its own --my-pct. The other side of each game is filled in "
              f"for you.", file=sys.stderr)
        return 1
    if focus and a.my_pct is None:
        print("focus day: every game needs your --my-pct - that estimate is the "
              "whole point of a slate this small.", file=sys.stderr)
        return 1
    # A pass naming one or two games is a game Ace actually looked at, and an
    # estimate for it is the only evidence that ever accumulates about whether
    # the expected-value bar is set right. Without it every passed row carries
    # edge_pts: null, which is what a week of "no bets" looks like from the
    # outside: unanswerable. A sweep of the whole board is exempt - one number
    # cannot be an estimate for sixteen different games, and pretending it is
    # would be worse than recording nothing.
    if (status == "passed" and a.my_pct is None
            and len(nums) <= ESTIMATE_REQUIRED_UPTO):
        print(f"--my-pct is required when you pass {len(nums)} game(s) by name: "
              f"this is one you looked at, and your estimate is the only record "
              f"of HOW close it was.\n"
              f"  ace-judge.py pass {a.number} --my-pct 54.5 --why \"{a.why[:40]}\"\n"
              f"Sweeping the rest with one reason does not need it.",
              file=sys.stderr)
        return 1

    placed = None
    if status == "bet":
        if len(nums) > 1:
            print("bet one at a time: each bet cites its own news and is checked against "
                  "its own price.", file=sys.stderr)
            return 1
        if a.stake is not None:
            print("drop --stake: every bet is a flat 1% of the starting bankroll, set by "
                  "ace-book.py.", file=sys.stderr)
            return 1
        # The bar is measured on the estimate, so a bet without one claims to
        # clear it with nothing on record to check.
        if a.my_pct is None:
            print("a bet needs --my-pct: the bar is measured on your estimate, and without "
                  "it there is no way to show it was cleared.", file=sys.stderr)
            return 1
        src = dict(rows[nums[0] - 1])
        b = book()
        why_not = b.refusal(src.get("sport"), src.get("event_id"), src.get("selection"), src.get("match")) \
            or evidence_stop()
        if why_not:
            print(f"refused: {why_not}.", file=sys.stderr)
            return 1
        # The price at this moment, not the one in candidates.json - which
        # can be half an hour old, and the bet is booked at what it says.
        live = live_price(src)
        if live:
            src.update(price=live["price"], novig_pct=live["novig_pct"])
        priced = (f"live at {live['at']} UTC" if live else
                  f"candidates.json at {doc.get('asof_utc') if isinstance(doc, dict) else '?'} UTC")
        verdict = bar(a.my_pct, src)
        if not verdict["clears"]:
            print(f"refused: {verdict['why']}. Pass it with this estimate instead.", file=sys.stderr)
            return 1
        event, why_not = cited_event(src, getattr(a, "event", None), current_pct=src.get("novig_pct"))
        if why_not:
            print(f"refused: {why_not}.", file=sys.stderr)
            return 1
        shadow, shadow_pct = kelly_stake(verdict["p_used"], src.get("price"), b.starting_bankroll())
        placed = {"sport": src.get("sport"), "event_id": src.get("event_id"), "match": src.get("match"),
                  "selection": src.get("selection"), "side": src.get("side"),
                  "starts_utc": src.get("starts_utc"), "price": src.get("price"),
                  "price_source": priced, "novig_pct": src.get("novig_pct"),
                  "sharp_pct": src.get("sharp_pct"), "my_pct": a.my_pct,
                  "p_used": verdict["p_used"], "ev_pct": verdict["ev_pct"],
                  "event": event, "why": a.why, "kelly_shadow_stake": shadow,
                  "kelly_shadow_pct": shadow_pct}
        rows = [dict(r) for r in rows]
        rows[nums[0] - 1] = src

    led = load_ledger()
    done = []
    for n in nums:
        src = rows[n - 1]
        row = {k: src.get(k) for k in CARRY}
        row["my_pct"] = a.my_pct
        novig = src.get("novig_pct")
        if a.my_pct is not None and novig is not None:
            row["edge_pts"] = round(float(a.my_pct) - float(novig), 1)
        if a.my_pct is not None:
            v = bar(a.my_pct, src)
            row.update(p_used=v["p_used"], gap_pts=v["gap_pts"], need_pts=v["need_pts"],
                       ev_pct=v["ev_pct"], clears_bar=v["clears"])
        row["why_not"] = [a.why]
        row["status"] = status
        led["candidates"] = [r for r in led.get("candidates", []) if key(r) != key(row)]
        led["candidates"].append(row)
        done.append(row)
    filled = []
    if focus and len(done) == 1 and a.my_pct is not None:
        src = other_side(rows, rows[nums[0] - 1])
        judged = {key(r) for r in led["candidates"]}
        if src and key(src) not in judged:
            twin = {k: src.get(k) for k in CARRY}
            twin["my_pct"] = round(100.0 - float(a.my_pct), 1)
            if src.get("novig_pct") is not None:
                twin["edge_pts"] = round(twin["my_pct"] - float(src["novig_pct"]), 1)
            tv = bar(twin["my_pct"], src)
            twin.update(p_used=tv["p_used"], gap_pts=tv["gap_pts"], need_pts=tv["need_pts"],
                        ev_pct=tv["ev_pct"], clears_bar=tv["clears"])
            twin["why_not"] = [f"other side of {done[0]['selection']}: {a.why}"]
            twin["status"] = "passed"
            led["candidates"].append(twin)
            filled.append(twin)
            print(f"  and PASSED the other side, {twin['selection']}, at "
                  f"{twin['my_pct']:.1f}% (edge {twin.get('edge_pts', 0):+.1f} pts)")
    if placed:
        bet = book().place(placed)
        done[0].update(stake=bet["stake"], bet_id=bet["id"])
    save(led)
    log_for_clv(done + filled)

    if len(done) == 1:
        r = done[0]
        edge = f", edge {r['edge_pts']:+.1f} pts" if r.get("edge_pts") is not None else ""
        if r.get("ev_pct") is not None:
            edge += f", EV {r['ev_pct']:+.1f}%"
        if status == "bet":
            edge += (f", counted as {r['p_used']:g}%, stake ${r['stake']:,.2f} flat, citing "
                     f"{placed['event']['player']} ({placed['event']['status']}) - booked at "
                     f"{r['price']} ({placed['price_source']})")
        print(f"{status.upper()}: {r['selection']} ({r['match']}) at {r['price']}"
              f"{edge} - {a.why}")
    else:
        print(f"{status.upper()} x{len(done)}: "
              + ", ".join(r["selection"] for r in done[:8])
              + (" ..." if len(done) > 8 else "") + f" - {a.why}")
    print(f"ledger now has {len(led['candidates'])} judged rows.")
    return 0


def cmd_pass(a):
    return record(a, "passed")


def _load(name, file):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clv():
    return _load("ace_clv", "ace-clv.py")


def book():
    """ace-book.py, pointed at this AGENT's state - the tests move AGENT."""
    m = _load("ace_book", "ace-book.py")
    m.STATE = AGENT / "state"
    m.BETS, m.BANK, m.RESULTS = m.STATE / "bets.jsonl", m.STATE / "bankroll.json", m.STATE / "results.json"
    return m


# How many bets may be open at once. It lived only in Ace's header - a
# sentence, honoured or not, with nothing checking. ace-book.py owns it now
# (it owns the money); ace-verify.py reads it from here, and a test fails
# the build if the header stops quoting the same number.
MAX_OPEN_BETS = _load("ace_book_cap", "ace-book.py").MAX_OPEN_BETS

# THE BAR (2026-10-01, replacing "+3% expected value at Ace's estimate").
# An outside review showed the old bar was Ace's own estimate restated: any
# estimate a few points above the line cleared it, so with ordinary
# estimating error it let through 13-45% of games with no edge at all - most
# easily the long-shot underdogs, the worst-priced bets on the board. Now:
#
#   q        the market's chance for the side: Pinnacle's no-vig when
#            ace-sharp.py has it, DraftKings' otherwise
#   p_used   Ace's estimate pulled SHRINK of the way back to q:
#            q + (1 - SHRINK) x (my_pct - q). A model's estimate is mostly
#            noise until the record says otherwise.
#   the bar  p_used beats q by at least GAP_MIN_PTS, or GAP_MIN_REL percent
#            of q if that is more; the bet is still positive expected value
#            at DraftKings' price; and q is between BET_RANGE - no long
#            shots, no heavy favourites, while his estimates are unproven.
SHRINK = 0.6
GAP_MIN_PTS = 2.0
GAP_MIN_REL = 3.0
BET_RANGE = (30.0, 75.0)
# Shadow staking only: quarter-Kelly at p_used, capped, stored on each bet.
# No money rides on it; the stake is ace-book.py's flat 1%.
KELLY_FRACTION = 0.25
STAKE_CAP_PCT = 3.0
# THE EVIDENCE STOP: after this many settled bets, a mean closing-line
# value at or below zero means no edge has shown up - betting stops and the
# owner decides. (The fault stop, a 30% loss, is ace-book.py's.)
EVIDENCE_MIN_BETS = 60
RETIRED_BANKROLL_KEYS = ("unit_pct", "max_stake_pct", "max_open_bets",
                         "stop_loss_pct", "stop_loss_floor")


def ev_pct(my_pct, price):
    """Expected return per $1 staked, in percent, at a chance. None if
    either number is missing or the price is not a price."""
    d = _clv().decimal_odds(price)
    if d is None or my_pct is None:
        return None
    # + 0.0 turns the -0.0 that break-even rounds to into 0.0, which would
    # otherwise print as "-0.0%" beside a bar it exactly misses.
    return round((float(my_pct) / 100 * d - 1) * 100, 1) + 0.0


def kelly_stake(my_pct, price, bankroll):
    """(stake in dollars, percent of bankroll) - quarter-Kelly, capped. The
    shadow figure stored on each bet."""
    d = _clv().decimal_odds(price)
    if d is None or my_pct is None or not bankroll:
        return 0.0, 0.0
    b, p = d - 1, float(my_pct) / 100
    full = (b * p - (1 - p)) / b
    if full <= 0:
        return 0.0, 0.0
    frac = min(KELLY_FRACTION * full, STAKE_CAP_PCT / 100)
    return round(float(bankroll) * frac, 2), round(frac * 100, 2)


def bar(my_pct, row):
    """Everything the bar decides for one estimate on one row:
    {q, q_source, p_used, gap_pts, need_pts, ev_pct, clears, why}."""
    q, src = row.get("sharp_pct"), "Pinnacle"
    if q is None:
        q, src = row.get("novig_pct"), "DraftKings"
    out = {"q": q, "q_source": src, "p_used": None, "gap_pts": None, "need_pts": None,
           "ev_pct": None, "clears": False, "why": ""}
    if my_pct is None or q is None:
        out["why"] = "no estimate" if my_pct is None else "no market price to measure against"
        return out
    q = float(q)
    p_used = round(q + (1 - SHRINK) * (float(my_pct) - q), 1)
    gap = round(p_used - q, 1)
    need = round(max(GAP_MIN_PTS, q * GAP_MIN_REL / 100), 1)
    ev = ev_pct(p_used, row.get("price"))
    out.update(p_used=p_used, gap_pts=gap, need_pts=need, ev_pct=ev)
    if not BET_RANGE[0] <= q <= BET_RANGE[1]:
        out["why"] = (f"the market gives this side {q:.1f}% - bets are only taken between "
                      f"{BET_RANGE[0]:g}% and {BET_RANGE[1]:g}%")
    elif gap < need:
        out["why"] = (f"your {float(my_pct):g}% counts as {p_used:g}% (pulled {SHRINK:.0%} of the way to "
                      f"the market's {q:.1f}%, {src}) - {gap:+.1f} pts, and the bar is +{need:g}")
    elif ev is None or ev <= 0:
        out["why"] = f"at {p_used:g}% the price {row.get('price')} is not positive expected value ({ev})"
    else:
        out["clears"] = True
        out["why"] = f"{p_used:g}% vs the market's {q:.1f}% ({src}): {gap:+.1f} pts, EV {ev:+.1f}%"
    return out


def evidence_stop():
    """Why betting has stopped on the evidence, or None."""
    try:
        r = _clv().report(lines_path=AGENT / "state" / "lines.json",
                          journal_path=AGENT / "state" / "judgements.jsonl")
    except Exception:
        return None
    clvs = [g.get("sharp_clv_pct") if g.get("sharp_clv_pct") is not None else g.get("clv_pct")
            for g in r.get("graded", []) if g.get("status") == "bet"]
    clvs = [c for c in clvs if c is not None]
    if len(clvs) >= EVIDENCE_MIN_BETS and sum(clvs) / len(clvs) <= 0:
        return (f"after {len(clvs)} bets the average closing-line value is "
                f"{sum(clvs) / len(clvs):+.2f}% - no edge has shown up, so betting has "
                f"stopped and the owner decides")
    return None


def cited_event(row, event_id, now=None, current_pct=None):
    """(the fresh injury a bet cites, None) or (None, why it cannot be used).

    A bet must point at something that happened - one of the row's
    fresh_injuries, by id - and the price must not have moved toward the
    side since it was reported. "My estimate likes them more than the line
    does" is not an edge; that sentence was the whole rule, and it was prose."""
    now = now or datetime.now(timezone.utc)
    news = row.get("fresh_injuries") or []
    if not event_id:
        ids = ", ".join(f"{i.get('id')} ({i.get('team')} {i.get('player')}, {i.get('status')})" for i in news)
        return None, ("a bet cites the news it is built on: --event ID, from this row's fresh_injuries"
                      + (f". This row has: {ids}" if ids else ". This row has none, so it cannot be bet"))
    hit = next((i for i in news if i.get("id") == event_id), None)
    if hit is None:
        return None, f"no fresh injury {event_id!r} on this row - `ace-judge.py list` shows each row's ids"
    clv = _clv()
    reported = clv.parse_start(hit.get("reported_utc"))
    if reported is None:
        return None, "that report has no time on it, so its age cannot be checked"
    hours = (now - reported).total_seconds() / 3600
    fresh_h = _load("ace_fetch_fresh", "ace-fetch.py").FRESH_INJURY_HOURS
    if hours > fresh_h:
        return None, f"that report is {hours:.1f} hours old - news is {fresh_h:g} hours or less"
    lines = clv._read(AGENT / "state" / "lines.json", {})
    entry = lines.get(clv.game_key(row.get("sport"), row.get("match"), row.get("starts_utc")))
    ok, moved = clv.unpriced(entry, row.get("side") or "home",
                             reported, current_pct if current_pct is not None else row.get("novig_pct"))
    if not ok:
        return None, moved
    return dict(hit, moved_since_pts=moved), None


def live_price(row):
    """DraftKings' price for this row right now, re-fetched from ESPN, as
    {price, novig_pct, at} - or None, and the bet uses the candidates file's
    price with its time. ACE_NO_LIVE_PRICE=1 turns the fetch off (tests,
    replays)."""
    if os.environ.get("ACE_NO_LIVE_PRICE") or not row.get("event_id") or row.get("side") not in ("home", "away"):
        return None
    try:
        f = _load("ace_fetch_live", "ace-fetch.py")
        odds = f.odds_of(f.summary_of(row["sport"], row["event_id"]))
    except Exception:
        return None
    side = row["side"]
    if odds.get(f"moneyline_{side}") is None or odds.get(f"novig_{side}_pct") is None:
        return None
    return {"price": odds[f"moneyline_{side}"], "novig_pct": odds[f"novig_{side}_pct"],
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}


# Passing this many games or fewer by name means Ace studied them, so it must
# say what it estimated. Above this it is sweeping the board with one shared
# reason, where a single number would be a fiction.
ESTIMATE_REQUIRED_UPTO = 2


def estimates_in(ledger):
    """Rows carrying Ace's own probability. The evidence, counted."""
    return [r for r in rows_of(ledger)
            if isinstance(r.get("my_pct"), (int, float))]


# ------------------------------------------------------------------ blind

def blind_sheet():
    try:
        return json.loads((AGENT / "data" / "blind.json").read_text())
    except Exception:
        return None


def blind_done(since=None):
    """{game key: home pct} estimated blind since `since` (epoch seconds)."""
    since = cycle_started() if since is None else since
    out = {}
    try:
        text = (AGENT / "state" / "blind.jsonl").read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        when = _clv().parse_start(j.get("utc"))
        if since and (when is None or when.timestamp() < since):
            continue
        out[j.get("key")] = j.get("home_pct")
    return out


def blind_key(g):
    return _clv().game_key(g.get("sport"), g.get("match"), g.get("starts_utc"))


def blind_missing(since=None):
    """The numbers on data/blind.json not yet estimated this cycle. [] when
    there is no sheet - nothing can be owed on a sheet that does not exist."""
    sheet = blind_sheet()
    if not sheet:
        return []
    done = blind_done(since)
    return [g["n"] for g in sheet.get("games") or [] if blind_key(g) not in done]


def blind_gate():
    missing = blind_missing()
    if not missing:
        return None
    return (f"blind estimates first: {len(missing)} game(s) on data/blind.json have none this cycle "
            f"({', '.join(str(n) for n in missing[:20])}{' ...' if len(missing) > 20 else ''}). "
            f"Read data/blind.json - it has no prices on purpose - and give each game the HOME "
            f"team's chance:\n  python3 ../../scripts/ace-judge.py blind \"1:55,2:41,3:62\"\n"
            f"Prices are shown after that.")


def cmd_blind(a):
    sheet = blind_sheet()
    if not sheet or not sheet.get("games"):
        print("data/blind.json has no games - nothing to estimate this cycle.")
        return 0
    games = {g["n"]: g for g in sheet["games"]}
    got, bad = {}, []
    for part in str(a.pairs).replace(" ", "").split(","):
        if not part:
            continue
        try:
            n, pct = part.split(":", 1)
            n, pct = int(n), float(pct)
        except ValueError:
            bad.append(f"{part!r} is not number:percent")
            continue
        if n not in games:
            bad.append(f"there is no game {n} - the sheet has 1-{len(games)}")
        elif not 1 <= pct <= 99:
            bad.append(f"game {n}: {pct:g}% - a chance is between 1 and 99")
        else:
            got[n] = pct
    if bad:
        print("nothing recorded:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = AGENT / "state" / "blind.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for n, pct in sorted(got.items()):
            g = games[n]
            f.write(json.dumps({"utc": stamp, "key": blind_key(g), "sport": g.get("sport"),
                                "event_id": g.get("event_id"), "match": g.get("match"),
                                "starts_utc": g.get("starts_utc"),
                                "home": (g.get("home") or {}).get("abbr"),
                                "away": (g.get("away") or {}).get("abbr"),
                                "home_pct": pct}) + "\n")
    left = blind_missing()
    print(f"recorded {len(got)} blind estimate(s)." + (
        f" Still owed: {', '.join(str(n) for n in left)}." if left else
        " Every game on the sheet is estimated - `ace-judge.py list` shows the prices now."))
    return 0


def cmd_bet(a):
    return record(a, "bet")


def cmd_rest(a):
    """Judge every remaining candidate with one shared reason.

    Only for a reason that is honestly true of all of them - "did not clear the
    bar on the no-vig line" is; "read the context file" is not. Games you never
    actually looked at are better left out: the board marks those `unjudged` on
    its own, which tells the user what was skipped."""
    focus = focus_today()
    if focus:
        print(f"focus day ({focus['sport']}, {focus['games']} games): no sweeping - "
              f"judge each game with its own --my-pct. `ace-judge.py list` shows "
              f"which are left.", file=sys.stderr)
        return 1
    gate = blind_gate()
    if gate:
        print(gate, file=sys.stderr)
        return 1
    _, rows = load_candidates()
    led = load_ledger()
    have = {key(r) for r in led.get("candidates", [])}
    todo = [i + 1 for i, r in enumerate(rows) if key(r) not in have]
    if not todo:
        print("every candidate is already judged.")
        return 0
    a.number = ",".join(str(n) for n in todo)
    return record(a, "passed")


def cmd_mark(a):
    """Close the cycle: bump cycle_count, stamp last_cycle_utc.

    Belfort has had this since the day its arithmetic moved into code. Ace
    never got it, so bankroll.json was the one deliverable with no helper -
    hand-edited JSON that signoff.py then demanded had been written this cycle.
    A cycle that got it slightly wrong was told MISSING with no way to see
    why, rewrote it, and went round again; one such run made 87 tool calls and
    cost $0.29 before the timeout stopped it."""
    bank = AGENT / "state" / "bankroll.json"
    try:
        b = json.loads(bank.read_text())
    except FileNotFoundError:
        print(f"{bank} does not exist. Seed it:\n"
              f"  cp -n {bank.with_suffix('')}.seed.json {bank}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{bank} does not parse ({exc}). It has been hand-edited into an "
              f"invalid state - restore it from state/bankroll.seed.json.", file=sys.stderr)
        return 2

    b["cycle_count"] = int(b.get("cycle_count") or 0) + 1
    b["last_cycle_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    retired = [k for k in RETIRED_BANKROLL_KEYS if k in b]
    for k in retired:
        del b[k]
    bank.write_text(json.dumps(b, indent=1) + "\n")
    if retired:
        print(f"removed settings nothing read: {', '.join(retired)} - the rules live in "
              f"ace-judge.py now")

    open_n = len(b.get("open_bets") or [])
    print(f"cycle {b['cycle_count']} recorded. bankroll ${float(b.get('bankroll') or 0):,.2f}, "
          f"{open_n} open bet(s).")
    return 0


def cmd_verdict(a):
    led = load_ledger()
    led["verdict"] = a.text
    save(led)
    print(f"verdict set: {a.text}")
    return 0


def ledger_on_disk():
    """The ledger file as it stands, whichever cycle wrote it.

    load_ledger() answers "what may this cycle write to", and a ledger from an
    earlier slot is empty to it on purpose. `show` answers "what did Ace
    decide", and must not report an empty ledger over a file full of rows -
    it did, from 20:00 ET onwards, for the afternoon's games.
    """
    try:
        doc = json.loads(LEDGER.read_text())
    except Exception:
        return {"candidates": []}
    if isinstance(doc, list):
        return {"candidates": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("candidates"), list):
        return {"candidates": []}
    return doc


def cmd_show(a):
    led = ledger_on_disk()
    rows = led.get("candidates", [])
    print(f"{LEDGER}")
    print(f"  day {led.get('day')}  slot {led.get('slot')}")
    hint = "(not set - run: ace-judge.py verdict 'one line on the slate')"
    print(f"  verdict: {led.get('verdict') or hint}")
    for r in rows:
        print(f"  [{r.get('status')}] {r.get('selection')} / {r.get('match')} "
              f"- {'; '.join(r.get('why_not') or [])}")
        print(f"      {edge_line(r)}")
    with_edge = sum(1 for r in rows if r.get("edge_pts") is not None)
    print(f"  {len(rows)} rows, {with_edge} with Ace's own estimate "
          f"(the rest were passed in a sweep, with no number of their own)")
    return 0


def _pct(v):
    return "?" if v is None else f"{float(v):.1f}%"


def edge_line(r):
    """The numbers behind one verdict: the price, the market's no-vig chance,
    Ace's own estimate, the gap between them, and what the bar counts the
    estimate as. A row passed in a sweep has no estimate, and says so rather
    than showing a gap nobody worked out."""
    head = f"price {r.get('price')}  market {_pct(r.get('novig_pct'))}"
    if r.get("my_pct") is None:
        return head + "  Ace: no estimate (swept)"
    edge = r.get("edge_pts")
    ev = r.get("ev_pct")
    return (head + f"  Ace {_pct(r.get('my_pct'))}  edge "
            + ("?" if edge is None else f"{float(edge):+.1f} pts")
            + ("" if r.get("p_used") is None else f"  counts as {_pct(r['p_used'])}")
            + ("" if ev is None else f"  EV {float(ev):+.1f}%"
               + (" (clears the bar)" if r.get("clears_bar") else ""))
            + (f"  stake {r['stake']:g}" if r.get("stake") is not None else ""))


def main():
    ap = argparse.ArgumentParser(description="Record verdicts in Ace's shadow ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("show").set_defaults(fn=cmd_show)

    for name, fn in (("pass", cmd_pass), ("bet", cmd_bet)):
        s = sub.add_parser(name)
        s.add_argument("number")          # "3", "1-6", "1-6,9,12"
        s.add_argument("--my-pct", type=float, dest="my_pct")
        s.add_argument("--why", default="")
        s.add_argument("--stake", type=float)
        s.add_argument("--event", help="the id of the fresh injury a bet is built on")
        s.set_defaults(fn=fn)

    bl = sub.add_parser("blind", help="home-win chances from data/blind.json, before any price")
    bl.add_argument("pairs", help='"1:55,2:41,..." - game number : home team\'s chance')
    bl.set_defaults(fn=cmd_blind)

    r = sub.add_parser("rest")
    r.add_argument("--my-pct", type=float, dest="my_pct")
    r.add_argument("--why", default="")
    r.add_argument("--stake", type=float)
    r.set_defaults(fn=cmd_rest)

    sub.add_parser("mark").set_defaults(fn=cmd_mark)

    v = sub.add_parser("verdict")
    v.add_argument("text")
    v.set_defaults(fn=cmd_verdict)

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
