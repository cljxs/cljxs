# Handoff: Scout and Emily (the Etsy shop) - and the Shop around them

Written 2026-10-21 by the Claude session that built most of this repo, for a
new chat that will look after the owner's Etsy print-on-demand shop:
**Scout** (finds what sells and proposes product ideas), **Emily** (turns an
approved idea into a product draft), and the **Shop** tools built around
them (the profit board, daily TikTok kits, AI product scenes, listing videos).
Read it top to bottom once; the rest of the repo will make sense after.

- This repository, on the branch everything lives on:
  https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- This note as raw text (easiest for another AI to read):
  https://raw.githubusercontent.com/cljxs/cljxs/claude/ai-agent-ecosystem-setup-0280zz/HANDOFF-SCOUT-EMILY.md
- Read before changing anything: [CLAUDE.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md)
  (how to change things without breaking them),
  [ROUTING.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md)
  (which files, anchors and tests each request touches - start there instead
  of reading the repo) and [BACKBONE.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/BACKBONE.md)
  (the task queue that wakes Emily). In a Claude Code session on this repo,
  CLAUDE.md and ROUTING.md load by themselves.

The repository is **public**. Nothing secret is in it and nothing secret may
ever be put in it: no API keys, tokens, passwords, IP addresses, email
addresses, or live data (ideas, builds, scans, sales are gitignored and live
only on the server).

**Two gates are the owner's, always:** Scout only *proposes* (the owner
approves), and Emily only makes *drafts* (the owner publishes). Nothing here
publishes a listing, sets a price, or posts to TikTok. Keep it that way.

---

## 1. Who the owner is, and how to work with them

- **Not a programmer.** Plain words; say what a timer, a commit or a draft
  does. Outcome first, short.
- **Works from an iPad**: Termius (terminal) to reach the server, Safari for
  the web pages.
  - **Every command is ONE line, in a code block to paste.** The terminal
    flattens multi-line pastes, and the iPad keyboard turns a typed `--` into a
    long dash. A button on the Deck beats a command.
  - Pages must work on a tablet and a phone.
