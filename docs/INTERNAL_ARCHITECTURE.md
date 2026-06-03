# Mask Picker — внутрішня архітектура (v1.15.0)

> ⚠ **ЧАСТКОВО ЗАСТАРІЛО (станом на APP_VERSION 1.16.2).** Авторитетна, актуальна
> архітектура — **[`docs/architecture/`](architecture/README.md)** (багатофайлова, заповнена
> 2026-06-01). Цей файл лишено як історичний (v1.15.0). Ключові дельти, яких тут НЕ враховано:
> - **reserved-ID (v1.16.0):** полігон → `POLYGON_ID_BASE(50000)+idx`, не `next_id=max+1` (тут §3.2/§5 застарілі) → [`01`](architecture/01_DATA_MODEL_AND_ID_SPACES.md)/[`09`](architecture/09_BAKING_AND_RESERVED_IDS.md).
> - **rejected live-SoT = `selections.json`**, не `selected/<model>/cleanup.json` (тут §2 SoT-таблиця інвертована для lazy-bake) → [`01`§2](architecture/01_DATA_MODEL_AND_ID_SPACES.md), finding F-004.
> - **compaction лише `bake_all.py --pack`**, finalize НЕ компактить (тут §6 крок 6 неточний) → [`12`](architecture/12_DELIVERABLE_EXPORT.md), F-003.
> - **Layer 1/2 дедуп reserved-iid, Solution B (client-side lasso), глобальний undo** (v1.16.1/2) — тут відсутні → [`07`](architecture/07_GROUPS_TOOL.md)/[`10`](architecture/10_UNDO_HISTORY.md).
>
> Нижче — оригінальний текст v1.15.0 (довіряй коду / новим docs, не цьому файлу де є розбіжність).

Документ описує **як влаштований** Mask Picker зсередини: які файли є
джерелами правди (SoT), хто пише, хто читає, які invariants і як вони
тримаються, lifecycle одного фото від початку до фінального ZIP.

Дата: 2026-05-28. Версія додатку: **1.15.0**.

Для cold-start ritual і поточних задач → `docs/CONTEXT_FOR_NEXT_CHAT.md`
+ `docs/ROADMAP.md`. Для історії змін → `docs/CHANGELOG.md`.

---

## 1. Огляд

**Mask Picker** — Flask web-app (`apps/mask_picker/`) для ручної доразмітки
датасету Cryobiology V. Анотатор працює з фото в браузері: відмічає погані
маски (Cleanup), домальовує полігонами (Polygons), об'єднує клітини у
групи (Groups). Backend пише шість файлових артефактів per stem +
запікає (bake) фінальні маски для замовника.

**Три mutation domains** (кожен — свій таб у редакторі + свій файл на диску):
- **Cleanup**  — `selected/<model>/cleanup.json` (SoT rejected per model).
- **Polygons** — `polygons/<stem>.json` (LabelMe envelope).
- **Groups**   — `groups/<stem>.json` (cell grouping).

**Один cross-cutting domain** — `data_sync.py` (диригент для операцій,
що зачіпають кілька файлів одразу, наприклад bake).

**Один immutable shared input** — `output/<model>/npy/<stem>.npy` (raw
output від моделі сегментації, read-only SSoT).

---

## 2. Карта файлів на диску

