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
    ace-judge.py pass 3 --my-pct 58.0 --why "gap 2.1 pts, need 8+"
    ace-judge.py pass 1-6,9 --why "no edge on the no-vig line"   one call, seven rows
    ace-judge.py rest --why "did not clear 8 points"             sweeps the remainder
    ace-judge.py bet  7 --my-pct 71.0 --stake 150 --why "SP scratched an hour ago"
    ace-judge.py verdict "No picks - 6 judged, nothing cleared 8 points."
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
    if status == "bet":
        if len(nums) > 1:
            print("bet one at a time. A stake is a decision per game, and the flat "
                  "1.5% rule means they are not interchangeable.", file=sys.stderr)
            return 1
        if a.stake is None:
            print("a bet needs --stake.", file=sys.stderr)
            return 1

    led = load_ledger()
    done = []
    for n in nums:
        src = rows[n - 1]
        row = {k: src.get(k) for k in CARRY}
        row["my_pct"] = a.my_pct
        novig = src.get("novig_pct")
        if a.my_pct is not None and novig is not None:
            row["edge_pts"] = round(float(a.my_pct) - float(novig), 1)
        row["why_not"] = [a.why]
        row["status"] = status
        if status == "bet":
            row["stake"] = float(a.stake)
        led["candidates"] = [r for r in led.get("candidates", []) if key(r) != key(row)]
        led["candidates"].append(row)
        done.append(row)
    save(led)

    if len(done) == 1:
        r = done[0]
        edge = f", edge {r['edge_pts']:+.1f} pts" if r.get("edge_pts") is not None else ""
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


def cmd_bet(a):
    return record(a, "bet")


def cmd_rest(a):
    """Judge every remaining candidate with one shared reason.

    Only for a reason that is honestly true of all of them - "did not clear the
    bar on the no-vig line" is; "read the context file" is not. Games you never
    actually looked at are better left out: the board marks those `unjudged` on
    its own, which tells the user what was skipped."""
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
    bank.write_text(json.dumps(b, indent=1) + "\n")

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


def cmd_show(a):
    led = load_ledger()
    rows = led.get("candidates", [])
    print(f"{LEDGER}")
    print(f"  day {led.get('day')}  slot {led.get('slot')}")
    hint = "(not set - run: ace-judge.py verdict 'one line on the slate')"
    print(f"  verdict: {led.get('verdict') or hint}")
    for r in rows:
        print(f"  [{r.get('status')}] {r.get('selection')} / {r.get('match')} "
              f"- {'; '.join(r.get('why_not') or [])}")
    print(f"  {len(rows)} rows")
    return 0


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
