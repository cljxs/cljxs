#!/usr/bin/env python3
"""
ace-verify.py — did the cycle actually do the work?

Ace ran a scheduled cycle in 23 seconds, reported no error, and left
bankroll.json untouched. systemd saw exit 0. There is no way, after the fact,
to tell that run apart from one that read six context files and passed on all
of them — which is the outcome Ace is *supposed* to have most days. That is the
whole problem: when "did nothing" and "correctly decided to do nothing" look
identical, a broken agent can sit there for weeks.

So this names which deliverable is missing, every cycle.

The ledger join check is the one that cannot be eyeballed. The console keys
rows on `selection|match` lowercased; if Ace paraphrases a selection instead of
copying it, the merge silently drops to zero matches and the board shows every
game as `unjudged` while Ace's report insists it judged them all. Nobody would
notice from the UI.

Usage:  ace-verify.py <memory-bytes-before> <cycle-count-before> <run-started-epoch>

Standard library only.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Same two functions the fetcher uses, so the name this demands and the
# name Ace was handed can never disagree.
import et_time  # noqa: E402
import remember  # noqa: E402
from et_time import eastern_now  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENT = ROOT / "agents" / "ace"
BANK = AGENT / "state" / "bankroll.json"
LEDGER = AGENT / "state" / "ledger.json"
CANDIDATES = AGENT / "data" / "candidates.json"

TOLERANCE = 0.01


def num(v, default=0.0):
    try:
        return float(str(v).replace("$", "").replace(",", "").lstrip("+"))
    except Exception:
        return default


def key(row):
    """Exactly what mission-control-api/ace.js joins on. Kept identical on
    purpose - a verifier that computes the key its own way would pass rows the
    console then fails to match."""
    return f"{str(row.get('selection') or '').lower()}|{str(row.get('match') or '').lower()}"


def rows_of(doc):
    if isinstance(doc, list):
        return doc
    return (doc or {}).get("candidates") or []


def reconcile(b, problems):
    """Money staked is money out of the bankroll until the bet settles.

        starting - staked on open bets - staked on settled + returned == bankroll

    A settled bet may record its outcome as `profit` (signed) or as `payout`
    (gross return including stake). Either is fine; neither, and the check
    cannot run, which is itself worth saying out loud rather than passing."""
    start = num(b.get("starting_bankroll"), 10000.0)
    bank = num(b.get("bankroll"))
    open_stake = sum(num(x.get("stake")) for x in b.get("open_bets") or [])

    settled_stake = returned = 0.0
    unreadable = []
    for s in b.get("settled_bets") or []:
        stake = num(s.get("stake"))
        settled_stake += stake
        if s.get("profit") is not None:
            returned += stake + num(s.get("profit"))
        elif s.get("payout") is not None:
            returned += num(s.get("payout"))
        elif str(s.get("result") or "").lower() in ("loss", "lost", "l"):
            returned += 0.0
        elif str(s.get("result") or "").lower() in ("push", "void", "cancelled"):
            returned += stake
        else:
            unreadable.append(s.get("selection") or s.get("match") or "a settled bet")

    if unreadable:
        print(f"  note: {len(unreadable)} settled bet(s) record no profit, payout or "
              f"recognisable result, so the bankroll cannot be checked against them: "
              f"{', '.join(str(u) for u in unreadable[:3])}")
        return

    expected = start - open_stake - settled_stake + returned
    gap = bank - expected
    if abs(gap) > TOLERANCE:
        problems.append(
            f"THE BANKROLL DOES NOT BALANCE. starting ${start:,.2f} - ${open_stake:,.2f} "
            f"at risk - ${settled_stake:,.2f} staked on settled + ${returned:,.2f} "
            f"returned = ${expected:,.2f}, but bankroll reads ${bank:,.2f}. That is "
            f"${gap:+,.2f} unaccounted for.")


def check_ledger(problems, notes, started):
    if not LEDGER.exists():
        problems.append("no state/ledger.json - the shadow ledger is the most useful "
                        "thing Ace produces, because the passes are the job. Without it "
                        "the console shows every game as unjudged.")
        return
    if started and LEDGER.stat().st_mtime < started:
        problems.append("state/ledger.json is left over from an earlier run, not "
                        "written by this one")
        return
    try:
        ledger = json.loads(LEDGER.read_text())
    except Exception as exc:
        problems.append(f"state/ledger.json does not parse ({exc}). A file the console "
                        f"cannot read shows the user nothing. Prices go in as plain "
                        f"numbers - 104, never +104, which is not valid JSON.")
        return

    # A bare JSON array is a valid ledger of rows but carries no day, slot or
    # verdict - and calling .get() on it is what crashed this verifier. rows_of
    # already tolerated both shapes; the metadata read below did not.
    meta = ledger if isinstance(ledger, dict) else {}
    if not isinstance(ledger, dict):
        notes.append("ledger.json is a bare array, so it has no day, slot or verdict "
                     "line - the village shows the verdict, so write it with "
                     "`ace-judge.py verdict \"...\"`, which produces the right shape")

    judged = rows_of(ledger)
    if not judged:
        problems.append("state/ledger.json has no candidates - a cycle that looked at "
                        "nothing is not a pass, it is a cycle that did not run")
        return

    try:
        base = rows_of(json.loads(CANDIDATES.read_text()))
    except Exception:
        notes.append(f"ledger has {len(judged)} rows (no candidates.json to join against)")
        return

    base_keys = {key(r) for r in base}
    matched = [r for r in judged if key(r) in base_keys]
    orphans = [r for r in judged if key(r) not in base_keys]
    if not matched and base_keys:
        problems.append(
            f"NONE of Ace's {len(judged)} ledger rows match the {len(base_keys)} rows in "
            f"candidates.json. The console joins on selection|match and will show every "
            f"game as unjudged. Copy `selection` and `match` exactly as the fetcher "
            f"wrote them. First mismatch: {judged[0].get('selection')!r} / "
            f"{judged[0].get('match')!r}")
    elif orphans:
        notes.append(f"{len(orphans)} ledger row(s) match nothing in candidates.json, "
                     f"e.g. {orphans[0].get('selection')!r} - those will not appear on "
                     f"the board")

    missing_why = [r for r in judged if not r.get("why_not") and r.get("status") != "bet"]
    if missing_why:
        notes.append(f"{len(missing_why)} passed row(s) carry no why_not reason - "
                     f"the reason is the point of the row")

    if not str(meta.get("verdict") or "").strip():
        notes.append("ledger has no verdict line - that is the sentence the village shows")

    notes.append(f"ledger: {len(judged)} judged, {len(matched)} joined to the board, "
                 f"{len(base_keys) - len(matched)} left unjudged")


def expected_report(agent_name, agent_dir):
    """The report filename this cycle owes, taken from the fetcher's _meta.json
    where it exists so both sides read one value, computed only as a fallback."""
    # One answer, from et_time, so this can never disagree with signoff.py.
    name, slot = et_time.expected_report(agent_dir, agent_name)
    return agent_dir / "reports" / name, slot


def main():
    mem_before = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    cycles_before = int(sys.argv[2]) if len(sys.argv) > 2 else -1
    started = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    problems, notes = [], []

    try:
        b = json.loads(BANK.read_text())
    except FileNotFoundError:
        print(f"ace-verify: {BANK} does not exist. Ace has no bankroll to read, so "
              f"there is nothing for a cycle to update. Seed it:\n"
              f"  cp -n {AGENT}/state/bankroll.seed.json {BANK}\nFAILED")
        return 1
    except Exception as exc:
        print(f"ace-verify: cannot read {BANK} ({exc}). FAILED")
        return 1

    reconcile(b, problems)

    cycles = int(num(b.get("cycle_count")))
    if cycles_before >= 0 and cycles <= cycles_before:
        problems.append(f"cycle_count did not advance ({cycles_before} -> {cycles}) "
                        "- nothing confirms the cycle reached the end")
    if started and BANK.stat().st_mtime < started:
        age = int(time.time() - BANK.stat().st_mtime)
        problems.append(f"state/bankroll.json was last written {age}s ago, before this "
                        "cycle started - this run did not touch it")

    check_ledger(problems, notes, started)

    # Prefer the name the fetcher already stamped. Computing it again here
    # would usually agree - but a run started near a slot boundary (a manual
    # test at 11:59 ET, say) would be handed "morning" and judged against
    # "afternoon", and the agent would be failed for obeying its instructions.
    report, want = expected_report("ace", AGENT)
    if not report.exists():
        # Everything already filed for this date, whatever slot it claims -
        # that is how the wrongly-named report gets surfaced rather than just
        # reported missing.
        others = sorted(x.name for x in (AGENT / "reports").glob(f"{report.name[:10]}-*.md")) \
            if (AGENT / "reports").is_dir() else []
        extra = f" (found {', '.join(others)})" if others else ""
        hint = f" - the {want} slot files as `-{want}.md`" if want else ""
        problems.append(f"no reports/{report.name} for this wake{extra}{hint}")
    elif started and report.stat().st_mtime < started:
        problems.append(f"reports/{report.name} is left over from an earlier run")
    else:
        words = len(report.read_text().split())
        if words < 60:
            problems.append(f"reports/{report.name} is {words} words - too short to "
                            "explain a single pass, let alone all of them")
        else:
            notes.append(f"report {report.name}, {words}w")

    memory = AGENT / "MEMORY.md"
    mem_after = memory.stat().st_size if memory.exists() else 0
    # Not byte growth: a trim at the 2KB cap shrinks the file while recording
    # perfectly well, and that turned a clean cycle into a reported failure.
    # remember.written_this_cycle is the one rule signoff.py uses too.
    mem_ok, mem_why = remember.written_this_cycle(memory, started)
    if not mem_ok:
        problems.append(mem_why)

    if problems:
        print("ace-verify: FAILED - the cycle did not produce its deliverables")
        for p in problems:
            print(f"  - {p}")
        for n in notes:
            print(f"  note: {n}")
        print("\nPassing on every game is a good day. Writing nothing is not the same "
              "thing, and this is the check that tells them apart.")
        return 1

    bank = num(b.get("bankroll"))
    start = num(b.get("starting_bankroll"), 10000.0)
    print(f"ace-verify: PASS - cycle {cycles}, bankroll ${bank:,.2f} "
          f"({(bank / start - 1) * 100:+.2f}%), {len(b.get('open_bets') or [])} open, "
          f"memory +{mem_after - mem_before}B")
    for n in notes:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    # A crash here is worse than a failed check: systemd shows exit 1 either
    # way, but a traceback hides every other result the cycle produced. This
    # one hid a whole run behind `'list' object has no attribute 'get'`.
    try:
        sys.exit(main())
    except Exception:
        import traceback
        print("ace-verify: CRASHED - the checks did not complete, so nothing below "
              "was verified. This is a bug in the verifier, not in the cycle.")
        traceback.print_exc()
        sys.exit(1)
