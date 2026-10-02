#!/usr/bin/env python3
"""
blanks.py - which blank product a listing is printed on, and whether Emily
has the same one.

    python3 scripts/blanks.py "Bookish Sweatshirt | Comfort Colors 1566 Crewneck"

THE ONE PLACE this is decided. market-scan.py reads it from the listings of a
scan, niche-scan.py turns it into a warning when Emily lacks the blank, and
emily-printify.py repeats that warning when she drafts. Three readers, one
matcher.

WHY (owner, 2026-10-02): "find the best selling niches and recreate the
product as much as possible, so using the same shirt, mug or poster. If we
don't have that product available to Emily yet, set some type of signal to
find the closest thing to it." The design stays Emily's own; the BLANK -
the shirt, the mug, the poster stock - is a manufactured product anyone can
print on, and matching the one the sellers who sell use is matching what
buyers chose.

TWO KINDS OF ANSWER.

  * A brand and model, when the seller names one: apparel sellers routinely
    write "Comfort Colors 1717" or "Gildan 18000" because buyers ask for it.
    Matched against Printify's blueprint `brand` and `model` fields.
  * Attributes, when they do not: mugs, posters, stickers and totes are
    sold as "11oz ceramic", "18x24 matte", "kiss-cut vinyl". Weaker - an
    attribute is a description, not a part number - and labelled that way.

Nothing is guessed: a listing that names no blank adds nothing, and a niche
whose sellers name none says "not named".

BELIEVED, NOT YET VERIFIED: the brand list and the wording below are how
sellers commonly write these; no real listing text could be fetched from
the session that wrote this. The first scans on the droplet show what it
finds, and that is where it gets tuned.

Standard library only.
"""

import re
import sys

# Apparel brands, as sellers write them -> the brand as Printify names it.
BRANDS = {
    "comfort colors": "Comfort Colors",
    "gildan": "Gildan",
    "bella canvas": "Bella+Canvas", "bella + canvas": "Bella+Canvas", "bella+canvas": "Bella+Canvas",
    "next level": "Next Level",
    "independent trading": "Independent Trading Co.",
    "champion": "Champion",
    "hanes": "Hanes",
    "jerzees": "Jerzees",
    "port & company": "Port & Company", "port and company": "Port & Company",
    "american apparel": "American Apparel",
    "district": "District",
    "lane seven": "Lane Seven",
}
# A model is letters-optional then 3-5 digits, close after the brand:
# "1717", "C1717", "SS4500", "3001CVC".
MODEL = r"(?:[A-Za-z]{0,3}\d{3,5}[A-Za-z]{0,3})"
_BRAND_RE = re.compile(
    r"\b(" + "|".join(re.escape(b) for b in sorted(BRANDS, key=len, reverse=True)) + r")\b"
    r"(?:\s*(?:®|™|co\.?|brand))*[\s:#\-–,]{0,4}(?:style\s*#?\s*)?(" + MODEL + r")?",
    re.I)

# Attribute vocabularies for products whose sellers rarely name a model.
_SIZE_OZ = re.compile(r"\b(\d{2})\s?oz\b", re.I)
_SIZE_IN = re.compile(r"\b(\d{1,2})\s?(?:\"|in(?:ch(?:es)?)?)?\s?[x×]\s?(\d{1,2})\b", re.I)
ATTRS = {
    "mug": ("ceramic", "enamel", "camp", "travel", "stainless", "glass", "black", "accent"),
    "tumbler": ("stainless", "skinny", "insulated"),
    "poster": ("matte", "glossy", "satin", "framed", "canvas", "unframed"),
    "print": ("matte", "glossy", "satin", "framed", "canvas", "unframed"),
    "sticker": ("kiss-cut", "kiss cut", "die-cut", "die cut", "vinyl", "holographic", "waterproof", "transparent"),
    "tote": ("canvas", "cotton", "all-over", "all over", "zipper", "heavy"),
}


def model_core(model):
    """'C1717' -> '1717', '3001CVC' -> '3001': the digits are the part number;
    letters around them are how one seller or another writes it."""
    m = re.search(r"\d{3,5}", str(model or ""))
    return m.group(0) if m else None


