"""Labels routes: GET/POST labels, /labels/rename, /base-label/<stem>."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from state import (
    Config,
    StateStore,
    _load_label_classes,
    _save_label_classes,
)
from polygons import _normalize_label_renames, _rename_labels_in_polygon_files
from catalog import CatalogService


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_labels", __name__)

    @bp.route("/api/labels", methods=["GET"])
    def api_labels_get():
        return jsonify(_load_label_classes(cfg.labels_file))

    @bp.route("/api/labels", methods=["POST"])
    def api_labels_post():
        if not cfg.labels_file:
            return jsonify({"error": "labels_file not configured"}), 503
        data = request.get_json(force=True)
        if not isinstance(data, list):
            return jsonify({"error": "expected JSON array"}), 400
        for item in data:
            if not isinstance(item, dict) or not item.get("name"):
                return jsonify({"error": "each label must have 'name'"}), 400
        try:
            _save_label_classes(cfg.labels_file, data)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "count": len(data)})

    @bp.route("/api/base-label/<stem>", methods=["POST"])
    def api_base_label_set(stem: str):
        data = request.get_json(force=True) or {}
        label = str(data.get("label") or "").strip()
        labels = _load_label_classes(cfg.labels_file)
        valid = {str(l.get("name")) for l in labels if l.get("name")}
        if not label or label not in valid:
            return jsonify({
                "error": f"unknown base label {label!r}",
                "valid_labels": sorted(valid),
            }), 400
        entry = state.get(stem) or {}
        entry["base_label"] = label
        state.set(stem, entry)
        # Day 7 lazy-bake: зміна base_label впливає на YOLO class_id запеченого
        # результату — позначаємо dirty (no-op якщо stem без Pick-entry).
        state.mark_dirty(stem)
        return jsonify({
            "ok": True, "stem": stem, "base_label": label,
            "dirty": state.is_dirty(stem),
        })

    @bp.route("/api/labels/rename", methods=["POST"])
    def api_labels_rename():
        if not cfg.polygons_dir:
            return jsonify({"error": "polygons_dir not configured"}), 503
        data = request.get_json(force=True) or {}
        renames, err = _normalize_label_renames(data)
        if err:
            return jsonify({"error": err}), 400
        try:
            result = _rename_labels_in_polygon_files(cfg.polygons_dir, renames or {})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "ok": True,
            "renamed": renames or {},
            **result,
        })

    return bp
