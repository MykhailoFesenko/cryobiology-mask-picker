"""
Groups routes (v2.0.0 Day 4-5 cell grouping).

Endpoints:
  GET  /api/groups/<stem>?model=<m>   — envelope + per-group classification
  POST /api/groups/<stem>             — replace entire groups list (atomic)

Storage: groups/<stem>.json. На POST виконуються:
  1. _validate_groups_payload     — структурна валідація
  2. _enforce_single_membership   — кожен iid / pi у одній групі (last-wins)
  3. _classify_group_membership   — counts + suggested_type + soft valid
  4. _backup_groups + write       — atomic
  5. _sync_polygons_group_id_mirror — derived mirror у polygons.json
     (LabelMe-сумісність; mirror не source of truth)
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
    StateStore,
    _load_label_classes,
    cv2,
    np,
)
from cleanup import _rotate_backups
from baking import POLYGON_ID_BASE
from polygons import (
    _polygons_path,
    _write_polygons_json,
)
from groups import (
    GROUPS_BACKUP_KEEP,
    GROUPS_VERSION,
    _backup_groups,
    _classify_group_membership,
    _empty_groups_envelope,
    _enforce_single_membership,
    _groups_backup_dir,
    _instance_label_lookup,
    _instance_labels_from_polygons,
    _instance_labels_from_yolo,
    _migrate_groups_type_to_class_id,
    _polygon_labels_from_payload,
    _read_groups,
    _strip_orphan_instance_ids,
    _sync_polygons_group_id_mirror,
    _validate_groups_payload,
    _write_groups,
)
from group_classes import _read_classes
from catalog import CatalogService


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_groups", __name__)

    def _load_polygons_payload(stem: str) -> Optional[dict]:
        if not cfg.polygons_dir:
            return None
        path = _polygons_path(cfg.polygons_dir, stem)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return None

    def _resolve_npy_path(stem: str, model: Optional[str]):
        """
        Шукає baked npy у selected/<model>/npy/ (priority), потім raw
        output/<model>/npy/. Повертає Path або None.
        """
        if not model:
            return None
        # Priority: baked output (з cleanup applied)
        baked = cfg.selected_dir / model / "npy" / f"{stem}.npy"
        if baked.exists():
            return baked
        # Fallback: raw model output
        for m in cfg.models:
            if m.name == model and m.npy_dir:
                raw = m.npy_dir / f"{stem}.npy"
                if raw.exists():
                    return raw
        return None

    def _build_lookups(stem: str, model: Optional[str]) -> tuple:
        """Build (instance_labels, polygon_labels, polygons_payload, known_iids,
        labels_arr).

        2026-05-21 fix: для запеченої маски (`selected/<model>/yolo/<stem>.txt`
        присутній) лейбли інстансів читаються з YOLO-multiclass рядків —
        кожен baked instance отримує реальну лейблу (vesicle / nucleus / …),
        а не одну спільну `base_label`. Це робить cell-групи валідними коли
        везикули вже у базовій масці, без потреби малювати їх полігонами.

        Round 5: повертає ще `known_iids` (set реальних instance_id з npy) —
        для auto-strip orphan iid у group.instance_ids перед classify/write.
        """
        polygons_payload = _load_polygons_payload(stem)
        polygon_labels = _polygon_labels_from_payload(polygons_payload)

        instance_labels: dict = {}
        known_iids: set = set()
        labels_arr = None
        if CLEANUP_AVAILABLE and model:
            npy_path = _resolve_npy_path(stem, model)
            if npy_path is not None:
                try:
                    arr = np.load(str(npy_path))
                    while arr.ndim > 2:
                        arr = arr[0]
                    labels_arr = arr
                    overrides: dict = {}
                    yolo_path = cfg.selected_dir / model / "yolo" / f"{stem}.txt"
                    try:
                        label_classes = _load_label_classes(cfg.labels_file)
                    except Exception:
                        label_classes = []
                    if label_classes:
                        overrides = _instance_labels_from_yolo(
                            arr, yolo_path, label_classes,
                        )
                    # Polygon overrides переписують YOLO: polygons.json — це
                    # найсвіжіше джерело лейблів (bake може бути stale, тоді
                    # YOLO застарілий). Без цього vesicle_cluster-група
                    # з реально vesicle-полігонами над baked nucleus
                    # помилково кваліфікується як «max nucleus exceeded».
                    poly_overrides = _instance_labels_from_polygons(
                        arr, polygons_payload,
                    )
                    if poly_overrides:
                        overrides.update(poly_overrides)
                    instance_labels = _instance_label_lookup(
                        arr,
                        polygons_payload=polygons_payload,
                        per_instance_overrides=overrides,
                    )
                    known_iids = set(int(i) for i in np.unique(arr) if int(i) > 0)
                except Exception:
                    instance_labels = {}
                    known_iids = set()
                    labels_arr = None
        return (instance_labels, polygon_labels, polygons_payload, known_iids,
                labels_arr)

    def _classifications_for(groups: list, instance_labels: dict,
                             polygon_labels: list,
                             classes: Optional[list] = None) -> list:
        return [
            _classify_group_membership(instance_labels, polygon_labels, g,
                                        classes=classes)
            for g in groups
        ]

    # -----------------------------------------------------------------------
    # GET /api/groups/<stem>
    # -----------------------------------------------------------------------

    @bp.route("/api/groups/<stem>", methods=["GET"])
    def api_groups_get(stem: str):
        if not cfg.groups_dir:
            return jsonify({"error": "groups_dir not configured"}), 503

        envelope = _read_groups(cfg.groups_dir, stem)
        model = request.args.get("model") or envelope.get("model")

        # Day 4-5 v2: load custom classes + migrate legacy type → class_id
        classes_env = _read_classes(cfg)
        classes = classes_env.get("classes") or []
        _migrate_groups_type_to_class_id(envelope["groups"], classes)

        instance_labels, polygon_labels, polygons_payload, known_iids, labels_arr = \
            _build_lookups(stem, model)
        # Round 5: auto-strip orphan iids — застарілі посилання на видалені
        # instance (rebake / cleanup-reject). Без strip-у counts брешуть і
        # classify дає хибне valid/invalid. Лише якщо known_iids непорожня
        # (npy успішно прочитався) — інакше strip видалив би все.
        stale_removed: list = []
        if known_iids:
            stale_removed = _strip_orphan_instance_ids(envelope["groups"], known_iids)
        classifications = _classifications_for(
            envelope["groups"], instance_labels, polygon_labels,
            classes=classes,
        )
        return jsonify({
            **envelope,
            "classifications": classifications,
            "classes": classes,
            "stale_removed": stale_removed,
        })

    # -----------------------------------------------------------------------
    # POST /api/groups/<stem>
    # -----------------------------------------------------------------------

    @bp.route("/api/groups/<stem>", methods=["POST"])
    def api_groups_set(stem: str):
        if not cfg.groups_dir:
            return jsonify({"error": "groups_dir not configured"}), 503

        try:
            body = request.get_json(force=True) or {}
        except Exception as e:
            return jsonify({"error": f"invalid JSON: {e}"}), 400

        # Структурна валідація
        err = _validate_groups_payload(body)
        if err:
            return jsonify({"error": err}), 400

        groups = list(body.get("groups") or [])
        model = body.get("model")

        # Single-membership: rewrite + журнал moves
        try:
            moves = _enforce_single_membership(groups)
        except Exception as e:
            print(f"[groups] enforce_single_membership failed for {stem}: {e}")
            traceback.print_exc()
            return jsonify({"error": f"single_membership failed: {e}"}), 500

        # Day 4-5 v2: load classes + migrate legacy type → class_id
        classes_env = _read_classes(cfg)
        classes = classes_env.get("classes") or []
        _migrate_groups_type_to_class_id(groups, classes)

        # Classifications (soft validation + suggested types/class_id)
        instance_labels, polygon_labels, polygons_payload, known_iids, labels_arr = \
            _build_lookups(stem, model)
        # Round 5: auto-strip orphan iids перед classify (invariant: state
        # == reality). Журнал у `stale_removed` для frontend toast.
        stale_removed: list = []
        if known_iids:
            stale_removed = _strip_orphan_instance_ids(groups, known_iids)
        classifications = _classifications_for(groups, instance_labels, polygon_labels,
                                               classes=classes)

        # Write groups.json (atomic + backup + rotate)
        payload = {
            "version": GROUPS_VERSION,
            "stem": stem,
            "model": model,
            "groups": groups,
        }
        try:
            _backup_groups(cfg.groups_dir, stem)
            _rotate_backups(_groups_backup_dir(cfg.groups_dir, stem),
                            keep=GROUPS_BACKUP_KEEP)
            _write_groups(cfg.groups_dir, stem, payload)
        except Exception as e:
            print(f"[groups] write failed for {stem}: {e}")
            traceback.print_exc()
            return jsonify({"error": f"write failed: {e}"}), 500

        # Sync polygons.json.shape.group_id mirror (gibrid C, derived)
        polygons_synced = 0
        if isinstance(polygons_payload, dict) and cfg.polygons_dir:
            try:
                polygons_synced = _sync_polygons_group_id_mirror(
                    polygons_payload, groups,
                )
                if polygons_synced > 0:
                    _write_polygons_json(cfg.polygons_dir, stem, polygons_payload)
            except Exception as e:
                # mirror sync — не критично, лише log
                print(f"[groups] mirror sync failed for {stem}: {e}")

        return jsonify({
            "ok": True,
            "groups": groups,
            "classifications": classifications,
            "moves": moves,
            "polygons_synced": polygons_synced,
            "stale_removed": stale_removed,
            "stem": stem,
            "model": model,
        })

    # -----------------------------------------------------------------------
    # POST /api/groups/<stem>/lasso-hit-test
    # -----------------------------------------------------------------------

    @bp.route("/api/groups/<stem>/lasso-hit-test", methods=["POST"])
    def api_groups_lasso_hit_test(stem: str):
        """
        Body: {
          "model": "instanseg",
          "path":  [[x,y], [x,y], ...],     # image coords; closed implicitly
          "min_overlap_ratio": 0.3,         # optional; за замовч. 0.0 (будь-який pixel)
        }

        Vectorized hit-test:
          lasso_mask = cv2.fillPoly(blank, [path], 1)
          ids = np.unique(labels[lasso_mask])
          for iid in ids: ratio = sum(labels==iid & lasso_mask) / sum(labels==iid)

        Returns {instance_ids: [iid, ...]} — інстанси з ratio >= threshold.
        """
        if not CLEANUP_AVAILABLE or not CV2_AVAILABLE:
            return jsonify({"error": "numpy/opencv not installed"}), 503

        try:
            body = request.get_json(force=True) or {}
        except Exception as e:
            return jsonify({"error": f"invalid JSON: {e}"}), 400

        model = body.get("model")
        if not model:
            return jsonify({"error": "model required"}), 400

        raw_path = body.get("path")
        if not isinstance(raw_path, list) or len(raw_path) < 3:
            return jsonify({"error": "path must be a list of ≥3 [x,y] points"}), 400

        try:
            min_ratio = float(body.get("min_overlap_ratio", 0.0))
        except (TypeError, ValueError):
            min_ratio = 0.0
        min_ratio = max(0.0, min(1.0, min_ratio))

        # Resolve npy
        baked = cfg.selected_dir / model / "npy" / f"{stem}.npy"
        npy_path = baked if baked.exists() else None
        if npy_path is None:
            for m in cfg.models:
                if m.name == model and m.npy_dir:
                    raw = m.npy_dir / f"{stem}.npy"
                    if raw.exists():
                        npy_path = raw
                        break
        if npy_path is None:
            return jsonify({"error": f"npy not found for model={model}, stem={stem}"}), 404

        try:
            labels = np.load(str(npy_path))
            while labels.ndim > 2:
                labels = labels[0]
            labels = labels.astype(np.int32, copy=False)
        except Exception as e:
            return jsonify({"error": f"npy load failed: {e}"}), 500

        H, W = int(labels.shape[0]), int(labels.shape[1])

        try:
            pts = np.array(
                [[int(round(float(p[0]))), int(round(float(p[1])))]
                 for p in raw_path if isinstance(p, (list, tuple)) and len(p) >= 2],
                dtype=np.int32,
            )
        except Exception as e:
            return jsonify({"error": f"path parse failed: {e}"}), 400
        if pts.shape[0] < 3:
            return jsonify({"error": "path needs ≥3 valid points"}), 400

        # Clamp до image bounds — fillPoly це робить, але для безпеки кліпнемо.
        pts[:, 0] = np.clip(pts[:, 0], 0, W - 1)
        pts[:, 1] = np.clip(pts[:, 1], 0, H - 1)

        lasso_canvas = np.zeros((H, W), dtype=np.uint8)
        cv2.fillPoly(lasso_canvas, [pts], 1)
        lasso_mask = lasso_canvas.astype(bool)
        if not np.any(lasso_mask):
            return jsonify({"instance_ids": [], "ratios": {}, "stem": stem, "model": model})

        # Vectorized: instances під lasso
        ids_under = labels[lasso_mask]
        unique_under, counts_under = np.unique(ids_under[ids_under > 0], return_counts=True)

        included: list = []
        ratios: dict = {}
        if min_ratio > 0.0:
            # Треба total area per id → np.unique з усього labels (опц., O(H*W)).
            unique_total, counts_total = np.unique(labels[labels > 0], return_counts=True)
            total_by_id = dict(zip(unique_total.tolist(), counts_total.tolist()))
            for iid, ci in zip(unique_under.tolist(), counts_under.tolist()):
                tot = total_by_id.get(int(iid), 0)
                r = (ci / tot) if tot > 0 else 0.0
                ratios[int(iid)] = round(float(r), 4)
                if r >= min_ratio:
                    included.append(int(iid))
        else:
            included = [int(i) for i in unique_under.tolist()]

        # Bug 4 fix (v1.16.0): id >= POLYGON_ID_BASE — це baked pixels
        # polygon-shape (reserved range), не model instance. Lasso для груп
        # повертає лише model instances; polygon-shapes ловляться окремо на
        # фронті (centroid hit-test) → polygon_indices. Без фільтра один
        # полігон рахується двічі (instance_ids 50003 + polygon_indices 3) —
        # це і є «lasso двійник».
        included = [i for i in included if i < POLYGON_ID_BASE]

        return jsonify({
            "instance_ids": sorted(included),
            "ratios": ratios,
            "stem": stem,
            "model": model,
        })

    return bp