- **They judge by looking.** They send screenshots of a product image, a kit or
  a card that is wrong ("there are 2 straps in this picture, people will know
  it's fake"). Treat each as a bug report: fix it *and* add the check or rule
  that stops it coming back.
- **They like to choose.** 2-4 options with a recommendation for anything big.
- **Money matters.** Say what something costs before building it.
- After each piece of work: tested, committed and pushed to the branch above,
  then the single-line commands to run on the server.
- **Say "verified" (you ran it and read the output) or "believed" (it should
  work).** Image and model output cannot be seen from a cloud session without
  keys - say so plainly when you have not seen the real result.
- When they decline something, do not raise it again unprompted.

---

## 2. The ecosystem, briefly

One Ubuntu server (a DigitalOcean droplet), repo cloned at `/root/ecosystem`.
Agents wake on **systemd timers** (Scout) or from the **task queue** (Emily:
`tasks/queue.db`, `scripts/task-dispatcher.py` claims a task every 2 seconds
and wakes the agent). Agents do their work by calling Python scripts in
`scripts/`, and **code judges the work by what is on disk**, never by what the
agent says. Other agents share the server, the budget and the web app: Ace,
Belfort, Bolt (paper trading), Peter (sports props), Paul (local-business
websites), Clip (video clips), Stan, the Agent Room, and Fury, the manager (the GM was retired into him on 2026-10-30).
ROUTING.md has a row for each.

**Which engine wakes a model** is `tasks/engines.json`, read only by
`scripts/wake.py`. **Scout runs on Sonnet (since 2026-10-28) and Emily on Haiku, on the owner's Claude plan**
(`claude -p`; Scout up to 40 turns, Emily 60), so their thinking spends no
OpenRouter money; if the plan refuses a wake before it starts, wake.py falls
back to OpenClaw on OpenRouter. (The agents' READMEs still name older
OpenRouter models - engines.json is the truth.) **Images cost real money**:
they go through OpenRouter (`google/gemini-2.5-flash-image` via
`scripts/emily-assets.py`). `tasks/limits.json`: the shared OpenRouter day is
**$1.50** (`daily_ai_budget`); TikTok scenes may spend **$0.50** a day
(`tiktok_daily_ai`) and must leave **$0.50** for Emily (`tiktok_leave_for_emily`).
`python3 scripts/budget.py` reads OpenRouter's own count of today's spend.

**The Command Deck** (`mission-control-api/`, Node/Express, port 3001, reached
from the iPad over Tailscale). The JavaScript holds no rules: each route runs
the Python script that owns the answer and returns its JSON. A changed
`public/*.html` needs only a reload; a changed `.js` module needs
`python3 scripts/restart-deck.py` (never `systemctl restart` - it kills jobs
the Deck started, like an approval drawing a design).

---

## 3. Scout - what sells, and ideas for it

**Proposes only.** Writes idea drafts; code files the ones with evidence; the
owner approves. Approving is what creates Emily's task.

| Timer (Central) | Runs | Costs |
|---|---|---|
| `scout-niches` 06:30 | `scripts/niche-scan.py run`: re-measures the oldest niches on Etsy (supply, favourites, price, and **sales** - how many of the top 10 listings had a review in the last year, a floor on purchases), adds new niches from tags selling listings use | nothing (Etsy API) |
| `scout-cycle` 08:00 | `scripts/scout-cycle.sh` -> wake.py -> Scout; first a Reddit block (`scripts/reddit_search.py scout`: what people say around his top 4 phrases, through web search, once a day - colour for an angle, never a phrase; owner 2026-10-26; OFF the same day - Claude's web search cannot reach reddit.com) | one plan wake + one small web-search question |

**The evidence rule.** Each idea line is `phrase | title | product | angle`
in `state/drafts.txt`. The **phrase must be a measured one** (`state/scans/`):
code refuses an unmeasured phrase, a scan older than 14 days, a phrase that
scored zero, one with no recent sales, or anything naming someone else's
property (`scripts/ip-check.py`). Code adds the evidence block (supply,
favourites per day, typical price, the market's own tags) - **no number in an
idea was typed by a model.** The title and angle are Scout's, and the angle may
only say the date and who it is for - never "popular", "trending", "sells well"
or any percentage.

What the scans settled (do not re-litigate from opinion): seasonal phrases
are dead ends; evergreen phrases (`water bottle stickers`, `laptop stickers`)
are alive at far higher competition; **apparel sells** when measured in sales
(`bookish sweatshirt`, `funny dad shirt`, `hiking shirt`), while mugs barely
did. Ideas are ranked by money (reviews x typical price) and spread across
products. Small formats only (the image models make about 1024px): stickers,
mugs, small prints, totes, phone cases, small chest prints on apparel; bold
graphic art on a plain background (it is cut out before printing).

Reviewing: the Scout house in the village (`drawIdeas`: each idea with its
evidence, Approve / Reject with a reason), or
`python3 scripts/scout-review.py list | approve <id> | reject <id> "<why>"`.
Verdicts go to `agents/emily/state/lessons.md`, which Scout reads so a
rejected direction does not come back. When too many ideas are waiting, code
does not wake Scout at all. Scripts also: `scripts/scout-ideas.py` (files the
drafts), `scripts/market-scan.py`, `scripts/trend-probe.py`, `scripts/etsy-probe.py`.
Rules for the model: `agents/scout/_scout-agents-header.md`.

---

## 4. Emily - an approved idea into a Printify draft

**Drafts only - never publishes.** Task-driven: no timer, costs nothing idle.

The chain: approve (Deck or `scout-review.py approve`) -> `scripts/emily-new-build.py`
draws the artwork first (`emily-assets.py`, with the evidence in the prompt)
and queues a task -> the dispatcher wakes Emily -> Emily writes the listing
copy (`scripts/emily-listing.py listing`, never by hand: title <=140 chars,
<=13 tags of <=20 chars) and records the build (`emily-listing.py build`) ->
`scripts/emily-finish.py` creates the Printify product **unpublished** (plain
code: blueprint, provider, variants) -> `scripts/emily-verify.py` fails the task
unless real artwork and a valid listing exist. The owner sets every price in
Printify and presses Publish there, which creates the Etsy listing (the shop
is connected inside Printify; no Etsy API is needed for that).

Rules that cost money to learn: Emily never draws a `.png` herself (a model
once wrote 49-byte text files named `design.png`); the art is the design, not
a photo of a product (`scripts/knockout.py` refuses a photograph and cuts out
the background); no trademarked IP (`scripts/ip-check.py`, exit 2 = refuse);
every design original, never a copy of a listing; **prices are the owner's**.
`scripts/blanks.py` warns when the selling listings use a different blank
(shirt, mug) than Emily's product; `scripts/disclosures.py` holds the
disclosures every Etsy listing must carry; embroidered products are their own
blueprints (`embroidered-...`).

Keeping builds moving: when the art cannot be drawn (most often the day's
image money is spent), the build is marked WAITING FOR ART instead of being
sent to Emily; the `redraw-art` timer (hourly 08:07-22:07 Central,
`emily-finish.py --redraw`) draws it and then sends it on. **Try again** on a gallery card
re-runs a failed draft; `scripts/emily-build.py list | remove | restore`
(remove archives, never deletes, and refuses while a Printify product points
at it). Emily's gallery is in her village house (`loadEmilyBuilds`, `retryBuild`).
Printify token: `agents/emily/state/credentials.env` via
`python3 scripts/set-credential.py`; check with `python3 scripts/check-credentials.py`.
Rules for the model: `agents/emily/_emily-agents-header.md`.

---

## 5. The Shop around them (`/shop` page)

`mission-control-api/shop.js` + `public/shop.html`:

- **Store board** (`scripts/store-board.py`): every build, what it earns per
  sale (price, Etsy's cut, Printify's cost and postage, what is left - from the
  one fee table in `emily-printify.py`), and its concerns worst first
  (LOSS / FIX / CHECK / GOOD), with Approve, Revise (a note, keep or redraw the
  art) and Kill.
- **Store report** (`scripts/store-report.py`): the live listings' numbers and an
  audit of what Etsy would refuse; needs the Etsy key (two values, never printed).
- **TikTok kits** (`scripts/tiktok-kit.py`, timer `tiktok-kit` 09:48 Eastern):
  one ready-to-post kit a day for a live listing - carousel slides, a short
  video, captions and hashtags - **never posted by code**; the owner posts and
  marks it. Since 2026-10-20 each kit looks different: places chosen
  least-recently-used per product (12-16 each), a different person in each
  people scene (10), and six video styles rotated (`scripts/listing-video.py`
  `STYLES`; Etsy listing videos keep the original look).
- **AI product scenes** (`scripts/scene.py`): the real product cut out of its
  mockup and set into a scene; paid scenes draw the product into a place and
  are checked against the real design (`design_problems`); a tote shows one
  pair of straps (2026-10-20 fix).
- `scripts/emily-banner.py` draws the shop banner.

The owner's Etsy shop is linked from their TikTok (@vintageloomtreasures).
TikTok will not make a bio link clickable until 1,000 followers or a Business
account (which needs tax details the owner does not have), so the bio names
the shop instead.

Tests: `tests/test_shop.py` (shop, kits, scenes, board), `tests/test_scout_scan.py`,
and `tests/test_parsers.py` classes for Scout, Emily, Printify, stickers, mugs
and listings (grep `class Scout`, `class Emily`, `Printify`). Run before a push:
the fast suite `python3 -B -m unittest tests.test_parsers -b`, the area's own
file, and `python3 scripts/check-secrets.py`; anything touching money or
Printify gets the full `python3 -B -m unittest discover -s tests -b` too
(about 40 minutes - run it in the background).

---

## 6. The rules this ecosystem paid for (apply them here)

1. **A model will claim work it did not do.** `emily-verify.py` judges the
   files; a build's report is read, never trusted.
2. **Anything a script can compute exactly belongs in a script**: evidence
   numbers, prices, fees, profit, listing JSON, the Printify draft.
3. **One fact, one place.** The fee table lives once (`emily-printify.py`); the
   board and the audit share `store-report.py`'s checks. Import, never copy.
4. **Real data in tests** (`tests/fixtures/`: real Etsy and Printify replies).
   Every parser bug here was a format guess. Each test names its bug and was
   checked by putting the bug back.
5. **Outward actions wait for the owner**: publishing, prices, posting.
6. **Credentials never leave the server** (`scripts/set-credential.py`, typed by
   the owner). Never ask for a key in chat; never print one.
7. **Changing an agent's instructions**: edit `agents/<name>/_<name>-agents-header.md`,
   then `scripts/deploy.sh <name>` on the server.
8. **Times are Eastern, stored as UTC** (`scripts/et_time.py`).
9. **Never `pkill -f` a pattern** on the server; stop processes by PID.
10. **Every commit ends with the session's attribution lines; no model names in
    commits, code or pull requests. Pull requests only when the owner asks.**

---

## 7. Where things stand (2026-10-21)

- **TikTok kit variety** (places, people, video styles) shipped 2026-10-20 on
  `git pull`; the first kits after it are the real test - ask the owner how
  they look.
- **Paid AI scenes** have been checked by tests, not yet by eye on many real
  products - each new product type is worth a look.
- **Offered, not answered:** the kit's video caption says "Link in bio", but
  the bio link is not clickable yet; it could say "Find us on Etsy:
  VintageLoomTreasures" until it is.
- Approved ideas wait in Scout's house; when Scout stops proposing, it is
  usually because too many are waiting - the owner approving or rejecting is
  the fix.

---

## 8. Links

- Repo (branch): https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- CLAUDE.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md
- ROUTING.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md
- BACKBONE.md (task queue): https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/BACKBONE.md
- The agents' rules: `agents/scout/_scout-agents-header.md`, `agents/emily/_emily-agents-header.md`; install notes in each agent's README.md
- Printify API: https://developers.printify.com - Etsy Open API v3: https://developers.etsy.com/documentation
- OpenRouter models and prices: https://openrouter.ai/models
- Claude Code on the web: https://code.claude.com/docs/en/claude-code-on-the-web
