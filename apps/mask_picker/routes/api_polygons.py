"""
api_polygons.py — HTTP-маршрути для polygons + bake.

Endpoints:
  GET  /api/polygons/<stem>                — повертає LabelMe envelope.
  POST /api/polygons/<stem>                — autosave LabelMe shapes.
  POST /api/polygons-export/<stem>         — save + bake (drop-in
                                              `data_sync.bake_with_resync`).
  POST /api/polygons/<stem>/seed-from-mask — instance-маска → polygon contours.
  POST /api/polygons/<stem>/multi-seed     — bulk seed (Shift+S у UI).
  POST /api/rebake/<stem>                  — пересічна перепікання
                                              (`data_sync.bake_with_resync`).

Bake усюди через `data_sync.bake_with_resync` (v1.15.0 — drop-in
заміна `_bake_polygons_to_selected` + self-heal B3 strip orphan).
"""
from __future__ import annotations

import json
import traceback
from typing import Optional

from flask import Blueprint, jsonify, request

from state import (
    CLEANUP_AVAILABLE,
    CV2_AVAILABLE,
    Config,
    EXPORT_AVAILABLE,
    StateStore,
    _find_image_filename,
    _load_original_image_array,
    cv2,
    np,
)
from cleanup import _find_npy_for
from polygons import (
    POLYGON_BACKUP_KEEP,
    _backup_polygons,
    _labelme_envelope,
    _load_labels,
    _polygons_backup_dir,
    _polygons_path,
    _validate_polygons_payload,
    _write_polygons_json,
)
from baking import _bake_polygons_to_selected, _mask_to_polygons
from data_sync import bake_with_resync
from cleanup import _rotate_backups
from catalog import CatalogService


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_polygons", __name__)

    @bp.route("/api/polygons/<stem>", methods=["GET"])
    def api_polygons_get(stem: str):
        """
        Повертає polygons/<stem>.json як LabelMe-like JSON.
        Якщо файла немає — повертає порожній envelope (із shapes=[]) з розмірами
        оригінальної картинки (якщо знайдено).
        """
        if not cfg.polygons_dir:
            return jsonify({"error": "polygons_dir not configured"}), 503
        path = _polygons_path(cfg.polygons_dir, stem)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return jsonify(data)
            except Exception as e:
                return jsonify({"error": f"failed to read polygons: {e}"}), 500
        # Немає файла — повертаємо порожній envelope з розмірами оригіналу.
        H, W = 0, 0
        if CLEANUP_AVAILABLE:
            arr = _load_original_image_array(cfg.images_dir, stem)
            if arr is not None:
                H, W = int(arr.shape[0]), int(arr.shape[1])
        image_name = _find_image_filename(cfg.images_dir, stem)
        return jsonify(_labelme_envelope(stem, image_name, H, W, []))

    @bp.route("/api/polygons/<stem>", methods=["POST"])
    def api_polygons_set(stem: str):
        """
        Body: повний LabelMe-подібний JSON (shapes, imageHeight, imageWidth, ...).
        Перед перезаписом робить бекап, потім rotate keep=POLYGON_BACKUP_KEEP.
        """
        if not cfg.polygons_dir:
            return jsonify({"error": "polygons_dir not configured"}), 503
        body = request.get_json(force=True) or {}
        err = _validate_polygons_payload(body)
        if err:
            return jsonify({"error": err}), 400
        # Заповнимо imagePath якщо пустий
        if not body.get("imagePath"):
            body["imagePath"] = _find_image_filename(cfg.images_dir, stem) or f"{stem}.jpg"
        try:
            _backup_polygons(cfg.polygons_dir, stem)
            _rotate_backups(_polygons_backup_dir(cfg.polygons_dir, stem),
                            keep=POLYGON_BACKUP_KEEP)
            _write_polygons_json(cfg.polygons_dir, stem, body)
        except Exception as e:
            print(f"[polygons] write failed for {stem}: {e}")
            return jsonify({"error": f"write failed: {e}"}), 500
        # Day 7 lazy-bake: чистий save polygons JSON НЕ запікає у selected/.
        # Позначаємо stem dirty — bake відбудеться через Save All / Finalize.
        state.mark_dirty(stem)
        return jsonify({
            "ok": True,
            "shape_count": len(body.get("shapes") or []),
            "path": str(_polygons_path(cfg.polygons_dir, stem)),
            "dirty": state.is_dirty(stem),
        })

    @bp.route("/api/polygons-export/<stem>", methods=["POST"])
    def api_polygons_export(stem: str):
        """
        Зберігає polygons/<stem>.json + запікає shapes у selected/<model>/ rasters.

        Body: {
          LabelMe-подібний payload (shapes, imageHeight, imageWidth, ...),
          "model": "cyto2",                # яка модель selected
          "rejected_instances": [5, 12],   # для фільтрації перед baking
        }

        Якщо model / .npy не знайдено — тільки зберігає JSON, baking пропускається.
        """
        if not EXPORT_AVAILABLE:
            return jsonify({"error": "cellsegkit not installed"}), 503
        if not CV2_AVAILABLE:
            return jsonify({"error": "opencv-python not installed — baking unavailable"}), 503
        if not cfg.polygons_dir:
            return jsonify({"error": "polygons_dir not configured"}), 503

        body = request.get_json(force=True) or {}
        model_name = body.get("model")
        rejected = body.get("rejected_instances") or []
        shapes = body.get("shapes") or []

        # Валідація payload
        err = _validate_polygons_payload(body)
        if err:
            return jsonify({"error": err}), 400

        if not body.get("imagePath"):
            body["imagePath"] = _find_image_filename(cfg.images_dir, stem) or f"{stem}.jpg"

        # 1. Зберегти polygon JSON (завжди)
        try:
            _backup_polygons(cfg.polygons_dir, stem)
            _rotate_backups(_polygons_backup_dir(cfg.polygons_dir, stem),
                            keep=POLYGON_BACKUP_KEEP)
            _write_polygons_json(cfg.polygons_dir, stem, body)
        except Exception as e:
            return jsonify({"error": f"polygon JSON save failed: {e}"}), 500

        no_bake_resp = {
            "ok": True,
            "shape_count": len(shapes),
            "baked": False,
            "baked_count": 0,
            "skipped_reasons": [],
            "overlap_warnings": [],
        }

        # 2. Якщо нема моделі або shapes — тільки JSON
        if not model_name or not shapes:
            return jsonify({**no_bake_resp,
                            "warn": "no model or no shapes — JSON saved only"})

        m = next((mm for mm in cfg.models if mm.name == model_name), None)
        if not m:
            return jsonify({**no_bake_resp,
                            "warn": f"unknown model {model_name!r}, JSON saved only"})

        src_npy = _find_npy_for(m, stem)
        if not src_npy:
            return jsonify({**no_bake_resp,
                            "warn": f"no .npy for {model_name}/{stem}, JSON saved only"})

        # 3. Bake через спільний helper
        try:
            entry = state.get(stem) or {}
            base_label = (
                body.get("base_label")
                or body.get("model_label")
                or entry.get("base_label")
                or None
            )

            result = bake_with_resync(
                cfg, stem, model_name, src_npy, shapes,
                rejected=rejected, base_label=base_label, do_backup=True,
            )

            if result["errors"]:
                return jsonify({
                    "error": "export failed for formats: "
                             + ", ".join(result["errors"])
                }), 500

            # Day 7: bake пройшов — selected/ актуальний, dirty знято.
            state.clear_dirty(stem)
            return jsonify({
                "ok": True,
                "shape_count": len(shapes),
                "baked": True,
                "baked_count": result["baked_count"],
                "skipped_reasons": result["skipped_reasons"],
                "overlap_warnings": result["overlap_warnings"],
                "base_label": result["base_label"],
                "base_class_id": result["base_class_id"],
                "backup": str(result["backup"]) if result["backup"] else None,
                "orphan_iids_stripped": result.get("orphan_iids_stripped", 0),
            })

        except Exception as e:
            print(f"[polygons-export] {stem}/{model_name}: {e}")
            traceback.print_exc()
            return jsonify({"error": f"baking failed: {e}"}), 500

    @bp.route("/api/polygons/<stem>/seed-from-mask", methods=["POST"])
    def api_polygons_seed(stem: str):
        """
        Body: {
          "model": "cyto2",
          "simplify_epsilon": 1.5,   # optional
          "min_area": 8,             # optional
          "label": "cell"            # optional
        }
        Конвертує instance-маску обраної моделі у LabelMe-shapes (не зберігає —
        повертає payload який фронт може застосувати/скасувати).
        """
        if not CV2_AVAILABLE:
            return jsonify({
                "error": "opencv-python не встановлено — seed-from-mask недоступний"
            }), 503
        body = request.get_json(force=True) or {}
        model_name = body.get("model")
        if not model_name:
            return jsonify({"error": "model required"}), 400
        m = next((mm for mm in cfg.models if mm.name == model_name), None)
        if not m:
            return jsonify({"error": f"unknown model {model_name}"}), 400
        npy = _find_npy_for(m, stem)
        if not npy:
            return jsonify({"error": f"no .npy for {model_name}/{stem}"}), 404
        eps = float(body.get("simplify_epsilon", 1.5))
        min_area = int(body.get("min_area", 8))
        label = str(body.get("label") or "nucleus")
        # Опційний фільтр: тільки конкретні instance-ID (селективний seed).
        raw_ids = body.get("instance_ids")
        keep_ids: Optional[list[int]] = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list):
                return jsonify({"error": "instance_ids must be a list"}), 400
            try:
                keep_ids = sorted({int(x) for x in raw_ids if int(x) != 0})
            except Exception:
                return jsonify({"error": "instance_ids must contain integers"}), 400
        try:
            labels = _load_labels(npy)
            # Викидаємо rejected інстанси, щоб seed не тягнув клітини,
            # які юзер уже відхилив у cleanup.
            cur = state.get(stem) or {}
            cur_cleanup = cur.get("cleanup") or {}
            if cur_cleanup.get("model") == model_name:
                rej = cur_cleanup.get("rejected_instances") or []
                if rej:
                    drop = np.isin(labels, rej)
                    labels = labels.copy()
                    labels[drop] = 0
            # Якщо передано keep_ids — лишаємо лише ці інстанси.
            if keep_ids is not None:
                keep_mask = np.isin(labels, keep_ids)
                labels = np.where(keep_mask, labels, 0).astype(labels.dtype)
            shapes = _mask_to_polygons(labels, simplify_epsilon=eps,
                                        min_area=min_area, label=label)
        except Exception as e:
            print(f"[seed-from-mask] {stem}/{model_name}: {e}")
            return jsonify({"error": f"seed failed: {e}"}), 500

        H, W = int(labels.shape[0]), int(labels.shape[1])
        image_name = _find_image_filename(cfg.images_dir, stem)
        return jsonify({
            "ok": True,
            "envelope": _labelme_envelope(stem, image_name, H, W, shapes),
            "shape_count": len(shapes),
            "source_model": model_name,
        })

    @bp.route("/api/rebake/<stem>", methods=["POST"])
    def api_rebake(stem: str):
        """
        Rebake selected/<model>/<stem>.{npy,png,yolo,overlay} з поточних
        polygons/<stem>.json + state[stem].cleanup.rejected_instances +
        base_label. Викликається після /api/select, щоб закрити gotcha
        "pick = ready dataset" (Day 3a v2.0.0).

        Body (optional):
          {"model": "cyto2"}  — fallback на state[stem].model якщо не передано

        Safety-check: якщо shapes порожні І rejected порожні → skip без
        bake (повертає {"ok": true, "skipped": "no_data"}). Це означає
        що для першого Pick цього stem нічого не запікається — raw
        cellpose уже у selected/ через /api/select.

        Returns: {
          "ok": true,
          "skipped": "no_data" | null,
          "baked": bool,
          "baked_count": int,
          "shapes_count": int,
          "rejected_count": int,
          "overlap_warnings": list,
          "skipped_reasons": list,
          "base_label": str | null,
          "base_class_id": int,
        }
        """
        if not EXPORT_AVAILABLE:
            return jsonify({"error": "cellsegkit not installed"}), 503
        if not CV2_AVAILABLE:
            return jsonify({"error": "opencv-python not installed — baking unavailable"}), 503
        if not cfg.polygons_dir:
            return jsonify({"error": "polygons_dir not configured"}), 503

        body = request.get_json(silent=True) or {}
        entry = state.get(stem) or {}

        # Model fallback: body → state[stem].model
        model_name = body.get("model") or entry.get("model")
        if not model_name:
            return jsonify({"error": "model required (body or state)"}), 400
        m = next((mm for mm in cfg.models if mm.name == model_name), None)
        if not m:
            return jsonify({"error": f"unknown model {model_name}"}), 400

        # Load shapes з polygons/<stem>.json (може бути відсутнім)
        shapes: list = []
        poly_path = _polygons_path(cfg.polygons_dir, stem)
        if poly_path.exists():
            try:
                with open(poly_path, "r", encoding="utf-8") as f:
                    poly_data = json.load(f)
                shapes = poly_data.get("shapes") or []
            except Exception as e:
                return jsonify({"error": f"failed to read polygons: {e}"}), 500

        # Load rejected з state[stem].cleanup (тільки якщо та сама модель)
        cleanup = entry.get("cleanup") or {}
        rejected: list = []
        if cleanup.get("model") == model_name:
            rejected = list(cleanup.get("rejected_instances") or [])

        # Safety-check: нема чого пекти
        if not shapes and not rejected:
            return jsonify({
                "ok": True,
                "skipped": "no_data",
                "baked": False,
                "baked_count": 0,
                "shapes_count": 0,
                "rejected_count": 0,
                "overlap_warnings": [],
                "skipped_reasons": [],
                "base_label": None,
                "base_class_id": 0,
            })

        # Find raw .npy
        src_npy = _find_npy_for(m, stem)
        if not src_npy:
            return jsonify({
                "ok": False,
                "skipped": "no_npy",
                "error": f"no .npy for {model_name}/{stem}",
            }), 404

        base_label = entry.get("base_label") or None

        try:
            result = bake_with_resync(
                cfg, stem, model_name, src_npy, shapes,
                rejected=rejected, base_label=base_label, do_backup=True,
            )
            if result["errors"]:
                return jsonify({
                    "error": "export failed for formats: "
                             + ", ".join(result["errors"])
                }), 500
            state.clear_dirty(stem)
            return jsonify({
                "ok": True,
                "skipped": None,
                "baked": True,
                "baked_count": result["baked_count"],
                "shapes_count": len(shapes),
                "rejected_count": len(rejected),
                "overlap_warnings": result["overlap_warnings"],
                "skipped_reasons": result["skipped_reasons"],
                "base_label": result["base_label"],
                "base_class_id": result["base_class_id"],
                "orphan_iids_stripped": result.get("orphan_iids_stripped", 0),
            })
        except Exception as e:
            print(f"[rebake] {stem}/{model_name}: {e}")
            traceback.print_exc()
            return jsonify({"error": f"rebake failed: {e}"}), 500

    @bp.route("/api/polygons/<stem>/multi-seed", methods=["POST"])
    def api_polygons_multi_seed(stem: str):
        """
        Bulk multi-class seed (v1.6.0).

        Body: {
          "mappings": [{"label": "nucleus", "model": "cyto2"}, ...],
          "iou_threshold": 0.6,         # optional, default 0.6
          "simplify_epsilon": 1.5,      # optional
          "min_area": 8                 # optional
        }

        Для кожного mapping викликає внутрішній seed-логіку, об'єднує shapes.
        Якщо нова shape має IoU > threshold з уже доданою — skip.

        Returns: {
          "ok": true,
          "envelope": <LabelMe>,
          "shapes_added": N,
          "shapes_skipped_overlap": M,
          "per_mapping": [{"label", "model", "added", "skipped"}, ...]
        }
        """
        if not CV2_AVAILABLE:
            return jsonify({"error": "opencv-python не встановлено"}), 503

        try:
            body = request.get_json(force=True) or {}
            mappings = body.get("mappings")
            if not isinstance(mappings, list) or not mappings:
                return jsonify({"error": "mappings: non-empty list required"}), 400

            iou_threshold = float(body.get("iou_threshold", 0.6))
            eps = float(body.get("simplify_epsilon", 1.5))
            min_area = int(body.get("min_area", 8))

            # Валідація mappings
            for i, mp in enumerate(mappings):
                if not isinstance(mp, dict):
                    return jsonify({"error": f"mappings[{i}]: must be object"}), 400
                if not mp.get("label") or not mp.get("model"):
                    return jsonify({"error": f"mappings[{i}]: 'label' and 'model' required"}), 400
                m = next((mm for mm in cfg.models if mm.name == mp["model"]), None)
                if not m:
                    return jsonify({"error": f"unknown model {mp['model']}"}), 400

            # v2.0.0 Day 3c′ — vectorized IoU. Раніше — O(N²) per-pixel
            # np.sum для кожної пари accepted×новий; на 200+ shapes
            # (instanseg vesicles + yolo nucleus) handler hang-нув на
            # хвилини. Тепер тримаємо acceptes як один int32-label map
            # і беремо unique IDs під новим polygon-ом → O(N·H·W).
            all_shapes: list[dict] = []
            per_mapping = []
            H = W = None
            cur = state.get(stem) or {}
            cur_cleanup = cur.get("cleanup") or {}
            cur_rejected_for_model: dict[str, list[int]] = {}
            if cur_cleanup.get("model"):
                cur_rejected_for_model[cur_cleanup["model"]] = (
                    cur_cleanup.get("rejected_instances") or []
                )

            accepted_map: Optional["np.ndarray"] = None  # int32, 0 = empty
            accepted_areas: dict[int, int] = {}
            next_accepted_id = 1

            for mp in mappings:
                label = str(mp["label"])
                model_name = str(mp["model"])
                m = next(mm for mm in cfg.models if mm.name == model_name)
                npy = _find_npy_for(m, stem)
                if not npy:
                    per_mapping.append({"label": label, "model": model_name,
                                        "added": 0, "skipped": 0,
                                        "error": "no .npy"})
                    continue
                try:
                    labels_arr = _load_labels(npy)
                    rej = cur_rejected_for_model.get(model_name) or []
                    if rej:
                        labels_arr = labels_arr.copy()
                        labels_arr[np.isin(labels_arr, rej)] = 0
                    shapes = _mask_to_polygons(labels_arr, simplify_epsilon=eps,
                                               min_area=min_area, label=label)
                except Exception as e:
                    per_mapping.append({"label": label, "model": model_name,
                                        "added": 0, "skipped": 0,
                                        "error": str(e)})
                    continue

                if H is None:
                    H, W = int(labels_arr.shape[0]), int(labels_arr.shape[1])
                    accepted_map = np.zeros((H, W), dtype=np.int32)

                added = 0
                skipped = 0
                for sh in shapes:
                    pts = sh.get("points") or []
                    if len(pts) < 3:
                        continue
                    pts_arr = np.array(
                        [[int(round(float(x))), int(round(float(y)))] for x, y in pts],
                        dtype=np.int32,
                    )
                    canvas = np.zeros((H, W), dtype=np.uint8)
                    cv2.fillPoly(canvas, [pts_arr], 1)
                    canvas_bool = canvas.astype(bool)
                    new_area = int(canvas_bool.sum())
                    if new_area == 0:
                        continue
                    overlap_region = accepted_map[canvas_bool]
                    ids_under = overlap_region[overlap_region > 0]
                    overlapped = False
                    if ids_under.size:
                        unique_ids, counts = np.unique(ids_under, return_counts=True)
                        for uid, inter in zip(unique_ids.tolist(), counts.tolist()):
                            prev_area = accepted_areas.get(int(uid), 0)
                            union = prev_area + new_area - int(inter)
                            if union > 0 and (int(inter) / union) > iou_threshold:
                                overlapped = True
                                break
                    if overlapped:
                        skipped += 1
                        continue
                    all_shapes.append(sh)
                    accepted_map[canvas_bool] = next_accepted_id
                    accepted_areas[next_accepted_id] = new_area
                    next_accepted_id += 1
                    added += 1
                per_mapping.append({"label": label, "model": model_name,
                                    "added": added, "skipped": skipped})

            if H is None:
                return jsonify({"error": "no model produced shapes"}), 404

            image_name = _find_image_filename(cfg.images_dir, stem)
            return jsonify({
                "ok": True,
                "envelope": _labelme_envelope(stem, image_name, H, W, all_shapes),
                "shapes_added": sum(p["added"] for p in per_mapping),
                "shapes_skipped_overlap": sum(p["skipped"] for p in per_mapping),
                "per_mapping": per_mapping,
            })
        except Exception as e:
            print(f"[multi-seed] {stem}: {e}")
            traceback.print_exc()
            return jsonify({"error": f"multi-seed failed: {e}"}), 500

    return bp
