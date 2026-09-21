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

// A slug comes from emily-new-build.py's slugify(), so it is already
// [a-z0-9-]. Anything else is not a build we made.
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,79}$/;
const IMAGE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.(png|jpg|jpeg|webp)$/;

const TYPES = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
};

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
    if (IMAGE_RE.test(e.name)) images.push({ name: e.name, bytes: st.size });
  }
  images.sort((a, b) => {
    // cover first, then design, then mockups in name order.
    const rank = n => (n.startsWith('cover') ? 0 : n.startsWith('design') ? 1 : 2);
    return rank(a.name) - rank(b.name) || a.name.localeCompare(b.name);
  });

  const build = readJson(path.join(dir, 'build.json')) || {};
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
    status: build.status || (Object.keys(build).length ? 'unknown' : 'in_progress'),
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

function register(app) {
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

  app.get('/api/emily/builds/:slug/file/:name', (req, res) => {
    const full = safeJoin(req.params.slug, req.params.name);
    if (!full) return res.status(400).json({ error: 'bad path' });
    let st;
    try { st = fs.statSync(full); } catch { return res.status(404).json({ error: 'not found' }); }
    if (!st.isFile()) return res.status(404).json({ error: 'not found' });
    res.type(TYPES[path.extname(full).toLowerCase()] || 'application/octet-stream');
    // The artwork changes when Emily rebuilds a slug, so revalidate rather
    // than let a phone cache yesterday's design forever.
    res.set('Cache-Control', 'no-cache');
    fs.createReadStream(full).pipe(res);
  });
}

module.exports = { register, listBuilds, describe };
