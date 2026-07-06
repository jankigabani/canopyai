// CanopyAI — Global-Forest-Watch-style platform (with real pixel-decode layers).

const $ = (id) => document.getElementById(id);
async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

// Basemaps (loaded directly; overlay tiles are proxied for canvas decoding).
const TILES = {
  light: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  sat: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
};
const baseLight = () => L.tileLayer(TILES.light, { attribution: "&copy; OSM &copy; CARTO", maxZoom: 19 });
const baseSat = () => L.tileLayer(TILES.sat, { attribution: "Imagery &copy; Esri", maxZoom: 19 });

// ---- Decode state ----------------------------------------------------------
let lossStart = 2001, lossEnd = 2024, lossCanopy = "tcd_30";

// Generic canvas-decode GridLayer: fetches an ENCODED PNG tile (same-origin
// proxy), reads the pixels, and recolors them per a decode() function. Caches
// the raw pixels so a filter change re-colors instantly without re-fetching.
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
      } catch (e) { /* tainted / empty */ }
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

// Tree-cover-loss decode: BLUE channel = loss year (2000+B), RED = intensity.
function decodeLoss(imageData, zoom) {
  const d = imageData.data;
  const exp = zoom < 13 ? 0.3 + (zoom - 3) / 20 : 1;
  const maxPow = Math.pow(255, exp);
  for (let i = 0; i < d.length; i += 4) {
    if (!d[i + 3]) continue;
    const year = 2000 + d[i + 2];
    if (year >= lossStart && year <= lossEnd) {
      const scale = Math.pow(d[i], exp) / maxPow;           // intensity -> alpha
      d[i] = 220; d[i + 1] = 102; d[i + 2] = 153;           // GFW pink
      d[i + 3] = Math.min(255, Math.round(scale * 255));
    } else { d[i + 3] = 0; }
  }
}

// GLAD/RADD integrated-alert decode: BLUE >= 200 = high confidence.
function decodeGlad(imageData) {
  const d = imageData.data;
  for (let i = 0; i < d.length; i += 4) {
    if (!d[i + 3]) continue;
    const high = d[i + 2] >= 200;
    d[i] = high ? 221 : 247; d[i + 1] = high ? 28 : 104; d[i + 2] = high ? 119 : 161;
    d[i + 3] = 190;
  }
}

// OPERA DIST-ALERT decode: BLUE >= 200 = high/confirmed disturbance.
function decodeDist(imageData) {
  const d = imageData.data;
  for (let i = 0; i < d.length; i += 4) {
    if (!d[i + 3]) continue;
    const high = d[i + 2] >= 200;
    d[i] = high ? 123 : 177; d[i + 1] = high ? 44 : 122; d[i + 2] = high ? 191 : 220;  // violet
    d[i + 3] = 200;
  }
}

const lossUrl = (c) => `/api/tile/loss/${lossCanopy}/${c.z}/${c.x}/${c.y}.png`;
const gladUrl = (c) => `/api/tile/glad/${c.z}/${c.x}/${c.y}.png`;
const distUrl = (c) => `/api/tile/dist/${c.z}/${c.x}/${c.y}.png`;

// ====================== MAP VIEW ===========================================
const bigmap = L.map("bigmap", { zoomControl: true }).setView([58, -96], 4);
bigmap.createPane("loss"); bigmap.getPane("loss").style.zIndex = 350;
bigmap.createPane("dist"); bigmap.getPane("dist").style.zIndex = 355;
bigmap.createPane("glad"); bigmap.getPane("glad").style.zIndex = 360;
const bLight = baseLight().addTo(bigmap);
let bSat = null;
const lossLayer = decodeGridLayer({ pane: "loss", opacity: 0.9, urlFn: lossUrl, decode: decodeLoss }).addTo(bigmap);
const distLayer = decodeGridLayer({ pane: "dist", opacity: 0.85, urlFn: distUrl, decode: decodeDist });
const gladLayer = decodeGridLayer({ pane: "glad", opacity: 0.8, urlFn: gladUrl, decode: decodeGlad });
const fireLayer = L.layerGroup().addTo(bigmap);
const riskLayer = L.layerGroup();

function setStatus(m, err) { $("mapStatus").textContent = m; $("mapStatus").style.color = err ? "#c0392b" : "#777"; }

