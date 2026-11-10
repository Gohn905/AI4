/* ==========================================================================
   Sun Shine - Operations console (ops.js)
   Self-contained add-on. Loaded with ONE line in index.html:
       <script src="ops.js" defer></script>
   Nothing here changes the existing page, API or optimiser. It adds an
   "Operations" button to the header that opens a full-page console:
   live interval-by-interval replay, scenario comparison, monthly bill view,
   digital-twin check, data health, roles + approvals + audit trail.
   Everything runs in the browser. Uploaded files are never sent anywhere.
   This is a PLANNING SIMULATION. It never controls equipment.
   ========================================================================== */
(function (root) {
'use strict';
const SunOps = {};
const p2 = n => String(n).padStart(2, '0');
const sum = a => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i]; return s; };
const clamp = (x, a, b) => x < a ? a : x > b ? b : x;
const median = a => { const b = Array.from(a).filter(Number.isFinite).sort((x, y) => x - y); if (!b.length) return NaN; const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };

/* ---------------- tariff, site and battery defaults ---------------- */
// TNB MV Time-of-Use, RP4 schedule used by the main app (MVT in index.html).
const DEFAULTS = {
  tariff: { ep: 31.32, eo: 27.23, md: 97.06, retail: 200, afa: 0 },   // sen/kWh, sen/kWh, RM/kW-month, RM/month, sen/kWh
  bat: { kwh: 1000, kw: 500, rte: 0.90, start: 0.5 },                  // ASSUMPTION for the demo, editable in Settings
  ev: { maxKw: 400, from: 7, to: 19 },                                 // 8 x 50 kW; hours in which EV energy may be moved
  ef: 0.74,                                                            // kg CO2 per kWh, Energy Commission provisional Peninsular figure [check latest]
  win: { from: 14, to: 22 }                                            // peak period, weekdays
};
SunOps.DEFAULTS = DEFAULTS;

/* ---------------- time parsing ---------------- */
function parseTs(v, order) {
  if (v instanceof Date) return Date.UTC(v.getFullYear(), v.getMonth(), v.getDate(), v.getHours(), v.getMinutes());
  if (typeof v === 'number') return NaN;
  const s = String(v == null ? '' : v).trim();
  let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})/);
  if (m) return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  m = s.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})(?:[ T](\d{1,2}):(\d{2}))?/);   // day/month/year by default; month/day/year when the file shows it
  if (m) { const a = +m[1], b = +m[2], dd = order === 'MDY' ? b : a, mm = order === 'MDY' ? a : b; if (mm < 1 || mm > 12 || dd < 1 || dd > 31) return NaN; return Date.UTC(+m[3], mm - 1, dd, +(m[4] || 0), +(m[5] || 0)); }
  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) return Date.UTC(+m[1], +m[2] - 1, +m[3], 0, 0);
  return NaN;
}
const serialToMs = n => Math.round((n - 25569) * 86400000 / 60000) * 60000;   // Excel serial -> wall clock ms
const dKey = ms => { const d = new Date(ms); return d.getUTCFullYear() + '-' + p2(d.getUTCMonth() + 1) + '-' + p2(d.getUTCDate()); };
SunOps.parseTs = parseTs;

/* ---------------- CSV / table reading ---------------- */
function splitCsvLine(line, sep) {
  const out = []; let cur = '', q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) { if (c === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
    else if (c === '"') q = true; else if (c === sep) { out.push(cur); cur = ''; } else cur += c;
  }
  out.push(cur); return out;
}
function csvToRows(text) {
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).filter(l => l.trim().length);
  if (!lines.length) return [];
  const sep = (lines[0].split(';').length > lines[0].split(',').length) ? ';' : (lines[0].split('\t').length > lines[0].split(',').length ? '\t' : ',');
  return lines.map(l => splitCsvLine(l, sep));
}
SunOps.csvToRows = csvToRows;

/* Find the header row and guess which column is which. rows = array of arrays. */
function detectColumns(rows) {
  let h = -1;
  for (let i = 0; i < Math.min(rows.length, 30); i++) {
    const r = rows[i] || [];
    if (r.some(c => /time|date/i.test(String(c == null ? '' : c))) && r.filter(c => c !== '' && c != null).length >= 2) { h = i; break; }
  }
  if (h < 0) return null;
  const head = rows[h].map(c => String(c == null ? '' : c).trim());
  const find = (...res) => { for (const re of res) { const i = head.findIndex(x => re.test(x)); if (i >= 0) return i; } return -1; };
  const tsI = find(/^timestamp$/i, /time.?stamp/i, /date.?time/i, /^time$/i, /date/i);
  let loadI = find(/(grid draw|load|demand|site|consumption).*\(?kw\)?\s*$/i, /(grid draw|load|demand|consumption).*power/i, /^kw$/i, /load|demand|consumption/i);
  if (loadI >= 0 && /kwh|energy/i.test(head[loadI]) && !/kw\b|power/i.test(head[loadI])) {
    const alt = head.findIndex((x, i) => i !== loadI && /(grid draw|load|demand|consumption)/i.test(x) && /(\bkw\b|power)/i.test(x) && !/kwh|energy/i.test(x));
    if (alt >= 0) loadI = alt;
  }
  let pvI = find(/(solar|pv|generation).*power/i, /(solar|pv|generation).*\(?kw\)?\s*$/i, /solar|pv|generation/i);
  if (pvI >= 0 && /kwh|energy/i.test(head[pvI]) && !/\bkw\b|power/i.test(head[pvI])) {
    const alt = head.findIndex((x, i) => /(solar|pv|generation)/i.test(x) && /(\bkw\b|power)/i.test(x) && !/kwh|energy/i.test(x)); if (alt >= 0) pvI = alt;
  }
  let evI = find(/ev.*\(kw\)\s*$/i, /ev.*power/i, /(ev|charger|non.?essential).*kw$/i, /(ev|charger)/i, /non.?essential/i);
  if (evI >= 0 && /kwh|energy/i.test(head[evI]) && !/\bkw\b|power|\(kw\)/i.test(head[evI])) {
    const alt = head.findIndex((x, i) => /(ev|charger|non.?essential)/i.test(x) && /(\(kw\)|power)/i.test(x) && !/kwh|energy/i.test(x)); if (alt >= 0) evI = alt;
  }
  let kwhI = find(/(grid draw|load|demand).*(energy|kwh)/i);
  const irrI = find(/irradiance/i);
  let skip = 1;                                           // the organisers' sheet has a units row under the header
  const nx = rows[h + 1] || [];
  if (!(nx.length && !Number.isFinite(parseTs(nx[tsI])) && typeof nx[tsI] !== 'number' && !(nx[tsI] instanceof Date))) skip = 0; else skip = 1;
  return { header: h, head, tsI, loadI, pvI, evI, kwhI, irrI, dataFrom: h + 1 + skip };
}
SunOps.detectColumns = detectColumns;

/* Build the table the engine needs from raw rows and a column map. */
function rowsToSeries(rows, map) {
  const out = [];
  // Slash dates are ambiguous (3/4/2025). Read the whole column: a first part above 12 means day/month, a second part above 12 means month/day.
  let maxA = 0, maxB = 0, nText = 0, nNum = 0;
  for (let i = map.dataFrom; i < rows.length; i++) { const v = rows[i] && rows[i][map.tsI]; if (typeof v === 'number') nNum++; else if (typeof v === 'string') { const m = v.trim().match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})/); if (m) { nText++; maxA = Math.max(maxA, +m[1]); maxB = Math.max(maxB, +m[2]); } } }
  const order = maxB > 12 ? 'MDY' : 'DMY';
  for (let i = map.dataFrom; i < rows.length; i++) {
    const r = rows[i]; if (!r) continue;
    let t = r[map.tsI];
    t = (typeof t === 'number') ? serialToMs(t) : parseTs(t, order);
    if (!Number.isFinite(t)) continue;
    const num = j => { if (j < 0) return NaN; const v = r[j]; if (v === '' || v == null) return NaN; const x = typeof v === 'number' ? v : parseFloat(String(v).replace(/,/g, '')); return Number.isFinite(x) ? x : NaN; };
    out.push({ t, load: num(map.loadI), pv: map.pvI >= 0 ? num(map.pvI) : 0, ev: map.evI >= 0 ? num(map.evI) : 0, kwh: num(map.kwhI), irr: num(map.irrI == null ? -1 : map.irrI) });
  }
  out.sort((a, b) => a.t - b.t);
  out.tsNote = (nText && nNum) ? { text: nText, dates: nNum, order } : (nText ? { text: nText, dates: 0, order } : null);
  return out;
}
SunOps.rowsToSeries = rowsToSeries;

/* ---------------- dataset build + health checks + repair ---------------- */
function buildDataset(series, opts) {
  opts = opts || {};
  if (series.length < 96) throw new Error('Need at least two days of readings.');
  const diffs = []; for (let i = 1; i < Math.min(series.length, 2000); i++) diffs.push((series[i].t - series[i - 1].t) / 60000);
  const stepMin = median(diffs.filter(x => x > 0));
  if (![15, 30, 60].includes(stepMin)) throw new Error('Readings must be every 15, 30 or 60 minutes (found ' + stepMin + ' min).');
  const spd = 1440 / stepMin, dt = stepMin / 60;
  const first = new Date(series[0].t);
  const firstMin = first.getUTCHours() * 60 + first.getUTCMinutes();
  let endConv = opts.endConvention;
  if (endConv == null) endConv = (firstMin === stepMin);     // first reading at 00:30 means each stamp marks the END of its interval
  const shift = endConv ? stepMin * 60000 : 0;
  const slotMs = s => s.t - shift;
  const t0 = Date.UTC(new Date(slotMs(series[0])).getUTCFullYear(), new Date(slotMs(series[0])).getUTCMonth(), new Date(slotMs(series[0])).getUTCDate());
  const tEnd = slotMs(series[series.length - 1]);
  const nD = Math.floor((tEnd - t0) / 86400000) + 1, n = nD * spd;
  const load = new Float64Array(n).fill(NaN), pv = new Float64Array(n).fill(NaN), ev = new Float64Array(n).fill(NaN), kwh = new Float64Array(n).fill(NaN), irr = new Float64Array(n).fill(NaN);
  let dups = 0, off = 0;
  for (const r of series) {
    const m = slotMs(r), i = Math.round((m - t0) / (stepMin * 60000));
    if (i < 0 || i >= n) continue;
    if ((m - t0) % (stepMin * 60000) !== 0) off++;
    if (Number.isFinite(load[i])) dups++;
    load[i] = r.load; pv[i] = r.pv; ev[i] = r.ev; kwh[i] = r.kwh; irr[i] = r.irr;
  }
  const days = []; for (let d = 0; d < nD; d++) { const ms = t0 + d * 86400000; days.push({ date: dKey(ms), dow: new Date(ms).getUTCDay(), month: new Date(ms).getUTCMonth(), ms }); }
  const health = [];
  const missing = []; for (let i = 0; i < n; i++) if (!Number.isFinite(load[i])) missing.push(i);
  // 0 timestamp formats
  if (series.tsNote && series.tsNote.text) health.push({ id: 'tsfmt', name: 'Timestamp formats', status: 'fixed', result: series.tsNote.dates ? series.tsNote.text.toLocaleString() + ' timestamps are text and ' + series.tsNote.dates.toLocaleString() + ' are real date cells (mixed in one column)' : series.tsNote.text.toLocaleString() + ' timestamps are text', handling: 'Text dates read as ' + (series.tsNote.order === 'MDY' ? 'month/day/year, because some have a day above 12 in the second position' : 'day/month/year') + '. Both kinds were converted to one clock.' });
  // 1 structure
  health.push({ id: 'rows', name: 'Readings found', status: 'ok', result: series.length.toLocaleString() + ' readings, ' + stepMin + '-minute steps, ' + nD + ' days (' + days[0].date + ' to ' + days[nD - 1].date + ')', handling: 'Each reading is placed on its own ' + stepMin + '-minute slot.' });
  health.push({ id: 'conv', name: 'Timestamp meaning', status: 'ok', result: endConv ? 'First reading is ' + p2(first.getUTCHours()) + ':' + p2(first.getUTCMinutes()) + ', so each stamp marks the END of its interval.' : 'Each stamp marks the START of its interval.', handling: endConv ? 'Shifted back one step so 14:00 to 22:00 peak hours line up with the tariff.' : 'Used as given.' });
  health.push({ id: 'gaps', name: 'Missing or duplicate intervals', status: (missing.length || dups) ? 'warn' : 'ok', result: missing.length + ' missing, ' + dups + ' duplicated', handling: missing.length ? 'Missing intervals are filled with the typical value for that time and day type.' : 'Nothing to do.', slots: missing.slice(0, 5000) });
  // 2 zero / dropout runs in load
  const zero = new Uint8Array(n); const med = median(load.filter ? Array.from(load) : load);
  const minRun = Math.max(2, Math.round(1 / dt));          // 1 hour of exact zeros is not a real factory load
  for (let i = 0; i < n;) {
    if (load[i] === 0) { let j = i; while (j < n && load[j] === 0) j++; if (j - i >= minRun) for (let k = i; k < j; k++) zero[k] = 1; i = j; } else i++;
  }
  const zeroN = sum(zero);
  const zeroDays = [...new Set(Array.from(zero).map((z, i) => z ? days[Math.floor(i / spd)].date : null).filter(Boolean))];
  health.push({ id: 'zero', name: 'Demand reads exactly zero (meter dropout)', status: zeroN ? 'fixed' : 'ok', result: zeroN ? zeroN + ' intervals on ' + zeroDays.join(', ') + ' (site median is ' + Math.round(med) + ' kW)' : 'None', handling: zeroN ? 'Treated as missing data, not as real zero demand, and filled with the typical profile. These intervals are marked in the charts.' : 'Nothing to do.', days: zeroDays });
  // 3 negatives / spikes / flatlines
  let neg = 0; for (let i = 0; i < n; i++) if (load[i] < 0 || pv[i] < 0 || ev[i] < 0) neg++;
  health.push({ id: 'neg', name: 'Negative readings', status: neg ? 'warn' : 'ok', result: neg + ' found', handling: neg ? 'Set to zero.' : 'Nothing to do.' });
  let spikes = 0; const hi = med * 8; for (let i = 0; i < n; i++) if (load[i] > hi && hi > 0) spikes++;
  health.push({ id: 'spike', name: 'Extreme spikes in demand', status: spikes ? 'warn' : 'ok', result: spikes ? spikes + ' readings above 8x the median' : 'None above 8x the median', handling: spikes ? 'Kept. They may be real peaks. Check them against the meter.' : 'Nothing to do.' });
  // 4 kWh vs kW consistency
  let mism = 0, cmp = 0; for (let i = 0; i < n; i++) if (Number.isFinite(kwh[i]) && Number.isFinite(load[i]) && load[i] > 0 && !zero[i]) { cmp++; const e = load[i] * dt; if (Math.abs(kwh[i] - e) > 0.05 * e && Math.abs(kwh[i] - e) > 2) mism++; }
  if (cmp) health.push({ id: 'kwh', name: 'Energy (kWh) agrees with power (kW)', status: mism > cmp * 0.005 ? 'warn' : 'ok', result: mism + ' of ' + cmp.toLocaleString() + ' intervals differ by more than 5%', handling: mism ? 'The kW column is used, because the maximum demand charge is based on kW.' : 'Nothing to do.' });
  // 5 irradiance blanks / night PV
  let irrBlank = 0; for (let i = 0; i < n; i++) if (!Number.isFinite(irr[i])) irrBlank++;
  let nightPv = 0; for (let i = 0; i < n; i++) { const h = Math.floor((i % spd) * dt); if ((h < 5 || h >= 20) && pv[i] > 1) nightPv++; }
  health.push({ id: 'pv', name: 'Solar output at night', status: nightPv ? 'warn' : 'ok', result: nightPv ? nightPv + ' intervals with output between 20:00 and 05:00' : 'Zero at night, as expected', handling: nightPv ? 'Kept as given.' : 'Nothing to do.' });
  if (irrBlank > n * 0.2) health.push({ id: 'irr', name: 'Irradiance column', status: 'ok', result: Math.round(irrBlank / n * 100) + '% blank (mostly night)', handling: 'Blank means no sunlight. The planner uses solar power, not irradiance.' });
  // repair
  for (let i = 0; i < n; i++) { if (load[i] < 0) load[i] = 0; if (!Number.isFinite(pv[i]) || pv[i] < 0) pv[i] = 0; if (!Number.isFinite(ev[i]) || ev[i] < 0) ev[i] = 0; }
  const bad = new Uint8Array(n); for (let i = 0; i < n; i++) if (!Number.isFinite(load[i]) || zero[i]) bad[i] = 1;
  const typeOf = dow => dow === 0 ? 2 : dow === 6 ? 1 : 0;
  let repaired = 0;
  for (let i = 0; i < n; i++) if (bad[i]) {
    const d = Math.floor(i / spd), s = i % spd, vals = [];
    for (let k = -28; k <= 28; k += 1) { const dd = d + k; if (dd < 0 || dd >= nD || dd === d) continue; if (typeOf(days[dd].dow) !== typeOf(days[d].dow)) continue; const j = dd * spd + s; if (!bad[j] && Number.isFinite(load[j])) vals.push(load[j]); }
    load[i] = vals.length ? median(vals) : (Number.isFinite(med) ? med : 0); repaired++;
  }
  const flags = bad;
  // does the demand column look like grid draw AFTER solar was taken off? (dips when the sun is high)
  let netHint = { suggested: false, corr: 0, sunnyMedian: NaN, nightMedian: NaN };
  {
    const xs = [], ys = [], night = [], sunny = []; let pmax = 0; for (let i = 0; i < n; i++) if (pv[i] > pmax) pmax = pv[i];
    for (let i = 0; i < n; i++) { if (bad[i]) continue; const h = (i % spd) * dt; if (pv[i] > 50) { xs.push(pv[i]); ys.push(load[i]); } if (h < 5) night.push(load[i]); if (pv[i] > 0.6 * pmax && pmax > 0) sunny.push(load[i]); }
    if (xs.length > 200 && pmax > 0) {
      const mx = sum(xs) / xs.length, my = sum(ys) / ys.length; let sxy = 0, sxx = 0, syy = 0;
      for (let i = 0; i < xs.length; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; syy += (ys[i] - my) ** 2; }
      netHint.corr = sxy / Math.sqrt(sxx * syy || 1); netHint.sunnyMedian = median(sunny); netHint.nightMedian = median(night);
      netHint.lowSunny = sunny.filter(v => v < 0.2 * netHint.nightMedian).length / (sunny.length || 1);
      netHint.suggested = netHint.corr < -0.2 && netHint.lowSunny > 0.08;
    }
  }
  return { netHint, stepMin, spd, dt, n, nD, days, load, pv, ev, flags, health, repaired, endConv, source: opts.source || 'upload', name: opts.name || 'Uploaded site' };
}
SunOps.buildDataset = buildDataset;
/* mode 'net': the demand column is grid draw after solar, so site demand = grid draw + solar. mode 'gross': use as given. */
function withDemandMode(D, mode) {
  if (mode !== 'net') return Object.assign({}, D, { demandMode: 'gross' });
  const load = new Float64Array(D.n); for (let i = 0; i < D.n; i++) load[i] = D.load[i] + (D.flags[i] ? 0 : D.pv[i]);
  // repaired slots already hold a typical gross-less value; add typical solar too so the shape stays consistent
  for (let i = 0; i < D.n; i++) if (D.flags[i]) load[i] = D.load[i] + D.pv[i];
  return Object.assign({}, D, { load, demandMode: 'net' });
}
SunOps.withDemandMode = withDemandMode;

/* ==========================================================================
   Planner + digital twin
   Plain-language summary of the rules (also shown in the console):
   - Solar surplus charges the battery first. Surplus beyond that is lost (no export income is assumed).
   - Maximum demand is billed on the highest interval in the weekday 14:00-22:00 peak window.
   - Optimised strategies hold a "cap" on grid import inside that window and use the battery only to stay under it.
   - They keep enough charge for the next peak window and top up from the grid just in time (cost-saving and balanced only).
   - EV energy is moved into hours with solar surplus, outside the peak window where possible.
   - Re-planning mode: every interval the forecast is corrected with what was actually measured, and the cap is recomputed.
   ========================================================================== */
const typeOf = dow => dow === 0 ? 2 : dow === 6 ? 1 : 0;      // 0 weekday, 1 Saturday, 2 Sunday
const isWeekday = dow => dow >= 1 && dow <= 5;

