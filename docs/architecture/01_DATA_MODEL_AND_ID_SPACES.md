# 01. Модель даних + ID-простори

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Фундамент: усе решта (`05`–`12`) посилається сюди. Звірено з кодом + реальним
> workspace `data/vesicles_good`. Знахідки → [`_AUDIT_FINDINGS.md`](_AUDIT_FINDINGS.md).

## Призначення
Mask Picker зберігає стан розмітки у **файлах на диску** (не в БД). Цей документ —
канонічна карта: які файли є, хто SoT, хто mirror, які три ID-простори інстансів і як
між ними конвертують. Без цієї карти будь-яка правка bake/groups/cleanup ризикує
розсинхроном (історично — джерело Bug 3/4/7/14, double-count, «привид»).

---

## 1. Карта файлів на диску (реальна, `data/vesicles_good`)

```
data/<dataset>/                              ← workspace root (cfg.workspace_dir)
├ images/<stem>.{jpg,png,tif,...}            ← оригінали (read-only). "Копия <stem>" толерується
├ labels.json                                ← КЛАСИ інстансів: [{id,name,color,shortcut}] (LabelMe)
├ group_classes.json                         ← КЛАСИ груп (1 на workspace): {version, classes:[…constraints]}
├ selections.json                            ← per-stem стан (status/model/cleanup/dirty/base_label/user)
├ output/<model>/                            ← RAW output моделі (SSoT, READ-ONLY для MP)
│   ├ npy/<stem>.npy                          ← raw instance ID (int32) — raw_iid простір
│   ├ png/<stem>.png · yolo/<stem>.txt · overlay/<stem>.png
│   └ (реально 7 моделей: cyto2, instanseg, instanseg_0605, instanseg_neuroblastoma,
│      cpsam_finetuned, yolo11_512, yolo11_680)
├ selected/<model>/                          ← BAKED output (MP ПИШЕ). Лише для ОБРАНИХ моделей
│   ├ npy/<stem>.npy                          ← filtered/baked instance ID — baked_iid простір
│   ├ png · yolo · overlay                    ← derived (cellsegkit render)
│   ├ cleanup.json                            ← per-model rejected SNAPSHOT (див. §2 — НЕ live-SoT!)
│   ├ semantic/ · mask_groups/ · overlays/    ← опційні derived при export ZIP
│   └ _backups/<stem>/<ts>/{npy,png,yolo,overlay}  ← ротація (BACKUP_KEEP=2)
├ polygons/<stem>.json                        ← SoT polygon-shapes (LabelMe v5.0.1 envelope)
│   └ _backups/<stem>/<ts>/polygons.json       ← ротація (POLYGON_BACKUP_KEEP=3)
├ groups/<stem>.json                          ← SoT cell-grouping
│   └ _backups/<stem>/<ts>/groups.json         ← ротація (GROUPS_BACKUP_KEEP=3)
├ skipped/<stem>.skipped.txt                  ← маркер пропущеного фото
├ _excluded/<stem>.<ext>                      ← виключені фото (api_state exclude; status="excluded",
│                                               відновлювані `unexclude`; catalog сканує окремо)
└ _tmp/                                        ← тимчасове staging (per-label overlays, derived masks,
                                                import-scan); НЕ артефакт стану
```

> **Реальність ≠ INTERNAL_ARCHITECTURE §2:** у workspace 7 output-моделей, але
> `selected/` має лише `instanseg` (єдина обрана). `_excluded/`, `_tmp/`,
> `group_overlay_index.html` (генерований) у старій карті відсутні — додано тут.

Хто пише/читає кожен (детально — `04_API_CONTRACT`):

