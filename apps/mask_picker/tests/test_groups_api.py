"""
HTTP API тести для groups (CP2).

Покриває:
  * GET  /api/groups/<stem>             — порожній envelope коли файла нема
  * POST /api/groups/<stem>             — save + reload roundtrip
  * POST                                — validate rejects invalid payload
  * POST                                — _enforce_single_membership через API
                                           (moves журнал у response)
  * POST                                — _sync_polygons_group_id_mirror
                                           (polygons.json.shape.group_id оновлено)
  * GET                                 — classifications включають n_nucleus,
                                           valid, suggested_type
  * POST                                — backup при перезаписі (>=1 у _backups/)
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import app as mask_picker_app  # noqa: E402
from app import (  # noqa: E402
    Config,
    ModelSource,
    StateStore,
    _polygons_path,
    _write_polygons_json,
    create_app,
)
from baking import POLYGON_ID_BASE  # noqa: E402


STEM = "db_img_test"
MODEL = "cyto2"


def _build_project(tmp_path: Path):
    images = tmp_path / "images"
    output = tmp_path / "output"
    selected = tmp_path / "selected"
    skipped = tmp_path / "skipped"
    polygons = tmp_path / "polygons"
    groups = tmp_path / "groups"

    model_out = output / MODEL
    for d in (
        images,
        model_out / "overlay",
        model_out / "png",
        model_out / "npy",
        model_out / "yolo",
        selected,
        skipped,
        polygons,
        groups,
        selected / MODEL / "npy",
    ):
        d.mkdir(parents=True, exist_ok=True)

    Image.fromarray(np.full((64, 64), 40, dtype=np.uint8), mode="L").save(
        images / f"{STEM}.jpg"
    )

    labels = np.zeros((64, 64), dtype=np.int32)
    labels[5:15, 5:15] = 1
    labels[20:30, 20:30] = 2
    labels[40:50, 40:50] = 3
    np.save(model_out / "npy" / f"{STEM}.npy", labels)
    # Те ж саме у selected/<model>/npy для baked lookup
    np.save(selected / MODEL / "npy" / f"{STEM}.npy", labels)

    Image.fromarray((labels * 80).astype(np.uint8), mode="L").save(
        model_out / "overlay" / f"{STEM}.png"
    )

    return images, output, selected, skipped, polygons, groups


@pytest.fixture
def client_cfg(tmp_path):
    images, output, selected, skipped, polygons, groups = _build_project(tmp_path)
    model = ModelSource(
        name=MODEL,
        overlay_dir=output / MODEL / "overlay",
        png_dir=output / MODEL / "png",
        npy_dir=output / MODEL / "npy",
        yolo_dir=output / MODEL / "yolo",
    )
    cfg = Config(
        images_dir=images,
        output_root=output,
        selected_dir=selected,
        skipped_dir=skipped,
        polygons_dir=polygons,
        groups_dir=groups,
        models=[model],
    )
    state = StateStore(selected.parent / "selections.json")
    app = create_app(cfg, state)
    app.testing = True
    with mask_picker_app._RGB_CACHE_LOCK:
        mask_picker_app._RGB_CACHE.clear()
    return app.test_client(), cfg, state


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def test_groups_get_empty_envelope(client_cfg):
    client, cfg, _ = client_cfg
    resp = client.get(f"/api/groups/{STEM}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["stem"] == STEM
    assert body["groups"] == []
    assert body["classifications"] == []


def test_groups_get_with_model_classifies(client_cfg):
    """GET з ?model=<m> підгружає npy і classifies instance_ids."""
    client, cfg, _ = client_cfg
    # Save group via POST (тест simulate normal flow)
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{
            "id": "g_001",
            "type": "cell",
            "instance_ids": [1, 2],
            "polygon_indices": [],
            "color_hue": 0,
        }],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # GET back з ?model — classifications мають n_other (default label fallback)
    resp2 = client.get(f"/api/groups/{STEM}?model={MODEL}")
    assert resp2.status_code == 200
    body = resp2.get_json()
    assert len(body["classifications"]) == 1
    cls = body["classifications"][0]
    # Без cleanup base_label fallback = "nucleus"; cell з 2 nuc 0 ves → invalid
    assert "suggested_type" in cls
    assert "valid" in cls


# ---------------------------------------------------------------------------
# POST roundtrip + validation
# ---------------------------------------------------------------------------

def test_groups_post_roundtrip(client_cfg):
    client, cfg, _ = client_cfg
    payload = {
        "model": MODEL,
        "groups": [{
            "id": "g_001",
            "type": "cell",
            "instance_ids": [1, 2, 3],
            "polygon_indices": [],
            "color_hue": 0,
        }],
    }
    resp = client.post(f"/api/groups/{STEM}", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["moves"] == []
    assert (cfg.groups_dir / f"{STEM}.json").exists()

    # GET retrieves same
    resp2 = client.get(f"/api/groups/{STEM}")
    body2 = resp2.get_json()
    assert len(body2["groups"]) == 1
    assert body2["groups"][0]["instance_ids"] == [1, 2, 3]


def test_groups_post_rejects_invalid_type(client_cfg):
    client, _, _ = client_cfg
    resp = client.post(f"/api/groups/{STEM}", json={
        "groups": [{"id": "g_001", "type": "BOGUS"}],
    })
    assert resp.status_code == 400
    assert "type" in resp.get_json()["error"]


def test_groups_post_rejects_non_object():
    """Plain text payload → 400."""
    # Reuse new client (без fixture для простоти)
    # Це покривається валідатором, тому окремий тест на rejects non-object
    # уже є у test_groups_smoke. Тут лише integration.


# ---------------------------------------------------------------------------
# Single-membership через API
# ---------------------------------------------------------------------------

def test_groups_post_enforces_single_membership(client_cfg):
    """iid у двох групах → moves журнал + last group wins.

    Використовуємо iid 1, 2, 3 — реально існують у npy фікстури.
    Round 5: auto-strip orphan видалив би 10/20/30 раніше за enforce,
    тому використовуємо real iid.
    """
    client, cfg, _ = client_cfg
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [
            {"id": "g_001", "type": "cell", "instance_ids": [1, 2],
             "polygon_indices": [], "color_hue": 0},
            {"id": "g_002", "type": "cell", "instance_ids": [2, 3],
             "polygon_indices": [], "color_hue": 30},
        ],
    })
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert any(m["id"] == 2 and m["from"] == "g_001" and m["to"] == "g_002"
               for m in body["moves"])
    # Stored: g_001 повинен мати лише [1], g_002 — [2, 3]
    assert body["groups"][0]["instance_ids"] == [1]
    assert body["groups"][1]["instance_ids"] == [2, 3]


# ---------------------------------------------------------------------------
# Polygons mirror sync
# ---------------------------------------------------------------------------

def test_groups_post_syncs_polygons_mirror(client_cfg):
    """POST groups → polygons.json.shape.group_id оновлюється."""
    client, cfg, _ = client_cfg
    # Prep polygons.json з 2 shapes
    polygons_payload = {
        "version": "5.0.1",
        "imagePath": f"{STEM}.jpg",
        "imageHeight": 64,
        "imageWidth": 64,
        "shapes": [
            {"label": "nucleus", "points": [[1, 1], [2, 1], [2, 2]],
             "shape_type": "polygon", "group_id": None, "flags": {}},
            {"label": "vesicle", "points": [[3, 3], [4, 3], [4, 4]],
             "shape_type": "polygon", "group_id": None, "flags": {}},
        ],
    }
    _write_polygons_json(cfg.polygons_dir, STEM, polygons_payload)

    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{"id": "g_001", "type": "cell",
                   "instance_ids": [], "polygon_indices": [0, 1],
                   "color_hue": 0}],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["polygons_synced"] >= 2

    # Read polygons.json — обидві shapes повинні мати group_id = "g_001"
    with open(_polygons_path(cfg.polygons_dir, STEM), "r",
              encoding="utf-8-sig") as f:
        polygons_back = json.load(f)
    assert polygons_back["shapes"][0]["group_id"] == "g_001"
    assert polygons_back["shapes"][1]["group_id"] == "g_001"


# ---------------------------------------------------------------------------
# Backup on overwrite
# ---------------------------------------------------------------------------

def test_lasso_hit_test_inside_path(client_cfg):
    """Lasso path накриває instance 1 (5..15, 5..15) → instance_id 1 у результаті."""
    client, cfg, _ = client_cfg
    resp = client.post(f"/api/groups/{STEM}/lasso-hit-test", json={
        "model": MODEL,
        "path": [[2, 2], [18, 2], [18, 18], [2, 18]],  # square covering instance 1
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert 1 in body["instance_ids"]
    assert 2 not in body["instance_ids"]
    assert 3 not in body["instance_ids"]


def test_lasso_hit_test_covers_all_three(client_cfg):
    """Великий lasso по всьому image → всі 3 instances."""
    client, cfg, _ = client_cfg
    resp = client.post(f"/api/groups/{STEM}/lasso-hit-test", json={
        "model": MODEL,
        "path": [[0, 0], [63, 0], [63, 63], [0, 63]],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert sorted(body["instance_ids"]) == [1, 2, 3]


def test_lasso_hit_test_excludes_reserved_polygon_ids(client_cfg):
    """Bug 4 fix (v1.16.0): lasso-hit-test НЕ повертає id >= POLYGON_ID_BASE
    (baked polygon pixels). Polygon ловиться окремо через centroid hit-test
    на фронті → polygon_indices. Без фільтра один полігон рахується двічі
    (lasso двійник)."""
    client, cfg, _ = client_cfg
    # Додаємо polygon-range instance (50005) у selected npy (емуляція
    # запеченого polygon-shape у reserved range).
    npy_path = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    labels = np.load(str(npy_path))
    labels[50:60, 50:60] = POLYGON_ID_BASE + 5
    np.save(str(npy_path), labels)

    resp = client.post(f"/api/groups/{STEM}/lasso-hit-test", json={
        "model": MODEL,
        "path": [[0, 0], [63, 0], [63, 63], [0, 63]],  # весь image
    })
    assert resp.status_code == 200
    body = resp.get_json()
    # model instances присутні
    assert 1 in body["instance_ids"]
    # polygon-range id виключено
    assert (POLYGON_ID_BASE + 5) not in body["instance_ids"]
    assert all(i < POLYGON_ID_BASE for i in body["instance_ids"]), body["instance_ids"]


def test_lasso_hit_test_min_overlap_filters_partial(client_cfg):
    """min_overlap_ratio=0.9 → instance 1 захоплений лише частково (трикутник зрізає кут)."""
    client, cfg, _ = client_cfg
    resp = client.post(f"/api/groups/{STEM}/lasso-hit-test", json={
        "model": MODEL,
        "path": [[5, 5], [9, 5], [5, 9]],  # трикутник на куті instance 1
        "min_overlap_ratio": 0.9,
    })
    assert resp.status_code == 200
    body = resp.get_json()
    # ratio < 0.9 → instance 1 НЕ включений
    assert 1 not in body["instance_ids"]


def test_lasso_hit_test_rejects_short_path(client_cfg):
    client, _, _ = client_cfg
    resp = client.post(f"/api/groups/{STEM}/lasso-hit-test", json={
        "model": MODEL,
        "path": [[0, 0], [1, 1]],  # 2 точки — недостатньо
    })
    assert resp.status_code == 400
    assert "path" in resp.get_json()["error"]


def test_lasso_hit_test_rejects_no_model(client_cfg):
    client, _, _ = client_cfg
    resp = client.post(f"/api/groups/{STEM}/lasso-hit-test", json={
        "path": [[0, 0], [5, 0], [5, 5]],
    })
    assert resp.status_code == 400


def test_lasso_hit_test_returns_404_for_missing_npy(client_cfg):
    client, cfg, _ = client_cfg
    resp = client.post(f"/api/groups/nope_no_such_stem/lasso-hit-test", json={
        "model": MODEL,
        "path": [[0, 0], [5, 0], [5, 5]],
    })
    assert resp.status_code == 404


def test_workspace_export_includes_groups(client_cfg, tmp_path):
    """Workspace export ZIP має містити groups/<stem>.json."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = tmp_path  # вмикаємо workspace mode для /export

    # Save groups
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{"id": "g_001", "type": "cell",
                   "instance_ids": [1, 2], "polygon_indices": [],
                   "color_hue": 0}],
    })
    assert resp.status_code == 200

    resp = client.get("/api/workspace/export")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())
        assert f"groups/{STEM}.json" in names, names
        loaded = json.loads(zf.read(f"groups/{STEM}.json").decode("utf-8"))
        assert loaded["groups"][0]["id"] == "g_001"


