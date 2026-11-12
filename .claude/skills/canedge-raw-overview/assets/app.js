/* Raw CAN data overview dashboard (canedge-raw-overview). Filters + sums the embedded FACTS in the browser.
   Built on the shared dashboard kit (canedge-reports/assets/dashboard.js: DASH). All data is UTC epoch seconds. */
"use strict";
(function () {
  const F = FACTS, B = F.buckets, BS = F.bucket_s;
  const DEVS = F.devices, CH = F.channels;
  const COL = { act: "#3d85c6", none: "#f0f2f5" };
  const SERIES = ["#3d85c6", "#ff9900", "#10cba9", "#ff6666", "#7b61c9", "#4f5b66", "#f2c14e", "#2fa4d6", "#a0522d", "#e377c2",
    "#1f5f99", "#cc7a00", "#0b8f77", "#c94c4c", "#5a4699", "#8c96a0", "#b8902e", "#1d7aa3", "#704020", "#b3529c"];  // 20 channels per device (CANmod.router: CAN11-18, ...)
  // CANmod.router (demuxed by raw_stats.py): Channel labels (CAN11 -> "router 0x010 S1") and devices whose CAN2 looks
  // like un-demuxed router tunnel frames
  const chLabel = (d, c) => ((F.meta[DEVS[d]].router || {}).labels || {})[c] || "";
  const chName = (d, c) => (chLabel(d, c) ? `${c} (${chLabel(d, c)})` : c);
  const ROUTER_HINT = DEVS.filter((dv) => (F.meta[dv].router || {}).router_hint);
  const FPS_ROWS = 30, MAX_TL = 20, MAX_PLOTS = 6;   // FPS table rows before "+ X more"; timeline rows; FPS charts
  const TRIP_DEF = `A trip = a period with bus activity (frames on at least one bus) lasting at least ${F.trip_min_min} min, ` +
    `joined across interruptions of up to ${F.segment_merge_min} min. Shorter activity (e.g. a wake-up while parked) is ` +
    "shown on the timeline but is not a trip. Usually a drive, but it can also be e.g. a charging session or, for " +
    "non-vehicle applications, a period of operation";
  const TIP_STATES = {
    act: "Active: Minutes with frames on at least one channel (incl. CAN9 GNSS/IMU data). Averages " +
      "such as Avg FPS use active time only",
    none: "No bar: No bus activity in this time (e.g. the logger was off, the bus was asleep, or those log files " +
      "are not included)",
  };
  const { DAY, VI, chCmp, parts, fmt, tzLabel, isoUtc, timeTicks, esc, info, num, r1, fmtH, metaHtml, table } = DASH;
  const TZ = CFG.tz || undefined;
  const IGN = CFG.activity_ignore || [];   // channels left out of the bus activity test (build option), e.g. CAN9
  const fps = (x) => (x == null || !isFinite(x) ? "-" : num(Math.round(x)));

  // ------------------------------------------------------------------ log file names (<device>/<session>/<split>.<ext>)
  const pad8 = (n) => String(n).padStart(8, "0");
  const fileName = (i) => `${DEVS[F.files.dev[i]]}/${pad8(F.files.session[i])}/${pad8(F.files.split[i])}.${F.files.ext[i]}`;
  const FILES_BY_DEV = DEVS.map(() => []);      // all files per device, by first frame (independent of the filters)
  F.files.first.forEach((_, i) => FILES_BY_DEV[F.files.dev[i]].push(i));
  FILES_BY_DEV.forEach((l) => l.sort((a, b) => F.files.first[a] - F.files.first[b]));
  function filesIn(d, a, b) {                    // files of device d with frames in [a, b)
    const l = FILES_BY_DEV[d]; let lo = 0, hi = l.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (F.files.first[l[m]] < b) lo = m + 1; else hi = m; }
    const out = [];
    for (let k = lo - 1; k >= 0 && F.files.last[l[k]] >= a; k--) out.push(l[k]);
    return out.reverse();
  }
  function filesTxt(list, max = 3) {             // tooltip lines
    if (!list.length) return "";
    return (list.length === 1 ? "Log file: " : `Log files (${list.length}):\n`) + list.slice(0, max).map(fileName).join("\n") +
      (list.length > max ? `\n+${list.length - max} more` : "");
  }
  // Device meta name (MF4 header "comment" = Configuration File general.device.meta): The latest file's value counts
  const META = F.meta_names || [];
  const metaOfFile = (i) => (F.files.meta && F.files.meta[i] >= 0 ? META[F.files.meta[i]] : null);
  function lastMeta(fileIdx) {   // meta name of the latest file (by first frame) with a readable header (not MFC/MFE/MFM)
    let best = -1;
    for (const i of fileIdx) if (F.files.hdr[i] && (best < 0 || F.files.first[i] > F.files.first[best])) best = i;
    return best < 0 ? null : metaOfFile(best);
  }
  const ALL_META = DEVS.map((_, d) => lastMeta(FILES_BY_DEV[d]));   // whole dataset (device dropdown)
  let META_BY_DEV = {};          // device ID -> last meta in the current selection; {} = no Meta column
  const devTip = (d) => { const m = META_BY_DEV[DEVS[d]]; return m ? `Meta: ${m}` : "Meta: Not set"; };
  function fileCell(list) {      // table cell: First file (+N); hover lists up to 20 files
    if (!list.length) return "-";
    const t = fileName(list[0]) + (list.length > 1 ? ` +${list.length - 1}` : "");
    return list.length > 1 ? { t, s: t, html: `<span class="fcell" data-tip="${esc(filesTxt(list, 20))}">${esc(t)}</span>` } : t;
  }
  const filesLine = (list) => (list.length ? `Log file${list.length > 1 ? "s" : ""}: ${fileName(list[0])}${list.length > 1 ? ` +${list.length - 1} more` : ""}` : "");
  const fmtN = (x) => (x >= 1e9 ? `${num(r1(x / 1e9))} B` : x >= 1e6 ? `${(x / 1e6).toFixed(1)} M` : num(Math.round(x)));
  const mbmin = (x) => (x < 10 ? String(Number(x.toPrecision(2))) : num(Math.round(x)));
  const median = (a) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  function wmedian(vals, ws) {
    const p = vals.map((v, i) => [v, ws[i]]).filter((x) => x[0] != null).sort((a, b) => a[0] - b[0]);
    if (!p.length) return null;
    const tot = p.reduce((s, x) => s + x[1], 0);
    let acc = 0;
    for (const [v, w] of p) { acc += w; if (acc >= tot / 2) return v; }
    return p[p.length - 1][0];
  }
  const resTxt = () => (BS < 3600 ? `${BS / 60} min` : BS < DAY ? `${BS / 3600} h` : "1 day");
  // Static ID: Fewer than 5% of its frames carry a new payload (raw_stats STATIC_MAX_CHANGES_PCT; same basis as the
  // canedge-config simulation's 'changes %')
  const STATIC_PCT = F.static_max_changes_pct ?? 5;
  const chgPct = (a) => (a.fr ? (100 * a.chg) / a.fr : null);
  const isStatic = (a) => a.fr > 0 && chgPct(a) < STATIC_PCT;
  const periodMs = (x) => (x == null ? "-" : x > 0 && x < 0.5 ? "<1" : Math.round(x));

  const T0 = Math.min(...B.t), T1 = Math.max(...B.t) + BS;
  let ctl;   // the dashboard app (DASH.app): state (devices, period, tab) + setRange

  // ------------------------------------------------------------------ compute the filtered view
  function compute(state) {
    const { devs, from, to } = state;
    const per = {};
    const dev = (d) => (per[d] = per[d] || { rec: 0, act: 0, bytes: 0, pk: 0, days: new Set(), ch: {}, rows: [] });
    for (let i = 0; i < B.t.length; i++) {
      const d = B.dev[i], t = B.t[i];
      if (!devs.has(d) || t < from || t >= to) continue;
      const p = dev(d);
      p.rec += B.rec[i]; p.act += B.act[i]; p.bytes += B.bytes[i];
      p.pk = Math.max(p.pk, B.pk[i]);
      if (B.act[i] > 0) p.days.add(Math.floor(t / DAY));
      p.rows.push(i);
      for (const ch of CH) {
        const f = B.fr[ch], a = f.a[i];
        if (!(a || f.pk[i])) continue;
        const c = (p.ch[ch] = p.ch[ch] || { a: 0, pk: 0 });
        c.a += a; c.pk = Math.max(c.pk, f.pk[i]);
      }
    }
    const files = [], trips = [];
    for (let i = 0; i < F.files.first.length; i++)
      if (devs.has(F.files.dev[i]) && F.files.last[i] >= from && F.files.first[i] < to) files.push(i);
    for (let i = 0; i < F.trips.start.length; i++)
      if (devs.has(F.trips.dev[i]) && F.trips.end[i] > from && F.trips.start[i] < to) trips.push(i);
    const ids = new Map(), g = F.id_grain_s, I = F.ids, NI = F.id_names.length, NC = CH.length;
    const dFrom = Math.floor(from / g) * g;
    for (let i = 0; i < I.fr.length; i++) {
      if (I.day[i] < dFrom || I.day[i] >= to || !devs.has(I.dev[i])) continue;
      const k = (I.dev[i] * NC + I.ch[i]) * NI + I.id[i];
      let a = ids.get(k);
      if (!a) { a = { dev: I.dev[i], ch: CH[I.ch[i]], id: F.id_names[I.id[i]], fr: 0, est: 0, up: 0, cap: 0, chg: 0, per: [], w: [], nf: 0 }; ids.set(k, a); }
      a.fr += I.fr[i]; a.est += I.est[i]; if (I.up[i] > a.up) a.up = I.up[i]; a.cap |= I.cap[i]; a.chg += I.chg ? I.chg[i] : I.fr[i];
      a.per.push(I.per[i]); a.w.push(I.fr[i]); a.nf += I.nf[i];
    }
    const filesOf = {}, tripsOf = {}, idsOf = {};
    for (const i of files) (filesOf[F.files.dev[i]] = filesOf[F.files.dev[i]] || []).push(i);
    for (const i of trips) (tripsOf[F.trips.dev[i]] = tripsOf[F.trips.dev[i]] || []).push(i);
    const devList = [...devs].filter((d) => per[d] && per[d].rec > 0).sort((a, b) => (DEVS[a] < DEVS[b] ? -1 : 1));
    const idl = [...ids.values()].filter((a) => a.fr > 0);
    for (const a of idl) (idsOf[a.dev] = idsOf[a.dev] || []).push(a);
    META_BY_DEV = {};
    for (const d of devList) { const m = lastMeta(filesOf[d] || []); if (m) META_BY_DEV[DEVS[d]] = m; }
    DASH.setMeta(META_BY_DEV, "The device's meta name (Configuration File general.device.meta) from its latest log file in the selection. Changes: See the Meta tab");
    return { per, files, trips, ids: idl, devList, filesOf: (d) => filesOf[d] || [], tripsOf: (d) => tripsOf[d] || [], idsOf: (d) => idsOf[d] || [] };
  }

  // ------------------------------------------------------------------ KPI groups
  function kpis(v) {
    const P = Object.values(v.per);
    const act = P.reduce((s, p) => s + p.act, 0), actDays = new Set(P.flatMap((p) => [...p.days])).size;
    const bytes = P.reduce((s, p) => s + p.bytes, 0);
    const frames = P.reduce((s, p) => s + Object.values(p.ch).reduce((x, c) => x + c.a, 0), 0);
    const size = v.files.reduce((s, i) => s + F.files.size[i], 0);
    const fws = [...new Set(v.files.map((i) => F.files.fw[i]).filter(Boolean))].sort();
    const crcs = new Set(v.files.map((i) => F.files.crc[i]).filter(Boolean));
    const sessions = new Set(v.files.map((i) => F.files.dev[i] + "/" + F.files.session[i])).size;
    let fmin = Infinity, fmax = -Infinity;
    for (const i of v.files) { if (F.files.first[i] < fmin) fmin = F.files.first[i]; if (F.files.last[i] > fmax) fmax = F.files.last[i]; }
    const t0 = Math.max(ctl.state.from, fmin), t1 = Math.min(ctl.state.to, fmax);
    let peak = [null, 0];
    for (const d of v.devList) if (v.per[d].pk > peak[1]) peak = [DEVS[d], v.per[d].pk];
    let longest = 0;
    for (const i of v.trips) if (F.trips.min[i] > longest) longest = F.trips.min[i];
    const chans = [...new Set(v.ids.map((a) => a.ch))].sort(chCmp);
    const kbit = (r) => r / 1000 + "k";
    const rates = [...new Set(v.devList.flatMap((d) => chans.flatMap((c) => F.meta[DEVS[d]].bitrates[c] || [])))].sort((a, b) => a - b);
    const nIds = v.ids.length, distinct = new Set(v.ids.map((a) => a.id)).size, nStatic = v.ids.filter(isStatic).length;
    const sizeTxt = size < 1e9 ? `${Math.round(size / 1e6)} MB` : `${(size / 1e9).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} GB`;
    const G = [
      { title: "Scope", c: VI.blue, k: [
        { l: "Devices", v: String(v.devList.length), n: (!fws.length ? "FW unknown" : fws.length === 1 ? `FW ${fws[0]}` : `${fws.length} FW`) + (crcs.size ? `, ${crcs.size} config${crcs.size > 1 ? "s" : ""}` : ", config unknown"),
          tip: "From each log file's MF4 header: Firmware version and config CRC32 checksum (one per Configuration File, so 2+ configs = the device was reconfigured). Older firmware may not write the checksum (config unknown). When and what changed: Meta tab" +
            (fws.length ? `
FW: ${fws.join(", ")}` : "") + (crcs.size ? `
Config CRC32: ${[...crcs].slice(0, 6).join(", ")}${crcs.size > 6 ? " ..." : ""}` : "") },
        { l: "Log files", v: num(v.files.length), n: `${sizeTxt}, ${mbmin(bytes / 1e6 / Math.max(act / 60, 1e-9))} MB/min`, tip: `${sessions} sessions. MB/min = log file size per minute of bus activity (silent periods excluded, all devices combined)` },
        { l: "Period", v: fmtH((t1 - t0) / 3600), n: `${fmt(t0, "dy")} to ${fmt(t1, "dy")}`, tip: "Time from the first to the last frame in the selection" }] },
      { title: "Activity", c: VI.green, k: [
        { l: "Bus active", v: fmtH(act / 3600), n: "all devices", tip: IGN.length ? `Time with frames on at least one channel other than ${IGN.join(", ")} (left out of the activity test for this report), all devices added up`
          : "Time with frames on at least one channel (incl. CAN9 internal GNSS/IMU/time messages; the report can be built with CAN9 left out of the activity test), all devices added up" },
        { l: "Days active", v: num(actDays), n: ((nd) => `out of ${num(nd)} day${nd > 1 ? "s" : ""}`)(Math.max(1, Math.ceil((t1 - t0) / DAY))), tip: "Calendar days (UTC) with bus activity on any selected device" },
        { l: "Trips", v: num(v.trips.length), n: longest ? `longest ${fmtH(longest / 60)}` : "", tip: TRIP_DEF }] },
      { title: "Traffic", c: VI.orange, k: [
        { l: "Frames", v: fmtN(frames), n: "all buses" },
        { l: "Avg FPS", v: fps(frames / Math.max(act, 1e-9)), n: "while bus active", tip: "Frames per second across all buses of a device, averaged over the time the bus was active (all devices combined)" },
        { l: "Peak FPS", v: fps(peak[1]), n: peak[0] || "", ntip: peak[0] ? devTip(DEVS.indexOf(peak[0])) : "", tip: "Highest frames/s of any device in a single minute, all buses" }] },
      { title: "Content", c: VI.red, k: [
        { l: "CAN IDs", v: num(nIds), n: `${num(distinct)} distinct`, tip: "IDs counted per device and channel (the same ID on two devices counts twice). Distinct = unique ID values across all devices and channels" },
        { l: "Channels", v: String(chans.length), n: (chans.length <= 3 ? chans.join(", ") : `${chans[0]} ... ${chans[chans.length - 1]}`) +
            (!rates.length ? "" : rates.length === 1 ? ` @ ${kbit(rates[0])}` : `, ${rates.length} bit-rates`),
          tip: "Channels with frames in the selection, and their bit-rates (from the log file headers) across the selected devices:\n" +
            chans.map((c) => { const r = [...new Set(v.devList.flatMap((d) => F.meta[DEVS[d]].bitrates[c] || []))].sort((a, b) => a - b);
              const lb = [...new Set(v.devList.map((d) => chLabel(d, c)).filter(Boolean))];
              return `${c}${lb.length ? ` (${lb.join(", ")})` : ""}: ${r.length ? r.map(kbit).join(", ") : lb.length ? "bit-rate set in the CANmod.router config" : "-"}`; }).join("\n") +
            (ROUTER_HINT.length ? `\n\nCANmod.router data NOT demuxed (${ROUTER_HINT.join(", ")}): CAN2 carries router tunnel frames (CAN FD ID 0x010/0x012). Add the router DBC to the project and rebuild to see the router channels as CAN11-14` : "") },
        { l: "Static IDs", v: num(nStatic), n: `${Math.round((100 * nStatic) / Math.max(nIds, 1))}% of IDs`, tip: `IDs (counted per device and channel, like CAN IDs) where fewer than ${STATIC_PCT}% of the frames carry a new payload (vs the previous frame of the ID): Candidates for a data prescaler (log on change, keeps every value change). Details: Storage tab (IDs table, 'Only static IDs')` }] },
    ];
    return DASH.kgroups(G);
  }

  function timelineSvg(v) {
    const devs = v.devList.slice().sort((a, b) => v.per[b].act - v.per[a].act).slice(0, MAX_TL).sort((a, b) => (DEVS[a] < DEVS[b] ? -1 : 1));
    const t0 = ctl.state.from, t1 = ctl.state.to, W = 1000, LW = 110, PW = W - LW - 16, RH = 34, BH = 20, top = 6;
    const x = (t) => LW + ((t - t0) / (t1 - t0)) * PW, axisY = top + RH * devs.length + 4, H = axisY + 62;
    const minW = Math.max((t1 - t0) / 800, 60);
    const o = [`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" font-size="12">`];
    for (const [tv, lab] of timeTicks(t0, t1)) o.push(`<line x1="${x(tv).toFixed(1)}" x2="${x(tv).toFixed(1)}" y1="${top}" y2="${axisY}" stroke="#e6e9ee"/><text x="${x(tv).toFixed(1)}" y="${axisY + 16}" text-anchor="middle" fill="#6b717b" font-size="11">${esc(lab)}</text>`);
    devs.forEach((d, k) => {
      const y = top + k * RH + (RH - BH) / 2, p = v.per[d];
      o.push(`<text x="${LW - 10}" y="${y + BH / 2 + 4}" text-anchor="end" fill="#2b2f36" font-weight="600" data-tip="${esc(devTip(d))}">${DEVS[d]}</text><rect x="${LW}" y="${y + BH / 2 - 1}" width="${PW}" height="2" fill="#f0f2f5"/>`);
      let run = null;   // consecutive slots with bus activity
      const flush = () => { if (!run) return;
        o.push(`<rect x="${x(run.a).toFixed(2)}" y="${y}" width="${Math.max(x(run.b) - x(run.a), 1.5).toFixed(2)}" height="${BH}" fill="${COL.act}" data-tip="${esc(`${DEVS[d]}\n${fmt(run.a)} to ${fmt(run.b)}\nBus active ${fmtH(run.act / 3600)}\n${filesTxt(filesIn(d, run.a, run.b))}`)}"/>`); run = null; };
      for (const i of p.rows) {
        const t = B.t[i];
        if (!B.act[i]) { flush(); continue; }
        if (run && run.b === t) { run.b = t + BS; run.act += B.act[i]; } else { flush(); run = { a: t, b: t + BS, act: B.act[i] }; }
      }
      flush();
      const tr = v.tripsOf(d).slice().sort((p_, q_) => F.trips.start[p_] - F.trips.start[q_]);
      let grp = [], grpEnd = 0;          // trips closer than ~1.5 px are drawn as one bar (tooltip: "N trips")
      const flushT = () => {
        if (!grp.length) return;
        const i0 = grp[0], a = Math.max(F.trips.start[i0], t0), b = Math.min(grpEnd, t1);
        const tipT = grp.length === 1
          ? `${DEVS[d]} - trip\n${fmt(F.trips.start[i0])} to ${fmt(F.trips.end[i0], "hm")} (${fmtH(F.trips.min[i0] / 60)})\n~${fps(F.trips.fps[i0])} FPS, all buses\n${filesTxt(filesIn(d, F.trips.start[i0], F.trips.end[i0]))}`
          : `${DEVS[d]} - ${grp.length} trips\n${fmt(F.trips.start[i0])} to ${fmt(b)}\n${fmtH(grp.reduce((s_, i) => s_ + F.trips.min[i], 0) / 60)} in total. Zoom in to see single trips\n${filesTxt(filesIn(d, F.trips.start[i0], grpEnd))}`;
        o.push(`<rect x="${x(a).toFixed(2)}" y="${y}" width="${Math.max(x(Math.max(b, a + minW)) - x(a), 1.5).toFixed(2)}" height="${BH}" fill="${COL.act}" data-tip="${esc(tipT)}"/>`);
        grp = [];
      };
      for (const i of tr) {
        if (grp.length && x(F.trips.start[i]) - x(Math.max(grpEnd, F.trips.start[grp[0]] + minW)) > 1.5) flushT();
        if (!grp.length) grpEnd = 0;
        grp.push(i);
        grpEnd = Math.max(grpEnd, F.trips.end[i]);
      }
      flushT();
    });
    o.push(`<line x1="${LW}" x2="${LW + PW}" y1="${axisY}" y2="${axisY}" stroke="#c9ced6"/><text x="${LW + PW / 2}" y="${axisY + 34}" text-anchor="middle" fill="#6b717b" font-size="11">${esc(tzLabel(t0, t1))}</text>`);
    let lx = LW;
    for (const [c, lab, tip] of [[COL.act, "Bus active", TIP_STATES.act], [COL.none, "No bus activity", TIP_STATES.none]]) {
      o.push(`<g data-tip="${esc(tip)}"><rect x="${lx}" y="${axisY + 46}" width="12" height="12" rx="2" fill="${c}"/><text x="${lx + 17}" y="${axisY + 56}" fill="#2b2f36">${esc(lab)}</text></g>`);
      lx += 30 + 7 * lab.length;
    }
    return { svg: o.join("") + "</svg>", shown: devs.length };
  }

  // ------------------------------------------------------------------ tabs
  function tabTimeline(v) {
    const rows = v.devList.map((d) => {
      const p = v.per[d], last = p.rows.reduce((m, i) => (B.act[i] ? Math.max(m, B.t[i]) : m), 0);
      return [DEVS[d], r1(p.act / 3600), p.days.size, last ? { t: fmt(last, "dy"), s: last } : "-"];
    });
    const tl = timelineSvg(v);
    return [table({ title: "Bus activity per device", dev: 0, columns: ["Device", "Active (h)", "Days active", "Last active"], rows, bars: [1, 2], sort: [0, 1], barColor: VI.green,
      tips: { "Active (h)": "Hours with frames on at least one bus", "Days active": "Calendar days (UTC) with bus activity", "Last active": "Most recent day with bus activity" } }),
      `<div class="ttitle">Bus activity timeline${info("Bars = periods with frames on at least one bus; trips are drawn on top with a minimum width so short ones stay visible. Hover the bars for details" + (tl.shown < v.devList.length ? `. Showing the ${tl.shown} devices with most bus activity; the table lists all ${v.devList.length}` : ""))}</div><figure>${tl.svg}</figure>`];
  }
  function tabFps(v) {
    const rows = [];
    for (const d of v.devList) {
      const p = v.per[d], chs = F.meta[DEVS[d]].channels.filter((c) => p.ch[c]);
      const idsCh = (c) => v.idsOf(d).filter((a) => a.ch === c).length;
      const avg = (c) => (p.ch[c] ? p.ch[c].a : 0) / Math.max(p.act, 1e-9);   // frames / active time
      const sum = (k) => chs.reduce((s, c) => s + val(c, k), 0);
      rows.push([DEVS[d], "Total", "", chs.reduce((s, c) => s + idsCh(c), 0), Math.round(chs.reduce((s, c) => s + avg(c), 0)), Math.round(p.pk)]);
      for (const c of chs) rows.push({ cls: "sub", cells: ["", chName(d, c),(F.meta[DEVS[d]].bitrates[c] || []).map((r) => r / 1000 + "k").join(", ") || "-", idsCh(c), Math.round(avg(c)), Math.round(p.ch[c].pk)] });
    }
    const plots = v.devList.slice().sort((a, b) => v.per[b].bytes - v.per[a].bytes).slice(0, MAX_PLOTS).sort((a, b) => (DEVS[a] < DEVS[b] ? -1 : 1));
    return [table({ title: "Average FPS per device and channel", dev: 0, group: true, sortable: false, expand: FPS_ROWS, rows, bars: [4, 5], barColor: VI.orange,
      columns: ["Device", "Channel", "Bit-rate", "IDs", "Avg FPS", "Peak FPS"],
      tips: { "Avg FPS": "Average frames/s while the bus is active (silent minutes excluded)", "Peak FPS": "Busiest single minute. Total row: All buses of the device together" } }),
      `<div class="ttitle">FPS per channel over time<span class="thint">Drag to zoom, double-click to reset</span>${info(`One chart per device, ${resTxt()} per bar: Stacked average frames/s per channel (while active; busiest channel at the bottom), so the bar height = the total. Hover for each channel, the total, the peak (busiest minute, all channels) and the log file; drag across a chart to zoom the whole dashboard to that period, double-click a chart (or All at the top) to reset` + (plots.length < v.devList.length ? `. Charts for the ${plots.length} devices with most data` : ""))}</div>` +
      plots.map((d) => `<div class="dchart"><div class="dname"><span data-tip="${esc(devTip(d))}">${DEVS[d]}</span></div><div class="uchart" data-dev="${d}"></div><div class="ufile">Log file: -</div></div>`).join("")];
  }
  function tabTrips(v) {
    const rows = v.devList.map((d) => {
      const tr = v.tripsOf(d), mins = tr.map((i) => F.trips.min[i]), days = v.per[d].days.size;
      const last = tr.length ? tr.reduce((m, i) => Math.max(m, F.trips.start[i]), 0) : null;
      return [DEVS[d], tr.length, r1(mins.reduce((s, m) => s + m, 0) / 60), mins.length ? Math.round(median(mins)) : "-", mins.length ? Math.max(...mins) : "-", days ? r1(tr.length / days) : 0, last ? { t: fmt(last), s: last } : "-"];
    });
    const tripRows = v.trips.map((i) => { const fl = filesIn(F.trips.dev[i], F.trips.start[i], F.trips.end[i]);
      return [{ t: fmt(F.trips.start[i]), s: F.trips.start[i] }, DEVS[F.trips.dev[i]], F.trips.min[i], F.trips.fps[i], fileCell(fl)]; });
    return [table({ title: "Trips per device", dev: 0, rows, bars: [1, 2, 3, 4, 5], sort: [0, 1], barColor: VI.green,
      columns: ["Device", "Trips", "Total (h)", "Median (min)", "Longest (min)", "Per day", "Last trip"],
      tips: { "Total (h)": "Duration of all trips of the device added up", "Median (min)": "Typical trip length: Half of the trips are shorter, half are longer", "Longest (min)": "Longest trip", "Per day": "Trips divided by the number of days with recorded data", "Last trip": "Start of the most recent trip" } }),
      table({ title: "Trips", dev: 1, count: true, rows: tripRows, bars: [2, 3], sort: [0, 1], barColor: VI.green, search: "Filter by device or log file",
        titleTip: "All trips in the selection, oldest first. Click a column to sort, e.g. Minutes for the longest trips",
        columns: ["Start", "Device", "Minutes", "Avg FPS", "Log files"],
        tips: { "Avg FPS": "Average frames/s during the trip, all buses", "Log files": "First log file of the trip (+ number of further files; hover to list them). Use them to pick data for decoding" } })];
  }
  function tabStorage(v) {
    const estOf = {};
    for (const d of v.devList) estOf[d] = v.idsOf(d).reduce((s, a) => s + a.est, 0);
    const est = (d) => estOf[d] || 0;
    const rows = v.devList.map((d) => {
      const L = v.idsOf(d).slice().sort((a, b) => b.est - a.est), tot = Math.max(est(d), 1), st = L.filter(isStatic);
      const fl = v.filesOf(d), mb = fl.reduce((s, i) => s + F.files.size[i], 0) / 1e6, p = v.per[d];
      return [DEVS[d], fl.length, Math.round(mb), Math.round((p.bytes / 1e6 / Math.max(p.act / 60, 1e-9)) * 100) / 100, L.length, st.length,
        r1((100 * st.reduce((s, a) => s + a.est, 0)) / tot), r1((100 * L.slice(0, 5).reduce((s, a) => s + a.est, 0)) / tot)];
    });
    const nfiles = (d) => v.filesOf(d).length;
    const share = (a) => (100 * a.est) / Math.max(est(a.dev), 1);
    const STATIC = { t: "Static", s: 1, html: '<span class="badge">Static</span>' }, DYN = { t: "", s: 0 };
    const idRows = v.ids.map((a) => { const per = wmedian(a.per, a.w);
      return { flag: isStatic(a), cells: [DEVS[a.dev], a.ch, { t: DASH.canId(a.id), s: parseInt(a.id, 16) }, r1(share(a)), { t: periodMs(per), s: per, num: true }, F.id_dlc[`${DEVS[a.dev]}|${a.ch}|${a.id}`] || "-",
        r1(chgPct(a)), isStatic(a) ? STATIC : DYN, Math.round((100 * a.nf) / Math.max(nfiles(a.dev), 1))] }; });
    return [table({ title: "Storage per device", dev: 0, rows, bars: [2, 3, 6, 7], decimals: { 3: 2 }, sort: [0, 1],
      columns: ["Device", "Files", "Size (MB)", "MB/min", "IDs", "Static IDs", "Static %", "Top 5 %"],
      tips: { Files: "Log files", "MB/min": "Log file size per minute of bus activity (silent periods excluded)", IDs: "IDs counted per channel", "Static IDs": `IDs where fewer than ${STATIC_PCT}% of the frames carry a new payload`, "Static %": "Estimated share of the device's storage taken by static IDs: Candidates for a data prescaler (log on change)", "Top 5 %": "Estimated share of the device's storage taken by its 5 largest IDs" } }),
      table({ title: "IDs", dev: 0, count: true, group: true, rows: idRows, bars: [3], sort: [3, -1], search: "Filter by device, channel or ID",
        toggle: { label: "Only static IDs" }, titleTip: "Every ID per device and channel, largest share of its device's storage first. Click a column to sort; sort by Device to group the IDs per device",
        columns: ["Device", "Channel", "ID", "Share %", "Period ms", "DLC", "Changes %", "Static", "Files %"],
        tips: { "Share %": "Estimated share of the device's log file storage: Frames x MF4 record weight by data length (<= 8 bytes 1.0, 12 B 1.75, 16 B 2.12, 64 B 4.16), as in the CANedge config editor", "Period ms": "Typical time between frames of the ID (frames-weighted median), rounded to whole ms", "Changes %": "Share of the frames that carry a new payload (vs the previous frame of the ID) = what a data prescaler on all bytes would log", Static: `Fewer than ${STATIC_PCT}% changes: A candidate for a data prescaler (log on change, keeps every value change). Toggling values show ~100% and are not static`, "Files %": "Share of the device's log files that contain the ID" } })];
  }
  function tabMeta(v) {
    const BADGE = { meta: '<span class="badge b-meta">Meta</span>', config: '<span class="badge b-cfg">Config</span>', fw: '<span class="badge b-fw">FW</span>' };
    const rows = [], unsup = v.files.filter((i) => !F.files.hdr[i]).length;
    for (const d of v.devList) {   // periods = consecutive files (by first frame) with the same meta, config CRC32, firmware
      const per = [];
      let prev = null;
      for (const i of v.filesOf(d).slice().sort((a, b) => F.files.first[a] - F.files.first[b])) {
        const key = F.files.hdr[i] ? [F.files.meta[i], F.files.crc[i], F.files.fw[i]] : null, ks = key ? key.join("|") : "?";
        const p = per[per.length - 1], se = F.files.session[i];
        if (p && p.ks === ks) { p.to = Math.max(p.to, F.files.last[i]); p.n++; p.ses.add(se); continue; }
        per.push({ ks, key, from: F.files.first[i], to: F.files.last[i], n: 1, ses: new Set([se]),
          changed: key && prev ? ["meta", "config", "fw"].filter((_, j) => prev[j] !== key[j]) : [] });
        if (key) prev = key;
      }
      const flag = per.some((p) => p.changed.length);
      for (const p of per) {
        const mt = !p.key ? "n/a" : p.key[0] >= 0 ? META[p.key[0]] : "(not set)";
        rows.push({ flag, cells: [DEVS[d], { t: mt, s: mt, html: metaHtml(mt) }, p.key ? p.key[1] || "-" : "n/a", p.key ? p.key[2] || "-" : "n/a",
          { t: fmt(p.from), s: p.from }, { t: fmt(p.to), s: p.to }, p.n, p.ses.size,
          { t: p.changed.join(" "), s: p.changed.length, html: p.changed.map((c) => BADGE[c]).join(" ") }] });
      }
    }
    const note = unsup ? `<div class="cap">${num(unsup)} of the log files are MFC/MFE/MFM: Their header is not readable yet, so meta name, config and firmware are n/a for them</div>` : "";
    return [table({ title: "Configuration history", count: true, group: true, rows, sort: [0, 1], search: "Filter by device, meta, config or firmware",
      toggle: { label: "Only devices with changes", on: rows.some((r) => r.flag) },
      titleTip: "One row per period with the same meta name, config CRC32 and firmware, per device in time order. Change = what differs from the device's previous period. A meta name change always comes with a config change (the meta name is part of the Configuration File); a config can also change without it",
      columns: ["Device", "Meta", "Config", "FW", "From", "To", "Files", "Sessions", "Change"],
      tips: { Meta: "Device meta name (Configuration File general.device.meta), from the log file header", Config: "CRC32 checksum of the Configuration File the device used: A new value = the device was reconfigured", Files: "Log files", Sessions: "Number of distinct sessions in the period (a new session starts at every power-on)" } }) + note];
  }
  const TABS = [
    ["Overview", "Key figures for the selected devices and period", kpis],
    ["Timeline", "When each device recorded data and how active its buses were", tabTimeline],
    ["FPS", "Frames per second (FPS) per device and channel, and over time", tabFps],
    ["Trips", TRIP_DEF + ". Use the log files of a trip to pick data for decoding", tabTrips],
    ["Storage", "How much each device logs, and which IDs take the space. Shares are estimates (frames x MF4 record weight by data length, as in the CANedge config editor). High-rate or static IDs that are not needed are candidates for filters or prescalers", tabStorage],
    ["Meta", "Device meta names, Configuration Files (CRC32) and firmware per period, from each log file's MF4 header: When was a device renamed, reconfigured or updated", tabMeta],
  ];

  // ------------------------------------------------------------------ uPlot charts (FPS tab)
  let plots = [];
  const MONTH = 30 * DAY;   // x ticks never finer than the data (1 min), so labels never repeat
  const XINCRS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, DAY, 2 * DAY, 3 * DAY, 7 * DAY, 14 * DAY,
    MONTH, 2 * MONTH, 3 * MONTH, 6 * MONTH, 365 * DAY].filter((x) => x >= Math.min(BS, DAY));
  function drawCharts(v) {         // stacked bars: Avg frames/s per channel (active time); height = total
    plots.forEach((p) => p.destroy()); plots = [];
    const width = Math.max(300, document.getElementById("panels").clientWidth - 4);
    document.querySelectorAll(".uchart").forEach((el) => {
      const d = +el.dataset.dev, fl = el.nextElementSibling, p = v.per[d], all = F.meta[DEVS[d]].channels.filter((c) => p.ch[c]);
      const stack = all.slice().sort((a, b) => p.ch[b].a - p.ch[a].a);   // busiest channel at the bottom
      const xs = p.rows.map((i) => B.t[i]);
      const peak = p.rows.map((i) => (B.act[i] ? B.pk[i] : null));
      const color = (c) => SERIES[all.indexOf(c) % SERIES.length];
      const x0 = Math.max(ctl.state.from, xs[0] ?? ctl.state.from), x1 = Math.min(ctl.state.to, (xs[xs.length - 1] ?? ctl.state.to) + BS);
      let res;   // shared stacked-bar helper (canedge-reports/assets/stacked-bars.js)
      res = stackedBars(el, {
        xs, binS: BS, width, height: 200, xRange: [x0, x1], tz: TZ, syncKey: "fps", fmtX: (t) => fmt(t), fmtY: fps, fmtAxisY: num,
        stacks: stack.map((c) => ({ label: chName(d, c), color: color(c), values: p.rows.map((i) => (B.act[i] ? B.fr[c].a[i] / B.act[i] : null)) })),
        totalLabel: "Total", extra: [{ label: "Peak", scale: "pk", stroke: "#9aa1ab", values: peak }],
        xIncrs: XINCRS, xValues: (u, ticks) => {
          const span = (u.scales.x.max || 0) - (u.scales.x.min || 0);
          return ticks.map((t, k) => (span > 3 * DAY ? fmt(t, "d") : k === 0 || parts(t).h + parts(t).mi === 0 ? fmt(t, "dhm") : fmt(t, "hm")));
        },
        hooks: { setCursor: [(u) => { const i = u.cursor.idx, ok = res && i != null && res.total[i] != null;
          fl.textContent = (ok && filesLine(filesIn(d, xs[i], xs[i] + BS))) || "Log file: -"; }],
          setSelect: [(u) => { if (u.select.width > 4) { const a = u.posToVal(u.select.left, "x"), b = u.posToVal(u.select.left + u.select.width, "x"); u.setSelect({ left: 0, width: 0 }, false); ctl.setRange(a, b); } }] },
      });
      res.u.over.addEventListener("dblclick", () => ctl.setRange(T0, T1));   // reset the zoom (same as the time strip)
      plots.push(res.u);
    });
  }

  // ------------------------------------------------------------------ the dashboard (kit: filter bar, tabs, hash, tooltips)
  function dataUsedTip(v) {
    const s = ctl.state;
    return "DATA USED\n" + [
      `Devices: ${v.devList.map((d) => DEVS[d]).join(", ")}`,
      `Time range: ${fmt(s.from)} to ${fmt(s.to)} (${tzLabel(s.from, s.to)})\nUTC: ${isoUtc(s.from)} to ${isoUtc(s.to)}`,
      `Data sources: ${CFG.sources.join(", ")}`, `Log files: ${num(v.files.length)} of ${num(F.files.first.length)}`,
      `Bus activity: ${IGN.length ? `all channels except ${IGN.join(", ")}` : "all channels"}`,
      `Time resolution: ${resTxt()} (Storage tab: ${F.id_grain_s >= 30 * DAY ? "30 days" : F.id_grain_s >= 7 * DAY ? "weeks" : "days"})`, `Tools: mdf2csv ${CFG.tool}`,
      `Data for follow-up questions: stats.json`, `Generated: ${CFG.generated}`].join("\n");
  }
  ctl = DASH.app({
    devices: DEVS, devMeta: (i) => ALL_META[i], T0, T1, step: BS, tabs: TABS,
    brushHint: "Bars = bus activity. Drag to select a period, drag the ends to adjust, double-click to reset",
    activity: (devs) => { const out = []; for (let i = 0; i < B.t.length; i++) if (devs.has(B.dev[i]) && B.act[i]) out.push([B.t[i], B.t[i] + BS]); return out; },
    compute, empty: (v) => !v.devList.length,
    header: (v, s) => ({
      title: `${CFG.title} - ${v.devList.length === 1 ? DEVS[v.devList[0]] : `${v.devList.length} devices`} - ${fmt(s.from, "ymd")} to ${fmt(s.to - 1, "ymd")}`,
      sub: `${num(v.files.length)} log files. Times in ${esc(tzLabel(s.from, s.to))}` + (IGN.length ? `. Bus activity (trips) ignores ${esc(IGN.join(", "))}` : "") + (v.devList.length ? info(dataUsedTip(v)) : "") }),
    printLine: (v, s) => `Devices: ${v.devList.map((d) => DEVS[d]).join(", ") || "none"}. Period: ${fmt(s.from)} to ${fmt(s.to)} (${tzLabel(s.from, s.to)})`,
    afterRender: drawCharts,
    footer: `Generated ${CFG.generated} with the canedge-raw-overview skill. Data processed locally with the CSS Electronics MF4 tools and open source Python.`,
  });
  ctl.start();
})();
