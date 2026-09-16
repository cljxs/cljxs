#!/usr/bin/env python3
"""
emily-printify.py — Emily's hands for Printify.

DELIBERATELY HAS NO PUBLISH COMMAND. Printify does not publish a product
unless something calls its publish endpoint, and nothing here ever does. The
capability simply is not present, so Emily cannot publish by accident or by
misunderstanding an instruction. You publish by clicking Publish in the
Printify dashboard, which pushes the listing to your connected Etsy shop.

Subcommands:
  check                         verify the token and list connected shops
  blueprints --search poster    find a product type
  providers  564                print providers for that product
  variants   564 27             sizes/colours and their variant ids
  upload     design.png         upload artwork, returns an image id
  create     --spec spec.json   create the product, UNPUBLISHED

  suggest --product sticker     find candidate blueprints for a product type
  pick --product sticker \\        remember a blueprint/provider once, by hand
       --blueprint 564 --provider 27
  draft builds/my-slug          build folder -> UNPUBLISHED product, no judgement

Standard library only.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

API = "https://api.printify.com/v1"


def load_credentials():
    here = Path(__file__).resolve().parent.parent
    for cand in (here / "agents" / "emily" / "state" / "credentials.env",
                 Path(os.environ.get("EMILY_CREDENTIALS", "/nonexistent"))):
        if not cand.is_file():
            continue
        for line in cand.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v and k not in os.environ:
                os.environ[k] = v


def token():
    load_credentials()
    t = os.environ.get("PRINTIFY_API_TOKEN", "").strip()
    if not t:
        print("PRINTIFY_API_TOKEN is not set.\n"
              "Put it in agents/emily/state/credentials.env "
              "(get one at printify.com/app/account/api).", file=sys.stderr)
        sys.exit(2)
    return t


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": f"Bearer {token()}",
                 "Content-Type": "application/json",
                 "User-Agent": "emily-printify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        print(f"Printify returned HTTP {e.code}: {detail}", file=sys.stderr)
        sys.exit(1)


def cmd_check(a):
    shops = call("/shops.json")
    print(json.dumps({"ok": True, "shops": shops}, indent=2))
    if not shops:
        print("\nNo shops connected. In Printify: Stores -> Connect -> Etsy.",
              file=sys.stderr)
    else:
        for s in shops:
            print(f"\n  shop_id={s.get('id')}  title={s.get('title')!r}  "
                  f"channel={s.get('sales_channel')}")


def cmd_blueprints(a):
    bps = call("/catalog/blueprints.json")
    term = (a.search or "").lower()
    hits = [b for b in bps if term in b.get("title", "").lower()] if term else bps
    for b in hits[:a.limit]:
        print(f"  {b['id']:>6}  {b['title']}")
    nxt = hits[0]["id"] if hits else None
    print(f"\n{len(hits)} match(es).")
    if nxt:
        print(f"Next, find who prints it:\n"
              f"  emily-printify.py providers {nxt}")


def cmd_providers(a):
    ps = call(f"/catalog/blueprints/{a.blueprint_id}/print_providers.json")
    for p in ps:
        print(f"  {p['id']:>6}  {p['title']}")
    if ps:
        print(f"\nPick one, then save the pair (once, ever):\n"
              f"  emily-printify.py pick --product sticker "
              f"--blueprint {a.blueprint_id} --provider {ps[0]['id']}")


def cmd_variants(a):
    d = call(f"/catalog/blueprints/{a.blueprint_id}/print_providers/{a.provider_id}/variants.json")
    for v in (d.get("variants") or [])[:a.limit]:
        print(f"  {v['id']:>8}  {v.get('title')}")
    print(f"\n{len(d.get('variants') or [])} variant(s) total.")


def cmd_upload(a):
    f = Path(a.file)
    if not f.is_file() or f.stat().st_size == 0:
        print(f"{f} is missing or empty - a blank asset is a failed build.", file=sys.stderr)
        sys.exit(1)
    res = call("/uploads/images.json", {
        "file_name": a.name or f.name,
        "contents": base64.b64encode(f.read_bytes()).decode(),
    })
    print(json.dumps({"ok": True, "image_id": res.get("id"),
                      "width": res.get("width"), "height": res.get("height"),
                      "file_name": res.get("file_name")}, indent=2))


def cmd_create(a):
    spec = json.loads(Path(a.spec).read_text())
    shop_id = spec.pop("shop_id", None) or os.environ.get("PRINTIFY_SHOP_ID")
    if not shop_id:
        print("shop_id missing: put it in the spec or in credentials.env.", file=sys.stderr)
        sys.exit(2)
    # Guard: refuse anything that even looks like an instruction to publish.
    for banned in ("publish", "is_published", "publishing"):
        spec.pop(banned, None)
    res = call(f"/shops/{shop_id}/products.json", spec)
    pid = res.get("id")
    print(json.dumps({
        "ok": True, "product_id": pid, "shop_id": shop_id,
        "published": False,
        "review_at": f"https://printify.com/app/store/products/{pid}" if pid else None,
        "note": "Created UNPUBLISHED. Review it in Printify, then publish by hand.",
    }, indent=2))


# ---------------------------------------------------------------- catalogue
#
# Creating a Printify product is not one call. It needs a blueprint id, a print
# provider for that blueprint, and the variant ids for the sizes you want -
# three lookups before the create, each depending on the last. Emily skipped
# the whole dance and wrote status "ready_local" instead, which is what a cheap
# model does with multi-step tool work.
#
# So the choosing happens once, by a human, and is cached. After that a draft
# is one command with no judgement in it.

CATALOG = Path(__file__).resolve().parent.parent / "agents" / "emily" / "state" / "printify-catalog.json"


def read_catalog():
    try:
        return json.loads(CATALOG.read_text())
    except Exception:
        return {}


def cmd_suggest(a):
    """Find candidate blueprints for a product type and show what to do next."""
    bps = call("/catalog/blueprints.json")
    term = (a.product or "").lower()
    hits = [b for b in bps if term in b.get("title", "").lower()]
    if not hits:
        hits = [b for b in bps if term.rstrip("s") in b.get("title", "").lower()]
    if not hits:
        print(f"nothing in the catalogue matches '{a.product}'. "
              f"Try: blueprints --search <word>")
        return
    print(f"Blueprints matching '{a.product}':\n")
    for b in hits[:a.limit]:
        print(f"  blueprint {b['id']:>6}  {b['title']}")
    first = hits[0]
    print(f"\nPick one, then find a print provider for it:")
    print(f"  emily-printify.py providers {first['id']}")
    print(f"\nThen save the pair (it is remembered, you only do this once):")
    print(f"  emily-printify.py pick --product {a.product} "
          f"--blueprint {first['id']} --provider <provider_id>")


def cmd_pick(a):
    """Save the blueprint/provider/variants for a product type, once."""
    variants = call(f"/catalog/blueprints/{a.blueprint}/print_providers/{a.provider}/variants.json")
    all_v = variants.get("variants") or []
    if not all_v:
        print("that blueprint/provider pair has no variants - check the ids.", file=sys.stderr)
        sys.exit(1)

    if a.variants:
        wanted = [int(v) for v in a.variants.split(",")]
        chosen = [v for v in all_v if v["id"] in wanted]
        if not chosen:
            print(f"none of {wanted} are variants of this product.", file=sys.stderr)
            sys.exit(1)
    else:
        # Default to every variant. For a sticker that is the size range, which
        # is what you want on a listing; trim it later with --variants if not.
        chosen = all_v[:a.limit]

    cat = read_catalog()
    cat[a.product] = {
        "blueprint_id": int(a.blueprint),
        "provider_id": int(a.provider),
        "variant_ids": [v["id"] for v in chosen],
        "variant_titles": [v.get("title") for v in chosen][:12],
        "chosen_at": _now(),
    }
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps(cat, indent=1) + "\n")
    print(f"saved '{a.product}' -> blueprint {a.blueprint}, provider {a.provider}, "
          f"{len(chosen)} variant(s)")
    for t in cat[a.product]["variant_titles"]:
        print(f"    {t}")
    print(f"\nWritten to {CATALOG}")
    print("Drafts for this product type are now automatic.")


def cmd_draft(a):
    """Turn a finished build folder into an UNPUBLISHED Printify product.

    Everything here is mechanical: the listing copy and the artwork already
    exist on disk, and the blueprint/provider/variants were chosen once. There
    is no judgement left, which is exactly why it should not be an agent's job.
    """
    here = Path(__file__).resolve().parent.parent
    d = Path(a.build_dir)
    if not d.is_absolute():
        for base in (here / "agents" / "emily", here):
            if (base / a.build_dir).is_dir():
                d = base / a.build_dir
                break
    if not d.is_dir():
        print(f"no such build folder: {a.build_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        listing = json.loads((d / "listing.json").read_text())
    except Exception as exc:
        print(f"cannot read {d/'listing.json'}: {exc}", file=sys.stderr)
        sys.exit(1)

    design = d / "design.png"
    if not design.is_file() or design.stat().st_size < 2000:
        print(f"{design} is missing or too small to be artwork "
              f"({design.stat().st_size if design.is_file() else 0} bytes).", file=sys.stderr)
        sys.exit(1)

    # An explicit --product wins over the listing's own product_type, so the
    # same build can be drafted against a second catalogue entry and the two
    # profit tables compared side by side. Without this the flag did nothing,
    # because listing.json always carries a product_type.
    product_type = (a.product or listing.get("product_type") or "sticker").lower()
    cat = read_catalog().get(product_type)
    if not cat:
        print(f"no catalogue entry for '{product_type}'. Choose one once:\n\n"
              f"  emily-printify.py suggest --product {product_type}\n",
              file=sys.stderr)
        sys.exit(2)

    shop_id = os.environ.get("PRINTIFY_SHOP_ID")
    if not shop_id:
        print("PRINTIFY_SHOP_ID is not set - run emily-printify.py check.", file=sys.stderr)
        sys.exit(2)

    print(f"uploading {design.name} ({design.stat().st_size // 1024} KB) ...")
    up = call("/uploads/images.json", {
        "file_name": f"{d.name}.png",
        "contents": base64.b64encode(design.read_bytes()).decode(),
    })
    image_id = up.get("id")
    if not image_id:
        print(f"upload returned no image id: {up}", file=sys.stderr)
        sys.exit(1)
    print(f"  image {image_id}")

    price_cents = int(round(float(listing.get("price_suggestion") or 0) * 100)) or 599
    variant_ids = cat["variant_ids"]

    spec = {
        "title": str(listing.get("title") or d.name)[:140],
        "description": str(listing.get("description") or ""),
        "tags": [str(t)[:20] for t in (listing.get("tags") or [])][:13],
        "blueprint_id": cat["blueprint_id"],
        "print_provider_id": cat["provider_id"],
        "variants": [{"id": v, "price": price_cents, "is_enabled": True} for v in variant_ids],
        "print_areas": [{
            "variant_ids": variant_ids,
            "placeholders": [{
                "position": "front",
                "images": [{"id": image_id, "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}],
            }],
        }],
    }

    print(f"creating the product ({len(variant_ids)} variants at "
          f"${price_cents/100:.2f}) ...")
    res = call(f"/shops/{shop_id}/products.json", spec)
    pid = res.get("id")
    url = f"https://printify.com/app/store/products/{pid}" if pid else None

    # Record it where the gallery and the verifier will see it.
    try:
        build = json.loads((d / "build.json").read_text())
    except Exception:
        build = {}
    # Keep every draft made from this build, so comparing two product types
    # does not lose the first product's id.
    drafts = build.get("printify_drafts") or []
    drafts.append({"product_type": product_type, "product_id": pid, "url": url,
                   "price": price_cents / 100, "drafted_at": _now()})
    build["printify_drafts"] = drafts
    build.update({
        "status": "ready_for_review",
        "printify_product_id": pid,
        "printify_url": url,
        "printify_shop_id": shop_id,
        "published": False,
        "drafted_at": _now(),
    })
    (d / "build.json").write_text(json.dumps(build, indent=1) + "\n")

    print(json.dumps({
        "ok": True, "product_id": pid, "shop_id": shop_id, "published": False,
        "review_at": url,
        "note": "Created UNPUBLISHED. Review it in Printify, then publish by hand.",
    }, indent=2))


def _builds_root():
    here = Path(__file__).resolve().parent.parent
    return here / "agents" / "emily" / "builds"


def _resolve_build(arg):
    d = Path(arg)
    if d.is_absolute() and d.is_dir():
        return d
    here = Path(__file__).resolve().parent.parent
    for base in (here / "agents" / "emily", here, _builds_root()):
        if (base / arg).is_dir():
            return base / arg
    return None


def _live_state(shop_id, pid):
    """What Printify says about this product right now.

    Whether a listing is live is a fact the shop already knows. Asking a person
    to remember to record it - or an agent to claim it - is how build.json sat
    at published:false while the product was on sale, and the gallery went on
    showing READY FOR REVIEW for something a customer could buy.
    """
    prod = call(f"/shops/{shop_id}/products/{pid}.json")
    ext = prod.get("external") or {}
    handle = ext.get("handle") or ""
    prices = sorted({v.get("price") for v in (prod.get("variants") or [])
                     if v.get("is_enabled") and v.get("price")})
    return {
        "title": prod.get("title"),
        "visible": bool(prod.get("visible")),
        "locked": bool(prod.get("is_locked")),
        "published": bool(handle),
        "etsy_url": handle or None,
        "variants_enabled": sum(1 for v in (prod.get("variants") or []) if v.get("is_enabled")),
        "mockups": len(prod.get("images") or []),
        "price_low": (prices[0] / 100) if prices else None,
        "price_high": (prices[-1] / 100) if prices else None,
    }


def cmd_status(a):
    """Ask Printify what actually happened to each drafted product, and record
    it. Nothing here is a judgement call, so nothing here asks anyone."""
    shop_id = os.environ.get("PRINTIFY_SHOP_ID")
    if not shop_id:
        print("PRINTIFY_SHOP_ID is not set - run emily-printify.py check.", file=sys.stderr)
        sys.exit(2)

    if a.build_dir:
        dirs = [_resolve_build(a.build_dir)]
        if dirs[0] is None:
            print(f"no such build folder: {a.build_dir}", file=sys.stderr)
            sys.exit(1)
    else:
        root = _builds_root()
        dirs = sorted([d for d in root.iterdir() if d.is_dir()]) if root.is_dir() else []
    if not dirs:
        print("no builds found.")
        return 0

    changed = 0
    for d in dirs:
        try:
            build = json.loads((d / "build.json").read_text())
        except Exception:
            continue
        pid = build.get("printify_product_id")
        if not pid:
            print(f"{d.name:<34} no Printify draft yet "
                  f"(run: emily-printify.py draft {d.name})")
            continue

        live = _live_state(build.get("printify_shop_id") or shop_id, pid)
        was = bool(build.get("published"))
        build.update({
            "published": live["published"],
            "etsy_url": live["etsy_url"],
            "printify_visible": live["visible"],
            "mockup_count": live["mockups"],
            "price_low": live["price_low"],
            "price_high": live["price_high"],
            "published_checked_utc": _now(),
        })
        if live["published"]:
            build["status"] = "published"
        (d / "build.json").write_text(json.dumps(build, indent=1) + "\n")
        if live["published"] != was:
            changed += 1

        price = ("-" if live["price_low"] is None
                 else f"${live['price_low']:.2f}"
                 + (f"-${live['price_high']:.2f}" if live["price_high"] != live["price_low"] else ""))
        mark = "LIVE " if live["published"] else "draft"
        print(f"{d.name:<34} {mark}  {live['variants_enabled']:>2} variants  "
              f"{price:<14} {live['mockups']:>2} mockups")
        if live["etsy_url"]:
            print(f"{'':<34}        {live['etsy_url']}")
        elif live["price_low"] is not None and live["price_low"] < 1:
            print(f"{'':<34}        prices look unset - set them in Printify before publishing")

    if changed:
        print(f"\n{changed} build(s) changed state. The gallery reads build.json, "
              f"so it will catch up on its next load.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Emily's Printify hands (no publish command by design)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(fn=cmd_check)

    p = sub.add_parser("blueprints"); p.add_argument("--search", default="")
    p.add_argument("--limit", type=int, default=40); p.set_defaults(fn=cmd_blueprints)

    p = sub.add_parser("providers"); p.add_argument("blueprint_id"); p.set_defaults(fn=cmd_providers)

    p = sub.add_parser("variants"); p.add_argument("blueprint_id"); p.add_argument("provider_id")
    p.add_argument("--limit", type=int, default=25); p.set_defaults(fn=cmd_variants)

    p = sub.add_parser("upload"); p.add_argument("file"); p.add_argument("--name")
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("create"); p.add_argument("--spec", required=True); p.set_defaults(fn=cmd_create)

    p = sub.add_parser("suggest"); p.add_argument("--product", required=True)
    p.add_argument("--limit", type=int, default=15); p.set_defaults(fn=cmd_suggest)

    p = sub.add_parser("pick"); p.add_argument("--product", required=True)
    p.add_argument("--blueprint", required=True); p.add_argument("--provider", required=True)
    p.add_argument("--variants", default=""); p.add_argument("--limit", type=int, default=12)
    p.set_defaults(fn=cmd_pick)

    p = sub.add_parser("status"); p.add_argument("build_dir", nargs="?")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("draft"); p.add_argument("build_dir")
    p.add_argument("--product", default=""); p.set_defaults(fn=cmd_draft)

    a = ap.parse_args()
    # Load credentials.env here, for every subcommand, not lazily inside
    # token(). "draft" reads PRINTIFY_SHOP_ID before it makes any API call, so
    # it saw an environment nothing had populated yet and reported the shop id
    # missing while it sat in the file. The whole point of this script is that
    # the credentials file is the source of truth; reading it once, up front,
    # is what makes that true.
    load_credentials()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
