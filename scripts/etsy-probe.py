#!/usr/bin/env python3
"""
etsy-probe.py — what does the Etsy API actually give us?

    etsy-probe.py ping                 is the key alive and approved?
    etsy-probe.py search fall sticker  what comes back for a keyword?
    etsy-probe.py search fall sticker --save   ...and keep it as a fixture

This is a PROBE, not the researcher. Its job is to answer "what fields are
really in the response" before anything is built on them - every parser bug
in this repo was a format assumption, and the way to avoid the next one is to
look at a real payload first.

WHY A KEY AT ALL. Scout invents ideas out of the model's own head today, and
an agent asked "what is selling on Etsy" with no data source will produce a
confident, detailed, plausible answer that is fiction. That is worse than no
researcher, because fiction with numbers on it gets acted on. This is the
only source that gives code on this droplet real listings, prices, tags and
favourite counts.

THE KEY IS TWO VALUES JOINED BY A COLON. Not documented anywhere obvious -
the API says so itself if you ask without one:

    {"error":"Invalid API key: should be in the format 'keystring:shared_secret'."}

Both come from https://www.etsy.com/developers/your-apps. The shared secret is
a credential: it lives in credentials.env, mode 600, and is never printed -
not by this script, not in an error, not in a log.

Standard library only.
"""

import importlib.util
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent

# THE credentials.env parser, imported rather than written again. A second one
# would drift, and the first thing it would drift on is the blank-line and
# quote handling that took two goes to get right.
_spec = importlib.util.spec_from_file_location("emily_assets", SCRIPTS / "emily-assets.py")
ea = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ea)

# THE redactor, imported rather than written again. capture-fixtures.py
# already knows every credential value on this box and every shape worth
# masking; a second copy here would be the one that forgets a rule.
_cf = importlib.util.spec_from_file_location("capture_fixtures",
                                             SCRIPTS / "capture-fixtures.py")
cf = importlib.util.module_from_spec(_cf)
_cf.loader.exec_module(cf)

FIXTURES = ROOT / "tests" / "fixtures"

# How many listings to keep in a saved fixture. The point is the SHAPE - what
# fields are really there, what a price and a timestamp really look like -
# and five of those is as informative as twenty-five and a quarter the size.
FIXTURE_ROWS = 5

API = "https://api.etsy.com/v3/application"
UA = "Mozilla/5.0 (compatible; cljxs-etsy-probe/1.0)"

# Where the key lives. Scout's own state dir: it is Scout's research key, and
# keeping it out of Emily's file means a leak of one is not a leak of both.
CRED = ROOT / "agents" / "scout" / "state" / "credentials.env"


def api_key():
    """keystring:shared_secret, from the environment or Scout's credentials."""
    env = dict(ea.read_env_file(CRED))
    key = (os.environ.get("ETSY_API_KEY") or env.get("ETSY_API_KEY") or "").strip()
    if not key:
        return None, (
            f"ETSY_API_KEY is not set.\n"
            f"  Put it in {CRED} as one line:\n"
            f"    ETSY_API_KEY=<keystring>:<shared_secret>\n"
            f"  Both values are at https://www.etsy.com/developers/your-apps.\n"
            f"  Then: chmod 600 that file. Never paste the secret into a chat.")
    if ":" not in key:
        # The API's own words. Worth catching here because its 403 looks
        # identical to "your key is wrong", and the two need different fixes.
        return None, ("ETSY_API_KEY has no colon in it. Etsy wants BOTH values:\n"
                      "    ETSY_API_KEY=<keystring>:<shared_secret>")
    return key, None


# What Etsy calls its own rate-limit headers. Read rather than assumed: the
# limits are per API key and differ between applications, so a constant here
# would be a guess about somebody else's account. Every successful response
# carries the current budget, which is the only honest source for it.
LIMIT_HEADERS = ("x-limit-per-second", "x-remaining-this-second",
                 "x-limit-per-day", "x-remaining-today")