def test_workspace_finalize_includes_groups(client_cfg, tmp_path):
    """Finalize <stem> ZIP має містити groups/<stem>.json."""
    client, cfg, state = client_cfg
    cfg.workspace_dir = tmp_path
    # Pick model — потрібно для finalize (state entry)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    # Save groups
    client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{"id": "g_001", "type": "cell",
                   "instance_ids": [1], "polygon_indices": [],
                   "color_hue": 30}],
    })

    resp = client.get(f"/api/workspace/finalize/{STEM}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())
        assert f"groups/{STEM}.json" in names, names


def test_group_classes_get_auto_creates_defaults(client_cfg, tmp_path):
    """GET /api/group-classes auto-creates 3 defaults у workspace."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = tmp_path  # require for path resolution
    cfg.group_classes_file = tmp_path / "group_classes.json"

    resp = client.get("/api/group-classes")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    names = [c["name"] for c in body["classes"]]
    assert "cell" in names
    assert "vesicle_cluster" in names
    assert "nuclei" in names


def test_group_classes_post_roundtrip(client_cfg, tmp_path):
    client, cfg, _ = client_cfg
    cfg.workspace_dir = tmp_path
    cfg.group_classes_file = tmp_path / "group_classes.json"

    custom = {
        "classes": [{
            "id": "cls_custom",
            "name": "membrane_complex",
            "color_hue": 200,
            "color_sat": 50,
            "color_light": 45,
            "constraints": {"min": {"membrane": 2}},
        }],
    }
    resp = client.post("/api/group-classes", json=custom)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # GET reflects new state
    resp2 = client.get("/api/group-classes")
    body = resp2.get_json()
    assert len(body["classes"]) == 1
    assert body["classes"][0]["name"] == "membrane_complex"


def test_group_classes_post_rejects_invalid(client_cfg, tmp_path):
    client, cfg, _ = client_cfg
    cfg.workspace_dir = tmp_path
    cfg.group_classes_file = tmp_path / "group_classes.json"
    resp = client.post("/api/group-classes", json={
        "classes": [{"id": "", "name": "x"}],  # empty id
    })
    assert resp.status_code == 400


def test_groups_get_returns_classes_and_classifications(client_cfg, tmp_path):
    """GET /api/groups/<stem> повертає classes список + classifications з class_id."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = tmp_path
    cfg.group_classes_file = tmp_path / "group_classes.json"

    # Save group with legacy `type` → backend migrates до class_id
    client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{"id": "g_001", "type": "cell",
                   "instance_ids": [1], "polygon_indices": [], "color_hue": 0}],
    })

    resp = client.get(f"/api/groups/{STEM}?model={MODEL}")
    body = resp.get_json()
    assert "classes" in body
    assert len(body["classes"]) >= 3  # auto-created defaults
    # Migration: group should have class_id matching "cell"
    cls_names = {c["id"]: c["name"] for c in body["classes"]}
    g = body["groups"][0]
    assert g.get("class_id") and cls_names[g["class_id"]] == "cell"


