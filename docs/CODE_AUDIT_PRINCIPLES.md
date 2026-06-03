# Mask Picker — принципи аудиту коду (анти-«фікс тут — зламав там»)

> Дата: 2026-06-01. Привід: серія багів, де виправлення в одному місці лишало
> той самий концепт зламаним у паралельному. Цей документ — **чекліст + реєстр
> небезпечних місць**, щоб ревʼю передбачало баги, а не лише реагувало на репорти.
> Перед GitHub (Day 11) пройти весь реєстр §3 по цьому чеклісту.

---

## 0. Чому ми по 3 рази перероблюємо одне й те саме

**Корінь майже всіх повторних правок — ОДИН патерн:**

> Один **логічний концепт** має **кілька представлень** (stored + derived, або
> два ID-простори, або N паралельних UI-елементів), а консюмери цього концепту
> розкидані по коду й оновлюються **НЕ всі разом**.

Виправляєш консюмера, на якого вказав юзер → інші консюмери того ж концепту
лишаються зі старою логікою → «зламав у другому місці». Це не випадковість —
це **структурна властивість** кодбази: концепти НЕ централізовані, кожен консюмер
реалізує свою копію логіки («чи це rejected?», «скільки полігонів у групі?»,
«куди йде Ctrl+Z?»).

**Висновок:** аудит має бути **концепт-орієнтований**, не файл-орієнтований.
Спочатку — «який це концепт і де ВСІ його представлення/консюмери», потім — код.

---

## 1. Золотий чекліст аудиту будь-якої функції/інструмента

Для кожної фічі/функції, яку чіпаєш або ревʼюєш, пройди 8 кроків:

### A. Назви КОНЦЕПТ
Що це логічно? («rejected-інстанс», «полігон у групі», «undo-дія», «covered»).
Не «функція X», а **концепт**, яким вона оперує.

### B. Знайди ВСІ представлення концепту
- Чи є **stored** форма (на диску / у стані) і **derived** форма (обчислена)?
  → `rejected` = `rejectedSet` (stored) + `covered` (derived з геометрії полігонів).
- Чи є **два ID-простори** для того самого? → raw_iid vs baked_iid; polygon_index
  vs reserved instance_id (`POLYGON_ID_BASE+idx`).
- Чи є **N паралельних** елементів? → 3 таби, 3 домени undo, 6 undo-кнопок.

### C. Визнач SoT (Single Source of Truth)
Одне джерело правди на концепт. Derived форми **обчислюються** з SoT, не
зберігаються незалежно (а якщо кешуються — інвалідуються при зміні SoT).
- covered → SoT = `pg.shapes` (геометрія). reserved-iid → НЕ SoT, артефакт bake
  з `polygon_indices`. Полігон-у-групі → SoT = `polygon_indices` (НЕ instance_ids).

### D. Перелічи КОНСЮМЕРІВ (grep-driven, не з памʼяті)
`grep` ім'я концепту/поля по ВСІЙ кодбазі (front+back). Випиши список. **Кожен**
консюмер мусить трактувати концепт однаково: render, count, hit-test, hide,
block, persist, export. **Баг майже завжди — консюмер, якого ти не вписав.**
> Приклад: `covered` читається у `_cleanupRedraw` (marks-red), `_groupsToggleInstance`
> (block), `_groupsRedrawMaskCanvas` (peek), **`_paintRejectedFromOriginal` (hide)**.
> Останній забули при derived-rejection → червоний контур лишався. Grep `covered`
> + `rejectedSet` одразу показав би всіх 4+.

### E. Lifecycle: що при зміні SoT?
Коли SoT **додають / видаляють / редагують** — кожна derived форма й кожен
консюмер мусять оновитись.
- Видалив полігон з групи (SoT `polygon_indices` змінився) → reserved-iid
  (derived) лишився stale → «привид». Треба: UI ігнорує reserved (Layer 1) +
  bake-sync реконсилює (Layer 2).

### F. Темпоральна коректність (before vs after)
- **Undo/snapshot:** знімай BEFORE-стан на ПОЧАТКУ жесту, не в кінці. Drag мутує
  під час mousemove → push на mouseup захоплював AFTER (off-by-one).
- **Race (autosave):** in-flight write може приїхати ПІСЛЯ локальної зміни й
  клобнути її → generation-token guard (`g.gen`).

