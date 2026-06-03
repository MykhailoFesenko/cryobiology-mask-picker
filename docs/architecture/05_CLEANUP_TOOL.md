# 05. Cleanup tool

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Джерело: `editor/cleanup.js` (повністю), `editor/zoompan.js`, `cleanup.py`,
> `routes/api_cleanup.py`, `catalog.py`. Найбагатший на баги концепт — covered/rejected.

## Призначення
Cleanup — перший таб: анотатор відмічає **погані маски** (reject) і ставить **markers**
(«тут пропущена клітина»). Reject згодом застосовується при bake (`cleaned[isin(rejected)]=0`).
Це найтонша підсистема: «rejected» має ДВА представлення (stored + derived covered), і
консюмери розкидані по cleanup/zoompan/groups/catalog — історичне джерело Bug A/B/C/M.

---

## 1. Інструменти
- **reject** (`tool="reject"`): клік по інстансу → `_cleanupToggleInstance(id)`. Hit-test:
  `cu.labelsInt32[floor(y)*W+floor(x)]` (raw_iid). Hover → жовтий bbox.
- **marker** (`tool="marker"`): клік → додати/прибрати точку «пропущена клітина»
  (`_cleanupAddMarker`/`_cleanupRemoveMarker`; поріг `MARKER_CLICK_PX=12` CSS-px).

---

## 2. Дані: SoT / derived / кеші

| Концепт | Форма | Де |
|---|---|---|
| **rejected** (explicit) | `cu.rejectedSet` (Set raw_iid) | браузер; persist → `selections.json` (live SoT, `01` §2) |
| **covered** (derived) | обчислюється з геометрії полігонів (>50%) | `_polyCoveredInstances()` → кеш `cu.coveredCache` |
| **markers** | `cu.markers` ([{x,y}] image-coords) | persist → `selections.json[stem].cleanup.markers`; **advisory** (НЕ в bake) |
| pixelCounts | `cu.pixelCounts` (Map id→px) | lazy (cleanup.js:202); raw стабільний → без інвалідації |

### `_polyCoveredInstances()` — SoT для «covered» (cleanup.js:195)
Сканує bbox КОЖНОГО polygon-shape, рахує скільки пікселів кожного raw-instance потрапляє
в полігон (`_pointInPoly`); `covered = cov/pixelCounts[id] > 0.5`. **НЕ мутує `rejectedSet`** —
це derived rejection (з v1.16.1): instance під полігоном «заміщений», тому завжди «rejected»
для рендеру/блокувань, але НЕ записується (видалив полігон → covered зник сам). Кеш
`coveredCache` інвалідується на `_polyMarkDirty` (зміна полігонів) → null.

---

## 3. Рендер — ДВА canvas (ключ до covered/rejected плутанини)

| Canvas | Малює | Метод |
|---|---|---|
| `#cleanupCanvasBase` | фон (overlay/original) + **ховання** прибраних інстансів | `zoompan._drawBase` (→ `02`§3) |
| `#cleanupCanvasMarks` | напівпрозорі марки інстансів поверх бази | `cleanup._cleanupRedraw` |

**`_drawBase` (база):** малює overlay або original; тоді `_overlayHideSet()` визначає, які
instance «стерти» (перемалювати чистим оригіналом через `_ensureRejectedPatch`, shape-accurate):
- **на cleanup**: ховає **лише covered** (явні rejected лишаються видимі — керовані червоними марками);
- **на polygons/groups**: ховає `rejected ∪ covered`.

**`_cleanupRedraw` (марки):** для кожного пікселя:
- `covered` → **skip** (база вже показує чистий оригінал — instance візуально зник, без червоного контуру; це Bug B/«covered зникають» UX);
- `rejectedSet.has(id)` → **червоний** `rgba(255,60,60,115)`;
- інакше (kept) → faint hash-колір (alpha 77).
- hover → жовтий bbox.

> **Чому 2 канали:** base = «що видно як фон» (з hide); marks = «семантична підсвітка»
> (червоне/faint). covered трактується ОДНАКОВО в обох (base ховає, marks пропускає) — саме
> розсинхрон цих консюмерів давав Bug B/M (covered лишався червоним або блимав).

---

