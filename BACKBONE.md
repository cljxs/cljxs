# Agent Ecosystem — Backbone

The task queue + dispatcher that agents plug into. Built and tested for
Ubuntu 24.04 on a VPS, running as root with systemd.

## Layout

    agents/                      one folder per agent (auto-discovered)
    tasks/queue.db               the SQLite task queue (created on first run)
    tasks/limits.json            spend caps
    scripts/task-dispatcher.py   the daemon that fires agents
    mission-control-api/         Express API on port 3001
    deploy/                      systemd unit files

Nothing hardcodes a path: both services resolve the root from their own
location, or from `ECOSYSTEM_ROOT` if you set it.

## Install (VPS, as root)

    git clone -b claude/ai-agent-ecosystem-setup-0280zz https://github.com/cljxs/cljxs.git /root/ecosystem
    cd /root/ecosystem/mission-control-api && npm install --ignore-scripts --no-audit --no-fund
    cp /root/ecosystem/deploy/*.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now mission-control-api task-dispatcher

## Verify

    systemctl status mission-control-api --no-pager
    systemctl status task-dispatcher --no-pager
    curl -s http://localhost:3001/tasks/stats
    node -e "const db=require('/root/ecosystem/mission-control-api/node_modules/better-sqlite3')('/root/ecosystem/tasks/queue.db');console.log(db.prepare('select name from sqlite_master where type=?').all('table'))"

`/tasks/stats` must return JSON, not 404. The table list must include
`tasks` and `events`.

## API

Base: `http://127.0.0.1:3001` (loopback only by design)

| Method | Path | Purpose |
|---|---|---|
| GET | `/tasks` | list; `?status=` `?assignee=` |
| POST | `/tasks` | create; enforces limits, 429 when a cap would break |
| GET | `/tasks/stats` | `{total, byStatus, today, limits}` |
| GET/PATCH | `/tasks/limits` | read / change spend caps |
| GET | `/tasks/:id` | one task |
| PATCH | `/tasks/:id` | partial update |
| POST | `/tasks/:id/assign` | -> `assigned`, stamps `assigned_at` |
| POST | `/tasks/:id/start` | -> `in_progress`, stamps `started_at` |
| POST | `/tasks/:id/complete` | -> `done`, stamps `completed_at`, saves `{result, cost_actual}` |
| POST | `/tasks/:id/reject` | -> `rejected`, saves `{reason}` into notes |
| GET | `/health` | liveness |

Literal routes (`/tasks/stats`, `/tasks/limits`) are registered before
`/tasks/:id`. Reordering them makes the literals 404.

## Spend caps — tasks/limits.json

    { "single_task_cost_max": 5.00, "daily_total_spend_cap": 10.00, "daily_new_tasks_cap": 50 }

`POST /tasks` returns **429** if the new task's `cost_estimate` exceeds
`single_task_cost_max`, or if today's committed spend or task count would
break a daily cap. A task's contribution to the daily total is its
`cost_actual` once recorded, otherwise its `cost_estimate`.

## How dispatching works

Every 2 seconds the dispatcher:

1. discovers agents (every subfolder of `agents/`, hidden folders skipped)
2. skips any agent already running something (concurrency = 1 per agent)
3. claims that agent's highest-priority pending task, flipping it to
   `in_progress` **before** firing, with the UPDATE re-checking
   `status='pending'` so a task can never be claimed twice
4. launches:

        openclaw agent --agent <name> --message "task #<id> assigned, see /tasks/<id>" \
          --session-id "task-<id>-<timestamp>" --timeout 600 --json

A fresh `--session-id` per task is required — reused sessions caused runaway
loops in the source system.

Agents complete their own work by calling `POST /tasks/:id/complete`. If an
agent process exits while its task is still `in_progress`, the dispatcher
marks it `failed` with the exit code, so nothing stays claimed forever.

### Robustness

- **Both services own the schema.** The dispatcher runs the same
  `CREATE TABLE IF NOT EXISTS` on startup, so boot order is irrelevant and a
  dispatcher that starts first can't crash-loop on "no such table: tasks".
- **Crash recovery.** On startup any task stuck in `in_progress` is reset to
  `pending`.
- **WAL + busy_timeout** let the API and dispatcher write concurrently.

## The dedupe index

    CREATE UNIQUE INDEX ux_tasks_dedupe ON tasks(dedupe_key)
      WHERE dedupe_key IS NOT NULL AND status IN ('pending','assigned','in_progress');

Scoped to open statuses only. Indexing `(dedupe_key, status)` instead is a
trap: a finished `(key,'done')` row and a new `(key,'pending')` row coexist,
and completing the second collides with the first. Scoping to open statuses
means finishing a task drops it out of the index entirely. Creating a second
open task with a key already in use returns **409**.

## Publishing a listing on Etsy — the half no script can check

Everything from artwork to draft is enforced in code. These steps live inside
Etsy's own UI, where nothing here can reach, so they are the ones that get
forgotten.

**Once per shop.** Register the production partner:

    Shop Manager -> Settings -> Production partners -> Add a production partner

      Partner name   Printify
      Location       the production location
      About          what they do for you, e.g. "Prints my original designs
                     on blank garments and ships them to my customers"

Location is awkward with **Printify Choice (provider 99)**, which routes each
order to whichever top-rated provider suits it — there is no single facility
to name. For a US shop selling US-fulfilled garments, the US is the honest
answer. Wanting one named facility is a reason to pick a specific provider
(SwiftPOD 39, Monster Digital 29) over Choice.

**Every listing, before publishing:**

- [ ] **Attach the production partner.** Edit listing -> *Production* section
      -> select Printify. Registering it in Settings is **not** enough; a
      listing with none attached is undisclosed even though the partner
      exists on the shop.
- [ ] **Who made it: "I did."** You designed it. Not "a member of my shop",
      and nothing implying you assembled the garment.
- [ ] **Read the description once.** `draft` writes the disclosure lines in,
      so they are present — but present is not the same as accurate.

**Already enforced, listed so you know what you are not checking:**

- The disclosure lines are in the description — `draft` appends whichever is
  missing and saves the corrected description back to `listing.json`
  (`scripts/disclosures.py`, wording in `agents/emily/state/disclosures.json`).
  It used to refuse instead, which stalled two finished hoodies over a rule
  Emily's instructions never mentioned.
- Apparel artwork has its background removed — `draft` refuses art that
  cannot be cut cleanly (`scripts/knockout.py`).
- Nothing here can publish. `emily-printify.py` has no publish command by
  design; every product is created UNPUBLISHED and you press Publish.

**One garment, one catalogue entry.** The blueprint/provider/variant choice
is cached in `agents/emily/state/printify-catalog.json` under a word — and the
word in a build's `listing.json` is whichever one Emily wrote. When they
differ, do **not** pick the blueprint again; point the second word at the
entry that already exists:

    python3 scripts/emily-printify.py alias hoodie sweatshirt

`draft` resolves a word through the key, then any aliases, then the
blueprint's own title, and prints which entry it used whenever the two names
differ. Two entries for one blueprint is how a price set on one stops
applying to the other.

**Throwing one away.** `scripts/emily-build.py list` shows every build and
its state; `remove <slug>` archives one you do not want to `builds/_removed/`
and the gallery stops showing it; `restore <slug>` puts it back. It archives
rather than deletes because the artwork cost a model call, and it refuses
without `--force` when the build has a Printify product — moving the folder
would leave that product in Printify with nothing here pointing at it, so
delete it there first.

**Two caveats.** Etsy blocks automated reads of `/legal/creativity/`,
`/legal/handmade/` and its help pages, so the above was assembled from
secondary sources — trust the screen over this file when a label differs, and
read those pages yourself. And none of it is legal advice: the checklist is a
reminder that cannot be forgotten, not a lawyer.

## Notes

- **Install with `--ignore-scripts`.** `better-sqlite3` ships ready-made
  binaries for every platform in `prebuilds/` (including `linux-x64.node`),
  but it also contains a `binding.gyp`, and npm's default is to run
  `node-gyp rebuild` whenever it sees one. That discards the prebuilt binary
  and tries to compile from source, which fails with
  `gyp ERR! stack Error: not found: make` on any box without build tools.
  `--ignore-scripts` skips the pointless rebuild and the shipped binary is
  used instead — a 2 second install with no compiler required.
- The API binds to `127.0.0.1`. Together with the firewall allowing only SSH,
  nothing here is reachable from the internet.
- On Windows the dispatcher spawns agents with `CREATE_NO_WINDOW` so console
  windows don't flash up; guarded by `os.name == "nt"`, so it's inert here.
