# 04. API-контракт (межа front↔back)

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Джерело: `routes/*.py` (повний інвентар `@bp.route`), `app.py`. Це **фізична межа**, де
> браузерний стан (`02`) ↔ диск-SoT (`01`). Bake-шляхи → `data_sync` (`09`/`11`/`12`).

## Призначення
Повний інвентар ендпоінтів: метод, шлях, призначення, side-effects, який SoT-файл і браузер-стан
зачіпає. ~40 маршрутів у 9 blueprint-ах (`register_all`, `routes/__init__.py`). Усі — closure-фабрики
`make_blueprint(cfg, state, catalog)`.

---

## 1. Misc (`api_misc.py`)
| Метод·Шлях | Призначення | Side-effects |
|---|---|---|
| GET `/` | index.html (Jinja) | — |
| GET `/api/version` | `{version}` (APP_VERSION) | — (тест `test_api_version`) |
| POST `/api/shutdown` | зупинити сервер | процес |
| GET `/api/config` | конфіг (paths/models/features) | — |
| GET `/static/<path>` | статика | — |

## 2. Catalog + state (`api_catalog.py`, `api_state.py`)
| Метод·Шлях | Призначення | Side-effects / SoT |
|---|---|---|
| GET `/api/catalog` | грід фото (+state) | CatalogService (30s TTL) |
| GET `/api/overlay/<model>/<stem>` | clean-overlay тайл (rejected приховані, **bbox**) | `_get_clean_overlay_bytes` (читає selections.json rejected) |
| GET `/api/image/<stem>` | оригінал фото | — |
| POST `/api/select` | **Pick** моделі для stem → копіює model→selected/ + rebake | selections.json (status/model) + `_copy_model_files_for_stem` + rebake |
| POST `/api/skip` | позначити skipped | selections.json + `skipped/<stem>.txt` |
| POST `/api/exclude/<stem>` | перенести фото у `_excluded/` | move file + status="excluded" + catalog.invalidate |
| POST `/api/restore/<stem>` | повернути з `_excluded/` | move back + unset status |
| POST `/api/bulk-user` | масово виставити user | selections.json (багато stems) |
| POST `/api/unset` | зняти status (unreviewed) | selections.json |
| POST `/api/hard-reset/<stem>` | повний скид stem (cleanup/poly/groups/selected) | видаляє артефакти stem |
| GET `/api/stats` | лічильники прогресу | — |

## 3. Labels + group-classes (`api_labels.py`, `api_group_classes.py`)
| Метод·Шлях | Призначення | SoT |
|---|---|---|
| GET/POST `/api/labels` | класи інстансів (nucleus/vesicle…) | `labels.json` |
| POST `/api/base-label/<stem>` | дефолт-клас для bake уцілілих | selections.json `base_label` |
| POST `/api/labels/rename` | глобальний rename label у всіх polygons.json | `_rename_labels_in_polygon_files` (+backup) |
| GET/POST `/api/group-classes` | класи груп + constraints | `group_classes.json` |

## 4. Cleanup (`api_cleanup.py`) — деталі `05`
| Метод·Шлях | Request | Response | Side-effects |
|---|---|---|---|
| GET `/api/labels-rgb/<model>/<stem>.png` | — | RGB-PNG (id=пікс) | кеш `_RGB_CACHE`; → `cu.labelsInt32` |
| GET `/api/instances/<model>/<stem>` | — | `{instance_count, shape}` | — |
| GET `/api/cleanup/<stem>` | — | `{model, rejected_instances, markers,…}` | ← **selections.json** |
| POST `/api/cleanup/<stem>` | `{model, rejected_instances[], markers?, user}` | `{ok, cleanup, dirty}` | `state.set_cleanup`→selections.json + `mark_dirty`; **НЕ пече** |
| POST `/api/cleanup-export/<stem>` | `{model, rejected_instances[], markers?}` | `{ok, baked, baked_count,…}` | `bake_with_resync` + **пише `cleanup.json`** (🔥, рідко) |

