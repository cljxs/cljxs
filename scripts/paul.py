#!/usr/bin/env python3
"""
paul.py - everything in Paul's cycle that is not a judgement call.

Paul finds local businesses with no website or a weak one, the owner picks
which ones (prospects: Approve + build, Reject), and Paul builds each a
site and drafts a pitch that the owner sends. Choosing the business,
designing the site and writing the words are the model's job. Everything
else lives here, because CLAUDE.md's two rules were each learned the
expensive way: anything a script can do exactly, a script does; and an
agent is judged by code reading its files, not by what it says it did.

So Paul never deploys, never writes a record and never contacts anyone. It
writes four things - work/target.json, work/site/, work/draft.md and
state/last-run.txt - and this file does the rest.

    the owner     init  sender  preflight  status  mark  exclude
                  teardown  go-live  dry-next  revise  prospects  approve  reject  build  find
                  remove  places  photo
    the wrapper   should-run  brief  finish        (scripts/paul-cycle.sh)
    Paul itself   seen  check

    python3 scripts/paul.py preflight          could Paul run? wakes nothing
    python3 scripts/paul.py status             pitches waiting on you, previews
    python3 scripts/paul.py mark <slug> sent   after you send one
    python3 scripts/paul.py dry-next           the next run is a dry cycle
    python3 scripts/paul.py revise <name> "..."  send a site back with what to fix

WHAT A PREVIEW IS. A site built under a real business's name before that
business has agreed to anything. It must never pass for their official site,
so code - not the model - adds everything that says what it is: a noindex
tag on every page, an X-Robots-Tag header, a robots.txt that disallows
everything, and a one-line footer naming who made it. The address is
<short-name>-concept-<6 random characters>, never the bare business name.
It comes down after TEARDOWN_DAYS unless they replied, and at once if they
said no.

Standard library only. Network: the business's own current site (to check
what Paul says is wrong with it), Vercel (deploy, verify, tear down), the
reference sites (are they up today) and OpenRouter's key endpoint through
budget.py, and Google's Places API for one lookup per new prospect when
GOOGLE_PLACES_KEY is set. Nothing else.
"""

import argparse
import concurrent.futures
import csv
import hashlib
import html
import importlib.util
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402
import remember  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# emily-assets.py owns the credentials.env format. A second parser here would
# be the preflight bug again: two readers, one accepting `KEY = value`, the
# other not, and a working setting reported as absent.
ea = _load("emily_assets", "emily-assets.py")


def A(*parts):
    """A path inside Paul's folder. A function rather than constants so a test
    can point ROOT somewhere else after import."""
    return ROOT.joinpath("agents", "paul", *parts)


# --------------------------------------------------------------------------
# the rules, in one place
# --------------------------------------------------------------------------

MAX_PASSES = 2            # check runs that find problems, before Paul stops
TEARDOWN_DAYS = 30        # a preview with no reply comes down after this
RESUME_DAYS = 3           # an interrupted build older than this is set aside
MAX_WAITING = 5           # unsent pitches before code stops building more
DEFAULT_MIN_LEFT = 0.50   # dollars of today's AI allowance a cycle needs to start
MAX_IMAGE_BYTES = 2_000_000
MAX_SITE_BYTES = 8_000_000
MAX_DRAFT_WORDS = 200     # was 170; the yearly fee and the changes line need room
MIN_DRAFT_WORDS = 25

OPT_OUT = "If this isn't for you, just say so and I won't follow up."
CONCEPT_ID = "paul-concept-note"
NOINDEX = "noindex, nofollow"
PREVIEW_URL = "[PREVIEW URL]"

STATUSES = ("drafted", "sent", "replied", "won", "declined", "do-not-contact")
KEEP_LIVE = ("replied", "won")
TAKE_DOWN_NOW = ("declined", "do-not-contact")
CHANNELS = ("email", "facebook", "instagram", "dm", "phone")
KINDS = ("trade", "food", "salon", "retail", "other")
SENDER_FIELDS = ("area", "name", "studio", "email", "phone", "address", "price")
# The offer, owner's decision 2026-10-06: one flat price, stated in the first
# message with what it covers. The price is a setting (paul.py sender --price);
# what it covers is fixed here, so the pitch and the brief cannot disagree.
# Milkbox's pitch, before there was an offer, gave the site away: "yours to
# keep, no strings attached".
OFFER_COVERS = ("the site with their own photos and wording, one round of changes, set up "
                "on their own web address, and no monthly fees")

# THE ONE ONGOING COST (owner, 2026-10-13): a web address renews once a year,
# paid by the business to its registrar - roughly $10-20 for a .com, so the
# pitch says "about $15 a year". Hosting a plain site like Paul's is free.
# "No monthly fees" alone let a buyer think there was no cost at all.
DOMAIN_FEE = 15
YEARLY_RE = re.compile(r"\b(a|per|each|every)\s+year\b|\byearly\b|\bannual(ly)?\b|/\s?(yr|year)\b", re.I)

# WHAT THEY CAN CHANGE AND ADD (owner, 2026-10-13): every pitch says the
# photos and colours are theirs to change, and names one feature that fits
# their kind of business that could be added later - booking haircuts, for
# the barbershop. A feature is a link to a service they choose (a booking
# app, an ordering page), never a form on the site: the no-forms rule holds.
FEATURE_IDEAS = {
    "salon":  ("online booking - a Book button that opens the booking app they choose", r"\bbook"),
    "food":   ("their menu online, or a link to order ahead", r"\bmenu\b|\border"),
    "trade":  ("a button to request a quote by text or email", r"\bquote"),
    "retail": ("a page showing what they sell, with photos", r"\bproducts?\b|\bshop\b|\bcatalog"),
    "other":  ("online booking, or a way to request a quote", r"\bbook|\bquote"),
}
FREEBIE = ("no strings attached", "yours to keep", "for free", "free of charge", "at no cost",
           "no charge", "on the house", "free website", "free site")

# What makes a business a target. Each one is something a person can check,
# so "their site looks dated" is not on the list - that is a vibe. The last
# three (owner, 2026-10-19: Paul checked six businesses and found nothing,
# because a working-but-weak site counted for nothing) are weak, not broken,
# and code confirms each from the page itself.
OUTDATED_YEARS = 3
PROBLEMS = {
    "no-site": "no website findable by search or on its Maps listing",
    "social-only": "its only web presence is a Facebook or Instagram page",
    "no-seo": "the home page has no title or no description for search engines",
    "outdated": f"the newest copyright year on the page is {OUTDATED_YEARS}+ years old",
    "dead-domain": "the website address does not load at all",
    "parked": "the domain shows a parking or for-sale page",
    "no-https": "the site only loads without HTTPS",
    "not-mobile": "the site is unusable on a phone",
    "broken": "broken links or images",
    "wrong-info": "wrong key information (hours, phone, address)",
}
SOCIAL_HOSTS = ("facebook.com", "fb.com", "fb.me", "instagram.com")


def is_social(url):
    host = urllib.parse.urlparse(url if "//" in str(url) else "https://" + str(url)).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)


def seo_basics(body):
    """(has a title, has a description) on a page."""
    title = re.search(r"<title[^>]*>\s*[^<\s][^<]*</title>", body or "", re.I)
    desc = re.search(r"<meta[^>]+name=[\"']?description[\"']?[^>]+content=[\"']\s*[^\"'\s]", body or "", re.I) or \
        re.search(r"<meta[^>]+content=[\"']\s*[^\"'\s][^>]+name=[\"']?description", body or "", re.I)
    return bool(title), bool(desc)


def copyright_year(body):
    """The newest year written next to a copyright mark, or None."""
    years = [int(y) for y in re.findall(r"(?:©|&copy;|&#169;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})",
                                        body or "", re.I)]
    return max(years) if years else None

# The quality bar. Two of the five were down when this was written
# (2026-10-03, DEPLOYMENT_NOT_FOUND), so brief() asks each one every cycle
# and only hands Paul the ones that load.
REFERENCES = (
    ("trade, home services", "https://rourke-plumbing.vercel.app"),
    # Real contractors' sites, the owner's suggestion (2026-10-16), from
    # Contractor Gorilla's list of construction websites: samples that might
    # help, "he isn't required to use anything from that site". REAL
    # businesses - ideas only, never a fact; their quote forms are not ours.
    ("optional idea, trade: trust line", "https://peepels.com/"),
    ("optional idea, trade: phone CTA", "https://abcseamless.com/"),
    ("optional idea, trade: red CTAs", "https://www.rebath.com/"),
    ("optional idea, trade: services", "https://www.restoretacticsinc.com/"),
    ("optional idea, trade: gallery", "https://ebersonconstruction.com/"),
    ("auto shop", "https://repair-champion-auto.vercel.app"),
    ("craft, furniture, product", "https://austin-furniture-upholstery.vercel.app"),
    ("food, cafe", "https://ferngrove-coffee.vercel.app"),
    ("salon, spa, wellness", "https://vellum-hair-umber.vercel.app"),
)

# Demo data inside those reference sites, read off the live pages on
# 2026-10-03. They are style references with invented facts - (512) 555-0188
# is a number reserved for fiction, "TX Master Plumber #41207" is made up -
# and none of it may reach a real business's site.
DEMO_FACTS = ("555-0188", "555-0142", "555-0173", "41207", "4,200+ jobs",
              "1109 woodland", "rourke", "ferngrove", "vellum", "repair champion",
              "austin furniture")
# templates/<kind>/ marks what Paul fills in as {{NAME}}, {{HERO_IMG}} ...
TEMPLATE_SLOT = re.compile(r"\{\{\s*[A-Z][A-Z0-9_]*\s*\}\}")
FICTIONAL_PHONE = re.compile(r"(?<!\d)555[\s.-]?01\d\d(?!\d)")

# Copy that talks about the site instead of the business, and leftovers.
META_COPY = ("real photo", "not a stock", "stock photo", "our new site", "our new website",
             "this new site", "this new website", "lorem ipsum", "placeholder",
             "ai-generated", "ai generated", "sample text", "your business name",
             "[insert", "todo:")

PARKED_MARKERS = ("domain is for sale", "buy this domain", "this domain may be for sale",
                  "parked free", "domain parking", "hugedomains", "sedoparking",
                  "parkingcrew", "afternic", "this domain has expired",
                  "domain has expired", "renew this domain", "is for sale!")

# Stock is Wikimedia Commons (owner, 2026-10-26: "Switch to Wikipedia").
# Unsplash's terms bar reaching it with "bots, spiders, scripts" outside its
# API, and Pexels' bar "programs or robots for automatic data collection" -
# which is how Paul fetched them. Commons files are openly licensed and its
# API is for scripts; each photo is credited on the page by code.
STOCK_HOSTS = ("commons.wikimedia.org",)
# "sample": a stock photo standing in for something the business has and the
# owner has no picture of - Royal Edge's products (owner, 2026-10-21: "just
# use sample pictures and put 'sample' on the picture"). Code measures the
# word on the picture (IMAGE_JS) and the pitch says so.
PHOTO_KINDS = ("business", "stock", "sample")
RASTER = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
FONTS = (".woff", ".woff2", ".ttf", ".otf", ".eot")

CONTACTED_FIELDS = ("date", "slug", "business", "phone", "area", "preview_url",
                    "channel", "status")
DEPLOY_FIELDS = ("slug", "preview_url", "business", "deployed", "teardown",
                 "status", "removed")

VERCEL_API = "https://api.vercel.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) paul-preview-check/1.0"
# Owner, 2026-10-05, from two checklists he trusts (Yates, "don't look
# vibecoded" and "don't get sued"). These are the ones a script can see; the
# rest are rules in the header. Phrases are the stock lines a model reaches
# for when it has nothing specific to say - copy that fits any business.
AI_COPY = ("elevate your", "elevate the", "nestled in", "nestled on", "unlock ", "seamless",
           "delve", "a testament to", "look no further", "whether you're", "whether you are",
           "in the heart of", "welcome to", "where tradition meets", "where quality meets",
           "experience the difference", "your one-stop", "one-stop shop", "second to none",
           "passion for excellence", "crafted with love", "elevated", "curated",
           # Hallmark's list (19), 2026-10-26: one rulebook.
           "robust", "empower", "supercharge", "effortless", "unleash", "next-generation",
           "next generation", "world-class", "best-in-class", "industry-leading", "mission-critical",
           "future-proof")
# A second button that says nothing: Hallmark's stock actions (19).
STOCK_ACTIONS = ("learn more", "get started", "book a demo", "read more", "click here", "discover more")
# The code rules from Hallmark's gates (2026-10-26). Colours and faces live
# once, as custom properties (48): the pitch promises the business can change
# the colours, which is then one line. Motion has a calm version (27).
# Nothing waits for a script to be seen (62).
COLOR_PROPS = re.compile(r"color|background|border|outline|shadow|fill|stroke|caret|decoration|column-rule", re.I)
COLOR_LITERAL = re.compile(r"#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb)\(|\b(?:white|black)\b", re.I)
FONT_GENERIC = re.compile(r"[\"']|\b(serif|sans-serif|monospace|system-ui|cursive|ui-sans-serif|ui-serif)\b", re.I)
MOTION = re.compile(r"@keyframes|\banimation(?:-name)?\s*:\s*(?!none\b)|\btransition\s*:[^;}]*transform", re.I)
REVEAL = re.compile(r"reveal|fade|animate|appear|\baos\b|in-?view|on-?scroll", re.I)
EM_DASH = "\u2014"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\uFE0F]")
CUSTOM_CURSOR = re.compile(r"cursor\s*:\s*(none|url\()", re.I)
FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")
VALEDICTION = re.compile(r"^(best|thanks|thank you|cheers|regards|kind regards|warm regards|"
                         r"best regards|sincerely|all the best|talk soon)\W*$", re.I)
DOH = "https://dns.google/resolve?type=MX&name="
# Claims a business can be held to - and a site can get it into trouble for
# if they are not true. College Hill Barbers' concept (2026-10-06) said
# "Certified Stylists ... stay trained in the latest techniques", in no
# source Paul read. Each match must be backed by an entry in target.json
# "claims" whose text holds the same words, with the URL it came from. A year
# ("since 1911") is backed by any claim holding that year.
CLAIM_PATTERNS = (
    r"\bcertified\b", r"\blicensed\b", r"\binsured\b", r"\baward[- ]winning\b",
    r"\bawards?\b", r"\boldest\b", r"#\s?1\b", r"\bnumber one\b", r"\btop[- ]rated\b",
    r"\bfamily[- ]owned\b", r"\blocally[- ]owned\b", r"\bvoted\b", r"\bguarantee[sd]?\b",
    r"\borganic\b", r"\blocal(?:ly)?[- ](?:sourced|ingredients|farms?)\b", r"\b\d+\+?\s+years\b",
    r"\bmaster barbers?\b", r"\bbest (?:in|of) \w+",
)
CLAIM_YEAR = re.compile(r"\b(?:since|est\.?|established|founded)\s+(?:in\s+)?((?:1[89]|20)\d\d)\b",
                        re.I)
BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
SHOTS = (("mobile", 390, 844), ("desktop", 1440, 900))

US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il",
    "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne",
    "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd",
    "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "alabama", "alaska", "arizona",
    "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma",
    "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "usa", "us", "united states",
}


# --------------------------------------------------------------------------
# small things
# --------------------------------------------------------------------------

def today():
    """The Eastern date. et_time owns the clock."""
    return et_time.day()


def day_plus(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, p)


def read_rows(path, fields):
    try:
        with open(path, newline="") as f:
            return [{k: (r.get(k) or "") for k in fields} for r in csv.DictReader(f)]
    except OSError:
        return []


def write_rows(path, fields, rows):
    """Rewrites the whole file. Rows are changed in place and never dropped:
    contacted.csv is the only memory of who has been pitched, so trimming it
    is how the same business gets a second pitch a month later."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, p)


def append_row(path, fields, row):
    rows = read_rows(path, fields)
    rows.append({k: str(row.get(k) or "") for k in fields})
    write_rows(path, fields, rows)


def contacted():
    return read_rows(A("state", "contacted.csv"), CONTACTED_FIELDS)


def deployments():
    return read_rows(A("state", "deployments.csv"), DEPLOY_FIELDS)


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def norm_phone(s):
    d = digits(s)
    return d[-10:] if len(d) >= 10 else d


def norm_name(s):
    """How two spellings of one business are told to be the same one:
    "Rosa's Tailoring, LLC" and "rosas tailoring" match."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['`’]", "", s).replace("&", " and ")
    stop = {"the", "llc", "inc", "co", "company", "ltd", "corp", "and"}
    return " ".join(w for w in re.findall(r"[a-z0-9]+", s) if w not in stop)


def seen_row(name, phone=""):
    """The contacted.csv row for this business, or None. A name or a phone
    number is enough: a business renamed on Google is still the one we
    pitched."""
    n, p = norm_name(name), norm_phone(phone)
    for r in contacted():
        if n and norm_name(r["business"]) == n:
            return r
        if p and len(p) >= 7 and norm_phone(r["phone"]) == p:
            return r
    return None


def record(text):
    """One line in MEMORY.md. remember.py appends and trims; trimmed lines go
    to memory-archive.md instead of being lost."""
    remember.remember(A(), text, archive=A("memory-archive.md"))


def last_memory_line():
    try:
        lines = A("MEMORY.md").read_text().strip().splitlines()
        return lines[-1] if lines else ""
    except OSError:
        return ""


def load_cycle():
    return read_json(A("state", "current-cycle.json"), {}) or {}


def save_cycle(c):
    write_json(A("state", "current-cycle.json"), c)