def identify(text, kind=None):
    """[{blank, brand, model, attrs}] named in one listing's text, most
    specific first. `kind` is the product word of the scan (tshirt, mug ...)."""
    text = str(text or "")
    out, seen = [], set()
    for m in _BRAND_RE.finditer(text):
        brand = BRANDS[m.group(1).lower()]
        model = model_core(m.group(2))
        name = f"{brand} {model}" if model else brand
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append({"blank": name, "brand": brand, "model": model, "attrs": []})
    # A model-less brand mention is dropped when the same text names a model
    # of that brand: "Comfort Colors 1717 ... Comfort Colors shirt" is one blank.
    named = {o["brand"] for o in out if o["model"]}
    out = [o for o in out if o["model"] or o["brand"] not in named]
    if kind in ATTRS and not out:
        low = text.lower()
        attrs = []
        size = _SIZE_OZ.search(text) if kind in ("mug", "tumbler") else _SIZE_IN.search(text)
        if size:
            attrs.append(f"{size.group(1)}oz" if kind in ("mug", "tumbler") else f"{size.group(1)}x{size.group(2)}")
        attrs += sorted({a.replace(" ", "-") for a in ATTRS[kind]
                         if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", low)})
        if attrs:
            out.append({"blank": f"{kind} " + " ".join(attrs), "brand": None, "model": None, "attrs": attrs})
    return out


def tally(texts, sold_ids=None, kind=None, keep=3):
    """What the listings of one search phrase are printed on.

    texts: {listing_id: title + description + materials}. Counted among the
    listings that SOLD (a recent review) when sold_ids is given, with all
    mentions alongside - "what selling sellers use" and "what gets mentioned"
    are different questions."""
    counts = {}
    for lid, text in (texts or {}).items():
        for b in identify(text, kind):
            c = counts.setdefault(b["blank"], dict(b, selling=0, mentions=0))
            c["mentions"] += 1
            if sold_ids is not None and lid in sold_ids:
                c["selling"] += 1
    rows = sorted(counts.values(), key=lambda c: (-c["selling"], -c["mentions"], c["blank"]))
    return rows[:keep]


def _words(s):
    """Lowercase words, hyphens closed up ("Kiss-Cut" -> kisscut, "T-shirt" ->
    tshirt) and a plural s dropped ("Stickers" -> sticker)."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(s or "").lower().replace("-", "")):
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


# Emily's product words, grouped into the families a blueprint title uses.
FAMILIES = {
    "tshirt": {"tshirt", "tee", "shirt", "t"},
    "sweatshirt": {"sweatshirt", "crewneck", "pullover"},
    "hoodie": {"hoodie", "hooded"},
    "poster": {"poster", "print"},
    "sticker": {"sticker", "decal"},
}


def family(kind):
    """'tee', 'shirt', 't-shirt' -> 'tshirt'; 'crewneck' -> 'sweatshirt'."""
    k = str(kind or "").lower().replace("-", "")
    for name, members in FAMILIES.items():
        if k == name or k in members:
            return name
    return k or None


def family_words(kind):
    f = family(kind)
    return FAMILIES.get(f, {f} if f else set())


def matches(blank, entry):
    """Does a catalogue entry / Printify blueprint (brand, model, title) carry
    this blank? 'exact' (brand and model), 'brand', 'attrs', or None."""
    b_brand = (blank.get("brand") or "").lower()
    e_brand = str(entry.get("brand") or "").lower()
    if b_brand and e_brand and b_brand.replace("+", "") == e_brand.replace("+", ""):
        if blank.get("model") and model_core(entry.get("model")) == blank["model"]:
            return "exact"
        return "brand" if not blank.get("model") else None
    if not b_brand and blank.get("attrs"):
        title = _words(entry.get("blueprint_title") or entry.get("title"))
        want = {a.replace("-", "") for a in blank["attrs"]}
        return "attrs" if want <= title else None
    return None


def gap(top, kind, catalog_entries):
    """None when Emily already has the blank the sellers who sell use, else a
    warning: {blank, kind, have} - `have` is what her catalogue does carry
    for that kind, so the warning says what she would print on instead."""
    if not top:
        return None
    best = top[0]
    entries = list(catalog_entries or [])
    if any(matches(best, e) for e in entries):
        return None
    fw = family_words(kind)
    have = [e.get("blueprint_title") or e.get("key") for e in entries
            if fw & (_words(e.get("key")) | _words(e.get("blueprint_title")))]
    return {"blank": best["blank"], "kind": kind, "selling": best.get("selling"),
            "mentions": best.get("mentions"), "have": have}


def closest(blank, kind, blueprints, keep=3):
    """The Printify blueprints nearest to a blank Emily lacks, best first, each
    with why: same model, same brand and kind, or same kind and attributes."""
    kind_words = family_words(kind)
    scored = []
    for bp in blueprints or []:
        how = matches(blank, bp)
        title = _words(bp.get("title"))
        same_kind = bool(kind_words & title)
        if how == "exact":
            rank, why = 0, "same brand and model"
        elif blank.get("brand") and str(bp.get("brand") or "").lower().replace("+", "") \
                == blank["brand"].lower().replace("+", "") and same_kind:
            rank, why = 1, f"same brand ({bp.get('brand')} {bp.get('model') or ''}), same kind"
        elif how == "attrs" and same_kind:
            rank, why = 2, "same kind and the same attributes"
        elif same_kind:
            rank, why = 3, ("same kind, different brand" if blank.get("brand") else "same kind")
        else:
            continue
        scored.append((rank, bp.get("id") or 0, bp, why))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [{"id": bp.get("id"), "title": bp.get("title"), "brand": bp.get("brand"),
             "model": bp.get("model"), "why": why} for _r, _i, bp, why in scored[:keep]]


if __name__ == "__main__":
    for b in identify(" ".join(sys.argv[1:]), kind=None):
        print(b["blank"])
