# Handoff: Paul (local-business websites and pitches)

Written 2026-10-21 by the Claude session that built most of this repo, for a
new chat that will look after **Paul**: the agent that finds local businesses
with no website or a weak one, builds each one the owner approves a real site,
publishes a private preview and drafts a pitch for the owner to send. Read it
top to bottom once; the rest of the repo will make sense after.

- This repository, on the branch everything lives on:
  https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- This note as raw text (easiest for another AI to read):
  https://raw.githubusercontent.com/cljxs/cljxs/claude/ai-agent-ecosystem-setup-0280zz/HANDOFF-PAUL.md
- Read before changing anything: [CLAUDE.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md)
  (how to change things without breaking them) and
  [ROUTING.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md)
  (which files, anchors and tests each request touches; its "paul.py anchors"
  section maps the 4,000-line script - grep for the anchor, never read it whole).
  In a Claude Code session on this repo both load by themselves.

The repository is **public**. Nothing secret is in it and nothing secret may
ever be put in it: no API keys, tokens, passwords, IP addresses, email
addresses, or live data (Paul's prospects, contacts, sites and drafts are
gitignored and live only on the server).

**Paul never contacts anyone.** No email, DM, call or contact form, ever -
he drafts, the owner sends. Keep it that way.

---

## 1. Who the owner is, and how to work with them

- **Not a programmer.** Plain words; say what a timer, a commit or a preview
  does. Outcome first, short.
- **Works from an iPad**: Termius (terminal) to reach the server, Safari for
  the web pages. So:
  - **Every command is ONE line, in a code block to paste.** The terminal
    flattens multi-line pastes, and the iPad keyboard turns a typed `--` into a
    long dash. Better still, build a button in Paul's Studio - the owner
    increasingly works from there rather than the terminal.
  - Pages must work on a tablet and a phone.
- **They judge Paul's sites by looking at them** and send screenshots of what
  is wrong. Treat each as a bug report: find why code let it through, fix the
  site *and* add the check that would have caught it.
- **They like to choose.** 2-4 options with a recommendation for anything big.
- **Money matters.** Say what something costs before building it.
- After each piece of work: tested, committed and pushed to the branch above,
  then the single-line commands to run on the server.
- **Say "verified" (you ran it and read the output) or "believed" (it should
  work).** The repo's tests can render pages in a real browser - use that,
  and say when you have not seen the real site on the server.

---

## 2. The ecosystem, briefly

One Ubuntu server (a DigitalOcean droplet), repo cloned at `/root/ecosystem`.
Each agent wakes on a **systemd timer**, does its work by calling Python
scripts, and **code judges the cycle by what is on disk**, never by what the
agent says it did. Other agents share the server, the budget and the web app:
Ace, Belfort, Bolt (paper trading), Peter (sports props), Scout and Emily
(Etsy shop), Clip (video clips), Stan, the Agent Room, Fury and the GM.
ROUTING.md has a row for each.

**Which engine wakes a model** is `tasks/engines.json`, read only by
`scripts/wake.py`. **Paul runs on Sonnet on the owner's Claude plan** (`claude -p`,
up to 80 turns), so his cycles spend no OpenRouter money; if the plan refuses a
wake before it starts, wake.py falls back to OpenRouter on Paul's own key
(`PAUL_OPENROUTER_KEY`, its own daily limit). `python3 scripts/wake.py engines`
shows what runs where.

**The Command Deck** (`mission-control-api/`, Node/Express, port 3001, reached
from the iPad over Tailscale). The JavaScript holds no rules: each route runs
`scripts/paul.py` with an argument array and returns its JSON. Paul's page is
`/paul` - **Paul's Studio** - also opened from his house in `/village`.
A changed `public/paul.html` needs only a reload; a changed
`mission-control-api/paul.js` needs `python3 scripts/restart-deck.py`
(never `systemctl restart` - it kills jobs the Deck started).

---

## 3. How Paul works

### The cycle - `paul-cycle` timer, 16:45 Central, daily

`scripts/paul-cycle.sh` asks `paul.py` what the cycle is for (`cycle_plan`),
writes the FACTS block (`paul.py brief`), wakes Paul through wake.py, then files
what he left (`paul.py finish`). Code decides; Paul only does the judgement work.

| Cycle | When | Paul writes |
|---|---|---|
| **SCOUT** | fewer than 5 prospects are waiting | `work/prospects.json`: real businesses with a checkable problem, each fact with its source URL |
| **BUILD** | the owner approved a prospect | `work/target.json`, `work/site/`, `work/draft.md` |
| **RESUME** | `work/` holds something | finishes it; `work/REVISE.md` = sent back by the owner with notes |
| **hold** | 5 prospects waiting, 5 unsent pitches, or no sender details | nothing - no model is woken |

He writes four things (`work/...` and `state/last-run.txt`); **`paul.py` does
everything else**: checks every fact has a source, tests what he says is wrong
with the business's current site against the page itself, adds the preview
safeguards, deploys to Vercel, checks the live page, fills the link into the
draft, writes the records, the report and the memory line.

