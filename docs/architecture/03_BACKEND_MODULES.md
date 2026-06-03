# 03. Backend-модулі + граф імпортів

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Foundation-трійця разом з [`01`](01_DATA_MODEL_AND_ID_SPACES.md)/[`02`](02_FRONTEND_STATE.md).
> Джерело: усі `apps/mask_picker/*.py` + `routes/`.

## Призначення
Backend — Flask-додаток із чіткою layered-структурою (anti-circular). Цей документ:
відповідальність кожного модуля, граф імпортів, layering rules + трюки уникнення циклів,
дубльована front↔back логіка (аудит), модель конкурентності.

---

## 1. Composition root (`app.py`)
```
create_app(cfg, state):           # app.py:146
   app = Flask(static, templates)
   catalog = CatalogService(cfg, state)
   routes.register_all(app, cfg, state, catalog)   # реєструє 9 blueprint-ів
```
- **Blueprint factory pattern:** кожен `routes/api_*.py` має `make_blueprint(cfg, state,
  catalog)` → Blueprint; маршрути захоплюють cfg/state/catalog через **closure** (не global).
  `register_all` (`routes/__init__.py:30`) реєструє 9: misc, state, catalog, labels, cleanup,
  polygons, groups, group_classes, workspace.
- **Re-export shim:** `app.py:37-138` ре-експортує ~усі символи з кожного модуля — **тести
  імпортують `from app import …`** (напр. `_validate_groups_payload`). Зміна сигнатури в
  модулі → звірити, що re-export і тести не зламані.
- **CLI / workspace mode:** `main()` — `--workspace <dir>` ставить ВСІ `cfg.*` шляхи під ws +
  `_discover_models`; інакше per-path override. `StateStore` = `selected_dir.parent/selections.json`.
- **Runtime:** `app.run(debug=False, use_reloader=False, threaded=True)` → **(env-грабля)** немає
  авто-reload: зміни в `.py`/`templates/*.html` видно лише після РЕСТАРТУ сервера. werkzeug
  access-log глушиться (інакше Flask тихо вмирав на Windows).

---

## 2. Модулі (відповідальність + ключовий API)

| Модуль | LOC | Відповідальність | Ключове |
|---|---|---|---|
| `state.py` | 480 | foundation: Config/ModelSource/StateStore, atomic write, image/labels helpers, feature-flags | `_atomic_write_json`, `StateStore` (+lock), `APP_VERSION`, `load_config`, `_discover_models` |
| `group_classes.py` | 222 | user-defined класи груп (1/workspace) + constraints | `_read_classes`, `_validate_class_against_counts`, `_suggest_class_for_counts` |
| `cleanup.py` | 221 | rejected per-model, RGB-encode raw, backup rotation, RGB cache | `_labels_to_rgb_png_bytes`, `_write_cleanup_json`, `_RGB_CACHE` |
| `polygons.py` | 229 | LabelMe envelope read/write/validate, label rename | `_load_labels`, `_write_polygons_json`, `_validate_polygons_payload` |
| `groups.py` | 795 | групи: single-membership, класифікація, strip-orphan, label lookup | `_enforce_single_membership`, `_classify_group_membership`, `_count_labels_in_group`, `POLYGON_ID_BASE` (копія) |
| `catalog.py` | 187 | image-grid + clean-overlay тайли (30s TTL cache) | `CatalogService`, `_get_clean_overlay_bytes` |
| `baking.py` | 749 | bake raw→filtered, reserved-ID, Layer 2 sync, derived masks | `_bake_polygons_into_labels`, `_bake_polygons_to_selected`, `POLYGON_ID_BASE` (канон) |
| `data_sync.py` | 357 | диригент cross-cutting: bake_with_resync, strip-orphan(disk), compaction | `bake_with_resync`, `_strip_orphans_in_groups_file`, `compact_instance_ids` |
| `workspace.py` | 57 | switch_workspace (runtime), ZIP import маркери | `switch_workspace`, `WORKSPACE_MARKER_DIRS` |
| `app.py` | 250 | Flask wiring + CLI + re-exports | `create_app`, `main` |
| `routes/*.py` | ~2.5k | тонкий HTTP-шар (9 blueprint-ів) | `make_blueprint` × 9 (→ `04`) |

---

