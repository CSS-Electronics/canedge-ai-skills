/* Explore tab of the config impact page (simulate_config.py explore_data -> D.explore). Pick a CAN message, set its
   filter (accept / reject) and prescaler (none / count / time / data; the default is the new config) and see each
   signal as recorded (blue) vs as logged (green: Each logged value holds until the next logged frame). A control
   signal in the config: Frames in its logging-off periods are removed after the prescaler (as simulate_config.py
   does), shown grey; a checkbox switches it off. prescale() is a port of simulate_config.py prescale(); on load the
   page compares its result for the default rules with Python's counts per ID (window.EXPLORE_CHECK, a note if any
   differ). The user copies the "Your changes" text into the chat, and the agent applies it to the config. */
"use strict";
(function () {
  const X = D.explore;
  if (!X || !X.ids.length) return;
  const $ = (id) => document.getElementById(id);
  const { esc, info } = DASH;
  const TZ = CFG.tz || undefined;
  const n0 = (x) => Math.round(x).toLocaleString("en-US");
  const SPAN = Math.max(X.t1 - X.t0, 1e-3);
  const REC = "#3d85c6", LOG = "#10cba9", OFF = "rgba(107,113,123,.2)";   // CSS VI blue / green; grey = logging off
  const PRE = [[0, "None"], [1, "Count"], [2, "Time"], [3, "Data"]];
  const LIM = { 1: [1, 256, ""], 2: [1, 4194304, "ms"] };   // schema limits of prescaler_value (count, time)
  const fmtS = (s) => (s < 1 ? `${Math.round(s * 1000)} ms` : s < 120 ? `${s.toFixed(s < 10 ? 2 : 1)} s` : `${(s / 60).toFixed(1)} min`);

  // ------------------------------------------------------------------ data: One zlib blob (X.z, base64) of little-endian
  // arrays; each ref = [byte offset, byte length]. Times and run-length indexes are deltas. Decoded lazily per ID
  let BUF = null;
  async function inflate() {
    const b = atob(X.z), u = new Uint8Array(b.length);
    for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
    BUF = await new Response(new Blob([u]).stream().pipeThrough(new DecompressionStream("deflate"))).arrayBuffer();
  }
  const view = (ref, T) => new T(BUF.slice(ref[0], ref[0] + ref[1]));
  const cumsum = (a) => { for (let i = 1; i < a.length; i++) a[i] += a[i - 1]; return a; };
  const FR = new Map();
  function frames(r) {   // frame times (ticks of unit_us after t0) + the changed-bytes masks
    if (!FR.has(r)) FR.set(r, { tu: cumsum(view(r.ts, Uint32Array)), chg: view(r.chg, r.wide ? Uint32Array : Uint8Array) });
    return FR.get(r);
  }
  function signals(r) {   // per signal: values (held over frames without the signal; null before the first) + in which frames it is
    const f = frames(r);
    if (f.sigs) return f.sigs;
    const pid = r.pats ? view(r.pid, r.pid16 ? Uint16Array : Uint8Array) : null;
    f.sigs = r.signals.map((s, k) => {
      const ix = cumsum(view(s.idx, Uint32Array)), v = view(s.val, s.f32 ? Float32Array : Float64Array), out = new Array(r.n);
      for (let i = 0, j = 0; i < r.n; i++) { if (j + 1 < ix.length && ix[j + 1] === i) j++; out[i] = Number.isNaN(v[j]) ? null : v[j]; }
      let has = null;   // null = in every frame
      if (pid) { const inPat = r.pats.map((p) => p.includes(k)); has = new Uint8Array(r.n); for (let i = 0; i < r.n; i++) has[i] = inPat[pid[i]] ? 1 : 0; }
      return { ...s, values: out, has };
    });
    return f.sigs;
  }

  // ------------------------------------------------------------------ rules: {type: -1 reject, 0 accept, 1 count, 2 time, 3 data, value | hex}
  // Data prescaler mask as in the config (CANedge Docs): Hex, bit i = data byte i (rightmost bit = byte 0), empty = all
  // bytes, max 16 hex digits (64 bytes, CAN FD); bytes beyond the frame are ignored. Byte level only, no single bits
  const HEX_OK = /^[0-9A-F]{0,16}$/i;
  const allBytes = (r) => Array.from({ length: Math.min(r.maxlen, 64) }, (_, j) => j);
  function maskOf(hex) {   // -> [lo, hi]: 32-bit words for bytes 0-31 and 32-63; empty = all bytes
    if (!hex) return [-1, -1];
    const h = hex.padStart(16, "0");
    return [parseInt(h.slice(8), 16) | 0, parseInt(h.slice(0, 8), 16) | 0];
  }
  function bytesOf(hex, r) {   // the ID's bytes the mask covers
    const [lo, hi] = maskOf(hex);
    return allBytes(r).filter((j) => (j < 32 ? (lo >>> j) & 1 : (hi >>> (j - 32)) & 1));
  }
  function hexOf(bytes) {
    let lo = 0, hi = 0;
    for (const j of bytes) { if (j < 32) lo = (lo | (1 << j)) >>> 0; else hi = (hi | (1 << (j - 32))) >>> 0; }
    const h = (hi ? hi.toString(16) + lo.toString(16).padStart(8, "0") : lo.toString(16)).toUpperCase();
    return h.length % 2 ? "0" + h : h;
  }
  function defRule(r) {
    if (r.fate === "rejected") return { type: -1 };
    if (!r.pt) return { type: 0 };
    return r.pt === 3 ? { type: 3, hex: String(r.pv ?? "").trim().replace(/^0x/i, "").toUpperCase() } : { type: r.pt, value: +r.pv || 1 };
  }
  const copy = (x) => ({ ...x });
  // Same effect = same rule: A data mask compares by the ID's bytes it covers (FF vs 3F on a 6-byte ID: no change)
  const sameRule = (a, b, r) => a.type === b.type && (a.type === 1 || a.type === 2 ? a.value === b.value
    : a.type === 3 ? bytesOf(a.hex, r).join() === bytesOf(b.hex, r).join() : true);
  const byteText = (b) => `byte${b.length > 1 ? "s" : ""} ${ranges(b)}`;
  const ranges = (b) => b.reduce((o, j) => { const l = o[o.length - 1]; if (l && l[1] === j - 1) l[1] = j; else o.push([j, j]); return o; }, [])
    .map(([a, z]) => (a === z ? `${a}` : `${a}-${z}`)).join(", ");
  function describe(x, r) {
    if (x.type === -1) return "reject";
    if (x.type === 0) return "accept (no prescaler)";
    if (x.type === 1) return `count prescaler 1/${x.value}`;
    if (x.type === 2) return `time prescaler ${x.value} ms`;
    const b = bytesOf(x.hex, r);
    if (!x.hex) return "data prescaler, empty mask (all bytes)";
    return `data prescaler mask ${x.hex} (${b.length === allBytes(r).length ? `all ${b.length} bytes` : b.length ? byteText(b) : "none of this ID's bytes: only the first frame"})`;
  }

  // Port of simulate_config.py prescale(): Which frames the rule logs (1 = logged). The first frame is always logged
  function prescale(x, r) {
    const f = frames(r), n = r.n, on = new Uint8Array(n);
    if (x.type === -1 || !n) return on;
    if (x.type === 0) return on.fill(1);
    on[0] = 1;
    if (x.type === 1) { const N = Math.max(1, x.value | 0); for (let i = 0; i < n; i += N) on[i] = 1; }
    else if (x.type === 2) {   // logged if >= T ms after the last logged frame
      const T = (Math.max(1, x.value) * 1000) / X.unit_us;
      let last = f.tu[0];
      for (let i = 1; i < n; i++) if (f.tu[i] - last >= T) { on[i] = 1; last = f.tu[i]; }
    } else {   // logged if a masked byte changed vs the previous frame (= vs the last logged one)
      const [lo, hi] = maskOf(x.hex);
      for (let i = 1; i < n; i++) if (r.wide ? (f.chg[2 * i] & lo) | (f.chg[2 * i + 1] & hi) : f.chg[i] & lo) on[i] = 1;
    }
    return on;
  }

  // ------------------------------------------------------------------ state
  const IDS = X.ids, DEF = IDS.map(defRule), CUR = DEF.map(copy);
  const CTL = X.ctl || {}, HAS_CTL = Object.values(CTL).some((o) => o.length);
  let cur = 0, plots = [], useCtl = true;
  // Start ID (after inflate), so the tab opens on a meaningful example: Among the IDs with a prescaler and a DBC, the one
  // whose busiest signal changes most often while logging is on (shows what the prescaler keeps); else the first
  // (= largest) logged ID with a DBC and a changing signal; else the first logged ID with a DBC; else the first logged ID
  function startId() {
    const dbc = (r) => r.dbc && r.signals && r.signals.length;
    const nchg = (r) => {
      const on = gated(new Uint8Array(r.n).fill(1), r);
      return Math.max(0, ...signals(r).map((s) => {
        let n = 0, p = null;
        s.values.forEach((x, i) => { if (x !== null && on[i]) { if (p !== null && x !== p) n++; p = x; } });
        return n;
      }));
    };
    let best = -1, most = 1;
    IDS.forEach((r, k) => { if (DEF[k].type > 0 && dbc(r)) { const n = nchg(r); if (n > most) { most = n; best = k; } } });
    const firstOf = (ok) => IDS.findIndex((r, k) => DEF[k].type !== -1 && ok(r));
    return [best, firstOf((r) => dbc(r) && nchg(r) > 0), firstOf(dbc), firstOf(() => true), 0].find((k) => k >= 0);
  }
  const changed = (k) => !sameRule(DEF[k], CUR[k], IDS[k]);

  // Control signal: Frames inside a logging-off interval [from, to) (ticks) of the ID's channel are not logged
  function gated(on, r) {
    const off = CTL[r.channel];
    if (!useCtl || !off || !off.length) return on;
    const tu = frames(r).tu;
    for (let i = 0, j = 0; i < r.n; i++) {
      while (j < off.length && off[j][1] <= tu[i]) j++;
      if (j < off.length && off[j][0] <= tu[i]) on[i] = 0;
    }
    return on;
  }
  const logged = (x, r) => gated(prescale(x, r), r);
  const offEpoch = (ch) => (CTL[ch] || []).map(([a, b]) => [X.t0 + (Math.max(0, a) * X.unit_us) / 1e6, Math.min(X.t1, X.t0 + (b * X.unit_us) / 1e6)]);

  function check() {   // the JS port vs Python for the config's own rules: Prescaler only, and with the control signal
    const sum = (a) => a.reduce((s, v) => s + v, 0);
    const bad = IDS.filter((r, k) => sum(prescale(DEF[k], r)) !== r.py);
    const badC = IDS.filter((r, k) => sum(logged(DEF[k], r)) !== r.py_ctl);
    window.EXPLORE_CHECK = { ids: IDS.length, mismatches: bad.map((r) => `${r.channel} ${r.id}`), control_mismatches: badC.map((r) => `${r.channel} ${r.id}`) };
    if (bad.length || badC.length) console.warn("Explore: JS differs from simulate_config.py", window.EXPLORE_CHECK);
    return new Set([...bad, ...badC]).size;
  }

  function note(bad) {
    const win = X.windowed ? `The busiest ${fmtS(SPAN)} of the log file (to keep the page small)` : "The whole log file";
    $("exnote").innerHTML = bad ? `<span style="color:#c43c3c">Check: ${bad} IDs differ from the simulation (see the browser console).</span>` : "";
    $("t-ex").insertAdjacentHTML("beforeend", info(`See how a filter or prescaler changes each signal, starting from ${D.config_name}, then copy the changes at the bottom into the chat. `
      + `Blue: As recorded. Green: As logged (each value held until the next logged frame)${HAS_CTL ? ". Grey: Logging off (control signal)" : ""}. `
      + "Logged frames keep their real timestamps (zoom in: each green dot sits on a blue ring), so the green line changes only at the next logged frame and can lag behind the blue one. "
      + `Data: ${win}`));
    $("exqi").innerHTML = info("Type any part of the channel, CAN ID, message name, a signal name (signals are searchable, not shown), accept / reject, the prescaler type (count, time, data) or 'changed'. Several words: All must match. Arrow keys + Enter to pick");
    $("t-exchg").insertAdjacentHTML("beforeend", info("One line per CAN ID whose rule you changed. Copy it into the chat: The agent updates the config (specific filters at the top), checks it and re-runs this report"));
  }

  // ------------------------------------------------------------------ message picker: One search field + a filtered list
  const shortRule = (x, r) => (x.type === -1 ? "reject" : x.type === 0 ? "accept" : x.type === 1 ? `count 1/${x.value}` : x.type === 2 ? `time ${x.value} ms`
    : `data ${x.hex || "all bytes"}`);
  const words = (x) => (x.type === -1 ? "reject rejected" : "accept accepted " + ["no prescaler", "count prescaler prescaled", "time prescaler prescaled", "data prescaler prescaled"][x.type]);
  const SIGS = IDS.map((r) => r.signals.map((sg) => sg.name).join(" "));
  const msgName = (r) => r.dbc || (D.has_dbc ? "(no DBC)" : "");
  const hay = (r, k) => `${r.channel} ${r.id} ${r.id.slice(2)} ${msgName(r)} ${words(CUR[k])} ${shortRule(CUR[k], r)} ${changed(k) ? "changed" : ""} ${r.tp ? "tp " + r.tp : ""} ${SIGS[k]}`.toLowerCase();
  let shown = [], act = 0;
  function openList(q) {
    const terms = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    shown = IDS.map((_, k) => k).filter((k) => terms.every((t) => hay(IDS[k], k).includes(t)))
      .sort((a, b) => (CUR[a].type === -1) - (CUR[b].type === -1)   // accepted first, then channel, then CAN ID
        || IDS[a].channel.localeCompare(IDS[b].channel, undefined, { numeric: true }) || parseInt(IDS[a].id, 16) - parseInt(IDS[b].id, 16));
    act = Math.max(0, shown.indexOf(cur));
    renderList();
    $("excombo").classList.add("open");
  }
  function renderList() {
    $("exlist").innerHTML = shown.length ? shown.map((k, j) => {
      const r = IDS[k], x = CUR[k];
      return `<div class="exopt${x.type === -1 ? " rej" : x.type > 0 ? " pre" : ""}${k === cur ? " sel" : ""}${j === act ? " act" : ""}" data-k="${k}">`
        + `<span>${changed(k) ? "*" : ""}</span><span class="fa">${x.type === -1 ? "reject" : "accept"}</span><span>${r.channel}</span><span class="mo">${esc(r.id)}</span>`
        + `<span>${esc(msgName(r))}</span><span class="ru">${x.type === -1 ? "" : x.type === 0 ? "no prescaler" : esc(shortRule(x, r))}</span></div>`;
    }).join("") : `<div class="exempty">No CAN message matches</div>`;
    const a = $("exlist").querySelector(".act");
    if (a) a.scrollIntoView({ block: "nearest" });
  }
  const closeList = () => { $("excombo").classList.remove("open"); const r = IDS[cur]; $("exq").value = `${r.channel}  ${r.id}  ${msgName(r)}`; };
  function pick(k) { cur = k; closeList(); $("exq").blur(); fillRule(); draw(); }

  // ------------------------------------------------------------------ rule: Filter (accept / reject), prescaler + value
  function fillRule() {
    const r = IDS[cur], x = CUR[cur], d = DEF[cur], cfg = " (config)";
    $("exfil").innerHTML = [["accept", "Accept"], ["reject", "Reject"]].map(([v, h]) =>
      `<option value="${v}"${(x.type === -1) === (v === "reject") ? " selected" : ""}>${h}${(d.type === -1) === (v === "reject") ? cfg : ""}</option>`).join("");
    $("exprew").style.display = x.type === -1 ? "none" : "";
    $("expre").innerHTML = PRE.map(([t, h]) => `<option value="${t}"${t === x.type ? " selected" : ""}>${h}${t === d.type ? cfg : ""}</option>`).join("");
    if (x.type === 1 || x.type === 2) {
      const [mn, mx, u] = LIM[x.type];
      $("exval").innerHTML = `${x.type === 1 ? "Log 1 of N frames" : "Min. time between logged frames"}<span><input type="number" id="exnum" min="${mn}" max="${mx}" step="1" value="${x.value}"> ${u}</span>`;
    } else if (x.type === 3) {
      const opts = r.signals.map((g, i) => `<option value="${i}">${esc(g.name)} (${byteText(g.bytes)})</option>`).join("");
      $("exval").innerHTML = `<span>Mask (hex)${info("The config value. Each mask bit is one data byte, the rightmost bit = byte 0: 0B = bytes 0, 1 and 3, 0300 = bytes 8-9. Empty = all bytes. Up to 16 hex digits (64 bytes, CAN FD). The device watches whole bytes, not single bits")}</span>`
        + `<span class="exmask"><input type="text" id="exhex" value="${esc(x.hex)}" placeholder="empty = all bytes" maxlength="16" spellcheck="false" autocomplete="off">`
        + (opts ? `<select id="exwatch" data-tip="Set the mask to the bytes of one signal (edit the hex to combine several)"><option value="">Watch a signal...</option>${opts}</select>` : "") + `</span>`;
    } else $("exval").innerHTML = "";
    $("exbyt").style.display = x.type === 3 ? "" : "none";
    $("exval").style.display = $("exval").innerHTML ? "" : "none";
    const w = $("exctlw"), gate = (CTL[r.channel] || []).length > 0;
    w.style.display = HAS_CTL ? "" : "none";
    $("exctl").disabled = !gate;
    w.style.opacity = gate ? "" : ".5";
    w.dataset.tip = gate ? "Remove the frames sent while the control signal has logging off (grey), after the prescaler, as the device does"
      : `The control signal does not switch logging off on ${r.channel} in this window`;
  }
  function setRule(x) { CUR[cur] = x; fillRule(); update(); closeList(); changes(); }

  // Stats + value changes for the visible (zoomed) range: [i0, i1) = the ID's frames in view
  let curOn = null, held = [], logIdx = [], hasIdx = [], grp = null, pending = false;
  function visible() {
    const r = IDS[cur], tu = frames(r).tu, [a, b] = grp ? grp.rangeX() : [X.t0, X.t1];
    const ta = ((a - X.t0) * 1e6) / X.unit_us, tb = ((b - X.t0) * 1e6) / X.unit_us;
    const bs = (t) => { let lo = 0, hi = r.n; while (lo < hi) { const m = (lo + hi) >> 1; if (tu[m] < t) lo = m + 1; else hi = m; } return lo; };
    const full = a <= X.t0 + 1e-6 && b >= X.t1 - 1e-6;   // not zoomed: all frames (no rounding at the ends)
    return { a, b, i0: full ? 0 : bs(ta), i1: full ? r.n : bs(tb + 1e-9), full };
  }
  function stats() {
    pending = false;
    if (!curOn) return;
    const r = IDS[cur], f = frames(r), x = CUR[cur], on = curOn, w = visible(), span = Math.max(w.b - w.a, 1e-3);
    let n = 0, gap = 0, last = -1;
    for (let i = w.i0; i < w.i1; i++) if (on[i]) { n++; if (last >= 0) gap = Math.max(gap, f.tu[i] - f.tu[last]); last = i; }
    const tot = w.i1 - w.i0;
    const offIn = offEpoch(r.channel).reduce((s, [p, q]) => s + Math.max(0, Math.min(q, w.b) - Math.max(p, w.a)), 0);
    const tp = r.tp && x.type !== 0 ? `<span class="warn" data-tip="Multi-frame messages are reassembled from all segments: Do not reject or prescale this ID">Transport protocol ID (${esc(r.tp)})</span>` : "";
    const ctl = useCtl && (CTL[r.channel] || []).length ? `<span data-tip="Share of the time in view in which the control signal has logging on for ${r.channel}">Logging on <b>${Math.round(100 * (1 - offIn / span))}%</b> of the time</span>` : "";
    const inView = w.full ? "Whole log file" : `Zoomed in: ${fmtS(span)}`;
    $("exstats").innerHTML = `<span class="exview" data-tip="The numbers below and the value changes per signal cover the time in view: Drag across a chart to zoom, double-click to reset">${inView}</span>`
      + `<span>Logged <b>${n0(n)}</b> of ${n0(tot)} frames (${tot ? Math.round((100 * n) / tot) : 0}%)</span>`
      + `<span>FPS <b>${(tot / span).toFixed(1)}</b> &rarr; <b style="color:var(--after)">${(n / span).toFixed(1)}</b></span>`
      + `<span data-tip="Longest time between two logged frames of this ID in view">Longest gap <b>${n > 1 ? fmtS((gap * X.unit_us) / 1e6) : "-"}</b></span>`
      + ctl + lim() + `<span style="color:var(--muted)">Config: ${esc(r.rule)}</span>${tp}`;
    if (x.type === 3) maskInfo(r, x, w);
    signals(r).forEach((sg, k) => {
      const v = sg.values, h = held[k], c = document.getElementById(`exc${k}`);
      if (!c || !h) return;
      let cr = 0, cl = 0;
      for (let i = Math.max(1, w.i0); i < w.i1; i++) { if (v[i] !== v[i - 1]) cr++; if (h[i] !== h[i - 1]) cl++; }
      c.textContent = `value changes ${n0(cr)} → ${n0(cl)}`;
    });
  }
  // Data mask: Its bytes + the signals in them with how often each changes in view (every change logs a frame, so a
  // counter or checksum in a masked byte = almost every frame is logged)
  function maskInfo(r, x, w) {
    const b = bytesOf(x.hex, r), set = new Set(b), sigs = signals(r);
    const rows = sigs.filter((sg) => sg.bytes.some((j) => set.has(j))).map((sg) => {
      let n = 0, c = 0, prev;
      for (let i = w.i0; i < w.i1; i++) { if (sg.has && !sg.has[i]) continue; if (n && sg.values[i] !== prev) c++; prev = sg.values[i]; n++; }
      return [sg.name, n > 1 ? c / (n - 1) : 0];
    }).sort((p, q) => q[1] - p[1]);
    const hot = rows.filter(([, f]) => f >= 0.5);
    const list = rows.slice(0, 8).map(([nm, f]) => `<span class="${f >= 0.5 ? "hot" : ""}" data-tip="Changes in ${Math.round(100 * f)}% of the frames in view">${esc(nm)} ${Math.round(100 * f)}%</span>`).join(", ")
      + (rows.length > 8 ? `, +${rows.length - 8} more` : "");
    const all = allBytes(r).length;
    const bt = byteText(b);
    $("exbyt").innerHTML = `<b>${!x.hex || b.length === all ? `All ${all} bytes` : b.length ? bt[0].toUpperCase() + bt.slice(1) : "No byte of this ID"}</b> (of ${all})`
      + (rows.length ? `. Signals in them (changes per frame in view): ${list}` : "")
      + (hot.length ? `<br><span class="warn">${esc(hot[0][0])} changes in most frames: The prescaler logs almost every frame${hot.length > 1 ? ` (${hot.length} such signals)` : ""}</span>` : "");
  }

  function lim() {   // CANedge Docs: The device prescales at most 100 IDs per channel (15 on CAN9), the first ones seen
    const ch = IDS[cur].channel, max = ch === "CAN9" ? 15 : 100, n = IDS.filter((r, k) => r.channel === ch && CUR[k].type > 0).length;
    return CUR[cur].type > 0 && n > max ? `<span class="warn" data-tip="The device prescales only the first ${max} IDs per channel it sees with a prescaler; the others are logged at full rate. This page prescales them all">${n} IDs on ${ch} prescaled: Max ${max}</span>` : "";
  }
  const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(stats); } };   // once per frame (zoom fires per chart)

  function update() {   // new rule: logged frames -> held values -> plots, then the stats for the range in view
    const r = IDS[cur], on = logged(CUR[cur], r), sigs = signals(r);
    curOn = on;
    held = sigs.map((sg) => {
      const v = sg.values, h = new Array(r.n), has = sg.has;
      let last = null, seen = false;
      for (let i = 0; i < r.n; i++) { if (on[i] && (!has || has[i])) { last = v[i]; seen = true; } h[i] = seen ? last : null; }
      return h;
    });
    const idx = (keep) => { const o = []; for (let i = 0; i < r.n; i++) if (keep(i)) o.push(i); return Int32Array.from(o); };
    logIdx = sigs.map((sg) => idx((i) => on[i] && (!sg.has || sg.has[i])));   // dots: frames logged that carry the signal
    hasIdx = sigs.map((sg) => (sg.has ? idx((i) => sg.has[i]) : null));     // ... and recorded frames that carry it
    plots.forEach((u, k) => {   // keep the zoom: uPlot does not redraw after setData(..., false), so set the x range again
      const { min, max } = u.scales.x;
      u.setData([u.data[0], sigs[k].values, held[k]], false);
      u.setScale("x", { min, max });
    });
    stats();
  }

  // A dot per frame once zoomed in so far that the frames are >= 6 px apart: Recorded (blue) and logged (green) frames
  const DOT_PX = 6;
  function dots(color, list, ring) {   // ring: hollow (recorded frames), else filled (logged frames)
    const inView = (a, i0, i1) => { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < i0) lo = m + 1; else hi = m; } const o = []; for (let j = lo; j < a.length && a[j] <= i1; j++) o.push(a[j]); return o; };
    return { size: ring ? 7 : 5, width: ring ? 1.25 : 1, fill: ring ? "#fff" : color, stroke: color,
      show: (u, si, i0, i1) => i1 - i0 + 1 <= u.bbox.width / (window.devicePixelRatio || 1) / DOT_PX,
      filter: (u, si, show) => { if (!show) return null; const a = list(); if (!a) return null; const [i0, i1] = u.series[si].idxs || [0, u.data[0].length - 1]; return inView(a, i0, i1); } };
  }

  function draw() {   // new ID: one chart per signal, shared zoom + cursor; grey = logging off (control signal)
    plots.forEach((u) => u.destroy()); plots = []; curOn = null;
    const r = IDS[cur], el = $("explots"), W = Math.max(300, el.clientWidth - 4), sigs = signals(r), f = frames(r), off = offEpoch(r.channel);
    const shade = (u) => {
      if (!useCtl || !off.length) return;
      const ctx = u.ctx, bb = u.bbox, P = (t) => u.valToPos(t, "x", true);
      ctx.save(); ctx.beginPath(); ctx.rect(bb.left, bb.top, bb.width, bb.height); ctx.clip(); ctx.fillStyle = OFF;
      for (const [a, b] of off) ctx.fillRect(P(a), bb.top, Math.max(1, P(b) - P(a)), bb.height);
      ctx.restore();
    };
    el.innerHTML = r.signals.length ? `<div class="legend ctl exleg"><span><i style="background:${REC}"></i>As recorded (every frame)</span><span><i style="background:${LOG}"></i>Logged (value held until the next logged frame)</span><span class="exdots" data-tip="Zoomed in far enough, each recorded frame shows as a blue ring and each logged frame as a green dot. A logged frame is a recorded frame with its real timestamp: The prescaler only picks which frames are kept"><i class="ring"></i><i class="dot"></i>Frames (when zoomed in)</span>`
      + (off.length ? `<span id="exoffl"${useCtl ? "" : ' style="opacity:.4"'}><i style="background:${OFF};border:1px solid #d5d8dc"></i>Logging off (control signal)</span>` : "") + `</div>`
      : `<div class="exnote">No signal values in this ID's frames.</div>`;
    const xs = Array.from(f.tu, (t) => X.t0 + (t * X.unit_us) / 1e6);
    grp = SERIES.group({ key: "ex" + cur, xRange: [X.t0, X.t1], tz: TZ, dateLine: true });
    sigs.forEach((sg, k) => {
      el.insertAdjacentHTML("beforeend", `<div class="cap">${esc(sg.name)}${sg.unit ? ` [${esc(sg.unit)}]` : ""} <span style="color:var(--muted);font-size:12px" id="exc${k}"></span>${info(`Bytes ${ranges(sg.bytes)}. Value changes: How often the value changes as recorded vs in the logged data, in the time in view. Drag to zoom all charts, double-click to reset; zoomed in far enough, each frame shows as a dot`)}</div>`);
      const d = document.createElement("div"); el.appendChild(d);
      let lo = Infinity, hi = -Infinity;
      for (const v of sg.values) if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; }
      const yRange = lo === hi && Number.isFinite(lo) ? [lo - 1, lo + 1] : undefined;   // a constant: around its value, not 0-100
      plots.push(SERIES.line(d, grp, { width: W, height: 160, xs, yRange, hooks: { drawAxes: [shade], ...(k === 0 ? { setScale: [(u, key) => key === "x" && schedule()] } : {}) },
        series: [{ label: "As recorded", values: sg.values, stroke: REC, width: 1.5, stepped: true, points: dots(REC, () => hasIdx[k], true) },
          { label: "Logged", values: sg.values, stroke: LOG, width: 1.5, stepped: true, points: dots(LOG, () => logIdx[k]) }] }));
    });
    update();
  }

  function changes() {
    const lines = IDS.map((r, k) => (changed(k) ? `${r.channel} ${r.id}${r.dbc ? " " + r.dbc : ""}: ${describe(DEF[k], r)} -> ${describe(CUR[k], r)}` : null)).filter(Boolean);
    $("exchg").innerHTML = lines.length
      ? `<div class="exhint">Copy this into the chat to update the config:</div><textarea id="exta" readonly>${esc(`Config changes from the Explore tab (${D.config_name}, ${D.source}):\n` + lines.join("\n"))}</textarea>
        <div style="margin-top:6px"><button type="button" class="exbtn" id="excopy">Copy</button> <span class="exhint" id="excopied"></span></div>`
      : `<div class="exhint">No changes yet: Pick a CAN message and change its rule above.</div>`;
    if (lines.length) $("excopy").onclick = () => {
      const ta = $("exta"), done = () => { $("excopied").textContent = "Copied: Paste it into the chat"; };
      (navigator.clipboard ? navigator.clipboard.writeText(ta.value) : Promise.reject()).then(done, () => { ta.select(); document.execCommand("copy"); done(); });
    };
  }

  async function init() {
    $("exnote").textContent = "Loading...";
    try { await inflate(); } catch (e) { $("exnote").textContent = `This browser cannot unpack the data (${e.message}): Use a current Chrome, Edge, Firefox or Safari`; return; }
    note(check());
    cur = startId();
    closeList(); fillRule(); changes(); draw();
    const q = $("exq");
    q.addEventListener("focus", () => { q.select(); openList(""); });
    q.addEventListener("input", () => openList(q.value));
    q.addEventListener("blur", closeList);
    q.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); act = Math.max(0, Math.min(shown.length - 1, act + (e.key === "ArrowDown" ? 1 : -1))); renderList(); }
      else if (e.key === "Enter" && shown.length) { e.preventDefault(); pick(shown[act]); }
      else if (e.key === "Escape") { closeList(); q.blur(); }
    });
    $("exlist").addEventListener("mousedown", (e) => { const o = e.target.closest(".exopt"); e.preventDefault(); if (o) pick(+o.dataset.k); });
    $("exfil").addEventListener("change", (e) => { const d = DEF[cur]; setRule(e.target.value === "reject" ? { type: -1 } : d.type >= 0 ? copy(d) : { type: 0 }); });
    $("expre").addEventListener("change", (e) => {
      const t = +e.target.value, d = DEF[cur];
      setRule(t === d.type ? copy(d) : t === 3 ? { type: 3, hex: "" } : t === 1 ? { type: 1, value: 10 } : t === 2 ? { type: 2, value: 95 } : { type: 0 });
    });
    $("exval").addEventListener("change", (e) => {
      const x = CUR[cur];
      if (e.target.id === "exnum") {
        const [mn, mx] = LIM[x.type], v = Math.round(+e.target.value);
        setRule({ ...x, value: Math.min(mx, Math.max(mn, Number.isFinite(v) ? v : mn)) });
      } else if (e.target.id === "exwatch" && e.target.value !== "") {
        setRule({ type: 3, hex: hexOf(IDS[cur].signals[+e.target.value].bytes) });
      } else if (e.target.id === "exhex" && HEX_OK.test(e.target.value.trim().replace(/^0x/i, ""))) fillRule();   // tidy the shown value
    });
    $("exval").addEventListener("input", (e) => {   // typing a mask: Apply each valid value, keep the cursor in the field
      if (e.target.id !== "exhex") return;
      const v = e.target.value.trim().replace(/^0x/i, ""), ok = HEX_OK.test(v);
      e.target.classList.toggle("bad", !ok);
      e.target.dataset.tip = ok ? "" : "Hex digits only (0-9, A-F), up to 16";
      if (ok) { CUR[cur] = { type: 3, hex: v.toUpperCase() }; update(); changes(); }
    });
    $("exctl").addEventListener("change", (e) => {
      useCtl = e.target.checked;
      const l = $("exoffl");
      if (l) l.style.opacity = useCtl ? "" : ".4";
      update(); plots.forEach((u) => u.redraw());
    });
    $("exreset").addEventListener("click", () => setRule(copy(DEF[cur])));
  }

  // Drawn when the tab is first shown (charts need the panel's width)
  const panel = document.querySelector('.panel[data-panel="explore"]');
  let started = false;
  const start = () => { if (!started && panel.classList.contains("on")) { started = true; init(); } };
  $("tabs").addEventListener("click", start);
  start();
})();
