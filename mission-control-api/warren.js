'use strict';

// Warren's page: the owner's Before-You-Buy Checklist, filled in (owner,
// 2026-11-19). The owner types the ticker, amount, months and goal; warren.py
// does the research and every number.
//
// No rules here. Everything comes from `python3 scripts/warren.py deck|show`,
// and Check runs `warren.py check` as one job at a time - it reads SEC files
// and asks Opus one question, so it takes a minute or two.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const SYM_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
const JOB_TIMEOUT_MS = 15 * 60 * 1000;

let job = null;

function python(args, timeout = 60000) {
  return new Promise(resolve => {
    execFile('python3', [path.join(ROOT, 'scripts', 'warren.py'), ...args], {
      cwd: ROOT, timeout, maxBuffer: 16 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({
      ok: !err, stdout: String(stdout || ''), stderr: String(stderr || ''),
    }));
  });
}

function said(r) {
  const lines = String(r.stderr || r.stdout || '').trim().split('\n');
  return lines.slice(-3).join('\n').slice(0, 500);
}

function sym(v) {
  const s = String(v || '').trim().toUpperCase();
  return SYM_RE.test(s) ? s : null;
}

function num(v, lo, hi) {
  const n = Number(v);
  return Number.isFinite(n) && n >= lo && n <= hi ? n : null;
}

function register(app) {
  app.get('/api/warren', async (req, res) => {
    const r = await python(['deck']);
    if (!r.ok) return res.json({ ok: false, error: said(r) || 'warren.py deck failed', job });
    try { res.json(Object.assign({ ok: true, job }, JSON.parse(r.stdout))); }
    catch { res.json({ ok: false, error: 'warren.py deck printed something that is not JSON', job }); }
  });

  app.get('/api/warren/check/:symbol', async (req, res) => {
    const s = sym(req.params.symbol);
    if (!s) return res.status(400).json({ error: 'a ticker is letters, like KMI' });
    const r = await python(['show', s]);
    if (!r.ok) return res.status(404).json({ ok: false, error: said(r) });
    try { res.json(JSON.parse(r.stdout)); } catch { res.status(500).json({ error: 'not JSON' }); }
  });

  app.post('/api/warren/check', (req, res) => {
    const b = req.body || {};
    const s = sym(b.symbol), amount = num(b.amount, 1, 10000000), months = num(b.months, 1, 600);
    const goal = ['growth', 'dividends', 'both'].includes(b.goal) ? b.goal : 'both';
    const hold = ['yes', 'no', 'unsure'].includes(b.hold_drop) ? b.hold_drop : 'unsure';
    const why = String(b.why || '').replace(/\s+/g, ' ').trim().slice(0, 300);
    if (!s || amount === null || months === null) {
      return res.status(400).json({ error: 'need a ticker, an amount and a number of months' });
    }
    if (job && job.state === 'running') return res.status(409).json({ error: `Warren is already checking ${job.symbol}`, job });
    job = { state: 'running', symbol: s, started_utc: new Date().toISOString(), finished_utc: null, error: null };
    const mine = job;
    (async () => {
      const r = await python(['check', s, '--amount', String(amount), '--months', String(Math.round(months)),
        '--goal', goal, `--why=${why}`, '--hold-drop', hold], JOB_TIMEOUT_MS);
      mine.state = r.ok ? 'done' : 'failed';
      mine.error = r.ok ? null : said(r);
      mine.finished_utc = new Date().toISOString();
    })();
    res.status(202).json({ job });
  });

  app.post('/api/warren/holdings', async (req, res) => {
    const b = req.body || {};
    const s = sym(b.symbol);
    const action = b.action === 'remove' ? 'remove' : 'add';
    if (!s) return res.status(400).json({ error: 'a ticker is letters, like KMI' });
    const args = ['holdings', action, s];
    if (action === 'add') {
      const amount = num(b.amount, 0, 100000000);
      if (amount === null) return res.status(400).json({ error: 'how much is in it?' });
      args.push(String(amount));
      if (b.sector) args.push(`--sector=${String(b.sector).slice(0, 40)}`);
      if (b.avg_price !== undefined && b.avg_price !== '' && b.avg_price !== null) {
        const avg = num(b.avg_price, 0.0001, 999999);
        if (avg === null) return res.status(400).json({ error: 'the average price paid is a dollar amount per share, like 27.50' });
        args.push(`--avg-price=${avg}`);
      }
    }
    const r = await python(args);
    if (!r.ok) return res.status(409).json({ ok: false, error: said(r) });
    try { res.json(JSON.parse(r.stdout)); } catch { res.json({ ok: true }); }
  });
}

module.exports = { register, _reset: () => { job = null; } };