function makeCtx(D, cfg) {
  cfg = cfg || {};
  const bat = Object.assign({}, DEFAULTS.bat, cfg.bat || {});
  const tariff = Object.assign({}, DEFAULTS.tariff, cfg.tariff || {});
  const ev = Object.assign({}, DEFAULTS.ev, cfg.ev || {});
  const spd = D.spd, dt = D.dt;
  const eta = Math.sqrt(bat.rte);
  return {
    D, spd, dt, bat, tariff, ev, ef: cfg.ef != null ? cfg.ef : DEFAULTS.ef,
    eta, w0: Math.round(DEFAULTS.win.from / dt), w1: Math.round(DEFAULTS.win.to / dt),
    evA: Math.round(ev.from / dt), evB: Math.round(ev.to / dt),
    margin: cfg.margin != null ? cfg.margin : 0.25,
    fcCache: new Map(), nextCache: new Map()
  };
}

/* ---- day-ahead forecast: uses only days BEFORE day d ---- */
function forecastDay(ctx, d) {
  if (ctx.fcCache.has(d)) return ctx.fcCache.get(d);
  const D = ctx.D, spd = ctx.spd, L = new Float64Array(spd), P = new Float64Array(spd);
  const same = [];
  for (let k = d - 1; k >= 0 && same.length < 8; k--) if (typeOf(D.days[k].dow) === typeOf(D.days[d].dow)) same.push(k);
  if (!same.length) for (let k = d - 1; k >= 0 && same.length < 8; k--) same.push(k);
  for (let s = 0; s < spd; s++) { const v = same.map(k => D.load[k * spd + s]).sort((a, b) => a - b), m = v.length >> 1; L[s] = v.length ? (v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2) : 0; }
  if (d >= 1) {
    const lastN = Math.min(7, d);
    for (let s = 0; s < spd; s++) {
      let m = 0; for (let k = d - lastN; k < d; k++) m += D.pv[k * spd + s];
      P[s] = 0.5 * D.pv[(d - 1) * spd + s] + 0.5 * m / lastN;
    }
  }
  const f = { L, P }; ctx.fcCache.set(d, f); return f;
}

/* ---- battery helpers ---- */
const capKwh = ctx => ctx.bat.kwh;
function availDis(ctx, soc, socMin) { return Math.max(0, Math.min(ctx.bat.kw, (soc - socMin) * ctx.bat.kwh * ctx.eta / ctx.dt)); }
function availChg(ctx, soc) { return Math.max(0, Math.min(ctx.bat.kw, (1 - soc) * ctx.bat.kwh / (ctx.eta * ctx.dt))); }

/* Simulate the peak window from slot `from`, battery state soc, holding grid import at or under `cap`.
   Returns whether the cap was held and the resulting import peak. net = load + EV - solar (kW). */
function simHold(ctx, net, from, to, soc, cap, socMin) {
  let peak = 0, ok = true;
  for (let s = from; s < to; s++) {
    const n = net[s];
    if (n < 0) { const c = Math.min(-n, availChg(ctx, soc)); soc += c * ctx.dt * ctx.eta / ctx.bat.kwh; if (n > -1e-9) peak = Math.max(peak, 0); }
    else { const d = Math.min(Math.max(n - cap, 0), availDis(ctx, soc, socMin)); soc -= d * ctx.dt / ctx.eta / ctx.bat.kwh; const g = n - d; if (g > cap + 0.5) ok = false; if (g > peak) peak = g; }
  }
  return { ok, peak, soc };
}
function minCap(ctx, net, from, to, soc, socMin) {         // lowest cap that can be held from this state
  let hi = 0; for (let s = from; s < to; s++) if (net[s] > hi) hi = net[s];
  if (hi <= 0) return 0;
  let lo = 0;
  if (simHold(ctx, net, from, to, soc, lo, socMin).ok) return 0;
  for (let it = 0; it < 22; it++) { const mid = (lo + hi) / 2; if (simHold(ctx, net, from, to, soc, mid, socMin).ok) hi = mid; else lo = mid; if (hi - lo < 0.5) break; }
  return hi;
}
function minStartSoc(ctx, net, from, to, cap, socMin) {    // least charge needed at the start of the window to hold `cap`
  if (simHold(ctx, net, from, to, socMin, cap, socMin).ok) return socMin;
  if (!simHold(ctx, net, from, to, 1, cap, socMin).ok) return 1;
  let lo = socMin, hi = 1;
  for (let it = 0; it < 18; it++) { const mid = (lo + hi) / 2; if (simHold(ctx, net, from, to, mid, cap, socMin).ok) hi = mid; else lo = mid; if (hi - lo < 0.002) break; }
  return hi;
}
function projectSoc(ctx, net, from, to, soc) {              // charge from surplus only, no discharge
  for (let s = from; s < to; s++) if (net[s] < 0) { const c = Math.min(-net[s], availChg(ctx, soc)); soc += c * ctx.dt * ctx.eta / ctx.bat.kwh; }
  return soc;
}

/* ---- strategies ---- */
const STRATS = {
  base: { key: 'base', name: 'Baseline (solar only)', short: 'Baseline', color: '#6b7280', bat: false },
  self: { key: 'self', name: 'Simple rule: self-consumption', short: 'Self-consumption rule', color: '#a855f7', bat: true, rule: 'self', reserve: 0, evShift: 0 },
  tou: { key: 'tou', name: 'Simple rule: discharge in peak hours', short: 'Peak-hours rule', color: '#0ea5e9', bat: true, rule: 'tou', reserve: 0, evShift: 0 },
  cost: { key: 'cost', name: 'Optimised: Cost-saving', short: 'Cost-saving', color: '#16803D', bat: true, rule: 'opt', reserve: 0, evShift: 1, topup: true },
  bal: { key: 'bal', name: 'Optimised: Balanced (20% reserve)', short: 'Balanced', color: '#F59E0B', bat: true, rule: 'opt', reserve: 0.2, evShift: 0.5, topup: true },
  zero: { key: 'zero', name: 'Optimised: Lowest-carbon (30% reserve, no grid charging)', short: 'Lowest-carbon', color: '#2563eb', bat: true, rule: 'opt', reserve: 0.3, evShift: 1, topup: false }
};
const STRAT_ORDER = ['base', 'self', 'tou', 'cost', 'bal', 'zero'];
SunOps.STRATS = STRATS; SunOps.STRAT_ORDER = STRAT_ORDER;

/* Place the EV energy still to deliver into allowed slots: lowest net load first, outside the peak window first. */
function allocEV(ctx, E, from, netNoEv, inWin, out) {
  let rem = E / ctx.dt;                                      // kW-slots still to place
  const cand = [];
  for (let s = Math.max(from, ctx.evA); s < ctx.evB; s++) cand.push(s);
  const pref = cand.filter(s => !inWin(s)).sort((a, b) => netNoEv[a] - netNoEv[b]);
  const late = cand.filter(s => inWin(s)).sort((a, b) => netNoEv[a] - netNoEv[b]);
  for (const s of pref.concat(late)) { if (rem <= 1e-9) break; const k = Math.min(ctx.ev.maxKw, rem); out[s] += k; rem -= k; }
  return rem * ctx.dt;                                       // energy that did not fit
}

/* ---- one day, step by step (used by the batch run AND by the live replay) ---- */
function newDay(ctx, d, soc0, Mrun, strat, mode, inputs) {
  const D = ctx.D, spd = ctx.spd, o = d * spd;
  const day = D.days[d];
  const L = inputs ? inputs.L : D.load.subarray(o, o + spd), P = inputs ? inputs.P : D.pv.subarray(o, o + spd), E0 = inputs ? inputs.E : D.ev.subarray(o, o + spd);
  const wd = isWeekday(day.dow);
  const X = {
    ctx, d, day, strat, mode, wd, L, P, E0, s: 0, soc: soc0, Mrun, cap: Infinity, capApplied: Infinity, Sreq: 0, Skeep: 0,
    Etot: sum(E0) * ctx.dt, evDone: 0, evPlan: new Float64Array(spd), fL: null, fP: null, f0: forecastDay(ctx, d),
    socMin: strat.bat ? strat.reserve : 0,
    out: { grid: new Float32Array(spd), soc: new Float32Array(spd), batt: new Float32Array(spd), capv: new Float32Array(spd), ev: new Float32Array(spd), curt: new Float32Array(spd), top: new Float32Array(spd), net: new Float32Array(spd) },
    pendingProp: null, log: [], throughput: 0
  };
  X.inWin = s => wd && s >= ctx.w0 && s < ctx.w1;
  X.fL = Float64Array.from(X.f0.L); X.fP = Float64Array.from(X.f0.P);
  if (strat.rule === 'opt') plan(X, 0, true);
  else if (strat.bat) X.evPlan.set(E0);
  else X.evPlan.set(E0);
  return X;
}

function correctForecast(X, t) {                              // fade a measured/forecast ratio back to forecast over ~6 h
  const ctx = X.ctx, spd = ctx.spd, k = Math.max(1, Math.round(2 / ctx.dt));  // look at the last 2 hours
  let aP = 0, fP = 0, aL = 0, fL = 0;
  for (let s = Math.max(0, t - k); s < t; s++) { aP += X.P[s]; fP += X.f0.P[s]; aL += X.L[s]; fL += X.f0.L[s]; }
  const rP = fP > 20 * k ? clamp(aP / fP, 0.3, 2) : 1, rL = fL > 20 * k ? clamp(aL / fL, 0.6, 1.6) : 1;
  for (let s = t; s < spd; s++) {
    const w = Math.exp(-(s - t + 1) * ctx.dt / 6);
    X.fP[s] = X.f0.P[s] * (1 + (rP - 1) * w); X.fL[s] = X.f0.L[s] * (1 + (rL - 1) * w);
  }
  X.ratioP = rP; X.ratioL = rL;
}

/* Next weekday's requirement for starting charge (used so the battery is not emptied overnight). Cached per day. */
function nextReq(ctx, X, t) {
  const D = ctx.D; let d2 = X.d + (X.wd && t < ctx.w0 ? 0 : 1);
  while (d2 < D.nD && !isWeekday(D.days[d2].dow)) d2++;
  if (d2 >= D.nD) return X.socMin;
  if (d2 === X.d) return X.Sreq;
  const key = X.strat.key + '|' + d2 + '|' + (X.Mrun >> 4);
  if (ctx.nextCache.has(key)) return ctx.nextCache.get(key);
  const f = forecastDay(ctx, d2), net = new Float64Array(ctx.spd);
  for (let s = 0; s < ctx.spd; s++) net[s] = f.L[s] - f.P[s];
  const sameMonth = D.days[d2].month === X.day.month;
  const cap = Math.max(minCap(ctx, net, ctx.w0, ctx.w1, 1, X.socMin), sameMonth ? X.Mrun : 0);
  const v = minStartSoc(ctx, net, ctx.w0, ctx.w1, cap, X.socMin);
  ctx.nextCache.set(key, v); return v;
}

function plan(X, t, first) {
  const ctx = X.ctx, spd = ctx.spd, S = X.strat;
  if (!first && X.mode === 'live') correctForecast(X, t);
  // 1. EV timing for what is still to deliver
  const Er = Math.max(0, X.Etot - X.evDone), f = S.evShift;
  X.evPlan.fill(0, t);
  if (Er > 0) {
    const netNoEv = new Float64Array(spd); for (let s = 0; s < spd; s++) netNoEv[s] = X.fL[s] - X.fP[s];
    let shape = 0; for (let s = t; s < spd; s++) shape += X.E0[s] * ctx.dt;
    const keep = (1 - f) * Er;
    if (keep > 0 && shape > 0) for (let s = t; s < spd; s++) X.evPlan[s] += keep * X.E0[s] * ctx.dt / shape;
    const move = f * Er + (keep > 0 && shape <= 0 ? keep : 0);
    if (move > 0) allocEV(ctx, move, t, netNoEv, X.inWin, X.evPlan);
  }
  // 2. cap and charge needed for the peak window
  const net = new Float64Array(spd), mg = ctx.margin || 0; for (let s = 0; s < spd; s++) net[s] = X.fL[s] * (1 + mg) + X.evPlan[s] - X.fP[s] * (1 - mg);
  X.netF = net;
  if (!X.wd || t >= ctx.w1) { X.cap = Infinity; X.Sreq = X.socMin; }
  else if (t >= ctx.w0) { X.cap = Math.max(minCap(ctx, net, t, ctx.w1, X.soc, X.socMin), X.Mrun); X.Sreq = X.socMin; }
  else if (S.topup) {
    const cf = minCap(ctx, net, ctx.w0, ctx.w1, 1, X.socMin);
    X.cap = Math.max(cf, X.Mrun); X.Sreq = Math.min(1, minStartSoc(ctx, net, ctx.w0, ctx.w1, X.cap, X.socMin) + 0.03);
  } else {
    const ps = projectSoc(ctx, net, t, ctx.w0, X.soc);
    X.cap = Math.max(minCap(ctx, net, ctx.w0, ctx.w1, ps, X.socMin), X.Mrun);
    X.Sreq = Math.min(1, minStartSoc(ctx, net, ctx.w0, ctx.w1, X.cap, X.socMin) + 0.03);
  }
  if (X.wd && t < ctx.w0) X.Skeep = Math.max(X.socMin, X.Sreq);
  else if (X.wd && t < ctx.w1) X.Skeep = X.socMin;
  else X.Skeep = Math.max(X.socMin, nextReq(ctx, X, t));
  return X;
}

/* Re-plan at the start of slot s (live mode). Returns a proposal if the cap moves materially. */
function prepareSlot(X, s) {
  const S = X.strat;
  X.pendingProp = null;
  if (S.rule === 'opt' && s > 0 && X.mode === 'live') {
    const before = X.capApplied;
    plan(X, s, false);
    if (Number.isFinite(X.cap) && Number.isFinite(before) && Math.abs(X.cap - before) >= Math.max(50, 0.05 * before)) {
      X.pendingProp = { slot: s, from: before, to: X.cap, soc: X.soc, ratioP: X.ratioP, ratioL: X.ratioL };
    } else if (Number.isFinite(X.cap) && !X.noPlan) X.capApplied = X.cap;
  } else if (S.rule === 'opt' && s === 0) { X.capApplied = X.cap; }
  return X.pendingProp;
}
function resolveProposal(X, accept) { if (X.pendingProp) { if (accept) X.capApplied = X.pendingProp.to; X.pendingProp = null; } }

/* Execute slot s with the data as measured. */
function execSlot(X, s) {
  const ctx = X.ctx, S = X.strat, dt = ctx.dt, B = ctx.bat, eta = ctx.eta, win = X.inWin(s);
  // EV actually delivered in this slot
  let ev;
  if (S.rule === 'opt') {
    const Er = Math.max(0, X.Etot - X.evDone);
    ev = Math.min(X.evPlan[s], Er / dt);
    if (s === ctx.evB - 1 || (s >= ctx.evB)) ev = Math.min(Er / dt, ctx.ev.maxKw);   // anything left must go now
  } else ev = X.E0[s];
  X.evDone += ev * dt;
  const n = X.L[s] + ev - X.P[s];
  let grid = 0, batt = 0, curt = 0, top = 0;
  if (!S.bat) { grid = Math.max(n, 0); curt = Math.max(-n, 0); }
  else if (n < 0) {
    const c = Math.min(-n, availChg(ctx, X.soc)); batt = -c; X.soc += c * dt * eta / B.kwh; curt = -n - c; grid = 0;
  } else {
    let d = 0;
    if (S.rule === 'self') d = Math.min(n, availDis(ctx, X.soc, 0));
    else if (S.rule === 'tou') {
      if (win) d = Math.min(n, availDis(ctx, X.soc, 0));
      else if (X.wd && s * dt < 7) { const g = availChg(ctx, X.soc); if (g > 0.01) { X.soc += g * dt * eta / B.kwh; top = g; batt = -g; grid += g; } }   // simple rule: charge overnight, discharge in peak hours
    }
    else if (win) d = Math.min(Math.max(n - X.capApplied, 0), availDis(ctx, X.soc, X.socMin));
    else { const above = Math.max(0, (X.soc - X.Skeep) * B.kwh * eta / dt); d = Math.min(n, B.kw, above); }
    if (d > 0 || S.rule !== 'tou' || !top) { batt = d; X.soc -= d * dt / eta / B.kwh; grid = n - d; } else { grid = n + top; }
    // just-in-time grid top-up before the window (cost-saving and balanced only)
    if (S.topup && X.wd && s < ctx.w0 && d === 0) {
      const proj = projectSoc(ctx, X.netF, s, ctx.w0, X.soc);
      const need = (X.Sreq - proj) * B.kwh / eta;                       // kWh still to buy
      if (need > 1e-6) {
        const left = ctx.w0 - s - 1;                                    // slots after this one
        const mustNow = Math.max(0, need - left * B.kw * dt) / dt;      // kW needed now so the rest still fits
        const g = Math.min(B.kw, availChg(ctx, X.soc), mustNow);
        if (g > 0.01) { X.soc += g * dt * eta / B.kwh; grid += g; top = g; batt = -g; }
      }
    }
  }
  X.throughput += Math.abs(batt) * dt;
  if (win && grid > X.Mrun) X.Mrun = grid;
  const o = X.out; o.grid[s] = grid; o.soc[s] = X.soc; o.batt[s] = batt; o.capv[s] = Number.isFinite(X.capApplied) ? X.capApplied : 0; o.ev[s] = ev; o.curt[s] = curt; o.top[s] = top; o.net[s] = n;
  X.s = s + 1;
  return grid;
}

/* ---- whole-period run ---- */
function runStrategy(ctx, strat, mode) {
  const D = ctx.D, spd = ctx.spd, n = D.n;
  const R = { strat, mode, grid: new Float32Array(n), soc: new Float32Array(n), batt: new Float32Array(n), cap: new Float32Array(n), ev: new Float32Array(n), curt: new Float32Array(n), top: new Float32Array(n), soc0: new Float32Array(D.nD), m0: new Float32Array(D.nD), throughput: 0 };
  let soc = ctx.bat.start, Mrun = 0, curMonth = -1;
  for (let d = 0; d < D.nD; d++) {
    if (D.days[d].month !== curMonth) { curMonth = D.days[d].month; Mrun = 0; }
    R.soc0[d] = soc; R.m0[d] = Mrun;
    const X = newDay(ctx, d, soc, Mrun, strat, mode);
    for (let s = 0; s < spd; s++) { prepareSlot(X, s); resolveProposal(X, true); execSlot(X, s); }
    const o = d * spd; R.grid.set(X.out.grid, o); R.soc.set(X.out.soc, o); R.batt.set(X.out.batt, o); R.cap.set(X.out.capv, o); R.ev.set(X.out.ev, o); R.curt.set(X.out.curt, o); R.top.set(X.out.top, o);
    soc = X.soc; Mrun = X.Mrun; R.throughput += X.throughput;
  }
  return R;
}

/* ==========================================================================
   Bill and KPI calculations (all from the half-hourly series)
   ========================================================================== */
