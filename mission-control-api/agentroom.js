'use strict';

// The Agent Room (owner, 2026-10-14): an idea in, a planner's phased plan,
// two reviewers' critiques, a revised plan, and the owner's Approve.
//
// No rules here. Everything comes from `python3 scripts/agentroom.py`: the
// page's data from `deck` and `show`, and each button runs the command that
// does it. The work itself is done by agentroom-runner.service, not by this
// server - so closing the page, or restarting the Deck, does not stop a run.
// Nothing here spends money directly: a new run is only queued, and the
// runner checks the caps before every call.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const ID_RE = /^\d{1,7}$/;
// agentroom.py checks the lengths and says why; these only keep a runaway
// body away from a process's arguments.
const TITLE_MAX = 200;
const TEXT_MAX = 9000;

function room(args, timeout = 30000) {
  return new Promise(resolve => {
    execFile('python3', [path.join(ROOT, 'scripts', 'agentroom.py'), ...args], {
      cwd: ROOT, timeout, maxBuffer: 16 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({
      ok: !err, stdout: String(stdout || ''), stderr: String(stderr || ''),
    }));
  });
}

function said(r) {
  return (r.stderr || r.stdout || '').trim().split('\n').slice(-3).join(' ').slice(0, 500);
}

function asJson(r, res) {
  if (!r.ok) return res.status(409).json({ ok: false, error: said(r) || 'agentroom.py failed' });
  try { return res.json(Object.assign({ ok: true }, JSON.parse(r.stdout))); }
  catch { return res.status(500).json({ ok: false, error: 'agentroom.py printed something that is not JSON' }); }
}

function text(v, max) {
  return typeof v === 'string' ? v.slice(0, max) : '';
}

function register(app) {
  app.get('/api/agentroom', async (req, res) => asJson(await room(['deck']), res));

  app.get('/api/agentroom/runs/:id', async (req, res) => {
    if (!ID_RE.test(req.params.id)) return res.status(400).json({ error: 'bad id' });
    asJson(await room(['show', req.params.id]), res);
  });

  app.post('/api/agentroom/runs', async (req, res) => {
    const b = req.body || {};
    // `--name=value`, never `--name value`: an idea or a note that starts
    // with a dash would otherwise be read as a flag.
    const args = ['new', `--title=${text(b.title, TITLE_MAX)}`, `--idea=${text(b.idea, TEXT_MAX)}`];
    if (b.dry === true) args.push('--dry');
    asJson(await room(args), res);
  });

  app.post('/api/agentroom/runs/:id/:action', async (req, res) => {
    const { id, action } = req.params;
    if (!ID_RE.test(id)) return res.status(400).json({ error: 'bad id' });
    if (action === 'approve' || action === 'stop') return asJson(await room([action, id]), res);
    if (action === 'sendback') {
      return asJson(await room(['sendback', id, `--note=${text((req.body || {}).note, TEXT_MAX)}`]), res);
    }
    res.status(404).json({ error: 'no such action' });
  });
}

module.exports = { register };