### G. Перевір ЗВОРОТНИЙ бік і edge-cases
Фікс часто ламає протилежний кейс:
- dedup reserved-iid «skip all ≥BASE» зламав fallback-union (next_id теж дає
  великі iid) → довелось розрізняти через `shape_idx_to_iid.values()`.
- undo → одразу тестуй redo + round-trip; «0 елементів»; повторний виклик.
- hide covered → не зламай explicit-rejected (на cleanup лишаються червоні).

### H. Верифікуй СПОСТЕРЕЖЕННЯМ, не «код виглядає правильно»
- Front: preview MCP — реальні пікселі canvas / DOM / network, не screenshot-«на
  око». Минулі сесії гадали по коду D/J і G по 3 рази — лише браузер знайшов
  корінь (`innerHTML=""` вбивав dblclick).
- Back: pytest + реальний API-запит на конкретних даних (db_img_0171 g_008).
- Драй real handler-и (з стабом координат), не лише прямі методи.

---

## 2. Як перевіряти СКЛАДЕНІ (interdependent) структури

Кодбаза — не набір ізольованих функцій, а композит. Тому:

1. **Малюй граф залежностей концепту перед правкою.** Хто пише SoT, хто читає,
   які derived форми, які кеші, коли інвалідуються. (Див. `INTERNAL_ARCHITECTURE.md`
   §3 ID-простори, §4 mutation domains, §5 invariants — це вже наполовину готовий
   граф; тримай його актуальним.)
2. **Складена (крос-доменна) дія = один атомарний запис/транзакція.** Один жест,
   що чіпає 2 домени (delete polygon → ремап `group.polygon_indices`), мусить бути
   ОДНИМ undo-записом (`_historyAttachSnap`) і логічно однією операцією. Інакше
   половина відкотиться/збережеться.
3. **Інваріанти — пиши їх явно й тестуй.** `INTERNAL_ARCHITECTURE.md §5` має I1-I5,
   B1-B3. Кожен фікс що чіпає groups/bake — звір з інваріантами. Self-heal (strip
   orphan, sync) має бути ідемпотентним.
4. **Кеш = derived. Завжди питай «коли інвалідується?»** `coveredCache`,
   `pixelCounts`, `_rejectedPatch` (sig), `_coveredBaseSig`. Кеш без коректної
   інвалідації = stale-баг. Сигнатура кешу мусить включати ВСЕ, від чого залежить.
5. **Front і back часто дублюють логіку — звіряй обидва.** Підрахунок ядер є в
   `groups.py::_count_labels_in_group` (back) І `groupMemberCount` (front). Фікс
   одного без іншого = розбіжність. Те саме: `POLYGON_ID_BASE` у baking.py І
   groups.py І groups.js (3 копії — guard-тест на рівність).

---

## 3. Реєстр НЕБЕЗПЕЧНИХ концептів цієї кодбази (multi-representation)

> Це головна цінність документа. Перед правкою будь-чого, що торкається рядка
> нижче — пройди §1 по ВСІХ консюмерах цього рядка.

