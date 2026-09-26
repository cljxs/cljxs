'use strict';

// Emily's finished work, exposed to the dashboard and the village.
//
// Nothing here spends money, queues a task or publishes anything. It reads
// builds/<slug>/ off disk and serves the images so the artwork can be looked
// at without SSH. Publishing stays where it was - a button the user presses
// in Printify - and there is deliberately no endpoint for it here.
//
// The one thing it writes is archiving a build the owner does not want, and
// it does not know how: it runs scripts/emily-build.py, which already owns
// what removing a build means - move it to _removed/ rather than delete it,
// and refuse when a Printify product still points at it. A second
// implementation of that in JavaScript is exactly the drift this repo keeps
// paying for.

const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { ROOT, AGENTS_DIR } = require('./db');

const BUILD_SCRIPT = path.join(ROOT, 'scripts', 'emily-build.py');

const BUILDS = path.join(AGENTS_DIR, 'emily', 'builds');
// Shop-level art - the Etsy banner - kept out of builds/ so nothing that walks
// the builds (draft, status, the gallery) mistakes it for a product.
const SHOP = path.join(AGENTS_DIR, 'emily', 'shop');

// A slug comes from emily-new-build.py's slugify(), so it is already
// [a-z0-9-]. Anything else is not a build we made.
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,79}$/;
const IMAGE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.(png|jpg|jpeg|webp)$/;

const TYPES = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.mp4': 'video/mp4',
};

// Listing videos, written by listing-video.py as videos/<listing id>.mp4 with
// an index.json it owns. An Etsy listing id is digits and nothing else.
const VIDEOS = path.join(SHOP, 'videos');
const LISTING_RE = /^\d{1,15}$/;

function readJson(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; }
}

// Same read, but it says which of the two things went wrong. `readJson` maps
// "no such file" and "this file is malformed" onto the same null, and the
// gallery then drew a card with no title, no product and no price and no way
// to tell those apart - which is exactly what a build with a broken
// listing.json looked like for two days.
function readJsonOrWhy(p) {
  let raw;
  try { raw = fs.readFileSync(p, 'utf8'); } catch { return { data: null, error: null }; }
  try { return { data: JSON.parse(raw), error: null }; }
  catch (e) { return { data: null, error: String(e.message || e).slice(0, 200) }; }
}

// Belt and braces: the regex above should make traversal impossible, but the
// resolved path is checked against the builds root anyway. A future caller
// that loosens the regex then still cannot walk out of the folder.
function safeJoin(slug, name) {
  if (!SLUG_RE.test(slug || '')) return null;
  if (name !== null && name !== undefined && !IMAGE_RE.test(name)) return null;
  const root = path.resolve(BUILDS);
  const full = path.resolve(root, slug, name || '.');
  if (full !== root && !full.startsWith(root + path.sep)) return null;
  return full;
}

// Emily writes build.json last, so a build without one is either mid-flight
// or a build that died. Either way the folder is still worth showing - the
// files on disk are the truth, build.json is her account of them.
// The task queue, for the one thing a build folder cannot say about itself:
// what happened to the run that was meant to fill it. Set by register().
let DB = null;

// Emily's latest task for a build, found by the dedupe key emily-new-build
// gives every build task: emily-build-<slug>.
function taskFor(slug) {
  if (!DB) return null;
  try {
    return DB.prepare(`SELECT id, status, created_at, started_at, completed_at, result
                       FROM tasks WHERE dedupe_key = ? ORDER BY id DESC LIMIT 1`)
      .get(`emily-build-${slug}`) || null;
  } catch { return null; }
}

// A build's status, in one place.
//
// It used to be build.json's status, or "in_progress" when build.json was
// empty - so a run the dispatcher had stopped at its ten-minute limit, and
// marked FAILED in the queue, went on saying "building..." in the gallery
// for as long as anyone looked. Emily's own record still wins when she wrote
// one; when she did not, the queue says what happened.
function statusOf(build, task) {
  if (build && build.status) return build.status;
  if (!task) return 'no_record';
  if (task.status === 'pending' || task.status === 'assigned') return 'queued';
  if (task.status === 'done') return 'finished_unrecorded';
  return task.status;                     // in_progress, failed, cancelled ...
}