**Finding prospects:** Google Places search (`GOOGLE_PLACES_KEY`) across the
owner's area and nearby towns (`paul.py nearby`), many business searches
(`SEARCHES`); `paul.py focus <kind>` restricts to one kind (trade, food,
salon, retail, other). A business qualifies by a problem a person can check
(`PROBLEMS`): no site, social-only, no SEO title, a copyright 3+ years old,
dead or parked domain, no HTTPS, not mobile, broken, wrong info. Of Google's
listing only counts, warnings and the place id are kept; its photos stay on Google.

### What code refuses on a site (`run_checks` / `check_site`)

Every fact sourced and shown; the real address and phone with a working call
link; nothing from a reference site; no placeholder or "our new site" copy;
no stock-AI phrasing, no em dashes, no emoji; no unsourced claims (licensed,
certified, years in business); at least 2 things only true of this business;
SEO basics; no forms, iframes or tracking; contrast measured in a browser;
**on a 390px phone no button or link wraps** (counted per run of text since
2026-10-21) and nothing scrolls sideways; **every photo measured at desktop
width - refused if stretched out of shape or shown over 1.25x its file size**
(2026-10-21); with an owner's reference site, none of its name or 8 words in a
row from it. Two failed `paul.py check` passes and the build is set aside.

### Previews (what a business sees)

`<name>-concept-<6 random>.vercel.app`, `noindex` everywhere, robots.txt
disallow, and a footer "Design concept by <studio>. Not the official site of
<business>." Teardown 30 days after going up with no reply; at once on
declined or do-not-contact; replied/won keep it. **Vercel's Hobby plan is
non-commercial** - pitching paid work is commercial; Pro or another host is the
owner's call (`paul.py preflight` shows the plan).

### The pitch (`outbox/<slug>/draft.md`)

Email if the business has a public email, else a DM or phone script. States the
owner's one-time price and what it covers (`OFFER_COVERS`), the one yearly cost
(their domain, about $15, paid to the registrar), that photos and colours can
change, and one feature that fits them (`FEATURE_IDEAS`). Code adds the opt-out
line and the signature (with a mailing address on email - CAN-SPAM). 25-200 words.

---

## 4. Paul's Studio (`/paul`) - what the owner can do there

- **Status** at the top: what the next cycle is for, the last run, live or
  dry, and "PAUL IS WORKING NOW" while his cycle runs.
- **Prospects tab:** each business Paul found, with why, its problems, links,
  a **Check Google** button, and:
  - **Approve + build** (starts his cycle now), with an optional **reference
    site** box ("build it in the style of this site") and **Reject**.
  - **Their photos for the site**: the owner uploads the business's own photos
    (Facebook, Instagram, Google...), which Paul uses before anything else.
    Shown on every card except rejected ones.
  - **↻ Try again** on a *Not built* card: brings his last attempt back from
    `sites/_below-bar/` to finish rather than start over.
- **Work tab:** each built site with phone and computer screenshots, Open the
  site, Live preview, Paul's notes, and:
  - **Send back to Paul** with what to fix (`paul.py revise`); his cycle starts.
    Progress shows on the card - 📝 sent, 🔨 Paul is working on it, ⏸ he
    stopped, ✓ updated (with links to the new site), ⚠ didn't pass (and why).
    The new version is its own card marked UPDATED FROM YOUR NOTE.
    (`state/revisions.json`: sent -> building -> updated | not shipped.)
  - **Delete this version** (`paul.py drop <slug>`): that version only - its
    preview comes down, its files wait 30 days in `sites/_deleted/`
    (`paul.py undelete <slug>`). Never a business's last version, never the
    one whose link was sent. The owner's rule (2026-10-22, after the old
    Delete took every Royal Edge site): **the Studio never deletes a whole
    business**. `paul.py remove --yes` still does, from the terminal only;
    `paul.py restore` builds one again (photos, reference, `--notes`).
  - **Take down N older previews** on a business's newest card (`paul.py retire`).
- **Drafts tab:** one pitch per business (its newest site's), with Copy
  message / number / subject buttons and **I sent it** (`paul.py mark <slug> sent`;
  replied / won / declined / do-not-contact from the terminal). Sending a
  site back carries its pitch over unchanged.
- **Activity tab:** his memory lines.

From the terminal: `paul.py build "<Business>" --ref <url> --where "<Town, ST>" --now`
adds a business the owner names (with an optional reference site) and builds it.

---

## 5. Where things are

    scripts/paul.py                  everything that is not a judgement call
    scripts/paul-cycle.sh            the timer's cycle: plan, brief, wake, finish
    agents/paul/_paul-agents-header.md   Paul's instructions (AGENTS.md is generated -
                                         edit this, then `scripts/deploy.sh paul`)
    agents/paul/templates/local/     the quality bar (code refuses a site built on it)
    agents/paul/README.md            install, go-live, day to day (some numbers there are older
                                     than this note - engines.json is the truth for the model)
    mission-control-api/paul.js      the Studio's routes        public/paul.html  the Studio
    on the server only (gitignored):
      agents/paul/state/   sender.json, credentials.env (VERCEL_TOKEN, GOOGLE_PLACES_KEY,
                           PAUL_OPENROUTER_KEY), prospects.json, contacted.csv,
                           deployments.csv, builds.jsonl, revisions.json, photos/
      agents/paul/work/    the build in progress      sites/<slug>/  built sites
      agents/paul/outbox/<slug>/   draft.md, target.json, screenshots
      agents/paul/sites/_below-bar/  builds that did not ship (Try again / Send back resume them)