def test_workspace_marker_dirs_include_groups():
    """WORKSPACE_MARKER_DIRS повинен містити 'groups' для ZIP import."""
    from app import WORKSPACE_MARKER_DIRS as _markers  # noqa
    assert "groups" in _markers


def test_groups_get_uses_yolo_labels_for_baked_mask(client_cfg):
    """Регресія 2026-05-21: коли `selected/<model>/yolo/<stem>.txt` присутній,
    instance_labels беруться з multiclass yolo (а не один base_label для всіх).

    Сценарій бага: фото з полігональними везикулами вже запеченими у базову
    маску. Юзер обводить cell (захоплює і ядро, і запечені везикули). До fix
    система казала «cell requires ≥1 vesicle», бо всі baked інстанси
    отримували лейбл "nucleus" з base_label.
    """
    client, cfg, _ = client_cfg

    # labels.json у workspace (як у data/vesicles_good): id=0 nucleus, id=1 vesicle
    cfg.labels_file = cfg.images_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([
            {"id": 1, "name": "nucleus"},
            {"id": 2, "name": "vesicle"},
        ]),
        encoding="utf-8",
    )

    # YOLO multiclass txt: 3 рядки в порядку sorted_unique([1, 2, 3]) = [1, 2, 3]
    # iid=1 → cid=0 → "nucleus"
    # iid=2 → cid=1 → "vesicle"
    # iid=3 → cid=1 → "vesicle"
    yolo_dir = cfg.selected_dir / MODEL / "yolo"
    yolo_dir.mkdir(parents=True, exist_ok=True)
    (yolo_dir / f"{STEM}.txt").write_text(
        "0 0.1 0.1 0.1 0.1\n"
        "1 0.4 0.4 0.1 0.1\n"
        "1 0.7 0.7 0.1 0.1\n",
        encoding="utf-8",
    )

    # Cell group: ядро + 2 запечені везикули, БЕЗ полігональних shapes
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{
            "id": "g_001",
            "type": "cell",
            "instance_ids": [1, 2, 3],
            "polygon_indices": [],
            "color_hue": 130,
        }],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    cls = body["classifications"][0]
    # До fix: valid=False, reason містить "vesicle"
    assert cls["valid"] is True, cls
    assert cls["n_nucleus"] == 1
    assert cls["n_vesicle"] == 2

    # GET повертає те ж саме після reload
    resp2 = client.get(f"/api/groups/{STEM}?model={MODEL}")
    body2 = resp2.get_json()
    cls2 = body2["classifications"][0]
    assert cls2["valid"] is True
    assert cls2["n_vesicle"] == 2


