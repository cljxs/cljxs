'use strict';

// Ace's shadow ledger: every game it considered, and why it did or did not bet.
//
// Read-only. The interesting output of a disciplined betting agent is mostly
// the passes - "six games, none cleared the bar" is the system working, but
// it is worthless unless you can see WHICH six and WHY. So Ace records every
// candidate, and this serves them with the aggregates computed here in code
// rather than asked of the model.

const fs = require('fs');
const path = require('path');
const { AGENTS_DIR } = require('./db');

const DIR = path.join(AGENTS_DIR, 'ace');

function readJson(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; }
}

// American odds are written "+104" as often as 104, and JSON forbids a leading
// plus on a number - so an agent writing it that way produces a string, or an
// unparseable file. Accept both rather than lose the row.
function num(v) {
  if (typeof v === 'number') return isFinite(v) ? v : null;
  if (typeof v === 'string') {
    const n = Number(v.replace(/^\+/, '').replace(/[%,$\s]/g, ''));
    return isFinite(n) && v.trim() !== '' ? n : null;
  }
  return null;
}

// Candidates may arrive slightly differently shaped depending on which cycle
// wrote them. Normalise once here so the UI never has to guess.
function normalise(c, i) {
  if (!c || typeof c !== 'object') return null;
  const why = c.why_not ?? c.whyNot ?? c.reasons ?? [];
  return {
    id: c.id ?? i + 1,
    selection: String(c.selection ?? c.pick ?? c.team ?? 'unnamed').slice(0, 120),
    sport: c.sport ?? null,
    match: c.match ?? c.game ?? null,
    starts_utc: c.starts_utc ?? c.start ?? null,
    price: num(c.price) ?? num(c.odds),
    novig_pct: num(c.novig_pct) ?? num(c.novig),
    my_pct: num(c.my_pct) ?? num(c.estimate),
    edge_pts: num(c.edge_pts) ?? num(c.edge),
    why_not: (Array.isArray(why) ? why : [why]).filter(Boolean).map(w => String(w).slice(0, 160)),
    status: c.status ?? (Array.isArray(why) && why.length ? 'passed' : 'unjudged'),
    stake: num(c.stake),
    result: c.result ?? null,
  };
}

// Counting which criterion killed each candidate is the single most useful
// view: it shows whether the bar is doing its job or is simply never passable.
function tally(rows, keyFn) {
  const out = new Map();
  for (const r of rows) {
    for (const k of [].concat(keyFn(r) ?? [])) {
      if (k === null || k === undefined || k === '') continue;
      const e = out.get(k) || { key: String(k), rows: 0, settled: 0, won: 0 };
      e.rows += 1;
      if (r.result === 'won' || r.result === 'lost') e.settled += 1;
      if (r.result === 'won') e.won += 1;
      out.set(k, e);
    }
  }
  return [...out.values()]
    .map(e => ({ ...e, hit_pct: e.settled ? Math.round((e.won / e.settled) * 1000) / 10 : null }))
    .sort((a, b) => b.rows - a.rows);
}

// One key per candidate so the fetcher's row and Ace's judgement of it line
// up. Selection plus match, lowercased - both sides write those the same way
// because the fetcher generates them and Ace copies them.
function keyOf(c) {
  return `${String(c.selection || '').toLowerCase()}|${String(c.match || '').toLowerCase()}`;
}

