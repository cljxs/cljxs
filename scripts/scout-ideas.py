#!/usr/bin/env python3
"""
scout-ideas.py merge — fold Scout's new proposals into the idea log.

    python3 scripts/scout-ideas.py merge        # called by scout-cycle.sh
    python3 scripts/scout-ideas.py show

Scout was told, in bold, at line 18 of its own instructions: "WRITE
state/ideas.json - every existing entry kept, yours appended". On
2026-09-18 it wrote "Cleared existing ideas to reflect no new proposals" and
emptied the file. Nothing was lost because nothing was pending, which is luck,
not a safeguard.

It is the second time: the comment in scout-cycle.service records that Scout
destroyed MEMORY.md twice the same way, and the fix there was to take the job
off it. Scout writes a scratch file it may overwrite freely, and code does the
append. Same fix here.

So Scout writes state/proposals.json - only the new ideas, no ids, no status -
and this merges them. Existing entries cannot be lost, because nothing here
removes one. Ids are assigned here too: Scout was computing max(existing)+1,
which is another thing it cannot get wrong if it never does it.

Standard library only.
"""

import importlib.util
import json
import re
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
STATE = ROOT / "agents" / "scout" / "state"
IDEAS = STATE / "ideas.json"
PROPOSALS = STATE / "proposals.json"
SCANS = STATE / "scans"
SCRIPTS = Path(__file__).resolve().parent

_ip = importlib.util.spec_from_file_location("ip_check", SCRIPTS / "ip-check.py")
ipc = importlib.util.module_from_spec(_ip)
_ip.loader.exec_module(ipc)

# How old a measurement may be and still back a proposal. Etsy supply moves;
# a number from six weeks ago is a claim about a market that no longer exists,
# and it would be presented at review with exactly the same confidence as one
# from this morning.
STALE_DAYS = 14

# An inch mark is a quote with more text after it; a quote that really ends a
# JSON string is followed by a comma, brace, bracket or line end. Scout wrote
# `sized for a 3.5" chest print` and closed the string on the inches.
_INCHES = re.compile(r'(\d)"(?=[^,}\]\n]*[^\s,}\]\n])')

KEEP = ("title", "product", "angle", "brief", "evidence")


def load_ideas():
    """The log, always as {"ideas": [...]}. A missing or broken file reads as
    empty rather than raising - but see merge(), which refuses to write over
    a file it could not read."""
    try:
        d = json.loads(IDEAS.read_text())
    except Exception:
        return {"ideas": []}, False
    if isinstance(d, list):
        return {"ideas": d}, True
    d.setdefault("ideas", [])
    return d, True


class ProposalsUnreadable(Exception):
    pass


def load_proposals():
    """This run's new ideas. Raises rather than returning [] on a bad file.

    The first version swallowed every exception and returned an empty list, so
    "Scout wrote nothing", "Scout wrote something I cannot parse" and "Scout
    proposed nothing" were one outcome with one message: "no new proposals".
    Scout proposed three hoodie ideas, the file did not parse, and the run
    reported a quiet pass. Three ideas lost inside an except clause written
    while fixing this exact class of bug elsewhere.
    """
    if not PROPOSALS.is_file():
        return []                      # a run that proposed nothing. Fine.
    raw = PROPOSALS.read_text()
    if not raw.strip():
        return []
    try:
        d = json.loads(raw)
    except Exception as exc:
        # One repair, because a model writing measurements into JSON will do
        # this again and the ideas are perfectly good. Loud, and only when it
        # actually yields valid JSON - a guess that parses is still a guess,
        # so the file is rewritten so you can see exactly what was changed.
        mended = _INCHES.sub(r'\1 inch', raw)
        if mended != raw:
            try:
                d = json.loads(mended)
                PROPOSALS.write_text(mended)
                print(f"NOTE: {PROPOSALS.name} did not parse - an inch mark had "
                      f"closed a string early.\n"
                      f"      Rewrote 3.5\" as 3.5 inch and it parses now. The "
                      f"ideas are unchanged otherwise.", file=sys.stderr)
                return _rows_of(d)
            except Exception:
                pass
        raise ProposalsUnreadable(
            f"{PROPOSALS} does not parse ({exc}).\n"
            f"  It holds this run's ideas and they are not in the log yet, so "
            f"they are still there to recover:\n"
            f"  {raw.strip()[:300]}") from exc

    return _rows_of(d)


