"use strict";

// Everything that comes from an uploaded file is untrusted: it is only ever inserted as
// text (textContent / createTextNode), never as HTML.

const $ = (id) => document.getElementById(id);
// the canvas renderer cannot resolve CSS variables, so read the theme colours once
const theme = getComputedStyle(document.documentElement);
const COLORS = Object.fromEntries(
  ["ok", "warn", "error", "na"].map((k) => [k, theme.getPropertyValue(`--${k}`).trim()]),
);

const map = L.map("map", { preferCanvas: true }).setView([20, 78], 4);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

let dataLayer = null;
let layersByIndex = new Map();

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function setStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
}

const nf = (digits) => new Intl.NumberFormat(undefined, { maximumFractionDigits: digits });

function area(m2) {
  if (m2 == null) return "–";
  if (m2 >= 1e6) return `${nf(3).format(m2 / 1e6)} km²`;
  if (m2 >= 1e4) return `${nf(2).format(m2 / 1e4)} ha`;
  return `${nf(1).format(m2)} m²`;
}

function length(m) {
  if (m == null) return "–";
  return m >= 1000 ? `${nf(3).format(m / 1000)} km` : `${nf(1).format(m)} m`;
}

function pct(p) {
  if (p == null) return "–";
  return p < 0.001 ? `${p.toExponential(1)} %` : `${nf(4).format(p)} %`;
}

function severity(props) {
  if (props._status === "error") return "error";
  if (props._status !== "ok") return "na";
  return props._issues.length ? "warn" : "ok";
}

function featureName(props) {
  return props.Name || props.name || props.NAME || props.title || `#${props._index}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const detail = body?.detail || `${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return { status: res.status, body };
}

// --- upload -------------------------------------------------------------------------------

async function upload(file) {
  const params = new URLSearchParams({ strategy: $("strategy").value });
  const crs = $("crs").value.trim();
  if (crs) params.set("crs", crs);
  const form = new FormData();
  form.append("file", file);

  setStatus(`Uploading ${file.name}…`);
  try {
    const { body } = await api(`/api/files?${params}`, {
      method: "POST",
      body: form,
      headers: { Prefer: "wait=20" },
    });
    await showFile(await waitUntilDone(body));
    refreshRecent();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function waitUntilDone(file) {
  while (file.status === "PENDING" || file.status === "PROCESSING") {
    setStatus(`${file.filename}: ${file.status.toLowerCase()}…`);
    await new Promise((r) => setTimeout(r, 500));
    file = (await api(`/api/files/${file.id}`)).body;
  }
  return file;
}

// --- rendering ----------------------------------------------------------------------------

async function showFile(file) {
  renderSummary(file);
  if (dataLayer) dataLayer.remove();
  layersByIndex = new Map();
  $("features").replaceChildren();
  $("features-section").hidden = true;

  if (file.status === "FAILED") {
    setStatus(`${file.filename}: ${file.error?.message || "processing failed"}`, true);
    return;
  }
  setStatus(`${file.filename}: ${file.feature_count} features in ${file.processing_ms} ms`);

  const res = await fetch(`/api/files/${file.id}/geojson`);
  const geojson = await res.json();
  dataLayer = L.geoJSON(geojson, {
    style: (f) => {
      const color = COLORS[severity(f.properties)];
      return { color, weight: 2, fillColor: color, fillOpacity: 0.25 };
    },
    pointToLayer: (f, latlng) =>
      L.circleMarker(latlng, { radius: 6, color: COLORS[severity(f.properties)], weight: 2 }),
    onEachFeature: (f, layer) => {
      layer.bindPopup(() => popup(f.properties));
      layersByIndex.set(f.id, layer);
    },
  }).addTo(map);

  const bounds = dataLayer.getBounds();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 18 });
  renderFeatureList(geojson.features);
}

function renderSummary(file) {
  $("summary").hidden = false;
  $("summary-title").textContent = file.filename;
  const s = file.summary || {};
  const rows = [
    ["Status", file.status],
    ["Format", file.format || "–"],
    ["CRS", file.crs || "unknown"],
    ["Strategy", file.strategy],
    ["Features", file.feature_count ?? "–"],
    ["Total area", area(s.total_area_m2)],
    ["Total length", length(s.total_length_m)],
    ["Max deviation", pct(s.max_deviation_pct)],
  ];
  $("summary-list").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));

  const issues = [...(file.issues || []), ...(file.layers || []).flatMap((l) => l.issues)];
  $("file-issues").replaceChildren(...issues.map((i) => el("li", {}, i.message)));
}

function renderFeatureList(features) {
  $("features-section").hidden = features.length === 0;
  $("feature-count").textContent = `(${features.length})`;
  const shown = features.slice(0, 500);
  $("features").replaceChildren(
    ...shown.map((f) => {
      const p = f.properties;
      const value = p._area_m2 != null ? area(p._area_m2) : p._length_m != null ? length(p._length_m) : p._geometry_type;
      const item = el(
        "li",
        { title: p._issues.join(", ") },
        el("span", { class: "name" },
          el("span", { class: "dot", style: `background:${COLORS[severity(p)]}` }),
          featureName(p)),
        el("span", { class: "value" }, value),
      );
      item.addEventListener("click", () => {
        $("features").querySelector(".active")?.classList.remove("active");
        item.classList.add("active");
        focusFeature(f.id);
      });
      return item;
    }),
  );
}

function focusFeature(index) {
  const layer = layersByIndex.get(index);
  if (!layer) return;
  if (layer.getBounds) map.fitBounds(layer.getBounds(), { maxZoom: 18, padding: [40, 40] });
  else map.setView(layer.getLatLng(), Math.max(map.getZoom(), 16));
  layer.openPopup();
}

function popup(p) {
  const rows = [
    ["Layer", p._layer],
    ["Type", p._geometry_type],
    ["Status", p._status],
    ["Area", area(p._area_m2)],
    ["Perimeter", length(p._perimeter_m)],
    ["Length", length(p._length_m)],
    ["Geodesic area", area(p._geodesic_area_m2)],
    ["Geodesic length", length(p._geodesic_length_m)],
    ["Deviation", pct(p._deviation_pct)],
    ["Method", p._method || "–"],
  ].filter(([, v]) => v !== "–");
  const attrs = Object.entries(p).filter(([k]) => !k.startsWith("_"));

  return el(
    "div",
    { class: "popup" },
    el("h3", {}, featureName(p)),
    el("table", {}, ...[...rows, ...attrs].map(([k, v]) =>
      el("tr", {}, el("td", {}, k), el("td", {}, v == null ? "–" : String(v))))),
    p._issues.length ? el("ul", { class: "issues" }, ...p._issues.map((i) => el("li", {}, i))) : null,
  );
}

async function refreshRecent() {
  try {
    const { body } = await api("/api/files?limit=10");
    $("recent").replaceChildren(
      ...body.items.map((f) => {
        const item = el(
          "li",
          {},
          el("span", { class: "name" }, f.filename),
          el("span", { class: "value" }, f.status === "COMPLETED" ? `${f.feature_count} features` : f.status.toLowerCase()),
        );
        item.addEventListener("click", async () => showFile(await waitUntilDone(f)));
        return item;
      }),
    );
    $("recent-section").hidden = body.items.length === 0;
  } catch {
    $("recent-section").hidden = true;
  }
}

// --- wiring -------------------------------------------------------------------------------

const drop = $("drop");
$("file").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));
["dragenter", "dragover"].forEach((t) =>
  drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) =>
  drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));

refreshRecent();
