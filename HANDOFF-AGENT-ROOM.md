# Handoff: building an "Agent Room" (a CoreOps-style multi-AI builder) for this ecosystem

Written 2026-10-14 by the Claude session that built most of this repo, for
a new chat that knows nothing about it. Read it top to bottom once; the
rest of the repo will make sense after.

- This repository, on the branch everything lives on:
  https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- This note as raw text (easiest for another AI to read):
  https://raw.githubusercontent.com/cljxs/cljxs/claude/ai-agent-ecosystem-setup-0280zz/HANDOFF-AGENT-ROOM.md
- The two files every session must read before changing anything:
  [CLAUDE.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md) (how to change things without breaking them) and
  [BACKBONE.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/BACKBONE.md) (the task queue and its API).

The repository is **public**. Nothing secret is in it and nothing secret may
ever be put in it: no API keys, no tokens, no passwords, no IP addresses, no
email addresses, no live data from the agents.

---

## 1. What the owner wants to build

The owner saw someone else's tool and wants one like it. In that person's
words:

> "a personal tool which connects my Kimi, DeepSeek and ChatGPT ... essentially
> a Discord/Slack for my AIs to talk and plan and look at stuff it's building.
> I have a SaaS idea, I give it to Kimi, it structures the plan, then DeepSeek
> and Codex review it and give suggestions, then it gets sent to Codex to build
> each phase properly, and then last to DeepSeek for bug checks ... so it's
> fully autonomous, I walk away and come back to functioning software."

Their screenshots (a tool called **CoreOps**) showed:

- **Top bar:** a project picker ("RankAbove Tools"), green status pills for
  each connection (**OpenRouter**, **Codex CLI**, **Local**), a "Real mode"
  badge (as opposed to a dry run), a "Usage OK" badge, a theme switch, and a
  **New Run** button.
- **Left menu:** Overview, Control Center, Projects, **Agent Room**, Agents,
  Collaboration, Runs, Tasks, Memory, Usage, Settings, then a list of
  projects ("Family Dinner Planner", "Agentic World").
