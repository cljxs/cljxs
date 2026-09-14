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
  providers  <blueprint_id>     print providers for that product
  variants   <bp_id> <pp_id>    sizes/colours and their variant ids
  upload     <file>             upload artwork, returns an image id
  create     --spec spec.json   create the product, UNPUBLISHED

Standard library only.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

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
    print(f"\n{len(hits)} match(es). Use the id with: providers <id>")


def cmd_providers(a):
    for p in call(f"/catalog/blueprints/{a.blueprint_id}/print_providers.json"):
        print(f"  {p['id']:>6}  {p['title']}")


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

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
