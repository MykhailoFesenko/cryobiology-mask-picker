# Roadmap — Cryobiology V

Беклог Mask Picker'а. Поточна версія — **v1.12.0** (Day 9 bug audit,
2026-05-20, див. `CHANGELOG.md`). Закрито Days 1-9 + 5 post-Day-6 hotfixes.
Залишилось Day 10-12 (план нижче).

CVAT-flow **відмінено** 2026-05-07 — Mask Picker = основний інструмент анотації.

---

## v2.0.0 прогрес — короткий статус

| Day | Тема | Статус | Handoff |
|---|---|---|---|
| 1 | Backend split (app.py 2529→223) | ✅ 2026-05-11 | `V2_CODE_AUDIT_2026-05-11.md` |
| 2 | Frontend ES modules (app.js 3395→151) | ✅ 2026-05-11 | `V2_FRONTEND_PLAN_2026-05-11.md`, `V2_CP5*_HANDOFF_2026-05-11.md` |
| 3a | Auto-bake on Pick | ✅ 2026-05-12 _(буде знято у Day 7)_ | `V2_DAY3A_PLAN_2026-05-12.md` |
| 3b | Chip popover refactor | ✅ 2026-05-12 | у `V2_DAY3C_HANDOFF_2026-05-12.md` |
| 3c′ | Multi-seed rescue + Hard Reset | ✅ 2026-05-12 | `V2_DAY3C_HANDOFF_2026-05-12.md` |
| 4-5 | Groups tab (driver feature) | ✅ 2026-05-13 | `V2_DAY4_5_REDESIGN_HANDOFF_2026-05-13.md` |
| 6 | Tab height + Lasso(Edit) + Groups rejected | ✅ 2026-05-14 | `V2_DAY6_HANDOFF_2026-05-14.md` |
| post-6 | Split / state.reload / groups hit-test / zoompan groups | ✅ 2026-05-13 _(без окремого handoff)_ | `CHANGELOG.md [1.8.0 post-Day-6 hotfixes]` |
| **7** | **Lazy-bake + dirty markers + Save All + progress** | ✅ 2026-05-19 (v1.9.0) | `V2_DAY7_HANDOFF_2026-05-19.md` |
| 8 | Workspace flow доробка (stats / merge / per-label / subset) | ✅ 2026-05-19 (v1.10.0) | `V2_DAY8_HANDOFF_2026-05-19.md` |
| 9 | Bug audit + UX fixes | ✅ 2026-05-20 (v1.12.0) | `V2_DAY9_HANDOFF_2026-05-20.md` |
| 10 | Documentation rewrite | ⏳ | — |
| 11 | GitHub упаковка | ⏳ | — |
| 12 | APP_VERSION → 2.0.0 | ⏳ | — |

---

## v1.5.x → v1.6.x — Багфікси (DONE 2026-05-08)

### Зібрано від команди в чаті 03–05.05.2026

- ✅ **v1.6.1** — Імпорт workspace: ZIP з префіксом `_workspace/`
  + auto-rediscover моделей.
- ✅ **v1.6.2** — Insert vertex: dblclick на ребрі (8 → 14 CSS-px,
  EPS=0.02, preventDefault).
- ✅ **v1.6.4** — Колір cleanup: catalog-тайли ховають rejected
  через PIL+кеш (clean overlay).

---

## v1.6.x — UX-фічі від команди

### Зібрано від команди в чаті 03–05.05.2026

- ✅ **v1.6.3** — Пробіл = підтвердити полігон. Enter залишається.
- ✅ **v1.6.5** — Кнопка "Закрити додаток": graceful shutdown.
- 🔥 **Автоматичне запікання** — при Pick маски одразу пекти у
  `selected/<model>/` без окремого Save Polygons. Потребує uniform render
  з `run_segmentation` (див. v1.7.0).

---

## v1.6.x — Workspace flow (продовження)

### Експорт-pipeline для розподілу робіт по команді

- 📤 **Експорт частини файлів** — galочки в catalog → export
  тільки обраних.
