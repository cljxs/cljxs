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
    cd /root/ecosystem/mission-control-api && npm install --no-audit --no-fund
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

## Notes

- `better-sqlite3` is a native module but ships prebuilt binaries, so a plain
  `npm install` works on Node 20/22 Linux x64. If a build is ever skipped, use
  `npm install --foreground-scripts`.
- The API binds to `127.0.0.1`. Together with the firewall allowing only SSH,
  nothing here is reachable from the internet.
- On Windows the dispatcher spawns agents with `CREATE_NO_WINDOW` so console
  windows don't flash up; guarded by `os.name == "nt"`, so it's inert here.
