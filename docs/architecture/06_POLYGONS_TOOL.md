# 06. Polygons tool

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Джерело: `editor/polygons.js` (повністю, 1088 р.), `polygons.py`, `baking._bake_polygons_into_labels`,
> `routes/api_polygons.py`, `multiseed.js`, `labels.js`.

## Призначення
Polygons — ручна доразмітка: домалювати ядра/везикули полігонами там, де модель помилилась.
SoT — `pg.shapes` (браузер) → `polygons/<stem>.json` (LabelMe envelope). Полігони — окремий шар
над raw-масками; при bake матеріалізуються у reserved-id (→ `09`). Найбільший фронт-модуль;
тісно звʼязаний з cleanup (covered) і groups (polygon_indices).

---

## 1. Інструменти (`pg.tool`)
- **`null`** = дефолт «навігація + редагування»: клік по полігону/вершині/ребру редагує, клік по
  пустоті — нічого (lasso-select вершин). Edit більше НЕ окрема кнопка (v1.16.2 UX).
- **`"draw"`**: малювання draft; клік по пустоті додає точку; **edit-in-draw** — клік по вершині
  (не mid-draft) захоплює її для drag.
- **`"pick"`**: клік по інстансу → seed-from-mask його контур у полігон (клік по існуючому shape →
  auto-switch у `null`+select).
- **Toggle:** повторний клік по активному tool / хоткей / Esc → `null`. open→null; `KeyE`→null.

## 2. Диспетч подій (важливо — спільний SVG для polygons І groups)
`_onSvgMouseDown/Move/Up` (events.js біндить на `#polygonSvg`) спершу **делегують** у
`_onGroupsSvgMouseDown/...` якщо `activeTab==="groups"` (polygons.js:75/211/303). Тобто SVG один,
а логіка розгалужується по активному табу.

## 3. Draw flow
- Draft: `pg.draft={points:[]}`; клік додає точку; **Shift+рух** = freehand (точка кожні `FREEHAND_MIN_DIST=15`px).
- **Закриття:** Space (keys.js, ≥3 точки) / Enter / dblclick mid-draft (`_polyCloseDraft` → push у `pg.shapes` з `activeLabel`).
- **Cancel** (Esc): `_polyCancelDraft` — draft окремий до замикання → **НЕ пушить undo** (інакше фантом, → `10`).
- Draft рендериться у **кольорі активного лейбла** (`_polyRedrawDraft`, було завжди синє).

## 4. Edit (tool=null) — взаємодія
| Жест | Дія | Метод |
|---|---|---|
| клік по вершині | select + почати drag | `_polyHitVertex` → `draggingVertex` |
| Shift+вершина | toggle у multi-select | `_polyToggleVertexSelected` |
| Alt+вершина | видалити вершину (<3→видалити shape) | `_polyDeleteVertex` |
| клік по тілу | select shape | `_polyHitShape` |
| клік по вершині у multi-select | group-drag усіх вибраних | `_polyStartGroupDrag` |
| клік по пустоті | lasso-select вершин (replace/add(Shift)/sub(Alt)) | `pg.lasso` → `_polyApplyLasso` |
| **dblclick по ребру** | вставити вершину | `_polyOnDblClick`→`_polyHitEdge` (адаптивний поріг) |
| Align (≥3 верш.) | підтягнути проміжні на пряму між 1-ю і останньою | `_polyAlignToLine` |

### dblclick — ручна детекція (Bug G, polygons.js:90-104)
Native `dblclick` НЕ долітає: кожен `_polyRedrawShapes` робить `innerHTML=""` → DOM-ціль 1-го кліку
зникає. Тому в `_onSvgMouseDown` — власна детекція (2 кліки <`DBL_CLICK_MS=350`ms + близькість
`VERTEX_CLICK_PX`). `_onSvgDblClick` лишений лише як запасний (порожній фон). Guard `_dblHandledT`
проти подвійного спрацювання manual+native.