- 📦 **Дроблення таски на N частин** — при експорті ввести
  N → авто-split по фото на N окремих zip-ів. Можливість вручну
  дублювати певні фото в декілька partings (для cross-check між
  анотаторами).
- 📊 **Статистика учасників** — показ хто скільки фото
  зробив (з `selections.json` поле `user`), таблиця/графік.
- 🔀 **Merge декількох папок при імпорті** — замість replace
  — merge по-фотографно.
- ✅ **v1.6.6** — Прибрати фото з датасету: кнопка exclude
  (переносить у `_excluded/`) + no-advance (без авто-переходу).

---

## v1.6.3 — Multi-class seed

**Контекст:** одна модель краще ловить `nucleus`, інша — `vesicle`.

### v1.6.3a Manual multi-seed ✅ DONE 2026-05-08 (v1.6.0)
Dropdown «модель» у Polygons toolbar. State `polygons.seedModel`.

### v1.6.3b Multi-class seed модалка ✅ DONE 2026-05-08 (v1.6.0)
Кнопка «🎯 Multi-seed» → таблиця `class → model → enable`. Apply →
`POST /api/polygons/<stem>/multi-seed` (overlap >60% IoU = skip).

### v1.6.3c Default model per class у Labels Manager ❌ NOT DOING
Рішення автора 2026-05-07: **не записувати `default_seed_model` у
`labels.json`**. Причина — набір моделей робочий і може змінюватись; жорстка
прив'язка класу до конкретної моделі швидко стане застарілою.

Замість цього:
- Multi-seed не має project-level default.
- Модель для класу обирається явно в модалці або береться із локального
  `localStorage` користувача.
- Якщо prefs немає, рядок класу стартує без моделі і не активується мовчки.
- Окреме UX-рішення на майбутнє: як поводиться перемикання active label,
  якщо для різних лейблів обрані різні seed-моделі/шари.

### Ідеї для майбутнього (v1.7+ або v2.0)

**D — Per-instance model voting**
Backend бере predictions з усіх моделей одночасно. Для кожного instance:
консенсус-класифікація (якщо A і B накладаються IoU>50% → майоритарний клас).
Користувач бачить готовий merged шар. Потрібна окрема conf-метрика.
**Складність:** ⭐⭐⭐⭐. Після спостереження за v1.6.3a/b у бою.

**E — Layer-based UI: показ всіх моделей одночасно**
Polygons-tab показує N полігон-шарів (по одному на модель), кожен з своєю
прозорістю + чекбоксом «прийняти shape». Кнопка «Merge selected» →
консолідує у `polygons/<stem>.json`. Макс контроль, але performance ризик
при ≥3 моделях × 100 instances. **Складність:** ⭐⭐⭐⭐⭐. Можливо v2.0.

**F — Shape-level model attribution (audit)**
Зберігати у LabelMe envelope для кожної shape поле
`source_model` / `seed_method` (напр. `"yolo11_680"`, `"manual_draw"`,
`"multi_seed_v1"`). Допоможе аналізувати яка модель частіше «виграє»
для якого класу. Не блокує наступні етапи; додавати при стійкому api.

---

## v1.7.0 — Архітектура ✅ DONE 2026-05-07

- 🏗️ **Уніфікувати render масок** — `run_segmentation` і Mask
  Picker cleanup/polygon baking тепер використовують спільний
  `cellsegkit.exporter.export_segmentation_bundle`. Доданий parity smoke-test.
- 📦 **Generic bake_all** — `tools/launchers/bake_all.py --data-dir data/<dataset>`
  більше не hardcoded на старий `data/nuclei`.

---

## v2.0.0 — Big rewrite

Умови виходу: ✅ баги v1.5.x закриті (v1.6.1/1.6.2/1.6.4) + більшість фічі
v1.6.x реалізовані (v1.6.3/1.6.5/1.6.6 + multi-seed) + нова чиста
структура після reorg + v1.7.0 uniform render. Залишилось: автоматичне
запікання при Pick, workspace export-частинами/N/stats/merge, UX-рішення для
multi-label/multi-model перемикання. Опційно — нові моделі (MicroSAM,
CellViT++, OmniPose).