async function loadBigFires() {
  try {
    const d = await api("/api/fires?region=canada&days=3");
    fireLayer.clearLayers();
    d.fires.forEach((f) => L.circleMarker([f.lat, f.lon], {
      radius: 3, color: "#e0433f", fillColor: "#e0433f", fillOpacity: 0.85, weight: 0.5,
    }).bindPopup(`🔥 ${f.province || ""} ${f.acq_date}<br>FRP ${f.frp ?? "n/a"} MW`).addTo(fireLayer));
    setStatus(`${d.count} active fires (live) · tree-cover loss ${lossStart}–${lossEnd}`);
  } catch (e) { setStatus(e.message, true); }
}
async function loadBigRisk() {
  try {
    const { zones } = await api("/api/risk?days=5");
    riskLayer.clearLayers();
    zones.forEach((z) => {
      const c = z.forecast_risk >= 70 ? "#e0433f" : z.forecast_risk >= 50 ? "#e8862a" : "#ffd24d";
      L.rectangle([[z.south, z.west], [z.north, z.east]], { color: c, weight: 1.5, dashArray: "4,3", fillColor: c, fillOpacity: 0.25 })
        .bindPopup(`🔮 Risk ${z.forecast_risk}/100 · ${z.weather_label ?? "?"} · Wind ${z.wind_kmh ?? "?"} km/h`).addTo(riskLayer);
    });
  } catch (e) { /* non-fatal */ }
}

// Controls
$("lyrLoss").addEventListener("change", (e) => e.target.checked ? lossLayer.addTo(bigmap) : bigmap.removeLayer(lossLayer));
$("lyrDist").addEventListener("change", (e) => e.target.checked ? distLayer.addTo(bigmap) : bigmap.removeLayer(distLayer));
$("lyrGlad").addEventListener("change", (e) => e.target.checked ? gladLayer.addTo(bigmap) : bigmap.removeLayer(gladLayer));
$("lyrFire").addEventListener("change", (e) => e.target.checked ? fireLayer.addTo(bigmap) : bigmap.removeLayer(fireLayer));
$("distOpacity").addEventListener("input", (e) => distLayer.setOpacity(e.target.value / 100));

// Combined "live disturbance" toggle — fires (active burning) + DIST-ALERT
// (vegetation loss), the two near-real-time signals, on together.
function setLayer(chkId, layer, on) {
  const box = $(chkId); box.checked = on;
  on ? layer.addTo(bigmap) : bigmap.removeLayer(layer);
}
$("liveDisturb").addEventListener("click", () => {
  const on = !$("liveDisturb").classList.contains("on");
  $("liveDisturb").classList.toggle("on", on);
  setLayer("lyrFire", fireLayer, on);
  setLayer("lyrDist", distLayer, on);
  setStatus(on ? "Live disturbance ON — active fires (red) + DIST-ALERT vegetation loss (violet)" : "Live disturbance off");
});
$("lyrRisk").addEventListener("change", (e) => { if (e.target.checked) { riskLayer.addTo(bigmap); loadBigRisk(); } else bigmap.removeLayer(riskLayer); });
$("lossOpacity").addEventListener("input", (e) => { lossLayer.setOpacity(e.target.value / 100); if (miniLoss) miniLoss.setOpacity(e.target.value / 100); });
$("gladOpacity").addEventListener("input", (e) => gladLayer.setOpacity(e.target.value / 100));
$("lossYear").addEventListener("input", (e) => {
  lossEnd = +e.target.value; $("lossYearLabel").textContent = `${lossStart}–${lossEnd}`;
  lossLayer.redecode(); if (miniLoss) miniLoss.redecode();
});
$("canopy").addEventListener("change", (e) => {
  lossCanopy = "tcd_" + e.target.value.replace(/\D/g, "");
  lossLayer.redraw(); if (miniLoss) miniLoss.redraw();
});
$("baseSat").addEventListener("change", (e) => {
  if (e.target.checked) { bSat = baseSat().addTo(bigmap); bSat.bringToFront(); }
  else if (bSat) { bigmap.removeLayer(bSat); bSat = null; }
});

