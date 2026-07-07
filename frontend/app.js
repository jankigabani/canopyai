// CanopyAI, live forest intelligence.

const $ = (id) => document.getElementById(id);
async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

// ---- Basemaps ---------------------------------------------------------------
const TILES = {
  light: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  sat: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
};
const baseLight = () => L.tileLayer(TILES.light, { attribution: "&copy; OSM &copy; CARTO", maxZoom: 19 });
const baseSat = () => L.tileLayer(TILES.sat, { attribution: "Imagery &copy; Esri", maxZoom: 19 });

// ---- Decode state -------------------------------------------------------------
let lossStart = 2001, lossEnd = 2024, lossCanopy = "tcd_30";

// Canvas-decode GridLayer. Fetches encoded PNGs through our proxy, reads the
// pixels, recolors them. Raw pixels are cached so filter changes are instant.
function decodeGridLayer({ pane, opacity = 1, urlFn, decode }) {
  const layer = new L.GridLayer({ pane, opacity, tileSize: 256, updateWhenIdle: true });
  const live = new Set();
  layer.createTile = function (coords, done) {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 256;
    const ctx = cv.getContext("2d", { willReadFrequently: true });
    cv._coords = coords; cv._ctx = ctx; cv._raw = null;
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      try {
        ctx.drawImage(img, 0, 0, 256, 256);
        const id = ctx.getImageData(0, 0, 256, 256);
        cv._raw = new Uint8ClampedArray(id.data);
        decode(id, coords.z);
        ctx.putImageData(id, 0, 0);
      } catch (e) { /* empty tile */ }
      done(null, cv);
    };
    img.onerror = () => done(null, cv);
    img.src = urlFn(coords);
    live.add(cv);
    return cv;
  };
  layer.on("tileunload", (e) => live.delete(e.tile));
  layer.redecode = function () {
    live.forEach((cv) => {
      if (!cv._raw) return;
      const id = cv._ctx.createImageData(256, 256);
      id.data.set(cv._raw);
      decode(id, cv._coords.z);
      cv._ctx.putImageData(id, 0, 0);
    });
  };
  return layer;
}

// Tree cover loss: blue channel is the loss year (2000 + B), red is intensity.
function decodeLoss(imageData, zoom) {
  const d = imageData.data;
  const exp = zoom < 13 ? 0.3 + (zoom - 3) / 20 : 1;
  const maxPow = Math.pow(255, exp);
  for (let i = 0; i < d.length; i += 4) {
    if (!d[i + 3]) continue;
    const year = 2000 + d[i + 2];
    if (year >= lossStart && year <= lossEnd) {
      const scale = Math.pow(d[i], exp) / maxPow;
      d[i] = 220; d[i + 1] = 102; d[i + 2] = 153;
      d[i + 3] = Math.min(255, Math.round(scale * 255));
    } else { d[i + 3] = 0; }
  }
}
// GLAD and RADD tropical alerts: blue over 200 means high confidence.
function decodeGlad(imageData) {
  const d = imageData.data;
  for (let i = 0; i < d.length; i += 4) {
    if (!d[i + 3]) continue;
    const high = d[i + 2] >= 200;
    d[i] = high ? 221 : 247; d[i + 1] = high ? 28 : 104; d[i + 2] = high ? 119 : 161;
    d[i + 3] = 190;
  }
}
// OPERA DIST-ALERT: blue over 200 means confirmed disturbance.
function decodeDist(imageData) {
  const d = imageData.data;
  for (let i = 0; i < d.length; i += 4) {
    if (!d[i + 3]) continue;
    const high = d[i + 2] >= 200;
    d[i] = high ? 123 : 177; d[i + 1] = high ? 44 : 122; d[i + 2] = high ? 191 : 220;
    d[i + 3] = 200;
  }
}

const lossUrl = (c) => `/api/tile/loss/${lossCanopy}/${c.z}/${c.x}/${c.y}.png`;
const gladUrl = (c) => `/api/tile/glad/${c.z}/${c.x}/${c.y}.png`;
const distUrl = (c) => `/api/tile/dist/${c.z}/${c.x}/${c.y}.png`;

