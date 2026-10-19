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
const express = require('express');
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

// room() with a body on stdin - the apps' AI text never goes on a command line.
function roomStdin(args, input, timeout = 120000) {
  return new Promise(resolve => {
    const child = execFile('python3', [path.join(ROOT, 'scripts', 'agentroom.py'), ...args], {
      cwd: ROOT, timeout, maxBuffer: 4 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({
      ok: !err, stdout: String(stdout || ''), stderr: String(stderr || ''),
    }));
    child.stdin.end(input);
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
  app.get('/api/agentroom', async (req, res) => {
    const r = await room(['deck']);
    if (r.ok) {
      try {
        const d = JSON.parse(r.stdout);
        d.sites_port = Number(process.env.SITES_PORT || Number(process.env.PORT || 3001) + 1);
        return res.json(Object.assign({ ok: true }, d));
      } catch { /* asJson says so */ }
    }
    asJson(r, res);
  });

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
    if (['approve', 'stop', 'retry', 'build', 'continue'].includes(action)) {
      return asJson(await room([action, id]), res);
    }
    if (action === 'resume') {               // a note is optional here
      const note = text((req.body || {}).note, TEXT_MAX).trim();
      return asJson(await room(note ? [action, id, `--note=${note}`] : [action, id]), res);
    }
    if (action === 'sendback' || action === 'fixphase') {
      return asJson(await room([action, id, `--note=${text((req.body || {}).note, TEXT_MAX)}`]), res);
    }
    res.status(404).json({ error: 'no such action' });
  });
}

// The apps the builder makes, on their OWN port (SITES_PORT, the Deck's + 1).
// A different port is a different origin: the app's code - written by an AI -
// cannot call the Deck's buttons, which have no login of their own. Only an
// approved run's site/ folder is served, by name, never anything above it.
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,80}$/;
const siteDirs = new Map();       // slug -> [dir, read at] - agentroom.py says where

function sitesApp() {
  const app = express();
  app.disable('x-powered-by');
  app.use((req, res, next) => {
    res.set({ 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
              'Cache-Control': 'no-cache' });
    next();
  });
  // The apps' AI helper (owner, 2026-10-19): POST <app>/_ai {"text": "..."}.
  // agentroom.py app-ai does every check and the spending; this only passes
  // the text on. JSON with the X-Agent-Room-App header, from this origin
  // only: a page on another site cannot send that without a CORS preflight,
  // which is never answered - so it cannot spend the owner's daily cap.
  app.post('/:slug/_ai', express.json({ limit: '64kb', type: 'application/json' }), async (req, res) => {
    const { slug } = req.params;
    if (!SLUG_RE.test(slug)) return res.status(404).json({ ok: false, error: 'no such app' });
    if (req.get('X-Agent-Room-App') !== '1') return res.status(403).json({ ok: false, error: 'not from an app' });
    const origin = req.get('Origin');
    if (origin && origin !== `${req.protocol}://${req.get('host')}`) {
      return res.status(403).json({ ok: false, error: 'not from this server' });
    }
    const t = (req.body || {}).text;
    if (typeof t !== 'string') return res.status(400).json({ ok: false, error: 'send {"text": "..."}' });
    const r = await roomStdin(['app-ai', slug], JSON.stringify({ text: t.slice(0, 7000) }));
    try { return res.json(JSON.parse(r.stdout)); }
    catch { return res.status(502).json({ ok: false, error: said(r) || 'the AI helper failed' }); }
  });
  app.use('/:slug', async (req, res, next) => {
    const { slug } = req.params;
    if (!SLUG_RE.test(slug)) return res.status(404).send('no such app');
    let hit = siteDirs.get(slug);
    if (!hit || Date.now() - hit[1] > 30000) {
      const r = await room(['site', slug]);
      hit = [r.ok ? r.stdout.trim() : null, Date.now()];
      siteDirs.set(slug, hit);
    }
    if (!hit[0]) return res.status(404).send('no such app');
    if (req.originalUrl.split('?')[0] === `/${slug}`) return res.redirect(`/${slug}/`);
    express.static(hit[0], { dotfiles: 'deny', index: 'index.html' })(req, res,
      () => res.status(404).send('not found'));
  });
  app.get('/', (req, res) => res.type('text').send('Agent Room apps: open one from the Agent Room page.'));
  return app;
}

module.exports = { register, sitesApp };
