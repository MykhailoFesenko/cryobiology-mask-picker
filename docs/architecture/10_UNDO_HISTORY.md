# 10. Undo/Redo — глобальний хронологічний стек

> **Статус: ✅ заповнено** (2026-06-01, Phase 2). Індекс: [README.md](README.md).
> Джерело: `editor/history.js` (повністю), `keys.js`, `events.js`, push-сайти в
> `cleanup.js`/`polygons.js`/`groups.js`/`multiseed.js`/`labels.js`. Browser-verified (CHANGELOG [1.16.2]).

## Призначення
Один Ctrl+Z має відкочувати **глобально-останню** дію будь-де (cleanup/polygons/groups),
автоматично перемикаючи таб. Замінив 3 окремі per-tab стеки (джерело розсинхрону). Це
класичний multi-representation-ризик: «undo-дія» мала N представлень (3 стеки) → тепер ОДНЕ
(`editor.state.history`). Концепт-аудит: усі точки входу (клавіші + 6 кнопок) і всі мутації
мусять іти через цей єдиний стек.

---

## 1. Модель (`history.js`)
`editor.state.history = { undo: [], redo: [] }`. Кожен запис:
```js
{ snaps: { <domain>: <before-snapshot>, ... }, primary: <domain> }
```
- Стек строго **LIFO** (хронологічний). Тому коли undo знімає глобально-останню дію, для
  її домену поточний стан == **AFTER** цієї дії (усі пізніші дії того домену вже відкочені).
- `_historyUndo`: знімає top; поточний стан доменів запису → у redo (як AFTER); відновлює
  before зі знятого (`_historyApply` → `switchTab(primary)` + `_historyRestore` кожного домену).
- `_historyRedo`: дзеркально (поточний==BEFORE → у undo; відновлює AFTER з redo).
- **Інваріант** «поточний==AFTER per-domain» тримається незалежно від чергування доменів —
  тому classic swap коректний навіть при перемішаних cleanup/polygons/groups діях.
- `HISTORY_CAP = 100` (знімки дешеві — per-gesture).

---

## 2. Snapshot / restore (per-domain, `_historySnap`/`_historyRestore`)

| Домен | Знімок (`_historySnap`) | Restore (`_historyRestore`) |
|---|---|---|
| cleanup | `{rejected:[...set], markers:[{x,y}]}` | new Set + копія markers; markDirty; `_cleanupRedraw`; markers redraw |
| polygons | `{shapes: deepcopy}` (`_snapshotShapes`) | assign shapes; скинути selection/draft; markDirty (→ інвалідує coveredCache); redraw |
| groups | `{list: deepcopy, activeId}` (`_snapshotGroups`) | assign list; activeId лише якщо група існує; dirty; render; scheduleAutosave |

Знятий запис більше не в стеку → пряме присвоєння snap як live-стану безпечне (нема аліасингу).

---

## 3. Точки push (ВСІ мутації — ПЕРЕД зміною)

| Домен | Виклик | Сайти |
|---|---|---|
| cleanup | `_historyPush("cleanup")` | toggle (cleanup.js:122), marker add/remove (242/252), bulk-clear rejected/markers (266/276) |
| polygons | `_polyPushUndoSnapshot()` → `_historyPush("polygons")` (полігони.js:695) | draw/close/pick/delete/align/seed (405/497/502/522/541/636/657/719/776) |
| polygons drag | те саме, але **через `.pushed`-флаг** | vertex/group drag: push на ПЕРШОМУ mousemove (225/253/261), НЕ на mouseup |
| groups | `_groupsPushUndo()` → `_historyPush("groups")` (groups.js:135) | new/delete/toggle-inst/toggle-poly/lasso/class/rogue (149/168/262/287/510/578/693) |
| зовнішні | `_polyPushUndoSnapshot` | `multiseed.js:151`, `labels.js:263` (rename) — тонкі обгортки лишені для backward-сумісності |