## 3. Граф імпортів + layering

```
L0 (leaf):    state.py            group_classes.py
                 │                      │
L1:    cleanup  polygons  catalog   groups(+group_classes)
          │  \    │          │         │
L2:           baking (state+cleanup+polygons)
                 │                          │
L3:        data_sync (state+baking+groups)  workspace (state+catalog)
                            │                  │
L4:                  routes/api_*  (state + domain-модулі + data_sync + catalog)
                            │
L5:                       app.py  (re-export усіх + routes)
```

**Layering rules (звірено з кодом):**
- `state`, `group_classes` нікого з MP не імпортують (чисті leaf).
- `cleanup`/`polygons`/`catalog` ← лише `state`. `groups` ← `state` + `group_classes`.
- `baking` ← state+cleanup+polygons. **НЕ імпортує groups** — читає `groups.json` напряму через `json` у `_sync_groups_instance_ids_after_bake` (уникнення циклу groups↔baking).
- `data_sync` ← state+baking+groups (диригент). `workspace` ← state+catalog.
- `routes/*` ← усе потрібне; **cross-cutting bake завжди через `data_sync.bake_with_resync`**, не напряму `baking`.

### Трюки уникнення циклів (аудит-важливо)
1. **`groups.POLYGON_ID_BASE` дублює `baking.POLYGON_ID_BASE`** — щоб легкий groups не тягнув важкий baking (PIL/cv2/cellsegkit). Guard-тест лише Python-копій (→ F-005, `01` §4).
2. **`baking` читає groups.json через path**, не `import groups`.
3. **`polygons` lazy-import `from cleanup import _rotate_backups`** усередині `_rename_labels_in_polygon_files` (щоб модулі лишались незалежними на рівні top-level).
4. **`data_sync` — єдина точка**, де baking і groups зустрічаються (`_bake_polygons_to_selected` + `_strip_orphan_instance_ids`).

---

## 4. Дубльована front↔back логіка (звіряти ОБИДВІ сторони — `CODE_AUDIT_PRINCIPLES §2.5`)

| Концепт | Backend | Frontend | Ризик розбіжності |
|---|---|---|---|
| `POLYGON_ID_BASE` | baking.py:111 / groups.py:112 | groups.js:176 | F-005 (JS без guard) |
| Підрахунок членів групи | `_count_labels_in_group` (skip ≥BASE) | `groupMemberCount` (skip <BASE filter) | звірено — паритет (Layer 1) |
| Hide rejected у фоні | catalog `_render_clean_overlay_bytes` (**bbox+pad**, лише explicit rejected з selections.json) | `_ensureRejectedPatch` (**shape-accurate**, rejected∪covered) | **різна точність** — catalog-тайл грубіший і НЕ ховає covered. Прийнятно (thumbnail), але це окремий консюмер «rejected» (→ `05`/`14`) |
| Класифікація групи | `_classify_group_membership` (counts/valid/rogue) | НЕ рахує — бере backend resp; лише `groupMemberCount` локально | звірено |
| Single-membership | `_enforce_single_membership` (back-only) | покладається на POST echo | back — єдине джерело |

---

## 5. Модель конкурентності (Flask `threaded=True`)
- **StateStore** — один global `threading.Lock`; усі мутації selections.json серіалізовані → fixed `.json.tmp` безпечний.
- **Autosave-файли** (cleanup/polygons/groups) — `_atomic_write_json` з **unique** tmp (`mkstemp`): паралельні POST того самого stem не клобають спільний tmp.
- **Кеші** (модульні): `_RGB_CACHE` (cleanup, lock, FIFO 32), `_CLEAN_OVERLAY_CACHE` (catalog, mtime-keyed), `CatalogService` (30s TTL + invalidate на exclude/import/switch).
- **bake_all** — окремий thread (background); прогрес через polling-ендпоінт (→ `11`/`12`).

---

## Звʼязки / посилання
- Кожен ендпоінт детально → [`04`](04_API_CONTRACT.md).
- bake/data_sync деталі → [`09`](09_BAKING_AND_RESERVED_IDS.md), [`11`](11_AUTOSAVE_DIRTY_SYNC.md).
- Дубльована логіка/реєстр консюмерів → [`14`](14_CROSS_CUTTING_MAP.md).