### 🆕 v2.0.0 driver feature: Cell grouping tool _(запит команди 2026-05-09)_

**Контекст:** на нашому датасеті команда хоче об'єднувати скупчення кількох
везикул + 1-2 ядра у одну "клітину" з спільним id. Зараз кожна везикула /
ядро — окремий instance з власним id, без зв'язку. Потрібен новий інструмент
поверх Cleanup і Polygons, який працює на **вже запечених** масках і дозволяє:

- **Lasso / multi-select** інстансів (везикул + ядер) з click-drag rubber-band
  чи free-shape lasso.
- **3 типи груп** (рішення 2026-05-09):
  - `cell` — ≥1 nucleus + ≥1 vesicle (норм клітина).
  - `vesicle_cluster` — ≥1 vesicle, 0 nuclei (скупчення без ядра).
  - `nucleus_only` — ≥1 nucleus, 0 vesicles (рідкісне; multi-nuclear).
- **Persistence:** окремий файл `groups/<stem>.json` (parallel
  `polygons/<stem>.json`), не міняє існуючі формати. Schema у
  `docs/HANDOFF_v2_planning_2026-05-09.md`.
- **Перегляд/розгрупування** існуючих груп (overlay різних кольорів per
  group через HSL hue, type-validation badge).
- **Bake**: groups.json копіюється у dataset_<name>.zip окремою папкою.
  Споживач даних сам обирає як використати (group_id як канал у masks
  або окремий мета-файл для cell-level analysis).
  ✅ 2026-05-22: додано опційний експорт растрових масок — `semantic`
  (per-label) + `mask_groups` (group-instance), галочка «Маски» у вікні
  Експорту. Див. `CHANGELOG.md` / `docs/V2_DERIVED_MASKS_2026-05-22.md`.
- **Витяг даних** простий: "всі ядра" = polygons[label=nucleus],
  "клітини" = groups[type=cell], "скупчення везикул" =
  groups[type=vesicle_cluster].

**Чому третій інструмент, а не розширення Polygon-табу:** Polygon вже
перевантажений (Draw / Edit / Pick / Multi-seed / Pick-from-rejected /
base_label / Active label / 4 SVG-шари). Додавати ще один tool усередину —
розчиниться. Окремий tab "Groups" з власним state і toolbar логічніший:

```
[ 🧹 Cleanup ] [ ✏️ Polygons ] [ 🔗 Groups ]   ← новий 3-й tab
```

**Передумова:** маски вже запечені (Save Polygons виконано). Groups
працюють на existing `selected/<model>/npy/<stem>.npy` + `polygons/<stem>.json`,
не міняють їх — лише додають окремий `groups/<stem>.json` (per-image).

**Технічні нотатки:**
- Lasso: SVG path або polyline. Hit-test через point-in-polygon.
- Group rendering: hue per group (HSL), напівпрозорий fill поверх polygon.
- Undo/redo: окремий стек для groups tab (як cleanup/polygons).
- Експорт: `groups/<stem>.json` у participant ZIP + у dataset_<name>.zip.
- Tests: round-trip групування 3 instance → 1 group → 1 instance ungroup.

**Ризики/Складність:** ⭐⭐⭐⭐ (3-й tab + lasso + новий persistence файл).
Бажано робити одразу у v2.0.0 рамках, не патчити поверх v1.x.

### Залишилось до v2.0.0 — НОВИЙ план Day 7-12 (узгоджено 2026-05-19)

#### Day 7 — Lazy-bake + dirty markers + Save All + progress ✅ DONE 2026-05-19 (v1.9.0)

**Контекст:** Day 3a auto-bake on Pick виявився важким на 2k+ vesicle-фото —
blocking UI на десятки секунд. Знято immediate bake, додано batch-режим
"запекти все що змінилось" з progress bar'ом.

**Зроблено** (деталі — `docs/V2_DAY7_HANDOFF_2026-05-19.md`):
- Auto-rebake on Pick прибрано з `catalog.js`.
- `dirty: bool` у `selections.json[stem]` + StateStore методи
  `mark_dirty`/`clear_dirty`/`is_dirty`/`list_dirty`.
