/* DBC changes page (canedge-dbc dbc_diff.py --html), built on the shared dashboard kit (canedge-reports
   dashboard.js: DASH): Tabs, tooltips and the table come from the kit, the diff view is diff2html as in the DBC
   editor / config editor. D = {a, b, text diff, semantic diff (dbc_diff.py --json)}. */
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const { esc, info, table, drawTables, page } = DASH;
  const n0 = (x) => Math.round(x).toLocaleString("en-US");
  const AR = `<span class="ar">&rarr;</span>`;
  const S = D.semantic, C = S.counts;

  $("sub").textContent = `Original: ${D.a.messages} messages | New: ${D.b.messages} messages${D.ignored.length ? " | Ignored: " + D.ignored.join(", ") : ""}`;
  $("files").innerHTML = `${esc(D.a.name)} ${AR} ${esc(D.b.name)}`;
  const v = $("verdict");
  if (S.identical) { v.className = "verdict same"; v.textContent = "Identical semantics: Every compared message and signal field matches"; }
  else {
    v.className = "verdict diff";
    v.textContent = `Messages: ${C.messages_removed} removed, ${C.messages_added} added, ${C.messages_changed} changed | ` +
      `Signals: ${C.signals_removed} removed, ${C.signals_added} added, ${C.signals_changed} changed`;
  }

  // Changes: One row per message/signal change from the semantic diff (what matters for decoding)
  const val = (x) => (x == null || x === "" ? "-" : typeof x === "object" ? JSON.stringify(x) : String(x));
  const fields = (fs) => fs.map((f) => `${f.field}: ${val(f.a)} → ${val(f.b)}`).join("; ");
  const badge = (k) => ({ t: k, s: k, html: `<span class="badge c-${k}">${k}</span>` });
  const mono = (x) => ({ t: x, s: x, html: `<span class="mono">${esc(x)}</span>` });
  const rows = [];
  S.removed.forEach((m) => rows.push([mono(m.id), m.name, "", badge("removed"), `Message with ${m.signals} signals, only in ${D.a.name}`]));
  S.added.forEach((m) => rows.push([mono(m.id), m.name, "", badge("added"), `Message with ${m.signals} signals, only in ${D.b.name}`]));
  S.changed.forEach((e) => {
    if (e.fields.length) rows.push([mono(e.id), e.name, "", badge("changed"), { t: fields(e.fields), tip: fields(e.fields) }]);
    const renamed = new Set(e.renames.flatMap((r) => [r.a, r.b]));
    e.renames.forEach((r) => rows.push([mono(e.id), e.name, r.b, badge("renamed"), `Likely rename from ${r.a} (same start, length and byte order)`]));
    e.signals_removed.filter((n) => !renamed.has(n)).forEach((n) => rows.push([mono(e.id), e.name, n, badge("removed"), `Only in ${D.a.name}`]));
    e.signals_added.filter((n) => !renamed.has(n)).forEach((n) => rows.push([mono(e.id), e.name, n, badge("added"), `Only in ${D.b.name}`]));
    e.signals_changed.forEach((s) => rows.push([mono(e.id), e.name, s.signal, badge("changed"), { t: fields(s.fields), tip: fields(s.fields) }]));
  });
  $("ctable").innerHTML = rows.length ? table({
    key: "changes", title: "Message and signal changes", rows, search: "Search message or signal",
    titleTip: "What changes the decoding: Messages matched by CAN ID, signals by name. Text-only changes (order, whitespace) are not listed",
    columns: ["CAN ID", "Message", "Signal", "Change", "Details"],
    filters: { col: 3, values: [["", "All"], ["changed", "Changed"], ["added", "Added"], ["removed", "Removed"], ["renamed", "Renamed"]] },
    tips: { Signal: "Empty = the message itself", Details: "Field: old → new (hover for the full text)" } })
    : `<p>No message or signal changes${D.text.diff ? ": The files only differ in text (order, formatting, attributes that do not affect decoding)" : ""}</p>`;

  page([["diff", "Diff", "Line by line changes from the original (left) to the new DBC file (right)"],
        ["changes", `Changes (${n0(rows.length)})`, "The changes that matter for decoding, per message and signal"]]);
  drawTables();

  // Diff: The same side-by-side rendering as the DBC editor / config editor (diff2html)
  const T = D.text;
  $("t-diff").insertAdjacentHTML("beforeend", info("The DBC files as text, original left and new right. Line endings and trailing spaces are ignored"));
  $("dhdr").innerHTML = `${esc(D.a.name)} ${AR} ${esc(D.b.name)} <span class="ar">|</span> <span class="add">+${n0(T.added)}</span> / <span class="del">-${n0(T.removed)}</span> lines`;
  $("diff").innerHTML = DIFF.html(T.text, "No changes: The two files have the same text");

  $("notes").innerHTML = D.notes.map((x) => `<li>${esc(x)}</li>`).join("");
  $("foot").textContent = `Generated ${D.generated} with the canedge-dbc skill (semantic diff with cantools, text diff with diff2html). Processed locally with open source Python.`;
})();
