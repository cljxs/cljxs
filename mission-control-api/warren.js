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
let newsJob = null;     // "Check news now": one at a time, like a check

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
    try { res.json(Object.assign({ ok: true, job, newsJob }, JSON.parse(r.stdout))); }
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

  // The owner's notes on a stock: add, edit, remove (2026-11-20)
  app.post('/api/warren/notes', async (req, res) => {
    const b = req.body || {};
    const s = sym(b.symbol);
    const action = ['add', 'edit', 'remove'].includes(b.action) ? b.action : null;
    if (!s || !action) return res.status(400).json({ error: 'which stock, and add, edit or remove?' });
    const args = ['notes', action, s];
    if (action !== 'add') {
      if (!/^[0-9a-f]{8}$/.test(String(b.id || ''))) return res.status(400).json({ error: 'which note?' });
      args.push(String(b.id));
    }
    if (action !== 'remove') {
      const text = String(b.text || '').trim();
      if (!text) return res.status(400).json({ error: 'the note is empty' });
      if (text.length > 4000) return res.status(400).json({ error: 'a note is at most 4000 characters' });
      args.push(`--text=${text}`);
    }
    const r = await python(args);
    if (!r.ok) return res.status(409).json({ ok: false, error: said(r) });
    try { res.json(JSON.parse(r.stdout)); } catch { res.json({ ok: true }); }
  });

  app.post('/api/warren/alerts', async (req, res) => {
    const b = req.body || {};
    const id = b.id === 'all' ? 'all' : String(b.id || '');
    if (!/^(all|[0-9a-f]{8})$/.test(id)) return res.status(400).json({ error: 'which alert?' });
    const args = ['alerts', 'seen', id];
    if (b.symbol) { const s = sym(b.symbol); if (!s) return res.status(400).json({ error: 'a ticker is letters' }); args.push(`--symbol=${s}`); }
    const r = await python(args);
    if (!r.ok) return res.status(409).json({ ok: false, error: said(r) });
    try { res.json(JSON.parse(r.stdout)); } catch { res.json({ ok: true }); }
  });

  app.post('/api/warren/news', (req, res) => {
    if (newsJob && newsJob.state === 'running') return res.status(409).json({ error: 'Warren is already reading the news', newsJob });
    newsJob = { state: 'running', started_utc: new Date().toISOString(), finished_utc: null, error: null };
    const mine = newsJob;
    (async () => {
      const r = await python(['news'], JOB_TIMEOUT_MS);
      mine.state = r.ok ? 'done' : 'failed';
      mine.error = r.ok ? null : said(r);
      mine.finished_utc = new Date().toISOString();
    })();
    res.status(202).json({ newsJob });
  });

  app.post('/api/warren/holdings', async (req, res) => {
    const b = req.body || {};
    const s = sym(b.symbol);
    const action = ['remove', 'buy', 'sell'].includes(b.action) ? b.action : 'add';
    if (!s) return res.status(400).json({ error: 'a ticker is letters, like KMI' });
    const args = ['holdings', action, s];
    const price = () => num(b.price, 0.0001, 999999);
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
    } else if (action === 'buy') {
      const spent = num(b.amount, 0.01, 100000000);
      if (spent === null || price() === null) return res.status(400).json({ error: 'how many dollars, at what price per share?' });
      args.push(String(spent), `--price=${price()}`);
    } else if (action === 'sell') {
      if (price() === null) return res.status(400).json({ error: 'at what price per share?' });
      args.push(`--price=${price()}`);
      if (b.all === true) args.push('--all');
      else {
        const shares = num(b.shares, 0.000001, 100000000);
        if (shares === null) return res.status(400).json({ error: 'how many shares did you sell?' });
        args.push(`--shares=${shares}`);
      }
    }
    const r = await python(args);
    if (!r.ok) return res.status(409).json({ ok: false, error: said(r) });
    try { res.json(JSON.parse(r.stdout)); } catch { res.json({ ok: true }); }
  });
}

module.exports = { register, _reset: () => { job = null; newsJob = null; } };