### Drag undo — на ПОЧАТКУ жесту
`draggingVertex/Group.pushed` — push на ПЕРШОМУ mousemove (стан ДО зсуву), не mouseup (off-by-one
fix). Клік без руху → нема push (нема фантома). → `10` §3.

## 5. Pick → derived rejection (КЛЮЧ — звʼязок з cleanup)
`_polyPickSeed` (polygons.js:739): hit-test `cu.labelsInt32` (raw_iid) → `seed-from-mask` контур →
push у `pg.shapes`. **НЕ пише в `rejectedSet`!** Instance під полігоном стає **covered** (derived,
`_polyCoveredInstances` >50%) → автоматично червоний + невибірний. Видалив полігон → `_polyMarkDirty`
→ `coveredCache=null` → instance повертається сам. (Bug A/B fix — раніше explicit reject застрягав.)

## 6. Видалення shape → ремап груп (СКЛАДЕНА undo-дія)
`_polyDeleteSelectedShape`/`_polyDeleteSelectedVertices` (shape<3 точок) → `splice` →
`_polyRemapGroupsAfterShapeDelete(removedIdxs)` (polygons.js:584): для кожної групи фільтрує точне
співпадіння + зрушує `polygon_indices > removed` на −1 (Bug 5). Before-snap груп кладеться у ТОЙ
САМИЙ undo-запис через `_historyAttachSnap("groups")` → один Ctrl+Z відкочує shape+ремап (→ `10` §4).
Mark groups dirty + `_groupsScheduleAutosave`.

## 7. covered-кеш інвалідація
`_polyMarkDirty` (polygons.js:802) ЗАВЖДИ `cu.coveredCache=null` (полігони змінились). `_polyRedraw`
кінчається `_drawBaseIfCoveredChanged()` → база перемальовується, якщо covered-набір змінився
(covered instance зникає/повертається ОДРАЗУ). → `05` §3, `02` §3.

## 8. Front↔back контракт
| Дія | Ендпоінт | Ефект |
|---|---|---|
| open | GET `/api/polygons/<stem>` | ← shapes + envelope (`_loadPolygons`) |
| autosave / Save | POST `/api/polygons/<stem>` (5с debounce) | пише `polygons.json`; **JSON only, НЕ пече** (Day 7) |
| Save + bake | POST `/api/polygons-export/<stem>` | `bake_with_resync` (server) |
| Pick / Seed | POST `/api/polygons/<stem>/seed-from-mask` | контур instance(s) → shapes (без write) |
| Multi-seed | POST `.../multi-seed` | bulk seed cross-model (`multiseed.js`) |

> ⚠ `_polySave`/`_polyAutosave` — JSON-only (lazy-bake). Bake — лише Save all/Finalize. (як cleanup, → `05`§4.)

## 9. Рендер (SVG-шари)
`_polyRedrawShapes` (#polygonShapesLayer, fill+stroke per label color, select/hover alpha) ·
`_polyRedrawVertices` (#polygonVerticesLayer, кружки, selected=білий/hover=блакитний) ·
`_polyRedrawDraft` (#polygonDraftLayer, label-колір) · `_polyDrawLasso` (#polygonLasso) ·
`_polyRedrawMarkersOnly` (#polygonMarkersLayer — cleanup-маркери, видимі з обох табів, ghost поза cleanup).
`_polyShapeIsRejected` завжди `false` (manual polygons зберігаються навіть над rejected-зонами).

## 10. Lifecycle + посилання
- shape add/close → push pg.shapes; delete → remap groups (compound); edit-drag → push on first move.
- Reserved-id при bake + Layer 2 → [`09`](09_BAKING_AND_RESERVED_IDS.md). polygon_index нестабільність → [`01`](01_DATA_MODEL_AND_ID_SPACES.md)§3.3.
- Pick→covered споживачі → [`05`](05_CLEANUP_TOOL.md). polygon_indices у групах → [`07`](07_GROUPS_TOOL.md). Undo → [`10`](10_UNDO_HISTORY.md).