| Файл | Пише | Читає |
|---|---|---|
| `output/<model>/npy` | `run_segmentation` (НЕ MP) | `/api/labels-rgb` (→ raw_iid у UI), bake (input) |
| `selected/<model>/npy` | bake (`baking._bake_polygons_to_selected`) | derived masks, groups GET (`np.unique`), ZIP |
| `selections.json` | `StateStore` (`set`/`set_cleanup`/`mark_dirty`) | catalog, editor open (GET cleanup/state), bake (rejected!) |
| `selected/<model>/cleanup.json` | `cleanup._write_cleanup_json` (лише cleanup-export) | finalize ZIP bundling |
| `polygons/<stem>.json` | `polygons._write_polygons_json` | bake, groups (label override), audit |
| `groups/<stem>.json` | `api_groups`, `baking` Layer 2, `data_sync` strip | groups GET, derived `mask_groups`, audit |
| `labels.json` | Labels Manager | bake (class_id map), front `appLabels` |
| `group_classes.json` | Group Classes Manager | classification (`_classify_group_membership`) |

---

## 2. SoT vs mirror — ⚠ з поправкою на lazy-bake (важливо!)

| Концепт | **Live SoT** (робочий стан) | Snapshot / mirror | Примітка |
|---|---|---|---|
| Status / model / dirty / base_label | `selections.json[stem]` | — | через `StateStore` (global lock) |
| **Rejected instances** | **`selections.json[stem].cleanup.rejected_instances`** | `selected/<model>/cleanup.json[stem].rejected` | див. нижче — ІНВЕРСІЯ vs стара дока |
| Markers («пропущена клітина») | `selections.json[stem].cleanup.markers` | `cleanup.json[stem].markers` | те саме |
| Polygon shapes | `polygons/<stem>.json` | `shapes[i].group_id` (mirror груп) | mirror не парсимо |
| Cell groups | `groups/<stem>.json` | `polygons.json.shapes[i].group_id` (для зовн. LabelMe) | mirror не парсимо |
| Raw labels | `output/<model>/npy` | png+yolo+overlay поряд | пише `run_segmentation` |
| Baked labels | `selected/<model>/npy` | png+yolo+overlay | пише bake |

### ⚠ Rejected: live-SoT = `selections.json`, НЕ `cleanup.json` (F-004)
INTERNAL_ARCHITECTURE §2 називає SoT-ом `selected/<model>/cleanup.json`. **Це
інвертовано для lazy-bake епохи (Day 7+).** Фактичний потік:
- POST `/api/cleanup/<stem>` (autosave) → `state.set_cleanup` → пише **лише
  `selections.json`** + `mark_dirty`. `cleanup.json` НЕ чіпається.
- GET `/api/cleanup` → `state.get_cleanup` → читає **`selections.json`**.
- bake (bake-all `_collect_bake_job:113`, finalize `:574`, rebake) бере
  `rejected` з **`selections.json`** (`cleanup.get("rejected_instances")`).
- `selected/<model>/cleanup.json` ІСТОРИЧНО писався **ТІЛЬКИ** `cleanup-export` (🔥,
  `api_cleanup.py:221`) → у звичайному flow (autosave+Save All) був відсутній/застарілий у ZIP.
  **✅ F-004 fix (2026-06-01):** тепер `data_sync.bake_with_resync` синхронізує його на **КОЖЕН**
  bake (`_sync_cleanup_json_after_bake`, зберігає user+markers). Читається для вкладання у
  finalize-ZIP (`api_workspace.py:645`).

→ Тобто live-SoT rejected = **`selections.json`**; `cleanup.json` — per-model deliverable-snapshot,
який ТЕПЕР завжди свіжий після bake (provenance у ZIP коректне). **Ключ різний:** `rejected_instances`
(selections) vs `rejected` (cleanup.json). Деталі → `05`, `12`; **F-004 ✅ fixed**.

### Per-model нюанс
`selections.json[stem].cleanup` тримає **один** запис (поле `model` + rejected) —
для поточно обраної моделі. Справжня per-model історія rejected існує лише у
`cleanup.json` (рідко пишеться). Для нашого workflow (1 модель на фото) — ок.

---

## 3. Три ID-простори інстансів

