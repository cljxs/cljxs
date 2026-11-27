#!/usr/bin/env python3
"""
emily-build.py — look at Emily's builds, and get rid of ones you do not want.

    emily-build.py list
    emily-build.py remove crisp-air-hiking-sticker
    emily-build.py restore crisp-air-hiking-sticker

`remove` archives. It moves the folder to builds/_removed/<slug>/ rather than
deleting it: the artwork cost a model call, the gallery reads the folder name,
and "I did not like it" and "destroy it" are different intentions. `restore`
puts one back. Empty builds/_removed/ by hand when you are sure.

A build with a Printify draft has the draft deleted first, asked of Printify
itself (emily-finish.py drop_drafts): already gone there is fine, an
unpublished draft is deleted, and one on sale on Etsy is never touched - the
build is then not archived either. Archiving the folder alone would leave the
product in Printify with nothing here pointing at it. --force archives
without asking Printify.

Standard library only.
"""

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
BUILDS = ROOT / "agents" / "emily" / "builds"
REMOVED = BUILDS / "_removed"


def read(d):
    try:
        return json.loads((d / "build.json").read_text())
    except Exception:
        return {}


def folders(base):
    if not base.is_dir():
        return []
    return sorted(c for c in base.iterdir() if c.is_dir() and not c.name.startswith("_"))


def cmd_list():
    live = folders(BUILDS)
    for d in live:
        b = read(d)
        pid = b.get("printify_product_id")
        state = b.get("status", "?")
        mark = f"printify {pid}" if pid else "local only"
        print(f"  {d.name:<44} {state:<14} {mark}")
    print(f"{len(live)} build(s)")
    gone = folders(REMOVED)
    if gone:
        print(f"\n{len(gone)} removed (restore by name):")
        for d in gone:
            print(f"  {d.name}")
    return 0


def cmd_remove(slug, force):
    d = BUILDS / slug
    if not d.is_dir():
        print(f"no build called {slug!r}. emily-build.py list", file=sys.stderr)
        return 1
    b = read(d)
    if b.get("printify_product_id") and not force:
        said = []
        if not _finish().drop_drafts(b, say=said.append):
            print("\n".join(said) + f"\n  {slug} was not deleted.", file=sys.stderr)
            return 1
        for line in said:
            print(line.strip())

    REMOVED.mkdir(parents=True, exist_ok=True)
    dest = REMOVED / slug
    if dest.exists():
        n = 2
        while (REMOVED / f"{slug}-{n}").exists():
            n += 1
        dest = REMOVED / f"{slug}-{n}"
    shutil.move(str(d), str(dest))
    print(f"archived to {dest.relative_to(ROOT)}")
    print(f"  the gallery will stop showing it. Restore with:\n"
          f"    emily-build.py restore {dest.name}")
    return 0


def _finish():
    """emily-finish.py as a module - it owns deleting a build's drafts."""
    spec = importlib.util.spec_from_file_location(
        "emily_finish_build", Path(__file__).resolve().parent / "emily-finish.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_restore(slug):
    src = REMOVED / slug
    if not src.is_dir():
        print(f"nothing archived called {slug!r}", file=sys.stderr)
        return 1
    dest = BUILDS / slug
    if dest.exists():
        print(f"{slug} already exists in builds/ - rename one first", file=sys.stderr)
        return 1
    shutil.move(str(src), str(dest))
    print(f"restored {dest.relative_to(ROOT)}")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        return cmd_list()
    if cmd in ("remove", "restore"):
        if len(sys.argv) < 3:
            print(f"usage: emily-build.py {cmd} <slug>", file=sys.stderr)
            return 2
        if cmd == "remove":
            return cmd_remove(sys.argv[2], "--force" in sys.argv)
        return cmd_restore(sys.argv[2])
    print("usage: emily-build.py list|remove <slug> [--force]|restore <slug>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
