#!/usr/bin/env python3
"""
emily-finish.py — turn a finished build into a Printify draft.

Run by the dispatcher after Emily completes a task, before the verifier.

Emily was told to create the Printify product and did not: she wrote
status "ready_local" instead, which her instructions reserve for the case
where no token is set. Creating a product takes a blueprint id, a print
provider and variant ids - three dependent lookups before the create - and
multi-step tool work is the thing cheap models quietly skip. Her artwork was
moved into code for the same reason.

Nothing here needs judgement: the listing copy and the artwork are already on
disk, and the blueprint was chosen once by a human.

    emily-finish.py <build_dir>

Exits 0 even when it cannot draft. A local build is still a real build - the
status stays "ready_local", the gallery shows LOCAL ONLY, and the user is told
why. Failing the whole task over the last mile would throw away good work.

"Told why" was a lie until now. The refusal was printed here and went into the
dispatcher log, which nobody reads; the card said LOCAL ONLY and stopped. A
build sat like that with a listing.json full of malformed JSON, the drafter
said so on two separate attempts, and the only way to find out was to ask.
So the reason is written onto build.json as `draft_blocked` and drawn on the
card. Cleared the moment a draft succeeds, because a stale reason is worse
than none.

Standard library only.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))


# Enough to name the problem on a card, not so much that the card becomes a
# log. Every refusal in emily-printify.py draft leads with its own one-line
# summary, so the first lines are the useful ones.
REASON_CHARS = 400


def reason_from(res):
    """The drafter's own words for why it refused.

    Its refusals go to stderr and its progress to stdout, so stderr is the
    reason and stdout is the fallback for a failure that never got that far.
    Not reworded here: a second phrasing of the same refusal is a second
    opinion about what went wrong, and the two drift.
    """
    text = (res.stderr or "").strip() or (res.stdout or "").strip()
    # A refusal leads with its message; a crash leads with forty lines of
    # stack and ends with the only line that says anything. Truncating from
    # the front puts "Traceback (most recent call last):" on the card and
    # throws the exception away, which is the wrong half.
    if text.startswith("Traceback (most recent call last):"):
        last = [ln for ln in text.splitlines() if ln.strip()][-1]
        text = f"the drafter crashed: {last.strip()}"
    if len(text) > REASON_CHARS:
        text = text[:REASON_CHARS].rstrip() + " ..."
    return text or f"the drafter exited {res.returncode} without saying why"


def note_draft(d, blocked):
    """Record - or clear - why the Printify draft is missing.

    Read-modify-write, and it gives up rather than rewrite a build.json it
    cannot parse: overwriting Emily's own account of what she made, to explain
    that something else would not parse, would destroy the more valuable file
    of the two.
    """
    path = d / "build.json"
    try:
        # A build Emily never reached has no build.json yet; that is not a
        # file to protect, so the reason starts one (2026-10-04, dad tee).
        build = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(build, dict):
            raise ValueError("build.json is not an object")
    except Exception as exc:
        print(f"emily-finish: not recording the reason - {path.name} "
              f"will not parse ({type(exc).__name__})")
        return
    if blocked:
        build["draft_blocked"] = blocked
    elif "draft_blocked" not in build:
        return
    else:
        build.pop("draft_blocked")
    path.write_text(json.dumps(build, indent=1) + "\n")


def find_build(raw):
    agent_dir = ROOT / "agents" / "emily"
    return next((c for c in ([Path(raw)] if Path(raw).is_absolute() else
                             [agent_dir / raw, agent_dir / "builds" / raw, ROOT / raw])
                 if c.is_dir()), None)


# THE REDO. The first test drafts (2026-10-02/03) went to Printify with a
# misspelled slogan, a date, the product's own name and a placeholder - before
# the proofread, the name check, the trim and the placeholder refusal existed.
# A draft is made once, so nothing ever re-made them. This deletes the old
# UNPUBLISHED draft, draws the art again through today's pipeline, and drafts
# again. Anything Printify says is live on Etsy is refused, never deleted.
def redo(raw, product=None, keep_art=False):
    d = find_build(raw)
    if d is None:
        print(f"emily-finish: no build folder for {raw}")
        return 1
    build, listing, order = _json(d / "build.json"), _json(d / "listing.json"), _json(d / "order.json")
    if build.get("published"):
        print(f"emily-finish: {d.name} is published - not touching a live listing")
        return 1
    ep = _module("emily_printify_redo", "emily-printify.py")
    ep.load_credentials()
    shop = build.get("printify_shop_id") or os.environ.get("PRINTIFY_SHOP_ID")
    pids = [p for p in dict.fromkeys([build.get("printify_product_id")] +
            [x.get("product_id") for x in build.get("printify_drafts") or []]) if p]
    for pid in pids:
        try:
            live = ep._live_state(shop, pid)
        except ep.ApiError as exc:
            if getattr(exc, "code", None) == 404:
                continue                                 # already gone
            print(f"emily-finish: could not read product {pid} ({exc}) - not deleting blind")
            return 1
        if live["published"] or live["locked"]:
            print(f"emily-finish: product {pid} is live on Etsy - refusing to delete it")
            return 1
    for pid in pids:
        try:
            ep.call(f"/shops/{shop}/products/{pid}.json", method="DELETE", soft=True)
            print(f"  deleted the old draft {pid}")
        except ep.ApiError as exc:
            if getattr(exc, "code", None) != 404:
                print(f"emily-finish: could not delete {pid} ({exc})")
                return 1
    for k in ("printify_product_id", "printify_url", "printify_drafts", "published",
              "drafted_at", "draft_blocked", "printify_shop_id"):
        build.pop(k, None)
    build["status"] = "ready_local"
    (d / "build.json").write_text(json.dumps(build, indent=1) + "\n")
    # What to draw it as: the order, or - for builds from before order.json -
    # what Emily listed. --product moves it (the emblem drafted as a sticker).
    order = {"product": product or order.get("product") or listing.get("product_type") or "",
             "name": order.get("name") or build.get("idea") or listing.get("title") or d.name,
             "brief": order.get("brief") or str(listing.get("description") or "")[:600]}
    (d / "order.json").write_text(json.dumps(order, indent=1) + "\n")
    if product and listing:
        listing["product_type"] = product
        (d / "listing.json").write_text(json.dumps(listing, indent=1) + "\n")
    ea = _module("emily_assets_redo", "emily-assets.py")
    # --redraft: the art was right and the drafting was not (the "Paws &
    # Kissies" sticker, 2026-10-07: blue left inside its "&", lettering past
    # the round cut). Draft the same design.png again through today's cut
    # and placement - no new drawing, which costs money and changes the art.
    if keep_art and (d / "design.png").is_file() and not ea.is_placeholder(d / "design.png"):
        return draft(d)
    ok, msg = generate(d)
    print(f"  {d.name}: {msg}")
    if not ok or ea.is_placeholder(d / "design.png"):
        print("  no real art yet - the hourly redraw will finish it")
        return 1
    return draft(d)


def main():
    if sys.argv[1:2] == ["--redraw"]:
        return redraw()
    if sys.argv[1:2] in (["--redo"], ["--redraft"]):
        keep_art = sys.argv[1] == "--redraft"
        args = sys.argv[2:]
        product = None
        if "--product" in args:
            i = args.index("--product")
            product = args[i + 1]
            args = args[:i] + args[i + 2:]
        for raw in args:
            if redo(raw, product, keep_art):
                print("emily-finish: stopping - the rest wait")
                return 1
        return 0
    if len(sys.argv) < 2:
        print("usage: emily-finish.py <build_dir>")
        return 0

    raw = sys.argv[1]
    agent_dir = ROOT / "agents" / "emily"
    d = next((c for c in ([Path(raw)] if Path(raw).is_absolute() else
                          [agent_dir / raw, agent_dir / "builds" / raw, ROOT / raw])
              if c.is_dir()), None)
    if d is None:
        print(f"emily-finish: no build folder for {raw} - nothing to draft")
        return 0

    try:
        build = json.loads((d / "build.json").read_text())
    except Exception:
        build = {}
    if build.get("printify_product_id"):
        print(f"emily-finish: {d.name} already has Printify product "
              f"{build['printify_product_id']} - leaving it alone")
        return 0

    return draft(d)


def draft(d):
    """Run the drafter on one build and record the outcome on it."""
    res = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "emily-printify.py"), "draft", str(d)],
        capture_output=True, text=True, timeout=300,
    )
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    print(out)

    if res.returncode == 0:
        note_draft(d, None)
        print("emily-finish: drafted in Printify, UNPUBLISHED. "
              "Press Publish there when you are happy with it.")
    else:
        note_draft(d, {
            "reason": reason_from(res),
            "exit": res.returncode,
            "when_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })
        print(f"emily-finish: could not create the Printify draft "
              f"(exit {res.returncode}). The build stays 'ready_local', and "
              "the reason is now\non the build so the gallery can show it "
              "instead of an unexplained LOCAL ONLY.")
    return 0


# THE REDRAW. Drafts are attempted once, when Emily finishes. On 2026-10-02 the
# day's OpenRouter money ran out mid-build, the art step fell back to its
# placeholder, and draft rightly refused it - and there the build stayed,
# LOCAL ONLY, until somebody typed the redraw by hand. This finds those
# builds and redraws them, hourly (deploy/redraw-art.timer).
REDRAW_PER_RUN = 3


def _module(name, file):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _json(path):
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def stuck(builds_root):
    """Builds with no Printify draft whose art is still the placeholder, and
    either a listing to draft from or an order to send to Emily (a build
    held at approval because the art failed). Oldest first."""
    ea = _module("emily_assets_redraw", "emily-assets.py")
    out = []
    for d in sorted(Path(builds_root).iterdir() if Path(builds_root).is_dir() else []):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if _json(d / "build.json").get("printify_product_id"):
            continue
        if ((d / "listing.json").is_file() or (d / "order.json").is_file()) \
                and ea.is_placeholder(d / "design.png"):
            out.append(d)
    return sorted(out, key=lambda d: (d / "design.png").stat().st_mtime)


def generate(d):
    """Draw a build's art again, through the same step a new build uses, from
    what it was ordered as. Builds from before order.json fall back to
    Emily's own listing."""
    order, listing = _json(d / "order.json"), _json(d / "listing.json")
    idea = order.get("name") or _json(d / "build.json").get("idea") or listing.get("title") or d.name
    brief = order.get("brief") or str(listing.get("description") or "")[:600]
    product = order.get("product") or listing.get("product_type") or ""
    nb = _module("emily_new_build_redraw", "emily-new-build.py")
    evidence = _json(d / "evidence.json") or None
    return nb.generate_artwork(d.name, idea, brief, product, evidence)