Useful commands (all free, no model woken unless said):

    python3 scripts/paul.py preflight     could Paul run? keys, browser, Vercel plan
    python3 scripts/paul.py status        pitches waiting, live previews
    python3 scripts/paul.py prospects     the prospect list and what the next cycle does
    python3 scripts/paul.py measure       the browser checks on work/ or the newest build set aside
    python3 scripts/paul.py find          start a cycle now (wakes Paul)
    python3 scripts/last-run.py paul      what his last cycle actually did and cost

**Tests:** `tests/test_paul.py` (over 200; a few drive a real headless Chrome
and are skipped without one - in a cloud session set
`PAUL_BROWSER=/opt/pw-browsers/chromium-1194/chrome-linux/chrome` if present),
and `tests/test_parsers.py` class `PaulHasAWorkshop` (which also pins the exact
list of Studio routes, so none can send or contact - add a new route there
deliberately). Run before a push: `python3 -B -m unittest tests.test_paul -b`,
the fast suite `python3 -B -m unittest tests.test_parsers -b`, and
`python3 scripts/check-secrets.py`.

---

## 6. The rules this ecosystem paid for (apply them to Paul)

1. **A model will claim work it did not do.** `paul.py check` / `finish` judge
   the files; Paul's notes are read, never trusted.
2. **Real or nothing.** Every business and fact is read from a public source
   that cycle with the URL kept; never invented, never recalled from memory.
   Reference sites hold demo data (555 numbers, invented licences) - never a fact.
3. **Anything a script can check, a script checks** - and when the owner finds
   something wrong on a site, the fix includes the check (Royal Edge's stretched
   photo became `image_problems`; a false "wraps on a phone" became per-run line
   counting).
4. **Test a check against the real thing**: a real headless browser on a real
   page, and a real sample of the bad case. Both Royal Edge bugs were checks
   that had never met that case.
5. **One fact, one place.** `run_checks` is the one set of rules for both
   `check` (Paul) and `finish` (code). Do not restate a rule in a second place.
6. **Outward actions wait for the owner.** Paul never contacts anyone; nothing
   is sent from code; deleting and going live are the owner's buttons.
7. **Credentials never leave the server** (`scripts/set-credential.py paul <NAME>`,
   typed by the owner). Never ask for a key in chat; never print one.
8. **Changing Paul's instructions**: edit the header, then `scripts/deploy.sh paul`
   on the server - otherwise he keeps last week's rules.
9. **Never `pkill -f` a pattern** on the server; stop processes by PID.
10. **Every commit ends with the session's attribution lines; no model names in
    commits, code or pull requests. Pull requests only when the owner asks.**

---

## 7. Where things stand (2026-10-21)

- **Royal Edge** (barbershop, Waterloo IA; built in the style of
  rudysbarbershop.com, the owner's reference). The first site went live
  2026-10-20. Its shop photo was a customer captioned "the front windows",
  blown up and stretched. Paul's fix was refused by a bug ("already in
  contacted.csv" - its own row), fixed the same day; the owner was told to
  **Send back** with "Finish your photo fix", which resumes Paul's set-aside
  work. **Check with the owner whether the update shipped** (its card should
  read ✓ Updated). Paul's notes: the door shows **626** while Booksy and Maps
  say **621** Sycamore St - the owner should confirm with the shop before pitching.
- **College Hill Barbers**: the owner asked to delete both of its sites (the
  Delete button); confirm it is gone.
- **5 prospects** were waiting for the owner's call (Approve or Reject) - Paul
  holds new scouting until they are decided.
- Ideas offered earlier and not yet answered: a "pitch only" option (no
  site), and a hand-over step after a sale (moving the site to a host the
  client owns - Vercel Hobby is non-commercial).

---

## 8. Links

- Repo (branch): https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- CLAUDE.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md
- ROUTING.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md
- Paul's instructions: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/agents/paul/_paul-agents-header.md
- Vercel REST API: https://vercel.com/docs/rest-api - plans: https://vercel.com/pricing
- Google Places API (New): https://developers.google.com/maps/documentation/places/web-service
- CAN-SPAM rules for business email: https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
- Claude Code on the web: https://code.claude.com/docs/en/claude-code-on-the-web