## 4. Front↔back контракт
| Дія | Ендпоінт | Ефект |
|---|---|---|
| open: raw labels | GET `/api/labels-rgb/<model>/<stem>.png` | → `cu.labelsInt32` (декод RGB) |
| open: saved | GET `/api/cleanup/<stem>` | ← `selections.json` (rejected_instances+markers) |
| autosave | POST `/api/cleanup/<stem>` (5с debounce) | `state.set_cleanup`→selections.json + `mark_dirty`; **НЕ пече** |
| 🔥 full rebake | POST `/api/cleanup-export/<stem>` | `bake_with_resync` + пише `cleanup.json` SoT-snapshot (рідко) |

⚠ **Naming-грабля:** фронтовий `_cleanupExportSave()` попри назву **лише зберігає JSON**
(викликає `_cleanupAutosave(true)`) — з Day-7 lazy-bake перепікання тут НЕ відбувається.
Реальний bake-export — окремий бекенд-ендпоінт `/api/cleanup-export` (клік 🔥, рідко).
Не плутати. (Поточний UI 🔥 прибрано з топ-бару — лишився Hard Reset; → handoff/`12`.)

---

## 5. 🔑 Консюмери концепту «rejected» (covered ∪ rejectedSet) — аудит-таблиця

| Консюмер | Що робить | covered? | rejectedSet? |
|---|---|---|---|
| `_cleanupRedraw` (marks) | covered→skip, rejected→red, kept→faint | ✅ | ✅ |
| `zoompan._overlayHideSet`/`_drawBase` | hide з бази (per-tab) | ✅ (завжди) | ✅ (working tabs) |
| `_ensureRejectedPatch` | shape-accurate patch (4px дилатація) | через hideIds | через hideIds |
| `_cleanupToggleInstance` | **block** toggle covered (обидва боки) | ✅ block | toggle |
| `groups._groupsHitTest` | covered→fallback на polygon | ✅ | ✅ |
| `groups` lasso filter | exclude rejected+covered | ✅ | ✅ |
| `groups._groupsRedrawMaskCanvas` peek | exclude covered+rejected | ✅ | ✅ |
| **catalog `_render_clean_overlay_bytes`** (back) | hide rejected у тайлі — **bbox+pad, лише explicit з selections.json** | ❌ **НЕ ховає covered** | ✅ (з cleanup.json/selections) |
| bake `cleaned[isin(rejected)]=0` | rejected→0 у npy | covered→0 опосередковано (полігон перезаписує) | ✅ |

> **Аудит-висновок:** усі UI-консюмери covered узгоджені (v1.16.2). Єдиний, що НЕ враховує
> covered — серверний **catalog-тайл** (`_render_clean_overlay_bytes`, bbox-based) — але це
> thumbnail низької точності, не редактор; covered там і не критичний (полігон ще не
> матеріалізований у тайлі). Окремий консюмер «rejected» (інша точність) → реєстр `14`.

---

## 6. Lifecycle (що при зміні SoT)
- **reject toggle** → history push → rejectedSet ± → markDirty → redraw + base hide (working-tab) → autosave selections.json.
- **covered зʼявився/зник** (намалював/прибрав полігон у Polygons) → `_polyMarkDirty` null-ить coveredCache → `_polyRedraw`→`_drawBaseIfCoveredChanged` перемальовує базу → instance зникає/повертається ОДРАЗУ.
- **bake** застосує rejected (raw→0); covered-instance перезаписується пікселями полігона.
- **marker add/remove** → history push → markers ± → autosave; advisory, у bake не йде.

---

## 7. Known limitations + посилання
- **rejected live-SoT = selections.json** (не cleanup.json). **F-004 ✅ fixed:** cleanup.json тепер синхронізується на КОЖЕН bake (`data_sync._sync_cleanup_json_after_bake`) → provenance у finalize-ZIP завжди свіже. Деталі → `01` §2.
- `_polyHasShapeOnInstance` (pixel-accurate hit-test, Bug 6) — допоміжний guard «reject над полігоном»; covered-блок (`_polyCoveredInstances`) — основний механізм (трасувати реальні виклики у Phase-2 `06`/`14`).
- Перцептивна точність catalog-тайла нижча за редактор (bbox vs shape) — навмисно.
- Деталі рендеру бази/кешів → [`02`](02_FRONTEND_STATE.md) §3, [`09`](09_BAKING_AND_RESERVED_IDS.md) (як rejected→0 при bake).
