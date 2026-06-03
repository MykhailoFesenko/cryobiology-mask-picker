"""
api_cleanup.py — HTTP-маршрути для cleanup (rejected_instances).

Endpoints:
  GET  /api/labels-rgb/<model>/<stem>.png  — RGB-encoded raw npy (для UI
                                              hit-test через RGBA pixel read).
  GET  /api/instances/<model>/<stem>       — швидкий count + shape.
  GET  /api/cleanup/<stem>                 — state[stem].cleanup.
  POST /api/cleanup/<stem>                 — toggle reject + write cleanup.json.
  POST /api/cleanup-export/<stem>          — full re-bake selected з cleanup.

ВАЖЛИВО: rejected_instances — це IDs у raw output (output/<model>/npy),
не у filtered (selected/<model>/npy). UI читає raw через /api/labels-rgb/
→ cu.labelsInt32. Bake потім застосовує rejected до raw → cleaned.
"""
from __future__ import annotations

import traceback

from flask import Blueprint, abort, current_app, jsonify, request

from state import (
    CLEANUP_AVAILABLE,
    Config,
    EXPORT_AVAILABLE,
    StateStore,
    _load_original_image_array,
    export_segmentation_bundle,
    np,
)
from cleanup import (
    BACKUP_KEEP,
    _RGB_CACHE,
    _RGB_CACHE_LOCK,
    _backup_dir_for_stem,
    _find_npy_for,
    _get_rgb_png_cached,
    _instance_stats,
    _make_backup,
    _rotate_backups,
    _write_cleanup_json,
)
from polygons import _load_labels, _polygons_path
from data_sync import bake_with_resync
from catalog import CatalogService

