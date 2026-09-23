#!/usr/bin/env python3
"""
scout-review.py — you approve or reject Scout's ideas.

This is the gate between Scout and Emily. Scout only ever writes proposals
into agents/scout/state/ideas.json. Nothing reaches Emily until you approve
it here, and approving is what creates Emily's queue task.

    scout-review.py list
    scout-review.py approve 3
    scout-review.py reject 4 "too close to a licensed character"

Rejections are written into Emily's lessons log so Scout stops proposing the
same direction.

Standard library only.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ECOSYSTEM_ROOT overrides the root and nothing here hardcodes a path - this
# file was the exception, and it meant scout-review.py read a different
# ideas.json from the one scout-ideas.py had just written whenever the
# variable was set.
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
IDEAS = ROOT / "agents" / "scout" / "state" / "ideas.json"
LESSONS = ROOT / "agents" / "emily" / "state" / "lessons.md"
NEW_BUILD = ROOT / "scripts" / "emily-new-build.py"


def load():
    if not IDEAS.is_file():
        print(f"No ideas yet ({IDEAS} does not exist).\n"
              "Scout writes it on its first run.", file=sys.stderr)
        sys.exit(1)
    d = json.loads(IDEAS.read_text())
    return d if isinstance(d, dict) else {"ideas": d}


def save(d):
    IDEAS.write_text(json.dumps(d, indent=1) + "\n")


def note_lesson(line):
    LESSONS.parent.mkdir(parents=True, exist_ok=True)
    if not LESSONS.is_file():
        LESSONS.write_text("# Lessons — what the user approved and rejected, and why\n\n")
    with LESSONS.open("a") as f:
        f.write(line.rstrip() + "\n")


def find(d, ident):
    for i in d.get("ideas", []):
        if str(i.get("id")) == str(ident):
            return i
    print(f"No idea with id {ident}. Run: scout-review.py list", file=sys.stderr)
    sys.exit(1)


def show_evidence(ev):
    """The measurement behind an idea, or the absence of one.

    This is the line the approval turns on. Before it existed, every
    proposal read the same whether it came from a market scan or from the
    model's own head - and the second kind is fiction, which is worse than
    no researcher because fiction with a confident tone gets built.

    An idea with no evidence is NOT hidden. It is shown saying so, because
    the old ideas in the log predate the requirement and quietly dropping
    them would look like they had been measured and passed.
    """
    if not isinstance(ev, dict) or not ev.get("phrase"):
        print(f"      evidence: NONE - proposed before measurements were "
              f"required, or by hand.")
        print(f"                Nothing here has been checked against Etsy.")
        return
    supply = ev.get("supply")
    print(f"      evidence: '{ev.get('phrase')}'")
    print(f"                {supply:,} active listings"
          if isinstance(supply, int) else "                supply unknown")
    heat, pull = ev.get("favs_per_day"), ev.get("favs_per_view")
    if heat is not None:
        print(f"                {heat:.3f} favourites/day across the top "
              f"results (a rate, not a count)")
    if pull is not None:
        print(f"                {pull:.4f} favourites/view - how often a "
              f"looker saves one")
    match = ev.get("match")
    if match is not None:
        print(f"                {match * 100:.0f}% of those listings really "
              f"contain the phrase")
    price = ev.get("typical_price")
    if price is not None:
        print(f"                {price:.2f} typical price")
    print(f"                measured {ev.get('measured_at')} "
          f"(scan: {ev.get('from_scan')})")
    if ev.get("ip_flag"):
        print(f"      IP FLAG : {ev['ip_flag']}")
        print(f"                Not refused - an ordinary word that is also a "
              f"property. Your call.")


def cmd_list(a, d):
    ideas = d.get("ideas", [])
    pending = [i for i in ideas if i.get("status", "pending") == "pending"]
    if not pending and not a.all:
        print("No pending ideas. Scout proposes on its next run.")
        return
    show = ideas if a.all else pending
    for i in show:
        st = i.get("status", "pending")
        mark = {"pending": " ", "approved": "✓", "rejected": "✗"}.get(st, "?")
        print(f"\n[{mark}] {i.get('id')}  {i.get('title')}")
        print(f"      product : {i.get('product')}")
        print(f"      angle   : {i.get('angle','')[:110]}")
        if i.get("brief"):
            print(f"      brief   : {i['brief'][:110]}")
        show_evidence(i.get("evidence"))
        if st != "pending":
            print(f"      {st} — {i.get('verdict_reason','')[:90]}")
    print(f"\n{len(pending)} pending. Approve with: scout-review.py approve <id>")


def cmd_approve(a, d):
    idea = find(d, a.id)
    if idea.get("status") == "approved":
        print("Already approved — not queueing twice.", file=sys.stderr)
        return 1
    cmd = [sys.executable, str(NEW_BUILD), idea["title"],
           "--brief", idea.get("brief") or idea.get("angle", ""),
           "--product", idea.get("product", "poster")]
    # THE EVIDENCE GOES WITH IT. Approval used to pass title, brief and
    # product and stop there, so Emily never learned the phrase she was
    # building for, what the market charges, or how many listings she was
    # competing with. Everything measured died at this line.
    if isinstance(idea.get("evidence"), dict) and idea["evidence"].get("phrase"):
        cmd += ["--evidence", json.dumps(idea["evidence"])]
    if a.force:
        cmd.append("--force")
    print("queueing to emily:", " ".join(cmd[2:5]), "...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        print("\nEmily was NOT queued. The idea stays pending.", file=sys.stderr)
        return r.returncode
    idea["status"] = "approved"
    idea["verdict_reason"] = a.reason or "approved by you"
    idea["verdict_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    save(d)
    note_lesson(f"- APPROVED {idea['title']!r} ({idea.get('product')}) — {idea['verdict_reason']}")
    print("approved, queued, and logged to Emily's lessons.")
    return 0


def cmd_reject(a, d):
    idea = find(d, a.id)
    idea["status"] = "rejected"
    idea["verdict_reason"] = a.reason
    idea["verdict_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    save(d)
    note_lesson(f"- REJECTED {idea['title']!r} ({idea.get('product')}) — {a.reason}")
    print(f"rejected and logged. Scout reads this before proposing again.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Approve or reject Scout's ideas")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("approve"); p.add_argument("id")
    p.add_argument("--reason", default=""); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("reject"); p.add_argument("id"); p.add_argument("reason")
    p.set_defaults(fn=cmd_reject)

    a = ap.parse_args()
    return a.fn(a, load()) or 0


if __name__ == "__main__":
    sys.exit(main())
