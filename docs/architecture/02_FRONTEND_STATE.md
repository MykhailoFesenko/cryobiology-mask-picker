# 02. Frontend state + межа «різної памʼяті»

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Пара до [`01`](01_DATA_MODEL_AND_ID_SPACES.md): 01 = дані на диску, 02 = стан у
> браузері + хто кого синхронізує. Джерело: `static/modules/editor/index.js` + grep.

## Призначення
`editor` — єдиний composite-обʼєкт, що тримає **весь** стан 3 табів у браузерній
памʼяті, поки модалка відкрита. Він **ефемерний**: при `close()` майже все обнуляється;
персистентний стан — лише на диску (`01`). Цей документ — повна форма стану + точна
**межа «різної памʼяті»** (де браузер ↔ де диск ↔ чим синхронізується). Розуміння цієї
межі критичне: помилки тут = «зберіг не те» / «undo не повертає» / stale-кеш.

---

## 1. Композиція (mixin-spread)
`editor` (`index.js:58`) збирається spread-ом 8 mixin-ів у один обʼєкт; усі поділяють
`this` і `this.state`:
```
editor = { state:{…}, …DOM-refs, …_enqueueSave,
  ...tabsMixin, ...zoompanMixin, ...eventsMixin, ...cleanupMixin,
  ...polygonsMixin, ...groupsMixin, ...keysMixin, ...historyMixin }
```
Тобто `_cleanupRedraw` (cleanupMixin), `_drawBase` (zoompanMixin), `_historyPush`
(historyMixin) — усі методи **одного** обʼєкта, викликають одне одного через `this`.
Немає інкапсуляції між табами — спільний `this.state` є і силою (легкий крос-таб
read, напр. groups читає `cu.labelsInt32`), і ризиком (консюмер легко проґавити).

Глобальний (поза editor) стан — `state.js`: `state.{catalog,models,filter,idx,user}`,
`appLabels` (live-binding), `currentItem()`. Editor читає `currentItem()` на `open()`.

---

## 2. Повна форма `editor.state` (декларована, `index.js:59`)

| Поле | Тип | Призначення | Скидається |
|---|---|---|---|
| `open` | bool | чи модалка відкрита | open=true / close=false |
| `stem`, `model` | str/null | поточне фото + обрана модель (null = polygons-only) | open/close |
| `activeTab` | "cleanup"\|"polygons"\|"groups" | активний таб | open |
| `baseLabel` | str | дефолт-клас для bake уцілілих model-instance | open (з `it.state.base_label`) |
| `W`, `H` | int | розмір зображення (px) | open / `_reloadCleanupData` |
| `originalImage`, `overlayImage` | Image/null | завантажені фон-зображення | open / close=null |
| `bgSource` | "original"\|"overlay" | (глобальний дефолт; реально per-tab — див. sub-state) | — |
| `scale,panX,panY,isPanning,…` | num/bool | zoom/pan transform | open reset |
| **`history`** | `{undo:[],redo:[]}` | ГЛОБАЛЬНИЙ undo-стек (3 домени) — `historyMixin` (→ `10`) | **open + close** (`_historyClear`) |
| `cleanup` | obj | sub-state Cleanup (нижче) | open / close |
| `polygons` | obj | sub-state Polygons (нижче) | open / close |
| `groups` | obj | sub-state Groups (нижче) | open / close |

### Sub-state `cleanup` (cu)
`tool` ("reject"\|"marker"), `labelsInt32` (Int32Array raw_iid/піксель), `allIds` (Set),
`bboxes` (Map id→{x0,y0,x1,y1}), `rejectedSet` (Set raw_iid), `markers` ([{x,y}]),
`hoverId`, `hoverMarkerIdx`, `dirty`, `dirtyExport`, `autosaveTimer`, `bgSource`, `available`.

### Sub-state `polygons` (pg)
`tool` (**null** = navigate+edit за замовч.; \|"draw"\|"pick"), `activeLabel`, `shapes`
([{label,points,shape_type,group_id,flags}]), `draft`, `cursor`, `hoverShapeIdx`,
`hoverVertex` ({si,vi}), `selectedShape`, `selectedVertices` (Set "si:vi"),
`draggingVertex`, `draggingGroup`, `lasso`, `freehand`, `dirty`, `autosaveTimer`,
`bgSource` ("original" дефолт у літералі, але open() ставить "overlay"), `envelope`
(LabelMe обгортка крім shapes).

### Sub-state `groups` (gr)
`tool` ("edit"\|"picker"), `list` ([{id,class_id/type,instance_ids,polygon_indices,color_hue,label}]),
`classifications` (вирівняний масив, з backend GET), `activeId`, `dirty`, `autosaveTimer`,
`bgSource`, `lasso` ({active,path}), `editPress` (drag-detection), `peekUngrouped` (hold-стан).

---

## 3. ⚠ Lazy-кеші (НЕ в state-літералі — створюються на льоту, нуляться на close)

Це часте джерело stale-багів — кеш без коректної сигнатури/інвалідації. Усі в `cu`:

| Кеш | Тип | Build site | Інвалідація | Clear |
|---|---|---|---|---|
| `cu.labelsInt32` | Int32Array | `_reloadCleanupData` (index.js:348) | raw стабільний → не треба в сесії | close |
| `cu.allIds`, `cu.bboxes` | Set / Map | `_reloadCleanupData` | — | close |
| `cu.pixelCounts` | Map id→к-сть px | lazy: cleanup.js:202 / groups.js:437 | raw стабільний → не треба | close (index.js:455) |
| `cu.coveredCache` | Set covered iid | `_polyCoveredInstances` (cleanup.js:195) | **`_polyMarkDirty`/polygon change → null** (polygons.js:806) | close |
| `cu._coveredBaseSig` | str `"size\|sum"` | `_drawBase` (zoompan.js:95) | порівнюється у `_drawBaseIfCoveredChanged` | (перезапис) |
| `cu._rejectedPatch` (+`Sig`) | offscreen canvas | `_ensureRejectedPatch` (zoompan.js:162) | sig `stem\|WxH\|count\|sum` змінився | (rebuild) |

**Підводний камінь:** кеш-сигнатура мусить включати ВСЕ, від чого залежить. `_rejectedPatchSig`
= `stem|WxH|hideIds.size|sum(hideIds)` — size+sum ловить зміну набору (теоретично можлива
колізія size+sum, але на практиці безпечно для blit). `_coveredBaseSig` так само size+sum
covered. (`CODE_AUDIT_PRINCIPLES` §2.4.)

---

## 4. 🔑 Межа «РІЗНОЇ ПАМʼЯТІ» — браузер ↔ диск

| Браузерний стан (ефемерний) | Disk-SoT (персистентний) | Синхронізація (хто/коли) |
|---|---|---|
| `cu.rejectedSet`, `cu.markers` | `selections.json[stem].cleanup` (live SoT, `01` §2) | autosave 5с → POST `/api/cleanup` (`state.set_cleanup`); GET на open |
| `pg.shapes` (+`envelope`) | `polygons/<stem>.json` | autosave → POST `/api/polygons`; GET `_loadPolygons` на open |
| `gr.list` | `groups/<stem>.json` | autosave → POST `/api/groups` (echo+gen-guard); GET `_groupsLoad` на open |
| `gr.classifications` | — (обчислюється backend) | приходить у GET `/api/groups` resp |
| `cu.labelsInt32` (raw_iid) | `output/<model>/npy` (RO) | one-way: GET `/api/labels-rgb` → декод на open |
| `state.history` | — (НЕ персиститься) | живе лише в памʼяті; clear на open/close |
| `originalImage`/`overlayImage` | `images/`, `selected|output overlay` | GET on open |
| zoom/pan, hover, draft, lasso, peek | — | суто ефемерні (UI-only) |

**Висновки:**
1. **Bake — НЕ тут.** Жоден autosave не пече. baked npy (`selected/npy`) оновлюється лише
   серверним bake (Save All / Finalize), при **закритому** редакторі (lazy-bake). Тому
   `gr.list.instance_ids` у браузері — у baked-просторі, але узгоджений завдяки reserved-ID
   (`01` §3.2) і серверному strip-orphan після bake (→ `11`).
2. **Один напрям для raw:** `labelsInt32` лише читається; UI ніколи не пише в `output/`.
3. **history не персиститься** — закрив редактор = історія втрачена (commit-межа = bake).

---

## 5. Lifecycle: open / close / flush

- **`open(model, {tab})`** (index.js:204): guard на той самий stem (лише reload cleanup при
  зміні моделі); інакше `close()` старого. Скидає всі 3 sub-state + `_historyClear`. Паралельно
  вантажить original+overlay; тоді `_reloadCleanupData` (labels-rgb→`labelsInt32`), `_loadPolygons`,
  `_groupsLoad`; `_drawBase` + `_activateTab`.
- **`close(skipFlush)`** (index.js:435): без skipFlush → делегує `flushIfDirty()` (яка в кінці
  кличе `close(true)`). Власне teardown: clear timers, unbind, `open=false`, `_historyClear`,
  **обнулення всіх кешів** (`labelsInt32/allIds/bboxes/pixelCounts/coveredCache/rejectedSet/…`),
  `_groupsClearVisualLayers`, `renderAll`.
- **`flushIfDirty()`** (index.js:485): re-entrancy guard (`_flushPromise`); по черзі flush poly /
  cleanup(export або autosave) / groups, тоді `close(true)`. Детальніше про чергу/race → `11`.

---

## Звʼязки / посилання
- Undo-модель та `state.history` → [`10`](10_UNDO_HISTORY.md).
- Autosave/dirty/flush/gen-guard → [`11`](11_AUTOSAVE_DIRTY_SYNC.md).
- Що означає кожен POST/GET (контракт) → [`04`](04_API_CONTRACT.md).
- Base-render + covered/rejected hide (`_drawBase`/`_overlayHideSet`/`_ensureRejectedPatch`) → [`05`](05_CLEANUP_TOOL.md).
- ID-простори (`labelsInt32`=raw, `instance_ids`=baked) → [`01`](01_DATA_MODEL_AND_ID_SPACES.md) §3.