// ====================== MAP VIEW ============================================
const VIEWS = { ontario: { center: [50.5, -85.5], zoom: 5 }, canada: { center: [58, -96], zoom: 4 } };
const bigmap = L.map("bigmap", { zoomControl: false }).setView(VIEWS.ontario.center, VIEWS.ontario.zoom);
L.control.zoom({ position: "topright" }).addTo(bigmap);
bigmap.createPane("loss"); bigmap.getPane("loss").style.zIndex = 350;
bigmap.createPane("dist"); bigmap.getPane("dist").style.zIndex = 355;
bigmap.createPane("glad"); bigmap.getPane("glad").style.zIndex = 360;
baseLight().addTo(bigmap);
let bSat = null;

// Ontario boundary box, subtle
L.rectangle([[41.6, -95.2], [56.9, -74.3]], { color: "#16a34a", weight: 1.2, fill: false, dashArray: "6,6", opacity: .6 }).addTo(bigmap);

// Live layers on by default: loss + fires + disturbance + GOES flash + lightning
const lossLayer = decodeGridLayer({ pane: "loss", opacity: 0.9, urlFn: lossUrl, decode: decodeLoss }).addTo(bigmap);
const distLayer = decodeGridLayer({ pane: "dist", opacity: 0.85, urlFn: distUrl, decode: decodeDist }).addTo(bigmap);
const gladLayer = decodeGridLayer({ pane: "glad", opacity: 0.8, urlFn: gladUrl, decode: decodeGlad });
const fireLayer = L.layerGroup().addTo(bigmap);
const riskLayer = L.layerGroup();
const goesLayer = L.layerGroup().addTo(bigmap);
const boltLayer = L.layerGroup().addTo(bigmap);
const ignitionLayer = L.layerGroup().addTo(bigmap);
bigmap.createPane("park"); bigmap.getPane("park").style.zIndex = 340;
const parkLayer = L.tileLayer("/api/tile/protected/{z}/{x}/{y}.png", { pane: "park", opacity: 0.55, maxZoom: 19 });

function setStatus(m, err) { $("mapStatus").textContent = m; $("mapStatus").style.color = err ? "#dc2626" : ""; }

async function loadBigFires() {
  try {
    const d = await api("/api/fires?region=canada&days=3");
    fireLayer.clearLayers();
    d.fires.forEach((f) => L.circleMarker([f.lat, f.lon], {
      radius: 3, color: "#dc2626", fillColor: "#dc2626", fillOpacity: 0.85, weight: 0.5,
    }).bindPopup(`<div class="pop"><div class="pop-head">Active fire</div><div class="pop-line">${f.province || ""} · ${f.acq_date}<br>Power ${f.frp ?? "n/a"} MW</div></div>`).addTo(fireLayer));
    setStatus(`${d.count.toLocaleString()} active fires live · disturbance live · loss ${lossStart} to ${lossEnd}`);
  } catch (e) { setStatus(e.message, true); }
}
async function loadBigRisk() {
  try {
    const { zones } = await api("/api/risk?days=5");
    riskLayer.clearLayers();
    zones.forEach((z) => {
      const c = z.forecast_risk >= 70 ? "#dc2626" : z.forecast_risk >= 50 ? "#ea8a2a" : "#eab308";
      L.rectangle([[z.south, z.west], [z.north, z.east]], { color: c, weight: 1.5, dashArray: "4,3", fillColor: c, fillOpacity: 0.22 })
        .bindPopup(`<div class="pop"><div class="pop-head">Risk forecast ${z.forecast_risk}/100</div><div class="pop-line">Weather ${z.weather_label ?? "?"} · wind ${z.wind_kmh ?? "?"} km/h</div></div>`).addTo(riskLayer);
    });
  } catch (e) { /* non-fatal */ }
}

