"""
baking.py — bake polygons + cleanup → запечені маски для замовника.

== Що це ==
Центральний модуль для "запікання" (bake): береться raw instance mask
від моделі (output/<model>/npy), фільтрується по cleanup.rejected,
накладаються polygon-shape з polygons.json, отримуємо filtered
labels.npy + 16-bit png + YOLO bbox + overlay PNG. Це фінальний дата-
продукт, який ми віддаємо команді і замовнику.

Орчестратор — `_bake_polygons_to_selected`. Виклик з 4 точок (через
обгортку `data_sync.bake_with_resync`):
- POST /api/polygons-export — Save Polygons + bake (UI кнопка).
- POST /api/rebake — пересічна перепікання після Pick (Day 7).
- POST /api/workspace/bake-all — фоновий batch (UI "Зберегти все").
- GET  /api/workspace/finalize/<stem> — стрим ZIP з останнім bake.

== Файли на диску ==
SoT input (read-only):
- output/<model>/npy/<stem>.npy            — raw instance ID від моделі.
- polygons/<stem>.json                     — LabelMe shapes.
- selected/<model>/cleanup.json            — rejected_instances per stem.

SoT output (writable):
- selected/<model>/npy/<stem>.npy          — filtered/baked instance ID.
- selected/<model>/png/<stem>.png          — 16-bit PNG mirror.
- selected/<model>/yolo/<stem>.txt         — bbox per instance + class_id.
- selected/<model>/overlay/<stem>.png      — colored overlay для перегляду.

Optional output (при export_derived_masks):
- selected/<model>/semantic/<stem>.png     — per-pixel class label {0,1,2}.
- selected/<model>/mask_groups/<stem>.png  — per-pixel group id (group fill).
- selected/<model>/overlays/<stem>__<label>.png — per-label overlays.

== Що пише сюди ==
- `_bake_polygons_to_selected` — головний bake pipeline.
- `_sync_groups_instance_ids_after_bake` — після bake дописує polygon-resolved
  iid у `group.instance_ids` (Bug 3 fix, v1.13.0+v1.13.1 authoritative resolve).
- `export_derived_masks` — semantic + mask_groups при ZIP export.
- `export_per_label_overlays` — per-label overlays при ZIP export.

== Хто читає ==
- Замовник (clusterization.py): npy + semantic + groups.json.
- Frontend (через /api/labels-rgb): не читає filtered, тільки raw.
- audit_export.py: усі формати.

== Invariants (баking-relevant) ==
- I4 strict: один iid не може бути у двох групах. Гарантується
  `_sync_groups_instance_ids_after_bake` (v1.13.1 authoritative resolve).
- Bug 3 closed: polygon-shape у group.polygon_indices після bake
  отримує iid у group.instance_ids (set union, idempotent).
- B3 lazy: orphan iid у groups після bake — self-heal у
  `data_sync.bake_with_resync._strip_orphans_in_groups_file`.

== Залежності ==
state.py (Config, _atomic_write_json, np), cleanup.py (backup helpers),
polygons.py (_load_labels). НЕ залежить від data_sync (data_sync залежить
від нього — для уникнення circular import).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PIL import Image

from state import (
    CV2_AVAILABLE,
    Config,
    _atomic_write_json,
    _load_label_classes,
    _load_original_image_array,
    cv2,
    draw_overlay,
    export_segmentation_bundle,
    np,
)
from cleanup import (
    BACKUP_KEEP,
    _RGB_CACHE,
    _RGB_CACHE_LOCK,
    _backup_dir_for_stem,
    _make_backup,
    _rotate_backups,
)
from polygons import _load_labels


# ---------------------------------------------------------------------------
# Reserved ID range для polygon-shapes (v1.16.0 foundational fix)
# ---------------------------------------------------------------------------
# Корінь desync-багів (Bug 3/4/7/14, I5): polygon-shapes раніше отримували
# `next_id = max(cleaned)+1`, що могло збігтися з raw instance id, який юзер
# щойно reject-нув (next_id collision). Тоді "прибрана" клітина і
# "намальований" полігон ділили один номер → плутанина по всіх інваріантах.
#
# Рішення: polygon-shapes завжди беруть номери з ЗАРЕЗЕРВОВАНОГО високого
# діапазону, який ніколи не перетинається з raw model output id.
#
# Виміряно на vesicles_good: max raw id = 6931 (усі моделі), max
# instance/фото = 4146. uint16 стеля (PNG/mask_groups) = 65535. Тобто
# діапазон [50000, 65535] вільний на 100% і дає 15535 слотів під полігони
# (спостережено ~234 baked polygons/фото; навіть агресивний multi-seed
# усіх везикул ≈ 4146 < 15535).
#
# polygon shape з index `i` у polygons.json.shapes → instance_id = BASE + i.
# Детерміновано, стабільно між bake, без колізій. Якщо raw id раптом
# перевищить BASE або shapes забагато (екстремальний випадок, не на наших
# даних) — graceful fallback на стару sequential-схему (max+1), з warning.
POLYGON_ID_BASE = 50000
POLYGON_ID_CEILING = 65000  # запас від uint16 стелі 65535


# ---------------------------------------------------------------------------
# YOLO multi-class writer (per-instance class_id)
# ---------------------------------------------------------------------------

def _write_yolo_multiclass(
    labels_arr: "np.ndarray",
    yolo_path: "Path",
    W: int,
    H: int,
    class_id_map: dict,
    default_class_id: int = 0,
) -> None:
    """YOLO bbox format з per-instance class_id."""
    yolo_path = Path(yolo_path)
    yolo_path.parent.mkdir(parents=True, exist_ok=True)
    instance_ids = [int(i) for i in np.unique(labels_arr) if i > 0]
    lines = []
    for inst_id in instance_ids:
        mask = labels_arr == inst_id
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        if len(rows) == 0 or len(cols) == 0:
            continue
        y1, y2 = int(rows[0]), int(rows[-1])
        x1, x2 = int(cols[0]), int(cols[-1])
        cx = (x1 + x2) / 2 / W
        cy = (y1 + y2) / 2 / H
        bw = (x2 - x1) / W
        bh = (y2 - y1) / H
        cid = class_id_map.get(inst_id, default_class_id)
        lines.append(f"{cid} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    with open(yolo_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Mask → polygon contours (CV2)
# ---------------------------------------------------------------------------

def _mask_to_polygons(labels, simplify_epsilon: float = 1.5,
                     min_area: int = 8, label: str = "nucleus") -> list[dict]:
    """
    Конвертує instance-маску в список LabelMe-шейпів через cv2.findContours.
    Кожен instance → один полігон (external contour, спрощений approxPolyDP).

    simplify_epsilon (pixels): тим більше, тим менше вершин. 1.5 — баланс для клітин.
    min_area (pixels²): ігноруємо тренькі контури.
    """
    if not CV2_AVAILABLE:
        raise RuntimeError("opencv-python не встановлено — seed-from-mask недоступний")
    shapes: list[dict] = []
    instance_ids = [int(i) for i in np.unique(labels) if int(i) != 0]
    for iid in instance_ids:
        mask = (labels == iid).astype(np.uint8) * 255
        # RETR_EXTERNAL — лише зовнішні контури. Якщо інстанс розірваний
        # (буває рідко) — беремо найбільший compoent.
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        if cv2.contourArea(cnt) < min_area:
            continue
        approx = cv2.approxPolyDP(cnt, simplify_epsilon, True)
        pts = approx.reshape(-1, 2).tolist()
        if len(pts) < 3:
            continue
        shapes.append({
            "label": label,
            "points": [[float(x), float(y)] for x, y in pts],
            "group_id": None,
            "shape_type": "polygon",
            "flags": {},
        })
    return shapes


# ---------------------------------------------------------------------------
# Polygon shapes → instance labels (bake)
# ---------------------------------------------------------------------------

def _bake_polygons_into_labels(
    labels: "np.ndarray",
    shapes: list[dict],
    W: int,
    H: int,
    label_class_ids: Optional[dict] = None,
    progress_cb=None,
) -> tuple:
    """
    Запікає polygon shapes у instance-label масив.

    Правила:
    - prefer polygon: пікселі полігонів перезаписують cellpose-інстанси.
    - prefer later: якщо два полігони перекриваються, пізніший перезаписує.
    - IoU > 60% між двома полігонами → skip пізнішого (дублікат).

    label_class_ids: {"nucleus": 0, "cell_body": 1, ...} — для YOLO multi-class.
    progress_cb: optional callable(done:int, total:int) — викликається у циклі
                 shapes для UI-прогресу (Day 7 Save All). Throttled кожні ~16.

    Returns:
        new_labels                 : np.ndarray int32
        baked_count                : int
        skipped_reasons            : list[{"shape_idx": int, "reason": str}]
        overlap_warnings           : list[{"i": int, "iou_pct": float, "note": str}]
        class_id_map               : dict[int, int]  instance_id → class_id
        shape_idx_to_instance_id   : dict[int, int]  position у `shapes` → отриманий
                                                     instance_id у baked_labels. Для
                                                     post-bake group sync (Bug 3 fix
                                                     v1.13.0). Включає лише shapes що
                                                     успішно запеклись (skipped — ні).
    """
    if not CV2_AVAILABLE:
        raise RuntimeError("opencv-python not installed — polygon baking unavailable")

    _label_class_ids = label_class_ids or {}

    new_labels = labels.copy().astype(np.int32)
    raw_max = int(np.max(new_labels)) if new_labels.size else 0
    # Reserved-range схема (v1.16.0): polygon shape index `i` → BASE + i.
    # Активна якщо raw id не залазить у зарезервований діапазон І shapes
    # вміщаються під стелю. Інакше — graceful fallback на legacy sequential.
    use_reserved = (
        raw_max < POLYGON_ID_BASE
        and len(shapes) <= (POLYGON_ID_CEILING - POLYGON_ID_BASE)
    )
    if not use_reserved and shapes:
        print(f"[baking] reserved ID range недоступний "
              f"(raw_max={raw_max}, shapes={len(shapes)}) — "
              f"fallback на legacy next_id (можливі колізії)")
    next_id = raw_max + 1  # legacy fallback counter
    baked_count = 0
    skipped_reasons: list[dict] = []
    overlap_warnings: list[dict] = []
    class_id_map: dict = {}
    shape_idx_to_instance_id: dict = {}

    # v1.16.0 bake-time backstop (Bug 14): фіксуємо original pixel-count
    # кожного raw instance ДО запікання полігонів. Після — instance, який
    # полігони перекрили майже повністю (лишився дрібний фрагмент), стираємо
    # повністю. Це підстраховка для випадку коли polygon домалювали поверх
    # instance, але не reject-нули його (frontend auto-reject пропустив /
    # imported data) → інакше у final mask лишається напів-стертий «фантом».
    _orig_counts: dict = {}
    if shapes:
        _rids, _rcnts = np.unique(new_labels, return_counts=True)
        _orig_counts = {int(i): int(c) for i, c in zip(_rids, _rcnts) if i > 0}

    # claimed: об'єднання пікселів вже запечених полігонів (для overlap-детекції)
    claimed = np.zeros((H, W), dtype=bool)

    total_shapes = len(shapes)
    for idx, sh in enumerate(shapes):
        if progress_cb is not None and (idx % 16 == 0):
            try:
                progress_cb(idx, total_shapes)
            except Exception:
                pass  # progress — best-effort, не валимо bake
        pts = sh.get("points", [])
        if len(pts) < 3:
            skipped_reasons.append({"shape_idx": idx, "reason": "less_than_3_vertices"})
            continue

        pts_arr = np.array(
            [[int(round(float(x))), int(round(float(y)))] for x, y in pts],
            dtype=np.int32,
        )

        out_of_bounds = (
            np.any(pts_arr[:, 0] < 0) or np.any(pts_arr[:, 0] >= W) or
            np.any(pts_arr[:, 1] < 0) or np.any(pts_arr[:, 1] >= H)
        )

        canvas = np.zeros((H, W), dtype=np.uint8)
        cv2.fillPoly(canvas, [pts_arr], 1)
        canvas_bool = canvas.astype(bool)

        if not np.any(canvas_bool):
            skipped_reasons.append({"shape_idx": idx, "reason": "empty_after_clamp"})
            continue

        if out_of_bounds:
            overlap_warnings.append({"i": idx, "iou_pct": 0.0,
                                     "note": "out_of_bounds_clamped"})

        # Overlap з уже запеченими полігонами
        overlap_px = int(np.sum(canvas_bool & claimed))
        if overlap_px > 0:
            union_px = int(np.sum(canvas_bool | claimed))
            iou = overlap_px / union_px if union_px > 0 else 0.0
            if iou > 0.60:
                skipped_reasons.append({
                    "shape_idx": idx,
                    "reason": f"overlap_over_60pct (IoU={iou:.0%})",
                })
                continue
            overlap_warnings.append({
                "i": idx,
                "iou_pct": round(iou * 100, 1),
                "note": "partial_overlap",
            })

        # Reserved-range: shape index → BASE + idx (детерміновано, без колізій
        # з raw id). Fallback: legacy sequential next_id (екстремальні дані).
        if use_reserved:
            poly_id = POLYGON_ID_BASE + idx
        else:
            poly_id = next_id
            next_id += 1
        new_labels[canvas_bool] = poly_id
        claimed |= canvas_bool
        shape_label = str(sh.get("label") or "nucleus")
        class_id_map[poly_id] = _label_class_ids.get(shape_label, 0)
        shape_idx_to_instance_id[idx] = poly_id
        baked_count += 1

    # bake-time backstop (v1.16.0; поріг вирівняно 0.15→0.50 у audit-fix F-011):
    # raw instance, від якого після запікання полігонів лишилось < 50% оригіналу
    # (тобто полігони перекрили > 50%), стираємо повністю. Це ВИРІВНЮЄ bake з UI:
    # frontend `_polyCoveredInstances` ховає/блокує instance при покритті > 0.5,
    # тож раніше інстанси 50–85% перекриті «зникали» у редакторі, але виживали у
    # npy як огризок (F-011: виміряно 75 на data/vesicles_good). Тепер deliverable
    # = те, що бачив анотатор. Інстанси з ≤50% перекриттям (легкий зачіп сусіда
    # великим полігоном) лишаються. Vectorized — 1 додатковий np.unique scan.
    if _orig_counts:
        new_ids, new_cnts = np.unique(new_labels, return_counts=True)
        new_count_map = {int(i): int(c) for i, c in zip(new_ids, new_cnts)}
        phantoms = [
            rid for rid, orig in _orig_counts.items()
            if 0 < new_count_map.get(rid, 0) < 0.50 * orig
        ]
        if phantoms:
            new_labels[np.isin(new_labels, phantoms)] = 0

    return (new_labels, baked_count, skipped_reasons, overlap_warnings,
            class_id_map, shape_idx_to_instance_id)


# ---------------------------------------------------------------------------
# Bake orchestrator (shared between /api/polygons-export and /api/workspace/finalize)
# ---------------------------------------------------------------------------

def _bake_polygons_to_selected(
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
    Спільна bake-логіка для api_polygons_export і api_workspace_finalize.

    1. Load instance labels з .npy → filter rejected → cleaned
    2. Compute label_class_ids + default_class_id (з base_label)
    3. _bake_polygons_into_labels
    4. Optional backup selected/<model>/<stem> (з ротацією)
    5. export_segmentation_bundle (npy/png/yolo + overlay if image present)
    6. Pop _RGB_CACHE for (model, stem)

    progress_cb: optional callable(phase:str, frac:float) — для UI-прогресу
                 у Save All (Day 7). phase — людський опис фази, frac ∈ [0,1].

    Returns dict з полями: ok, baked_count, skipped_reasons, overlap_warnings,
    base_label, base_class_id, backup (Path or None), errors (list).
    """
    def _emit(phase: str, frac: float) -> None:
        if progress_cb is not None:
            try:
                progress_cb(phase, frac)
            except Exception:
                pass

    _emit("Завантаження масок", 0.04)
    labels = _load_labels(src_npy)
    if rejected:
        cleaned = labels.copy()
        cleaned[np.isin(cleaned, [int(i) for i in rejected])] = 0
    else:
        cleaned = labels

    H, W = int(cleaned.shape[0]), int(cleaned.shape[1])

    classes = _load_label_classes(cfg.labels_file)
    label_class_ids = {c["name"]: i for i, c in enumerate(classes)}
    default_class_id = (
        label_class_ids.get(str(base_label), 0) if base_label else 0
    )

    # Прогрес циклу полігонів мапимо у діапазон 0.08..0.55.
    def _shape_progress(done: int, total: int) -> None:
        frac = 0.08 + (0.47 * done / total if total else 0.47)
        _emit(f"Запікання полігонів {done}/{total}", frac)

    (baked_labels, baked_count, skipped_reasons, overlap_warnings,
     class_id_map, shape_idx_to_iid) = _bake_polygons_into_labels(
        cleaned, shapes, W, H, label_class_ids,
        progress_cb=_shape_progress if shapes else None,
    )

    backup_root = None
    if do_backup:
        _emit("Резервна копія", 0.58)
        backup_root = _make_backup(cfg.selected_dir, model_name, stem)
        _rotate_backups(
            _backup_dir_for_stem(cfg.selected_dir, model_name, stem),
            keep=BACKUP_KEEP,
        )

    selected_model_dir = cfg.selected_dir / model_name
    image_arr = _load_original_image_array(cfg.images_dir, stem)
    formats = ["npy", "png", "yolo"]
    if image_arr is not None:
        formats.append("overlay")

    def _multiclass_yolo_writer(mask, output_txt_path, image_size, silent):
        width, height = image_size
        _write_yolo_multiclass(
            mask,
            Path(output_txt_path),
            width,
            height,
            class_id_map,
            default_class_id=default_class_id,
        )
        return True

    _emit("Експорт NPY / PNG / YOLO / overlay", 0.65)
    export_result = export_segmentation_bundle(
        baked_labels,
        selected_model_dir,
        stem,
        image=image_arr,
        image_size=(W, H),
        export_formats=formats,
        yolo_writer=_multiclass_yolo_writer,
        silent=True,
    )

    with _RGB_CACHE_LOCK:
        _RGB_CACHE.pop((model_name, stem), None)

    # Bug 3 fix (v1.13.0): після bake, polygon-shape що залишена у
    # `group.polygon_indices` отримала свіжий `instance_id` (з shape_idx_to_iid).
    # Дописуємо ці iid у `group.instance_ids` (set union → idempotent) і
    # атомарно перезаписуємо `groups/<stem>.json`. Без цього downstream-код,
    # що шукає ядро/везикулу лише у `group.instance_ids`, втрачає всі
    # polygon-only групи (12.4% cells на vesicles_good_2026-05).
    groups_sync_added = 0
    if shape_idx_to_iid:
        try:
            _emit("Sync groups", 0.92)
            groups_sync_added = _sync_groups_instance_ids_after_bake(
                cfg, stem, shape_idx_to_iid,
            )
        except Exception as exc:
            # Не валимо bake — це додатковий sync, основний результат вже на диску.
            print(f"[baking] groups sync failed for {stem}: {exc}")

    _emit("Готово", 1.0)

    result = _export_result_dict(
        export_result, baked_count, skipped_reasons, overlap_warnings,
        base_label, default_class_id, backup_root,
    )
    result["groups_sync_added"] = groups_sync_added
    return result


