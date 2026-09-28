"""The rights gate. Nothing is ingested without a source that says why we may
use it.

This is the one check in Clipper that is about law rather than quality, so it
is deliberately blunt: a named kind of permission, and written evidence of it.
It cannot tell whether the evidence is true - that is the owner's job when
adding a source - but it makes "we never recorded why this was allowed" an
impossible state rather than a common one.
"""

RIGHTS = {
    "own": "your own channel or recording",
    "permission": "the rights holder said yes: a clipping campaign, a written OK",
    "cc-by": "Creative Commons Attribution - credit required",
    "cc-by-sa": "Creative Commons Attribution-ShareAlike - credit, same licence",
    "cc-by-nc": "Creative Commons NonCommercial - refused unless allow_noncommercial",
    "public-domain": "no copyright: old works, US federal government",
}

NEEDS_ATTRIBUTION = {"cc-by", "cc-by-sa", "cc-by-nc"}


def check(rights, evidence, attribution=None, allow_noncommercial=False):
    """None if the source may be added, else the reason it may not."""
    if rights not in RIGHTS:
        return (f"unknown rights {rights!r} - one of: "
                + ", ".join(f"{k} ({v})" for k, v in RIGHTS.items()))
    if not (evidence or "").strip():
        return ("evidence is required: a link to the licence, the campaign page, or the "
                "message granting permission. It is what you show if a clip is ever disputed.")
    if rights == "cc-by-nc" and not allow_noncommercial:
        return ("cc-by-nc forbids commercial use, and clips posted for views or campaign "
                "pay are commercial. Set allow_noncommercial only if you will never earn from them.")
    if rights in NEEDS_ATTRIBUTION and not (attribution or "").strip():
        return f"{rights} requires credit: give --attribution, e.g. \"Clip from <creator> (CC BY)\""
    return None
