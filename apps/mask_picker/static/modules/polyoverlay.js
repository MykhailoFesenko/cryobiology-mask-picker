// Polygon overlays on the catalog grid + zoom modal. Caches polygon envelopes
// per stem to avoid refetching. editor.close() must call _clearPolygonsCache(stem)
// to invalidate after edits.

import { $$, _hexToRgba } from "./util.js";
import { api } from "./api.js";
import { currentItem, getLabelColor, DEFAULT_LABEL } from "./state.js";

const _polyCache = new Map();

export function _clearPolygonsCache(stem = null) {
  if (stem === null) _polyCache.clear();
  else _polyCache.delete(stem);
}

async function _fetchPolygonsEnvelope(stem) {
  if (_polyCache.has(stem)) return _polyCache.get(stem);
  try {
    const data = await api(`/api/polygons/${encodeURIComponent(stem)}`);
    const shapes = (data.shapes || [])
      .filter((sh) => Array.isArray(sh.points) && sh.points.length >= 3)
      .map((sh) => ({ points: sh.points.map((p) => [+p[0], +p[1]]), label: sh.label || DEFAULT_LABEL }));
    const imgW = data.imageWidth | 0;
    const imgH = data.imageHeight | 0;
    const entry = (imgW > 0 && imgH > 0 && shapes.length) ? { imgW, imgH, shapes } : null;
    _polyCache.set(stem, entry);
    return entry;
  } catch (e) {
    _polyCache.set(stem, null);
    return null;
  }
}

function _renderPolyOverlayInto(wrapperEl, entry, overlayClass) {
  if (!wrapperEl || !entry) return;
  wrapperEl.querySelectorAll(`.${overlayClass}`).forEach((n) => n.remove());
  const { imgW, imgH, shapes } = entry;
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", overlayClass);
  svg.setAttribute("viewBox", `0 0 ${imgW} ${imgH}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  shapes.forEach((sh) => {
    const pts = sh.points || sh;
    const color = getLabelColor(sh.label || DEFAULT_LABEL);
    const poly = document.createElementNS(NS, "polygon");
    poly.setAttribute("points", pts.map((p) => `${p[0]},${p[1]}`).join(" "));
    poly.style.stroke = _hexToRgba(color, 0.65);
    svg.appendChild(poly);
  });
  wrapperEl.appendChild(svg);
}

export async function refreshPolygonOverlaysForCurrent() {
  const it = currentItem();
  if (!it) return;
  const entry = await _fetchPolygonsEnvelope(it.stem);
  if (!entry) return;
  $$("#variants .variant").forEach((v) => {
    if (v.classList.contains("variant--original")) return;
    const wrap = v.querySelector(".variant__image-wrap");
    if (wrap) _renderPolyOverlayInto(wrap, entry, "variant__polyoverlay");
  });
}

export async function refreshPolygonOverlaysForZoom() {
  const it = currentItem();
  if (!it) return;
  const entry = await _fetchPolygonsEnvelope(it.stem);
  if (!entry) return;
  $$("#zoomImages figure").forEach((fig) => {
    const cap = fig.querySelector("figcaption");
    if (cap && cap.textContent.trim() === "Оригінал") return;
    const wrap = fig.querySelector(".zoom-figure__img-wrap");
    if (wrap) _renderPolyOverlayInto(wrap, entry, "zoom-figure__polyoverlay");
  });
}