def _sync_groups_instance_ids_after_bake(
    cfg: "Config",
    stem: str,
    shape_idx_to_iid: dict,
) -> int:
    """
    Sync polygon→instance mapping назад у `groups/<stem>.json`.

    **Authoritative resolve** (v1.13.1 hotfix): polygon-shape, що належить
    групі через `polygon_indices`, є **джерелом істини** для свого
    resolved instance_id. Тобто:

      1. Будуємо мапу `iid_owner_gid` = {resolved_iid → gid поточної групи}.
      2. Для кожної групи:
         a) Видаляємо з її `instance_ids` усі iid, чий owner — **інша** група
            (legacy збіг номера: `next_id = max(npy)+1` міг повторно зайняти
            iid, що раніше був у іншій групі через lasso/manual).
         b) Додаємо iid, чий owner — **ця** група (через polygon_indices).

    Це гарантує invariant I4 ("instance_ids unique across groups") після
    bake для **усіх** polygon-resolved iid. Manual iid, доданий юзером
    через lasso/click на baked-instance (без polygon-shape), не у
    `iid_owner_gid` → не чіпаємо.

    Симптом B (lasso додає форму як baked_instance + polygon-shape окремо)
    — окремий код-шлях (Bug 4, Phase 2 desync audit), цей fix його не
    змінює.

    Idempotence: повторний bake з тим же `shape_idx_to_iid` → ті ж
    owner-mapping → стан конвергує. Manual iid (поза polygon-resolve)
    збережений.

    Атомарний запис через `_atomic_write_json` (захист від concurrent
    autosave на Flask threaded).

    Args:
        cfg              : Config (потребує `cfg.groups_dir`).
        stem             : ім'я фото.
        shape_idx_to_iid : {polygon_index у polygons.json.shapes → baked iid}.

    Returns:
        Кількість iid-діфів (додані + видалені) — метрика для result dict,
        тестів, прогрес-індикатора.
    """
    if not cfg.groups_dir:
        return 0
    groups_path = cfg.groups_dir / f"{stem}.json"
    if not groups_path.exists():
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

    # 1. Build authoritative owner-mapping (resolved_iid → owner_gid).
    # Якщо polygon_index фігурує у кількох групах (rare; формально це
    # порушення single-membership на polygon-індексі — закриває POST через
    # _enforce_single_membership, last-wins) — береться остання згадка
    # за порядком списку.
    iid_owner_gid: dict = {}
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        gid = grp.get("id")
        if not isinstance(gid, str):
            continue
        for pi in (grp.get("polygon_indices") or []):
            try:
                pi_int = int(pi)
            except (TypeError, ValueError):
                continue
            iid = shape_idx_to_iid.get(pi_int)
            if iid is None:
                continue
            try:
                iid_owner_gid[int(iid)] = gid
            except (TypeError, ValueError):
                continue

    if not iid_owner_gid:
        return 0

    # iid-и, у які резолвляться полігони ЦЬОГО bake (для усіх shapes, незалежно
    # від членства у групах). Reserved-scheme: BASE+idx; fallback: next_id.
    # Використовується щоб відрізнити «полігон-resolved iid без групи» (stale →
    # зняти) від легітимного старого/manual iid (зберегти, навіть якщо >= BASE
    # через fallback next_id — idempotence union).
    poly_iids_this_bake: set = set()
    for v in shape_idx_to_iid.values():
        try:
            poly_iids_this_bake.add(int(v))
        except (TypeError, ValueError):
            continue

    # 2. Apply authoritative resolve per group.
    total_diff = 0
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        gid = grp.get("id")
        existing: set = set()
        for iid in (grp.get("instance_ids") or []):
            try:
                existing.add(int(iid))
            except (TypeError, ValueError):
                continue
        new_set = set(existing)
        # 2a. Зняти iid, чий owner — інша група (legacy collision).
        for iid in list(existing):
            owner = iid_owner_gid.get(iid)
            if owner is not None and owner != gid:
                new_set.discard(iid)
            elif owner is None and iid in poly_iids_this_bake:
                # iid — полігон-resolved ЦЬОГО bake, але його полігон не в жодній
                # групі (юзер прибрав його з polygon_indices) → stale. Знімаємо:
                # інакше пікселі полігона дістануть group_id цієї групи у
                # mask_groups, і класифікація рахує «привид» (db_img_0171 g_008).
                # NB: перевіряємо членство у shape_idx_to_iid.values(), а НЕ
                # `iid >= POLYGON_ID_BASE` — fallback next_id теж дає великі iid,
                # але старий polygon-iid (re-bake) має лишитись (idempotence union).
                # Manual model-iid (lasso/click на baked-instance) не polygon-
                # resolved → не у poly_iids_this_bake → не чіпаємо.
                new_set.discard(iid)
        # 2b. Додати iid, чий owner — ця група.
        for iid, owner in iid_owner_gid.items():
            if owner == gid:
                new_set.add(iid)
        if new_set != existing:
            grp["instance_ids"] = sorted(new_set)
            total_diff += len(new_set ^ existing)  # symm diff = added + removed

    if total_diff > 0:
        _atomic_write_json(groups_path, payload)
    return total_diff


