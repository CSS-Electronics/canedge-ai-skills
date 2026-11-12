/* S3 bucket overview dashboard (canedge-s3-overview), built on the shared dashboard kit (canedge-reports dashboard.js:
   DASH). FACTS: Per device totals + files/bytes per UTC day, by logging day ('Timestamp' metadata) and by upload day
   (S3 LastModified). All times are UTC epoch seconds. */
"use strict";
(function () {
  const F = FACTS, DEVS = F.devices, D = F.dev, AS_OF = F.as_of, N = F.days, CUT = AS_OF - N * 86400;
  const { DAY, VI, fmt, fmtUtcDay, tzLabel, isoUtc, esc, info, num, fmtH, sizeTxt, table, kgroups } = DASH;
  const TZ = CFG.tz || undefined;
  const SERIES = ["#3d85c6", "#ff9900", "#10cba9", "#ff6666", "#7b61c9", "#4f5b66", "#f2c14e", "#2fa4d6", "#c3c8cf"];
  const MAX_HM = 20, MAX_STACK = 8;   // heatmap rows; devices stacked in the per-day charts (the rest = "Other")
  const gb = (b) => b / 1e9;

  // ------------------------------------------------------------------ definitions (hover texts)
  const DEF = {
    log: "Logging time = when the data was recorded: The 'Timestamp' metadata the CANedge adds to each log file on upload (the start of the file)",
    up: "Upload time = when the file arrived in S3 (S3 'LastModified'). A device can upload old data late, e.g. after a period without connectivity",
    notLog: `No log file started in the ${N} days before ${fmt(AS_OF)} (logging time): The device may be off or not recording (e.g. a parked vehicle). It can still upload older files`,
    notUp: `No upload in the ${N} days before ${fmt(AS_OF)} (upload time): The device may be off or without connectivity, or its S3 settings changed`,
  };
  DEF.sync = "Config sync (as in CANcloud): The CRC32 of the device's Configuration File in S3 (<device>/config-XX.YY.json) equals the config CRC32 the device reports in its device.json, i.e. the device runs the config that is in S3. Not synced: The device has not loaded a changed config yet (e.g. it was offline since), or the file in S3 was edited";
  DEF.sd = "SD card space used, from the device's device.json (space_used_mb). With cyclic logging the card can be full by design: The oldest log files are then overwritten";
  DEF.hb = "When the device last uploaded its device.json (S3 LastModified): The device sends it as a heartbeat while it is online";
  const ST = (d) => D[d].status || {};
  const syncOf = (d) => (D[d].status ? D[d].status.sync : null);
  const sdPct = (d) => (ST(d).sd_total_mb ? (100 * ST(d).sd_used_mb) / ST(d).sd_total_mb : null);
  const fwOf = (d) => ST(d).fw || fwLatest(d);
  // Time since an event, to the report time: One unit per column (days, or hours if all are within 48 h), 1 decimal
  function since(ts, what) {
    const hrs = ts.filter(Boolean).map((x) => Math.max(0, (AS_OF - x) / 3600)), days = Math.max(0, ...hrs) >= 48;
    return { unit: days ? "d" : "h", cell: (x) => { if (!x) return "-"; const h = Math.max(0, (AS_OF - x) / 3600), v = Math.round((days ? h / 24 : h) * 10) / 10;
      return { t: v, s: v, tip: `${what}: ${fmt(x)}` }; } };
  }
  const SINCE_TIP = (what) => `Time from the ${what} to the report time (${fmt(AS_OF)}): Days, or hours if all devices are within 48 h. Hover for the date`;
  const notLogging = (d) => !D[d].last_log || D[d].last_log < CUT;
  const notUploading = (d) => !D[d].last_up || D[d].last_up < CUT;
  const devTip = (d) => (D[d].meta ? `Meta: ${D[d].meta}` : "Meta: Not set");
  const listTxt = (ds) => (ds.length <= 2 ? ds.map((d) => DEVS[d]).join(", ") : `${DEVS[ds[0]]} +${ds.length - 1} more`);
  const fwLatest = (d) => (D[d].fw.length ? D[d].fw[D[d].fw.length - 1][0] : null);
  const fwTip = (d) => "Firmware versions (Fw metadata, newest last):\n" + D[d].fw.map(([f, n, a, b]) =>
    `${f}: ${num(n)} files${a ? `, ${fmt(a, "dy")} to ${fmt(b, "dy")}` : ""}`).join("\n");

  // ------------------------------------------------------------------ period + filtered view
  const T0 = Math.min(...(F.day.t.length ? F.day.t : F.up.t)), T1 = Math.max(...F.day.t, ...F.up.t) + DAY;
  const HAS_META = D.some((x) => x.meta), NO_TS = D.some((x) => x.no_ts), USE_GB = Math.max(0, ...D.map((x) => x.bytes)) >= 1e9;
  const ALL_META = Object.fromEntries(D.filter((x) => x.meta).map((x) => [x.id, x.meta]));
  let ctl;
  function compute(state) {
    const { devs, from, to } = state, per = {};
    const P = (d) => (per[d] = per[d] || { files: 0, bytes: 0, days: new Set(), upFiles: 0, upBytes: 0 });
    for (let i = 0; i < F.day.t.length; i++) {
      const d = F.day.dev[i], t = F.day.t[i];
      if (!devs.has(d) || t < from || t >= to) continue;
      const p = P(d); p.files += F.day.files[i]; p.bytes += F.day.bytes[i]; p.days.add(t);
    }
    for (let i = 0; i < F.up.t.length; i++) {
      const d = F.up.dev[i], t = F.up.t[i];
      if (!devs.has(d) || t < from || t >= to) continue;
      const p = P(d); p.upFiles += F.up.files[i]; p.upBytes += F.up.bytes[i];
    }
    const sel = [...devs].sort((a, b) => (DEVS[a] < DEVS[b] ? -1 : 1));
    if (HAS_META) DASH.setMeta(ALL_META,
      "The device's meta name (Configuration File general.device.meta) from its device.json in the bucket: The current value, as of the device's last heartbeat upload");
    return { per, sel, withData: sel.filter((d) => per[d] && per[d].files) };
  }
  const pd = (v, d) => v.per[d] || { files: 0, bytes: 0, days: new Set(), upFiles: 0, upBytes: 0 };

  // ------------------------------------------------------------------ Overview: KPI groups
  function kpis(v) {
    const s = ctl.state, P = v.sel.map((d) => pd(v, d)), sum = (k) => P.reduce((x, p) => x + p[k], 0);
    const days = new Set(P.flatMap((p) => [...p.days])).size, nDays = Math.max(1, Math.round((s.to - s.from) / DAY));
    const newest = (k) => v.sel.reduce((m, d) => (D[d][k] && (m == null || D[d][k] > D[m][k]) ? d : m), null);
    const lastLog = newest("last_log"), lastUp = newest("last_up");
    const nl = v.sel.filter(notLogging), nu = v.sel.filter(notUploading);
    const tot = (k) => v.sel.reduce((x, d) => x + D[d][k], 0), unread = tot("unread");
    const known = v.sel.filter((d) => syncOf(d) != null), unsynced = known.filter((d) => syncOf(d) === false);
    const sd = v.sel.map((d) => [d, sdPct(d)]).filter(([, x]) => x != null), sdMax = sd.reduce((m, e) => (!m || e[1] > m[1] ? e : m), null);
    const fwNow = new Map();
    for (const d of v.sel) { const f = fwOf(d); if (f) fwNow.set(f, (fwNow.get(f) || 0) + 1); }
    const fwList = [...fwNow].sort((a, b) => (a[0] < b[0] ? 1 : -1));
    return kgroups([
      { title: "Scope", c: VI.blue, k: [
        { l: "Devices", v: num(v.withData.length), n: v.withData.length === v.sel.length ? "all with data" : `${num(v.sel.length - v.withData.length)} without data`,
          tip: "Selected devices (device folders in the bucket) with log files in the period (logging time). Devices without data in the period are still listed in the Devices tab" },
        { l: "Log files", v: num(sum("files")), n: sizeTxt(sum("bytes")),
          tip: `Log files of the selected devices that started in the period. ${DEF.log}` + (unread ? `. ${num(unread)} older files (uploaded before the period the report reads) are in the bucket totals of the Devices tab only` : "") },
        { l: "Period", v: fmtH((s.to - s.from) / 3600), n: `${fmtUtcDay(s.from)} to ${fmtUtcDay(s.to - 1)}`, tip: "The selected period (UTC days): Change it with the date fields or by dragging on the strip above" }] },
      { title: "Logging", c: VI.green, k: [
        { l: "Days with data", v: num(days), n: `out of ${num(nDays)} days`, tip: "Calendar days (UTC) on which a selected device started at least one log file (logging time)" },
        { l: "Last logged", v: lastLog != null ? fmt(D[lastLog].last_log, "dy") : "-", n: lastLog != null ? DEVS[lastLog] : "", ntip: lastLog != null ? devTip(lastLog) : "",
          tip: `Start of the newest log file of the selected devices, whatever the period. ${DEF.log}` },
        { l: `Not logging (${N} d)`, v: num(nl.length), n: nl.length ? listTxt(nl) : "all logging", ntip: nl.map((d) => DEVS[d]).join(", "), tip: `Devices with ${DEF.notLog[0].toLowerCase()}${DEF.notLog.slice(1)}` }] },
      { title: "Uploads", c: VI.orange, k: [
        { l: "Uploaded", v: num(sum("upFiles")), n: sizeTxt(sum("upBytes")), tip: `Log files of the selected devices uploaded in the period. ${DEF.up}` },
        { l: "Last upload", v: lastUp != null ? fmt(D[lastUp].last_up, "dy") : "-", n: lastUp != null ? DEVS[lastUp] : "", ntip: lastUp != null ? devTip(lastUp) : "",
          tip: `Newest upload of the selected devices, whatever the period. ${DEF.up}` },
        { l: `Not uploading (${N} d)`, v: num(nu.length), n: nu.length ? listTxt(nu) : "all uploading", ntip: nu.map((d) => DEVS[d]).join(", "), tip: `Devices with ${DEF.notUp[0].toLowerCase()}${DEF.notUp.slice(1)}` }] },
      { title: "Device status", c: VI.red, k: [
        { l: "Firmware", v: num(fwNow.size), n: fwList.length ? `newest ${fwList[0][0]}` : "-", ntip: fwList.map(([f, n]) => `${f}: ${n} device${n > 1 ? "s" : ""}`).join("\n"),
          tip: "Firmware versions the selected devices run now (device.json; devices without one: the newest log file). Per device: Meta tab" },
        { l: "Config synced", v: known.length ? `${known.length - unsynced.length}/${known.length}` : "-", n: !known.length ? "no device.json" : unsynced.length ? listTxt(unsynced) + " not synced" : "all synced",
          ntip: unsynced.map((d) => DEVS[d]).join(", "), tip: DEF.sync },
        { l: "SD storage used", v: sd.length ? `${Math.round(sd.reduce((x, e) => x + e[1], 0) / sd.length)}%` : "-", n: sdMax ? `max ${Math.round(sdMax[1])}% ${DEVS[sdMax[0]]}` : "no device.json",
          tip: DEF.sd + ". Value = the average of the selected devices" }] },
    ]);
  }

  // ------------------------------------------------------------------ Devices
  const BADGE = { log: `<span class="badge b-meta" data-tip="${esc(DEF.notLog)}">no logs</span>`,
    up: `<span class="badge b-cfg" data-tip="${esc(DEF.notUp)}">no uploads</span>`, cfg: `<span class="badge b-fw" data-tip="${esc(DEF.sync)}">not synced</span>` };
  const LABEL = { log: "no logs", up: "no uploads", cfg: "not synced" };   // badge text (column width + search)
  function tabDevices(v) {
    const noTs = NO_TS, delay = F.delays, su = since(D.map((x) => x.last_up), "Last upload");   // all devices: columns do not move
    const useGb = USE_GB, SZ = useGb ? "GB" : "MB";
    const rows = v.sel.map((d) => {
      const p = pd(v, d), x = D[d], fl = fwLatest(d), st = [notLogging(d) ? "log" : "", notUploading(d) ? "up" : "", syncOf(d) === false ? "cfg" : ""].filter(Boolean);
      const fw = !fl ? "-" : x.fw.length > 1 ? { t: fl, s: fl, w: 17, html: `${esc(fl)} <span class="badge b-fw" data-tip="${esc(fwTip(d))}">+${x.fw.length - 1}</span>` } : fl;
      return { flag: st.length > 0, cells: [DEVS[d], p.files, useGb ? Math.round(gb(p.bytes) * 100) / 100 : Math.round(p.bytes / 1e5) / 10, p.days.size,
        x.last_log ? { t: fmt(x.last_log, "dy"), s: x.last_log } : "-", su.cell(x.last_up), fw,
        ...(noTs ? [x.no_ts] : []), ...(delay ? [x.delay_min != null ? { t: fmtH(x.delay_min / 60), s: x.delay_min, num: true } : "-"] : []),
        { t: st.map((k) => LABEL[k]).join(" "), s: st.length, w: 27, html: st.map((k) => BADGE[k]).join(" ") }] };
    });
    return table({ title: "Devices", dev: 0, count: true, rows, bars: [2, 5], decimals: { 2: useGb ? 2 : 1, 5: 1 }, sort: [0, 1], search: "Filter by device or meta",
      toggle: { label: "Only devices with a status" }, barColor: VI.blue,
      titleTip: `One row per device folder in the bucket. Files, ${SZ} and Days: Log files that started in the selected period. Last logged / Upload age: Whatever the period`,
      columns: ["Device", "Files", SZ, "Days", "Last logged", `Upload age (${su.unit})`, "FW", ...(noTs ? ["No Timestamp"] : []), ...(delay ? ["Upload delay"] : []), "Status"],
      tips: { Files: "Log files that started in the period (logging time)", [SZ]: "Size of these log files", Days: "Calendar days (UTC) with at least one log file started",
        "Last logged": `Start of the device's newest log file. ${DEF.log}`, [`Upload age (${su.unit})`]: `${SINCE_TIP("device's newest upload")}. ${DEF.up}`,
        FW: "Firmware of the newest log file (Fw metadata). +N = more versions in the bucket: Hover the badge, or see the Meta tab",
        "No Timestamp": "Log files without the Timestamp metadata (no logging time): Not in Files/GB/Days",
        "Upload delay": "Median time from the start of a log file (Timestamp) to its upload (LastModified). Includes the file's own length",
        Status: `no logs / no uploads: Nothing logged / uploaded in the ${N} days before ${fmt(AS_OF)}. not synced: The device does not run the Configuration File that is in S3 (Meta tab)` } });
  }

  // ------------------------------------------------------------------ Activity: heatmap + per-day charts
  function heatmapSvg(v) {
    const s = ctl.state, d0 = Math.floor(s.from / DAY) * DAY, nd = Math.max(1, Math.ceil((s.to - d0) / DAY));
    const devs = v.withData.slice().sort((a, b) => v.per[b].bytes - v.per[a].bytes).slice(0, MAX_HM).sort((a, b) => (DEVS[a] < DEVS[b] ? -1 : 1));
    const W = 1000, LW = 110, PW = W - LW - 16, RH = 26, CH = 20, top = 6, cw = PW / nd;
    const x = (t) => LW + ((t - d0) / DAY) * cw, axisY = top + RH * devs.length + 4, H = axisY + 62;
    const cells = [], sel = new Set(devs);
    let max = 0;
    for (let i = 0; i < F.day.t.length; i++) {
      const d = F.day.dev[i], t = F.day.t[i];
      if (!sel.has(d) || t < s.from || t >= s.to) continue;
      cells.push(i); max = Math.max(max, F.day.bytes[i]);
    }
    const shade = (b) => { const k = Math.sqrt(b / Math.max(max, 1)), a = [214, 230, 245], z = [29, 79, 124];   // sqrt: small days stay visible
      return `rgb(${a.map((c, j) => Math.round(c + (z[j] - c) * k)).join(",")})`; };
    const o = [`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" font-size="12">`];
    for (const [tv, lab] of DASH.timeTicks(d0, d0 + nd * DAY)) o.push(`<line x1="${x(tv).toFixed(1)}" x2="${x(tv).toFixed(1)}" y1="${top}" y2="${axisY}" stroke="#e6e9ee"/><text x="${x(tv).toFixed(1)}" y="${axisY + 16}" text-anchor="middle" fill="#6b717b" font-size="11">${esc(lab)}</text>`);
    const row = new Map(devs.map((d, k) => [d, k]));
    devs.forEach((d, k) => { const y = top + k * RH + (RH - CH) / 2;
      o.push(`<text x="${LW - 10}" y="${y + CH / 2 + 4}" text-anchor="end" fill="#2b2f36" font-weight="600" data-tip="${esc(devTip(d))}">${DEVS[d]}</text><rect x="${LW}" y="${y + CH / 2 - 1}" width="${PW}" height="2" fill="#f0f2f5"/>`); });
    for (const i of cells) {
      const d = F.day.dev[i], y = top + row.get(d) * RH + (RH - CH) / 2;
      o.push(`<rect x="${x(F.day.t[i]).toFixed(2)}" y="${y}" width="${Math.max(cw - (cw > 4 ? 1 : 0), 1).toFixed(2)}" height="${CH}" fill="${shade(F.day.bytes[i])}" data-tip="${esc(`${DEVS[d]}\n${fmtUtcDay(F.day.t[i])} (UTC day)\n${num(F.day.files[i])} log file${F.day.files[i] > 1 ? "s" : ""} started, ${sizeTxt(F.day.bytes[i])}`)}"/>`);
    }
    o.push(`<line x1="${LW}" x2="${LW + PW}" y1="${axisY}" y2="${axisY}" stroke="#c9ced6"/><text x="${LW + PW / 2}" y="${axisY + 34}" text-anchor="middle" fill="#6b717b" font-size="11">Logging day (UTC)</text>`);
    let lx = LW;
    for (const [c, lab, tip] of [[shade(max * 0.04), "Little data", "Lighter = less data logged that day"], [shade(max), "Most data", `Darkest = the busiest device-day in the view (${sizeTxt(max)})`], ["#f0f2f5", "No log file", "No log file started that day (by logging time)"]]) {
      o.push(`<g data-tip="${esc(tip)}"><rect x="${lx}" y="${axisY + 46}" width="12" height="12" rx="2" fill="${c}"/><text x="${lx + 17}" y="${axisY + 56}" fill="#2b2f36">${esc(lab)}</text></g>`);
      lx += 30 + 7 * lab.length;
    }
    return { svg: o.join("") + "</svg>", shown: devs.length };
  }
  function tabActivity(v) {
    const hm = heatmapSvg(v);
    const chart = (id, title, tip) => `<div class="dchart"><div class="ttitle">${esc(title)}<span class="thint">Drag to zoom, double-click to reset</span>${info(tip)}</div><div class="uchart" data-kind="${id}"></div></div>`;
    return [`<div class="ttitle">Log data per device and day${info("GB of log files that started each day (logging time, UTC days). Hover a cell for files and GB" + (hm.shown < v.withData.length ? `. Showing the ${hm.shown} devices with most data; the Devices tab lists all ${v.withData.length}` : ""))}</div><figure>${hm.svg}</figure>`,
      chart("day", "Logged per day (GB)", `GB of log files by the day their logging started. ${DEF.log}`) +
      chart("up", "Uploaded per day (GB)", `GB of log files by the day they arrived in S3. ${DEF.up}. A gap between the two charts = data uploaded later than it was logged`)];
  }
  let plots = [];
  function drawCharts(v) {
    plots.forEach((p) => p.destroy()); plots = [];
    const s = ctl.state, width = Math.max(300, document.getElementById("panels").clientWidth - 4);
    const xs = []; for (let t = Math.floor(s.from / DAY) * DAY; t < s.to; t += DAY) xs.push(t);
    const at = new Map(xs.map((t, i) => [t, i]));
    const top = v.withData.slice().sort((a, b) => v.per[b].bytes - v.per[a].bytes);
    const stackDevs = top.length > MAX_STACK + 1 ? top.slice(0, MAX_STACK) : top, rest = top.length > stackDevs.length;
    document.querySelectorAll(".uchart").forEach((el) => {
      const src = el.dataset.kind === "day" ? F.day : F.up, sel = new Set(v.sel), idx = new Map(stackDevs.map((d, k) => [d, k]));
      const vals = [...stackDevs, ...(rest ? ["rest"] : [])].map(() => xs.map(() => null));
      for (let i = 0; i < src.t.length; i++) {
        const d = src.dev[i], j = at.get(src.t[i]);
        if (j == null || !sel.has(d)) continue;
        const k = idx.has(d) ? idx.get(d) : rest ? stackDevs.length : -1;
        if (k >= 0) vals[k][j] = (vals[k][j] || 0) + gb(src.bytes[i]);
      }
      const stacks = vals.map((values, k) => ({ label: k < stackDevs.length ? DEVS[stackDevs[k]] : "Other devices", color: SERIES[Math.min(k, SERIES.length - 1)], values }));
      const res = stackedBars(el, { xs, binS: DAY, width, height: 170, xRange: [xs[0], xs[xs.length - 1] + DAY], tz: TZ, syncKey: "s3",
        stacks, totalLabel: stacks.length > 1 ? "Total" : undefined, fmtX: (t) => fmtUtcDay(t), fmtY: (y) => (y == null ? "-" : y.toFixed(2)), fmtAxisY: (y) => String(y),
        xIncrs: [DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 61 * DAY, 91 * DAY, 182 * DAY, 365 * DAY], xValues: (u, ticks) => ticks.map((t) => fmt(t, "d")),
        hooks: { setSelect: [(u) => { if (u.select.width > 4) { const a = u.posToVal(u.select.left, "x"), b = u.posToVal(u.select.left + u.select.width, "x"); u.setSelect({ left: 0, width: 0 }, false); ctl.setRange(a, b); } }] } });
      res.u.over.addEventListener("dblclick", () => ctl.setRange(T0, T1));
      plots.push(res.u);
    });
  }

  // ------------------------------------------------------------------ Firmware + Objects
  const SYNC = { true: '<span class="ok">&#10003;</span>', false: `<span class="badge b-fw" data-tip="${esc(DEF.sync)}">not synced</span>` };
  function tabMeta(v) {
    const sh = since(D.map((x) => (x.status || {}).heartbeat), "Last heartbeat");
    const rows = v.sel.map((d) => {
      const s = D[d].status, pct = sdPct(d);
      if (!s) return [DEVS[d], fwOf(d) || "-", "-", "-", "-", "-", "-"];
      const sync = s.sync == null ? "-" : { t: s.sync ? "synced" : "not synced", s: s.sync ? 1 : 0, w: 11, html: SYNC[s.sync] };
      return { flag: s.sync === false, cells: [DEVS[d], s.fw || "-",
        s.cfg_crc32 ? { t: s.cfg_crc32, s: s.cfg_crc32, html: `<span data-tip="${esc(`${s.cfg_name || "Config"} the device runs (device.json): ${s.cfg_crc32}\nConfig file in S3: ${s.cfg_s3_crc32 || "none"}`)}">${esc(s.cfg_crc32)}</span>` } : "-",
        sync, pct != null ? Math.round(pct) : "-", s.sd_life_pct != null ? s.sd_life_pct : "-", sh.cell(s.heartbeat)] };
    });
    const fwRows = [];
    for (const d of v.sel) for (const [f, n, a, b] of D[d].fw.slice().reverse())
      fwRows.push({ flag: D[d].fw.length > 1, cells: [DEVS[d], f, n, a ? { t: fmt(a), s: a } : "-", b ? { t: fmt(b), s: b } : "-"] });
    return [table({ title: "Device status", dev: 0, count: true, rows, bars: [4, 6], decimals: { 6: 1 }, sort: [0, 1], search: "Filter by device or meta",
      toggle: { label: "Only devices with config not synced" }, barColor: VI.red,
      titleTip: "The latest status each device reported in its device.json (as in CANcloud): A snapshot as of its last heartbeat, whatever the period. Devices without device.json show '-'",
      columns: ["Device", "FW", "Config CRC32", "Config sync", "SD used %", "SD wear %", `Heartbeat age (${sh.unit})`],
      tips: { FW: "Firmware the device runs now (device.json)",
        "Config CRC32": "Checksum of the Configuration File the device runs (device.json; hover for the file name and the CRC32 of the file in S3). The same value is in each log file header, so it shows which config recorded which file (canedge-raw-overview Meta tab)",
        "Config sync": DEF.sync, "SD used %": DEF.sd, "SD wear %": "SD card health: Share of the card's lifespan used, as the card reports it (device.json sd_used_lifespan)",
        [`Heartbeat age (${sh.unit})`]: `${SINCE_TIP("device's last heartbeat")}. ${DEF.hb}` } }),
    table({ title: "Firmware history", dev: 0, count: true, group: true, rows: fwRows, bars: [2], sort: [0, 1], search: "Filter by device or firmware",
      toggle: { label: "Only devices with 2+ firmware versions", on: fwRows.some((r) => r.flag) }, barColor: VI.red,
      titleTip: "One row per device and firmware version in its log files (Fw metadata), newest first: When was a device updated. All log files whose metadata the report read, whatever the period",
      columns: ["Device", "FW", "Files", "First logged", "Last logged"],
      tips: { Files: "Log files with this firmware", "First logged": `Start of the first log file with this firmware. ${DEF.log}`, "Last logged": "Start of the last log file with this firmware" } })];
  }
  function tabObjects() {
    const rows = F.others.map(([k, size, t]) => [k, Math.round((size / 1024) * 10) / 10, { t: fmt(t), s: t }]);
    return table({ title: "Other objects", count: true, rows, bars: [1], sort: [0, 1], search: "Filter by name", barColor: VI.orange,
      titleTip: "Objects in the bucket that are not log files: device.json (device status), Configuration Files, firmware files, DBC files and data lake JSON files" +
        (F.others_total > F.others.length ? `. Showing ${num(F.others.length)} of ${num(F.others_total)}` : ""),
      columns: ["Key", "Size (KB)", "Last modified"], tips: { "Last modified": "When the object was last uploaded or changed (S3 LastModified)" } });
  }

  const TABS = [
    ["Overview", "Key figures for the selected devices and period", kpis],
    ["Devices", "Every device in the bucket: Data in the period, last logged and uploaded, firmware and status", tabDevices],
    ["Activity", "When the devices logged data and when it was uploaded, per day", tabActivity],
    ["Meta", "Device status from device.json (firmware, config + sync, SD card, heartbeat) and the firmware history from the log files", tabMeta],
    ["Objects", "Non-log objects in the bucket: device.json, Configuration Files, DBC files, ...", tabObjects],
  ];

  // ------------------------------------------------------------------ the dashboard (kit: filter bar, tabs, hash, tooltips)
  function dataUsedTip(v) {
    const s = ctl.state, tot = (k) => v.sel.reduce((x, d) => x + D[d][k], 0);
    return "DATA USED\n" + [
      `Bucket: s3://${F.bucket}/${F.prefix}` + (CFG.endpoint ? ` (${CFG.endpoint})` : ""),
      `Devices: ${v.sel.length <= 12 ? v.sel.map((d) => DEVS[d]).join(", ") : `${v.sel.length} devices`}`,
      `Period: ${fmt(s.from)} to ${fmt(s.to)} (${tzLabel(s.from, s.to)})\nUTC: ${isoUtc(s.from)} to ${isoUtc(s.to)}`,
      `Reference time (not logging / not uploading): ${fmt(AS_OF)}`,
      `Log files in the bucket: ${num(tot("files"))}, metadata read for ${num(tot("files") - tot("unread"))}`,
      `Source: S3 listing + object metadata (Timestamp, Fw), device.json. Log files are not downloaded`,
      `Inventory synced: ${F.synced || "?"}`, `Data for follow-up questions: report.json`, `Generated: ${CFG.generated}`].join("\n");
  }
  ctl = DASH.app({
    devices: DEVS, devMeta: (i) => D[i].meta, T0, T1, step: DAY, tabs: TABS,
    brushHint: "Bars = days with logged data. Drag to select a period, drag the ends to adjust, double-click to reset",
    activity: (devs) => { const out = []; for (let i = 0; i < F.day.t.length; i++) if (devs.has(F.day.dev[i])) out.push([F.day.t[i], F.day.t[i] + DAY]); return out; },
    compute, empty: (v) => !v.sel.length,
    header: (v, s) => ({
      title: `${CFG.title} - ${v.sel.length === 1 ? DEVS[v.sel[0]] : `${v.sel.length} devices`} - ${fmtUtcDay(s.from)} to ${fmtUtcDay(s.to - 1)}`,
      sub: `s3://${esc(F.bucket)}/${esc(F.prefix)} - ${num(v.sel.reduce((x, d) => x + D[d].files, 0))} log files in the bucket. Times in ${esc(tzLabel(s.from, s.to))}` + (v.sel.length ? info(dataUsedTip(v)) : "") }),
    printLine: (v, s) => `Devices: ${v.sel.map((d) => DEVS[d]).join(", ") || "none"}. Period: ${fmt(s.from)} to ${fmt(s.to)} (${tzLabel(s.from, s.to)})`,
    afterRender: drawCharts,
    footer: `Generated ${CFG.generated} with the canedge-s3-overview skill. Read-only: S3 listing and object metadata, the log files are not downloaded.`,
  });
  ctl.start();
})();
