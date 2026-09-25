'use strict';

// The Town Hall: the month's budget, and the shared knowledge the owner
// accepts or retires.
//
// This file holds no rules. The budget arithmetic is budget.py's and the
// knowledge store - its schema, its refusals, who may make an entry true -
// is knowledge.py's. Every route here runs one of those scripts, so the page
// and the command line cannot disagree about what an entry is or whether the
// month is over.
//
// The write routes sit behind the same trust boundary as every other write
// endpoint here (loopback and Tailscale only). Accepting is the owner's act;
// no agent is given this address to call.

const { execFile } = require('child_process');
const path = require('path');
const { ROOT } = require('./db');

const BUDGET = path.join(ROOT, 'scripts', 'budget.py');
const KNOWLEDGE = path.join(ROOT, 'scripts', 'knowledge.py');

// Entry ids are integers. Anything else is refused before it reaches a
// process, and the process gets an argument array, never a shell string.
const ID_RE = /^\d{1,9}$/;
const WHY_MAX = 300;

function run(script, args, timeout = 30000) {
  return new Promise(resolve => {
    execFile('python3', [script, ...args], {
      cwd: ROOT, timeout, maxBuffer: 4 * 1024 * 1024,
    }, (err, stdout, stderr) => resolve({
      code: err ? (typeof err.code === 'number' ? err.code : 1) : 0,
      stdout: String(stdout || ''),
      stderr: String(stderr || ''),
    }));
  });
}

function parse(text) {
  try { return JSON.parse(text); } catch (e) { return null; }
}

// knowledge.py prints "refused: <reason>" and exits 2 when a rule says no.
// That reason is the whole point of the refusal, so it goes back to the page.
function reply(res, r) {
  if (r.code === 0) return res.json({ ok: true, message: r.stdout.trim() });
  const why = (r.stderr.trim() || r.stdout.trim() || 'failed').replace(/^refused:\s*/, '');
  return res.status(r.code === 2 ? 400 : 500).json({ ok: false, error: why });
}

function register(app) {
  app.get('/api/townhall', async (req, res) => {
    const [b, k] = await Promise.all([
      run(BUDGET, ['--json']),
      run(KNOWLEDGE, ['list', '--json']),
    ]);
    // budget.py prints JSON even when OpenRouter is unreachable (exit 1,
    // state "unreadable"), so the page can say so instead of going blank.
    res.json({
      budget: parse(b.stdout) || { state: 'unreadable', error: b.stderr.trim() },
      knowledge: parse(k.stdout) || { entries: [], due: [], error: k.stderr.trim() },
    });
  });

  for (const verb of ['accept', 'renew']) {
    app.post(`/api/knowledge/:id/${verb}`, async (req, res) => {
      if (!ID_RE.test(req.params.id)) return res.status(400).json({ ok: false, error: 'bad id' });
      reply(res, await run(KNOWLEDGE, [verb, req.params.id]));
    });
  }

  app.post('/api/knowledge/:id/retire', async (req, res) => {
    if (!ID_RE.test(req.params.id)) return res.status(400).json({ ok: false, error: 'bad id' });
    const why = String((req.body && req.body.why) || '').trim().slice(0, WHY_MAX);
    if (!why) return res.status(400).json({ ok: false, error: 'say why it is retired' });
    reply(res, await run(KNOWLEDGE, ['retire', req.params.id, '--why', why]));
  });
}

module.exports = { register, ID_RE };
