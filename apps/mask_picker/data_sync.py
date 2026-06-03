"""
data_sync.py — єдиний диригент cross-cutting мутацій Mask Picker.

== Що це ==
Один модуль, через який проходять усі операції, які зачіпають
**кілька файлів одночасно** (polygons.json + groups.json + cleanup.json +
selected/<model>/npy/<stem>.npy). Existing routes/api_*.py стають тонкими
HTTP-обгортками, які делегують у data_sync.* і повертають JSON.

Single-file (per-domain) мутації лишаються у своїх модулях:
- groups.py     — single-membership, classification, orphan-strip helper;
- cleanup.py    — write cleanup.json, RGB cache, backup rotation;
- polygons.py   — LabelMe envelope валідація, polygons.json read/write;
- baking.py     — _bake_polygons_to_selected (raw npy → baked artifacts).

data_sync імпортує ці helper'и і **координує** їх у правильному порядку.

== Файли на диску, з якими працює модуль ==
- data/<dataset>/polygons/<stem>.json                 — polygons (LabelMe envelope)
- data/<dataset>/groups/<stem>.json                   — групи + group_classes
- data/<dataset>/selected/<model>/cleanup.json        — SoT rejected + markers
- data/<dataset>/selected/<model>/npy/<stem>.npy      — filtered baked npy
- data/<dataset>/selected/<model>/{png,yolo,overlay}/ — derived
- data/<dataset>/selections.json                      — mirror cleanup.rejected + status
- data/<dataset>/output/<model>/npy/<stem>.npy        — raw output (SSoT, read-only)

== ID-простори ==
**raw_iid**    : instance ID у raw output (output/<model>/npy). Стабільний
                 (Mask Picker його не пише). UI hit-test читає це через
                 GET /api/labels-rgb/. cleanup.rejected — IDs у цьому просторі.
**baked_iid**  : instance ID у filtered baked npy (selected/<model>/npy).
                 Bake фільтрує raw → rejected=0, додає polygon-shapes у reserved-
                 range id (POLYGON_ID_BASE+idx, 50000+, v1.16.0; next_id=max+1 —
                 лише graceful fallback). Уцілілі raw зберігають свій id.

raw і baked простори здебільшого ЗБІГАЮТЬСЯ для уцілілих інстансів (reserved-ID
v1.16.0: raw_iid==baked_iid, raw<7000; полігони → ≥50000). Фронт читає raw для
hit-test, бекенд читає filtered для derived masks. Але групи
зберігають `instance_ids` у baked-просторі — тому при bake потрібен
post-step strip-orphan, який і робить `bake_with_resync`.

== Invariants (короткий список) ==
- I1 strict : іd у group.instance_ids унікальний у межах групи.
- I3 strict : polygon_index у group.polygon_indices унікальний.
- I4 strict : жоден іd не у двох групах (single-membership).
- I5 lazy   : rejected ∩ group.instance_ids == ∅ (self-heal at bake).
- B3 lazy   : group.instance_ids ⊆ unique(baked npy) (self-heal at bake/GET).
- I7 strict : polygon_index валідний для polygons.shapes (B1/B2 у audit).
- I2 soft   : polygon label семантично відповідає npy під ним (advisory).

== Гарантії data_sync ==
1. Узгоджена зміна у всіх потрібних файлах (один логічний крок —
   один виклик функції).
2. Атомарний запис кожного файлу через `state._atomic_write_json`.
3. Lazy invariants (B3) self-heal-яться у bake_with_resync напряму на
   диску — навіть для batch-bake без UI.
4. Reseat-helper готовий для майбутнього випадку коли raw output
   перерендерять (зараз не активний — raw стабільний).

== Що НЕ робить data_sync ==
- НЕ модифікує raw output (output/<model>/npy) — read-only SSoT.
- НЕ реалізує single-file CRUD (це у groups.py / cleanup.py / polygons.py).
- НЕ парсить HTTP request body (це routes/api_*.py).

== Public функції ==
- bake_with_resync(cfg, stem, model_name, src_npy, shapes, ...)
    Drop-in замість `_bake_polygons_to_selected`. Викликає bake, потім
    self-heal B3 через `_strip_orphans_in_groups_file`. Результат —
    той самий dict що повертає старий `_bake_polygons_to_selected`,
    + 1 поле `orphan_iids_stripped`.

- reseat_rejected_after_bake(old_npy, new_npy, rejected_old, ...)
    Pure-функція. Транслює rejected IDs з OLD npy у NEW через геометричний
    overlap (`overlap_ratio > overlap_min` → нове id = dominant у NEW).
    Без файлових операцій, легко тестується.
    Зараз НЕ викликається з bake_with_resync — це safety-net для майбутніх
    migration-scripts (наприклад при regenerate raw output).

== Private helpers ==
- _strip_orphans_in_groups_file(cfg, stem, model_name) → int
    Читає groups/<stem>.json, бере known_iids з NEW filtered npy,
    викликає groups._strip_orphan_instance_ids, перезаписує файл
    атомарно якщо щось strip-нуто. Повертає число видалених iid.

== Як читати цей файл ==
Спочатку — public функції (вгорі), потім reseat_rejected_after_bake,
потім private helpers. Кожна функція має docstring що пояснює "навіщо"
не лише "що".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from state import (
    Config,
    _atomic_write_json,
    np,
)
from baking import _bake_polygons_to_selected
from groups import _strip_orphan_instance_ids
from cleanup import _write_cleanup_json


# ---------------------------------------------------------------------------
# Public: bake_with_resync — drop-in замість _bake_polygons_to_selected
# ---------------------------------------------------------------------------

def bake_with_resync(
    cfg: "Config",
    stem: str,
    model_name: str,
    src_npy: Path,
    shapes: list,
    *,
    rejected: list,
    base_label: Optional[str],
    do_backup: bool,
    progress_cb=None,
) -> dict:
    """
    Drop-in заміна `baking._bake_polygons_to_selected` з self-heal step'ами.

    Послідовність:
      1. Виклик existing `_bake_polygons_to_selected` (без змін). Він уже:
         - пише NEW filtered npy/png/yolo;
         - всередині робить groups_sync (Bug 3, authoritative resolve);
         - НЕ робить strip orphan напряму на диску (тільки in-memory у GET
           /api/groups, який bake не викликає).
      2. NEW: `_strip_orphans_in_groups_file(cfg, stem, model_name)` —
         читає groups/<stem>.json, бере known_iids з NEW filtered npy,
         strip-ає сирітські iid (баг 3 sync лишив тільки polygon-resolved
         iid, а old manual iid що зник у фільтрованому npy лишився сиротою).
         Атомарно перезаписує groups.json якщо щось strip-нуто. Це
         self-heal **на диску** для batch-bake без UI.

    Сигнатура (стара частина) ідентична `_bake_polygons_to_selected` —
    тобто drop-in. Frontend контракти не міняються — routes просто
    кличуть цю функцію замість старої.

    Args:
        cfg          : Config.
        stem         : ім'я фото.
        model_name   : ім'я моделі (напр. "instanseg").
        src_npy      : Path до raw output .npy (input для bake).
        shapes       : polygons.json shapes для bake.
        rejected     : поточний cleanup.rejected (у просторі raw npy).
        base_label   : default label для polygon shapes без явного label.
        do_backup    : чи робити backup перед bake.
        progress_cb  : callback(phase, frac) для UI progress.

    Returns:
        dict зі стандартними полями `_bake_polygons_to_selected` +
        `orphan_iids_stripped: int` — скільки сирітських iid знято з
        groups.json після bake.

    Безпека:
    - Якщо bake впав (errors not empty) → strip пропускається.
    - Якщо strip падає → у result.errors додається запис, NEW npy лишається.
    """
    # 1. Bake (без змін). Усередині — groups_sync (Bug 3).
    result = _bake_polygons_to_selected(
        cfg, stem, model_name, src_npy, shapes,
        rejected=rejected, base_label=base_label, do_backup=do_backup,
        progress_cb=progress_cb,
    )

    # 2. Self-heal B3: strip orphan iids з groups.json на диску.
    orphan_stripped = 0
    if not result.get("errors") and cfg.groups_dir:
        try:
            orphan_stripped = _strip_orphans_in_groups_file(
                cfg, stem, model_name,
            )
        except Exception as exc:
            print(f"[data_sync] strip orphans failed for {stem}/{model_name}: {exc}")
            result.setdefault("errors", []).append(f"strip_orphans: {exc}")

    # 3. F-004: синхронізувати provenance у selected/<model>/cleanup.json
    # (rejected = фактично запечений цей bake). У lazy-bake flow rejected живе у
    # selections.json; cleanup.json раніше писав лише cleanup-export → у звичайному
    # циклі (autosave + Save All) cleanup.json у finalize-ZIP був stale/відсутній.
    # Маски від цього не страждали (npy вже baked), але provenance розходилось.
    if not result.get("errors"):
        try:
            _sync_cleanup_json_after_bake(cfg, stem, model_name, rejected)
        except Exception as exc:
            print(f"[data_sync] cleanup.json sync failed for {stem}/{model_name}: {exc}")

    result["orphan_iids_stripped"] = orphan_stripped
    return result


# ---------------------------------------------------------------------------
# Public: reseat_rejected_after_bake (safety net, не використовується у
# звичайному bake — для migration-scripts при regenerate raw output)
# ---------------------------------------------------------------------------

def reseat_rejected_after_bake(
    old_npy: "np.ndarray",
    new_npy: "np.ndarray",
    rejected_old: list[int],
    *,
    overlap_min: float = 0.5,
) -> tuple[list[int], list[int]]:
    """
    Переносить rejected IDs з OLD npy у NEW npy через геометричний overlap.

    **Коли потрібно:** rejected зберігаються у raw output просторі. Якщо
    raw output **перерендериться** (run_segmentation запускається на тих
    самих фото) — нумерування raw IDs стане інше, старі rejected
    неактуальні. Цей хелпер транслює rejected зі старого raw у новий
    через геометричний overlap.

    **Чому НЕ викликається у звичайному bake_with_resync:** у звичайному
    робочому flow raw output не змінюється — bake читає raw, фільтрує,
    додає polygon-shapes, пише filtered. Rejected у raw-просторі лишаються
    валідними. Reseat тут — no-op.

    **Коли стане у пригоді:**
    - Migration-скрипт коли юзер заново сегментує (новий raw).
    - Майбутній сценарій "rejected переноситься між моделями".
    - Backup-restore сценарій коли rejected з backup треба перенести на
      поточний raw.

    Алгоритм для кожного old_iid:
      1. mask = (old_npy == old_iid)             — pixel'и старого rejected
      2. if mask.sum() == 0: drop (instance уже не існував у OLD)
      3. unique_new, counts = np.unique(new_npy[mask], return_counts=True)
         — які instance ID у NEW лежать на тих же pixel'ах
      4. Виключаємо 0 (фон). Якщо нічого не лишилось → drop.
      5. dominant = unique_new[counts.argmax()] серед ненульових.
      6. overlap_ratio = counts.max() / mask.sum()
      7. if overlap_ratio > overlap_min → rejected_new.add(dominant)
         else → drop (геометрія сильно змінилась, не та сама клітина).

    Args:
        old_npy        : labels.npy ДО зміни (наприклад старий raw).
        new_npy        : labels.npy ПІСЛЯ зміни (наприклад новий raw).
        rejected_old   : список rejected IDs у просторі old_npy.
        overlap_min    : мінімальний overlap_ratio для переносу (0.0..1.0).

    Returns:
        (rejected_new, dropped):
          rejected_new : sorted list[int] — нові IDs у просторі new_npy.
          dropped      : sorted list[int] — old IDs які не вдалось перенести.

    Pure function (без I/O). Безпечно для тестування у ізоляції.
    """
    if old_npy is None or new_npy is None:
        return ([int(i) for i in rejected_old], [])
    if old_npy.shape != new_npy.shape:
        # Зміна геометрії (resize) — не можемо перенести, drop усе.
        return ([], sorted({int(i) for i in rejected_old}))

    rejected_new: set[int] = set()
    dropped: list[int] = []

    for raw_iid in rejected_old:
        try:
            old_iid = int(raw_iid)
        except (TypeError, ValueError):
            continue
        if old_iid <= 0:
            continue

        mask = (old_npy == old_iid)
        mask_size = int(mask.sum())
        if mask_size == 0:
            dropped.append(old_iid)
            continue

        sub = new_npy[mask]
        unique_new, counts = np.unique(sub, return_counts=True)
        nonzero_mask = unique_new != 0
        unique_nz = unique_new[nonzero_mask]
        counts_nz = counts[nonzero_mask]
        if len(unique_nz) == 0:
            dropped.append(old_iid)
            continue

        idx = int(counts_nz.argmax())
        dominant = int(unique_nz[idx])
        overlap_ratio = float(counts_nz[idx]) / float(mask_size)

        if overlap_ratio > overlap_min:
            rejected_new.add(dominant)
        else:
            dropped.append(old_iid)

    return (sorted(rejected_new), sorted(set(dropped)))


# ---------------------------------------------------------------------------
# Public: compact_instance_ids — dense 1..N для deliverable (v1.16.0)
# ---------------------------------------------------------------------------

def compact_instance_ids(labels: "np.ndarray", groups: Optional[list] = None):
    """
    Переномеровує instance id у щільні **1..N без пропусків** для deliverable.

    == Навіщо ==
    Робоче `selected/<model>/npy` використовує reserved-range схему
    (sparse: 1..~7000 модель + 50000+ полігони) — стабільні id, щоб групи
    не дрейфували між bake. АЛЕ замовнику instance-маска має йти у
    стандартному вигляді: id від 1 до N, всі присутні, без стрибків
    (це конвенція instance-масок; багато downstream-тулів припускають
    `max(label) == num_instances`).

    Ця функція робить фінальне ущільнення ЛИШЕ при export/finalize/pack —
    робоче selected/ лишається sparse.

    == Чому це безпечно (не повертає стару next_id колізію) ==
    Стара колізія була крос-файловою: `cleanup.rejected` (raw space) міг
    випадково = новому polygon id. Тут інакше:
    - npy + groups.instance_ids переномеровуються РАЗОМ, тим самим remap;
    - `cleanup.rejected` НЕ чіпається (відхилені вже видалені з npy при
      bake, їх немає у фінальній масці — нічого мапити);
    - remap детермінований: old id сортуються зростаюче → 1,2,3,...
      (порядок зберігається, менші old → менші new).

    Args:
        labels : np.ndarray (H,W) int — sparse instance mask (working).
        groups : optional list[dict] — group payload; кожен
                 `instance_ids` переномеровується тим самим remap
                 (in-place + повертається у dense_groups).

    Returns:
        (dense_labels, remap):
          dense_labels : np.ndarray same shape, dtype — id 1..N (0=фон).
          remap        : dict {old_id: new_id} (включає 0→0).

    Pure-ish (мутує переданий groups in-place для зручності; повертає теж).
    """
    if labels is None:
        return labels, {0: 0}
    uniq = np.unique(labels)
    pos = sorted(int(u) for u in uniq if u > 0)
    remap: dict = {0: 0}
    for new_id, old_id in enumerate(pos, start=1):
        remap[old_id] = new_id

    maxid = int(labels.max()) if labels.size else 0
    # LUT-перетворення (швидке): old id < 65536, тому LUT масив дешевий.
    lut = np.zeros(maxid + 1, dtype=labels.dtype)
    for old_id, new_id in remap.items():
        if 0 <= old_id <= maxid:
            lut[old_id] = new_id
    dense_labels = lut[labels]

    if groups:
        for g in groups:
            if not isinstance(g, dict):
                continue
            iids = g.get("instance_ids")
            if isinstance(iids, list):
                mapped = sorted({
                    remap[int(i)] for i in iids
                    if _safe_int_in(i, remap) and remap[int(i)] > 0
                })
                g["instance_ids"] = mapped

    return dense_labels, remap


def _safe_int_in(val, remap: dict) -> bool:
    try:
        return int(val) in remap
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _strip_orphans_in_groups_file(
    cfg: "Config",
    stem: str,
    model_name: str,
) -> int:
    """
    Self-heal крок: читає groups/<stem>.json + NEW filtered npy, strip-ає
    сирітські instance_ids з усіх груп, атомарно перезаписує файл.

    Це **те саме** що робить GET /api/groups in-memory, але напряму на
    диску. Потрібно для bake_all (batch-bake без UI) щоб groups.json
    лишився чистим без orphan-iid.

    Args:
        cfg          : Config.
        stem         : ім'я фото.
        model_name   : ім'я моделі (для шляху до filtered npy).

    Returns:
        Кількість strip-нутих iid (сума по всіх групах). 0 якщо файл
        не існує або нічого не strip-нуто.

    Не падає при відсутності файлів — просто повертає 0.
    """
    if not cfg.groups_dir:
        return 0
    groups_path = cfg.groups_dir / f"{stem}.json"
    npy_path = cfg.selected_dir / model_name / "npy" / f"{stem}.npy"
    if not groups_path.exists() or not npy_path.exists():
        return 0

    try:
        labels = np.load(str(npy_path))
        while labels.ndim > 2:
            labels = labels[0]
        known_iids = {int(i) for i in np.unique(labels) if int(i) > 0}
    except Exception:
        return 0

    if not known_iids:
        # NEW npy порожній — strip видалив би все. Не чіпаємо (захист).
        return 0

    try:
        with open(groups_path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        return 0

    log = _strip_orphan_instance_ids(groups, known_iids)
    if not log:
        return 0

    removed_count = sum(len(entry.get("removed") or []) for entry in log)
    if removed_count > 0:
        _atomic_write_json(groups_path, payload)
    return removed_count


def _sync_cleanup_json_after_bake(
    cfg: "Config",
    stem: str,
    model_name: str,
    rejected: list,
) -> None:
    """F-004: записати `selected/<model>/cleanup.json[stem].rejected` = rejected цього
    bake, щоб provenance у finalize-ZIP завжди відповідав фактично запеченому стану.

    Контекст: live-SoT rejected — `selections.json`; `cleanup.json` — per-model
    deliverable-snapshot, що раніше писався ЛИШЕ через `/api/cleanup-export` (🔥).
    У звичайному lazy-bake flow (autosave + Save All / Finalize) він був
    відсутній/застарілий у ZIP. Маски коректні (npy уже baked), але provenance
    розходилось — цей крок його вирівнює на КОЖЕН bake (choke point).

    Зберігає наявні `markers` (через `markers=None` у `_write_cleanup_json`) і `user`
    (читає з поточного `cleanup.json`, дефолт "baked"). Provenance-крок —
    некритичний → caller обгортає у try/except, bake не валиться.
    """
    rej = sorted({int(i) for i in (rejected or [])})
    user = "baked"
    cleanup_path = cfg.selected_dir / model_name / "cleanup.json"
    try:
        if cleanup_path.exists():
            with open(cleanup_path, "r", encoding="utf-8") as f:
                prev = (json.load(f) or {}).get(stem) or {}
            if isinstance(prev, dict) and isinstance(prev.get("user"), str) and prev["user"]:
                user = prev["user"]
    except Exception:
        pass
    _write_cleanup_json(cfg.selected_dir, model_name, stem, rej, user, markers=None)
