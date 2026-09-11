'use strict';

const express = require('express');
const { openDb, readLimits, writeLimits, logEvent } = require('./db');

const PORT = Number(process.env.PORT || 3001);
// Bind to loopback by default, matching the OpenClaw gateway's posture.
// Set HOST=0.0.0.0 only if you deliberately want this reachable off-box.
const HOST = process.env.HOST || '127.0.0.1';

const db = openDb();
const app = express();
app.use(express.json({ limit: '1mb' }));

const OPEN_STATUSES = ['pending', 'assigned', 'in_progress'];

function getTask(id) {
  return db.prepare('SELECT * FROM tasks WHERE id = ?').get(id);
}

function notFound(res) {
  return res.status(404).json({ error: 'task not found' });
}

// A task's spend for cap purposes: what it actually cost if recorded,
// otherwise what it was estimated to cost.
const TODAY_SPEND_SQL = `
  SELECT
    COALESCE(SUM(CASE WHEN cost_actual > 0 THEN cost_actual ELSE cost_estimate END), 0) AS spend,
    COUNT(*) AS count
  FROM tasks
  WHERE date(created_at) = date('now')
    AND status != 'rejected'
`;

// ---------------------------------------------------------------------------
// Literal /tasks routes MUST be registered before /tasks/:id, or Express lets
// the param route swallow "stats" and "limits" and they 404.
// ---------------------------------------------------------------------------

app.get('/tasks/stats', (req, res) => {
  const rows = db.prepare('SELECT status, COUNT(*) AS n FROM tasks GROUP BY status').all();
  const byStatus = {};
  for (const r of rows) byStatus[r.status] = r.n;
  const total = rows.reduce((sum, r) => sum + r.n, 0);
  const today = db.prepare(TODAY_SPEND_SQL).get();
  res.json({
    total,
    byStatus,
    today: { tasks_created: today.count, spend: Number(today.spend.toFixed(4)) },
    limits: readLimits(),
  });
});

app.get('/tasks/limits', (req, res) => res.json(readLimits()));

app.patch('/tasks/limits', (req, res) => {
  const allowed = ['single_task_cost_max', 'daily_total_spend_cap', 'daily_new_tasks_cap'];
  const patch = {};
  for (const k of allowed) {
    if (req.body && req.body[k] !== undefined) {
      const v = Number(req.body[k]);
      if (!Number.isFinite(v) || v < 0) {
        return res.status(400).json({ error: `${k} must be a non-negative number` });
      }
      patch[k] = v;
    }
  }
  if (!Object.keys(patch).length) return res.status(400).json({ error: 'no valid fields to update' });
  res.json(writeLimits(patch));
});

app.get('/tasks', (req, res) => {
  const where = [];
  const args = [];
  if (req.query.status) { where.push('status = ?'); args.push(String(req.query.status)); }
  if (req.query.assignee) { where.push('assignee = ?'); args.push(String(req.query.assignee)); }
  const sql = `SELECT * FROM tasks ${where.length ? 'WHERE ' + where.join(' AND ') : ''}
               ORDER BY priority DESC, id ASC`;
  res.json(db.prepare(sql).all(...args));
});

app.post('/tasks', (req, res) => {
  const b = req.body || {};
  const costEstimate = Number(b.cost_estimate || 0);
  if (!Number.isFinite(costEstimate) || costEstimate < 0) {
    return res.status(400).json({ error: 'cost_estimate must be a non-negative number' });
  }

  const limits = readLimits();

  if (costEstimate > limits.single_task_cost_max) {
    return res.status(429).json({
      error: `cost_estimate ${costEstimate} exceeds single_task_cost_max ${limits.single_task_cost_max}`,
    });
  }

  const today = db.prepare(TODAY_SPEND_SQL).get();

  if (today.count + 1 > limits.daily_new_tasks_cap) {
    return res.status(429).json({
      error: `daily_new_tasks_cap ${limits.daily_new_tasks_cap} reached (${today.count} created today)`,
    });
  }

  if (today.spend + costEstimate > limits.daily_total_spend_cap) {
    return res.status(429).json({
      error: `daily_total_spend_cap ${limits.daily_total_spend_cap} would be exceeded ` +
             `(${today.spend.toFixed(2)} committed today + ${costEstimate.toFixed(2)})`,
    });
  }

  const payload = b.payload === undefined ? null
    : (typeof b.payload === 'string' ? b.payload : JSON.stringify(b.payload));

  try {
    const info = db.prepare(`
      INSERT INTO tasks (created_by, assignee, type, status, payload, cost_estimate,
                         priority, parent_id, dedupe_key, notes, kill_criteria)
      VALUES (@created_by, @assignee, @type, @status, @payload, @cost_estimate,
              @priority, @parent_id, @dedupe_key, @notes, @kill_criteria)
    `).run({
      created_by: b.created_by ?? null,
      assignee: b.assignee ?? null,
      type: b.type ?? null,
      status: b.status ?? 'pending',
      payload,
      cost_estimate: costEstimate,
      priority: Number.isFinite(Number(b.priority)) ? Number(b.priority) : 0,
      parent_id: b.parent_id ?? null,
      dedupe_key: b.dedupe_key ?? null,
      notes: b.notes ?? null,
      kill_criteria: b.kill_criteria ?? null,
    });
    const task = getTask(info.lastInsertRowid);
    logEvent(db, task.id, 'created', `task created by ${task.created_by || 'unknown'}`);
    res.status(201).json(task);
  } catch (err) {
    if (String(err.message).includes('UNIQUE constraint failed')) {
      return res.status(409).json({ error: 'an open task with this dedupe_key already exists' });
    }
    if (String(err.message).includes('CHECK constraint failed')) {
      return res.status(400).json({ error: 'invalid status value' });
    }
    throw err;
  }
});

