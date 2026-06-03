/**
 * Groups module (Day 4-5 v2 redesign) — top-level backend bridge + color helpers.
 *
 * v2 redesign: color приходить з user-defined class (group_classes.json),
 * per-group jitter ±15° hue, ±20% sat, ±15% light для відтінків внутрі одного
 * класу (cell-1 темно-зелена, cell-2 салатова, cell-3 блакитно-зелена).
 *
 * Колір налаштовується через Group Classes Manager — muted/deep palette
 * (sat ~40-55%, light ~42-50%, не неонові).
 */
import { api } from "./api.js";

// ---------------------------------------------------------------------------
// Backend bridge
// ---------------------------------------------------------------------------

/** GET /api/groups/<stem>?model=<m> → envelope + classifications + classes */
export async function loadGroups(stem, model) {
  const qs = model ? `?model=${encodeURIComponent(model)}` : "";
  return api(`/api/groups/${encodeURIComponent(stem)}${qs}`);
}

/** POST /api/groups/<stem> з повним списком груп. */
export async function saveGroups(stem, model, groups) {
  return api(`/api/groups/${encodeURIComponent(stem)}`, {
    method: "POST",
    body: JSON.stringify({ model: model || null, groups }),
  });
}

// ---------------------------------------------------------------------------
// Color helpers — index-based per-class spread (Bug 2 fix, 2026-05-26)
// ---------------------------------------------------------------------------
//
// Hue spread ±50° через golden angle (137.5°) — кожна група всередині класу
// має помітно різний відтінок (зелений 130° → 80° жовто-салатовий /
// 180° бірюзовий), але не їде у жовтий чи синій. Sat ±40% / Light ±35%
// — основна "різнокольоровість" робиться через насиченість і яскравість.

// v1.16.2: per-group colours generated in OKLCH (perceptual Oklab space), not raw
// HSL. Each class keeps its HUE FAMILY (tight arc), groups differ by perceptually
// uniform LIGHTNESS, all at high chroma (vivid). Returned as HSL so consumers
// (_hslToRgb / hslCss) stay unchanged. Fixes HSL muddy/pink drift + uneven steps.
const GROUP_CHROMA_MIN = 0.095;  // chroma ALSO varies per group -> more variety
const GROUP_CHROMA_SPAN = 0.105; // 0.095..0.20 (muted -> vivid)
const GROUP_L_MIN = 0.40;     // OKLCH lightness band — WIDE for strong light->dark spread
const GROUP_L_SPAN = 0.50;    // range 0.40..0.90 perceptual lightness
const GROUP_HUE_ARC = 0.70;   // radians (~40deg total) hue spread, still within family