// Minutes since SQLite's UTC "YYYY-MM-DD HH:MM:SS", or null.
function minutesSince(utc) {
  const t = Date.parse(String(utc || '').replace(' ', 'T') + 'Z');
  return Number.isNaN(t) ? null : Math.max(0, Math.round((Date.now() - t) / 60000));
}

// The words both pages print for a build's state, and why it is stuck when
// it is. The village and the Deck each had their own copy of this, and a fix
// to one left the other saying "building..." about a run that had failed.
// { label, warn, note }
function statusWords(status, task) {
  const mins = task ? minutesSince(task.started_at) : null;
  switch (status) {
    case 'ready_for_review': return { label: 'ready for review', warn: false, note: null };
    case 'ready_local': return { label: 'local only', warn: false, note: null };
    case 'in_progress':
      return { label: mins === null ? 'building…' : `building ${mins} min`, warn: false, note: null };
    case 'queued': return { label: 'queued', warn: false, note: null };
    case 'failed':
      return { label: 'failed', warn: true,
               note: `Emily's run ended without finishing it${task && task.result
                 ? ' — ' + String(task.result).slice(0, 300) : ''}. Approve the idea again, `
                 + `or remove this folder.` };
    case 'no_record':
      return { label: 'no build record', warn: true,
               note: 'No build.json and no task in the queue for this folder - nothing is working on it.' };
    default: return { label: String(status).replace(/_/g, ' '), warn: false, note: null };
  }
}

function describe(slug) {
  const dir = safeJoin(slug, null);
  if (!dir) return null;
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return null; }

  const images = [];
  let newest = 0;
  for (const e of entries) {
    if (!e.isFile()) continue;
    let st;
    try { st = fs.statSync(path.join(dir, e.name)); } catch { continue; }
    if (st.mtimeMs > newest) newest = st.mtimeMs;
    // The URL carries the file's timestamp. Both pages redraw the gallery on
    // a timer, and served as no-cache every redraw re-downloaded every image
    // - 13 MB every 20 seconds over a tablet's VPN, so the larger ones never
    // finished and seven of eight cards sat blank. Versioned, a redraw is
    // served from the browser's cache and a rebuilt design gets a new URL.
    if (IMAGE_RE.test(e.name)) images.push({
      name: e.name, bytes: st.size,
      url: `/api/emily/builds/${encodeURIComponent(slug)}/file/${encodeURIComponent(e.name)}`
         + `?v=${Math.round(st.mtimeMs)}`,
    });
  }
  images.sort((a, b) => {
    // cover first, then design, then mockups in name order.
    const rank = n => (n.startsWith('cover') ? 0 : n.startsWith('design') ? 1 : 2);
    return rank(a.name) - rank(b.name) || a.name.localeCompare(b.name);
  });

  const build = readJson(path.join(dir, 'build.json')) || {};
  const task = build.status ? null : taskFor(slug);
  const read = readJsonOrWhy(path.join(dir, 'listing.json'));
  const listing = read.data || {};

  // Emily records the art mode in build.json, but the field name has moved
  // around in her reports. Accept the spellings she actually writes.
  const art = String(
    build.art_mode || build.mode || build.art || build.image_mode || ''
  ).toLowerCase();

  return {
    slug,
    title: listing.title || build.idea || build.title || slug.replace(/-/g, ' '),
    status: statusOf(build, task),
    status_words: statusWords(statusOf(build, task), task),
    task: task ? {
      id: task.id, status: task.status, started_at: task.started_at,
      completed_at: task.completed_at,
      note: task.result ? String(task.result).slice(0, 400) : null,
    } : null,
    art_mode: art.includes('placeholder') ? 'placeholder'
            : art.includes('generated') ? 'generated' : null,
    product_type: listing.product_type || build.product || null,
    price_suggestion: listing.price_suggestion ?? null,
    tags: Array.isArray(listing.tags) ? listing.tags.slice(0, 13) : [],
    substitutions: build.substitutions || build.ip_substitutions || null,
    printify: build.printify_product_id || build.printify || null,
    // Whether it is on sale is read back from Printify by
    // `emily-printify.py status`, not recorded by hand - a build sat at
    // published:false while the product was live and the gallery went on
    // showing READY FOR REVIEW for something a customer could buy.
    published: build.published === true,
    etsy_url: build.etsy_url || null,
    // Why there is no Printify draft, in the drafter's own words, recorded by
    // emily-finish.py. A card that says LOCAL ONLY and nothing else sends the
    // owner to the dispatcher log to find out, which is where this used to
    // live and die.
    draft_blocked: (build.draft_blocked && typeof build.draft_blocked === 'object')
      ? build.draft_blocked : null,
    listing_error: read.error,
    price_low: build.price_low ?? null,
    price_high: build.price_high ?? null,
    mockup_count: build.mockup_count ?? null,
    published_checked_at: build.published_checked_utc || null,
    images,
    file_count: entries.filter(e => e.isFile()).length,
    bytes: images.reduce((n, i) => n + i.bytes, 0),
    updated_at: newest ? new Date(newest).toISOString() : null,
  };
}