// ---- Minutes-level GOES flash fires -----------------------------------------
function goesMarker(lat, lon, popupHtml, fresh = true) {
  // Only fresh detections get the animated ping. Hundreds of infinite
  // animations melt the compositor, and a static amber dot reads fine.
  const m = L.marker([lat, lon], {
    icon: L.divIcon({ className: "", html: `<div class="${fresh ? "goes-pulse" : "goes-dot"}"></div>`, iconSize: [14, 14], iconAnchor: [7, 7] }),
  });
  if (popupHtml) m.bindPopup(popupHtml);
  return m;
}
async function loadGoesFires() {
  try {
    const d = await api("/api/fires/latest?minutes=90");
    goesLayer.clearLayers();
    d.fires.forEach((f) => goesMarker(f.lat, f.lon,
      `<div class="pop"><div class="pop-head">Flash fire · GOES</div><div class="pop-line">Detected ${f.age_min} min ago${f.province ? " · " + f.province : ""}<br>Power ${f.frp ? Math.round(f.frp) + " MW" : "n/a"}</div></div>`,
      f.age_min <= 30
    ).addTo(goesLayer));
    $("mapStatus").dataset.goes = d.count;
  } catch (e) { /* non-fatal */ }
}

// ---- Lightning + ignition watch ----------------------------------------------
function boltMarker(lat, lon, ageMin) {
  const op = Math.max(0.25, 1 - (ageMin ?? 0) / 60);
  return L.marker([lat, lon], {
    opacity: op,
    icon: L.divIcon({ className: "", html: '<div class="bolt-dot"></div>', iconSize: [8, 8], iconAnchor: [4, 4] }),
  });
}
async function loadLightning() {
  try {
    const d = await api("/api/lightning?minutes=60");
    boltLayer.clearLayers();
    ignitionLayer.clearLayers();
    d.strikes.slice(0, 800).forEach((s) => boltMarker(s.lat, s.lon, s.age_min).addTo(boltLayer));
    (d.ignition || []).filter((z) => z.ignition_risk >= 40).forEach((z) => {
      L.circle([z.lat, z.lon], { radius: 28000, color: "#f59e0b", weight: 2, dashArray: "6,4", fillColor: "#f59e0b", fillOpacity: 0.12 })
        .bindPopup(`<div class="pop"><div class="pop-head">Ignition watch ${z.ignition_risk}/100</div><div class="pop-line">${z.strikes} strikes in the last hour.<br>Fire weather ${z.weather_label ?? "?"} (${z.weather_risk ?? "?"}/100). No fire yet. Watching.</div></div>`)
        .addTo(ignitionLayer);
    });
    $("lyrBolt").parentElement.title = d.available ? "Feed connected" : "Feed connecting…";
  } catch (e) { /* non-fatal */ }
}

// ---- Live WebSocket feed -------------------------------------------------------
let feedCount = 0;
function addFeedItem(ev, seed) {
  const box = $("liveFeedItems");
  const div = document.createElement("div");
  div.className = "feed-item" + (seed ? " seed" : "");
  const when = ev.at ? ev.at.slice(11, 16) + " UTC" : "";
  div.innerHTML = `<span class="fi-dot ${ev.type}"></span><span class="fi-text"></span><span class="fi-time">${when}</span>`;
  div.querySelector(".fi-text").textContent = ev.text;
  if (ev.lat != null && ev.lon != null) {
    div.classList.add("clickable");
    div.addEventListener("click", () => bigmap.flyTo([ev.lat, ev.lon], 8));
  }
  box.prepend(div);
  while (box.children.length > 8) box.removeChild(box.lastChild);
  if (!seed) feedCount++;
}
function handleLiveEvent(ev) {
  if (ev.type === "hello") { (ev.events || []).slice().reverse().forEach((e) => addFeedItem(e, true)); return; }
  addFeedItem(ev);
  if (ev.type === "fire" && ev.lat != null) {
    goesMarker(ev.lat, ev.lon,
      `<div class="pop"><div class="pop-head">Flash fire · GOES</div><div class="pop-line">Just detected${ev.province ? " · " + ev.province : ""}<br>${ev.frp ? Math.round(ev.frp) + " MW" : ""}</div></div>`
    ).addTo(goesLayer);
  }
  if (ev.type === "lightning" && ev.lat != null && bigmap.hasLayer(boltLayer)) {
    const m = boltMarker(ev.lat, ev.lon, 0).addTo(boltLayer);
    setTimeout(() => boltLayer.removeLayer(m), 10 * 60 * 1000);
  }
  if (ev.type === "ignition" && ev.lat != null) loadLightning();
}
let wsRetry = 1000;
function connectLive() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  ws.onopen = () => { wsRetry = 1000; $("liveFeedState").textContent = "connected"; };
  ws.onmessage = (m) => { try { handleLiveEvent(JSON.parse(m.data)); } catch (e) { /* ignore */ } };
  ws.onclose = () => {
    $("liveFeedState").textContent = "reconnecting…";
    setTimeout(connectLive, wsRetry);
    wsRetry = Math.min(wsRetry * 2, 30000);
  };
}

