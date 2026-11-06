'use strict';

// The brain map: every agent and what passes between them, exactly as
// scripts/brain.py reads it from the files and databases that own it.
// Read-only - nothing here decides or sends anything.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const SCRIPT = path.join(ROOT, 'scripts', 'brain.py');

function register(app) {
  app.get('/api/brain', (req, res) => {
    execFile('python3', [SCRIPT], { cwd: ROOT, timeout: 60000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        let d = null;
        try { d = err ? null : JSON.parse(stdout); } catch { d = null; }
        if (!d) return res.status(500).json({ error: String(stderr || err || 'unreadable').slice(-300) });
        res.json(d);
      });
  });
}

module.exports = { register };
