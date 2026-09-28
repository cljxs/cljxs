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
CARRY = ("selection", "match", "sport", "price", "novig_pct", "starts_utc")


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
        print(f"  {i:>3}. {str(r.get('selection')):<10} {str(r.get('match')):<14} "
              f"price {price:>6}   no-vig {r.get('novig_pct')}%{mark}")
    print(f"\n{len(done)} judged so far. The other {len(rows) - len(done)} will show "
          f"as unjudged on the board, which is honest - judge the ones you looked at.")
    return 0


def record(a, status):
    _, rows = load_candidates()
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

    bet_stake = bet_frac = bet_ev = None
    if status == "bet":
        if len(nums) > 1:
            print("bet one at a time. A stake is sized per game, from that game's "
                  "estimate and price.", file=sys.stderr)
            return 1
        if a.stake is not None:
            print(f"drop --stake: the stake is computed now - quarter-Kelly from your "
                  f"estimate and the price, capped at {STAKE_CAP_PCT:g}% of the bankroll.",
                  file=sys.stderr)
            return 1
        # The bar is expected value at your estimate, so a bet without one
        # claims to clear it with nothing on record to check - and it is the
        # one row where that matters most, because it is the one that moves money.
        if a.my_pct is None:
            print(f"a bet needs --my-pct. The bar is +{EV_MIN_PCT:g}% expected value at "
                  f"your estimate, and without the estimate there is no way to show it "
                  f"was cleared.", file=sys.stderr)
            return 1
        src = rows[nums[0] - 1]
        bet_ev = ev_pct(a.my_pct, src.get("price"))
        if bet_ev is None:
            print("this row has no usable price, so its expected value cannot be "
                  "worked out - it cannot be bet.", file=sys.stderr)
            return 1
        if bet_ev < EV_MIN_PCT:
            print(f"refused: at {a.my_pct:g}% and a price of {src.get('price')}, the expected "
                  f"value is {bet_ev:+.1f}% - under the +{EV_MIN_PCT:g}% bar. Pass it with "
                  f"this estimate instead.", file=sys.stderr)
            return 1
        bank = current_bankroll()
        if not bank:
            print("cannot size the bet: state/bankroll.json has no bankroll.", file=sys.stderr)
            return 1
        floor = stop_floor()
        if floor is not None and bank <= floor:
            print(f"FULL STOP: the bankroll is ${bank:,.2f}, at or below the ${floor:,.2f} "
                  f"stop ({STOP_LOSS_PCT:g}% under where it started). Open nothing - say so "
                  f"in the report; the owner decides.", file=sys.stderr)
            return 1
        bet_stake, bet_frac = kelly_stake(a.my_pct, src.get("price"), bank)

    led = load_ledger()
    done = []
    for n in nums:
        src = rows[n - 1]
        row = {k: src.get(k) for k in CARRY}
        row["my_pct"] = a.my_pct
        novig = src.get("novig_pct")
        if a.my_pct is not None and novig is not None:
            row["edge_pts"] = round(float(a.my_pct) - float(novig), 1)
        ev = ev_pct(a.my_pct, src.get("price"))
        if ev is not None:
            row["ev_pct"] = ev
            row["clears_bar"] = ev >= EV_MIN_PCT
        row["why_not"] = [a.why]
        row["status"] = status
        if status == "bet":
            row["stake"] = bet_stake
            row["stake_pct"] = bet_frac
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
            tev = ev_pct(twin["my_pct"], src.get("price"))
            if tev is not None:
                twin["ev_pct"], twin["clears_bar"] = tev, tev >= EV_MIN_PCT
            twin["why_not"] = [f"other side of {done[0]['selection']}: {a.why}"]
            twin["status"] = "passed"
            led["candidates"].append(twin)
            filled.append(twin)
            print(f"  and PASSED the other side, {twin['selection']}, at "
                  f"{twin['my_pct']:.1f}% (edge {twin.get('edge_pts', 0):+.1f} pts)")
    save(led)
    log_for_clv(done + filled)

    if len(done) == 1:
        r = done[0]
        edge = f", edge {r['edge_pts']:+.1f} pts" if r.get("edge_pts") is not None else ""
        if r.get("ev_pct") is not None:
            edge += f", EV {r['ev_pct']:+.1f}%"
        if status == "bet":
            edge += f", stake ${r['stake']:,.2f} ({r['stake_pct']:g}% of bankroll, quarter-Kelly)"
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


# How many bets may be open at once. It lived only in Ace's header - a
# sentence, honoured or not, with nothing checking. "Max 2 open bets" was a
# rule the agent was asked to keep and no one could tell had been broken.
# ace-verify.py imports this rather than restating it, and a test fails the
# build if the header stops quoting the same number.
MAX_OPEN_BETS = 4