- **Overview:** counters (2 active projects, 1 active run, 5 open tasks, 0
  completed today), a Recent Activity feed ("OpenRouter Orchestrator created
  project plan", "... delegated 8 tasks", "Codex Builder started build run"),
  an **Active Agents** list with a role and online/busy state for each
  (OpenRouter Orchestrator - Orchestrator; Codex Builder - Code Executor;
  OpenRouter Reviewer - Reviewer; OpenRouter Utility - Prompt Improver;
  Memory Agent - Local Archivist), and Pinned Projects with a percent done.
- **Durable Runner:** "Runs entirely in the server process, not this browser
  tab - closing this tab (or the whole browser) does not stop it. Uses Codex
  CLI." A status ("running · Codex CLI · 3/13 completed"), a **Stop** button,
  a list of phases each marked done / in-progress / todo (Phase 1A
  foundation, 1B identity and roles, 1C audit and jobs, 1D tests and CI,
  then Phases 2-10 of features), and a collapsible "Recent events (46)".

So the shape is:

    owner's idea
      -> PLANNER (Kimi) writes a phased plan
      -> REVIEWERS (DeepSeek + GPT/Codex) critique it; the plan is revised
      -> BUILDER (Codex CLI) builds one phase at a time, in a real code folder
      -> BUG CHECKER (DeepSeek) reviews each phase; failures go back to the builder
      -> repeat until every phase is done
    all of it visible as a chat ("Agent Room") and a run board, and it keeps
    going when the browser is closed.

---

## 2. Who the owner is, and how to work with them

- **They are not a programmer.** Explain in plain words. Never assume they
  know what a timer, a port, a commit or an API is; say what it does.
- **They work from an iPad**: Termius (a terminal app) to reach the server,
  and Safari for the web pages. So:
  - **Every command you give must be ONE line.** The terminal flattens
    multi-line pastes and breaks them. No heredocs, no multi-line `python3 -c`.
  - Pages must work on a tablet and a phone.
- **They like to choose.** Before building anything sizeable, list your
  concerns and offer 2-4 clear options with a recommendation; they will pick.
  They have often run an idea past another AI first and want your honest view
  of it.
- **Money matters to them.** They ask "does it cost anything?" about most
  things. Say what something costs per day/month before building it, and
  build caps in.
- They expect, after each piece of work: tested, committed and pushed to the
  branch above, then the single-line commands to run on the server.
- Be honest about what you checked. This repo's house rule (CLAUDE.md) is to
  say **verified** (you ran it and read the output) or **believed** (it should
  work) - every bad surprise here came from "believed" presented as
  "verified".

---

## 3. What already exists: "the ecosystem"

An ecosystem of AI agents, each doing one job on a schedule, on **one
Ubuntu server** (a DigitalOcean "droplet", $12/month). The repo is cloned on
the server at `/root/ecosystem`. Every script resolves that root from
`ECOSYSTEM_ROOT` or its own location - nothing hardcodes a path.

### The agents

| Agent | What it does | Model? |
|---|---|---|
| **Belfort** | paper-trades stocks (practice money): the model decides at 9:35 and 15:55 Eastern each trading day, and code checks exits at 12:35 | yes |
| **Bolt** | paper-trades crypto every 10 minutes | **no** - pure code |
| **Ace** | places paper sports bets, and grades them against closing lines | yes |
| **Peter** | NFL player-prop forecasts; a Grade button scores them after games | no (script) |
| **Scout** | researches the Etsy market and proposes product ideas for the owner to approve | yes |
| **Emily** | turns an approved idea into a product: artwork, a Printify product, listing text. **Never publishes** - the owner presses Publish | yes |
| **Paul** | finds local businesses with weak websites, builds a preview site for each the owner approves, drafts a pitch. **Never contacts anyone** | yes |
| **Fury** | writes the morning briefing | no - pure code |
| **GM** | a "general manager" brief that proposes jobs for the owner to approve | yes |
| **Clip** (`clipper/`) | cuts permitted videos into captioned short clips | partly |

New this week (the Shop): `scripts/tiktok-kit.py` makes one ready-to-post
TikTok kit a day for an Etsy listing; `scripts/store-board.py` shows every
product's profit per sale; `scripts/scene.py` makes AI pictures of the real
product (paid, through OpenRouter, checked against the real photo).

### How an agent runs

1. A **systemd timer** (`deploy/<agent>-cycle.timer`) wakes it on a schedule.
   systemd is Linux's built-in service manager; a timer is a scheduled job.
2. The timer runs a **service** (`deploy/<agent>-cycle.service`), which runs
   **OpenClaw** - the agent runner installed on the server - like:
   `openclaw agent --agent <name> --message "scheduled cycle" --session-id "wake-<name>-<time>" --timeout 900 --json`
3. The agent reads its instructions, `agents/<name>/AGENTS.md`, does its work
   by **calling Python scripts** in `scripts/`, writes a report, and signs off.
4. **A verifier script judges the cycle** by what is on disk, not by what the
   agent says it did.

Some work goes through the **task queue** instead (BACKBONE.md): a SQLite
database `tasks/queue.db`, an API at `http://127.0.0.1:3001/tasks`, and
`scripts/task-dispatcher.py`, which every 2 seconds claims the next pending
task for an idle agent and launches it with a fresh session. Agents finish a
task with `POST /tasks/:id/complete`. Spend caps refuse new tasks with 429.

### Where things are

    agents/<name>/        AGENTS.md (generated - do not edit), _<name>-agents-header.md
                          (edit THIS), MEMORY.md, state/, reports/, data/
    agents/<name>/NO_MODEL   marks an agent that is plain code (Bolt, Fury)
    scripts/              everything that decides or calculates, in Python, standard library
    deploy/               systemd .service/.timer files; scripts/deploy.sh installs them
    mission-control-api/  "the Command Deck": the web app (Node.js, Express, port 3001)
    clipper/              the video clipper
    tests/                python3 -m unittest - real captured fixtures in tests/fixtures/
    tasks/limits.json     the money caps

### Changing an agent

Edit `agents/<name>/_<name>-agents-header.md`, then on the server
`scripts/deploy.sh <name>` - it pulls, rebuilds AGENTS.md, installs units and
restarts timers. Model settings: `openclaw config get agents.entries.<name>.model`;
change with `scripts/set-agent-model.sh <name> <slug> --apply`, which checks
the model name exists first (a wrong name fails silently hours later).

### The Command Deck (the web app)

`mission-control-api/server.js`, Express on port **3001**. It listens on the
server itself (loopback) and on the server's **Tailscale** address - Tailscale
is a private network between the owner's devices, so the Deck is reachable
from the iPad but not from the internet. Pages:

    /dashboard   the Deck (tabs: Overview, Peter, Markets, Bolt, Shop)
    /village     a pixel-art village (Phaser): each agent has a house;
                 tapping a house opens that agent's page in a frame
    /markets /peter /bolt /paul /shop   one page per agent

How the JS side is written, and the new tool should copy it: **the JavaScript
holds no rules.** Each route runs the Python script that owns the answer with
`execFile` and an argument array (never a shell string), validates every
name with a strict regex before it reaches a path or process, and returns the
script's JSON. Long jobs run in a single "job slot" (one at a time, 409 when
busy) and the page polls `/api/<thing>/job`. Examples: `mission-control-api/shop.js`,
`paul.js`, `props.js`.

