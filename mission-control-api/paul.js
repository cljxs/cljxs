'use strict';

// Paul's builds in the village: what he made, how it looks on a phone and a
// computer, and the site itself to open and click through.
//
// No rules here. Everything comes from `python3 scripts/paul.py deck`, the
// same code `paul.py status` uses. Three buttons write, each by running the
// paul.py command that does it (owner, 2026-10-13): Approve + build, Reject,
// and Find prospects; since 2026-10-15 also Add/remove photos and Check
// Google. Nothing here sends, deploys or marks a pitch.

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

const ID_RE = /^\d{1,6}$/;
// paul.py's own names for a saved photo (01.jpg) and where it came from.
const PHOTO_RE = /^\d{2}\.(jpg|png)$/;
const FROM = ['facebook', 'instagram', 'google', 'website', 'owner'];
const UPLOAD_TYPES = { 'image/jpeg': '.jpg', 'image/png': '.png' };

function paul(args, timeout = 60000) {
  return new Promise(resolve => {
    execFile('python3', [path.join(ROOT, 'scripts', 'paul.py'), ...args], { cwd: ROOT, timeout },
      (err, stdout, stderr) => resolve({ ok: !err, out: (String(stdout || '') + String(stderr || '')).trim() }));
  });
}

function register(app) {
  // A prospect's number only - never a name, never anything a shell could read.
  app.post('/api/paul/prospects/:id/approve', async (req, res) => {
    if (!ID_RE.test(req.params.id)) return res.status(400).json({ error: 'bad id' });
    const r = await paul(['approve', req.params.id, '--build']);
    res.status(r.ok ? 200 : 409).json(r);
  });
  app.post('/api/paul/prospects/:id/reject', async (req, res) => {
    if (!ID_RE.test(req.params.id)) return res.status(400).json({ error: 'bad id' });
    const r = await paul(['reject', req.params.id]);
    res.status(r.ok ? 200 : 409).json(r);
  });
  // Photos the owner saved from the business's Facebook, Instagram or Google
  // listing (owner, 2026-10-15). The page sends one image at a time, already
  // resized - and re-encoded, which drops the phone's location data. The
  // bytes go to a file and `paul.py photo add` decides whether to keep it.
  app.post('/api/paul/prospects/:id/photos',
    express.raw({ type: Object.keys(UPLOAD_TYPES), limit: '7mb' }), async (req, res) => {
      const { id } = req.params, from = String(req.query.from || 'facebook');
      const ext = UPLOAD_TYPES[String(req.headers['content-type'] || '').split(';')[0]];
      if (!ID_RE.test(id) || !FROM.includes(from)) return res.status(400).json({ error: 'bad request' });
      if (!ext || !Buffer.isBuffer(req.body) || !req.body.length) return res.status(400).json({ error: 'send a JPEG or PNG' });
      const tmp = path.join(PAUL, 'state', `.upload-${process.pid}-${Date.now()}${ext}`);
      try {
        fs.mkdirSync(path.dirname(tmp), { recursive: true });
        fs.writeFileSync(tmp, req.body);
        const r = await paul(['photo', id, 'add', tmp, '--from', from]);
        res.status(r.ok ? 200 : 409).json(r);
      } finally {
        fs.rm(tmp, { force: true }, () => {});
      }
    });
  app.delete('/api/paul/prospects/:id/photos/:file', async (req, res) => {
    const { id, file } = req.params;
    if (!ID_RE.test(id) || !PHOTO_RE.test(file)) return res.status(400).json({ error: 'bad request' });
    const r = await paul(['photo', id, 'remove', file]);
    res.status(r.ok ? 200 : 404).json(r);
  });
  app.get('/api/paul/photo/:id/:file', (req, res) => {
    const { id, file } = req.params;
    if (!ID_RE.test(id) || !PHOTO_RE.test(file)) return res.status(400).json({ error: 'bad name' });
    const full = path.join(PAUL, 'state', 'photos', id, file);
    if (!fs.existsSync(full)) return res.status(404).json({ error: 'no such photo' });
    res.sendFile(full, { headers: { 'Cache-Control': 'max-age=86400' } });
  });
  // Check one prospect against its Google listing again (paul.py places).
  app.post('/api/paul/prospects/:id/google', async (req, res) => {
    if (!ID_RE.test(req.params.id)) return res.status(400).json({ error: 'bad id' });
    const r = await paul(['places', req.params.id]);
    res.status(r.ok ? 200 : 409).json(r);
  });
  app.post('/api/paul/find', async (req, res) => {
    const r = await paul(['find']);
    res.status(r.ok ? 200 : 409).json(r);
  });

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
