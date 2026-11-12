/* Config impact page (canedge-config simulate_config.py), built on the shared dashboard kit (canedge-reports
   dashboard.js: DASH): Tabs, tooltips, the CAN ID table and the time format come from the kit. D = the simulation. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const { esc, info, table, drawTables, page } = DASH;
  const mb = (x) => (x === 0 ? "0" : x < 0.001 ? "<0.001" : x >= 10 ? x.toFixed(1) : x >= 0.1 ? x.toFixed(2) : x.toFixed(3));
  const n0 = (x) => Math.round(x).toLocaleString("en-US");
  // Consistent decimals: One count per column / list, from its largest value (never 12.5 next to 13 or 0.10 next to 0.078)
  const fixed = (x, d) => x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const decFor = (max) => (max >= 10 ? 1 : max >= 1 ? 2 : 3);
  const mbD = (x, d) => (x > 0 && x < 0.5 * 10 ** -d ? "<" + (10 ** -d).toFixed(d) : fixed(x, d));
  const hrs = (h) => (h >= 48 ? `${n0(h / 24)} d` : `${h.toFixed(h < 10 ? 1 : 0)} h`);   // short: Both values fit the KPI card
  const B = D.before, A = D.after;
  const TZ = CFG.tz || undefined;

  // CANmod.router report (simulate_config.py on a router config): Title + S1-S4 next to the demuxed channels
  const RCH = (D.router && D.router.channels) || {};
  const chLbl = (c) => (RCH[c] ? `${c} (${RCH[c]})` : c);
  if (D.router) document.querySelector("header h1").textContent = "CANmod.router config impact";
  $("sub").innerHTML = `Device ${esc(D.device)} | ${esc(D.source)} | ${D.recorded_min} min of data | ${D.ids} CAN IDs | Times in ${esc(TZ || "PC local time")}`;
  const AR = `<span class="ar">&rarr;</span>`;
  const crc = c => c ? ` <span style="color:var(--muted)">[crc32: ${esc(c)}]</span>` : "";
  $("cfgs").innerHTML = `${esc(D.baseline_name)}${crc(D.baseline_crc32)} ${AR} ${esc(D.config_name)}${crc(D.config_crc32)}`;
  // Compression turned on by the new config (C, a 50% estimate): The headline, the final bar and the log size KPIs
  // include it; the measured reduction (filters, prescalers, control signal) shows below the headline
  const C = D.compression && D.compression.enabled && D.compression.total_pct != null ? D.compression : null;
  const pctTxt = (x) => { const r = Math.round(x); return r > 0 ? `-${r}%` : r < 0 ? `+${-r}%` : "0%"; };
  $("big").textContent = pctTxt(C ? C.total_pct : D.reduction_pct);
  $("bl").textContent = C ? "log file size per minute, incl. compression" : "log file size per minute";
  if (C) $("big2").innerHTML = `<b>${pctTxt(D.reduction_pct)}</b> excl. compression`;
  else if (D.compression) $("big2").innerHTML = `Compression is off: 50% smaller if enabled`;

  // Before = one blue bar. Measured after = the logged part, followed by the removed parts (faded): Prescalers
  // (orange), filters (red), control signal (grey). With C: A dark measured bar, then the green final bar (compressed)
  const tot = B.mb_per_min, pct = (x) => (100 * x) / tot, dH = decFor(tot);
  const k = D.breakdown, mid = C ? "#434343" : "var(--after)";
  let rows = `<div class="lb">Before</div><div class="track"><span style="width:100%;background:var(--before)"></span></div>
    <div class="val" style="color:var(--before)">${mbD(B.mb_per_min, dH)} MB/min</div>
    <div class="lb">${C ? "Excl. compression" : "After"}</div><div class="track">
      <span style="width:${pct(A.mb_per_min)}%;background:${mid}${C ? ";opacity:.75" : ""}" data-tip="Logged after the update${C ? ", before compression" : ""}: ${mb(A.mb_per_min)} MB/min"></span>
      <span class="gone" style="width:${pct(k.prescaled)}%;background:var(--pre)" data-tip="Cut by prescalers: ${mb(k.prescaled)} MB/min"></span>
      <span class="gone" style="width:${pct(k.rejected)}%;background:var(--drop)" data-tip="Cut by filters (IDs no longer logged): ${mb(k.rejected)} MB/min"></span>
      <span class="gone" style="width:${pct(k.control)}%;background:var(--ctl)" data-tip="Cut by the control signal (logging off): ${mb(k.control)} MB/min"></span></div>
    <div class="val" style="color:${mid}">${mbD(A.mb_per_min, dH)} MB/min</div>`;
  if (C) {
    rows += `<div class="lb">After</div><div class="track">
      <span style="width:${pct(C.mb_per_min)}%;background:var(--after)" data-tip="Logged after the update, compressed: ${mb(C.mb_per_min)} MB/min. ${esc(C.note)}"></span>
      <span class="hatch gone" style="width:${pct(A.mb_per_min - C.mb_per_min)}%" data-tip="${esc(C.note)}"></span></div>
      <div class="val" style="color:var(--after)" data-tip="${esc(C.note)}">${mbD(C.mb_per_min, dH)} MB/min</div>`;
  } else if (D.compression) {   // compression off: A muted "if enabled" hint, not part of the numbers
    const c = D.compression;
    rows += `<div class="lb">+ compression</div><div class="track"><span class="hatch off" style="width:${pct(c.mb_per_min)}%" data-tip="${esc(c.note)}"></span></div>
      <div class="val" style="color:var(--muted)" data-tip="${esc(c.note)}">${mbD(c.mb_per_min, dH)} MB/min</div>`;
  }
  $("cmp").innerHTML = rows;
  $("legend").innerHTML = [["var(--pre)", "Cut by prescalers", 0.28, k.prescaled], ["var(--drop)", "Cut by filters", 0.28, k.rejected], ["var(--ctl)", "Cut by control signal", 0.28, k.control]].filter((x) => x[3] > 0)
    .map(([c, t, o]) => `<span><i style="background:${c};opacity:${o}"></i>${t}</span>`).join("")
    + (C ? `<span><i class="hatch" style="opacity:.28"></i>Cut by compression (estimate)</span>`
      : D.compression ? `<span><i class="hatch off"></i>If compression is enabled</span>` : "");

  function ctlRows() {   // logging on-time + switches (one value if all controlled channels agree, else per channel)
    const rx = D.control.filter((c) => c.controls.includes("rx"));
    const one = (key, f) => { const v = [...new Set(rx.map((c) => c[key]))]; return v.length === 1 ? f(v[0]) : rx.map((c) => `${c.channel} ${f(c[key])}`).join(" / "); };
    const sigs = [...new Set(D.control.map((c) => c.start.signal))];
    return [["Logging on", rx.length ? one("rx_on_pct", (x) => `${x}%`) : "-", "Share of the recorded time in which the control signal lets the channels log"],
      ["Start/stop switches", rx.length ? one("rx_switches", String) : "-", "How often logging switches on or off in this file"],
      ["Signal", `<span style="font-size:13px;font-weight:400" data-tip="${esc(sigs.join(", "))}">${esc(sigs[0].length > 28 ? sigs[0].slice(0, 27) + "..." : sigs[0])}${sigs.length > 1 ? ` +${sigs.length - 1}` : ""}</span>`]];
  }
  const arrow = (a, b) => `${a}${AR}<span class="a">${b}</span>`;
  const group = (title, color, items) => `<div class="kgroup" style="--c:${color}"><div class="gt">${title}</div>${items
    .map(([l, v, tip]) => `<div class="krow"><span class="l">${l}${info(tip)}</span><span class="v">${v}</span></div>`).join("")}</div>`;
  const c = D.counts;
  const aH = C ? C.mb_per_h : A.mb_per_h, inc = C ? ". After = incl. compression (50% estimate)" : "";
  $("kpis").innerHTML =
    group("Log size", "var(--after)", [["MB per hour", arrow(n0(B.mb_per_h), n0(aH)), "Log file size per hour of recorded time (first to last frame of the log file)" + inc],
      ["Time per 8 GB SD", arrow(hrs(8000 / B.mb_per_h), hrs(8000 / aH)), "Recorded time that fits on an 8 GB SD card at this rate (h = hours, d = days)" + inc]]) +
    group("Traffic", "var(--before)", [["Avg FPS", arrow(n0(B.frames_per_s), n0(A.frames_per_s)), "Frames per second logged, all channels, averaged over the recorded time"],
      ["Frames per hour", arrow(`${(B.frames_per_s * 3.6 / 1000).toFixed(1)} M`, `${(A.frames_per_s * 3.6 / 1000).toFixed(1)} M`)]]) +
    group("CAN IDs", "var(--pre)", [["Logged", arrow(B.ids_logged, A.ids_logged), "IDs with at least one logged frame (counted per channel)"],
      ["IDs by decision", [[c.accepted, "accepted", "var(--after)"], [c.prescaled, "prescaled", "var(--pre)"], [c.rejected, "rejected", "var(--drop)"]].map(([n, w, col]) => `<span style="color:${col}" data-tip="${n} IDs ${w}">${n}</span>`).join(" / "), "IDs accepted (green) / prescaled (orange) / rejected (red) by the new config: Details in the Filters tab"]]) +
    (D.has_dbc ? group("DBC", "var(--drop)", [["Decodable share after", `${D.decodable_after_pct}%`, "Share of the size after the update that the device's DBC files can decode"],
      ["Decodable IDs rejected", String(D.decodable_rejected), "IDs the DBC files decode, but the new config no longer logs"]]) : "") +
    (D.control.length ? group("Control signal", "var(--ctl)", ctlRows()) : "");

  const cmax = Math.max(...D.channels.map((x) => x.before)), dC = decFor(cmax);
  $("chs").innerHTML = D.channels.map((x) => `<div class="lb">${esc(chLbl(x.channel))}</div><div class="bars">
      <div class="b" style="width:${(100 * x.before) / cmax}%;background:var(--before)"></div>
      <div class="b" style="width:${(100 * x.after) / cmax}%;background:var(--after)"></div></div>
    <div class="val">${mbD(x.before, dC)} ${AR} <b style="color:#434343">${mbD(x.after, dC)}</b></div>`).join("");

  // CAN ID table (kit table): Grouped BEFORE / AFTER columns on one bar scale, decision buttons, search, sort, pages
  const dbc = D.has_dbc, o = dbc ? 1 : 0, xmax = Math.max(...D.rows.map((r) => r.mb_before)), dT = decFor(xmax);
  const TP = (r) => `<span class="badge tp" data-tip="Transport protocol (${esc(r.tp)}): Multi-frame messages are reassembled from all segments, so do not reject or prescale this ID">tp</span>`;
  const idRows = D.rows.map((r) => [r.channel, { t: r.id, s: r.id, html: `<span class="mono">${esc(r.id)}</span>${r.tp ? TP(r) : ""}` },
    ...(dbc ? [r.dbc ? { t: "yes", s: 1, html: `<span class="ok" data-tip="DBC message: ${esc(r.dbc)}">&#10003;</span>` } : { t: "no", s: 0, html: '<span class="no">&#10007;</span>' }] : []),
    r.fps_before, r.mb_before, r.fps_after, r.mb_after, { t: r.fate, s: r.fate, html: `<span class="badge f-${r.fate}">${r.fate}</span>` }, { t: r.rule, s: r.rule, tip: r.rule }]);
  const tipMb = "Log file size per minute taken by the ID (frames x MF4 record weight by data length, as in the CANedge config editor)";
  $("idtable").innerHTML = table({ title: "CAN IDs", count: true, rows: idRows, sort: [3 + o, -1], search: "Filter by channel, ID or rule",
    bars: [3 + o, 5 + o], barColors: { [3 + o]: "var(--before)", [5 + o]: "var(--after)" }, barMax: xmax,
    decimals: { [2 + o]: 1, [3 + o]: dT, [4 + o]: 1, [5 + o]: dT },
    groups: [["", 2 + o], ["BEFORE", 2, "var(--before)"], ["AFTER", 2, "var(--after)"], ["", 2]],
    filters: { col: 6 + o, values: [["", "All"], ["accepted", "Accepted"], ["prescaled", "Prescaled"], ["rejected", "Rejected"]] },
    titleTip: "Every CAN ID per channel, largest size before first. Click a column to sort; the buttons filter by decision",
    columns: ["Channel", "ID", ...(dbc ? ["DBC"] : []), "FPS", "MB/min", "FPS", "MB/min", "Decision", "Rule"],
    tips: { DBC: "Decoded by the device's DBC files (green check, hover it for the message) or not (red cross)", FPS: "Frames per second, averaged over the recorded time",
      "MB/min": tipMb, Decision: "Accepted = logged as is, prescaled = fewer frames, rejected = not logged (CANedge Docs terms)",
      Rule: "The first matching filter in the new config (first match wins)" } });

  // Tabs: Overview, Filters, Explore (config-explore.js), Control signal (only with a control signal), Diff (only with a baseline)
  page([["overview", "Overview", "Combined impact of the new config on the log file size"], ["filters", "Filters", "Every CAN ID before and after, with the filter decision"]]
    .concat(D.explore && D.explore.ids.length ? [["explore", "Explore", "Pick a CAN ID and see each signal as recorded vs as logged under a rule you set (prescalers, reject)"]] : [])
    .concat(D.control.length ? [["control", "Control signal", "The control signal value and when each channel logs"]] : [])
    .concat(D.diff ? [["diff", "Diff", "Line by line changes from the original to the new Configuration File"]] : []));
  drawTables();
  $("t-size").insertAdjacentHTML("beforeend", info("Log file size per minute per channel, before (blue) and after (green) the update"));
  if (D.control.length) {
    $("t-ctl").insertAdjacentHTML("beforeend", info("The control signal value in this log file with the start and stop ranges: Logging starts when the value enters the start range and stops when it enters the stop range"));
    $("t-onoff").insertAdjacentHTML("beforeend", info("Per controlled channel: Green = logging (or the transmit list) is on"));
  }
  // Diff: The same rendering as the config editor's "Review changes" (diff2html, side by side)
  if (D.diff) {
    const df = D.diff;
    $("t-diff").insertAdjacentHTML("beforeend", info("Changes from the original (left) to the new Configuration File (right), as in the config editor's Review changes. Whitespace changes are ignored; passwords and keys show as ***. The CRC32 identifies each file (the device writes it to every log file header)"));
    $("dhdr").innerHTML = `${esc(D.baseline_name)} [crc32: ${df.crc32[0]}] ${AR} ${esc(D.config_name)} [crc32: ${df.crc32[1]}]` +
      ` <span class="ar">|</span> <span class="add">+${n0(df.added)}</span> / <span class="del">-${n0(df.removed)}</span> lines`;
    $("diff").innerHTML = DIFF.html(df.text, "No changes: The two files have the same settings");
  }

  // Timeline: FPS per channel before (blues) / after (greens), control signal values with the start (green) and
  // stop (red) ranges, and when each controlled channel logs / transmits. All charts share the cursor
  const T = D.timeline;
  if (!T) $("tlwrap").style.display = "none";
  else {
    const W = Math.max(300, $("tl").clientWidth - 4), xr = [T.xs[0], T.xs[T.xs.length - 1] + T.bin_s];
    const BLUES = ["#3d85c6", "#9fc5e8", "#1f5a8c", "#6fa8dc"], GREENS = ["#10cba9", "#8de8d6", "#0b8f77", "#4fd8bd"];
    const sum = (a) => a.reduce((s, x) => s + (x || 0), 0);
    const chans = T.channels.slice().sort((a, b) => sum(T.before[b]) - sum(T.before[a]));   // busiest channel at the bottom
    // Shared kit (canedge-reports series.js): One zoom for all timeline charts (drag across any chart to zoom all of
    // them, double-click to reset), shared cursor, local-time axes
    const g = SERIES.group({ key: "tl", xRange: xr, tz: TZ }), rangeX = g.rangeX;
    const add = (cap, where = "tl") => {   // short chart label + chart container
      const d = document.createElement("div");
      $(where).insertAdjacentHTML("beforeend", cap ? `<div class="cap">${cap}</div>` : ""); $(where).appendChild(d); return d;
    };
    // Periods where every controlled channel stops logging: grey shade on the "after" chart
    const rx = T.states.filter((s) => s.label.endsWith("logging"));
    function offShade(u) {
      if (!rx.length) return;
      const ctx = u.ctx, bb = u.bbox, X = (t) => u.valToPos(t, "x", true);
      ctx.save(); ctx.fillStyle = "rgba(107,113,123,.13)";
      let t = xr[0];
      const on = rx.flatMap((s) => s.on).sort((a, b) => a[0] - b[0]);
      for (const [a, b] of on.concat([[xr[1], xr[1]]])) { if (a > t) ctx.fillRect(X(t), bb.top, X(a) - X(t), bb.height); t = Math.max(t, b); }
      ctx.restore();
    }
    // One y scale for before and after: The highest stacked total of either
    const yMax = Math.max(1, ...["before", "after"].flatMap((key) => T.xs.map((_, i) => chans.reduce((s, c) => s + (T[key][c][i] || 0), 0))));
    $("t-fps").insertAdjacentHTML("beforeend", info("Stacked frames per second per channel (busiest channel at the bottom), before and after the update, on the same scale"
      + (rx.length ? ". Grey in the After chart = logging off (control signal)" : "") + ". Hover for the values; drag across any chart to zoom all charts, double-click to reset"));
    for (const [key, pal, cap] of [["before", BLUES, "Before"], ["after", GREENS, "After"]]) {
      g.track(stackedBars(add(cap), { xs: T.xs, binS: T.bin_s, width: W, height: 150, xRange: xr, xRangeFn: rangeX, yMax, fmtX: g.fmtX, fmtY: (x) => fixed(x, yMax >= 100 ? 0 : 1), fmtAxisY: n0, tz: TZ, xValues: g.xValues,
        stacks: chans.map((c, i) => ({ label: c, color: pal[i % pal.length], values: T[key][c] })), totalLabel: "Total", syncKey: "tl",
        hooks: key === "after" ? { draw: [offShade], setSelect: [g.onSelect] } : { setSelect: [g.onSelect] } }).u);
    }
    for (const sg of T.signals) {   // control signal value (mean per bar) + trigger ranges
      const vals = sg.values.filter((v) => v != null), lo = Math.min(...vals), hi = Math.max(...vals), span = Math.max(hi - lo, 1e-9);
      const near = (x) => x >= lo - span && x <= hi + span;   // show thresholds near the data (not e.g. 1000 m/s)
      const yr = [Math.min(lo, ...sg.ranges.flatMap((r) => r.slice(0, 2)).filter(near)), Math.max(hi, ...sg.ranges.flatMap((r) => r.slice(0, 2)).filter(near))];
      const pad = (yr[1] - yr[0]) * 0.08 || 1;
      // Start (green) / stop (red) ranges: The wider range first, the inner one on top in a solid tint, so nested
      // ranges show what the device does (CANedge Docs: the inner range takes precedence). Dashed lines at each
      // threshold within the visible scale (e.g. not at 1000 m/s)
      const COL = { start: ["#d3f5ee", "#10cba9"], stop: ["#ffe0e0", "#ff6666"] };
      const bands = (u) => {
        const ctx = u.ctx, bb = u.bbox, dpr = window.devicePixelRatio || 1;
        const lo = yr[0] - pad, hi = yr[1] + pad, Y = (v) => u.valToPos(Math.max(lo, Math.min(hi, v)), "y", true);
        ctx.save(); ctx.beginPath(); ctx.rect(bb.left, bb.top, bb.width, bb.height); ctx.clip();
        for (const [a, b, w] of sg.ranges.slice().sort((x, y) => (y[1] - y[0]) - (x[1] - x[0]))) {
          ctx.fillStyle = COL[w][0]; ctx.fillRect(bb.left, Y(b), bb.width, Y(a) - Y(b));
        }
        ctx.setLineDash([5 * dpr, 4 * dpr]); ctx.lineWidth = 1.5 * dpr;
        for (const [a, b, w] of sg.ranges) for (const v of [a, b]) {
          if (v < lo || v > hi) continue;
          ctx.strokeStyle = COL[w][1]; ctx.beginPath(); ctx.moveTo(bb.left, Y(v)); ctx.lineTo(bb.left + bb.width, Y(v)); ctx.stroke();
        }
        ctx.restore();
      };
      const ranges = sg.ranges.map(([a, b, w]) => `${w === "start" ? "Start" : "Stop"} range ${a} to ${b}`).join(", ");
      SERIES.line(add(`${esc(sg.label)}${info(`${sg.detail ? sg.detail + ". " : ""}${ranges}. Value = average per bar. Where the ranges are nested, the inner one takes precedence (CANedge Docs)`)}`, "tlctl"), g,
        { width: W, height: 225, xs: T.xs.map((x) => x + T.bin_s / 2), series: [{ label: sg.label, values: sg.values }],
          yRange: () => [yr[0] - pad, yr[1] + pad], hooks: { drawAxes: [bands] } });
      // Legend: The ranges (+ the hysteresis gap between non-overlapping start / stop ranges)
      const st = sg.ranges.filter((r) => r[2] === "start"), sp = sg.ranges.filter((r) => r[2] === "stop");
      const gap = st.length && sp.length && (st[0][0] > sp[0][1] || sp[0][0] > st[0][1]);
      $("tlctl").insertAdjacentHTML("beforeend", `<div class="legend ctl">${sg.ranges.map(([a, b, w]) =>
        `<span><i style="background:${COL[w][0]};border:1px dashed ${COL[w][1]}"></i>${w === "start" ? "Start" : "Stop"}: ${a} to ${b}</span>`).join("")}${
        gap ? `<span><i style="background:#fff;border:1px solid #cbcbcb"></i>Gap: no change (hysteresis)</span>` : ""}</div>`);
    }
    if (T.states.length) SERIES.states(add("", "tlonoff"), g, { width: W, rows: T.states });   // one row per controlled channel: green = on
  }

  $("notes").innerHTML = D.notes.map((x) => `<li>${esc(x)}</li>`).join("");
  $("foot").textContent = `Generated ${D.generated} with the canedge-config skill (frame-by-frame simulation of the Configuration File on the raw data). Data processed locally with the CSS Electronics MF4 tools and open source Python.`;
})();