Existing endpoints a new tool could read (all on the server, port 3001):

    GET /health                      liveness
    GET /api/dashboard               every agent's state, last run, money, positions
    GET /api/townhall                today's AI spend against the budget, problems
    GET /tasks  /tasks/stats  /tasks/:id     the task queue (POST /tasks to add one)
    GET /api/reports/:agent  /api/reports/:agent/:file   agents' reports
    GET /api/belfort/board  /api/bolt/board  /api/ace/ledger  /api/props/week
    GET /api/emily/builds  /api/scout/ideas  /api/paul  /api/shop/tiktok  /api/shop/board  /api/clip

### Free, always-safe health checks (no AI woken, no money)

    python3 scripts/preflight.py      could each agent run? (files, units, model, credit)
    python3 scripts/health-check.py   did it run? (systemd vs disk vs Deck)
    python3 scripts/budget.py         today's AI spend vs the cap

### Money

- Model calls go through **OpenRouter** (a service that sells access to many
  AI models - GPT, Gemini, DeepSeek, Kimi, Claude - through one account and
  one API). Most agents share one key. **Paul has his own key with a $2 daily
  limit** (owner, 2026-10-14), so his work never eats the shared day;
  `python3 scripts/paul.py preflight` shows what is left on it.
- **The owner has no separate DeepSeek or Kimi account that this repo knows
  of** - none is referenced anywhere here. None is needed to start: the
  OpenRouter account already reaches DeepSeek and Kimi (Moonshot) models, so
  the new tool can use a NEW OpenRouter key of its own with its own daily
  limit, the way Paul's was set up (agents/paul/README.md, step 3). Codex
  CLI is different: it needs an OpenAI account or a ChatGPT plan.
- `tasks/limits.json` holds `daily_ai_budget` (**$1.50 a day**, the owner's
  number), plus task caps and the Shop's `tiktok_daily_ai` ($0.50) and
  `tiktok_leave_for_emily` ($0.50).
- `scripts/budget.py` reads OpenRouter's **own** count of today's spend (the
  day is UTC - it turns over at 8pm Eastern) - nothing estimates it.
- The OpenRouter key itself has a daily credit limit, the hard stop.

**A multi-AI builder will blow through $1.50 a day in minutes.** Codex-style
building is hundreds of turns. Before building, agree with the owner how it
is paid for: its own key and its own cap, or a ChatGPT plan (Codex CLI can
sign in with a ChatGPT subscription instead of per-call billing - check
current terms).

---

## 4. How the new tool would reach the ecosystem

