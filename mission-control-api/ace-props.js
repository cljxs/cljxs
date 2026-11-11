'use strict';

// Ace Props' page (owner, 2026-10-26): NBA points milestones by plain code,
// on Ace's bankroll and out of his record. Read-only - the board, the book
// and every number come from scripts/ace-props.py, which bets on its own
// timers (ace-props, ace-props-close), never from here.
// ?sport=nfl (owner, 2026-11-11): Ace Props NFL, the same rule on NFL
// ladders, from scripts/ace-props-nfl.py and its own timers.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const SCRIPTS = { nba: path.join(ROOT, 'scripts', 'ace-props.py'),
                  nfl: path.join(ROOT, 'scripts', 'ace-props-nfl.py') };

function register(app) {
  app.get('/api/ace-props', (req, res) => {
    const script = SCRIPTS[req.query.sport === 'nfl' ? 'nfl' : 'nba'];
    execFile('python3', [script, 'board', '--json'], { cwd: ROOT, timeout: 30000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        let d = null;
        try { d = err ? null : JSON.parse(stdout); } catch { d = null; }
        if (!d) return res.status(500).json({ error: String(stderr || err || 'unreadable').slice(-300) });
        res.json(d);
      });
  });
}

module.exports = { register };
