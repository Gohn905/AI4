/* Sun Shine: deviation log (judge feedback: highlight deviations and off-track performance, tied to roles).
   One shared store in this browser (localStorage 'sundev_log'). Anything that detects a deviation calls SunDev.add(...).
   Today the Operations console feeds it (same rules as its "Deviations and alerts" feed). Other detectors can use the same call.
   Roles: Operator acknowledges, Analyst investigates and resolves, Approver sees open items before approving, Auditor reads everything.
   The Owner can do all. These checks run in the browser only (see handoff notes: not enforced on a server). */
(function (root) {
  'use strict';
  var KEY = 'sundev_log', MAXN = 200;
  var ACT = { ack: ['owner', 'operator'], investigate: ['owner', 'analyst'], resolve: ['owner', 'analyst'] };
  var NAME = { owner: 'Owner', analyst: 'Analyst', operator: 'Operator', approver: 'Approver', auditor: 'Auditor', system: 'System' };
  var TPL = {
    load: {
      above: { why: 'Demand is higher than planned. More grid import, so the energy bill and the billed peak can rise.', did: 'Forecast corrected for the rest of the day and the plan re-checked.', next: 'Operator: check for unplanned equipment. Analyst: check whether the demand forecast needs retraining.' },
      below: { why: 'Demand is lower than planned. The battery and flexible load may be reserved for a peak that will not come.', did: 'Forecast corrected for the rest of the day and the plan re-checked.', next: 'Operator: check for a stopped line or a meter fault. Analyst: review the demand forecast.' }
    },
    pv: {
      above: { why: 'Solar is higher than planned. The battery may fill earlier and some solar may go unused.', did: 'Forecast corrected for the rest of the day and the plan re-checked.', next: 'Analyst: check the solar forecast and panel data.' },
      below: { why: 'Solar is lower than planned. More grid import, so the energy bill and the billed peak can rise.', did: 'Forecast corrected for the rest of the day and the plan re-checked.', next: 'Operator: check panels for shade or faults. Analyst: review the weather forecast.' }
    },
    twin: { any: { why: 'The digital twin no longer matches reality, so the plan may not be trustworthy.', did: 'Alert raised. The plan is based on a forecast that is not holding today.', next: 'Analyst: recalibrate the twin and check the data. Approver: hold approval until it is back within tolerance.' } },
    hw: { any: { why: 'A part of the demo factory is not behaving the way the digital twin expects (see the 3D chips).', did: 'Alert raised. The 3D model shows how far the part is from what was expected.', next: 'Operator: check the part and its wiring or light. Analyst: check whether the twin rules still fit the hardware.' } },
    empty: { any: { why: 'The battery is empty while demand is above the cap. The billed peak for the month may rise.', did: 'Alert raised. No charge is left to cut the peak.', next: 'Operator: reduce flexible load in the peak window. Approver: review the plan before approving.' } },
    cap: { any: { why: 'Grid import is above the cap. This sets a new running peak, which sets the billed demand.', did: 'Alert raised and the new running peak recorded.', next: 'Operator: shed or move flexible load now. Analyst: review the cap.' } }
  };
  var listeners = [];

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function load() { try { var a = JSON.parse(localStorage.getItem(KEY)); return Array.isArray(a) ? a : []; } catch (e) { return []; } }
  function save(a) { try { localStorage.setItem(KEY, JSON.stringify(a.slice(-MAXN))); } catch (e) { } }
  function role() { return root.sunWho || 'owner'; }
  function person() { try { return typeof root.whoName === 'function' ? root.whoName() : 'this browser'; } catch (e) { return 'this browser'; } }
  function canDo(action) { return (ACT[action] || []).indexOf(role()) >= 0; }
  function tell() { listeners.forEach(function (f) { try { f(); } catch (e) { } }); try { root.dispatchEvent(new Event('sundev')); } catch (e) { } }
  function note(text, by) {   // one line into both audit trails, when they exist
    try { if (typeof root.actLog === 'function') root.actLog(text); } catch (e) { }
    try { if (root.SunOps && root.SunOps.audit) root.SunOps.audit('deviation', text, by || role()); } catch (e) { }
  }
  function sizeFrom(text) { var m = /([\d,.]+ kW(?: \(\d+%\))?)/.exec(text || ''); return m ? m[1] : ''; }
  function stamp() { return new Date().toISOString(); }
  function hhmm(iso) { var d = new Date(iso); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); }

  var SunDev = {
    list: load,
    /* e: {kind:'load'|'pv'|'twin'|'empty'|'cap', sev:'high'|'warn', title, detail, slot, start:'HH:MM', dir:'above'|'below'|'', fac, day, source} */
    add: function (e) {
      var a = load(), key = [e.day, e.fac, e.kind, e.dir || '', e.slot].join('|');
      if (a.some(function (x) { return x.key === key; })) return null;
      var t = (TPL[e.kind] || {}); t = t[e.dir] || t.any || { why: '', did: '', next: '' };
      var r = { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 5), key: key, kind: e.kind, sev: e.sev || 'warn', title: e.title, start: e.start || '', day: e.day || '', fac: e.fac || '',
        size: sizeFrom(e.detail), sim: !!e.sim, detail: e.detail || '', why: t.why, did: t.did, next: t.next, source: e.source || '', status: 'open', recovered: '', at: stamp(), hist: [{ at: stamp(), role: 'system', by: 'system', what: 'Detected' }] };
      a.push(r); save(a); note('Deviation detected: ' + e.title + ' at ' + (e.start || '?') + (e.day ? ' on ' + e.day : ''), 'system'); tell(); return r;
    },
    /* the reading is back inside tolerance */
    recover: function (e) {
      var a = load(), ch = false;
      a.forEach(function (x) { if (x.kind === e.kind && x.day === e.day && x.fac === e.fac && !x.recovered && x.status !== 'closed') { x.recovered = e.start || hhmm(stamp()); x.hist.push({ at: stamp(), role: 'system', by: 'system', what: 'Back within tolerance at ' + (e.start || '') }); ch = true; } });
      if (ch) { save(a); tell(); }
    },
    can: canDo,
    act: function (id, action, text) {
      if (!canDo(action)) return false;
      var a = load(), r = a.filter(function (x) { return x.id === id; })[0]; if (!r || r.status === 'closed') return false;
      var to = { ack: 'acknowledged', investigate: 'investigating', resolve: 'closed' }[action], lab = { ack: 'Acknowledged', investigate: 'Investigating', resolve: 'Resolved' }[action];
      r.status = to; r.hist.push({ at: stamp(), role: role(), by: person(), what: lab + (text ? ': ' + text : '') });
      save(a); note(lab + ' deviation "' + r.title + '" (' + r.start + ')' + (text ? ': ' + text : ''), role()); tell(); return true;
    },
    /* open high-severity items that make the plan less trustworthy (used by the Approve card) */
    /* only items for the day being approved, so old console replays cannot block today's plan */
    openHigh: function () {
      var today = null, viewed = null;
      try { today = typeof TODAY !== 'undefined' ? TODAY : null; viewed = typeof cur !== 'undefined' ? cur : null; } catch (e) { }
      if (today && viewed && viewed !== today) return [];
      return load().filter(function (x) { return x.sev === 'high' && x.status !== 'closed' && (x.kind === 'twin' || x.kind === 'empty' || x.kind === 'cap') && (!today || x.day === today); });
    },
    approveWarn: function () {
      var w = SunDev.openHigh(); if (!w.length) return '';
      var tw = w.filter(function (x) { return x.kind === 'twin'; });
      return '<div class="dv-warn" role="alert"><b>' + (tw.length ? 'The digital twin is out of tolerance.' : 'There are open high-severity deviations.') + '</b> The plan may not be trustworthy. ' +
        w.slice(-3).map(function (x) { return esc(x.title) + ' since ' + esc(x.start) + (x.sim ? ' (simulated)' : ''); }).join('; ') + '. Check the deviation log, or approve only if you accept the risk.</div>';
    },
    clear: function () { save([]); tell(); },
    onChange: function (f) { listeners.push(f); },
    css: ''
  };

  function pill(r) {
    var m = { open: ['Open', 'dv-open'], acknowledged: ['Acknowledged', 'dv-ack'], investigating: ['Investigating', 'dv-inv'], closed: ['Resolved', 'dv-ok'] }[r.status];
    return '<span class="dv-pill ' + m[1] + '">' + m[0] + '</span>';
  }
  function entry(r) {
    var b = '';
    if (r.status !== 'closed') {
      if (r.status === 'open' && canDo('ack')) b += '<button data-dv="ack" data-id="' + r.id + '">Acknowledge</button>';
      if ((r.status === 'open' || r.status === 'acknowledged') && canDo('investigate')) b += '<button data-dv="investigate" data-id="' + r.id + '">Investigate</button>';
      if (canDo('resolve')) b += '<button data-dv="resolve" data-id="' + r.id + '">Resolve</button>';
    }
    var h = r.hist.slice(-4).map(function (x) { return '<li>' + esc(hhmm(x.at)) + ' ' + esc(NAME[x.role] || x.role) + (x.role !== 'system' && x.by ? ' (' + esc(x.by) + ')' : '') + ': ' + esc(x.what) + '</li>'; }).join('');
    if (noteFor && noteFor.id === r.id) b = '<label class="dv-nl">' + (noteFor.action === 'resolve' ? 'How was it resolved?' : 'What are you checking?') + ' <input id="dvNote" maxlength="200" placeholder="Short note (optional)"></label><button data-dv="save" data-id="' + r.id + '">Save</button><button data-dv="cancel">Cancel</button>';
    return '<div class="dv-item ' + (r.sev === 'high' ? 'hi' : 'wa') + '"><div class="dv-top"><span class="dv-sev">' + (r.sev === 'high' ? 'HIGH' : 'WARNING') + '</span><b>' + esc(r.title) + '</b>' + (r.sim ? '<span class="dv-sim">SIMULATED</span>' : '') + pill(r) + '</div>' +
      '<div class="dv-meta">Since <b>' + esc(r.start) + '</b>' + (r.day ? ' on ' + esc(r.day) : '') + (r.size ? ' &middot; size <b>' + esc(r.size) + '</b>' : '') + (r.recovered ? ' &middot; <span class="dv-back">back within tolerance at ' + esc(r.recovered) + '</span>' : (r.status === 'closed' ? '' : ' &middot; <b>still off</b>')) + (r.source ? ' &middot; ' + esc(r.source) : '') + '</div>' +
      '<div class="dv-row"><b>Why it matters:</b> ' + esc(r.why) + '</div><div class="dv-row"><b>What the system did:</b> ' + esc(r.did) + '</div><div class="dv-row"><b>What to do next:</b> ' + esc(r.next) + '</div>' +
      '<ul class="dv-hist">' + h + '</ul>' + (b ? '<div class="dv-btns">' + b + '</div>' : '') + '</div>';
  }
  var showAll = false, noteFor = null;
  function inner() {
    var a = load().slice().reverse(), n = { open: 0, acknowledged: 0, investigating: 0, closed: 0 };
    a.forEach(function (x) { n[x.status]++; });
    var open = n.open + n.acknowledged + n.investigating, rl = role(), mine = canDo('ack') || canDo('investigate') || canDo('resolve');
    var roleNote = rl === 'operator' ? 'You can acknowledge deviations.' : rl === 'analyst' ? 'You can investigate and resolve deviations.' : rl === 'owner' ? 'You can acknowledge, investigate and resolve.' : rl === 'approver' ? 'You read this before approving. Open items also show on the Approve card.' : 'Read-only: the whole record, with who did what and when.';
    var shown = showAll ? a : a.slice(0, 5);
    var body = a.length ? shown.map(entry).join('') + (a.length > 5 ? '<div class="dv-btns"><button data-dv="more">' + (showAll ? 'Show fewer' : 'Show all ' + a.length) + '</button></div>' : '')
      : '<p class="note" style="margin:6px 0">No deviations recorded yet. Open <b>Operations &rarr; Live replay</b> and press <b>Cloud front from now</b> or <b>Demand surge from now</b> to see one appear here.</p>' + (root.SunOps && root.SunOps.openLive ? '<button data-dv="live">Open live replay</button>' : '');
    var ex = (rl === 'owner' || rl === 'auditor' || rl === 'approver') && a.length ? '<button data-dv="export">Export log (JSON)</button>' : '';
    return '<div class="bar" style="margin:0 0 6px"><h2 style="margin:0">Deviations and off-track performance</h2><span class="sp"></span><span class="dv-chip ' + (open ? 'bad' : 'good') + '">' + (open ? open + ' open' : 'All clear') + '</span>' + ex + '</div>' +
      '<p class="note" style="margin:0 0 8px">Raised when demand is off the forecast by 20%, solar by 25%, or grid import by 15% for two readings in a row (same rules as the Operations console), or when the battery runs empty in the peak window or grid import passes the cap. ' + roleNote + '</p>' + body;
  }
  SunDev.card = function () { return '<div class="card" data-role="tech" data-tour="devlog" id="dvCard">' + inner() + '</div>'; };
  SunDev.refresh = function () { var c = root.document && root.document.getElementById('dvCard'); if (c) c.innerHTML = inner(); };
  SunDev.onChange(SunDev.refresh);

  if (root.document) {
    var st = root.document.createElement('style');
    st.textContent = '#dvCard .dv-item{border-left:5px solid #F59E0B;background:#FFFBEA;border-radius:8px;padding:8px 12px;margin:8px 0}#dvCard .dv-item.hi{border-color:#B42318;background:#FEF3F2}' +
      '.dv-top{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.dv-sev{font-size:11px;font-weight:800;letter-spacing:.06em;color:#8A5A00}.hi .dv-sev{color:#B42318}' +
      '.dv-pill{margin-left:auto;font-size:12px;font-weight:700;border-radius:99px;padding:2px 9px}.dv-open{background:#FDE8E8;color:#B42318}.dv-ack{background:#FFF0C2;color:#8A5A00}.dv-inv{background:#DCE9FB;color:#1E4FA3}.dv-ok{background:#DDF3E4;color:#116032}' +
      '.dv-meta{font-size:13px;color:var(--mut,#5B6360);margin:3px 0}.dv-back{color:#116032;font-weight:700}.dv-row{font-size:13px;margin:2px 0}.dv-hist{margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--mut,#5B6360)}' +
      '.dv-btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.dv-chip{font-size:13px;font-weight:700;border-radius:99px;padding:3px 11px;margin-right:6px}.dv-chip.bad{background:#FDE8E8;color:#B42318}.dv-chip.good{background:#DDF3E4;color:#116032}' +
      '.dv-sim{font-size:10px;font-weight:800;letter-spacing:.06em;border:1px solid #B7791F;color:#8A5A00;border-radius:99px;padding:1px 7px}.dv-nl{display:flex;gap:6px;align-items:center;font-size:13px;flex:1;min-width:220px}.dv-nl input{flex:1}.dv-warn{border-left:5px solid #B42318;background:#FEF3F2;border-radius:8px;padding:8px 12px;margin:0 0 10px;font-size:14px}';
    root.document.head.appendChild(st);
    root.document.addEventListener('click', function (e) {
      var b = e.target && e.target.closest ? e.target.closest('[data-dv]') : null; if (!b) return;
      var a = b.dataset.dv, id = b.dataset.id;
      if (a === 'more') { showAll = !showAll; SunDev.refresh(); }
      else if (a === 'live') { if (root.SunOps && root.SunOps.openLive) root.SunOps.openLive('lab', true); }
      else if (a === 'export') {
        var t = JSON.stringify({ schema: 'sunshine/deviation-log/v1', exported_at: stamp(), by: role(), entries: load() }, null, 1), l = root.document.createElement('a');
        l.href = URL.createObjectURL(new Blob([t], { type: 'application/json' })); l.download = 'deviation_log.json'; root.document.body.appendChild(l); l.click(); l.remove(); note('Exported the deviation log', role());
      }
      else if (a === 'ack') SunDev.act(id, 'ack', '');
      else if (a === 'investigate' || a === 'resolve') { noteFor = { id: id, action: a }; SunDev.refresh(); var f = root.document.getElementById('dvNote'); if (f) f.focus(); }
      else if (a === 'save') { var i = root.document.getElementById('dvNote'); if (noteFor) { SunDev.act(noteFor.id, noteFor.action, i ? i.value.trim() : ''); noteFor = null; SunDev.refresh(); } }
      else if (a === 'cancel') { noteFor = null; SunDev.refresh(); }
    });
  }
  root.SunDev = SunDev;
})(typeof window !== 'undefined' ? window : globalThis);