/* TNB bills the highest 30-minute AVERAGE demand. With 15-minute data, average each clock-aligned pair of steps before taking the maximum. */
function bp(g, o, s0, s1, kB) {
  let v = 0, at = -1;
  for (let s = s0; s < s1; s += kB) { let a = 0, n = 0; for (let j = 0; j < kB && s + j < s1; j++) { a += g[o + s + j]; n++; } a /= n || 1; if (a > v) { v = a; at = o + s; } }
  return { v, i: at };
}
const blockOf = ctx => Math.max(1, Math.round(30 / (ctx.D.stepMin || 30)));
function billFor(ctx, kwhPk, kwhOff, md) {
  const T = ctx.tariff;
  const energyRM = (kwhPk * (T.ep + T.afa) + kwhOff * (T.eo + T.afa)) / 100;
  const mdRM = md * T.md, retailRM = T.retail;
  return { energyRM, mdRM, retailRM, totalRM: energyRM + mdRM + retailRM };
}
/* d0..d1 inclusive day indexes. Maximum demand is the highest import in the weekday peak window. */
function periodKpi(ctx, R, d0, d1) {
  const D = ctx.D, spd = ctx.spd, dt = ctx.dt;
  let kPk = 0, kOff = 0, md = 0, mdAt = -1, pv = 0, curt = 0, load = 0, gridTot = 0;
  for (let d = d0; d <= d1; d++) {
    const wd = isWeekday(D.days[d].dow);
    for (let s = 0; s < spd; s++) {
      const i = d * spd + s, g = R.grid[i];
      gridTot += g * dt; pv += D.pv[i] * dt; curt += R.curt[i] * dt; load += (D.load[i] + R.ev[i]) * dt;
      if (wd && s >= ctx.w0 && s < ctx.w1) kPk += g * dt; else kOff += g * dt;
    }
    if (wd) { const q = bp(R.grid, d * spd, ctx.w0, ctx.w1, blockOf(ctx)); if (q.v > md) { md = q.v; mdAt = q.i; } }
  }
  const b = billFor(ctx, kPk, kOff, md);
  return Object.assign({ d0, d1, kwhPeak: kPk, kwhOff: kOff, kwh: gridTot, md, mdAt, pvKwh: pv, curtKwh: curt, solarUsed: pv - curt, loadKwh: load, co2: gridTot * ctx.ef, days: d1 - d0 + 1 }, b);
}
function monthsOf(D) {
  const out = []; let cur = null;
  for (let d = 0; d < D.nD; d++) {
    const key = D.days[d].date.slice(0, 7);
    if (!cur || cur.key !== key) { cur = { key, d0: d, d1: d, month: D.days[d].month, label: key }; out.push(cur); } else cur.d1 = d;
  }
  return out;
}
function dailyMd(ctx, R) {                                      // billed-window peak per day (kW), 0 on non-billing days
  const D = ctx.D, spd = ctx.spd, a = new Float32Array(D.nD);
  for (let d = 0; d < D.nD; d++) if (isWeekday(D.days[d].dow)) a[d] = bp(R.grid, d * spd, ctx.w0, ctx.w1, blockOf(ctx)).v;
  return a;
}
function dayEnergyRM(ctx, R, d) {                              // energy charge for one day (RM), not including maximum demand
  const D = ctx.D, spd = ctx.spd, dt = ctx.dt, T = ctx.tariff, wd = isWeekday(D.days[d].dow); let pk = 0, off = 0;
  for (let s = 0; s < spd; s++) { const g = R.grid[d * spd + s] * dt; if (wd && s >= ctx.w0 && s < ctx.w1) pk += g; else off += g; }
  return (pk * (T.ep + T.afa) + off * (T.eo + T.afa)) / 100;
}

/* ---- digital twin: what the plan expected (using the forecast) versus what happened ---- */
function twinDay(ctx, R, d) {
  const D = ctx.D, spd = ctx.spd, o = d * spd, f = forecastDay(ctx, d);
  const inputs = { L: f.L, P: f.P, E: D.ev.subarray(o, o + spd) };
  const X = newDay(ctx, d, R.soc0[d], R.m0[d], R.strat, 'plan', inputs);
  for (let s = 0; s < spd; s++) { prepareSlot(X, s); resolveProposal(X, true); execSlot(X, s); }
  const pred = X.out.grid, act = R.grid.subarray(o, o + spd);
  let ae = 0, am = 0, pp = 0, ap = 0, pe = 0, ae2 = 0;
  const wd = isWeekday(D.days[d].dow);
  for (let s = 0; s < spd; s++) { ae += Math.abs(pred[s] - act[s]); am += act[s]; pe += pred[s] * ctx.dt; ae2 += act[s] * ctx.dt; if (wd && s >= ctx.w0 && s < ctx.w1) { pp = Math.max(pp, pred[s]); ap = Math.max(ap, act[s]); } }
  return { d, pred, act, mape: am > 0 ? ae / am : 0, predPeak: pp, actPeak: ap, peakErr: pp > 0 ? (ap - pp) / pp : 0, predKwh: pe, actKwh: ae2, kwhErr: pe > 0 ? (ae2 - pe) / pe : 0 };
}

/* ---- run everything the console needs ---- */
function runAll(ctx, onProgress) {
  const res = {};
  const jobs = [['base', 'plan'], ['self', 'plan'], ['tou', 'plan'], ['cost', 'plan'], ['cost', 'live'], ['bal', 'plan'], ['bal', 'live'], ['zero', 'plan'], ['zero', 'live']];
  for (const [k, m] of jobs) { res[k + '_' + m] = runStrategy(ctx, STRATS[k], m); if (onProgress) onProgress(k + '_' + m); }
  return res;
}

SunOps.makeCtx = makeCtx; SunOps.forecastDay = forecastDay; SunOps.runStrategy = runStrategy; SunOps.runAll = runAll;
SunOps.periodKpi = periodKpi; SunOps.monthsOf = monthsOf; SunOps.dailyMd = dailyMd; SunOps.dayEnergyRM = dayEnergyRM; SunOps.twinDay = twinDay;
SunOps.newDay = newDay; SunOps.prepareSlot = prepareSlot; SunOps.resolveProposal = resolveProposal; SunOps.execSlot = execSlot; SunOps.billFor = billFor;
SunOps.isWeekday = isWeekday;

/* ==========================================================================
   Console UI: styles, helpers, charts, roles, audit trail
   ========================================================================== */
const HAS_DOM = typeof document !== 'undefined';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt0 = x => Number.isFinite(x) ? Math.round(x).toLocaleString('en-GB') : '-';
const fmt1 = x => Number.isFinite(x) ? x.toLocaleString('en-GB', { maximumFractionDigits: 1, minimumFractionDigits: 1 }) : '-';
const fmtRM = x => Number.isFinite(x) ? (x < 0 ? '-RM ' : 'RM ') + Math.abs(Math.round(x)).toLocaleString('en-GB') : '-';
const fmtPct = (x, d) => Number.isFinite(x) ? (x * 100).toFixed(d == null ? 1 : d) + '%' : '-';
const sgn = (x, f) => (x > 0 ? '+' : x < 0 ? '-' : '') + f(Math.abs(x));
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DOWS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

const CSS = `
#sunops{position:fixed;inset:0;top:var(--sotop,0px);z-index:65;overflow:auto;--s:clamp(300px,44vw,580px);--cx:calc(100% - var(--s)*.2);--cy:0px;background:radial-gradient(circle var(--s) at var(--cx) var(--cy),rgba(255,176,80,.30) 0,rgba(255,192,112,.15) calc(var(--s)*.44),transparent calc(var(--s)*.44 + 1px),transparent calc(var(--s)*.56 - 1px),rgba(245,158,11,.18) calc(var(--s)*.56),rgba(245,158,11,.18) calc(var(--s)*.56 + 2px),transparent calc(var(--s)*.56 + 3px),transparent calc(var(--s)*.70 - 1px),rgba(245,158,11,.12) calc(var(--s)*.70),rgba(245,158,11,.12) calc(var(--s)*.70 + 2px),transparent calc(var(--s)*.70 + 3px),transparent calc(var(--s)*.84 - 1px),rgba(245,158,11,.07) calc(var(--s)*.84),rgba(245,158,11,.07) calc(var(--s)*.84 + 2px),transparent calc(var(--s)*.84 + 3px),transparent 100%) fixed,linear-gradient(180deg,#FFFDF9 0%,#FFF6EC 55%,#FFECDB 100%) fixed;color:var(--ink,#252B2A);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;padding-top:env(safe-area-inset-top,0)}
#sunops[hidden]{display:none!important}
#sunops *{box-sizing:border-box}
#sunops .wrap{max-width:1280px;margin:0 auto;padding:14px 16px 60px}
#sunops .sotop{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:0 0 10px}
#sunops h1{font-size:19px;font-weight:800;letter-spacing:-.015em;margin:0 6px 0 0;color:var(--acc,#252B2A)}
#sunops h2{color:var(--acc,#252B2A);font-size:19px;letter-spacing:-.015em;margin:0 0 4px}
#sunops h3{font-size:14px;margin:12px 0 4px}
#sunops .sp{flex:1}
#sunops .card{background:var(--card,#F3FAEE);border:1px solid var(--card-line,#D3E5C6);border-radius:10px;padding:14px;margin-bottom:12px}
#sunops .card.soft{background:#fff;border-color:var(--line,#E7E0CA)}
#sunops .note{color:var(--mut,#5B6360);font-size:13px;margin:4px 0}
#sunops .warn{background:#FFF3CD;border:1px solid #F0D98A;border-radius:10px;padding:8px 12px;font-size:13px;margin:8px 0}
#sunops .bad{background:#FDE8E8;border:1px solid #F3B4B4;border-radius:10px;padding:8px 12px;font-size:13px;margin:8px 0}
#sunops .goodbox{background:#E8F6EC;border:1px solid #B7DFC3;border-radius:10px;padding:8px 12px;font-size:13px;margin:8px 0}
#sunops button,#sunops select,#sunops input{font:inherit;color:inherit;background:#fff;border:1px solid var(--line,#E7E0CA);border-radius:6px;padding:6px 10px}
#sunops button{cursor:pointer}
#sunops button:hover:not(:disabled){border-color:var(--amb,#F59E0B)}
#sunops button:disabled{opacity:.45;cursor:not-allowed}
#sunops button.on,#sunops button.pri{background:var(--amb,#F59E0B);border-color:var(--amb,#F59E0B);color:#252B2A;font-weight:600}
#sunops button.good{background:#16803D;border-color:#16803D;color:#fff;font-weight:600}
#sunops button.badb{background:#B42318;border-color:#B42318;color:#fff;font-weight:600}
#sunops button:focus-visible,#sunops input:focus-visible,#sunops select:focus-visible{outline:2px solid #252B2A;outline-offset:2px}
#sunops .seg{display:inline-flex;border:1px solid var(--line,#E7E0CA);border-radius:10px;overflow:hidden;background:#fff}
#sunops .seg button{border:0;border-radius:0;border-right:1px solid var(--line,#E7E0CA)}
#sunops .seg button:last-child{border-right:0}
#sunops .tabs{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
#sunops .tabs button{border-radius:6px;font-weight:600}
#sunops button.on:hover{background:#E08A00;border-color:#E08A00}
#sunops .bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}
#sunops .grid{display:grid;gap:12px}
#sunops .g2{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
#sunops .g3{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
#sunops .live{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:12px}
@media(max-width:980px){#sunops .live{grid-template-columns:1fr}}
#sunops .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:10px}
#sunops .kpi{background:transparent;border:1px solid var(--line,#E7E0CA);border-radius:8px;padding:8px 10px}
#sunops .kpi b{display:block;font-size:20px;line-height:1.2}
#sunops .kpi span{font-size:12px;color:var(--mut,#5B6360)}
#sunops .kpi.hl{background:#E8F6EC;border-color:#B7DFC3}
#sunops table{border-collapse:collapse;width:100%;font-size:13px}
#sunops th,#sunops td{padding:5px 8px;border-bottom:1px solid var(--line,#E7E0CA);text-align:right;white-space:nowrap}
#sunops th:first-child,#sunops td:first-child{text-align:left}
#sunops th{font-size:12px;color:var(--mut,#5B6360);font-weight:500;position:sticky;top:0;background:var(--card,#F3FAEE)}
#sunops .card.soft th{background:#fff}
#sunops .tw{overflow-x:auto;max-height:420px}
#sunops .chart{position:relative}
#sunops .chart svg{width:100%;height:auto;display:block}
#sunops .tip{position:absolute;pointer-events:none;background:rgba(37,43,42,.95);color:#fff;border-radius:8px;padding:6px 9px;font-size:12px;line-height:1.35;display:none;z-index:3;min-width:130px}
#sunops .legend{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 2px}
#sunops .legend label{display:inline-flex;gap:5px;align-items:center;font-size:12px;border:1px solid var(--line,#E7E0CA);border-radius:999px;padding:2px 9px;cursor:pointer;background:#fff;user-select:none}
#sunops .legend label.off{opacity:.45}
#sunops .legend i{width:12px;height:4px;border-radius:2px;display:inline-block}
#sunops .pill{display:inline-block;font-size:11px;font-weight:700;padding:1px 8px;border-radius:999px}
#sunops .pill.ok{background:#DDF3E4;color:#116032}#sunops .pill.warn{background:#FFF0C2;color:#8A5A00}#sunops .pill.fixed{background:#DCE9FB;color:#1E4FA3}#sunops .pill.bad{background:#FADADA;color:#9B1C1C}
#sunops .alert{border-left:4px solid #F59E0B;background:#FFFBEA;border-radius:6px;padding:6px 9px;margin:6px 0;font-size:13px}
#sunops .alert.hi{border-color:#B42318;background:#FEF3F2}#sunops .alert.info{border-color:#2563eb;background:#EFF6FF}#sunops .alert.ok{border-color:#16803D;background:#F0FAF3}
#sunops .alert small{color:var(--mut,#5B6360);display:block}
#sunops .feed{max-height:300px;overflow:auto}
#sunops .form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
#sunops .form label{display:block;font-size:12px;color:var(--mut,#5B6360);margin-bottom:2px}
#sunops .form input,#sunops .form select{width:100%}
#sunops .role{display:inline-flex;gap:6px;align-items:center}
#sunops .tag{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;background:var(--line,#E7E0CA)}
#sunops .spin{display:inline-block;width:14px;height:14px;border:2px solid #ddd;border-top-color:#F59E0B;border-radius:50%;animation:so-spin 1s linear infinite;vertical-align:-2px}
@keyframes so-spin{to{transform:rotate(360deg)}}
#sunops input[type=range]{padding:0;width:min(420px,100%)}
#sunops .mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
#sunopsEntry{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px;border:1px solid #F0D98A;background:#FFF9E3}
#sunopsEntry b{font-size:15px}#sunopsEntry .sp{flex:1}
#sunopsEntry button.pri{background:var(--amb,#F59E0B);border-color:var(--amb,#F59E0B);font-weight:700}
@media print{#sunops{display:none!important}}
`;

/* ---------------- tiny SHA-256 (sync) for the tamper-evident audit chain ---------------- */
function sha256(str) {
  const K = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];
  const rr = (x, n) => (x >>> n) | (x << (32 - n));
  const bytes = []; const u = unescape(encodeURIComponent(str)); for (let i = 0; i < u.length; i++) bytes.push(u.charCodeAt(i));
  const l = bytes.length * 8; bytes.push(0x80); while (bytes.length % 64 !== 56) bytes.push(0);
  for (let i = 7; i >= 0; i--) bytes.push(i >= 4 ? 0 : (l >>> (i * 8)) & 255);
  let h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  for (let o = 0; o < bytes.length; o += 64) {
    const w = new Array(64);
    for (let i = 0; i < 16; i++) w[i] = (bytes[o + 4 * i] << 24) | (bytes[o + 4 * i + 1] << 16) | (bytes[o + 4 * i + 2] << 8) | bytes[o + 4 * i + 3];
    for (let i = 16; i < 64; i++) { const s0 = rr(w[i - 15], 7) ^ rr(w[i - 15], 18) ^ (w[i - 15] >>> 3), s1 = rr(w[i - 2], 17) ^ rr(w[i - 2], 19) ^ (w[i - 2] >>> 10); w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0; }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const S1 = rr(e, 6) ^ rr(e, 11) ^ rr(e, 25), ch = (e & f) ^ (~e & g), t1 = (hh + S1 + ch + K[i] + w[i]) | 0;
      const S0 = rr(a, 2) ^ rr(a, 13) ^ rr(a, 22), mj = (a & b) ^ (a & c) ^ (b & c), t2 = (S0 + mj) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    h = [(h[0] + a) | 0, (h[1] + b) | 0, (h[2] + c) | 0, (h[3] + d) | 0, (h[4] + e) | 0, (h[5] + f) | 0, (h[6] + g) | 0, (h[7] + hh) | 0];
  }
  return h.map(x => (x >>> 0).toString(16).padStart(8, '0')).join('');
}
SunOps.sha256 = sha256;

