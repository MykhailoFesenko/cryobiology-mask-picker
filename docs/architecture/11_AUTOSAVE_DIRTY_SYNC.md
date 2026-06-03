# 11. Autosave + dirty + sync (анти-race)

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Джерело: `editor/index.js` (`_enqueueSave`/`flushIfDirty`), `cleanup.js`/`polygons.js`/`groups.js`
> (autosave+gen-guard), `state.py` (dirty), `data_sync.py` (strip-orphan).

## Призначення
Усі 3 домени зберігаються незалежно з debounce-autosave; при close/switch/nav — flush. Bake —
окремо (lazy, Day 7). Тут — як уникнути двох класів багів: **паралельні POST** (race на tab-switch/
close) і **clobber свіжого стану** in-flight-відповіддю (undo vs groups-echo).

---

## 1. Dirty-прапори (front ↔ back)
| Прапор | Де | Значення |
|---|---|---|
| `cu.dirty` / `cu.dirtyExport` | браузер | незбережені rejected/markers (export = «треба bake», історично) |
| `pg.dirty` | браузер | незбережені shapes |
| `gr.dirty` | браузер | незбережені групи |
| `gr.gen` | браузер | лічильник поколінь (race-guard, нижче §4) |
| `selections.json[stem].dirty` | диск | stem має збережені зміни, ще НЕ запечені у `selected/` |
| `it.state.dirty` (catalog) | браузер | жовта крапка в гриді (ставиться при кожному autosave) |

## 2. Debounce-таймери (⚠ різні!)
| Домен | Затримка | Метод |
|---|---|---|
| cleanup | **5000 ms** | `_cleanupMarkDirty` → `_cleanupAutosave` |
| polygons | **5000 ms** | `_polyMarkDirty` → `_polyAutosave` |
| groups | **800 ms** (`AUTOSAVE_DEBOUNCE_MS`) | `_groupsScheduleAutosave` → `_groupsSave(true)` |

> Groups зберігаються агресивніше (800ms) бо POST повертає авторитетний стан (single-membership/
> classify) — UI хоче швидкий feedback. cleanup/polygons — суто JSON dump, 5с достатньо.

## 3. `_enqueueSave` — серіалізація (Day 9 Bug 2)
Усі save-операції (3 домени, autosave і явні) проходять через `editor._enqueueSave(fn)`
(index.js:184) — один проміс-ланцюг `_saveChain`. Гарантія: **ніколи 2 паралельних POST** на той
самий stem (фікс гонки при швидкому tab-switch/нав/close під час debounce). Tail ковтає помилку
(`catch(()=>{})`) — ланцюг живе далі.

## 4. `gen`-guard — анти-clobber (ТІЛЬКИ groups)
**Проблема:** `_groupsSave` реасайнить `g.list = resp.groups` (echo backend-стану). In-flight POST,
запущений ДОДАВАННЯМ інстанса, міг резолвитись ПІСЛЯ undo → клобав undo (інстанс повертався).
**Рішення** (groups.js:100): `_groupsScheduleAutosave` бампає `g.gen`; `_groupsSave` запамʼятовує
`gen0` перед мережею і **пропускає реасайн**, якщо `g.gen !== gen0` (локальний стан випередив — новий
autosave вже заплановано). → перетин з undo (`10`§7).
**Чому лише groups:** cleanup/polygons autosave **НЕ реасайнять** локальний стан з відповіді (просто
`dirty=false`) → там клобати нічого. (`CODE_AUDIT_PRINCIPLES §1F/§2.5`.)

## 5. `flushIfDirty` (index.js:485)
Re-entrancy guard (`_flushPromise`): повторний виклик під час активного flush повертає той самий
проміс (не запускає 2-й flush). Порядок: polygons → cleanup (export або autosave) → groups → `close(true)`.
Точки виклику: close (×/backdrop), tab-switch, навігація між фото. Escape-hatch: повторний клік × під
час повільного flush → force `close(true)` (events.js:156).

## 6. Lazy-bake (Day 7) — dirty не пече одразу
POST `/api/cleanup` і `/api/polygons` лише пишуть JSON + `state.mark_dirty(stem)`. Bake відкладено:
- **«Зберегти все»** → POST `/api/workspace/bake-all` (фоновий thread, прогрес-polling) → для кожного
  dirty stem `bake_with_resync` → `state.clear_dirty`.
- **Finalize** → bake одного stem + clear_dirty.
- POST `/api/groups` теж лише JSON (групи не потребують bake — Solution B).

## 7. B3 self-heal (strip-orphan) — два шляхи
- **In-memory** (GET `/api/groups`): backend `_strip_orphan_instance_ids` → `stale_removed` у відповіді
  → front mark dirty + toast + autosave (`_groupsLoad`, groups.js:69). → `07`§8.
- **На диску** (bake): `data_sync._strip_orphans_in_groups_file` (known_iids з NEW baked npy; guard
  проти порожнього npy). Для batch-bake без UI. → `09`§6.

## 8. Atomic write
Autosave-файли (cleanup/polygons/groups) — `state._atomic_write_json` (unique tmp via `mkstemp`):
паралельні POST того самого stem на Flask `threaded=True` не клобають спільний tmp. selections.json —
`StateStore._flush` під global lock (fixed tmp ок). → `01`§6, `03`§5.

## 9. Lifecycle + посилання
- mutate → markDirty (+timer) → debounce POST → dirty=false + catalog dot.
- close/switch/nav → flushIfDirty (serialized) → close.
- Save all/Finalize → bake → clear_dirty (disk).
- Форма стану → [`02`](02_FRONTEND_STATE.md); undo-перетин → [`10`](10_UNDO_HISTORY.md); bake → [`09`](09_BAKING_AND_RESERVED_IDS.md); ендпоінти → [`04`](04_API_CONTRACT.md).
