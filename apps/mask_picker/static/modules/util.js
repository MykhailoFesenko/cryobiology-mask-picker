// Shared pure helpers — DOM selectors, geometry, colour, time, toast.
// No app state, no imports. Safe to use from any module.

export const $ = (sel) => document.querySelector(sel);
export const $$ = (sel) => [...document.querySelectorAll(sel)];

export function _hexToRgba(hex, alpha) {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

export function fmtTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch { return iso; }
}

export function closeModal(m) { m.classList.remove("open"); }

export function _pointInPoly(x, y, points) {
  let inside = false;
  const n = points.length;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const xi = points[i][0], yi = points[i][1];
    const xj = points[j][0], yj = points[j][1];
    const intersect = ((yi > y) !== (yj > y))
      && (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-9) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

export function _projectPointOnSegment(px, py, ax, ay, bx, by, clamp = true) {
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy || 1e-9;
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  if (clamp) t = Math.max(0, Math.min(1, t));
  return { x: ax + t * dx, y: ay + t * dy, t };
}

export function showToast(msg, kind = "ok", ms = 2500) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = `toast toast--${kind} toast--visible`;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = "toast"; }, ms);
}
