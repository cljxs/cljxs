'use strict';

// Bolt's desk: the crypto paper book, from `bolt.py board` - the one place
// its numbers are worked out. Nothing here adds, ranks or rounds. His daily
// reports come through /api/reports/bolt like every agent's.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const BOLT = path.join(ROOT, 'scripts', 'bolt.py');

function register(app) {
  app.get('/api/bolt/board', (req, res) => {
    execFile('python3', [BOLT, 'board'], { cwd: ROOT, timeout: 30000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return res.status(500).json({ error: String(stderr || err.message).trim().slice(0, 400) });
        try { res.set('Cache-Control', 'no-cache'); res.json(JSON.parse(stdout)); }
        catch { res.status(500).json({ error: 'bolt.py board printed something that is not JSON' }); }
      });
  });
}

module.exports = { register };