| Простір | Файл | Діапазон | Стабільність |
|---|---|---|---|
| **raw_iid** | `output/<model>/npy` | 1..~7000 (max на vesicles_good = 6931) | СТАБІЛЬНИЙ (MP не пише) |
| **baked_iid** | `selected/<model>/npy` | model: 1..7000 · polygon: 50000+ | стабільний між bake (reserved) |
| **polygon_index** | `polygons.json.shapes[]` | 0..N-1 (індекс) | НЕ стабільний при splice |

### 3.1. raw_iid
RGB-кодується (`cleanup._labels_to_rgb_png_bytes`: `id = R<<16|G<<8|B`) → `/api/labels-rgb`
→ фронт декодує у `cu.labelsInt32` (Int32Array на піксель). UI hit-test, lasso, covered —
усе в raw-просторі. `cleanup.rejected` — теж raw_iid.

### 3.2. baked_iid (reserved-ID, v1.16.0 — фундамент Solution B)
Bake: `cleaned = raw.copy(); cleaned[isin(rejected)] = 0`; уцілілі raw зберігають
**той самий** id → **`baked_iid == raw_iid`**. Polygon shape #k → `POLYGON_ID_BASE + k`
(детерміновано). Тому id, який фронт порахує з робочих даних (raw + polygon idx),
**дорівнює** тому, що дасть фінальний bake → інтерактивне групування без серверного
bake (Solution B). `group.instance_ids` — у baked-просторі.

### 3.3. polygon_index
Polygon-shape не має stable id — ідентифікується **індексом** у `shapes[]`. Видалення
shape зрушує всі вищі індекси на −1 → `group.polygon_indices` стає невалідним. **Fix
(Bug 5):** фронт `_polyRemapGroupsAfterShapeDelete` ремапить індекси при splice (один
undo-запис разом із групами; → `06`, `10`). SoT «полігон у групі» = `polygon_indices`,
**не** reserved-iid (→ `07`, `09`).

### 3.4. Перехід між просторами
Лише bake-time: `(raw + rejected + polygons) → baked`. `shape_idx_to_iid` (з
`_bake_polygons_into_labels`) — міст polygon_index → baked_iid, далі Layer 2 sync
кладе ці iid у `group.instance_ids` (→ `09`). Зворотного переходу нема (raw read-only).

---

## 4. Reserved-ID — деталі

`POLYGON_ID_BASE = 50000`, `POLYGON_ID_CEILING = 65000` (запас від uint16 стелі 65535
для PNG/mask_groups). Активується умовно (`baking.py:238`):
```python
use_reserved = raw_max < POLYGON_ID_BASE and len(shapes) <= (CEILING - BASE)
```
Інакше — **graceful fallback** на legacy `next_id = max(cleaned)+1` (з warning). На наших
даних reserved завжди активний (raw_max 6931 ≪ 50000; ~234 polygon/фото ≪ 15535 слотів).

### 3 копії константи (дубльовано навмисно — layering)
| Копія | Чому |
|---|---|
| `baking.py:111` | канонічна (bake пише) |
| `groups.py:112` | groups не тягне важкий baking (PIL/cv2) — лише state/group_classes |
| `static/modules/groups.js:176` | фронт `groupMemberCount` фільтрує `<BASE` |

**Guard:** `tests/test_groups_smoke.py:265 test_polygon_id_base_matches_baking` звіряє
`groups.POLYGON_ID_BASE == baking.POLYGON_ID_BASE`. **⚠ Лише Python-копії.** JS-копія
(`groups.js:176`) **не покрита** автоматичним guard-ом → ризик тихого дрейфу (finding **F-005**).

---

## 5. Compaction (dense 1..N) — лише deliverable

Робоче `selected/<model>/npy` лишається **sparse** (1..7000 + 50000+) — стабільні id,
щоб групи не дрейфували між bake. Замовнику ж instance-маска має йти **dense 1..N без
пропусків** (конвенція; багато тулів припускають `max(label)==num_instances`).

`data_sync.compact_instance_ids(labels, groups)` робить remap (npy + `groups.instance_ids`
**разом**, тим самим LUT; `cleanup.rejected` НЕ чіпає — у raw space). Безпечно (не повертає
next_id-колізію): remap детермінований, rejected уже видалені при bake.

