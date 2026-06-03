# Mask Picker — архітектурна документація

> **Жива, багатофайлова технічна документація** додатку Mask Picker (Cryobiology V),
> по підсистемі/механіці. Мета: щоб і людина, і AI-асистент могли детально зрозуміти
> **де / що / як** працює, з усіма звʼязками front↔back і tool↔tool.
>
> Це **вехікул аудиту**: трасуючи кожен звʼязок при написанні спеки, ловимо
> глибинні баги (неузгодженості між консюмерами одного концепту) → лог у
> [`_AUDIT_FINDINGS.md`](_AUDIT_FINDINGS.md).
>
> Орхеструється майстер-планом [`../AUDIT_AND_DOCS_PLAN.md`](../AUDIT_AND_DOCS_PLAN.md).
> Метод аудиту — [`../CODE_AUDIT_PRINCIPLES.md`](../CODE_AUDIT_PRINCIPLES.md).
> Стан: **APP_VERSION 2.0.0, pytest 237/237**.

---

## Як читати

1. Почни з цього README — big-picture lifecycle + ID-простори + межа front↔back.
2. Далі — `01_DATA_MODEL_AND_ID_SPACES` (фундамент: усі дані й 3 ID-простори).
3. Потім за інтересом: backend (`03`), API (`04`), або конкретна механіка (`05`–`12`).
4. Для аудиту — `14_CROSS_CUTTING_MAP` (реєстр multi-representation концептів) +
   `_AUDIT_FINDINGS` (живий лог багів).

**Numbering 01–14 — порядок читання, не жорстка ієрархія.** Кожен файл —
самодостатня технічна записка.

---

## Індекс

| # | Файл | Що описує | Статус |
|---|---|---|---|
| — | [README.md](README.md) | Цей індекс + big-picture lifecycle | ✅ |
| 01 | [01_DATA_MODEL_AND_ID_SPACES.md](01_DATA_MODEL_AND_ID_SPACES.md) | Файли на диску, 3 ID-простори, SoT vs mirror, atomic writes | ✅ |
| 02 | [02_FRONTEND_STATE.md](02_FRONTEND_STATE.md) | `editor.state.*`, ефемерний браузерний стан, межа «різної памʼяті» | ✅ |
| 03 | [03_BACKEND_MODULES.md](03_BACKEND_MODULES.md) | Модулі backend + граф імпортів + layering rules | ✅ |
| 04 | [04_API_CONTRACT.md](04_API_CONTRACT.md) | Кожен ендпоінт: метод/шлях/req/resp/side-effects = межа front↔back | ✅ |
| 05 | [05_CLEANUP_TOOL.md](05_CLEANUP_TOOL.md) | Reject (rejectedSet, raw_iid) + covered + markers + рендер/hit-test | ✅ |
| 06 | [06_POLYGONS_TOOL.md](06_POLYGONS_TOOL.md) | Draw/Pick/Seed/edit-in-draw, draft, lasso, reserved-ID при bake | ✅ |
| 07 | [07_GROUPS_TOOL.md](07_GROUPS_TOOL.md) | Групування, single-membership, polygon_indices vs instance_ids, класифікація | ✅ |
| 08 | [08_GROUP_COLORS.md](08_GROUP_COLORS.md) | OKLCH-логіка `effectiveHSL`, 4 константи, blend, white stroke | ✅ |
| 09 | [09_BAKING_AND_RESERVED_IDS.md](09_BAKING_AND_RESERVED_IDS.md) | Bake pipeline, reserved-ID, Layer 2 sync, backstop, compaction | ✅ |
| 10 | [10_UNDO_HISTORY.md](10_UNDO_HISTORY.md) | Глобальний хронологічний стек, snapshot/restore, складені дії | ✅ |
| 11 | [11_AUTOSAVE_DIRTY_SYNC.md](11_AUTOSAVE_DIRTY_SYNC.md) | Per-domain dirty + debounce + `_enqueueSave` + race-guard | ✅ |
| 12 | [12_DELIVERABLE_EXPORT.md](12_DELIVERABLE_EXPORT.md) | Finalize/ZIP, semantic/mask_groups/overlays, що отримує замовник | ✅ |
| 13 | [13_SEGMENTATION_BOUNDARY.md](13_SEGMENTATION_BOUNDARY.md) | Контракт із сегментацією: raw output = read-only SSoT | ✅ |
| 14 | [14_CROSS_CUTTING_MAP.md](14_CROSS_CUTTING_MAP.md) | Граф tool↔tool + реєстр multi-representation концептів (аудит-карта) | ✅ |
| — | [_AUDIT_FINDINGS.md](_AUDIT_FINDINGS.md) | Живий лог знайдених багів (severity/repro/консюмери/статус) | ✅ (живий) |

