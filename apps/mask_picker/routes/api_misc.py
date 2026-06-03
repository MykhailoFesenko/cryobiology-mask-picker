"""Misc routes: index, version, shutdown, config, static."""
from __future__ import annotations

import os
import threading
import time

from flask import Blueprint, current_app, jsonify, send_from_directory

from state import (
    APP_NAME,
    APP_VERSION,
    CLEANUP_AVAILABLE,
    CV2_AVAILABLE,
    Config,
    EXPORT_AVAILABLE,
    StateStore,
    _load_label_classes,
)
from catalog import CatalogService


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_misc", __name__)

    @bp.route("/")
    def index():
        return send_from_directory(current_app.template_folder, "index.html")

    @bp.route("/api/version")
    def api_version():
        return jsonify({"name": APP_NAME, "version": APP_VERSION})

    @bp.route("/api/shutdown", methods=["POST"])
    def api_shutdown():
        """Graceful shutdown: відповідає одразу і за 200 ms кладе процес.
        Лише для локального single-user тулу.
        """
        def kill():
            time.sleep(0.2)
            os._exit(0)

        threading.Thread(target=kill, daemon=True).start()
        return jsonify({"ok": True})

    @bp.route("/api/config")
    def api_config():
        return jsonify({
            "images_dir": str(cfg.images_dir),
            "output_root": str(cfg.output_root),
            "selected_dir": str(cfg.selected_dir),
            "skipped_dir": str(cfg.skipped_dir),
            "polygons_dir": str(cfg.polygons_dir) if cfg.polygons_dir else None,
            "workspace_dir": str(cfg.workspace_dir) if cfg.workspace_dir else None,
            "workspace_mode": cfg.workspace_dir is not None,
            "models": [m.name for m in cfg.models],
            "formats_to_copy": list(cfg.formats_to_copy),
            "features": {
                "cleanup": CLEANUP_AVAILABLE,
                "cleanup_export": EXPORT_AVAILABLE,
                "seed_from_mask": CV2_AVAILABLE,
            },
            "labels": _load_label_classes(cfg.labels_file),
        })

    @bp.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(current_app.static_folder, filename)

    return bp
