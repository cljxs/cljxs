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
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("MISSION_CONTROL_API", "http://127.0.0.1:3001")
DAILY_DRAFT_CAP = int(os.environ.get("EMILY_DAILY_CAP", "3"))


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


def drafts_today():
    status, rows = call("GET", "/tasks?assignee=emily")
    if status != 200 or not isinstance(rows, list):
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for t in rows if (t.get("created_at") or "").startswith(today))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("idea", help="the approved idea, in a few words")
    ap.add_argument("--brief", default="", help="what the design should feel like")
    ap.add_argument("--product", default="poster", help="poster, tee, mug, tote ...")
    ap.add_argument("--priority", type=int, default=0)
    ap.add_argument("--cost-estimate", type=float, default=0.25)
    ap.add_argument("--force", action="store_true",
                    help="bypass the daily draft cap (an explicit go-ahead)")
    a = ap.parse_args()

    n = drafts_today()
    if n >= DAILY_DRAFT_CAP and not a.force:
        print(f"Emily already has {n} build task(s) queued today "
              f"(cap {DAILY_DRAFT_CAP}). Re-run with --force to override.",
              file=sys.stderr)
        return 1

    slug = slugify(a.idea)
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
