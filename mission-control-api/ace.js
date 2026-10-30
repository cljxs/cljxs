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
const { execFile } = require('child_process');
const { AGENTS_DIR, ROOT } = require('./db');
// THE starting-capital rule, imported rather than written a second time.
// This file used to fall back to the literal 10000, which is why Ace's own
// panel showed a confident percentage while the Deck showed none - two
// pieces of code deciding one thing, and they disagreed.
const { startingCapital } = require('./dashboard-data');

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
    // Which game and which side of it: the fetcher writes both, and the
    // judge copies them, so the board can pair a team with its opponent.
    event_id: c.event_id != null ? String(c.event_id) : null,
    side: c.side === 'home' || c.side === 'away' ? c.side : null,
    price: num(c.price) ?? num(c.odds),
    novig_pct: num(c.novig_pct) ?? num(c.novig),
    sharp_pct: num(c.sharp_pct),
    my_pct: num(c.my_pct) ?? num(c.estimate),
    edge_pts: num(c.edge_pts) ?? num(c.edge),
    // Expected value at Ace's estimate, and whether it clears the bar - both
    // written by ace-judge.py, which owns the bar, so this page never holds a
    // copy of the threshold.
    ev_pct: num(c.ev_pct),
    // His estimate after the judge pulls it toward the market - the number
    // the bar is actually measured on.
    p_used: num(c.p_used),
    clears_bar: c.clears_bar === true,
    fresh_injuries: Array.isArray(c.fresh_injuries) ? c.fresh_injuries : [],
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
      p_used: n.p_used ?? prev.p_used,
      event_id: prev.event_id ?? n.event_id,
      side: prev.side ?? n.side,
      edge_pts: n.edge_pts ?? prev.edge_pts,
      ev_pct: n.ev_pct ?? prev.ev_pct,
      clears_bar: n.clears_bar || prev.clears_bar,
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
  // Was `?? 10000` - a literal nobody had read from anywhere, and the reason
  // this panel showed Ace a confident percentage while the Deck showed none.
  // THE starting-capital rule is dashboard-data's, imported rather than
  // written a second time; it falls back to the seed file, which is a real
  // recorded value, and to nothing after that.
  const start = startingCapital(DIR, bank);

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

// ---------------------------------------------------------------- the board
//
// The same rows as build(), arranged the way a person reads a slate: one card
// per game, away team above home team, each side carrying Ace's call on it -
// a bet and why, or a pass and why. Team names, records and form come from
// the context file ace-fetch.py wrote for that game; nothing is re-derived.

const SPORT_RE = /^[a-z]{2,8}$/;
const EVENT_RE = /^[0-9]{1,15}$/;

function contextOf(sport, eventId) {
  if (!SPORT_RE.test(String(sport || '')) || !EVENT_RE.test(String(eventId || ''))) return null;
  return readJson(path.join(DIR, 'data', 'context', `${sport}-${eventId}.json`));
}

function teamOf(t, ctx) {
  if (!t) return null;
  const abbr = typeof t === 'string' ? t : t.abbr;
  const form = ((ctx && ctx.last_five) || [])
    .filter(g => g && g.team === abbr && g.result).map(g => String(g.result).slice(0, 1)).slice(0, 5);
  return {
    abbr: abbr || null,
    name: (typeof t === 'object' && t.name) || null,
    record: (typeof t === 'object' && t.record) || null,
    form,
  };
}

// Away and home for one game: from its context file when there is one, and
// otherwise from the match string ESPN writes as "AWAY @ HOME" - settled bets
// can outlive the context file of their game.
function matchupOf(sport, eventId, match) {
  const ctx = contextOf(sport, eventId);
  if (ctx && ctx.home && ctx.away) {
    const odds = ctx.odds || {};
    return {
      away: teamOf(ctx.away, ctx), home: teamOf(ctx.home, ctx),
      name: ctx.name || null, venue: ctx.venue || null,
      weather: ctx.weather && (ctx.weather.temp_f != null || ctx.weather.conditions) ? ctx.weather : null,
      line: odds.details || null, total: num(odds.over_under), book: odds.book || null,
    };
  }
  const m = /^\s*(\S.*?)\s+@\s+(\S.*?)\s*$/.exec(String(match || ''));
  if (!m) return null;
  return { away: teamOf(m[1], null), home: teamOf(m[2], null), name: null, venue: null,
           weather: null, line: null, total: null, book: null };
}

function sideRow(c) {
  return {
    selection: c.selection, price: c.price, novig_pct: c.novig_pct, sharp_pct: c.sharp_pct,
    my_pct: c.my_pct, p_used: c.p_used, edge_pts: c.edge_pts, ev_pct: c.ev_pct,
    clears_bar: c.clears_bar, status: c.status, why: c.why_not, stake: c.stake, result: c.result,
  };
}