```
data/<dataset>/                            ← workspace root
├ images/<stem>.{jpg,png,...}              ← оригінальні фото (read-only)
├ labels.json                              ← {nucleus, vesicle, ...} класи
├ group_classes.json                       ← кастомні класи груп
├ selections.json                          ← per-stem state (mirror)
├ output/<model>/                          ← RAW output від моделі (SSoT, read-only)
│   ├ npy/<stem>.npy                       ← raw instance ID (int32)
│   ├ png/<stem>.png                       ← 16-bit PNG mirror
│   ├ yolo/<stem>.txt                      ← bbox per instance
│   └ overlay/<stem>.png                   ← colored overlay
├ selected/<model>/                        ← BAKED output (Mask Picker пише)
│   ├ npy/<stem>.npy                       ← filtered instance ID (після rejected + polygons)
│   ├ png/<stem>.png                       ← 16-bit PNG mirror filtered
│   ├ yolo/<stem>.txt                      ← bbox + class_id per instance
│   ├ overlay/<stem>.png                   ← colored overlay filtered
│   ├ cleanup.json                         ← SoT rejected per stem (per model!)
│   ├ semantic/<stem>.png                  ← per-pixel class (опційно при export)
│   ├ mask_groups/<stem>.png               ← per-pixel group_id (опційно)
│   ├ overlays/<stem>__<label>.png         ← per-label overlays (опційно)
│   └ _backups/<stem>/<ts>/                ← rotational backup
├ polygons/<stem>.json                     ← SoT polygon-shapes (LabelMe v5.0.1)
├ polygons/_backups/<stem>/<ts>/           ← polygon backup rotation
├ groups/<stem>.json                       ← SoT cell grouping
├ groups/_backups/<stem>/<ts>/             ← groups backup rotation
├ skipped/<stem>.skipped.txt               ← skipped photo markers
└ _backups/                                ← workspace-level backups (zip)
```

### SoT vs mirror

| Артефакт | SoT | Mirror | Хто пише SoT |
|---|---|---|---|
| Status / model / dirty | `selections.json[stem]` | — | StateStore |
| Rejected instances | `selected/<model>/cleanup.json[stem].rejected` | `selections.json[stem].cleanup.rejected_instances` | `cleanup._write_cleanup_json` |
| Polygon shapes | `polygons/<stem>.json` | `polygons/<stem>.json.shapes[i].group_id` mirror groups | `polygons._write_polygons_json` |
| Cell groups | `groups/<stem>.json` | polygons.json.shapes[i].group_id (зовнішнім LabelMe-тулам) | `routes/api_groups.py::api_groups_set` |
| Raw labels | `output/<model>/npy/<stem>.npy` | png + yolo + overlay у тій же папці | `run_segmentation` (НЕ Mask Picker) |
| Baked labels | `selected/<model>/npy/<stem>.npy` | png + yolo + overlay | `baking._bake_polygons_to_selected` |

**Mirror не парсимо.** Якщо mirror != SoT — довіряємо SoT, mirror автогенерується наступним write.

---

## 3. ID-простори

Це **критично важливо** для розуміння. У Mask Picker одночасно живуть
два різні простори instance ID:

### 3.1. raw_iid — instance ID у raw output

`output/<model>/npy/<stem>.npy` — **стабільний**. Mask Picker сюди
**ніколи не пише**. Усі raw_iid не змінюються між сесіями (поки
`run_segmentation` не запустять заново).

UI читає raw labels через ендпоінт `GET /api/labels-rgb/<model>/<stem>.png`
(`cleanup._labels_to_rgb_png_bytes` — кодує int32 → RGB pixel). Frontend
читає RGB pixel у `cu.labelsInt32` — це raw_iid.

**`cleanup.rejected`** — список raw_iid, які юзер позначив як погані.

### 3.2. baked_iid — instance ID у filtered baked npy

`selected/<model>/npy/<stem>.npy` — **перенумерований** при кожному bake.

Bake pipeline:
1. Завантажує raw_npy.
2. `cleaned = raw_npy.copy()`.
3. `cleaned[np.isin(cleaned, rejected)] = 0` — rejected стають фоном.
4. Для кожного polygon-shape: `next_id = max(cleaned) + 1`, fill polygon у cleaned.
5. Записує filtered npy → `selected/<model>/npy/`.

baked_iid може дорівнювати raw_iid (для instance, які не змінилися).
Але **polygon-resolved baked_iid** (новий ID для polygon-shape) **може
випадково збігтися** з якимось raw_iid у `rejected` (це класичний
`next_id` collision).

**`group.instance_ids`** — список baked_iid (у просторі filtered npy).

### 3.3. polygon_index — індекс polygon-shape у polygons.json.shapes[]

Не стабільний при splice. Видалення shape зрушує всі вищі індекси на -1.
Bug 5 fix v1.14.0: фронт автоматично оновлює `group.polygon_indices` при
delete shape.