Pick deliberately; this is the decision with the most consequences.

**Option A - a separate tool that only reads (safest start).** It runs as its
own service on the server (a new port, e.g. 3002, also bound to loopback +
Tailscale only) and reads the ecosystem through the Deck's GET endpoints
above and the files under `/root/ecosystem` (reports, MEMORY.md, state). It
can show agents' status in its Agent Room without being able to break them.

**Option B - it builds things, in its own folders.** New projects (a SaaS
idea) are built in their own git repositories or folders, e.g.
`/root/builds/<project>/`, never inside `/root/ecosystem`. The builder (Codex
CLI) works there. This is what the CoreOps screenshots do, and nothing it
gets wrong can stop Belfort trading.

**Option C - it works on the ecosystem itself.** It edits this repo. Then it
must work on a **separate branch or git worktree**, run the tests
(`python3 -m unittest discover -s tests -b`), and **never** deploy to the
live agents without the owner approving - because a bad change to
`scripts/` reaches every agent at its next wake. Merging and running
`scripts/deploy.sh` stays a human step.

**Option D - it sends work to the existing agents** through the task queue
(`POST http://127.0.0.1:3001/tasks`, see BACKBONE.md for the fields, caps and
the dedupe key). Only jobs an agent already has instructions for - see
`scripts/gm.py` `TASKS` for the list that exists - otherwise a model wakes
with no idea what to do.

Whichever option: it should appear in the existing Deck too - a page (e.g.
`/agentroom`) linked from `/dashboard`, and a house in the village
(`mission-control-api/public/village.html`: `THEMED`, `OUTFITS`,
`PLOT_ORDER` and `FRAMES` are where a house, its look, its plot and its page
are declared; Bolt's and Paul's were the latest added - copy them).

Credentials for any new provider (Kimi, DeepSeek, OpenAI) go in a file on the
server with mode 600, like `agents/<name>/state/credentials.env`, typed in by
the owner in the server's terminal. **Never ask the owner to paste a key into
the chat. Never print, echo or log one.** `scripts/check-credentials.py`
shows enough to diagnose without revealing them.

---

## 5. The lessons this ecosystem paid for - apply them to the new tool

1. **A model will claim it did work it did not do.** Every agent here is
   judged by a verifier script that reads the disk, not by its own report.
   For a builder: "phase done" must mean *the tests ran and passed* and the
   files exist - checked by code - never "the AI said it's done". Two AIs
   reviewing each other is not a check; they share blind spots.
2. **Anything a script can compute exactly belongs in a script.** Models
   asked to do arithmetic eventually invent numbers. Prices, costs, counts,
   progress percentages: code.
3. **One fact, one place.** When two pieces of code decide the same thing
   they drift, and an agent acts on the wrong one. Import, don't restate.
4. **Cost scales with turns, not tokens.** Every turn re-reads the whole
   prompt. A 12-turn run on a dearer model beats a 68-turn run on a cheap
   one. A loop is the most expensive bug there is - cap turns and dollars
   per run, per phase and per day, checked *before* each call.
5. **A wrong model name fails silently.** Check a model slug exists
   (OpenRouter: `GET https://openrouter.ai/api/v1/models`) before using it.
6. **Durable means systemd, not a browser tab.** Long runs here are systemd
   services that survive the iPad going to sleep. A fresh session id per run
   (reused sessions caused runaway loops). On restart, anything stuck
   "in progress" is reset, not left hanging.
7. **Outward actions wait for the owner.** Emily never publishes; Paul never
   contacts a business; Clip does not download from YouTube; the TikTok kit
   never posts. A builder should never deploy, publish, send email or spend
   beyond its cap on its own. "Fully autonomous" should mean it builds and
   tests on its own and stops at a gate for the owner.
