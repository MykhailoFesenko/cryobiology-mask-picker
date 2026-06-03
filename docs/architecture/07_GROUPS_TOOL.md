# 07. Groups tool

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Колірна логіка — окремо [`08`](08_GROUP_COLORS.md). Джерело: `editor/groups.js` (повністю,
> 1054 р.), `modules/groups.js`, `groups.py`, `routes/api_groups.py`.

## Призначення
Groups — 3-й таб: обʼєднати інстанси+полігони у «клітини» (1 nucleus + N vesicles), класифікувати
за `group_classes.json`. SoT — `gr.list` (браузер) → `groups/<stem>.json`. Тут сходяться обидва
ID-простори: `instance_ids` (baked_iid) + `polygon_indices` (індекси shapes). Workflow ітеративний —
Groups часто виявляє прогалини Cleanup/Polygons → юзер вертається назад.

---

## 1. Інструменти (`gr.tool`)
- **`"edit"`**: клік = toggle instance/polygon у активну групу; drag (>`EDIT_DRAG_THRESHOLD_PX=5`) = lasso.
- **`"picker"`**: клік по iid/polygon → знайти його групу → зробити активною → auto-switch у edit.
Зміна tool скидає in-flight lasso/press.

## 2. Hit-test (`_groupsHitTest`, groups.js:212)
```
1. cu.labelsInt32[y*W+x] → iid (raw_iid)
2. iid>0 ∧ НЕ rejected ∧ НЕ covered → {kind:"instance", id:iid}
   (rejected/covered трактуються як ФОН → fallthrough на polygon — клік по covered-зоні
    влучає у полігон, не повертає «не можна»)
3. інакше: polygon body (_pointInPoly по shapes, reverse) → {kind:"polygon", id:i}
```

## 3. Toggle + single-membership (frontend optimistic)
`_groupsToggleInstance`/`_groupsTogglePolygon`: push undo → якщо вже в active — видалити; інакше
**видалити з УСІХ інших груп** (optimistic single-membership) + додати в active. Backend
`_enforce_single_membership` дублює (last-wins) при POST. Блок: rejected/covered instance не додається
(toast). → реєстр консюмерів covered (`05`§5).

## 4. Lasso — Solution B (client-side, БЕЗ серверного bake)
`_groupsApplyLasso` (groups.js:461):
1. **`_groupsLassoHitTestLocal(path)`** (groups.js:422) — скан `cu.labelsInt32` у bbox lasso, ratio
   `pixels_під_lasso / pixelCounts[iid] ≥ LASSO_MIN_OVERLAP(0.30)`. **Реюз `cu.pixelCounts`** (той самий
   кеш що covered). Семантика 1:1 з колишнім backend hit-test.
2. Фільтр rejected ∪ covered (`_polyCoveredInstances`).
3. Полігони — centroid-in-lasso (`polygonHits`).
4. **No-op guard:** якщо всі вже в active → no undo-запис (інакше фантомний Ctrl+Z).
5. push undo → додати з single-membership → autosave.

> **Чому коректно (фундамент):** reserved-ID (v1.16.0) → `raw_iid==baked_iid` для уцілілих → id,
> пораховані з робочих даних, = id фінального bake. Жодного 7с-фризу / серверного раунд-трипу
> (раніше POST `/api/groups/<stem>/lasso-hit-test` читав `selected/npy`, потребуючи full-bake). → `09`, `13`.

## 5. Класифікація (з backend, front не рахує сам)
GET `/api/groups` повертає `classifications[]` (вирівняно з `list`): `counts`, `valid`, `reason`,
`suggested_class_id`, `iids_by_label`, `orphan_iids`, `rogue_iids`. Front рахує лише `groupMemberCount`
(model-iids `<BASE` + `polygon_indices.length`, → `01`§4/`09`§3). Chip показує counts/badge ✓✗/tooltip
(iids by label + orphan). **🧹 rogue cleanup** (`_groupsRemoveRogue`) — видалити iid-порушники одним кліком.

## 6. Рендер (3 шари)
| Шар | Що | Метод |
|---|---|---|
| `#groupsChips` (DOM) | список груп: колір/назва/count/badge/🧹 | `_groupsRenderChips` |
| `#groupsOverlayLayer` (SVG) | полігони груп (per `polygon_indices`) + peek-polygons (cyan) + lasso preview | `_groupsRedrawOverlay` |
| `#groupsMaskCanvas` (canvas) | pixel-tint інстансів: active(α220)/other(α170)/**rogue(червоний α240)**; peek-ungrouped(cyan) | `_groupsRedrawMaskCanvas` |
| `#groupsBoundaryLayer` (SVG) | контур: active=**білий**, rogue=**червоний** (bbox-обмежений scan) | `_groupsDrawBoundaryStroke` |

Колір — `effectiveHSL(group, classes, classIndexMap)` (→ `08`). Усі шари чистяться `_groupsClearVisualLayers`
(index.js) на виході з Groups/close — інакше біла обводка лишалась (Bug D/J).

### peek «Необведені» (hold-I / hold-кнопка, `_setPeek`)
Підсвічує cyan інстанси/полігони поза будь-якою групою. **Виключає covered** (Bug M: covered «обведені»
за визначенням) + rejected.

## 7. Autosave + race-guard (gen) — детально у `11`
`_groupsScheduleAutosave` бампає `g.gen` + debounce `AUTOSAVE_DEBOUNCE_MS=800`. `_groupsSave` (force)
через `_enqueueSave`; реасайнить `g.list = resp.groups` (echo) **лише якщо `g.gen` не змінився** під час
in-flight POST (інакше undo/свіжа дія клобнулась би). → [`11`](11_AUTOSAVE_DIRTY_SYNC.md)§gen-guard, [`10`](10_UNDO_HISTORY.md)§7.

## 8. stale_removed self-heal (на load)
GET може повернути `stale_removed` (backend strip-orphan B3) → front mark dirty + toast + autosave
(закріпити). → `09`§6, `11`.

## 9. Front↔back контракт
| Дія | Ендпоінт | Ефект |
|---|---|---|
| open | GET `/api/groups/<stem>` | envelope + classifications + classes + stale_removed |
| save | POST `/api/groups/<stem>` | single-membership + classify + strip-orphan(memory) + mirror group_id; повертає moves |
| ~~lasso~~ | ~~POST `.../lasso-hit-test`~~ | **legacy** — фронт більше не кличе (Solution B); бекенд лишено для тестів |

## 10. Lifecycle + посилання
- new/delete/toggle/lasso/class/rogue → push undo + dirty + autosave.
- polygon-shape delete у Polygons ремапить `polygon_indices` (compound undo) → [`06`](06_POLYGONS_TOOL.md)§6.
- SoT/дедуп reserved → [`01`](01_DATA_MODEL_AND_ID_SPACES.md), Layer 1/2 → [`09`](09_BAKING_AND_RESERVED_IDS.md). Кольори → [`08`](08_GROUP_COLORS.md).

> ✅ **F-007 fixed (2026-06-01):** header-docstring `editor/groups.js` оновлено під Solution B
> (client-side `_groupsLassoHitTestLocal`; lasso-hit-test = LEGACY). node --check OK.