### Темпоральна коректність (`CODE_AUDIT_PRINCIPLES §1F)
- **Drag — push на ПОЧАТКУ жесту** (перший mousemove, прапор `pg.draggingVertex/Group.pushed`),
  не на mouseup. Інакше знімок захоплював AFTER (зсунутий) стан → перший Ctrl+Z по драгу
  «нічого не робив» (off-by-one). Клік без руху → нема push → нема фантома.
- **Esc-скасування draft** (`_polyCancelDraft`) і **lasso по вже-згрупованих (no-op)** — НЕ пушать
  (draft не в snapshot shapes; інакше перший Ctrl+Z після Esc «нічого не робив» — фантом).

---

## 4. Складені (крос-доменні) дії = ОДИН запис
Єдина така дія: **видалення polygon-shape** ремапить `group.polygon_indices` (Bug 5).
`_polyDeleteShape*` спершу `_polyPushUndoSnapshot()` (створює запис із polygons-snap), потім
remap кладе before-snap груп у **той самий** запис через `_historyAttachSnap("groups", groupsBefore)`
(polygons.js:620). Один Ctrl+Z відновлює і shape, і `polygon_indices`.
**Pick — НЕ складена дія:** instance під полігоном rejected DERIVED з геометрії (covered, v1.16.1),
не пишеться в rejectedSet → лише polygons-snap.

---

## 5. Точки входу (увесь ввід через global)
- **Клавіші** (`keys.js:76`): `Ctrl+Z`→`_historyUndo`; `Ctrl+Shift+Z` / `Ctrl+Y`→`_historyRedo`. Tab-independent (не залежить від activeTab).
- **6 кнопок** (`events.js`): `#cleanupUndo/Redo` (69-70), `#polyUndo/Redo` (91-92), `#groupsUndo/Redo` (120-122) — усі → `_historyUndo`/`_historyRedo`. (Groups-кнопки раніше були **мертві** — не прив'язані; v1.16.2 fix.)
- `_historyUpdateButtons()` — уніфіковано enable/disable усіх 6 за станом стека (виклик з push/undo/redo/clear/apply). DOM статичний → `disabled` ставиться завжди, незалежно від активного табу.

---

## 6. Clear / межі
- `_historyClear()` на **open** і **close** (index.js:232/451) — per-stem межа = commit point.
  Bake завжди при ЗАКРИТОМУ редакторі (lazy-bake, Day 7), тож історія не переживає bake.
- `switchTab` **НЕ** пушить історію (лише flush autosave старого табу — безпечно) → рекурсії нема.
- Новий `_historyPush` чистить `redo` (стандартна інвалідація).

---

## 7. Race-guard (перетин з autosave)
Groups-autosave реасайнить `g.list = resp.groups` (echo) → in-flight POST міг клобнути undo
(«додавання інстанса не відмінялось»). Рішення — **generation-guard** (`g.gen`): `_groupsScheduleAutosave`
бампає gen; `_groupsSave` пропускає реасайн, якщо gen змінився під час польоту. cleanup/polygons
НЕ реасайнять → там race нема. Деталі → [`11`](11_AUTOSAVE_DIRTY_SYNC.md).

---

## 8. Known / edge + посилання
- Verified (браузерна верифікація, db_img_0084): крос-домен undo×3+redo round-trip з автоперемиканням табів; compound polygon-delete #102/g_001 = один запис [groups,polygons]; клавіатура Ctrl+Z/Shift+Z/Y. 0 console-err.
- Знімки — deep-copy (shapes/list); cleanup — свіжі Set/масиви. Аліасингу зі стеком нема (знятий запис вже не в стеку).
- Стан групи/полігонів → [`07`](07_GROUPS_TOOL.md)/[`06`](06_POLYGONS_TOOL.md); форма state → [`02`](02_FRONTEND_STATE.md).
