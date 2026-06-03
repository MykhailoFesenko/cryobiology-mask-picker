# 14. Крос-каттинг карта + реєстр концептів (аудит-капстоун)

> **Статус: ✅ заповнено** (2026-06-01, Phase 2 — системний прохід §7 плану). Індекс: [README.md](README.md).
> Метод: [`../CODE_AUDIT_PRINCIPLES.md`](../CODE_AUDIT_PRINCIPLES.md) §3. Це синтез усіх 01–13 +
> вердикт консистентності кожного multi-representation концепту (grep по front+back).

## Призначення
Кодбаза — композит без централізації концептів: кожен консюмер реалізує свою копію логіки
(«чи rejected?», «скільки в групі?», «куди undo?»). Корінь усіх повторних багів — концепт із
кількома представленнями, де консюмери оновлюються НЕ всі разом. Ця карта — **єдиний список SoT +
усіх консюмерів кожного концепту**, щоб правка перевірялась по ВСІХ, а не реактивно.

---

## 1. Граф tool↔tool (хто кого читає/пише)
```
                 cu.labelsInt32 (raw_iid, з /api/labels-rgb)
                        │ читають ВСІ три таби (hit-test/lasso/covered)
        ┌───────────────┼────────────────────────┐
        ▼               ▼                         ▼
   CLEANUP          POLYGONS                   GROUPS
   rejectedSet ───► covered(derived) ◄──────── читає covered+rejected (hide/block/peek)
   (selections)     pg.shapes                  gr.list
        │               │ polygon_index ───────► group.polygon_indices (SoT)
        │               │ delete→remap groups (compound undo)
        ▼               ▼                         ▼
   ────────────────── BAKE (data_sync.bake_with_resync) ──────────────────
   rejected→0 · polygons→reserved 50000+ · Layer2 sync · strip-orphan
        ▼
   selected/<model>/npy (baked_iid) ──► group.instance_ids · derived masks · ZIP
```
**Крос-таб мутації:** (1) Pick (polygons) → covered (cleanup render/block + groups block); (2) polygon
delete (polygons) → `group.polygon_indices` remap (groups) = ОДИН undo-запис; (3) bake → `group.instance_ids`
(Layer2) + strip-orphan.

---

## 2. Реєстр концептів — SoT + УСІ консюмери + вердикт

### К1. Rejected інстанс  → деталі `05`
- **SoT:** `cu.rejectedSet` / `selections.json[stem].cleanup.rejected_instances` (raw_iid). **Derived:** `covered` (геометрія >50%).
- **Консюмери:** `_cleanupRedraw`(red), `zoompan._overlayHideSet`/`_drawBase`/`_ensureRejectedPatch`(hide), `_cleanupToggleInstance`(block), `_groupsHitTest`(fallback), groups lasso filter, `_groupsRedrawMaskCanvas`(peek skip), bake `cleaned[isin(rejected)]=0`, **catalog `_render_clean_overlay_bytes`**(bbox, тільки explicit).
- **Вердикт:** ✅ UI-консюмери covered узгоджені (v1.16.2). catalog-тайл — окремий консюмер нижчої точності (bbox, не ховає covered) — навмисно. **F-004:** live-SoT = selections.json, не cleanup.json.

### К2. Полігон у групі  → деталі `07`/`09`
- **SoT:** `group.polygon_indices`. **Derived:** reserved-iid `≥50000` у `instance_ids` (bake-артефакт).
- **Консюмери:** back `_count_labels_in_group`/`_iids_by_label_in_group`(skip ≥BASE), Layer2 `_sync_groups_instance_ids_after_bake`(stale-strip), `export_derived_masks`(mask_groups polygon fill); front `groupMemberCount`(skip <BASE), `_groupsTogglePolygon`, `_polyRemapGroupsAfterShapeDelete`, overlay render.
- **Вердикт:** ✅ Layer 1 (count skip ≥BASE) узгоджений у 3 місцях; Layer 2 stale-strip коректний (сигнал = `in shape_idx_to_iid.values()` ∧ owner None). Double-count/привид закриті.

### К3. ID-простори  → деталі `01`/`09`/`13`
- raw_iid (output, RO) / baked_iid (selected, reserved 50000+) / polygon_index (shapes[]).
- **Консюмери:** UI hit-test=raw (`cu.labelsInt32`); `group.instance_ids`=baked; bake конвертує; compaction→dense (deliverable).
- **Вердикт:** ✅ reserved-ID робить `raw==baked` для уцілілих (фундамент Solution B). **F-005:** JS-копія BASE без guard-тесту. **F-003:** compaction лише `--pack`.

### К4. Undo-дія  → деталі `10`
- **SoT:** `editor.state.history` (єдиний стек). Раніше 3 per-tab — джерело розсинхрону.
- **Консюмери:** keys.js (Ctrl+Z/Y), events.js (**6 кнопок**), кожна мутація (`_historyPush` ПЕРЕД), `_historyAttachSnap` (compound), restore×3 домени.
- **Вердикт:** ✅ один стек; 6 кнопок прив'язані; drag push-on-start; compound = 1 запис; clear on open/close.