### 3.4. Чому це не плутає себе

- UI hit-test (Cleanup tab) використовує raw_iid → `cu.labelsInt32`.
- UI render (Groups tab mask canvas) використовує raw_iid теж.
- Backend bake читає raw → пише filtered.
- Backend групи читає filtered (`np.unique(selected/<model>/npy)`).
- Перехід між просторами: bake-time (raw + polygons → filtered).

Якщо raw output **перерендериться** (`run_segmentation` на тих же фото
з новими ML-моделями) — старі raw_iid стають неактуальними. Тоді корисний
helper `data_sync.reseat_rejected_after_bake` (поки не активний у звичайному
flow; safety net для migration).

---

## 4. Mutation domains — хто що робить

### 4.1. Cleanup (`selected/<model>/cleanup.json`)

| Дія | Endpoint | Що оновлюється |
|---|---|---|
| Toggle reject | POST `/api/cleanup/<stem>` | cleanup.json + selections.json mirror |
| Read state | GET `/api/cleanup/<stem>` | state[stem].cleanup |

**Cleanup is per-model** — кожна модель має свій cleanup.json. Якщо юзер
змінив selected model — попередній rejected не переноситься (інший
ID-простір).

### 4.2. Polygons (`polygons/<stem>.json`)

| Дія | Endpoint | Що оновлюється |
|---|---|---|
| Save JSON only | POST `/api/polygons/<stem>` | polygons.json |
| Save + Bake | POST `/api/polygons-export/<stem>` | polygons.json + bake (через data_sync) |
| Seed-from-mask | POST `/api/polygons/<stem>/seed-from-mask` | повертає shapes (без write) |
| Multi-seed | POST `/api/polygons/<stem>/multi-seed` | повертає shapes (без write) |

LabelMe v5.0.1 envelope формат — сумісність з LabelMe Desktop.

### 4.3. Groups (`groups/<stem>.json`)

| Дія | Endpoint | Що оновлюється |
|---|---|---|
| Read + classify | GET `/api/groups/<stem>` | повертає envelope + classifications + stale_removed |
| Save groups | POST `/api/groups/<stem>` | groups.json + polygons.json mirror |
| Lasso hit-test | POST `/api/groups/<stem>/lasso-hit-test` | повертає bakedIds |

POST автоматично робить:
- `_enforce_single_membership` (last-wins для iid у двох групах);
- `_classify_group_membership` (cell / vesicle_cluster / nucleus_only);
- `_strip_orphan_instance_ids` (in-memory) — видаляє iid яких нема у baked npy.

### 4.4. Cross-cutting через `data_sync.py` (v1.15.0)

| Дія | Endpoint | Що оновлюється |
|---|---|---|
| Bake | POST `/api/polygons-export`, `/api/rebake`, finalize, bake-all | filtered npy + groups.json (sync + strip orphan) + cleanup.json mirror |

`data_sync.bake_with_resync` — drop-in заміна `_bake_polygons_to_selected`.
Додає self-heal крок `_strip_orphans_in_groups_file` напряму на диску —
це закриває lazy invariant B3 у batch-bake без UI.

Public helper `reseat_rejected_after_bake` зараз **не використовується**
у звичайному bake (raw стабільний). Залишений як safety-net для майбутнього
сценарію перенумерування raw (наприклад при reload моделей).

---

## 5. Invariants (правила що тримаються у даних)

| ID | Тип | Правило | Self-heal |
|---|---|---|---|
| I1 | strict | iid унікальний у межах одного group.instance_ids | — (POST validate) |
| I3 | strict | polygon_index унікальний у group.polygon_indices | — (POST validate) |
| I4 | strict | iid НЕ у двох групах (single-membership) | `_enforce_single_membership` last-wins при POST |
| I5 | lazy | rejected ∩ group.instance_ids == ∅ | при bake `cleaned[isin(rejected)]=0` → orphan → strip |
| B1 | strict | polygon_index валідний для polygons.shapes | `_classify_group_membership` reports invalid |
| B2 | strict | polygon.group_id ∈ groups[].id або None | POST validate |
| B3 | lazy | group.instance_ids ⊆ unique(baked npy) | `_strip_orphan_instance_ids` (GET + bake) |
| I2 | soft | polygon label == semantic class під ним | advisory (audit_export warning) |

