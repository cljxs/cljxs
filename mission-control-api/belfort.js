'use strict';

// Belfort's two books for his house in the village: his own, and the no-AI
// shadow book that runs his rules without his judgement. Both come from
// `belfort-trade.py stats --json`, the one place their numbers are worked
// out - nothing here adds, ranks or rounds. The shadow book's $10,000 is its
// own: it lives in state/shadow.json, which dashboard-data.js never reads,
// so it is in no total the Deck shows.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const TRADE = path.join(ROOT, 'scripts', 'belfort-trade.py');

function register(app) {
  // Everything the Markets page draws, from `belfort-trade.py board` - the
  // page renders it and works nothing out.
  app.get('/api/belfort/board', (req, res) => {
    execFile('python3', [TRADE, 'board'], { cwd: ROOT, timeout: 30000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return res.status(500).json({ error: String(stderr || err.message).trim().slice(0, 400) });
        try { res.set('Cache-Control', 'no-cache'); res.json(JSON.parse(stdout)); }
        catch { res.status(500).json({ error: 'belfort-trade.py board printed something that is not JSON' }); }
      });
  });

  app.get('/api/belfort/books', (req, res) => {
    execFile('python3', [TRADE, 'stats', '--json'], { cwd: ROOT, timeout: 30000 },
      (err, stdout, stderr) => {
        if (err) return res.status(500).json({ error: String(stderr || err.message).trim().slice(0, 400) });
        try { res.json(JSON.parse(stdout)); }
        catch { res.status(500).json({ error: 'belfort-trade.py stats printed something that is not JSON' }); }
      });
  });
}

module.exports = { register };
