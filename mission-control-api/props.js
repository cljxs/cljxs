'use strict';

// The Props Hall's game picker: this week's NFL games, and a button that runs
// props-forecast.py for one of them.
//
// Nothing here knows football. The week, its kickoff times in Eastern and the
// forecasts themselves all come from props-forecast.py, which is the one place
// that talks to ESPN - so this file cannot disagree with the script about
// which game is which.
//
// A run costs nothing but ESPN requests: no model wakes and nothing is spent.
// It still sits behind the same trust boundary as every write endpoint here
// (loopback and Tailscale only), because it writes forecasts.json.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const SCRIPT = path.join(ROOT, 'scripts', 'props-forecast.py');

// ESPN event ids are digits. Anything else is refused before it gets near a
// process - and the process is started with an argument array, never a shell
// string, so even a loosened pattern here could not become a command.
const EVENT_RE = /^\d{5,12}$/;

// A real run fetches two seasons of game logs for a dozen players; measured
// at twelve seconds for LAC @ BUF. Ten minutes is room for a slow ESPN, not
// an expected duration.
const RUN_TIMEOUT_MS = 10 * 60 * 1000;
const LOG_LINES = 200;

// One run at a time. Two runs of the same script write the same
// forecasts.json, and the later save would silently drop the earlier run's
// rows.
let job = null;

function tail(text, n) {
  const lines = String(text || '').split('\n');
  return lines.slice(Math.max(0, lines.length - n)).join('\n');
}

function python(args, timeout) {
  return new Promise(resolve => {
    execFile('python3', [SCRIPT, ...args], {
      cwd: ROOT, timeout, maxBuffer: 8 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({
      ok: !err,
      code: err ? (typeof err.code === 'number' ? err.code : 1) : 0,
      killed: !!(err && err.killed),
      stdout: String(stdout || ''),
      stderr: String(stderr || ''),
    }));
  });
}

function startRun(eventId, fixture) {
  job = {
    event_id: eventId, fixture: fixture || null, state: 'running',
    started_utc: new Date().toISOString(), finished_utc: null, code: null, log: '',
  };
  const mine = job;
  python(['forecast', eventId], RUN_TIMEOUT_MS).then(r => {
    mine.state = r.ok ? 'done' : 'failed';
    mine.code = r.code;
    mine.finished_utc = new Date().toISOString();
    mine.log = tail(r.stdout + (r.stderr ? '\n' + r.stderr : ''), LOG_LINES)
      + (r.killed ? `\n\nstopped after ${RUN_TIMEOUT_MS / 60000} minutes` : '');
  });
  return mine;
}

function register(app) {
  app.get('/api/props/week', async (req, res) => {
    const r = await python(['week', '--json'], 60000);
    if (!r.ok) {
      return res.json({ available: false, reason: tail(r.stderr || r.stdout, 5).trim()
        || 'the week could not be read', games: [] });
    }
    let d;
    try { d = JSON.parse(r.stdout); } catch {
      return res.json({ available: false, reason: 'the week came back unreadable', games: [] });
    }
    res.json({ available: true, week: d.week ?? null,
               games: Array.isArray(d.games) ? d.games : [], job });
  });

  app.post('/api/props/forecast', (req, res) => {
    const eventId = String((req.body || {}).event_id || '');
    if (!EVENT_RE.test(eventId)) return res.status(400).json({ error: 'bad event id' });
    if (job && job.state === 'running') {
      return res.status(409).json({ error: `already forecasting ${job.fixture || job.event_id}`, job });
    }
    const fixture = String((req.body || {}).fixture || '').slice(0, 40) || null;
    res.status(202).json({ job: startRun(eventId, fixture) });
  });

  app.get('/api/props/job', (req, res) => res.json({ job }));
}

// _reset is for tests: the job is module state and each test wants a clean one.
module.exports = { register, EVENT_RE, _reset: () => { job = null; } };
