'use strict';

// The Shop page: today's TikTok kit to download and post, and the listing
// board - every build's money and concerns, to approve, send back or kill
// (owner, 2026-10-13).
//
// No rules here. The kits come from `tiktok-kit.py list --json` and the board
// from `store-board.py show --json`; each button runs the command that does
// it. Kill is not here at all - the page calls Emily's existing
// DELETE /api/emily/builds/:slug, which runs emily-build.py remove.
//
// Nothing here publishes or posts. Publishing is the Publish button in
// Printify and posting is the owner's thumb in TikTok, on purpose.

const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');
const { ROOT, AGENTS_DIR } = require('./db');

const KITS = path.join(AGENTS_DIR, 'emily', 'shop', 'tiktok');
// tiktok-kit.py's KIT_RE and emily's SLUG_RE, restated only as a gate: a
// name that fails here is refused before it reaches a path or a process.
const KIT_RE = /^\d{4}-\d{2}-\d{2}-\d{1,15}(-\d{1,2})?$/;
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,79}$/;
const SLIDE_RE = /^\d{2}\.jpg$/;
const KIT_FILES = { 'video.mp4': 'video/mp4', 'caption.txt': 'text/plain; charset=utf-8',
                    'audio.txt': 'text/plain; charset=utf-8' };

// A kit downloads a listing's photos and runs ffmpeg a dozen times - about
// half a minute. A revision draws the art again and drafts it - minutes.
const JOB_TIMEOUT_MS = 25 * 60 * 1000;
const LOG_LINES = 60;

// One job at a time: a kit and a revision both take the CPU for a while,
// and two kits at once would both be "today's".
let job = null;

function tail(text, n) {
  const lines = String(text || '').trim().split('\n');
  return lines.slice(Math.max(0, lines.length - n)).join('\n');
}

function python(script, args, timeout = 60000) {
  return new Promise(resolve => {
    execFile('python3', [path.join(ROOT, 'scripts', script), ...args], {
      cwd: ROOT, timeout, maxBuffer: 8 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({
      ok: !err, killed: !!(err && err.killed),
      stdout: String(stdout || ''), stderr: String(stderr || ''),
    }));
  });
}

function startJob(kind, label, script, args) {
  job = { kind, label, state: 'running', started_utc: new Date().toISOString(),
          finished_utc: null, log: '' };
  const mine = job;
  python(script, args, JOB_TIMEOUT_MS).then(r => {
    mine.state = r.ok ? 'done' : 'failed';
    mine.finished_utc = new Date().toISOString();
    mine.log = tail(r.stdout + (r.stderr ? '\n' + r.stderr : ''), LOG_LINES)
      + (r.killed ? `\n\nstopped after ${JOB_TIMEOUT_MS / 60000} minutes` : '');
  });
  return mine;
}

function busy(res) {
  if (job && job.state === 'running') {
    res.status(409).json({ error: `busy: ${job.label} - try again when it finishes`, job });
    return true;
  }
  return false;
}

function asJson(r, res, what) {
  if (!r.ok) return res.json({ available: false, reason: tail(r.stderr || r.stdout, 5) || `${what} could not be read` });
  try { return res.json(Object.assign({ available: true, job }, JSON.parse(r.stdout))); }
  catch { return res.json({ available: false, reason: `${what} came back unreadable` }); }
}

function kitDir(kit) {
  if (!KIT_RE.test(kit || '')) return null;
  const full = path.join(KITS, kit);
  return fs.existsSync(path.join(full, 'kit.json')) ? full : null;
}

function send(res, full, type, req, download) {
  let st;
  try { st = fs.statSync(full); } catch { return res.status(404).json({ error: 'not found' }); }
  if (!st.isFile()) return res.status(404).json({ error: 'not found' });
  if (download) return res.download(full, download);
  // sendFile answers range requests, which Safari needs to play a video.
  res.sendFile(full, { headers: { 'Content-Type': type,
    'Cache-Control': req.query.v ? 'max-age=31536000, immutable' : 'no-cache' } });
}

function register(app) {
  // ---- TikTok kits

  app.get('/api/shop/tiktok', async (req, res) => {
    asJson(await python('tiktok-kit.py', ['list', '--json']), res, 'the kits');
  });

  app.post('/api/shop/tiktok/make', (req, res) => {
    if (busy(res)) return;
    // "Make one now" makes another even when today's exists - the button is
    // only pressed on purpose; the timer is the once-a-day one.
    res.status(202).json({ job: startJob('kit', 'making a TikTok kit', 'tiktok-kit.py', ['make', '--again']) });
  });

  app.post('/api/shop/tiktok/:kit/posted', async (req, res) => {
    if (!kitDir(req.params.kit)) return res.status(404).json({ error: 'no such kit' });
    const undo = (req.body || {}).undo === true;
    const r = await python('tiktok-kit.py', ['posted', req.params.kit].concat(undo ? ['--undo'] : []));
    res.status(r.ok ? 200 : 409).json({ ok: r.ok, said: (r.stdout + r.stderr).trim() });
  });

  app.get('/api/shop/tiktok/:kit/slide/:name', (req, res) => {
    const dir = kitDir(req.params.kit);
    if (!dir || !SLIDE_RE.test(req.params.name)) return res.status(400).json({ error: 'bad name' });
    send(res, path.join(dir, 'slides', req.params.name), 'image/jpeg', req,
         req.query.dl === '1' ? `${req.params.kit}-${req.params.name}` : null);
  });

  app.get('/api/shop/tiktok/:kit/file/:name', (req, res) => {
    const dir = kitDir(req.params.kit);
    const name = req.params.name;
    if (!dir) return res.status(404).json({ error: 'no such kit' });
    if (name === 'kit.zip') return send(res, path.join(dir, `${req.params.kit}.zip`),
                                        'application/zip', req, `${req.params.kit}.zip`);
    if (!Object.prototype.hasOwnProperty.call(KIT_FILES, name)) return res.status(400).json({ error: 'bad name' });
    send(res, path.join(dir, name), KIT_FILES[name], req,
         req.query.dl === '1' ? `${req.params.kit}-${name}` : null);
  });

  // ---- the listing board

  app.get('/api/shop/board', async (req, res) => {
    asJson(await python('store-board.py', ['show', '--json']), res, 'the board');
  });

  app.post('/api/shop/board/:slug/approve', async (req, res) => {
    if (!SLUG_RE.test(req.params.slug || '')) return res.status(400).json({ error: 'bad name' });
    const undo = (req.body || {}).undo === true;
    const r = await python('store-board.py', ['approve', req.params.slug].concat(undo ? ['--undo'] : []));
    res.status(r.ok ? 200 : 409).json({ ok: r.ok, said: (r.stdout + r.stderr).trim() });
  });

  // Send it back. keep_art drafts the same art again (free); otherwise the
  // art is drawn again with the owner's note in its brief (one image call).
  app.post('/api/shop/board/:slug/revise', (req, res) => {
    if (!SLUG_RE.test(req.params.slug || '')) return res.status(400).json({ error: 'bad name' });
    if (busy(res)) return;
    const body = req.body || {};
    const note = String(body.note || '').replace(/\s+/g, ' ').trim().slice(0, 300);
    const args = ['revise', req.params.slug];
    if (note) args.push('--note', note);
    if (body.keep_art === true) args.push('--keep-art');
    res.status(202).json({ job: startJob('revise', `revising ${req.params.slug}`, 'store-board.py', args) });
  });

  app.get('/api/shop/job', (req, res) => res.json({ job }));
}

module.exports = { register, KIT_RE, SLUG_RE, _reset: () => { job = null; } };
