"""Catalog routes: catalog list, overlay rendering."""
from __future__ import annotations

import io

from flask import Blueprint, abort, jsonify, request, send_file

from state import Config, StateStore
from catalog import CatalogService, _get_clean_overlay_bytes


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_catalog", __name__)

    @bp.route("/api/catalog")
    def api_catalog():
        fresh = request.args.get("fresh") == "1"
        items = catalog.get(fresh=fresh)
        return jsonify({"items": items, "total": len(items)})

    @bp.route("/api/overlay/<model>/<stem>")
    def api_overlay(model: str, stem: str):
        m = next((mm for mm in cfg.models if mm.name == model), None)
        if not m:
            abort(404, f"Unknown model '{model}'")
        # пробуємо і stem і "Копия stem"
        for s in (stem, f"Копия {stem}"):
            p = m.overlay_path(s)
            if not p:
                continue
            # On-the-fly clean overlay: якщо для (stem, model) є rejected у
            # selections.json — згенерувати overlay з прозорими (через
            # original.jpg) rejected-інстансами замість червоних обводок.
            try:
                clean_bytes = _get_clean_overlay_bytes(state, cfg, m, s)
            except Exception:
                clean_bytes = None
            if clean_bytes is not None:
                return send_file(io.BytesIO(clean_bytes), mimetype="image/png")
            return send_file(p)
        abort(404)

    return bp