def redraw(limit=REDRAW_PER_RUN):
    """Redraw and draft up to `limit` stuck builds. Stops at the first redraw
    that is still a placeholder: the money or the model is still out, and the
    rest would fail the same way at a cost."""
    ea = _module("emily_assets_redraw2", "emily-assets.py")
    todo = stuck(ROOT / "agents" / "emily" / "builds")
    print(f"emily-finish: {len(todo)} build(s) waiting on real art")
    for d in todo[:limit]:
        ok, msg = generate(d)
        print(f"  {d.name}: {msg}")
        if not ok or ea.is_placeholder(d / "design.png"):
            note_draft(d, {"reason": f"redraw still failing: {msg}"[:REASON_CHARS], "exit": 1,
                           "when_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
            print("  still no real art - stopping until the next run")
            return 0
        if (d / "listing.json").is_file():
            draft(d)
        else:
            send_to_emily(d)
    return 0


def send_to_emily(d):
    """A held build has real art now: queue Emily's task, through
    emily-new-build's own queue(), and stop saying it is waiting."""
    order = _json(d / "order.json")
    nb = _module("emily_new_build_queue", "emily-new-build.py")
    code = nb.queue(d.name, order.get("name") or d.name, order.get("brief") or "",
                    order.get("product") or "", _json(d / "evidence.json") or None)
    build = _json(d / "build.json")
    if code == 0 and (build.get("status") == "waiting_for_art" or "draft_blocked" in build):
        build.pop("status", None)
        build.pop("draft_blocked", None)
        (d / "build.json").write_text(json.dumps(build, indent=1) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
