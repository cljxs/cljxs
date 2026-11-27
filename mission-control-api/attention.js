'use strict';

// The badges on the Command Deck's tabs (owner, 2026-10-16: "Can we add some
// type of notification to these categories when they need my attention?").
//
// No rules here: scripts/attention.py reads each page's own script and says
// what is waiting. Kept for 30 seconds, so a Deck open on several screens
// asks the droplet once, not once per screen.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const KEEP_MS = 30 * 1000;
let cached = null, at = 0, pending = null;

function read() {
  return new Promise(resolve => {
    execFile('python3', [path.join(ROOT, 'scripts', 'attention.py')], { cwd: ROOT, timeout: 30000, maxBuffer: 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return resolve({ ok: false, error: String(stderr || err.message).trim().slice(-300) });
        try { resolve(Object.assign({ ok: true }, JSON.parse(stdout))); }
        catch { resolve({ ok: false, error: 'attention.py printed something that is not JSON' }); }
      });
  });
}

function register(app) {
  app.get('/api/attention', async (req, res) => {
    if (cached && Date.now() - at < KEEP_MS) return res.json(cached);
    pending = pending || read().then(r => { cached = r; at = Date.now(); pending = null; return r; });
    res.json(await pending);
  });
}

module.exports = { register };