## 5. Polygons (`api_polygons.py`) — деталі `06`
| Метод·Шлях | Request | Response | Side-effects |
|---|---|---|---|
| GET `/api/polygons/<stem>` | — | LabelMe envelope | ← `polygons/<stem>.json` (порожній якщо нема) |
| POST `/api/polygons/<stem>` | LabelMe payload | `{ok, shape_count, dirty}` | backup+write `polygons.json` + `mark_dirty`; **JSON only** |
| POST `/api/polygons-export/<stem>` | payload + `{model, rejected_instances[]}` | `{baked, baked_count, skipped_reasons, overlap_warnings, orphan_iids_stripped,…}` | write JSON + `bake_with_resync` + `clear_dirty` |
| POST `/api/polygons/<stem>/seed-from-mask` | `{model, instance_ids?, label?, simplify_epsilon?, min_area?}` | `{envelope, shape_count}` | **без write** (повертає shapes); викидає rejected |
| POST `/api/polygons/<stem>/multi-seed` | `{mappings:[{label,model}], iou_threshold?,…}` | `{envelope, shapes_added, shapes_skipped_overlap, per_mapping}` | **без write**; vectorized IoU dedup (accepted_map) |
| POST `/api/rebake/<stem>` | `{model?}` (fallback state.model) | `{baked, baked_count, skipped,…}` | `bake_with_resync` (shapes з polygons.json + rejected з state); skip якщо no_data |

## 6. Groups (`api_groups.py`) — деталі `07`
| Метод·Шлях | Request | Response | Side-effects |
|---|---|---|---|
| GET `/api/groups/<stem>` | `?model=` | `{groups, classifications, classes, stale_removed}` | strip-orphan in-memory; класифікація |
| POST `/api/groups/<stem>` | `{model, groups[]}` | `{groups, classifications, moves}` | `_enforce_single_membership` + classify + write `groups.json` + mirror `polygons.json.group_id` |
| POST `/api/groups/<stem>/lasso-hit-test` | `{path, …}` | `{bakedIds}` | **legacy** — фронт не кличе (Solution B); читає `selected/npy` |

## 7. Workspace (`api_workspace.py`) — деталі `11`/`12`
| Метод·Шлях | Призначення | Side-effects |
|---|---|---|
| GET `/api/workspace/info` | поточний workspace | — |
| POST `/api/workspace/pick-folder`·`/pick-dir` | системний діалог вибору папки | — |
| POST `/api/workspace/import` | розпакувати ZIP у workspace (marker dirs) | пише файли + `switch_workspace` |
| GET `/api/workspace/export` | bulk ZIP (`?stems=`, `?masks=1`, `?dest=`) | **selected/<model>/… layout (sparse)** + polygons/groups/selections + overlays + опц. semantic/mask_groups |
| GET `/api/workspace/finalize/<stem>` | per-photo ZIP | `bake_with_resync` (sparse!) + bundle selected/<model>/… + cleanup.json |
| POST `/api/workspace/split` | розбити workspace | файли |
| POST `/api/workspace/bake-all` | фоновий batch-bake усіх dirty | thread; `bake_with_resync` per stem + `clear_dirty` |
| GET `/api/workspace/bake-progress` | прогрес bake-all | ← thread-стан (polling) |
| POST `/api/workspace/import-scan`·`/import-apply` | preview+apply import | `_tmp/` staging + merge |

> **⚠ Deliverable layout — три шляхи (→ `12`, F-003):** Flask `export`/`finalize` дають **sparse**
> `selected/<model>/…`; CLI `bake_all.py --pack` дає **dense 1..N** у плоскому `masks_npy/masks/groups/…`.

---

## 8. Патерни контракту (аудит)
- **Lazy-bake:** усі інтерактивні POST (cleanup/polygons/groups) лише пишуть JSON + `mark_dirty`; bake — окремі ендпоінти (export/rebake/bake-all/finalize) через `bake_with_resync`.
- **rejected source:** GET/POST cleanup + bake-шляхи беруть **selections.json** (live-SoT). `cleanup.json` синхронізується на КОЖЕН bake через `bake_with_resync` (F-004 ✅ fixed) → provenance у ZIP свіже.
- **seed/multi-seed** — pure (повертають shapes, не пишуть) → фронт застосовує з undo.
- **Помилки:** JSON `{error}` + HTTP-код; 503 коли немає optional dep (cellsegkit/cv2/numpy).
- request/response високотрафікових (cleanup/polygons/groups/workspace) — повні вище; admin/state — на рівні side-effect (поглибити за потреби).

## Посилання
Стан браузера ↔ ці ендпоінти → [`02`](02_FRONTEND_STATE.md)§4; bake → [`09`](09_BAKING_AND_RESERVED_IDS.md); autosave/flush → [`11`](11_AUTOSAVE_DIRTY_SYNC.md); deliverable → [`12`](12_DELIVERABLE_EXPORT.md).
