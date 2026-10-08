#!/usr/bin/env python3
"""
paul.py - everything in Paul's cycle that is not a judgement call.

Paul finds one local business with no website or a weak one, builds it a
site, and drafts a pitch that the owner sends. Choosing the business,
designing the site and writing the words are the model's job. Everything
else lives here, because CLAUDE.md's two rules were each learned the
expensive way: anything a script can do exactly, a script does; and an
agent is judged by code reading its files, not by what it says it did.

So Paul never deploys, never writes a record and never contacts anyone. It
writes four things - work/target.json, work/site/, work/draft.md and
state/last-run.txt - and this file does the rest.

    the owner     init  sender  preflight  status  mark  exclude
                  teardown  go-live  dry-next
    the wrapper   should-run  brief  finish        (scripts/paul-cycle.sh)
    Paul itself   seen  check

    python3 scripts/paul.py preflight          could Paul run? wakes nothing
    python3 scripts/paul.py status             pitches waiting on you, previews
    python3 scripts/paul.py mark <slug> sent   after you send one
    python3 scripts/paul.py dry-next           the next run is a dry cycle

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
budget.py. Nothing else.
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
MAX_DRAFT_WORDS = 170
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
FREEBIE = ("no strings attached", "yours to keep", "for free", "free of charge", "at no cost",
           "no charge", "on the house", "free website", "free site")

# What makes a business a target. Each one is something a person can check,
# so "their site looks dated" is not on the list - that is a vibe.
PROBLEMS = {
    "no-site": "no website findable by search or on its Maps listing",
    "dead-domain": "the website address does not load at all",
    "parked": "the domain shows a parking or for-sale page",
    "no-https": "the site only loads without HTTPS",
    "not-mobile": "the site is unusable on a phone",
    "broken": "broken links or images",
    "wrong-info": "wrong key information (hours, phone, address)",
}

# The quality bar. Two of the five were down when this was written
# (2026-10-03, DEPLOYMENT_NOT_FOUND), so brief() asks each one every cycle
# and only hands Paul the ones that load.
REFERENCES = (
    ("trade, home services", "https://rourke-plumbing.vercel.app"),
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

STOCK_HOSTS = ("unsplash.com", "pexels.com")
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
           "passion for excellence", "crafted with love", "elevated", "curated")
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


def screenshot(browser, url, out, w, h, run=subprocess.run):
    """True only if a real image landed on disk. A browser that exits 0 having
    written nothing is the case this exists to catch."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    cmd = [browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--disable-dev-shm-usage", f"--window-size={w},{h}",
           "--virtual-time-budget=4000", f"--screenshot={out}", url]
    try:
        run(cmd, capture_output=True, timeout=90)
    except Exception:
        return False
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
            else:
                self.refs.append(("link", a["href"]))
        if a.get("style"):
            self.css.append(a["style"])

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
            self._style = False

    def handle_data(self, data):
        if self._style:
            self.css.append(data)
        elif not self._skip:
            self.text.append(data)


CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)


def _skip_ref(u):
    low = u.strip().lower()
    return (not low or low.startswith(("#", "mailto:", "tel:", "sms:", "javascript:", "data:")))


def _external(u):
    return re.match(r"^(https?:)?//", u.strip(), re.I) is not None


def strip_note(markup):
    """The page without the footer code adds, so the footer's own words are
    not read as meta copy."""
    return re.sub(rf'(?is)<p[^>]*id="{CONCEPT_ID}"[^>]*>.*?</p>', "", markup)


def scan_site(site):
    """Everything check() needs to know about a site folder, read once."""
    site = Path(site)
    out = {"pages": [], "errors": [], "text": "", "tel": [], "images": {},
           "external_images": [], "bytes": 0, "viewport": True, "local_refs": [],
           "no_alt": [], "embeds": [], "scripts": [], "styles": [], "forms": 0,
           "icon": False, "css": []}
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
        for k in ("no_alt", "embeds", "scripts", "styles"):
            out[k] += getattr(pr, k)
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


