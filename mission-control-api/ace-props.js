'use strict';

// Ace Props' page (owner, 2026-10-26): NBA points milestones by plain code,
// on Ace's bankroll and out of his record. Read-only - the board, the book
// and every number come from scripts/ace-props.py, which bets on its own
// timers (ace-props, ace-props-close), never from here.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const SCRIPT = path.join(ROOT, 'scripts', 'ace-props.py');

function register(app) {
  app.get('/api/ace-props', (req, res) => {
    execFile('python3', [SCRIPT, 'board', '--json'], { cwd: ROOT, timeout: 30000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        let d = null;
        try { d = err ? null : JSON.parse(stdout); } catch { d = null; }
        if (!d) return res.status(500).json({ error: String(stderr || err || 'unreadable').slice(-300) });
        res.json(d);
      });
  });
}

module.exports = { register };