/* ---------------- charts (plain SVG, no library) ---------------- */
function niceMax(v) { if (!(v > 0)) return 1; const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p; }
function line(o) {                       // returns {html, meta}. o.n points; series[{name,color,data,dash,w,upTo,fill,fmt,hidden}]
  const W = o.w || 820, H = o.h || 270, m = { l: 56, r: 14, t: 12, b: 30 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b, n = o.n;
  const vis = o.series.filter(s => !s.hidden);
  let lo = o.yMin != null ? o.yMin : 0, hi = o.yMax;
  if (hi == null) { hi = 0; for (const s of vis) { const up = s.upTo == null ? n : Math.min(n, s.upTo); for (let i = 0; i < up; i++) { const v = s.data[i]; if (Number.isFinite(v) && v > hi) hi = v; } } hi = niceMax(hi * 1.05); }
  const X = i => m.l + iw * (i + 0.5) / n, Y = v => m.t + ih * (1 - (v - lo) / (hi - lo || 1));
  let g = '';
  for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4, y = Y(v); g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="#E7E0CA" stroke-width="1"/><text x="${m.l - 6}" y="${y + 4}" text-anchor="end" font-size="11" fill="#5B6360">${o.yFmt ? o.yFmt(v) : fmt0(v)}</text>`; }
  for (const b of (o.bands || [])) { const x1 = m.l + iw * b.a / n, x2 = m.l + iw * b.b / n; g += `<rect x="${x1}" y="${m.t}" width="${Math.max(0, x2 - x1)}" height="${ih}" fill="${b.fill || '#F59E0B'}" opacity="${b.op || .1}"/>`; if (b.label) g += `<text x="${x1 + 4}" y="${m.t + 12}" font-size="10" fill="${b.text || '#92400E'}">${esc(b.label)}</text>`; }
  for (const t of (o.xTicks || [])) g += `<line x1="${m.l + iw * t.i / n}" x2="${m.l + iw * t.i / n}" y1="${m.t + ih}" y2="${m.t + ih + 4}" stroke="#999"/><text x="${m.l + iw * t.i / n}" y="${H - 10}" text-anchor="middle" font-size="11" fill="#5B6360">${esc(t.label)}</text>`;
  for (const s of vis) {
    const up = s.upTo == null ? n : Math.min(n, s.upTo); let d = '', pen = false, first = -1, last = -1;
    for (let i = 0; i < up; i++) { const v = s.data[i]; if (!Number.isFinite(v)) { pen = false; continue; } d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1); pen = true; if (first < 0) first = i; last = i; }
    if (!d) continue;
    if (s.fill) g += `<path d="${d}L${X(last).toFixed(1)} ${Y(lo)}L${X(first).toFixed(1)} ${Y(lo)}Z" fill="${s.color}" opacity=".16"/>`;
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.w || 2}" ${s.dash ? 'stroke-dasharray="' + s.dash + '"' : ''} stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  for (const h of (o.hlines || [])) if (Number.isFinite(h.v)) g += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(h.v)}" y2="${Y(h.v)}" stroke="${h.color || '#B42318'}" stroke-dasharray="${h.dash || '5 4'}" stroke-width="1.5"/>${h.label ? `<text x="${W - m.r - 4}" y="${Y(h.v) - 4}" text-anchor="end" font-size="11" fill="${h.color || '#B42318'}">${esc(h.label)}</text>` : ''}`;
  for (const v of (o.vlines || [])) g += `<line x1="${X(v.i)}" x2="${X(v.i)}" y1="${m.t}" y2="${m.t + ih}" stroke="${v.color || '#252B2A'}" stroke-width="1.5"/>${v.label ? `<text x="${X(v.i) + 4}" y="${m.t + 22}" font-size="11" fill="${v.color || '#252B2A'}">${esc(v.label)}</text>` : ''}`;
  for (const k of (o.markers || [])) g += `<path d="M${X(k.i) - 5} ${m.t + ih + 1}L${X(k.i) + 5} ${m.t + ih + 1}L${X(k.i)} ${m.t + ih - 9}Z" fill="${k.color || '#B42318'}"><title>${esc(k.title || '')}</title></path>`;
  if (o.yLabel) g += `<text x="12" y="${m.t + ih / 2}" transform="rotate(-90 12 ${m.t + ih / 2})" text-anchor="middle" font-size="11" fill="#5B6360">${esc(o.yLabel)}</text>`;
  const html = `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.aria || o.title || 'chart')}"><rect x="0" y="0" width="${W}" height="${H}" fill="#fff"/>${g}<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" stroke="#252B2A" stroke-width="1" opacity="0" /></svg><div class="tip"></div></div>`;
  return { html, meta: { o, W, H, m, iw, n } };
}
function bars(o) {                       // grouped bars: labels[], series[{name,color,data}]
  const W = o.w || 820, H = o.h || 270, m = { l: 62, r: 14, t: 12, b: 34 }, iw = W - m.l - m.r, ih = H - m.t - m.b, n = o.labels.length;
  const vis = o.series.filter(s => !s.hidden);
  let hi = 0; for (const s of vis) for (const v of s.data) if (Number.isFinite(v) && v > hi) hi = v; hi = niceMax(hi * 1.05);
  const Y = v => m.t + ih * (1 - v / hi); let g = '';
  for (let k = 0; k <= 4; k++) { const v = hi * k / 4, y = Y(v); g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="#E7E0CA"/><text x="${m.l - 6}" y="${y + 4}" text-anchor="end" font-size="11" fill="#5B6360">${o.yFmt ? o.yFmt(v) : fmt0(v)}</text>`; }
  const gw = iw / n, bw = Math.min(26, gw * 0.8 / Math.max(1, vis.length));
  for (let i = 0; i < n; i++) {
    const cx = m.l + gw * (i + 0.5);
    vis.forEach((s, j) => { const v = s.data[i]; if (!Number.isFinite(v)) return; const x = cx - bw * vis.length / 2 + j * bw; g += `<rect x="${x}" y="${Y(v)}" width="${bw - 1}" height="${Math.max(0, m.t + ih - Y(v))}" fill="${s.color}" rx="2"><title>${esc(o.labels[i] + ' - ' + s.name + ': ' + (o.yFmt ? o.yFmt(v) : fmt0(v)))}</title></rect>`; });
    g += `<text x="${cx}" y="${H - 14}" text-anchor="middle" font-size="11" fill="#5B6360">${esc(o.labels[i])}</text>`;
  }
  if (o.yLabel) g += `<text x="12" y="${m.t + ih / 2}" transform="rotate(-90 12 ${m.t + ih / 2})" text-anchor="middle" font-size="11" fill="#5B6360">${esc(o.yLabel)}</text>`;
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.aria || 'bar chart')}"><rect width="${W}" height="${H}" fill="#fff"/>${g}</svg></div>`;
}
function mount(el, c) {                  // inject a line chart and wire the hover read-out
  if (!el) return;
  el.innerHTML = c.html;
  const { o, W, m, iw, n } = c.meta, svg = el.querySelector('svg'), tip = el.querySelector('.tip'), xh = el.querySelector('.xh'), box = el.querySelector('.chart');
  if (!svg || o.noHover) return;
  const move = e => {
    const r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * (W / r.width);
    let i = Math.floor((px - m.l) / iw * n); if (i < 0 || i >= n) { tip.style.display = 'none'; xh.setAttribute('opacity', 0); return; }
    const xx = m.l + iw * (i + 0.5); xh.setAttribute('x1', xx); xh.setAttribute('x2', xx); xh.setAttribute('opacity', .5);
    let h = `<b>${esc(o.tipTitle ? o.tipTitle(i) : i)}</b>`;
    for (const s of o.series) { if (s.hidden) continue; const up = s.upTo == null ? n : s.upTo; const v = i < up ? s.data[i] : NaN; if (!Number.isFinite(v)) continue; h += `<div><span style="color:${s.color}">&#9632;</span> ${esc(s.name)}: <b>${(s.fmt || (o.tipFmt) || fmt0)(v)}</b></div>`; }
    tip.innerHTML = h; tip.style.display = 'block';
    const bw = box.getBoundingClientRect(); let left = (e.clientX - bw.left) + 14; if (left + 190 > bw.width) left = (e.clientX - bw.left) - 200; tip.style.left = Math.max(4, left) + 'px'; tip.style.top = Math.max(4, (e.clientY - bw.top) - 20) + 'px';
  };
  svg.addEventListener('pointermove', move); svg.addEventListener('pointerleave', () => { tip.style.display = 'none'; xh.setAttribute('opacity', 0); });
}
function legend(items, state, onToggle) {   // chips that show/hide series
  return `<div class="legend">${items.map(it => `<label class="${state[it.key] === false ? 'off' : ''}"><input type="checkbox" data-leg="${esc(it.key)}" ${state[it.key] === false ? '' : 'checked'} style="display:none"><i style="background:${it.color}"></i>${esc(it.name)}</label>`).join('')}</div>`;
}

/* ==========================================================================
   State, roles, audit trail, data loading, live replay glue
   ========================================================================== */
const LS = {
  get(k, def) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : def; } catch (e) { return def; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { } }
};
const ROLES = {
  owner: { name: 'Owner', can: ['run', 'settings', 'propose', 'approve', 'export', 'verify', 'read'], blurb: 'Can do everything.' },
  analyst: { name: 'Analyst', can: ['run', 'settings', 'propose', 'read'], blurb: 'Runs the replay, changes settings and data, proposes plan changes. Cannot approve.' },
  operator: { name: 'Operator', can: ['run', 'propose', 'read'], blurb: 'Runs the replay and proposes plan changes. Cannot change settings or approve.' },
  approver: { name: 'Approver', can: ['approve', 'export', 'read'], blurb: 'Approves or rejects proposed plans. Cannot change data or settings.' },
  auditor: { name: 'Auditor', can: ['export', 'verify', 'read'], blurb: 'Read-only. Checks the audit trail and exports it.' }
};
const ST = {
  open: false, fac: 'leader', role: (typeof window !== 'undefined' && window.sunWho && ROLES[window.sunWho]) ? window.sunWho : 'owner', tab: 'live', status: 'idle', msg: '', raw: null, D: null, ctx: null, R: null, twinCache: {},
  cfg: Object.assign({ bat: Object.assign({}, DEFAULTS.bat), tariff: Object.assign({}, DEFAULTS.tariff), ef: DEFAULTS.ef, margin: 0.25, demandMode: 'auto' }, HAS_DOM ? LS.get('sunops_cfg', {}) : {}),
  sel: { day: 0, month: 0, strat: 'cost', mode: 'live', scView: 'day', vis: { base: true, self: false, tou: true, cost: true, bal: false, zero: false }, showF: false },
  live: null, props: [], propSeq: 0, audit: HAS_DOM ? LS.get('sunops_audit', []) : [], bills: HAS_DOM ? LS.get('sunops_bills', {}) : {}, upload: null
};
ST.cfg.bat = Object.assign({}, DEFAULTS.bat, ST.cfg.bat); ST.cfg.tariff = Object.assign({}, DEFAULTS.tariff, ST.cfg.tariff);
const can = a => ROLES[ST.role].can.includes(a);
if (typeof window !== 'undefined') window.sunSetRole = function (r) {
  if (!ROLES[r] || ST.role === r) return;
  ST.role = r; audit('role_switch', 'Now viewing as ' + ROLES[r].name); stopLive(); ST.tab = can('run') ? 'live' : 'appr';
  if (ST.open && typeof render === 'function') render();
};
const saveCfg = () => LS.set('sunops_cfg', ST.cfg);

/* ---- tamper-evident audit trail (browser-local demo; production would write to the server table) ---- */
function audit(action, detail, actor) {
  const last = ST.audit[ST.audit.length - 1];
  const e = { seq: last ? last.seq + 1 : 1, at: new Date().toISOString(), actor: actor || ST.role, action, detail: String(detail || ''), prev: last ? last.hash : 'GENESIS' };
  e.hash = sha256([e.prev, e.seq, e.at, e.actor, e.action, e.detail].join('|'));
  ST.audit.push(e); if (ST.audit.length > 500) ST.audit.splice(0, ST.audit.length - 500);
  LS.set('sunops_audit', ST.audit); return e;
}
function verifyAudit() {
  let bad = -1;
  for (let i = 0; i < ST.audit.length; i++) {
    const e = ST.audit[i];
    if (i > 0 && e.prev !== ST.audit[i - 1].hash) { bad = e.seq; break; }
    if (sha256([e.prev, e.seq, e.at, e.actor, e.action, e.detail].join('|')) !== e.hash) { bad = e.seq; break; }
  }
  return { ok: bad < 0, bad, n: ST.audit.length };
}

/* ---- time labels ---- */
const tmS = s => { const m = Math.round(s * ST.ctx.dt * 60); return p2(Math.floor(m / 60)) + ':' + p2(m % 60); };
const stratOf = k => STRATS[k];
const isOpt = k => STRATS[k].rule === 'opt';
const Rof = k => ST.R[k + '_' + (isOpt(k) ? ST.sel.mode : 'plan')];
const demandModeNow = () => ST.cfg.demandMode === 'auto' ? (ST.raw && ST.raw.netHint.suggested ? 'net' : 'gross') : ST.cfg.demandMode;

/* ---- loading + computing ---- */
function setStatus(status, msg) { ST.status = status; ST.msg = msg || ''; if (HAS_DOM) renderStatusOnly(); }
const FACS = {
  leader: { name: "Leader Energy manufacturing site (organisers' synthetic data, 2025)", short: 'Leader site', file: 'leader_site.csv', step: '30-min', has3d: false },
  lab: { name: 'Lab factory: small 3D prototype site (simulated 15-minute data)', short: 'Lab factory', file: 'lab_site.csv', step: '15-min', has3d: true, bat: { kwh: 200, kw: 100 } }
};
async function loadPreset(key) {
  if (key) ST.fac = key; const f = FACS[ST.fac];
  if (f.bat) { if (!ST._bat0) ST._bat0 = ST.cfg.bat; ST.cfg.bat = Object.assign({}, ST._bat0, f.bat); } else if (ST._bat0) { ST.cfg.bat = ST._bat0; ST._bat0 = null; }
  setStatus('loading', 'Loading ' + f.short + ' data...');
  try {
    const r = await fetch(f.file, { cache: 'no-cache' });
    if (!r.ok) throw new Error(f.file + ' not found on the server (HTTP ' + r.status + '). Add it next to index.html.');
    const rows = csvToRows(await r.text()), map = detectColumns(rows);
    await adoptSeries(rowsToSeries(rows, map), { name: f.name, source: 'preset' });
  } catch (e) { setStatus('error', String(e.message || e)); }
}
async function adoptSeries(series, info) {
  setStatus('loading', 'Checking the data...');
  await new Promise(r => setTimeout(r, 20));
  ST.raw = buildDataset(series, info);
  audit('data_loaded', info.name + ': ' + ST.raw.nD + ' days, ' + ST.raw.stepMin + '-minute readings, ' + ST.raw.repaired + ' intervals repaired');
  await recompute();
}
async function recompute() {
  if (!ST.raw) return;
  stopLive(); ST.live = null; ST.twinCache = {};
  const mode = demandModeNow();
  ST.D = withDemandMode(ST.raw, mode);
  ST.ctx = makeCtx(ST.D, { bat: ST.cfg.bat, tariff: ST.cfg.tariff, ef: ST.cfg.ef, margin: ST.cfg.margin });
  const jobs = [['base', 'plan'], ['self', 'plan'], ['tou', 'plan'], ['cost', 'plan'], ['cost', 'live'], ['bal', 'plan'], ['bal', 'live'], ['zero', 'plan'], ['zero', 'live']];
  const R = {};
  for (let i = 0; i < jobs.length; i++) {
    setStatus('loading', 'Planning the year, strategy ' + (i + 1) + ' of ' + jobs.length + '...');
    await new Promise(r => setTimeout(r, 16));
    R[jobs[i][0] + '_' + jobs[i][1]] = runStrategy(ST.ctx, STRATS[jobs[i][0]], jobs[i][1]);
  }
  ST.R = R;
  // default day: the highest billed-window peak in the baseline, after the first two weeks of warm-up
  const md = dailyMd(ST.ctx, R.base_plan), mc = dailyMd(ST.ctx, R.cost_live), cl = R.cost_live, spd = ST.ctx.spd, dt = ST.ctx.dt; let best = 14, bv = -1, any = false;
  // best day to show: biggest cut in the billed-window peak, among weekdays where the battery is topped up before the window and EV charging really moves
  for (let d = 14; d < ST.D.nD; d++) {
    if (!isWeekday(ST.D.days[d].dow)) continue;
    const o = d * spd; let pre = 0, evE = 0, thr = 0;
    for (let s = Math.round(10 / dt); s < Math.round(14 / dt); s++) pre = Math.max(pre, cl.soc[o + s]);
    for (let s = 0; s < spd; s++) { evE += cl.ev[o + s] * dt; thr += Math.abs(cl.batt[o + s]) * dt; }
    const ok = pre >= 0.7 && evE >= 800 && thr >= 800, sh = md[d] - mc[d];
    if ((ok && !any) || (ok === any && sh > bv)) { any = any || ok; bv = sh; best = d; }
  }
  ST.demoDay = best;
  ST.sel.day = best; ST.picks = null;
  const ms = monthsOf(ST.D); ST.sel.month = Math.min(ST.sel.month, ms.length - 1); if (ST.sel.month === 0 && ms.length > 1) ST.sel.month = ms.findIndex(m => ST.sel.day >= m.d0 && ST.sel.day <= m.d1);
  setStatus('ready', '');
  audit('plan_computed', 'Demand read as ' + (mode === 'net' ? 'grid draw after solar (solar added back)' : 'demand before solar') + '; battery ' + ST.cfg.bat.kwh + ' kWh / ' + ST.cfg.bat.kw + ' kW; forecast margin ' + Math.round(ST.cfg.margin * 100) + '%');
  if (HAS_DOM) render();
}

/* ---- live replay ---- */
function stopLive() { if (ST.live && ST.live.timer) { clearInterval(ST.live.timer); ST.live.timer = null; } if (ST.live) ST.live.playing = false; }
function runDay(ctx, d, soc0, m0, strat, mode, inputs) { const X = newDay(ctx, d, soc0, m0, strat, mode, inputs); for (let s = 0; s < ctx.spd; s++) { prepareSlot(X, s); resolveProposal(X, true); execSlot(X, s); } return X; }
function liveStart(d, keep) {
  stopLive();
  if (!['cost', 'bal', 'zero'].includes(ST.sel.strat)) ST.sel.strat = 'cost';
  const ctx = ST.ctx, D = ST.D, spd = ctx.spd, o = d * spd, strat = STRATS[ST.sel.strat], mode = ST.sel.mode;
  const Rr = ST.R[strat.key + '_' + mode], inject = (ST.live && keep && keep.inject) || (ST.live && ST.live.inject) || 'none';
  const L = Float64Array.from(D.load.subarray(o, o + spd)), P = Float64Array.from(D.pv.subarray(o, o + spd)), E = Float64Array.from(D.ev.subarray(o, o + spd));
  const hr = s => s * ctx.dt;
  if (inject === 'cloud' || inject === 'both') for (let s = 0; s < spd; s++) if (hr(s) >= 12 && hr(s) < 15) P[s] *= 0.4;
  if (inject === 'surge' || inject === 'both') for (let s = 0; s < spd; s++) if (hr(s) >= 15 && hr(s) < 19) L[s] *= 1.25;
  const f0 = forecastDay(ctx, d);
  const B = runDay(ctx, d, 0, 0, STRATS.base, 'plan', { L, P, E });
  const T = runDay(ctx, d, Rr.soc0[d], Rr.m0[d], strat, 'plan', { L: Float64Array.from(f0.L), P: Float64Array.from(f0.P), E });
  const X = newDay(ctx, d, Rr.soc0[d], Rr.m0[d], strat, mode, { L, P, E });
  const prevReq = ST.live ? ST.live.require : false, prevSpeed = ST.live ? ST.live.speed : 800;
  ST.live = { d, X, B, T, idx: 0, prepared: -1, playing: false, timer: null, speed: prevSpeed, alerts: [], pending: null, inject, require: prevReq, quiet: false,
    dev: { load: { n: 0, ok: 0, on: false }, pv: { n: 0, ok: 0, on: false }, twin: { n: 0, ok: 0, on: false } }, flags: { empty: false, over: 0 }, done: false };
  if (HAS_DOM) drawLive();
}
function addAlert(level, title, text, slot) {
  const L = ST.live; L.alerts.push({ slot, level, title, text });
  if (!L.quiet && level !== 'ok') audit('alert', tmS(slot) + ' ' + title + ' - ' + text, 'system');
}
function addProposal(p, status, by) {
  if (ST.live.quiet) return;
  const rec = Object.assign({ id: ++ST.propSeq, date: ST.D.days[ST.live.d].date, strat: ST.sel.strat, status, by: by || (status === 'auto' ? 'auto (demo mode)' : ''), at: new Date().toISOString() }, p);
  ST.props.push(rec); ST.live.pendingRec = status === 'pending' ? rec : ST.live.pendingRec;
  audit('plan_' + status, rec.date + ' ' + tmS(p.slot) + ' ' + p.type + ': ' + (p.from == null ? 'none' : fmt0(p.from)) + ' -> ' + fmt0(p.to) + ' kW cap. ' + p.reason, status === 'auto' ? 'system' : ST.role);
  return rec;
}
function liveStep() {
  const L = ST.live; if (!L || L.done) return false;
  if (L.pending) return false;
  const ctx = ST.ctx, X = L.X, s = L.idx, spd = ctx.spd, strat = X.strat;
  if (L.prepared !== s) {
    const prop = prepareSlot(X, s); L.prepared = s;
    if (strat.rule === 'opt' && s === 0 && Number.isFinite(X.cap)) {
      const p = { type: 'Day plan', slot: 0, from: null, to: X.cap, reason: 'Day-ahead plan: hold grid import under ' + fmt0(X.cap) + ' kW in the peak window, keep ' + fmt0(X.Sreq * 100) + '% charge for it, ' + fmt0(X.Etot) + ' kWh of EV charging timed by the plan.' };
      if (L.require && !L.quiet) { X.capApplied = Infinity; L.pending = { kind: 'plan', p }; addProposal(p, 'pending'); return false; }
      X.capApplied = X.cap; addProposal(p, 'auto');
    } else if (prop) {
      const p = { type: 'Plan change', slot: s, from: prop.from, to: prop.to, reason: 'Forecast corrected (solar x' + (X.ratioP || 1).toFixed(2) + ', demand x' + (X.ratioL || 1).toFixed(2) + '), battery at ' + fmt0(prop.soc * 100) + '%.' };
      if (L.require && !L.quiet) { L.pending = { kind: 'change', p }; addProposal(p, 'pending'); return false; }
      resolveProposal(X, true); addProposal(p, 'auto');
    }
  }
  const capBefore = X.capApplied;
  execSlot(X, s);
  analyse(s);
  L.idx++;
  if (L.idx >= spd) { L.done = true; stopLive(); if (!L.quiet) audit('replay_finished', ST.D.days[L.d].date + ' ' + strat.short + ': billed-window peak ' + fmt0(windowPeak(X.out.grid, X)) + ' kW vs baseline ' + fmt0(windowPeak(L.B.out.grid, L.B)) + ' kW'); }
  return true;
}
const windowPeak = (g, X) => X.wd ? bp(g, 0, X.ctx.w0, X.ctx.w1, blockOf(X.ctx)).v : 0;
function analyse(s) {
  const L = ST.live, X = L.X, f0 = X.f0, ctx = ST.ctx, pad = tmS(s);
  const ep = (name, flag, start) => { const e = L.dev[name]; if (flag) { e.n++; e.ok = 0; if (!e.on && e.n >= 2) { e.on = true; start(); } } else { e.ok++; e.n = 0; if (e.on && e.ok >= 2) { e.on = false; addAlert('ok', 'Back within tolerance', name === 'load' ? 'Demand is tracking the forecast again.' : name === 'pv' ? 'Solar is tracking the forecast again.' : 'Grid import is tracking the twin again.', s); } } };
  const dL = X.L[s] - f0.L[s], thL = Math.max(0.2 * f0.L[s], 100);
  ep('load', Math.abs(dL) >= thL, () => addAlert('warn', 'Demand ' + (dL > 0 ? 'above' : 'below') + ' forecast', fmt0(Math.abs(dL)) + ' kW (' + fmt0(Math.abs(dL) / Math.max(f0.L[s], 1) * 100) + '%) for two intervals. Forecast corrected for the rest of the day; plan re-checked.', s - 1));
  const dP = X.P[s] - f0.P[s], thP = Math.max(0.25 * f0.P[s], 100);
  ep('pv', Math.abs(dP) >= thP, () => addAlert('warn', 'Solar ' + (dP > 0 ? 'above' : 'below') + ' forecast', fmt0(Math.abs(dP)) + ' kW (' + fmt0(Math.abs(dP) / Math.max(f0.P[s], 1) * 100) + '%) for two intervals. Forecast corrected for the rest of the day; plan re-checked.', s - 1));
  const pr = L.T.out.grid[s], ac = X.out.grid[s], thT = Math.max(0.15 * Math.max(pr, 50), 100);
  ep('twin', Math.abs(ac - pr) >= thT, () => addAlert('hi', 'Digital twin out of tolerance', 'Measured grid import is ' + fmt0(Math.abs(ac - pr)) + ' kW ' + (ac > pr ? 'above' : 'below') + ' what the twin expected. The plan is based on a forecast that is not holding today.', s - 1));
  if (X.strat.bat && X.inWin(s) && X.soc <= X.socMin + 0.02 && X.out.net[s] > X.capApplied + 50 && !L.flags.empty) { L.flags.empty = true; addAlert('hi', 'Battery empty in the peak window', 'Demand is above the cap and no charge is left. The billed peak for this month may rise.', s); }
  if (X.strat.bat && X.inWin(s) && Number.isFinite(X.capApplied) && ac > X.capApplied + 25 && ac > L.flags.over + 50) { L.flags.over = ac; addAlert('hi', 'Grid import above the cap', fmt0(ac) + ' kW against a cap of ' + fmt0(X.capApplied) + ' kW. This sets a new running peak.', s); }
}
function liveSeek(k) {
  const d = ST.live.d, inj = ST.live.inject, req = ST.live.require, sp = ST.live.speed;
  liveStart(d, { inject: inj }); ST.live.require = false; ST.live.quiet = true; ST.live.speed = sp;
  while (ST.live.idx < k && liveStep()) { }
  ST.live.quiet = false; ST.live.require = req; ST.propSeq = ST.propSeq;
}
function livePlay() {
  const L = ST.live; if (!L || L.done || L.playing) return; L.playing = true;
  audit('replay_play', ST.D.days[L.d].date + ' ' + ST.sel.strat + ' (' + (ST.sel.mode === 'live' ? 're-plan every interval' : 'day-ahead plan only') + ') inject=' + L.inject);
  const tick = () => { if (!ST.live || ST.live !== L) return; liveStep(); drawLive(); if (L.done) { drawLive(); renderLiveButtons(); } };
  if (L.speed === 0) { while (!L.done && !L.pending) liveStep(); stopLive(); drawLive(); renderLiveButtons(); return; }
  L.timer = setInterval(tick, L.speed); renderLiveButtons();
}
function decide(id, accept) {
  const L = ST.live, rec = ST.props.find(p => p.id === id); if (!rec || rec.status !== 'pending') return;
  rec.status = accept ? 'approved' : 'rejected'; rec.by = ROLES[ST.role].name; rec.decidedAt = new Date().toISOString();
  audit('plan_' + rec.status, rec.date + ' ' + tmS(rec.slot) + ' ' + rec.type + ' ' + (accept ? 'approved' : 'rejected') + ' by ' + ROLES[ST.role].name);
  if (L && L.pending && L.pending.p.slot === rec.slot && L.pending.p.type === rec.type) {
    const X = L.X, kind = L.pending.kind;
    if (kind === 'plan') { X.capApplied = accept ? L.pending.p.to : Infinity; if (!accept) X.noPlan = true; } else resolveProposal(X, accept);
    L.pending = null; L.pendingRec = null;
    if (!accept) addAlert('info', rec.type + ' rejected', kind === 'plan' ? 'No plan approved, so the battery will not discharge in the peak window today.' : 'The previous cap stays in place.', rec.slot);
  }
  if (HAS_DOM) { drawLive(); renderApprovalsIfOpen(); }
}

/* ==========================================================================
   Tabs
   ========================================================================== */
const $root = () => HAS_DOM ? document.getElementById('sunops') : null;
const $q = sel => { const r = $root(); return r ? r.querySelector(sel) : null; };
const dayLabel = d => DOWS[ST.D.days[d].dow] + ' ' + ST.D.days[d].date;
const monthOpts = sel => monthsOf(ST.D).map((m, i) => `<option value="${i}" ${i === sel ? 'selected' : ''}>${MONTHS[m.month]} ${m.key.slice(0, 4)}</option>`).join('');
const stratOpts = (sel, keys) => (keys || STRAT_ORDER).map(k => `<option value="${k}" ${k === sel ? 'selected' : ''}>${esc(STRATS[k].short)}</option>`).join('');
const dis = ok => ok ? '' : 'disabled';
const TABS = [['live', 'Play the day'], ['scen', 'Compare plans'], ['bill', 'Monthly bill vs TNB'], ['twin', 'Did the forecast hold?'], ['data', 'Data health'], ['appr', 'Approvals & audit'], ['fac', '3D factory'], ['set', 'Settings & data']];

function shell() {
  return `<div class="sotop"><h1>Operations</h1><span class="tag">Planning simulation. Never controls equipment.</span><span class="sp"></span>
    <span class="role"><span class="note" style="margin:0">Facility</span><span class="seg" role="group" aria-label="Facility">${Object.keys(FACS).map(k => `<button data-act="fac" data-v="${k}" class="${ST.fac === k ? 'on' : ''}" title="${esc(FACS[k].name)}">${esc(FACS[k].short)} (${FACS[k].step})</button>`).join('')}</span></span></div>
    <div class="note" style="margin:0 0 8px">${esc(ROLES[ST.role].blurb)}</div>
    <div class="tabs" role="tablist">${TABS.filter(t => t[0] !== 'fac' || FACS[ST.fac].has3d).map(t => `<button role="tab" data-act="tab" data-v="${t[0]}" class="${ST.tab === t[0] ? 'on' : ''}">${t[1]}</button>`).join('')}</div>
    <div id="so_body"></div>`;
}
function loadingCard() {
  if (ST.status === 'error') return `<div class="card"><div class="bad"><b>Could not load the data.</b> ${esc(ST.msg)}</div><div class="bar"><button class="pri" data-act="retry">Try again</button><button data-act="tab" data-v="set">Upload a file instead</button></div></div>`;
  return `<div class="card"><span class="spin"></span> <b id="so_status">${esc(ST.msg || 'Starting...')}</b><p class="note">The planner replays a full year of 30-minute readings in your browser. Nothing is uploaded.</p></div>`;
}
function renderStatusOnly() { const el = $q('#so_status'); if (el && ST.status === 'loading') el.textContent = ST.msg; else if (ST.open && ST.status !== 'ready') render(); }
function render() {
  const root = $root(); if (!root || !ST.open) return;
  facStop();
  root.querySelector('.wrap').innerHTML = shell();
  const body = $q('#so_body');
  if (ST.tab === 'set') { body.innerHTML = bodySettings(); return; }
  if (ST.status !== 'ready') { body.innerHTML = loadingCard(); return; }
  const fn = { live: bodyLive, scen: bodyScen, bill: bodyBill, twin: bodyTwin, data: bodyData, appr: bodyAppr, fac: bodyFactory }[ST.tab];
  body.innerHTML = fn();
  ({ live: mountLive, scen: mountScen, bill: mountBill, twin: mountTwin, data: mountData, appr: () => { }, fac: mountFactory }[ST.tab] || (() => { }))();
}
function renderApprovalsIfOpen() { if (ST.open && ST.tab === 'appr') render(); }

/* ---------- shared bits ---------- */
function legendFor(items, vis) { return `<div class="legend">${items.map(it => `<label data-act="leg" data-v="${it.key}" class="${vis[it.key] === false ? 'off' : ''}" title="Show or hide"><i style="background:${it.color}"></i>${esc(it.name)}</label>`).join('')}</div>`; }
const caveat = () => `<div class="note"><b>How to read the numbers.</b> A planning simulation on ${ST.raw && ST.raw.source === 'preset' ? "the organisers' synthetic data" : 'the uploaded data'}. The battery (${fmt0(ST.cfg.bat.kwh)} kWh / ${fmt0(ST.cfg.bat.kw)} kW) is an assumption and its cost is not counted. No export income. Retail charge included; KWTBB levy and tax are not. These are model results, not proven savings.</div>`;
function xTicksDay() { const ctx = ST.ctx, t = []; for (let h = 0; h < 24; h += 3) t.push({ i: h / ctx.dt, label: p2(h) + ':00' }); return t; }

/* ======================= LIVE REPLAY ======================= */
function pickDays() {
  if (ST.picks && ST.picks.key === ST.R) return ST.picks;
  const ctx = ST.ctx, D = ST.D, spd = ctx.spd, md = dailyMd(ctx, ST.R.base_plan), p = { key: ST.R, list: [] };
  const mc = dailyMd(ctx, ST.R.cost_live); let hiPeak = 14, bv = -1, solar = 14, sv = -1, dem = 14, dv = -1, cut = 14, cv = -1;
  const wk = [];
  for (let d = 14; d < D.nD; d++) {
    if (md[d] > bv) { bv = md[d]; hiPeak = d; }
    if (md[d] - mc[d] > cv) { cv = md[d] - mc[d]; cut = d; }
    const f = forecastDay(ctx, d); let es = 0, el = 0, any = false;
    for (let s = Math.round(8 / ctx.dt); s < Math.round(17 / ctx.dt); s++) { es += Math.abs(D.pv[d * spd + s] - f.P[s]); el += Math.abs(D.load[d * spd + s] - f.L[s]); }
    if (isWeekday(D.days[d].dow) && !D.flags.subarray(d * spd, d * spd + spd).some(x => x)) { if (es > sv) { sv = es; solar = d; } if (el > dv) { dv = el; dem = d; } wk.push([d, md[d]]); }
  }
  wk.sort((a, b) => a[1] - b[1]); const typ = wk.length ? wk[wk.length >> 1][0] : 14;
  const sat = D.days.findIndex((x, i) => i > 14 && x.dow === 6);
  const gap = D.days.findIndex((x, i) => D.flags.subarray(i * spd, i * spd + spd).some(v => v));
  p.list = [['Best day to demo (big peak cut, battery and EV active)', ST.demoDay != null ? ST.demoDay : cut], ['Biggest peak cut by the optimiser', cut], ['Highest billed peak of the year', hiPeak], ['Biggest solar surprise', solar], ['Biggest demand surprise', dem], ['A typical weekday', typ], ['A Saturday', sat]];
  if (gap >= 0) p.list.push(['Meter dropout (repaired data)', gap + 1 < D.nD ? gap + 1 : gap]);
  ST.picks = p; return p;
}
function bodyLive() {
  if (!ST.live) liveStart(ST.sel.day);
  const D = ST.D, L = ST.live, picks = pickDays().list;
  const a = can('run');
  return `<div class="card soft"><div class="bar">
      <b>Replay a day, interval by interval.</b><span class="note" style="margin:0">The planner only sees readings up to "now", re-checks the plan as each ${ST.ctx.stepMin || ST.D.stepMin}-minute reading arrives, and flags anything that drifts from the forecast or the digital twin.</span></div>
    <div class="bar">
      <label class="note">Day <input type="date" id="lv_date" value="${D.days[L.d].date}" min="${D.days[0].date}" max="${D.days[D.nD - 1].date}" ${dis(a)}></label>
      <select id="lv_pick" ${dis(a)}><option value="">Jump to...</option>${picks.map(p => `<option value="${p[1]}">${esc(p[0])}: ${D.days[p[1]].date}</option>`).join('')}</select>
      <label class="note">Strategy <select id="lv_strat" ${dis(a)}>${stratOpts(ST.sel.strat, ['cost', 'bal', 'zero'])}</select></label>
      <label class="note">Planner <select id="lv_mode" ${dis(a)}><option value="live" ${ST.sel.mode === 'live' ? 'selected' : ''}>Re-plan every interval</option><option value="plan" ${ST.sel.mode === 'plan' ? 'selected' : ''}>Day-ahead plan only</option></select></label>
      <label class="note">What-if <select id="lv_inj" ${dis(a)}><option value="none" ${L.inject === 'none' ? 'selected' : ''}>No disturbance (as recorded)</option><option value="cloud" ${L.inject === 'cloud' ? 'selected' : ''}>Cloud front 12:00-15:00 (solar x0.4)</option><option value="surge" ${L.inject === 'surge' ? 'selected' : ''}>Demand +25% 15:00-19:00</option><option value="both" ${L.inject === 'both' ? 'selected' : ''}>Both</option></select></label>
    </div>
    <div class="bar" id="lv_btns"></div>
    <div class="bar"><label class="note"><input type="checkbox" id="lv_req" ${L.require ? 'checked' : ''} ${dis(a)}> Require approver sign-off for plan changes (pauses the replay)</label></div>
    <div id="lv_inj_note"></div></div>
  <div id="lv_kpi" class="kpis"></div>
  <div class="live"><div>
    <div class="card"><h2>Grid import, demand and solar</h2><div class="legend" id="lv_leg1"></div><div id="lv_c1"></div></div>
    <div class="grid g2"><div class="card"><h2>Battery charge</h2><div id="lv_c2"></div></div><div class="card"><h2>EV charging: as requested vs planned</h2><div id="lv_c3"></div></div></div>
  </div><div>
    <div class="card" id="lv_pend_card"><h2>Approval</h2><div id="lv_pend"></div></div>
    <div class="card"><h2>Deviations and alerts</h2><div class="feed" id="lv_feed"></div></div>
    <div class="card"><h2>What the planner is doing</h2><div id="lv_why" class="note"></div></div>
  </div></div>${caveat()}`;
}
function renderLiveButtons() {
  const el = $q('#lv_btns'); if (!el || !ST.live) return; const L = ST.live, a = can('run'), spd = ST.ctx.spd;
  el.innerHTML = `<button class="pri" data-act="lv_play" ${dis(a && !L.playing && !L.done && !L.pending)}>&#9654; Play</button><button data-act="lv_pause" ${dis(a && L.playing)}>&#10073;&#10073; Pause</button><button data-act="lv_step" ${dis(a && !L.playing && !L.done && !L.pending)}>Step</button><button data-act="lv_restart" ${dis(a)}>Restart</button>
    <label class="note">Speed <select id="lv_speed" ${dis(a)}><option value="1500" ${L.speed === 1500 ? 'selected' : ''}>Slow</option><option value="800" ${L.speed === 800 ? 'selected' : ''}>Normal</option><option value="250" ${L.speed === 250 ? 'selected' : ''}>Fast</option><option value="0" ${L.speed === 0 ? 'selected' : ''}>Instant</option></select></label>
    <input type="range" id="lv_seek" min="0" max="${spd}" value="${L.idx}" ${dis(a)} aria-label="Scrub through the day"><span class="note" style="margin:0">${tmS(Math.min(L.idx, spd))} of 24:00</span>`;
}
function mountLive() { renderLiveButtons(); drawLive(); }
function drawLive() {
  factorySend();
  if (!ST.open || ST.tab !== 'live' || !ST.live) return;
  const L = ST.live, X = L.X, ctx = ST.ctx, spd = ctx.spd, dt = ctx.dt, idx = L.idx, D = ST.D, T = ctx.tariff, wd = X.wd;
  const kpi = $q('#lv_kpi'); if (!kpi) return;
  // running numbers from what has been "read" so far
  let costX = 0, costB = 0, pkX = 0, pkB = 0, kwhX = 0, kwhB = 0;
  for (let s = 0; s < idx; s++) {
    const pk = wd && s >= ctx.w0 && s < ctx.w1, r = (pk ? T.ep : T.eo) + T.afa;
    costX += X.out.grid[s] * dt * r / 100; costB += L.B.out.grid[s] * dt * r / 100; kwhX += X.out.grid[s] * dt; kwhB += L.B.out.grid[s] * dt;
    if (pk) { pkX = Math.max(pkX, X.out.grid[s]); pkB = Math.max(pkB, L.B.out.grid[s]); }
  }
  const twinBad = L.dev.twin.on, devBad = L.dev.load.on || L.dev.pv.on;
  const finite = Number.isFinite(X.capApplied);
  kpi.innerHTML = `
    <div class="kpi"><b>${idx >= spd ? '24:00' : tmS(idx)}</b><span>Time now &middot; ${esc(dayLabel(L.d))}</span></div>
    <div class="kpi"><b>${fmt0(X.soc * 100)}%</b><span>Battery charge (${fmt0(X.soc * ctx.bat.kwh)} kWh)</span></div>
    <div class="kpi"><b>${finite ? fmt0(X.capApplied) + ' kW' : (wd ? 'none' : 'n/a')}</b><span>${wd ? 'Cap on grid import in peak window' : 'Weekend: no peak charge'}</span></div>
    <div class="kpi ${pkB - pkX > 1 ? 'hl' : ''}"><b>${wd && idx > ctx.w0 ? fmt0(pkX) + ' kW' : '-'}</b><span>Peak so far vs ${wd && idx > ctx.w0 ? fmt0(pkB) + ' kW baseline' : 'baseline (window not started)'}</span></div>
    <div class="kpi ${costB - costX > 0 ? 'hl' : ''}"><b>${fmtRM(costB - costX)}</b><span>Energy cost saved so far (${fmt0(kwhB - kwhX)} kWh less from grid)</span></div>
    <div class="kpi"><b>${devBad ? 'Drifting' : 'On forecast'}</b><span>Solar x${(X.ratioP || 1).toFixed(2)}, demand x${(X.ratioL || 1).toFixed(2)} of forecast (last 2 h)</span></div>
    <div class="kpi ${twinBad ? '' : ''}" style="${twinBad ? 'background:#FEF3F2;border-color:#F3B4B4' : ''}"><b>${twinBad ? 'Out of tolerance' : 'Within tolerance'}</b><span>Digital twin vs measured grid import</span></div>`;
  // chart 1
  const demand = new Float64Array(spd).fill(NaN), sol = new Float64Array(spd).fill(NaN), capv = new Float64Array(spd).fill(NaN);
  for (let s = 0; s < idx; s++) { demand[s] = X.L[s] + X.out.ev[s]; sol[s] = X.P[s]; if (X.out.capv[s] > 0 && X.inWin(s)) capv[s] = X.out.capv[s]; }
  const fdem = new Float64Array(spd), fsol = new Float64Array(spd); for (let s = 0; s < spd; s++) { fdem[s] = X.f0.L[s]; fsol[s] = X.f0.P[s]; }
  const vis = ST.sel.l1 || (ST.sel.l1 = { grid: true, base: true, twin: true, dem: true, sol: true, cap: true, fc: false });
  const sty = STRATS[X.strat.key];
  const ser = [
    { key: 'grid', name: 'Grid import (' + sty.short + ')', color: sty.color, data: X.out.grid, upTo: idx, w: 3, hidden: !vis.grid },
    { key: 'base', name: 'Grid import, baseline', color: '#6b7280', data: L.B.out.grid, upTo: idx, w: 2, hidden: !vis.base },
    { key: 'twin', name: 'Digital twin expected', color: '#2563eb', data: L.T.out.grid, dash: '6 4', w: 2, hidden: !vis.twin },
    { key: 'dem', name: 'Site demand', color: '#252B2A', data: demand, w: 1.5, hidden: !vis.dem },
    { key: 'sol', name: 'Solar', color: '#F59E0B', data: sol, w: 1.5, hidden: !vis.sol },
    { key: 'cap', name: 'Cap', color: '#B42318', data: capv, dash: '5 4', w: 1.5, hidden: !vis.cap },
    { key: 'fcd', name: 'Forecast demand', color: '#9ca3af', data: fdem, dash: '2 3', w: 1.5, hidden: !vis.fc },
    { key: 'fcs', name: 'Forecast solar', color: '#fcd34d', data: fsol, dash: '2 3', w: 1.5, hidden: !vis.fc }
  ];
  $q('#lv_leg1').outerHTML = `<div class="legend" id="lv_leg1">${[['grid', 'Grid import', sty.color], ['base', 'Baseline', '#6b7280'], ['twin', 'Twin expected', '#2563eb'], ['dem', 'Demand', '#252B2A'], ['sol', 'Solar', '#F59E0B'], ['cap', 'Cap', '#B42318'], ['fc', 'Forecasts', '#9ca3af']].map(x => `<label data-act="leg1" data-v="${x[0]}" class="${vis[x[0]] === false ? 'off' : ''}"><i style="background:${x[2]}"></i>${x[1]}</label>`).join('')}</div>`;
  const markers = L.alerts.filter(a => a.level === 'hi' || a.level === 'warn').map(a => ({ i: Math.max(0, a.slot), color: a.level === 'hi' ? '#B42318' : '#F59E0B', title: tmS(Math.max(0, a.slot)) + ' ' + a.title }));
  mount($q('#lv_c1'), line({ n: spd, h: 310, series: ser, yLabel: 'kW', bands: wd ? [{ a: ctx.w0, b: ctx.w1, label: 'Peak window: maximum demand is billed here' }] : [], xTicks: xTicksDay(), vlines: idx > 0 && idx < spd ? [{ i: idx - 1, label: 'now' }] : [], markers, tipTitle: i => tmS(i) + ' - ' + tmS(i + 1), aria: 'Grid import, demand and solar through the day' }));
  // chart 2
  const soc = new Float64Array(spd).fill(NaN); for (let s = 0; s < idx; s++) soc[s] = X.out.soc[s] * 100;
  mount($q('#lv_c2'), line({ n: spd, h: 200, yMax: 100, yMin: 0, series: [{ name: 'Battery charge %', color: '#16803D', data: soc, fill: true, w: 2.5, fmt: v => fmt0(v) + '%' }], hlines: X.socMin > 0 ? [{ v: X.socMin * 100, label: 'reserve ' + fmt0(X.socMin * 100) + '%', color: '#92400E' }] : [], xTicks: xTicksDay(), yFmt: v => fmt0(v) + '%', bands: wd ? [{ a: ctx.w0, b: ctx.w1 }] : [], tipTitle: i => tmS(i + 1) }));
  // chart 3
  const evo = Array.from(X.E0), evs = new Float64Array(spd).fill(NaN), evp = new Float64Array(spd).fill(NaN);
  for (let s = 0; s < idx; s++) evs[s] = X.out.ev[s]; for (let s = idx; s < spd; s++) evp[s] = X.evPlan[s];
  mount($q('#lv_c3'), line({ n: spd, h: 200, series: [{ name: 'As requested', color: '#9ca3af', data: evo, dash: '4 3', w: 1.5 }, { name: 'Delivered', color: '#b24fc9', data: evs, w: 2.5, fill: true }, { name: 'Planned ahead', color: '#b24fc9', data: evp, dash: '2 3', w: 2 }], xTicks: xTicksDay(), yLabel: 'kW', bands: wd ? [{ a: ctx.w0, b: ctx.w1 }] : [], tipTitle: i => tmS(i) }));
  // feed
  const feed = $q('#lv_feed');
  feed.innerHTML = L.alerts.length ? L.alerts.slice().reverse().map(a => `<div class="alert ${a.level}"><b>${esc(tmS(Math.max(0, a.slot)))} ${esc(a.title)}</b><small>${esc(a.text)}</small></div>`).join('') : `<div class="note">Nothing yet. Alerts appear when demand or solar drift from the forecast for two readings in a row, or measured grid import leaves the twin's expected path.</div>`;
  // approval box
  const pend = $q('#lv_pend');
  if (L.pending) {
    const p = L.pending.p;
    pend.innerHTML = `<div class="warn"><b>${esc(p.type)} waiting for approval</b> at ${tmS(p.slot)}.<br>${p.from == null ? '' : 'Cap ' + fmt0(p.from) + ' &rarr; '}<b>${fmt0(p.to)} kW</b>. ${esc(p.reason)}</div>` +
      (can('approve') ? `<div class="bar"><button class="good" data-act="appr_yes">Approve</button><button class="badb" data-act="appr_no">Reject</button></div>` : `<div class="note">Waiting for an Approver (or the Owner) to decide.</div>`);
  } else pend.innerHTML = `<div class="note">${L.require ? 'Sign-off is required. The next plan change will pause here until an approver decides.' : 'Plan changes are auto-approved in demo mode and logged. Tick "Require approver sign-off" to try the approval flow.'}</div>`;
  // why
  const why = $q('#lv_why');
  why.innerHTML = L.pending ? 'Waiting for an approver. Nothing is applied until the plan is approved.' : X.strat.bat ? (wd && idx < ctx.w1 ? `Holding grid import under <b>${Number.isFinite(X.capApplied) ? fmt0(X.capApplied) : '-'} kW</b> between 14:00 and 22:00, keeping about <b>${fmt0(X.Sreq * 100)}%</b> charge ready for it${X.strat.topup ? ', topping up from the grid just before the window only if solar has not filled the battery' : ''}. The cap is the lowest level the battery can hold given the corrected forecast, and never below this month's peak so far (${fmt0(X.Mrun)} kW), because shaving below it saves nothing.` : (wd ? 'The peak window has passed. The battery now keeps what tomorrow\'s window needs and serves load above that.' : 'No maximum demand is billed today. The battery keeps what the next weekday needs.')) : '';
  const sl = $q('#lv_seek'); if (sl) { sl.value = idx; if (sl.nextElementSibling) sl.nextElementSibling.textContent = tmS(Math.min(idx, spd)) + ' of 24:00'; }
  const inj = $q('#lv_inj_note'); if (inj) inj.innerHTML = L.inject !== 'none' ? `<div class="warn"><b>SIMULATED DISTURBANCE.</b> The "recorded" solar or demand for this day has been altered to test the response. It is not real data.</div>` : '';
  if (L.done) { const gk = windowPeak(X.out.grid, X), bk = windowPeak(L.B.out.grid, L.B); feed.insertAdjacentHTML('afterbegin', `<div class="alert ok"><b>Day complete</b><small>Billed-window peak ${fmt0(gk)} kW vs ${fmt0(bk)} kW baseline. Energy cost saved ${fmtRM(costB - costX)}. Each kW of peak removed is worth RM ${T.md}/month if this day sets the month's peak.</small></div>`); }
}

/* ======================= SCENARIOS ======================= */
const dayStats = (k, d) => {
  const ctx = ST.ctx, R = Rof(k), spd = ctx.spd, o = d * spd, dt = ctx.dt; let kwh = 0, curt = 0, thr = 0, pk = 0;
  const wd = isWeekday(ST.D.days[d].dow);
  for (let s = 0; s < spd; s++) { kwh += R.grid[o + s] * dt; curt += R.curt[o + s] * dt; thr += Math.abs(R.batt[o + s]) * dt; if (wd && s >= ctx.w0 && s < ctx.w1) pk = Math.max(pk, R.grid[o + s]); }
  return { rm: dayEnergyRM(ctx, R, d), pk, kwh, curt, thr, co2: kwh * ctx.ef };
};
function bodyScen() {
  const v = ST.sel.scView, ms = monthsOf(ST.D);
  const items = STRAT_ORDER.map(k => ({ key: k, name: STRATS[k].short, color: STRATS[k].color }));
  return `<div class="card soft"><div class="bar"><b>All strategies on one chart.</b>
    <span class="seg" role="group"><button data-act="scv" data-v="day" class="${v === 'day' ? 'on' : ''}">One day</button><button data-act="scv" data-v="month" class="${v === 'month' ? 'on' : ''}">One month</button><button data-act="scv" data-v="year" class="${v === 'year' ? 'on' : ''}">Whole year</button></span>
    ${v === 'day' ? `<label class="note">Day <input type="date" id="sc_date" value="${ST.D.days[ST.sel.day].date}" min="${ST.D.days[0].date}" max="${ST.D.days[ST.D.nD - 1].date}"></label>` : ''}
    ${v === 'month' ? `<label class="note">Month <select id="sc_month">${monthOpts(ST.sel.month)}</select></label>` : ''}
    <label class="note">Optimiser <select id="sc_mode"><option value="live" ${ST.sel.mode === 'live' ? 'selected' : ''}>Re-plan every interval</option><option value="plan" ${ST.sel.mode === 'plan' ? 'selected' : ''}>Day-ahead plan only</option></select></label></div>
    ${legendFor(items, ST.sel.vis)}</div>
  <div class="card"><h2 id="sc_t"></h2><div id="sc_c"></div><div id="sc_c2" style="margin-top:8px"></div></div>
  <div class="card"><div class="tw" id="sc_tbl"></div></div>${caveat()}`;
}
function mountScen() {
  const v = ST.sel.scView, ctx = ST.ctx, D = ST.D, spd = ctx.spd, vis = ST.sel.vis, keys = STRAT_ORDER.filter(k => vis[k] !== false);
  const base = Rof('base'), bKpi = {};
  const head = '<tr><th>Strategy</th>';
  if (v === 'day') {
    const d = ST.sel.day, o = d * spd, wd = isWeekday(D.days[d].dow);
    $q('#sc_t').textContent = 'Grid import on ' + dayLabel(d) + ' (kW)';
    const ser = keys.map(k => ({ name: STRATS[k].short, color: STRATS[k].color, data: Rof(k).grid.subarray(o, o + spd), w: k === 'base' ? 2 : 2.5, dash: k === 'base' ? '5 4' : null }));
    const demand = new Float64Array(spd); for (let s = 0; s < spd; s++) demand[s] = D.load[o + s] + base.ev[o + s];
    ser.unshift({ name: 'Site demand (before solar)', color: '#d4d4d8', data: demand, fill: true, w: 1 });
    // tidy: demand shown as light fill, solar as thin amber
    const sol = D.pv.subarray(o, o + spd); ser.push({ name: 'Solar', color: '#F59E0B', data: sol, w: 1, dash: '2 3' });
    mount($q('#sc_c'), line({ n: spd, h: 320, series: ser, yLabel: 'kW', xTicks: xTicksDay(), bands: wd ? [{ a: ctx.w0, b: ctx.w1, label: 'Peak window' }] : [], tipTitle: i => tmS(i) + ' - ' + tmS(i + 1), aria: 'Grid import for every strategy on one day' }));
    const soc = keys.filter(k => STRATS[k].bat).map(k => ({ name: STRATS[k].short + ' charge', color: STRATS[k].color, data: Float64Array.from(Rof(k).soc.subarray(o, o + spd), x => x * 100), w: 2, fmt: x => fmt0(x) + '%' }));
    mount($q('#sc_c2'), line({ n: spd, h: 170, yMax: 100, yMin: 0, series: soc, xTicks: xTicksDay(), yFmt: x => fmt0(x) + '%', yLabel: 'battery', bands: wd ? [{ a: ctx.w0, b: ctx.w1 }] : [], tipTitle: i => tmS(i + 1) }));
    const b0 = dayStats('base', d);
    $q('#sc_tbl').innerHTML = `<table>${head}<th>Energy cost</th><th>vs baseline</th><th>Peak in billed window (kW)</th><th>From grid (kWh)</th><th>Solar lost (kWh)</th><th>CO2 (kg)</th><th>Battery throughput (kWh)</th></tr>` +
      keys.map(k => { const s = dayStats(k, d); return `<tr><td><i style="display:inline-block;width:10px;height:10px;background:${STRATS[k].color};border-radius:2px"></i> ${esc(STRATS[k].short)}</td><td>${fmtRM(s.rm)}</td><td>${k === 'base' ? '-' : sgn(s.rm - b0.rm, fmtRM)}</td><td>${wd ? fmt0(s.pk) : 'n/a'}</td><td>${fmt0(s.kwh)}</td><td>${fmt0(s.curt)}</td><td>${fmt0(s.co2)}</td><td>${fmt0(s.thr)}</td></tr>`; }).join('') + `</table><div class="note">Energy cost is for this day only. Maximum demand is billed once a month on the highest peak, see the Month and Whole year views.</div>`;
  } else if (v === 'month') {
    const m = monthsOf(D)[ST.sel.month], n = m.d1 - m.d0 + 1;
    $q('#sc_t').textContent = 'Highest import in the billed window, each weekday of ' + MONTHS[m.month] + ' (kW)';
    const ser = keys.map(k => { const a = dailyMd(ctx, Rof(k)), data = new Float64Array(n).fill(NaN); for (let i = 0; i < n; i++) if (isWeekday(D.days[m.d0 + i].dow)) data[i] = a[m.d0 + i]; return { name: STRATS[k].short, color: STRATS[k].color, data, w: k === 'base' ? 2 : 2.5, dash: k === 'base' ? '5 4' : null }; });
    const ticks = []; for (let i = 0; i < n; i += 3) ticks.push({ i, label: String(i + 1) });
    // billed peak lines: the highest of each scenario
    const hl = keys.filter(k => k !== 'base').slice(0, 0);
    mount($q('#sc_c'), line({ n, h: 320, series: ser, yLabel: 'kW', xTicks: ticks, tipTitle: i => DOWS[D.days[m.d0 + i].dow] + ' ' + D.days[m.d0 + i].date, aria: 'Daily billed-window peak by strategy' }));
    $q('#sc_c2').innerHTML = '<div class="note">The month is billed on the highest point of each line. Optimised strategies aim to lower the top of the line without letting any later day pop above it.</div>';
    const b0 = periodKpi(ctx, base, m.d0, m.d1);
    $q('#sc_tbl').innerHTML = `<table>${head}<th>Billed peak (kW)</th><th>Peak charge</th><th>Energy charge</th><th>Retail</th><th>Month total</th><th>vs baseline</th></tr>` +
      keys.map(k => { const s = periodKpi(ctx, Rof(k), m.d0, m.d1); return `<tr><td><i style="display:inline-block;width:10px;height:10px;background:${STRATS[k].color};border-radius:2px"></i> ${esc(STRATS[k].short)}</td><td>${fmt0(s.md)}</td><td>${fmtRM(s.mdRM)}</td><td>${fmtRM(s.energyRM)}</td><td>${fmtRM(s.retailRM)}</td><td><b>${fmtRM(s.totalRM)}</b></td><td>${k === 'base' ? '-' : sgn(s.totalRM - b0.totalRM, fmtRM) + ' (' + sgn((s.totalRM - b0.totalRM) / b0.totalRM * 100, x => x.toFixed(1)) + '%)'}</td></tr>`; }).join('') + `</table>`;
  } else {
    const ms = monthsOf(D);
    $q('#sc_t').textContent = 'Estimated monthly bill by strategy (RM, before KWTBB levy and tax)';
    const ser = keys.map(k => ({ name: STRATS[k].short, color: STRATS[k].color, data: ms.map(m => periodKpi(ctx, Rof(k), m.d0, m.d1).totalRM) }));
    $q('#sc_c').innerHTML = bars({ labels: ms.map(m => MONTHS[m.month]), series: ser, h: 320, yFmt: x => 'RM ' + (x / 1000).toFixed(0) + 'k', aria: 'Monthly bill by strategy' });
    $q('#sc_c2').innerHTML = '';
    const tot = ser.map(s => sum(s.data)), bi = keys.indexOf('base'), bt = bi >= 0 ? tot[bi] : sum(ms.map(m => periodKpi(ctx, base, m.d0, m.d1).totalRM));
    $q('#sc_tbl').innerHTML = `<table>${head}<th>Peak charges, 12 months</th><th>Energy charges</th><th>Year total</th><th>vs baseline</th></tr>` +
      keys.map((k, i) => { let md = 0, en = 0; ms.forEach(m => { const s = periodKpi(ctx, Rof(k), m.d0, m.d1); md += s.mdRM; en += s.energyRM; }); return `<tr><td><i style="display:inline-block;width:10px;height:10px;background:${STRATS[k].color};border-radius:2px"></i> ${esc(STRATS[k].short)}</td><td>${fmtRM(md)}</td><td>${fmtRM(en)}</td><td><b>${fmtRM(tot[i])}</b></td><td>${k === 'base' ? '-' : sgn(tot[i] - bt, fmtRM) + ' (' + sgn((tot[i] - bt) / bt * 100, x => x.toFixed(1)) + '%)'}</td></tr>`; }).join('') + `</table><div class="note">${ms[0].d1 - ms[0].d0 < 30 ? '' : ''}The first week of January has no history to forecast from, so optimised plans are weaker there.</div>`;
  }
}

/* ======================= MONTHLY BILL ======================= */
function bodyBill() {
  const ms = monthsOf(ST.D), m = ms[ST.sel.month], key = m.key;
  return `<div class="card soft"><div class="bar"><b>Monthly summary in the shape of a TNB bill.</b>
    <label class="note">Month <select id="bl_month">${monthOpts(ST.sel.month)}</select></label>
    <label class="note">Strategy <select id="bl_strat">${stratOpts(ST.sel.strat, ['self', 'tou', 'cost', 'bal', 'zero'])}</select></label>
    <label class="note">Optimiser <select id="bl_mode"><option value="live" ${ST.sel.mode === 'live' ? 'selected' : ''}>Re-plan every interval</option><option value="plan" ${ST.sel.mode === 'plan' ? 'selected' : ''}>Day-ahead plan only</option></select></label></div></div>
  <div><div class="card"><h2>${MONTHS[m.month]} ${key.slice(0, 4)} estimate</h2><div class="tw" id="bl_tbl"></div>
    <h3>Compare with your real TNB bill</h3><div class="bar"><label class="note">Total on the bill (RM) <input type="number" id="bl_real" min="0" step="1" value="${ST.bills[key] != null ? ST.bills[key] : ''}" placeholder="optional" style="width:150px"></label></div><div id="bl_var" class="note"></div></div>
  <div class="card"><h2>Billed-window peak each weekday</h2><div id="bl_c1"></div></div></div>
  <div class="card"><h2>Estimated bill every month</h2><div id="bl_c2"></div></div>${caveat()}`;
}
function mountBill() {
  const ctx = ST.ctx, D = ST.D, ms = monthsOf(D), m = ms[ST.sel.month], k = ST.sel.strat, T = ctx.tariff;
  const a = periodKpi(ctx, Rof('base'), m.d0, m.d1), b = periodKpi(ctx, Rof(k), m.d0, m.d1);
  const row = (n, q, rate, x, y) => `<tr><td>${n}</td><td>${q}</td><td>${rate}</td><td>${fmtRM(x)}</td><td>${fmtRM(y)}</td></tr>`;
  $q('#bl_tbl').innerHTML = `<table><tr><th>Item</th><th>Quantity (selected)</th><th>Rate</th><th>Baseline</th><th>${esc(STRATS[k].short)}</th></tr>` +
    row('Peak-hours energy (weekdays 14:00-22:00)', fmt0(b.kwhPeak) + ' kWh', (T.ep + T.afa).toFixed(2) + ' sen', a.kwhPeak * (T.ep + T.afa) / 100, b.kwhPeak * (T.ep + T.afa) / 100) +
    row('Off-peak energy', fmt0(b.kwhOff) + ' kWh', (T.eo + T.afa).toFixed(2) + ' sen', a.kwhOff * (T.eo + T.afa) / 100, b.kwhOff * (T.eo + T.afa) / 100) +
    row('Maximum demand', fmt0(b.md) + ' kW (baseline ' + fmt0(a.md) + ')', 'RM ' + T.md.toFixed(2) + '/kW', a.mdRM, b.mdRM) +
    row('Retail charge', '', 'RM ' + T.retail, a.retailRM, b.retailRM) +
    `<tr><td><b>Estimated total</b></td><td></td><td></td><td><b>${fmtRM(a.totalRM)}</b></td><td><b>${fmtRM(b.totalRM)}</b></td></tr><tr><td>Difference</td><td></td><td></td><td></td><td><b>${sgn(b.totalRM - a.totalRM, fmtRM)} (${sgn((b.totalRM - a.totalRM) / a.totalRM * 100, x => x.toFixed(1))}%)</b></td></tr></table><div class="note">Not included: KWTBB levy (1.6%), taxes, power-factor charges, AFA changes. Billed peak demand is the highest 30-minute average in the window, as TNB bills it.</div>`;
  const upd = () => { const v = ST.bills[m.key]; const el = $q('#bl_var'); if (!el) return; el.innerHTML = Number.isFinite(v) ? `Model baseline ${fmtRM(a.totalRM)} vs your bill ${fmtRM(v)}: ${sgn(a.totalRM - v, fmtRM)} (${sgn((a.totalRM - v) / v * 100, x => x.toFixed(1))}%). Differences of a few percent are normal because of the items not modelled. A large gap means the data or the tariff does not match the site.` : 'Enter a bill to see how close the model baseline is. It is saved in this browser only.'; }; upd();
  const inp = $q('#bl_real'); if (inp) inp.oninput = () => { const x = parseFloat(inp.value); if (Number.isFinite(x)) ST.bills[m.key] = x; else delete ST.bills[m.key]; LS.set('sunops_bills', ST.bills); upd(); };
  const n = m.d1 - m.d0 + 1, mk = r => { const q = dailyMd(ctx, r), d = new Float64Array(n).fill(NaN); for (let i = 0; i < n; i++) if (isWeekday(D.days[m.d0 + i].dow)) d[i] = q[m.d0 + i]; return d; };
  const ticks = []; for (let i = 0; i < n; i += 3) ticks.push({ i, label: String(i + 1) });
  mount($q('#bl_c1'), line({ n, h: 250, series: [{ name: 'Baseline', color: '#6b7280', data: mk(Rof('base')), dash: '5 4', w: 2 }, { name: STRATS[k].short, color: STRATS[k].color, data: mk(Rof(k)), w: 2.5 }], hlines: [{ v: a.md, label: 'baseline billed peak', color: '#6b7280' }, { v: b.md, label: 'billed peak', color: STRATS[k].color, dash: '2 3' }], xTicks: ticks, yLabel: 'kW', tipTitle: i => D.days[m.d0 + i].date }));
  $q('#bl_c2').innerHTML = bars({ labels: ms.map(x => MONTHS[x.month]), series: ['base', 'cost', 'bal', 'zero'].map(q => ({ name: STRATS[q].short, color: q === 'base' ? '#9ca3af' : STRATS[q].color, data: ms.map(x => periodKpi(ctx, Rof(q), x.d0, x.d1).totalRM) })), h: 260, yFmt: x => 'RM ' + (x / 1000).toFixed(0) + 'k' }) + `<div class="legend">${['base', 'cost', 'bal', 'zero'].map(q => `<label><i style="background:${q === 'base' ? '#9ca3af' : STRATS[q].color}"></i>${esc(STRATS[q].short)}</label>`).join('')}</div>`;
}

/* ======================= TWIN CHECK ======================= */
function twinAll(k) {
  const key = k + '|' + ST.cfg.margin; if (ST.twinCache[key]) return ST.twinCache[key];
  const R = ST.R[k + '_plan'], out = []; for (let d = 7; d < ST.D.nD; d++) if (isWeekday(ST.D.days[d].dow)) out.push(twinDay(ST.ctx, R, d));
  return ST.twinCache[key] = out;
}
function bodyTwin() {
  const k = ['cost', 'bal', 'zero'].includes(ST.sel.strat) ? ST.sel.strat : 'cost', all = twinAll(k), ok = all.filter(t => Math.abs(t.peakErr) <= 0.10).length, ok25 = all.filter(t => Math.abs(t.peakErr) <= 0.25).length;
  const errs = all.map(t => Math.abs(t.peakErr)).sort((a, b) => a - b), med = errs[errs.length >> 1] || 0, mape = sum(all.map(t => t.mape)) / (all.length || 1);
  return `<div class="card soft"><div class="bar"><b>Does the digital twin match what happened?</b><label class="note">Strategy <select id="tw_strat">${stratOpts(k, ['cost', 'bal', 'zero'])}</select></label><label class="note">Month <select id="tw_month">${monthOpts(ST.sel.month)}</select></label></div>
    <div class="note">The twin is the same planner run on the day-ahead forecast. It says what grid import and billed peak to expect. The comparison is against the day as it was actually executed. Gaps measure forecast error. Real battery and EV meter data would be needed to validate the equipment model itself.</div></div>
  <div class="kpis"><div class="kpi hl"><b>${fmt0(ok / (all.length || 1) * 100)}%</b><span>Weekdays where the billed peak was within 10% of the twin</span></div><div class="kpi"><b>${fmt0(ok25 / (all.length || 1) * 100)}%</b><span>Weekdays within 25%</span></div><div class="kpi"><b>${fmtPct(med)}</b><span>Median billed-peak error</span></div><div class="kpi"><b>${fmtPct(mape)}</b><span>Average error in half-hourly grid import</span></div><div class="kpi"><b>${all.length}</b><span>Weekdays checked (first week is warm-up)</span></div></div>
  <div class="grid g2"><div class="card"><h2>Billed-window peak: twin vs measured</h2><div id="tw_c"></div></div><div class="card"><h2>Forecast quality this month</h2><div id="tw_f" class="note"></div></div></div>
  <div class="card"><h2>Day by day</h2><div class="tw" id="tw_t"></div></div>${caveat()}`;
}
function mountTwin() {
  const k = ['cost', 'bal', 'zero'].includes(ST.sel.strat) ? ST.sel.strat : 'cost', m = monthsOf(ST.D)[ST.sel.month], all = twinAll(k).filter(t => t.d >= m.d0 && t.d <= m.d1), n = m.d1 - m.d0 + 1;
  const pa = new Float64Array(n).fill(NaN), aa = new Float64Array(n).fill(NaN); all.forEach(t => { pa[t.d - m.d0] = t.predPeak; aa[t.d - m.d0] = t.actPeak; });
  const ticks = []; for (let i = 0; i < n; i += 3) ticks.push({ i, label: String(i + 1) });
  mount($q('#tw_c'), line({ n, h: 250, series: [{ name: 'Twin expected', color: '#2563eb', data: pa, dash: '5 4', w: 2 }, { name: 'Measured', color: STRATS[k].color, data: aa, w: 2.5 }], xTicks: ticks, yLabel: 'kW', tipTitle: i => ST.D.days[m.d0 + i].date }));
  const ctx = ST.ctx, spd = ctx.spd; let eL = 0, aL = 0, eP = 0, aP = 0;
  for (let d = m.d0; d <= m.d1; d++) { const f = forecastDay(ctx, d); for (let s = Math.round(8 / ctx.dt); s < Math.round(17 / ctx.dt); s++) { const i = d * spd + s; eL += Math.abs(ST.D.load[i] - f.L[s]); aL += ST.D.load[i]; eP += Math.abs(ST.D.pv[i] - f.P[s]); aP += ST.D.pv[i]; } }
  $q('#tw_f').innerHTML = `Daytime (08:00-17:00) day-ahead forecast error this month: demand <b>${fmtPct(eL / (aL || 1))}</b>, solar <b>${fmtPct(eP / (aP || 1))}</b>. Day-to-day swings in this data are large (a weekday can use anywhere from about 10 to 22 MWh), which a forecast built from past days cannot see. That is why the plan is re-checked every interval, and why a real deployment should feed in the production schedule and a weather forecast.`;
  $q('#tw_t').innerHTML = `<table><tr><th>Date</th><th>Twin peak (kW)</th><th>Measured peak (kW)</th><th>Peak error</th><th>Twin energy (kWh)</th><th>Measured (kWh)</th><th>Energy error</th><th>Half-hour error</th><th>Status</th></tr>` +
    all.map(t => { const bad = Math.abs(t.peakErr) > 0.10; return `<tr><td>${DOWS[ST.D.days[t.d].dow]} ${ST.D.days[t.d].date}</td><td>${fmt0(t.predPeak)}</td><td>${fmt0(t.actPeak)}</td><td>${sgn(t.peakErr * 100, x => x.toFixed(1))}%</td><td>${fmt0(t.predKwh)}</td><td>${fmt0(t.actKwh)}</td><td>${sgn(t.kwhErr * 100, x => x.toFixed(1))}%</td><td>${fmtPct(t.mape)}</td><td><span class="pill ${bad ? 'warn' : 'ok'}">${bad ? 'Check' : 'OK'}</span></td></tr>`; }).join('') + '</table>';
}

/* ======================= DATA HEALTH ======================= */
function bodyData() {
  const H = ST.raw.health.slice(), nh = ST.raw.netHint, mode = demandModeNow();
  const rows = H.map(h => `<tr><td style="white-space:normal"><b>${esc(h.name)}</b></td><td><span class="pill ${h.status}">${h.status === 'ok' ? 'OK' : h.status === 'fixed' ? 'Fixed' : 'Check'}</span></td><td style="white-space:normal;text-align:left">${esc(h.result)}</td><td style="white-space:normal;text-align:left">${esc(h.handling)}</td></tr>`).join('');
  const netBox = nh.suggested ? `<div class="warn"><b>The demand column looks like grid draw after solar.</b> Demand and solar move in opposite directions (correlation ${nh.corr.toFixed(2)}), and in ${fmt0(nh.lowSunny * 100)}% of the sunniest half-hours the reading is under 20% of its night-time level (${fmt0(nh.sunnyMedian)} kW at midday against ${fmt0(nh.nightMedian)} kW at night). A factory's own demand does not fall at noon on a sunny day, but a meter behind solar panels does.<br>Current setting: <b>${mode === 'net' ? 'solar added back to get site demand (baseline = measured grid draw)' : 'column read as demand before solar'}</b>. Change it in Settings and confirm with the data provider.</div>` : '';
  return `<div class="card soft"><b>Data health.</b> Every file passes these checks before any planning. Anything fixed is listed with what was done to it.</div>${netBox}
  <div class="card"><div class="tw"><table><tr><th>Check</th><th>Result</th><th style="text-align:left">What we found</th><th style="text-align:left">What we did</th></tr>${rows}</table></div></div>
  <div class="card"><h2>Repaired meter dropout</h2><div id="dh_c"></div><div class="note">Shaded intervals read exactly zero and were replaced with the typical value for that time and day type. The repaired values are used in the plan, so a gap is not mistaken for a free day.</div></div>`;
}
function mountData() {
  const raw = ST.raw, spd = raw.spd, fl = raw.flags, el = $q('#dh_c'); if (!el) return;
  let first = -1; for (let i = 0; i < raw.n; i++) if (fl[i]) { first = Math.floor(i / spd); break; }
  if (first < 0) { el.innerHTML = '<div class="note">No dropouts were found in this file.</div>'; return; }
  const d0 = Math.max(0, first - 1), d1 = Math.min(raw.nD - 1, first + 4), n = (d1 - d0 + 1) * spd;
  const orig = new Float64Array(n), rep = new Float64Array(n), bands = [];
  for (let i = 0; i < n; i++) { const j = d0 * spd + i; rep[i] = raw.load[j]; orig[i] = fl[j] ? 0 : raw.load[j]; }
  for (let i = 0; i < n;) { if (fl[d0 * spd + i]) { let j = i; while (j < n && fl[d0 * spd + j]) j++; bands.push({ a: i, b: j, fill: '#B42318', op: .12 }); i = j; } else i++; }
  const ticks = []; for (let d = 0; d <= d1 - d0; d++) ticks.push({ i: d * spd, label: raw.days[d0 + d].date.slice(5) });
  mount(el, line({ n, h: 240, series: [{ name: 'As recorded', color: '#B42318', data: orig, w: 1.5 }, { name: 'After repair', color: '#16803D', data: rep, w: 2 }], bands, xTicks: ticks, yLabel: 'kW', tipTitle: i => raw.days[d0 + Math.floor(i / spd)].date + ' ' + p2(Math.floor((i % spd) * raw.dt)) + ':' + p2(Math.round(((i % spd) * raw.dt % 1) * 60)) }));
}

/* ======================= APPROVALS & AUDIT ======================= */
function bodyAppr() {
  const v = verifyAudit();
  const matrix = `<table><tr><th>Action</th>${Object.keys(ROLES).map(k => `<th>${esc(ROLES[k].name)}</th>`).join('')}</tr>${[['Run replay, change data and settings', 'run'], ['Propose plan changes', 'propose'], ['Approve or reject plans', 'approve'], ['Export the audit trail', 'export'], ['Verify the audit chain', 'verify'], ['View everything', 'read']].map(r => `<tr><td>${r[0]}</td>${Object.keys(ROLES).map(k => `<td>${ROLES[k].can.includes(r[1]) ? '&#10003;' : '-'}</td>`).join('')}</tr>`).join('')}</table>`;
  const props = ST.props.slice().reverse().map(p => `<tr><td>${p.id}</td><td>${p.date} ${tmS(p.slot)}</td><td>${esc(p.type)}</td><td>${p.from == null ? '-' : fmt0(p.from)} &rarr; ${fmt0(p.to)} kW</td><td><span class="pill ${p.status === 'approved' ? 'ok' : p.status === 'rejected' ? 'bad' : p.status === 'pending' ? 'warn' : 'fixed'}">${p.status === 'auto' ? 'auto-approved' : p.status}</span></td><td>${esc(p.by || '')}</td><td>${p.status === 'pending' && can('approve') ? `<button class="good" data-act="appr_id" data-v="${p.id}:1">Approve</button> <button class="badb" data-act="appr_id" data-v="${p.id}:0">Reject</button>` : ''}</td></tr>`).join('');
  const log = ST.audit.slice().reverse().slice(0, 150).map(e => `<tr><td>${e.seq}</td><td>${esc(e.at.replace('T', ' ').slice(0, 19))}</td><td>${esc(e.actor)}</td><td>${esc(e.action)}</td><td style="white-space:normal;text-align:left">${esc(e.detail)}</td><td class="mono">${e.hash.slice(0, 10)}</td></tr>`).join('');
  return `<div class="card soft"><b>Roles, sign-off and an audit trail.</b> Plans are recommendations. A person approves before anything would be sent to equipment, and every step is logged with a hash chain so edits to the log are detectable.<div class="warn">Demonstration: roles are switched on this page and the log is kept in this browser. In production the roles come from Azure sign-in and the log is written server-side, so neither can be changed by the user.</div></div>
  <div class="grid g2"><div class="card"><h2>Who can do what</h2><div class="tw">${matrix}</div></div>
  <div class="card"><h2>Plans awaiting or past a decision</h2><div class="tw">${props ? `<table><tr><th>#</th><th>When</th><th>Type</th><th>Cap</th><th>Status</th><th>Decided by</th><th></th></tr>${props}</table>` : '<div class="note">No plans yet. Start a replay in the Live tab. Tick "Require approver sign-off" to see a plan wait for an approver.</div>'}</div></div></div>
  <div class="card"><div class="bar"><h2 style="margin:0">Audit trail</h2><span class="sp"></span><span class="pill ${v.ok ? 'ok' : 'bad'}">${v.ok ? 'Chain intact (' + v.n + ' entries)' : 'TAMPERING DETECTED at entry ' + v.bad}</span><button data-act="aud_verify" ${dis(can('verify') || can('read'))}>Verify now</button><button data-act="aud_export" ${dis(can('export'))}>Export CSV</button><button data-act="aud_reset" ${dis(can('settings'))} title="Demo only">Reset demo log</button></div>
  <div class="tw"><table><tr><th>#</th><th>Time (UTC)</th><th>Who</th><th>Action</th><th style="text-align:left">Detail</th><th>Hash</th></tr>${log}</table></div></div>`;
}

/* ======================= SETTINGS & DATA ======================= */
function bodySettings() {
  const a = can('settings'), c = ST.cfg, u = ST.upload, ro = a ? '' : 'disabled';
  const num = (id, label, v, st) => `<div><label for="${id}">${label}</label><input type="number" id="${id}" value="${v}" step="${st || 'any'}" ${ro}></div>`;
  let map = '';
  if (u) { const o = (sel) => `<option value="-1">(none)</option>` + u.map.head.map((h, i) => `<option value="${i}" ${i === sel ? 'selected' : ''}>${esc(h || 'column ' + (i + 1))}</option>`).join('');
    map = `<div class="card"><h2>Check the columns we found in ${esc(u.name)}</h2><div class="form"><div><label>Timestamp</label><select id="up_ts">${u.map.head.map((h, i) => `<option value="${i}" ${i === u.map.tsI ? 'selected' : ''}>${esc(h || 'column ' + (i + 1))}</option>`).join('')}</select></div><div><label>Site demand (kW)</label><select id="up_load">${o(u.map.loadI)}</select></div><div><label>Solar (kW)</label><select id="up_pv">${o(u.map.pvI)}</select></div><div><label>EV chargers / flexible load (kW)</label><select id="up_ev">${o(u.map.evI)}</select></div></div><div class="note">Readings must be every 15, 30 or 60 minutes. Choose power (kW) columns, not energy (kWh).</div><div class="bar"><button class="pri" data-act="up_go">Use this data</button><button data-act="up_cancel">Cancel</button></div></div>`; }
  return `<div class="card soft"><h2>Data</h2><div class="bar"><b>Loaded:</b> ${ST.raw ? esc(ST.raw.name) + ' (' + ST.raw.nD + ' days)' : 'nothing yet'}</div>
    <div class="bar"><button data-act="load_preset" ${ro}>Use the Leader Energy site data</button><label class="note">Or upload your own (.csv or .xlsx): <input type="file" id="up_file" accept=".csv,.txt,.xlsx" ${ro}></label></div>
    <div class="note">Files are read in this browser and never uploaded. An .xlsx file loads a small spreadsheet reader from cdnjs the first time. The organisers' sheet layout is recognised automatically.</div><div id="up_msg"></div></div>${map}
  <div class="card"><h2>Site assumptions</h2><div class="form">
    ${num('cf_kwh', 'Battery energy (kWh)', c.bat.kwh, 50)}${num('cf_kw', 'Battery power (kW)', c.bat.kw, 10)}${num('cf_rte', 'Round-trip efficiency (%)', Math.round(c.bat.rte * 100), 1)}
    ${num('cf_ep', 'Peak energy rate (sen/kWh)', c.tariff.ep, 0.01)}${num('cf_eo', 'Off-peak energy rate (sen/kWh)', c.tariff.eo, 0.01)}${num('cf_md', 'Maximum demand (RM/kW/month)', c.tariff.md, 0.01)}
    ${num('cf_retail', 'Retail charge (RM/month)', c.tariff.retail, 1)}${num('cf_afa', 'AFA (sen/kWh)', c.tariff.afa, 0.01)}${num('cf_ef', 'Grid emission factor (kg CO2/kWh)', c.ef, 0.01)}
    ${num('cf_margin', 'Forecast safety margin (%)', Math.round(c.margin * 100), 5)}
    <div><label for="cf_dm">How to read the demand column</label><select id="cf_dm" ${ro}><option value="auto" ${c.demandMode === 'auto' ? 'selected' : ''}>Detect automatically</option><option value="gross" ${c.demandMode === 'gross' ? 'selected' : ''}>It is demand before solar</option><option value="net" ${c.demandMode === 'net' ? 'selected' : ''}>It is grid draw after solar</option></select></div></div>
    <div class="note">Tariff defaults are TNB MV time-of-use, the same as the main app. The battery size is an assumption for this demonstration, not a recommendation. The safety margin inflates forecast demand and trims forecast solar when choosing the peak cap, so one surprise does not empty the battery early.</div>
    <div class="bar"><button class="pri" data-act="cf_apply" ${ro}>Apply and re-plan the year</button><button data-act="cf_reset" ${ro}>Back to defaults</button></div></div>`;
}

/* ==========================================================================
   Demo factory: binds factory3d.html (the 3D hardware-demo model) to the site.
   The model exposes SunShine.update(reading) and listens for
   postMessage({type:"sunshine-reading", reading}). Three sources feed it:
     replay - the planner's live replay on this page (default)
     sim    - the model's own sunlight simulation
     hw     - a web address that returns the reading as JSON (ESP32 / Azure, later)
   Reading shape (unchanged):
     {panels:[{v},{v},{v}], battery:{v,pct,charging}, conveyor:{running,speed,dir,mode}, ev:{charging,reason}}
   ========================================================================== */
const FAC = { mode: HAS_DOM ? LS.get('sunops_facmode', 'replay') : 'replay', url: HAS_DOM ? LS.get('sunops_hwurl', '') : '', timer: 0, poll: 0, msg: '' };
function facStop() { if (FAC.timer) { clearInterval(FAC.timer); FAC.timer = 0; } if (FAC.poll) { clearInterval(FAC.poll); FAC.poll = 0; } }
const facFrame = () => $q('#fac_frame');
function facPost(reading) {
  const f = facFrame(); if (!f || !f.contentWindow) return;
  try { f.contentWindow.postMessage({ type: 'sunshine-reading', reading }, location.origin === 'null' ? '*' : location.origin); } catch (e) { }
  const j = $q('#fac_json'); if (j) j.textContent = JSON.stringify(reading, null, 1);
}
/* Turn the planner's state at the slot just replayed into the model's reading. */
function facReading() {
  const L = ST.live, D = ST.D; if (!L || !D || !ST.ctx) return null;
  if (!D.facRef) {
    let pvMax = 0; for (let i = 0; i < D.n; i++) if (D.pv[i] > pvMax) pvMax = D.pv[i];
    const sorted = Float64Array.from(D.load).sort(); D.facRef = { pvMax: pvMax || 1, loadRef: sorted[Math.floor(0.9 * sorted.length)] || 1 };
  }
  const ref = D.facRef, X = L.X, spd = ST.ctx.spd, started = L.idx > 0, s = started ? Math.min(spd - 1, L.idx - 1) : 0;
  const pv = started ? X.P[s] : X.f0.P[0], load = started ? X.L[s] : X.f0.L[0];
  const ev = started ? X.out.ev[s] : 0, batt = started ? X.out.batt[s] : 0, soc = started ? X.out.soc[s] : X.soc;   // batt: + discharge, - charge
  const rel = clamp(pv / ref.pvMax, 0, 1), spread = [1, 0.98, 1.015];
  const running = load >= 0.4 * ref.loadRef, speed = running ? Math.max(30, clamp(Math.round(100 * load / ref.loadRef), 0, 100)) : 0;
  const mode = batt > 1 ? 'Battery' : pv >= load + ev ? 'Solar' : pv > 0.2 * (load + ev) ? 'Solar + grid' : 'Grid';
  const inWin = X.inWin(s);
  const reason = ev > 1 ? (pv >= load + ev ? 'Surplus solar' : !inWin ? 'Planned outside the peak window' : 'Scheduled in the peak window') : (inWin ? 'Held back in the peak window' : 'No EV demand now');
  return {
    panels: spread.map(k => ({ v: +(3.3 * clamp(rel * k, 0, 1)).toFixed(2) })),
    battery: { v: +(3.3 + 0.9 * soc).toFixed(2), pct: Math.round(soc * 100), charging: batt < -1 },
    conveyor: { running, speed, dir: 'Forward', mode },
    ev: { charging: ev > 1, reason }
  };
}
function factorySend() {                         // called after every replay tick (see drawLive)
  if (!ST.open || ST.tab !== 'fac' || FAC.mode !== 'replay') return;
  const r = facReading(); if (r) facPost(r);
  drawFactoryStatus();
}
function facBeat() {                             // the model shows "No data" after 12 s of silence, so repeat the last reading
  if (!ST.open || ST.tab !== 'fac') { facStop(); return; }
  if (FAC.mode === 'replay') { const r = facReading(); if (r) facPost(r); }
}
async function facPollOnce() {
  try {
    if (!FAC.url) throw new Error('no address set');
    const r = await fetch(FAC.url, { cache: 'no-store' }); if (!r.ok) throw new Error('HTTP ' + r.status);
    let j = await r.json(); j = j && j.reading ? j.reading : j; if (!j || typeof j !== 'object') throw new Error('not a reading');
    facPost(j); FAC.msg = 'Last reading received at ' + new Date().toLocaleTimeString() + '.';
  } catch (e) { FAC.msg = 'Could not read the feed: ' + (e.message || e) + '. The model shows "No data" after 12 seconds.'; }
  drawFactoryStatus();
}
function facApply() {
  if (FAC.mode === 'replay') { const r = facReading(); if (r) facPost(r); drawFactoryStatus(); }
  else if (FAC.mode === 'sim') { try { facFrame().contentWindow.SunShine.simulate(); } catch (e) { } drawFactoryStatus(); }
  else { if (FAC.poll) clearInterval(FAC.poll); facPollOnce(); FAC.poll = setInterval(facPollOnce, 2000); }
}
function drawFactoryStatus() {
  const el = $q('#fac_status'); if (!el) return;
  if (FAC.mode === 'replay') {
    const L = ST.live; if (!L) { el.textContent = ''; return; }
    el.innerHTML = 'Replaying <b>' + esc(dayLabel(L.d)) + '</b> at <b>' + tmS(Math.min(L.idx, ST.ctx.spd)) + '</b>, ' + esc(STRATS[ST.sel.strat].short) + (L.inject !== 'none' ? ' <span class="tag">simulated disturbance</span>' : '') +
      (L.pending ? '<div class="warn">The replay is paused: a plan change is waiting for an approver. Open Live replay as Approver to decide.</div>' : '') + (L.done ? ' (day complete)' : '');
  } else if (FAC.mode === 'sim') el.textContent = "The model is running its own sunlight simulation. Use its Sunlight slider.";
  else el.textContent = FAC.msg || 'Enter the address of the feed and press Connect.';
}
function bodyFactory() {
  if (!ST.live) liveStart(ST.sel.day);
  const D = ST.D, L = ST.live, a = can('run'), picks = pickDays().list, m = FAC.mode;
  const replayCtl = m === 'replay' ? `<div class="bar">
      <label class="note">Day <input type="date" id="lv_date" value="${D.days[L.d].date}" min="${D.days[0].date}" max="${D.days[D.nD - 1].date}" ${dis(a)}></label>
      <select id="lv_pick" ${dis(a)}><option value="">Jump to...</option>${picks.map(p => `<option value="${p[1]}">${esc(p[0])}: ${D.days[p[1]].date}</option>`).join('')}</select>
      <label class="note">Strategy <select id="lv_strat" ${dis(a)}>${stratOpts(ST.sel.strat, ['cost', 'bal', 'zero'])}</select></label>
      <label class="note">What-if <select id="lv_inj" ${dis(a)}><option value="none" ${L.inject === 'none' ? 'selected' : ''}>As recorded</option><option value="cloud" ${L.inject === 'cloud' ? 'selected' : ''}>Cloud front 12:00-15:00</option><option value="surge" ${L.inject === 'surge' ? 'selected' : ''}>Demand +25% 15:00-19:00</option><option value="both" ${L.inject === 'both' ? 'selected' : ''}>Both</option></select></label></div>
    <div class="bar" id="lv_btns"></div>` : '';
  const hwCtl = m === 'hw' ? `<div class="bar"><label class="note">Feed address <input type="url" id="fac_url" value="${esc(FAC.url)}" placeholder="https://your-endpoint/latest" style="min-width:300px"></label><button class="pri" data-act="fac_connect">Connect</button></div>
    <div class="note">The address must return the reading as JSON (see below) and allow requests from this site (CORS). The ESP32 firmware and the Azure endpoint are still to be built.</div>` : '';
  return `<div class="card soft"><div class="bar"><b>Demo factory.</b><span class="note" style="margin:0">The 3D model of the hardware demo, bound to this site. Drag to rotate, click a part for its status.</span></div>
    <div class="bar"><label class="note">Readings come from <select id="fac_mode"><option value="replay" ${m === 'replay' ? 'selected' : ''}>The planner's live replay</option><option value="sim" ${m === 'sim' ? 'selected' : ''}>The model's own sunlight simulation</option><option value="hw" ${m === 'hw' ? 'selected' : ''}>A hardware feed (web address)</option></select></label></div>${replayCtl}${hwCtl}
    <div class="note" id="fac_status"></div></div>
  <div class="card"><iframe id="fac_frame" src="factory3d.html?embed=1" title="Sun Shine demo factory" style="width:100%;height:740px;border:0;border-radius:10px;background:#fff" allowfullscreen></iframe>
    <div class="note">If this is blank, <code>factory3d.html</code> is not in the repo root next to <code>index.html</code>. The model loads three.js from cdnjs, so it needs internet.</div></div>
  <div class="card"><h2>How the replay drives the model</h2>
    <div class="note"><b>Panels:</b> the planner's solar output as a share of the site's best output, shown as panel voltage (3.3 V = full sun, three panels with a small spread). <b>Battery:</b> the planned battery charge, shown as cell voltage (3.3 V empty to 4.2 V full) and charging when the plan charges it. <b>Conveyor:</b> production, running while site demand is above 40% of its usual high level, with the power source taken from the plan. <b>EV station:</b> the planned EV charging, with the reason. This is a visual of the planner's schedule on the organisers' data. It does not control any hardware, and the model's scale is a demo, not the site's size.</div>
    <details><summary>Reading being sent to the model</summary><pre class="mono" id="fac_json" style="white-space:pre-wrap"></pre></details>
    <div class="note">Same format for the hardware feed: <code>{"panels":[{"v":2.9},{"v":2.8},{"v":3.0}],"battery":{"v":3.95,"pct":72,"charging":true},"conveyor":{"running":true,"speed":80,"dir":"Forward","mode":"Solar"},"ev":{"charging":true,"reason":"Surplus solar"}}</code>. Send one at least every 12 seconds.</div></div>`;
}
function mountFactory() {
  const f = facFrame(); if (f) f.addEventListener('load', facApply);
  renderLiveButtons(); drawFactoryStatus();
  FAC.timer = setInterval(facBeat, 3000);
}
SunOps.factoryUpdate = r => { if (HAS_DOM) facPost(r); };      // other pages can push a reading: SunOps.factoryUpdate({...})

/* ==========================================================================
   Events, upload, open/close, boot
   ========================================================================== */
function downloadText(name, text, type) { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' })); a.download = name; document.body.appendChild(a); a.click(); a.remove(); }
function loadScript(src) { return new Promise((res, rej) => { if (root.XLSX) return res(); const s = document.createElement('script'); s.src = src; s.onload = res; s.onerror = () => rej(new Error('Could not load the spreadsheet reader (needs internet access to cdnjs.cloudflare.com). Save the sheet as CSV instead.')); document.head.appendChild(s); }); }
async function handleUpload(file) {
  const msg = $q('#up_msg'); const say = (t, c) => { if (msg) msg.innerHTML = `<div class="${c || 'note'}">${esc(t)}</div>`; };
  try {
    say('Reading ' + file.name + '...');
    let rows;
    if (/\.xlsx?$/i.test(file.name)) {
      await loadScript('https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js');
      const wb = root.XLSX.read(await file.arrayBuffer(), { type: 'array' });
      const name = wb.SheetNames.find(n => /data/i.test(n)) || wb.SheetNames[0];
      rows = root.XLSX.utils.sheet_to_json(wb.Sheets[name], { header: 1, raw: true, defval: '' });
    } else rows = csvToRows(await file.text());
    const map = detectColumns(rows);
    if (!map || map.tsI < 0 || map.loadI < 0) { say('Could not find a timestamp column and a demand (kW) column. Check the header row, or see the sample file leader_site.csv.', 'bad'); return; }
    ST.upload = { name: file.name, rows, map }; audit('upload_selected', file.name + ': ' + rows.length + ' rows'); render();
  } catch (e) { say(String(e.message || e), 'bad'); }
}
async function useUpload() {
  const u = ST.upload; if (!u) return; const g = id => parseInt($q('#' + id).value, 10);
  const map = Object.assign({}, u.map, { tsI: g('up_ts'), loadI: g('up_load'), pvI: g('up_pv'), evI: g('up_ev') });
  try { const ser = rowsToSeries(u.rows, map); ST.upload = null; ST.tab = 'live'; render(); await adoptSeries(ser, { name: u.name, source: 'upload' }); }
  catch (e) { ST.tab = 'set'; render(); const m = $q('#up_msg'); if (m) m.innerHTML = `<div class="bad">${esc(e.message)}</div>`; }
}
function readCfg() {
  const v = id => parseFloat($q('#' + id).value), c = ST.cfg;
  const n = (x, d) => Number.isFinite(x) && x >= 0 ? x : d;
  c.bat.kwh = Math.max(10, n(v('cf_kwh'), c.bat.kwh)); c.bat.kw = Math.max(10, n(v('cf_kw'), c.bat.kw)); c.bat.rte = clamp(n(v('cf_rte'), 90) / 100, 0.5, 1);
  c.tariff.ep = n(v('cf_ep'), c.tariff.ep); c.tariff.eo = n(v('cf_eo'), c.tariff.eo); c.tariff.md = n(v('cf_md'), c.tariff.md); c.tariff.retail = n(v('cf_retail'), c.tariff.retail); c.tariff.afa = n(v('cf_afa'), c.tariff.afa);
  c.ef = n(v('cf_ef'), c.ef); c.margin = clamp(n(v('cf_margin'), 25) / 100, 0, 0.6); c.demandMode = $q('#cf_dm').value;
}
function dayIdx(date) { return ST.D.days.findIndex(x => x.date === date); }
function onClick(e) {
  const t = e.target.closest('[data-act]'); if (!t) return; const act = t.dataset.act, v = t.dataset.v;
  if (act === 'role') { if (ST.role !== v) { ST.role = v; audit('role_switch', 'Now viewing as ' + ROLES[v].name); stopLive(); ST.tab = can('run') ? 'live' : 'appr'; } render(); }
  else if (act === 'fac') { if (ST.fac !== v) { stopLive(); if (ST.tab === 'fac') ST.tab = 'live'; audit('facility_switch', FACS[v].name); render(); loadPreset(v); } }
  else if (act === 'close') closeConsole();
  else if (act === 'tab') { if (v !== 'live' && v !== 'fac') stopLive(); ST.tab = v; render(); }
  else if (act === 'fac_connect') { FAC.url = ($q('#fac_url').value || '').trim(); LS.set('sunops_hwurl', FAC.url); audit('setting', 'Hardware feed address ' + (FAC.url ? 'set' : 'cleared')); facApply(); }
  else if (act === 'retry') loadPreset();
  else if (act === 'leg') { ST.sel.vis[v] = ST.sel.vis[v] === false; mountScen(); t.classList.toggle('off', ST.sel.vis[v] === false); }
  else if (act === 'leg1') { const l = ST.sel.l1; l[v] = !l[v]; drawLive(); }
  else if (act === 'scv') { ST.sel.scView = v; render(); }
  else if (act === 'lv_play') { if (!can('run')) return; livePlay(); renderLiveButtons(); }
  else if (act === 'lv_pause') { stopLive(); renderLiveButtons(); }
  else if (act === 'lv_step') { if (!can('run')) return; stopLive(); liveStep(); drawLive(); renderLiveButtons(); }
  else if (act === 'lv_restart') { liveStart(ST.live.d); renderLiveButtons(); }
  else if (act === 'appr_yes' || act === 'appr_no') { const r = ST.live && ST.live.pendingRec; if (r) decide(r.id, act === 'appr_yes'); renderLiveButtons(); }
  else if (act === 'appr_id') { const [id, y] = v.split(':'); decide(parseInt(id, 10), y === '1'); }
  else if (act === 'aud_verify') { const r = verifyAudit(); audit('audit_verify', r.ok ? 'Chain intact, ' + r.n + ' entries' : 'Tampering detected at entry ' + r.bad); render(); }
  else if (act === 'aud_export') { audit('audit_export', ST.audit.length + ' entries'); const h = 'seq,time_utc,actor,action,detail,prev_hash,hash'; downloadText('sunshine_audit_trail.csv', [h].concat(ST.audit.map(e => [e.seq, e.at, e.actor, e.action, '"' + e.detail.replace(/"/g, '""') + '"', e.prev, e.hash].join(','))).join('\n'), 'text/csv'); render(); }
  else if (act === 'aud_reset') { if (confirm('Clear the demo audit log in this browser?')) { ST.audit = []; LS.set('sunops_audit', []); audit('log_reset', 'Demo log cleared'); render(); } }
  else if (act === 'load_preset') { ST.tab = 'live'; render(); loadPreset(); }
  else if (act === 'up_go') useUpload();
  else if (act === 'up_cancel') { ST.upload = null; render(); }
  else if (act === 'cf_apply') { readCfg(); saveCfg(); ST.tab = 'live'; ST.live = null; render(); recompute(); }
  else if (act === 'cf_reset') { ST.cfg = { bat: Object.assign({}, DEFAULTS.bat), tariff: Object.assign({}, DEFAULTS.tariff), ef: DEFAULTS.ef, margin: 0.25, demandMode: 'auto' }; saveCfg(); ST.tab = 'live'; render(); recompute(); }
}
function onChange(e) {
  const id = e.target.id, v = e.target.value; if (!id) return;
  if (id === 'up_file') { if (e.target.files[0]) handleUpload(e.target.files[0]); return; }
  if (id === 'lv_date') { const d = dayIdx(v); if (d >= 0) { ST.sel.day = d; liveStart(d); renderLiveButtons(); } }
  else if (id === 'lv_pick') { if (v !== '') { ST.sel.day = parseInt(v, 10); if (ST.tab === 'live') render(); liveStart(ST.sel.day); renderLiveButtons(); const di = $q('#lv_date'); if (di) di.value = ST.D.days[ST.sel.day].date; } }
  else if (id === 'lv_strat') { ST.sel.strat = v; liveStart(ST.live.d); renderLiveButtons(); }
  else if (id === 'lv_mode') { ST.sel.mode = v; liveStart(ST.live.d); renderLiveButtons(); }
  else if (id === 'lv_inj') { const k = v; ST.live.inject = k; liveStart(ST.live.d, { inject: k }); if (ST.tab === 'live') render(); else { renderLiveButtons(); drawLive(); } }
  else if (id === 'fac_mode') { stopLive(); FAC.mode = v; LS.set('sunops_facmode', v); render(); }
  else if (id === 'lv_req') { ST.live.require = e.target.checked; audit('setting', 'Approver sign-off ' + (e.target.checked ? 'required' : 'not required') + ' for plan changes'); drawLive(); }
  else if (id === 'lv_speed') { ST.live.speed = parseInt(v, 10); if (ST.live.playing) { stopLive(); livePlay(); } }
  else if (id === 'sc_date') { const d = dayIdx(v); if (d >= 0) { ST.sel.day = d; ST.live = null; mountScen(); } }
  else if (id === 'sc_month' || id === 'bl_month' || id === 'tw_month') { ST.sel.month = parseInt(v, 10); render(); }
  else if (id === 'sc_mode' || id === 'bl_mode') { ST.sel.mode = v; ST.live = null; render(); }
  else if (id === 'bl_strat' || id === 'tw_strat') { ST.sel.strat = v; render(); }
}
function onInput(e) { if (e.target.id === 'lv_seek' && ST.live && can('run')) { stopLive(); liveSeek(parseInt(e.target.value, 10)); drawLive(); renderLiveButtons(); } }

const headEl = () => document.querySelector('header.top');
function fitTop() { const r = $root(), h = headEl(); if (r && h) r.style.setProperty('--sotop', Math.max(0, Math.round(h.getBoundingClientRect().bottom)) + 'px'); }
let savedOn = null;
function markBtn(open) {
  const btn = document.getElementById('sunOpsBtn'), rp = document.querySelectorAll('[data-rp]');
  if (open) { savedOn = Array.from(rp).filter(b => b.classList.contains('on')); rp.forEach(b => b.classList.remove('on')); if (btn) btn.classList.add('on'); }
  else { if (btn) btn.classList.remove('on'); if (savedOn) savedOn.forEach(b => b.classList.add('on')); savedOn = null; }
}
function openConsole() {
  const r = ensureRoot(); const was = ST.open; ST.open = true; r.hidden = false; document.body.style.overflow = 'hidden'; fitTop(); if (!was) markBtn(true);
  if (!ST.raw && ST.status !== 'loading') loadPreset();
  render();
}
function closeConsole() { const was = ST.open; stopLive(); facStop(); ST.open = false; if (was) markBtn(false); const r = $root(); if (r) r.hidden = true; document.body.style.overflow = ''; if (location.hash === '#ops') history.replaceState(null, '', location.pathname + location.search); }
function ensureRoot() {
  let r = $root(); if (r) return r;
  r = document.createElement('div'); r.id = 'sunops'; r.hidden = true; r.setAttribute('role', 'dialog'); r.setAttribute('aria-label', 'Operations console'); r.innerHTML = '<div class="wrap"></div>';
  document.body.appendChild(r);
  r.addEventListener('click', onClick); r.addEventListener('change', onChange); r.addEventListener('input', onInput);
  return r;
}
function entryCard() {
  if (LS.get('sunops_entry_off', false)) return;
  const app = document.getElementById('app'), ph = app && app.querySelector('.pagehead'); if (!ph || document.getElementById('sunopsEntry')) return;
  const c = document.createElement('div'); c.id = 'sunopsEntry'; c.className = 'card';
  c.innerHTML = '<b>New: Operations console</b><span class="note" style="margin:0">Live replay, all strategies on one chart, monthly bill, digital-twin check, approvals and audit trail, and the 3D demo factory.</span><span class="sp"></span><button class="pri" data-e="ops">Open the console</button><button data-e="fac">See the demo factory</button><button data-e="x" aria-label="Dismiss this card">Dismiss</button>';
  c.addEventListener('click', e => { const t = e.target.closest('[data-e]'); if (!t) return; const k = t.dataset.e; if (k === 'x') { LS.set('sunops_entry_off', true); c.remove(); } else { ST.tab = k === 'fac' ? 'fac' : 'live'; openConsole(); } });
  app.insertBefore(c, ph);
}
function boot() {
  const st = document.createElement('style'); st.id = 'sunops-css'; st.textContent = CSS; document.head.appendChild(st);
  const b = document.createElement('button'); b.id = 'sunOpsBtn'; b.type = 'button'; b.textContent = 'Operations'; b.title = 'Live replay, scenarios, monthly bill, digital twin, approvals'; b.addEventListener('click', () => { if (!ST.open) openConsole(); });
  const grp = document.getElementById('roleSwitch');
  if (grp) { grp.appendChild(b); grp.addEventListener('click', e => { if (ST.open && e.target.closest('[data-rp]')) closeConsole(); }, true); }
  else { b.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:50;background:#F59E0B;border:1px solid #F59E0B;border-radius:8px;padding:8px 12px;font-weight:600;cursor:pointer'; document.body.appendChild(b); }
  window.addEventListener('resize', () => { if (ST.open) fitTop(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && ST.open) closeConsole(); });
  entryCard();
  if (location.hash === '#ops') openConsole();
  window.addEventListener('hashchange', () => { if (location.hash === '#ops' && !ST.open) openConsole(); });
}
SunOps.open = () => HAS_DOM && openConsole(); SunOps.state = ST;
if (HAS_DOM) { if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot(); }
root.SunOps = SunOps;
})(typeof window !== 'undefined' ? window : globalThis);