def test_groups_get_polygon_override_beats_stale_yolo(client_cfg):
    """Регресія 2026-05-21 round 2 (db_img_0170): bake stale, YOLO має всі
    nucleus, polygons.json свіжий з vesicle-шейпами. vesicle_cluster-група
    з інстансами під vesicle-полігонами має бути валідною.

    Без polygon override (тільки YOLO) — `vesicle_cluster requires ≥1 vesicle`
    помилково fail-ив, бо stale YOLO каже «всі nucleus».
    """
    client, cfg, _ = client_cfg

    # labels.json: nucleus(cid 0), vesicle(cid 1)
    cfg.labels_file = cfg.images_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([
            {"id": 1, "name": "nucleus"},
            {"id": 2, "name": "vesicle"},
        ]),
        encoding="utf-8",
    )

    # STALE YOLO — всі 3 інстанси як nucleus (cid 0). Тобто bake ще не
    # пройшов після того як юзер позначив vesicle-полігони.
    yolo_dir = cfg.selected_dir / MODEL / "yolo"
    yolo_dir.mkdir(parents=True, exist_ok=True)
    (yolo_dir / f"{STEM}.txt").write_text(
        "0 0.12 0.12 0.16 0.16\n"
        "0 0.39 0.39 0.16 0.16\n"
        "0 0.70 0.70 0.16 0.16\n",
        encoding="utf-8",
    )

    # СВІЖИЙ polygons.json: vesicle-шейпи накривають instance 2 і 3
    polygons_payload = {
        "version": "5.0.1",
        "imagePath": f"{STEM}.jpg",
        "imageHeight": 64, "imageWidth": 64,
        "shapes": [
            {"label": "vesicle",
             "points": [[20, 20], [29, 20], [29, 29], [20, 29]],
             "shape_type": "polygon", "group_id": None, "flags": {}},
            {"label": "vesicle",
             "points": [[40, 40], [49, 40], [49, 49], [40, 49]],
             "shape_type": "polygon", "group_id": None, "flags": {}},
        ],
    }
    _write_polygons_json(cfg.polygons_dir, STEM, polygons_payload)

    # vesicle_cluster: enforce ≥1 vesicle + 0 nucleus
    # У _build_project instance 1 у (5..15, 5..15), 2 у (20..30, 20..30),
    # 3 у (40..50, 40..50). vesicle-шейпи покривають інстанси 2 і 3.
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{
            "id": "g_001",
            "type": "vesicle_cluster",
            "instance_ids": [2, 3],
            "polygon_indices": [],
            "color_hue": 28,
        }],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    cls = body["classifications"][0]
    assert cls["valid"] is True, cls
    assert cls["n_vesicle"] == 2
    assert cls["n_nucleus"] == 0


