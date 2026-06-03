"""
Workspace lifecycle: switch (runtime), import (ZIP unpack з префіксом-маркерами),
структурні vs flat-ZIP detection.

Власне route-функції (`/api/workspace/*`) лишаються в `routes/api_workspace.py`,
вони використовують ці helpers.

Залежить від: state.py (Config, StateStore, _discover_models), catalog.py
(CatalogService.invalidate).
"""
from __future__ import annotations

from pathlib import Path

from state import Config, StateStore, _discover_models
from catalog import CatalogService


# ZIP path-marker logic: дозволяє розпакувати обгортки типу `_workspace/...`
WORKSPACE_MARKER_DIRS = {"images", "output", "selected", "polygons", "groups"}
WORKSPACE_META_FILES = {"selections.json", "labels.json"}


def _find_marker_index(parts):
    """Індекс першої частини шляху що є маркером workspace-структури.

    Дозволяє розпакувати ZIP-обгортки типу `_workspace/images/x.jpg` →
    `images/x.jpg`. None якщо жодна частина не є маркером.
    """
    for i, part in enumerate(parts):
        if part in WORKSPACE_MARKER_DIRS:
            return i
    return None


def switch_workspace(cfg: Config, state: StateStore, catalog: CatalogService,
                     ws: Path) -> dict:
    """Перемикає всі шляхи у cfg на новий workspace без перезапуску сервера.

    Створює необхідні папки, перевідкриває StateStore, оновлює список моделей,
    інвалідує catalog cache. Повертає опис нового стану.
    """
    ws = ws.resolve()
    ws.mkdir(parents=True, exist_ok=True)
    cfg.workspace_dir = ws
    cfg.images_dir = ws / "images"
    cfg.output_root = ws / "output"
    cfg.selected_dir = ws / "selected"
    cfg.skipped_dir = ws / "skipped"
    cfg.polygons_dir = ws / "polygons"
    cfg.groups_dir = ws / "groups"
    cfg.labels_file = ws / "labels.json"
    cfg.group_classes_file = ws / "group_classes.json"
    for d in (cfg.images_dir, cfg.output_root, cfg.selected_dir,
              cfg.skipped_dir, cfg.polygons_dir, cfg.groups_dir):
        d.mkdir(parents=True, exist_ok=True)
    cfg.models = _discover_models(cfg.output_root)
    state.reload(cfg.selected_dir.parent / "selections.json")
    catalog.invalidate()
    return {
        "workspace_dir": str(cfg.workspace_dir),
        "images_dir": str(cfg.images_dir),
        "output_root": str(cfg.output_root),
        "selected_dir": str(cfg.selected_dir),
        "skipped_dir": str(cfg.skipped_dir),
        "polygons_dir": str(cfg.polygons_dir),
        "groups_dir": str(cfg.groups_dir),
        "models": [m.name for m in cfg.models],
    }