def _export_result_dict(export_result, baked_count, skipped_reasons,
                        overlap_warnings, base_label, default_class_id,
                        backup_root) -> dict:
    """Зведення результату _bake_polygons_to_selected (винесено для читабельності)."""
    return {
        "ok": not export_result["errors"],
        "baked_count": baked_count,
        "skipped_reasons": skipped_reasons,
        "overlap_warnings": overlap_warnings,
        "base_label": base_label,
        "base_class_id": default_class_id,
        "backup": backup_root,
        "errors": list(export_result["errors"]),
    }


# ---------------------------------------------------------------------------
# Day 8 — per-label overlays (overlay-PNG по кожному класу при export)
# ---------------------------------------------------------------------------

def export_per_label_overlays(cfg: "Config", model_name: str, stem: str,
                              out_dir) -> list:
    """
    Рендерить overlay-PNG окремо для кожного класу одного stem.

    Клас кожного instance відновлюється з `selected/<model>/yolo/<stem>.txt`:
    рядки YOLO йдуть у тому самому порядку, що `sorted(np.unique(labels))` —
    так пише `_write_yolo_multiclass`. Тобто рядок i ↔ instance_ids[i].

    Файли: `<out_dir>/<stem>__<label>.png` (overlay з червоними межами на
    оригіналі, але лише для instance цього класу).

    Повертає list[Path] створених файлів; [] якщо даних бракує або mismatch.
    """
    if draw_overlay is None or np is None:
        return []
    npy_path = cfg.selected_dir / model_name / "npy" / f"{stem}.npy"
    yolo_path = cfg.selected_dir / model_name / "yolo" / f"{stem}.txt"
    if not npy_path.exists() or not yolo_path.exists():
        return []

    labels = np.load(str(npy_path))
    while labels.ndim > 2:
        labels = labels[0]
    inst_ids = [int(i) for i in np.unique(labels) if i > 0]
    if not inst_ids:
        return []

    yolo_lines = [
        ln for ln in yolo_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if len(yolo_lines) != len(inst_ids):
        # Розбіжність кількості — не ризикуємо хибним мапінгом instance↔class.
        return []

    classes = _load_label_classes(cfg.labels_file)
    cid_to_name = {i: c["name"] for i, c in enumerate(classes)}
    by_class: dict[str, list[int]] = {}
    for inst_id, line in zip(inst_ids, yolo_lines):
        try:
            cid = int(line.split()[0])
        except (ValueError, IndexError):
            continue
        name = cid_to_name.get(cid, f"class{cid}")
        by_class.setdefault(name, []).append(inst_id)
    if not by_class:
        return []

    image = _load_original_image_array(cfg.images_dir, stem)
    if image is None:
        return []

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list = []
    for name, ids in by_class.items():
        masked = np.where(np.isin(labels, ids), labels, 0)
        dst = out_dir / f"{stem}__{name}.png"
        if draw_overlay(image, masked, str(dst), silent=True):
            written.append(dst)
    return written


# ---------------------------------------------------------------------------
# Derived masks — semantic (per-label) + group-instance (2026-05-22)
# ---------------------------------------------------------------------------

def export_derived_masks(cfg: "Config", model_name: str, stem: str,
                         out_dir) -> list:
    """
    Генерує 2 похідні маски одного фото з ЗАПЕЧЕНОЇ розмітки. Опційно
    додаються у finalize/export ZIP (галочка «Маски» у вікні Експорту).

    1. `semantic/<stem>.png` — 8-біт PNG. Фон 0; клас кожного instance:
       nucleus=1, vesicle=2 (class_id+1 з YOLO multiclass, порядок labels.json).
       Якщо к-сть YOLO-рядків ≠ к-сть instance — semantic пропускається (не
       ризикуємо хибним мапінгом instance↔class, як у export_per_label_overlays).
    2. `mask_groups/<stem>.png` — 16-біт PNG. Фон 0; кожна група з
       `groups/<stem>.json` залита своїм номером (1..N у порядку файлу).
       Включає й polygon_indices: shape з `polygons/<stem>.json` рендериться
       у gmask, але лише на бекграунді (не overwrite instance іншої групи).
       Instance поза будь-якою групою лишаються 0.

    Будується з `selected/<model>/{npy,yolo}` + `groups/<stem>.json` — тобто
    з фінального запеченого стану. Викликати ПІСЛЯ bake.

    Повертає list[Path] створених файлів ([] якщо даних бракує). Кожен шлях —
    `<out_dir>/<semantic|mask_groups>/<stem>.png`.
    """
    if np is None:
        return []
    npy_path = cfg.selected_dir / model_name / "npy" / f"{stem}.npy"
    if not npy_path.exists():
        return []
    labels = np.load(str(npy_path))
    while labels.ndim > 2:
        labels = labels[0]
    inst_ids = [int(i) for i in np.unique(labels) if int(i) > 0]
    if not inst_ids:
        return []
    max_id = int(labels.max())
    out_dir = Path(out_dir)
    written: list = []

    # --- 1. Semantic mask (8-біт: фон 0, nucleus 1, vesicle 2) ---
    yolo_path = cfg.selected_dir / model_name / "yolo" / f"{stem}.txt"
    if yolo_path.exists():
        try:
            yolo_lines = [
                ln for ln in yolo_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
        except Exception:
            yolo_lines = []
        # Рядок i ↔ inst_ids[i] (sorted np.unique) — як пише _write_yolo_multiclass.
        if len(yolo_lines) == len(inst_ids):
            sem_lut = np.zeros(max_id + 1, dtype=np.uint8)
            for iid, line in zip(inst_ids, yolo_lines):
                try:
                    cid = int(line.split()[0])
                except (ValueError, IndexError):
                    continue
                if 0 <= cid <= 254:
                    sem_lut[iid] = cid + 1
            sem = sem_lut[labels]            # vectorized LUT — один прохід
            sem_dir = out_dir / "semantic"
            sem_dir.mkdir(parents=True, exist_ok=True)
            sem_path = sem_dir / f"{stem}.png"
            Image.fromarray(sem, mode="L").save(str(sem_path), format="PNG")
            written.append(sem_path)

    # --- 2. Group-instance mask (16-біт: фон 0, кожна група свій номер) ---
    groups_path = (cfg.groups_dir / f"{stem}.json") if cfg.groups_dir else None
    if groups_path is not None and groups_path.exists():
        try:
            with open(groups_path, "r", encoding="utf-8-sig") as f:
                groups = (json.load(f) or {}).get("groups") or []
        except Exception:
            groups = []
        grp_lut = np.zeros(max_id + 1, dtype=np.uint16)
        for g_idx, grp in enumerate(groups, start=1):
            for iid in (grp.get("instance_ids") or []):
                try:
                    iid_int = int(iid)
                except (TypeError, ValueError):
                    continue
                if 0 < iid_int <= max_id:
                    grp_lut[iid_int] = g_idx
        gmask = grp_lut[labels]              # vectorized LUT

        # Polygon shapes у тих групах, що мають polygon_indices — fill лише
        # на бекграунді (gmask == 0), щоб не overwrite уже залиті instance
        # тієї ж або іншої групи. Bug 1 fix (2026-05-26): UI counts
        # включають polygon_indices, а mask_groups.png раніше їх ігнорував.
        if CV2_AVAILABLE and cv2 is not None:
            polygons_path = (cfg.polygons_dir / f"{stem}.json") if cfg.polygons_dir else None
            if polygons_path is not None and polygons_path.exists():
                try:
                    with open(polygons_path, "r", encoding="utf-8-sig") as f:
                        shapes = (json.load(f) or {}).get("shapes") or []
                except Exception:
                    shapes = []
                if shapes:
                    H, W = gmask.shape
                    for g_idx, grp in enumerate(groups, start=1):
                        for pi in (grp.get("polygon_indices") or []):
                            try:
                                pi_int = int(pi)
                            except (TypeError, ValueError):
                                continue
                            if not (0 <= pi_int < len(shapes)):
                                continue
                            sh = shapes[pi_int]
                            if not isinstance(sh, dict):
                                continue
                            pts = sh.get("points") or []
                            if len(pts) < 3:
                                continue
                            try:
                                arr_pts = np.array(
                                    [[int(round(float(x))), int(round(float(y)))]
                                     for x, y in pts],
                                    dtype=np.int32,
                                )
                            except (TypeError, ValueError):
                                continue
                            poly_mask = np.zeros((H, W), dtype=np.uint8)
                            cv2.fillPoly(poly_mask, [arr_pts], 1)
                            bg_under_poly = (poly_mask > 0) & (gmask == 0)
                            if bg_under_poly.any():
                                gmask[bg_under_poly] = g_idx

        grp_dir = out_dir / "mask_groups"
        grp_dir.mkdir(parents=True, exist_ok=True)
        grp_path = grp_dir / f"{stem}.png"
        Image.fromarray(gmask).save(str(grp_path), format="PNG")  # uint16 → I;16
        written.append(grp_path)

    return written