# Every site starts from templates/local, the owner's chosen look (2026-10-08:
# "we're targeting any business that has a poor or no website"). Its
# stylesheet opens with this line; a page designed from blank does not.
TEMPLATE_MARK = "Paul's site template"


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
    if not site or site.lower() == "none":
        for c in codes:
            if c in ("no-site", "wrong-info"):
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
    other = sorted(said - {price}, key=int)
    if other:
        errs.append(f"the only price is ${price} - remove ${', $'.join(other)}")
    if "monthly" not in low:
        errs.append('say there are no monthly fees - it is the selling point')
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
    # Owner, 2026-10-06: Paul's prospects have no working site, so their
    # photos sit behind Facebook's or Instagram's login, and an all-stock
    # preview is the usual case (College Hill Barbers, Milkbox). Fine for a
    # concept - but the pitch says so, rather than let stock pass as theirs.
    photos = [p for p in (t or {}).get("photos") or [] if isinstance(p, dict)]
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
                        f"(Unsplash/Pexels: add ?w=1600&q=80 to the URL)")
    if sc["bytes"] > MAX_SITE_BYTES:
        errs.append(f"the site is {sc['bytes'] // 1024} KB - keep it under "
                    f"{MAX_SITE_BYTES // 1024} KB so it is fast on a phone")
    if any(site.glob("sitemap*.xml")):
        errs.append("remove sitemap.xml - a preview must not be offered to search engines")

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
    errs += claim_problems(sc["text"], t)
    if not any(TEMPLATE_MARK in c for c in sc["css"]):
        errs.append("every site starts from the template: "
                    "mkdir -p work/site && cp -r templates/local/. work/site/ - then fill it in")

    photos = {str(p.get("file") or "").lstrip("./").removeprefix("site/"): p
              for p in (t.get("photos") or []) if isinstance(p, dict)}
    rasters = [r for r in sc["images"] if r.lower().endswith(RASTER)]
    for rel in rasters:
        p = photos.get(rel)
        if not p:
            errs.append(f'{rel} has no entry in target.json "photos" - say where it came from')
            continue
        src = str(p.get("source") or "")
        if not re.match(r"^https?://", src):
            errs.append(f"{rel}: its source must be the URL you downloaded it from")
        if p.get("kind") not in ("business", "stock"):
            errs.append(f'{rel}: kind must be "business" (their own posted photo) or "stock"')
        elif p.get("kind") == "stock":
            host = urllib.parse.urlsplit(src).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in STOCK_HOSTS):
                errs.append(f"{rel}: stock photos come from Unsplash or Pexels only, not {host}")
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
    return errs


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
 el.scrollIntoView({block:'center'});var bg=behind(el);if(!bg)continue;
 var fg=over(rgba(s.color),bg);var L1=lum(fg),L2=lum(bg);var ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
 var px=parseFloat(s.fontSize),w=parseInt(s.fontWeight)||400;var need=(px>=24||(px>=18.66&&w>=700))?3:4.5;
 if(ratio<need)out.push({text:own.slice(0,60),ratio:Math.round(ratio*100)/100,need:need});}
