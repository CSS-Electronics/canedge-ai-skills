/* Shared dashboard kit (canedge-reports): Time formatting, tooltips, sortable/paged tables with in-cell bars, KPI groups,
   tabs + URL hash, and the filter bar (devices + period inputs + activity brush). Used by the raw CAN data overview, the
   S3 bucket overview, the lake analysis page and (DASH.page) the config + DBC diff pages: Each app adds its data model, tab contents and charts, then calls DASH.app({...}).start().
   Times are UTC epoch seconds, shown in CFG.tz (IANA name) or, if null, the viewer's PC time zone. */
"use strict";
const DASH = (function () {
  const DAY = 86400;
  const VI = { blue: "#3d85c6", orange: "#ff9900", green: "#10cba9", red: "#ff6666" };   // CSS Electronics visual identity
  // Series colors: One color per device / series name on every chart of the page (first use takes the next CAT color).
  // Apps register their devices first (colorOf(id) in device order) so a device keeps its color in all charts
  const CAT = [VI.blue, VI.orange, VI.green, VI.red, "#6b717b", "#9fc5e8", "#ffc266", "#8de8d6"];
  const COLORS_USED = new Map();
  const colorOf = (key) => { const k = String(key); if (!COLORS_USED.has(k)) COLORS_USED.set(k, CAT[COLORS_USED.size % CAT.length]); return COLORS_USED.get(k); };
  const chCmp = (a, b) => a.localeCompare(b, "en", { numeric: true });   // CAN2 before CAN10
  const PAGE = 20;   // table page size

  // ------------------------------------------------------------------ time (data in UTC, shown in local time)
  const TZ = (typeof CFG !== "undefined" && CFG.tz) || undefined;
  const dtf = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hourCycle: "h23", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const pad = (n) => String(n).padStart(2, "0");
  const offCache = new Map();   // UTC offset per half hour (DST switches on the hour/half hour); Intl is slow
  function offsetS(t) {
    const k = Math.floor(t / 1800);
    let o = offCache.get(k);
    if (o === undefined) {
      const q = {}, t0 = k * 1800;
      for (const p of dtf.formatToParts(new Date(t0 * 1000))) q[p.type] = p.value;
      o = Date.UTC(+q.year, +q.month - 1, +q.day, +q.hour % 24, +q.minute, +q.second) / 1000 - t0;
      offCache.set(k, o);
    }
    return o;
  }
  function parts(t) {
    const d = new Date((Math.floor(t) + offsetS(t)) * 1000);
    return { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds() };
  }
  function epochOf(y, mo, d, h = 0, mi = 0) {   // local wall time -> epoch seconds (two passes for DST edges)
    const g = Date.UTC(y, mo - 1, d, h, mi) / 1000;
    const e = g - offsetS(g);
    return g - offsetS(e);
  }
  function fmt(t, f = "dt") {      // YY/MM/DD HH:MM ("dt"), YY/MM/DD ("dy"), + seconds ("dts"), HH:MM ("hm"), HH:MM:SS ("hms")
    const p = parts(t), ymd = `${String(p.y).slice(2)}/${pad(p.mo)}/${pad(p.d)}`;
    if (f === "dy" || f === "ymd" || f === "d") return ymd;
    if (f === "in") return `${p.y}-${pad(p.mo)}-${pad(p.d)}T${pad(p.h)}:${pad(p.mi)}`;
    if (f === "hm") return `${pad(p.h)}:${pad(p.mi)}`;
    if (f === "hms") return `${pad(p.h)}:${pad(p.mi)}:${pad(p.s)}`;
    if (f === "dts") return `${ymd} ${pad(p.h)}:${pad(p.mi)}:${pad(p.s)}`;
    return `${ymd} ${pad(p.h)}:${pad(p.mi)}`;   // "dt", "dhm"
  }
  function fmtUtcDay(t) {          // a UTC calendar day (day facts) as YY/MM/DD
    const d = new Date(t * 1000);
    return `${String(d.getUTCFullYear()).slice(2)}/${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}`;
  }
  function offStr(t) { const m = Math.round(offsetS(t) / 60), a = Math.abs(m); return `UTC${m < 0 ? "-" : "+"}${pad(Math.floor(a / 60))}:${pad(a % 60)}`; }
  const tzLabel = (t0, t1) => `${(typeof CFG !== "undefined" && CFG.tz) || "PC local time"} (${[...new Set([offStr(t0), offStr(t1)])].join(" / ")})`;
  const isoUtc = (t) => new Date(t * 1000).toISOString().slice(0, 19) + "Z";
  function timeTicks(t0, t1, max = 8) {   // [epoch, label] for a time axis between t0 and t1
    const span = Math.max(t1 - t0, 60), out = [];
    for (const m of [10, 30, 60, 180, 360, 720]) if (span / (m * 60) <= max) {
      const p = parts(t0); let t = epochOf(p.y, p.mo, p.d, p.h);
      for (; t <= t1; t += m < 60 ? m * 60 : 3600) {
        const q = parts(t);
        if (t >= t0 && (q.h * 60 + q.mi) % m === 0) out.push([t, out.length && q.h + q.mi ? fmt(t, "hm") : fmt(t, "dhm")]);
      }
      return out;
    }
    for (const dd of [1, 2, 7, 14]) if (span / (dd * DAY) <= max) {
      const p = parts(t0); let k = 1;
      for (let t = epochOf(p.y, p.mo, p.d + 1); t <= t1; t = epochOf(p.y, p.mo, p.d + 1 + dd * k++)) out.push([t, fmt(t, "d")]);
      return out;
    }
    for (const mm of [1, 2, 3, 6, 12]) if (span / (mm * 30.4 * DAY) <= max) {
      let { y, mo } = parts(t0);
      for (;;) {
        mo++; if (mo > 12) { y++; mo = 1; }
        if ((mo - 1) % mm) continue;
        const t = epochOf(y, mo, 1);
        if (t > t1) return out;
        out.push([t, fmt(t, "dy")]);
      }
    }
    return out;
  }

  // ------------------------------------------------------------------ formatting
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const info = (tip) => (tip ? `<span class="info" data-tip="${esc(tip)}">i</span>` : "");
  const NF = new Intl.NumberFormat("en-US"), num = (x) => NF.format(x);
  const r1 = (x) => Math.round(x * 10) / 10;
  // CAN ID as shown to people (as canedge-workspace dbc_groups.fmt_can_id): 11-bit 0x123, 29-bit 0x18FEF100, LIN 0x2A.
  // The raw stats cache writes 11-bit IDs with 4 digits (0x0123): Shortened here, for display only
  const canId = (s) => (/^0x[0-9A-Fa-f]{4}$/.test(s) ? "0x" + s.slice(3).toUpperCase() : s);
  function fmtH(h) {
    h = h || 0;
    if (h < 1) return `${Math.round(h * 60)} min`;
    if (h < 48) return `${h.toFixed(1)} h`;
    return h < 24 * 365 ? `${(h / 24).toFixed(1)} days` : `${(h / 24 / 365.25).toFixed(1)} years`;
  }
  const sizeTxt = (bytes) => (bytes < 1e9 ? `${Math.round(bytes / 1e6)} MB` : `${(bytes / 1e9).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} GB`);
  const metaHtml = (m) => `<span class="mcell" data-tip="${esc(m)}">${esc(m)}</span>`;

  // ------------------------------------------------------------------ tables: Sortable columns, PAGE rows per page (Prev/Next,
  // "Show all"), optional search + toggle. Fixed-size tables (t.expand = N) show N rows + "+ X more".
  // Cell: number | string | {t: text, s: sort value, num: right-align, html, w: width in chars, tip: hover text} | {stack: [...], tip, s}. Row: [cells] | {cells, cls, flag}
  // t.dev = index of the device column: A Meta column follows it if setMeta() gave any device a meta name.
  // t.barColors = {col: color}; t.barMax = one bar scale for all bar columns; t.groups = [[label, colspan, color]] (header row above the columns);
  // t.filters = {col, values: [[value, label]]} (buttons that keep the rows whose cell text equals value; "" = all)
  const TBL = new Map();          // rendered tables of the current view (id -> model)
  const TSTATE = {};              // per table key: sort, dir, page, all, q, only - kept across filter changes
  let META_BY_DEV = {}, META_TIP = "";
  const setMeta = (map, tip) => { META_BY_DEV = map || {}; META_TIP = tip || ""; };
  const cellT = (c) => (c != null && typeof c === "object" ? (c.stack ? "" : c.t) : c);
  const cellS = (c) => (c != null && typeof c === "object" ? c.s : c);
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const blank = (v) => v == null || v === "" || v === "-";
  const CHAN = /^[A-Za-z]+\d+$/;    // channel-like names sort naturally (CAN2 before CAN10); hex device IDs do not
  function cmp(a, b) {            // blanks last; numbers numerically; text alphabetically
    if (blank(a) || blank(b)) return blank(a) - blank(b);
    if (isNum(a) && isNum(b)) return a - b;
    a = String(a); b = String(b);
    return CHAN.test(a) && CHAN.test(b) ? chCmp(a, b) : a < b ? -1 : a > b ? 1 : 0;
  }
  function withMeta(t) {          // Meta column right after the device column (t.dev = its index), if any device has one
    if (t.dev == null || !Object.keys(META_BY_DEV).length) return t;
    const k = t.dev, sh = (i) => (i > k ? i + 1 : i);
    const cell = (dv) => { if (blank(dv)) return ""; const m = META_BY_DEV[dv]; return m ? { t: m, s: m, html: metaHtml(m) } : "-"; };
    const ins = (cells) => { const c = cells.slice(); c.splice(k + 1, 0, cell(cellT(c[k]))); return c; };
    return { ...t, key: (t.key || t.title) + "|meta", gcols: k === 0 ? 2 : 1,
      columns: [...t.columns.slice(0, k + 1), "Meta", ...t.columns.slice(k + 1)],
      rows: t.rows.map((r) => (Array.isArray(r) ? ins(r) : { ...r, cells: ins(r.cells) })),
      bars: (t.bars || []).map(sh), sort: t.sort ? [sh(t.sort[0]), t.sort[1]] : t.sort,
      decimals: t.decimals ? Object.fromEntries(Object.entries(t.decimals).map(([i, x]) => [sh(+i), x])) : t.decimals,
      tips: { ...(t.tips || {}), Meta: META_TIP } };
  }
  function table(t) {
    t = withMeta(t);
    const id = "t" + TBL.size, key = t.key || t.title;
    const rows = t.rows.map((r) => (Array.isArray(r) ? { cells: r, cls: "" } : { cls: "", ...r }));
    const cols = t.columns, bars = new Set(t.bars || []);
    const numLike = (c) => isNum(cellT(c)) || !!(c && c.num);   // right-aligned: Numbers (+ cells marked num, e.g. ">=1000")
    const numCol = cols.map((_, i) => rows.some((r) => numLike(r.cells[i])) && rows.every((r) => numLike(r.cells[i]) || blank(cellT(r.cells[i]))));
    const bmax = {}, dec = {};
    bars.forEach((i) => { bmax[i] = Math.max(0, ...rows.map((r) => (isNum(cellT(r.cells[i])) ? cellT(r.cells[i]) : 0))) || null; });
    if (t.barMax) bars.forEach((i) => { bmax[i] = t.barMax; });   // one scale for several bar columns (e.g. before / after)
    cols.forEach((_, i) => {      // one number format per column
      if ((t.decimals || {})[i] !== undefined) dec[i] = t.decimals[i];
      else if (rows.some((r) => isNum(cellT(r.cells[i])) && !Number.isInteger(cellT(r.cells[i])))) dec[i] = 1;
    });
    const st = (TSTATE[key] = TSTATE[key] || { sort: t.sort ? t.sort[0] : null, dir: t.sort ? t.sort[1] : 1, page: 0, all: false, q: "", only: !!(t.toggle && t.toggle.on), f: "" });
    // fixed column widths from the header + ALL rows (not the visible page), so sorting/paging never moves columns
    const sortable = t.sortable !== false, tips = t.tips || {};
    const clen = (c, i) => {
      if (c && c.stack) return 16;
      if (c && c.w) return c.w;   // explicit width (characters), e.g. for badge cells
      const v = cellT(c), txt = isNum(v) ? (dec[i] !== undefined ? v.toFixed(dec[i]) : num(v)) : String(v ?? "-");
      const w = [...txt].reduce((x, ch) => x + (ch >= "A" && ch <= "Z" ? 1.25 : 1), 0) * (c && c.html != null && cols[i] !== "Meta" ? 1.6 : 1);   // capitals + badges are wider
      return Math.min(cols[i] === "Meta" ? 12 : 40, w) + (bars.has(i) ? 9 : 0);
    };
    const cw = cols.map((c, i) => Math.max(c.length * 1.25 + (tips[c] ? 4.5 : 0) + (sortable ? 3 : 0), i === t.dev ? 12 : 0, ...rows.map((rw) => clen(rw.cells[i], i))) + 2.5);   // device IDs never cut
    const cwSum = cw.reduce((a, b) => a + b, 0);
    // bar columns: One text width for the value before the bar (longest value of ALL rows), so the bars never shift
    const fmtNum = (v, i) => (dec[i] !== undefined ? v.toLocaleString("en-US", { minimumFractionDigits: dec[i], maximumFractionDigits: dec[i] }) : num(v));
    const tw = {};
    bars.forEach((i) => { tw[i] = Math.max(1, ...rows.map((r) => (isNum(cellT(r.cells[i])) ? fmtNum(cellT(r.cells[i]), i).length : 1))); });
    TBL.set(id, { t, rows, cols, bars, numCol, bmax, dec, tw, st, colw: cw.map((w) => (100 * w) / cwSum) });
    const fcount = (v) => (v === "" ? rows.length : rows.filter((r) => cellT(r.cells[t.filters.col]) === v).length);
    const tools = (t.filters ? t.filters.values.map(([v, lab]) => `<button type="button" class="fbtn tf${st.f === v ? " on" : ""}" data-f="${esc(v)}">${esc(lab)} (${num(fcount(v))})</button>`).join("") : "") +
      (t.search ? `<input type="search" class="tq" placeholder="${esc(t.search)}" value="${esc(st.q)}">` : "") +
      (t.toggle ? `<label><input type="checkbox" class="tt"${st.only ? " checked" : ""}>${esc(t.toggle.label)}</label>` : "");
    return `<div class="tbl" data-tid="${id}">` +
      (t.title ? `<div class="ttitle">${esc(t.title)}<span class="tcount"></span>${info(t.titleTip)}</div>` : "") +
      (tools ? `<div class="tbar">${tools}</div>` : "") +
      `<table class="fixed"><colgroup>${TBL.get(id).colw.map((w) => `<col style="width:${w.toFixed(2)}%">`).join("")}</colgroup><thead></thead><tbody></tbody></table><div class="tfoot"></div></div>`;
  }
  function drawTable(id) {
    const m = TBL.get(id), { t, st, cols, bars, numCol, bmax, dec, tw } = m, root = document.querySelector(`[data-tid="${id}"]`);
    if (!root) return;
    const sortable = t.sortable !== false;
    let rows = m.rows;
    if (st.q) { const q = st.q.toLowerCase(); rows = rows.filter((r) => r.cells.some((c) => String(cellT(c) ?? "").toLowerCase().includes(q))); }
    if (st.only && t.toggle) rows = rows.filter((r) => r.flag);
    if (st.f && t.filters) rows = rows.filter((r) => cellT(r.cells[t.filters.col]) === st.f);
    if (sortable && st.sort != null) {   // ties: The table's default order (e.g. IDs of a device by share)
      const by = (k, dir) => (a, b) => { const x = cellS(a.cells[k]), y = cellS(b.cells[k]); return blank(x) || blank(y) ? cmp(x, y) : dir * cmp(x, y); };
      const p = by(st.sort, st.dir), q = t.sort && t.sort[0] !== st.sort ? by(t.sort[0], t.sort[1]) : () => 0;
      rows = rows.slice().sort((a, b) => p(a, b) || q(a, b));
    }
    const n = rows.length, pages = Math.max(1, Math.ceil(n / PAGE));
    st.page = Math.min(st.page, pages - 1);
    const vis = t.expand ? (st.all ? rows : rows.slice(0, t.expand)) : st.all ? rows : rows.slice(st.page * PAGE, st.page * PAGE + PAGE);
    const tips = t.tips || {};
    root.querySelector("thead").innerHTML = (t.groups ? `<tr>${t.groups.map(([lab, n, c]) => `<th class="grp${lab ? "" : " g0"}" colspan="${n}"${c ? ` style="color:${c};border-bottom:2px solid ${c}"` : ""}>${esc(lab)}</th>`).join("")}</tr>` : "") +
      "<tr>" + cols.map((c, i) => {
      const on = sortable && st.sort === i, cl = [numCol[i] && !bars.has(i) ? "num" : "", sortable ? "s" : "", on ? "on" : ""].filter(Boolean).join(" ");
      return `<th${cl ? ` class="${cl}"` : ""} data-col="${i}">${esc(c)}${info(tips[c])}${sortable ? `<span class="sa">${on ? (st.dir > 0 ? "▲" : "▼") : "↕"}</span>` : ""}</th>`;
    }).join("") + "</tr>";
    const grouped = !!t.group;   // consecutive rows of the same device: Show device (+ meta) once
    const sep = grouped && (!sortable || st.sort == null || st.sort === 0);   // separator lines only in device order
    const fv = (v, i) => (isNum(v) ? (dec[i] !== undefined ? v.toLocaleString("en-US", { minimumFractionDigits: dec[i], maximumFractionDigits: dec[i] }) : num(v)) : esc(v ?? "-"));
    let last = null;
    root.querySelector("tbody").innerHTML = vis.map((r, k) => {
      let cls = r.cls, first = r.cells[0];
      if (grouped && !blank(first)) { if (first === last) first = ""; else { if (k > 0 && sep) cls += " grp"; last = r.cells[0]; } }
      const cells = r.cells.map((c, i) => {
        if (grouped && i === 0) c = first;
        else if (grouped && i < (t.gcols || 1) && first === "") c = "";   // Meta follows its device in groups
        if (c && c.stack) return `<td><div class="stack" data-tip="${esc(c.tip)}">${c.stack.map((s) => `<span style="width:${Math.max(s.pct, 0).toFixed(2)}%;background:${s.color}"></span>`).join("")}</div></td>`;
        if (c && c.html != null) return `<td>${c.html}</td>`;
        const v = cellT(c);
        const tipA = c && c.tip ? ` data-tip="${esc(c.tip)}"` : "";
        if (bmax[i] && isNum(v)) return `<td class="bar"${tipA}><div class="bc"><span class="t" style="width:${tw[i] + 0.5}ch">${fv(v, i)}</span><span class="k"><span class="f" style="width:${((100 * Math.max(v, 0)) / bmax[i]).toFixed(1)}%;background:${(t.barColors || {})[i] || t.barColor || "var(--blue)"}"></span></span></div></td>`;
        return `<td${numCol[i] ? ' class="num"' : ""}${tipA}>${fv(v, i)}</td>`;
      }).join("");
      return `<tr${cls.trim() ? ` class="${cls.trim()}"` : ""}>${cells}</tr>`;
    }).join("") || `<tr><td colspan="${cols.length}" class="none">No rows match</td></tr>`;
    const total = m.rows.length, tc = root.querySelector(".tcount");
    if (tc) tc.textContent = t.count ? (n === total ? ` (${num(total)})` : ` (${num(n)} of ${num(total)})`) : "";
    let foot = "";
    if (t.expand) { if (n > t.expand) foot = `<button type="button" data-act="all">${st.all ? "Show less" : `+ ${num(n - t.expand)} more`}</button>`; }
    else if (n > PAGE) foot = st.all
      ? `<span>All ${num(n)} rows</span><button type="button" data-act="all">Show ${PAGE} per page</button>`
      : `<span>${num(st.page * PAGE + 1)}-${num(Math.min(n, st.page * PAGE + PAGE))} of ${num(n)}</span>` +
        `<button type="button" data-act="prev"${st.page ? "" : " disabled"}>&lsaquo; Prev</button><button type="button" data-act="next"${st.page < pages - 1 ? "" : " disabled"}>Next &rsaquo;</button>` +
        `<button type="button" data-act="all">Show all</button>`;
    root.querySelector(".tfoot").innerHTML = foot;
  }
  function tableEvents(panels) {
    const mOf = (el) => { const r = el.closest(".tbl"); return r ? [r.dataset.tid, TBL.get(r.dataset.tid)] : [null, null]; };
    panels.addEventListener("click", (e) => {
      const th = e.target.closest("th.s"), b = e.target.closest(".tfoot button"), fb = e.target.closest(".tf");
      if (fb) {
        const [id, m] = mOf(fb);
        m.st.f = fb.dataset.f; m.st.page = 0;
        fb.parentNode.querySelectorAll(".tf").forEach((x) => x.classList.toggle("on", x === fb));
        drawTable(id); return;
      }
      if (th && !e.target.closest(".info")) {
        const [id, m] = mOf(th), c = +th.dataset.col;
        if (m.st.sort === c) m.st.dir = -m.st.dir; else { m.st.sort = c; m.st.dir = m.numCol[c] || m.bars.has(c) ? -1 : 1; }
        m.st.page = 0; drawTable(id);
      } else if (b) {
        const [id, m] = mOf(b), a = b.dataset.act;
        if (a === "prev") m.st.page--; else if (a === "next") m.st.page++; else { m.st.all = !m.st.all; m.st.page = 0; }
        drawTable(id);
      }
    });
    panels.addEventListener("input", (e) => { if (!e.target.matches(".tq")) return; const [id, m] = mOf(e.target); m.st.q = e.target.value; m.st.page = 0; drawTable(id); });
    panels.addEventListener("change", (e) => { if (!e.target.matches(".tt")) return; const [id, m] = mOf(e.target); m.st.only = e.target.checked; m.st.page = 0; drawTable(id); });
  }
  let printAll = null;            // print every row of every table, then restore
  window.addEventListener("beforeprint", () => { printAll = [...TBL.keys()].map((id) => [id, TBL.get(id).st.all]); TBL.forEach((m, id) => { m.st.all = true; drawTable(id); }); });
  window.addEventListener("afterprint", () => { (printAll || []).forEach(([id, a]) => { TBL.get(id).st.all = a; drawTable(id); }); printAll = null; });

  // ------------------------------------------------------------------ KPI groups: [{title, c: color, k: [{l, v, n, tip, ntip}]}]
  const kgroups = (G) => `<div class="kgroups">${G.map((g) => `<div class="kgroup" style="--c:${g.c}"><div class="gt">${esc(g.title)}</div>${g.k.map((k) =>
    `<div class="krow"><div class="l">${esc(k.l)}${info(k.tip)}</div><div class="v">${esc(k.v)}</div><div class="n"${k.ntip ? ` data-tip="${esc(k.ntip)}"` : ""}>${esc(k.n)}</div></div>`).join("")}</div>`).join("")}</div>`;

  // ------------------------------------------------------------------ shared page behaviour
  function initTips() {   // hover tooltips for any [data-tip] (text only), in #tip
    const tip = document.getElementById("tip");
    document.addEventListener("mouseover", (e) => { const el = e.target.closest && e.target.closest("[data-tip]"); if (!el) { tip.style.display = "none"; return; } tip.textContent = el.getAttribute("data-tip"); tip.style.display = "block"; });
    document.addEventListener("mousemove", (e) => { if (tip.style.display !== "block") return; let x = e.clientX + 14, y = e.clientY + 16; if (x + tip.offsetWidth > innerWidth - 8) x = e.clientX - tip.offsetWidth - 14; if (y + tip.offsetHeight > innerHeight - 8) y = e.clientY - tip.offsetHeight - 12; tip.style.left = x + "px"; tip.style.top = y + "px"; });
  }
  // A page with fixed panels (<div class="panel" data-panel="key">): Tab buttons in #tabs, (i) hovers, #tab=key in
  // the URL, tooltips, table events. tabs = [[key, title, tip]]. Call drawTables() after inserting table() html
  function page(tabs) {
    const el = document.getElementById("tabs");
    const show = (key) => {
      el.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x.dataset.tab === key));
      document.querySelectorAll(".panel").forEach((x) => x.classList.toggle("on", x.dataset.panel === key));
    };
    el.innerHTML = tabs.map(([k, h, tip]) => `<button type="button" class="tab" data-tab="${k}">${esc(h)}${info(tip)}</button>`).join("");
    el.addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (!b) return; show(b.dataset.tab); history.replaceState(null, "", "#tab=" + b.dataset.tab); });
    const h = new URLSearchParams(location.hash.slice(1)).get("tab");
    show(tabs.some(([k]) => k === h) ? h : tabs[0][0]);
    initTips();
    tableEvents(document.body);
  }
  const drawTables = () => TBL.forEach((_, id) => drawTable(id));

  // ------------------------------------------------------------------ the app: Filter bar + tabs + panels + URL hash
  // opts: devices (IDs), devMeta(i) (dropdown hint), T0, T1, step (period snap, s), activity(devSet) -> [[t0, t1], ...] (brush),
  // brushHint, tabs [[name, tip, fn(v) -> html | [html]]], compute(state) -> v, empty(v), header(v, state) -> {title, sub},
  // printLine(v, state), afterRender(v), footer
  function app(o) {
    const DEVS = o.devices, T0 = o.T0, T1 = o.T1, BS = o.step, TABS = o.tabs;
    const state = { devs: new Set(DEVS.map((_, i) => i)), from: T0, to: T1, tab: 0 };
    const $ = (id) => document.getElementById(id);
    document.getElementById("bhint").textContent = o.brushHint || "";
    function readHash() {
      const h = new URLSearchParams(location.hash.slice(1));
      if (h.get("dev")) { const s = new Set(h.get("dev").split(",").map((d) => DEVS.indexOf(d)).filter((i) => i >= 0)); if (s.size) state.devs = s; }
      if (h.get("xdev")) { const x = new Set(h.get("xdev").split(",")); state.devs = new Set(DEVS.map((_, i) => i).filter((i) => !x.has(DEVS[i]))); }
      const f = Date.parse(h.get("from") || ""), t = Date.parse(h.get("to") || "");
      if (isFinite(f)) state.from = Math.max(T0, f / 1000);
      if (isFinite(t)) state.to = Math.min(T1, t / 1000);
      if (state.to <= state.from) { state.from = T0; state.to = T1; }
      const ti = TABS.findIndex(([n]) => n.toLowerCase() === (h.get("tab") || "").toLowerCase());
      if (ti >= 0) state.tab = ti;
    }
    function writeHash() {
      const h = new URLSearchParams();
      if (state.devs.size < DEVS.length) {
        if (state.devs.size <= DEVS.length / 2) h.set("dev", [...state.devs].map((i) => DEVS[i]).join(","));
        else h.set("xdev", DEVS.filter((_, i) => !state.devs.has(i)).join(","));
      }
      if (state.from > T0) h.set("from", isoUtc(state.from));
      if (state.to < T1) h.set("to", isoUtc(state.to));
      if (state.tab) h.set("tab", TABS[state.tab][0].toLowerCase());
      history.replaceState(null, "", "#" + h.toString());
    }
    const snapDown = (t) => Math.floor(t / BS) * BS, snapUp = (t) => Math.ceil(t / BS) * BS;
    function setRange(a, b) {
      a = Math.max(T0, snapDown(Math.min(a, b))); b = Math.min(T1, snapUp(Math.max(a, b)));
      if (b - a < BS) b = Math.min(T1, a + BS);
      state.from = a; state.to = b; render();
    }

    // devices dropdown
    const devbtn = $("devbtn"), devpanel = $("devpanel");
    function devUi() {
      const n = state.devs.size;
      devbtn.textContent = (n === DEVS.length ? `All devices (${n})` : `${n} of ${DEVS.length} devices: ${[...state.devs].sort((a, b) => a - b).map((i) => DEVS[i]).slice(0, 3).join(", ")}${n > 3 ? " ..." : ""}`) + " ▾";
      devbtn.classList.toggle("on", n < DEVS.length);
      devpanel.innerHTML = `<div class="dact"><button type="button" data-a="all">All</button><button type="button" data-a="none">None</button></div>` +
        DEVS.map((d, i) => { const m = o.devMeta && o.devMeta(i); return `<label><input type="checkbox" data-i="${i}"${state.devs.has(i) ? " checked" : ""}>${esc(d)}${m ? `<span class="dmeta">${esc(m)}</span>` : ""}</label>`; }).join("");
    }
    devbtn.addEventListener("click", (e) => { devpanel.classList.toggle("open"); e.stopPropagation(); });
    devpanel.addEventListener("click", (e) => e.stopPropagation());
    devpanel.addEventListener("change", (e) => { const i = +e.target.dataset.i; if (e.target.checked) state.devs.add(i); else state.devs.delete(i); render(true); });
    devpanel.addEventListener("click", (e) => { const a = e.target.dataset.a; if (!a) return; state.devs = a === "all" ? new Set(DEVS.map((_, i) => i)) : new Set(); render(true); });
    document.addEventListener("click", () => devpanel.classList.remove("open"));

    // period inputs: Text in YY/MM/DD HH:MM (local time) + a calendar button that opens the browser's date picker
    const fromIn = $("from"), toIn = $("to"), fromP = $("fromP"), toP = $("toP");
    const parseIn = (s) => { const m = /^(\d+)-(\d+)-(\d+)T(\d+):(\d+)/.exec(s); return m ? epochOf(+m[1], +m[2], +m[3], +m[4], +m[5]) : null; };
    function parseYY(s) {         // "26/06/08 11:04" (time optional, 4-digit year accepted)
      const m = /^\s*(\d{2}|\d{4})\/(\d{1,2})\/(\d{1,2})(?:[ T]+(\d{1,2}):(\d{2}))?\s*$/.exec(s);
      if (!m || +m[2] < 1 || +m[2] > 12 || +m[3] < 1 || +m[3] > 31 || +(m[4] || 0) > 23 || +(m[5] || 0) > 59) return null;
      return epochOf(m[1].length === 2 ? 2000 + +m[1] : +m[1], +m[2], +m[3], +(m[4] || 0), +(m[5] || 0));
    }
    const applyIn = (el, isFrom) => { const t = parseYY(el.value); el.classList.toggle("bad", t == null); if (t != null) { if (isFrom) setRange(t, state.to); else setRange(state.from, t); } };
    fromIn.addEventListener("change", () => applyIn(fromIn, true));
    toIn.addEventListener("change", () => applyIn(toIn, false));
    fromP.addEventListener("change", () => { const t = parseIn(fromP.value); if (t != null) setRange(t, state.to); });
    toP.addEventListener("change", () => { const t = parseIn(toP.value); if (t != null) setRange(state.from, t); });
    document.querySelectorAll(".cal").forEach((b) => b.addEventListener("click", () => { const p = $(b.dataset.for); try { p.showPicker(); } catch (e) { p.focus(); } }));
    document.querySelectorAll(".fq").forEach((b) => b.addEventListener("click", () => { const q = b.dataset.q; if (q === "all") setRange(T0, T1); else setRange(T1 - +q * DAY, T1); }));

    // brush: Activity of the selected devices over the whole period; drag to select
    const cv = $("brush");
    let drag = null, preview = null;
    const bx = (t) => ((t - T0) / (T1 - T0)) * cv.clientWidth, bt = (x) => T0 + (Math.min(Math.max(x, 0), cv.clientWidth) / cv.clientWidth) * (T1 - T0);
    function drawBrush() {
      const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = 46;
      cv.width = w * dpr; cv.height = h * dpr;
      const c = cv.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
      c.fillStyle = "#f6f8fa"; c.fillRect(0, 0, w, h);
      const on = new Uint8Array(w);   // binary: Any selected device with activity in this pixel's time
      for (const [a, b] of o.activity(state.devs)) {
        const x0 = Math.min(w - 1, Math.floor(bx(a))), x1 = Math.min(w, Math.max(x0 + 1, Math.floor(bx(b))));
        on.fill(1, x0, x1);
      }
      c.fillStyle = "#9fc5e8";
      for (let x = 0; x < w; x++) if (on[x]) c.fillRect(x, 6, 1, h - 12);
      const [a, b] = preview || [state.from, state.to], xa = bx(a), xb = bx(b);
      c.fillStyle = "rgba(255,255,255,0.72)"; c.fillRect(0, 0, xa, h); c.fillRect(xb, 0, w - xb, h);
      c.strokeStyle = "#3d85c6"; c.lineWidth = 2; c.strokeRect(xa + 1, 1, Math.max(xb - xa - 2, 1), h - 2);
      c.fillStyle = "#3d85c6"; c.fillRect(xa - 3, h / 2 - 9, 6, 18); c.fillRect(xb - 3, h / 2 - 9, 6, 18);
      $("bl0").textContent = fmt(T0, "dy");
      $("bl1").textContent = fmt(T1, "dy");
    }
    const pos = (e) => e.clientX - cv.getBoundingClientRect().left;
    cv.addEventListener("mousedown", (e) => {
      const x = pos(e), xa = bx(state.from), xb = bx(state.to);
      const full = state.from <= T0 && state.to >= T1;   // nothing selected yet: any drag starts a new selection
      drag = !full && Math.abs(x - xa) < 8 ? { m: "l" } : !full && Math.abs(x - xb) < 8 ? { m: "r" }
        : !full && x > xa && x < xb ? { m: "move", x0: x, a: state.from, b: state.to } : { m: "new", x0: x };
      e.preventDefault();
    });
    cv.addEventListener("mousemove", (e) => { if (drag) return; const x = pos(e); cv.style.cursor = state.from <= T0 && state.to >= T1 ? "crosshair" : Math.abs(x - bx(state.from)) < 8 || Math.abs(x - bx(state.to)) < 8 ? "ew-resize" : x > bx(state.from) && x < bx(state.to) ? "grab" : "crosshair"; });
    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      const x = pos(e);
      if (drag.m === "l") preview = [Math.min(bt(x), state.to - BS), state.to];
      else if (drag.m === "r") preview = [state.from, Math.max(bt(x), state.from + BS)];
      else if (drag.m === "move") { const dt = bt(x) - bt(drag.x0), len = drag.b - drag.a; let a = drag.a + dt; a = Math.min(Math.max(a, T0), T1 - len); preview = [a, a + len]; }
      else preview = [Math.min(bt(drag.x0), bt(x)), Math.max(bt(drag.x0), bt(x))];
      drawBrush();
    });
    window.addEventListener("mouseup", () => { if (!drag) return; const p = preview; drag = null; preview = null; if (p && p[1] - p[0] > 0) setRange(p[0], p[1]); else drawBrush(); });
    cv.addEventListener("dblclick", () => setRange(T0, T1));

    function render(keepPanel) {
      if (!keepPanel) devpanel.classList.remove("open");
      devUi();
      fromIn.value = fmt(state.from, "yy"); toIn.value = fmt(state.to, "yy"); fromIn.classList.remove("bad"); toIn.classList.remove("bad");
      fromP.value = fmt(state.from, "in"); toP.value = fmt(state.to, "in");
      document.querySelectorAll(".fq").forEach((b) => b.classList.toggle("on", b.dataset.q === "all" ? state.from === T0 && state.to === T1 : Math.abs(state.to - T1) < BS && Math.abs(state.from - (T1 - +b.dataset.q * DAY)) < BS));
      drawBrush();
      const v = o.compute(state), hd = o.header(v, state);
      $("title").textContent = hd.title;
      $("sub").innerHTML = hd.sub;
      $("fsum").textContent = o.printLine ? o.printLine(v, state) : "";
      if (o.empty(v)) {
        $("panels").innerHTML = `<div class="empty">No data for the selected devices and period. Use <strong>All</strong> or double-click the strip above to reset the period.</div>`;
        $("tabs").innerHTML = "";
        writeHash(); return;
      }
      $("tabs").innerHTML = TABS.map(([h, tip], i) => `<button type="button" class="tab${i === state.tab ? " on" : ""}" data-tab="${i}">${esc(h)}${info(tip)}</button>`).join("");
      TBL.clear();
      $("panels").innerHTML = TABS.map(([h, tip, fn], i) => `<div class="panel${i === state.tab ? " on" : ""}" data-panel="${i}"><h2>${esc(h)}</h2>${[].concat(fn(v)).join('<hr class="sep">')}</div>`).join("");
      TBL.forEach((_, id) => drawTable(id));
      if (o.afterRender) o.afterRender(v);
      writeHash();
    }
    function start() {
      $("tabs").addEventListener("click", (e) => {
        const b = e.target.closest(".tab"); if (!b) return;
        state.tab = +b.dataset.tab;
        document.querySelectorAll(".tab,.panel").forEach((x) => x.classList.remove("on"));
        b.classList.add("on"); document.querySelector(`.panel[data-panel="${state.tab}"]`).classList.add("on");
        writeHash();
      });
      tableEvents($("panels"));
      initTips();
      let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => render(), 200); });
      window.addEventListener("hashchange", () => { readHash(); render(); });
      $("foot").textContent = o.footer || "";
      readHash();
      render();
    }
    return { state, setRange, render, start, T0, T1 };
  }

  return { DAY, VI, CAT, colorOf, chCmp, pad, parts, epochOf, fmt, fmtUtcDay, tzLabel, isoUtc, timeTicks, esc, info, num, r1, canId, fmtH, sizeTxt,
    metaHtml, setMeta, table, drawTables, kgroups, initTips, page, app };
})();
