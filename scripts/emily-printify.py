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
  alias hoodie sweatshirt       another word for a product already chosen
  refresh                       fill in blueprint titles, so the garment's own
                                name resolves without an alias
  draft builds/my-slug          build folder -> UNPUBLISHED product, no judgement

Standard library only.
"""

import argparse
import base64
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
import disclosures  # noqa: E402


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


class ApiError(Exception):
    def __init__(self, code, detail, path):
        super().__init__(f"HTTP {code} on {path}: {detail}")
        self.code, self.detail, self.path = code, detail, path


def call(path, body=None, method=None, soft=False):
    """soft=True raises ApiError instead of killing the process.

    Every call used to sys.exit(1) on any HTTP error. That is right for a
    one-shot command and wrong for a sweep: one stale product id returned 404
    and took the whole `status` run with it, so the other builds were never
    looked at and the output said nothing about which product had failed.
    """
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
        if soft:
            raise ApiError(e.code, detail, path)
        print(f"Printify returned HTTP {e.code} on {path}: {detail}", file=sys.stderr)
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


# Printify puts trademark symbols inside product names: "Unisex Heavy Blend(TM)
# Hooded Sweatshirt", "Unisex EcoSmart(R) Crewneck Sweatshirt". A plain
# substring search for "heavy blend hooded" therefore matches nothing, because
# the symbol sits between two of the words you typed - and the only hit is the
# Youth version, which happens not to carry it. That looked like the product
# being absent from the catalogue.
_NOISE = dict.fromkeys(map(ord, "\u2122\u00ae\u00a9\u2120"), None)


def searchable(title):
    """A title with trademark symbols dropped and whitespace collapsed."""
    return " ".join(str(title or "").translate(_NOISE).lower().split())


def matches(title, term):
    return searchable(term) in searchable(title)


def cmd_blueprints(a):
    bps = call("/catalog/blueprints.json")
    term = (a.search or "").lower()
    hits = [b for b in bps if matches(b.get("title"), term)] if term else bps
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

# ECOSYSTEM_ROOT overrides the root - every other script here honours it and
# these two did not, which is also why the catalogue could not be exercised
# from a test without reaching into the module.
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
CATALOG = ROOT / "agents" / "emily" / "state" / "printify-catalog.json"


def read_catalog():
    try:
        return json.loads(CATALOG.read_text())
    except Exception:
        return {}


class Ambiguous(Exception):
    """More than one catalogue entry answers to that word."""

    def __init__(self, word, keys):
        super().__init__(word)
        self.word = word
        self.keys = keys


def words(s):
    """The lowercase words in a string, with a trailing plural s dropped."""
    out = []
    for w in re.split(r"[^a-z0-9]+", str(s or "").lower()):
        if not w:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def title_answers_to(blueprint_title, product_type):
    """Does a blueprint's own title answer to this word?

    Printify puts the garment last - "Unisex Heavy Blend Hooded Sweatshirt",
    "Unisex Jersey Short Sleeve Tee", "Kiss-Cut Stickers" - so the head noun
    is one of the final two words. Matching any word of the title instead made
    "blend" find the hoodie, which is how a loose match becomes a wrong draft.
    Every word asked for must be in the title, and the head must be the
    garment.
    """
    have = words(blueprint_title)
    asked = words(product_type)
    if not have or not asked:
        return False
    return asked[-1] in have[-2:] and all(w in have for w in asked)


def resolve(cat, product_type):
    """(key, entry) for a product type, or (None, None).

    The catalogue key is a word a model picked when it wrote listing.json, and
    the lookup used to be exact. Emily wrote "sweatshirt", the entry had been
    saved as "hoodie", and draft stopped with "no catalogue entry for
    'sweatshirt'. Choose one once" - inviting a second entry for a garment that
    was already chosen. Two catalogue entries for one blueprint is the drift
    this repo keeps paying for.

    So a word reaches an entry three ways, in order:

      the key itself, then the aliases recorded on it, then a word of the
      blueprint's own title - "sweatshirt" finds the entry whose blueprint is
      "Unisex Heavy Blend Hooded Sweatshirt" without anyone maintaining a list.

    Two entries answering to one word is not resolved by guessing: Ambiguous
    says which, and the caller names them.
    """
    want = " ".join(words(product_type))
    if not want:
        return None, None

    for key, entry in cat.items():
        if str(key).lower() == str(product_type).lower():
            return key, entry

    hits = [k for k in cat if " ".join(words(k)) == want]
    if not hits:
        hits = [k for k, e in cat.items()
                if any(" ".join(words(x)) == want for x in (e.get("aliases") or []))]
    if not hits:
        hits = [k for k, e in cat.items()
                if title_answers_to(e.get("blueprint_title"), product_type)]

    if len(hits) > 1:
        raise Ambiguous(product_type, sorted(hits))
    if not hits:
        return None, None
    return hits[0], cat[hits[0]]


def no_entry(cat, product_type):
    """What to print when nothing matches. It lists what IS in the catalogue.

    The old message said only "choose one once", which is wrong advice when
    the garment is already there under another word - and that is exactly the
    case that produced it.
    """
    lines = [f"no catalogue entry for '{product_type}'."]
    if cat:
        lines.append("\n  The catalogue already has:")
        for key, entry in sorted(cat.items()):
            title = entry.get("blueprint_title") or f"blueprint {entry.get('blueprint_id')}"
            extra = ", ".join(entry.get("aliases") or [])
            lines.append(f"    {key:<14} {title}" + (f"  (also: {extra})" if extra else ""))
        asked = set(words(product_type))
        likely = next((k for k, e in sorted(cat.items())
                       if asked & set(words(e.get("blueprint_title")) + words(k))),
                      sorted(cat)[0])
        lines.append(f"\n  If one of those IS this product, say so rather than "
                     f"picking it twice:\n"
                     f"    emily-printify.py alias {likely} {product_type}")
        lines.append(f"\n  If it is genuinely a new product:")
    else:
        lines.append("\n  The catalogue is empty. Choose a blueprint once:")
    lines.append(f"    emily-printify.py suggest --product {product_type}")
    return "\n".join(lines)


def cmd_refresh(a):
    """Fill in blueprint_title on entries saved before it was recorded.

    Aliasing is for a word the catalogue could not have guessed. A word that
    IS the garment's name should never have needed one - "hooded sweatshirt"
    is right there in "Unisex Heavy Blend Hooded Sweatshirt". The hoodie entry
    was chosen before pick started saving that title, so every wording of the
    same garment had to be aliased by hand, one failed draft at a time.

    One read per entry, once.
    """
    cat = read_catalog()
    if not cat:
        print("the catalogue is empty - nothing to refresh")
        return 0
    filled, already, failed = [], [], []
    for key, entry in sorted(cat.items()):
        if entry.get("blueprint_title"):
            already.append(key)
            continue
        bid = entry.get("blueprint_id")
        if not bid:
            failed.append((key, "no blueprint_id"))
            continue
        try:
            title = call(f"/catalog/blueprints/{bid}.json").get("title") or ""
        except Exception as exc:
            failed.append((key, f"{type(exc).__name__}"))
            continue
        if not title:
            failed.append((key, "blueprint has no title"))
            continue
        entry["blueprint_title"] = title
        filled.append((key, title))

    if filled:
        CATALOG.write_text(json.dumps(cat, indent=1) + "\n")

    def reaches(word):
        """Which entry a word reaches, or None if it is ambiguous.

        resolve() raises on ambiguity, and this report walked every word of
        every title straight into that - "cut" reaches both kisscut and
        sticker. A crash here left the file written and the summary half
        printed. Ambiguity is the interesting part of this report, not an
        error in it.
        """
        try:
            return resolve(cat, word)[0]
        except Ambiguous:
            return None

    contested = {}
    for key, title in filled:
        print(f"  {key:<14} {title}")
        extra = sorted({w for w in words(title) if reaches(w) == key})
        if extra:
            print(f"{'':<16}now also answers to: {', '.join(extra)}")
        for word in words(title):
            try:
                resolve(cat, word)
            except Ambiguous as exc:
                contested[word] = exc.keys
    for key in already:
        print(f"  {key:<14} (already had a title)")
    for key, why in failed:
        print(f"  {key:<14} COULD NOT REFRESH - {why}", file=sys.stderr)
    if contested:
        print("\n  These words now match more than one entry. Drafting against "
              "one of them\n  still works - the exact key wins - but any other "
              "wording will be refused\n  rather than guessed at:")
        for word, keys in sorted(contested.items()):
            print(f"    {word:<14} {', '.join(keys)}")
        print("\n  If two of those entries are really the same product, that is "
              "a duplicate\n  worth removing: a price set on one does not apply "
              "to the other.")
    if not filled and not failed:
        print("\nnothing needed refreshing.")
    return 1 if failed else 0


def cmd_alias(a):
    """Teach an existing entry another word, rather than duplicating it."""
    cat = read_catalog()
    if a.product not in cat:
        print(no_entry(cat, a.product), file=sys.stderr)
        sys.exit(2)
    entry = cat[a.product]
    have = list(entry.get("aliases") or [])
    added = []
    for word in a.alias:
        if word == a.product or word in have:
            continue
        clash, _ = (None, None)
        try:
            clash, _ = resolve(cat, word)
        except Ambiguous as exc:
            clash = exc.keys[0]
        if clash and clash != a.product:
            print(f"'{word}' already reaches '{clash}' - two entries answering "
                  f"to one word is the thing this avoids.", file=sys.stderr)
            sys.exit(1)
        have.append(word)
        added.append(word)
    entry["aliases"] = have
    CATALOG.write_text(json.dumps(cat, indent=1) + "\n")
    if added:
        print(f"'{a.product}' now also answers to: {', '.join(added)}")
    else:
        print(f"'{a.product}' already answered to all of those")
    print(f"  aliases: {', '.join(have) or '(none)'}")
    return 0


def cmd_suggest(a):
    """Find candidate blueprints for a product type and show what to do next."""
    bps = call("/catalog/blueprints.json")
    term = (a.product or "").lower()
    hits = [b for b in bps if matches(b.get("title"), term)]
    if not hits:
        hits = [b for b in bps if matches(b.get("title"), term.rstrip("s"))]
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
    elif a.colors:
        want = {c.strip().lower() for c in a.colors.split(",") if c.strip()}
        chosen = [v for v in all_v if (colour_of(v.get("title")) or "").lower() in want]
        missing = want - {(colour_of(v.get("title")) or "").lower() for v in chosen}
        if missing:
            have = sorted({colour_of(v.get("title")) or "?" for v in all_v})
            print(f"no variants in: {', '.join(sorted(missing))}", file=sys.stderr)
            print(f"\n  colours available: {', '.join(have)}", file=sys.stderr)
            sys.exit(1)
    elif len(all_v) > a.limit:
        # Silently keeping the first 12 of 274 is how a hoodie listing ends up
        # with six sizes of Ash and no Black. A sticker has five variants and
        # never hit this; a garment hits it every time.
        have = sorted({colour_of(v.get("title")) or "?" for v in all_v})
        print(f"this product has {len(all_v)} variants - too many to take blindly.",
              file=sys.stderr)
        print(f"\n  colours available ({len(have)}):", file=sys.stderr)
        for c in have:
            print(f"    {c}", file=sys.stderr)
        print(f"\n  Choose some:\n"
              f"    emily-printify.py pick --product {a.product} --blueprint {a.blueprint} "
              f"--provider {a.provider} \\\n        --colors \"{have[0]},{have[1] if len(have)>1 else have[0]}\"\n"
              f"\n  Or take all {len(all_v)}:  --limit {len(all_v)}", file=sys.stderr)
        sys.exit(1)
    else:
        chosen = all_v[:a.limit]

    # The blueprint's own title, so resolve() can match a word Emily used
    # against the garment's real name without anyone maintaining a synonym
    # list. One extra read, once, at the only moment a human is here anyway.
    try:
        blueprint_title = call(f"/catalog/blueprints/{a.blueprint}.json").get("title") or ""
    except Exception:
        blueprint_title = ""

    cat = read_catalog()
    cat[a.product] = {
        "blueprint_id": int(a.blueprint),
        "blueprint_title": blueprint_title,
        "aliases": [w.strip() for w in (a.alias or "").split(",") if w.strip()],
        "provider_id": int(a.provider),
        "variant_ids": [v["id"] for v in chosen],
        # Every title, not the first 12. The cap was invisible while the only
        # product was a five-size sticker; a tee has sizes x colours, so it
        # dropped most of them - and --by-size has nothing to read a size from
        # in a title that was never saved.
        "variant_titles": [v.get("title") for v in chosen],
        "chosen_at": _now(),
    }
    # Recorded only when asked for. Absent means the default, and the default
    # is to cut the background out - see needs_cutout().
    if getattr(a, "no_cutout", False):
        cat[a.product]["cutout"] = False
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps(cat, indent=1) + "\n")
    print(f"saved '{a.product}' -> blueprint {a.blueprint}, provider {a.provider}, "
          f"{len(chosen)} variant(s)")
    for t in cat[a.product]["variant_titles"][:12]:
        print(f"    {t}")
    if len(chosen) > 12:
        print(f"    ... and {len(chosen) - 12} more")
    print(f"\nWritten to {CATALOG}")
    print("Drafts for this product type are now automatic.")


# Printify writes an apparel variant as "S / Black" or "Black / S" depending
# on the blueprint, so the size is found by looking for a known token rather
# than by position.
SIZES = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"]
_SIZE_ALIAS = {"XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL", "XXXXXL": "5XL"}


def size_of(title):
    """The size in a variant title, or None. Case and order insensitive."""
    for part in str(title or "").replace("(", "/").replace(")", "/").split("/"):
        tok = part.strip().upper()
        tok = _SIZE_ALIAS.get(tok, tok)
        if tok in SIZES:
            return tok
    return None


def needs_cutout(entry):
    """Does this product need a transparent background? Yes, unless told.

    This used to answer "a sticker is die-cut, so an opaque square is fine",
    and infer apparel from whether the variants carried garment sizes. Both
    halves were wrong and a real product showed how.

    A die cut follows the artwork's TRANSPARENCY. Given an opaque square, the
    cut has nothing to follow, so Printify falls back to its own shape and
    prints the whole square inside it: the maple leaf came back as a disc with
    a cream band straight across it and white in the corners. It looked right
    in every thumbnail, because a thumbnail of a square IS a square.

    So the default inverts. A background that is meant to print is the unusual
    case and says so - `pick --no-cutout` records it on the catalogue entry.
    Silence now means "cut it out", which is the safe way round: a new product
    type added next year is opaque-by-accident under the old default and
    transparent-by-default under this one.
    """
    if "cutout" in (entry or {}):
        return bool(entry["cutout"])
    return True


def colour_of(title):
    """The colour half of "Dark Heather / 2XL". None if there is no colour."""
    parts = [x.strip() for x in str(title or "").split("/")]
    rest = [x for x in parts if x and size_of(x) is None]
    return " / ".join(rest) if rest else None


def cmd_prices(a):
    """Set a price per variant for a product type, once.

    draft used a single price for every variant, so a five-size sticker went
    up with the same number against every size. Sizes are priced differently;
    that is the whole reason a listing has them.
    """
    cat = read_catalog()
    try:
        _key, entry = resolve(cat, a.product)
    except Ambiguous as exc:
        print(f"'{exc.word}' matches more than one entry: {', '.join(exc.keys)}. "
              f"Name one of those exactly.", file=sys.stderr)
        sys.exit(2)
    if not entry:
        print(no_entry(cat, a.product), file=sys.stderr)
        sys.exit(2)

    ids = entry.get("variant_ids") or []
    titles = entry.get("variant_titles") or []
    # --by-size and --all are handled below; only an empty invocation lists.
    if not a.price and not a.by_size and not a.all_price:
        current = entry.get("prices") or {}
        sizes_seen = {}
        for i, vid in enumerate(ids):
            sz = size_of(titles[i] if i < len(titles) else "")
            if sz:
                sizes_seen.setdefault(sz, []).append(current.get(str(vid)))

        print(f"'{a.product}' has {len(ids)} variant(s).\n")
        if sizes_seen:
            # Apparel. Listing a hundred rows and asking for a hundred numbers
            # in the right order is not a workflow, so show the sizes instead.
            for sz in sorted(sizes_seen, key=lambda z: SIZES.index(z) if z in SIZES else 99):
                vals = sizes_seen[sz]
                set_ = [v for v in vals if v]
                shown = f"${set_[0]/100:.2f}" if set_ else "(unset)"
                print(f"  {sz:<5} {len(vals):>3} variant(s)  {shown}")
            example = " ".join(f"{z}=0.00" for z in
                               sorted(sizes_seen, key=lambda z: SIZES.index(z)
                                      if z in SIZES else 99))
            print(f"\nPrice them by size:\n"
                  f"  emily-printify.py prices --product {a.product} --by-size {example}")
            print(f"\nOr one price for all of them:\n"
                  f"  emily-printify.py prices --product {a.product} --all 21.99")
            return 0

        for i, vid in enumerate(ids):
            t = titles[i] if i < len(titles) else f"variant {vid}"
            now = current.get(str(vid))
            print(f"  {i+1:>2}. {t:<28} {('$%.2f' % (now/100)) if now else '(unset)'}")
        print(f"\nSet them in that order:\n"
              f"  emily-printify.py prices --product {a.product} "
              f"{' '.join('0.00' for _ in ids)}")
        return 0

    # One price per size beats one per variant: a tee is sizes x colours, and
    # typing a hundred numbers in the right order is not a workflow.
    if a.by_size or a.all_price:
        if a.all_price:
            wanted = {vid: float(a.all_price) for vid in ids}
        else:
            table = {}
            for pair in a.by_size:
                if "=" not in pair:
                    print(f"--by-size takes SIZE=PRICE, got {pair!r}", file=sys.stderr)
                    sys.exit(2)
                k, v = pair.split("=", 1)
                k = k.strip().upper()
                table[_SIZE_ALIAS.get(k, k)] = float(v)

            wanted, unmatched = {}, []
            for i, vid in enumerate(ids):
                t = titles[i] if i < len(titles) else ""
                sz = size_of(t)
                if sz in table:
                    wanted[vid] = table[sz]
                else:
                    unmatched.append(f"{t or vid} (size {sz or 'unreadable'})")
            if unmatched:
                # Refusing beats pricing most of them and leaving the rest at
                # Printify's default, which is how a listing ends up with one
                # size priced wrong and nobody noticing until it sells.
                print(f"{len(unmatched)} variant(s) have no price in --by-size:",
                      file=sys.stderr)
                for u in unmatched[:10]:
                    print(f"    {u}", file=sys.stderr)
                if len(unmatched) > 10:
                    print(f"    ... and {len(unmatched) - 10} more", file=sys.stderr)
                have = sorted(set(size_of(t) or "?" for t in titles))
                print(f"\n  sizes present: {', '.join(have)}", file=sys.stderr)
                print(f"  sizes you priced: {', '.join(sorted(table))}", file=sys.stderr)
                sys.exit(1)

        prices = {}
        for vid, dollars in wanted.items():
            cents = int(round(dollars * 100))
            if cents < 100:
                print(f"${dollars:.2f} is below $1.00 - almost certainly a typo.",
                      file=sys.stderr)
                sys.exit(1)
            prices[str(vid)] = cents

        entry["prices"] = prices
        entry["priced_at"] = _now()
        cat[a.product] = entry
        CATALOG.write_text(json.dumps(cat, indent=1) + "\n")

        by = {}
        for i, vid in enumerate(ids):
            sz = size_of(titles[i] if i < len(titles) else "") or "?"
            by.setdefault(sz, []).append(prices[str(vid)])
        for sz in sorted(by, key=lambda s: SIZES.index(s) if s in SIZES else 99):
            c = by[sz]
            print(f"  {sz:<5} {len(c):>3} variant(s)  ${c[0]/100:.2f}")
        print(f"\nSaved {len(prices)} variant price(s). Every draft of "
              f"'{a.product}' from now on uses these.")
        return 0

    if len(a.price) != len(ids):
        print(f"'{a.product}' has {len(ids)} variants and you gave {len(a.price)} "
              f"prices. One price per variant, in the order listed by:\n"
              f"  emily-printify.py prices --product {a.product}", file=sys.stderr)
        sys.exit(1)

    prices = {}
    for vid, dollars in zip(ids, a.price):
        cents = int(round(float(dollars) * 100))
        if cents < 100:
            print(f"${float(dollars):.2f} is below $1.00 - that is almost certainly a "
                  f"typo, and Printify's default is what we are trying to replace.",
                  file=sys.stderr)
            sys.exit(1)
        prices[str(vid)] = cents

    entry["prices"] = prices
    entry["priced_at"] = _now()
    cat[a.product] = entry
    CATALOG.write_text(json.dumps(cat, indent=1) + "\n")
    for i, vid in enumerate(ids):
        t = titles[i] if i < len(titles) else f"variant {vid}"
        print(f"  {t:<28} ${prices[str(vid)]/100:.2f}")
    print(f"\nSaved. Every draft of '{a.product}' from now on uses these.")
    return 0


def cmd_draft(a):
    """Turn a finished build folder into an UNPUBLISHED Printify product.

    Everything here is mechanical: the listing copy and the artwork already
    exist on disk, and the blueprint/provider/variants were chosen once. There
    is no judgement left, which is exactly why it should not be an agent's job.
    """
    # Uses the same resolver as `status`. These were two separate loops, and
    # draft's did not look in agents/emily/builds - so `status` listed
    # crisp-air-hiking-sticker and `draft crisp-air-hiking-sticker` replied
    # "no such build folder" for the folder it had just printed.
    d = _resolve_build(a.build_dir)
    if d is None:
        root = _builds_root()
        have = sorted(x.name for x in root.iterdir() if x.is_dir()) if root.is_dir() else []
        print(f"no such build folder: {a.build_dir}", file=sys.stderr)
        if have:
            print(f"builds in {root}:", file=sys.stderr)
            for n in have:
                print(f"  {n}", file=sys.stderr)
        else:
            print(f"there are no build folders in {root}", file=sys.stderr)
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
    catalog = read_catalog()
    try:
        cat_key, cat = resolve(catalog, product_type)
    except Ambiguous as exc:
        print(f"'{exc.word}' matches more than one catalogue entry: "
              f"{', '.join(exc.keys)}.\n  Draft against one of them explicitly:\n"
              f"    emily-printify.py draft {a.build_dir} --product {exc.keys[0]}",
              file=sys.stderr)
        sys.exit(2)
    if not cat:
        print(no_entry(catalog, product_type), file=sys.stderr)
        sys.exit(2)
    if cat_key.lower() != product_type:
        # Say it out loud. A listing that says "sweatshirt" drafted against the
        # "hoodie" entry is right, but only if nobody has to guess that it was.
        print(f"'{product_type}' -> catalogue entry '{cat_key}'")

    shop_id = os.environ.get("PRINTIFY_SHOP_ID")
    if not shop_id:
        print("PRINTIFY_SHOP_ID is not set - run emily-printify.py check.", file=sys.stderr)
        sys.exit(2)

    # Etsy requires the production partner and the AI use disclosed in the
    # listing. This used to refuse without them, which blocked two finished
    # hoodies over a rule Emily was never told about - a rule enforced in code
    # and absent from the instructions, the failure this repo keeps repeating.
    # The text is fixed, so code writes it. It is also written back to
    # listing.json, so what shipped and what is on disk stay the same thing.
    desc, added = disclosures.ensure(listing.get("description"))
    if added:
        listing["description"] = desc
        (d / "listing.json").write_text(json.dumps(listing, indent=1) + "\n")
        print(f"added {len(added)} required disclosure(s) to the description:")
        for line in added:
            print(f"  + {line}")
        print(f"  (wording lives in agents/emily/state/disclosures.json)")

    ko = Path(__file__).resolve().parent / "knockout.py"

    # EVERY print file is checked first, whatever the product. This used to
    # run for apparel only, because a sticker is die-cut and an opaque square
    # is fine on one - true, and it meant nothing looked at a sticker's file
    # at all. A PHOTOGRAPH of a sticker lying on a desk went to Printify as
    # the artwork: wood grain, ruler and all, and it would have printed that
    # way. It looked perfectly good as a thumbnail, which is how it got past
    # everyone including me.
    print(f"checking {design.name} is artwork and not a photograph ...")
    r = subprocess.run([sys.executable, str(ko), str(design), "--check"],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        print(f"\nnot drafting: this file is not artwork. Printify prints what "
              f"it is given,\n  so a product shot would arrive with its "
              f"background on it.\n  Regenerate the art - emily-assets.py asks "
              f"for flat art on a plain background.", file=sys.stderr)
        sys.exit(1)

    # Apparel needs that background actually removed before it goes anywhere
    # near a garment: an opaque file prints its background as a visible
    # rectangle - a white box on a black hoodie.
    upload_from = design
    if needs_cutout(cat):
        cut = d / "design-cutout.png"
        print(f"cutting the background out of {design.name} so the print has "
              f"none ...")
        r = subprocess.run([sys.executable, str(ko), str(design), str(cut)],
                           capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr)
            print(f"\nnot drafting: the artwork cannot be cut out, and an opaque "
                  f"file prints its\n  background - as a rectangle on a garment, "
                  f"and as whatever shape the die cut\n  falls back to on a "
                  f"sticker.\n"
                  f"  Regenerate the art on a plain, even background.", file=sys.stderr)
            sys.exit(1)
        upload_from = cut

    print(f"uploading {upload_from.name} "
          f"({upload_from.stat().st_size // 1024} KB) ...")
    up = call("/uploads/images.json", {
        "file_name": f"{d.name}.png",
        "contents": base64.b64encode(upload_from.read_bytes()).decode(),
    })
    image_id = up.get("id")
    if not image_id:
        print(f"upload returned no image id: {up}", file=sys.stderr)
        sys.exit(1)
    print(f"  image {image_id}")

    variant_ids = cat["variant_ids"]

    # Per-variant prices if they have been set for this product type, and a
    # single fallback price if not. Sizes are priced differently - putting one
    # number against all five was the bug this replaces.
    saved = cat.get("prices") or {}
    fallback = int(round(float(listing.get("price_suggestion") or 0) * 100)) or 599
    price_of = {v: int(saved.get(str(v), fallback)) for v in variant_ids}
    unset = [v for v in variant_ids if str(v) not in saved]
    if unset:
        print(f"note: {len(unset)} of {len(variant_ids)} variants have no price set, "
              f"so they go up at ${fallback/100:.2f}. Set them properly with:\n"
              f"  emily-printify.py prices --product {product_type}")

    spec = {
        "title": str(listing.get("title") or d.name)[:140],
        "description": str(listing.get("description") or ""),
        "tags": [str(t)[:20] for t in (listing.get("tags") or [])][:13],
        "blueprint_id": cat["blueprint_id"],
        "print_provider_id": cat["provider_id"],
        "variants": [{"id": v, "price": price_of[v], "is_enabled": True} for v in variant_ids],
        "print_areas": [{
            "variant_ids": variant_ids,
            "placeholders": [{
                "position": "front",
                "images": [{"id": image_id, "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}],
            }],
        }],
    }

    lo, hi = min(price_of.values()), max(price_of.values())
    span = f"${lo/100:.2f}" + (f"-${hi/100:.2f}" if hi != lo else "")
    print(f"creating the product ({len(variant_ids)} variants at {span}) ...")
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
                   "price_low": lo / 100, "price_high": hi / 100,
                   "drafted_at": _now()})
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
    return ROOT / "agents" / "emily" / "builds"


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
    prod = call(f"/shops/{shop_id}/products/{pid}.json", soft=True)
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

    try:
        shops = call("/shops.json", soft=True)
        names = {str(x.get("id")): x.get("title") for x in shops}
        if str(shop_id) not in names:
            print(f"PRINTIFY_SHOP_ID={shop_id} is not one of your shops "
                  f"({', '.join(f'{k} ({v})' for k, v in names.items()) or 'none connected'}).",
                  file=sys.stderr)
            sys.exit(2)
        print(f"shop {shop_id} ({names[str(shop_id)]})\n")
    except ApiError as exc:
        print(f"cannot list shops: {exc}", file=sys.stderr)
        sys.exit(1)

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

        used_shop = build.get("printify_shop_id") or shop_id
        try:
            live = _live_state(used_shop, pid)
        except ApiError as exc:
            if exc.code == 404:
                print(f"{d.name:<34} GONE   product {pid} is not in shop {used_shop}")
                print(f"{'':<34}        it was deleted in Printify, or it belongs to a "
                      f"different shop than PRINTIFY_SHOP_ID={shop_id}")
                print(f"{'':<34}        re-create it with: "
                      f"emily-printify.py draft {d.name}")
            else:
                print(f"{d.name:<34} ERROR  {exc}")
            continue
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
    p.add_argument("--colors", "--colours", default="", dest="colors",
                   help='comma-separated colour names, e.g. --colors "Black,Navy"')
    p.add_argument("--no-cutout", dest="no_cutout", action="store_true",
                   help="this product prints its background - a poster, an "
                        "all-over print. Everything else gets the background "
                        "removed before upload.")
    p.add_argument("--alias", default="",
                   help="other words that mean this product, comma-separated")
    p.set_defaults(fn=cmd_pick)

    sub.add_parser("refresh").set_defaults(fn=cmd_refresh)

    p = sub.add_parser("alias"); p.add_argument("product")
    p.add_argument("alias", nargs="+"); p.set_defaults(fn=cmd_alias)

    p = sub.add_parser("prices"); p.add_argument("--product", required=True)
    p.add_argument("price", nargs="*")
    p.add_argument("--by-size", nargs="+", dest="by_size", metavar="SIZE=PRICE",
                   help='price by size, e.g. --by-size S=21.99 M=21.99 2XL=23.99')
    p.add_argument("--all", dest="all_price", metavar="PRICE",
                   help="one price for every variant")
    p.set_defaults(fn=cmd_prices)

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