def _rows_of(d):
    rows = d if isinstance(d, list) else (d.get("proposals") or d.get("ideas") or [])
    if not isinstance(rows, list):
        raise ProposalsUnreadable(
            f"{PROPOSALS} parsed, but the ideas are not a list - found "
            f"{type(rows).__name__}. Expected a JSON list of "
            f'{{"title": ..., "product": ...}}.')

    good = [r for r in rows if isinstance(r, dict) and str(r.get("title") or "").strip()]
    if rows and not good:
        raise ProposalsUnreadable(
            f"{PROPOSALS} has {len(rows)} entr(ies), none with a title. "
            f"Every idea needs one - it is what the log is keyed on.")
    return good


def cmd_merge():
    existing, readable = load_ideas()
    if IDEAS.is_file() and not readable:
        print(f"{IDEAS} does not parse. Not touching it - a merge into a file "
              f"I cannot read would destroy whatever is in there.", file=sys.stderr)
        return 1

    try:
        new = load_proposals()
    except ProposalsUnreadable as exc:
        print(f"scout-ideas: {exc}", file=sys.stderr)
        print(f"\n  Nothing was merged and nothing was lost. Fix the file and "
              f"re-run:\n    python3 scripts/scout-ideas.py merge", file=sys.stderr)
        return 1
    if not new:
        print(f"no new proposals ({len(existing['ideas'])} idea(s) in the log, unchanged)")
        return 0

    have = {str(i.get("title", "")).strip().lower() for i in existing["ideas"]}
    next_id = max([int(i.get("id") or 0) for i in existing["ideas"]] or [0]) + 1

    added, skipped = [], []
    for row in new:
        title = str(row["title"]).strip()
        if title.lower() in have:
            skipped.append(title)
            continue
        entry = {"id": next_id, "status": "pending",
                 "proposed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        entry.update({k: row.get(k) for k in KEEP if row.get(k) is not None})
        entry["title"] = title
        existing["ideas"].append(entry)
        have.add(title.lower())
        added.append(f"{next_id}  {title}")
        next_id += 1

    IDEAS.parent.mkdir(parents=True, exist_ok=True)
    IDEAS.write_text(json.dumps(existing, indent=1) + "\n")
    # Scout owns this file and may overwrite it; emptying it here means a rerun
    # cannot add the same ideas twice.
    PROPOSALS.write_text("[]\n")

    for line in added:
        print(f"  + {line}")
    for t in skipped:
        print(f"  = already proposed: {t}")
    print(f"{len(added)} added, {len(existing['ideas'])} idea(s) in the log")
    return 0


def scans():
    """Every saved scan, newest first."""
    out = []
    for f in sorted(SCANS.glob("*.json")) if SCANS.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue                      # a broken scan is not a crash here
        if isinstance(d, dict) and isinstance(d.get("rows"), list):
            out.append(d)
    return sorted(out, key=lambda d: str(d.get("scanned_at") or ""), reverse=True)


def age_days(stamp):
    try:
        when = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


def measured(phrase):
    """(row, scan, problem) for a phrase - the newest measurement of it.

    THIS IS THE WHOLE POINT. Scout does not type a supply figure or a
    favourites rate; it names a phrase that has been measured and the numbers
    are copied from disk. A number that is never typed cannot be invented,
    and an idea with no measurement behind it cannot be filed at all.
    """
    want = (phrase or "").strip().lower()
    if not want:
        return None, None, "no phrase given"
    if not scans():
        return None, None, (
            "nothing has been measured yet. Run a scan first:\n"
            "    python3 ../../scripts/market-scan.py scan <phrase> --save")
    for scan in scans():
        for row in scan["rows"]:
            if str(row.get("phrase", "")).strip().lower() == want:
                old = age_days(scan.get("scanned_at"))
                if old is None:
                    # An undateable scan used to be treated as never stale,
                    # so a file with a malformed timestamp would back
                    # proposals forever. An unknown age is not a young one.
                    return None, scan, (
                        f"the scan holding {phrase!r} has no readable date "
                        f"({scan.get('scanned_at')!r}),\n  so there is no "
                        f"telling how old its numbers are. Re-scan it:\n"
                        f"    python3 ../../scripts/market-scan.py scan "
                        f"{scan.get('seed')} --save")
                if old > STALE_DAYS:
                    return None, scan, (
                        f"the only measurement of {phrase!r} is {old:.0f} days "
                        f"old, and supply moves. Re-scan it:\n"
                        f"    python3 ../../scripts/market-scan.py scan "
                        f"{scan.get('seed')} --save")
                return row, scan, None
        for row in scan.get("excluded") or []:
            if str(row.get("phrase", "")).strip().lower() == want:
                pct = row.get("match")
                return None, scan, (
                    f"{phrase!r} was left out of its own ranking: only "
                    f"{(pct or 0) * 100:.0f}% of the listings Etsy returned "
                    f"contain it, so its numbers describe a different market.")
    return None, None, (
        f"{phrase!r} has not been measured. Propose only phrases that have:\n"
        f"    python3 ../../scripts/scout-ideas.py evidence")


def cmd_evidence(argv):
    """Everything Scout is allowed to propose, and what it is worth."""
    found = scans()
    if not found:
        print("no scans on disk. Nothing can be proposed until something is "
              "measured:\n  python3 scripts/market-scan.py scan <phrase> --save")
        return 1
    for scan in found:
        old = age_days(scan.get("scanned_at"))
        stale = " STALE" if old is not None and old > STALE_DAYS else ""
        print(f"\n  {scan.get('seed')}  ({old:.0f} days ago{stale})"
              if old is not None else f"\n  {scan.get('seed')}")
        for row in scan["rows"]:
            score = row.get("score")
            dead = "   DEAD - nothing here is being saved" if not score else ""
            price = row.get("price")
            print(f"      {str(row.get('phrase'))[:34]:<36}"
                  f"{row.get('supply') or 0:>9,} listings  "
                  f"{('$%.2f' % price) if price is not None else '    ?':>7} median  "
                  f"score {score or 0:.4f}{dead}")
    print(f"\n  Propose one by name:\n"
          f"    scout-ideas.py propose --phrase \"<one of the above>\" "
          f"--title ... --product ...")
    return 0


def cmd_propose(argv):
    """Add one idea from arguments, so no model ever types JSON here.

        scout-ideas.py propose --title "Minimalist Mountain Badge" \
          --product hoodie --angle "..." --brief "..."

    Same reason ace-judge.py exists: a selection that is never typed cannot be
    mistyped. Scout wrote a good brief containing 3.5" and broke the file on
    the inches - not a reasoning failure, a quoting one, and quoting is exactly
    what a script should own.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="scout-ideas.py propose")
    ap.add_argument("--phrase", required=True,
                    help="a phrase that market-scan.py has measured")
    ap.add_argument("--title", required=True)
    ap.add_argument("--product", required=True)
    ap.add_argument("--angle", default="")
    ap.add_argument("--brief", default="")
    a = ap.parse_args(argv)

    # 1. IS IT SOMEBODY ELSE'S? Checked before anything else, and on the
    # title and angle too - the phrase can be clean while the idea built on
    # it is 'fall sticker, Pikmin style'. Sixteen of 229 searches in the
    # first real sweep were a trademark.
    for field, text in (("phrase", a.phrase), ("title", a.title),
                        ("angle", a.angle), ("brief", a.brief)):
        tier, what, why = ipc.risky(text)
        if tier == "blocked":
            print(f"REFUSED - the {field} is somebody else's property.\n"
                  f"  {what}: {why}\n"
                  f"  Nothing was written. Propose something that is ours to "
                  f"make.", file=sys.stderr)
            return 2

    # 2. HAS IT BEEN MEASURED? An idea with no measurement behind it is the
    # model's opinion wearing a proposal's clothes.
    row, scan, problem = measured(a.phrase)
    if problem:
        print(f"REFUSED - {problem}\n\n  Nothing was written.", file=sys.stderr)
        return 2

    # 3. IS THE MARKET ALIVE? A phrase whose top listings gain no favourites
    # a day is not a gap waiting to be filled; it is a place people have
    # already tried. 'fall sticker pack' had 5,536 listings and a score of
    # zero. Refusing costs one idea; building into it costs a build.
    if not row.get("score"):
        print(f"REFUSED - {a.phrase!r} was measured and scored zero: "
              f"{row.get('supply') or 0:,} active\n"
              f"  listings and not one favourite a day across the top "
              f"{row.get('returned') or 25}.\n"
              f"  People are already listing into it and nobody is saving the "
              f"results.\n\n  Nothing was written.", file=sys.stderr)
        return 2

    tier, what, why = ipc.risky(a.phrase + " " + a.title)
    rows = []
    if PROPOSALS.is_file() and PROPOSALS.read_text().strip():
        try:
            rows = _rows_of(json.loads(PROPOSALS.read_text()))
        except Exception:
            print(f"{PROPOSALS} does not parse, so this would overwrite ideas "
                  f"already in it. Fix or delete it first.", file=sys.stderr)
            return 1

    entry = {"title": a.title.strip(), "product": a.product.strip()}
    if a.angle.strip():
        entry["angle"] = a.angle.strip()
    if a.brief.strip():
        entry["brief"] = a.brief.strip()

    # The numbers are COPIED, never typed. This is the field the user reads
    # at review, and Scout has no way to put a different number in it.
    entry["evidence"] = {
        "phrase": row.get("phrase"), "supply": row.get("supply"),
        "favs_per_day": row.get("heat"), "favs_per_view": row.get("pull"),
        "match": row.get("match"), "score": row.get("score"),
        "typical_price": row.get("price"),
        "tags": row.get("tags") or [],
        "measured_at": scan.get("scanned_at"), "from_scan": scan.get("seed"),
    }
    if tier == "check":
        entry["evidence"]["ip_flag"] = f"{what}: {why}"
    rows.append(entry)

    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS.write_text(json.dumps({"proposals": rows}, indent=1) + "\n")
    ev = entry["evidence"]
    print(f"proposed: {entry['title']} ({entry['product']})")
    print(f"  evidence: {ev['phrase']!r} - {ev['supply']:,} listings, "
          f"{ev['favs_per_day']:.3f} favs/day,")
    print(f"            {(ev['favs_per_view'] or 0):.4f} favs/view, score "
          f"{ev['score']:.4f}, measured {ev['measured_at']}")
    if entry["evidence"].get("ip_flag"):
        print(f"  FLAGGED for a human: {entry['evidence']['ip_flag']}")
    print(f"  [{len(rows)} waiting to merge]")
    return 0


def cmd_show():
    d, _ = load_ideas()
    for i in d["ideas"]:
        print(f"  [{i.get('status','pending')}] {i.get('id')}  {i.get('title')} "
              f"({i.get('product')})")
    print(f"{len(d['ideas'])} idea(s)")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "merge":
        return cmd_merge()
    if cmd == "propose":
        return cmd_propose(sys.argv[2:])
    if cmd == "show":
        return cmd_show()
    if cmd == "evidence":
        return cmd_evidence(sys.argv[2:])
    print("usage: scout-ideas.py propose|merge|show|evidence", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
