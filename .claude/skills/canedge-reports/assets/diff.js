/* DIFF: Line diff tabs (canedge-config impact page, canedge-dbc diff page) rendered side by side with diff2html (vendored
   in assets/diff2html), as in the CANedge config editor's "Review changes" and the DBC editor. Inlined by
   dashboard.diff_parts() together with diff2html; the text comes from dashboard.line_diff(). */
const DIFF = {
  html: (text, empty) => text ? Diff2Html.html(text, { outputFormat: "side-by-side", drawFileList: false, matching: "none",
    matchWordsThreshold: 0.25, matchingMaxComparisons: 5000, maxLineSizeInBlockForComparison: 200 }) : `<p>${empty}</p>`,
};
