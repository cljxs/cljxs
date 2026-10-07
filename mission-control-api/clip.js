'use strict';

// Clip's studio: the clips Clip has cut, and what Spotter, its research
// assistant, found trending.
//
// No rules here. Everything comes from `python3 -m clipper deck`, the same
// code the command line uses, so the page and the terminal cannot disagree
// about whether a creator is permitted or a moment is in your footage. The
// two buttons run the clipper commands with an argument array, never a shell
// string, behind the same trust boundary as every write route here.

const { execFile, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { ROOT } = require('./db');

const HOME = process.env.CLIPPER_HOME
  ? path.resolve(process.env.CLIPPER_HOME)
  : path.join(ROOT, 'clipper', 'var');
const CLIPS = path.join(HOME, 'clips');
// The venv's python if it exists (it has faster-whisper), else the system
// one - `deck`, `spot` and `remake` need nothing beyond the standard library.
const VENV_PY = path.join(ROOT, 'clipper', '.venv', 'bin', 'python');
const ID_RE = /^\d{1,9}$/;
// A remake renders a clip: about a minute of CPU per 30 s on the droplet.
const REMAKE_TIMEOUT_MS = 300000;
const SPOT_TIMEOUT_MS = 180000;
const APPROVE_TIMEOUT_MS = 600000;
const TEXT_MAX = 2200;                  // TikTok's caption limit; nothing a person types is longer
const PLATFORMS = ['youtube', 'tiktok'];

function reply(res, r) {
  res.status(r.code === 0 ? 200 : 400)
    .json({ ok: r.code === 0, message: (r.code === 0 ? r.stdout : r.stderr || r.stdout).trim().slice(-800) });
}

function python() {
  return fs.existsSync(VENV_PY) ? VENV_PY : 'python3';
}

function clipper(args, timeout = 30000) {
  return new Promise(resolve => {
    execFile(python(), ['-m', 'clipper', ...args], {
      cwd: ROOT, timeout, maxBuffer: 8 * 1024 * 1024,
      env: Object.assign({}, process.env, { CLIPPER_HOME: HOME }),
    }, (err, stdout, stderr) => resolve({
      code: err ? (typeof err.code === 'number' ? err.code : 1) : 0,
      stdout: String(stdout || ''), stderr: String(stderr || ''),
    }));
  });
}

function register(app) {
  app.get('/api/clip', async (req, res) => {
    const r = await clipper(['deck']);
    if (r.code !== 0) {
      return res.json({ installed: false, error: (r.stderr || r.stdout).trim().slice(-400) });
    }
    try { res.json(Object.assign({ installed: true }, JSON.parse(r.stdout))); }
    catch (e) { res.json({ installed: false, error: 'clipper deck printed something that is not JSON' }); }
  });

  // One finished clip. sendFile answers range requests, which Safari needs
  // before it will play a video at all (learned on Emily's listing videos).
  app.get('/api/clip/file/:video/:rank', (req, res) => {
    const { video, rank } = req.params;
    if (!ID_RE.test(video) || !ID_RE.test(rank)) return res.status(400).json({ error: 'bad id' });
    const full = path.join(CLIPS, video, `${String(rank).padStart(2, '0')}.mp4`);
    let st;
    try { st = fs.statSync(full); } catch { return res.status(404).json({ error: 'no such clip' }); }
    if (!st.isFile()) return res.status(404).json({ error: 'not found' });
    if (req.query.dl === '1') return res.download(full, `clip-${video}-${rank}.mp4`);
    res.sendFile(full, { headers: { 'Cache-Control': 'no-cache' } });
  });

  // Spotter's research, now. Costs YouTube quota units, not money.
  app.post('/api/clip/spot', async (req, res) => {
    const r = await clipper(['spot'], SPOT_TIMEOUT_MS);
    res.status(r.code === 0 ? 200 : 500)
      .json({ ok: r.code === 0, message: (r.code === 0 ? r.stdout : r.stderr || r.stdout).trim().slice(-600) });
  });

  // Approving records the verdict and answers; the upload runs on in the
  // background. It used to wait for the upload, and on 2026-10-07 Safari
  // gave up first ("Could not reach the API: Load failed") and the approval
  // was never recorded. publish takes a lock, so two quick approvals cannot
  // upload the same clip twice.
  app.post('/api/clip/approve/:id', async (req, res) => {
    if (!ID_RE.test(req.params.id || '')) return res.status(400).json({ ok: false, error: 'bad id' });
    const note = String((req.body && req.body.note) || '').slice(0, TEXT_MAX);
    const r = await clipper(['approve', req.params.id, '--later', ...(note ? ['--note', note] : [])],
      APPROVE_TIMEOUT_MS);
    if (r.code === 0) {
      spawn(python(), ['-m', 'clipper', 'publish'], {
        cwd: ROOT, detached: true, stdio: 'ignore',
        env: Object.assign({}, process.env, { CLIPPER_HOME: HOME }),
      }).unref();
    }
    reply(res, r);
  });

  app.post('/api/clip/reject/:id', async (req, res) => {
    if (!ID_RE.test(req.params.id || '')) return res.status(400).json({ ok: false, error: 'bad id' });
    const why = String((req.body && req.body.why) || '').trim().slice(0, TEXT_MAX);
    if (!why) return res.status(400).json({ ok: false, error: 'say why - it is what Clip learns from' });
    reply(res, await clipper(['reject', req.params.id, '--why', why]));
  });

  // The owner's own words for a post. Only fields sent are changed.
  app.post('/api/clip/edit/:id', async (req, res) => {
    if (!ID_RE.test(req.params.id || '')) return res.status(400).json({ ok: false, error: 'bad id' });
    const b = req.body || {};
    const args = ['edit', req.params.id];
    for (const k of ['title', 'caption', 'tags']) {
      if (typeof b[k] === 'string') args.push('--' + k, b[k].slice(0, TEXT_MAX));
    }
    reply(res, await clipper(args));
  });

  app.post('/api/clip/redraft/:id', async (req, res) => {
    if (!ID_RE.test(req.params.id || '')) return res.status(400).json({ ok: false, error: 'bad id' });
    reply(res, await clipper(['copy', req.params.id, '--redo'], 90000));
  });

  app.post('/api/clip/posted/:id/:platform', async (req, res) => {
    const { id, platform } = req.params;
    if (!ID_RE.test(id || '') || !PLATFORMS.includes(platform)) {
      return res.status(400).json({ ok: false, error: 'bad id or platform' });
    }
    reply(res, await clipper(['posted', id, platform]));
  });

  // Clip's own cut of a trending moment, from permitted footage. clipper
  // refuses anything not matched in a permitted source.
  app.post('/api/clip/remake/:id', async (req, res) => {
    if (!ID_RE.test(req.params.id || '')) return res.status(400).json({ ok: false, error: 'bad id' });
    const r = await clipper(['remake', req.params.id], REMAKE_TIMEOUT_MS);
    res.status(r.code === 0 ? 200 : 400)
      .json({ ok: r.code === 0, message: (r.code === 0 ? r.stdout : r.stderr || r.stdout).trim().slice(-600) });
  });
}

module.exports = { register, HOME, CLIPS };
