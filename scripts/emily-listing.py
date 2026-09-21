#!/usr/bin/env python3
"""
emily-listing.py — write Emily's two JSON files, so she never hand-writes JSON.

    emily-listing.py listing builds/<slug> --title "..." --description "..."
                     --tag fall --tag autumn --product sticker --price 5.99

    emily-listing.py build builds/<slug> --status ready_local --art generated

THE INCIDENT. A finished sticker sat at LOCAL ONLY for two days because
listing.json read

    {"title": "Maple Leaf Pocket Sticker" "product_type": "sticker", ...}

- one missing comma. Emily wrote the same broken file on two separate
attempts, the verifier rejected both, the drafter never got past reading it,
and the gallery showed a badge with no reason. The artwork was fine. The
listing copy was fine. A comma cost the build.

A model asked to emit JSON will eventually emit nearly-JSON, the way a model
asked to do arithmetic will eventually claim it did. So it stops being asked.
Emily passes strings on a command line and json.dumps does the quoting, the
escaping and the commas - none of which needed judgement, and all of which
she was being asked to get exactly right by hand, every cycle, forever.

THE RULES ARE NOT RESTATED HERE. What a listing must be is
emily-verify.py's business, because that is the file that decides whether a
build is finished. This imports listing_problems() from it and refuses to
write anything that would fail. Two copies of "13 tags" drift, and the
direction they drift in is a writer that cheerfully produces what the
verifier then fails.

Standard library only.
"""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location(
    "emily_verify", SCRIPTS / "emily-verify.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

# The statuses Emily is allowed to set. `ready_for_review` is not one of them:
# it means a Printify draft exists, and only emily-finish.py knows whether one
# does. She set it by hand once on a build that had no product.
HER_STATUSES = ("in_progress", "ready_local")

# Written by code that is not Emily, and preserved when she rewrites the file.
KEPT = ("printify_product_id", "published", "published_checked_utc", "etsy_url",
        "price_low", "price_high", "mockup_count", "draft_blocked")


def build_dir(raw):
    """The same resolver the verifier uses, for the same reason.

    build_dir in a task payload is "builds/<slug>", relative to EMILY'S
    workspace rather than to the ecosystem root, and she may equally well type
    a bare slug because she is standing in that workspace.
    """
    agent = ROOT / "agents" / "emily"
    for c in ([Path(raw)] if Path(raw).is_absolute() else
              [agent / raw, agent / "builds" / raw, ROOT / raw, Path(raw)]):
        if c.is_dir():
            return c
    return None


def write(path, data):
    """json.dumps does the quoting. That is the whole point of this file."""
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def no_folder(raw):
    root = ROOT / "agents" / "emily" / "builds"
    have = sorted(x.name for x in root.iterdir()
                  if x.is_dir() and not x.name.startswith("_")) if root.is_dir() else []
    print(f"no such build folder: {raw}", file=sys.stderr)
    if have:
        print("builds you could mean:", file=sys.stderr)
        for n in have:
            print(f"  {n}", file=sys.stderr)
    else:
        print(f"there are no build folders in {root} - make one first.",
              file=sys.stderr)
    return 1


def cmd_listing(a):
    d = build_dir(a.build_dir)
    if d is None:
        return no_folder(a.build_dir)

    listing = {
        "title": a.title,
        "description": a.description,
        "tags": a.tag,
        "product_type": a.product,
    }
    if a.price is not None:
        listing["price_suggestion"] = a.price
    if a.materials:
        listing["materials"] = a.materials

    # Checked with the verifier's own function, before anything is written. A
    # file that exists and fails is worse than no file: it looks like work.
    problems = ev.listing_problems(listing)
    if problems:
        print("not written - this listing would fail the build:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    path = d / "listing.json"
    write(path, listing)
    print(f"wrote {path}")
    print(f"  title       {listing['title']}")
    print(f"  product     {listing['product_type']}")
    print(f"  tags        {len(a.tag)}: {', '.join(a.tag)}")
    if a.price is not None:
        print(f"  suggests    ${a.price:.2f}")
    return 0


def cmd_build(a):
    d = build_dir(a.build_dir)
    if d is None:
        return no_folder(a.build_dir)

    if a.status not in HER_STATUSES:
        print(f"'{a.status}' is not yours to set. Use one of: "
              f"{', '.join(HER_STATUSES)}.\n"
              f"  ready_for_review means a Printify draft exists, and only "
              f"emily-finish.py\n  knows whether one does.", file=sys.stderr)
        return 2

    # The byte counts are read off the disk rather than typed. Emily was
    # asked for "every file produced and its real byte count", which is an
    # invitation to report a number nobody checked - the same reason the
    # indicators, the ledger rows and the cycle arithmetic are all in code.
    files = sorted((f.name, f.stat().st_size) for f in d.iterdir() if f.is_file()
                   and f.name not in ("build.json", "listing.json"))

    existing = {}
    try:
        was = json.loads((d / "build.json").read_text())
        if isinstance(was, dict):
            # Whatever the finisher and the status reader have recorded is
            # theirs, not hers, and rewriting the file must not lose it.
            existing = {k: v for k, v in was.items() if k in KEPT}
    except Exception:
        pass

    build = dict(existing)
    build.update({
        "status": a.status,
        "art_mode": a.art,
        "files": [{"name": n, "bytes": b} for n, b in files],
        "bytes": sum(b for _n, b in files),
    })
    if a.idea:
        build["idea"] = a.idea
    if a.note:
        build["note"] = a.note

    path = d / "build.json"
    write(path, build)
    print(f"wrote {path}")
    print(f"  status      {build['status']}")
    print(f"  art         {build['art_mode']}"
          + ("   NOT SELLABLE - say so in your report" if a.art == "placeholder" else ""))
    for n, b in files:
        print(f"  {n:<22} {b:>9,} bytes")
    if not files:
        print("  (no files yet - the checker rejects a build with no artwork)")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="emily-listing.py",
        description="Write Emily's JSON files without her writing JSON.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("listing", help="write listing.json")
    p.add_argument("build_dir")
    p.add_argument("--title", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--tag", action="append", default=[],
                   help="repeat for each tag: --tag fall --tag autumn")
    p.add_argument("--product", default="sticker")
    p.add_argument("--price", type=float, default=None)
    p.add_argument("--materials", default="")
    p.set_defaults(fn=cmd_listing)

    p = sub.add_parser("build", help="write build.json")
    p.add_argument("build_dir")
    p.add_argument("--status", default="ready_local")
    p.add_argument("--art", required=True, choices=("generated", "placeholder"),
                   help="what emily-assets.py reported as its mode")
    p.add_argument("--idea", default="")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_build)

    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
