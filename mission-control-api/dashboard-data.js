'use strict';

// Read-only aggregation for the Command Deck dashboard.
// This module NEVER writes inside agents/ — it only reads. The one file it
// writes is tasks/dashboard-snapshots.json, which is dashboard-owned history.

const fs = require('fs');
const path = require('path');
const { openDb, AGENTS_DIR, TASKS_DIR, readLimits } = require('./db');

const SNAPSHOT_PATH = path.join(TASKS_DIR, 'dashboard-snapshots.json');
const SNAPSHOT_MIN_GAP_MS = 15 * 60 * 1000;
const SNAPSHOT_MAX_POINTS = 600;
const STALE_MINUTES = 90;

// Data is only "stale" if it should have been refreshed. The fetcher doesn't
// run overnight or at weekends, so without this the dashboard would report the
// system degraded every evening and all weekend.
function usMarketOpen(now = new Date()) {
  const et = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const day = et.getDay();
  if (day === 0 || day === 6) return false;
  const mins = et.getHours() * 60 + et.getMinutes();
  return mins >= 9 * 60 + 30 && mins <= 16 * 60;
}

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function firstOf(obj, keys, fallback = null) {
  for (const k of keys) {
    if (obj && obj[k] !== undefined && obj[k] !== null) return obj[k];
  }
  return fallback;
}

function num(v) {
  const n = typeof v === 'string' ? parseFloat(v.replace(/[$,]/g, '')) : Number(v);
  return Number.isFinite(n) ? n : null;
}

function minutesSince(iso) {
  if (!iso) return null;
  const t = Date.parse(String(iso).replace(' ', 'T') + (String(iso).endsWith('Z') ? '' : 'Z'));
  if (!Number.isFinite(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 60000));
}

function listAgents() {
  try {
    return fs.readdirSync(AGENTS_DIR, { withFileTypes: true })
      .filter(d => d.isDirectory() && !d.name.startsWith('.'))
      .map(d => d.name)
      .sort();
  } catch { return []; }
}