**Strict** — порушується = система зламана. Code path має валідувати.
**Lazy** — порушується між mutation і bake. Self-heal при bake (B3) або
наступному GET (I5/B3).
**Soft** — advisory тільки, не блокує.

### Відомі обмеження (v1.15.0)

**I5 next_id collision:** rejected (raw_iid) може випадково збігатись з
polygon-resolved baked_iid. Якщо тупо strip — видалимо legit polygon
(Bug 7 revert підтвердив це: 13 LOST cells). Зараз I5 = ~37-60 порушень
на типовому workspace, але семантично OK. Реальне рішення (далі за
v1.15.0): резервувати ID-діапазон для polygon-resolved іids щоб уникати
колізій.

---

## 6. Lifecycle одного фото (sequence)

> **ВАЖЛИВО — workflow НЕ строго лінійний.** Кроки 2-4 (Cleanup / Polygons /
> Groups) — це не одноразовий конвеєр, а **ітеративні таби**, між якими
> анотатор вільно перемикається. Типовий реальний сценарій:
> - почав Cleanup → перейшов у Polygons → почав робити Groups →
> - **на етапі Groups помітив, що пропустив полігон** → повернувся у Polygons,
>   домалював → знову Groups;
> - **помітив, що десь не прибрав зайве** → повернувся у Cleanup, прибрав →
>   знову Groups.
>
> Тобто Groups часто стає місцем, де видно прогалини попередніх двох
> етапів, і людина повертається «на 2 сторінки назад». Тому стан усіх трьох
> табів живе одночасно (`this.state.cleanup/polygons/groups`), autosave
> незалежний per-таб, а bake застосовує підсумок усіх трьох. Послідовність
> нижче — лише ОДИН прохід; реально їх кілька з поверненнями.