**Легенда статусу:** ✅ готово · 🔲 SKELETON (наповнюється у Phase 2) · 🚧 в роботі.

---

## Big-picture — lifecycle одного фото (з висоти пташиного польоту)

> Workflow **НЕ строго лінійний**: Cleanup/Polygons/Groups — ітеративні таби, юзер
> вільно вертається назад (часто на Groups помічає прогалину → назад у Polygons/
> Cleanup). Bake — серверний, **лише на Save all / Finalize** (не в інтерактиві,
> Solution B v1.16.2). Нижче — один логічний прохід.

```
 run_segmentation  (НЕ Mask Picker — зовнішнє, Cryobiology III / cellsegkit)
        │
        ▼
 output/<model>/{npy,png,yolo,overlay}        [RAW SSoT · raw_iid · READ-ONLY]
        │  editor.open(stem, model)
        │  GET /api/labels-rgb · /api/cleanup · /api/polygons · /api/groups
        ▼
╔════════════════ БРАУЗЕР (ефемерний стан, поки editor відкритий) ════════════════╗
║  Cleanup           Polygons              Groups                                  ║
║  cu.rejectedSet    pg.shapes             gr.list                                 ║
║  cu.markers        pg.draft, lasso       gr.classifications                      ║
║  (+covered derived з геометрії полігонів >50%)   state.history (global undo)     ║
║                   ↕ ітеративно, юзер вертається назад ↕                          ║
╚═══════════ autosave 5с debounce / flushIfDirty(close,switch,nav) ═══════════════╝
        │  POST /api/cleanup · /api/polygons · /api/groups        (JSON only, БЕЗ bake)
        ▼
 cleanup.json(raw_iid) · polygons/<stem>.json · groups/<stem>.json   [SoT НА ДИСКУ]
        │  «Зберегти все» / Finalize  →  data_sync.bake_with_resync
        ▼
 baking._bake_polygons_to_selected:
   raw npy − rejected(=0) + полігони(reserved id = 50000+idx) → backstop(<15%→0)
   → export_segmentation_bundle (cellsegkit: npy/png/yolo/overlay)
   → Layer 2: _sync_groups_instance_ids_after_bake (polygon→iid у group.instance_ids)
 data_sync._strip_orphans_in_groups_file        [B3 self-heal на диску]
        ▼
 selected/<model>/{npy,png,yolo,overlay}     [baked_iid · sparse working (1..7000 + 50000+)]
        │  Finalize / --pack
        │  compact_instance_ids (dense 1..N)  +  export_derived_masks
        ▼
 ZIP замовнику: images + selected/{npy,png,yolo,overlay,semantic,mask_groups,
                overlays} + polygons + groups(dense) + cleanup + labels + skipped
        │
        ▼
 Замовник → clusterization.py: npy + semantic + mask_groups.png + groups.json
   → 1 group = 1 клітина (1 nucleus + N vesicles), instance id 1..N без стрибків
```

---

## ID-простори — швидка довідка (деталі → `01`)

| Простір | Де живе | Хто пише | Консюмер |
|---|---|---|---|
| **raw_iid** | `output/<model>/npy` (RO SSoT) | `run_segmentation` (НЕ MP) | UI hit-test (`cu.labelsInt32` через `/api/labels-rgb`); `cleanup.rejected` тут |
| **baked_iid** | `selected/<model>/npy` | bake (MP) | `group.instance_ids`; derived masks |
| **polygon_index** | `polygons.json.shapes[]` (індекс) | UI draw / write | SoT «полігон у групі» (`group.polygon_indices`) |

**Reserved-ID (v1.16.0):** уцілілі raw → `baked==raw` (raw<7000); полігон shape #k →
`POLYGON_ID_BASE(50000)+k`. Це **фундамент Solution B**: фронт рахує id з робочих
даних = тим, що дасть фінальний bake → інтерактивне групування без серверного bake.

---

## Конвенції документа

- **SoT** = Single Source of Truth. **Mirror** = derived, не парсимо.
- **Концепт-орієнтований аудит:** для кожного концепту — SoT + усі представлення +
  усі консюмери (grep, не памʼять). Розбіжність трактування = баг → findings.
- Посилання на код: `шлях:рядок` (напр. `baking.py:111`).
- Дати — абсолютні. Версії — `[X.Y.Z]` ↔ `../CHANGELOG.md`.