// Panel tabs
document.querySelectorAll(".ptab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".ptab").forEach((x) => x.classList.remove("active")); b.classList.add("active");
  $("ptab-legend").hidden = b.dataset.ptab !== "legend"; $("ptab-analysis").hidden = b.dataset.ptab !== "analysis";
  if (b.dataset.ptab === "analysis") loadMapAnalysis();
}));
async function loadMapAnalysis() {
  $("mapAnalysis").textContent = "Running ML change-detection…";
  try {
    const r = await api("/api/analysis/run?send=false", { method: "POST" });
    $("mapAnalysis").innerHTML =
      `<b>Ontario — live ML check</b><br>Severity: <b>${r.severity.toUpperCase()}</b><br>` +
      `Fires today: ${r.today_count} (${r.net_change >= 0 ? "+" : ""}${r.net_change} vs yesterday)<br>` +
      `New fire clusters: <b>${r.new_cluster_count}</b>${r.is_anomaly ? " · ⚠️ anomaly" : ""}<br>` +
      `Est. impact: ${r.impact.area_km2} km² · ${r.impact.co2_kilotonnes} kt CO₂`;
  } catch (e) { $("mapAnalysis").textContent = e.message; }
}

// ====================== DASHBOARD VIEW =====================================
const minimap = L.map("minimap", { zoomControl: true }).setView([58, -96], 4);
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
    `<div class="crumb" data-id="${c.id}">${i ? '<span class="chev">▸</span>' : ""}${c.name}</div>`).join("");
  document.querySelectorAll(".crumb").forEach((el) => el.addEventListener("click", () => loadRegion(el.dataset.id)));

  $("regionSelectWrap").innerHTML = r.children.length
    ? `<select id="regionSelect"><option value="">Select a region…</option>` +
      r.children.map((c) => `<option value="${c.value}">${c.id}</option>`).join("") + `</select>`
    : "";
  if (r.children.length) $("regionSelect").addEventListener("change", (e) => e.target.value && loadRegion(e.target.value));

  $("dashSummary").innerHTML = r.summary + (r.approx ? ' <span class="note">(figures approximate)</span>' : "");

  const f = r.forest;
  $("mForest").textContent = f.forest_mha;
  $("mLoss").textContent = `${f.loss_value} ${f.loss_unit}`;
  $("mLossLbl").textContent = `forest lost (${f.loss_year})`;
  $("mCo2").textContent = f.co2;
  $("dashIntro").textContent = `Forest change and live fire activity in ${r.name}. Loss figures: ${f.source}; fires: live NASA FIRMS.`;
  $("sourceTag").textContent = `Source: ${f.source}`;
  $("fLoss").textContent = `${f.loss_value} ${f.loss_unit}`;
  $("fLossLbl").textContent = `forest lost (${f.loss_year})`;
  $("fPct").textContent = `${f.land_pct}%`;
  $("sourceTag2").textContent = `Source: ${f.source}`;

  renderLossChart(r);

  const fi = r.fires;
  $("firToday").textContent = fi.error ? "—" : fi.today;
  $("firArea").textContent = fi.impact.area_km2;
  $("firCo2").textContent = fi.impact.co2_kilotonnes;
  renderTrend(fi.trend);

  minimap.setView(r.center, r.zoom);
  if (miniBox) minimap.removeLayer(miniBox);
  const [w, s, e, n] = r.bbox;
  miniBox = L.rectangle([[s, w], [n, e]], { color: "#97bd3d", weight: 2, fill: false }).addTo(minimap);
  setTimeout(() => minimap.invalidateSize(), 80);
}

function toHa(value, unit) { return unit === "Mha" ? value * 1e6 : unit === "kha" ? value * 1e3 : value; }
function renderLossChart(r) {
  let labels, values, live = !!r.loss_live;
  if (live && r.loss_by_year) {
    labels = r.loss_by_year.map((d) => d.year);
    values = r.loss_by_year.map((d) => Math.round(d.area_ha / 1000)); // kha
  } else {
    labels = [r.forest.loss_year];
    values = [Math.round(toHa(r.forest.loss_value, r.forest.loss_unit) / 1000)];
  }
  $("lossLiveTag").innerHTML = live
    ? '<span class="live-tag">LIVE · GFW Data API</span>'
    : '<span class="live-tag curated">curated — set GFW_API_KEY for live by-year</span>';
  if (lossChart) { lossChart.data.labels = labels; lossChart.data.datasets[0].data = values; lossChart.update(); return; }
  lossChart = new Chart($("lossYearChart"), {
    type: "bar", data: { labels, datasets: [{ label: "kha lost", data: values, backgroundColor: "#ff5db1", borderRadius: 3 }] },
    options: { plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: "#999", font: { size: 9 } }, grid: { display: false } },
                y: { ticks: { color: "#999", font: { size: 9 } }, grid: { color: "#eee" } } } },
  });
}
function renderTrend(trend) {
  const labels = (trend || []).map((d) => d.date.slice(5));
  const values = (trend || []).map((d) => d.count);
  if (trendChart) { trendChart.data.labels = labels; trendChart.data.datasets[0].data = values; trendChart.update(); return; }
  trendChart = new Chart($("fireTrend"), {
    type: "bar", data: { labels, datasets: [{ data: values, backgroundColor: "#e0433f", borderRadius: 4 }] },
    options: { plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: "#999", font: { size: 10 } }, grid: { display: false } },
                y: { ticks: { color: "#999", font: { size: 10 } }, grid: { color: "#eee" } } } },
  });
}

