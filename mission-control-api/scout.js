'use strict';

// Scout's idea review, exposed to the dashboard.
//
// These endpoints SPEND MONEY: approving queues Emily, who then generates
// artwork. They are safe to expose only because the API is loopback/Tailscale
// only with the firewall allowing nothing else - the same trust boundary as
// the SSH key. Do not open this port publicly.

const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');
const { ROOT, AGENTS_DIR } = require('./db');

const IDEAS = path.join(AGENTS_DIR, 'scout', 'state', 'ideas.json');
const REVIEW = path.join(ROOT, 'scripts', 'scout-review.py');

function readIdeas() {
  try {
    const j = JSON.parse(fs.readFileSync(IDEAS, 'utf8'));
    const ideas = Array.isArray(j) ? j : (j.ideas || []);
    return ideas.map(i => ({ ...i, status: i.status || 'pending' }));
  } catch { return null; }
}

// execFile with an argument array - never a shell string - so a reason
// containing quotes, semicolons or backticks is data, not code.
function runReview(args, port) {
  return new Promise(resolve => {
    execFile('python3', [REVIEW, ...args], {
      cwd: ROOT,
      // Approving draws the art before queueing Emily, and that step is
      // allowed 180s (emily-new-build), redraws included. At 60s an approval
      // from the Deck could be killed half way through its own drawing.
      timeout: 240000,
      env: { ...process.env, MISSION_CONTROL_API: `http://127.0.0.1:${port}` },
    }, (err, stdout, stderr) => {
      resolve({
        ok: !err,
        code: err ? (err.code ?? 1) : 0,
        stdout: String(stdout || '').trim(),
        stderr: String(stderr || '').trim(),
      });
    });
  });
}

// The standing focus, read from the one file scout-ideas.py writes. Shown in
// Scout's house because a focus nobody can see is a focus nobody clears.
const FOCUS = path.join(AGENTS_DIR, 'scout', 'state', 'focus.txt');
function readFocus() {
  try { return fs.readFileSync(FOCUS, 'utf8').trim() || null; } catch { return null; }
}

// THE IDEAS BUTTON. First (owner, 2026-10-16: "add a run scan option inside
// scouts house") it started scout-niches.service, the 06:30 scan - which
// measures markets and writes no ideas, so pressing it "did nothing" (owner,
// 2026-10-28: "I thought the run market scan was a get ideas now button").
// It now starts scout-ideas-now.service: that scan, then the 08:00 idea run,
// each the timer's own command. Never while any of the three is running.
const IDEAS_UNIT = 'scout-ideas-now.service';
const UNITS = [IDEAS_UNIT, 'scout-niches.service', 'scout-cycle.service'];
const SCANS = path.join(AGENTS_DIR, 'scout', 'state', 'scans');
// What the last scan did, written by niche-scan.py, and Scout's own line from
// its last idea run (scout-cycle.sh writes it, held runs included).
const LAST = path.join(AGENTS_DIR, 'scout', 'state', 'last-scan.json');
const LAST_RUN = path.join(AGENTS_DIR, 'scout', 'state', 'last-run.txt');

function systemctl(args) {
  return new Promise(resolve => execFile('systemctl', args, { timeout: 20000 },
    (err, stdout, stderr) => resolve({ ok: !err, out: String(stdout || '').trim(), err: String(stderr || err && err.message || '').trim() })));
}

function lastScan() {
  try {
    let newest = 0;
    for (const f of fs.readdirSync(SCANS)) {
      if (f.endsWith('.json')) newest = Math.max(newest, fs.statSync(path.join(SCANS, f)).mtimeMs);
    }
    return newest ? new Date(newest).toISOString() : null;
  } catch { return null; }
}

async function scanState() {
  // "activating" is a oneshot at work; anything else is not running.
  const busy = [];
  for (const u of UNITS) if ((await systemctl(['is-active', u])).out === 'activating') busy.push(u);
  let result = null, scout = null;
  try { result = JSON.parse(fs.readFileSync(LAST, 'utf8')); } catch { /* not run since this existed */ }
  try {
    const st = fs.statSync(LAST_RUN);
    scout = { line: fs.readFileSync(LAST_RUN, 'utf8').trim().slice(0, 400), at: st.mtime.toISOString() };
  } catch { /* Scout has not run */ }
  return { running: busy.length > 0, busy, unit: IDEAS_UNIT, last_scan_utc: lastScan(), result, scout };
}

function register(app, port) {
  app.get('/api/scout/scan', async (req, res) => res.json(await scanState()));

  app.post('/api/scout/scan', async (req, res) => {
    const now = await scanState();
    if (now.running) return res.status(409).json(Object.assign({ error: 'Scout is already scanning or writing ideas' }, now));
    const r = await systemctl(['start', '--no-block', IDEAS_UNIT]);
    if (!r.ok) return res.status(500).json({ error: `could not start the scan: ${r.err || 'systemctl failed'}` });
    res.status(202).json(Object.assign({ started: true }, await scanState()));
  });

  app.get('/api/scout/ideas', (req, res) => {
    const ideas = readIdeas();
    if (ideas === null) {
      return res.json({ available: false, reason: 'no ideas.json yet - Scout writes it on its first run',
                        ideas: [], focus: readFocus() });
    }
    res.json({
      available: true,
      focus: readFocus(),
      counts: {
        pending: ideas.filter(i => i.status === 'pending').length,
        approved: ideas.filter(i => i.status === 'approved').length,
        rejected: ideas.filter(i => i.status === 'rejected').length,
      },
      ideas,
    });
  });

  const idOf = req => {
    const n = Number(req.params.id);
    return Number.isInteger(n) && n > 0 && n < 1e9 ? String(n) : null;
  };

  app.post('/api/scout/ideas/:id/approve', async (req, res) => {
    const id = idOf(req);
    if (!id) return res.status(400).json({ error: 'id must be a positive integer' });
    const reason = String((req.body && req.body.reason) || 'approved from the dashboard').slice(0, 300);
    const r = await runReview(['approve', id, '--reason', reason], port);
    res.status(r.ok ? 200 : 409).json(r);
  });

  app.post('/api/scout/ideas/:id/reject', async (req, res) => {
    const id = idOf(req);
    if (!id) return res.status(400).json({ error: 'id must be a positive integer' });
    const reason = String((req.body && req.body.reason) || '').slice(0, 300).trim();
    if (!reason) return res.status(400).json({ error: 'a reason is required - Scout reads it to avoid repeating the idea' });
    const r = await runReview(['reject', id, reason], port);
    res.status(r.ok ? 200 : 409).json(r);
  });
}

module.exports = { register, readIdeas };