// ---- Smoke (Insights tab) ------------------------------------------------------
async function loadSmoke() {
  try {
    const { cities } = await api("/api/air");
    $("smokeBox").innerHTML = cities.map((c) => `
      <div class="smoke-row">
        <span class="smoke-city">${c.city}</span>
        <span class="smoke-badge ${(c.label || "").split(" ")[0].toLowerCase()}">${c.label ?? "?"}</span>
        <span class="smoke-val">${c.pm25_now ?? "?"} µg/m³</span>
      </div>`).join("") || "No data.";
  } catch (e) { $("smokeBox").textContent = e.message; }
}

// ---- Layer wiring: switches + rail stay in sync ------------------------------
const LAYERS = {
  lyrLoss: { layer: lossLayer, rail: "railLoss" },
  lyrFire: { layer: fireLayer, rail: "railFire" },
  lyrDist: { layer: distLayer, rail: "railDist" },
  lyrRisk: { layer: riskLayer, rail: "railRisk", onEnable: loadBigRisk },
  lyrGlad: { layer: gladLayer, rail: null },
  lyrGoes: { layer: goesLayer, rail: "railGoes", onEnable: loadGoesFires },
  lyrBolt: { layer: boltLayer, rail: "railBolt", onEnable: loadLightning, twin: ignitionLayer },
  lyrPark: { layer: parkLayer, rail: "railPark" },
};
function applyLayer(chkId, on) {
  const cfg = LAYERS[chkId];
  $(chkId).checked = on;
  if (cfg.rail) $(cfg.rail).classList.toggle("active", on);
  if (on) { cfg.layer.addTo(bigmap); if (cfg.twin) cfg.twin.addTo(bigmap); if (cfg.onEnable) cfg.onEnable(); }
  else { bigmap.removeLayer(cfg.layer); if (cfg.twin) bigmap.removeLayer(cfg.twin); }
}
Object.keys(LAYERS).forEach((chkId) => {
  $(chkId).addEventListener("change", (e) => applyLayer(chkId, e.target.checked));
  const railId = LAYERS[chkId].rail;
  if (railId) $(railId).addEventListener("click", () => applyLayer(chkId, !$(chkId).checked));
});

$("lossOpacity").addEventListener("input", (e) => { lossLayer.setOpacity(e.target.value / 100); if (miniLoss) miniLoss.setOpacity(e.target.value / 100); });
$("distOpacity").addEventListener("input", (e) => distLayer.setOpacity(e.target.value / 100));
$("gladOpacity").addEventListener("input", (e) => gladLayer.setOpacity(e.target.value / 100));
$("lossYear").addEventListener("input", (e) => {
  lossEnd = +e.target.value; $("lossYearLabel").textContent = `${lossStart}-${lossEnd}`;
  lossLayer.redecode(); if (miniLoss) miniLoss.redecode();
});
$("canopy").addEventListener("change", (e) => {
  lossCanopy = "tcd_" + e.target.value.replace(/\D/g, "");
  lossLayer.redraw(); if (miniLoss) miniLoss.redraw();
});
$("baseSat").addEventListener("change", (e) => {
  if (e.target.checked) { bSat = baseSat().addTo(bigmap); }
  else if (bSat) { bigmap.removeLayer(bSat); bSat = null; }
});

