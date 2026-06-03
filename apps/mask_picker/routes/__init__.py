"""
HTTP API blueprints for Mask Picker v2.0.0.

Each module exports `make_blueprint(cfg, state, catalog)` — factory that
returns a Flask Blueprint with all routes for that domain. Routes capture
cfg/state/catalog via closure.

`register_all(app, cfg, state, catalog)` registers all blueprints in one call.
"""
from __future__ import annotations

from flask import Flask

from state import Config, StateStore
from catalog import CatalogService

from . import (
    api_catalog,
    api_cleanup,
    api_group_classes,
    api_groups,
    api_labels,
    api_misc,
    api_polygons,
    api_state,
    api_workspace,
)


def register_all(app: Flask, cfg: Config, state: StateStore,
                 catalog: CatalogService) -> None:
    """Register all API blueprints onto `app`."""
    app.register_blueprint(api_misc.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_state.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_catalog.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_labels.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_cleanup.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_polygons.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_groups.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_group_classes.make_blueprint(cfg, state, catalog))
    app.register_blueprint(api_workspace.make_blueprint(cfg, state, catalog))
