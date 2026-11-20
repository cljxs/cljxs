#!/usr/bin/env python3
"""
emily-listing.py — write Emily's two JSON files, so she never hand-writes JSON.

    emily-listing.py listing builds/<slug> --title "..." --description "..."
                     --tag fall --tag autumn --product sticker

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


def ordered_product(d):
    """The product this build was ordered as, or None for a build made before
    order.json existed."""
    try:
        return str(json.loads((d / "order.json").read_text()).get("product") or "").strip() or None
    except (OSError, ValueError, AttributeError):
        return None


def stitch_problems(listing):
    """[problem] if the copy calls a printed product embroidered. Checked here,
    where Emily can rewrite it, with the same function the draft refuses on."""
    try:
        spec = importlib.util.spec_from_file_location(
            "emily_printify_stitch", Path(__file__).resolve().parent / "emily-printify.py")
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)
        claim = ep.listing_claims_embroidery(listing)
        if not claim:
            return []
        _key, entry = ep.resolve(ep.read_catalog(), listing.get("product_type"))
    except Exception:
        return []                 # an unreadable catalogue is the draft's to refuse
    if entry and not ep.is_embroidered(entry):
        return [f"the copy says {claim!r}, and this product is printed, not "
                f"embroidered - describe what the buyer will receive"]
    return []


# THE SEARCH THIS DESIGN IS FOR (owner, 2026-10-28: traffic). Scout's idea
# names a measured phrase - what shoppers type - saved with the build as
# evidence.json. A listing that never says it cannot be found for it; the
# shop's first 35 listings averaged two views each. So the title starts with
# it and it is a tag. What "starts" and "a tag" mean is store-report.py's
# audit - where search shows a title, plurals as one search - imported.
#
# PUT THERE, NOT REFUSED (2026-11-03). It refused at first, and the "cat
# meme sticker tiktok dramatic pause cat" build failed again and again: the
# listing was never written, and Emily reported success. The disclosure lines
# went the same way once (disclosures.py: refusing stalled two hoodies over a
# rule a script can simply apply). The phrase is fixed text, so code puts it
# in and says so; Emily's own words follow it.
def _audit():
    spec = importlib.util.spec_from_file_location("store_report_listing", SCRIPTS / "store-report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def target_of(d):
    """The measured phrase this build is for, or None (a build with no evidence)."""
    try:
        phrase = json.loads((d / "evidence.json").read_text()).get("phrase")
    except (OSError, ValueError, AttributeError):
        return None
    return " ".join(str(phrase or "").lower().split()) or None


def ours(phrase):
    """The phrase without words that are somebody else's mark, or None.

    The measured phrase was "cat meme sticker tiktok" (2026-11-03): Emily
    would not write TikTok into an Etsy title, rightly, three runs running.
    A word ip-check.py refuses on its own is dropped ("cat meme sticker");
    if what is left is still refused, or is under two words, there is no
    phrase to put in. A CHECK-tier word ("frozen") is an ordinary word too,
    so it stays - the owner saw that flag when approving the idea."""
    spec = importlib.util.spec_from_file_location("ip_check_listing", SCRIPTS / "ip-check.py")
    ipc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ipc)
    if ipc.risky(phrase)[0] != "blocked":
        return phrase
    kept = " ".join(w for w in phrase.split() if ipc.risky(w)[0] != "blocked")
    if len(kept.split()) < 2 or ipc.risky(kept)[0] == "blocked":
        return None
    return kept


def with_search(d, listing):
    """(listing, [what was changed]) - the title led by the build's measured
    phrase and the phrase among the tags, wherever Emily did not put them."""
    measured = target_of(d)
    if not measured:
        return listing, []
    phrase = ours(measured)
    if not phrase:
        return listing, [f"the measured phrase {measured!r} names someone else's property - "
                         f"left out of the title and tags"]
    sr = _audit()
    norm = lambda text: " ".join(sr._stem(w) for w in sr._words(text))
    out = dict(listing)
    changed = [] if phrase == measured else [
        f"the measured phrase {measured!r} names someone else's property - used {phrase!r}"]
    title = " ".join(str(out.get("title") or "").split())
    if norm(phrase) not in norm(title[:max(sr.TITLE_SEEN, len(phrase) + 10)]):
        head = sr._cap(phrase)
        rest = title
        while rest and len(head) + 3 + len(rest) > sr.TITLE_MAX:
            rest = rest.rsplit(" ", 1)[0] if " " in rest else ""
        out["title"] = f"{head} | {rest}" if rest else head
        changed.append(f"title now starts with {phrase!r}, the search this design is for")
    tags = list(out.get("tags") or [])
    if len(phrase) <= sr.TAG_MAX and not any(norm(t) == norm(phrase) for t in tags):
        dropped = tags[sr.TAG_SLOTS - 1:] if len(tags) >= sr.TAG_SLOTS else []
        out["tags"] = [phrase] + tags[:sr.TAG_SLOTS - 1]
        changed.append(f"{phrase!r} added as the first tag" + (f" (in place of {dropped[0]!r})" if dropped else ""))
    return out, changed


def cmd_listing(a):
    d = build_dir(a.build_dir)
    if d is None:
        return no_folder(a.build_dir)
    want = ordered_product(d)
    a.product = (a.product or want or "sticker").strip()
    if want and a.product.lower() != want.lower():
        print(f"not written - this build was ordered as {want!r}, not {a.product!r}.\n"
              f"  Use --product {want}. The product is the owner's choice, made "
              f"when the idea was approved.", file=sys.stderr)
        return 2

    listing = {
        "title": a.title,
        "description": a.description,
        "tags": a.tag,
        "product_type": a.product,
    }
    if a.materials:
        listing["materials"] = a.materials

    # Checked with the verifier's own function, before anything is written. A
    # file that exists and fails is worse than no file: it looks like work.
    listing, fixed = with_search(d, listing)
    problems = ev.listing_problems(listing) + stitch_problems(listing)
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
    print(f"  tags        {len(listing['tags'])}: {', '.join(listing['tags'])}")
    for f in fixed:
        print(f"  fixed       {f}")
    # The shop's listing audit, as advice: the same words the owner's Fix
    # listings tab would show for this listing once it is live.
    try:
        advice = _audit().audit_listing(listing)
    except Exception:
        advice = []
    for kind, msg in advice:
        print(f"  audit {kind.lower():<7}{msg}")
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
    # No default of its own: a build knows what it was ordered as. "sticker"
    # was the default, so a build listed without --product became a sticker -
    # the first test run's chest emblem did (2026-10-02). Only a build from
    # before order.json existed still falls back to it.
    p.add_argument("--product", default=None)
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
