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
    ace-judge.py bet  7 --my-pct 71.0 --stake 150 --why "SP scratched an hour ago"
    ace-judge.py verdict "No picks - 6 judged, nothing cleared 8 points."
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


def load_ledger():
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:
            pass
    now = et_time.eastern_now()
    return {"day": et_time.day(now), "slot": et_time.slot("ace", now),
            "verdict": None, "candidates": []}


def save(led):
    led["asof_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, indent=1) + "\n")


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
    n = a.number
    if not 1 <= n <= len(rows):
        print(f"there is no candidate {n} - the list has {len(rows)}. "
              f"Run `ace-judge.py list` to see them.", file=sys.stderr)
        return 1
    src = rows[n - 1]

    if not a.why:
        print("--why is required. The reason IS the row - a verdict with no reason "
              "tells the user nothing about why you passed.", file=sys.stderr)
        return 1

    row = {k: src.get(k) for k in CARRY}
    row["my_pct"] = a.my_pct
    novig = src.get("novig_pct")
    if a.my_pct is not None and novig is not None:
        row["edge_pts"] = round(float(a.my_pct) - float(novig), 1)
    row["why_not"] = [a.why]
    row["status"] = status
    if status == "bet":
        if a.stake is None:
            print("a bet needs --stake.", file=sys.stderr)
            return 1
        row["stake"] = float(a.stake)

    led = load_ledger()
    led["candidates"] = [r for r in led.get("candidates", []) if key(r) != key(row)]
    led["candidates"].append(row)
    save(led)

    edge = f", edge {row['edge_pts']:+.1f} pts" if row.get("edge_pts") is not None else ""
    print(f"{status.upper()}: {row['selection']} ({row['match']}) at {row['price']}"
          f"{edge} - {a.why}")
    print(f"ledger now has {len(led['candidates'])} judged rows.")
    return 0


def cmd_pass(a):
    return record(a, "passed")


def cmd_bet(a):
    return record(a, "bet")


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
        s.add_argument("number", type=int)
        s.add_argument("--my-pct", type=float, dest="my_pct")
        s.add_argument("--why", default="")
        s.add_argument("--stake", type=float)
        s.set_defaults(fn=fn)

    v = sub.add_parser("verdict")
    v.add_argument("text")
    v.set_defaults(fn=cmd_verdict)

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