# THE BAR AND THE STAKE (2026-09-28). They replace "8 percentage points over
# the no-vig line" and "1.5% of bankroll, flat", which lived only as
# sentences in Ace's header - arithmetic a model was trusted to do.
#
# The bar is EXPECTED VALUE at the price actually offered, by Ace's own
# estimate: my_pct x decimal_odds - 1. It prices the vig in, and it is the
# same number the closing-line grade uses. +3% is the careful end of the
# +2-3% the research suggested, because Ace's estimates are a model's.
#
# The stake is QUARTER-KELLY - a quarter of the fraction of bankroll the
# Kelly formula gives for that estimate at that price - which is how
# professionals size bets while allowing for their own estimates being wrong.
# It is capped at the hard cap the header always had: a confident estimate at
# plus money can make even quarter-Kelly ask for a tenth of the bankroll.
# Code computes both; Ace supplies only his estimate and his reason.
EV_MIN_PCT = 3.0
KELLY_FRACTION = 0.25
STAKE_CAP_PCT = 3.0

# THE STOP. Down this far from where the bankroll started, `bet` refuses and
# the owner decides. It sat in bankroll.json as stop_loss_pct and
# stop_loss_floor beside unit_pct, max_stake_pct and max_open_bets - five
# settings no code read, two of them (1.5% flat, max 2 open) contradicting
# the rules actually enforced. A number in a data file looks like a setting
# and is not one; `mark` removes them from the live file.
STOP_LOSS_PCT = 15.0
RETIRED_BANKROLL_KEYS = ("unit_pct", "max_stake_pct", "max_open_bets",
                         "stop_loss_pct", "stop_loss_floor")


def stop_floor():
    """The bankroll at which betting stops: starting_bankroll less
    STOP_LOSS_PCT. None if bankroll.json has no starting figure."""
    try:
        start = json.loads((AGENT / "state" / "bankroll.json").read_text()).get("starting_bankroll")
        return round(float(start) * (1 - STOP_LOSS_PCT / 100), 2) if start else None
    except Exception:
        return None


def _clv():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ace_clv", Path(__file__).resolve().parent / "ace-clv.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ev_pct(my_pct, price):
    """Expected return per $1 staked, in percent, at Ace's estimate. None if
    either number is missing or the price is not a price."""
    d = _clv().decimal_odds(price)
    if d is None or my_pct is None:
        return None
    # + 0.0 turns the -0.0 that break-even rounds to into 0.0, which would
    # otherwise print as "-0.0%" beside a bar it exactly misses.
    return round((float(my_pct) / 100 * d - 1) * 100, 1) + 0.0


def kelly_stake(my_pct, price, bankroll):
    """(stake in dollars, percent of bankroll) - quarter-Kelly, capped."""
    d = _clv().decimal_odds(price)
    if d is None or my_pct is None or not bankroll:
        return 0.0, 0.0
    b, p = d - 1, float(my_pct) / 100
    full = (b * p - (1 - p)) / b
    if full <= 0:
        return 0.0, 0.0
    frac = min(KELLY_FRACTION * full, STAKE_CAP_PCT / 100)
    return round(float(bankroll) * frac, 2), round(frac * 100, 2)


def current_bankroll():
    try:
        v = json.loads((AGENT / "state" / "bankroll.json").read_text()).get("bankroll")
        return float(v) if v is not None else None
    except Exception:
        return None

# Passing this many games or fewer by name means Ace studied them, so it must
# say what it estimated. Above this it is sweeping the board with one shared
# reason, where a single number would be a fiction.
ESTIMATE_REQUIRED_UPTO = 2

# How many estimates a cycle has to come home with. Requiring one per studied
# game was unenforceable - nothing can see which files were opened - and a
# sweep of the whole board with `rest` records none at all, which is what a
# week of unanswerable "no bets" was made of.
#
# Three, because the instructions already say to study three or four. Ace
# chooses which three; it is the choosing that makes them the studied ones.
# A slate smaller than three asks only for what is on it.
MIN_ESTIMATES = 3


def estimates_in(ledger):
    """Rows carrying Ace's own probability. The evidence, counted."""
    return [r for r in rows_of(ledger)
            if isinstance(r.get("my_pct"), (int, float))]


def open_bet_count():
    """How many bets are open right now, from the bankroll file."""
    try:
        b = json.loads((AGENT / "state" / "bankroll.json").read_text())
    except Exception:
        return 0
    return len(b.get("open_bets") or [])


def cmd_bet(a):
    # The cap is checked here because this is where a bet becomes a recorded
    # decision. Refusing after the fact, in the verifier, would mean failing a
    # cycle for something it could have been stopped from doing.
    open_now = open_bet_count()
    if open_now >= MAX_OPEN_BETS:
        print(f"{open_now} bets are already open and the cap is "
              f"{MAX_OPEN_BETS}. Grade something before opening another, or "
              f"pass this one.", file=sys.stderr)
        return 1
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
    Ace's own estimate and the gap between them - the edge the 8-point bar is
    measured on. A row passed in a sweep has no estimate, and says so rather
    than showing a gap nobody worked out."""
    head = f"price {r.get('price')}  market {_pct(r.get('novig_pct'))}"
    if r.get("my_pct") is None:
        return head + "  Ace: no estimate (swept)"
    edge = r.get("edge_pts")
    ev = r.get("ev_pct")
    return (head + f"  Ace {_pct(r.get('my_pct'))}  edge "
            + ("?" if edge is None else f"{float(edge):+.1f} pts")
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
        s.set_defaults(fn=fn)

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