function latestReport(agentDir) {
  const dir = path.join(agentDir, 'reports');
  try {
    const files = fs.readdirSync(dir)
      .filter(f => !f.startsWith('.') && /\.(md|txt)$/i.test(f))
      .map(f => ({ f, m: fs.statSync(path.join(dir, f)).mtimeMs }))
      .sort((a, b) => b.m - a.m);
    if (!files.length) return { count: 0, name: null, when: null, excerpt: null };
    const body = fs.readFileSync(path.join(dir, files[0].f), 'utf8');
    const excerpt = body.split('\n')
      .map(l => l.replace(/^[#>\-*\s]+/, '').trim())
      .find(l => l.length > 20) || null;
    return {
      count: files.length,
      name: files[0].f,
      when: new Date(files[0].m).toISOString().replace('T', ' ').slice(0, 19),
      excerpt: excerpt ? excerpt.slice(0, 220) : null,
    };
  } catch { return { count: 0, name: null, when: null, excerpt: null }; }
}

function lastMemoryLine(agentDir) {
  try {
    const lines = fs.readFileSync(path.join(agentDir, 'MEMORY.md'), 'utf8')
      .split('\n').map(l => l.trim())
      .filter(l => l.startsWith('-') && !/no cycles run yet/i.test(l));
    return lines.length ? lines[lines.length - 1].replace(/^-\s*/, '').slice(0, 200) : null;
  } catch { return null; }
}

// Portfolio shapes vary by agent, so every field is looked up by alias.
function readPortfolio(agentDir) {
  const dir = path.join(agentDir, 'state');
  let p = readJson(path.join(dir, 'portfolio.json'));
  if (!p) {
    try {
      const alt = fs.readdirSync(dir).find(f => /portfolio|bankroll|balance/i.test(f) && f.endsWith('.json') && !f.includes('seed'));
      if (alt) p = readJson(path.join(dir, alt));
    } catch { /* no state dir */ }
  }
  return p;
}

function markToMarket(agentDir, portfolio) {
  const quotes = readJson(path.join(agentDir, 'data', 'quotes.json'));
  const priceOf = sym => {
    const q = quotes && quotes.quotes && quotes.quotes[sym];
    return q ? num(q.price) : null;
  };

  const raw = Array.isArray(portfolio && portfolio.positions) ? portfolio.positions
    : Array.isArray(portfolio && portfolio.open_bets) ? portfolio.open_bets : [];
  const rows = [];
  for (const pos of raw) {
    const symbol = String(firstOf(pos, ['symbol', 'ticker', 'sym'], '') || '').toUpperCase();
    if (!symbol) continue;
    const shares = num(firstOf(pos, ['shares', 'qty', 'quantity', 'units', 'size']));
    const entry = num(firstOf(pos, ['cost_basis', 'entry_price', 'entry', 'avg_price', 'buy_price', 'price', 'cost']));
    const live = priceOf(symbol);
    const cost = shares !== null && entry !== null ? shares * entry : null;
    const value = shares !== null && live !== null ? shares * live : cost;
    const pnl = value !== null && cost !== null ? value - cost : null;
    rows.push({
      symbol, shares, entry, price: live, cost,
      value, pnl,
      pnl_pct: pnl !== null && cost ? (pnl / cost) * 100 : null,
      priced: live !== null,
    });
  }
  return { rows, quotes_asof: quotes ? quotes.asof_utc : null };
}

function buildAgent(name) {
  const dir = path.join(AGENTS_DIR, name);
  const portfolio = readPortfolio(dir);
  const report = latestReport(dir);
  const memory = lastMemoryLine(dir);
  const meta = readJson(path.join(dir, 'data', '_meta.json'));
  const dataAge = meta ? minutesSince(meta.asof_utc) : null;

  const { rows, quotes_asof } = markToMarket(dir, portfolio);
  const cash = portfolio ? num(firstOf(portfolio, ['cash', 'bankroll', 'balance', 'available_cash'], 0)) ?? 0 : null;
  const start = portfolio ? num(firstOf(portfolio, ['starting_cash', 'starting_bankroll', 'start_balance', 'initial'], null)) : null;
  const held = rows.reduce((s, r) => s + (r.value ?? 0), 0);
  const value = portfolio ? cash + held : null;
  const pnl = value !== null && start !== null ? value - start : null;

  const cycles = portfolio ? num(firstOf(portfolio, ['cycle_count', 'cycles', 'runs'], null)) : null;
  const runs = cycles !== null ? cycles : report.count;

  // Trust whichever is NEWER. An agent that writes its report but forgets to
  // update its state file would otherwise be reported as days idle when it
  // actually ran minutes ago - which is exactly what Belfort did on 2026-09-14.
  const stateRun = portfolio && firstOf(portfolio, ['last_cycle_utc', 'last_run', 'updated_at'], null);
  const stateAge = minutesSince(stateRun);
  const reportAge = minutesSince(report.when);
  const candidates = [[stateAge, stateRun], [reportAge, report.when]]
    .filter(([age]) => age !== null)
    .sort((a, b) => a[0] - b[0]);
  const lastRun = candidates.length ? candidates[0][1] : null;
  const lastRunAge = candidates.length ? candidates[0][0] : null;

  let status = 'idle';
  if (!portfolio && report.count === 0) status = 'waiting';
  else if (dataAge !== null && dataAge > STALE_MINUTES && usMarketOpen()) status = 'stale';
  else if (lastRunAge !== null && lastRunAge < 20) status = 'active';

  return {
    name,
    status,
    mode: portfolio ? firstOf(portfolio, ['mode'], null) : null,
    last_run: lastRun,
    last_run_age_min: lastRunAge,
    data_age_min: dataAge,
    quotes_asof,
    runs,
    report_count: report.count,
    latest_report: report.name,
    headline: report.excerpt || memory || null,
    memory_line: memory,
    has_portfolio: Boolean(portfolio),
    cash, starting_cash: start, value, pnl,
    pnl_pct: pnl !== null && start ? (pnl / start) * 100 : null,
    positions: rows,
  };
}

function queueSpend(db) {
  let byAgent = [];
  let totalToday = 0;
  let hasCost = false;
  try {
    byAgent = db.prepare(`
      SELECT COALESCE(assignee,'unassigned') AS agent,
             COUNT(*) AS runs,
             COALESCE(SUM(cost_actual),0) AS cost
      FROM tasks
      WHERE status = 'done'
      GROUP BY COALESCE(assignee,'unassigned')
      ORDER BY cost DESC, runs DESC
    `).all();
    const t = db.prepare(`
      SELECT COALESCE(SUM(cost_actual),0) AS c
      FROM tasks WHERE status='done' AND date(completed_at) = date('now')
    `).get();
    totalToday = t ? t.c : 0;
    hasCost = byAgent.some(r => r.cost > 0);
  } catch { /* queue unreadable */ }
  return { by_agent: byAgent, total_today: totalToday, has_cost_data: hasCost };
}

function dailyActivity(db, agents) {
  const map = new Map();
  try {
    for (const r of db.prepare(`
      SELECT date(completed_at) AS d, COUNT(*) AS n
      FROM tasks WHERE completed_at IS NOT NULL
      GROUP BY date(completed_at) ORDER BY d DESC LIMIT 14
    `).all()) {
      if (r.d) map.set(r.d, (map.get(r.d) || 0) + r.n);
    }
  } catch { /* ignore */ }

  // Timer-driven agents never touch the queue, so also count their reports.
  for (const a of agents) {
    const dir = path.join(AGENTS_DIR, a.name, 'reports');
    try {
      for (const f of fs.readdirSync(dir)) {
        const d = new Date(fs.statSync(path.join(dir, f)).mtimeMs).toISOString().slice(0, 10);
        map.set(d, (map.get(d) || 0) + 1);
      }
    } catch { /* none */ }
  }
  // Emit a complete 14-day window (zeros included) so the chart reads as a
  // chart from day one instead of a single slab.
  const out = [];
  for (let i = 13; i >= 0; i--) {
    const day = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10);
    out.push({ d: day, n: map.get(day) || 0 });
  }
  return out;
}

// Dashboard-owned value history. Agents never see this file.
function snapshotHistory(totalValue) {
  let hist = readJson(SNAPSHOT_PATH);
  if (!Array.isArray(hist)) hist = [];
  const now = Date.now();
  const last = hist[hist.length - 1];
  if (totalValue !== null && (!last || now - Date.parse(last.t) >= SNAPSHOT_MIN_GAP_MS)) {
    hist.push({ t: new Date(now).toISOString(), v: Number(totalValue.toFixed(2)) });
    if (hist.length > SNAPSHOT_MAX_POINTS) hist = hist.slice(-SNAPSHOT_MAX_POINTS);
    try {
      fs.mkdirSync(TASKS_DIR, { recursive: true });
      fs.writeFileSync(SNAPSHOT_PATH, JSON.stringify(hist));
    } catch { /* read-only fs is fine, just skip history */ }
  }
  return hist;
}

function build() {
  const db = openDb();
  const agents = listAgents().map(buildAgent);

  const withPortfolio = agents.filter(a => a.has_portfolio && a.value !== null);
  const totalValue = withPortfolio.length ? withPortfolio.reduce((s, a) => s + a.value, 0) : null;
  const totalStart = withPortfolio.reduce((s, a) => s + (a.starting_cash ?? 0), 0) || null;
  const totalPnl = totalValue !== null && totalStart !== null ? totalValue - totalStart : null;
  const openPositions = agents.reduce((s, a) => s + a.positions.length, 0);

  let queue = { total: 0, byStatus: {} };
  try {
    const rows = db.prepare('SELECT status, COUNT(*) n FROM tasks GROUP BY status').all();
    queue.byStatus = Object.fromEntries(rows.map(r => [r.status, r.n]));
    queue.total = rows.reduce((s, r) => s + r.n, 0);
  } catch { /* ignore */ }

  const spend = queueSpend(db);
  const history = snapshotHistory(totalValue);
  const daily = dailyActivity(db, agents);   // must run BEFORE the db is closed
  const stale = agents.filter(a => a.status === 'stale').length;

  db.close();

  return {
    generated_at: new Date().toISOString().replace('T', ' ').slice(0, 19),
    health: {
      api: 'up',
      queue: queue.total >= 0 ? 'ok' : 'error',
      agents_total: agents.length,
      agents_stale: stale,
      market_open: usMarketOpen(),
      status: stale > 0 ? 'degraded' : 'healthy',
    },
    kpis: {
      total_value: totalValue,
      total_pnl: totalPnl,
      total_pnl_pct: totalPnl !== null && totalStart ? (totalPnl / totalStart) * 100 : null,
      open_positions: openPositions,
      spend_today: spend.total_today,
      has_cost_data: spend.has_cost_data,
      runs_total: agents.reduce((s, a) => s + (a.runs ?? 0), 0),
    },
    agents,
    spend,
    queue,
    limits: readLimits(),
    charts: {
      value_series: history,
      daily,
    },
  };
}

module.exports = { build };