def builds():
    """Every finished cycle, one JSON line each. brief() reads the layouts
    from here so Paul can vary them; go-live reads it for the dry cycle."""
    out = []
    try:
        for line in A("state", "builds.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def log_build(entry):
    p = A("state", "builds.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(entry) + "\n")


# A site sent back to Paul, from the owner's note to the new version (owner,
# 2026-10-21: "show me an updated sign... I need to be able to tell the
# process of what's going on"). Keyed by the slug that was sent back:
#   sent -> building (his cycle took it) -> updated (new_slug) | not shipped (why)
# Written where each step happens - revise, brief, finish - never inferred.
REVISIONS = ("state", "revisions.json")


def revisions():
    return read_json(A(*REVISIONS), {}) or {}


def set_revision(slug, **fields):
    allr = revisions()
    allr[slug] = {**allr.get(slug, {}), **fields,
                  "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    write_json(A(*REVISIONS), allr)


def revising():
    """The slug work/ is a revision of, or None."""
    return (read_json(A("work", "revise.json"), {}) or {}).get("slug")


def already_pitched(t):
    """contacted.csv's row that keeps Paul off this business, or None. A
    revision of the business is not picking it again: Royal Edge's fix was
    refused as "already in contacted.csv" (2026-10-21) - by its own row."""
    row = seen_row(t.get("name") or "", t.get("phone", ""))
    if row and revising() and norm_name(row["business"]) == norm_name(t.get("name")) \
            and row.get("status") not in ("declined", "do-not-contact"):
        return None
    return row


# --------------------------------------------------------------------------
# sender, credentials, budget
# --------------------------------------------------------------------------

def load_sender():
    """(details, missing fields). Never committed - this repo is public."""
    d = read_json(A("state", "sender.json"), {}) or {}
    return d, [k for k in SENDER_FIELDS if not str(d.get(k) or "").strip()]


def user_md(s):
    """USER.md, which openclaw injects into every wake. Short on purpose:
    it is re-sent on every call, and the mailing address and phone are code's
    business (the signature), not the model's."""
    return (f"# USER.md - who Paul works for\n\n"
            f"- Name: {s.get('name', '')}\n"
            f"- Studio: {s.get('studio', '')}\n"
            f"- Target area: {s.get('area', '')}\n\n"
            "Paul never contacts anyone. Code adds the preview footer and the pitch\n"
            "signature from these details - Paul does not type them.\n"
            "Generated by `scripts/paul.py sender`; change it there, not here.\n")


def looks_us(area):
    """True when the area ends in a US state, its abbreviation or "USA".
    Outside the US the cold-email rules differ (CASL, PECR, GDPR)."""
    parts = [p.strip().lower() for p in re.split(r",", str(area or "")) if p.strip()]
    if not parts:
        return False
    last = parts[-1]
    if last in US_STATES:
        return True
    words = re.findall(r"[a-z]+(?: [a-z]+)?", last)
    tail = last.split()[-1] if last.split() else ""
    return tail in US_STATES or any(w in US_STATES and len(w) > 2 for w in words)


def credentials():
    return ea.read_env_file(A("state", "credentials.env"))


def vercel_token():
    return (credentials().get("VERCEL_TOKEN") or os.environ.get("VERCEL_TOKEN") or "").strip()


def vercel_team():
    return (credentials().get("VERCEL_TEAM_ID") or os.environ.get("VERCEL_TEAM_ID") or "").strip()


def min_left():
    """Dollars of today's AI allowance a cycle needs before it starts. Lives in
    tasks/limits.json beside the other spend caps, so the owner changes it
    there; the default is a planning number, not a measurement."""
    try:
        v = float((read_json(ROOT / "tasks" / "limits.json", {}) or {}).get(
            "paul_min_left", DEFAULT_MIN_LEFT))
    except (TypeError, ValueError):
        v = DEFAULT_MIN_LEFT
    return v


def own_key():
    """Paul's own OpenRouter key from his credentials.env, or "" - the one
    place that decides which key his budget is read from."""
    return (credentials().get("PAUL_OPENROUTER_KEY") or "").strip()


def budget_source():
    """Which allowance budget_left() read, for preflight to say. Both read
    $0.67 or so on a quiet day, and the owner could not tell from preflight
    whether his new key had been saved (2026-10-05)."""
    return ("Paul's own key" if own_key()
            else "the shared key - PAUL_OPENROUTER_KEY not saved")


def budget_left():
    """Today's AI allowance left, or None when unreadable - unreadable is not
    "over" (budget.py's rule too).

    Paul has his own OpenRouter key (owner, 2026-10-05), so his cycles cannot
    eat the shared key's day - Emily's images need $1.00 of room on it. When
    PAUL_OPENROUTER_KEY is in his credentials.env, what is left is what
    OpenRouter says is left on THAT key (limit_remaining, read by preflight's
    one reader). Without it, the shared allowance from budget.py, as before."""
    own = own_key()
    try:
        if own:
            left = _load("preflight", "preflight.py").key_status(own).get("limit_remaining")
            return round(float(left), 2) if isinstance(left, (int, float)) else None
        return _load("budget", "budget.py").read().get("ai_left_today")
    except Exception:
        return None


# --------------------------------------------------------------------------
# the network
# --------------------------------------------------------------------------

def http_get(url, timeout=20, limit=400_000):
    """One GET. `ok` means the server answered at all - a 404 is an answer.
    Never raises: a dead domain is a result here, not an error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "status": r.status, "url": r.geturl(),
                    "headers": {k.lower(): v for k, v in r.headers.items()},
                    "body": r.read(limit).decode("utf-8", "replace"), "error": ""}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(limit).decode("utf-8", "replace")
        except Exception:
            body = ""
        return {"ok": True, "status": e.code, "url": url,
                "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
                "body": body, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "status": None, "url": url, "headers": {}, "body": "",
                "error": str(getattr(e, "reason", e))[:160]}


class DeployError(Exception):
    pass


def vercel(method, path, token, body=None, data=None, headers=None, team=None, timeout=60):
    """(status, parsed JSON) from Vercel's API. Never prints the token."""
    url = VERCEL_API + path
    if team:
        url += ("&" if "?" in url else "?") + "teamId=" + urllib.parse.quote(team)
    h = {"Authorization": "Bearer " + token}
    h.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read() or b"", e.code
    except Exception as e:
        return None, {"error": {"message": str(getattr(e, "reason", e))[:200]}}
    try:
        return status, (json.loads(raw) if raw.strip() else {})
    except ValueError:
        return status, {"error": {"message": raw[:200].decode("utf-8", "replace")}}


def _vmsg(body):
    err = (body or {}).get("error") or {}
    return err.get("message") or err.get("code") or json.dumps(body)[:200]


def vercel_deploy(site, project, token, team=None, api=None, sleep=time.sleep, wait=180):
    """Upload every file, create a production deployment, wait for it.
    Returns (https url, deployment id). The project is named after the slug,
    so tearing it down later is one DELETE by name."""
    api = api or vercel
    files = []
    for p in sorted(site.rglob("*")):
        if not p.is_file():
            continue
        data = p.read_bytes()
        sha = hashlib.sha1(data).hexdigest()
        status, body = api("POST", "/v2/files", token, data=data, team=team,
                           headers={"Content-Type": "application/octet-stream",
                                    "x-vercel-digest": sha})
        if status not in (200, 201):
            raise DeployError(f"Vercel refused {p.relative_to(site)} ({status}): {_vmsg(body)}")
        files.append({"file": p.relative_to(site).as_posix(), "sha": sha, "size": len(data)})
    if not files:
        raise DeployError(f"nothing to deploy in {site}")

    payload = {"name": project, "files": files, "target": "production",
               "projectSettings": {"framework": None, "buildCommand": None,
                                   "installCommand": None, "outputDirectory": None}}
    status, body = api("POST", "/v13/deployments?skipAutoDetectionConfirmation=1",
                       token, body=payload, team=team)
    if status not in (200, 201):
        raise DeployError(f"Vercel refused the deployment ({status}): {_vmsg(body)}")
    dep_id = body.get("id")

    waited = 0
    while True:
        state = body.get("readyState") or body.get("status")
        if state == "READY" and (body.get("aliasAssigned") or waited >= 30):
            break
        if state in ("ERROR", "CANCELED"):
            raise DeployError(f"the deployment ended {state}: {_vmsg(body)}")
        if waited >= wait:
            raise DeployError(f"the deployment was not ready after {wait}s (state {state})")
        sleep(3)
        waited += 3
        status, body = api("GET", f"/v13/deployments/{dep_id}", token, team=team)
        if status != 200:
            raise DeployError(f"could not read the deployment back ({status}): {_vmsg(body)}")

    # The project's own production domain, not the per-deployment URL: with
    # Vercel's standard protection the generated deployment URL asks for a
    # login, and a link that asks a plumber to log in to Vercel is broken.
    want = f"{project}.vercel.app"
    aliases = [a for a in (body.get("alias") or []) if isinstance(a, str)]
    host = want if want in aliases else next(
        (a for a in aliases if a.endswith(".vercel.app")), want)
    return "https://" + host, dep_id


def vercel_remove(project, token, team=None, api=None):
    """(ok, detail). Deleting the project removes every deployment and frees
    the address. Already gone counts as done."""
    status, body = (api or vercel)("DELETE", f"/v9/projects/{urllib.parse.quote(project)}",
                                   token, team=team)
    if status in (200, 204):
        return True, "removed"
    if status == 404:
        return True, "already gone"
    return False, f"Vercel said {status}: {_vmsg(body)}"


def vercel_whoami(token, team=None, api=None):
    """(username or None, plan or None, error). The plan is reported only when
    Vercel says it - it is never guessed."""
    api = api or vercel
    status, body = api("GET", "/v2/user", token)
    if status != 200:
        return None, None, f"Vercel did not accept the token ({status}): {_vmsg(body)}"
    user = body.get("user") or {}
    plan = ((user.get("billing") or {}).get("plan"))
    tid = team or user.get("defaultTeamId")
    if tid:
        s2, b2 = api("GET", f"/v2/teams/{urllib.parse.quote(tid)}", token)
        if s2 == 200:
            plan = ((b2.get("billing") or {}).get("plan")) or plan
    return user.get("username") or user.get("email") or "?", plan, ""


# --------------------------------------------------------------------------
# screenshots
# --------------------------------------------------------------------------

def find_browser():
    env = os.environ.get("PAUL_BROWSER", "").strip()
    if env and os.access(env, os.X_OK):
        return env
    for b in BROWSERS:
        p = shutil.which(b)
        if p:
            return p
    return None


# Headless Chrome will not lay a page out narrower than 500px, whatever
# --window-size says: measured 2026-10-13, a 390px "phone" screenshot was a
# 500px layout with its right 110px cut off, and a probe at 390 reported
# innerWidth 500. So anything narrower is drawn inside an iframe of exactly
# that width, which is the width the page then lays itself out to.
CHROME_MIN_W = 500


def phone_frame(url, w, h, script=""):
    """A page holding `url` in a w x h iframe, for widths Chrome will not do."""
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>html,body{{margin:0;'
            f'overflow:hidden;background:#fff}}iframe{{border:0;width:{w}px;height:{h}px;'
            f'display:block}}</style></head><body><iframe src="{html.escape(url, quote=True)}">'
            f'</iframe>{script}</body></html>')


def screenshot(browser, url, out, w, h, run=subprocess.run):
    """True only if a real image landed on disk. A browser that exits 0 having
    written nothing is the case this exists to catch."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    frame = None
    if w < CHROME_MIN_W:
        frame = out.with_name(f".{out.stem}-frame.html")
        frame.write_text(phone_frame(url, w, h))
        url = frame.as_uri()
    cmd = [browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--disable-dev-shm-usage", "--allow-file-access-from-files",
           f"--window-size={w},{h}", "--virtual-time-budget=4000", f"--screenshot={out}", url]
    try:
        run(cmd, capture_output=True, timeout=90)
    except Exception:
        return False
    finally:
        if frame:
            frame.unlink(missing_ok=True)
    return out.is_file() and out.stat().st_size > 1000


def fresh_shots(index, folder, browser=None):
    """shoot(), unless the site is unchanged since these screenshots were
    taken - the same reasoning as measured_contrast."""
    folder = Path(folder)
    memo = folder / ".shots.json"
    fp = site_fingerprint(Path(index).parent)
    old = [(n, folder / f"{n}.png") for n, _w, _h in SHOTS]
    if (read_json(memo, {}) or {}).get("site") == fp and all(p.is_file() for _n, p in old):
        return old
    out = shoot(Path(index).as_uri(), folder, browser)
    if all(p for _n, p in out):
        write_json(memo, {"site": fp})
    return out


def shoot(url, folder, browser=None):
    """[(name, path or None)] - None where no picture was taken."""
    browser = browser or find_browser()
    if not browser:
        return [(n, None) for n, _, _ in SHOTS]
    out = []
    for name, w, h in SHOTS:
        p = Path(folder) / f"{name}.png"
        out.append((name, p if screenshot(browser, url, p, w, h) else None))
    return out


# --------------------------------------------------------------------------
# reading a site
# --------------------------------------------------------------------------

class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs, self.text, self.css, self.tel = [], [], [], []
        self.viewport, self.robots = False, []
        self.no_alt, self.embeds, self.scripts, self.styles = [], [], [], []
        self.icon, self.forms = False, 0
        self.ids, self.hashes, self.media = set(), [], []
        self.controls, self._ctl = [], []
        self._skip = 0
        self._style = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
            self._style = tag == "style"
        if tag == "meta":
            name = a.get("name", "").lower()
            if name == "viewport":
                self.viewport = True
            if name == "robots":
                self.robots.append(a.get("content", ""))
        rel = a.get("rel", "").lower()
        if tag in ("a", "button"):
            self._ctl.append([])
        for k in ("id", "name") if tag == "a" else ("id",):
            if a.get(k):
                self.ids.add(a[k])
        if tag in ("video", "audio"):
            self.media.append(a.get("src") or tag)
        if tag == "img" and a.get("src"):
            self.refs.append(("image", a["src"]))
            if not a.get("alt", "").strip():
                self.no_alt.append(a["src"])
        if tag in ("iframe", "embed", "object"):
            self.embeds.append(a.get("src") or a.get("data") or tag)
        if tag in ("form", "input", "textarea", "select"):
            self.forms += 1
        if tag == "link" and "icon" in rel and a.get("href"):
            self.icon = True
        if tag == "link" and "stylesheet" in rel and _external(a.get("href", "")):
            self.styles.append(a["href"])
        if tag in ("img", "source") and a.get("srcset"):
            for part in a["srcset"].split(","):
                u = part.strip().split(" ")[0]
                if u:
                    self.refs.append(("image", u))
        if tag in ("video", "audio", "source") and a.get("src"):
            self.refs.append(("media", a["src"]))
        if tag == "video" and a.get("poster"):
            self.refs.append(("image", a["poster"]))
        if tag == "link" and a.get("href"):
            kind = "image" if ("icon" in rel or a.get("as") == "image") else (
                "style" if "stylesheet" in rel else "other")
            self.refs.append((kind, a["href"]))
        if tag == "script" and a.get("src"):
            self.refs.append(("script", a["src"]))
            if _external(a["src"]):
                self.scripts.append(a["src"])
        if tag == "a" and a.get("href"):
            if a["href"].lower().startswith("tel:"):
                self.tel.append(a["href"])
            elif len(a["href"]) > 1 and a["href"].startswith("#"):
                self.hashes.append(a["href"])
            else:
                self.refs.append(("link", a["href"]))
        if a.get("style"):
            self.css.append(a["style"])

    def handle_endtag(self, tag):
        if tag in ("a", "button") and self._ctl:
            t = " ".join("".join(self._ctl.pop()).split())
            if t:
                self.controls.append(t)
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
            self._style = False

    def handle_data(self, data):
        if self._style:
            self.css.append(data)
        elif not self._skip:
            self.text.append(data)
            for c in self._ctl:
                c.append(data)


CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)


def _skip_ref(u):
    low = u.strip().lower()
    return (not low or low.startswith(("#", "mailto:", "tel:", "sms:", "javascript:", "data:")))


def _external(u):
    return re.match(r"^(https?:)?//", u.strip(), re.I) is not None


def strip_note(markup):
    """The page without the footer code adds, so the footer's own words are
    not read as meta copy."""
    return re.sub(rf'(?is)<p[^>]*id="({CONCEPT_ID}|{CREDITS_ID})"[^>]*>.*?</p>', "", markup)


def scan_site(site):
    """Everything check() needs to know about a site folder, read once."""
    site = Path(site)
    out = {"pages": [], "errors": [], "text": "", "tel": [], "images": {},
           "external_images": [], "bytes": 0, "viewport": True, "local_refs": [],
           "no_alt": [], "embeds": [], "scripts": [], "styles": [], "forms": 0,
           "icon": False, "css": [], "media": [], "controls": []}
    pages = sorted(site.rglob("*.html"))
    out["pages"] = [p.relative_to(site).as_posix() for p in pages]
    texts = []
    for page in pages:
        markup = strip_note(page.read_text(errors="replace"))
        # A template slot nobody filled - in text, an alt or a link alike.
        left = sorted(set(TEMPLATE_SLOT.findall(markup)))
        if left:
            out["errors"].append(f"{page.relative_to(site).as_posix()}: template slots left unfilled: "
                                 f"{', '.join(left[:6])} - fill each one, or delete the block it is in")
        pr = _Page()
        try:
            pr.feed(markup)
        except Exception as exc:
            out["errors"].append(f"{page.name} does not parse as HTML: {exc}")
            continue
        if not pr.viewport:
            out["viewport"] = False
        if page.name == "index.html" and page.parent == site:
            out["icon"] = pr.icon
        for k in ("no_alt", "embeds", "scripts", "styles", "media", "controls"):
            out[k] += getattr(pr, k)
        # A menu item that goes nowhere: the Services drop-down's links are
        # #fades, #beards... and each needs its section on the page
        # (owner, 2026-10-21). "#top" is the top of any page.
        gone = sorted({h for h in pr.hashes
                       if urllib.parse.unquote(h[1:]) not in pr.ids and h.lower() != "#top"})
        for h in gone[:4]:
            out["errors"].append(f'{page.relative_to(site).as_posix()} links to "{h}" but nothing on '
                                 f'that page has id="{h[1:]}" - the link goes nowhere')
        out["forms"] += pr.forms
        out["css"] += pr.css
        texts.append(" ".join(pr.text))
        out["tel"] += pr.tel
        refs = list(pr.refs)
        for chunk in pr.css:
            refs += [("css", u) for u in CSS_URL.findall(chunk)]
        for kind, u in refs:
            _ref(site, page.parent, kind, u, out)
    for css in sorted(site.rglob("*.css")):
        out["css"].append(css.read_text(errors="replace"))
        for u in CSS_URL.findall(css.read_text(errors="replace")):
            _ref(site, css.parent, "css", u, out)
    out["text"] = " ".join(" ".join(texts).split())
    out["bytes"] = sum(p.stat().st_size for p in site.rglob("*") if p.is_file())
    return out


def _ref(site, base, kind, u, out):
    u = html.unescape(u).strip()
    if _skip_ref(u):
        return
    path_part = urllib.parse.urlsplit(u).path
    ext = os.path.splitext(path_part.lower())[1]
    if kind == "css":
        kind = "font" if ext in FONTS else "image"
    if _external(u):
        if kind == "image":
            out["external_images"].append(u)
        return
    target = (base / urllib.parse.unquote(path_part)).resolve() if not path_part.startswith("/") \
        else (site / urllib.parse.unquote(path_part.lstrip("/"))).resolve()
    if path_part.endswith("/") or not ext and target.is_dir():
        target = target / "index.html"
    try:
        rel = target.relative_to(site.resolve()).as_posix()
    except ValueError:
        out["errors"].append(f"{u} points outside the site folder")
        return
    if not target.is_file():
        out["errors"].append(f"{u} is linked but work/site/{rel} does not exist")
        return
    out["local_refs"].append(rel)
    if kind == "image" or ext in RASTER:
        out["images"][rel] = target.stat().st_size


def old_site_photos(page):
    body = (page or {}).get("body") or ""
    return len(re.findall(r"<img[^>]+src=[\"'][^\"']+\.(?:jpe?g|png|webp|avif)", body, re.I))


# --------------------------------------------------------------------------
# what Paul wrote, checked
# --------------------------------------------------------------------------

# THE FOCUS: the kind of business every cycle looks for, until cleared. The
# owner wanted restaurants and cafes first (2026-10-08). One file, so the
# brief that asks for it and the check that enforces it read the same answer.
KIND_WORDS = {"food": "a restaurant, cafe, bakery, coffee shop or food truck",
              "trade": "a trade (plumber, roofer, electrician...)",
              "salon": "a salon, barber or spa", "retail": "a shop", "other": "any other business"}


# Hallmark's themes (agents/paul/hallmark/themes/): a material - paper, ink,
# type, surfaces - never a layout. With no reference from the owner, code
# deals three for the kind of business, none used in the last three builds
# (Hallmark's deal; owner, 2026-10-26), and Paul builds in the one he picks.
THEMES_FOR = {
    "trade": ("formwork", "furrow", "blueprint", "terrazzo", "brutal", "ink"),
    "food": ("vellum", "garden", "hedgerow", "solstice", "spore", "squeeze", "halftone", "overprint"),
    "salon": ("loom", "sport", "darkroom", "brutal", "vellum", "overprint"),
    "retail": ("vellum", "loom", "sticker", "squeeze", "terrazzo", "garden", "halftone"),
    "other": ("ink", "broadside", "darkroom", "formwork", "loom", "garden"),
}
DEAL = 3


def deal_themes(kind, rand=None):
    """Three themes for this kind, none of the last three builds' own."""
    rand = rand or secrets.SystemRandom()
    recent = {b.get("theme") for b in [b for b in builds() if b.get("outcome") == "built"][-3:]}
    mine = [t for t in THEMES_FOR.get(kind) or () if t not in recent]
    rest = [t for t in dict.fromkeys(t for ts in THEMES_FOR.values() for t in ts) if t not in recent and t not in mine]
    pick = rand.sample(mine, min(DEAL, len(mine)))
    return pick + rand.sample(rest, DEAL - len(pick))


def focus_kind():
    try:
        k = A("state", "focus").read_text().strip()
    except OSError:
        return None
    return k if k in KINDS else None


def cmd_focus(a):
    path = A("state", "focus")
    if a.clear:
        path.unlink(missing_ok=True)
        print("focus cleared - Paul looks for any kind of business again.")
        return 0
    if not a.kind:
        k = focus_kind()
        print(f"focus: {k} - {KIND_WORDS[k]}" if k else "no focus - any kind of business.")
        return 0
    if a.kind not in KINDS:
        print(f"{a.kind!r} is not a kind. One of: {', '.join(KINDS)}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(a.kind + "\n")
    print(f"focus set: every cycle looks for {KIND_WORDS[a.kind]}, until `paul.py focus --clear`.")
    return 0


# templates/local is a worked example of the quality bar, not a mold. Owner,
# 2026-10-08: "I don't want to use the same premade template style website
# for every site ... I want these websites to be unique to that business using
# things from their facebook or their history." Its stylesheet opens with this
# line, so a site built on it is caught.
TEMPLATE_MARK = "Paul's site template"

# What makes a site theirs: details only true of this business, each with the
# page it came from and the words that show it on the site.
MIN_IDENTITY = 2


def identity_problems(text, t):
    """target.json "identity": at least MIN_IDENTITY details from their
    Facebook, their own pages or their history, each sourced and each shown
    on the page in the words given."""
    rows = [r for r in (t or {}).get("identity") or [] if isinstance(r, dict)]
    errs = []
    page = re.sub(r"\s+", " ", text).lower()
    good = 0
    for r in rows:
        what = str(r.get("detail") or "").strip() or "(no detail)"
        shown = re.sub(r"\s+", " ", str(r.get("on_page") or "")).strip()
        if not re.match(r"^https?://\S+$", str(r.get("source") or "").strip()):
            errs.append(f'identity: "{what}" needs the URL it came from (their Facebook, their '
                        f"site, a news story)")
        elif not shown:
            errs.append(f'identity: "{what}" needs "on_page" - the words on the site that show it')
        elif shown.lower() not in page:
            errs.append(f'identity: "{shown}" is not on the page - show "{what}" on the site, '
                        f"in those words")
        else:
            good += 1
    if good < MIN_IDENTITY:
        errs.append(f"the site shows {good} detail(s) only true of this business; it needs at "
                    f"least {MIN_IDENTITY} in target.json \"identity\" - from their Facebook, their "
                    f"history, their own pages. Nothing to find? This is not the business to "
                    f"build for: status \"below-bar\".")
    return errs


def validate_target(t):
    if not isinstance(t, dict):
        return ["work/target.json is not a JSON object"]
    st = t.get("status")
    if st not in ("built", "no-target", "below-bar"):
        return ['"status" must be "built", "no-target" or "below-bar"']
    if st != "built":
        if not str(t.get("why") or "").strip():
            return [f'status "{st}" needs a "why": one line saying what you looked at']
        return []

    errs = []
    for k in ("name", "category", "address", "phone", "channel", "contact", "layout"):
        if not str(t.get(k) or "").strip():
            errs.append(f'"{k}" is empty')
    if t.get("kind") not in KINDS:
        errs.append(f'"kind" must be one of: {", ".join(KINDS)}')
    elif focus_kind() and t.get("kind") != focus_kind():
        errs.append(f'the focus is "{focus_kind()}" - {KIND_WORDS[focus_kind()]}. This is '
                    f'"{t.get("kind")}". Find one of those, or write status "no-target".')
    ch = t.get("channel")
    if ch and ch not in CHANNELS:
        errs.append(f'"channel" must be one of: {", ".join(CHANNELS)}')
    phone = str(t.get("phone") or "")
    if FICTIONAL_PHONE.search(phone):
        errs.append(f'{phone} is a 555-01xx number - those are reserved for fiction. '
                    f'That is demo data, not this business')
    elif phone and len(digits(phone)) < 7:
        errs.append(f'"phone" {phone!r} is not a full number')
    if t.get("address") and not re.search(r"\d", str(t["address"])):
        errs.append('"address" needs the full street address, number included')
    cur = str(t.get("current_site") or "").strip()
    if not cur:
        errs.append('"current_site" is empty - their URL, or "none"')
    elif cur.lower() != "none" and not re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}", cur, re.I):
        errs.append(f'"current_site" {cur!r} is neither a URL nor "none"')

    src = t.get("sources") if isinstance(t.get("sources"), dict) else {}
    need = ["name", "address", "phone", "contact"]
    if str(t.get("hours") or "").strip():
        need.append("hours")
    for k in need:
        if not re.match(r"^https?://\S+$", str(src.get(k) or "").strip()):
            errs.append(f'sources.{k} needs the URL where you read it this cycle')

    probs = t.get("problems")
    if not isinstance(probs, list) or not probs:
        errs.append('"problems" is empty - a business qualifies only with at least one '
                    'documented problem')
    else:
        for p in probs:
            p = p if isinstance(p, dict) else {}
            if p.get("code") not in PROBLEMS:
                errs.append(f'problem code {p.get("code")!r} is not one of: {", ".join(PROBLEMS)}')
            if not str(p.get("evidence") or "").strip():
                errs.append(f'problem {p.get("code")!r} has no evidence')

    contact = str(t.get("contact") or "").strip()
    if ch == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", contact, re.I):
        errs.append(f'channel is email but contact {contact!r} is not an email address')
    if ch in ("facebook", "instagram", "dm") and not re.match(r"^https?://", contact):
        errs.append(f'channel is {ch} - contact must be the profile URL')
    if ch == "phone" and len(digits(contact)) < 7:
        errs.append('channel is phone - contact must be the number')

    if not isinstance(t.get("photos", []), list):
        errs.append('"photos" must be a list')
    return errs


def verify_problems(t, get=http_get):
    """What code can confirm of Paul's account of the current site.

    Returns (rows, page): rows are (code, verdict, detail) with verdict one of
    confirmed / contradicted / unconfirmed, and page is the fetched site or
    None. Only a contradiction fails a check - absence and wrong hours cannot
    be proved by a fetch, so the evidence Paul wrote stands for those."""
    site = str(t.get("current_site") or "").strip()
    codes = [str((p or {}).get("code") or "") for p in (t.get("problems") or [])
             if isinstance(p, dict)]
    rows = []
    if site and site.lower() != "none" and is_social(site):
        # A Facebook or Instagram page is not a website of their own: the
        # one thing to confirm is that this is what it is.
        for c in codes:
            rows.append((c, "confirmed", f"{site} is a social page, not a site of their own")
                        if c in ("social-only", "no-site") else
                        (c, "contradicted", f"{site} is a social page - the problem is social-only"))
        return rows, None
    if not site or site.lower() == "none":
        for c in codes:
            if c in ("no-site", "wrong-info", "social-only"):
                rows.append((c, "unconfirmed", "cannot be proved by a fetch - your evidence stands"))
            else:
                rows.append((c, "contradicted",
                             'current_site is "none", so there is no site for this to be true of'))
        return rows, None

    hp = re.sub(r"^https?://", "", site, flags=re.I).strip("/")
    s, h = get("https://" + hp), get("http://" + hp)
    s_good = s["ok"] and s["status"] and s["status"] < 400 and str(s["url"]).startswith("https://")
    alive = any(r["ok"] and r["status"] and r["status"] < 400 for r in (s, h))
    page = s if s_good else (h if h["ok"] and h["status"] and h["status"] < 400 else None)
    for c in codes:
        if c == "no-site":
            rows.append((c, "contradicted", f"current_site is {site} - a site exists; name its problem"))
        elif c == "dead-domain":
            rows.append((c, "contradicted", f"{site} loads") if alive else
                        (c, "confirmed", f"no working answer ({s['error'] or s['status']})"))
        elif c == "no-https":
            if s_good:
                rows.append((c, "contradicted", f"https://{hp} loads fine ({s['status']})"))
            elif h["ok"]:
                rows.append((c, "confirmed", f"https fails ({s['error'] or s['status']}) while http answers"))
            else:
                rows.append((c, "unconfirmed", "neither answers - that is dead-domain"))
        elif c == "parked":
            body = ((page or s or {}).get("body") or "").lower()
            hit = next((m for m in PARKED_MARKERS if m in body), None)
            rows.append((c, "confirmed", f'the page says "{hit}"') if hit else
                        (c, "unconfirmed", "no parking text found - your evidence stands"))
        elif c == "social-only":
            rows.append((c, "contradicted", f"{site} is their own site, not a social page"))
        elif c == "no-seo":
            if page and page["body"]:
                has_t, has_d = seo_basics(page["body"])
                rows.append((c, "contradicted", "it has a title and a description") if has_t and has_d else
                            (c, "confirmed", "no " + " and no ".join(
                                w for w, ok in (("title", has_t), ("description", has_d)) if not ok)
                             + " for search engines"))
            else:
                rows.append((c, "unconfirmed", "could not load the page to look"))
        elif c == "outdated":
            yr = copyright_year((page or {}).get("body"))
            now_y = int(today()[:4])
            rows.append((c, "unconfirmed", "no copyright year on the page - your evidence stands") if yr is None else
                        (c, "confirmed", f"the newest copyright year is {yr}") if yr <= now_y - OUTDATED_YEARS else
                        (c, "contradicted", f"the page says {yr}"))
        elif c == "not-mobile":
            if page and page["body"]:
                vp = re.search(r"<meta[^>]+name=[\"']?viewport", page["body"], re.I)
                rows.append((c, "unconfirmed", "it has a viewport tag - your evidence stands") if vp else
                            (c, "confirmed", "no viewport tag: phones get a shrunken desktop page"))
            else:
                rows.append((c, "unconfirmed", "could not load the page to look"))
        else:
            rows.append((c, "unconfirmed", "code cannot check this - your evidence stands"))
    return rows, page


def parse_draft(text):
    head, lines, i = {}, str(text or "").splitlines(), 0
    while i < len(lines) and re.match(r"^\s*(channel|to|subject)\s*:", lines[i], re.I):
        k, v = lines[i].split(":", 1)
        head[k.strip().lower()] = v.strip()
        i += 1
    return head, "\n".join(lines[i:]).strip()


def offer_problems(body):
    """The pitch states the owner's price - that exact figure, no other - and
    what it covers, and never offers the site free."""
    price = digits(load_sender()[0].get("price") or "")
    if not price:
        return []
    errs, low = [], body.lower()
    said = {digits(m) for m in re.findall(r"\$\s?(\d[\d,]*)(?:\.\d\d)?", body)}
    if price not in said:
        errs.append(f"the pitch must state the price: ${price}, one time")
    other = sorted(said - {price, str(DOMAIN_FEE)}, key=int)
    if other:
        errs.append(f"the only prices are ${price} and the ${DOMAIN_FEE} yearly web address - "
                    f"remove ${', $'.join(other)}")
    if "monthly" not in low:
        errs.append('say there are no monthly fees - it is the selling point')
    if not YEARLY_RE.search(body):
        errs.append(f"say the one cost that stays: their web address renews once a year, "
                    f"about ${DOMAIN_FEE}, paid to the registrar")
    if not any(w in low for w in ("web address", "domain", "own address")):
        errs.append("say it goes live on their own web address")
    gave = [f for f in FREEBIE if f in low]
    if gave:
        errs.append(f"{', '.join(repr(f) for f in gave)} offers it free - it is ${price}")
    return errs


def check_draft(text, t):
    errs = []
    head, body = parse_draft(text)
    ch = (head.get("channel") or "").lower()
    if ch not in CHANNELS:
        errs.append(f'draft.md must start with "Channel: <{"|".join(CHANNELS)}>"')
    elif t and t.get("channel") and ch != t["channel"]:
        errs.append(f'draft.md says Channel: {ch} but target.json says {t["channel"]}')
    if not head.get("to"):
        errs.append('draft.md needs a "To:" line - the email, profile URL or number')
    if ch == "email" and not head.get("subject"):
        errs.append('an email needs a "Subject:" line')
    if PREVIEW_URL not in body:
        errs.append(f"the pitch must contain {PREVIEW_URL} exactly - code puts the link there")
    words = len(re.findall(r"\b\w+\b", body.replace(PREVIEW_URL, "")))
    if words > MAX_DRAFT_WORDS:
        errs.append(f"the pitch is {words} words - keep it under {MAX_DRAFT_WORDS}; "
                    f"the link does the selling")
    elif words < MIN_DRAFT_WORDS:
        errs.append(f"the pitch is {words} words - too short to read as a person")
    if re.search(r"(?m)^\s*([-*•]|\d+[.)])\s+", body):
        errs.append("no lists in the pitch - write it the way a person writes a note")
    street = str((t or {}).get("address") or "").split(",")[0].strip().lower()
    if len(street) > 6 and street in body.lower():
        errs.append("the pitch recites their own address back to them - cut it")
    if EM_DASH in body:
        errs.append("em dashes in the pitch read as AI-written - use a comma or a full stop")
    errs += offer_problems(body)
    low = body.lower()
    if not (re.search(r"\b(photos?|pictures?|images?)\b", low) and re.search(r"\bcolou?rs?\b", low)):
        errs.append("say they can change the photos and the colours")
    kind = (t or {}).get("kind")
    if kind in FEATURE_IDEAS and not re.search(FEATURE_IDEAS[kind][1], low):
        errs.append(f"name one feature they could add later - for a {kind} business: "
                    f"{FEATURE_IDEAS[kind][0]}")
    # Owner, 2026-10-06: Paul's prospects have no working site, so their
    # photos sit behind Facebook's or Instagram's login, and an all-stock
    # preview is the usual case (College Hill Barbers, Milkbox). Fine for a
    # concept - but the pitch says so, rather than let stock pass as theirs.
    photos = [p for p in (t or {}).get("photos") or [] if isinstance(p, dict)]
    if any(p.get("kind") == "sample" for p in photos) and "sample" not in body.lower():
        errs.append('some photos are samples - say so in one line, e.g. "the product photos are '
                    'samples until you send me pictures of what you carry"')
    if photos and all(p.get("kind") == "stock" for p in photos) and "placeholder" not in body.lower():
        errs.append('every photo is stock - say so in one line, e.g. "the photos are placeholders '
                    'until you send me yours"')
    me = str(load_sender()[0].get("name") or "").strip().lower()
    tail = [ln.strip() for ln in body.strip().splitlines() if ln.strip()][-3:]
    if any(VALEDICTION.match(ln) or (me and ln.lower().strip(",.") in (me, me.split()[0]))
           for ln in tail):
        errs.append("the pitch signs itself off - stop at the last sentence; code adds the "
                    "user's name and signature")
    extra = sorted(set(re.findall(r"\[[^\]\n]{1,40}\]", body)) - {PREVIEW_URL})
    if extra:
        errs.append(f"remove {', '.join(extra)} - code adds the signature; nothing else "
                    f"gets filled in")
    return errs, head, body, words


def check_site(site, t, old_page=None):
    """Static checks on the built site. (errors, info)."""
    site = Path(site)
    errs, info = [], {}
    if not (site / "index.html").is_file():
        return [f"work/site/index.html does not exist"], info
    sc = scan_site(site)
    errs += sc["errors"]
    info.update(pages=len(sc["pages"]), kb=sc["bytes"] // 1024, images=len(sc["images"]))
    if not sc["viewport"]:
        errs.append('every page needs <meta name="viewport" content="width=device-width, '
                    'initial-scale=1"> - mobile first')
    for u in sc["external_images"][:5]:
        errs.append(f"{u} is hotlinked - download it into work/site/img/ (platform links expire)")
    for rel, size in sorted(sc["images"].items()):
        if size > MAX_IMAGE_BYTES:
            errs.append(f"{rel} is {size // 1024} KB - download a smaller size "
                        f"(paul.py stock get <file> --width 1280)")
    if sc["bytes"] > MAX_SITE_BYTES:
        errs.append(f"the site is {sc['bytes'] // 1024} KB - keep it under "
                    f"{MAX_SITE_BYTES // 1024} KB so it is fast on a phone")
    if any(site.glob("sitemap*.xml")):
        errs.append("remove sitemap.xml - a preview must not be offered to search engines")
    # Royal Edge after mancaveformen.com (owner, 2026-10-21): "get rid of the
    # video since I don't have one". The owner saves photos only, so a video
    # on one of Paul's sites is a stock clip passing as the business.
    for u in sc["media"][:2]:
        errs.append(f"{u}: no video or sound - the business has none to use; show their "
                    f"photos instead")

    text = sc["text"].lower()
    name = str(t.get("name") or "")
    if name and norm_name(name) not in norm_name(sc["text"]):
        errs.append(f'the site never shows the business name "{name}"')
    want = norm_phone(t.get("phone"))
    if want and not any(norm_phone(x) == want for x in sc["tel"]):
        errs.append(f'no working call link: add <a href="tel:+1{want}"> with their real number '
                    f'(one clear call-to-action)')
    if want and want not in digits(sc["text"]):
        errs.append(f"the phone number {t.get('phone')} is not written anywhere on the page")
    street = str(t.get("address") or "").split(",")[0].strip()
    words = street.split()
    if len(words) >= 2 and re.match(r"\d", words[0]):
        # The number and the longest of the next few words: in "1801 E 7th St"
        # that is 7th. Checking the word right after the number checked "e",
        # which every page in English contains.
        name_word = max(words[1:4], key=len).lower().strip(".,")
        if not (words[0].lower() in text and name_word in text):
            errs.append(f'their real street address "{street}" is not on the page')
    for phrase in META_COPY:
        if phrase in text:
            errs.append(f'"{phrase}" - no copy about the site itself or placeholders; just show it')
    own = norm_name(name)
    for fact in DEMO_FACTS:
        if fact in text and norm_name(fact) not in own:
            errs.append(f'"{fact}" comes from a reference site - their facts are demo data')
    if FICTIONAL_PHONE.search(sc["text"]):
        errs.append("a 555-01xx number is on the page - that is fiction, not this business")

    errs += look_and_law(sc)
    errs += seo_problems(site, t)
    errs += claim_problems(sc["text"], t)
    errs += identity_problems(sc["text"], t)
    if any(TEMPLATE_MARK in c for c in sc["css"]):
        errs.append("this site is built on templates/local - that is an example of the quality "
                    "bar, not a layout. Design this one for them, from what is only true of them")

    photos = {str(p.get("file") or "").lstrip("./").removeprefix("site/"): p
              for p in (t.get("photos") or []) if isinstance(p, dict)}
    rasters = [r for r in sc["images"] if r.lower().endswith(RASTER)]
    credits = read_json(site / CREDITS, {}) or {}
    for rel in rasters:
        p = photos.get(rel)
        if not p:
            errs.append(f'{rel} has no entry in target.json "photos" - say where it came from')
            continue
        src = str(p.get("source") or "")
        if rel.startswith(f"img/{SUPPLIED_PREFIX}"):
            if p.get("kind") != "business":
                errs.append(f'{rel} is their own photo, saved by the owner - kind "business"')
            continue
        if not re.match(r"^https?://", src):
            errs.append(f"{rel}: its source must be the URL you downloaded it from")
        if p.get("kind") not in PHOTO_KINDS:
            errs.append(f'{rel}: kind must be "business" (their own posted photo), "stock", or '
                        f'"sample" (stock standing in for what they sell, labelled Sample)')
        elif p.get("kind") in ("stock", "sample"):
            host = urllib.parse.urlsplit(src).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in STOCK_HOSTS):
                errs.append(f"{rel}: stock photos come from Wikimedia Commons only, not {host} - "
                            f'python3 ../../scripts/paul.py stock find <words>')
            elif (credits.get(rel) or {}).get("page") != src:
                errs.append(f"{rel}: fetch it with paul.py stock get, so its credit is recorded - "
                            f"code writes it on the page")
    theirs = sorted(f"img/{x.name}" for x in (site / "img").glob(f"{SUPPLIED_PREFIX}*")) \
        if (site / "img").is_dir() else []
    if theirs and not any(r in rasters for r in theirs):
        errs.append(f"the owner saved {len(theirs)} of their own photos for this site "
                    f"({', '.join(theirs[:3])}{'...' if len(theirs) > 3 else ''}) and it uses none - "
                    f"use them, the hero first")
    had = old_site_photos(old_page)
    if had and not rasters:
        errs.append(f"their current site shows {had} photo(s) and yours shows none - "
                    f"never ship a downgrade")
    info["photos"] = len(rasters)
    return errs, info


def _claimkey(x):
    return re.sub(r"[\s-]+", " ", str(x).lower()).strip()


def claim_problems(text, t):
    """Claim words on the page with no sourced entry in target.json "claims"."""
    claims = [c for c in (t or {}).get("claims") or [] if isinstance(c, dict)]
    # An identity detail is sourced the same way: "since 1911" shown because
    # their Facebook says so needs no second entry.
    claims += [{"text": r.get("on_page"), "source": r.get("source")}
               for r in (t or {}).get("identity") or [] if isinstance(r, dict) and r.get("on_page")
               and re.match(r"^https?://", str(r.get("source") or ""))]
    errs = [f'claims: "{c.get("text", "")}" needs the URL it came from' for c in claims
            if not re.match(r"^https?://\S+$", str(c.get("source") or "").strip())]
    backed = " | ".join(_claimkey(c.get("text") or "") for c in claims
                        if re.match(r"^https?://", str(c.get("source") or "")))
    missing = []
    for pat in CLAIM_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            if _claimkey(m.group(0)) not in backed:
                missing.append(m.group(0))
    for m in CLAIM_YEAR.finditer(text):
        if m.group(1) not in backed:
            missing.append(m.group(0))
    for w in sorted(set(missing), key=str.lower)[:6]:
        errs.append(f'"{w}" is a claim with no source - add it to target.json "claims" with the '
                    f"URL where the business says it, or cut it")
    return errs


def look_and_law(sc):
    """The owner's two checklists (2026-10-05), the parts a script can see:
    what makes a site read as AI-made, and what gets a small business sued.
    No forms, embeds or outside scripts means no data is collected, so no
    privacy, cookie or refund page is needed - and Paul must never write a
    policy for a business that did not write one."""
    errs = []
    text = sc["text"]
    if EM_DASH in text:
        errs.append(f"{text.count(EM_DASH)} em dash(es) in the page text - they read as "
                    f"AI-written; use a comma, a full stop or a plain hyphen")
    emoji = sorted(set(EMOJI.findall(text)) - {"\ufe0f"})
    if emoji:
        errs.append(f"emoji in the page ({' '.join(emoji[:5])}) - use real icons (inline SVG) "
                    f"or none")
    low = text.lower()
    stock = [p.strip() for p in AI_COPY if p in low]
    if stock:
        errs.append(f"stock AI phrasing: {', '.join(repr(p) for p in stock[:5])} - say "
                    f"something only true of this business")
    for src in sc["no_alt"][:5]:
        errs.append(f"{src} has no alt text - describe what the photo shows "
                    f"(screen readers, and the law)")
    if not sc["icon"]:
        errs.append('index.html has no favicon - add <link rel="icon" href="img/icon.svg"> '
                    "with a simple mark that fits the business")
    for u in sc["embeds"][:3]:
        errs.append(f"embedded {u} - no iframes or embeds (they track visitors); link to "
                    f"Maps or the menu instead")
    for u in sc["scripts"][:3]:
        errs.append(f"outside script {u} - no third-party scripts or tracking; write "
                    f"the little JS you need")
    for u in sc["styles"][:3]:
        host = urllib.parse.urlsplit(u if "//" in u else "https:" + u).hostname or ""
        if host not in FONT_HOSTS:
            errs.append(f"outside stylesheet {u} - only Google Fonts may load from elsewhere")
    if sc["forms"]:
        errs.append("a form is on the page - forms collect data and need consent and a privacy "
                    "policy; the call button is the contact")
    if any(CUSTOM_CURSOR.search(c) for c in sc["css"]):
        errs.append("a custom cursor - leave the visitor's cursor alone")
    stock = sorted({t for t in sc["controls"] if t.lower().strip(" .!") in STOCK_ACTIONS})
    if stock:
        errs.append(f'"{stock[0]}" as a button or link - it says nothing; name what it does '
                    f'("See the menu", "Call to book")')
    errs += code_rules(sc["css"])
    return errs


def code_rules(css):
    """Hallmark's gates that read the code (48, 27, 62), over every
    stylesheet, <style> block and style attribute of a site."""
    errs, literal = [], []
    for chunk in css:
        body = re.sub(r"(?s)/\*.*?\*/", "", chunk)
        body = re.sub(r"(?is)@font-face\s*\{[^}]*\}", "", body)
        for prop, val in re.findall(r"(?<![\w-])(-?[a-zA-Z][\w-]*)\s*:\s*([^;{}]+)", body):
            if prop.startswith("--"):
                continue
            bare = re.sub(r"(?i)url\([^)]*\)", "", val)
            if COLOR_PROPS.search(prop) and COLOR_LITERAL.search(bare):
                literal.append(f"{prop}: {val.strip()[:40]}")
            elif prop.lower() == "font-family" and "var(" not in val and val.strip().lower() not in ("inherit", "initial"):
                literal.append(f"{prop}: {val.strip()[:40]}")
            elif prop.lower() == "font" and "var(" not in val and FONT_GENERIC.search(val):
                literal.append(f"{prop}: {val.strip()[:40]}")
    if literal:
        errs.append(f'colours or faces written in place ({len(literal)}, e.g. "{literal[0]}") - declare each '
                    f"once in :root (--color-ink, --font-body ...) and use var(); the pitch promises "
                    f"their colours can change, and then that is one line")
    whole = "\n".join(css)
    if MOTION.search(whole) and "prefers-reduced-motion" not in whole:
        errs.append("motion with no calm version - add @media (prefers-reduced-motion: reduce) that "
                    "stops each animation and transform transition")
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"(?s)/\*.*?\*/", "", whole)):
        sel = sel.strip()
        if (re.search(r"opacity\s*:\s*0(?![.\d])|visibility\s*:\s*hidden", body) and REVEAL.search(sel)
                and not sel.startswith(".js") and not re.search(r"\.(is-)?(visible|shown|in)\b|:hover|:focus", sel)):
            errs.append(f'"{sel[:40]}" hides content until a script shows it - visible by default; hide it '
                        f"only under a .js class the script adds, or animate from visible")
            break
    return errs


# SEARCH: GOOGLE AND AI ANSWERS (owner, 2026-10-16: "Include SEO as well as
# AI SEO"). A preview is kept out of search on purpose - noindex, robots.txt
# - but the site it becomes is the one that must be found, so it is built
# findable from the first draft and checked here, by code:
#   * every page: a <title> (the home page's names the business), a meta
#     description a search result can show whole, and one <h1>;
#   * the home page: Open Graph tags, for link previews in texts and chats;
#   * the home page: schema.org JSON-LD for the business - name, phone and
#     street address that are the ones Paul found, and hours if he has them.
#     Consistent name/address/phone is what local search and AI assistants
#     both match a business on;
#   * AI search: a short FAQ, visible on the page AND as FAQPage JSON-LD -
#     plain questions with plain answers are what an AI answer quotes;
#   * no aggregateRating or review in the markup: a rating Paul wrote is a
#     claim nobody made, and Google treats self-served review markup as spam.
TITLE_MAX = 65
DESC_MIN, DESC_MAX = 50, 160
FAQ_MIN = 3
NOT_THE_BUSINESS = {"FAQPage", "WebSite", "WebPage", "BreadcrumbList", "Question", "Answer",
                    "ImageObject", "Organization"}


class _Seo(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.desc, self.h1, self.og, self.ld = None, None, 0, set(), []
        self._in, self._buf = None, []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in, self._buf = "title", []
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in, self._buf = "ld", []
        elif tag == "h1":
            self.h1 += 1
        elif tag == "meta":
            if a.get("name", "").lower() == "description":
                self.desc = a.get("content", "").strip()
            prop = a.get("property", "").lower()
            if prop.startswith("og:") and a.get("content", "").strip():
                self.og.add(prop)

    def handle_endtag(self, tag):
        if self._in == "title" and tag == "title":
            self.title = " ".join("".join(self._buf).split())
            self._in = None
        elif self._in == "ld" and tag == "script":
            self.ld.append("".join(self._buf))
            self._in = None

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


def _ld_things(blocks):
    """Every JSON-LD object on a page, @graph flattened. (things, errors)."""
    things, errs = [], []
    for raw in blocks:
        try:
            d = json.loads(raw)
        except ValueError as exc:
            errs.append(f"a JSON-LD block does not parse ({exc}) - it must be valid JSON")
            continue
        stack = d if isinstance(d, list) else [d]
        while stack:
            x = stack.pop(0)
            if isinstance(x, dict):
                things.append(x)
                if isinstance(x.get("@graph"), list):
                    stack += x["@graph"]
    return things, errs


def _types(x):
    t = x.get("@type")
    return set(t) if isinstance(t, list) else {t} if isinstance(t, str) else set()


def seo_problems(site, t):
    """What would keep the finished site from being found by Google and
    quoted by AI search - checked on the files, the same way every time."""
    site = Path(site)
    errs = []
    name, phone = str(t.get("name") or ""), norm_phone(t.get("phone"))
    addr = str(t.get("address") or "")
    street = addr.split(",")[0].strip()
    number = street.split()[0] if street.split() and re.match(r"\d", street.split()[0]) else ""
    town = addr.split(",")[1].strip() if addr.count(",") >= 1 else ""
    index = None
    for page in sorted(site.rglob("*.html")):
        rel = page.relative_to(site).as_posix()
        sp = _Seo()
        try:
            sp.feed(strip_note(page.read_text(errors="replace")))
        except Exception:
            continue                                  # scan_site already says it does not parse
        if rel == "index.html":
            index = sp
        if not sp.title:
            errs.append(f"{rel}: no <title> - it is the blue link in a search result")
        elif len(sp.title) > TITLE_MAX:
            errs.append(f"{rel}: the title is {len(sp.title)} characters - keep it under "
                        f"{TITLE_MAX} or Google cuts it off")
        if not sp.desc:
            errs.append(f'{rel}: no <meta name="description"> - write the line a search result shows')
        elif not DESC_MIN <= len(sp.desc) <= DESC_MAX:
            errs.append(f"{rel}: the description is {len(sp.desc)} characters - {DESC_MIN} to "
                        f"{DESC_MAX}, so a search result shows it whole")
        if sp.h1 != 1:
            errs.append(f"{rel}: {sp.h1} <h1> headings - exactly one, saying what the page is")
    if index is None:
        return errs
    if index.title and name and not same_business(name, index.title):
        errs.append(f'index.html: the title "{index.title}" does not name the business')
    if town and town.lower() not in f"{index.title or ''} {index.desc or ''}".lower():
        errs.append(f'index.html: say "{town}" in the title or the description - people search '
                    f'"<what they need> {town}"')
    missing = [p for p in ("og:title", "og:description", "og:image") if p not in index.og]
    if missing:
        errs.append(f"index.html: no {', '.join(missing)} - the preview a shared link shows")
    things, bad = _ld_things(index.ld)
    errs += [f"index.html: {e}" for e in bad]
    biz = [x for x in things if _types(x) and not _types(x) & NOT_THE_BUSINESS and x.get("address")]
    if not biz:
        errs.append('index.html: no schema.org JSON-LD for the business - a <script '
                    'type="application/ld+json"> with the most specific @type (e.g. '
                    '"HomeAndConstructionBusiness", "Plumber", "HairSalon", "Restaurant"), name, '
                    'telephone, address and openingHoursSpecification')
    else:
        b = biz[0]
        if name and not same_business(name, b.get("name")):
            errs.append(f'JSON-LD "name" is {b.get("name")!r}, not the business, {name!r}')
        if phone and norm_phone(b.get("telephone")) != phone:
            errs.append(f'JSON-LD "telephone" must be their number, {t.get("phone")}')
        a = b.get("address") if isinstance(b.get("address"), dict) else {}
        if number and number not in str(a.get("streetAddress") or ""):
            errs.append(f'JSON-LD "address" needs a PostalAddress with streetAddress "{street}", '
                        f'addressLocality, addressRegion and postalCode')
        if str(t.get("hours") or "").strip() and not (b.get("openingHoursSpecification")
                                                      or b.get("openingHours")):
            errs.append('JSON-LD has no openingHoursSpecification - their hours are known, add them')
    if any("aggregateRating" in x or "review" in x for x in things):
        errs.append("JSON-LD has aggregateRating or review - no ratings or reviews in the markup: "
                    "Paul did not collect them, and Google treats it as spam")
    visible = norm_name(scan_site(site)["text"])
    faq = [x for x in things if "FAQPage" in _types(x)]
    qs = [q for f in faq for q in (f.get("mainEntity") or []) if isinstance(q, dict)]
    good = [q for q in qs if str(q.get("name") or "").strip()
            and str(((q.get("acceptedAnswer") or {}) if isinstance(q.get("acceptedAnswer"), dict) else {})
                    .get("text") or "").strip()]
    if len(good) < FAQ_MIN:
        errs.append(f"index.html: AI search quotes plain answers - add a short FAQ ({FAQ_MIN}+ "
                    f"questions a customer asks, answered from what you sourced) on the page, and "
                    f"the same as FAQPage JSON-LD")
    else:
        hidden = [q["name"] for q in good if norm_name(q["name"]) not in visible]
        if hidden:
            errs.append(f'FAQ question "{hidden[0]}" is in the markup but not on the page - every '
                        f"marked-up question must be visible")
    return errs


# Every probe scrolls with behavior:'instant'. The template's
# html{scroll-behavior:smooth} turned scrollIntoView into a glide still under
# way when the probe looked: below the first screen, contrast was skipped
# unsaid and Royal Edge's Sample labels read as behind their photos
# (2026-10-21).
# Measured in the browser, because a colour in CSS is not the colour on the
# screen: Milkbox's "Call us" (2026-10-05) was brown text on a brown button,
# inherited from a{color}, and Paul read the screenshot and missed it. For
# each visible piece of text: its colour against whatever is painted behind
# it, WCAG's ratio, 4.5 (3 for large text). Text over a photo or a gradient
# is skipped - that cannot be judged from colours, and a guess would cost
# Paul a pass for nothing.
CONTRAST_ID = "paul-contrast"
CONTRAST_JS = r"""(function(){
var cv=document.createElement('canvas');cv.width=cv.height=1;var cx=cv.getContext('2d',{willReadFrequently:true});
function rgba(c){cx.clearRect(0,0,1,1);cx.fillStyle='#000';cx.fillStyle=c;cx.fillRect(0,0,1,1);var d=cx.getImageData(0,0,1,1).data;return [d[0],d[1],d[2],d[3]/255];}
function lum(p){var a=[p[0],p[1],p[2]].map(function(v){v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)});return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2];}
function over(t,b){var a=t[3];return [t[0]*a+b[0]*(1-a),t[1]*a+b[1]*(1-a),t[2]*a+b[2]*(1-a),1];}
var MEDIA={IMG:1,VIDEO:1,CANVAS:1,PICTURE:1,SVG:1,svg:1,IFRAME:1};
function behind(el){var r=el.getBoundingClientRect();var x=r.left+Math.min(r.width/2,20),y=r.top+r.height/2;
 var st=document.elementsFromPoint(x,y);var i=st.indexOf(el);if(i<0)return null;var layers=[];
 for(var j=i;j<st.length;j++){var e=st[j];if(MEDIA[e.tagName])return null;var s=getComputedStyle(e);
  if(s.backgroundImage&&s.backgroundImage!=='none')return null;var c=rgba(s.backgroundColor);
  if(c[3]>0){layers.push(c);if(c[3]>=0.999)break;}}
 var b=[255,255,255,1];for(var k=layers.length-1;k>=0;k--)b=over(layers[k],b);return b;}
var out=[],seen=0;var all=document.body.querySelectorAll('*');
for(var n=0;n<all.length&&seen<400;n++){var el=all[n];if(/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(el.tagName))continue;
 var own='';for(var c=el.firstChild;c;c=c.nextSibling)if(c.nodeType===3)own+=c.nodeValue;own=own.replace(/\s+/g,' ').trim();if(!own)continue;
 var s=getComputedStyle(el);if(s.visibility!=='visible'||s.display==='none'||el.getClientRects().length===0)continue;
 var fill=rgba(s.webkitTextFillColor||s.color);if(fill[3]===0)continue;seen++;
 el.scrollIntoView({block:'center',behavior:'instant'});var bg=behind(el);if(!bg)continue;
 var fg=over(rgba(s.color),bg);var L1=lum(fg),L2=lum(bg);var ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
 var px=parseFloat(s.fontSize),w=parseInt(s.fontWeight)||400;var need=(px>=24||(px>=18.66&&w>=700))?3:4.5;
 if(ratio<need)out.push({text:own.slice(0,60),ratio:Math.round(ratio*100)/100,need:need});}
var pre=document.createElement('pre');pre.id='paul-contrast';pre.textContent=JSON.stringify({checked:seen,fails:out});document.body.appendChild(pre);
})();"""


# IS THE PAGE STYLED AT THIS WIDTH. Royal Edge's revision (2026-10-21): in
# the Studio its computer screenshot was a bare page - black serif text on
# white, the photo at full size - while its phone screenshot looked right.
# No check looked: contrast passes black on white, and an unstyled photo is
# shown at its own size. CSS that applies at one width only (an @media
# block never closed) or not at all (a stylesheet that does not load) looks
# the same from here: switching the page's stylesheets off changes almost
# nothing. Measured as the share of elements whose computed style changes.
STYLED_MIN = 0.2
STYLED_JS = r"""function paulStyled(){
var P=['color','backgroundColor','fontFamily','fontSize','fontWeight','marginTop','paddingTop','paddingLeft','display','textTransform','borderTopWidth','lineHeight'];
var els=[document.body].concat([].slice.call(document.body.querySelectorAll('*')).filter(function(e){return !/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|PRE)$/.test(e.tagName);}).slice(0,300));
function sig(){return els.map(function(e){var s=getComputedStyle(e);return P.map(function(k){return s[k];}).join('|');});}
var a=sig(),sh=[].slice.call(document.styleSheets);sh.forEach(function(x){x.disabled=true;});var b=sig();sh.forEach(function(x){x.disabled=false;});
var n=0;for(var i=0;i<a.length;i++)if(a[i]!==b[i])n++;return a.length?Math.round(n/a.length*100)/100:1;}"""
# An element as Paul would find it in his own markup: <nav class="menu main">.
DESC_JS = r"""function desc(e){var c=(e.getAttribute('class')||'').trim().split(/\s+/).slice(0,2).join(' ');
 return '<'+e.tagName.toLowerCase()+(e.id?' id="'+e.id+'"':'')+(c?' class="'+c+'"':'')+'>';}"""
# Bump when a probe learns something new, so a site measured before is
# measured again rather than passed from memory.
PROBE_V = 6
# Why a sample photo's label was not found on it, as Paul is told.
SAMPLE_WHY = {
    "none": "no element saying Sample has its middle inside the photo - a ::before/::after "
            "label cannot be measured, use a real element",
    "small": "the Sample label inside it is hidden or under 10px",
    "behind": "the Sample label is behind the photo - give the label position:absolute and a "
              "z-index above the image",
}


# Does it fit: buttons that break onto two lines (counted per run of text -
# see PHONE_JS), and a page that scrolls sideways with what sticks out. One
# copy, measured on a phone and, since 2026-10-26, on an iPad and a computer
# too (Hallmark's 34 and 49a: at every width, not one).
FIT_JS = r"""function box(n,el){for(var p=n.parentNode;p&&p!==el;p=p.parentNode){if(getComputedStyle(p).display!=='inline')return p;}return el;}
function lines(rs){var ls=[];rs.sort(function(a,b){return a.top-b.top;});
 for(var i=0;i<rs.length;i++){var r=rs[i],h=r.bottom-r.top,on=null;
  for(var j=0;j<ls.length;j++){var l=ls[j],o=Math.min(l.b,r.bottom)-Math.max(l.t,r.top);
   if(o>=0.5*Math.min(h,l.b-l.t)){on=l;break;}}
  if(on){on.t=Math.min(on.t,r.top);on.b=Math.max(on.b,r.bottom);}else ls.push({t:r.top,b:r.bottom});}
 return ls.length;}
function paulWraps(){var out=[];var els=document.body.querySelectorAll('a,button,[class*="btn"],[class*="button"]');
for(var n=0;n<els.length;n++){var el=els[n];var t=(el.innerText||'').replace(/\s+/g,' ').trim();
 if(!t||t.length>40)continue;var s=getComputedStyle(el);
 if(s.visibility!=='visible'||s.display==='none'||el.getClientRects().length===0)continue;
 var runs=[],keys=[],w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,null),tn;
 while((tn=w.nextNode())){if(!tn.nodeValue.trim())continue;var k=box(tn,el),at=keys.indexOf(k);
  if(at<0){keys.push(k);runs.push([]);at=runs.length-1;}
  var rg=document.createRange();rg.selectNodeContents(tn);var rs=rg.getClientRects();
  for(var i=0;i<rs.length;i++){if(rs[i].width>1)runs[at].push(rs[i]);}}
 var most=0;for(var i=0;i<runs.length;i++){most=Math.max(most,lines(runs[i]));}
 if(most>1)out.push({text:t,lines:most});}
return out;}
function paulOver(){var wide=document.documentElement.scrollWidth>window.innerWidth+1;
// What sticks out (Royal Edge, 2026-10-21: "the phone layout scrolls
// sideways" twice, and Paul could not find what): the outermost elements
// past the right edge, leaving out what a clipping box or a fixed one hides.
var over=[];if(wide){var W=window.innerWidth;
 var past=[].slice.call(document.body.querySelectorAll('*')).filter(function(e){var r=e.getBoundingClientRect();if(!(r.right>W+1&&r.width>0))return false;
  for(var a=e;a&&a!==document.body;a=a.parentElement){var s=getComputedStyle(a);if(s.position==='fixed')return false;
   if(a!==e&&s.overflowX!=='visible'&&a.getBoundingClientRect().right<=W+1)return false;}return true;});
 over=past.filter(function(e){return past.indexOf(e.parentElement)<0;}).slice(0,3).map(function(e){
  return {el:desc(e),right:Math.round(e.getBoundingClientRect().right+window.scrollX)};});}
return {wide:wide,over:over};}
"""


# THE LOOK: one rulebook with Hallmark's (owner, 2026-10-26: "combine the
# best parts of both of them to make one rulebook"). Its gates that fit a
# local business's site, measured in the page at each IMAGE_SCREENS width;
# each finding is (rule, detail), its words to Paul in LOOK_SAYS. Hallmark's
# numbers in brackets. Royal Edge's "ABOUT US" and "621 SYCAMORE ST,
# WATERLOO" were eyebrows in tracked capitals - from Paul's own template.
WORDS_PER_SCREEN = 90      # sentences only, in any 900px of a computer screen (58: "about 80")
PHOTO_WALL = 0.34          # of the page below the first screen, in stock photos (83)
LOOK_JS = r"""function paulLook(){
(document.getAnimations?document.getAnimations():[]).forEach(function(a){try{if(a.effect.getTiming().iterations!==Infinity)a.finish();}catch(x){}});
var out=[],W=innerWidth,H=innerHeight,big=W>=1200;
function add(k,d){if(out.filter(function(o){return o.k===k;}).length<3)out.push({k:k,d:d});}
function vis(e){var s=getComputedStyle(e);if(s.display==='none'||s.visibility!=='visible'||parseFloat(s.opacity)===0)return false;
 var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
function own(e){var t='';for(var c=e.firstChild;c;c=c.nextSibling)if(c.nodeType===3)t+=c.nodeValue;return t.replace(/\s+/g,' ').trim();}
function words(t){return (String(t||'').match(/[A-Za-z0-9À-ɏ']+/g)||[]).length;}
function txt(e){return (e.innerText||'').replace(/\s+/g,' ').trim();}
function mine(e){return !!e.closest('#paul-concept,#paul-credits,#paul-images,#paul-phone');}
// A wordmark is the one place tracked capitals belong (67's legal case).
function brand(e){for(var a=e;a&&a!==document.body;a=a.parentElement){if(/brand|logo|wordmark/i.test((a.getAttribute('class')||'')+' '+(a.id||'')))return true;
 var h=a.closest&&a.closest('header');if(a.tagName==='A'&&h&&a===h.querySelector('a'))return true;}return false;}
var all=[].slice.call(document.body.querySelectorAll('*')).filter(function(e){return !/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|PRE)$/.test(e.tagName)&&!mine(e);});
var heads=[].slice.call(document.querySelectorAll('h1,h2,h3')).filter(function(h){return vis(h)&&!mine(h);});
// 54 the eyebrow: short inert type just before a heading, announcing it.
heads.forEach(function(h){if(h.closest('nav'))return;var p=h.previousElementSibling;while(p&&!vis(p))p=p.previousElementSibling;
 if(!p||!/^(P|SPAN|DIV|SMALL|STRONG|B|EM|I)$/.test(p.tagName)||p.querySelector('a,time,img,svg,button,input,h1,h2,h3')||p.getAttribute('role')==='status')return;
 var t=txt(p);if(t&&t.length<=60&&words(t)<=7)add('eyebrow','"'+t.slice(0,40)+'" above "'+txt(h).slice(0,40)+'"');});
// 67 tracked capitals: small text set uppercase, in small caps or spaced out.
all.forEach(function(e){var t=own(e);if(!/[a-z]/i.test(t)||!vis(e)||brand(e))return;var s=getComputedStyle(e),fs=parseFloat(s.fontSize);if(fs>15)return;
 var ls=s.letterSpacing==='normal'?0:parseFloat(s.letterSpacing);
 if(s.textTransform==='uppercase'||/small-caps/.test(s.fontVariant+' '+(s.fontVariantCaps||''))||ls>0.03*fs+0.01)add('caps','"'+t.slice(0,40)+'"');});
// 63 template punctuation: "A · B · C", and an arrow stuck to a button.
all.forEach(function(e){var t=own(e);if(t&&vis(e)&&(t.match(/\s[·•]\s/g)||[]).length>=2)add('dots','"'+t.slice(0,50)+'"');});
[].slice.call(document.querySelectorAll('a,button')).forEach(function(e){var t=txt(e);
 if(vis(e)&&(/(→|›|»|->|⟶|➔)$/.test(t)||/^(←|‹|«|<-)/.test(t)))add('arrow','"'+t.slice(0,40)+'"');});
// 38 one word set apart in a heading: coloured, italic, bolder, marked.
heads.forEach(function(h){if(brand(h))return;var hs=getComputedStyle(h),ht=txt(h);
 [].slice.call(h.querySelectorAll('span,em,i,strong,b,mark,u')).forEach(function(x){var t=txt(x),xs=getComputedStyle(x);
  if(!t||t.length>=ht.length*0.8||xs.display==='block')return;
  if(xs.color!==hs.color||xs.fontStyle!==hs.fontStyle||xs.backgroundColor!=='rgba(0, 0, 0, 0)'||xs.textDecorationLine!==hs.textDecorationLine
   ||Math.abs(parseFloat(xs.fontWeight)-parseFloat(hs.fontWeight))>=200)add('accent','"'+t.slice(0,30)+'" in "'+ht.slice(0,50)+'"');});});
// 2a 2b 29: purple gradients, gradient text, blurred blobs.
function purple(c){var m=c.match(/oklch\(([\d.]+)%?\s+([\d.]+)\s+([\d.]+)/);if(m)return parseFloat(m[2])>0.06&&parseFloat(m[3])>=280&&parseFloat(m[3])<=335;
 var n=(c.match(/[\d.]+/g)||[]).map(Number);if(n.length<3)return false;var r=n[0]/255,g=n[1]/255,b=n[2]/255,mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn,l=(mx+mn)/2;
 if(!d)return false;var sat=d/(1-Math.abs(2*l-1)),h=mx===r?((g-b)/d)%6:mx===g?(b-r)/d+2:(r-g)/d+4;h*=60;if(h<0)h+=360;return sat>0.25&&l>0.12&&l<0.9&&h>=250&&h<=330;}
all.forEach(function(e){if(!vis(e))return;var s=getComputedStyle(e),bi=s.backgroundImage||'';
 if(/gradient/.test(bi)){if((bi.match(/(rgba?|oklch)\([^)]*\)/g)||[]).some(purple))add('purple',desc(e));
  if(/text/.test(s.backgroundClip+' '+(s.webkitBackgroundClip||'')))add('gradtext',desc(e));}
 var m=(s.filter||'').match(/blur\(([\d.]+)px\)/),r=e.getBoundingClientRect();if(m&&parseFloat(m[1])>=20&&r.width>=150&&r.height>=150)add('blob',desc(e));});
// 4 a card inside a card.
function card(e){if(/^(A|BUTTON|INPUT|SUMMARY|IMG|LABEL|SELECT|TEXTAREA|svg)$/i.test(e.tagName))return false;var s=getComputedStyle(e),r=e.getBoundingClientRect();
 if(r.width<120||r.height<60||!(parseFloat(s.borderTopLeftRadius)>0)||!(parseFloat(s.paddingTop)>0))return false;
 return (parseFloat(s.borderTopWidth)>0&&s.borderTopStyle!=='none')||(s.boxShadow&&s.boxShadow!=='none')||s.backgroundColor!=='rgba(0, 0, 0, 0)';}
var cards=all.filter(function(e){return vis(e)&&card(e);});
cards.forEach(function(c){for(var a=c.parentElement;a&&a!==document.body;a=a.parentElement){if(cards.indexOf(a)>=0){add('nest',desc(c)+' inside '+desc(a));break;}}});
// 37 more than three type families; 75 text too small to read.
var fam={};all.forEach(function(e){if(!own(e)||!vis(e))return;fam[getComputedStyle(e).fontFamily.split(',')[0].replace(/["']/g,'').trim().toLowerCase()]=1;});
var fl=Object.keys(fam);if(fl.length>3)add('fonts',fl.join(', '));
all.forEach(function(e){var t=own(e);if(!words(t)||!vis(e))return;var fs=parseFloat(getComputedStyle(e).fontSize);
 if((/^(P|LI|TD|DD|BLOCKQUOTE)$/.test(e.tagName)&&fs<12)||fs<11)add('tiny','"'+t.slice(0,30)+'" at '+Math.round(fs)+'px');});
// 80 81 a drawing where a photograph belongs; a coloured box with nothing in it.
[].slice.call(document.querySelectorAll('svg')).forEach(function(v){if(!vis(v)||brand(v)||mine(v))return;var r=v.getBoundingClientRect();
 if(r.width<120||r.height<120||/map|chart|diagram|logo/i.test((v.getAttribute('aria-label')||'')+' '+(v.getAttribute('class')||''))||v.querySelector('text'))return;
 if(v.querySelectorAll('path,circle,ellipse,polygon,polyline').length>=3)add('drawing',desc(v)+' '+Math.round(r.width)+'x'+Math.round(r.height));});
all.forEach(function(e){if(!vis(e))return;var r=e.getBoundingClientRect();if(r.width<300||r.height<200)return;var s=getComputedStyle(e);
 if(/url\(/.test(s.backgroundImage)||!(/gradient/.test(s.backgroundImage)||s.backgroundColor!=='rgba(0, 0, 0, 0)'))return;
 if(txt(e)||e.querySelector('img,svg,video,canvas,picture'))return;add('empty',desc(e)+' '+Math.round(r.width)+'x'+Math.round(r.height));});
if(big){window.scrollTo(0,0);
 // 44b the hero: headline, its first sentence and the main button on one laptop screen.
 var h1=document.querySelector('h1');
 if(h1&&vis(h1)){var after=[].slice.call(document.body.querySelectorAll('p,a,button')).filter(function(e){
   return vis(e)&&!e.closest('nav')&&(h1.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING);});
  function isCta(e){return e.tagName==='BUTTON'||/btn|button|cta/i.test(String(e.className||''))||/^tel:/.test(e.getAttribute('href')||'');}
  // The hero's own button; a hero with none leaves the call button in the
  // header to be the main one, and check already wants their call link.
  var cta=after.filter(function(e){return e.tagName!=='P'&&isCta(e);})[0];
  var lead=after.filter(function(e){return e.tagName==='P'&&words(txt(e))>=3&&(!cta||(e.compareDocumentPosition(cta)&Node.DOCUMENT_POSITION_FOLLOWING));});
  var miss=[];if(h1.getBoundingClientRect().bottom>H+1)miss.push('the headline');
  if(lead[0]&&lead[0].getBoundingClientRect().bottom>H+1)miss.push('its first sentence');
  if(cta&&cta.getBoundingClientRect().bottom>H+1)miss.push('the main button "'+txt(cta).slice(0,30)+'"');
  if(miss.length)add('fold',miss.join(', '));
  if(lead.length>2)add('herotext',lead.length+' paragraphs between the headline and the main button');}
 // 58 the screen budget, counting sentences only: lists, tables, hours and prices are free.
 var prose=[].slice.call(document.body.querySelectorAll('p,blockquote,figcaption')).filter(function(e){return vis(e)&&!mine(e)
  &&!e.closest('li,table,dl,address,footer,nav,[class*=hour],[class*=price],[class*=menu],[id*=hour],[id*=price],[id*=menu]');})
  .map(function(e){var r=e.getBoundingClientRect();return {y:(r.top+r.bottom)/2+scrollY,n:words(txt(e))};});
 var most=0,at=0;for(var y=0;y<document.documentElement.scrollHeight;y+=150){var n=0;prose.forEach(function(p){if(p.y>=y&&p.y<y+900)n+=p.n;});if(n>most){most=n;at=y;}}
 if(most>__WORDS__)add('words',most+' words of sentences in the 900px from '+Math.round(at)+'px down');
 // 83 the photo wall: stock photos (not the business's own) over a third of the page.
 var area=0;[].slice.call(document.images).forEach(function(i){if(!vis(i)||/\/yours-/.test(i.src))return;var r=i.getBoundingClientRect();
  if(r.top+scrollY>=H)area+=r.width*r.height;});
 var below=Math.max(1,(document.documentElement.scrollHeight-H)*W);if(area/below>__WALL__)add('wall',Math.round(area/below*100)+'%');
 // 69 the echo: a price or a percentage in four sections or more.
 var seen={};[].slice.call(document.querySelectorAll('body > *, main > *')).filter(function(e){return vis(e)&&e.tagName!=='MAIN'&&!mine(e);})
  .forEach(function(sec,i){(txt(sec).match(/\$\s?\d[\d,]*(\.\d\d)?|\b\d+(\.\d+)?%/g)||[]).forEach(function(m){m=m.replace(/\s/g,'');(seen[m]=seen[m]||{})[i]=1;});});
 for(var m in seen){var k=Object.keys(seen[m]).length;if(k>=4)add('echo',m+' in '+k+' sections');}
 // 26a a link or button that shows nothing when a keyboard reaches it.
 var P=['outlineStyle','outlineWidth','boxShadow','textDecorationLine','backgroundColor','borderTopColor','color'],nofocus=[];
 [].slice.call(document.querySelectorAll('a[href],button,summary')).filter(vis).slice(0,40).forEach(function(e){
  var a=P.map(function(k){return getComputedStyle(e)[k];}).join('|');e.focus({preventScroll:true});
  var s1=getComputedStyle(e),b=P.map(function(k){return s1[k];}).join('|');
  e.blur();if(a===b)nofocus.push(txt(e).slice(0,30)||e.tagName.toLowerCase());});
 if(nofocus.length)add('focus',nofocus.length+', e.g. "'+nofocus[0]+'"');
 window.scrollTo(0,0);}
// 34 49a at this width too: a button breaking onto two lines; sideways scroll.
paulWraps().forEach(function(w){add('wrap','"'+w.text+'" on '+w.lines+' lines');});
var fit=paulOver();if(fit.wide)add('wide',fit.over.map(function(o){return o.el+' reaches '+o.right+'px';}).join('; ')||'something is wider than the screen');
return out;}""".replace("__WORDS__", str(WORDS_PER_SCREEN)).replace("__WALL__", str(PHOTO_WALL))
# What Paul is told for each, after where and what: one place for the words.
LOOK_SAYS = {
    "eyebrow": "a label above a heading (an eyebrow) - open the section with the heading itself; a fact in "
               "the label moves into the sentence under it",
    "caps": "small text in capitals or spaced out - set labels, nav and tags in the body face a step "
            "smaller, sentence case, unspaced (the wordmark may stay)",
    "dots": "words joined with dots - use line breaks, columns or a short sentence",
    "arrow": "an arrow stuck to a link or button - the label says what it does; no arrow",
    "accent": "one word set apart in a heading (colour, italic, weight or a mark) - set the whole heading one way",
    "purple": "a purple or blue-violet gradient - one solid colour from the business",
    "gradtext": "gradient-filled text - solid ink",
    "blob": "a blurred glow or blob - a solid surface; depth from light and dark steps",
    "nest": "a card inside a card - one box, not two",
    "fonts": "more than three type families - a display face, a body face, at most one more",
    "tiny": "text too small to read - body text 16px, labels a step smaller, nothing under 12px",
    "drawing": "a drawn illustration - their photo, a Commons photo (stock find), or type at size",
    "empty": "a coloured box with nothing in it - a photo, or remove it",
    "fold": "not on a laptop screen (1280x800) without scrolling - the headline, one sentence and the main "
            "button must be; tighten the hero",
    "herotext": "too much before the main button - the hero is a headline, one sentence and its buttons",
    "words": "too many words on one screen - about 80 words of sentences per screen; cut, or move detail "
             "into a list or table",
    "wall": "of the page below the first screen is stock photos - keep the strongest, show them bigger, "
            "and carry the rest in type",
    "echo": "the same figure in many sections - say it once, where it does work",
    "focus": "link(s) or button(s) show nothing when reached by keyboard - give :focus-visible an outline",
    "wrap": "a button or link breaks onto two lines - shorten it or let it fit",
    "wide": "the page scrolls sideways - make it fit (max-width:100%, wrap, or hide it until opened)",
}


# THE PHONE PROBE. College Hill Barbers (2026-10-06): on a phone the header's
# "CALL (319) 277-9972" button broke onto two lines and squeezed the name
# beside it - in the phone screenshot Paul read, and passed. A button is a
# short label in a box; when its text needs two lines at 390px it is too
# long or too big for the header. Measured in a real browser like contrast,
# on the first page only (the header is the same on every page) to keep
# `check` quick. Also: a page that scrolls sideways on a phone.
PHONE_W, PHONE_H = 390, 844
# Lines are counted per run of text: the text nodes that flow together
# inside one link or button, up to the first box of their own (a block, a
# flex item, an inline-block). Two rects are one line when their heights
# mostly overlap. Counting every rect's top as a line (until 2026-10-21) read
# a one-line "Royal Edge" logo as two or three lines - a crown icon beside it,
# or the two words in different sizes, each sit a few pixels higher - and
# failed Royal Edge's build twice, even after white-space:nowrap.
PHONE_JS = r"""(function(){
var out=paulWraps(),fit=paulOver(),wide=fit.wide,over=fit.over;
// Every finite animation and transition jumped to its end - an intro over
// the page, a menu sliding open - as a visitor sees it a moment later.
// Waiting for them does not work: headless Chrome may never play them.
function settled(then){var n=0;(function end(){(document.getAnimations?document.getAnimations():[]).forEach(function(a){
 var e=a.effect&&a.effect.getTiming?a.effect.getTiming():{};if(e.iterations!==Infinity){try{a.finish();}catch(x){}}});
 if(n++<2)return setTimeout(end,50);then();})();}
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(a)e.preventDefault();},true);
function reach(e){e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});var r=e.getBoundingClientRect();if(r.width<2||r.height<2)return false;
 var x=r.left+r.width/2,y=r.top+r.height/2;if(x<0||y<0||x>innerWidth||y>innerHeight)return false;
 var h=document.elementFromPoint(x,y);return !!h&&(h===e||e.contains(h));}
// textContent, not innerText: a link inside a closed drop-down has no
// innerText at all, and was dropped from the list it is checked against.
function label(l){return (l.textContent||l.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim();}
var links=[].slice.call(document.querySelectorAll('header a[href],nav a[href]')).filter(function(l){
 var t=label(l);return t&&!/^skip/i.test(t);});
var names={},got={};links.forEach(function(l){names[l.getAttribute('href')]=label(l);});
// Reached when any link to the same place can be tapped: a header "Book"
// hidden on a phone because a bar at the bottom has the same link is fine.
var every=[].slice.call(document.querySelectorAll('a[href]'));
function sweep(){for(var k in names){if(!got[k]&&every.some(function(l){return l.getAttribute('href')===k&&reach(l);}))got[k]=1;}}
function toggles(){return [].slice.call(document.querySelectorAll('header summary,nav summary,header button,nav button,header [aria-expanded],nav [aria-expanded],header [aria-controls],nav [aria-controls]'));}
var tapped=[];
function finish(){var hidden=[];for(var k in names)if(!got[k])hidden.push(names[k]);window.scrollTo(0,0);
 var pre=document.createElement('pre');pre.id='paul-phone';pre.textContent=JSON.stringify({wraps:out,wide:wide,over:over,width:window.innerWidth,hidden:hidden,styled:paulStyled()});document.body.appendChild(pre);}
// What a visitor can tap, once the page's intro has played: every menu
// button tapped in turn, each given time to open.
function step(){sweep();var ts=toggles().filter(function(t){return tapped.indexOf(t)<0&&reach(t);});
 if(!ts.length||tapped.length>=12)return finish();tapped.push(ts[0]);ts[0].click();setTimeout(function(){settled(step);},50);}
settled(step);
})();""".replace("(function(){", "(function(){" + STYLED_JS + DESC_JS + FIT_JS, 1)


def phone_problems(site, browser, run=subprocess.run):
    """{'wraps': [(text, lines)], 'wide': bool} for the first page at phone
    width, or None when the browser gave no answer."""
    page = Path(site) / "index.html"
    if not page.is_file():
        return None
    probe = page.with_name(f".{CONTRAST_ID}-phone-{page.name}")
    frame = page.with_name(f".{CONTRAST_ID}-phoneframe-{page.name}")
    markup = page.read_text(errors="replace")
    tag = f"<script>{PHONE_JS}</script>"
    probe.write_text(re.sub(r"(?i)</body>", lambda _m: tag + "</body>", markup, count=1)
                     if re.search(r"(?i)</body>", markup) else markup + tag)
    # The probe runs in a 390px iframe (CHROME_MIN_W); the frame copies its
    # answer out, because --dump-dom prints only the outer page.
    # The probe answers after tapping through the menu, so the frame waits
    # for the answer rather than a fixed moment after load.
    lift = ("<script>var f=document.querySelector('iframe');f.addEventListener('load',function(){"
            "var n=0;(function look(){var d=null;try{d=f.contentDocument}catch(e){}"
            "var p=d&&d.getElementById('paul-phone');if(!p&&n++<80)return setTimeout(look,100);"
            "var o=document.createElement('pre');"
            "o.id='paul-phone';o.textContent=p?p.textContent:'';document.body.appendChild(o);})();"
            "});</script>")
    frame.write_text(phone_frame(probe.name, PHONE_W, PHONE_H, lift))
    try:
        r = run([browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 "--no-first-run", "--disable-dev-shm-usage", "--allow-file-access-from-files",
                 f"--window-size={CHROME_MIN_W},{PHONE_H}",
                 "--virtual-time-budget=10000", "--dump-dom", frame.as_uri()],
                capture_output=True, text=True, timeout=90)
        out = r.stdout or ""
    except Exception:
        out = ""
    finally:
        probe.unlink(missing_ok=True)
        frame.unlink(missing_ok=True)
    m = re.search(r'<pre id="paul-phone">(.+?)</pre>', out, re.S)
    if not m:
        return None
    try:
        d = json.loads(html.unescape(m.group(1)))
    except ValueError:
        return None
    return {"wraps": [(w.get("text", ""), w.get("lines")) for w in d.get("wraps") or []],
            "wide": bool(d.get("wide")), "hidden": [str(x) for x in d.get("hidden") or []],
            "styled": d.get("styled"),
            "over": [(str(o.get("el")), o.get("right")) for o in d.get("over") or [] if isinstance(o, dict)]}


def measured_phone(site, browser, memo, run=subprocess.run):
    """phone_problems, remembered against the site's fingerprint, as contrast is."""
    fp = f"{site_fingerprint(site)}|{PROBE_V}"
    memo = Path(memo)
    seen = read_json(memo, {}) or {}
    if seen.get("phone_site") == fp and isinstance(seen.get("phone"), dict):
        got = seen["phone"]
        return {"wraps": [tuple(w) for w in got.get("wraps") or []], "wide": bool(got.get("wide")),
                "hidden": list(got.get("hidden") or []), "styled": got.get("styled"),
                "over": [tuple(o) for o in got.get("over") or []]}
    got = phone_problems(site, browser, run=run)
    if got is not None:
        memo.parent.mkdir(parents=True, exist_ok=True)
        write_json(memo, {**(read_json(memo, {}) or {}), "phone_site": fp, "phone": got})
    return got


def site_fingerprint(site):
    """What the site is, for "has it changed since it was last measured":
    every file's path, size and modified time. The contrast probe's hidden
    copies are left out - they come and go during a measurement."""
    site = Path(site)
    h = hashlib.sha1()
    for f in sorted(site.rglob("*")):
        if f.is_file() and not f.name.startswith(f".{CONTRAST_ID}"):
            st = f.stat()
            h.update(f"{f.relative_to(site).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def measured_contrast(site, browser, memo, run=subprocess.run):
    """contrast_problems, remembered against the site's fingerprint. Paul
    runs check again after every fix, and on the droplet's one CPU each
    Chrome launch is seconds: check went from under 10 s to 12.3 s when the
    probe arrived (2026-10-06), past the point where openclaw moves a
    command to the background - and the next Gemini cycle spent 70 tool
    calls running and polling commands. An unchanged site is not measured
    twice."""
    fp = f"{site_fingerprint(site)}|{PROBE_V}"
    memo = Path(memo)
    seen = read_json(memo, {}) or {}
    # A list is an answer; None (the browser said nothing) is asked again.
    if seen.get("site") == fp and isinstance(seen.get("contrast"), list):
        return [tuple(x) for x in seen["contrast"]]
    bad = contrast_problems(site, browser, run=run)
    memo.parent.mkdir(parents=True, exist_ok=True)
    write_json(memo, {**seen, "site": fp, "contrast": bad})
    return bad


def contrast_problems(site, browser, run=subprocess.run, width=1280, height=900):
    """[(page, text, ratio, need)] for text a visitor cannot read, or None
    when the browser gave no answer - unmeasured is not "fine", and it is
    not "broken" either. The probe runs on a hidden copy of each page beside
    it, so relative links resolve; the copy is always removed."""
    site = Path(site)
    bad, answered = [], False
    for page in sorted(site.rglob("*.html"))[:6]:
        probe = page.with_name(f".{CONTRAST_ID}-{page.name}")
        markup = page.read_text(errors="replace")
        tag = f"<script>{CONTRAST_JS}</script>"
        probe.write_text(re.sub(r"(?i)</body>", lambda _m: tag + "</body>", markup, count=1)
                         if re.search(r"(?i)</body>", markup) else markup + tag)
        try:
            r = run([browser, "--headless=new", "--no-sandbox", "--disable-gpu",
                     "--no-first-run", "--disable-dev-shm-usage",
                     f"--window-size={width},{height}", "--virtual-time-budget=4000",
                     "--dump-dom", probe.as_uri()], capture_output=True, text=True, timeout=90)
            out = r.stdout or ""
        except Exception:
            out = ""
        finally:
            probe.unlink(missing_ok=True)
        m = re.search(rf'<pre id="{CONTRAST_ID}">(.*?)</pre>', out, re.S)
        if not m:
            continue
        try:
            d = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        answered = True
        rel = page.relative_to(site).as_posix()
        for f in d.get("fails") or []:
            bad.append((rel, f.get("text", ""), f.get("ratio"), f.get("need")))
    return bad if answered else None


# Photos as a visitor sees them (owner, 2026-10-21: Royal Edge's shop photo
# was "overstretched"). Measured in the page at desktop width, where photos
# are largest: a photo pulled out of its own shape, or shown much bigger
# than the file is - which is what looks soft and stretched. Headless Chrome
# loads lazy images below the fold anyway (measured 2026-10-21, 9000px down);
# they are made eager too, in case a later Chrome stops doing that.
# Sharp screens (owner, 2026-10-21, Royal Edge's About photos on the iPad:
# "all over stretched and look bad"). Phones and iPads draw two or three
# dots per CSS pixel, so a photo that only matched its file at 1x - which
# passed - is soft on every screen the owner and the businesses use. A file
# needs IMAGE_SHARP pixels for each pixel it is shown at. Measured at a
# computer's width and an upright iPad's, where single-column layouts show
# photos widest.
IMAGE_SHARP = 1.5
IMAGE_SKEW = 0.05          # proportions changed by more than this: stretched
IMAGE_SCREENS = ((1280, 800, "a computer"), (820, 1180, "an iPad"))
# A photo column running on past the text beside it (the same About: two
# tall photos beside two paragraphs - "a bunch of blank space"), in
# screen heights.
GAP_SCREENS = 0.75
IMAGE_JS = r"""(function(){
var imgs=[].slice.call(document.images);imgs.forEach(function(i){i.loading='eager';});
var t0=Date.now();
function done(){var out=[];imgs.forEach(function(i){
 var r=i.getBoundingClientRect();if(r.width<40||r.height<40)return;
 var s=getComputedStyle(i);if(s.display==='none'||s.visibility!=='visible')return;
 var nw=i.naturalWidth,nh=i.naturalHeight,src=i.getAttribute('src')||'';
 if(!nw||!nh||/\.svg(\?|#|$)/i.test(src))return;
 var fit=s.objectFit,sx=r.width/nw,sy=r.height/nh,scale;
 if(fit==='fill'){var d=Math.abs(sx/sy-1);if(d>SKEW)out.push({src:src,kind:'stretched',by:Math.round(d*100)});scale=Math.max(sx,sy);}
 else if(fit==='contain'||fit==='scale-down'){scale=Math.min(sx,sy);}else if(fit==='none'){scale=1;}else{scale=Math.max(sx,sy);}
 if(scale*SHARP>1)out.push({src:src,kind:'enlarged',scale:Math.round(scale*100)/100,
   natural:nw+'x'+nh,shown:Math.round(r.width)+'x'+Math.round(r.height),most:Math.floor(r.width/(scale*SHARP))});});
 // Side by side, one ending far above the other: what each holds, not its
 // box - a grid stretches the short column's box to the tall one's height.
 // An empty element (a dotted price leader) holds nothing: its own box.
 function ends(e){var g=document.createRange();g.selectNodeContents(e);var r=g.getBoundingClientRect();
  return (r.width||r.height)?r.bottom:e.getBoundingClientRect().bottom;}
 function sticky(e){if(getComputedStyle(e).position==='sticky')return true;
  for(var c=e.firstElementChild;c;c=c.nextElementSibling)if(getComputedStyle(c).position==='sticky')return true;return false;}
 var gaps=[];[].slice.call(document.body.querySelectorAll('*')).forEach(function(p){
  var kids=[].slice.call(p.children).filter(function(c){var s=getComputedStyle(c);
   return s.display!=='none'&&s.position!=='absolute'&&s.position!=='fixed'&&c.getBoundingClientRect().height>0;});
  for(var k=0;k+1<kids.length;k++){var a=kids[k].getBoundingClientRect(),b=kids[k+1].getBoundingClientRect();
   if(a.right>b.left+1&&b.right>a.left+1)continue;if(Math.min(a.bottom,b.bottom)<=Math.max(a.top,b.top))continue;
   var ea=ends(kids[k]),eb=ends(kids[k+1]),lo=ea<eb?kids[k]:kids[k+1],hi=lo===kids[k]?kids[k+1]:kids[k];
   var gap=Math.abs(ea-eb);if(gap>GAP*innerHeight&&!sticky(lo)&&gaps.length<3)gaps.push({short:desc(lo),tall:desc(hi),gap:Math.round(gap)});}});
 var SAMPLES=__SAMPLES__;
 function own(e){var t='';for(var c=e.firstChild;c;c=c.nextSibling)if(c.nodeType===3)t+=c.nodeValue;return t;}
 var tags=[].slice.call(document.body.querySelectorAll('*')).filter(function(e){return /\bsample\b/i.test(own(e));});
 // A label that lets taps through (pointer-events:none, the usual way to
 // lay a badge over a photo) is invisible to elementsFromPoint, and read as
 // missing (Royal Edge, 2026-10-21) - so every element is hit-testable here.
 var pe=document.createElement('style');pe.textContent='*{pointer-events:auto!important}';document.head.appendChild(pe);
 var bare=[];imgs.forEach(function(i){var u=decodeURI(i.src||'');
  if(!SAMPLES.some(function(r){return u.slice(-(r.length+1))==='/'+r;}))return;
  var s=getComputedStyle(i);if(s.display==='none'||s.visibility!=='visible')return;
  i.scrollIntoView({block:'center',behavior:'instant'});var r=i.getBoundingClientRect();if(r.width<40||r.height<40)return;
  var why='none';
  var on=tags.some(function(t){var q=t.getBoundingClientRect(),ts=getComputedStyle(t);
   var x=q.left+q.width/2,y=q.top+q.height/2;if(x<r.left||x>r.right||y<r.top||y>r.bottom)return false;
   if(q.width<2||q.height<2||ts.visibility!=='visible'||parseFloat(ts.fontSize)<10){why='small';return false;}
   var st=document.elementsFromPoint(x,y),a=st.indexOf(i),b=-1;
   for(var k=0;k<st.length;k++){if(st[k]===t||t.contains(st[k])){b=k;break;}}
   if(b>=0&&(a<0||b<a))return true;why='behind';return false;});
  if(!on)bare.push({src:i.getAttribute('src')||'',why:why});});
 pe.remove();
 var pre=document.createElement('pre');pre.id='paul-images';pre.textContent=JSON.stringify({images:out,bare:bare,gaps:gaps,look:paulLook(),styled:paulStyled()});document.body.appendChild(pre);}
(function wait(){if(imgs.every(function(i){return i.complete;})||Date.now()-t0>3000)done();else setTimeout(wait,50);})();
})();""".replace("SKEW", str(IMAGE_SKEW)).replace("SHARP", str(IMAGE_SHARP)).replace(
    "GAP*", f"{GAP_SCREENS}*").replace("(function(){", "(function(){" + STYLED_JS + DESC_JS + FIT_JS + LOOK_JS, 1)


def image_problems(site, browser, run=subprocess.run, width=1280, height=900, samples=(),
                   screen="a computer"):
    """[(page, src, kind, detail)] for photos stretched or shown bigger than
    a sharp screen can show them (kind "enlarged"), sample photos with no
    "Sample" on them ("unlabelled"), a column ending far above the one
    beside it ("gap", src the taller one), and a page whose CSS does not
    apply at this width ("unstyled", src ""), or None when the browser gave
    no answer. Run on a hidden copy of each page, as contrast is; the copy
    is always removed."""
    site = Path(site)
    bad, answered = [], False
    for page in sorted(site.rglob("*.html"))[:6]:
        if page.name.startswith("."):
            continue
        probe = page.with_name(f".{CONTRAST_ID}-img-{page.name}")
        markup = page.read_text(errors="replace")
        tag = f"<script>{IMAGE_JS.replace('__SAMPLES__', json.dumps(sorted(samples)))}</script>"
        probe.write_text(re.sub(r"(?i)</body>", lambda _m: tag + "</body>", markup, count=1)
                         if re.search(r"(?i)</body>", markup) else markup + tag)
        try:
            r = run([browser, "--headless=new", "--no-sandbox", "--disable-gpu",
                     "--no-first-run", "--disable-dev-shm-usage", "--allow-file-access-from-files",
                     f"--window-size={width},{height}", "--virtual-time-budget=5000",
                     "--dump-dom", probe.as_uri()], capture_output=True, text=True, timeout=90)
            out = r.stdout or ""
        except Exception:
            out = ""
        finally:
            probe.unlink(missing_ok=True)
        m = re.search(r'<pre id="paul-images">(.*?)</pre>', out, re.S)
        if not m:
            continue
        try:
            d = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        answered = True
        rel = page.relative_to(site).as_posix()
        at = f"on {screen} ({width}px)"
        for f in d.get("images") or []:
            detail = (f"{f.get('by')}%" if f.get("kind") == "stretched"
                      else f"{at} it is shown at {f.get('shown')} from a {f.get('natural')} photo; a "
                           f"phone or iPad screen needs {IMAGE_SHARP}x that, so at most {f.get('most')}px wide")
            bad.append((rel, f.get("src", ""), f.get("kind"), detail))
        for g in d.get("gaps") or []:
            bad.append((rel, g.get("tall", ""), "gap", f"{at} {g.get('short')} ends {g.get('gap')}px "
                                                       f"above {g.get('tall')} beside it"))
        for b in d.get("bare") or []:
            b = b if isinstance(b, dict) else {"src": b}
            bad.append((rel, b.get("src", ""), "unlabelled", SAMPLE_WHY.get(b.get("why"), SAMPLE_WHY["none"])))
        for f in d.get("look") or []:
            if f.get("k") in LOOK_SAYS:
                bad.append((rel, "", "look:" + f["k"], f"{at} {f.get('d', '')}"))
        if isinstance(d.get("styled"), (int, float)) and d["styled"] < STYLED_MIN:
            bad.append((rel, "", "unstyled", f"{at}: {round(d['styled'] * 100)}% of the page changes "
                                             f"with its stylesheets switched off"))
    return bad if answered else None


def measured_images(site, browser, memo, run=subprocess.run, samples=()):
    """image_problems, remembered against the site's fingerprint, as contrast is."""
    fp = f"{site_fingerprint(site)}|{PROBE_V}|{','.join(sorted(samples))}"
    memo = Path(memo)
    seen = read_json(memo, {}) or {}
    if seen.get("images_site") == fp and isinstance(seen.get("images"), list):
        return [tuple(x) for x in seen["images"]]
    bad, seen_once = None, set()
    for w, h, screen in IMAGE_SCREENS:
        got = image_problems(site, browser, run=run, width=w, height=h, samples=samples, screen=screen)
        if got is None:
            continue
        bad = bad or []
        for x in got:                    # once per photo and kind, at the first screen
            if (x[0], x[1], x[2]) not in seen_once:
                seen_once.add((x[0], x[1], x[2]))
                bad.append(x)
    if bad is not None:
        memo.parent.mkdir(parents=True, exist_ok=True)
        write_json(memo, {**(read_json(memo, {}) or {}), "images_site": fp, "images": bad})
    return bad


def mail_verdict(address, get=http_get):
    """("none" | "yes" | "unknown", detail) for an email address's domain,
    from Google's DNS-over-HTTPS. Milkbox (2026-10-05): the pitch went to an
    address at milkboxbakery.com, a domain that no longer exists - the
    email would have bounced. "none" only on proof: no such domain (Status
    3), or a null MX ("0 .", RFC 7505: accepts no mail). No MX at all is not
    proof - mail then goes to the domain's own address - so it is "unknown"."""
    domain = str(address or "").rsplit("@", 1)[-1].strip().strip("<>.").lower()
    if not domain or "." not in domain:
        return "unknown", f"{address!r} has no domain to look up"
    r = get(DOH + urllib.parse.quote(domain), timeout=15)
    if not r.get("ok") or r.get("status") != 200:
        return "unknown", f"could not look up {domain} ({r.get('error') or r.get('status')})"
    try:
        d = json.loads(r.get("body") or "")
    except ValueError:
        return "unknown", f"the lookup for {domain} did not answer in JSON"
    if d.get("Status") == 3:
        return "none", f"{domain} does not exist (no such domain in DNS)"
    if d.get("Status") != 0:
        return "unknown", f"DNS lookup for {domain} failed (status {d.get('Status')})"
    mx = [str(a.get("data") or "") for a in d.get("Answer") or [] if a.get("type") == 15]
    if mx and all(m.split()[-1:] == ["."] for m in mx):
        return "none", f"{domain} says it accepts no email (null MX)"
    if mx:
        return "yes", f"{domain} receives email"
    return "unknown", f"{domain} lists no mail server"


def browser_problems(t, work, browser):
    """(errors, notes) from the page as a browser draws it: contrast, photos,
    the phone layout and menu, styling at both widths. One set of words for
    check, finish and `paul.py measure`."""
    errs, notes = [], []
    bad = measured_contrast(work / "site", browser, work / "shots" / ".measured.json")
    if bad is None:
        notes.append("text contrast not measured - the browser gave no answer")
    for rel, text, ratio, need in (bad or [])[:6]:
        errs.append(f'"{text}" on {rel} is hard to read: contrast {ratio}:1, needs '
                    f"{need}:1 - change the text colour or what is behind it")
    samples = sorted(str(p.get("file") or "").lstrip("./").removeprefix("site/")
                     for p in t.get("photos") or [] if isinstance(p, dict) and p.get("kind") == "sample")
    pics = measured_images(work / "site", browser, work / "shots" / ".measured.json", samples=samples)
    if pics is None:
        notes.append("photo sizes not measured - the browser gave no answer")
    for rel, src, kind, detail in (pics or [])[:14]:
        if kind.startswith("look:"):
            errs.append(f"{rel}: {detail} - {LOOK_SAYS[kind[5:]]}")
            continue
        errs.append(f"{src} on {rel} is stretched out of its own shape ({detail}) - give it "
                    f"object-fit:cover, or a box with the photo's proportions" if kind == "stretched" else
                    f'{src} on {rel} is a sample photo with no "Sample" on it ({detail}) - put the '
                    f'word over the picture in an element of its own, e.g. <span class="sample-tag">'
                    f"Sample</span> inside the photo's box" if kind == "unlabelled" else
                    f"{rel} shows unstyled ({detail}) - its CSS does not "
                    f"apply. Link the stylesheet by a relative path (style.css, not /style.css) "
                    f"and close every @media block" if kind == "unstyled" else
                    f"{rel}: {detail} - a big empty space. Put the photos in a row above or below "
                    f"the text, or make them smaller, so the two sides end together" if kind == "gap" else
                    f"{src} on {rel} is blown up - {detail}, so it looks soft and stretched. Show it "
                    f"smaller (a max-width, or a smaller box in a row), or use a larger photo")
    phone = measured_phone(work / "site", browser, work / "shots" / ".measured.json")
    if phone is None:
        notes.append("the phone layout was not measured - the browser gave no answer")
    else:
        for text, lines in phone["wraps"][:4]:
            errs.append(f'on a phone ({PHONE_W}px) "{text}" breaks onto {lines} lines - '
                        f"shorten the label (\"Call\" with the number on its own line, "
                        f"or the number alone), shrink it, or move it out of the header")
        if phone["wide"]:
            over = phone.get("over") or []
            errs.append(f"on a phone ({PHONE_W}px) the page scrolls sideways - "
                        + ("; ".join(f"{el} reaches {right}px" for el, right in over[:3])
                           + f" on a {PHONE_W}px screen. Make it fit (max-width:100%, wrap, or "
                             f"hide it until opened with display:none)" if over else
                           "something is wider than the screen"))
        # A drop-down that opens on hover only cannot be opened on a phone
        # or the owner's iPad (owner, 2026-10-21: a Services drop-down).
        for text in (phone.get("hidden") or [])[:4]:
            errs.append(f'on a phone ({PHONE_W}px) the menu item "{text}" never shows, even after '
                        f"tapping every menu button - a menu must open on a tap (<details><summary>, "
                        f"or a <button> that toggles it), not on hover alone")
        st = phone.get("styled")
        if isinstance(st, (int, float)) and st < STYLED_MIN:
            errs.append(f"on a phone ({PHONE_W}px) index.html shows unstyled ({round(st * 100)}% of "
                        f"the page changes with its stylesheets switched off) - its CSS does not "
                        f"apply. Link the stylesheet by a relative path and close every @media block")
    return errs, notes


def run_checks(t, get=http_get, work=None):
    """Every rule, for check (Paul, mid-cycle) and finish (code, after) alike.
    One function, two readers: when they were two copies elsewhere in this
    repo, an agent was told "present" and failed for "missing" by the other.
    Returns (errors, notes, info)."""
    work = Path(work or A("work"))
    errs, notes, info = list(validate_target(t)), [], {}
    if errs or (t or {}).get("status") != "built":
        return errs, notes, info

    rows, page = verify_problems(t, get)
    for code, verdict, detail in rows:
        if verdict == "contradicted":
            errs.append(f"problem {code}: {detail}")
        else:
            notes.append(f"problem {code}: {verdict} - {detail}")
    info["problems"] = rows

    serrs, sinfo = check_site(work / "site", t, page)
    errs += serrs
    dealt = (read_json(work / "themes.json", {}) or {}).get("themes")
    if dealt and t.get("theme") not in dealt:
        errs.append(f'target.json "theme" must be the one you built in, of those dealt: {", ".join(dealt)}')
    info.update(sinfo)
    ref = own_reference(work)
    if ref and (work / "site" / "index.html").is_file():
        rerrs, rnotes = reference_problems(scan_site(work / "site")["text"], t, ref, get)
        errs += rerrs
        notes += rnotes
    browser = find_browser()
    if browser and (work / "site" / "index.html").is_file():
        berrs, bnotes = browser_problems(t, work, browser)
        errs += berrs
        notes += bnotes

    dp = work / "draft.md"
    if not dp.is_file():
        errs.append("work/draft.md does not exist")
    else:
        derrs, head, _body, words = check_draft(dp.read_text(errors="replace"), t)
        errs += derrs
        info.update(channel=head.get("channel"), to=head.get("to"), words=words)
        if (head.get("channel") or "").lower() == "email" and "@" in (head.get("to") or ""):
            verdict, detail = mail_verdict(head["to"], get)
            if verdict == "none":
                errs.append(f"an email to {head['to']} would bounce: {detail}. Reach them "
                            f"another way - their Facebook or Instagram (a DM), or a phone script")
            elif verdict == "unknown":
                notes.append(f"email address not confirmed: {detail}")
    return errs, notes, info


# --------------------------------------------------------------------------
# what code adds to every preview
# --------------------------------------------------------------------------

def concept_note(business, studio):
    return (f'<p id="{CONCEPT_ID}" style="margin:0;padding:14px 16px;'
            f'font:12px/1.5 system-ui,sans-serif;text-align:center;opacity:.75">'
            f"Design concept by {html.escape(studio)}. "
            f"Not the official site of {html.escape(business)}.</p>")


def apply_safeguards(site, business, studio):
    """noindex on every page, the footer on every page, robots.txt,
    vercel.json with the header, and no sitemap. Idempotent. HTML comments
    go too: the template's say "target.json" and "the owner", and anyone can
    read a page's source."""
    site = Path(site)
    note = concept_note(business, studio)
    credit = credits_line(site) or ""
    meta = f'<meta name="robots" content="{NOINDEX}">'
    for page in sorted(site.rglob("*.html")):
        markup = re.sub(r"(?s)<!--.*?-->\n?", "", page.read_text(errors="replace"))
        markup = re.sub(rf'(?is)<p[^>]*id="({CONCEPT_ID}|{CREDITS_ID})"[^>]*>.*?</p>', "", markup)
        markup = re.sub(r'(?i)<meta[^>]+name=["\']robots["\'][^>]*>', "", markup)
        if re.search(r"(?i)<head[^>]*>", markup):
            markup = re.sub(r"(?i)(<head[^>]*>)", lambda m: m.group(1) + meta, markup, count=1)
        else:
            markup = meta + markup
        if re.search(r"(?i)</body>", markup):
            idx = [m.start() for m in re.finditer(r"(?i)</body>", markup)][-1]
            markup = markup[:idx] + credit + note + markup[idx:]
        else:
            markup += credit + note
        page.write_text(markup)
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (site / "vercel.json").write_text(json.dumps({"headers": [{
        "source": "/(.*)", "headers": [{"key": "X-Robots-Tag", "value": NOINDEX}]}]},
        indent=1) + "\n")
    for p in site.glob("sitemap*.xml"):
        p.unlink()


def make_slug(name, suffix=None):
    base = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"['`’]", "", base)
    words = [w for w in re.findall(r"[a-z0-9]+", base)
             if w not in ("the", "llc", "inc", "co", "ltd", "corp")]
    short = "-".join(words)[:24].strip("-") or "site"
    suffix = suffix or "".join(secrets.choice(string.ascii_lowercase + string.digits)
                               for _ in range(6))
    return f"{short}-concept-{suffix}"


def compose_draft(head, body, sender, dry):
    lines = []
    if dry:
        lines += ["DRY RUN - this preview was built locally and never deployed. Not for sending.", ""]
    lines += [f"Channel: {head.get('channel', '')}", f"To: {head.get('to', '')}"]
    if head.get("subject"):
        lines.append(f"Subject: {head['subject']}")
    lines += ["", body.strip()]
    ch = head.get("channel")
    if ch != "phone":
        if OPT_OUT.lower() not in body.lower():
            lines += ["", OPT_OUT]
        lines += ["", sender.get("name", ""), sender.get("studio", "")]
        if ch == "email":
            lines += [f"{sender.get('email', '')} · {sender.get('phone', '')}",
                      sender.get("address", "")]
    return "\n".join(lines) + "\n"


def reusable_draft(text):
    """A pitch code composed (outbox/<slug>/draft.md), back as Paul wrote it:
    the link a placeholder again, without the dry-run line, the opt-out line
    and the signature code adds. Owner, 2026-10-21: "Paul doesn't need to
    draft a pitch every time we send something back if the pitch isn't
    changing" - Royal Edge had four, one per revision."""
    lines = str(text or "").splitlines()
    if lines and lines[0].startswith("DRY RUN"):
        lines = lines[1:]
    head, body = parse_draft("\n".join(lines).strip())
    body = re.sub(r"https?://[a-z0-9-]+-concept-[a-z0-9]{6}\.vercel\.app/?", PREVIEW_URL, body)
    if (head.get("channel") or "") != "phone":
        rows = body.splitlines()
        cut = max((i for i, ln in enumerate(rows) if ln.strip() == OPT_OUT), default=None)
        if cut is not None:
            body = "\n".join(rows[:cut]).rstrip()
    top = [f"Channel: {head.get('channel', '')}", f"To: {head.get('to', '')}"]
    if head.get("subject"):
        top.append(f"Subject: {head['subject']}")
    return "\n".join(top) + "\n\n" + body.strip() + "\n"


def live_checks(url, site, business, get=http_get):
    """[(ok, line)] for a deployed preview. Every local file the pages use is
    fetched, because "the page loads" says nothing about its images."""
    out = []
    r = get(url)
    good = r["ok"] and r["status"] == 200
    out.append((good, f"{url} answers {r['status'] or r['error']}"))
    if not good:
        if r["status"] in (401, 403):
            out.append((False, "the preview asks for a login - Vercel deployment protection is "
                               "on for this project"))
        return out
    body = r["body"]
    out.append((norm_name(business) in norm_name(html.unescape(re.sub(r"<[^>]+>", " ", body))),
                "the page shows the business name"))
    out.append(("noindex" in r["headers"].get("x-robots-tag", "").lower(),
                "X-Robots-Tag: noindex header"))
    out.append((bool(re.search(r'(?i)<meta[^>]+name=["\']robots["\'][^>]+noindex', body)),
                "noindex meta tag"))
    rb = get(url.rstrip("/") + "/robots.txt")
    out.append((rb["status"] == 200 and "disallow: /" in rb["body"].lower(),
                "robots.txt disallows everything"))
    sc = scan_site(site)
    for rel in sorted(set(sc["local_refs"])):
        g = get(urllib.parse.urljoin(url.rstrip("/") + "/", urllib.parse.quote(rel)))
        out.append((g["status"] == 200, f"{rel} -> {g['status'] or g['error']}"))
    return out


# --------------------------------------------------------------------------
# commands: the owner
# --------------------------------------------------------------------------

def cmd_init(a):
    for d in ("state", "sites", "outbox", "reports"):
        A(d).mkdir(parents=True, exist_ok=True)
    made = []
    for name, fields in (("contacted.csv", CONTACTED_FIELDS), ("deployments.csv", DEPLOY_FIELDS)):
        p = A("state", name)
        if not p.is_file():
            write_rows(p, fields, [])
            made.append(f"state/{name}")
    if not A("MEMORY.md").is_file() and A("MEMORY.seed.md").is_file():
        shutil.copyfile(A("MEMORY.seed.md"), A("MEMORY.md"))
        made.append("MEMORY.md")
    print("paul: " + (f"created {', '.join(made)}" if made else "state files present"))
    return 0


def cmd_sender(a):
    cur, _ = load_sender()
    given = {k: getattr(a, k) for k in SENDER_FIELDS if getattr(a, k) is not None}
    if given:
        if "email" in given and not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", given["email"], re.I):
            print(f"refused: {given['email']!r} is not an email address", file=sys.stderr)
            return 2
        if "price" in given:
            if not re.fullmatch(r"\$?\s*\d{2,5}", given["price"].strip()):
                print(f"refused: {given['price']!r} is not a price in whole dollars, e.g. 350",
                      file=sys.stderr)
                return 2
            given["price"] = digits(given["price"])
        if "phone" in given and len(digits(given["phone"])) < 7:
            print(f"refused: {given['phone']!r} is not a phone number", file=sys.stderr)
            return 2
        cur.update({k: " ".join(v.split()) for k, v in given.items()})
        cur["set"] = today()
        write_json(A("state", "sender.json"), cur)
        A("USER.md").write_text(user_md(cur))
    _, missing = load_sender()
    for k in SENDER_FIELDS:
        print(f"  {k:<8} {cur.get(k) or '-- not set --'}")
    if missing:
        print(f"\nstill needed: {', '.join(missing)}")
        print('  python3 scripts/paul.py sender --area "City, ST" --name "Your Name" '
              '--studio "Studio" --email you@x.com --phone "555 555 5555" --address "PO Box 1, City, ST 00000" '
              '--price 350')
        return 1
    if given:
        print("\nsaved to agents/paul/state/sender.json and USER.md (both stay on this droplet)")
    if not looks_us(cur.get("area")):
        print("\n!! the area does not look like a US one. Cold-email rules outside the US differ")
        print("   (Canada CASL, UK PECR, EU GDPR) - tell Claude before go-live.")
    return 0


def _openclaw_get(key):
    """What `openclaw config get <key>` says, first line, verbatim. Shown,
    never interpreted: these keys differ between OpenClaw versions, and a
    guess about which answer means "unset" has already cost this repo a week
    (set-agent-tools.sh)."""
    if not shutil.which("openclaw"):
        return "openclaw not on PATH - run this on the droplet"
    try:
        r = subprocess.run(["openclaw", "config", "get", key],
                           capture_output=True, text=True, timeout=30)
        first = ((r.stdout or r.stderr or "").strip().splitlines() or ["(empty)"])[0]
        return first[:120]
    except Exception as exc:
        return f"could not ask openclaw ({exc})"


def cmd_preflight(a):
    """Could Paul run a dry cycle, and a live one? Free: no model is woken."""
    block_dry, need_live = [], []
    print("paul preflight - could Paul run? Nothing here wakes a model.\n")

    if A("AGENTS.md").is_file():
        print("  workspace   agents/paul/AGENTS.md present")
    else:
        print("  workspace   AGENTS.md MISSING - Paul is not registered or not deployed")
        block_dry.append("openclaw agents add paul --workspace /root/ecosystem/agents/paul "
                         "--non-interactive && scripts/deploy.sh paul")

    s, missing = load_sender()
    if missing:
        print(f"  sender      NOT SET ({', '.join(missing)})")
        block_dry.append("python3 scripts/paul.py sender --area ... (see README)")
    else:
        print(f"  sender      {s['studio']} - area {s['area']}")

    browser = find_browser()
    if browser:
        tmp = A("state", "preflight-shot.png")
        ok = screenshot(browser, "data:text/html,<h1>paul preflight</h1>", tmp, 390, 844)
        print(f"  browser     {browser} - test screenshot {'ok' if ok else 'FAILED'}")
        if tmp.exists():
            tmp.unlink()
        if not ok:
            need_live.append("the browser is installed but could not take a screenshot")
    else:
        print("  browser     NOT FOUND - no screenshots (dry cycles still run, and say so)")
        need_live.append("install Chrome: see README, 'Install'")

    token = vercel_token()
    if not token:
        print("  vercel      no VERCEL_TOKEN in agents/paul/state/credentials.env")
        need_live.append("python3 scripts/set-credential.py paul VERCEL_TOKEN")
    else:
        user, plan, err = vercel_whoami(token, vercel_team() or None)
        if err:
            print(f"  vercel      {err}")
            need_live.append("a Vercel token that Vercel accepts")
        else:
            print(f"  vercel      token accepted for {user}; plan: {plan or 'not reported'}")
            if (plan or "").lower() == "hobby":
                print("              !! Hobby is for personal, non-commercial use. Pitching sites")
                print("                 for paid work is commercial - your call (README, 'Hosting').")

    # On the Claude plan (2026-10-15) the OpenClaw lines below described an
    # engine Paul no longer wakes on: his searches are Claude's WebSearch and
    # his runs spend nothing from the OpenRouter budget. Said once, here.
    plan = on_the_plan()
    if plan:
        print("  engine      your Claude plan (tasks/engines.json) - web search is Claude's own,")
        print("              and the OpenRouter budget below applies only if the plan refuses a wake")
    # The provider, not tools.web.search itself: once anything under it is
    # set, that is an object, and its first line is "{" (owner's droplet,
    # 2026-10-05, after openclaw configure --section web). One scalar says it.
    print(f"  web search  {'fallback (OpenClaw) ' if plan else ''}provider: {_openclaw_get('tools.web.search.provider')}")
    if not plan:
        print("              (Paul needs a web search tool to find businesses - this should name")
        print("               one, e.g. brave; if it says unset, send Claude this line)")
    # Paul must not heartbeat - it is woken by its timer and nothing else.
    # Not set from here: adding a heartbeat block to one agent can change which
    # agents heartbeat at all, which would be touching the others.
    print(f"  heartbeat   defaults: {_openclaw_get('agents.defaults.heartbeat.every')}")
    print(f"              paul:     {_openclaw_get('agents.entries.paul.heartbeat.every')}")
    print("              (should be off - 0m, or unset with no default interval; if either")
    print("               shows an interval, send Claude these two lines)")

    if places_key():
        print(f"  google      key set - {places_used()} of {PLACES_DAILY} lookups used today")
    else:
        print("  google      no GOOGLE_PLACES_KEY (optional) - prospects are not checked on Google")
    left, need = budget_left(), min_left()
    if left is None:
        print(f"  budget      unreadable - a cycle starts anyway (budget.py's rule)")
    else:
        print(f"  budget      ${max(left, 0):.2f} left on {budget_source()}; "
              + ("not used while he is on the plan" if plan else f"a cycle needs ${need:.2f} to start"))

    live = read_json(A("state", "live.json"))
    print(f"  go-live     {'since ' + live.get('since', '?') if live else 'not yet - dry cycles only'}")
    if not missing:
        print(f"  email rules {'US: CAN-SPAM - the signature carries name, address and an opt-out' if looks_us(s.get('area')) else 'area does not look US - check local rules before go-live'}")

    print()
    if block_dry:
        print("DRY CYCLE: blocked")
        for b in block_dry:
            print(f"  - {b}")
    else:
        print("DRY CYCLE: ready - python3 scripts/paul.py dry-next && systemctl start --no-block paul-cycle.service")
    if block_dry or need_live:
        print("LIVE CYCLES need:")
        for b in block_dry + need_live:
            print(f"  - {b}")
    else:
        print("LIVE CYCLES: ready" + ("" if live else " - after a dry cycle: python3 scripts/paul.py go-live"))
    return 1 if block_dry else 0


def cmd_status(a):
    rows, deps = contacted(), deployments()
    by_slug = {d["slug"]: d for d in deps}
    waiting = [r for r in rows if r["status"] == "drafted"]
    print(f"Paul - {today()}")
    print(f"\nwaiting for you to send ({len(waiting)}):")
    for r in waiting:
        d = by_slug.get(r["slug"], {})
        print(f"  {r['slug']}  {r['business']}  {r['channel']}")
        print(f"      outbox/{r['slug']}/draft.md   {r['preview_url']}   "
              f"(comes down {d.get('teardown') or '?'})")
    if not waiting:
        print("  nothing")
    live = [d for d in deps if d["status"] == "live"]
    print(f"\nlive previews: {len(live)}   removed: {sum(d['status'] == 'removed' for d in deps)}"
          f"   contacted or excluded: {len(rows)}")
    for d in live:
        st = next((r["status"] for r in rows if r["slug"] == d["slug"]), "?")
        print(f"  {d['preview_url']}  {st}  down {d['teardown']}")
    lr = A("state", "last-run.txt")
    print(f"\nlast run: {lr.read_text().strip() if lr.is_file() else 'never'}")
    live_ok = read_json(A("state", "live.json"))
    print(f"go-live: {'since ' + live_ok.get('since', '?') if live_ok else 'not yet (dry cycles only)'}")
    if A("state", "dry-next").is_file():
        print("the next paul-cycle.service run is a DRY cycle")
    return 0


DECK_BUILDS = 12


def cycle_running(run=subprocess.run):
    """Is Paul's cycle running right now? From systemd, which started it."""
    try:
        r = run(["systemctl", "is-active", "paul-cycle.service"], capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() in ("active", "activating")
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_deck(a):
    """Everything the village's Paul panel shows, as JSON - the panel holds
    no rules, so it and `status` cannot disagree. Newest build first."""
    rows = {r["slug"]: r for r in contacted() if r.get("slug")}
    revs = revisions()
    made_from = {r.get("new_slug"): dict(r, slug=old) for old, r in revs.items() if r.get("new_slug")}
    deps = {d["slug"]: d for d in deployments() if d.get("slug")}
    out, newest = [], set()
    for b in reversed(builds()):
        slug = str(b.get("slug") or "")
        site, box = A("sites", slug), A("outbox", slug)
        t = read_json(box / "target.json", {}) if slug else {}
        draft = box / "draft.md"
        text = draft.read_text(errors="replace") if draft.is_file() else None
        # One pitch per business: its newest site's (Royal Edge had four
        # in Drafts, one per revision, 2026-10-21).
        built = bool(slug) and (site / "index.html").is_file()
        current = built and norm_name(b.get("name")) not in newest
        if built:
            newest.add(norm_name(b.get("name")))
        head, message = parse_draft(re.sub(r"\ADRY RUN[^\n]*\n+", "", text or ""))
        out.append({
            "day": b.get("day"), "mode": b.get("mode"), "outcome": b.get("outcome"),
            "slug": slug, "name": b.get("name"), "kind": b.get("kind"), "layout": b.get("layout"),
            "site": built,
            "shots": [n for n, _w, _h in SHOTS if slug and (box / f"{n}.png").is_file()],
            # A preview taken down has no link to open.
            "preview_url": (deps.get(slug) or {}).get("preview_url")
            if (deps.get(slug) or {}).get("status") == "live" else None,
            # On a business's newest card: its older previews still up.
            "older_live": sum(1 for d in deps.values() if d.get("status") == "live" and d["slug"] != slug
                              and norm_name(d.get("business")) == norm_name(b.get("name"))) if current else 0,
            "teardown": (deps.get(slug) or {}).get("teardown") or None,
            "status": (rows.get(slug) or {}).get("status") or None,
            "identity": [{"detail": r.get("detail"), "source": r.get("source")}
                         for r in (t or {}).get("identity") or [] if isinstance(r, dict)],
            "notes": (t or {}).get("notes") or None,
            "draft": text, "current": current,
            "versions": len(versions(b.get("name"))) if built else 0,
            # What the Studio's Copy buttons copy: what to send, and where.
            "pitch": {"to": head.get("to"), "subject": head.get("subject"),
                      "message": message} if text else None,
            # Sent back: how far it got. Made from a note: which note.
            "revision": revs.get(slug),
            "from_note": made_from.get(slug),
        })
        if len(out) >= DECK_BUILDS:
            break
    lr = A("state", "last-run.txt")
    live = read_json(A("state", "live.json"))
    kind, detail = cycle_plan()
    plan = {"kind": kind, "detail": (detail.get("name") if kind == "build" else detail)}
    snd, _missing = load_sender()
    try:
        activity = [l.lstrip("- ").strip() for l in A("MEMORY.md").read_text().splitlines()
                    if l.strip().startswith("-")][-20:][::-1]
    except OSError:
        activity = []
    print(json.dumps({
        "prospects": [dict(p, supplied=supplied(p["id"]) if str(p.get("id") or "").isdigit() else [],
                           maps=maps_link(p.get("name"), (p.get("google") or {}).get("place_id"))
                           if (p.get("google") or {}).get("place_id") else None)
                      for p in reversed(load_prospects())],
        "plan": plan, "activity": activity,
        "studio": {k: snd.get(k) for k in ("studio", "email", "area", "website") if snd.get(k)},
        "today": today(),
        "last_run": lr.read_text().strip() if lr.is_file() else None,
        "go_live": bool(live), "dry_next": A("state", "dry-next").is_file(),
        "running": cycle_running(),
        "focus": focus_kind(),
        "waiting": sum(1 for r in contacted() if r["status"] == "drafted"),
        "builds": out,
    }))
    return 0


def _find(key):
    rows = contacted()
    hit = [r for r in rows if r["slug"] and r["slug"] == key] or \
          [r for r in rows if norm_name(r["business"]) == norm_name(key)]
    return rows, hit


def cmd_mark(a):
    rows, hit = _find(a.who)
    if not hit:
        print(f"no business or slug {a.who!r} in contacted.csv - `paul.py status` lists them",
              file=sys.stderr)
        return 2
    for r in hit:
        r["status"] = a.status
    write_rows(A("state", "contacted.csv"), CONTACTED_FIELDS, rows)
    print(f"{hit[0]['business']}: {a.status}")
    if a.status in TAKE_DOWN_NOW:
        return teardown(only={r["slug"] for r in hit if r["slug"]})
    if a.status in KEEP_LIVE:
        print("its preview stays up past the usual 30 days")
    return 0


def cmd_exclude(a):
    if seen_row(a.business, a.phone or ""):
        print(f"{a.business} is already in contacted.csv - to change it: paul.py mark")
        return 0
    append_row(A("state", "contacted.csv"), CONTACTED_FIELDS,
               {"date": today(), "business": a.business, "phone": a.phone or "",
                "area": (load_sender()[0] or {}).get("area", ""), "status": "do-not-contact"})
    print(f"{a.business}: do-not-contact - Paul will never pick it")
    return 0


def cmd_remove(a, api=None):
    """Delete a business's builds completely (owner, 2026-10-13: "get rid of
    the bakery site completely, I don't want to see it anymore"): its site
    folders, drafts and screenshots, its reports, its lines in the build log
    and its prospect card, and a live preview comes down first. It stays in
    contacted.csv - added as do-not-contact if it was never there - so Paul
    never picks it again; that file is never shown in his studio."""
    n = norm_name(a.who)
    if not n:
        print("say which business")
        return 2
    log = builds()
    hit = [b for b in log if n in norm_name(b.get("name")) or b.get("slug") == a.who]
    names = sorted({b.get("name") for b in hit if b.get("name")})
    for tj in A("outbox").glob("*/target.json") if A("outbox").is_dir() else []:
        nm = (read_json(tj, {}) or {}).get("name")
        if nm and n in norm_name(nm) and nm not in names:
            names.append(nm)
    pros = [p for p in load_prospects() if n in norm_name(p.get("name"))]
    names = sorted(set(names) | {p["name"] for p in pros})
    if not names:
        print(f'nothing of Paul\'s matches "{a.who}"')
        return 1
    if len(names) > 1:
        print(f'"{a.who}" matches more than one business - say which: ' + "; ".join(names))
        return 1
    name = names[0]
    slugs = {b["slug"] for b in log if b.get("name") == name and b.get("slug")}
    slugs |= {tj.parent.name for tj in A("outbox").glob("*/target.json")
              if (read_json(tj, {}) or {}).get("name") == name}
    short = make_slug(name, suffix="x").rsplit("-concept-", 1)[0]
    paths = [A("sites", sl) for sl in slugs] + [A("outbox", sl) for sl in slugs]
    paths += [A("outbox", "_failed", sl) for sl in slugs]
    for folder in ("_below-bar", "_abandoned"):
        paths += [d for d in A("sites", folder).glob(f"*-{short}-*")] if A("sites", folder).is_dir() else []
    reports = [r for r in A("reports").glob("*.md") if any(sl in r.name for sl in slugs)
               or f"-{short}-" in r.name] if A("reports").is_dir() else []
    paths = [p for p in paths + reports if p.exists()]
    live = [d for d in deployments() if d["slug"] in slugs and d.get("status") == "live"]
    print(f"{name}: {len(paths)} file(s)/folder(s), {sum(1 for b in log if b.get('name') == name)} "
          f"build log line(s), {len(pros)} prospect card(s), {len(live)} live preview(s)")
    for p in paths:
        print(f"  {p.relative_to(A())}")
    if not a.yes:
        print(f'nothing deleted - run again with --yes to delete it all')
        return 0
    if live:
        token = vercel_token()
        if not token:
            print("!! a preview is live and there is no VERCEL_TOKEN to take it down - nothing deleted")
            return 1
        deps = deployments()
        for d in deps:
            if d["slug"] in slugs and d.get("status") == "live":
                ok, detail = vercel_remove(d["slug"], token, vercel_team() or None, api=api)
                if not ok:
                    print(f"!! could not take down {d['preview_url']}: {detail} - nothing deleted")
                    return 1
                d["status"], d["removed"] = "removed", today()
        write_rows(A("state", "deployments.csv"), DEPLOY_FIELDS, deps)
    for p in paths:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    kept = [b for b in log if b.get("name") != name]
    A("state", "builds.jsonl").write_text("".join(json.dumps(b) + "\n" for b in kept))
    save_prospects([p for p in load_prospects() if p.get("name") != name])
    if not seen_row(name):
        append_row(A("state", "contacted.csv"), CONTACTED_FIELDS,
                   {"date": today(), "business": name, "area": load_sender()[0].get("area", ""),
                    "status": "do-not-contact"})
    # His log lines about it go too: the studio's Activity tab reads them.
    # contacted.csv already keeps it from ever being picked again.
    for mem in (A("MEMORY.md"), A("memory-archive.md")):
        if mem.is_file():
            lines = mem.read_text().splitlines(keepends=True)
            mem.write_text("".join(l for l in lines if norm_name(name) not in norm_name(l)))
    record("removed one business at the owner's request - every build deleted, never picked again")
    print(f"deleted. {name} is gone from Paul's studio, and he will never pick it again.")
    return 0


def teardown(only=None, list_only=False, day=None, api=None):
    """Take down every preview that is due. Free - no model involved."""
    day = day or today()
    deps = deployments()
    status_of = {r["slug"]: r["status"] for r in contacted() if r["slug"]}
    due = []
    for d in deps:
        if only is not None and d["slug"] not in only:
            continue
        why = due_reason(d, status_of.get(d["slug"], ""), day)
        if why:
            due.append((d, why))
    if list_only:
        for d, why in due:
            print(f"  due: {d['preview_url']} ({d['business']}) - {why}")
        print(f"{len(due)} preview(s) due to come down")
        return 0
    return take_down(deps, due, day, api)


def take_down(deps, due, day, api=None):
    """Remove each (row, why) in due from Vercel and record it in
    deployments.csv (deps is the whole file, rows in it). 0, or 1 if any failed."""
    if not due:
        return 0
    token = vercel_token()
    if not token:
        print(f"!! {len(due)} preview(s) are due to come down but there is no VERCEL_TOKEN")
        return 1
    failed = 0
    for d, why in due:
        ok, detail = vercel_remove(d["slug"], token, vercel_team() or None, api=api)
        if ok:
            d["status"], d["removed"] = "removed", day
            print(f"   took down {d['preview_url']} ({d['business']}) - {why}: {detail}")
        else:
            failed += 1
            print(f"   !! could not take down {d['preview_url']}: {detail}")
    write_rows(A("state", "deployments.csv"), DEPLOY_FIELDS, deps)
    return 1 if failed else 0


def older_previews(who):
    """(name, the newest site's slug, [live deployment rows of its older
    sites], deps) for a business, by name or slug. Its newest site is the
    one its pitch links to; each revision put up a preview of its own."""
    log = [b for b in builds() if b.get("outcome") == "built" and b.get("slug")
           and (A("sites", b["slug"]) / "index.html").is_file()]
    hit = [b for b in log if b["slug"] == who] or [b for b in log if norm_name(b.get("name")) == norm_name(who)]
    if not hit:
        return None, None, [], []
    name = hit[0].get("name")
    newest = [b for b in log if norm_name(b.get("name")) == norm_name(name)][-1]["slug"]
    deps = deployments()
    old = [d for d in deps if d.get("status") == "live" and d.get("slug") != newest
           and norm_name(d.get("business")) == norm_name(name)]
    return name, newest, old, deps


def cmd_retire(a, api=None):
    """Take down a business's older previews and keep its newest (owner,
    2026-10-21: Royal Edge had four up, one per revision). Not once a pitch
    has gone out: the link they were sent may be an older one."""
    name, newest, old, deps = older_previews(a.who)
    if not name:
        print(f'no built site matches "{a.who}"')
        return 1
    row = next((r for r in contacted() if norm_name(r["business"]) == norm_name(name)), None)
    if row and row.get("status") not in ("", "drafted"):
        print(f"{name} is marked {row['status']} - the link they were sent may be an older one, "
              f"so its previews stay up until their usual day")
        return 1
    if not old:
        print(f"{name} has no older preview up - only the newest, {newest}")
        return 0
    print(f"{name}: keeping {newest}, taking down {len(old)} older preview(s)")
    return take_down(deps, [(d, "an older version - the newest stays up") for d in old], today(), api)


def due_reason(d, contact_status, day):
    """Why this preview should come down today, or None. The whole policy."""
    if d.get("status") != "live":
        return None
    if contact_status in TAKE_DOWN_NOW:
        return f"marked {contact_status}"
    if contact_status in KEEP_LIVE:
        return None
    if d.get("teardown") and day > d["teardown"]:
        return f"{TEARDOWN_DAYS} days with no reply"
    return None


def cmd_teardown(a):
    rc = teardown(list_only=a.list)
    if not a.list:
        purge_deleted()
    return rc


# One version deleted from the Studio (owner, 2026-10-22: "I want delete
# this version, never delete the whole business" - the old Delete, on every
# card, took every Royal Edge site and its previews). Its preview comes down
# - Vercel cannot bring a project back - and its files wait here
# DELETED_DAYS for `paul.py undelete`.
DELETED_DAYS = 30


def deleted_dir(slug):
    return A("sites", "_deleted", slug)


def versions(name):
    """A business's built versions with a site on disk, oldest first."""
    return [b for b in builds() if b.get("outcome") == "built" and b.get("slug")
            and norm_name(b.get("name")) == norm_name(name)
            and (A("sites", b["slug"]) / "index.html").is_file()]


def cmd_drop(a, api=None):
    """Delete one version of a business's site. Never its last one, and never
    the one whose link went out in a pitch."""
    log = builds()
    me = next((b for b in log if b.get("slug") == a.slug and b.get("outcome") == "built"), None)
    if not me or not (A("sites", a.slug) / "index.html").is_file():
        print(f"no built site {a.slug}")
        return 1
    name = me.get("name")
    mine = versions(name)
    if len(mine) < 2:
        print(f"this is the only version of {name} - nothing here deletes a whole business. "
              f"To change it, send it back to Paul")
        return 1
    rows = contacted()
    row = next((r for r in rows if norm_name(r["business"]) == norm_name(name)), None)
    if row and row.get("slug") == a.slug and row.get("status") not in ("", "drafted"):
        print(f"this is the version whose link you sent ({row['status']}) - it stays")
        return 1
    deps = deployments()
    live = [d for d in deps if d.get("slug") == a.slug and d.get("status") == "live"]
    if live and take_down(deps, [(d, "this version deleted by the owner") for d in live], today(), api):
        print("its preview could not come down - nothing deleted")
        return 1
    dest = deleted_dir(a.slug)
    dest.mkdir(parents=True, exist_ok=True)
    for kind in ("sites", "outbox"):
        if A(kind, a.slug).exists():
            shutil.move(str(A(kind, a.slug)), str(dest / kind))
    write_json(dest / "deleted.json", {"day": today(), "build": me, "index": log.index(me)})
    A("state", "builds.jsonl").write_text("".join(json.dumps(b) + "\n" for b in log if b is not me))
    print(f"deleted this version of {name} ({a.slug}); its files are kept {DELETED_DAYS} days - "
          f"python3 scripts/paul.py undelete {a.slug}")
    if row and row.get("slug") == a.slug:
        # The pitch pointed here and was never sent: it moves to the newest left.
        now = versions(name)[-1]["slug"]
        dep = next((d for d in reversed(deps) if d.get("slug") == now), {})
        row.update(slug=now, preview_url=dep.get("preview_url") or "")
        write_rows(A("state", "contacted.csv"), CONTACTED_FIELDS, rows)
        print(f"its pitch is now the one for {now}" + ("" if dep.get("status") == "live" else
              " - that version has no live preview; send it back to Paul for one"))
    record(f"the owner deleted one version of {name}: {a.slug}")
    return 0


def cmd_undelete(a):
    """A version deleted from the Studio, back - files and its card. Its
    preview does not come back; Send back to Paul makes a new one."""
    dest = deleted_dir(a.slug)
    info = read_json(dest / "deleted.json", {}) or {}
    if not info.get("build"):
        print(f"{a.slug} is not among the deleted versions")
        return 1
    for kind in ("sites", "outbox"):
        if (dest / kind).exists():
            if A(kind, a.slug).exists():
                print(f"{kind}/{a.slug} exists again - nothing changed")
                return 1
            A(kind).mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest / kind), str(A(kind, a.slug)))
    log = builds()
    log.insert(min(int(info.get("index") or 0), len(log)), info["build"])
    A("state", "builds.jsonl").write_text("".join(json.dumps(b) + "\n" for b in log))
    shutil.rmtree(dest)
    print(f"{info['build'].get('name')} {a.slug} is back - its preview is not; send it back to Paul for a new one")
    return 0


def purge_deleted(day=None):
    """Deleted versions older than DELETED_DAYS are gone for good."""
    day = day or today()
    root = A("sites", "_deleted")
    for d in sorted(root.glob("*")) if root.is_dir() else []:
        when = (read_json(d / "deleted.json", {}) or {}).get("day")
        if when and day > day_plus(when, DELETED_DAYS):
            shutil.rmtree(d)
            print(f"   deleted version {d.name} is past {DELETED_DAYS} days - gone")


def cmd_go_live(a):
    if a.off:
        p = A("state", "live.json")
        if p.exists():
            p.unlink()
        print("live cycles paused - the timer still fires, and holds without waking a model")
        return 0
    problems = []
    _, missing = load_sender()
    if missing:
        problems.append("sender details are not set: python3 scripts/paul.py sender ...")
    token = vercel_token()
    if not token:
        problems.append("no VERCEL_TOKEN: python3 scripts/set-credential.py paul VERCEL_TOKEN")
    else:
        _user, _plan, err = vercel_whoami(token, vercel_team() or None)
        if err:
            problems.append(err)
    if not find_browser():
        problems.append("no headless browser for the screenshots - install Chrome (README)")
    if not a.anyway and not any(b.get("mode") == "dry" and b.get("outcome") == "built"
                                for b in builds()):
        problems.append("no dry cycle has built a site yet - see one first: "
                        "python3 scripts/paul.py dry-next && systemctl start --no-block "
                        "paul-cycle.service  (or go-live --anyway)")
    if problems:
        print("not going live:")
        for p in problems:
            print(f"  - {p}")
        return 1
    write_json(A("state", "live.json"), {"since": today()})
    print(f"live from the next timer run. Pause: python3 scripts/paul.py go-live --off")
    return 0


def strip_safeguards(site):
    """apply_safeguards undone, for a site going back to Paul: he edits what
    he built, and code adds the notice, noindex and robots again at finish."""
    site = Path(site)
    for page in site.rglob("*.html"):
        markup = re.sub(rf'(?is)<p[^>]*id="({CONCEPT_ID}|{CREDITS_ID})"[^>]*>.*?</p>', "",
                        page.read_text(errors="replace"))
        page.write_text(re.sub(r'(?i)<meta[^>]+name=["\']robots["\'][^>]*>', "", markup))
    for name in ("robots.txt", "vercel.json"):
        (site / name).unlink(missing_ok=True)


def cmd_revise(a):
    """Send a finished site back to Paul with the owner's notes (2026-10-13:
    College Hill Barbers). It goes back into work/ - the folder a cycle
    resumes - with REVISE.md saying what to fix, so the next cycle fixes
    this business instead of finding a new one. The old build stays where
    it is; the fixed one is filed beside it."""
    work = A("work")
    if work.is_dir() and any(work.iterdir()):
        print("work/ already holds an unfinished build - the next cycle resumes it. Let it "
              "finish first, then send this one back.")
        return 1
    key = " ".join(a.who.split())
    hits = []
    for tj in sorted(A("outbox").glob("*/target.json")):
        slug = tj.parent.name
        if slug.startswith("_"):
            continue
        t = read_json(tj, {}) or {}
        if key == slug or (norm_name(key) and norm_name(key) in norm_name(t.get("name") or "")):
            if (A("sites", slug) / "index.html").is_file():
                hits.append((A("sites", slug).stat().st_mtime, slug, t))
    names = sorted({t.get("name") for _m, _s, t in hits})
    if not hits:
        print(f'no finished site matches "{key}" - `paul.py status` lists them')
        return 1
    if len(names) > 1:
        print(f'"{key}" matches more than one business - say which: ' + "; ".join(names))
        return 1
    _m, slug, t = max(hits)
    notes = " ".join(a.notes).strip()
    if not notes:
        print("say what to fix - those words are what Paul reads")
        return 2
    given = getattr(a, "ref", None)
    ref = clean_ref(given) if given else None
    if given and not ref:
        print(f'"{given}" is not a web address - give the whole https://... link')
        return 1
    # A revision of this site that did not ship is newer work than the site:
    # Royal Edge's fixed photo was set aside by a refusal that was the
    # check's fault (2026-10-21). Sending it back again resumes that work.
    tried = [d for d in (A("sites", "_below-bar").glob("*") if A("sites", "_below-bar").is_dir() else [])
             if (d / "site" / "index.html").is_file() and (
                 (read_json(d / "revise.json", {}) or {}).get("slug") == slug
                 or f"(sites/{slug})" in ((d / "REVISE.md").read_text(errors="replace")
                                          if (d / "REVISE.md").is_file() else ""))]
    tried = max(tried, key=lambda d: d.stat().st_mtime, default=None)
    before = ""
    if tried and tried.stat().st_mtime > A("sites", slug).stat().st_mtime:
        before = (tried / "REVISE.md").read_text(errors="replace") if (tried / "REVISE.md").is_file() else ""
        if work.is_dir():
            work.rmdir()
        shutil.move(str(tried), str(work))
        (work / "REVISE.md").unlink(missing_ok=True)
        print(f"resuming your last revision of it (from {tried.relative_to(A()).as_posix()}), not the old site")
    else:
        shutil.copytree(A("sites", slug), work / "site")
        strip_safeguards(work / "site")
        write_json(work / "target.json", t)
        old = A("outbox", slug, "draft.md")
        if old.is_file():
            # The pitch rides along as it is; code puts the new link in.
            (work / "draft.md").write_text(reusable_draft(old.read_text(errors="replace")))
    write_json(work / "revise.json", {"slug": slug})
    # The prospect rides along, as in a build: its id is where the owner's
    # saved photos are (Royal Edge's revision had none placed, 2026-10-21),
    # and its reference is the look - a new one when the owner gives it
    # ("I have a new sample site for Paul to use", 2026-10-21).
    rows = load_prospects()
    n = norm_name(t.get("name")).replace(" ", "")
    mine = [p for p in rows if p.get("slug") == slug] or [
        p for p in rows if n and (n in norm_name(p.get("name")).replace(" ", "")
                                  or norm_name(p.get("name")).replace(" ", "") in n)]
    p = max(mine, key=lambda p: p.get("status") == "built") if mine else {"name": t.get("name")}
    if ref:
        p["reference"] = ref
        if mine:
            save_prospects(rows)
    write_json(work / "prospect.json", p)
    earlier = re.search(r"## What the owner wants fixed\n\n(.*?)\n\n", before, re.S)
    (work / "REVISE.md").write_text(
        f"# Sent back by the owner - {today()}\n\n"
        f"This is your build of {t.get('name')} (sites/{slug}). Fix it - do not pick a new "
        f"business. Keep what works; change what the owner asks, then run check.\n\n"
        + (f"You already worked on this once; your work is in work/site/. The owner asked "
           f"then: {earlier.group(1).strip()}\n\n" if earlier else "")
        + f"## What the owner wants fixed\n\n{notes}\n\n"
        + (f"The owner picked a new reference site for the look: {ref} (FACTS: OWNER'S REF). "
           f"Rebuild the layout after it; keep this business's facts, photos and claims.\n\n"
           if ref else "")
        + f"Your pitch is carried over in work/draft.md, with {PREVIEW_URL} where code puts the "
        f"new link. Leave it as it is unless what you change makes it wrong - then change only "
        f"that. Update work/target.json if the photos or claims change.\n")
    set_revision(slug, state="sent", notes=notes, name=t.get("name"))
    record((f"sent back by the owner: {t.get('name')} - {notes}"
            + (f" (after {ref})" if ref else ""))[:300])
    print(f"{t.get('name')} is back in work/ with your notes"
          + (f", built after {ref}" if ref else "") + "; the next cycle fixes it.")
    print("  a dry cycle now:  python3 scripts/paul.py dry-next && systemctl start --no-block paul-cycle.service")
    return 0


def cmd_measure(a):
    """What the browser checks find on a build, without waking Paul: work/ if
    it holds one, else the newest build set aside in sites/_below-bar/
    (Royal Edge, 2026-10-21: refused twice, and the owner asked how to fix
    it). Nothing is changed but the measurement memo beside it."""
    if a.folder:
        d = Path(a.folder)
    elif A("work", "site", "index.html").is_file():
        d = A("work")
    else:
        aside = [x for x in A("sites", "_below-bar").glob("*") if (x / "site" / "index.html").is_file()] \
            if A("sites", "_below-bar").is_dir() else []
        d = max(aside, key=lambda x: x.stat().st_mtime, default=None)
    if not d or not (d / "site" / "index.html").is_file():
        print("no build to measure - give a folder that holds site/index.html")
        return 1
    browser = find_browser()
    if not browser:
        print("no headless browser on this machine - see paul.py preflight")
        return 1
    t = read_json(d / "target.json", {}) or {}
    errs, notes = browser_problems(t, d, browser)
    print(f"{t.get('name') or '?'} - {d.relative_to(A()) if d.is_relative_to(A()) else d}")
    for e in errs:
        print(f"  - {e}")
    for n in notes:
        print(f"  note: {n}")
    if not errs:
        print("  the browser checks find nothing to refuse")
    return 1 if errs else 0


def mark_dry_next():
    m = A("state", "dry-next")
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(today() + "\n")


def cmd_dry_next(a):
    m = A("state", "dry-next")
    if a.take:
        if m.is_file():
            m.unlink()
            return 0
        return 1
    mark_dry_next()
    print("the next paul-cycle.service run will be a dry cycle:")
    print("  systemctl start --no-block paul-cycle.service")
    return 0


# --------------------------------------------------------------------------
# commands: the wrapper
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# prospects: Paul finds, the owner picks, Paul builds (owner, 2026-10-13)
# --------------------------------------------------------------------------
#
# Paul used to choose a business and build it in one cycle, so every site
# the owner would not have chosen was paid for anyway. Now a cycle is one of
# three, and code decides which (cycle_plan):
#
#   scout   find new prospects - research only, nothing built - until
#           PROSPECT_POOL wait for the owner
#   build   build the oldest prospect the owner approved
#   resume  finish what is in work/ (an interrupted build, or a site the
#           owner sent back with `revise`)
#
# and when the pool is full and nothing is approved, no model is woken.

# --------------------------------------------------------------------------
# Google: a prospect's listing checked, its photos pointed at - never copied
# --------------------------------------------------------------------------
#
# Owner, 2026-10-15: Paul could not see businesses' Facebook or Instagram
# (a login wall), so their real photos never reached his sites. Google's
# Places API answers instead - but its terms say "You must not pre-fetch,
# cache, or store Places API content" beyond the place ID, and photos shown
# must carry their author and Google's logo. So code asks Google once per
# new prospect and keeps only what it decided: the place ID (allowed
# indefinitely), how many photos the listing has and how many the business
# posted itself, and what disagreed with what Paul found. The Studio links
# to the listing; the owner saves the photos worth having and uploads them.
#
# Text Search with phone/website is the Enterprise SKU: 1,000 a month free,
# then $35 per 1,000 (Google's price page, 2026-10-15). PLACES_DAILY keeps
# a runaway loop inside the free month.
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = ("places.id,places.displayName,places.nationalPhoneNumber,places.websiteUri,"
                 "places.businessStatus,places.photos")
PLACES_DAILY = 25


class PlacesError(Exception):
    pass


def places_key():
    return (credentials().get("GOOGLE_PLACES_KEY") or os.environ.get("GOOGLE_PLACES_KEY") or "").strip()


def places_used(bump=False):
    """Lookups made today; bump=True counts one more first."""
    path = A("state", "places-usage.json")
    d = read_json(path, {}) or {}
    n = int(d.get("n") or 0) if d.get("day") == today() else 0
    if bump:
        n += 1
        write_json(path, {"day": today(), "n": n})
    return n


def _places_post(body, key, timeout=20, fields=PLACES_FIELDS):
    req = urllib.request.Request(PLACES_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": fields})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode()).get("error", {}).get("message") or ""
        except Exception:
            msg = ""
        raise PlacesError(f"Google said {exc.code}: {msg[:160] or exc.reason}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PlacesError(f"Google could not be reached: {str(exc)[:120]}") from None


def same_business(a, b):
    """One name inside the other once both are normalised: "Milkbox" and
    "Milkbox Coffee Bar" are one shop; "Rosa's" and "Rosati's" are not."""
    x, y = norm_name(a), norm_name(b)
    return bool(x and y) and (x == y or f" {x} " in f" {y} " or f" {y} " in f" {x} ")


def google_check(p, key, post=_places_post):
    """What Google's listing says about a prospect, as code's verdict - never
    Google's content. {checked, found, place_id, photos, owner_photos, flags}."""
    if places_used() >= PLACES_DAILY:
        raise PlacesError(f"today's {PLACES_DAILY} Google lookups are used - checked tomorrow")
    places_used(bump=True)
    area = str(p.get("address") or "") or str(load_sender()[0].get("area") or "")
    d = post({"textQuery": f"{p.get('name')} {area}".strip(), "pageSize": 3}, key)
    place = next((x for x in d.get("places") or []
                  if same_business(p.get("name"), (x.get("displayName") or {}).get("text"))), None)
    if not place:
        return {"checked": today(), "found": False}
    shown = (place.get("displayName") or {}).get("text")
    photos = [x for x in place.get("photos") or [] if isinstance(x, dict)]
    own = [x for x in photos if any(same_business(shown, (w or {}).get("displayName"))
                                    for w in x.get("authorAttributions") or [])]
    flags = []
    status = place.get("businessStatus")
    if status == "CLOSED_PERMANENTLY":
        flags.append(CLOSED_FOR_GOOD)
    elif status == "CLOSED_TEMPORARILY":
        flags.append("Google lists it as temporarily closed")
    theirs, ours = norm_phone(place.get("nationalPhoneNumber")), norm_phone(p.get("phone"))
    if theirs and ours and theirs != ours:
        flags.append("the phone Paul found is not the one on their Google listing")
    if (place.get("websiteUri") and not is_social(place["websiteUri"])
            and str(p.get("current_site") or "").strip().lower() in ("", "none")):
        flags.append("Google lists a website for them - Paul said they have none")
    return {"checked": today(), "found": True, "place_id": place.get("id"),
            "photos": len(photos), "owner_photos": len(own), "flags": flags}


CLOSED_FOR_GOOD = "Google lists it as permanently closed"


def maps_link(name, place_id):
    """Google's documented Maps URL for one place - built from the stored ID."""
    return ("https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(str(name or ""))
            + "&query_place_id=" + urllib.parse.quote(str(place_id or "")))


def check_on_google(p, key=None, post=_places_post):
    """p["google"], set in place. No key: nothing, and nothing is held up."""
    key = key if key is not None else places_key()
    if not key:
        return p
    try:
        p["google"] = google_check(p, key, post)
    except PlacesError as exc:
        p["google"] = {"checked": today(), "error": str(exc)[:200]}
    return p


# --------------------------------------------------------------------------
# the finder: Google's listings searched for businesses with no site of their
# own, and code's own checks on the sites the rest have
# --------------------------------------------------------------------------
#
# Owner, 2026-10-19: Paul checked six Cedar Falls businesses and listed none.
# A web search finds businesses that HAVE websites, and the three he did find
# with only a Facebook page he dropped because nothing proved they had no
# site. A Google listing with no website on it is that proof. So before a
# scout wakes him, code searches a few kinds of business in the area and the
# nearby towns - a different few each time - and hands him the leads:
# listings with no website, or only a Facebook or Instagram page, and
# businesses whose own site fails one of code's checks. He verifies each and
# finds a way to reach it; code checks his file as before.
#
# Nothing Google says is kept: the leads go into this cycle's FACTS and are
# gone. Only which searches were run, and when, is written down - our own
# queries - so the next scout asks about something else.
SEARCHES = (("barber shop", "salon"), ("hair salon", "salon"), ("nail salon", "salon"),
            ("tattoo shop", "other"), ("auto repair shop", "trade"), ("plumber", "trade"),
            ("electrician", "trade"), ("roofing contractor", "trade"), ("landscaping company", "trade"),
            ("house cleaning service", "other"), ("bakery", "food"), ("food truck", "food"),
            ("family restaurant", "food"), ("coffee shop", "food"), ("clothing boutique", "retail"),
            ("florist", "retail"), ("pet grooming", "other"), ("photographer", "other"),
            ("handyman", "trade"), ("painting contractor", "trade"), ("massage therapist", "salon"),
            ("gift shop", "retail"), ("catering", "food"), ("lawn care", "trade"))
SEARCHES_PER_SCOUT = 4
SITES_CHECKED_PER_SCOUT = 10      # each is two fetches; the rest wait for the next scout
LEADS_MAX = 15                    # about what Paul is told to look at
KEEP_FOR_PROSPECTS = 6            # Google lookups left for check_on_google on the day's finds
FINDER_FIELDS = ("places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
                 "places.websiteUri,places.businessStatus")
WEAK = ("dead-domain", "parked", "no-https", "not-mobile", "no-seo", "outdated")


def towns():
    """The area, then the nearby towns the owner added (`paul.py nearby`)."""
    s = load_sender()[0]
    extra = [t for t in (read_json(A("state", "nearby.json"), []) or []) if str(t).strip()]
    return [t for t in [str(s.get("area") or "").strip()] + extra if t]


def next_searches(n=SEARCHES_PER_SCOUT, focus=None, done=None):
    """The n (kind of business, town) pairs searched longest ago - never yet
    first - within the focus if there is one. Each kind is listed town by
    town, so a scout covers the nearby towns too, not only once the area's
    every kind is done."""
    done = done if done is not None else (read_json(A("state", "finder.json"), {}) or {}).get("done", {})
    pairs = [(q, k, t) for q, k in SEARCHES for t in towns() if not focus or k == focus]
    pairs.sort(key=lambda x: done.get(f"{x[0]}|{x[2]}", ""))          # stable: the order above breaks ties
    return pairs[:n]


def site_weakness(url, get=http_get):
    """[(code, detail)] code confirms on a business's own site."""
    rows, _page = verify_problems({"current_site": url, "problems": [{"code": c} for c in WEAK]}, get)
    return [(c, d) for c, verdict, d in rows if verdict == "confirmed"]


def find_leads(key, post=_places_post, get=http_get, say=print, remember=True):
    """Leads for this scout, as plain dicts that live only as long as it.
    remember=False (paul.py leads) leaves the searches for the next scout."""
    known = {norm_name(p.get("name")) for p in load_prospects()}
    leads, seen_ids, sites = [], set(), 0
    state = read_json(A("state", "finder.json"), {}) or {}
    done = state.get("done", {})
    for q, kind, town in next_searches(focus=focus_kind(), done=done):
        if places_used() >= PLACES_DAILY - KEEP_FOR_PROSPECTS:
            say(f"finder: stopping at today's Google allowance ({PLACES_DAILY}, {KEEP_FOR_PROSPECTS} kept back)")
            break
        places_used(bump=True)
        try:
            d = post({"textQuery": f"{q} in {town}", "pageSize": 20}, key, fields=FINDER_FIELDS)
        except PlacesError as exc:
            say(f"finder: {q} in {town} - {exc}")
            continue
        done[f"{q}|{town}"] = today()
        for x in d.get("places") or []:
            name = (x.get("displayName") or {}).get("text") or ""
            if (not name or x.get("id") in seen_ids or x.get("businessStatus") not in (None, "OPERATIONAL")
                    or norm_name(name) in known or seen_row(name, x.get("nationalPhoneNumber") or "")):
                continue
            seen_ids.add(x.get("id"))
            site = str(x.get("websiteUri") or "").strip()
            lead = {"name": name, "kind": kind, "category": q, "address": x.get("formattedAddress") or "",
                    "phone": x.get("nationalPhoneNumber") or "", "maps": maps_link(name, x.get("id"))}
            if not site:
                lead.update(code="no-site", why="no-site: no website on its Google listing")
            elif is_social(site):
                lead.update(code="social-only", why=f"social-only: its Google listing's website is {site}", site=site)
            elif sites < SITES_CHECKED_PER_SCOUT:
                sites += 1
                weak = site_weakness(site, get)
                if not weak:
                    continue
                lead.update(code=weak[0][0], why=f"{site} - " + "; ".join(f"{c}: {d}" for c, d in weak), site=site)
            else:
                continue
            leads.append(lead)
    if remember:
        write_json(A("state", "finder.json"), {**state, "done": done})
    # No site at all first, then a social page, then a weak site.
    order = {"no-site": 0, "social-only": 1}
    leads.sort(key=lambda l: order.get(l["code"], 2))
    return leads[:LEADS_MAX]


def lead_lines(leads):
    if not leads:
        return ["leads        none from Google today - search the area yourself"]
    out = ["leads        from Google's listings and code's checks of their sites, today. Start",
           "             here. Each is a lead, not a fact: open its Maps link and its sources,",
           "             and confirm before you list it. A listing with no website IS your",
           "             no-site evidence - cite the Maps link."]
    for i, l in enumerate(leads, 1):
        out.append(f"             {i:>2}. {l['name']} ({l['category']}, kind {l['kind']}) | {l['address']}"
                   f" | {l['phone'] or 'no phone listed'}")
        out.append(f"                 {l['why']}")
        out.append(f"                 {l['maps']}")
    return out


def cmd_leads(a):
    """What the next scout would be handed, without waking Paul or using up
    its searches. Costs Google lookups from today's allowance."""
    key = places_key()
    if not key:
        print("no GOOGLE_PLACES_KEY - python3 scripts/set-credential.py paul GOOGLE_PLACES_KEY")
        return 1
    print("searching: " + "; ".join(f"{q} in {t}" for q, _k, t in next_searches(focus=focus_kind())))
    print("\n".join(lead_lines(find_leads(key, remember=False))))
    print(f"\nGoogle lookups today: {places_used()} of {PLACES_DAILY}")
    return 0


def cmd_nearby(a):
    """Other towns the finder searches besides the area."""
    path = A("state", "nearby.json")
    cur = [t for t in (read_json(path, []) or []) if str(t).strip()]
    if a.clear:
        cur = []
    for t in a.towns or []:
        t = " ".join(t.split())
        if t and t not in cur:
            cur.append(t)
    if a.towns or a.clear:
        write_json(path, cur)
    print("searched: " + " · ".join(towns()))
    return 0


def cmd_places(a, post=_places_post):
    """Check one prospect against Google again, now."""
    key = places_key()
    if not key:
        print("no GOOGLE_PLACES_KEY - python3 scripts/set-credential.py paul GOOGLE_PLACES_KEY")
        return 1
    rows = load_prospects()
    p, err = find_prospect(rows, a.who)
    if not p:
        print(err)
        return 1
    check_on_google(p, key, post)
    save_prospects(rows)
    g = p.get("google") or {}
    if g.get("error"):
        print(f"{p['name']}: {g['error']}")
        return 1
    if not g.get("found"):
        print(f"{p['name']}: no Google listing by that name")
        return 0
    print(f"{p['name']}: {g['photos']} photo(s) on Google, {g['owner_photos']} posted by the business")
    for f in g["flags"]:
        print(f"  !! {f}")
    print(f"  {maps_link(p['name'], g['place_id'])}")
    return 0


# --------------------------------------------------------------------------
# photos the owner saved for a prospect (the Studio's Add photos)
# --------------------------------------------------------------------------
#
# Their Facebook, Instagram and Google photos are behind a login or a
# licence that rules Paul out - but the owner can open them on the iPad and
# save the good ones. They live in state/photos/<prospect id>/, go into
# work/site/img/ as yours-NN when the build starts, and the check holds the
# site until it uses them.
PHOTO_FROM = ("facebook", "instagram", "google", "website", "owner")
MAX_SUPPLIED = 12
SUPPLIED_MAX_BYTES = 6_000_000
SUPPLIED_PREFIX = "yours-"


def photo_dir(pid):
    return A("state", "photos", str(int(pid)))


def supplied(pid):
    """[{file, from, added}] saved for this prospect, files that exist only."""
    d = photo_dir(pid)
    rows = read_json(d / "photos.json", []) or []
    return [r for r in rows if isinstance(r, dict) and (d / str(r.get("file"))).is_file()]


def image_kind(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return None


def add_photo(pid, data, origin):
    """(file, error) - one photo saved for prospect pid."""
    if origin not in PHOTO_FROM:
        return None, f"say where it is from: {', '.join(PHOTO_FROM)}"
    ext = image_kind(data)
    if not ext:
        return None, "not a JPEG or PNG"
    if len(data) > SUPPLIED_MAX_BYTES:
        return None, f"{len(data) // 1024} KB - under {SUPPLIED_MAX_BYTES // 1_000_000} MB please"
    have = supplied(pid)
    if len(have) >= MAX_SUPPLIED:
        return None, f"{MAX_SUPPLIED} photos is plenty - remove one first"
    d = photo_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    used = {int(m.group(1)) for r in have for m in [re.match(r"(\d+)", str(r["file"]))] if m}
    n = next(i for i in range(1, 1000) if i not in used)
    name = f"{n:02d}{ext}"
    tmp = d / f".{name}.part"
    tmp.write_bytes(data)
    tmp.replace(d / name)
    write_json(d / "photos.json", have + [{"file": name, "from": origin, "added": today()}])
    return name, None


def remove_photo(pid, name):
    d = photo_dir(pid)
    have = supplied(pid)
    if not any(r["file"] == name for r in have):
        return False
    (d / name).unlink(missing_ok=True)
    write_json(d / "photos.json", [r for r in have if r["file"] != name])
    return True


# --------------------------------------------------------------------------
# stock: Wikimedia Commons, credited by code
# --------------------------------------------------------------------------
#
# `paul.py stock find <words>` lists photos from Hallmark's library of vetted
# Commons photos (agents/paul/hallmark/photos.json) and from a Commons search;
# `paul.py stock get <File:...>` downloads one at the width a page needs and
# records whose it is in site/photo-credits.json. check refuses a stock photo
# that is not in that file, and finish writes the credits onto every page:
# Paul never types a licence, so none can be wrong.

HALLMARK = Path(__file__).resolve().parent.parent / "agents" / "paul" / "hallmark"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_FILE = "https://commons.wikimedia.org/wiki/File:"
# Wikimedia's rule for scripts: say who you are and how to reach you.
WIKI_UA = "PaulSiteBuilder/1.0 (https://github.com/cljxs/cljxs; local business site previews)"
CREDITS = "photo-credits.json"
CREDITS_ID = "paul-credits"
FREE_LICENSE = re.compile(r"^(cc0|cc-zero|public domain|pd\b.*|cc by(-sa)? [1-4]\.0\b.*)$", re.I)
# Commons holds re-uploads of stock sites' photos, whose terms we left them for.
FROM_STOCK_SITES = re.compile(r"unsplash|pexels|pixabay", re.I)
THUMB_STEPS = (500, 960, 1280, 1920)


def wiki_get(url, binary=False, timeout=30, limit=4_000_000):
    """One GET to Wikimedia with its required User-Agent: (status, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit)
            return r.status, (data if binary else data.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, b"" if binary else ""
    except Exception as e:
        return None, str(getattr(e, "reason", e))[:160]


def plain(markup):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(markup or ""))).split())


def license_url(short):
    s = str(short or "").strip().lower()
    m = re.match(r"cc (by(?:-sa)?) ([1-4]\.0)", s)
    if m:
        return f"https://creativecommons.org/licenses/{m.group(1)}/{m.group(2)}/"
    if s in ("cc0", "cc-zero"):
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    return None


def library():
    return (read_json(HALLMARK / "photos.json", {}) or {}).get("photos") or []


def library_find(words, limit=8):
    want = [w for w in re.findall(r"[a-z0-9]+", " ".join(words).lower()) if len(w) > 2]
    scored = []
    for p in library():
        hay = " ".join([p.get("subject", ""), p.get("cat", ""), p.get("id", "")] +
                       [str(t) for t in p.get("tags") or []]).lower()
        n = sum(1 for w in want if w in hay)
        if n:
            scored.append((-n, p["id"], p))
    return [p for _n, _i, p in sorted(scored)[:limit]]


def commons_info(pages):
    """Commons API pages (formatversion=2) as our rows; unusable ones left out."""
    out = []
    for pg in pages or []:
        ii = (pg.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        val = lambda k: plain((meta.get(k) or {}).get("value"))
        lic, author = val("LicenseShortName"), val("Artist")
        said = " ".join([val("Credit"), val("ImageDescription"), author])
        if not (FREE_LICENSE.match(lic) and author and ii.get("url")) or FROM_STOCK_SITES.search(said):
            continue
        if str(ii.get("mime") or "") not in ("image/jpeg", "image/png"):
            continue
        out.append({"id": pg.get("title", "").removeprefix("File:"), "title": pg.get("title"),
                    "page": ii.get("descriptionurl") or COMMONS_FILE + pg.get("title", "").removeprefix("File:"),
                    "file": ii["url"], "thumb": ii.get("thumburl"), "w": ii.get("width"), "h": ii.get("height"),
                    "license": lic, "author": author, "subject": val("ImageDescription")[:120]})
    return out


def commons_query(params, get=wiki_get):
    q = {"action": "query", "format": "json", "formatversion": "2", "prop": "imageinfo",
         "iiprop": "url|size|mime|extmetadata", "maxlag": "5",
         "iiextmetadatafilter": "Artist|LicenseShortName|Credit|ImageDescription", **params}
    status, body = get(COMMONS_API + "?" + urllib.parse.urlencode(q))
    if status != 200:
        return None, f"Commons answered {status}: {str(body)[:120]}"
    try:
        return commons_info((json.loads(body).get("query") or {}).get("pages")), ""
    except ValueError:
        return None, "Commons did not answer in JSON"


def thumb_for(file_url, width):
    """upload.wikimedia.org's thumbnail of a file at a standard width."""
    m = re.match(r"(https://upload\.wikimedia\.org/wikipedia/commons)/(\w/\w\w)/([^/?#]+)$", str(file_url or ""))
    if not m:
        return None
    name = m.group(3)
    return f"{m.group(1)}/thumb/{m.group(2)}/{name}/{width}px-{name}" + (".jpg" if name.lower().endswith((".tif", ".tiff")) else "")


def cmd_stock(a, get=wiki_get):
    if a.action == "find":
        lib = library_find(a.words, a.limit)
        for p in lib:
            print(f"  lib  File:{p['id']}  {p.get('w')}x{p.get('h')}  {p['license']}  {p['author']} - {p.get('subject', '')}")
        found, why = commons_query({"generator": "search", "gsrnamespace": "6", "gsrlimit": str(a.limit * 2),
                                    "gsrsearch": " ".join(a.words) + " filetype:bitmap", "iiurlwidth": "960"}, get)
        for p in (found or [])[:a.limit]:
            print(f"  web  {p['title']}  {p['w']}x{p['h']}  {p['license']}  {p['author']} - {p['subject']}")
        if found is None:
            print(f"  (Commons search: {why})")
        if not lib and not found:
            print("nothing found - try other words: what is in the picture, not the business")
        print("then: python3 ../../scripts/paul.py stock get \"File:<name>\" [--width 1280] - and look at it")
        return 0
    title = str(a.which or "").strip()
    title = urllib.parse.unquote(title.split("/wiki/", 1)[-1]) if "/wiki/" in title else title
    name = title.removeprefix("File:").replace("_", " ")
    hit = next((p for p in library() if p["id"].replace("_", " ") == name), None)
    width = next((w for w in THUMB_STEPS if w >= a.width), THUMB_STEPS[-1])
    if hit:
        row = dict(hit, title="File:" + hit["id"])
        url = thumb_for(hit["file"], width) if (hit.get("w") or 0) > width else hit["file"]
    else:
        found, why = commons_query({"titles": "File:" + name, "iiurlwidth": str(width)}, get)
        if not found:
            print(f"File:{name}: " + (why or "not on Commons, or not under a licence we may use "
                                      "(CC0, public domain, CC BY, CC BY-SA), or from a stock site"))
            return 1
        row = found[0]
        url = row["thumb"] if (row.get("w") or 0) > width and row.get("thumb") else row["file"]
    status, data = get(url, binary=True)
    if status != 200 or not isinstance(data, bytes) or not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        print(f"could not download {url} ({status})")
        return 1
    site = A("work", "site")
    stem = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name.rsplit(".", 1)[0]).encode("ascii", "ignore")
                  .decode().lower()).strip("-")[:40] or "photo"
    rel = f"img/stock-{stem}" + (".png" if data[:4] == b"\x89PNG" else ".jpg")
    (site / "img").mkdir(parents=True, exist_ok=True)
    (site / rel).write_bytes(data)
    credits = read_json(site / CREDITS, {}) or {}
    page = row.get("page") or COMMONS_FILE + name.replace(" ", "_")
    credits[rel] = {"title": name, "page": page, "author": row["author"], "license": row["license"],
                    "license_url": license_url(row["license"])}
    write_json(site / CREDITS, credits)
    print(f"saved work/site/{rel} ({len(data) // 1024} KB) - {row['license']}, {row['author']}")
    print(f'list it in target.json photos: {{"file": "{rel}", "source": "{page}", "kind": "stock"}}'
          f' (or "sample") - code writes the credit on the page')
    return 0


def credits_line(site):
    """The credits for the Commons photos a site shows: title, author,
    source and licence, as CC BY asks. None when it shows none."""
    site = Path(site)
    used = set(scan_site(site)["images"])
    rows = [(rel, c) for rel, c in sorted((read_json(site / CREDITS, {}) or {}).items()) if rel in used]
    if not rows:
        return None
    bits = []
    for _rel, c in rows:
        lic = (f'<a href="{html.escape(c["license_url"], quote=True)}">{html.escape(c["license"])}</a>'
               if c.get("license_url") else html.escape(c.get("license") or ""))
        bits.append(f'<a href="{html.escape(c["page"], quote=True)}">{html.escape(c["title"])}</a> by '
                    f'{html.escape(c["author"])}, {lic}')
    return (f'<p id="{CREDITS_ID}" style="margin:0;padding:0 16px 14px;font:12px/1.5 system-ui,sans-serif;'
            f'text-align:center;opacity:.75">Photos from Wikimedia Commons: ' + "; ".join(bits) + ".</p>")


def place_supplied(pid, site):
    """Copy the owner's photos into the build's img/ as yours-NN, once each.
    [(rel path, from)]."""
    out = []
    img = Path(site) / "img"
    for r in supplied(pid):
        dest = img / f"{SUPPLIED_PREFIX}{r['file']}"
        if not dest.exists():
            img.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(photo_dir(pid) / r["file"], dest)
        out.append((f"img/{dest.name}", r["from"]))
    return out


def cmd_photo(a):
    rows = load_prospects()
    p, err = find_prospect(rows, a.who)
    if not p:
        print(err)
        return 1
    if a.action == "add":
        try:
            data = Path(a.file).read_bytes()
        except (OSError, TypeError) as exc:
            print(f"cannot read {a.file}: {exc}")
            return 1
        name, err = add_photo(p["id"], data, a.origin)
        print(f"refused: {err}" if err else f"saved {name} for {p['name']} (from their {a.origin})")
        return 1 if err else 0
    if a.action == "remove":
        ok = remove_photo(p["id"], a.file or "")
        print(f"removed {a.file}" if ok else f"{p['name']} has no photo {a.file}")
        return 0 if ok else 1
    for r in supplied(p["id"]):
        print(f"  {r['file']}  from their {r['from']}  ({r['added']})")
    return 0


PROSPECT_POOL = 5
PROSPECT_FIELDS = ("name", "category", "kind", "address", "phone", "hours", "current_site",
                   "problems", "channel", "contact", "sources", "why", "links")


def load_prospects():
    d = read_json(A("state", "prospects.json"), {}) or {}
    return d.get("prospects") if isinstance(d.get("prospects"), list) else []


def save_prospects(rows):
    write_json(A("state", "prospects.json"), {"prospects": rows})


def cycle_plan():
    """(kind, detail): what the next cycle is for. detail is the approved
    prospect for "build", how many to find for "scout", why for "hold"."""
    work = A("work")
    if work.is_dir() and any(work.iterdir()):
        return "resume", None
    rows = load_prospects()
    approved = sorted((p for p in rows if p.get("status") == "approved"),
                      key=lambda p: str(p.get("decided") or ""))
    if approved:
        return "build", approved[0]
    waiting = [p for p in rows if p.get("status") == "waiting"]
    if len(waiting) < PROSPECT_POOL:
        return "scout", PROSPECT_POOL - len(waiting)
    return "hold", (f"{len(waiting)} prospects are waiting for your call - approve or reject "
                    f"them in Paul's house")


def find_prospect(rows, key):
    """(prospect, error): by id, or by a part of the name that fits one."""
    key = str(key or "").strip()
    if key.isdigit():
        hit = [p for p in rows if str(p.get("id")) == key]
    else:
        n = norm_name(key)
        hit = [p for p in rows if n and n in norm_name(p.get("name"))]
    if not hit:
        return None, f'no prospect matches "{key}" - `paul.py prospects` lists them'
    if len(hit) > 1:
        return None, f'"{key}" fits more than one: ' + "; ".join(
            f"{p['id']} {p['name']}" for p in hit)
    return hit[0], ""


def prospect_problems(p, rows, get=http_get):
    """Why this prospect cannot go on the list, [] when it can. The build's
    own rules (validate_target) are the prospect's too - one set, so a
    prospect that passes here is one a build can start from."""
    if not isinstance(p, dict):
        return ["not a JSON object"]
    errs = [e for e in validate_target({**p, "status": "built", "layout": "-"})
            if '"layout"' not in e]
    if not str(p.get("why") or "").strip():
        errs.append('"why" is empty - one line on why this business, for the owner')
    if errs:
        return errs
    if seen_row(p["name"], p.get("phone", "")):
        return ["already contacted or excluded (contacted.csv)"]
    n, ph = norm_name(p["name"]), norm_phone(p.get("phone"))
    for q in rows:
        if norm_name(q.get("name")) == n or (ph and len(ph) >= 7 and norm_phone(q.get("phone")) == ph):
            return [f"already a prospect ({q.get('status')}, {q.get('found')})"]
    rows_v, _page = verify_problems(p, get)
    bad = [d for code, verdict, d in rows_v if verdict == "contradicted"]
    if bad:
        return [f"its website says otherwise: {bad[0]}"]
    if p.get("channel") == "email" and "@" in str(p.get("contact")):
        verdict, detail = mail_verdict(p["contact"], get)
        if verdict == "none":
            return [f"the email would bounce ({detail}) - reach them another way"]
    return []


def prospects_file_problems(get=http_get):
    """(good, dropped, error) for work/prospects.json, read the same way by
    Paul's check and by finish."""
    d = read_json(A("work", "prospects.json"))
    if d is None:
        return [], [], ("work/prospects.json is missing or not JSON - write "
                        '{"prospects": [...]} (or {"prospects": [], "why": "..."})')
    if not isinstance(d, dict) or not isinstance(d.get("prospects"), list):
        return [], [], 'work/prospects.json must be {"prospects": [...]}'
    if not d["prospects"] and not str(d.get("why") or "").strip():
        return [], [], 'no prospects and no "why" - say what you looked at'
    rows, good, dropped = load_prospects(), [], []
    for p in d["prospects"]:
        errs = prospect_problems(p, rows + good, get)
        if errs:
            dropped.append(((p or {}).get("name") if isinstance(p, dict) else "?", errs))
        else:
            good.append({k: p.get(k) for k in PROSPECT_FIELDS if p.get(k) is not None})
    return good, dropped, None


def finish_scout(cyc, get=http_get):
    started = float(cyc.get("started") or 0)
    ok_line, why = remember.written_this_cycle(A("state", "last-run.txt"), started)
    good, dropped, err = prospects_file_problems(get)
    day = today()
    if err:
        record(f"prospects not filed - {err}"[:300])
        print(f"PAUL'S PROSPECTS: {err}")
        return 1
    rows = load_prospects()
    key = places_key()
    for p in list(good):
        check_on_google(p, key)
        if CLOSED_FOR_GOOD in (p.get("google") or {}).get("flags", []):
            good.remove(p)
            dropped.append((p["name"], [CLOSED_FOR_GOOD]))
    nxt = max([int(p.get("id") or 0) for p in rows] + [0]) + 1
    for i, p in enumerate(good):
        p.update(id=nxt + i, found=day, status="waiting")
    save_prospects(rows + good)
    lines = [f"# Paul - {day} - prospects", "",
             f"{len(good)} added for your call, {len(dropped)} dropped by code.", ""]
    lines += [f"- {p['id']}. {p['name']} ({p.get('category')}) - {p.get('why')}" for p in good]
    lines += [f"- dropped {n}: {'; '.join(e[:2])}" for n, e in dropped]
    why_none = (read_json(A("work", "prospects.json"), {}) or {}).get("why")
    if not good and why_none:
        lines += ["", f"Why none: {why_none}"]
    _report(f"{day}-prospects.md", lines)
    names = ", ".join(p["name"] for p in good) or "none"
    record((f"found {len(good)} prospect(s): {names}" + (f" ({len(dropped)} dropped)" if dropped else ""))[:300])
    shutil.rmtree(A("work"), ignore_errors=True)
    print(f"prospects: {len(good)} added, {len(dropped)} dropped")
    for n, e in dropped:
        print(f"  dropped {n}: {'; '.join(e[:2])}")
    if not ok_line:
        print(f"PAUL RECORDED NOTHING ({why})")
        return 1
    return 0


def close_prospect(pid, b):
    """A build cycle's outcome, onto the prospect it built."""
    rows = load_prospects()
    for p in rows:
        if str(p.get("id")) == str(pid):
            if b.get("outcome") == "built":
                p.update(status="built", slug=b.get("slug"), built=b.get("day"), mode=b.get("mode"))
            elif p.get("status") == "built":
                # A revision that did not ship: the site that is up stays up.
                continue
            else:
                p.update(status="not built", built=b.get("day"),
                         note=str(b.get("why") or b.get("outcome") or "")[:300])
    save_prospects(rows)


def start_cycle(run=subprocess.run):
    """Start Paul's cycle now, the way the timer does. The Deck's Approve +
    build and Find buttons, and the GM's run_scout, all start a unit."""
    try:
        r = run(["systemctl", "start", "--no-block", "paul-cycle.service"],
                capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def cmd_prospects(a):
    rows = load_prospects()
    if not rows:
        print("no prospects yet - the next cycle finds some")
        return 0
    for p in rows:
        print(f"{p['id']:>3}  {p['status']:<10} {p['name']} ({p.get('category')}) - {p.get('why')}")
    kind, detail = cycle_plan()
    print(f"\nnext cycle: {kind}" + (f" {detail['name']}" if kind == "build" else
                                     f" (find {detail})" if kind == "scout" else
                                     f" - {detail}" if kind == "hold" else ""))
    return 0


def cmd_approve(a, run=subprocess.run):
    rows = load_prospects()
    p, err = find_prospect(rows, a.who)
    if not p:
        print(err)
        return 1
    if p.get("status") not in ("waiting", "rejected", "not built"):
        print(f"{p['name']} is already {p['status']}")
        return 1
    # The Studio's reference box (owner, 2026-10-20): "I just want him to
    # reference that site when I press the approve + build button".
    given = getattr(a, "ref", None)
    ref = clean_ref(given) if given else None
    if given and not ref:
        print(f'"{given}" is not a web address - give the whole https://... link')
        return 1
    again = p.get("status") == "not built"
    p.update(status="approved", decided=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if ref:
        p["reference"] = ref
    save_prospects(rows)
    record(f"approved by the owner: {p['name']}" + (f", after {ref}" if ref else ""))
    print(f"approved: {p['name']} - the next cycle builds it" + (f", after {ref}" if ref else ""))
    if again:
        back = restore_set_aside(p)
        if back:
            print(f"its last build is back in work/ ({back}) - Paul finishes it rather than starting over")
    if a.build:
        # Not live yet: the build is a dry one - built here, nothing published.
        live = bool(read_json(A("state", "live.json")))
        if not live:
            mark_dry_next()
        ok, detail = start_cycle(run)
        print(("started Paul's cycle now" + ("" if live else " (a dry run - you have not gone live)"))
              if ok else f"could not start the cycle ({detail}) - it runs at its usual time")
    return 0


def clean_ref(u):
    """The owner's reference as a URL Paul can open, or None. Tracking
    parameters dropped: ?ref=land-book.com is where the owner found it, not
    part of the site."""
    u = str(u or "").strip()
    if not re.match(r"https?://[^\s/]+\.[^\s/]+", u):
        return None
    parts = urllib.parse.urlsplit(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
         if k.lower() not in ("ref", "fbclid", "gclid") and not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q), fragment=""))


def cmd_build(a, run=subprocess.run):
    """Owner, 2026-10-20: "I want Paul to build Royal Edges barbershop website
    using this site as a reference". A business the owner names - one Paul
    already found, or a new one - approved in one step, with the site its
    build should take after. Paul still reads every fact from a public
    source; naming it is not vouching for an address or a phone number."""
    ref = None
    if a.ref:
        ref = clean_ref(a.ref)
        if not ref:
            print(f'"{a.ref}" is not a web address - give the whole https://... link')
            return 1
    rows = load_prospects()
    # Spaces ignored: "Royal Edges Barbershop" is the "Royal Edges Barber Shop"
    # Paul found, and a second prospect for one business is a second pitch.
    n = norm_name(a.name).replace(" ", "")
    hit = [p for p in rows if n and (n in norm_name(p.get("name")).replace(" ", "")
                                     or norm_name(p.get("name")).replace(" ", "") in n)]
    if len(hit) > 1:
        print(f'"{a.name}" fits more than one: ' + "; ".join(f"{p['id']} {p['name']}" for p in hit))
        return 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if hit:
        p = hit[0]
        if p.get("status") == "built":
            print(f"{p['name']} is already built - send it back with: "
                  f'python3 scripts/paul.py revise "{p["name"]}" "what to change"')
            return 1
        p.update(status="approved", decided=now)
        said = f"approved: {p['name']} (prospect {p['id']})"
    else:
        if seen_row(a.name):
            print(f"{a.name} is in contacted.csv (contacted or excluded) - Paul will not build it")
            return 1
        nxt = max([int(q.get("id") or 0) for q in rows] + [0]) + 1
        p = {"id": nxt, "name": a.name.strip(), "found": today(), "status": "approved",
             "decided": now, "source": "owner",
             "why": "named by the owner" + (f", in {a.where}" if a.where else "")}
        if a.where:
            p["where"] = a.where.strip()
        rows.append(p)
        said = f"added and approved: {p['name']} (prospect {p['id']})"
    if ref:
        p["reference"] = ref
    notes = " ".join(str(getattr(a, "notes", None) or "").split())
    if notes:
        p["owner_notes"] = notes
    save_prospects(rows)
    record(f"the owner asked for {p['name']}" + (f", after {ref}" if ref else ""))
    print(said + (f"\nreference: {ref}" if ref else ""))
    kind, detail = cycle_plan()
    if kind == "build" and str(detail.get("id")) != str(p["id"]):
        print(f"{detail['name']} was approved first and is built before it")
    elif kind == "resume":
        print("Paul finishes what is in work/ first, then builds this")
    if a.now:
        live = bool(read_json(A("state", "live.json")))
        if not live:
            mark_dry_next()
        ok, detail = start_cycle(run)
        print(("started Paul's cycle now" + ("" if live else " (a dry run - you have not gone live)"))
              if ok else f"could not start the cycle ({detail}) - it runs at its usual time")
    return 0


def cmd_restore(a, run=subprocess.run):
    """Build a business again after Delete took every site of it (owner,
    2026-10-22: Delete on an older Royal Edge card removed all four, and
    their previews; "just rebuild it"). Delete kept it in contacted.csv so
    Paul would never pick it, and its saved photos stayed in state/photos/
    under its old prospect number. Its row goes - only while the pitch was
    never sent - the prospect comes back under that number, so the photos
    are its again, and it is built as `paul.py build` builds a named one."""
    name = " ".join(str(a.name or "").split())
    if any(norm_name(b.get("name")) == norm_name(name) and b.get("slug")
           and (A("sites", b["slug"]) / "index.html").is_file() for b in builds()):
        print(f"{name} still has a site - send it back to Paul instead")
        return 1
    rows = contacted()
    mine = [r for r in rows if norm_name(r["business"]) == norm_name(name)]
    if any(r.get("status") not in ("", "drafted") for r in mine):
        print(f"{name} is marked {mine[0]['status']} in contacted.csv - a pitch went out, or it "
              f"is not to be contacted, so Paul will not build it again")
        return 1
    pros = load_prospects()
    if any(norm_name(p.get("name")) == norm_name(name) for p in pros):
        print(f"{name} is already a prospect - approve it in the Studio")
        return 1
    taken = {str(p.get("id")) for p in pros}
    if a.photos is not None:
        pid = a.photos
    else:
        orphans = sorted(int(d.name) for d in A("state", "photos").glob("*")
                         if d.is_dir() and d.name.isdigit() and d.name not in taken) \
            if A("state", "photos").is_dir() else []
        if len(orphans) > 1:
            print(f"photos of {len(orphans)} businesses no longer listed ({', '.join(map(str, orphans))}) - "
                  f"say which are {name}'s with --photos <number>")
            return 1
        pid = orphans[0] if orphans else None
    if pid is not None and str(pid) in taken:
        print(f"photo folder {pid} belongs to another prospect")
        return 1
    if pid is None:
        pid = max([int(p.get("id") or 0) for p in pros] + [0]) + 1
    if mine:
        write_rows(A("state", "contacted.csv"), CONTACTED_FIELDS, [r for r in rows if r not in mine])
    p = {"id": pid, "name": name, "found": today(), "status": "waiting", "source": "owner",
         "why": "built again after its sites were deleted by mistake"}
    if a.where:
        p["where"] = a.where.strip()
    save_prospects(pros + [p])
    n = len(supplied(pid))
    print(f"{name} is back as prospect {pid}" + (f", with the {n} photo(s) you saved for it" if n else
                                                  " (no saved photos found)"))
    return cmd_build(a, run=run)


def restore_set_aside(p):
    """Try again on a build that was not shipped (owner, 2026-10-21: Royal
    Edge, failed twice by a phone check that was itself wrong). Its last
    build - set aside in sites/_below-bar/ by finish - goes back into work/
    with REVISE.md saying why it stopped, so the next cycle finishes that
    build instead of paying for a new one. Returns where it came from, or
    None when there is nothing to bring back or work/ is busy."""
    work = A("work")
    if work.is_dir() and any(work.iterdir()):
        return None
    kept = [d for d in A("sites", "_below-bar").glob("*")
            if (read_json(d / "prospect.json", {}) or {}).get("id") == p.get("id")
            and (d / "site" / "index.html").is_file()]
    if not kept:
        return None
    last = max(kept, key=lambda d: d.stat().st_mtime)
    if work.is_dir():
        work.rmdir()
    shutil.move(str(last), str(work))
    write_json(work / "prospect.json", p)
    (work / "REVISE.md").write_text(
        f"# Try again - {today()}\n\n"
        f"This is your last build of {p.get('name')}. It was not shipped because: "
        f"{p.get('note') or 'its check failed'}\n\n"
        f"Run check first - the reason may already be gone (a check can be corrected). Fix only "
        f"what check still reports, keep everything else, then finish as usual. Do not start over "
        f"and do not pick a new business.\n")
    return last.relative_to(A()).as_posix()


def cmd_reject(a):
    rows = load_prospects()
    p, err = find_prospect(rows, a.who)
    if not p:
        print(err)
        return 1
    if p.get("status") not in ("waiting", "approved", "not built"):
        print(f"{p['name']} is already {p['status']}")
        return 1
    p.update(status="rejected", decided=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             note=" ".join(a.why or [])[:300] or None)
    save_prospects(rows)
    record(f"rejected by the owner: {p['name']}")
    print(f"rejected: {p['name']} - never proposed again")
    return 0


def cmd_find(a, run=subprocess.run):
    kind, detail = cycle_plan()
    if kind == "hold":
        print(detail)
        return 1
    ok, err = start_cycle(run)
    print(f"started Paul's cycle now: {kind}" if ok else f"could not start the cycle ({err})")
    return 0 if ok else 1


def on_the_plan():
    """True when Paul wakes on the owner's Claude plan (tasks/engines.json,
    whose one reader is wake.py). His runs then spend nothing on OpenRouter,
    so its allowance is not a reason to hold him - it held every cycle of his
    with money he was not going to spend. Should the plan refuse a wake,
    wake.py sends it through OpenRouter, where his own key's daily cap is
    still the stop."""
    w = _load("wake_paul", "wake.py")
    try:
        return w.setting("paul", w.engines(ROOT / "tasks" / "engines.json"))["engine"] == "claude"
    except (SystemExit, ValueError):
        return False


def hold_reason(dry):
    """Why the model should not be woken now, or None. Decided by code
    because a woken model that has nothing to do still costs money to say so."""
    _, missing = load_sender()
    if missing:
        return (f"sender details not set ({', '.join(missing)}) - "
                f"python3 scripts/paul.py sender")
    if not A("AGENTS.md").is_file():
        return "agents/paul/AGENTS.md is missing - scripts/deploy.sh paul"
    kind, detail = cycle_plan()
    if kind == "hold":
        return detail
    # Scouting publishes nothing and contacts nobody, so it needs no go-live.
    if not dry and kind != "scout":
        if not read_json(A("state", "live.json")):
            return "live cycles start after you have seen a dry cycle - python3 scripts/paul.py go-live"
        if not vercel_token():
            return "no VERCEL_TOKEN - python3 scripts/set-credential.py paul VERCEL_TOKEN"
        if not find_browser():
            return "no headless browser for the screenshots - install Chrome (README)"
        waiting = [r for r in contacted() if r["status"] == "drafted"]
        if len(waiting) >= MAX_WAITING:
            return (f"{len(waiting)} pitches are waiting for you to send - mark the ones you "
                    f"have sent: python3 scripts/paul.py mark <slug> sent")
    if on_the_plan():
        return None
    left, need = budget_left(), min_left()
    if left is not None and left < need:
        return (f"today's AI allowance has ${max(left, 0):.2f} left; a Paul cycle needs "
                f"${need:.2f} to start")
    return None


def cmd_should_run(a):
    why = hold_reason(a.dry)
    if not why:
        print("run")
        return 0
    print(why)
    if a.record:
        stamp = et_time.eastern_now().strftime("%Y-%m-%d %H:%M ET")
        A("state").mkdir(parents=True, exist_ok=True)
        A("state", "last-run.txt").write_text(f"{stamp} | held by code, model not woken - {why}\n")
        # A hold repeats every day until it is fixed. One line says so; thirty
        # identical ones would push every real cycle out of MEMORY.md.
        if not last_memory_line().endswith(f"held - {why}"):
            record(f"held - {why}")
    return 1


def references(get=http_get):
    """[(what, url, up)] - asked every cycle, because two were down the day
    this was written and a model sent to study a 404 learns nothing."""
    out = []
    for what, url in REFERENCES:
        r = get(url, timeout=10, limit=2000)
        out.append((what, url, bool(r["ok"] and r["status"] == 200)))
    return out


REF_RUN = 8          # words in a row shared with the reference = copied, not inspired


def own_reference(work=None):
    """The site the owner asked this build to take after, or None."""
    return (read_json(Path(work or A("work")) / "prospect.json", {}) or {}).get("reference")


def reference_problems(site_text, t, ref, get=http_get):
    """(errors, notes) for a site built after the owner's reference. The
    reference is a REAL business (rudysbarbershop.com, 2026-10-20), so its
    name and its sentences may not travel: take the layout and the feel,
    write the words. Its own pages say which words are its; nothing about
    it is restated here."""
    r = get(ref, timeout=15)
    if not (r["ok"] and r["status"] == 200):
        return [], [f"the reference {ref} did not load ({r['error'] or r['status']}) - "
                    f"not checked for copying"]
    pr = _Page()
    try:
        pr.feed(r["body"])
    except Exception:
        return [], [f"the reference {ref} did not parse - not checked for copying"]
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r["body"])
    title = html.unescape(m.group(1)).strip() if m else ""
    theirs = re.split(r"\s[|\u2013\u2014-]\s|:\s", title)[0].strip()
    ours, own_name = norm_name(site_text), set(norm_name(t.get("name")).split())
    errs = []
    host = urllib.parse.urlsplit(ref).hostname or ""
    stem = re.sub(r"^www\.", "", host).split(".")[0]
    # Their whole name, their web address run together, or a possessive in it
    # ("Rudy's") - an ordinary word that happens to be in a name is not enough.
    whole = norm_name(theirs)
    owners = [norm_name(w) for w in re.findall(r"\b([A-Z]\w+['\u2019]s)\b", theirs)]
    found = ((whole and len(whole) >= 6 and whole not in norm_name(t.get("name"))
              and re.search(rf"\b{re.escape(whole)}\b", ours))
             or (len(stem) >= 6 and stem not in norm_name(t.get("name")).replace(" ", "")
                 and stem in ours.replace(" ", ""))
             or any(w not in own_name and re.search(rf"\b{re.escape(w)}\b", ours) for w in owners))
    if found:
        errs.append(f'"{theirs or host}" is the reference\'s name, and it is on your site - '
                    f"that is another business; use none of its name")
    words = norm_name(" ".join(pr.text)).split()
    runs = {" ".join(words[i:i + REF_RUN]) for i in range(len(words) - REF_RUN + 1)}
    mine = ours.split()
    shared = []
    for i in range(len(mine) - REF_RUN + 1):
        run = " ".join(mine[i:i + REF_RUN])
        if run in runs and not any(run in x for x in shared):
            shared.append(" ".join(mine[i:i + REF_RUN + 4]))
    if shared:
        errs.append(f'"{shared[0]}..." is the reference\'s own wording - take its layout and feel, '
                    f"write your own words for this business")
    return errs, []


def cmd_brief(a, get=http_get):
    s, _ = load_sender()
    work = A("work")
    resume = "none"
    if work.is_dir() and any(work.iterdir()):
        newest = max(p.stat().st_mtime for p in work.rglob("*")) if any(work.rglob("*")) else 0
        old = (time.time() - newest) / 86400
        t = read_json(work / "target.json", {}) or read_json(work / "prospect.json", {}) or {}
        if (work / "REVISE.md").is_file():
            if revising():
                set_revision(revising(), state="building")
            resume = (f'work/ holds "{t.get("name") or "a business"}", sent back by the owner '
                      f"to fix - read work/REVISE.md first and fix every point in it. Same "
                      f"business: do not look for a new one")
        elif old <= RESUME_DAYS:
            resume = (f'work/ holds an unfinished build for "{t.get("name") or "a business"}" - '
                      f"finish it rather than starting over")
        else:
            dest = A("sites", "_abandoned", f"{today()}-{int(time.time())}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work), str(dest))
            resume = f"none (a build older than {RESUME_DAYS} days was moved to sites/_abandoned/)"
    kind, detail = cycle_plan()
    if kind == "build":
        A("work").mkdir(parents=True, exist_ok=True)
        write_json(A("work", "prospect.json"), detail)
    A("work").mkdir(parents=True, exist_ok=True)
    # Kept to the millisecond. Whole seconds let a line written in the same
    # second just BEFORE the cycle began pass as this cycle's - a dry run
    # started right after a hold was signed off on the hold's line.
    # A build of an approved prospect that was interrupted resumes as one:
    # its id rides along in work/prospect.json, so finish still closes it.
    pid = (detail.get("id") if kind == "build" else
           (read_json(A("work", "prospect.json"), {}) or {}).get("id"))
    save_cycle({"started": round(time.time(), 3), "mode": "dry" if a.dry else "live",
                "day": today(), "passes": 0, "resume": resume != "none", "kind": kind,
                "prospect": pid})

    yours = place_supplied(pid, A("work", "site")) if str(pid or "").isdigit() else []

    rows = contacted()
    recent = [f"{r['business']} | {norm_phone(r['phone']) or '-'}" for r in rows[-15:]]
    if kind == "scout":
        # Every prospect ever listed, whatever the owner said: a rejected one
        # is never proposed again, and a waiting one not twice.
        recent += [f"{p['name']} | {norm_phone(p.get('phone')) or '-'} (prospect, {p.get('status')})"
                   for p in load_prospects()[-20:]]
    lays = [f"{b.get('day')} {b.get('kind')}: {b.get('layout')}" for b in builds()
            if b.get("outcome") == "built"][-3:]
    refs = references(get)
    up = [(w, u) for w, u, ok in refs if ok]
    down = [u for _w, u, ok in refs if not ok]

    L = ["FACTS, COUNTED BY CODE - use them as given"]
    if a.dry:
        L += ["mode         DRY: code checks your files and builds locally only. Nothing is",
              "             deployed and nothing is recorded as contacted. Do the same work."]
    else:
        L += ["mode         LIVE: after you finish, code checks your files, deploys a private",
              "             preview, puts the link in your draft and files the records."]
    L += [f"today        {today()} (Eastern)",
          f"area         {' · '.join(towns()) or s.get('area', '?')}",
          f"studio       {s.get('studio', '?')} - code writes the footer and the signature",
          f"offer        ${s.get('price', '?')}, one time. It covers {OFFER_COVERS}.",
          f"yearly       the one cost that stays: their web address, about ${DOMAIN_FEE} a year,",
          "             paid to the registrar - say so. Hosting is free.",
          "changes      say they can change the photos and colours, and name one feature",
          "             they could add later that fits them:",
          *[f"             {k:<7}{v[0]}" for k, v in FEATURE_IDEAS.items()],
          "             The pitch states that exact price and what it covers, in your words.",
          f"resume       {resume}",
          *([f"cycle        SCOUT: find {detail} new prospect(s) for the owner to choose from",
             "             and write work/prospects.json. Build nothing this cycle."] if kind == "scout" else
            [f"cycle        BUILD: the owner approved {detail['name']}. Start from",
             "             work/prospect.json - skip finding a target - and build it."] if kind == "build" else
            ["cycle        RESUME: finish what is in work/ (see resume)"]),
          *([f"FOCUS        {KIND_WORDS[focus_kind()]} only (kind \"{focus_kind()}\"). Code refuses",
             "             any other kind; none good enough is a valid no-target day."]
            if focus_kind() else []),
          *([f"photos       {len(yours)} of THEIR OWN photos, saved by the owner from their "
             f"{', '.join(sorted({o for _r, o in yours}))}:",
             *[f"             work/site/{r}" for r, _o in yours],
             "             Use these before anything else - the hero first. Open each with",
             "             view_image. List each in photos with kind \"business\" and source",
             "             \"saved by the owner from their <where>\". Stock only for gaps."] if yours else []),
          f"passes       {MAX_PASSES} - each `paul.py check` that finds problems uses one",
          f"skip         {len(rows)} businesses already contacted or excluded. Check a candidate:",
          '             python3 ../../scripts/paul.py seen "<name>" "<phone>"']
    for r in recent[::-1]:
        L.append(f"             {r}")
    if kind == "scout":
        key = places_key()
        L += lead_lines(find_leads(key, say=lambda m: print(m, file=sys.stderr)) if key else [])
    mine = (read_json(A("work", "prospect.json"), {}) or {}) if kind != "scout" else {}
    if mine.get("source") == "owner" and not A("work", "REVISE.md").is_file():
        L += [f"named        the owner named {mine.get('name')}"
              + (f" ({mine['where']})" if mine.get("where") else "") + " - nothing about it is",
              "             checked yet. Find it yourself as in scouting: its address, phone",
              "             and hours from its own pages, Google or Facebook, each with the URL.",
              "             Not sure it is the right business, or it closed? below-bar, say why."]
    themes = A("work", "themes.json")
    if kind in ("build", "resume") and not A("work", "REVISE.md").is_file() and not mine.get("reference"):
        dealt = (read_json(themes, {}) or {}).get("themes")
        if not dealt:
            dealt = deal_themes(mine.get("kind") or "other")
            write_json(themes, {"themes": dealt, "kind": mine.get("kind")})
        L += [f"THEMES       no reference from the owner: build in one of {', '.join(dealt)}.",
              "             Read hallmark/themes/<name>.md and look at hallmark/themes/<name>.jpg,",
              "             pick the one that fits them, and set target.json \"theme\" to it."]
    if mine.get("owner_notes"):
        words, line = str(mine["owner_notes"]).split(), ""
        rows_ = []
        for w in words:
            if len(line) + len(w) > 62:
                rows_.append(line)
                line = ""
            line += (" " if line else "") + w
        rows_.append(line)
        L += ["OWNER WANTS  " + rows_[0], *[f"             {r}" for r in rows_[1:]],
              "             Build every point of it. Code's checks still apply."]
    if mine.get("reference"):
        L += [f"OWNER'S REF  {mine['reference']}",
              "             The owner picked this site for this build. Open it first (WebFetch)",
              "             and take after it: its layout, section order, type and feel. It is",
              "             a REAL business - never its name, words, photos, prices or claims.",
              "             Code refuses its name and any 8 words in a row from its pages."]
    if lays:
        L.append("last builds  " + lays[-1])
        L += [f"             {x}" for x in lays[-2::-1]]
        L.append("             vary the layout - never the same one twice in a row")
    else:
        L.append("last builds  none yet")
    L.append("references   open the closest one before you build (they are live today);")
    L.append("             the ones marked optional are the owner's samples - use them or not:")
    for w, u in up:
        L.append(f"             {w:<26} {u}")
    if not up:
        L.append("             none answered today - use the build rules alone")
    if down:
        L.append(f"             down today, skip: {', '.join(down)}")
    print("\n".join(L))
    return 0


# --------------------------------------------------------------------------
# commands: Paul
# --------------------------------------------------------------------------

def cmd_seen(a):
    r = seen_row(a.name, a.phone or "")
    if not r:
        print(f"NEW - {a.name} has never been contacted. Go ahead.")
        return 0
    print(f"SEEN - {r['business']} ({r['date']}, {r['status']}). Pick another business.")
    return 1


def check_scout(cyc, get=http_get):
    good, dropped, err = prospects_file_problems(get)
    ok_line, why = remember.written_this_cycle(A("state", "last-run.txt"), float(cyc.get("started") or 0))
    if err:
        print(f"PROSPECTS: {err}")
        return 1
    print(f"PROSPECTS: {len(good)} would be filed, {len(dropped)} dropped by code")
    for p in good:
        print(f"  + {p['name']} ({p.get('category')}) - {p.get('why')}")
    for n, e in dropped:
        print(f"  - {n}: {'; '.join(e[:3])}")
    if dropped:
        print("Fix or replace the dropped ones if you can, then check again; whatever passes is filed.")
    if not ok_line:
        print(f"MEMORY: MISSING - {why}. Write your one line to state/last-run.txt, then check again.")
        return 1
    print(f"MEMORY: {A('state', 'last-run.txt').read_text().strip()[:160]}")
    print("\nAll deliverables present. Paste the lines above as your sign-off.")
    return 0


# THE COLD REVIEW (Hallmark's finish review, owner 2026-10-26: "4 is fine
# do it"). The thread that built a page is its weakest reviewer: once per
# build, when every check above passes, a fresh model that did not build it
# reads the screenshots and the page, on the plan through wake.py ask, with
# Read and nothing else. One round: Paul fixes what is real, and the next
# check does not ask again. Code still judges everything it can measure;
# this asks what it cannot.
REVIEW = "review.json"
REVIEW_ASK = """You are reviewing a local business's website that someone else built. You did not build it
and will not be told why anything is the way it is: judge the page, not the intent.

Read these files in this folder: mobile.png (a 390px phone), desktop.png (a 1440px computer), then
index.html only to quote its words. Stop reading by your sixth turn and write.

The business: {name}, {kind}{where}. Its theme: {theme}. {notes}
Code has already measured contrast, photo sharpness, sizes, wrapping, the first screen and the
banned patterns: those are not findings. Neither is a style you would not have chosen.

Ask:
- Would the headline, or any section, pass unchanged on a competitor's site? Name it.
- Does anything read as a template or as machine-made: a screen with nothing worth looking at, a
  narrow column beside an empty half, a decoration with no job, the same section shape repeated?
- Do the photos carry this business - is any caption wrong for what it shows, any stock photo
  that would not pass for a shop like theirs in their town?
- Does the page lead a visitor to what they came to do - book, visit, order, call - and end on it?
- Did it do what the owner asked, if the owner asked something?

First line, one word: ship (nothing owed) or fix (findings follow). Then at most six findings,
worst first, one line each: what, where, what you would do. Do not fix anything."""


def review_lines(review):
    """The cold review, for the build's report."""
    if not review:
        return ["", "## Fresh review", "", "- not run"]
    return ["", "## Fresh review", "", f"- {review.get('first') or 'not run'}"] + \
        [f"  {ln}" for ln in str(review.get("review") or "").splitlines()[1:] if ln.strip()]


def review_needed(work):
    return not (Path(work) / REVIEW).is_file()


def cold_review(t, work, shots, ask=None):
    """(first word, findings text) of the one review, or (None, why) when it
    could not be asked - which never blocks a build."""
    files = [p for _n, p in shots if p] + [Path(work) / "site" / "index.html"]
    if len(files) < 3:
        return None, "no screenshots to review"
    asked = (read_json(Path(work) / "prospect.json", {}) or {}).get("owner_notes")
    revise = Path(work) / "REVISE.md"
    if revise.is_file():
        m = re.search(r"## What the owner wants fixed\n\n(.*?)\n\n", revise.read_text(errors="replace"), re.S)
        asked = m.group(1).strip() if m else asked
    q = REVIEW_ASK.format(name=t.get("name"), kind=t.get("category") or t.get("kind") or "",
                          where=f" in {t['address']}" if t.get("address") else "",
                          theme=t.get("theme") or "its own", notes=f"The owner asked: {asked}" if asked else "")
    ask = ask or _load("wake_paul", "wake.py").ask
    ok, text = ask("paul", q, timeout=300, tools=("Read",), files=[str(f) for f in files])
    if not ok:
        return None, text
    text = str(text or "").strip()
    first = (re.match(r"\W*(\w+)", text) or [None, ""])[1].lower()
    return (first if first in ("ship", "fix") else "fix"), text


def cmd_check(a, get=http_get):
    cyc = load_cycle()
    if cyc.get("kind") == "scout":
        return check_scout(cyc, get)
    started = float(cyc.get("started") or 0)
    tp = A("work", "target.json")
    t = read_json(tp)
    if t is None:
        if tp.is_file():
            print("TARGET: BROKEN - work/target.json does not parse as JSON. Rewrite it whole.")
        else:
            print("TARGET: MISSING - write work/target.json (see AGENTS.md). If you found no "
                  'business worth building for, write {"status": "no-target", "why": "..."}.')
        return 1
    if not isinstance(t, dict):
        print("TARGET: BROKEN - work/target.json must be one JSON object {...}")
        return 1

    # The screenshots are taken while the other checks run, not after them:
    # each is a Chrome launch, seconds apiece on the droplet's one CPU.
    built = t.get("status") == "built"
    index = A("work", "site", "index.html")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        later = pool.submit(fresh_shots, index, A("work", "shots")) if built and index.is_file() else None
        errs, notes, info = run_checks(t, get)
        shots = later.result() if later else []
    if built and not errs and t.get("name") and already_pitched(t):
        errs.append(f"{t['name']} is already in contacted.csv - pick another business")

    ok_line, why = remember.written_this_cycle(A("state", "last-run.txt"), started)

    if errs:
        cyc["passes"] = int(cyc.get("passes") or 0) + 1
        save_cycle(cyc)
        left = MAX_PASSES - cyc["passes"]
        print(f"CHECK FAILED - pass {cyc['passes']} of {MAX_PASSES}")
        for e in errs:
            print(f"  - {e}")
        for n in notes:
            print(f"  . {n}")
        if left > 0:
            print("\nFix these and run check again.")
        else:
            print("\nOut of passes. Stop building: set \"status\": \"below-bar\" and a \"why\" in "
                  "work/target.json, write state/last-run.txt, and finish.")
        return 1

    if built and any(p for _n, p in shots) and review_needed(A("work")):
        first, said = cold_review(t, A("work"), shots, getattr(a, "ask", None))
        write_json(A("work", REVIEW), {"day": today(), "first": first, "review": said})
        if first == "fix":
            print("REVIEW - every check passed; one fresh reviewer, who did not build it, says:")
            print("\n".join("  " + ln for ln in said.splitlines()[1:] if ln.strip()) or "  (no findings given)")
            print("\nFix what is real, then run check again. This round uses no pass, and there is "
                  "no second one.")
            return 1
        print("REVIEW: ship - a fresh reviewer found nothing owed" if first else f"REVIEW: not run ({said})")
    print("CHECK PASSED")
    if built:
        print(f"TARGET: {t['name']} ({t.get('category')}) - "
              f"{len(t.get('problems') or [])} problem(s), every fact sourced")
        for n in notes:
            print(f"  . {n}")
        print(f"SITE: {info.get('pages')} page(s), {info.get('kb')} KB, "
              f"{info.get('photos')} photo(s), all local")
        print(f"DRAFT: {info.get('channel')} to {info.get('to')}, {info.get('words')} words")
        for name, p in shots:
            print(f"SHOT: {p.relative_to(A()) if p else name + ' NOT TAKEN - no browser here'}")
        if any(p for _n, p in shots):
            print("      read them to see the site the way a visitor will")
    else:
        print(f"TARGET: {t.get('status')} - {t.get('why')}")
    if not ok_line:
        print(f"MEMORY: MISSING - {why}. Write your one line to state/last-run.txt, then run "
              f"check again.")
        return 1
    print(f"MEMORY: {A('state', 'last-run.txt').read_text().strip()[:160]}")
    print("\nAll deliverables present. Paste the lines above as your sign-off.")
    return 0


# --------------------------------------------------------------------------
# finish: code files what Paul left
# --------------------------------------------------------------------------

def _set_aside(folder, why_slug):
    work = A("work")
    if not work.is_dir():
        return None
    dest = A("sites", folder, f"{today()}-{why_slug}-{int(time.time())}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(work), str(dest))
    return dest


def _report(name, lines):
    p = A("reports", name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines).rstrip() + "\n")
    return p


def cmd_finish(a, get=http_get, api=None, browser=None, sleep=time.sleep):
    cyc = load_cycle()
    if cyc.get("kind") == "scout":
        return finish_scout(cyc, get)
    before = len(builds())
    rc = finish_build(a, get, api, browser, sleep)
    done = builds()[before:]
    if cyc.get("prospect") is not None and done:
        close_prospect(cyc["prospect"], done[-1])
    return rc


def finish_build(a, get=http_get, api=None, browser=None, sleep=time.sleep):
    cyc = load_cycle()
    started = float(cyc.get("started") or 0)
    if not started:
        print("finish: no state/current-cycle.json - this runs after a cycle, from paul-cycle.sh")
        return 2
    dry = bool(a.dry or cyc.get("mode") == "dry")
    day = today()
    s, _ = load_sender()
    lr = A("state", "last-run.txt")
    ok_line, why = remember.written_this_cycle(lr, started)
    t = read_json(A("work", "target.json"))

    if not ok_line:
        if A("work").is_dir() and any(A("work").iterdir()):
            record("interrupted - work/ kept; the next cycle resumes it")
            print(f"PAUL RECORDED NOTHING ({why}). work/ is kept for the next cycle to resume.")
        else:
            record(f"recorded nothing - {why}")
            print(f"PAUL RECORDED NOTHING: {why}")
        return 1
    line = lr.read_text().strip().splitlines()[0][:300] if lr.read_text().strip() else ""

    if t is not None and not isinstance(t, dict):
        t = None
    if t is None and "no target" not in line.lower():
        # A run that says it built something and left no target.json has
        # nothing for code to check. Filing it as "no target" would report a
        # broken run as a quiet day. work/ stays for the next cycle to resume.
        record(f"wrote no valid work/target.json - {line}"[:300])
        print(f"PAUL WROTE NO VALID work/target.json (its line: {line})")
        return 1
    status = (t or {}).get("status")
    if t is None or status == "no-target":
        reason = (t or {}).get("why") or line
        _report(f"{day}-no-target.md", [f"# Paul - {day} - no target{' (dry run)' if dry else ''}",
                                         "", reason])
        log_build({"day": day, "mode": "dry" if dry else "live", "outcome": "no-target",
                   "why": str(reason)[:300]})
        record(f"no target - {reason}"[:300])
        if A("work").is_dir():
            shutil.rmtree(A("work"))
        print(f"no target this cycle: {reason}")
        return 0

    errs, notes, info = run_checks(t, get)
    if status == "built" and not errs and not dry and already_pitched(t):
        errs.append(f"{t['name']} is already in contacted.csv")
    was = revising()

    if status == "below-bar" or errs:
        short = make_slug(t.get("name") or "site", suffix="x").rsplit("-concept-", 1)[0]
        dest = _set_aside("_below-bar", short)
        reason = t.get("why") if status == "below-bar" else "; ".join(errs[:3])
        _report(f"{day}-{short}-below-bar.md",
                [f"# Paul - {day} - {t.get('name') or 'unnamed'}: not shipped", "",
                 f"Why: {reason}", "", f"The work is kept in {dest.relative_to(A()) if dest else '-'}."]
                + [f"- {e}" for e in errs])
        log_build({"day": day, "mode": "dry" if dry else "live", "outcome": "below-bar",
                   "why": str(reason)[:300],
                   "name": t.get("name")})
        record(f"below bar, not shipped: {t.get('name') or '?'} - {reason}"[:300])
        if was:
            set_revision(was, state="not shipped", why=str(reason)[:300])
        print(f"not shipped: {reason}")
        return 0 if status == "below-bar" else 1

    # ---- a site that passed every check ---------------------------------
    slug = make_slug(t["name"])
    site, box = A("sites", slug), A("outbox", slug)
    box.mkdir(parents=True, exist_ok=True)
    shutil.move(str(A("work", "site")), str(site))
    apply_safeguards(site, t["name"], s.get("studio", ""))
    head, body = parse_draft(A("work", "draft.md").read_text(errors="replace"))
    write_json(box / "target.json", t)
    (box / "draft.md").write_text(compose_draft(head, body, s, dry))
    shots = shoot((site / "index.html").as_uri(), box, browser)
    review = read_json(A("work", REVIEW), {}) or {}
    shutil.rmtree(A("work"), ignore_errors=True)

    rep = [f"# Paul - {day} - {t['name']}{' (DRY RUN)' if dry else ''}", ""]
    facts = [("name", t["name"]), ("category", t.get("category")), ("address", t.get("address")),
             ("phone", t.get("phone")), ("hours", t.get("hours")),
             ("current site", t.get("current_site")), ("contact", f"{t.get('channel')}: {t.get('contact')}")]
    src = t.get("sources") or {}
    srckey = {"current site": "current_site"}

    def tail(url, deployed):
        rep[2:2] = [f"**Preview:** {url}", f"**Comes down:** {deployed}",
                    f"**Pitch:** outbox/{slug}/draft.md ({head.get('channel')} to {head.get('to')})", ""]

    rep += ["## The business", "", "| | | source |", "|---|---|---|"]
    for k, v in facts:
        rep.append(f"| {k} | {v or '-'} | {src.get(srckey.get(k, k), '') or '-'} |")
    rep += ["", "## Why their web presence falls short", ""]
    for code, verdict, detail in info.get("problems", []):
        ev = next((p.get("evidence") for p in t.get("problems", []) if p.get("code") == code), "")
        rep.append(f"- **{code}** ({verdict} by code: {detail}) - {ev}")
    rep += ["", "## Photos", ""]
    for p in t.get("photos") or []:
        rep.append(f"- {p.get('file')} - {p.get('kind')} - {p.get('source')}")
    if not t.get("photos"):
        rep.append("- none")
    rep += ["", "## Checks", "", f"- static: passed ({info.get('pages')} page(s), {info.get('kb')} KB)"]
    for name, p in shots:
        rep.append(f"- {name} screenshot: {('outbox/' + slug + '/' + p.name) if p else 'NOT TAKEN - no browser on this machine'}")

    if dry:
        tail("not deployed - dry run", "-")
        rep += review_lines(review)
        rep += ["", "## Notes from Paul", "", str(t.get("notes") or "-"), "",
                "Dry run: built locally only. Nothing was deployed and the business was not "
                "recorded as contacted."]
        _report(f"{day}-{slug}.md", rep)
        log_build({"day": day, "mode": "dry", "outcome": "built", "slug": slug,
                   "name": t["name"], "kind": t.get("kind"), "layout": t.get("layout"), "theme": t.get("theme")})
        record(f"DRY: {t['name']} | built locally, sites/{slug} | {head.get('channel')} | not deployed")
        if was:
            set_revision(was, state="updated", new_slug=slug)
        print(f"dry cycle: {t['name']} built locally at agents/paul/sites/{slug}")
        return 0

    # ---- live: deploy, verify, record ------------------------------------
    token, team = vercel_token(), vercel_team() or None
    url, checks, failed = None, [], None
    try:
        url, _dep = vercel_deploy(site, slug, token, team, api=api, sleep=sleep)
        checks = live_checks(url, site, t["name"], get)
        if not all(ok for ok, _ in checks):
            sleep(20)                       # one retry: a new alias can lag
            checks = live_checks(url, site, t["name"], get)
        if not all(ok for ok, _ in checks):
            failed = next(line for ok, line in checks if not ok)
    except DeployError as exc:
        failed = str(exc)

    if failed:
        removed = vercel_remove(slug, token, team, api=api)[1] if token else "no token"
        dest = A("outbox", "_failed", slug)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(box), str(dest))
        append_row(A("state", "deployments.csv"), DEPLOY_FIELDS,
                   {"slug": slug, "preview_url": url or "", "business": t["name"],
                    "deployed": day, "status": "failed", "removed": day})
        rep += ["", "## Live verification FAILED", "", f"- {failed}", f"- teardown: {removed}"]
        rep += [f"- {'ok' if ok else 'FAIL'}: {line}" for ok, line in checks]
        rep += ["", f"No pitch: the draft was moved to outbox/_failed/{slug}/ - a broken "
                    f"preview is worse than none."]
        _report(f"{day}-{slug}.md", rep)
        log_build({"day": day, "mode": "live", "outcome": "failed", "slug": slug, "name": t["name"]})
        record(f"{t['name']} | deploy failed, taken down | {head.get('channel')} | failed - {failed}"[:300])
        print(f"LIVE CHECK FAILED: {failed}")
        return 1

    down = day_plus(day, TEARDOWN_DAYS)
    draft = (box / "draft.md").read_text().replace(PREVIEW_URL, url)
    (box / "draft.md").write_text(draft)
    live_shots = shoot(url, box, browser)
    row = {"date": day, "slug": slug, "business": t["name"], "phone": t.get("phone"),
           "area": s.get("area"), "preview_url": url, "channel": head.get("channel"),
           "status": "drafted"}
    rows = contacted()
    mine = [r for r in rows if was and norm_name(r["business"]) == norm_name(t["name"])]
    if mine:
        # One business, one row: the revision's link replaces the old one; a
        # pitch already sent keeps its status.
        mine[-1].update({k: v for k, v in row.items() if k != "status"})
        write_rows(A("state", "contacted.csv"), CONTACTED_FIELDS, rows)
    else:
        append_row(A("state", "contacted.csv"), CONTACTED_FIELDS, row)
    append_row(A("state", "deployments.csv"), DEPLOY_FIELDS,
               {"slug": slug, "preview_url": url, "business": t["name"], "deployed": day,
                "teardown": down, "status": "live"})
    tail(url, down)
    rep += [f"- live: {'ok' if ok else 'FAIL'} - {line}" for ok, line in checks]
    for name, p in live_shots:
        rep.append(f"- live {name} screenshot: {('outbox/' + slug + '/' + p.name) if p else 'NOT TAKEN'}")
    rep += review_lines(review)
    rep += ["", "## Notes from Paul", "", str(t.get("notes") or "-")]
    _report(f"{day}-{slug}.md", rep)
    log_build({"day": day, "mode": "live", "outcome": "built", "slug": slug, "name": t["name"],
               "kind": t.get("kind"), "layout": t.get("layout"), "theme": t.get("theme")})
    record(f"{t['name']} | {url} | {head.get('channel')} | drafted")
    if was:
        set_revision(was, state="updated", new_slug=slug, preview_url=url)
    print(f"ready to send: outbox/{slug}/draft.md  preview {url}")
    return 0


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="paul.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create Paul's state files (deploy.sh runs this)")
    p = sub.add_parser("sender", help="your details for the footer and signature")
    for k in SENDER_FIELDS:
        p.add_argument(f"--{k}")
    sub.add_parser("preflight", help="could Paul run? wakes no model")
    sub.add_parser("status", help="pitches waiting on you, live previews")
    p = sub.add_parser("mark", help="after you send one: sent/replied/won/declined/do-not-contact")
    p.add_argument("who", help="the slug, or the business name")
    p.add_argument("status", choices=STATUSES)
    p = sub.add_parser("drop", help="delete one version of a business's site (never its last)")
    p.add_argument("slug")
    p = sub.add_parser("undelete", help="bring back a version deleted in the last 30 days")
    p.add_argument("slug")
    p = sub.add_parser("retire", help="take down a business's older previews; its newest stays up")
    p.add_argument("who", help="the slug, or the business name")
    p = sub.add_parser("exclude", help="a business Paul must never pick")
    p.add_argument("business")
    p.add_argument("phone", nargs="?")
    p = sub.add_parser("teardown", help="take down previews that are due")
    p.add_argument("--list", action="store_true", help="only say which are due")
    p = sub.add_parser("go-live", help="let scheduled cycles deploy (after a dry cycle)")
    p.add_argument("--off", action="store_true")
    p.add_argument("--anyway", action="store_true", help="skip the seen-a-dry-cycle check")
    p = sub.add_parser("revise", help="send a finished site back to Paul with what to fix")
    p.add_argument("who", help="the slug, or (part of) the business name")
    p.add_argument("notes", nargs="+", help="what to fix, in your words")
    p.add_argument("--ref", help="a new site whose layout and feel Paul should take after")
    p = sub.add_parser("measure", help="the browser checks on a build (work/, or the newest set aside) - "
                                       "no model woken")
    p.add_argument("folder", nargs="?", help="a folder holding site/index.html")
    sub.add_parser("prospects", help="the businesses Paul found, and what the next cycle does")
    p = sub.add_parser("approve", help="approve a prospect for Paul to build")
    p.add_argument("who", help="its number, or part of its name")
    p.add_argument("--build", action="store_true", help="and start the cycle now")
    p.add_argument("--ref", help="a site whose layout and feel Paul should take after")
    p = sub.add_parser("build", help="build a business you name next, optionally after a site you like")
    p.add_argument("name", help="the business, as it is known")
    p.add_argument("--ref", help="a site whose layout and feel Paul should take after")
    p.add_argument("--where", help="its town, if Paul might find the wrong one")
    p.add_argument("--notes", help="what the owner wants on it, in their words")
    p.add_argument("--now", action="store_true", help="and start the cycle now")
    p = sub.add_parser("restore", help="build a business again after its sites were deleted by mistake")
    p.add_argument("name", help="the business, as it was")
    p.add_argument("--ref", help="a site whose layout and feel Paul should take after")
    p.add_argument("--where", help="its town")
    p.add_argument("--notes", help="what the owner wants on it, in their words")
    p.add_argument("--photos", type=int, help="the number of its folder in state/photos/, if not found")
    p.add_argument("--now", action="store_true", help="and start the cycle now")
    p = sub.add_parser("reject", help="reject a prospect - never proposed again")
    p.add_argument("who", help="its number, or part of its name")
    p.add_argument("why", nargs="*")
    sub.add_parser("find", help="start a cycle now (whatever cycle_plan says it is for)")
    p = sub.add_parser("places", help="check a prospect against its Google listing now")
    p.add_argument("who", help="its number, or part of its name")
    p = sub.add_parser("photo", help="photos you saved for a prospect: add, remove, list")
    p.add_argument("who", help="its number, or part of its name")
    p.add_argument("action", choices=("add", "remove", "list"))
    p.add_argument("file", nargs="?", help="add: the image file; remove: its name (01.jpg)")
    p.add_argument("--from", dest="origin", default="facebook", choices=PHOTO_FROM)
    p = sub.add_parser("stock", help="(Paul) Wikimedia Commons photos: find, or get one with its credit")
    sp = p.add_subparsers(dest="action", required=True)
    q = sp.add_parser("find", help="photos for some words, from the library and Commons")
    q.add_argument("words", nargs="+")
    q.add_argument("--limit", type=int, default=6)
    q = sp.add_parser("get", help="download one into work/site/img/ and record its credit")
    q.add_argument("which", help='"File:<name>" or its Commons page link')
    q.add_argument("--width", type=int, default=1280, help="the widest it is shown, in pixels, times 1.5")
    p = sub.add_parser("remove", help="delete every build of one business, for good")
    p.add_argument("who", help="part of the business name, or a slug")
    p.add_argument("--yes", action="store_true", help="really delete (without it, only lists)")
    p = sub.add_parser("dry-next", help="make the next paul-cycle.service run a dry cycle")
    p.add_argument("--take", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("deck", help="(village) everything the Paul panel shows, as JSON")
    p = sub.add_parser("focus", help="look only for one kind of business, until cleared")
    p.add_argument("kind", nargs="?", help=f"one of: {', '.join(KINDS)}")
    p.add_argument("--clear", action="store_true")
    sub.add_parser("leads", help="preview what the next scout is handed (uses Google lookups)")
    p = sub.add_parser("nearby", help="other towns the finder searches besides the area")
    p.add_argument("towns", nargs="*", help='e.g. "Waterloo, IA"')
    p.add_argument("--clear", action="store_true")
    p = sub.add_parser("should-run", help="(wrapper) wake the model or hold")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--record", action="store_true")
    p = sub.add_parser("brief", help="(wrapper) the FACTS block")
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("finish", help="(wrapper) file what Paul left")
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("seen", help="(Paul) has this business been contacted?")
    p.add_argument("name")
    p.add_argument("phone", nargs="?")
    sub.add_parser("check", help="(Paul) check your files; your sign-off")

    a = ap.parse_args(argv)
    return {"init": cmd_init, "sender": cmd_sender, "preflight": cmd_preflight,
            "status": cmd_status, "mark": cmd_mark, "retire": cmd_retire, "drop": cmd_drop,
            "undelete": cmd_undelete, "exclude": cmd_exclude,
            "teardown": cmd_teardown, "go-live": cmd_go_live, "dry-next": cmd_dry_next,
            "revise": cmd_revise, "measure": cmd_measure, "prospects": cmd_prospects, "approve": cmd_approve,
            "reject": cmd_reject, "build": cmd_build, "restore": cmd_restore, "find": cmd_find, "remove": cmd_remove,
            "places": cmd_places, "photo": cmd_photo, "stock": cmd_stock,
            "focus": cmd_focus, "nearby": cmd_nearby, "leads": cmd_leads, "deck": cmd_deck,
            "should-run": cmd_should_run, "brief": cmd_brief, "finish": cmd_finish,
            "seen": cmd_seen, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
