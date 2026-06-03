"""
Mask Picker — інструмент для відбору кращих масок сегментації з кількох моделей.

Запуск:
    python app.py
    python app.py --config custom_config.yaml
    python app.py --port 5001

Потім відкрий у браузері: http://localhost:5000

v2.0.0: модулі розділено. Цей файл — лише Flask wiring (`create_app`) і CLI.
Уся логіка лежить у:
    state.py     — Config/ModelSource/StateStore, label classes, image helpers
    cleanup.py   — RGB cache, backup rotation, cleanup.json
    polygons.py  — LabelMe envelope, validators, label rename
    baking.py    — _bake_polygons_into_labels + _bake_polygons_to_selected
    catalog.py   — CatalogService + clean overlay rendering
    workspace.py — switch_workspace, ZIP marker logic
    routes/      — blueprints (api_misc, api_state, api_catalog, api_labels,
                    api_cleanup, api_polygons, api_workspace)

Тести імпортують з `app` напряму — re-exports нижче зберігають backward
compatibility.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask

# v2.0.0 split — re-export для тестів, які імпортують з `app`.
from state import (  # noqa: F401
    APP_NAME, APP_VERSION,
    CLEANUP_AVAILABLE, EXPORT_AVAILABLE, CV2_AVAILABLE,
    HERE, PROJECT_ROOT, DATA_ROOT,
    IMAGE_EXTS, OVERLAY_EXTS,
    Config, ModelSource, StateStore,
    DEFAULT_LABEL_CLASSES,
    _atomic_write_json, _utc_iso, _utc_stamp,
    _discover_models, load_config,
    _load_label_classes, _save_label_classes,
    _load_original_image_array, _find_image_filename, _load_json_file,
    _copy_model_files_for_stem,
    np, Image, cv2, export_segmentation_bundle, draw_overlay, yaml,
)
from cleanup import (  # noqa: F401
    BACKUP_KEEP,
    _RGB_CACHE, _RGB_CACHE_LOCK, _RGB_CACHE_MAX,
    _backup_dir_for_stem,
    _find_existing_selected,
    _find_npy_for,
    _get_rgb_png_cached,
    _instance_stats,
    _labels_to_rgb_png_bytes,
    _make_backup,
    _rotate_backups,
    _write_cleanup_json,
)
from polygons import (  # noqa: F401
    POLYGON_BACKUP_KEEP,
    _backup_polygons,
    _labelme_envelope,
    _load_labels,
    _normalize_label_renames,
    _polygons_backup_dir,
    _polygons_path,
    _rename_labels_in_polygon_files,
    _validate_polygons_payload,
    _write_polygons_json,
)
from baking import (  # noqa: F401
    _bake_polygons_into_labels,
    _bake_polygons_to_selected,
    _mask_to_polygons,
    _write_yolo_multiclass,
)
from groups import (  # noqa: F401
    GROUPS_BACKUP_KEEP,
    GROUPS_VERSION,
    GROUP_TYPES,
    PALETTE_HUES,
    _backup_groups,
    _classify_group_membership,
    _count_labels_in_group,
    _empty_groups_envelope,
    _enforce_single_membership,
    _groups_backup_dir,
    _groups_path,
    _iids_by_label_in_group,
    _instance_label_lookup,
    _instance_labels_from_polygons,
    _instance_labels_from_yolo,
    _orphan_iids_in_group,
    _strip_orphan_instance_ids,
    _violating_iids_for_class,
    _migrate_groups_type_to_class_id,
    _next_color_hue,
    _next_group_id,
    _polygon_labels_from_payload,
    _read_groups,
    _resolve_class_id,
    _sync_polygons_group_id_mirror,
    _validate_groups_payload,
    _write_groups,
)
from group_classes import (  # noqa: F401
    DEFAULT_GROUP_CLASSES,
    GROUP_CLASSES_VERSION,
    _class_by_id,
    _class_by_name,
    _empty_classes_envelope,
    _next_class_id,
    _read_classes,
    _resolve_classes_path,
    _suggest_class_for_counts,
    _validate_class_against_counts,
    _validate_classes_payload,
    _write_classes,
)
from catalog import (  # noqa: F401
    CatalogService,
    _CLEAN_OVERLAY_CACHE,
    _CLEAN_OVERLAY_PAD,
    _get_clean_overlay_bytes,
    _normalize_stem,
    _render_clean_overlay_bytes,
)
from workspace import (  # noqa: F401
    WORKSPACE_MARKER_DIRS,
    WORKSPACE_META_FILES,
    _find_marker_index,
    switch_workspace,
)
import routes


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def create_app(cfg: Config, state: StateStore) -> Flask:
    """Build Flask app with all routes registered."""
    app = Flask(
        __name__,
        static_folder=str(HERE / "static"),
        template_folder=str(HERE / "templates"),
    )
    catalog = CatalogService(cfg, state)
    routes.register_all(app, cfg, state, catalog)
    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Mask Picker")
    p.add_argument("--config", type=Path, default=HERE / "config.yaml",
                   help="Шлях до config.yaml (якщо немає — працює авто-дискавері).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--no-browser", action="store_true",
                   help="Не відкривати браузер автоматично.")
    p.add_argument("--images-dir", type=Path, default=None,
                   help="Override images_dir з конфіга.")
    p.add_argument("--output-root", type=Path, default=None,
                   help="Override output_root з конфіга.")
    p.add_argument("--selected-dir", type=Path, default=None,
                   help="Override selected_dir з конфіга.")
    p.add_argument("--skipped-dir", type=Path, default=None,
                   help="Override skipped_dir з конфіга.")
    p.add_argument("--polygons-dir", type=Path, default=None,
                   help="Override polygons_dir з конфіга (Stage C).")
    p.add_argument("--groups-dir", type=Path, default=None,
                   help="Override groups_dir з конфіга (cell grouping).")
    p.add_argument("--workspace", type=Path, default=None,
                   help="Workspace-папка для портативного режиму. "
                        "Default: <app_dir>/_workspace/. "
                        "При вказанні — всі директорії беруться з цієї папки, "
                        "а відсутність моделей не є помилкою.")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config if args.config and args.config.exists() else None)

    # ---- workspace mode --------------------------------------------------
    workspace_mode = False
    if args.workspace:
        ws = args.workspace.resolve()
        ws.mkdir(parents=True, exist_ok=True)
        cfg.workspace_dir = ws
        cfg.images_dir   = ws / "images"
        cfg.output_root  = ws / "output"
        cfg.selected_dir = ws / "selected"
        cfg.skipped_dir  = ws / "skipped"
        cfg.polygons_dir = ws / "polygons"
        cfg.groups_dir   = ws / "groups"
        cfg.labels_file  = ws / "labels.json"
        cfg.group_classes_file = ws / "group_classes.json"
        for d in (cfg.images_dir, cfg.output_root, cfg.selected_dir,
                  cfg.skipped_dir, cfg.polygons_dir, cfg.groups_dir):
            d.mkdir(parents=True, exist_ok=True)
        cfg.models = _discover_models(cfg.output_root)
        workspace_mode = True
    # ---- CLI overrides (non-workspace) -----------------------------------
    else:
        if args.images_dir:
            cfg.images_dir = args.images_dir.resolve()
        if args.output_root:
            cfg.output_root = args.output_root.resolve()
            cfg.models = _discover_models(cfg.output_root)
        if args.selected_dir:
            cfg.selected_dir = args.selected_dir.resolve()
        if args.skipped_dir:
            cfg.skipped_dir = args.skipped_dir.resolve()
        if args.polygons_dir:
            cfg.polygons_dir = args.polygons_dir.resolve()
        if args.groups_dir:
            cfg.groups_dir = args.groups_dir.resolve()

    print("=" * 68)
    print("Mask Picker" + ("  [workspace mode]" if workspace_mode else ""))
    print("=" * 68)
    print(f"  images_dir   : {cfg.images_dir}")
    print(f"  output_root  : {cfg.output_root}")
    print(f"  selected_dir : {cfg.selected_dir}")
    print(f"  skipped_dir  : {cfg.skipped_dir}")
    print(f"  polygons_dir : {cfg.polygons_dir}")
    print(f"  groups_dir   : {cfg.groups_dir}")
    if workspace_mode:
        print(f"  workspace    : {cfg.workspace_dir}")
    print(f"  models       : {[m.name for m in cfg.models] or '(none)'}")
    print(f"  features     : cleanup={CLEANUP_AVAILABLE} export={EXPORT_AVAILABLE} seed={CV2_AVAILABLE}")
    if not cfg.models and not workspace_mode:
        print()
        print("  [ERROR] Не знайдено жодної моделі з масками!")
        print(f"  Перевір що у {cfg.output_root} є підпапки з overlay/ всередині.")
        print("  Наприклад: output/cyto2/overlay/db_img_0001.png")
        sys.exit(1)
    if not cfg.images_dir.exists() and not workspace_mode:
        print()
        print(f"  [ERROR] Папка зображень не існує: {cfg.images_dir}")
        sys.exit(1)
    print("=" * 68)
    print(f"  Server: http://{args.host}:{args.port}")
    print("  Ctrl+C щоб зупинити")
    print("=" * 68)

    state = StateStore(cfg.selected_dir.parent / "selections.json")
    app = create_app(cfg, state)

    # Глушимо werkzeug per-request access-logs (на Windows вони переповнюють
    # консоль на ~5 хв роботи і весь Flask-процес тихо завершується).
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    if not args.no_browser:
        url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False,
            threaded=True)


if __name__ == "__main__":  # entry point
    main()