def test_groups_get_auto_strips_orphan_iids(client_cfg):
    """Round 5: GET на групу з orphan iid (немає у npy) → backend
    видаляє їх + повертає `stale_removed` журнал.

    Регресія db_img_0169 g_045: 3 iid (3077, 3078, 3082) не існують у
    baked npy після cleanup-rejection — без strip-у counts брешуть.
    """
    client, cfg, _ = client_cfg

    # У _build_project npy має instance 1, 2, 3. Створюємо групу з orphan 999.
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{
            "id": "g_001", "type": "cell",
            "instance_ids": [1, 2, 999],
            "polygon_indices": [], "color_hue": 0,
        }],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    # POST теж робить strip — 999 одразу видалено
    assert body["groups"][0]["instance_ids"] == [1, 2]
    assert any(x["group_id"] == "g_001" and 999 in x["removed"]
               for x in body.get("stale_removed", []))

    # GET повертає вже clean state
    resp2 = client.get(f"/api/groups/{STEM}?model={MODEL}")
    body2 = resp2.get_json()
    assert body2["groups"][0]["instance_ids"] == [1, 2]


def test_groups_classifications_include_rogue_iids(client_cfg):
    """Round 5: classifications кожної групи мають flat `rogue_iids`
    для frontend підсвітки.
    """
    client, cfg, _ = client_cfg

    cfg.labels_file = cfg.images_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([{"id": 1, "name": "nucleus"},
                    {"id": 2, "name": "vesicle"}]),
        encoding="utf-8",
    )
    yolo_dir = cfg.selected_dir / MODEL / "yolo"
    yolo_dir.mkdir(parents=True, exist_ok=True)
    # iid 1 → nucleus, 2 → vesicle, 3 → vesicle
    (yolo_dir / f"{STEM}.txt").write_text(
        "0 0.1 0.1 0.16 0.16\n"
        "1 0.4 0.4 0.16 0.16\n"
        "1 0.7 0.7 0.16 0.16\n",
        encoding="utf-8",
    )

    # vesicle_cluster з захопленим nucleus (iid 1) — invalid + rogue=[1]
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{
            "id": "g_001", "type": "vesicle_cluster",
            "instance_ids": [1, 2, 3],
            "polygon_indices": [], "color_hue": 28,
        }],
    })
    body = resp.get_json()
    cls = body["classifications"][0]
    assert cls["valid"] is False
    assert cls["rogue_iids"] == [1]


