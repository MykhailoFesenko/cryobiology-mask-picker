# 12. Deliverable / Export (що отримує замовник)

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Джерело: `routes/api_workspace.py` (export/finalize), `tools/launchers/bake_all.py` (`--pack`),
> `baking.export_derived_masks`/`export_per_label_overlays`, `data_sync.compact_instance_ids`.

## Призначення
Як робота анотаторів перетворюється на пакет для (а) команди (round-trip) і (б) замовника (фінал).
**Три шляхи експорту, ДВА layout-и, sparse vs dense** — це найзаплутаніше місце; документуємо точно.

---

## 1. Три шляхи експорту
| Шлях | Тригер | Аудиторія | Layout | ID |
|---|---|---|---|---|
| **Flask bulk** `/api/workspace/export` | UI «Експорт» | команда (round-trip) | `selected/<model>/{npy,png,yolo,overlay}` + `polygons/` + `groups/` + `selections.json` + `overlays/` (+опц. `selected/<model>/{semantic,mask_groups}` при `?masks=1`) | **sparse** (1..7000+50000+) |
| **Flask finalize** `/api/workspace/finalize/<stem>` | UI «Finalize» (1 фото) | команда | те саме `selected/<model>/…` + `cleanup.json`(per-stem) | **sparse** |
| **CLI `--pack`** `bake_all.py --data-dir … --pack [--group-masks]` | термінал (Day 11 / replace customer) | **замовник** | плоский `masks_npy/` + `masks/` + **`semantic/` (ЗАВЖДИ)** + **`mask_groups/` (лише `--group-masks`)** + `groups/` + `yolo/` + `overlays/` + `polygons/` + `labels.json` + `selections.json` + `README.md` | **dense 1..N** |

> **Ключ:** Flask-шляхи лишають **sparse** working id (reserved 50000+) — придатно для re-import (групи не
> дрейфують). `--pack` робить **dense 1..N** (compaction) — стандарт instance-масок для замовника.
> INTERNAL_ARCHITECTURE §6 плутає finalize з compaction (→ **F-003**); finalize НЕ компактить.

## 2. Compaction (`--pack` only) → `09`§7
`compact_instance_ids(sparse, groups)` per stem: npy + `groups.instance_ids` переномеровуються
**разом** (той самий LUT) → dense 1..N; png-mirror (16-bit) з того ж dense; `cleanup.rejected` НЕ
чіпається (raw space). yolo/overlay/polygons — **id-agnostic, копіюються як є**. Fallback на sparse-copy
якщо compaction впала. (`bake_all.py:233-267`.)

## 3. Derived masks (`baking.export_derived_masks`) — опційні
- `semantic/<stem>.png` — 8-bit per-pixel клас (LUT `iid→class_id+1`; nucleus=1, vesicle=2). Пропуск якщо `len(yolo)≠len(instances)`.
- `mask_groups/<stem>.png` — 16-bit per-pixel group_id (LUT `iid→group_idx`; + polygon fill лише на bg `gmask==0`).
- `overlays/<stem>__<label>.png` — per-label overlay (`export_per_label_overlays`).
- **Коли:** Flask `export?masks=1` → у `selected/<model>/{semantic,mask_groups}/` (sparse). **CLI `--pack` (F-008 fix):** `semantic/` ЗАВЖДИ + `mask_groups/` за `--group-masks` (реюз `export_derived_masks`; semantic=per-class і mask_groups=per-group-order → id-value-незалежні → коректні для dense, хоч рендеряться з sparse `selected/`).

## 4. Що отримує замовник + clusterization (ВЕРИФІКОВАНО з `_inbox/clusterization.py`)
Реальний консюмер `clusterization.py` (wavefront-привʼязка везикул до ядер) читає на фото:
- `images/<stem>.jpg`; `npy/<stem>.npy` (**instance**); `semantic/<stem>.png` (1=nucleus, 2=vesicle);
  `groups/<stem>.json` (ground-truth: `class_id=="cls_001"`, ядро+везикули через `instance_ids`;
  fallback на `polygons/` для polygon-only ядер — `clusterization_patch.md`).

**НЕ читає `mask_groups`!** Ground truth береться з `groups.json`, не з pixel-маски груп. → Рішення
юзера (instance+semantic ЗАВЖДИ; mask_groups лише за `--group-masks`) **точно відповідає консюмеру**.

> ⚠ **F-009 (layout):** clusterization очікує `npy/`, а `--pack` пише instance у **`masks_npy/`**
> (semantic/groups/polygons/images — збігаються). Треба перейменувати `masks_npy→npy` АБО узгодити
> імена з замовником. Рішення — за юзером.

> ℹ Патч `clusterization_patch.md` (fallback на polygon-ядра) адресує **Bug 3**, який MP закрив
> (v1.13.0 Layer 2 sync, → `09`§4): на сучасних `--pack` polygon-ядра вже у dense `instance_ids` →
> «втрати ядер» (12.4% у патчі) не має бути. Перевірити `_tmp/verify_bug3_clusterization.py`.

> ✅ **F-008 ВИРІШЕНО (2026-06-01, рішення юзера):** замовнику йдуть instance (dense npy/png) +
> **semantic ЗАВЖДИ**; **mask_groups — за вибором** (`--group-masks`). Реалізовано у `bake_all.py`
> через `export_derived_masks`. Verified read-only на `data/vesicles_good`: без прапора 16 semantic /
> 0 mask_groups; з `--group-masks` 16/16. Тобто за замовчуванням замовник отримує instance+semantic;
> маски груп додаються свідомо прапором.

## 5. Інваріант-верифікатори (перед send)
`apps/mask_picker/tools/audit_export.py` (9 інваріантів export), `_tmp/desync_invariants.py`,
`_tmp/verify_bug3_clusterization.py` (емулює замовницький clusterization без patch). → `09`§9.

## 6. Lifecycle + посилання
- Save all → bake усіх dirty (sparse) → Flask export/finalize дає sparse team-пакет.
- Замовницький фінал: `bake_all.py --data-dir data/vesicles_good --pack` (⚠ лише за явним «так» + manual verify, дані святі).
- compaction/derived → [`09`](09_BAKING_AND_RESERVED_IDS.md); ID-простори dense vs sparse → [`01`](01_DATA_MODEL_AND_ID_SPACES.md)§5; сегментація-вхід → [`13`](13_SEGMENTATION_BOUNDARY.md).
