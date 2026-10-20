'use strict';

// The trading coach's page: the journal, the daily analyses and the weekly
// reviews, exactly as scripts/coach.py saved them. Read-only - the coach
// writes on its own timers (coach-daily, coach-weekly), never from here.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const SCRIPT = path.join(ROOT, 'scripts', 'coach.py');

function register(app) {
  app.get('/api/coach', (req, res) => {
    execFile('python3', [SCRIPT, 'show', '--json'], { cwd: ROOT, timeout: 30000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        let d = null;
        try { d = err ? null : JSON.parse(stdout); } catch { d = null; }
        if (!d) return res.status(500).json({ error: String(stderr || err || 'unreadable').slice(-300) });
        res.json(d);
      });
  });
}

module.exports = { register };