- Save Polygons / Save Cleanup → pure JSON save (без bake) — фікс bug
  «довге збереження».
- Кнопка `💾 Зберегти все` + `POST /api/workspace/bake-all` (background
  thread) + `GET /api/workspace/bake-progress` polling.
- **2 progress bars:** фото N/M + фази поточного фото.
- Кнопка `🔥` — re-bake all (для зміни labels.json).
- Жовта крапка dirty у sidebar; пропущені фото → червоні.
- Export preflight confirm коли є dirty.
- 7 нових pytest → 135/135 green.

#### Day 8 — Workspace flow доробка ✅ DONE 2026-05-19 (v1.10.0)

Зроблено (деталі — `docs/V2_DAY8_HANDOFF_2026-05-19.md`):
- 📊 **Stats per-user** — `by_user` у `/api/stats` + таблиця у statsModal.
- 📤 **Export-subset** — чекбокси у sidebar + `export?stems=`.
- 🎨 **Per-label overlays** — `overlays/<stem>__<label>.png` при export/finalize.
- 🔀 **Merge import з range-picker** — scan ZIP → modal діапазонів →
  selective merge (imported wins per-stem).
- 5 нових pytest → 140/140 green.

#### Day 8.5 — Workspace flow polish ✅ DONE 2026-05-19 (v1.11.0)

Допрацювання після першого огляду Day 8 (деталі —
`docs/V2_DAY8_5_HANDOFF_2026-05-19.md`):

- ♻️ **Restore з смітника** — `POST /api/restore/<stem>` + кнопка `♻`.
- 👤 **Bulk-призначення анотатора** — `POST /api/bulk-user`, кнопка `👤`
  + modal з range-picker.
- 📤 **Export rework** — `📤 Експорт` → окреме вікно: список фото з
  чекбоксами + «Обрати всі» + range-фільтр + опції Finalize / Split.
  Постійні sidebar-чекбокси і окрему кнопку `📦 Спліт` прибрано.
- 🔀 **Merge import — newest-wins** (зміна з imported-wins, рішення
  автора): фото зі змінами vs без → зі змінами; обидва зі змінами →
  новіший за `ts`. Файли копіюються лише якщо imported переміг.
- 5 нових pytest → 145/145 green.

#### Day 9 — Bug audit pass ✅ DONE 2026-05-20 (v1.12.0)

Зроблено (деталі — `docs/V2_DAY9_HANDOFF_2026-05-20.md`):
- 🐛 **Bug 1 — пікселізація Cleanup → Polygons** — Polygons-таб малював
  фоном дрібний overlay-PNG (775×589 проти оригіналу 2572×1956); `open()`
  виправлено на `bgSource="original"` + згладжування + CSS `image-rendering`
  лише для instance-масок.
- 🐛 **Bug 2 — async-flush race** — проміс-черга `_enqueueSave` (ніколи
  2 паралельних POST) + re-entrancy guard на `flushIfDirty`; backend —
  спільний `_atomic_write_json` з унікальним tmp (Flask `threaded=True`).
- 🐛 **Bug 3 — Save-кнопка з'їжджала** — закріплена `position:absolute`
  внизу-праворуч тулбара (клас `.editor__save`), стабільно на 3 табах.
- **Quick win #1** — `_pxPerCss` тепер враховує `object-fit: contain` →
  допуск кліку по ребру/вершині/маркеру відповідає закладеним CSS-px
  (раніше ~1.7× занижений). + t-рендж вставки 0.02→0.005.
- **Quick win #2** — Pick-чекбокс toggle (повторний клік знімає вибір).
- 1 новий pytest → 146/146 green.

**Лишилось у беклозі (НЕ входило у Day 9 bug audit):**
- Groups hide/show individual + filter view hold-N/V (відкладено з Day 4-5).
- 🎨 **UX multi-label/multi-model** — спрощення 3 селекторів у Polygons
  toolbar (Active label / base_label / Seed model), які зараз плутають.

#### Day 10 — Documentation rewrite