```
1. Анотатор відкриває editor.open(stem, model).
   ┌─────────────────────────────────────────────┐
   │ Frontend                                    │
   │   GET  /api/labels-rgb/<model>/<stem>.png   │ ← raw_iid у cu.labelsInt32
   │   GET  /api/cleanup/<stem>                  │ ← rejected раніше
   │   GET  /api/polygons/<stem>                 │ ← LabelMe envelope
   │   GET  /api/groups/<stem>                   │ ← envelope + classifications
   │                                              │   + stale_removed (toast)
   └─────────────────────────────────────────────┘

2. Cleanup tab — юзер клікає на бракені маски.
   cu.rejectedSet.add(raw_iid) → debounce 5s → POST /api/cleanup/<stem>
   → cleanup.py._write_cleanup_json (atomic write)
   → state.mark_dirty(stem)

3. Polygons tab — юзер домальовує.
   pg.shapes.push(...) → debounce 5s → POST /api/polygons/<stem>
   → polygons._write_polygons_json (atomic write)
   → state.mark_dirty(stem)

4. Groups tab — юзер об'єднує клітини.
   state.groups.list.push(...) → debounce 5s → POST /api/groups/<stem>
   → routes.api_groups_set:
     a) _enforce_single_membership(groups) → moves[]
     b) _strip_orphan_instance_ids(groups, known_iids) → stale_removed
     c) _classify_group_membership(...)
     d) groups._write_groups (atomic)
     e) polygons.json mirror sync (group_id у shape)

5. Save All — фоновий bake усіх dirty stems.
   POST /api/workspace/bake-all → _run_bake_all thread
   для кожного dirty stem:
     data_sync.bake_with_resync(cfg, stem, model, src_npy, shapes, ...)
       1. baking._bake_polygons_to_selected:
          a) cleaned = raw_npy; cleaned[isin(rejected)] = 0
          b) bake polygons → reserved id (POLYGON_ID_BASE + shape_idx,
             v1.16.0; колізія з raw id неможлива), class_id_map
          c) bake-time backstop: raw instance >85% перекритий полігоном
             → стерти фрагмент (Bug 14)
          d) export_segmentation_bundle → npy/png/yolo/overlay
          e) _sync_groups_instance_ids_after_bake → group.instance_ids
             += polygon-resolved iid (authoritative resolve, Bug 3)
       2. data_sync._strip_orphans_in_groups_file (v1.15.0):
          - read groups.json
          - known_iids = unique(filtered npy)
          - strip orphans + atomic write якщо щось strip-нуто
     state.clear_dirty(stem)

   ПРИМІТКА: selected/<model>/npy лишається у reserved-range просторі
   (sparse: 1..~7000 модель + 50000+ полігони). Ці id СТАБІЛЬНІ між bake
   (полігон #3 завжди 50003) — тому групи не дрейфують. Це РОБОЧЕ
   представлення, не deliverable.

6. Finalize / Export ZIP — стрим одного або кількох фото.
   GET /api/workspace/finalize/<stem> → bake (через bake_with_resync) →
   COMPACTION (v1.16.0): npy + groups переномеровуються у щільні 1..N
   (data_sync.compact_instance_ids) → ZIP з ЧИСТИМИ contiguous id.
   ZIP: images/<stem>.jpg + selected/<model>/{npy,png,yolo,overlay,semantic,
   mask_groups,overlays}/ + polygons/<stem>.json + groups/<stem>.json (dense) +
   selected/<model>/cleanup.json (filtered per stem) + labels.json + skipped.

   COMPACTION (deliverable-only):
   - sparse working id (1..7000 + 50000+) → dense 1..N без пропусків.
   - npy + groups.instance_ids переномеровуються РАЗОМ (той самий remap).
   - cleanup.rejected НЕ чіпається (raw space, стабільний).
   - застосовується ЛИШЕ при export/finalize/pack — робоче selected/
     лишається sparse-stable (групи не дрейфують між bake).

7. Замовник отримує ZIP з contiguous 1..N масками.
   clusterization.py читає: npy + semantic + groups.json
   → group_id = pixel у mask_groups.png
   → ground truth: 1 group = 1 клітина (1 nucleus + N vesicles)
   → instance id у npy = 1..N без стрибків (стандарт instance-масок)
```

---

## 7. Atomic write — як data_sync гарантує цілісність

Усі JSON-writes у Mask Picker йдуть через `state._atomic_write_json`:

```python
def _atomic_write_json(path, payload):
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                     prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_name, path)  # POSIX atomic у межах одного FS
```

- **unique tmp** (через `tempfile.mkstemp`) — захист від колізій під
  Flask `threaded=True` (інакше два паралельних writes одного файлу
  клобають спільний `.json.tmp`).
- **os.replace** — атомарний у межах однієї файлової системи.

`data_sync.bake_with_resync` поки **не** робить multi-file two-phase
commit (атомарний запис кількох файлів одразу). Кожен write атомарний
per-файл; між writes може бути crash → один файл новий, інший старий.
Це **прийнятно** для нашого use case (всі writes у одному короткому
ланцюжку, crash маловірогідний). Real 2PC — кандидат для майбутнього
рефакторингу якщо знайдеться real-world корнер-кейс.

---

## 8. Залежності модулів (граф)

```
              ┌────────┐
              │state.py│  (foundation — APP_VERSION, Config, _atomic_write_json)
              └───┬────┘
                  │ imports
        ┌─────────┼──────────┬──────────┬──────────┐
        ▼         ▼          ▼          ▼          ▼
   ┌──────┐ ┌────────┐ ┌─────────┐ ┌────────┐ ┌─────────────┐
   │cleanup│ │polygons│ │ groups  │ │ catalog│ │ workspace   │
   └───┬──┘ └────┬───┘ └────┬────┘ └───┬────┘ └─────┬───────┘
       │         │          │          │            │
       └────┬────┴──────────┘          │            │
            ▼                          │            │
        ┌────────┐                     │            │
        │baking  │  (_bake_polygons_to_selected — orchestrator)
        └────┬───┘                     │            │
             │                          │            │
             ▼                          │            │
        ┌──────────┐                    │            │
        │data_sync │  (bake_with_resync — wrapper + strip orphan)
        └────┬─────┘                    │            │
             │                          │            │
             ▼                          ▼            ▼
        ┌────────────────────────────────────────────┐
        │ routes/api_* — тонкий HTTP layer           │
        │  api_polygons.py — використовує data_sync  │
        │  api_workspace.py — використовує data_sync │
        │  api_cleanup.py  — пише cleanup.json напряму│
        │  api_groups.py   — пише groups.json напряму│
        └────────────────────────────────────────────┘
```

