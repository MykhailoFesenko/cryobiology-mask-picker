"""
Group classes CRUD (Day 4-5 v2 redesign).

User-defined класи (name + HSL color + universal label constraints) живуть у
`group_classes.json` per workspace. Auto-create defaults при першому read.

Endpoints:
  GET  /api/group-classes      — повний envelope (auto-creates якщо нема)
  POST /api/group-classes      — replace entire classes list (atomic)
"""
from __future__ import annotations

import traceback

from flask import Blueprint, jsonify, request

from state import Config, StateStore
from catalog import CatalogService
from group_classes import (
    _read_classes,
    _resolve_classes_path,
    _validate_classes_payload,
    _write_classes,
)


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_group_classes", __name__)

    @bp.route("/api/group-classes", methods=["GET"])
    def api_group_classes_get():
        envelope = _read_classes(cfg)
        path = _resolve_classes_path(cfg)
        return jsonify({
            **envelope,
            "path": str(path) if path else None,
        })

    @bp.route("/api/group-classes", methods=["POST"])
    def api_group_classes_set():
        try:
            body = request.get_json(force=True) or {}
        except Exception as e:
            return jsonify({"error": f"invalid JSON: {e}"}), 400

        err = _validate_classes_payload(body)
        if err:
            return jsonify({"error": err}), 400

        try:
            payload = {
                "version": body.get("version") or "1.0",
                "classes": body.get("classes") or [],
            }
            path = _write_classes(cfg, payload)
        except Exception as e:
            print(f"[group_classes] write failed: {e}")
            traceback.print_exc()
            return jsonify({"error": f"write failed: {e}"}), 500

        return jsonify({
            "ok": True,
            "classes": payload["classes"],
            "path": str(path),
        })

    return bp