def call(path, key):
    """(data, headers, error). Headers carry the rate-limit budget."""
    req = urllib.request.Request(f"{API}{path}", headers={
        "x-api-key": key, "User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30,
                                    context=ssl.create_default_context()) as r:
            return json.load(r), dict(r.headers), None
    except urllib.error.HTTPError as e:
        body = e.read(600).decode("utf-8", "replace")
        # The body is Etsy's, and it never contains the key - but it is
        # printed here rather than the request, which does.
        note = ""
        if e.code == 429:
            # Etsy evaluates QPS first, then QPD, and says how long to wait.
            note = f" retry after {e.headers.get('retry-after', '?')}s"
        return None, dict(e.headers), f"HTTP {e.code}{note}: {body[:400]}"
    except Exception as e:
        return None, {}, f"{type(e).__name__}: {str(e)[:200]}"


def show_limits(headers):
    """Print the budget this key actually has, if the response said."""
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    got = [(h, lower[h]) for h in LIMIT_HEADERS if h in lower]
    if not got:
        return
    print("  rate limit: " + ", ".join(f"{h.replace('x-', '')} {v}"
                                       for h, v in got))


def cmd_ping(key):
    data, headers, err = call("/openapi-ping", key)
    if err:
        print(f"ping failed - {err}", file=sys.stderr)
        if "403" in err:
            print("\n  A 403 here is usually one of two things:\n"
                  "    - the key is not APPROVED yet (Etsy reviews new apps; the\n"
                  "      status is on your Manage Your Apps page), or\n"
                  "    - the keystring and shared secret are the wrong way round.",
                  file=sys.stderr)
        return 1
    print(f"ping ok: {json.dumps(data)}")
    show_limits(headers)
    return 0


def save_fixture(data, words):
    """Write the real payload to tests/fixtures/, redacted, for the parser.

    market-scan.py has to read num_favorers, views, price and
    original_creation_timestamp out of these rows. Every parser bug in this
    repo was a format assumption, and the fix that keeps working is a real
    captured sample that the tests can be re-run against.
    """
    rows = (data.get("results") or [])[:FIXTURE_ROWS]
    keep = {"count": data.get("count"), "results": rows}
    text = cf.redact(json.dumps(keep, indent=2, sort_keys=True),
                     cf.known_secrets())
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / "etsy-listings-active.json"
    path.write_text(text)
    print(f"\n  saved {len(rows)} of {len(data.get('results') or [])} rows to "
          f"tests/fixtures/{path.name}")
    print(f"  ({len(text)} bytes, redacted through capture-fixtures.py) "
          f"for '{' '.join(words)}'")
    return path


def cmd_search(key, words):
    q = urllib.parse.urlencode({"keywords": " ".join(words), "limit": 25,
                                "sort_on": "score"})
    data, headers, err = call(f"/listings/active?{q}", key)
    if err:
        print(f"search failed - {err}", file=sys.stderr)
        return 1
    show_limits(headers)

    rows = data.get("results") or []
    print(f"'{' '.join(words)}' -> {data.get('count', '?')} active listings, "
          f"{len(rows)} returned\n")
    if not rows:
        print("  nothing came back. The keyword may be too narrow.")
        return 0

    # WHICH FIELDS ARE REALLY THERE. The whole point of a probe: build the
    # researcher on what the payload has, not on what a README said it has.
    keys = sorted({k for r in rows for k in r})
    print(f"  fields present ({len(keys)}):")
    for i in range(0, len(keys), 4):
        print("    " + "  ".join(f"{k:<24}" for k in keys[i:i + 4]))

    wanted = ("title", "price", "num_favorers", "views", "tags", "taxonomy_id",
              "when_made", "quantity", "shop_id")
    missing = [w for w in wanted if w not in keys]
    if missing:
        print(f"\n  NOT in the payload: {', '.join(missing)}")
        print("  Anything missing here cannot be part of a scoring rule, no "
              "matter how\n  sensible the rule sounds.")

    print("\n  three rows:")
    for r in rows[:3]:
        price = r.get("price") or {}
        amount = price.get("amount")
        divisor = price.get("divisor") or 100
        shown = f"{amount / divisor:.2f} {price.get('currency_code', '')}" \
            if amount is not None else "?"
        print(f"    {str(r.get('title', ''))[:52]:<52} {shown:>12}  "
              f"favs={r.get('num_favorers', '?')}")

    if "--save" in sys.argv:
        save_fixture(data, words)

    tags = [t for r in rows for t in (r.get("tags") or [])]
    if tags:
        counts = {}
        for t in tags:
            counts[t.lower()] = counts.get(t.lower(), 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
        print(f"\n  commonest tags across those {len(rows)} listings:")
        print("    " + ", ".join(f"{t} ({n})" for t, n in top))
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("ping", "search"):
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    key, why = api_key()
    if not key:
        print(why, file=sys.stderr)
        return 2
    if cmd == "ping":
        return cmd_ping(key)
    if len(sys.argv) < 3:
        print("usage: etsy-probe.py search <keywords...>", file=sys.stderr)
        return 2
    return cmd_search(key, [a for a in sys.argv[2:] if a != "--save"])


if __name__ == "__main__":
    sys.exit(main())