// ---------------------------------------------------------------------------
// Param routes come after the literals above.
// ---------------------------------------------------------------------------

app.get('/tasks/:id', (req, res) => {
  const task = getTask(req.params.id);
  return task ? res.json(task) : notFound(res);
});

app.patch('/tasks/:id', (req, res) => {
  if (!getTask(req.params.id)) return notFound(res);
  const allowed = ['assignee', 'type', 'status', 'payload', 'result', 'cost_estimate',
                   'cost_actual', 'priority', 'parent_id', 'dedupe_key', 'notes', 'kill_criteria'];
  const sets = [];
  const args = [];
  for (const k of allowed) {
    if (req.body && req.body[k] !== undefined) {
      let v = req.body[k];
      if ((k === 'payload' || k === 'result') && v !== null && typeof v !== 'string') {
        v = JSON.stringify(v);
      }
      sets.push(`${k} = ?`);
      args.push(v);
    }
  }
  if (!sets.length) return res.status(400).json({ error: 'no valid fields to update' });
  args.push(req.params.id);
  try {
    db.prepare(`UPDATE tasks SET ${sets.join(', ')} WHERE id = ?`).run(...args);
  } catch (err) {
    if (String(err.message).includes('UNIQUE constraint failed')) {
      return res.status(409).json({ error: 'an open task with this dedupe_key already exists' });
    }
    if (String(err.message).includes('CHECK constraint failed')) {
      return res.status(400).json({ error: 'invalid status value' });
    }
    throw err;
  }
  res.json(getTask(req.params.id));
});

function transition(res, id, status, stampColumn, extra = {}) {
  const task = getTask(id);
  if (!task) return notFound(res);
  const sets = [`status = '${status}'`];
  const args = [];
  if (stampColumn) sets.push(`${stampColumn} = datetime('now')`);
  for (const [k, v] of Object.entries(extra)) { sets.push(`${k} = ?`); args.push(v); }
  args.push(id);
  db.prepare(`UPDATE tasks SET ${sets.join(', ')} WHERE id = ?`).run(...args);
  return getTask(id);
}

app.post('/tasks/:id/assign', (req, res) => {
  const extra = {};
  if (req.body && req.body.assignee) extra.assignee = req.body.assignee;
  const task = transition(res, req.params.id, 'assigned', 'assigned_at', extra);
  if (!task) return;
  logEvent(db, task.id, 'assigned', `assigned to ${task.assignee || 'unassigned'}`);
  res.json(task);
});

app.post('/tasks/:id/start', (req, res) => {
  const task = transition(res, req.params.id, 'in_progress', 'started_at');
  if (!task) return;
  logEvent(db, task.id, 'started', `started by ${task.assignee || 'unknown'}`);
  res.json(task);
});

app.post('/tasks/:id/complete', (req, res) => {
  const b = req.body || {};
  const extra = {};
  if (b.result !== undefined) {
    extra.result = typeof b.result === 'string' ? b.result : JSON.stringify(b.result);
  }
  if (b.cost_actual !== undefined) {
    const c = Number(b.cost_actual);
    if (!Number.isFinite(c) || c < 0) {
      return res.status(400).json({ error: 'cost_actual must be a non-negative number' });
    }
    extra.cost_actual = c;
  }
  const task = transition(res, req.params.id, 'done', 'completed_at', extra);
  if (!task) return;
  logEvent(db, task.id, 'completed', `cost_actual=${task.cost_actual}`);
  res.json(task);
});

app.post('/tasks/:id/reject', (req, res) => {
  const reason = (req.body && req.body.reason) || null;
  const task = transition(res, req.params.id, 'rejected', 'completed_at',
    reason ? { notes: reason } : {});
  if (!task) return;
  logEvent(db, task.id, 'rejected', reason || 'no reason given');
  res.json(task);
});

app.get('/health', (req, res) => res.json({ ok: true, service: 'mission-control-api' }));

app.use((err, req, res, next) => {
  console.error('[mission-control-api]', err);
  res.status(500).json({ error: 'internal error' });
});

if (require.main === module) {
  app.listen(PORT, HOST, () => {
    console.log(`[mission-control-api] listening on http://${HOST}:${PORT}`);
  });
}

module.exports = app;
