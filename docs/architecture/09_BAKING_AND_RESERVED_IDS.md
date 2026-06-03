# 09. Bake pipeline + reserved-ID

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Джерело: `baking.py` (повністю), `data_sync.py` (повністю), `tools/launchers/bake_all.py`.
> Консолідує reserved-ID/Layer-2/compaction, на які посилаються `01`/`05`/`07`/`12`.

## Призначення
Bake матеріалізує підсумок 3 табів (raw маски **−** rejected **+** polygons) у фінальні
файли `selected/<model>/{npy,png,yolo,overlay}` для двох споживачів: замовника (deliverable)
і derived masks. Reserved-ID + Layer 2 sync — серце узгодженості груп після bake (історично
Bug 3/4/7/14). Bake — **серверний, лише на Save all/Finalize/rebake** (lazy-bake, не в інтерактиві).

---

## 1. Pipeline (`_bake_polygons_to_selected`, baking.py:356)
```
1. _load_labels(src_npy)                          # raw_iid простір (output/<model>/npy)
2. cleaned = raw.copy(); cleaned[isin(rejected)]=0 # rejected → фон (baking.py:395)
3. _bake_polygons_into_labels(cleaned, shapes,…)   # полігони → reserved id (нижче §2)
   ├ overlap IoU>0.60 між полігонами → skip пізнішого (:306)
   ├ poly_id = BASE+idx (reserved) | next_id (fallback)  (:321/323)
   └ backstop: raw instance, від якого лишився <15% → стерти повністю (:338)
4. (опц.) _make_backup + _rotate_backups            # selected/_backups/<stem>/<ts>
5. export_segmentation_bundle(baked_labels,…)       # cellsegkit: npy/png/yolo(+overlay)
6. _RGB_CACHE.pop((model,stem))                     # інвалідувати raw RGB-кеш
7. _sync_groups_instance_ids_after_bake(…)          # Layer 2 (нижче §4)
```
Повертає dict (`ok/baked_count/skipped_reasons/overlap_warnings/groups_sync_added/…`).
`export_segmentation_bundle`/`draw_overlay` — з `cellsegkit` (Cryobiology III) — це **6 із 7с**
bake-часу (deliverable render), тому прибрані з інтерактиву (Solution B; → `13`).

---

## 2. Reserved-ID (v1.16.0)
`POLYGON_ID_BASE=50000`, `POLYGON_ID_CEILING=65000` (baking.py:111). У `_bake_polygons_into_labels`:
```python
use_reserved = raw_max < POLYGON_ID_BASE and len(shapes) <= (CEILING - BASE)   # :238
poly_id = POLYGON_ID_BASE + idx  if use_reserved else next_id(=max+1)          # :321/323
```
- **Чому:** старий `next_id=max(cleaned)+1` міг збігтися з raw_iid, який юзер щойно reject-нув
  (next_id collision) → «прибрана» клітина і «намальований» полігон ділили номер → плутанина
  по всіх інваріантах (Bug 3/4/7/14, I5). Reserved діапазон ніколи не перетинається з raw.
- **Наслідок:** уцілілі raw зберігають id (`baked==raw`); полігон #k → `50000+k`, детерміновано
  й стабільно між bake → групи не дрейфують. Це фундамент Solution B (→ `01`§3.2, `07`).
- **3 копії константи + guard** → `01`§4 / F-005 (JS без автотесту).
- `shape_idx_to_iid` — мапа `polygon_index → baked_iid` (повертається з bake; вхід для Layer 2).

---

## 3. Layer 1 vs Layer 2 — РОЗМЕЖУВАННЯ (ключ до дедуп-фіксу)
Полігон у групі має ДВА представлення: `polygon_indices` (SoT) і його baked reserved-iid у
`instance_ids` (для deliverable npy). Щоб не рахувати двічі / не лишати «привид»:
- **Layer 1 (підрахунок/UI):** `_count_labels_in_group`/`_iids_by_label_in_group` (groups.py) +
  front `groupMemberCount` ЗАВЖДИ **skip `iid≥BASE`**; полігони рахуються лише через
  `polygon_indices`. (→ `07`)
- **Layer 2 (deliverable npy):** reserved-iid **лишається** в `instance_ids` поки полігон у групі —
  інакше пікселі полігона не дістануть group_id у `mask_groups.png`.

---

