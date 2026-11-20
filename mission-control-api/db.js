'use strict';

const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

// Resolve the ecosystem root from this file's location so nothing is hardcoded.
// Override with ECOSYSTEM_ROOT if you move things around.
const ROOT = process.env.ECOSYSTEM_ROOT
  ? path.resolve(process.env.ECOSYSTEM_ROOT)
  : path.resolve(__dirname, '..');

const TASKS_DIR = path.join(ROOT, 'tasks');
const DB_PATH = process.env.QUEUE_DB || path.join(TASKS_DIR, 'queue.db');
const LIMITS_PATH = path.join(TASKS_DIR, 'limits.json');
const AGENTS_DIR = path.join(ROOT, 'agents');

const DEFAULT_LIMITS = {
  single_task_cost_max: 5.0,
  daily_total_spend_cap: 10.0,
  daily_new_tasks_cap: 50,
};

// The dispatcher runs this exact same schema on its own startup. Both services
// own it, so whichever boots first creates the tables and the other is happy.
const SCHEMA = `
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

-- Dedupe prevents duplicate OPEN tasks only. The index deliberately does NOT
-- include status: indexing (dedupe_key, status) is a trap, because a finished
-- (key,'done') row and a new (key,'pending') row can coexist, and completing
-- the second then collides with the first. Scoping the unique key to open
-- statuses means finishing a task drops it out of the index entirely.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_dedupe ON tasks(dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('pending','assigned','in_progress');

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  task_id INTEGER, event TEXT, msg TEXT, meta TEXT
);

CREATE INDEX IF NOT EXISTS ix_tasks_status_assignee ON tasks(status, assignee);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id);
`;

function openDb() {
  fs.mkdirSync(TASKS_DIR, { recursive: true });
  const db = new Database(DB_PATH);
  // WAL + a busy timeout let the API and the dispatcher write concurrently.
  db.pragma('journal_mode = WAL');
  db.pragma('busy_timeout = 5000');
  db.exec(SCHEMA);
  return db;
}

function readLimits() {
  try {
    const raw = JSON.parse(fs.readFileSync(LIMITS_PATH, 'utf8'));
    return { ...DEFAULT_LIMITS, ...raw };
  } catch {
    return { ...DEFAULT_LIMITS };
  }
}

function writeLimits(limits) {
  const merged = { ...readLimits(), ...limits };
  fs.mkdirSync(TASKS_DIR, { recursive: true });
  fs.writeFileSync(LIMITS_PATH, JSON.stringify(merged, null, 2) + '\n');
  return merged;
}

function logEvent(db, taskId, event, msg, meta) {
  db.prepare('INSERT INTO events (task_id, event, msg, meta) VALUES (?, ?, ?, ?)')
    .run(taskId ?? null, event, msg ?? null, meta ? JSON.stringify(meta) : null);
}

module.exports = {
  ROOT, TASKS_DIR, DB_PATH, LIMITS_PATH, AGENTS_DIR,
  SCHEMA, DEFAULT_LIMITS,
  openDb, readLimits, writeLimits, logEvent,
};