| Концепт | SoT | Derived / друге представлення | Консюмери (grep) | Граблі |
|---|---|---|---|---|
| **Rejected інстанс** | `cu.rejectedSet` (raw_iid) | `covered` = derived з `pg.shapes` (>50%) | `_cleanupRedraw`, `_paintRejectedFromOriginal`/`_ensureRejectedPatch`, `_cleanupToggleInstance` (block), `_groupsToggleInstance` (block), groups lasso filter, `_groupsRedrawMaskCanvas` (peek), bake `cleaned[isin(rejected)]=0` | covered НЕ в rejectedSet — це геометрія. Будь-який консюмер «rejected» мусить врахувати І covered. Grep `covered` + `rejectedSet` РАЗОМ. |
| **Полігон у групі** | `group.polygon_indices` | reserved `instance_id = POLYGON_ID_BASE+idx` (bake-артефакт у `instance_ids`) | `_count_labels_in_group`, `_iids_by_label_in_group`, `groupMemberCount` (front), `_sync_groups_instance_ids_after_bake`, mask_groups export | Підрахунок — ЛИШЕ через polygon_indices; reserved iid ігнорувати в UI. На bake sync реконсилює (owner None + у `shape_idx_to_iid.values()` → strip). |
| **ID-простори інстансів** | raw_npy (raw_iid) / baked_npy (baked_iid) | `raw_iid==baked_iid` для model (reserved-ID v1.16.0); polygon → `≥BASE` | UI hit-test = raw (`cu.labelsInt32`); `group.instance_ids` = baked; bake конвертує | НЕ плутати простори. `<BASE` = model, `≥BASE` = polygon (АЛЕ fallback next_id теж великий — розрізняй через shape_idx_to_iid). |
| **Undo-дія** | `editor.state.history` (єдиний стек) | — (раніше 3 per-tab стеки — джерело розсинхрону) | keys.js (Ctrl+Z/Y), events.js (**6 кнопок**: cleanup/poly/groups × undo/redo), кожна мутація (`_historyPush` ПЕРЕД зміною), restore | Drag: push на ПОЧАТКУ (перший mousemove), не mouseup. Кнопки: усі 6, не 4. Складена дія: один запис. |
| **Стан групи** | `groups/<stem>.json` | mirror `polygons.json.shapes[i].group_id` | POST groups (пише обидва), зовн. LabelMe (читає mirror) | Mirror НЕ парсимо; SoT = groups.json. |
| **Dirty / autosave** | per-domain `dirty` + debounce | — | flush на switch/close/nav; `_enqueueSave` (черга); `g.gen` (race-guard) | Groups autosave реасайнить `g.list = resp` → race з undo. Generation-guard. cleanup/poly НЕ реасайнять — там race нема. |
| **Класифікація групи** | обчислюється з counts | `n_nucleus/n_vesicle/iids_by_label/valid/rogue` | back GET повертає; front chip показує `cls.counts` + `groupMemberCount` | Front не рахує сам — бере backend. Але `groupMemberCount` рахує окремо → звіряй з counts. |
| **Base render (overlay vs hidden)** | bgSource + `_overlayHideSet()` | `_rejectedPatch` (кеш по sig), `_coveredBaseSig` | `_drawBase`, `_paintRejectedFromOriginal`, `_cleanupRedraw` (marks), `_drawBaseIfCoveredChanged` (live) | hide-set різний per-tab (cleanup=covered; working=rejected∪covered). Кеш-sig мусить включати hide-set. |

---

## 4. Анти-патерни, які вже кусали (швидкий список «не повтори»)

1. **Фікс одного консюмера derived-стану, забув паралельний.** (covered у marks, не
   в base-hide; double-count у `_count_labels` , не в `groupMemberCount`.)
2. **Snapshot/слухач у НЕправильний момент.** (undo на mouseup після мутації.)
3. **Прив'язав N-1 з N паралельних UI.** (undo-кнопки 2/3 тулбарів.)
4. **Derived не реконсилюється при зміні SoT.** (stale reserved-iid = привид.)
5. **In-flight write клобає свіжий стан.** (groups autosave vs undo → gen-guard.)
6. **Сліпа евристика `iid≥BASE`** замість семантичної перевірки (`in shape_idx_to_iid`).
7. **Кеш без повної сигнатури** (sig не включав щось, від чого залежить → stale).
8. **Гадання по коду замість браузера** (D/J, G — по 3 переписки наосліп).
9. **HTML-зміна без рестарту Flask** (Jinja кешує шаблон при debug=False).
10. **Read-вивід глючив біля лімітів** — якорись на GREP (ripgrep) для істини.

---

## 5. Процедура pre-GitHub аудиту (Day 11)

1. Для КОЖНОГО рядка реєстру §3: grep усіх консюмерів → звір трактування концепту
   → виправ розбіжності. Це системний прохід, не «по фічах».
2. Прогони інваріант-верифікатори: `tools/audit_export.py`, `_tmp/desync_invariants.py`,
   `_tmp/verify_bug3_clusterization.py`.
3. pytest повний + node --check усіх JS.
4. Браузер-smoke ключових жестів (preview MCP): cleanup toggle, polygon draw/edit/
   delete, groups toggle/lasso, undo/redo крос-домен, covered hide.
5. Перевір 3 копії `POLYGON_ID_BASE` (guard-тест) і інші продубльовані константи.
6. Звір front↔back дубльовану логіку (counts, member count, id-простори).

---

## 6. Одне речення, яке резюмує все

> **Перш ніж правити — назви концепт, знайди ВСІ його представлення й консюмерів
> (grep, не пам'ять), визнач SoT, перевір що derived слідує за SoT при зміні,
> зніми стан у правильний момент, і верифікуй спостереженням. Баг — це майже
> завжди консюмер, якого ти не вписав у список.**
