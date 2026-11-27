/* busy.js - a loading sign on anything a tap makes the page wait for.

   Owner, 2026-10-23: "Can you add a loading function to anything that has to
   load, like approving Scout's idea". Approving an idea draws its design
   first - up to three minutes - and the page showed nothing while it did, so
   it looked frozen.

   One file, loaded by every page, so no button can be missed: it watches
   fetch() itself. A request that does something (POST, PUT, PATCH, DELETE)
   and starts just after a tap - or after the confirm/prompt box that tap
   opened - belongs to that tap. While it is out,
   the tapped button shows a spinner and is disabled, and after a moment a
   "Working..." pill counts the seconds - so a slow job reads as working, not
   stuck. Polls and page loads nobody tapped for show nothing.

   A button can name what it is doing: data-busy="Drawing the design - up to 3 minutes".
   Pages that redraw their buttons while waiting lose the spinner on the old
   button; the pill still shows. */
(function () {
  if (window.__busyInstalled || !window.fetch) return;
  window.__busyInstalled = true;

  var GESTURE_MS = 1500;     // a fetch this soon after a tap is the tap's
  var SHOW_AFTER_MS = 350;   // quicker than this needs no sign at all
  var SLOW_MS = 8000;        // past this the pill says it is a long one
  var tap = null;            // {el, at}
  var pending = [];          // {label, started}
  var pill = null, timer = null;

  function control(t) {
    return t && t.closest && t.closest('button, a, [role="button"], input[type="submit"], input[type="button"], summary');
  }
  document.addEventListener('click', function (e) {
    tap = { el: control(e.target), at: Date.now() };
  }, true);
  // A confirm or prompt between the tap and the request (Approve asks for a
  // note first) must not let the tap go stale while the box is open.
  ['confirm', 'prompt'].forEach(function (name) {
    var real = window[name];
    if (typeof real !== 'function') return;
    window[name] = function () {
      var out = real.apply(window, arguments);
      if (tap) tap.at = Date.now();
      return out;
    };
  });

  var css = document.createElement('style');
  css.textContent =
    '@keyframes busyspin{to{transform:rotate(360deg)}}' +
    '.busy-spin{display:inline-block;width:.85em;height:.85em;margin-right:.45em;vertical-align:-.1em;' +
    'flex:none;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:busyspin .7s linear infinite}' +
    '.is-busy{opacity:.75;cursor:progress!important}' +
    '#busy-pill{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:2147483000;display:none;' +
    'align-items:center;gap:8px;width:max-content;max-width:calc(100vw - 32px);box-sizing:border-box;padding:10px 16px;border-radius:999px;' +
    'font:600 14px/1.3 system-ui,-apple-system,sans-serif;color:#e8eefc;background:rgba(17,24,39,.96);' +
    'border:1px solid rgba(148,163,184,.35);box-shadow:0 6px 24px rgba(0,0,0,.45)}' +
    '#busy-pill.on{display:flex}#busy-pill .busy-t{color:#94a3b8;font-weight:500;white-space:nowrap}';
  (document.head || document.documentElement).appendChild(css);

  function drawPill() {
    if (!pill) {
      pill = document.createElement('div');
      pill.id = 'busy-pill';
      pill.setAttribute('role', 'status');
      pill.setAttribute('aria-live', 'polite');
      document.body.appendChild(pill);
    }
    var now = Date.now();
    var live = pending.filter(function (p) { return now - p.started >= SHOW_AFTER_MS; });
    if (!live.length) { pill.classList.remove('on'); return; }
    var p = live[0], secs = Math.floor((now - p.started) / 1000);
    var label = p.label || (now - p.started > SLOW_MS ? 'Still working - this one takes a while' : 'Working');
    pill.innerHTML = '<span class="busy-spin"></span><span></span><span class="busy-t"></span>';
    pill.children[1].textContent = label + '…';
    pill.children[2].textContent = secs >= 2 ? secs + 's' : '';
    pill.classList.add('on');
  }
  function tick() {
    drawPill();
    if (pending.length) timer = setTimeout(tick, 500); else timer = null;
  }

  function hold(el) {
    if (!el || !el.isConnected || el.classList.contains('is-busy')) return null;
    var spin = document.createElement('span');
    spin.className = 'busy-spin';
    el.insertBefore(spin, el.firstChild);
    var was = el.disabled;
    el.classList.add('is-busy');
    el.setAttribute('aria-busy', 'true');
    if ('disabled' in el) el.disabled = true;
    return function () {
      spin.remove();
      el.classList.remove('is-busy');
      el.removeAttribute('aria-busy');
      if ('disabled' in el) el.disabled = was;
    };
  }

  // Only a request that does something - POST, PUT, PATCH, DELETE - is a
  // tap's. Every refresh here is a GET, and one fired in the moment between
  // a prompt closing and the tap's own request took the tap: the slow job
  // then had no sign at all (found by the browser test, 2026-10-23).
  function method(args) {
    var init = args[1], req = args[0];
    var m = (init && init.method) || (req && typeof req === 'object' && req.method) || 'GET';
    return String(m).toUpperCase();
  }

  var realFetch = window.fetch;
  window.fetch = function () {
    var p = realFetch.apply(this, arguments);
    if (!tap || Date.now() - tap.at > GESTURE_MS || method(arguments) === 'GET') return p;
    var t = tap;
    tap = null;                                   // one tap, one request
    var job = { label: t.el && t.el.dataset ? (t.el.dataset.busy || '') : '', started: Date.now() };
    var release = hold(t.el);
    pending.push(job);
    if (!timer) timer = setTimeout(tick, SHOW_AFTER_MS);
    var done = function () {
      pending = pending.filter(function (j) { return j !== job; });
      if (release) release();
      drawPill();
    };
    p.then(done, done);
    return p;
  };
})();
