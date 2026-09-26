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
      timeout: 60000,
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

function register(app, port) {
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
