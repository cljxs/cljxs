#!/usr/bin/env python3
"""
emily-new-build.py — approve an idea and hand it to Emily.

This is the bridge the whole agent depends on. The dispatcher only watches the
QUEUE: a folder, an email or a note somewhere is inert. Nothing reaches Emily
until a row exists in tasks/queue.db with assignee="emily". This script writes
that row.

    emily-new-build.py "Cosy Cabin Reading Poster" \
        --brief "warm muted cabin, rain on the window, for book lovers" \
        --product poster

Standard library only.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402

API = os.environ.get("MISSION_CONTROL_API", "http://127.0.0.1:3001")
DAILY_DRAFT_CAP = int(os.environ.get("EMILY_DAILY_CAP", "3"))

# A raised cap for ONE Eastern day, written by emily-cap.py. It carries its
# date and stops applying at midnight Eastern by itself: the alternative - a
# higher EMILY_DAILY_CAP on the service - stays raised until somebody
# remembers to lower it, which is the forgotten-edit failure this repo keeps
# naming.
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
CAP_TODAY = ROOT / "agents" / "emily" / "state" / "cap-today.json"


def daily_cap(today=None):
    """(cap, raised) for today: the raised cap if one was set for today's
    Eastern date, otherwise the standing one."""
    today = today or et_time.day()
    try:
        d = json.loads(CAP_TODAY.read_text())
        if d.get("day") == today and int(d.get("cap")) > 0:
            return int(d["cap"]), True
    except Exception:
        pass
    return DAILY_DRAFT_CAP, False

# Distinct from a failure, so scout-review can say how to override the cap
# for the one idea that hit it, rather than repeat a flag the Deck cannot pass.
CAP_REACHED = 3


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as exc:
        print(f"cannot reach mission-control-api at {API}: {exc}", file=sys.stderr)
        print("is it running?  systemctl status mission-control-api", file=sys.stderr)
        sys.exit(2)


def slugify(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:60]


def started_today(rows, today=None):
    """Emily's build tasks started on today's EASTERN date, whatever became
    of them.

    Every status counts, finished and failed alike: the cap is on spending,
    and a build that finished spent its money on artwork just the same. What
    this used to get wrong was the day. It took the date off a UTC clock, so
    the day rolled over at 8 PM Eastern - three totes approved after 8 PM on
    the 23rd filled the cap for the 24th, and the Deck refused an approval the
    next morning saying three builds were "queued" when none was. The Eastern
    date comes from et_time, like every other date here.

    created_at is SQLite's datetime('now'): UTC with no marker, which
    to_eastern reads as UTC.
    """
    today = today or et_time.day()
    out = []
    for t in rows if isinstance(rows, list) else []:
        when = et_time.to_eastern(str(t.get("created_at") or ""))
        if when is not None and et_time.day(when) == today:
            out.append(t)
    return out


def drafts_today():
    status, rows = call("GET", "/tasks?assignee=emily")
    if status != 200:
        return []
    return started_today(rows)


def task_label(t):
    """'Coastal Tide Botanical Collage Tote (done)' for the cap message."""
    try:
        payload = json.loads(t.get("payload") or "{}") if isinstance(t.get("payload"), str) \
            else (t.get("payload") or {})
    except Exception:
        payload = {}
    return f"{payload.get('idea') or payload.get('title') or 'task ' + str(t.get('id'))} " \
           f"({t.get('status') or '?'})"


def _assets():
    """emily-assets.py as a module - it owns the prompt and the art direction."""
    spec = importlib.util.spec_from_file_location(
        "emily_assets", Path(__file__).resolve().parent / "emily-assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def generate_artwork(slug, idea, brief, product, evidence=None):
    """Make the artwork here, in plain code, before Emily ever wakes.

    Emily was told to run emily-assets.py. She wrote 49-byte text files named
    design.png instead, recorded their byte counts in build.json, and set
    "art_generated": "yes". Fury did the same with its briefing and Scout with
    its log - told to run a tool, wrote a file. Rewording never fixed it once.

    So the step she keeps faking is done for her, from the brief that is
    already in the task. She can still replace it with something better, and
    the verifier's size floor means a hand-written file cannot pass either way.

    Returns (ok, message). A failure here is not fatal: the task is still
    queued, and the verifier will reject the build if no real art appears.
    """
    here = Path(__file__).resolve().parent
    out = here.parent / "agents" / "emily" / "builds" / slug / "design.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    # THE prompt is composed in emily-assets.py, imported rather than built
    # again here. This line used to append its own art direction - 'Flat
    # vector illustration ..., clean edges, no text' - alongside
    # PRINT_DIRECTION, which says the same thing differently. They had
    # already drifted on whether text was allowed.
    ea = _assets()
    prompt = ea.compose(
        idea, brief, product, evidence,
        ea.prior_designs(out.parent.parent, (evidence or {}).get("phrase")))

    try:
        res = subprocess.run(
            [sys.executable, str(here / "emily-assets.py"),
             "--prompt", prompt, "--out", str(out), "--size", "1024",
             # The product decides the art direction, so it has to travel
             # with the prompt. Without it an all-over tote is drawn with
             # PRINT_DIRECTION's plain background and prints a blob in the
             # middle of an even field.
             "--product", str(product or "")],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:
        return False, f"could not run emily-assets.py: {exc}"

    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "unknown error").strip()[:300]

    size = out.stat().st_size if out.is_file() else 0
    mode = "generated" if '"generated"' in res.stdout else (
        "placeholder" if "placeholder" in res.stdout else "unknown")
    if size < 2000:
        return False, f"wrote only {size} bytes - that is not artwork"
    return True, f"{out.name} {size // 1024} KB ({mode})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("idea", help="the approved idea, in a few words")
    ap.add_argument("--brief", default="", help="what the design should feel like")
    ap.add_argument("--product", default="poster", help="poster, tee, mug, tote ...")
    ap.add_argument("--evidence", default="",
                    help="JSON from the approved idea: the measured phrase, "
                         "supply, price band and the market's own tags")
    ap.add_argument("--priority", type=int, default=0)
    ap.add_argument("--cost-estimate", type=float, default=0.25)
    ap.add_argument("--force", action="store_true",
                    help="bypass the daily draft cap (an explicit go-ahead)")
    a = ap.parse_args()

    done_today = drafts_today()
    cap, raised = daily_cap()
    if len(done_today) >= cap and not a.force:
        print(f"Emily has started {len(done_today)} build(s) today "
              f"({et_time.day()}, Eastern) and the daily cap is "
              f"{cap}{' (raised for today)' if raised else ''}:", file=sys.stderr)
        for t in done_today:
            print(f"  - {task_label(t)}", file=sys.stderr)
        print("Finished builds count too - each one paid for its artwork. "
              "The cap resets at midnight Eastern.", file=sys.stderr)
        return CAP_REACHED

    slug = slugify(a.idea)

    evidence = None
    if a.evidence.strip():
        try:
            evidence = json.loads(a.evidence)
        except Exception as exc:
            # Not fatal: a build with no evidence is the old behaviour, and
            # losing the build over a bad argument would be worse. But it is
            # said out loud, because silently unmeasured is how this started.
            print(f"  --evidence did not parse ({exc}); building WITHOUT "
                  f"market direction.", file=sys.stderr)
    if not isinstance(evidence, dict) or not evidence.get("phrase"):
        evidence = None
        print("  no market evidence with this build - the art prompt will "
              "carry no\n  competition, price or tag direction.")

    # Do the art before queueing, so the task Emily receives already has real
    # pixels sitting in its build folder.
    print(f"generating artwork for '{slug}' ...")
    art_ok, art_msg = generate_artwork(slug, a.idea, a.brief, a.product,
                                       evidence)
    print(("  ok: " if art_ok else "  FAILED: ") + art_msg)
    if not art_ok:
        print("  queueing anyway - Emily can run emily-assets.py herself, and "
              "the verifier will reject the build if no real art appears.")

    status, task = call("POST", "/tasks", {
        "created_by": "you",
        "assignee": "emily",
        "type": "product-build",
        "priority": a.priority,
        "cost_estimate": a.cost_estimate,
        "dedupe_key": f"emily-build-{slug}",
        "payload": {
            "idea": a.idea,
            "brief": a.brief,
            "product": a.product,
            "slug": slug,
            "build_dir": f"builds/{slug}",
            "artwork": "already generated at builds/%s/design.png - do NOT create it" % slug
                       if art_ok else "NOT generated - you must run emily-assets.py",
            "evidence": evidence,
            "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        },
        "notes": "DRAFT ONLY - Emily must not publish.",
        "kill_criteria": "Stop if assets cannot be produced, or if the idea "
                         "requires trademarked IP.",
    })

    if status == 201:
        print(f"queued task #{task['id']} for emily -> builds/{slug}")
        print("the dispatcher will pick it up within a couple of seconds")
        return 0
    if status == 409:
        print(f"an open build for '{slug}' already exists - nothing queued", file=sys.stderr)
        return 1
    if status == 429:
        print(f"spend cap hit: {task.get('error')}", file=sys.stderr)
        return 1
    print(f"unexpected response {status}: {task}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