function withMatchup(b) {
  const side = b.side === 'home' || b.side === 'away' ? b.side : null;
  return { ...b, side, matchup: matchupOf(b.sport, b.event_id, b.match) };
}

function board() {
  const d = build();
  const games = new Map();
  for (const c of d.candidates) {
    const k = c.event_id ? `${c.sport}|${c.event_id}` : `m|${c.match}`;
    if (!games.has(k)) {
      games.set(k, { key: k, sport: c.sport, event_id: c.event_id, match: c.match,
                     starts_utc: c.starts_utc, sides: {}, injuries: [] });
    }
    const g = games.get(k);
    if (c.side) g.sides[c.side] = sideRow(c);
    for (const inj of c.fresh_injuries) {
      if (inj && !g.injuries.some(x => x.id && x.id === inj.id)) g.injuries.push(inj);
    }
  }
  const out = [...games.values()].map(g => {
    const calls = Object.values(g.sides).map(s => s.status);
    const verdict = calls.includes('bet') ? 'bet'
      : calls.length && calls.every(x => x === 'passed') ? 'pass' : 'open';
    return { ...g, matchup: matchupOf(g.sport, g.event_id, g.match), verdict };
  }).sort((a, b) => String(a.starts_utc || '').localeCompare(String(b.starts_utc || ''))
                 || String(a.match || '').localeCompare(String(b.match || '')));

  const bank = readJson(path.join(DIR, 'state', 'bankroll.json'));
  const allSettled = (bank && Array.isArray(bank.settled_bets)) ? bank.settled_bets : [];
  const record = { won: 0, lost: 0, push: 0, void: 0 };
  for (const s of allSettled) if (s && s.result in record) record[s.result] += 1;
  const openBets = d.open_bets.map(withMatchup);

  return {
    available: d.available, day: d.day, slot: d.slot, asof_utc: d.asof_utc,
    judged: d.judged, verdict: d.verdict,
    bankroll: d.bankroll && {
      ...d.bankroll, record,
      // Straight from the book's own rows: what is staked right now, and
      // what settled bets have returned. Sums, not a second ledger.
      at_risk: openBets.reduce((t, b) => t + (num(b.stake) || 0), 0),
      settled_profit: allSettled.reduce((t, s) => t + (num(s.profit) || 0), 0),
      // Ace Props shares this bankroll and is listed apart in it (ace-book.py):
      // its result is shown beside his, never inside his record.
      props_profit: ((bank && bank.props_settled_bets) || []).reduce((t, s) => t + (num(s.profit) || 0), 0),
      props_count: ((bank && bank.props_settled_bets) || []).length + ((bank && bank.props_open_bets) || []).length,
    },
    counts: { games: out.length, bets: out.filter(g => g.verdict === 'bet').length,
              passes: out.filter(g => g.verdict === 'pass').length,
              open: out.filter(g => g.verdict === 'open').length },
    games: out,
    open_bets: openBets,
    settled_bets: d.settled_bets.map(withMatchup),
    by_reason: d.by_reason.slice(0, 8),
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
    rows.sort((a, b) => (a.actual === null ? 0 : 1) - (b.actual === null ? 0 : 1)
                      || String(b.forecast_utc || '').localeCompare(String(a.forecast_utc || '')));
    const graded = rows.filter(r => r.actual !== null && r.actual !== undefined);
    res.json({
      available: true,
      counts: {
        total: rows.length,
        pending: rows.length - graded.length,
        graded: graded.length,
        coarse: rows.filter(r => r.coarse).length,
        markets: new Set(rows.map(r => r.market)).size,
      },
      // The market catalogue rides along with the forecasts rather than
      // being restated here: props-forecast.py writes it on every save, so a
      // market added there cannot leave a tab missing in the village.
      markets: (data.markets && typeof data.markets === 'object') ? data.markets : {},
      forecasts: rows,
    });
  });

  app.get('/api/ace/board', (req, res) => {
    const d = board();
    if (!d.available) {
      return res.json({ available: false, reason: 'Ace has not written a ledger yet - it writes one each cycle', games: [] });
    }
    res.json(d);
  });

  // The no-AI bettors beside him (owner, 2026-10-29: a tab each on his
  // page). Every record and pick is graded by ace-baselines.py - the same
  // closes and results as Ace - and only passed through here.
  app.get('/api/ace/no-ai', (req, res) => {
    execFile('python3', [path.join(ROOT, 'scripts', 'ace-baselines.py'), 'board'],
      { cwd: ROOT, timeout: 30000, maxBuffer: 16 * 1024 * 1024 }, (err, stdout, stderr) => {
        let d = null;
        try { d = err ? null : JSON.parse(stdout); } catch { d = null; }
        if (!d) return res.status(500).json({ error: String(stderr || err || 'unreadable').slice(-300) });
        res.json(d);
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

module.exports = { register, build, board };
