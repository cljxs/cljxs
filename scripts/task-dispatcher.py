#!/usr/bin/env python3
"""
task-dispatcher.py — the daemon that fires agents.

Every POLL_SECONDS it walks the known agents (every subfolder under agents/,
auto-discovered, nothing hardcoded) and, for each agent that is not already
running something, claims its next pending task and launches OpenClaw for it.

Concurrency is 1 task per agent.
"""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- paths ------------------------------------------------------------------
# Resolved from this file's location so nothing is hardcoded to a machine.
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENTS_DIR = ROOT / "agents"
TASKS_DIR = ROOT / "tasks"
DB_PATH = Path(os.environ.get("QUEUE_DB", TASKS_DIR / "queue.db"))

POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "600"))
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")

# The dispatcher itself talks to SQLite, not the API. This is only used to
# tell an agent where its task lives - it has no other way to find out, and
# a wake message saying "see /tasks/3" is not enough to act on.
API_BASE = os.environ.get("MISSION_CONTROL_API", "http://127.0.0.1:3001")

# --- schema -----------------------------------------------------------------
# Identical to the API's. The dispatcher ensures the schema itself on startup so
# it never crashes with "no such table: tasks" when it boots before the API.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  created_by TEXT, assignee TEXT, type TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','assigned','in_progress','done','failed','rejected','cancelled','blocked')),
  payload TEXT, result TEXT,
  cost_estimate REAL DEFAULT 0, cost_actual REAL DEFAULT 0,
  priority INTEGER DEFAULT 0, parent_id INTEGER,
  dedupe_key TEXT, notes TEXT, kill_criteria TEXT,
  assigned_at TEXT, started_at TEXT, completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_dedupe ON tasks(dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('pending','assigned','in_progress');

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  task_id INTEGER, event TEXT, msg TEXT, meta TEXT
);

