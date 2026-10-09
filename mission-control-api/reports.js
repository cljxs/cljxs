'use strict';

// Every agent's reports, readable from the dashboard and the village.
//
// Read-only. Nothing here runs an agent, queues work, or writes a file - it
// serves markdown that is already on disk. The agents write these reports to
// explain themselves; until now both surfaces only showed the filename, which
// meant reading one still needed SSH.

const fs = require('fs');
const path = require('path');
const { AGENTS_DIR } = require('./db');

// Agent folder names are created by us, not by user input. Anything outside
// this shape is not one of ours.
const AGENT_RE = /^[a-z][a-z0-9_-]{0,31}$/;
// Every agent names reports by date: 2026-09-15.md, 2026-09-15-close.md,
// 2026-09-15-afternoon.md, 2026-09-15-HIMS.md.
const FILE_RE = /^\d{4}-\d{2}-\d{2}[A-Za-z0-9._-]{0,60}\.md$/;

function reportsDir(agent) {
  if (!AGENT_RE.test(agent || '')) return null;
  const root = path.resolve(AGENTS_DIR);
  const dir = path.resolve(root, agent, 'reports');
  // Belt and braces, as in emily.js: the regex should make traversal
  // impossible, but the resolved path is checked against the agents root
  // anyway so a future loosening cannot walk out.
  if (!dir.startsWith(root + path.sep)) return null;
  return dir;
}

function listFor(agent) {
  const dir = reportsDir(agent);
  if (!dir) return null;
  let names;
  try { names = fs.readdirSync(dir); } catch { return null; }
  return names
    .filter(n => FILE_RE.test(n))
    .map(n => {
      let st;
      try { st = fs.statSync(path.join(dir, n)); } catch { return null; }
      return { name: n, bytes: st.size, written_at: new Date(st.mtimeMs).toISOString() };
    })
    .filter(Boolean)
    // Newest first by filename, which is date-ordered by construction, then
    // by mtime so several reports on one day sort sensibly.
    .sort((a, b) => b.name.localeCompare(a.name) || b.written_at.localeCompare(a.written_at));
}

function register(app) {
  app.get('/api/reports/:agent', (req, res) => {
    const reports = listFor(req.params.agent);
    if (reports === null) {
      return res.status(404).json({ error: 'no such agent, or it has no reports folder', reports: [] });
    }
    res.json({ agent: req.params.agent, count: reports.length, reports });
  });

  app.get('/api/reports/:agent/:file', (req, res) => {
    const dir = reportsDir(req.params.agent);
    const name = req.params.file;
    if (!dir || !FILE_RE.test(name || '')) {
      return res.status(400).json({ error: 'bad agent or filename' });
    }
    const full = path.join(dir, name);
    let st;
    try { st = fs.statSync(full); } catch { return res.status(404).json({ error: 'not found' }); }
    if (!st.isFile()) return res.status(404).json({ error: 'not found' });
    // A report is prose written by an agent. Cap it so a runaway file cannot
    // be pulled into a phone browser whole.
    const MAX = 512 * 1024;
    let text = fs.readFileSync(full, 'utf8');
    let truncated = false;
    if (text.length > MAX) { text = text.slice(0, MAX); truncated = true; }
    res.set('Cache-Control', 'no-cache');
    res.json({
      agent: req.params.agent,
      name,
      bytes: st.size,
      written_at: new Date(st.mtimeMs).toISOString(),
      truncated,
      markdown: text,
    });
  });
}

module.exports = { register, listFor };