**Правила circular imports:**
- `state.py` не імпортує нікого.
- `cleanup/polygons/groups` імпортують тільки state.
- `baking` імпортує state + cleanup + polygons (НЕ groups — runtime через
  groups_path read).
- `data_sync` імпортує state + baking + groups.
- `routes/*` імпортує всіх.

---

## 9. Тести

**195 + 20 нових = 215 pytest** як baseline. Структура:

- `test_baking_smoke.py` — 12 (Bug 3 + v1.13.1 hotfix + derived masks).
- `test_audit_export.py` — 12 (Phase 3 invariants CLI).
- `test_groups_api.py` — 26 (incl. single-membership, polygon mirror).
- `test_groups_smoke.py` — 8.
- `test_polygons_smoke.py` — 16 (incl. test_api_version → "1.15.0").
- `test_cleanup_smoke.py` — 10.
- `test_annotation_task_pack.py` — 6.
- `test_data_sync.py` — 20 (v1.15.0: reseat pure function + bake_with_resync
   integration + _strip_orphans_in_groups_file).

**Інваріант-вериficator-и (CLI):**
- `_tmp/desync_invariants.py` — 8 інваріантів у workspace (виконуваний).
- `_tmp/verify_bug3_clusterization.py` — емулює замовницький clusterization
  без patch'у (acceptance).
- `apps/mask_picker/tools/audit_export.py` — 9 інваріантів export, CLI
  для запуску перед send-у.

---

## 10. Корисні file-pointers

| Що шукаєш | Де |
|---|---|
| APP_VERSION | `apps/mask_picker/state.py:65` |
| Bake orchestrator | `apps/mask_picker/baking.py::_bake_polygons_to_selected` |
| Cross-cutting wrapper | `apps/mask_picker/data_sync.py::bake_with_resync` |
| Strip orphan (in-memory) | `apps/mask_picker/groups.py::_strip_orphan_instance_ids` |
| Strip orphan (на disk) | `apps/mask_picker/data_sync.py::_strip_orphans_in_groups_file` |
| Single-membership | `apps/mask_picker/groups.py::_enforce_single_membership` |
| Atomic JSON write | `apps/mask_picker/state.py::_atomic_write_json` |
| RGB-encoded raw labels | `apps/mask_picker/cleanup.py::_labels_to_rgb_png_bytes` |
| Reseat helper (safety net) | `apps/mask_picker/data_sync.py::reseat_rejected_after_bake` |

---

## 11. Що далі (post-v1.15.0)

- **Day 11** — GitHub упаковка: `git init`, `.gitignore`, LICENSE, README,
  GitHub Actions для pytest, secret audit.
- **Day 12** — APP_VERSION → 2.0.0: CHANGELOG entry, git tag, final pytest +
  manual checklist, send-пакет 2.0.0.
- **Post-2.0.0 ідеї** (з ROADMAP):
  - Annotation quality metrics (IoU/Dice/kappa/STAPLE).
  - Multi-version import (зберігати ВСІ versions фото).
  - SOTA-моделі (MicroSAM, CellViT++, OmniPose).
  - polygon-over-instance поведінка (відкладено юзером).
  - I5 collision-aware fix (резервація next_id діапазону).
  - Розширення data_sync на інші public mutators (delete_polygon,
    pick_polygon_from_instance, ...) — щоб усі cross-cutting операції
    мали єдиний шлях.

---

## Кінець документа

Якщо щось у коді розходиться з цим документом — **довіряй коду**, оновлюй
документ. Інваріант: цей файл змінюється разом з відповідним кодом
у тому ж PR/commit batch.