document.querySelectorAll(".dtab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".dtab").forEach((x) => x.classList.remove("active")); b.classList.add("active");
  ["summary", "forest", "fires"].forEach((t) => $(`dtab-${t}`).hidden = t !== b.dataset.dtab);
}));

// ---- ML + alerts (Fires tab) ----
async function runAnalysis() {
  $("runNow").disabled = true; $("analysisResult").textContent = "Running…";
  try {
    const r = await api("/api/analysis/run?send=false", { method: "POST" });
    const cl = (r.new_clusters || []).slice(0, 3).map((c) => `<li>${c.size} @ (${c.lat}, ${c.lon}) · ${c.total_frp} MW</li>`).join("");
    $("analysisResult").innerHTML = `<span class="badge ${r.severity}">${r.severity.toUpperCase()}</span>
      <div>Today ${r.today_count} · ${r.net_change >= 0 ? "+" : ""}${r.net_change} vs yesterday · 🆕 ${r.new_cluster_count} new${r.is_anomaly ? " · ⚠️ anomaly" : ""}</div>${cl ? `<ul>${cl}</ul>` : ""}`;
  } catch (e) { $("analysisResult").textContent = e.message; } finally { $("runNow").disabled = false; }
}
async function findChat() {
  $("chatResult").textContent = "Checking…";
  try {
    const r = await api("/api/alert/chatid");
    if (!r.ok) return ($("chatResult").textContent = r.error);
    if (!r.chats.length) return ($("chatResult").textContent = "No messages — message your bot first, then retry.");
    $("chatResult").innerHTML = r.chats.map((c) => `<span class="pick" data-id="${c.chat_id}">${c.name || c.chat_id} (${c.chat_id})</span>`).join(", ");
    document.querySelectorAll(".pick").forEach((el) => el.addEventListener("click", () => ($("chatId").value = el.dataset.id)));
  } catch (e) { $("chatResult").textContent = e.message; }
}
async function saveCfg() { try { await api("/api/alert/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ telegram_chat_id: $("chatId").value }) }); $("chatResult").textContent = "Saved."; } catch (e) { $("chatResult").textContent = e.message; } }
async function testAlert() { try { await api("/api/alert/test", { method: "POST" }); $("chatResult").textContent = "Test alert sent — check Telegram. 📲"; } catch (e) { $("chatResult").textContent = "Failed: " + e.message; } }
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
$("shareBtn").addEventListener("click", () => { navigator.clipboard?.writeText(location.href); $("shareBtn").textContent = "✓ COPIED"; setTimeout(() => ($("shareBtn").textContent = "⤴ SHARE"), 1500); });
$("downloadBtn").addEventListener("click", () => window.open("/api/region/CAN", "_blank"));

// ====================== View switching =====================================
function showView(v) {
  document.querySelectorAll(".nav-link").forEach((l) => l.classList.toggle("active", l.dataset.view === v));
  $("view-map").hidden = v !== "map"; $("view-dash").hidden = v !== "dash";
  setTimeout(() => (v === "map" ? bigmap : minimap).invalidateSize(), 60);
}
document.body.addEventListener("click", (e) => {
  const link = e.target.closest("[data-view]");
  if (link) { e.preventDefault(); showView(link.dataset.view); }
});

// ====================== Boot ===============================================
(async function () {
  loadBigFires();
  await loadCatalog();
  await loadRegion("CAN");
  loadLog();
  setInterval(loadBigFires, 5 * 60 * 1000);
})();
