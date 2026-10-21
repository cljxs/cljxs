# Routing table - where things live

Read this before searching. Find the row for the request, open only the files
it names, and **grep for the anchor** (a function or constant name) instead of
reading a big file whole. Line numbers drift; anchors do not.

Big files - never read whole:
`mission-control-api/public/village.html` (~4,200 lines), `scripts/paul.py`
(~3,900), `tests/test_parsers.py` (220+ classes), `clipper/cli.py`.

`tests/test_routing.py` fails when a path here stops existing, or when a new
script, Deck module, page or unit is not listed - so update this file with
the change that needs it.

## Agents

| Agent | What it does | Runs by (timer, ET unless noted) | Code | Deck page / API | Tests |
|---|---|---|---|---|---|
| Ace | sports bets (NBA/NFL/college) | `ace-cycle` 11:30, 23:30; `ace-fetch` every 30 min; `ace-close` | `scripts/ace-cycle.sh`, `scripts/ace-fetch.py`, `scripts/ace-book.py` (money), `scripts/ace-clv.py`, `scripts/ace-sharp.py`, `scripts/ace-judge.py`, `scripts/ace-focus.py`, `scripts/ace-baselines.py`, `scripts/ace-verify.py` | `/ace` `mission-control-api/ace.js`, `mission-control-api/public/ace.html` | `tests/test_ace_board.py`; test_parsers `Ace*` |
| Peter | NFL/NBA player props, weather, defense vs position | `peter-defense` 06:40 | `scripts/props-forecast.py`, `scripts/nfl-defense.py` | `/peter` `mission-control-api/props.js`, `mission-control-api/public/peter.html` | `tests/test_defense.py`; test_parsers `Peter*`, `Props*` |
| Belfort | paper stock trader | `belfort-cycle` Mon-Fri 09:35, 15:55; `belfort-fetch` /10 min; `belfort-exits`; `belfort-review` monthly | `scripts/belfort-cycle.sh`, `scripts/belfort-fetch.py`, `scripts/belfort-trade.py` (book, exits, `book_gap`), `scripts/belfort-learn.py`, `scripts/belfort-verify.py`, `scripts/indicators.py`, `scripts/market-scan.py` | `/markets` `mission-control-api/belfort.js`, `mission-control-api/public/markets.html` | test_parsers `Belfort*` |
| Bolt | crypto momentum trader, plain code (+ news book asks the plan) | `bolt-cycle` every 10 min | `scripts/bolt.py` (`exit_reason`, `stop_for`, `trailing`, NEWS_*) | `/bolt` `mission-control-api/bolt.js`, `mission-control-api/public/bolt.html` | test_parsers `Bolt*` |
| Coach | trading journal, daily analysis, Sunday proposals (never changes rules) | `coach-daily` 00:20; `coach-weekly` Sun 20:10 | `scripts/coach.py` | `/coach` `mission-control-api/coach.js`, `mission-control-api/public/coach.html` | `tests/test_coach.py` |
| Scout | Etsy niche research, ideas for Emily | `scout-cycle` 08:00 CT; `scout-niches` 06:30 CT | `scripts/scout-cycle.sh`, `scripts/niche-scan.py`, `scripts/scout-ideas.py`, `scripts/scout-review.py`, `scripts/trend-probe.py`, `scripts/ip-check.py`, `scripts/etsy-probe.py` | `mission-control-api/scout.js`; village `drawIdeas`, `loadScoutScan` | `tests/test_scout_scan.py`; test_parsers `Scout*` |
| Emily | designs -> Printify drafts (never publishes) | task queue (`scripts/task-dispatcher.py`); `redraw-art` hourly | `scripts/emily-new-build.py`, `scripts/emily-assets.py`, `scripts/emily-finish.py` (`needs_art`, `stuck`, redraw), `scripts/emily-printify.py`, `scripts/emily-listing.py`, `scripts/emily-build.py`, `scripts/emily-verify.py`, `scripts/emily-banner.py`, `scripts/knockout.py`, `scripts/blanks.py`, `scripts/disclosures.py` | `mission-control-api/emily.js`; village `loadEmilyBuilds`, `retryBuild` | test_parsers `Emily*`, `Printify`/`Sticker`/`Mug` classes |
| Shop / TikTok kits | store board, Etsy report, daily TikTok kit (never posts) | `tiktok-kit` 09:48 | `scripts/store-board.py`, `scripts/store-report.py`, `scripts/tiktok-kit.py`, `scripts/scene.py` (places, people, paid scenes), `scripts/listing-video.py` (STYLES) | `/shop` `mission-control-api/shop.js`, `mission-control-api/public/shop.html` | `tests/test_shop.py` |
| Paul | local-business websites + pitches (never contacts anyone) | `paul-cycle` 16:45 CT; Studio buttons | `scripts/paul-cycle.sh`, `scripts/paul.py`, `agents/paul/templates/` | `/paul` `mission-control-api/paul.js`, `mission-control-api/public/paul.html` | `tests/test_paul.py`; test_parsers `PaulHasAWorkshop` |
| Clip (+ Spotter) | permitted video -> captioned shorts; YouTube research | `clip-pickup` /15 min | `clipper/` (see below) | `mission-control-api/clip.js`; village `openClipPanel`, `drawClip`, `drawPost`, `editSubtitles` | `tests/test_clipper.py`; test_parsers `ClipHasAStudio` |
| Stan | software pains from Hacker News | `stan-fetch` 06:12 CT; `stan-cycle` 06:22 CT | `scripts/stan-fetch.py`, `scripts/stan.py` | `/stan` `mission-control-api/stan.js`, `mission-control-api/public/stan.html` | `tests/test_stan.py` |
| Agent Room (Sylvester) | idea -> reviewed build plan | `agentroom-runner` | `scripts/agentroom.py`, `scripts/agentroom-guard.py`, `scripts/agentroom-builder-setup.sh`, `agentroom/roles.json` | `/agentroom` `mission-control-api/agentroom.js`, `mission-control-api/public/agentroom.html` | `tests/test_agentroom.py`, `tests/test_agentroom_build.py`, `tests/test_app_ai.py`, `tests/test_site.py` |
| Fury | collects the whole ecosystem's state | `fury-cycle` 08:30 CT | `scripts/fury-collect.py` | Deck dashboard | test_parsers |
| GM / Town Hall | morning facts -> proposals; knowledge | `gm-brief` 08:50 CT | `scripts/gm.py`, `scripts/knowledge.py` | `mission-control-api/townhall.js`; village `drawHall`, `drawGm`, `actOnProposal` | test_parsers `GMHarness`, `TheGM*`, `TheTownHall*` |