8. **Dry run first.** Every new outward-facing thing here started in a
   dry-run mode (CoreOps's "Real mode" badge is the same idea).
9. **Tests of real data, not imagined data.** Every parser bug here was a
   wrong guess about a format. Save a real response as a fixture and test
   against it. Each test here names the bug it guards and was checked by
   putting the bug back and watching the test fail.
10. **Never `pkill -f` a pattern** on the server - it has killed the session
    running it, twice. Stop processes by their exact PID.
11. **Headless Chrome will not lay a page out narrower than 500px** - a
    "phone screenshot" at 390px is really a cropped 500px page. Use an iframe
    of the true width to test a phone layout.
12. **Times are Eastern, stored as UTC**; `scripts/et_time.py` owns the clock.

---

## 6. A suggested design, if the owner agrees

- **Providers as adapters**, one file each: OpenRouter (already used - it
  can serve DeepSeek, Kimi and GPT models through the one key), Codex CLI
  (runs on the server as a program; the "builder"), and a local one for the
  memory/archivist role. Check current model names and prices live:
  [OpenRouter models](https://openrouter.ai/models).
- **Roles, not models**, in a settings file the owner can change: Planner,
  Reviewer(s), Builder, Bug-checker, Archivist. The model behind each role is
  a setting.
- **A run is a state machine in code**, stored in SQLite: plan -> review ->
  revise -> for each phase (build -> test -> bug-check -> fix, at most N
  rounds) -> done or blocked. Code moves it between states based on checks;
  models only produce text and code.
- **The Agent Room is a message log**: every model message, tool result,
  test run and cost, in order, per project - which is also the audit trail.
- **The runner is a systemd service** with a single job slot, a Stop button
  that really stops it, and a per-run dollar cap checked before each call.
- **Gates for the owner**: approve the plan before building; approve before
  anything leaves the server.
- **Each project in its own git repo/folder**, so every phase is a commit
  that can be looked at and undone.

---

## 7. Questions to ask the owner before writing code

1. Which AI accounts do you actually have? OpenRouter is known (it already
   covers DeepSeek, Kimi and GPT models). A ChatGPT plan for Codex CLI?
2. What may it spend a day, on its own new OpenRouter key - separate from the
   shared $1.50 and Paul's $2?
3. Should it run on the same $12 droplet (check memory with `free -h` -
   builds and Codex can starve the trading agents) or a separate machine?
4. What will it build: new projects only (Option B), or also changes to this
   ecosystem (Option C)?
5. How autonomous: stop for approval after the plan, after each phase, or
   only at the end?
6. Should it have a house in the village and a tab in the Deck?

---

## 8. Links

- This repo (branch): https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- CLAUDE.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md
- BACKBONE.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/BACKBONE.md
- An example of a page + API pair to copy: `mission-control-api/shop.js` and `mission-control-api/public/shop.html`
- An example of a plain-code agent: `scripts/bolt.py`, `deploy/bolt-cycle.*`, `agents/bolt/`
- OpenRouter docs: https://openrouter.ai/docs - models: https://openrouter.ai/models
- OpenAI Codex CLI: https://github.com/openai/codex
- DeepSeek API docs: https://api-docs.deepseek.com
- Kimi / Moonshot platform: https://platform.moonshot.ai
- Tailscale docs: https://tailscale.com/kb
- systemd timers: https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html
- Claude Code on the web (how these cloud sessions work): https://code.claude.com/docs/en/claude-code-on-the-web

Model names and prices change weekly; treat every one in this note as
something to check, not a fact.

---

## 9. Where the previous chat left off (so nothing is lost)

- **Paid AI scenes for TikTok kits** were pushed but **not yet run against
  the real API** (no key in the sandbox). The first droplet run is the real
  test:
  `python3 scripts/scene.py try "<a product photo>" --thing "tote bag" --n 1 --out /root/ecosystem/agents/emily/shop/scene-test`
- Ideas the owner was offered and has not answered: an AI motion clip for
  the kit video; a "pitch only" option for Paul; a hand-over step after Paul
  sells a site (move it to a host the client owns - Vercel's free plan is
  non-commercial).
- Every commit ends with the session's attribution lines; no model names go
  in commits, code or pull requests. Pull requests only when the owner asks.
