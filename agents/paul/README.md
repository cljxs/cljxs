# Paul — website builder + local outreach

Finds ONE real local business a day with no website or a weak one, builds it
a genuinely good site, publishes a private preview and drafts a pitch.
**Paul never contacts anyone** - you read the pitch, change what you like,
and send it yourself.

## The loop

    Paul finds + builds  ->  code checks, deploys a private preview  ->  YOU send  ->  YOU mark it

Paul writes four things - `work/target.json`, `work/site/`, `work/draft.md`
and `state/last-run.txt` - and `scripts/paul.py` does everything else:
checks every fact has a source, tests Paul's claims about the business's
current site, adds the preview safeguards, deploys, verifies the live page,
fills in the link, writes the records, the report and the MEMORY line. Same
split as every agent here, for the same reasons (CLAUDE.md).

## Install - single lines, in this order

1. Register Paul, then build its instructions and units (seeds AGENTS.md
   first; `deploy.sh` merges the header onto it):

       cd /root/ecosystem && git pull --ff-only && openclaw agents add paul --workspace /root/ecosystem/agents/paul --non-interactive && scripts/deploy.sh paul

2. Your details - the preview footer and the pitch signature. Stays on the
   droplet (`state/sender.json`, gitignored; this repo is public):

       python3 /root/ecosystem/scripts/paul.py sender --area "City, ST" --name "Your Name" --studio "Studio Name" --email you@example.com --phone "555 555 5555" --address "PO Box 1, City, ST 00000" --price 350

3. The model, **Paul's own OpenRouter key**, and a gateway restart. Make a new
   key at openrouter.ai/settings/keys just for Paul, with its own daily limit:
   his cycles then never touch the shared key's day, where Emily's images need
   $1.00 of room. The same key goes in twice - once for the model calls, once
   (as PAUL_OPENROUTER_KEY) so his budget check reads what is left on it:

       /root/ecosystem/scripts/set-agent-model.sh paul openrouter/anthropic/claude-sonnet-5.5 --apply && openclaw models auth paste-api-key --provider openrouter --agent paul && openclaw gateway restart

       python3 /root/ecosystem/scripts/set-credential.py paul PAUL_OPENROUTER_KEY

4. A browser for screenshots (Google's .deb - the snap Chromium cannot write
   under /root):

       wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && apt-get install -y /tmp/chrome.deb && python3 /root/ecosystem/scripts/paul.py preflight

## The dry cycle - before anything goes live

Same work, same wiring the timer uses, but built locally only: nothing is
deployed and the business is not recorded as contacted.

    python3 /root/ecosystem/scripts/paul.py dry-next && systemctl start --no-block paul-cycle.service

Then, when it has finished (up to ~30 minutes):

    python3 /root/ecosystem/scripts/paul.py status && ls /root/ecosystem/agents/paul/reports/ && python3 /root/ecosystem/scripts/last-run.py paul

`last-run.py paul` prints what the cycle actually cost. The site is in
`sites/<slug>/`, the screenshots and the DRY RUN pitch in `outbox/<slug>/`.

## Going live

Previews are deployed to Vercel through its API, with a token kept in
`state/credentials.env` (never printed, never committed):

    python3 /root/ecosystem/scripts/set-credential.py paul VERCEL_TOKEN && python3 /root/ecosystem/scripts/paul.py go-live

`go-live` refuses until the token works, the browser works, and a dry cycle
has built a site. Until then the timer still fires every day and holds
without waking a model, so it costs nothing. Pause live runs again with
`paul.py go-live --off`.

**Hosting.** Vercel's Hobby plan is for personal, non-commercial use, and
building sites to pitch for paid work is commercial. `paul.py preflight`
prints the plan when Vercel reports it. Pro, or a different host for
previews, is your call.