- Виправити mojibake у `AGENTS.md` (root) — частково зроблено 2026-05-19.
- Переписати `apps/mask_picker/README.md` під 1.8.0+ з усіма post-Day-6 fixes.
- Закрити gap'и доків (PROJECT_CONTEXT, для_мене/TODO, інші).
- Sync між `docs/AGENTS.md` (stub root) і `AGENTS.md` (root).

#### Day 11 — GitHub упаковка

- `git init` (зараз проект НЕ git-репо!).
- `.gitignore`:
  ```
  data/
  _backups/
  _archive/
  _tmp/
  _inbox/
  _send/
  *.pyc
  __pycache__/
  .pytest_cache/
  cryobiology4/weights/
  .idea/
  ```
- LICENSE (вибрати з автором).
- Top-level README.md (для GitHub repo сторінки).
- GitHub Actions для pytest (`apps/mask_picker/tests/`).
- Secret audit: token, email, OneDrive шлях, абсолютні шляхи у docs/скриптах.

#### Day 12 — APP_VERSION → 2.0.0

- bump `apps/mask_picker/state.py:63` → `"2.0.0"`.
- CHANGELOG entry `[2.0.0]` — підсумок усіх Days 1-11.
- git tag.
- Final pytest + manual checklist.
- Send-пакет 2.0.0 для команди.

---

### Curve Align — перенесено у post-v2.0.0

Раніше планувався як Day 7, але пріоритет змістився на lazy-bake (Day 7).
Curve Align — це medium feature з SVG polyline + Catmull-Rom snap.
Лишається у `V2_FUTURE_UX_IDEAS_2026-05-11.md` секція 2.

---

## 🆕 Annotation Quality & Multi-Annotator Comparison (post-v2.0.0)

**Запит юзера 2026-05-19.** Велика фіча — розширене вікно статистики +
метрики якості розмітки для крос-чеку між анотаторами. Дослідження
проведено 2026-05-19 (WebSearch). **НЕ робити у v2.0.0** — окремий етап.

### Передумова
Cross-check workflow: одне фото дають **кільком** анотаторам (Split уже
вміє дублювати фото у кілька partings). Потім порівнюємо їхні розмітки.

### Частина A — Розширена статистика (порівняння продуктивності)
- Відхилення кожного анотатора від середнього по команді (скільки
  розмітив vs avg).
- Хто більше / менше; ранжування.
- Radar-chart («багатокутник якості») по параметрах: швидкість,
  узгодженість з консенсусом, к-сть правок, к-сть rejected тощо.
- Попарне порівняння 2-4 анотаторів.

### Частина B — Inter-annotator agreement метрики
Для фото, розмічених кількома людьми, рахувати:
- **IoU / Dice** між парами розміток одного фото (per-instance матчинг
  через Hungarian / greedy IoU). Стандарт якості: inter-annotator
  **IoU ≥ 0.75**.
- **Cohen's kappa** (попарно, 2 анотатори), **Fleiss' kappa** (≥3).
  Стандарт: **Fleiss κ > 0.6**.
- **Boundary metrics** — Normalized Surface Distance (NSD): DSC не завжди
  корелює з якістю меж, NSD додатково.
- Підсвітити **найбільші розбіжності** між розмітками одного фото →
  юзер обирає правильний варіант.

### Частина C — STAPLE consensus
- **STAPLE** (Simultaneous Truth And Performance Level Estimation) —
  EM-алгоритм, що з N розміток оцінює sensitivity/specificity кожного
  анотатора і генерує консенсусну «ground truth».
- Обмеження STAPLE: не сходиться коли розмітки взагалі не перетинаються;
  недооцінює межі (majority voting); потребує ≥3 анотаторів при високій
  варіативності.
- Рекомендація з літератури: **мінімум 3 анотатори** на фото для
  надійного консенсусу.

### Частина D — Multi-version import (зберігати обидві розмітки)
**Запит автора 2026-05-19.** Поточний merge-import (newest-wins) лишає
тільки одну версію розмітки фото. Для крос-чеку треба:
- При імпорті, коли фото є у кількох наборах — **зберігати ВСІ версії**
  (напр. `polygons/<stem>__<source>.json`), а не лише переможця.
