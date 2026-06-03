# Mask Picker — лог аудиту (живий)

> Знахідки під час документування/аудиту. Кожен
> запис трасується методом `CODE_AUDIT_PRINCIPLES.md` (назви концепт → grep усіх
> консюмерів → SoT → що при зміні SoT → before/after → зворотний кейс → верифікуй).
>
> **Політика (§3 плану):** тривіальні/однозначні фіксь з повним consumer-аудитом +
> верифікацією; **ризиковані/неоднозначні — НЕ вгадувати, спитати мейнтейнера** (краще
> окремий fix-чат після рев'ю). Тут лише ЛОГ; фікс — окремо.

## Шкала severity

| Рівень | Значення |
|---|---|
| 🔴 **high** | хибні дані замовнику / втрата роботи / зламаний інваріант |
| 🟠 **med** | неправильна поведінка UI / розбіжність консюмерів, але не псує deliverable |
| 🟡 **low** | косметика / edge-case / незручність |
| 📘 **doc** | doc-drift: документація розходиться з кодом (поведінка коректна) |

## Статуси
`logged` (записано) · `asked` (спитано мейнтейнера, чекаю) · `approved` (мейнтейнер ОК фіксити) ·
`fixed` (виправлено + верифіковано) · `wontfix` (свідомо лишаємо, з причиною).

## Шаблон запису
```
### F-NNN — <короткий заголовок>   [severity] [статус]
- **Концепт:** <логічний концепт (не «функція X»)>
- **SoT:** <джерело правди> · **Представлення/консюмери:** <grep-список>
- **Симптом / repro:** <як проявляється; на яких даних>
- **Підозра / корінь:** <гіпотеза>
- **Зворотний кейс:** <що могло б зламатись від «очевидного» фіксу>
- **Дія:** logged / asked(дата) / fixed(commit+verify)
```

---

## Знахідки

### F-001 — docstring-и описують стару `next_id`-схему (до reserved-ID)   📘 doc · ✅ fixed
- **Концепт:** baked_iid полігона (як полігон отримує instance_id при bake).
- **SoT коду:** `baking._bake_polygons_into_labels` — primary = **reserved** (`POLYGON_ID_BASE+idx`, v1.16.0); `next_id=max+1` лишився ЛИШЕ як graceful fallback (`use_reserved=False`).
- **Симптом:** документація неповна/застаріла (не баг поведінки):
  - `cleanup.py` docstring (≈р.36): «Polygon-shape додаються з `next_id = max(cleaned)+1`».
  - `polygons.py` docstring (≈р.34-38): «При bake кожен polygon-shape отримує свіжий baked_iid (`next_id`)».
  - `data_sync.py` docstring §ID-простори (≈р.32-37): «додає polygon-shapes з new IDs (`next_id = max(cleaned)+1`). Bake перенумеровує».
- **Дія:** ✅ fixed (2026-06-01, Phase 2/doc-01): оновлено docstring-и `cleanup.py`/`polygons.py`/`data_sync.py` §ID-простори — reserved-ID primary, `next_id` = graceful fallback. **pytest 236/236** (docstring-only, поведінка не зачеплена).

### F-002 — `INTERNAL_ARCHITECTURE.md` датований v1.15.0 (частково застарів)   📘 doc · ✅ mitigated
- **Концепт:** загальна архітектура (ID-простори, bake, інваріанти).
- **Симптом:** header «v1.15.0»; §3.2 крок 4 «next_id = max(cleaned)+1» (до reserved-ID); §5 «known limitations» I5 next_id collision частково знято reserved-ID (v1.16.0); не згадані Layer 1/Layer 2 дедуп reserved-iid (v1.16.2), Solution B, глобальний undo. Сам файл це визнає («⚠️ датовано v1.15.0»).
- **Дія:** ✅ mitigated (2026-06-01): додано банер-supersession угорі `INTERNAL_ARCHITECTURE.md` — перенаправляє на `docs/architecture/` + перелічує ключові дельти (reserved-ID, rejected SoT, compaction, Layer1/2/Solution B/undo). Тіло v1.15.0 лишено історичним (повне переписування — не потрібне, нові docs авторитетні).

### F-003 — `INTERNAL_ARCHITECTURE §6` каже finalize компактить; реально — лише `--pack`   📘 doc · logged
- **Концепт:** compaction (sparse working id → dense 1..N deliverable).
- **SoT коду:** `compact_instance_ids` кличеться **ТІЛЬКИ** `tools/launchers/bake_all.py:242` (CLI `--pack`).
- **Симптом:** `INTERNAL_ARCHITECTURE.md §6` крок 6 стверджує «GET /api/workspace/finalize → COMPACTION (dense 1..N)». Перевірено `api_workspace.py:543-660`: finalize робить `bake_with_resync` + бандлить selected as-is, **без** `compact_instance_ids` → ZIP містить **sparse** id (50000+).
- **Не баг поведінки:** finalize = per-photo «send-back» пакет (sparse, round-trip команді зберігає групи); `--pack` = dense customer-deliverable. Розумний дизайн; помилка лише в описі.
- **Дія:** logged. Виправити в `INTERNAL_ARCHITECTURE` при синхронізації (F-002); задокументовано коректно в `01` §5 + буде в `12`.

### F-004 — live-SoT для rejected = `selections.json`, а не `selected/<model>/cleanup.json`   📘 doc (🟠) · ✅ fixed
- **Концепт:** rejected instances (raw_iid) — де джерело правди у lazy-bake flow.
- **SoT коду:** запис — `state.set_cleanup`→`selections.json` (POST /api/cleanup); читання — `state.get_cleanup`←`selections.json`; bake (bake-all `:113`, finalize `:574`, rebake) бере `rejected` з `selections.json`. `selected/<model>/cleanup.json` пишеться ЛИШЕ `cleanup-export` (`api_cleanup.py:221`), читається ЛИШЕ для finalize-ZIP бандлу (`:645`).
- **Симптом:** `INTERNAL_ARCHITECTURE §2` SoT-таблиця називає SoT-ом `cleanup.json`, а `selections.json` — mirror. Інвертовано для Day-7+ (lazy-bake). Ключ теж різний: `rejected_instances` (selections) vs `rejected` (cleanup.json).
- **Наслідок (🟡):** у normal flow (autosave+Save All, без 🔥) `cleanup.json` у finalize-ZIP може бути відсутній/застарілий vs фактично запечені rejected. **Маски коректні** (npy уже baked); cleanup.json — лише provenance.
- **Зворотний кейс:** НЕ «фіксити» зміною коду навмання — selections.json як робочий store коректний. Питання лише: чи треба синхронізувати cleanup.json на bake-all/finalize (для точного provenance у ZIP)? → **ризиковане/неоднозначне, спитати мейнтейнера** перед будь-яким код-фіксом.
- **Дія:** ✅ fixed (2026-06-01, мейнтейнер обрав «синхронізувати на bake»): `data_sync.bake_with_resync` тепер кличе новий `_sync_cleanup_json_after_bake` — пише `cleanup.json[stem].rejected` = фактично запечений rejected, зберігаючи `user`+`markers`, на КОЖЕН bake-шлях (polygons-export/rebake/cleanup-export/bake-all/finalize). Provenance у ZIP завжди свіже. **pytest 236/236.**

### F-005 — JS-копія `POLYGON_ID_BASE` без автоматичного guard   🟡 low · logged
- **Концепт:** 3 копії `POLYGON_ID_BASE=50000` (baking.py:111, groups.py:112, groups.js:176).
- **SoT коду:** guard `test_groups_smoke.py:265` звіряє лише `groups==baking` (Python). JS-копія (`static/modules/groups.js:176`) не покрита жодним тестом.
- **Симптом:** якщо хтось змінить BASE у Python, JS мовчки розійдеться → `groupMemberCount`/lasso рахуватимуть полігони як model-instance (double-count повернеться).
- **Зворотний кейс:** автотест JS↔Python нетривіальний (різні рантайми); можливо досить коментаря-якоря + ручної звірки в pre-GitHub чеклісті.
- **Дія:** logged. Рішення (тест чи процес) — обговорити; зараз лише задокументовано в `01` §4 + `09`.

### F-006 — handoff наводить застарілі значення колірних констант   🟡 doc · logged
- **Концепт:** OKLCH-константи `effectiveHSL` (groups.js:44-48).
- **SoT коду:** `GROUP_CHROMA_MIN=0.095`, `GROUP_CHROMA_SPAN=0.105`, `GROUP_L_MIN=0.40`, `GROUP_L_SPAN=0.50`, `GROUP_HUE_ARC=0.70`.
- **Симптом:** `NEXT_SESSION_HANDOFF_2026-06-01.md` §1 наводить `GROUP_CHROMA=0.15, L_MIN=0.42, L_SPAN=0.44, HUE_ARC=0.50` (інші імена + значення — рання версія). CHANGELOG [1.16.2] збігається з кодом.
- **Дія:** logged. Задокументовано коректно в `08` §2. Handoff — ефемерний; виправляти не обовʼязково (не плутати при тюнінгу — брати код).

### F-007 — header-docstring `editor/groups.js` описує старий lasso-flow   📘 doc · ✅ fixed
- **Концепт:** lasso hit-test у Groups.
- **SoT коду:** `_groupsApplyLasso`→`_groupsLassoHitTestLocal` (client-side, Solution B v1.16.2).
- **Симптом:** docstring `editor/groups.js` р.15-28 ще пише «`_groupsApplyLasso` паралельно: bakedIds через POST /api/groups/<stem>/lasso-hit-test … Bug 4 hypothesis» — застаріле (бекенд-ендпоінт фронт більше не кличе; CHANGELOG [1.16.2] Removed це фіксує). Тіло коду правильне.
- **Дія:** ✅ fixed (2026-06-01): docstring `editor/groups.js` (Lasso flow + endpoints) оновлено під Solution B (client-side `_groupsLassoHitTestLocal`; lasso-hit-test = LEGACY). node --check OK.

### F-008 — deliverable-артефакти розходяться між export-шляхами   🟠 med · ✅ fixed
- **Концепт:** що саме отримує замовник (instance npy + groups + derived masks).
- **SoT коду:** `--pack` (`bake_all.py`) → dense `masks_npy`+`masks`+`groups`+yolo+overlays+polygons, **БЕЗ** `semantic`/`mask_groups`. Flask `export?masks=1` → `semantic`/`mask_groups` у `selected/<model>/`, але **sparse**.
- **Симптом:** `clusterization.py` (замовник) за INTERNAL_ARCHITECTURE §6 читає `npy + semantic + mask_groups + groups.json`. Жоден ОДИН шлях не дає водночас dense npy І dense mask_groups. handoff 2c називає `--pack` шляхом оновлення замовника — а він mask_groups не містить.
- **Зворотний кейс:** якщо додати derived masks у `--pack`, треба рендерити їх з DENSE npy (не sparse) — інакше id у mask_groups розійдуться з npy.
- **Дія:** ✅ fixed (2026-06-01, рішення мейнтейнера: instance+semantic ЗАВЖДИ, group-masks за вибором): `bake_all.py --pack` тепер рендерить `semantic/` **ЗАВЖДИ** + `mask_groups/` **лише за `--group-masks`** (реюз tested `baking.export_derived_masks`). semantic=per-class, mask_groups=per-group-order → **id-value-незалежні** → коректні для dense deliverable, хоч рендеряться з sparse `selected/`. **Verified end-to-end read-only на `data/vesicles_good`:** без прапора 16 semantic / 0 mask_groups; з `--group-masks` 16/16; дані не змінено. pytest 236/236.

### F-009 — `--pack` layout не збігається з тим, що читає `clusterization.py`   🟠 med · logged (рішення мейнтейнера)
- **Концепт:** структура папок deliverable vs очікування консюмера.
- **Джерело істини:** `_inbox/clusterization.py:466-470` читає `{base}/images/`, `{base}/npy/` (instance),
  `{base}/semantic/`, `{base}/groups/<stem>.json` (+fallback `{base}/polygons/`). **`mask_groups` НЕ читає.**
- **Симптом:** `bake_all.py --pack` пише instance npy у **`masks_npy/`** (+ png у `masks/`), а clusterization
  очікує **`npy/`**. `semantic/`/`groups/`/`polygons/`/`images/` — збігаються. Тобто замовницький
  clusterization «з коробки» не знайде instance-маску (шукає `npy/`, є `masks_npy/`).
- **Підтвердження F-008:** clusterization НЕ використовує mask_groups → дефолт «mask_groups off» правильний;
  semantic+instance+groups (що `--pack` тепер дає) — рівно те, що треба.
- **Зворотний кейс:** перейменувати `masks_npy→npy` у `--pack` — але це змінює усталений layout
  (можливо інші консюмери/скрипти очікують `masks_npy/`). Тому — рішення мейнтейнера, не авто-фікс.
- **Дія:** logged. Опції: (1) `--pack` пише `npy/` замість `masks_npy/`; (2) лишити, замовник перейменовує;
  (3) додати у README deliverable-а мапу імен. Спитати мейнтейнера. Також: `mask_groups` png у `--pack` —
  чи потрібен будь-кому (clusterization — ні)?

### F-010 — `clusterization_patch.md` fallback застарів (Bug 3 закрито у MP)   📘 doc/info · logged
- **Концепт:** «втрата polygon-only ядер» у ground truth.
- **Симптом:** патч додає fallback (polygon→nucleus iid) бо `groups.json` не мав polygon-resolved iid у
  `instance_ids`. Це **Bug 3**, який MP закрив v1.13.0 (`_sync_groups_instance_ids_after_bake`, Layer 2).
- **Наслідок:** на сучасних `--pack`-експортах (з compaction, що ремапить reserved polygon-iid у dense
  instance_ids) ядра вже у `instance_ids` → fallback НЕ потрібен. Патч лишається безпечним (idempotent).
- **Дія:** logged. Рекомендація: прогнати `_tmp/verify_bug3_clusterization.py` на свіжому `--pack` для
  підтвердження 0 lost; повідомити замовника, що патч більше не обовʼязковий (але не шкодить).

## Глибокий пошук багів (deep pass, 2026-06-01, повний контекст додатку)

### F-011 — instance 50–85% під полігоном виживав у deliverable як «огризок»   🟠 med (data-quality) · ✅ fixed
- **Концепт:** поріг «covered» (UI) vs backstop (bake).
- **Код:** UI `_polyCoveredInstances` ховає/блокує instance при `cov/total > **0.5**` (cleanup.js:234);
  bake-backstop стирає instance лише якщо лишилось `< **0.15**` оригіналу (baking.py:343, тобто >85% під полігоном).
- **Симптом:** якщо полігон покриває інстанс на **50–85%** → у UI він «зник» (covered, невибірний у групи),
  АЛЕ у `selected/npy` лишається його **залишок-огризок** (15–50% пікселів) зі своїм класом. У deliverable —
  стрей-інстанс, якого анотатор вважав прибраним; clusterization побачить «нічию» везикулу/ядро-огризок → легке
  забруднення ground truth. (Типово полігон малюють щільно → >85% → стирається; проблема лише при частковому перекритті.)
- **Зворотний кейс (чому НЕ авто-фікс):** опустити backstop 0.85→0.5 = агресивніше стирання → ризик стерти
  СУСІДА, якого великий полігон зачепив на 50% (саме тому 0.85 консервативний). Два пороги служать різним цілям.
- **Дія:** ✅ fixed (2026-06-01, мейнтейнер авторизував зсув до 50%): backstop-поріг `baking.py` **0.15→0.50** —
  вирівняно з UI `covered>0.5`. Виміряно ПЕРЕД фіксом на data/vesicles_good (16 фото): **75 огризків**
  (50–85%); ≤50%-сусіди (836) НЕ зачеплені (захист збережено); >85% (2122) як і раніше стираються.
  +регресія `test_bake_backstop_erases_over_half_covered`. **pytest 237/237.**
  ⚠ Розмітку вже надіслано замовнику → застосується на НАСТУПНОМУ ребейку/доставці (`--pack`).

### F-012 — `--pack` мовчки робить неповний deliverable для нестрейканого stem   🟡 low (robustness) · logged
- **Код:** `bake_all.py pack_zip` — якщо `selected/<model>/npy/<stem>.npy` відсутній (bake впав/не робився),
  compaction skip → fallback теж нічого не знаходить → у ZIP потрапляють groups/polygons/images БЕЗ npy/semantic.
- **Симптом:** «фото без маски» у пакеті, без попередження. (`bake_all` спершу пече все, тож зазвичай не буває;
  але якщо `stats.fail>0` — pack усе одно йде.)
- **Дія:** logged. Рек.: у pack_zip рахувати/warn-ити stems без npy; не падати, але вивести список наприкінці.

### F-013 — reserved-ID fallback на екстремальних датасетах повертає стару колізію   🟡 low (known-limit) · logged
- **Код:** `_bake_polygons_into_labels` (baking.py:238): `use_reserved` вимикається якщо `raw_max ≥ 50000` АБО
  `len(shapes) > 15000` → fallback на `next_id=max+1`, що МОЖЕ збігтись з rejected raw_iid (стара Bug 3/I5 колізія).
- **Симптом:** на vesicles_good безпечно (raw_max 6931). Але **публічний репо** — інші мейнтейнери на СВОЇХ даних
  (модель з >50k інстансів, або >15k полігонів/фото) отримають fallback → латентні desync-баги.
- **Дія:** logged. Рек.: документувати ліміт у README/тех.записці; або підняти стелю (uint32 npy замість uint16
  PNG-mirror — більша зміна). Зараз — лише задокументувати.

### F-014 — semantic-клас залежить від ПОРЯДКУ `labels.json`; clusterization хардкодить 1=nucleus,2=vesicle   🟡 low (contract) · logged
- **Код:** `export_derived_masks` semantic = `cid+1` (cid = індекс класу у labels.json, baking.py:773);
  `clusterization.py` читає `sem==1`(nucleus)/`sem==2`(vesicle) хардкодом.
- **Симптом:** якщо хтось переставить порядок у `labels.json` (vesicle перед nucleus) → semantic-номери
  поміняються → deliverable «мовчки» розійдеться з консюмером. Зараз labels.json = [nucleus, vesicle] (ОК).
- **Дія:** logged. Рек.: зафіксувати інваріант у docs/тех.записці («порядок labels.json = семантичний контракт:
  1=nucleus, 2=vesicle»); опційно — semantic за фіксованою мапою назв, не за порядком.

> _Сегментація (`apps/segmentation/run_segmentation.py`) переглянута — багів нема (грейсфул skip моделей,
> resume, UTF-8 fix). Залежності: `shared/cellsegkit` (in-repo) + pip (instanseg/stardist/ultralytics) +
> `cryobiology4/weights/` для кастомних моделей (built-in cyto2 працює без них)._

---

> _Усі 14 docs готові + fix-pass + deep-bug-hunt (2026-06-01). **Закрито код-фіксами: F-001, F-004,
> F-007, F-008, F-011** (pytest **237/237**; F-008+F-011 verified на реальних даних). F-002 mitigated (банер).
> **Залоговані (doc/процес/known-limit, не баги поведінки):** F-003 (compaction-only-pack, документовано),
> F-005 (JS BASE guard), F-006 (handoff-константи), F-009 (deliverable layout npy vs masks_npy — рішення мейнтейнера),
> F-010 (clusterization-патч застарів), F-012 (pack без warn на missing-npy), F-013 (reserved fallback на
> екстремальних даних — публічний ліміт), F-014 (semantic = порядок labels.json — контракт).
> Розбіжностей трактування концептів у коді не виявлено (системний прохід `14`)._