## Day to day

    python3 /root/ecosystem/scripts/paul.py status                      what is waiting for you
    python3 /root/ecosystem/scripts/paul.py mark <slug> sent            after you send it
    python3 /root/ecosystem/scripts/paul.py mark <slug> replied         keeps its preview up
    python3 /root/ecosystem/scripts/paul.py mark <slug> declined        takes the preview down now
    python3 /root/ecosystem/scripts/paul.py exclude "<business>" "<phone>"   never pick this one

Paul's line also shows up in Fury's morning briefing and on the Deck, like
every agent's - that is the "a pitch is ready" notice.

**Five unsent pitches and Paul stops building** - code holds the run without
waking a model. Mark the ones you have sent (or declined) and it resumes.

## Previews - what code adds, and when they come down

A preview carries a real business's name before they have agreed to
anything, so it must never pass for their site:

* `noindex` meta tag on every page, an `X-Robots-Tag: noindex` header, and a
  robots.txt that disallows everything. Never submitted to a search engine.
* The address is `<short-name>-concept-<6 random characters>.vercel.app` -
  never the bare business name.
* A one-line footer: "Design concept by <your studio>. Not the official site
  of <business>."
* **Teardown:** 30 days after it went up with no reply, or at once when you
  mark it declined or do-not-contact. `replied` and `won` keep it up. Runs at
  the start of every cycle and costs nothing; `paul.py teardown --list` shows
  what is due.

Photos: the business's own posted photos first (its site, Facebook,
Instagram, Google Business Profile owner uploads), then Unsplash/Pexels.
Never customer review photos, never AI images of their product or place.
Every image is downloaded and served with the site, because platform image
links expire. Each photo's source is in the report.

## The pitch

`outbox/<slug>/draft.md` - email when they have a public email, otherwise a
labelled DM or phone script. Code adds an opt-out line ("If this isn't for
you, just say so and I won't follow up") and your signature; on email that
includes your mailing address (CAN-SPAM). Outside the US the rules differ
(Canada's CASL, UK PECR, EU GDPR) - `paul.py sender` warns when the area does
not look like a US one.

## What runs when, and what it costs

| | Schedule | Costs |
|---|---|---|
| `paul-cycle.timer` | 16:45 CT, daily | **fuel**, one business per run |

The timer is the only thing that wakes Paul - no task queue, no heartbeat.
`paul.py preflight` prints openclaw's heartbeat setting so you can see it is
off; it is not changed from here, because adding a heartbeat block to one
agent can change which agents heartbeat at all.

Late on purpose: the daily AI allowance turns over at midnight UTC (7pm CT in
summer, 6pm in winter) and every other scheduled wake is earlier in that day,
so Paul only ever spends what the others left. Before waking the model, code
holds the run when less than `paul_min_left` (default $0.50, settable in
`tasks/limits.json`) of today's allowance is left.

**Building a site is the most expensive job in this ecosystem.** Planning
numbers from `scripts/cost-estimate.py` (20-30 turns) put a Sonnet-class
cycle at roughly $2-4, against a whole-ecosystem cap of $1 a day. The dry
cycle measures the real number (`last-run.py paul`); decide the model and
the cap from that, not from this paragraph.

## Files

    _paul-agents-header.md   Paul's instructions (AGENTS.md is generated from it)
    state/sender.json        your details             state/credentials.env   VERCEL_TOKEN
    state/contacted.csv      every business pitched or excluded - never trimmed
    state/deployments.csv    every preview, its teardown date and status
    state/builds.jsonl       every finished cycle (layouts, outcomes)
    state/current-cycle.json the cycle in progress   state/last-run.txt  Paul's line
    work/                    Paul's build in progress (an interrupted one resumes)
    sites/<slug>/            the site as deployed      outbox/<slug>/  pitch + screenshots
    reports/<date>-<slug>.md facts with sources, checks, photo sources
    MEMORY.md                one line a cycle, ~2KB; older lines move to memory-archive.md

## Pausing it

    systemctl disable --now paul-cycle.timer
