/* GNSS map for the dashboard kit (Leaflet, assets/leaflet, BSD-2): A route colored by a signal and/or one dot per
   trip, a cursor marker that follows the charts, and a time range filter. Online it draws on an Esri light gray
   basemap (works from file://); offline (or when the tiles fail) it shows on a plain background, so the page still
   works as a single file. o.tiles = {url, labels, subdomains, maxZoom, attribution} for another basemap.

   const m = TRACK.map(el, o) -> { setRange(a, b), setCursor(t), fit() }
     o.t, o.lat, o.lon   route: epoch seconds + degrees (same length; null = no fix); empty arrays = no route
     o.gapS              route: No line across a time gap longer than this (s; default: no limit). Only draw a route
                         from detailed data (seconds per point): A line between averaged positions minutes or hours
                         apart is meaningless
     o.v, o.label, o.unit  optional value per route point: Colors the route (5 shades, low = light) + legend
     o.dots              optional [{lat, lon, v, label}]: One dot each (e.g. trip starts), colored by v, label = hover
     o.height (360), o.color (single route color without o.v)
     o.marks             optional [{t, label}]: A red point on the route at each time (e.g. where episodes started)
*/
"use strict";
const TRACK = (function () {
  const SHADES = ["#c9def2", "#9fc5e8", "#6fa8dc", "#3d85c6", "#1f5a8c"];   // visual identity blue, light -> dark
  // Basemap: Esri light gray + its place-name labels (no key, works from pages opened from disk; the OpenStreetMap
  // and CARTO tile servers refuse requests without a Referer, i.e. file://). Own provider: o.tiles
  // {url, labels, maxZoom, attribution}
  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/";
  const TILES = { url: ESRI + "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", labels: ESRI + "World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
    maxZoom: 16, attribution: "Powered by Esri | Esri, HERE, Garmin, &copy; OpenStreetMap contributors, and the GIS user community" };

  function map(el, o) {
    const H = o.height || 360;
    el.innerHTML = `<div class="tmap" style="height:${H}px"></div><div class="tlegend"></div>`;
    const box = el.querySelector(".tmap"), leg = el.querySelector(".tlegend");
    const m = L.map(box, { zoomControl: true, attributionControl: true, preferCanvas: true });
    const tl = o.tiles || TILES;
    const tiles = L.tileLayer(tl.url, { subdomains: tl.subdomains || "abc", maxZoom: tl.maxZoom || 18, attribution: tl.attribution }).addTo(m);
    if (tl.labels) L.tileLayer(tl.labels, { subdomains: tl.subdomains || "abc", maxZoom: tl.maxZoom || 18, pane: "overlayPane" }).addTo(m);
    tiles.on("tileerror", () => box.classList.add("offline"));
    const T = o.t || [], n = T.length, ok = (i) => o.lat[i] != null && o.lon[i] != null;
    const dots = (o.dots || []).filter((d) => d.lat != null && d.lon != null);
    const vs = (o.v ? o.v : dots.map((d) => d.v)).filter((x) => x != null);
    const lo = vs.length ? Math.min(...vs) : 0, hi = vs.length ? Math.max(...vs) : 1;
    const shade = (x) => (x == null ? "#cbcbcb" : SHADES[Math.min(4, Math.floor(((x - lo) / Math.max(hi - lo, 1e-9)) * 5))]);
    const gap = o.gapS || Infinity;
    let layer = null, marker = null, range = n ? [T[0], T[n - 1]] : [-Infinity, Infinity];

    function draw() {
      if (layer) layer.remove();
      layer = L.layerGroup().addTo(m);
      for (const mk of o.marks || []) {   // in the marker pane: above the route
        if (mk.t < range[0] || mk.t > range[1] || !n) continue;
        const i = nearest(mk.t);
        if (ok(i)) L.circleMarker([o.lat[i], o.lon[i]], { radius: 5, color: "#fff", weight: 1.5, fillColor: "#ff6666", fillOpacity: 1, pane: "markerPane" })
          .bindTooltip(DASH.esc(mk.label || "")).addTo(layer);
      }
      for (const d of dots) {   // e.g. trip starts, colored by their value
        L.circleMarker([d.lat, d.lon], { radius: 5, color: "#fff", weight: 1, fillColor: shade(d.v), fillOpacity: 0.95, pane: "markerPane" })
          .bindTooltip(DASH.esc(d.label || "")).addTo(layer);
      }
      let seg = [], col = null;
      const flush = () => { if (seg.length > 1) L.polyline(seg, { color: col, weight: 4, opacity: 0.9 }).addTo(layer); };
      for (let i = 0; i < n; i++) {
        if (T[i] < range[0] || T[i] > range[1] || !ok(i)) { flush(); seg = []; col = null; continue; }
        if (i && T[i] - T[i - 1] > gap) { flush(); seg = []; col = null; }   // a time gap: no line across it
        const c = o.v ? shade(o.v[i]) : o.color || SHADES[3];
        if (c !== col && seg.length) { seg.push([o.lat[i], o.lon[i]]); flush(); seg = [seg[seg.length - 1]]; }
        col = c; seg.push([o.lat[i], o.lon[i]]);
      }
      flush();
    }
    function fit() {
      const pts = [];
      for (let i = 0; i < n; i++) if (ok(i) && T[i] >= range[0] && T[i] <= range[1]) pts.push([o.lat[i], o.lon[i]]);
      if (!pts.length) for (const d of dots) pts.push([d.lat, d.lon]);
      if (pts.length) m.fitBounds(L.latLngBounds(pts), { padding: [20, 20], maxZoom: 15 });
      else m.setView([0, 0], 1);
    }
    function nearest(t) {   // binary search on the sorted times
      let a = 0, b = n - 1;
      while (b - a > 1) { const c = (a + b) >> 1; if (T[c] < t) a = c; else b = c; }
      return Math.abs(T[a] - t) <= Math.abs(T[b] - t) ? a : b;
    }
    if (vs.length) {
      const step = (hi - lo) / 5, f = (x) => +x.toPrecision(3);
      leg.innerHTML = `<span class="tl-l">${DASH.esc(o.label || "")}${o.unit ? ` (${DASH.esc(o.unit)})` : ""}</span>` +
        SHADES.map((c, k) => `<span><i style="background:${c}"></i>${f(lo + k * step)}-${f(lo + (k + 1) * step)}</span>`).join("");
    }
    draw(); fit();
    return {
      setRange(a, b) { range = [a, b]; draw(); fit(); },
      setCursor(t) {
        if (t == null || !n) { if (marker) { marker.remove(); marker = null; } return; }
        const i = nearest(t);
        if (!ok(i) || Math.abs(T[i] - t) > gap) return;
        if (!marker) marker = L.circleMarker([o.lat[i], o.lon[i]], { radius: 6, color: "#ff9900", weight: 3, fillOpacity: 0.9 }).addTo(m);
        else marker.setLatLng([o.lat[i], o.lon[i]]);
      },
      fit, map: m,
    };
  }
  return { map };
})();
