# 08. Колірна логіка груп (OKLCH)

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Винесено окремо (прохання юзера). Джерело: `static/modules/groups.js:40-154` + `style.css`.

## Призначення
Кожна група має отримати **виразний, перцептивно-різний** колір, але **в межах hue-родини
свого класу** (cell — зелені, vesicle_cluster — помаранчеві, nuclei — фіолетові). Раніше
сирий HSL-jitter «їхав» у рожевий/коричневий і давав нерівні кроки світлоти. Рішення —
генерувати в **OKLCH** (перцептивний Oklab) і повертати HSL (споживачі незмінні).

---

## 1. `effectiveHSL(group, classes, index)` (groups.js:102)
```
1. base H/S/L з класу (group_classes.json: color_hue/sat/light); fallback group.color_hue; default 200/45/45.
2. base HSL hue → OKLCH hue angle: _hslToRgb01(baseH, max(60,baseS), 50) → _rgbToOklab → atan2(b,a).
   (бо HSL-hue ≠ OKLCH-hue числово — конвертуємо через vivid-зразок базового кольору.)
3. golden-ratio fractional spread по index-within-class (детерміновано, рівномірно):
     fL = (i·0.6180339887) mod 1
     fH = ((i·0.7548776662) mod 1) − 0.5
     fC = (i·0.5436890126) mod 1
4. L = L_MIN + fL·L_SPAN          # перцептивна світлота
   H = baseHueRad + fH·HUE_ARC    # дуга відтінку в межах родини
   C = CHROMA_MIN + fC·CHROMA_SPAN # chroma теж варіюється
5. _oklchToRgb(L,C,H) → RGB → _rgbToHslObj → {h,s,l}   # повертаємо HSL
```
**index** — позиція групи всередині класу (`classIndexMap`, лічильник per class_id у порядку list).
Тобто cell-0 / cell-1 / cell-2 закономірно різняться світлотою+chroma+невеликим відтінком,
але всі лишаються «зеленими».

---

## 2. Константи — ФАКТИЧНІ значення (groups.js:44-48)
| Константа | Значення | Що тюнить |
|---|---|---|
| `GROUP_CHROMA_MIN` | **0.095** | мінімальна насиченість (muted) |
| `GROUP_CHROMA_SPAN` | **0.105** | діапазон → chroma 0.095..0.20 (muted→vivid) |
| `GROUP_L_MIN` | **0.40** | нижня межа світлоти |
| `GROUP_L_SPAN` | **0.50** | діапазон → L 0.40..0.90 (сильний light→dark розкид) |
| `GROUP_HUE_ARC` | **0.70 рад** (~40°) | ширина дуги відтінку в межах родини |

**Тюнінг:** більше vivid → ↑ CHROMA_MIN/SPAN; більший розкид світлоти → ↑ L_SPAN; ширший
відтінок (ризик виходу з родини) → ↑ HUE_ARC.

> ⚠ **F-006 (doc):** `NEXT_SESSION_HANDOFF_2026-06-01.md` §1 наводить ІНШІ значення
> (`GROUP_CHROMA=0.15, L_MIN=0.42, L_SPAN=0.44, HUE_ARC=0.50`) — застарілі/округлені з раннього
> раунду. **Код = таблиця вище** (CHANGELOG [1.16.2] збігається з кодом: «L 0.40-0.90, chroma
> 0.095-0.20, hue ~40°»). Не копіювати числа з handoff.

---

## 3. sRGB ↔ Oklab хелпери (groups.js:50-99)
`_hslToRgb01` (HSL→RGB) · `_srgbToLinear`/`_linearToSrgb` (gamma) · `_rgbToOklab` (RGB→Oklab L/a/b,
cbrt) · `_oklchToRgb` (LCH→RGB, out-of-gamut clamp у `_linearToSrgb` через `min(1,max(0,…))`) ·
`_rgbToHslObj` (RGB→{h,s,l}). Стандартні матриці Oklab (Björn Ottosson). Усі module-private, чисті.

## 4. Похідні: `classIndexMap`, `hslCss`
- `classIndexMap(groups)` → `{group_id → index_within_class}` (лічильник per class_id, у порядку list).
- `hslCss(hsl, alpha?)` → `"hsl(...)"` або `"hsla(...,a)"` — для CSS/canvas.

---

## 5. Консюмери кольору (рендер, editor/groups.js)
| Сайт | Шар | Як |
|---|---|---|
| `_groupsRender` overlay (groups.js:612) | `#groupsOverlayLayer` (SVG) | `effectiveHSL`→`hslCss` |
| boundary stroke (groups.js:745) | `#groupsBoundaryLayer` | контур групи |
| mask canvas (groups.js:885) | `#groupsMaskCanvas` (`.cleanup__canvas--groups`) | pixel-tint |
| chip (список груп) | DOM | колір індикатора |

Усі беруть `effectiveHSL(group, g.classes, idxMap.get(group.id))` — один обчислювач, узгоджені.

---

## 6. Blend + контури (style.css)
- **`#groupsOverlayLayer { mix-blend-mode: multiply }`** (css:842) + **`.cleanup__canvas--groups
  { mix-blend-mode: multiply }`** (css:848) — muted/deep кольори натуральніше лягають на фон;
  полігони у Groups теж multiply (однакова інтенсивність з інстансами).
- **`.svg--groups #polygonShapesLayer { opacity: 0.9 }`** (css:575) — read-only контур полігонів
  у Groups приглушений, щоб не змагався з group-fill.
- **White boundary stroke:** active-група має білий контур (`_groupsDrawBoundaryStroke`, path fill
  `#ffffff`) у `#groupsBoundaryLayer`. **Грабля D/J:** цей шар чиститься лише через
  `_groupsClearVisualLayers()` (index.js) — раніше його забували → біла обводка лишалась на
  Cleanup/Polygons (→ `02`, `07`, CHANGELOG [1.16.2]).
- **hold-O** ховає `#groupsMaskCanvas`+`#groupsBoundaryLayer`+`#groupsOverlayLayer` (css:568-570,
  Bug H) — щоб preview оригіналу був чистий.

## 7. Посилання
Рендер груп загалом → [`07`](07_GROUPS_TOOL.md); візуальні шари/очистка → [`02`](02_FRONTEND_STATE.md).