Each agent's rules: `agents/<name>/_<name>-agents-header.md` (AGENTS.md is
generated - edit the header, then `scripts/deploy.sh <name>`).

## One fact, one owner - import it, never restate it

| Question | Owner |
|---|---|
| What time / which wake slot is it (Eastern) | `scripts/et_time.py` |
| Which engine and model wakes an agent | `tasks/engines.json`, read only by `scripts/wake.py` (`wake.py ask` = one question, no tools) |
| One OpenRouter call and its cost | `scripts/or_chat.py` |
| Today's AI spend vs cap | `scripts/budget.py`; estimates `scripts/cost-estimate.py`; caps `tasks/limits.json` |
| Could an agent run / did it run | `scripts/preflight.py` / `scripts/health-check.py`; last wake `scripts/last-run.py` |
| Writing a memory line, the sign-off | `scripts/remember.py`, `scripts/signoff.py` |
| Task queue (create/complete) | `scripts/task.py`, `scripts/task-dispatcher.py`, `BACKBONE.md` |
| Credentials | `scripts/set-credential.py`, `scripts/check-credentials.py` (never print one) |
| What needs the owner on each Deck tab | `scripts/attention.py` -> `mission-control-api/attention.js` |
| Model slugs, model comparison | `scripts/set-agent-model.sh`, `scripts/model-trial.py` |
| A Claude plan login | `scripts/new-claude-login.py` |
| Provider error or agent error | `scripts/check-provider-error.py` |

## The Command Deck (Express, port 3001)

* `mission-control-api/server.js` - requires each `<area>.js` and calls its
  `register(app)`; serves each page at `app.get('/<page>'`. A new page = a
  module + a `public/<page>.html` + a route + village `FRAMES` and `NAV`.
* `mission-control-api/dashboard-data.js` - the dashboard's agent rows;
  `mission-control-api/db.js` - ROOT, the tasks DB; `mission-control-api/reports.js` - reports.
* Restart: `python3 scripts/restart-deck.py` (waits for jobs). Start script
  `scripts/mission-control-start.sh`. A changed Deck module needs a restart; a
  changed page under `mission-control-api/public/` does not.
* Village anchors in `mission-control-api/public/village.html`: `const NAV`, `const FRAMES`, `openFrame`,
  `renderFrame`, `avatarSrc` (character pictures), feed `last_run_utc`,
  `openPanel`, Scout `drawIdeas`, Emily `loadEmilyBuilds`, Clip
  `openClipPanel`/`drawClip`/`drawPost`/`clipCard`/`editSubtitles`/`fixSubtitles`,
  Town Hall `drawHall`. Shared front-end helpers: `mission-control-api/public/shared/`.