function listBuilds() {
  let dirs;
  try {
    dirs = fs.readdirSync(BUILDS, { withFileTypes: true })
      .filter(e => e.isDirectory() && SLUG_RE.test(e.name))
      .map(e => e.name);
  } catch { return null; }
  return dirs
    .map(describe)
    .filter(Boolean)
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
}

// execFile with an argument array - never a shell string - so a slug is data
// even if the regex above is ever loosened.
function runBuildScript(args) {
  return new Promise(resolve => {
    execFile('python3', [BUILD_SCRIPT, ...args], { cwd: ROOT, timeout: 30000 },
      (err, stdout, stderr) => resolve({
        ok: !err,
        code: err ? (err.code ?? 1) : 0,
        stdout: String(stdout || '').trim(),
        stderr: String(stderr || '').trim(),
      }));
  });
}

// Every listing video on disk, newest first, titled from listing-video.py's
// index. A file with no index entry is still listed - it exists - just
// without a title.
function listVideos() {
  let names = [];
  try { names = fs.readdirSync(VIDEOS); } catch { return []; }
  const index = readJson(path.join(VIDEOS, 'index.json')) || {};
  const out = [];
  for (const name of names) {
    const m = name.match(/^(\d{1,15})\.mp4$/);
    if (!m) continue;
    let st;
    try { st = fs.statSync(path.join(VIDEOS, name)); } catch { continue; }
    if (!st.isFile()) continue;
    const meta = index[m[1]] || {};
    out.push({ listing_id: m[1], title: meta.title || '', seconds: meta.seconds ?? null,
               bytes: st.size, updated_at: new Date(st.mtimeMs).toISOString() });
  }
  return out.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

function register(app, db) {
  DB = db || null;

  // What shop-level art exists, so Emily's house can show the banner and
  // cache it by its timestamp rather than re-download it every refresh.
  app.get('/api/emily/shop', (req, res) => {
    let st = null;
    try { st = fs.statSync(path.join(SHOP, 'banner.png')); } catch { /* not drawn */ }
    res.json({ banner: st && st.isFile()
      ? { bytes: st.size, updated_at: new Date(st.mtimeMs).toISOString() } : null,
      videos: listVideos() });
  });

  // One listing video. sendFile answers range requests, which Safari needs
  // before it will play a video at all; ?dl=1 asks the browser to save it.
  app.get('/api/emily/shop/video/:listing', (req, res) => {
    const id = req.params.listing;
    if (!LISTING_RE.test(id || '')) return res.status(400).json({ error: 'bad listing id' });
    const full = path.join(VIDEOS, `${id}.mp4`);
    let st;
    try { st = fs.statSync(full); } catch {
      return res.status(404).json({ error: `no video yet - run scripts/listing-video.py ${id}` });
    }
    if (!st.isFile()) return res.status(404).json({ error: 'not found' });
    if (req.query.dl === '1') return res.download(full, `listing-${id}.mp4`);
    res.sendFile(full, { headers: { 'Cache-Control': req.query.v ? 'max-age=31536000, immutable' : 'no-cache' } });
  });

  // Archive a build. The script decides whether it may go: a build with a
  // live Printify product is refused, because moving the folder would leave
  // that product in Printify with nothing here pointing at it. `force` is the
  // owner overriding that on purpose, which the dashboard makes them ask for
  // a second time rather than offering as the first button.
  app.delete('/api/emily/builds/:slug', async (req, res) => {
    const slug = req.params.slug;
    if (!SLUG_RE.test(slug || '')) return res.status(400).json({ error: 'bad slug' });
    const force = req.query.force === '1' || req.body?.force === true;

    const r = await runBuildScript(force ? ['remove', slug, '--force']
                                         : ['remove', slug]);
    if (r.ok) return res.json({ ok: true, slug, message: r.stdout });

    // The script's own refusal is the message worth showing - it names the
    // product id and what to do about it. Repeating it here in other words is
    // how the two would drift.
    const why = r.stderr || r.stdout || 'emily-build.py failed';
    // 409 is the owner's to act on, 404 is a stale page asking to remove
    // something already gone, 500 is genuinely ours. One status for all three
    // tells the dashboard nothing it can respond to differently.
    const blocked = /printify product/i.test(why);
    const missing = /no build called/i.test(why);
    res.status(blocked ? 409 : missing ? 404 : 500).json({ error: why, blocked, slug });
  });

  app.post('/api/emily/builds/:slug/restore', async (req, res) => {
    const slug = req.params.slug;
    if (!SLUG_RE.test(slug || '')) return res.status(400).json({ error: 'bad slug' });
    const r = await runBuildScript(['restore', slug]);
    if (r.ok) return res.json({ ok: true, slug, message: r.stdout });
    res.status(404).json({ error: r.stderr || r.stdout || 'could not restore' });
  });

  // What has been archived, so removing something is not a one-way door with
  // no way back except SSH.
  app.get('/api/emily/removed', (req, res) => {
    let names = [];
    try {
      names = fs.readdirSync(path.join(BUILDS, '_removed'), { withFileTypes: true })
        .filter(e => e.isDirectory() && SLUG_RE.test(e.name))
        .map(e => e.name)
        .sort();
    } catch { /* nothing has ever been removed */ }
    res.json({ removed: names });
  });

  app.get('/api/emily/builds', (req, res) => {
    const builds = listBuilds();
    if (builds === null) {
      return res.json({
        available: false,
        reason: 'no builds folder yet - Emily creates it on her first build',
        builds: [],
      });
    }
    res.json({
      available: true,
      counts: {
        total: builds.length,
        published: builds.filter(b => b.published).length,
        ready_for_review: builds.filter(b => b.status === 'ready_for_review' && !b.published).length,
        ready_local: builds.filter(b => b.status === 'ready_local').length,
        placeholder: builds.filter(b => b.art_mode === 'placeholder').length,
        blocked: builds.filter(b => b.draft_blocked || b.listing_error).length,
      },
      builds,
    });
  });

  app.get('/api/emily/builds/:slug', (req, res) => {
    const b = describe(req.params.slug);
    if (!b) return res.status(404).json({ error: 'no such build' });
    res.json(b);
  });

  // The shop banner, for saving to a tablet and uploading to Etsy. A plain
  // filename only: IMAGE_RE refuses a slash, so nothing outside SHOP is
  // reachable.
  app.get('/api/emily/shop/:name', (req, res) => {
    const name = req.params.name;
    if (!IMAGE_RE.test(name || '')) return res.status(400).json({ error: 'bad name' });
    const full = path.join(SHOP, name);
    let st;
    try { st = fs.statSync(full); } catch {
      return res.status(404).json({ error: 'not drawn yet - run scripts/emily-banner.py' });
    }
    if (!st.isFile()) return res.status(404).json({ error: 'not found' });
    res.type(TYPES[path.extname(full).toLowerCase()] || 'application/octet-stream');
    // Emily's house asks for it with ?v=<timestamp>, so a cached copy is
    // only ever the current one; a bare URL still revalidates.
    res.set('Cache-Control', req.query.v ? 'max-age=31536000, immutable' : 'no-cache');
    fs.createReadStream(full).pipe(res);
  });

  app.get('/api/emily/builds/:slug/file/:name', (req, res) => {
    const full = safeJoin(req.params.slug, req.params.name);
    if (!full) return res.status(400).json({ error: 'bad path' });
    let st;
    try { st = fs.statSync(full); } catch { return res.status(404).json({ error: 'not found' }); }
    if (!st.isFile()) return res.status(404).json({ error: 'not found' });
    res.type(TYPES[path.extname(full).toLowerCase()] || 'application/octet-stream');
    // Versioned by the file's timestamp (?v=, from describe), a cached copy is
    // always the current file, so it may be kept. A bare URL still
    // revalidates, so nothing old is ever shown under a new design.
    res.set('Cache-Control', req.query.v ? 'max-age=31536000, immutable' : 'no-cache');
    fs.createReadStream(full).pipe(res);
  });
}

module.exports = { register, listBuilds, describe, statusOf, statusWords, listVideos, LISTING_RE };
