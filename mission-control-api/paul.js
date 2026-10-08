'use strict';

// Paul's builds in the village: what he made, how it looks on a phone and a
// computer, and the site itself to open and click through.
//
// No rules here. Everything comes from `python3 scripts/paul.py deck`, the
// same code `paul.py status` uses. Read-only: nothing here sends, deploys or
// marks anything - those stay with you in the terminal.

const { execFile } = require('child_process');
const express = require('express');
const fs = require('fs');
const path = require('path');
const { ROOT } = require('./db');

const PAUL = path.join(ROOT, 'agents', 'paul');
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,80}$/;
const SHOTS = ['mobile', 'desktop'];

function deck() {
  return new Promise(resolve => {
    execFile('python3', [path.join(ROOT, 'scripts', 'paul.py'), 'deck'], {
      cwd: ROOT, timeout: 30000, maxBuffer: 4 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({ err, stdout: String(stdout || ''), stderr: String(stderr || '') }));
  });
}

function register(app) {
  app.get('/api/paul', async (req, res) => {
    const r = await deck();
    if (r.err) return res.json({ ok: false, error: (r.stderr || r.stdout || String(r.err)).trim().slice(-400) });
    try { res.json(Object.assign({ ok: true }, JSON.parse(r.stdout))); }
    catch (e) { res.json({ ok: false, error: 'paul.py deck printed something that is not JSON' }); }
  });

  // A screenshot code took of the build (outbox/<slug>/mobile.png, desktop.png).
  app.get('/api/paul/shot/:slug/:name', (req, res) => {
    const { slug, name } = req.params;
    if (!SLUG_RE.test(slug) || !SHOTS.includes(name)) return res.status(400).json({ error: 'bad name' });
    const full = path.join(PAUL, 'outbox', slug, `${name}.png`);
    if (!fs.existsSync(full)) return res.status(404).json({ error: 'no screenshot' });
    res.sendFile(full, { headers: { 'Cache-Control': 'no-cache' } });
  });

  // The built site itself, so it can be opened and clicked through on a
  // phone. A trailing slash, so its own relative links (style.css, img/...)
  // resolve inside its folder.
  app.use('/paul/site/:slug', (req, res, next) => {
    const { slug } = req.params;
    if (!SLUG_RE.test(slug)) return res.status(400).send('bad name');
    const dir = path.join(PAUL, 'sites', slug);
    if (!fs.existsSync(path.join(dir, 'index.html'))) return res.status(404).send('no such site');
    if (req.originalUrl.split('?')[0] === `/paul/site/${slug}`) return res.redirect(`/paul/site/${slug}/`);
    // fallthrough: a missing file or a path that tries to climb out of the
    // folder ends here as a plain 404, never another route or an error page.
    express.static(dir, { dotfiles: 'deny', index: 'index.html' })(req, res,
      () => res.status(404).send('not found'));
  });
}

module.exports = { register };