// Locate buttons
$("locOn").addEventListener("click", () => bigmap.flyTo(VIEWS.ontario.center, VIEWS.ontario.zoom));
$("locCa").addEventListener("click", () => bigmap.flyTo(VIEWS.canada.center, VIEWS.canada.zoom));

// Panel tabs
document.querySelectorAll(".ptab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".ptab").forEach((x) => x.classList.remove("active")); b.classList.add("active");
  $("ptab-legend").hidden = b.dataset.ptab !== "legend"; $("ptab-analysis").hidden = b.dataset.ptab !== "analysis";
  if (b.dataset.ptab === "analysis") { loadMapAnalysis(); loadSmoke(); }
}));
async function loadMapAnalysis() {
  $("mapAnalysis").textContent = "Running live check…";
  try {
    const r = await api("/api/analysis/run?send=false", { method: "POST" });
    $("mapAnalysis").innerHTML =
      `<b>Ontario live check</b><br>` +
      `Severity <b>${r.severity.toUpperCase()}</b><br>` +
      `${r.today_count} fires today, ${r.net_change >= 0 ? "up" : "down"} ${Math.abs(r.net_change)} vs yesterday<br>` +
      `${r.new_cluster_count} new fire clusters${r.is_anomaly ? ", statistically unusual day" : ""}<br>` +
      `Estimated ${r.impact.area_km2} km² burned, ${r.impact.co2_kilotonnes} kt CO₂`;
  } catch (e) { $("mapAnalysis").textContent = e.message; }
}

// ---- Point intelligence -------------------------------------------------------
let inspectOn = false;
$("inspectBtn").addEventListener("click", () => {
  inspectOn = !inspectOn;
  $("inspectBtn").classList.toggle("on", inspectOn);
  bigmap.getContainer().style.cursor = inspectOn ? "crosshair" : "";
});
bigmap.on("click", async (e) => {
  if (!inspectOn) return;
  const { lat, lng } = e.latlng;
  const pop = L.popup({ maxWidth: 300 })
    .setLatLng(e.latlng)
    .setContent('<div class="pop"><div class="pop-line">Reading this spot…</div></div>')
    .openOn(bigmap);
  try {
    const d = await api(`/api/inspect?lat=${lat.toFixed(4)}&lon=${lng.toFixed(4)}`);
    const w = d.weather;
    pop.setContent(`
      <div class="pop">
        <div class="pop-head">${d.province || "Site report"} · ${lat.toFixed(2)}, ${lng.toFixed(2)}</div>
        <div class="pop-stat">
          <div><b>${d.fires_within_25km}</b><span>fires &lt; 25 km</span></div>
          <div><b>${d.fires_within_100km}</b><span>fires &lt; 100 km</span></div>
          <div><b>${w ? w.risk : "?"}</b><span>risk tomorrow</span></div>
        </div>
        ${d.protected ? `<div class="pop-line" style="margin-top:4px"><span class="pop-dot"></span>${d.protected.name} · ${d.protected.designation ?? "protected area"}</div>` : ""}
        <div class="pop-line">${d.headline}</div>
        ${d.nearest.length ? `<div class="pop-line" style="margin-top:6px">Nearest fire ${d.nearest[0].km} km away, ${d.nearest[0].frp ?? "?"} MW, ${d.nearest[0].date}</div>` : ""}
      </div>`);
  } catch (err) {
    pop.setContent(`<div class="pop"><div class="pop-line">${err.message}</div></div>`);
  }
});

// ---- Ask Canopy -----------------------------------------------------------------
$("chatHead").addEventListener("click", () => $("chatDock").classList.toggle("closed"));
function chatMsg(text, who) {
  const div = document.createElement("div");
  div.className = `msg ${who}`;
  div.textContent = text;
  $("chatLog").appendChild(div);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
  return div;
}
$("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("chatInput").value.trim();
  if (!q) return;
  $("chatInput").value = "";
  $("chatInput").disabled = true;
  chatMsg(q, "user");
  const typing = chatMsg("canopy is reading the live data…", "bot typing");
  try {
    const r = await api("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    typing.classList.remove("typing");
    typing.textContent = r.text;
  } catch (err) {
    typing.classList.remove("typing");
    typing.textContent = "Something broke: " + err.message;
  } finally {
    $("chatInput").disabled = false;
    $("chatInput").focus();
    $("chatLog").scrollTop = $("chatLog").scrollHeight;
  }
});

