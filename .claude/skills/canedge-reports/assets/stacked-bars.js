/* Stacked bar chart (uPlot) for frames/s per channel, shared by canedge-raw-overview (FPS tab), canedge-s3-overview,
   canedge-config (config impact timeline) and canedge-lake-analysis. Bars start at each point and span binS seconds; a 1 px white line separates the
   stacked segments. Requires uPlot (canedge-reports/assets/uplot).

   stackedBars(el, o) -> {u, cum, total}
     o.xs              bar start times (s)
     o.binS            bar width (s)
     o.stacks          [{label, color, values}] bottom first; null = no bar
     o.width, o.height chart size (px)
     o.xRange          [x0, x1] visible range (a small margin is added)
     o.xRangeFn        optional () => [min, max] for the x scale instead (e.g. a zoom shared by several charts)
     o.yMax            fixed y maximum (e.g. shared by a before and an after chart); default: the data maximum
     o.fmtX, o.fmtY    legend / axis formatters; o.xIncrs + o.xValues: x axis ticks
     o.totalLabel      adds a legend-only series with the stack total (e.g. "Total")
     o.extra           [{label, stroke, values, scale}] more legend-only series (e.g. a peak on its own scale)
     o.tz              IANA time zone for the axis (optional)
     o.syncKey         cursor sync key (charts with the same key share the cursor)
     o.hooks           extra uPlot hooks (draw hooks run after the segment lines)
*/
function stackedBars(el, o) {
  const xs = o.xs, BS = o.binS, raw = o.stacks.map((s) => s.values);
  // Running sum per bar: A segment without a value (null) adds nothing, but the segments above still stack on the
  // ones below it (x + null would drop them: wrong bar heights and a total of only the top segment)
  const base = xs.map(() => 0), seen = xs.map(() => false), cum = [];
  raw.forEach((r) => cum.push(r.map((x, i) => (x == null ? null : ((seen[i] = true), (base[i] += x))))));
  const top = o.stacks.length - 1, total = base.map((b, i) => (seen[i] ? b : null));
  const none = () => null;
  const [x0, x1] = o.xRange, pad = Math.max(BS, (x1 - x0) * 0.008);
  const pxPerBar = (o.width * BS) / (x1 - x0 + 2 * pad);   // white 1 px gap between bars when they are wide enough to see points
  const bars = uPlot.paths.bars({ size: [1, Infinity, 2], gap: pxPerBar >= 4 ? 1 : 0, align: 1 });   // bar starts at the point; min 2 px
  const val = (arr) => (u, x, si, i) => (i == null || arr[i] == null ? "-" : o.fmtY(arr[i]));   // legend: Own value, not the stack sum
  const series = [{ value: (u, t) => (t == null ? "-" : o.fmtX(t)) }], data = [xs];
  for (let k = top; k >= 0; k--) {   // top of the stack first, lower segments drawn over it
    const s = o.stacks[k];
    series.push({ label: s.label, fill: s.color, stroke: s.color, width: 0, paths: bars, points: { show: false }, value: val(raw[k]) });
    data.push(cum[k]);
  }
  if (o.totalLabel) { series.push({ label: o.totalLabel, stroke: "#434343", paths: none, points: { show: false }, value: val(total) }); data.push(total); }
  const scales = { x: { time: true, range: o.xRangeFn || (() => [x0 - pad, x1 + pad]) },   // small margin: bars at the edges stay fully visible
    y: { range: (u, mn, mx) => [0, (o.yMax || mx || 1) * 1.05] } };   // o.yMax: same scale across charts
  for (const e of o.extra || []) {
    series.push({ label: e.label, scale: e.scale || "y", stroke: e.stroke, paths: none, points: { show: false }, value: val(e.values) });
    data.push(e.values);
    if (e.scale) scales[e.scale] = { auto: true };
  }
  function segLines(u) {   // 1 px white line between the stacked channels of each bar (visual separation)
    const ctx = u.ctx, bb = u.bbox, dpr = window.devicePixelRatio || 1;
    ctx.save(); ctx.beginPath(); ctx.rect(bb.left, bb.top, bb.width, bb.height); ctx.clip();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = dpr; ctx.beginPath();
    for (let k = 1; k < cum.length; k++) {
      for (let i = 0; i < xs.length; i++) {
        const below = cum[k][i] == null ? 0 : cum[k][i] - raw[k][i];   // top of the segments under this one
        if (!raw[k][i] || !(below > 0)) continue;   // only where a segment sits on another one
        const y = Math.round(u.valToPos(below, "y", true)) + 0.5;
        ctx.moveTo(u.valToPos(xs[i], "x", true), y); ctx.lineTo(u.valToPos(xs[i] + BS, "x", true), y);
      }
    }
    ctx.stroke(); ctx.restore();
  }
  const hooks = Object.assign({}, o.hooks || {});
  hooks.draw = [segLines].concat(hooks.draw || []);
  const opts = {
    width: o.width, height: o.height || 200, series, legend: { live: true }, scales,
    axes: [{ stroke: "#6b717b", grid: { stroke: "#e6e9ee" }, incrs: o.xIncrs, values: o.xValues },
      { stroke: "#6b717b", grid: { stroke: "#e6e9ee" }, size: 56, values: (u, t) => t.map((x) => o.fmtAxisY ? o.fmtAxisY(x) : x) }],
    cursor: { drag: { x: true, y: false, setScale: false }, sync: { key: o.syncKey || "fps" }, points: { show: false } },
    hooks,
  };
  if (o.tz) opts.tzDate = (ts) => uPlot.tzDate(new Date(ts * 1e3), o.tz);
  return { u: new uPlot(opts, data, el), cum, total };
}
