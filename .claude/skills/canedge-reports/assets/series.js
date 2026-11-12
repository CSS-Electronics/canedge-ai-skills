/* Time-series charts for the dashboard kit (uPlot), shared by canedge-config (config impact timeline) and
   canedge-lake-analysis (analysis page). Requires uPlot (assets/uplot) + DASH (dashboard.js: fmt). Times are epoch
   seconds; axes and hovers in local time (CFG.tz, else the PC's zone).

   const g = SERIES.group({ key, xRange: [t0, t1], tz, dateLine })
     One zoom for several charts: Drag across any chart to zoom all of them, double-click to reset; shared cursor.
     dateLine: The date on a second axis line (first tick + midnight) instead of a long first label.
     g.track(u)      add a chart (stackedBars(...).u, SERIES.line, SERIES.states do it themselves)
     g.rangeX()      visible x range (the zoom, else xRange)      g.setZoom([a, b] | null)
     g.cursor, g.scaleX, g.onSelect (setSelect hook), g.xAxis(grid), g.xValues, g.tzDate, g.fmtX: uPlot options for
     charts built by hand
   SERIES.line(el, g, o) -> u     o.xs, o.series [{label, values, stroke, width, dash, stepped, points}], o.width, o.height (225),
                                  o.yRange ([a, b] or () => [a, b]), o.hooks (more uPlot hooks), o.legend (true),
                                  o.fmtV (legend value), o.ySize (56), o.points (dots, for sparse data);
                                  stepped: Hold each value until the next one (step line); points: uPlot points options
                                  of one series (show / filter functions: dots on chosen frames). Times get ms when < 10 s are in view
   SERIES.states(el, g, o) -> u   one row per item with its on periods: o.rows [{label, on: [[a, b, color?]],
                                  color}], o.width, o.rowHeight (22), o.base (row background)
   SERIES.bars(el, g, o) -> u     grouped bars per period: o.xs (period starts), o.binS (period length, s),
                                  o.series [{label, values, color}], o.width, o.height (190), o.fmtV
*/
"use strict";
const SERIES = (function () {
  const ON = "#10cba9", BASE = "#eef1f4", AXIS = "#6b717b", GRID = "#e6e9ee";

  function group(o) {
    const xr = o.xRange, tz = o.tz || undefined;
    let zoom = null;
    const plots = [], rangeX = () => zoom || xr;
    const setZoom = (z) => { zoom = z; const [a, b] = rangeX(); plots.forEach((p) => p.setScale("x", { min: a, max: b })); };
    const onSelect = (u) => {
      if (u.select.width > 4) { const a = u.posToVal(u.select.left, "x"), b = u.posToVal(u.select.left + u.select.width, "x"); u.setSelect({ left: 0, width: 0 }, false); setZoom([a, b]); }
    };
    const SUB = 10;   // visible span below this (s): times with milliseconds
    const hmsMs = (t) => { const tt = Math.round(t * 1000) / 1000, s0 = Math.floor(tt); return `${DASH.fmt(s0, "hms")}.${String(Math.round((tt - s0) * 1000)).padStart(3, "0")}`; };
    const span = () => { const [a, b] = rangeX(); return b - a; };
    const fmtX = (t) => (span() < SUB ? `${DASH.fmt(Math.floor(t), "dy")} ${hmsMs(t)}` : DASH.fmt(t, "dts"));
    const xValues = (u, ticks) => {   // HH:MM (HH:MM:SS for short spans, + .mmm below SUB s); YY/MM/DD on the first tick and at midnight
      const d = (u.scales.x.max || 0) - (u.scales.x.min || 0), sec = d < 600, ms = d < SUB && ticks.some((t) => Math.abs(t - Math.round(t)) > 1e-4);
      const tm = (t) => (ms ? hmsMs(t) : DASH.fmt(t, sec ? "hms" : "hm"));
      if (o.dateLine && d >= 2 * 86400) return ticks.map((t) => DASH.fmt(t, "dy"));   // days: the date only
      if (o.dateLine) return ticks.map((t, k) => tm(t) + (k === 0 || DASH.fmt(t, "hm") === "00:00" ? "\n" + DASH.fmt(t, "dy") : ""));
      return ticks.map((t, k) => (k === 0 || DASH.fmt(t, "hm") === "00:00" ? (ms ? `${DASH.fmt(t, "dy")} ${hmsMs(t)}` : DASH.fmt(t, sec ? "dts" : "dt")) : tm(t)));
    };
    return {
      key: o.key, xRange: xr, tz, rangeX, setZoom, onSelect, fmtX, xValues,
      tzDate: tz ? (ts) => uPlot.tzDate(new Date(ts * 1e3), tz) : undefined,
      xAxis: (grid) => ({ stroke: AXIS, grid: grid ? { stroke: GRID } : { show: false }, values: xValues, ...(o.dateLine ? { size: 58 } : {}),
        space: (u, i, mn, mx) => (mx - mn < SUB ? 96 : 50) }),   // wider ticks for HH:MM:SS.mmm
      scaleX: { time: true, range: rangeX },
      cursor: { sync: { key: o.key }, drag: { x: true, y: false, setScale: false }, points: { show: false } },
      track(u) { plots.push(u); u.over.addEventListener("dblclick", () => setZoom(null)); return u; },
    };
  }

  const hooksWith = (g, extra) => {   // the group's setSelect hook + the chart's own hooks
    const h = { ...(extra || {}) };
    h.setSelect = [g.onSelect, ...(h.setSelect || [])];
    return h;
  };

  function line(el, g, o) {
    const fmtV = o.fmtV || ((v) => (v == null ? "-" : +v.toPrecision(4)));
    const y = o.yRange ? { range: typeof o.yRange === "function" ? o.yRange : () => o.yRange } : {};
    return g.track(new uPlot({ width: o.width, height: o.height || 225, legend: { live: true, show: o.legend !== false }, cursor: g.cursor,
      scales: { x: g.scaleX, y }, axes: [g.xAxis(true), { stroke: AXIS, grid: { stroke: GRID }, size: o.ySize || 56 }], tzDate: g.tzDate,
      series: [{ value: (u, t) => (t == null ? "-" : g.fmtX(t)) }, ...o.series.map((s) => ({ label: s.label, stroke: s.stroke || "#434343",
        width: s.width || 1.5, dash: s.dash, spanGaps: false, points: s.points ? { size: 5, ...s.points } : { show: !!o.points, size: 5 }, value: (u, v) => fmtV(v),
        ...(s.stepped ? { paths: uPlot.paths.stepped({ align: 1 }) } : {}) }))],
      hooks: hooksWith(g, o.hooks) }, [o.xs, ...o.series.map((s) => s.values)], el));
  }

  function states(el, g, o) {
    const rows = o.rows, RH = o.rowHeight || 22;
    const paint = (u) => {
      const ctx = u.ctx, bb = u.bbox, dpr = window.devicePixelRatio || 1, X = (t) => u.valToPos(t, "x", true);
      ctx.save(); ctx.font = `${11 * dpr}px Open Sans, sans-serif`; ctx.textBaseline = "middle"; ctx.textAlign = "left";
      const rh = bb.height / rows.length;
      rows.forEach((r, k) => {
        const y = bb.top + k * rh + 3 * dpr, h = rh - 6 * dpr;
        ctx.fillStyle = o.base || BASE; ctx.fillRect(bb.left, y, bb.width, h);
        const [z0, z1] = g.rangeX();   // clip to the visible (zoomed) range
        for (const [a, b, c] of r.on) if (b > z0 && a < z1) {
          ctx.fillStyle = c || r.color || ON;
          ctx.fillRect(X(Math.max(a, z0)), y, Math.max(1, X(Math.min(b, z1)) - X(Math.max(a, z0))), h);
        }
        const tw = ctx.measureText(r.label).width;   // label on a white tag, readable over the bars
        ctx.fillStyle = "rgba(255,255,255,.85)"; ctx.fillRect(bb.left + 3 * dpr, y + 2 * dpr, tw + 8 * dpr, h - 4 * dpr);
        ctx.fillStyle = "#434343"; ctx.fillText(r.label, bb.left + 7 * dpr, y + h / 2);
      });
      ctx.restore();
    };
    return g.track(new uPlot({ width: o.width, height: rows.length * RH + 62, legend: { show: false }, cursor: g.cursor,
      scales: { x: g.scaleX, y: { range: [0, 1] } }, axes: [g.xAxis(false), { size: 56, values: () => [], grid: { show: false }, ticks: { show: false } }], tzDate: g.tzDate,
      series: [{}, { show: false }], hooks: hooksWith(g, { draw: [paint] }) }, [g.xRange, [null, null]], el));
  }

  // Grouped bars per period (e.g. one bar per car and day): For overviews of resampled data, where a line would
  // suggest a trend between points hours or days apart. Drawn in a draw hook (uPlot keeps axes, cursor, legend, zoom)
  function bars(el, g, o) {
    const n = o.series.length, BS = o.binS, vals = o.series.flatMap((s) => s.values).filter((x) => x != null);
    const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), pad = (hi - lo) * 0.05 || 1;
    const fmtV = o.fmtV || ((v) => (v == null ? "-" : +v.toPrecision(4)));
    const paint = (u) => {
      const ctx = u.ctx, bb = u.bbox, dpr = window.devicePixelRatio || 1, X = (t) => u.valToPos(t, "x", true), Y = (v) => u.valToPos(v, "y", true);
      ctx.save(); ctx.beginPath(); ctx.rect(bb.left, bb.top, bb.width, bb.height); ctx.clip();
      o.xs.forEach((x, i) => {
        const x0 = X(x), w = X(x + BS) - x0, bw = (w * 0.8) / n;
        o.series.forEach((s, k) => {
          const v = s.values[i];
          if (v == null) return;
          const y0 = Y(0), y1 = Y(v);
          ctx.fillStyle = s.color;
          ctx.fillRect(x0 + w * 0.1 + k * bw, Math.min(y0, y1), Math.max(dpr, bw - (bw > 4 * dpr ? dpr : 0)), Math.max(dpr, Math.abs(y1 - y0)));
        });
      });
      ctx.restore();
    };
    return g.track(new uPlot({ width: o.width, height: o.height || 190, legend: { live: true }, cursor: g.cursor,
      scales: { x: g.scaleX, y: { range: () => [lo < 0 ? lo - pad : 0, hi + pad] } },
      axes: [g.xAxis(true), { stroke: AXIS, grid: { stroke: GRID }, size: o.ySize || 56 }], tzDate: g.tzDate,
      series: [{ value: (u, t) => (t == null ? "-" : DASH.fmt(t, BS >= 86400 ? "dy" : "dt")) },
        ...o.series.map((s) => ({ label: s.label, stroke: s.color, fill: s.color, width: 0, paths: () => null, points: { show: false }, value: (u, v) => fmtV(v) }))],
      hooks: hooksWith(g, { draw: [paint] }) }, [o.xs, ...o.series.map((s) => s.values)], el));
  }

  return { group, line, states, bars };
})();