## Clipper (`clipper/`, its own `clipper/DESIGN.md`)

| Stage / feature | Module |
|---|---|
| CLI commands (`python -m clipper ...`) | `clipper/cli.py` (grep `def cmd_<name>`) |
| Settings and paths | `clipper/config.py` (`DEFAULTS`) |
| DB schema + migrations | `clipper/db.py` |
| Rights gate, sources | `clipper/rights.py` |
| Ingest, Dropbox pickup, link inbox, comments/pins | `clipper/ingest.py`, `clipper/dropbox.py`, `clipper/inbox.py` |
| Transcribe, spellings | `clipper/transcribe.py` |
| Moments, scoring, campaign lists | `clipper/moments.py`, `clipper/score.py`, `clipper/campaign.py` |
| Stages, recut, subtitles (`set_subtitles`, `recut`, `clip_ass`) | `clipper/pipeline.py` |
| Framing on faces (`shot_layout`, `crowd`) | `clipper/framing.py` |
| ffmpeg render, clean copy, `burn`, watermark | `clipper/render.py`, `clipper/media.py`, `clipper/cutout.py` |
| Captions (ASS) | `clipper/captions.py` |
| Title/caption/hashtags | `clipper/postcopy.py` |
| Approve, reject, delete, post; YouTube upload | `clipper/publish.py` (`delete`), `clipper/youtube.py` |
| Spotter (YouTube research, quota) | `clipper/trends.py` |
| Trimming a short clip to its moment | `clipper/trim.py` |
| What the Deck shows | `clipper/report.py` (`snapshot`) |
| Disk space: finished clips' videos deleted, `cleanup`, `report` | `clipper/storage.py` (`drop_clip_files`, `finished_sources`) |

## paul.py anchors

`cmd_brief` (FACTS block), `cycle_plan`, `run_checks` / `check_site` /
`reference_problems` (what check refuses), `cmd_finish` (deploy, records),
`prospect_problems`, `find_leads` / `SEARCHES` / `towns` (finder),
`cmd_places` / `google_check`, `add_photo` / `place_supplied`, `cmd_approve`
/ `cmd_build` / `clean_ref`, `cmd_deck` (Studio JSON), `REFERENCES`, `PROBLEMS`.

## Tests - which file

Run: fast `python3 -B -m unittest tests.test_parsers -b` (~1.5 min); one area
`python3 -B -m unittest tests.test_<area> -b`; full `discover` ~40 min - run it
in the background. `tests/test_clipper.py` alone is ~20 min; run a class
(`tests.test_clipper.Posting`) when only that changed.

| Area | File |
|---|---|
| Parsers, cross-cutting rules, Deck wiring, most agents | `tests/test_parsers.py` (grep a class name) |
| Shop, TikTok kits, scenes, store board | `tests/test_shop.py` |
| Paul | `tests/test_paul.py` |
| Clip | `tests/test_clipper.py` |
| Coach | `tests/test_coach.py` |
| Peter defense | `tests/test_defense.py` |
| Ace board | `tests/test_ace_board.py` |
| Scout scan | `tests/test_scout_scan.py` |
| Stan | `tests/test_stan.py` |
| Agent Room | `tests/test_agentroom.py`, `tests/test_agentroom_build.py`, `tests/test_app_ai.py`, `tests/test_site.py` |
| Engines, wake | `tests/test_wake.py`, `tests/test_models.py`, `tests/test_new_claude_login.py` |
| Deck restarts, attention, village frames | `tests/test_restarts.py`, `tests/test_attention.py`, `tests/test_village_frame.py` |
| This table | `tests/test_routing.py` |

Real captured inputs live in `tests/fixtures/`; refresh with
`scripts/capture-fixtures.py`. Before any push: `scripts/check-secrets.py`.

## Deploy and the droplet

* Units: `deploy/<name>.service` + `.timer`, installed by `scripts/deploy.sh`
  (also rebuilds AGENTS.md via `scripts/merge-header.sh`). Agent tools:
  `scripts/set-agent-tools.sh`. Network: `scripts/tailscale-setup.sh`.
* Root on the droplet: `/root/ecosystem`. Pulling code is enough for
  scripts and `clipper/`; a changed header needs `scripts/deploy.sh <name>`;
  a changed Deck `.js` needs `python3 scripts/restart-deck.py`.
* Other docs: `BACKBONE.md` (queue/API), `clipper/DESIGN.md`,
  `agentroom/README.md`, `HANDOFF-AGENT-ROOM.md`, `HANDOFF-ACE-BELFORT-BOLT.md`, `HANDOFF-PAUL.md`, `HANDOFF-SCOUT-EMILY.md`, `HANDOFF-CLIP.md`, `docs/` (public site pages).
