'use strict';

// Stan's page: the software problems he found on Hacker News, to approve into
// the Agent Room or reject with a reason (owner, 2026-10-14).
//
// No rules here. Everything comes from `python3 scripts/stan.py deck`, and
// each button runs the stan.py command that does it. "Look now" runs the
// fetcher and then the cycle - the same two the timers run - in a single job
// slot; the cycle checks the money before its one AI call, as on the timer.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const ID_RE = /^S\d{1,6}$/;
const JOB_TIMEOUT_MS = 15 * 60 * 1000;

let job = null;

function python(script, args, timeout = 60000) {
  return new Promise(resolve => {
    execFile('python3', [path.join(ROOT, 'scripts', script), ...args], {
      cwd: ROOT, timeout, maxBuffer: 8 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({
      ok: !err, stdout: String(stdout || ''), stderr: String(stderr || ''),
    }));
  });
}

function tail(text, n) {
  const lines = String(text || '').trim().split('\n');
  return lines.slice(Math.max(0, lines.length - n)).join('\n');
}

function said(r) {
  return tail(r.stderr || r.stdout, 3).slice(0, 500);
}

function register(app) {
  app.get('/api/stan', async (req, res) => {
    const r = await python('stan.py', ['deck']);
    if (!r.ok) return res.json({ ok: false, error: said(r) || 'stan.py deck failed', job });
    try { res.json(Object.assign({ ok: true, job }, JSON.parse(r.stdout))); }
    catch { res.json({ ok: false, error: 'stan.py deck printed something that is not JSON', job }); }
  });

  app.post('/api/stan/problems/:id/approve', async (req, res) => {
    if (!ID_RE.test(req.params.id)) return res.status(400).json({ error: 'bad id' });
    const r = await python('stan.py', ['approve', req.params.id]);
    if (!r.ok) return res.status(409).json({ ok: false, error: said(r) });
    try { res.json(JSON.parse(r.stdout)); } catch { res.json({ ok: true }); }
  });

  app.post('/api/stan/problems/:id/reject', async (req, res) => {
    if (!ID_RE.test(req.params.id)) return res.status(400).json({ error: 'bad id' });
    const reason = String((req.body || {}).reason || '').replace(/\s+/g, ' ').trim().slice(0, 300);
    // `--reason=...`: a reason that starts with a dash is not a flag.
    const r = await python('stan.py', ['reject', req.params.id, `--reason=${reason}`]);
    res.status(r.ok ? 200 : 409).json(r.ok ? { ok: true } : { ok: false, error: said(r) });
  });

  // The fetch, then the cycle: what the two timers do each morning, now.
  app.post('/api/stan/run', (req, res) => {
    if (job && job.state === 'running') return res.status(409).json({ error: 'Stan is already looking', job });
    job = { state: 'running', started_utc: new Date().toISOString(), finished_utc: null, log: '' };
    const mine = job;
    (async () => {
      const f = await python('stan-fetch.py', [], JOB_TIMEOUT_MS);
      const c = f.ok ? await python('stan.py', ['cycle'], JOB_TIMEOUT_MS) : null;
      mine.state = f.ok && c.ok ? 'done' : 'failed';
      mine.finished_utc = new Date().toISOString();
      mine.log = tail(f.stdout + f.stderr + (c ? '\n' + c.stdout + c.stderr : ''), 20);
    })();
    res.status(202).json({ job });
  });
}

module.exports = { register, _reset: () => { job = null; } };