## 4. Layer 2 sync (`_sync_groups_instance_ids_after_bake`, baking.py:487)
Authoritative resolve — polygon-shape у `polygon_indices` є джерелом істини для свого iid:
```
1. iid_owner_gid = { resolved_iid → gid }  з polygon_indices×shape_idx_to_iid   (:552)
2. poly_iids_this_bake = set(shape_idx_to_iid.values())                          (:580)
3. для кожної групи:
   2a. зняти iid, чий owner — ІНША група (legacy collision)                      (:603)
   2a'. зняти iid, чий owner None АЛЕ iid ∈ poly_iids_this_bake → STALE          (:605)
        (полігон прибрано з групи; інакше «привид» — db_img_0171 g_008)
   2b. додати iid, чий owner — ЦЯ група                                          (:617)
```
- **Тонкий сигнал stale:** членство у `shape_idx_to_iid.values()` ∧ `owner is None` — **НЕ**
  `iid≥BASE`. Бо fallback next_id теж дає великі iid, але старий polygon-iid при re-bake має
  лишитись (idempotence union). Manual model-iid (lasso/click) не polygon-resolved → не чіпається.
- **Idempotence:** повторний bake з тим же mapping → той самий owner-граф → конвергує.

---

## 5. backstop — поріг 50% (baking.py:338, вирівняно з UI covered у audit-fix F-011)
Фіксуємо `_orig_counts` кожного raw instance ДО полігонів; після — instance, від якого лишилось
`0 < new_count < 0.50*orig` (полігони перекрили **>50%**), стираємо повністю (`new_labels[isin(phantoms)]=0`).
Підстраховка: полігон домалювали поверх instance, але не reject-нули → інакше у масці лишається огризок.
> ✅ **Вирівняно (F-011, 2026-06-01):** поріг 0.15→**0.50** = той самий 0.5, що UI `_polyCoveredInstances`.
> Тепер **bake = те, що бачив анотатор**: covered>0.5 у редакторі ⟺ стерто у npy. Раніше інстанси
> 50–85% перекриті виживали як «огризок» (виміряно **75** на vesicles_good). Інстанси з **≤50%**
> перекриттям (легкий зачіп сусіда великим полігоном) лишаються — захист сусіда збережено.
> Регресія: `test_bake_backstop_erases_over_half_covered`.

---

## 6. bake_with_resync (data_sync.py:107) — drop-in wrapper
```
1. result = _bake_polygons_to_selected(…)           # bake + Layer 2 всередині
2. if not errors: _strip_orphans_in_groups_file(…)   # B3 self-heal НА ДИСКУ (:170)
   → known_iids = unique(NEW filtered npy); strip iid поза ними; atomic write
   → guard: якщо known_iids порожній (npy empty) — НЕ чіпати (захист, :403)
```
Це закриває B3 для **batch-bake без UI** (in-memory strip є лише в GET /api/groups, який
bake не кличе). Усі 5 точок виклику йдуть через цей wrapper (не напряму baking) — див. §8.

---

## 7. Compaction (deliverable-only) + derived masks
- **`compact_instance_ids`** (data_sync.py:285): sparse (1..7000+50000+) → dense **1..N** (LUT;
  npy + `groups.instance_ids` тим самим remap; `cleanup.rejected` НЕ чіпає). **Кличеться ЛИШЕ
  `bake_all.py:242` (`--pack`)** — НЕ Flask finalize (→ F-003, `01`§5, `12`).
- **`export_derived_masks`** (baking.py:717): `semantic/<stem>.png` (8-bit class LUT iid→cid+1) +
  `mask_groups/<stem>.png` (16-bit group_id LUT; polygon fill лише на bg `gmask==0`). Опційно при ZIP
  export (→ `12`).

---

## 8. Точки виклику (усі через `bake_with_resync`)
| Шлях | Ендпоінт | backup | rejected з |
|---|---|---|---|
| Save Polygons + bake | POST `/api/polygons-export` | так | selections.json |
| Rebake після Pick | POST `/api/rebake` | — | state cleanup |
| 🔥 cleanup full re-bake | POST `/api/cleanup-export` | так | request body |
| Save all (batch, thread) | POST `/api/workspace/bake-all` | — | selections.json (`_collect_bake_job`) |
| Finalize (per-photo ZIP) | GET `/api/workspace/finalize/<stem>` | — | selections.json |
| **`--pack` (deliverable)** | CLI `bake_all.py` | — | + **compaction** |

`reseat_rejected_after_bake` (data_sync) — pure-функція safety-net для **regenerate raw** сценарію
(перенос rejected old→new npy через overlap). Зараз **не активний** (raw стабільний).

---

## 9. Інваріанти + посилання
- **I4** (iid не у 2 групах) — Layer 2 authoritative resolve гарантує для polygon-resolved iid.
- **I5** (rejected ∩ instance_ids=∅) — lazy; bake `cleaned[isin(rejected)]=0` → orphan → strip.
- **B3** (instance_ids ⊆ unique(baked npy)) — `_strip_orphans_in_groups_file` (disk) + GET (memory).
- Reserved-ID/ID-простори → [`01`](01_DATA_MODEL_AND_ID_SPACES.md); Layer 1 count → [`07`](07_GROUPS_TOOL.md); deliverable ZIP/derived → [`12`](12_DELIVERABLE_EXPORT.md); cellsegkit межа → [`13`](13_SEGMENTATION_BOUNDARY.md).