# v1.14.0 Bug 7 fix REVERTED 2026-05-27:
# Initial approach — викликати helper `_strip_rejected_from_groups`
# у POST /api/cleanup для видалення rejected iid з усіх
# `group.instance_ids`. Проблема: до bake поточний npy ще містить
# rejected (cleanup — лише прапор). Якщо rejected iid випадково
# збігся з polygon-resolved iid (`next_id = max(npy)+1` колізія) —
# видаляли legitimately-resolved nucleus з групи → Bug 3 регресія
# (13 LOST cells у 4 stems при backfill testing).
# Висновок: I5 (`rejected ∩ instance_ids ≠ ∅`) — це **lazy invariant**.
# Self-heal при наступному bake: `cleaned[isin(rejected)]=0` робить iid
# фактично відсутнім у npy → `_strip_orphan_instance_ids` у GET/POST
# /api/groups видаляє його. Phantom видно лише між reject і bake;
# UX-задача, не data integrity.


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_cleanup", __name__)

    @bp.route("/api/labels-rgb/<model>/<stem>.png")
    def api_labels_rgb(model: str, stem: str):
        """
        Рендерить instance-маску моделі як RGB PNG де колір == instance_id.
        Використовується для hit-test на клієнті (picking buffer).
        """
        if not CLEANUP_AVAILABLE:
            abort(503, "numpy/Pillow not installed on server")
        m = next((mm for mm in cfg.models if mm.name == model), None)
        if not m:
            abort(404, f"Unknown model '{model}'")
        npy = _find_npy_for(m, stem)
        if not npy:
            abort(404, f"No .npy mask for {model}/{stem}")
        try:
            data = _get_rgb_png_cached(model, stem, npy)
        except Exception as e:
            print(f"[cleanup] encoding failed: {e}")
            abort(500, str(e))
        resp = current_app.response_class(data, mimetype="image/png")
        # кеш у браузері — 5 хв (PNG не змінюється для того самого .npy)
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp

    @bp.route("/api/instances/<model>/<stem>")
    def api_instances(model: str, stem: str):
        """Метаінфо про маску: кількість інстансів, розмір."""
        if not CLEANUP_AVAILABLE:
            return jsonify({"error": "numpy/Pillow not installed"}), 503
        m = next((mm for mm in cfg.models if mm.name == model), None)
        if not m:
            abort(404, f"Unknown model '{model}'")
        npy = _find_npy_for(m, stem)
        if not npy:
            return jsonify({"instance_count": 0, "shape": None})
        return jsonify(_instance_stats(npy))

    @bp.route("/api/cleanup/<stem>", methods=["GET"])
    def api_cleanup_get(stem: str):
        return jsonify(state.get_cleanup(stem))

    @bp.route("/api/cleanup/<stem>", methods=["POST"])
    def api_cleanup_set(stem: str):
        """
        Body: {
          "model": "cyto2",
          "rejected_instances": [5, 12, 47],
          "markers": [{"x": 123.4, "y": 456.7}, ...]  # optional, Stage C
          "user": "..."
        }
        Debounced autosave від фронта — зберігає rejected + markers у selections.json.
        НЕ перезаписує файли в selected/ — для цього є /api/cleanup-export.
        """
        body = request.get_json(force=True) or {}
        model_name = body.get("model")
        rejected = body.get("rejected_instances", [])
        markers = body.get("markers")  # None = не чіпати; список = записати
        user = body.get("user") or "anonymous"
        if not model_name:
            return jsonify({"error": "model required"}), 400
        if not isinstance(rejected, list):
            return jsonify({"error": "rejected_instances must be a list"}), 400
        if markers is not None and not isinstance(markers, list):
            return jsonify({"error": "markers must be a list"}), 400
        saved = state.set_cleanup(stem, model_name, rejected, markers=markers, user=user)
        # Day 7 lazy-bake: зміна rejected/markers НЕ перепікає selected/ —
        # позначаємо dirty, bake відбудеться через Save All / Finalize.
        state.mark_dirty(stem)
        # v1.14.0 (Bug 7 — REVERTED): backend strip rejected з groups
        # видаляло legitimately polygon-resolved iid через next_id
        # колізію. I5 invariant — lazy, self-heal після bake.
        return jsonify({"ok": True, "cleanup": saved, "dirty": state.is_dirty(stem)})

    @bp.route("/api/cleanup-export/<stem>", methods=["POST"])
    def api_cleanup_export(stem: str):
        """
        Body: { "model": "cyto2", "rejected_instances": [5, 12, 47], "user": "..." }

        Регенерує чисті файли у selected/<model>/{npy,png,yolo,overlay}/
        з урахуванням rejected-інстансів. Попередні версії — у _backups/<stem>/<ts>/.
        Оновлює selections.json і selected/<model>/cleanup.json.
        output/<model>/ — НЕ ЧІПАЄМО.
        """
        if not EXPORT_AVAILABLE:
            return jsonify({
                "error": "cellsegkit not installed — run "
                         "`pip install -e ./cryobiology3` з кореня проекту"
            }), 503

        body = request.get_json(force=True) or {}
        model_name = body.get("model")
        rejected = body.get("rejected_instances", [])
        markers = body.get("markers")
        user = body.get("user") or "anonymous"
        if not model_name:
            return jsonify({"error": "model required"}), 400
        if not isinstance(rejected, list):
            return jsonify({"error": "rejected_instances must be a list"}), 400
        if markers is not None and not isinstance(markers, list):
            return jsonify({"error": "markers must be a list"}), 400

        m = next((mm for mm in cfg.models if mm.name == model_name), None)
        if not m:
            return jsonify({"error": f"unknown model {model_name}"}), 400

        cur = state.get(stem) or {}
        if cur.get("status") != "selected" or cur.get("model") != model_name:
            return jsonify({
                "error": f"stem {stem} не має активного Pick з model={model_name}; "
                         "спочатку натисни Pick"
            }), 400

        src_npy = _find_npy_for(m, stem)
        if not src_npy:
            return jsonify({"error": f"original .npy not found for {model_name}/{stem}"}), 404

        rejected_ids = sorted(set(int(i) for i in rejected))

        try:
            # v1.15.0 fix: cleanup-export тепер ходить через
            # `data_sync.bake_with_resync` — це закриває B3 (strip orphan iid
            # з groups.json) і робить usage path єдиним з основним bake flow.
            # Раніше викликали `export_segmentation_bundle` напряму — після
            # 🔥 cleanup-export групи лишалися з orphan iid (бо strip
            # in-memory тільки у GET /api/groups, який cleanup-export не
            # викликає).
            shapes: list = []
            if cfg.polygons_dir:
                poly_path = _polygons_path(cfg.polygons_dir, stem)
                if poly_path.exists():
                    import json as _json
                    try:
                        with open(poly_path, "r", encoding="utf-8") as f:
                            poly_data = _json.load(f)
                        shapes = poly_data.get("shapes") or []
                    except Exception as e:
                        return jsonify({
                            "error": f"failed to read polygons: {e}"
                        }), 500

            cur_entry = state.get(stem) or {}
            base_label = cur_entry.get("base_label") or None

            result = bake_with_resync(
                cfg, stem, model_name, src_npy, shapes,
                rejected=rejected_ids, base_label=base_label, do_backup=True,
            )
            if result["errors"]:
                return jsonify({
                    "error": "export failed for formats: "
                             + ", ".join(result["errors"])
                }), 500

            # Оновити state + per-model cleanup.json (markers + user тут).
            saved = state.set_cleanup(stem, model_name, rejected_ids,
                                      markers=markers, user=user)
            _write_cleanup_json(cfg.selected_dir, model_name, stem, rejected_ids,
                                user, markers=saved.get("markers"))

            # Інвалідувати RGB-кеш (на випадок якщо хтось реюзає ендпоінт).
            with _RGB_CACHE_LOCK:
                _RGB_CACHE.pop((model_name, stem), None)

            state.clear_dirty(stem)
            return jsonify({
                "ok": True,
                "cleanup": saved,
                "baked": True,
                "baked_count": result["baked_count"],
                "backup": str(result["backup"]) if result["backup"] else None,
                "rejected_count": len(rejected_ids),
                "orphan_iids_stripped": result.get("orphan_iids_stripped", 0),
                "groups_sync_added": result.get("groups_sync_added", 0),
            })
        except Exception as e:
            print(f"[cleanup-export] {stem}/{model_name}: {e}")
            traceback.print_exc()
            return jsonify({"error": f"export failed: {e}"}), 500

    return bp
