/* Analysis page (canedge-lake-analysis build_analysis.py) on the shared dashboard kit (canedge-reports): Filter bar,
   tabs, KPI groups and tables (DASH), line charts + state timelines (SERIES), stacked bars (stackedBars), GNSS map
   (TRACK). R = results.json (references/results-spec.md). The page never queries: "filtered" panels re-aggregate the
   facts for the selected devices + period, "fixed" panels show what was computed (dimmed when the selection differs).
   Custom panels: ANALYSIS.panel("name", (el, view, R, panel) => ...) in the --custom-js file. */
"use strict";
const ANALYSIS = (function () {
  const custom = {};
  return { panel: (name, fn) => { custom[name] = fn; }, custom };
})();

window.addEventListener("DOMContentLoaded", () => {
  const { esc, info, fmt, table, kgroups, VI, CAT, colorOf, num, setMeta } = DASH;
  const ep = (x) => (x == null ? null : typeof x === "number" ? x : Date.parse(x) / 1000);
  const DEVS = Object.keys(R.devices).sort(), NAME = R.devices;
  const label = (d) => NAME[d] || d;
  const T0 = ep(R.window[0]), T1 = ep(R.window[1]), STEP = R.step || 86400, SNAP = Math.min(STEP, 60);
  const COLORS = { blue: VI.blue, green: VI.green, orange: VI.orange, red: VI.red };
  // One color per device on every chart (DASH.colorOf: devices first, in ID order, as the device stacks always were);
  // series named after a device (meta name, bare name or ID) take its color, other names keep theirs across charts
  DEVS.forEach((d) => colorOf(d));
  const DEV_OF = {};
  for (const d of DEVS) for (const n of [d, NAME[d], (NAME[d] || "").replace(/\s*\([0-9A-Fa-f]{8}\)\s*$/, "")]) if (n) DEV_OF[n] = d;
  const nameColor = (lab) => colorOf(DEV_OF[lab] ?? lab);
  const SEQ = ["#dbe9f6", "#b4d2ee", "#86b6e2", "#5b99d3", "#3d85c6", "#2a6aa6", "#1f5a8c", "#143d61"];
  setMeta(Object.fromEntries(DEVS.filter((d) => NAME[d]).map((d) => [d, NAME[d].replace(/\s*\([0-9A-Fa-f]{8}\)\s*$/, "")])),   // next to the ID column: the bare name
    "Device meta name. Names are not unique: The device ID identifies the device");

  // ---------------------------------------------------------------- facts: filter by device + period overlap
  const F = {};
  for (const [k, f] of Object.entries(R.facts || {})) F[k] = { ...f, ix: Object.fromEntries(f.columns.map((c, i) => [c, i])) };
  function rowsOf(name, v, where) {
    const f = F[name];
    if (!f) throw new Error(`results.json: facts '${name}' not found`);
    const di = f.ix.device, ti = f.ix.t, ei = f.ix.end, per = f.period || STEP;
    let rows = f.rows.filter((r) => (di == null || v.devs.has(r[di])) &&
      (ti == null || (r[ti] < v.to && (ei != null ? Math.max(r[ei], r[ti]) : r[ti] + per) > v.from)));
    for (const [c, w] of Object.entries(where || {})) {
      const i = f.ix[c], ok = new Set([].concat(w));
      rows = rows.filter((r) => ok.has(r[i]));
    }
    return { f, rows };
  }
  const colOf = (f, rows, c) => {
    const i = f.ix[c];
    if (i == null) throw new Error(`results.json: column '${c}' not in the facts (${f.columns.join(", ")})`);
    return rows.map((r) => r[i]).filter((x) => x != null);
  };
  const sumA = (a) => a.reduce((s, x) => s + x, 0);
  function agg(f, rows, s) {
    let x = null;
    if (s.agg === "count") x = rows.length;
    else if (s.agg === "share") {   // part of a whole: sum(col) where num / sum(col), e.g. time above 90 km/h of the driving time
      const tot = sumA(colOf(f, rows, s.col));
      const part = rows.filter((r) => Object.entries(s.num || {}).every(([c, w]) => [].concat(w).includes(r[f.ix[c]])));
      x = tot ? sumA(colOf(f, part, s.col)) / tot : null;
    }
    else if (s.agg === "ratio") { const d = sumA(colOf(f, rows, s.cols[1])); x = d ? sumA(colOf(f, rows, s.cols[0])) / d : null; }
    else {
      const a = colOf(f, rows, s.col);
      if (s.agg === "sum") x = sumA(a);
      else if (s.agg === "max") x = a.length ? a.reduce((m, y) => (y > m ? y : m), -Infinity) : null;
      else if (s.agg === "min") x = a.length ? a.reduce((m, y) => (y < m ? y : m), Infinity) : null;
      else if (s.agg === "mean") x = a.length ? sumA(a) / a.length : null;
      else if (s.agg === "distinct") x = new Set(a).size;
      else throw new Error(`results.json: unknown agg '${s.agg}'`);
    }
    return x == null ? null : x * (s.scale ?? 1);
  }
  const smart = (x) => (Math.abs(x) >= 1000 ? num(Math.round(x)) : String(+x.toPrecision(3)));
  const fmtV = (x, s = {}) => (x == null ? "-" : (s.decimals != null ? x.toLocaleString("en-US", { minimumFractionDigits: s.decimals, maximumFractionDigits: s.decimals }) : smart(x)) + (s.unit ? (s.unit === "%" ? "%" : ` ${s.unit}`) : ""));
  const groupBy = (rows, f, cols) => {
    const g = new Map();
    for (const r of rows) { const k = JSON.stringify(cols.map((c) => r[f.ix[c]])); if (!g.has(k)) g.set(k, []); g.get(k).push(r); }
    return g;
  };

  // ---------------------------------------------------------------- activity (brush) from the activity facts
  const ACT = {};
  if (R.activity && F[R.activity]) {
    const f = F[R.activity], per = f.period || STEP;
    for (const r of f.rows) (ACT[r[f.ix.device]] = ACT[r[f.ix.device]] || new Set()).add(r[f.ix.t]);
    for (const d of Object.keys(ACT)) ACT[d] = [...ACT[d]].sort((a, b) => a - b).map((t) => [t, t + per]);
  }

  // ---------------------------------------------------------------- panels: html now, charts after render
  let DRAW = [], MAPS = [], seq = 0;
  const W = () => Math.max(300, document.getElementById("panels").clientWidth - 4);
  const slot = (draw) => { const id = `ap${seq++}`; DRAW.push(() => draw(document.getElementById(id))); return `<div id="${id}" class="apanel"></div>`; };
  const title = (p) => (p.title ? `<div class="ttitle">${esc(p.title)}${info(p.tip)}</div>` : "");
  const sameSel = (v, c) => {
    if (v.devs.size === DEVS.length && v.from <= T0 + SNAP && v.to >= T1 - SNAP) return true;   // default view: as authored
    const ds = new Set(c.devices || DEVS);
    return ds.size === v.devs.size && [...ds].every((d) => v.devs.has(d)) &&
      Math.abs(ep(c.from ?? R.window[0]) - v.from) < SNAP + 1 && Math.abs(ep(c.to ?? R.window[1]) - v.to) < SNAP + 1;
  };
  function fixedNote(p, v) {
    const c = p.computed || {}, ds = c.devices || DEVS;
    const txt = `Computed for ${ds.length === DEVS.length && ds.length > 1 ? `all ${ds.length} devices` : ds.map(label).join(", ")}, ${fmt(ep(c.from ?? R.window[0]), "dy")} to ${fmt(ep(c.to ?? R.window[1]), "dy")}`;
    return { cls: sameSel(v, c) ? "" : " dim", note: `<div class="fixnote">${esc(txt)}${info("A fixed result: It does not follow the device and period filters (it does not merge, e.g. percentiles or comparisons). Dimmed when the selection differs")}</div>` };
  }
  const showLink = (o, txt = "show") => `<a href="#" class="show" data-show="${esc(JSON.stringify(o))}">${txt}</a>`;

  // Category bars (one horizontal bar per group, largest first) and histograms (one column per bin, in order).
  // Values: p.value = {agg, col | cols, scale, unit, decimals} per group of p.group (default device), or the sum of
  // p.value (a column, e.g. seconds) per p.stack label (hist). Fixed: p.items = [{label, value}] inline
  function catBars(p, v) {
    let items;
    if (p.items) items = p.items.map((x) => ({ label: x.label, value: x.value }));
    else {
      const { f, rows } = rowsOf(p.facts, v, p.where);
      if (p.layout === "hist") {
        const st = p.stack || "label", col = typeof p.value === "string" ? p.value : "seconds";
        const labs = p.order || [...new Set(rows.map((r) => r[f.ix[st]]))];
        items = labs.map((l) => ({ label: l, value: sumA(colOf(f, rows.filter((r) => r[f.ix[st]] === l), col)) * (p.scale ?? 1) }));
      } else {
        const gc = p.group || "device";
        items = [...groupBy(rows, f, [gc]).entries()].map(([k, rs]) => { const key = JSON.parse(k)[0]; return { label: gc === "device" ? label(key) : String(key), value: agg(f, rs, p.value) }; });
        items = items.filter((x) => x.value != null).sort((a, b) => b.value - a.value).slice(0, p.top || 20);
      }
    }
    if (!items.length) return `<div class="empty">No data in the selection</div>`;
    const fv = p.layout === "hist" ? { unit: p.unit, decimals: p.decimals } : { unit: (p.value || {}).unit ?? p.unit, decimals: (p.value || {}).decimals ?? p.decimals };
    const mx = Math.max(...items.map((x) => Math.abs(x.value || 0)), 1e-12), tot = sumA(items.map((x) => x.value || 0));
    if (p.layout === "hist") {
      return `<div class="vbars" style="--n:${items.length}">${items.map((x) => `<div class="vb" data-tip="${esc(`${x.label}: ${fmtV(x.value, fv)}${tot ? ` (${Math.round((100 * x.value) / tot)}%)` : ""}`)}"><div class="vb-v">${fmtV(x.value, fv)}</div><div class="vb-b" style="height:${(100 * Math.abs(x.value || 0)) / mx}%"></div></div>`).join("")}</div>` +
        `<div class="vbars-l" style="--n:${items.length}">${items.map((x) => `<span>${esc(x.label)}</span>`).join("")}</div>` + (p.xlabel ? `<div class="vbars-x">${esc(p.xlabel)}</div>` : "");
    }
    return `<div class="hbars">${items.map((x) => `<div class="hb-l" title="${esc(x.label)}">${esc(x.label)}</div><div class="hb-t"><span style="width:${(100 * Math.abs(x.value || 0)) / mx}%;background:${p.color || VI.blue}"></span></div><div class="hb-v">${fmtV(x.value, fv)}</div>`).join("")}</div>`;
  }

  const PANELS = {
    note: (p) => `<p class="anote">${esc(p.text)}</p>`,
    kpis(p, v) {
      return kgroups(p.groups.map((g) => ({ title: g.title, c: COLORS[g.color] || g.color || VI.blue, k: g.items.map((it) => {
        if (!it.facts) return { l: it.label, v: typeof it.value === "number" ? fmtV(it.value * (it.scale ?? 1), it) : String(it.value ?? "-"), n: it.note || "", tip: it.tip };   // literal (computed) value
        const { f, rows } = rowsOf(it.facts, v, it.where);
        return { l: it.label, v: fmtV(agg(f, rows, it), it), n: it.note || "", tip: it.tip };
      }) })));
    },
    table(p, v) {
      if (p.mode === "fixed") {   // columns: strings or {label, unit, bar, tip, decimals} (as the facts tables)
        const hd = (c) => (typeof c === "object" && c ? c.label + (c.unit ? ` (${c.unit})` : "") : String(c));
        const cols = p.columns.map(hd), tips = {}, dec = { ...(p.decimals || {}) }, bars = [...(p.bars || [])];
        p.columns.forEach((c, i) => { if (typeof c !== "object" || !c) return; if (c.tip) tips[cols[i]] = c.tip; if (c.decimals != null) dec[i] = c.decimals; if (c.bar) bars.push(i); });
        const dev = cols.findIndex((c) => /^device$/i.test(c));
        return table({ title: p.title, titleTip: p.tip, columns: cols, rows: p.rows, dev: dev >= 0 ? dev : undefined, bars, tips, sort: p.sort, count: true, decimals: dec });
      }
      // One row per group (e.g. per device); a column may take its values from other facts (c.facts, same group
      // columns), e.g. driving hours from the speed bands next to the average current from the daily stats
      const { f, rows } = rowsOf(p.facts, v, p.where), g = groupBy(rows, f, p.group);
      const other = {};
      for (const c of p.columns) if (c.facts && c.facts !== p.facts && !other[c.facts]) {
        const o = rowsOf(c.facts, v);
        other[c.facts] = { f: o.f, g: groupBy(o.rows, o.f, p.group) };
      }
      const cell = (c, k, rs) => {
        const src = c.facts && c.facts !== p.facts ? other[c.facts] : { f, g: null };
        const rr = src.g ? src.g.get(k) || [] : rs;
        const sel = c.where ? rr.filter((r) => Object.entries(c.where).every(([cc, w]) => [].concat(w).includes(r[src.f.ix[cc]]))) : rr;
        if (c.agg === "span") {   // first to last period: a date range (text)
          const ts = colOf(src.f, sel, "t");
          const f2 = (t) => fmt(t, "dy").slice(0, c.days ? 8 : 5);   // YY/MM (YY/MM/DD with c.days)
          return ts.length ? `${f2(Math.min(...ts))} to ${f2(Math.max(...ts))}` : "-";
        }
        const x = agg(src.f, sel, c);
        return x == null ? "-" : x;
      };
      const out = [...g.entries()].map(([k, rs]) => [...JSON.parse(k), ...p.columns.map((c) => cell(c, k, rs))]);
      const n = p.group.length, dec = {}, tips = {};
      const head = (c) => c.label + (c.unit ? ` (${c.unit})` : "");
      p.columns.forEach((c, i) => { if (c.decimals != null) dec[n + i] = c.decimals; if (c.tip) tips[head(c)] = c.tip; });
      const dev = p.group.indexOf("device");
      return table({ title: p.title, titleTip: p.tip, count: true, rows: out, dev: dev >= 0 ? dev : undefined, decimals: dec, tips,
        columns: [...p.group.map((c) => p.groupLabels?.[c] || c[0].toUpperCase() + c.slice(1)), ...p.columns.map(head)],
        bars: p.columns.map((c, i) => (c.bar ? n + i : null)).filter((x) => x != null), sort: p.sort || [n, -1] });
    },
    series(p, v) {
      if (p.data) return title(p) + slot((el) => {   // inline data (e.g. a resample result, a computed series): t + series
        const d = p.data, g = SERIES.group({ key: `f${seq}`, xRange: [d.t[0], d.t[d.t.length - 1]], tz: CFG.tz, dateLine: true });
        SERIES.line(el, g, { width: W(), height: p.height || 240, xs: d.t, points: d.t.length <= 120, fmtV: (x) => fmtV(x, p),
          series: d.series.map((s) => ({ label: s.label, values: s.values, stroke: s.color || nameColor(s.label), width: 2 })) });
      });
      return title(p) + slot((el) => {
        const { f, rows } = rowsOf(p.facts, v, p.where || p.y.where), split = p.split || "device";
        const xs0 = [...new Set(rows.map((r) => r[f.ix.t]))].sort((a, b) => a - b), per = f.period || STEP;
        const xs = xs0.flatMap((x, i) => (i && x - xs0[i - 1] > 1.5 * per ? [xs0[i - 1] + per, x] : [x]));   // a gap point: no line across periods without data
        const xi = new Map(xs.map((x, i) => [x, i]));
        const lines = [...groupBy(rows, f, [split]).entries()].map(([k, rs]) => {
          const vals = xs.map(() => null);
          for (const [t, rr] of groupBy(rs, f, ["t"])) vals[xi.get(JSON.parse(t)[0])] = agg(f, rr, p.y);
          const key = JSON.parse(k)[0];
          return { label: split === "device" ? label(key) : String(key), values: vals, stroke: split === "device" ? colorOf(key) : nameColor(String(key)), width: 2 };
        });
        if (!xs.length) { el.innerHTML = `<div class="empty">No data in the selection</div>`; return; }
        const g = SERIES.group({ key: `s${seq}`, xRange: [Math.max(v.from, xs[0]), Math.min(v.to, xs[xs.length - 1] + STEP)], tz: CFG.tz, dateLine: true });
        SERIES.line(el, g, { width: W(), height: p.height || 240, xs, series: lines, points: xs.length <= 120, fmtV: (x) => fmtV(x, p.y) });
      });
    },
    bars(p, v) {
      if (p.layout === "category" || p.layout === "hist") return title(p) + catBars(p, v);
      const { f, rows } = rowsOf(p.facts, v, p.where), st = p.stack || "label", val = p.value || "seconds";
      const devStack = st === "device", seen = new Set(rows.map((r) => r[f.ix[st]]));
      const labs = p.order || (devStack ? DEVS.filter((d) => seen.has(d)) : [...seen]);
      const nm = (l) => (devStack ? label(l) : String(l));   // devices: meta names + one colour per device on every chart
      const pal = devStack ? labs.map((d) => colorOf(d))
        : (p.palette || "sequential") === "sequential" ? labs.map((_, i) => SEQ[Math.round((i * (SEQ.length - 1)) / Math.max(labs.length - 1, 1))]) : labs.map((l) => nameColor(String(l)));
      const sc = { scale: p.scale, unit: p.unit, decimals: p.decimals };
      const legend = `<div class="alegend">${labs.map((l, i) => `<span><i style="background:${pal[i]}"></i>${esc(nm(l))}</span>`).join("")}</div>`;
      if (p.layout === "share") {   // one 100% bar per device
        const byDev = groupBy(rows, f, ["device"]);
        const bars = [...byDev.entries()].map(([k, rs]) => {
          const d = JSON.parse(k)[0], tot = sumA(colOf(f, rs, val)) || 1;
          const seg = labs.map((l, i) => { const x = sumA(colOf(f, rs.filter((r) => r[f.ix[st]] === l), val)); return x ? `<span style="width:${(100 * x) / tot}%;background:${pal[i]}" data-tip="${esc(`${label(d)}: ${nm(l)} ${fmtV(x * (p.scale ?? 1), sc)} (${Math.round((100 * x) / tot)}%)`)}"></span>` : ""; }).join("");
          return `<div class="sb-l" title="${esc(d)}">${esc(label(d))}</div><div class="sb-t">${seg}</div><div class="sb-v">${fmtV(tot * (p.scale ?? 1), sc)}</div>`;
        }).join("");
        return title(p) + `<div class="sharebars">${bars || '<div class="empty">No data in the selection</div>'}</div>` + legend;
      }
      return title(p) + slot((el) => {
        const xs = [...new Set(rows.map((r) => r[f.ix.t]))].sort((a, b) => a - b), xi = new Map(xs.map((x, i) => [x, i]));
        if (!xs.length) { el.innerHTML = `<div class="empty">No data in the selection</div>`; return; }
        const stacks = labs.map((l, i) => {
          const vals = xs.map(() => null);
          for (const r of rows) if (r[f.ix[st]] === l) { const k = xi.get(r[f.ix.t]); vals[k] = (vals[k] || 0) + r[f.ix[val]] * (p.scale ?? 1); }
          return { label: nm(l), color: pal[i], values: vals };
        });
        const g = SERIES.group({ key: `b${seq}`, xRange: [xs[0], xs[xs.length - 1] + STEP], tz: CFG.tz, dateLine: true });
        stackedBars(el, { xs, binS: STEP, stacks, width: W(), height: p.height || 220, xRange: g.xRange, fmtX: (t) => fmt(t, STEP >= 86400 ? "dy" : "dt"),
          fmtY: (x) => fmtV(x, sc), fmtAxisY: smart, tz: CFG.tz, xValues: g.xValues, totalLabel: "Total", syncKey: `b${seq}` });
      });
    },
    episodes(p, v) {
      const { f, rows } = rowsOf(p.facts, v, p.where), ctxTab = TABS.findIndex(([, , , t]) => t && t.panels.some((q) => q.type === "context"));
      // One timeline row per device, or per device + message when a device has several (e.g. one per DTC source)
      const multi = f.ix.message != null && new Set(rows.map((r) => `${r[f.ix.device]}|${r[f.ix.message]}`)).size > new Set(rows.map((r) => r[f.ix.device])).size;
      const byDev = groupBy(rows, f, multi ? ["device", "message"] : ["device"]);
      const tl = title(p) + slot((el) => {
        if (!rows.length) { el.innerHTML = `<div class="empty">No episodes in the selection</div>`; return; }
        const g = SERIES.group({ key: `e${seq}`, xRange: [v.from, v.to], tz: CFG.tz, dateLine: true });
        SERIES.states(el, g, { width: W(), rows: [...byDev.entries()].map(([k, rs]) => ({ label: label(JSON.parse(k)[0]) + (multi ? ` · ${JSON.parse(k)[1]}` : ""), color: VI.red,
          on: rs.map((r) => [r[f.ix.t], Math.max(r[f.ix.end], r[f.ix.t])]) })) });
      });
      const vc = p.value || "max", dur = f.ix.duration_s;
      const tr = rows.map((r) => {
        const a = r[f.ix.t], b = Math.max(r[f.ix.end], a), pad = Math.max(60, (b - a) * 0.5);
        return [r[f.ix.device], { t: fmt(a, "dts"), s: a }, dur != null ? +r[dur].toFixed(1) : "-", r[f.ix[vc]] ?? "-",
          ...(multi ? [r[f.ix.message]] : []), { t: "show", s: 0, html: showLink({ devices: [r[f.ix.device]], from: a - pad, to: b + pad, tab: ctxTab >= 0 ? TABS[ctxTab][0] : null }) }];
      });
      return [tl, table({ title: `${p.title || "Episodes"}: list`, count: true, dev: 0, rows: tr, sort: [1, 1], bars: [2], decimals: { 2: 1 },
        columns: ["Device", "Start", "Duration (s)", `${vc[0].toUpperCase() + vc.slice(1)}${p.unit ? ` (${p.unit})` : ""}`, ...(multi ? ["Message"] : []), ""],
        tips: { "Duration (s)": "First to last matching row (one row = 0 s)" } })];
    },
    context(p, v) {
      const C = R.context || {}, keys = p.signals ? new Set(p.signals) : null;
      const sigs = (C.signals || []).filter((s) => v.devs.has(s.device) && (!keys || keys.has(s.key)));
      const pick = (tiers) => {   // finest tier covering the selection, else the coarsest (and say so)
        const cov = tiers.filter((t) => t.from <= v.from + SNAP && t.to >= v.to - SNAP).sort((a, b) => a.interval - b.interval);
        return cov[0] ? { tier: cov[0], fine: true } : { tier: tiers.slice().sort((a, b) => b.interval - a.interval)[0], fine: false };
      };
      const cut = (t, arrs) => { let a = 0, b = t.length; while (a < b && t[a] < v.from) a++; while (b > a && t[b - 1] > v.to) b--; return arrs.map((x) => x.slice(a, b)); };
      const byLab = new Map();
      for (const s of sigs) { const k = `${s.label}|${s.unit || ""}`; if (!byLab.has(k)) byLab.set(k, []); byLab.get(k).push(s); }
      let coarse = false, minI = Infinity;
      const parts = [];
      // Map: A route line only from detailed data (<= ROUTE_MAX_S per point, e.g. the detail around a finding), broken at
      // time gaps; otherwise one dot per trip start of every selected car (averaged positions minutes or hours apart
      // joined by lines are meaningless). Dots: Colored by the trip's average of the first context signal
      const ROUTE_MAX_S = 60;
      // A gap = a pause longer than 3 buckets, 5x the data's own typical spacing (a message every few seconds in 1 s
      // buckets is no gap) and 10 s
      const gapLimit = (t, iv) => {
        const d = t.slice(1).map((x, i) => x - t[i]).sort((a, b) => a - b);
        return Math.max(3 * iv, 5 * (d.length ? d[d.length >> 1] : iv), 10);
      };
      const fineTier = (t) => { const pk = pick(t.tiers); return pk.fine && pk.tier.interval <= ROUTE_MAX_S ? pk.tier : null; };
      const track = (p.map !== false) && (C.tracks || []).find((t) => v.devs.has(t.device) && fineTier(t));
      const tripSets = p.map !== false ? (C.trips || []).filter((x) => v.devs.has(x.device)) : [];
      const tripsIn = tripSets.flatMap((x) => x.trips.filter((r) => r.t < v.to && r.end > v.from).map((r) => ({ ...r, device: x.device, set: x })));
      let mapRef = null;
      if (track || tripsIn.length) {
        const ts0 = tripSets[0] || {};
        const head = track ? `GNSS route: ${esc(label(track.device))}` : `Trip starts: ${tripsIn.length} trip${tripsIn.length === 1 ? "" : "s"}`;
        const tip = track ? "Route in the selected period from detailed data (resampled to seconds), colored by the signal named below the map; dots = trip starts. The orange dot follows the chart cursor"
          : "One dot per trip start (a trip = continuous data, split at pauses over 5 min), colored by the trip's average. Hover for the details. Routes show when you select a period with detailed data (e.g. via an Overview card)";
        parts.push(`<div class="ttitle">${head}${info(tip)}</div>` + slot((el) => {
          let t = [], lat = [], lon = [], vals = null, marks = null, gapS;
          const cs = track && track.color_by && sigs.find((s) => s.key === track.color_by);
          if (track) {
            const tier = fineTier(track);
            [t, lat, lon] = cut(tier.t, [tier.t, tier.lat, tier.lon]);
            gapS = gapLimit(t, tier.interval);
            if (cs) {   // the color signal at each route point (nearest earlier value)
              const ct = pick(cs.tiers).tier; let j = 0;
              vals = t.map((x) => { while (j + 1 < ct.t.length && ct.t[j + 1] <= x) j++; return ct.t.length && Math.abs(ct.t[j] - x) <= Math.max(ct.interval, tier.interval) * 2 ? ct.v[j] : null; });
            }
            if (p.markers && F[p.markers]) {   // e.g. episodes: a point where each one started
              const mf = rowsOf(p.markers, { devs: new Set([track.device]), from: v.from, to: v.to }).rows, fx = F[p.markers].ix;
              marks = mf.map((r) => ({ t: r[fx.t], label: `${fmt(r[fx.t], "dts")}${fx.duration_s != null ? `, ${(+r[fx.duration_s]).toFixed(0)} s` : ""}${fx.max != null && r[fx.max] != null ? `, max ${smart(r[fx.max])}` : ""}` }));
            }
          }
          const u = (track && cs ? cs.unit : ts0.unit) || "";
          const dots = (track ? tripsIn.filter((r) => r.device === track.device) : tripsIn).map((r) => ({ lat: r.lat, lon: r.lon, v: r.v,
            label: `${label(r.device)}: ${fmt(r.t, "dt")}, ${Math.round((r.end - r.t) / 60)} min` + (r.v != null ? `, average ${smart(r.v)}${u ? " " + u : ""}` : "") }));
          mapRef = TRACK.map(el, { t, lat, lon, v: vals, gapS, dots, marks, label: (track && cs ? cs.label : ts0.label) || "", unit: u });
          MAPS.push(mapRef);
        }));
      }
      const g = SERIES.group({ key: `c${seq}`, xRange: [v.from, v.to], tz: CFG.tz, dateLine: true });
      // Lines only for detailed data (a trip, a day: <= LINE_MAX_S per point); for overviews of resampled data one bar per
      // car and day (week for long selections): A line between averages hours apart suggests a trend that is not there
      const LINE_MAX_S = 300;
      const ivSel = Math.max(0, ...sigs.map((s) => pick(s.tiers).tier.interval));
      const lines = ivSel <= LINE_MAX_S;
      const binS = v.to - v.from > 120 * 86400 ? 7 * 86400 : 86400, per = binS === 86400 ? "daily" : "weekly";
      for (const [k, ss] of byLab) {
        const [lab, unit] = k.split("|");
        const vt = ss[0].values ? "Values: " + Object.entries(ss[0].values).map(([a, b]) => `${a} = ${b}`).join(", ") + ". " : "";
        parts.push(`<div class="ttitle">${esc(lab)}${unit ? ` (${esc(unit)})` : ""}${lines ? "" : `, ${per} average`}${info(vt + (lines ? "Resampled to the resolution shown above" : `The ${per} average per car of the resampled data. Lines show for a trip or a short period with detailed data (e.g. via an Overview card)`))}</div>` + slot((el) => {
          const fv = (x) => (x == null ? "-" : (ss[0].values && ss[0].values[x] != null ? `${x} ${ss[0].values[x]}` : smart(x)) + (unit ? ` ${unit}` : ""));
          const tabs = ss.map((s) => { const { tier, fine } = pick(s.tiers); coarse ||= !fine; minI = Math.min(minI, tier.interval); return [cut(tier.t, [tier.t, tier.v]), tier.interval]; });
          if (!lines) {   // per car: The mean of its resampled values per day / week (UTC periods, as the facts)
            const b0 = Math.floor(v.from / 86400) * 86400, key = (t) => b0 + Math.floor((t - b0) / binS) * binS;
            const per = tabs.map(([[t, y]]) => { const m = new Map(); t.forEach((x, i) => { if (y[i] == null) return; const kk = key(x), e = m.get(kk) || [0, 0]; e[0] += y[i]; e[1]++; m.set(kk, e); }); return m; });
            const xs = [...new Set(per.flatMap((m) => [...m.keys()]))].sort((a, b) => a - b);
            if (!xs.length) { el.innerHTML = `<div class="empty">No values in the selection</div>`; return; }
            SERIES.bars(el, g, { width: W(), height: 190, xs, binS, fmtV: fv,
              series: ss.map((s, i) => ({ label: label(s.device), color: colorOf(s.device), values: xs.map((x) => { const e = per[i].get(x); return e ? e[0] / e[1] : null; }) })) });
            return;
          }
          const gapify = ([t, y], iv) => {   // a null point in every real gap: no line across pauses
            const lim = gapLimit(t, iv), T = [], Y = [];
            t.forEach((x, i) => { if (i && x - t[i - 1] > lim) { T.push(t[i - 1] + iv); Y.push(null); } T.push(x); Y.push(y[i]); });
            return [T, Y];
          };
          const gt = tabs.map(([d, iv]) => gapify(d, iv));
          const data = gt.length > 1 ? uPlot.join(gt) : gt[0];
          if (!data[0].length) { el.innerHTML = `<div class="empty">No values in the selection</div>`; return; }
          SERIES.line(el, g, { width: W(), height: 190, xs: data[0], series: ss.map((s, i) => ({ label: label(s.device), values: data[i + 1], stroke: colorOf(s.device) })),
            fmtV: fv, hooks: { setCursor: [(u) => mapRef && mapRef.setCursor(u.cursor.idx == null ? null : u.data[0][u.cursor.idx])] } });
        }));
      }
      if (!parts.length) return `<div class="empty">No context signals for the selected devices</div>`;
      const nid = `cn${seq++}`;
      DRAW.push(() => {
        const el = document.getElementById(nid);
        if (!el) return;
        const res = minI < Infinity ? `${minI >= 3600 ? minI / 3600 + " h" : minI >= 60 ? minI / 60 + " min" : minI + " s"} per point` : "";
        el.innerHTML = lines ? (coarse ? "The selection is finer than the stored detail: Ask the agent to add detail for this period (context.py --detail) and rebuild the page" : `Resolution: ${res} (resampled)`)
          : `Overview: ${per} averages (data resampled to ${res}). Select a trip or a shorter period for the signal lines, e.g. via an Overview card`;
      });
      return `<div class="fixnote" id="${nid}"></div>` + parts.join("");
    },
    // Event-aligned traces (events.py align --trace): x = seconds from each event's onset (0 = onset line), a thin line
    // per event and a bold median per role (e.g. "problem" = fault onsets, "reference" = the same moment without a
    // fault). Answers "what does this signal do before, at and after the onset, compared with normal"
    aligned(p) {
      return title(p) + slot((el) => {
        const d = p.data || {}, xs = d.t || [], evs = d.events || [];
        if (!xs.length || !evs.length) { el.innerHTML = `<div class="empty">No aligned data</div>`; return; }
        const RC = { problem: VI.red, reference: VI.blue }, roles = [...new Set(evs.map((e) => e.role || "problem"))];
        const col = (r) => RC[r] || CAT[roles.indexOf(r) % CAT.length];
        const rgba = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };
        const med = (a) => { const s = a.filter((x) => x != null).sort((x, y) => x - y); return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : null; };
        const meds = roles.map((r) => { const es = evs.filter((e) => (e.role || "problem") === r); return xs.map((_, i) => med(es.map((e) => e.values[i]))); });
        const rl = (r) => (p.roles && p.roles[r]) || r;
        const nOf = (r) => evs.filter((e) => (e.role || "problem") === r).length;
        const rel = (t) => { const a = Math.abs(t), s = t < 0 ? "-" : t > 0 ? "+" : ""; return a >= 120 ? `${s}${+(a / 60).toFixed(1)} min` : `${s}${+a.toFixed(a < 10 ? 1 : 0)} s`; };
        const series = [{}, ...evs.map((e) => ({ stroke: rgba(col(e.role || "problem"), 0.3), width: 1, spanGaps: false })),
          ...roles.map((r) => ({ stroke: col(r), width: 2.5, spanGaps: false }))];
        const zero = (u) => { const x = u.valToPos(0, "x", true), c = u.ctx; c.save(); c.strokeStyle = "#6b717b"; c.setLineDash([4, 4]); c.beginPath(); c.moveTo(x, u.bbox.top); c.lineTo(x, u.bbox.top + u.bbox.height); c.stroke(); c.restore(); };
        el.innerHTML = `<div class="alleg">${roles.map((r) => `<span><i style="background:${col(r)}"></i>${esc(rl(r))} (${nOf(r)}, bold line = median)</span>`).join("")}<span class="alread"></span></div><div class="alplot"></div>` +
          (p.xlabel !== "" ? `<div class="vbars-x">${esc(p.xlabel || "Time from the onset (dashed line)")}</div>` : "");
        const read = el.querySelector(".alread");
        new uPlot({ width: W(), height: p.height || 220, legend: { show: false }, series,
          scales: { x: { time: false } }, cursor: { points: { show: false }, drag: { x: false, y: false } },
          axes: [{ stroke: "#6b717b", grid: { stroke: "#e6e9ee" }, values: (u, t) => t.map(rel),
            splits: (u, i, mn, mx) => {   // round steps (1 s ... 1 h), ~8 ticks, always through 0
              const st = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600].find((s) => (mx - mn) / s <= 8) || 7200;
              const out = []; for (let t = Math.ceil(mn / st) * st; t <= mx + 1e-9; t += st) out.push(+t.toFixed(3)); return out;
            } }, { stroke: "#6b717b", grid: { stroke: "#e6e9ee" }, size: 56 }],
          hooks: { draw: [zero], setCursor: [(u) => { const i = u.cursor.idx; read.textContent = i == null ? "" : `${rel(xs[i])}: ` + roles.map((r, k) => `${rl(r)} ${fmtV(meds[k][i], p)}`).join(" | "); }] } },
          [xs, ...evs.map((e) => e.values), ...meds], el.querySelector(".alplot"));
      });
    },
    custom(p, v) {
      return title(p) + slot((el) => {
        const fn = ANALYSIS.custom[p.fn];
        if (!fn) { el.innerHTML = `<div class="empty">Custom panel '${esc(p.fn)}' is missing (--custom-js)</div>`; return; }
        fn(el, v, R, p);
      });
    },
  };

  function panel(p, v) {
    const fn = PANELS[p.type];
    if (!fn) return `<div class="empty">Unknown panel type '${esc(p.type)}'</div>`;
    let html;
    try { html = [].concat(fn(p, v)).join(""); } catch (e) { html = `<div class="empty">${esc(e.message)}</div>`; }
    if (p.type === "table" || p.type === "kpis") html = (p.type === "kpis" ? title(p) : "") + html;
    if (p.mode !== "fixed") return html;
    const fx = fixedNote(p, v);
    return `<div class="fixed${fx.cls}">${fx.note}${html}</div>`;
  }

  // ---------------------------------------------------------------- tabs: Overview first, then the spec's tabs
  const prov = R.provenance || {};
  const provText = Object.entries(prov).map(([k, x]) => `${k}: ${typeof x === "object" ? JSON.stringify(x) : x}`).join("\n");
  // Overview, answer first: The question (one muted line), the takeaway as the page title, one hero card (the number
  // that carries the answer + its reference + confidence), the evidence visual, then up to 3 supporting cards. Card
  // values are literals or facts specs, so they follow the filters. No text blocks: The reasoning goes to the chat
  const CARD_C = [VI.orange, VI.green, VI.red, VI.blue];
  // spec: A literal (a number gets fm's unit, decimals and scale: the card's), {value, unit, decimals, scale} or a facts
  // spec {facts, agg, col/cols, where, num, scale, unit, decimals, devices}. Returns the text, the number and its format
  function cardValue(spec, v, base, fm = {}) {
    if (spec != null && typeof spec === "object" && !spec.facts) { fm = { ...fm, ...spec }; spec = spec.value; }
    if (typeof spec === "number") { const x = spec * (fm.scale ?? 1); return { txt: fmtV(x, fm), x, devs: null, fm }; }
    if (spec == null || typeof spec !== "object") return { txt: spec == null ? "" : String(spec), x: null, devs: null, fm };   // text
    let devs = v.devs;
    if (Array.isArray(spec.devices)) devs = new Set(spec.devices.filter((d) => v.devs.has(d)));
    else if (spec.devices === "others") {   // the reference: the selected devices without the card's own
      const own = base || new Set();
      devs = new Set([...v.devs].filter((d) => !own.has(d)));
      if (!devs.size) devs = new Set(DEVS.filter((d) => !own.has(d)));
    }
    const sf = { unit: fm.unit, decimals: fm.decimals, ...spec };   // the facts spec's own format, else the card's
    if (!devs.size) return { txt: "-", x: null, devs, fm: sf };
    const { f, rows } = rowsOf(spec.facts, { ...v, devs }, spec.where);
    const x = agg(f, rows, spec);
    return { txt: fmtV(x, sf), x, devs, fm: sf };
  }
  function card(c, v, hero, color) {
    const val = cardValue(c.value, v, null, { unit: c.unit, decimals: c.decimals, scale: c.scale });
    const rf = c.ref ? { unit: c.ref.unit ?? val.fm.unit, decimals: c.ref.decimals ?? val.fm.decimals, scale: c.ref.scale ?? c.scale } : null;
    const ref = c.ref ? cardValue(c.ref.value ?? c.ref, v, val.devs, rf) : null;
    let delta = "";
    if (ref && val.x != null && ref.x != null && ref.x !== 0) {
      const d0 = c.delta || "ratio", d = d0 !== "diff" && val.x === 0 ? "diff" : d0, r = val.x / ref.x;   // 0 vs ref: "0.0x" says nothing
      const txt = d === "ratio" ? `${r >= 10 ? r.toFixed(0) : r.toFixed(1)}x` : d === "pct" ? `${r >= 1 ? "+" : ""}${Math.round(100 * (r - 1))}%`
        : val.fm.unit === "%" ? `${val.x - ref.x >= 0 ? "+" : ""}${fmtV(val.x - ref.x, { ...val.fm, unit: "" })} pt`   // difference of shares: points
        : `${val.x - ref.x >= 0 ? "+" : ""}${fmtV(val.x - ref.x, val.fm)}`;
      const up = val.x > ref.x, cls = c.good === "higher" ? (up ? "good" : "bad") : c.good === "lower" ? (up ? "bad" : "good") : "";
      delta = `<span class="delta ${cls}" data-tip="${esc(`${c.ref.label || "Reference"}: ${ref.txt}`)}">${txt} vs ${esc(c.ref.label || "reference")}</span>`;
    }
    // Fixed rows (title, value, delta, note), each one line: Cards line up and nothing wraps. The whole card is the
    // "show" link (devices + period + tab of the finding), marked with an arrow in the title row
    const target = c.from || c.devices || c.tab ? esc(JSON.stringify({ devices: c.devices, from: ep(c.from), to: ep(c.to), tab: c.tab })) : "";
    return `<div class="kgroup fcard${hero ? " hero" : ""}${target ? " link" : ""}" style="--c:${COLORS[c.color] || color}"${target ? ` data-show="${target}" title="Show this in the data"` : ""}>` +
      `<div class="ft"><span class="ftt" data-tip="${esc(c.title || "")}">${esc(c.title || "")}</span>${info(c.tip)}${target ? `<span class="farr">&rarr;</span>` : ""}</div>` +
      `<div class="fv">${esc(val.txt)}</div><div class="fd">${delta}</div><div class="fn" data-tip="${esc(c.note || "")}">${esc(c.note || "")}</div></div>`;
  }
  function tabOverview(v) {
    const out = [], A = R.answer;
    const bold = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");   // **key phrase** only, everything else escaped
    const head = (R.question && R.show_question !== false ? `<div class="qline"><b>Question:</b> ${esc(R.question)}</div>` : "") +
      ((R.summary || []).length ? `<div class="cline"><b>Conclusion:</b></div><ul class="exsum">${R.summary.slice(0, 5).map((x) => `<li>${bold(x)}</li>`).join("")}</ul><hr class="exsep">` : "");
    const cards = (A ? [card(A, v, true, VI.blue)] : []).concat((R.findings || []).slice(0, A ? 3 : 4).map((x, i) => card(x, v, false, CARD_C[i % CARD_C.length])));
    if (cards.length) out.push(head + `<div class="fcards" style="--k:${cards.length}">${cards.join("")}</div>` + (A && A.visual ? panel(A.visual, v) : ""));
    for (const p of R.overview || []) out.push(panel(p, v));
    return out.length ? out : [`<div class="empty">No findings</div>`];
  }
  const TABS = [["Overview", "The answer to the question and the key findings: Click a card to see it in the data", tabOverview]]
    .concat((R.tabs || []).map((t) => [t.title, t.tip, (v) => t.panels.map((p) => panel(p, v)), t]));

  // ---------------------------------------------------------------- the app
  const ctl = DASH.app({
    devices: DEVS, devMeta: (i) => NAME[DEVS[i]] || "", T0, T1, step: SNAP, tabs: TABS,
    brushHint: R.activity ? "Data per device and period: Drag to select a period" : "",
    activity: (devs) => [...devs].flatMap((i) => ACT[DEVS[i]] || []),
    compute: (state) => { DRAW = []; MAPS = []; return { devs: new Set([...state.devs].map((i) => DEVS[i])), from: state.from, to: state.to }; },
    empty: (v) => v.devs.size === 0,
    header: (v) => ({ title: (R.answer && R.answer.headline) || R.title || "Analysis", sub: `${DEVS.length === 1 ? "1 device" : v.devs.size === DEVS.length ? `All ${DEVS.length} devices` : `${v.devs.size} of ${DEVS.length} devices`} | ${fmt(v.from, "dt")} to ${fmt(v.to, "dt")} | ${esc(DASH.tzLabel(v.from, v.to))}` +
      `${info(`Filtered panels add up the stored facts (per ${STEP >= 86400 ? "day" : STEP >= 3600 ? "hour" : STEP + " s"}) of the selected devices and period: A period that covers part of a day counts that whole day. Fixed panels show what was computed.\n\n${provText}`)}` }),
    printLine: (v) => `${v.devs.size} device(s), ${fmt(v.from, "dt")} to ${fmt(v.to, "dt")}`,
    afterRender: () => { DRAW.forEach((d) => d()); DRAW = []; },
    footer: `Generated ${R.generated || ""} with the canedge-lake-analysis skill (aggregated results only, no raw data). ${CFG.source || ""}`.trim(),
  });

  function applyShow(o) {
    if (o.devices && o.devices.length) { const s = new Set(o.devices.map((d) => DEVS.indexOf(d)).filter((i) => i >= 0)); if (s.size) ctl.state.devs = s; }
    if (o.tab) { const i = TABS.findIndex(([n]) => n === o.tab); if (i >= 0) ctl.state.tab = i; }
    if (o.from != null && o.to != null) ctl.setRange(Math.max(T0, o.from), Math.min(T1, o.to)); else ctl.render();
    setTimeout(() => MAPS.forEach((m) => { m.map.invalidateSize(); m.fit(); }), 50);
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a.show, .fcard[data-show]");
    if (!a) return;
    e.preventDefault();
    applyShow(JSON.parse(a.dataset.show));
  });
  document.getElementById("tabs").addEventListener("click", () => setTimeout(() => MAPS.forEach((m) => { m.map.invalidateSize(); m.fit(); }), 50));
  ctl.start();
});