// ====================== DASHBOARD ===========================================
const minimap = L.map("minimap", { zoomControl: true }).setView(VIEWS.ontario.center, VIEWS.ontario.zoom);
minimap.createPane("mloss"); minimap.getPane("mloss").style.zIndex = 350;
baseLight().addTo(minimap);
const miniLoss = decodeGridLayer({ pane: "mloss", opacity: 0.9, urlFn: lossUrl, decode: decodeLoss }).addTo(minimap);
let miniBox = null, catalog = {}, trendChart, lossChart;

async function loadCatalog() { (await api("/api/regions")).regions.forEach((r) => (catalog[r.id] = r)); }

async function loadRegion(id) {
  let r; try { r = await api(`/api/region/${id}`); } catch (e) { return; }

  const chain = []; let cur = r.id;
  while (cur) { chain.unshift(catalog[cur] || { id: cur, name: r.name }); cur = (catalog[cur] || {}).parent; }
  $("breadcrumb").innerHTML = chain.map((c, i) =>
    `<div class="crumb" data-id="${c.id}">${i ? '<span class="chev">›</span>' : ""}${c.name}</div>`).join("");
  document.querySelectorAll(".crumb").forEach((el) => el.addEventListener("click", () => loadRegion(el.dataset.id)));

  $("regionSelectWrap").innerHTML = r.children.length
    ? `<select id="regionSelect"><option value="">Select a region…</option>` +
      r.children.map((c) => `<option value="${c.value}">${c.id}</option>`).join("") + `</select>`
    : "";
  if (r.children.length) $("regionSelect").addEventListener("change", (e) => e.target.value && loadRegion(e.target.value));

  $("dashSummary").innerHTML = r.summary + (r.approx ? ' <span class="source-tag">(figures approximate)</span>' : "");

  const f = r.forest;
  $("mForest").textContent = f.forest_mha;
  $("mLoss").textContent = `${f.loss_value} ${f.loss_unit}`;
  $("mLossLbl").textContent = `forest lost (${f.loss_year})`;
  $("mCo2").textContent = f.co2;
  $("sourceTag").textContent = `Loss figures: ${f.source}. Fires: NASA FIRMS, live.`;
  $("fLoss").textContent = `${f.loss_value} ${f.loss_unit}`;
  $("fLossLbl").textContent = `forest lost (${f.loss_year})`;
  $("fPct").textContent = `${f.land_pct}%`;
  $("sourceTag2").textContent = `Source: ${f.source}`;

  renderLossChart(r);

  const fi = r.fires;
  $("firToday").textContent = fi.error ? "?" : fi.today;
  $("firArea").textContent = fi.impact.area_km2;
  $("firCo2").textContent = fi.impact.co2_kilotonnes;
  renderTrend(fi.trend);

  // Ontario district table, only for Canada and Ontario
  const showDistricts = id === "CAN" || id === "ON";
  $("districtSection").hidden = !showDistricts;
  if (showDistricts) loadDistricts();

  minimap.setView(r.center, r.zoom);
  if (miniBox) minimap.removeLayer(miniBox);
  const [w, s, e, n] = r.bbox;
  miniBox = L.rectangle([[s, w], [n, e]], { color: "#16a34a", weight: 2, fill: false }).addTo(minimap);
  setTimeout(() => minimap.invalidateSize(), 80);
}