CREATE INDEX IF NOT EXISTS ix_tasks_status_assignee ON tasks(status, assignee);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id);
"""

running = {}      # agent name -> {"proc": Popen, "task_id": int, "started": float}
shutting_down = False


def log(msg):
    print(f"[dispatcher {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def connect():
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets the Node API and this daemon write concurrently.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def record_event(conn, task_id, event, msg=None, meta=None):
    conn.execute(
        "INSERT INTO events (task_id, event, msg, meta) VALUES (?, ?, ?, ?)",
        (task_id, event, msg, json.dumps(meta) if meta else None),
    )
    conn.commit()


def discover_agents():
    """Known agents = every subfolder under agents/. Never hardcoded."""
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in AGENTS_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def recover_stuck_tasks(conn):
    """An agent killed mid-task must not leave its work claimed forever."""
    cur = conn.execute(
        "UPDATE tasks SET status='pending', started_at=NULL WHERE status='in_progress'"
    )
    conn.commit()
    if cur.rowcount:
        log(f"crash recovery: reset {cur.rowcount} stuck in_progress task(s) to pending")
        record_event(conn, None, "recovery", f"reset {cur.rowcount} in_progress task(s) to pending")


def claim_next_task(conn, agent):
    """
    Flip the task to in_progress FIRST, then fire. The UPDATE re-checks
    status='pending' so two dispatchers could never claim the same row.
    """
    row = conn.execute(
        """SELECT id FROM tasks
           WHERE assignee = ? AND status = 'pending'
           ORDER BY priority DESC, id ASC LIMIT 1""",
        (agent,),
    ).fetchone()
    if not row:
        return None

    cur = conn.execute(
        """UPDATE tasks
           SET status='in_progress',
               started_at=datetime('now'),
               assigned_at=COALESCE(assigned_at, datetime('now'))
           WHERE id = ? AND status = 'pending'""",
        (row["id"],),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None  # somebody else got it first
    return row["id"]


def spawn_agent(agent, task_id):
    """Launch OpenClaw for one task with a fresh session id."""
    # A fresh --session-id per task is REQUIRED: reusing sessions caused
    # runaway loops in the source system.
    session_id = f"task-{task_id}-{int(time.time())}"
    cmd = [
        OPENCLAW_BIN, "agent",
        "--agent", agent,
        # Self-contained on purpose. "see /tasks/3" is a path with no host,
        # no port and no verb - Emily woke to exactly that, was told by her
        # instructions to read her payload, had no way to, and quit in 14
        # seconds having written nothing. The wake message now carries the
        # whole command.
        "--message", (f"Task #{task_id} is assigned to you. Read it first with:\n"
                      f"  curl -s {API_BASE}/tasks/{task_id}\n"
                      f"The payload holds everything you need. "
                      f"When you are finished, complete it with:\n"
                      f"  curl -s -X POST {API_BASE}/tasks/{task_id}/complete "
                      f"-H 'Content-Type: application/json' "
                      f"-d '{{\"result\":\"<one line>\",\"cost_actual\":0.0}}'"),
        "--session-id", session_id,
        "--timeout", str(TASK_TIMEOUT),
        "--json",
    ]

    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}

    # Windows only: stop a console window flashing up on every agent run.
    # There is no window to hide on Mac/Linux, so this is guarded.
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si

    log(f"firing {agent} for task #{task_id} (session {session_id})")
    return subprocess.Popen(cmd, **kwargs)


def reap_finished(conn):
    """Collect finished agent processes and free their slot."""
    for agent in list(running):
        entry = running[agent]
        proc = entry["proc"]
        if proc.poll() is None:
            continue  # still working

        task_id = entry["task_id"]
        del running[agent]
        code = proc.returncode
        try:
            stdout, stderr = proc.communicate(timeout=5)
            stderr = (stderr or b"").decode("utf-8", "replace").strip()
            stdout = (stdout or b"").decode("utf-8", "replace").strip()
        except Exception:
            stdout = stderr = ""
        # An agent that exits 0 without completing its task prints its reason
        # to stdout, not stderr - and that was being discarded, which cost a
        # round of guessing about a 14-second failure.
        if stdout:
            log(f"task #{task_id} ({agent}) said:\n{stdout[-1500:]}")

        current = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        status = current["status"] if current else None

        if status == "in_progress":
            # The agent exited without completing its task through the API.
            note = f"agent exited with code {code}"
            if stderr:
                note += f": {stderr[:500]}"
            conn.execute(
                "UPDATE tasks SET status='failed', completed_at=datetime('now'), result=? WHERE id = ?",
                (note, task_id),
            )
            conn.commit()
            record_event(conn, task_id, "failed", note)
            log(f"task #{task_id} ({agent}) failed: {note}")
        else:
            record_event(conn, task_id, "agent_exit", f"exit code {code}, task status {status}")
            log(f"task #{task_id} ({agent}) finished, status={status}")
            if status == "done":
                verify_completed(conn, task_id, agent)


def verify_completed(conn, task_id, agent):
    """If an agent ships a verifier, let it have the last word.

    Completing a task goes through the API, which means "done" has only ever
    meant the agent said so. Four agents in this ecosystem have finished a run
    having written nothing at all. So when scripts/<agent>-verify.py exists, it
    is run against the task's build_dir and can flip the task back to failed.

    Agents without a verifier are unaffected - this does nothing for them.
    """
    script = ROOT / "scripts" / f"{agent}-verify.py"
    if not script.is_file():
        return
    row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)).fetchone()
    try:
        payload = json.loads(row["payload"]) if row and row["payload"] else {}
    except Exception:
        payload = {}
    target = payload.get("build_dir") or payload.get("slug")
    if not target:
        return
    try:
        res = subprocess.run([sys.executable, str(script), str(target)],
                             cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    except Exception as exc:
        log(f"task #{task_id} ({agent}) verifier could not run: {exc}")
        return
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    if res.returncode == 0:
        log(f"task #{task_id} ({agent}) verified ok")
        record_event(conn, task_id, "verified", out[:500])
        return
    conn.execute(
        "UPDATE tasks SET status='failed', completed_at=datetime('now'), result=? WHERE id = ?",
        (f"verifier rejected the result: {out[:450]}", task_id),
    )
    conn.commit()
    record_event(conn, task_id, "verify_failed", out[:500])
    log(f"task #{task_id} ({agent}) FAILED verification:\n{out}")


def handle_signal(signum, _frame):
    global shutting_down
    shutting_down = True
    log(f"received signal {signum}, shutting down")


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    conn = connect()
    conn.executescript(SCHEMA)   # own the schema; boot order is irrelevant
    conn.commit()

    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    recover_stuck_tasks(conn)

    if not shutil.which(OPENCLAW_BIN):
        log(f"WARNING: '{OPENCLAW_BIN}' not found on PATH — tasks will fail until it is")

    log(f"root={ROOT} db={DB_PATH} poll={POLL_SECONDS}s")
    log(f"known agents: {discover_agents() or '(none yet)'}")

    last_seen_agents = None

    while not shutting_down:
        try:
            reap_finished(conn)

            agents = discover_agents()
            if agents != last_seen_agents:
                log(f"known agents: {agents or '(none yet)'}")
                last_seen_agents = agents

            for agent in agents:
                if agent in running:
                    continue  # concurrency = 1 per agent
                task_id = claim_next_task(conn, agent)
                if task_id is None:
                    continue
                record_event(conn, task_id, "dispatched", f"dispatched to {agent}")
                try:
                    proc = spawn_agent(agent, task_id)
                except Exception as exc:
                    conn.execute(
                        "UPDATE tasks SET status='failed', completed_at=datetime('now'), result=? WHERE id=?",
                        (f"failed to launch agent: {exc}", task_id),
                    )
                    conn.commit()
                    record_event(conn, task_id, "failed", f"spawn error: {exc}")
                    log(f"could not launch {agent} for task #{task_id}: {exc}")
                    continue
                running[agent] = {"proc": proc, "task_id": task_id, "started": time.time()}

        except sqlite3.Error as exc:
            log(f"database error: {exc}")
        except Exception as exc:  # never let the loop die
            log(f"unexpected error: {exc}")

        time.sleep(POLL_SECONDS)

    for agent, entry in running.items():
        log(f"terminating {agent} (task #{entry['task_id']})")
        entry["proc"].terminate()
    conn.close()
    log("stopped")


if __name__ == "__main__":
    sys.exit(main())