function _hslToRgb01(h, s, l) {
  h = ((h % 360) + 360) % 360; s = Math.max(0, Math.min(100, s)) / 100; l = Math.max(0, Math.min(100, l)) / 100;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = c; g = x; } else if (h < 120) { r = x; g = c; }
  else if (h < 180) { g = c; b = x; } else if (h < 240) { g = x; b = c; }
  else if (h < 300) { r = x; b = c; } else { r = c; b = x; }
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}
function _srgbToLinear(c) { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
function _linearToSrgb(c) { c = c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055; return Math.round(Math.max(0, Math.min(1, c)) * 255); }
function _rgbToOklab(r, g, b) {
  const lr = _srgbToLinear(r), lg = _srgbToLinear(g), lb = _srgbToLinear(b);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return {
    L: 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
    a: 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    b: 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
  };
}
function _oklchToRgb(L, C, H) {
  const a = C * Math.cos(H), b = C * Math.sin(H);
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.2914855480 * b;
  const l = l_ * l_ * l_, m = m_ * m_ * m_, s = s_ * s_ * s_;
  return [
    _linearToSrgb(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    _linearToSrgb(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    _linearToSrgb(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s),
  ];
}
function _rgbToHslObj(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  const l = (mx + mn) / 2;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  let h = 0;
  if (d !== 0) {
    if (mx === r) h = (((g - b) / d) % 6 + 6) % 6;
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
  }
  return { h: Math.round(h), s: Math.round(s * 100), l: Math.round(l * 100) };
}

/** Index-aware HSL. `index` — позиція групи у межах класу (0-based). */
export function effectiveHSL(group, classes, index) {
  let baseH = 200, baseS = 45, baseL = 45;
  const cid = group?.class_id;
  let cls = null;
  if (cid && Array.isArray(classes)) {
    cls = classes.find((c) => c.id === cid) || null;
  }
  if (cls) {
    if (Number.isFinite(cls.color_hue))   baseH = cls.color_hue;
    if (Number.isFinite(cls.color_sat))   baseS = cls.color_sat;
    if (Number.isFinite(cls.color_light)) baseL = cls.color_light;
  } else if (Number.isFinite(group?.color_hue)) {
    baseH = group.color_hue;
  }

  // class base hue -> OKLCH hue (HSL hue != OKLCH hue numerically; convert via a
  // vivid sample of the base colour, then take its Oklab angle).
  const bRgb = _hslToRgb01(baseH, Math.max(60, baseS), 50);
  const ob = _rgbToOklab(bRgb[0], bRgb[1], bRgb[2]);
  const baseHueRad = Math.atan2(ob.b, ob.a);
  const i = Number.isFinite(index) ? index : 0;
  // golden-ratio fractional spread — even, deterministic per group index.
  const fL = (i * 0.6180339887) % 1;
  const fH = ((i * 0.7548776662) % 1) - 0.5;
  const fC = (i * 0.5436890126) % 1;
  const L = GROUP_L_MIN + fL * GROUP_L_SPAN;                 // perceptual lightness band
  const H = baseHueRad + fH * GROUP_HUE_ARC;                 // family hue arc
  const C = GROUP_CHROMA_MIN + fC * GROUP_CHROMA_SPAN;       // chroma varies too
  const rgb = _oklchToRgb(L, C, H);
  return _rgbToHslObj(rgb[0], rgb[1], rgb[2]);
}

/** {group_id → index_within_class}. Будується у тому ж порядку що list. */
export function classIndexMap(groups) {
  const out = new Map();
  const counters = new Map();
  if (!Array.isArray(groups)) return out;
  for (const g of groups) {
    if (!g || g.id == null) continue;
    const cid = g.class_id || "_none";
    const i = counters.get(cid) || 0;
    out.set(g.id, i);
    counters.set(cid, i + 1);
  }
  return out;
}

export function hslCss(hsl, alpha) {
  if (typeof alpha === "number") {
    return `hsla(${hsl.h}, ${hsl.s}%, ${hsl.l}%, ${alpha})`;
  }
  return `hsl(${hsl.h}, ${hsl.s}%, ${hsl.l}%)`;
}

// ---------------------------------------------------------------------------
// ID allocator
// ---------------------------------------------------------------------------

export function nextGroupId(existing) {
  const used = new Set();
  for (const g of existing) {
    const m = /^g_(\d+)$/.exec(g?.id || "");
    if (m) used.add(parseInt(m[1], 10));
  }
  let n = 1;
  while (used.has(n)) n++;
  return `g_${String(n).padStart(3, "0")}`;
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

// Канонічно baking.POLYGON_ID_BASE — полігон shape #k бейкається у iid BASE+k.
const POLYGON_ID_BASE = 50000;

export function groupMemberCount(g) {
  // Полігони рахуються ЛИШЕ через polygon_indices (джерело правди). Reserved-range
  // iid (>=BASE) — bake-артефакт того ж полігона; ЗАВЖДИ пропускаємо, інакше:
  // (а) дубль коли полігон у групі; (б) «привид» коли полігон прибрано з групи,
  // а baked iid лишився stale (db_img_0171 g_008). Model-instance завжди <BASE.
  const iids = (g?.instance_ids || []).filter((iid) => iid < POLYGON_ID_BASE);
  return iids.length + ((g?.polygon_indices || []).length);
}

export function firstClassId(classes) {
  return Array.isArray(classes) && classes.length > 0 ? classes[0].id : null;
}
