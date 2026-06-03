# 13. Межа із сегментацією (контракт)

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> ⚠ Навмисно **легкий** — лише межа/контракт. Інтеграція seg-коду в репо = Day 11+ (§10 плану).
> Джерело: `state.py::_discover_models`, `cleanup.py`, BUGK-handoff (атрибуція cellsegkit).

## Призначення
Mask Picker (Cryobiology V) **не сегментує** — він доразмічає вже готовий raw output моделей.
Цей документ фіксує контракт між сегментацією (вище за течією) і MP: що MP очікує на вході,
що читає, чого ніколи не чіпає. Це найважливіша інтеграційна межа для Day 11.

---

## 1. Контракт входу
`run_segmentation` (зовнішнє — НЕ MP) пише per-модель per-фото:
```
output/<model>/npy/<stem>.npy      ← raw instance mask, int32, id 1..~7000 (0=фон). SSoT.
output/<model>/png/<stem>.png      ← 16-bit PNG mirror
output/<model>/yolo/<stem>.txt     ← bbox per instance
output/<model>/overlay/<stem>.png  ← кольоровий overlay (для перегляду в каталозі/редакторі)
```
- **raw_iid простір** (→ `01`§3.1). MP читає npy через `/api/labels-rgb` (RGB-encode у
  `cleanup._labels_to_rgb_png_bytes`) → `cu.labelsInt32`.
- **READ-ONLY SSoT:** MP **ніколи** не пише в `output/`. Тому raw_iid стабільні між сесіями
  (поки `run_segmentation` не запустять заново) — фундамент усієї ID-логіки.

## 2. Model discovery (`state.py::_discover_models`)
- Модель = підпапка `output/<name>/` з підпапкою `overlay/` всередині (`name` = cyto2,
  instanseg, …). Legacy: `overlay/` прямо в `output/` → `_legacy_root`. Порожні (без overlay) ігноруються.
- Реальний workspace `data/vesicles_good`: 7 моделей (cyto2, instanseg, instanseg_0605,
  instanseg_neuroblastoma, cpsam_finetuned, yolo11_512, yolo11_680). Юзер обирає одну per-фото (Pick).

## 3. cellsegkit (deliverable render) — атрибуція
- `export_segmentation_bundle` / `draw_overlay` (імпорт `state.py:78`) — з пакета **`cellsegkit`**
  (репо `github.com/nazarzharskyi/cryobiology3` = **Cryobiology III**, попереднє покоління;
  автор Fedir Yarovyi, MIT). Встановлюється editable (`pip install -e ./cryobiology3`; вендор-копія
  у `shared/cellsegkit`).
- **Призначення:** batch-експортер (прогнати модель → експортувати ВСІ формати раз/фото). MP
  **перевикористав** його у bake як deliverable-render (npy/png/yolo/overlay).
- **Solution B (v1.16.2):** cellsegkit прибрано з **інтерактивного** шляху — групування рахується
  з робочих даних на фронті (reserved-ID робить це коректним). cellsegkit лишається лише для
  **фінального deliverable** (Save all / Finalize). Тобто інтерактивна логіка стала оригінальною
  (не запозиченою з Cryobiology III). Деталі → [`09`](09_BAKING_AND_RESERVED_IDS.md)§1, BUGK-handoff.

## 4. Що MP читає / НЕ чіпає
| Артефакт | MP | Простір |
|---|---|---|
| `output/<model>/npy` | читає (bake input, labels-rgb) — **НЕ пише** | raw_iid |
| `output/<model>/{png,yolo,overlay}` | overlay читає для перегляду | — |
| `selected/<model>/*` | **пише** (bake) | baked_iid |

## 5. Regenerate raw — safety-net
Якщо `run_segmentation` перезапустять (нові моделі) на тих самих фото → raw_iid стануть інші,
старі `cleanup.rejected` (raw space) неактуальні. `data_sync.reseat_rejected_after_bake` (pure,
**зараз неактивний**) транслює rejected old→new через геометричний overlap (>0.5 → dominant new id).
Гачок для майбутнього migration-скрипта.

## 6. Roadmap-нотатка (НЕ цей трек)
- **Інтеграція seg-коду** (`apps/segmentation` / `run_segmentation`) у репо — найважливіша частина,
  питання «коли тулити» — **Day 11+** (план §10). Тут лише межа.
- Повне прибирання cellsegkit і з deliverable-експорту — окрема більша задача (зараз не потрібна).
- Звʼязки: ID-простори → [`01`](01_DATA_MODEL_AND_ID_SPACES.md); deliverable → [`12`](12_DELIVERABLE_EXPORT.md).