### ⚠ Хто реально кличе compaction (F-003)
**ТІЛЬКИ** `tools/launchers/bake_all.py:242` (CLI `--pack` — замовницький deliverable).
**Flask `api_workspace_finalize` НЕ компактить** (`bake_with_resync` → sparse, бандл as-is) —
тобто finalize-ZIP містить sparse id (50000+), це «send-back» пакет для round-trip команди,
а не dense customer-deliverable. INTERNAL_ARCHITECTURE §6 («finalize → COMPACTION») —
неточність. Деталі → `12`.

---

## 6. Atomic write — два ідіоми

| Ідіом | Де | Захист |
|---|---|---|
| **`state._atomic_write_json`** (unique tmp via `mkstemp` + `os.replace`) | cleanup/polygons/groups/data_sync/baking — high-concurrency autosave | паралельні POST того самого файлу не клобають спільний tmp (Flask threaded) |
| **fixed `.json.tmp` + `os.replace`** | `StateStore._flush` (selections.json), `_save_label_classes`, `group_classes._write_classes` | StateStore — під global `threading.Lock`; labels/classes — рідкісні writes |

Обидва атомарні (os.replace). Multi-file 2PC немає (кожен write per-file атомарний;
crash між writes теоретично можливий — прийнятно, короткий ланцюг; → `11`).

---

## 7. Інваріанти + lifecycle (зведення; деталі — у механіках)

| ID | Тип | Правило | Self-heal |
|---|---|---|---|
| I1/I3 | strict | iid / polygon_index унікальні в межах групи | POST validate |
| I4 | strict | iid не у двох групах | `_enforce_single_membership` (last-wins) |
| I5 | lazy | rejected ∩ instance_ids = ∅ | bake `cleaned[isin(rejected)]=0` → orphan → strip |
| B3 | lazy | instance_ids ⊆ unique(baked npy) | `_strip_orphan_instance_ids` (GET + bake/disk) |
| B1/B2 | strict | polygon_index валідний; group_id ∈ groups∪None | classify report / POST validate |
| I2 | soft | polygon label == semantic під ним | advisory (audit) |

**Lifecycle SoT (що при зміні):**
- **reject add/remove** (raw_iid) → selections.json; covered (derived) перераховується на фронті; bake застосує.
- **polygon add/remove/edit** → polygons.json; splice ремапить `group.polygon_indices` (Bug 5); covered-кеш інвалідується.
- **group add/remove iid|polygon** → groups.json; single-membership; класифікація; mirror group_id.
- **bake** → baked npy перенумеровує; Layer 2 sync + strip-orphan реконсилюють groups.

---

## 8. Консюмери data-model концептів (grep-таблиця = аудит)

| Концепт | SoT | Усі консюмери (back / front) |
|---|---|---|
| rejected (raw_iid) | selections.json | back: bake-job/finalize/rebake/cleanup-export; front: `cu.rejectedSet`, cleanup/groups/zoompan (→ `05`) |
| polygon-у-групі | `polygon_indices` | back: `_count_labels_in_group`,`_iids_by_label_in_group`,Layer 2 sync,mask_groups; front: `groupMemberCount` (→ `07`,`09`) |
| reserved-iid (≥50000) | bake-артефакт | `_bake_polygons_into_labels`(пише), 3 копії BASE, Layer 2 stale-strip, front skip-у-count |
| baked_iid | selected/npy | groups GET (`np.unique`), derived masks, compaction, strip-orphan |

---

## Known limitations / посилання
- **I5 next_id collision** (legacy fallback) — лише якщо reserved недоступний (не на наших даних). Reserved-ID усунув це для типового workspace.
- **F-003/F-004/F-005** — див. findings (compaction-only-pack; rejected SoT inversion; JS BASE без guard).
- INTERNAL_ARCHITECTURE.md (v1.15.0) §2/§6 потребує синхронізації під цей файл (F-002).