function build() {
  // Mechanical rows from ace-fetch.py: what was on the slate, at what price,
  // and what plain code already knows (started, unpriced, no context file).
  const base = readJson(path.join(DIR, 'data', 'candidates.json'));
  // Ace's own judgement, overlaid. If it never writes this, the board still
  // shows the slate rather than nothing - the same reason Fury's briefing is
  // assembled in code.
  const ledger = readJson(path.join(DIR, 'state', 'ledger.json'));
  const bank = readJson(path.join(DIR, 'state', 'bankroll.json'));

  const baseRows = (base && (Array.isArray(base) ? base : base.candidates)) || [];
  const judged = (ledger && (Array.isArray(ledger) ? ledger : ledger.candidates)) || [];

  const merged = new Map();
  for (const r of baseRows) { const n = normalise(r, merged.size); if (n) merged.set(keyOf(n), n); }
  for (const r of judged) {
    const n = normalise(r, merged.size);
    if (!n) continue;
    const prev = merged.get(keyOf(n));
    if (!prev) { merged.set(keyOf(n), n); continue; }
    merged.set(keyOf(n), {
      ...prev,
      my_pct: n.my_pct ?? prev.my_pct,
      edge_pts: n.edge_pts ?? prev.edge_pts,
      stake: n.stake ?? prev.stake,
      result: n.result ?? prev.result,
      status: n.status === 'unjudged' ? prev.status : n.status,
      // Keep both sets of reasons: the mechanical ones and Ace's.
      why_not: [...new Set([...prev.why_not, ...n.why_not])],
    });
  }
  const candidates = [...merged.values()];

  const open = (bank && (bank.open_bets || bank.positions)) || [];
  const settled = (bank && bank.settled_bets) || [];
  const cash = bank ? (bank.bankroll ?? bank.cash ?? bank.balance ?? null) : null;
  const start = bank ? (bank.starting_bankroll ?? bank.starting_cash ?? 10000) : null;

  const picked = candidates.filter(c => c.status === 'bet' || c.status === 'picked');

  return {
    available: !!(ledger || bank || base),
    day: (ledger && ledger.day) || (base && base.day) || null,
    slot: (ledger && ledger.slot) || null,
    asof_utc: (ledger && ledger.asof_utc) || (base && base.asof_utc) || null,
    judged: !!ledger,
    // Say the headline in words, the way the agent is told to think about it:
    // passing on everything is a result, not an absence of one.
    verdict: (ledger && ledger.verdict) ||
      (candidates.length
        ? `${picked.length ? `${picked.length} pick${picked.length === 1 ? '' : 's'}` : 'No picks'} — ` +
          `${candidates.length} candidate${candidates.length === 1 ? '' : 's'} tracked, ` +
          `${picked.length ? '' : 'nothing cleared the bar. That is the system working.'}`
        : null),
    bankroll: cash === null ? null : {
      cash,
      starting: start,
      pnl: start ? cash - start : null,
      pnl_pct: start ? (cash / start - 1) * 100 : null,
      open_count: Array.isArray(open) ? open.length : 0,
      settled_count: Array.isArray(settled) ? settled.length : 0,
    },
    counts: {
      candidates: candidates.length,
      picked: picked.length,
      passed: candidates.filter(c => c.status === 'passed').length,
      unjudged: candidates.filter(c => c.status === 'unjudged').length,
    },
    candidates,
    open_bets: Array.isArray(open) ? open : [],
    settled_bets: Array.isArray(settled) ? settled.slice(-25).reverse() : [],
    by_reason: tally(candidates, c => c.why_not),
    by_sport: tally(candidates, c => c.sport),
    by_status: tally(candidates, c => c.status),
  };
}

function register(app) {
  // Player forecasts from props-forecast.py. Read-only, and deliberately
  // carries no odds: the script that writes this holds none, and a price
  // appearing here would invite exactly the comparison Ace's instructions
  // open by warning against.
  app.get('/api/ace/forecasts', (req, res) => {
    let data;
    try {
      data = JSON.parse(fs.readFileSync(path.join(DIR, 'data', 'forecasts.json'), 'utf8'));
    } catch {
      return res.json({
        available: false,
        reason: 'no forecasts yet - run props-forecast.py forecast <event_id>',
        forecasts: [],
      });
    }
    const rows = Array.isArray(data.forecasts) ? data.forecasts : [];
    // Ungraded first (they are the live ones), then newest.
    rows.sort((a, b) => (a.actual_yards === null ? 0 : 1) - (b.actual_yards === null ? 0 : 1)
                      || String(b.forecast_utc || '').localeCompare(String(a.forecast_utc || '')));
    const graded = rows.filter(r => r.actual_yards !== null && r.actual_yards !== undefined);
    res.json({
      available: true,
      counts: {
        total: rows.length,
        pending: rows.length - graded.length,
        graded: graded.length,
        coarse: rows.filter(r => r.coarse).length,
      },
      forecasts: rows,
    });
  });

  app.get('/api/ace/ledger', (req, res) => {
    const d = build();
    if (!d.available) {
      return res.json({ available: false, reason: 'Ace has not written a ledger yet - it writes one each cycle', candidates: [] });
    }
    res.json(d);
  });
}

module.exports = { register, build };