async function loadDistricts() {
  try {
    const d = await api("/api/ontario/live");
    const max = Math.max(1, ...d.districts.map((x) => x.today));
    $("districtTable").innerHTML = d.districts.map((x) => `
      <div class="drow" data-id="${x.id}">
        <span class="dname">${x.name}</span>
        <span class="dbar-wrap"><span class="dbar" style="width:${(x.today / max) * 100}%"></span></span>
        <span class="dcount">${x.today}<span class="dunit">today</span></span>
      </div>`).join("");
    document.querySelectorAll(".drow").forEach((el) =>
      el.addEventListener("click", () => loadRegion(el.dataset.id)));
  } catch (e) { $("districtTable").innerHTML = `<div class="feed-row">${e.message}</div>`; }
}

async function loadAlertFeed() {
  try {
    let last = await api("/api/analysis/last");
    if (!last.result) {
      await api("/api/analysis/run?send=false", { method: "POST" });
      last = await api("/api/analysis/last");
    }
    const r = last.result;
    if (!r) { $("alertFeed").textContent = "No analysis yet."; return; }
    const when = (last.ran_at || "").replace("T", " ").replace("+00:00", " UTC");
    const rows = (r.new_clusters || []).slice(0, 5).map((c) => `
      <div class="feed-row">
        <span class="feed-dot ${r.severity}"></span>
        <span class="feed-main">New fire cluster near ${c.lat}, ${c.lon}. ${c.size} detections, ${c.total_frp} MW.</span>
        <span class="feed-meta">today</span>
      </div>`).join("");
    $("alertFeed").innerHTML = `
      <div class="feed-row">
        <span class="feed-dot ${r.severity}"></span>
        <span class="feed-main"><b>${r.today_count} fires in Ontario today.</b> Severity ${r.severity}. ${r.new_cluster_count} new clusters.</span>
        <span class="feed-meta">${when}</span>
      </div>` + rows;
  } catch (e) { $("alertFeed").textContent = e.message; }
}

function toHa(value, unit) { return unit === "Mha" ? value * 1e6 : unit === "kha" ? value * 1e3 : value; }
function renderLossChart(r) {
  let labels, values, live = !!r.loss_live;
  if (live && r.loss_by_year) {
    labels = r.loss_by_year.map((d) => d.year);
    values = r.loss_by_year.map((d) => Math.round(d.area_ha / 1000));
  } else {
    labels = [r.forest.loss_year];
    values = [Math.round(toHa(r.forest.loss_value, r.forest.loss_unit) / 1000)];
  }
  $("lossLiveTag").innerHTML = live
    ? '<span class="live-tag">live · GFW API</span>'
    : '<span class="live-tag curated">curated</span>';
  if (lossChart) { lossChart.data.labels = labels; lossChart.data.datasets[0].data = values; lossChart.update(); return; }
  lossChart = new Chart($("lossYearChart"), {
    type: "bar", data: { labels, datasets: [{ label: "kha lost", data: values, backgroundColor: "#dc6699", borderRadius: 3 }] },
    options: { plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: "#98a2b3", font: { size: 9 } }, grid: { display: false } },
                y: { ticks: { color: "#98a2b3", font: { size: 9 } }, grid: { color: "#f2f4f7" } } } },
  });
}
function renderTrend(trend) {
  const labels = (trend || []).map((d) => d.date.slice(5));
  const values = (trend || []).map((d) => d.count);
  if (trendChart) { trendChart.data.labels = labels; trendChart.data.datasets[0].data = values; trendChart.update(); return; }
  trendChart = new Chart($("fireTrend"), {
    type: "bar", data: { labels, datasets: [{ data: values, backgroundColor: "#dc2626", borderRadius: 3 }] },
    options: { plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: "#98a2b3", font: { size: 10 } }, grid: { display: false } },
                y: { ticks: { color: "#98a2b3", font: { size: 10 } }, grid: { color: "#f2f4f7" } } } },
  });
}

document.querySelectorAll(".dtab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".dtab").forEach((x) => x.classList.remove("active")); b.classList.add("active");
  ["summary", "forest", "fires"].forEach((t) => $(`dtab-${t}`).hidden = t !== b.dataset.dtab);
}));