- Ручний вибір: який діапазон з якого набору взяти як **основний**
  (UI як range-picker в Export — «від №–до №» per джерело).
- Окрема дія «об'єднати у ground truth» — консолідація версій (STAPLE
  з Частини C або ручний вибір кращих shape).

### Технічні нотатки
- Дані вже є: `polygons/<stem>.json` per-stem; при крос-чеку треба
  зберігати розмітки per-annotator (напр. `polygons/<stem>__<user>.json`
  або окремі workspace).
- `selections.json[stem].user` — хто розмітив (Day 8.5 bulk-user це
  посилює).
- Бібліотеки: `SimpleITK` має STAPLE out-of-the-box
  (`sitk.STAPLE`); kappa — `scikit-learn` / `statsmodels`.
- Складність: ⭐⭐⭐⭐⭐. Окремий вкладений етап після 2.0.0.

---

## 🆕 Нові SOTA-моделі (post-v2.0.0)

**Запит автора 2026-05-19.** Після 2.0.0 — розширити набір моделей
сегментації для `output/<model>/`:
- **MicroSAM** — окремий conda env (Python 3.11), уже у беклозі.
- **CellViT++**, **OmniPose** — кандидати, оцінити на vesicles-датасеті.
- Можлива інтеграція через `cellsegkit.loader.model_loader` (фабрика
  моделей). Кожна нова модель — просто ще одна підпапка `output/<model>/`,
  Mask Picker автоматично її підхопить (`_discover_models`).
- Деталі SOTA-огляду — `docs/archive/SOTA_MODELS_REVIEW_2026-04-30.md`.

**Джерела дослідження:**
- BasicAI — Quality Metrics for CV Data Annotation
- «Assessing Inter-Annotator Agreement for Medical Image Segmentation»
  (NLM/IEEE 2023) — kappa + STAPLE
- Warfield et al. — STAPLE original (PMC1283110)

---

## Опційне (не блокує жоден реліз)

- 🚀 **PyInstaller `.exe`** — справжній `.exe` без Python-залежності.
  Зараз lightweight launcher (`tools/launchers/mask_picker_launcher.pyw`)
  достатній.
- 🧪 **MicroSAM experiment** — окремий conda env (Python 3.11).
  Деталі — `docs/archive/SOTA_MODELS_REVIEW_2026-04-30.md`.
- 🌑 **Dark mode** Mask Picker — у `для_мене/TODO.md` лежить.

---

## ❌ Скасовано

- ~~CVAT export / CVAT Task setup~~ — Mask Picker замінив CVAT повністю
  для нашого workflow (рішення 2026-05-07).
- ~~`export_to_cvat.py`~~ — видалено разом з `coco_export/`.
- ~~StarDist на Windows GPU~~ — TF несумісний; навіть якщо колись WSL2,
  пріоритет нижчий за нові SOTA-моделі.

---

## Як планується робота

1. **Закриті багфікси v1.5.x → v1.6.x** — лишаються в changelog як історія.
2. **Відкриті фічі v1.6.x** — розбити по 3-4 групи (Labels / Workspace flow / Stats),
  кожна — окремий цикл.
3. **Auto-bake при Pick** — наступний логічний крок після v1.7.0.
4. **v2.0.0** — коли все вище ✅.

Перед кодом: завжди план кроками + узгодження з автором (див.
`feedback_comms.md` у memory).
# Annotation handoff note — 2026-05-07

CLI-часть workspace flow закрыта для текущей задачи: `tools/launchers/make_annotation_task.py`
умеет собрать переносимый workspace/ZIP для участника по всем фото, конкретным
`--stems`, списку `--list` или с разбиением `--parts N`. UI-кнопка "финализации"
по-прежнему может быть отдельной UX-задачей, если нужен именно workflow из
браузера; сейчас верхний Export отдает результаты (`selected/`, `polygons/`,
`selections.json`), а task packer готовит задания для отправки участникам.