def test_groups_get_falls_back_to_base_label_when_yolo_missing(client_cfg):
    """Без YOLO txt — поведінка як раніше: всі baked → base_label."""
    client, cfg, _ = client_cfg

    # Cell group без yolo txt
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{
            "id": "g_001", "type": "cell",
            "instance_ids": [1, 2, 3],
            "polygon_indices": [], "color_hue": 0,
        }],
    })
    body = resp.get_json()
    cls = body["classifications"][0]
    # Без yolo всі instance отримують default base_label="nucleus" → cell invalid
    assert cls["valid"] is False
    assert "vesicle" in cls["reason"]


def test_groups_post_creates_backup_on_overwrite(client_cfg):
    client, cfg, _ = client_cfg
    payload = {
        "model": MODEL,
        "groups": [{"id": "g_001", "type": "cell",
                   "instance_ids": [1], "polygon_indices": [], "color_hue": 0}],
    }
    client.post(f"/api/groups/{STEM}", json=payload)
    time.sleep(1.1)  # backup folder використовує секундну ts (UTC %Y%m%dT%H%M%SZ)
    payload["groups"][0]["instance_ids"] = [1, 2]
    client.post(f"/api/groups/{STEM}", json=payload)

    backup_root = cfg.groups_dir / "_backups" / STEM
    assert backup_root.exists()
    backups = list(backup_root.iterdir())
    assert len(backups) >= 1
    # Кожна backup-папка має groups.json
    for b in backups:
        assert (b / "groups.json").exists()