// ---- ML + phone alerts ----
async function runAnalysis() {
  $("runNow").disabled = true; $("analysisResult").textContent = "Running…";
  try {
    const r = await api("/api/analysis/run?send=false", { method: "POST" });
    const cl = (r.new_clusters || []).slice(0, 3).map((c) => `<li>${c.size} detections near ${c.lat}, ${c.lon}, ${c.total_frp} MW</li>`).join("");
    $("analysisResult").innerHTML = `<span class="badge ${r.severity}">${r.severity.toUpperCase()}</span>
      <div>${r.today_count} fires today, ${r.net_change >= 0 ? "up" : "down"} ${Math.abs(r.net_change)} vs yesterday. ${r.new_cluster_count} new clusters${r.is_anomaly ? ". Unusual day" : ""}.</div>${cl ? `<ul>${cl}</ul>` : ""}`;
    loadAlertFeed();
  } catch (e) { $("analysisResult").textContent = e.message; } finally { $("runNow").disabled = false; }
}
async function findChat() {
  $("chatResult").textContent = "Checking…";
  try {
    const r = await api("/api/alert/chatid");
    if (!r.ok) return ($("chatResult").textContent = r.error);
    if (!r.chats.length) return ($("chatResult").textContent = "No messages yet. Message your bot first, then retry.");
    $("chatResult").innerHTML = r.chats.map((c) => `<span class="pick" data-id="${c.chat_id}">${c.name || c.chat_id} (${c.chat_id})</span>`).join(", ");
    document.querySelectorAll(".pick").forEach((el) => el.addEventListener("click", () => ($("chatId").value = el.dataset.id)));
  } catch (e) { $("chatResult").textContent = e.message; }
}
async function saveCfg() { try { await api("/api/alert/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ telegram_chat_id: $("chatId").value }) }); $("chatResult").textContent = "Saved."; } catch (e) { $("chatResult").textContent = e.message; } }
async function testAlert() { try { await api("/api/alert/test", { method: "POST" }); $("chatResult").textContent = "Test alert sent. Check your phone."; } catch (e) { $("chatResult").textContent = "Failed: " + e.message; } }
async function loadLog() {
  try {
    const { alerts } = await api("/api/alert/log");
    $("alertLog").innerHTML = alerts.length
      ? alerts.map((a) => `<div><span class="${a.delivered ? "ok" : "fail"}">${a.delivered ? "✓" : "✗"}</span> ${a.time.replace("T", " ").replace("+00:00", "")} · ${a.severity} · ${a.new_clusters} new</div>`).join("")
      : "No alerts yet.";
  } catch (e) { /* ignore */ }
}
$("runNow").addEventListener("click", runAnalysis);
$("findChat").addEventListener("click", findChat);
$("saveCfg").addEventListener("click", saveCfg);
$("testAlert").addEventListener("click", testAlert);
$("shareBtn").addEventListener("click", () => { navigator.clipboard?.writeText(location.href); $("shareBtn").textContent = "Copied"; setTimeout(() => ($("shareBtn").textContent = "Share"), 1500); });
$("downloadBtn").addEventListener("click", () => window.open("/api/region/CAN", "_blank"));

// ====================== View switching ======================================
function showView(v) {
  document.querySelectorAll(".nav-link").forEach((l) => l.classList.toggle("active", l.dataset.view === v));
  $("view-map").hidden = v !== "map"; $("view-dash").hidden = v !== "dash";
  setTimeout(() => (v === "map" ? bigmap : minimap).invalidateSize(), 60);
}
document.body.addEventListener("click", (e) => {
  const link = e.target.closest("[data-view]");
  if (link) { e.preventDefault(); showView(link.dataset.view); }
});

// ====================== Boot ================================================
(async function () {
  loadBigFires();
  loadGoesFires();
  loadLightning();
  connectLive();
  await loadCatalog();
  await loadRegion("ON");
  loadAlertFeed();
  loadLog();
  setInterval(loadBigFires, 5 * 60 * 1000);
  setInterval(() => { if (bigmap.hasLayer(goesLayer)) loadGoesFires(); }, 2 * 60 * 1000);
  setInterval(() => { if (bigmap.hasLayer(boltLayer)) loadLightning(); }, 2 * 60 * 1000);
})();