var pre=document.createElement('pre');pre.id='paul-contrast';pre.textContent=JSON.stringify({checked:seen,fails:out});document.body.appendChild(pre);
})();"""


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
    fp = site_fingerprint(site)
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
    info.update(sinfo)
    browser = find_browser()
    if browser and (work / "site" / "index.html").is_file():
        bad = measured_contrast(work / "site", browser, work / "shots" / ".measured.json")
        if bad is None:
            notes.append("text contrast not measured - the browser gave no answer")
        for rel, text, ratio, need in (bad or [])[:6]:
            errs.append(f'"{text}" on {rel} is hard to read: contrast {ratio}:1, needs '
                        f"{need}:1 - change the text colour or what is behind it")

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
    meta = f'<meta name="robots" content="{NOINDEX}">'
    for page in sorted(site.rglob("*.html")):
        markup = re.sub(r"(?s)<!--.*?-->\n?", "", page.read_text(errors="replace"))
        markup = re.sub(rf'(?is)<p[^>]*id="{CONCEPT_ID}"[^>]*>.*?</p>', "", markup)
        markup = re.sub(r'(?i)<meta[^>]+name=["\']robots["\'][^>]*>', "", markup)
        if re.search(r"(?i)<head[^>]*>", markup):
            markup = re.sub(r"(?i)(<head[^>]*>)", lambda m: m.group(1) + meta, markup, count=1)
        else:
            markup = meta + markup
        if re.search(r"(?i)</body>", markup):
            idx = [m.start() for m in re.finditer(r"(?i)</body>", markup)][-1]
            markup = markup[:idx] + note + markup[idx:]
        else:
            markup += note
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

    # The provider, not tools.web.search itself: once anything under it is
    # set, that is an object, and its first line is "{" (owner's droplet,
    # 2026-10-05, after openclaw configure --section web). One scalar says it.
    print(f"  web search  provider: {_openclaw_get('tools.web.search.provider')}")
    print("              (Paul needs a web search tool to find businesses - this should name")
    print("               one, e.g. brave; if it says unset, send Claude this line)")
    # Paul must not heartbeat - it is woken by its timer and nothing else.
    # Not set from here: adding a heartbeat block to one agent can change which
    # agents heartbeat at all, which would be touching the others.
    print(f"  heartbeat   defaults: {_openclaw_get('agents.defaults.heartbeat.every')}")
    print(f"              paul:     {_openclaw_get('agents.entries.paul.heartbeat.every')}")
    print("              (should be off - 0m, or unset with no default interval; if either")
    print("               shows an interval, send Claude these two lines)")

    left, need = budget_left(), min_left()
    if left is None:
        print(f"  budget      unreadable - a cycle starts anyway (budget.py's rule)")
    else:
        print(f"  budget      ${max(left, 0):.2f} left on {budget_source()}; "
              f"a cycle needs ${need:.2f} to start")

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
    return teardown(list_only=a.list)


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


def cmd_dry_next(a):
    m = A("state", "dry-next")
    if a.take:
        if m.is_file():
            m.unlink()
            return 0
        return 1
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(today() + "\n")
    print("the next paul-cycle.service run will be a dry cycle:")
    print("  systemctl start --no-block paul-cycle.service")
    return 0


# --------------------------------------------------------------------------
# commands: the wrapper
# --------------------------------------------------------------------------

def hold_reason(dry):
    """Why the model should not be woken now, or None. Decided by code
    because a woken model that has nothing to do still costs money to say so."""
    _, missing = load_sender()
    if missing:
        return (f"sender details not set ({', '.join(missing)}) - "
                f"python3 scripts/paul.py sender")
    if not A("AGENTS.md").is_file():
        return "agents/paul/AGENTS.md is missing - scripts/deploy.sh paul"
    if not dry:
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


def cmd_brief(a, get=http_get):
    s, _ = load_sender()
    work = A("work")
    resume = "none"
    if work.is_dir() and any(work.iterdir()):
        newest = max(p.stat().st_mtime for p in work.rglob("*")) if any(work.rglob("*")) else 0
        old = (time.time() - newest) / 86400
        t = read_json(work / "target.json", {}) or {}
        if old <= RESUME_DAYS:
            resume = (f'work/ holds an unfinished build for "{t.get("name") or "a business"}" - '
                      f"finish it rather than starting over")
        else:
            dest = A("sites", "_abandoned", f"{today()}-{int(time.time())}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work), str(dest))
            resume = f"none (a build older than {RESUME_DAYS} days was moved to sites/_abandoned/)"
    A("work").mkdir(parents=True, exist_ok=True)
    # Kept to the millisecond. Whole seconds let a line written in the same
    # second just BEFORE the cycle began pass as this cycle's - a dry run
    # started right after a hold was signed off on the hold's line.
    save_cycle({"started": round(time.time(), 3), "mode": "dry" if a.dry else "live",
                "day": today(), "passes": 0, "resume": resume != "none"})

    rows = contacted()
    recent = [f"{r['business']} | {norm_phone(r['phone']) or '-'}" for r in rows[-15:]]
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
          f"area         {s.get('area', '?')}",
          f"studio       {s.get('studio', '?')} - code writes the footer and the signature",
          f"offer        ${s.get('price', '?')}, one time. It covers {OFFER_COVERS}.",
          "             The pitch states that exact price and what it covers, in your words.",
          f"resume       {resume}",
          *([f"FOCUS        {KIND_WORDS[focus_kind()]} only (kind \"{focus_kind()}\"). Code refuses",
             "             any other kind; none good enough is a valid no-target day."]
            if focus_kind() else []),
          f"passes       {MAX_PASSES} - each `paul.py check` that finds problems uses one",
          f"skip         {len(rows)} businesses already contacted or excluded. Check a candidate:",
          '             python3 ../../scripts/paul.py seen "<name>" "<phone>"']
    for r in recent[::-1]:
        L.append(f"             {r}")
    if lays:
        L.append("last builds  " + lays[-1])
        L += [f"             {x}" for x in lays[-2::-1]]
        L.append("             vary the layout - never the same one twice in a row")
    else:
        L.append("last builds  none yet")
    L.append("references   open the closest one before you build (they are live today):")
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


def cmd_check(a, get=http_get):
    cyc = load_cycle()
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
    if built and not errs and t.get("name") and seen_row(t["name"], t.get("phone", "")):
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
        log_build({"day": day, "mode": "dry" if dry else "live", "outcome": "no-target"})
        record(f"no target - {reason}"[:300])
        if A("work").is_dir():
            shutil.rmtree(A("work"))
        print(f"no target this cycle: {reason}")
        return 0

    errs, notes, info = run_checks(t, get)
    if status == "built" and not errs and not dry and seen_row(t["name"], t.get("phone", "")):
        errs.append(f"{t['name']} is already in contacted.csv")

    if status == "below-bar" or errs:
        short = make_slug(t.get("name") or "site", suffix="x").rsplit("-concept-", 1)[0]
        dest = _set_aside("_below-bar", short)
        reason = t.get("why") if status == "below-bar" else "; ".join(errs[:3])
        _report(f"{day}-{short}-below-bar.md",
                [f"# Paul - {day} - {t.get('name') or 'unnamed'}: not shipped", "",
                 f"Why: {reason}", "", f"The work is kept in {dest.relative_to(A()) if dest else '-'}."]
                + [f"- {e}" for e in errs])
        log_build({"day": day, "mode": "dry" if dry else "live", "outcome": "below-bar",
                   "name": t.get("name")})
        record(f"below bar, not shipped: {t.get('name') or '?'} - {reason}"[:300])
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
        rep += ["", "## Notes from Paul", "", str(t.get("notes") or "-"), "",
                "Dry run: built locally only. Nothing was deployed and the business was not "
                "recorded as contacted."]
        _report(f"{day}-{slug}.md", rep)
        log_build({"day": day, "mode": "dry", "outcome": "built", "slug": slug,
                   "name": t["name"], "kind": t.get("kind"), "layout": t.get("layout")})
        record(f"DRY: {t['name']} | built locally, sites/{slug} | {head.get('channel')} | not deployed")
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
    append_row(A("state", "contacted.csv"), CONTACTED_FIELDS,
               {"date": day, "slug": slug, "business": t["name"], "phone": t.get("phone"),
                "area": s.get("area"), "preview_url": url, "channel": head.get("channel"),
                "status": "drafted"})
    append_row(A("state", "deployments.csv"), DEPLOY_FIELDS,
               {"slug": slug, "preview_url": url, "business": t["name"], "deployed": day,
                "teardown": down, "status": "live"})
    tail(url, down)
    rep += [f"- live: {'ok' if ok else 'FAIL'} - {line}" for ok, line in checks]
    for name, p in live_shots:
        rep.append(f"- live {name} screenshot: {('outbox/' + slug + '/' + p.name) if p else 'NOT TAKEN'}")
    rep += ["", "## Notes from Paul", "", str(t.get("notes") or "-")]
    _report(f"{day}-{slug}.md", rep)
    log_build({"day": day, "mode": "live", "outcome": "built", "slug": slug, "name": t["name"],
               "kind": t.get("kind"), "layout": t.get("layout")})
    record(f"{t['name']} | {url} | {head.get('channel')} | drafted")
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
    p = sub.add_parser("exclude", help="a business Paul must never pick")
    p.add_argument("business")
    p.add_argument("phone", nargs="?")
    p = sub.add_parser("teardown", help="take down previews that are due")
    p.add_argument("--list", action="store_true", help="only say which are due")
    p = sub.add_parser("go-live", help="let scheduled cycles deploy (after a dry cycle)")
    p.add_argument("--off", action="store_true")
    p.add_argument("--anyway", action="store_true", help="skip the seen-a-dry-cycle check")
    p = sub.add_parser("dry-next", help="make the next paul-cycle.service run a dry cycle")
    p.add_argument("--take", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("focus", help="look only for one kind of business, until cleared")
    p.add_argument("kind", nargs="?", help=f"one of: {', '.join(KINDS)}")
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
            "status": cmd_status, "mark": cmd_mark, "exclude": cmd_exclude,
            "teardown": cmd_teardown, "go-live": cmd_go_live, "dry-next": cmd_dry_next,
            "focus": cmd_focus,
            "should-run": cmd_should_run, "brief": cmd_brief, "finish": cmd_finish,
            "seen": cmd_seen, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