### К5. Стан групи (mirror)  → деталі `07`
- **SoT:** `groups/<stem>.json`. **Mirror:** `polygons.json.shapes[i].group_id` (для зовн. LabelMe).
- **Консюмери:** POST groups пише обидва (`_sync_polygons_group_id_mirror`); MP mirror **не парсить**.
- **Вердикт:** ✅ mirror лише пишеться, не читається — розсинхрон неможливий за дизайном.

### К6. Dirty / autosave  → деталі `11`
- **SoT:** per-domain dirty (front) + `selections.json.dirty` (back) + `gr.gen` (race-token).
- **Консюмери:** flush на switch/close/nav; `_enqueueSave` (черга); `_groupsSave` gen-guard.
- **Вердикт:** ✅ gen-guard лише в groups (єдиний, хто реасайнить echo); cleanup/poly не реасайнять → race нема.

### К7. Класифікація групи  → деталі `07`/`09`
- **SoT:** обчислюється backend з counts (`_classify_group_membership`). Front НЕ рахує (крім `groupMemberCount`).
- **Консюмери:** GET повертає counts/valid/rogue/orphan/iids_by_label; chip показує; `groupMemberCount` (локальний, звірений з counts).
- **Вердикт:** ✅ front бере backend; `groupMemberCount` дає той самий результат (skip ≥BASE + polygon_indices).

### К8. Base render (overlay vs hide)  → деталі `02`/`05`
- **SoT:** `bgSource` (per-tab) + `_overlayHideSet()`. **Кеші:** `_rejectedPatch`(sig stem|WxH|count|sum), `_coveredBaseSig`(size|sum).
- **Консюмери:** `_drawBase`, `_paintRejectedFromOriginal`, `_cleanupRedraw`(marks), `_drawBaseIfCoveredChanged`(live).
- **Вердикт:** ✅ hide-set per-tab (cleanup=covered; working=rejected∪covered); кеш-sig включає hide-set.

---

## 3. Дубльована front↔back логіка (звіряти ОБИДВІ) → `03`§4
| Концепт | Back | Front | Вердикт |
|---|---|---|---|
| `POLYGON_ID_BASE` | baking.py:111 = groups.py:112 (guard-тест) | groups.js:176 (manual) | ⚠ F-005 (JS без guard) |
| member-count | `_count_labels_in_group` | `groupMemberCount` | ✅ паритет |
| hide rejected | catalog bbox (explicit) | `_ensureRejectedPatch` shape (rejected∪covered) | різна точність (навмисно) |
| класифікація | `_classify_group_membership` | — (бере backend) | ✅ |

---

## 4. Вердикт консистентності (підсумок аудиту)
**Розбіжностей трактування концептів у КОДІ не виявлено** — серія v1.16.x (covered у всіх консюмерах,
Layer 1/2 дедуп, єдиний undo, gen-guard) закрила історичні multi-representation баги. Усі знахідки
цього проходу — **doc-drift або deliverable-питання**, не баги поведінки:

| # | Severity | Суть | Статус |
|---|---|---|---|
| F-001 | 📘 | next_id docstrings (до reserved-ID) | ✅ fixed (pytest 236/236) |
| F-002 | 📘 | INTERNAL_ARCHITECTURE v1.15.0 застарів | ✅ mitigated (банер) |
| F-003 | 📘 | compaction лише `--pack` (не Flask finalize) | logged (документовано) |
| F-004 | 📘🟠 | rejected live-SoT=selections.json (не cleanup.json) | ✅ fixed (sync на bake) |
| F-005 | 🟡 | JS `POLYGON_ID_BASE` без guard | logged (питання тесту) |
| F-006 | 🟡 | handoff колірні константи застарілі | logged |
| F-007 | 📘 | groups.js docstring старий lasso-flow | ✅ fixed |
| F-008 | 🟠 | deliverable masks (--pack: semantic always, mask_groups за прапором) | ✅ fixed (verified) |

## 5. Pre-GitHub registry pass (план §5 принципів, Day 11)
Перед упаковкою: grep кожного рядка реєстру §2 → звірити трактування → інваріант-верифікатори
(`audit_export.py`, `_tmp/desync_invariants.py`, `_tmp/verify_bug3_clusterization.py`) → pytest +
node --check → браузер-smoke (cleanup toggle, polygon draw/edit/delete, groups toggle/lasso, undo
крос-домен, covered hide) → guard 3 копій BASE → звірка front/back counts.

## 6. Посилання
Кожен концепт → свій doc (К1→`05`, К2→`07`/`09`, К3→`01`, К4→`10`, К5→`07`, К6→`11`, К7→`07`, К8→`02`/`05`).
Метод правки будь-чого з реєстру — `../CODE_AUDIT_PRINCIPLES.md` §1 (8-крок).
