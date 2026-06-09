"""
Smoke тести для Stage C (polygons + markers).

Покриває:
  * GET  /api/polygons/<stem>                — порожній envelope коли файла нема
  * POST /api/polygons/<stem>                — save+reload roundtrip, бекап при перезаписі
  * POST /api/polygons/<stem>/seed-from-mask — CV2-seed з fake 3-instance маски
  * POST /api/cleanup/<stem>                 — markers roundtrip (autosave path)
  * _validate_polygons_payload               — юніт-перевірка валідатора
"""
from __future__ import annotations

import io
import importlib.util
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
    CV2_AVAILABLE,
    Config,
    ModelSource,
    POLYGON_BACKUP_KEEP,
    StateStore,
    _validate_polygons_payload,
    create_app,
)


STEM = "db_img_test"
MODEL = "cyto2"


def _build_project(tmp_path: Path):
    images = tmp_path / "images"
    output = tmp_path / "output"
    selected = tmp_path / "selected"
    skipped = tmp_path / "skipped"
    polygons = tmp_path / "polygons"

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

    Image.fromarray((labels * 80).astype(np.uint8), mode="L").save(
        model_out / "overlay" / f"{STEM}.png"
    )

    return images, output, selected, skipped, polygons


@pytest.fixture
def client_cfg(tmp_path):
    images, output, selected, skipped, polygons = _build_project(tmp_path)
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
        models=[model],
    )
    state = StateStore(selected.parent / "selections.json")
    app = create_app(cfg, state)
    app.testing = True
    with mask_picker_app._RGB_CACHE_LOCK:
        mask_picker_app._RGB_CACHE.clear()
    return app.test_client(), cfg, state


# ---------------------------------------------------------------------------
# Polygons GET/POST
# ---------------------------------------------------------------------------

def test_polygons_get_empty_envelope(client_cfg):
    """Якщо файла polygons/<stem>.json ще немає — повертається envelope з shapes=[]."""
    client, cfg, _ = client_cfg
    resp = client.get(f"/api/polygons/{STEM}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["shapes"] == []
    assert body["imageWidth"] == 64
    assert body["imageHeight"] == 64
    assert body["imagePath"] == f"{STEM}.jpg"
    assert body["version"] == "5.0.1"
    assert not (cfg.polygons_dir / f"{STEM}.json").exists(), \
        "GET не повинен створювати файл"


def test_polygons_post_roundtrip(client_cfg):
    """POST зберігає payload, GET повертає те саме."""
    client, cfg, _ = client_cfg
    payload = {
        "version": "5.0.1",
        "flags": {},
        "shapes": [{
            "label": "cell",
            "points": [[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0]],
            "group_id": None,
            "shape_type": "polygon",
            "flags": {},
        }],
        "imagePath": f"{STEM}.jpg",
        "imageData": None,
        "imageHeight": 64,
        "imageWidth": 64,
    }
    resp = client.post(f"/api/polygons/{STEM}", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["shape_count"] == 1

    # Файл має існувати і містити ті самі shapes.
    path = cfg.polygons_dir / f"{STEM}.json"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["shapes"] == payload["shapes"]

    # GET повертає той самий контент.
    got = client.get(f"/api/polygons/{STEM}").get_json()
    assert got["shapes"] == payload["shapes"]
    assert got["imageWidth"] == 64


def test_polygons_post_creates_backup_on_rewrite(client_cfg):
    """Другий POST повинен бекапити попередню версію у _backups/<stem>/<ts>/."""
    client, cfg, _ = client_cfg
    empty = {"shapes": [], "imageHeight": 64, "imageWidth": 64}

    resp = client.post(f"/api/polygons/{STEM}", json=empty)
    assert resp.status_code == 200
    time.sleep(1.1)  # ts-папка секундна

    empty2 = {
        "shapes": [{
            "label": "cell",
            "points": [[1.0, 1.0], [5.0, 1.0], [5.0, 5.0]],
            "shape_type": "polygon",
            "flags": {},
        }],
        "imageHeight": 64,
        "imageWidth": 64,
    }
    resp = client.post(f"/api/polygons/{STEM}", json=empty2)
    assert resp.status_code == 200

    backup_dir = cfg.polygons_dir / "_backups" / STEM
    assert backup_dir.exists()
    subs = sorted([p for p in backup_dir.iterdir() if p.is_dir()])
    assert len(subs) == 1
    assert (subs[0] / "polygons.json").exists()
    # Бекап містить перший (empty) payload.
    bk = json.loads((subs[0] / "polygons.json").read_text(encoding="utf-8"))
    assert bk["shapes"] == []


def test_polygons_post_rotates_backups(client_cfg):
    """Після POLYGON_BACKUP_KEEP+1 записів має лишитись POLYGON_BACKUP_KEEP бекапів."""
    client, cfg, _ = client_cfg
    base = {"shapes": [], "imageHeight": 64, "imageWidth": 64}
    for i in range(POLYGON_BACKUP_KEEP + 2):
        if i > 0:
            time.sleep(1.1)
        resp = client.post(f"/api/polygons/{STEM}", json=base)
        assert resp.status_code == 200

    backup_dir = cfg.polygons_dir / "_backups" / STEM
    subs = sorted([p for p in backup_dir.iterdir() if p.is_dir()])
    # Бекап робиться лише коли файл уже існує — тому після N+2 POST
    # маємо N+1 бекап, але rotate обрізає до POLYGON_BACKUP_KEEP.
    assert len(subs) == POLYGON_BACKUP_KEEP, (
        f"expected {POLYGON_BACKUP_KEEP} backups, got {len(subs)}: "
        f"{[p.name for p in subs]}"
    )


def test_polygons_post_validation_rejects_bad_shape(client_cfg):
    client, _, _ = client_cfg
    bad = {
        "shapes": [{"label": "x", "points": [[1, 2], [3]]}],  # другий pt некоректний
        "imageHeight": 64, "imageWidth": 64,
    }
    resp = client.post(f"/api/polygons/{STEM}", json=bad)
    assert resp.status_code == 400
    assert "points" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Seed-from-mask
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_seed_from_mask_extracts_three_shapes(client_cfg):
    """3 інстанси в fake-labels → 3 полігони з ≥3 вершин кожен."""
    client, _, _ = client_cfg
    resp = client.post(
        f"/api/polygons/{STEM}/seed-from-mask",
        json={"model": MODEL, "simplify_epsilon": 1.0, "min_area": 4},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["shape_count"] == 3
    env = body["envelope"]
    assert env["imageWidth"] == 64 and env["imageHeight"] == 64
    for sh in env["shapes"]:
        assert sh["shape_type"] == "polygon"
        assert sh["label"] == "nucleus"
        assert len(sh["points"]) >= 3
        for pt in sh["points"]:
            assert 0 <= pt[0] <= 64 and 0 <= pt[1] <= 64


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_seed_from_mask_skips_rejected_instances(client_cfg):
    """Якщо юзер уже rejected-нув інстанс у cleanup — seed не повинен його повертати."""
    client, _, _ = client_cfg
    # Pick + reject instance #2 через cleanup autosave
    client.post("/api/select", json={"stem": STEM, "model": MODEL})
    client.post(f"/api/cleanup/{STEM}",
                json={"model": MODEL, "rejected_instances": [2]})

    resp = client.post(
        f"/api/polygons/{STEM}/seed-from-mask",
        json={"model": MODEL, "simplify_epsilon": 1.0, "min_area": 4},
    )
    assert resp.status_code == 200
    assert resp.get_json()["shape_count"] == 2  # лишилось тільки 1 і 3


def test_seed_from_mask_requires_model(client_cfg):
    client, _, _ = client_cfg
    resp = client.post(f"/api/polygons/{STEM}/seed-from-mask", json={})
    # Якщо CV2 недоступний — 503; інакше — 400 бо немає model.
    assert resp.status_code in (400, 503)


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_seed_from_mask_instance_ids_filters_to_one(client_cfg):
    """Передача instance_ids=[id] → тільки одна форма (Pick-tool workflow)."""
    client, _, _ = client_cfg
    resp = client.post(
        f"/api/polygons/{STEM}/seed-from-mask",
        json={"model": MODEL, "simplify_epsilon": 1.0, "min_area": 4,
              "instance_ids": [1]},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["shape_count"] == 1


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_seed_from_mask_instance_ids_bad_type(client_cfg):
    """instance_ids з нечисловими значеннями → 400."""
    client, _, _ = client_cfg
    resp = client.post(
        f"/api/polygons/{STEM}/seed-from-mask",
        json={"model": MODEL, "instance_ids": "not-a-list"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Markers (Stage C) — roundtrip через /api/cleanup
# ---------------------------------------------------------------------------

def test_cleanup_markers_roundtrip(client_cfg):
    client, _, _ = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL})

    markers = [{"x": 12.5, "y": 34.5}, {"x": 40.0, "y": 55.0}]
    resp = client.post(
        f"/api/cleanup/{STEM}",
        json={"model": MODEL, "rejected_instances": [], "markers": markers},
    )
    assert resp.status_code == 200
    saved = resp.get_json()["cleanup"]
    assert saved["markers"] == markers

    got = client.get(f"/api/cleanup/{STEM}").get_json()
    assert got["markers"] == markers


def test_cleanup_markers_none_preserves_existing(client_cfg):
    """POST без поля markers не має затирати попередні маркери."""
    client, _, _ = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL})

    # Спершу зберігаємо маркери.
    client.post(f"/api/cleanup/{STEM}", json={
        "model": MODEL, "rejected_instances": [], "markers": [{"x": 1.0, "y": 2.0}],
    })
    # Потім пишемо rejected без markers — маркери мають лишитись.
    client.post(f"/api/cleanup/{STEM}", json={
        "model": MODEL, "rejected_instances": [1],
    })
    got = client.get(f"/api/cleanup/{STEM}").get_json()
    assert got["markers"] == [{"x": 1.0, "y": 2.0}]
    assert got["rejected_instances"] == [1]


def test_cleanup_markers_empty_list_clears(client_cfg):
    """markers=[] (порожній список) має явно очистити маркери."""
    client, _, _ = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL})
    client.post(f"/api/cleanup/{STEM}", json={
        "model": MODEL, "rejected_instances": [], "markers": [{"x": 1.0, "y": 2.0}],
    })
    client.post(f"/api/cleanup/{STEM}", json={
        "model": MODEL, "rejected_instances": [], "markers": [],
    })
    got = client.get(f"/api/cleanup/{STEM}").get_json()
    assert got["markers"] == []


def test_cleanup_markers_rejects_non_list(client_cfg):
    client, _, _ = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL})
    resp = client.post(f"/api/cleanup/{STEM}", json={
        "model": MODEL, "rejected_instances": [], "markers": "nope",
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Validator unit
# ---------------------------------------------------------------------------

def test_validate_polygons_payload_accepts_minimal():
    assert _validate_polygons_payload({"shapes": []}) is None
    assert _validate_polygons_payload({"shapes": [
        {"label": "c", "points": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]},
    ]}) is None


def test_validate_polygons_payload_rejects_garbage():
    assert _validate_polygons_payload("not a dict") is not None  # type: ignore[arg-type]
    assert _validate_polygons_payload({"shapes": "x"}) is not None
    assert _validate_polygons_payload({"shapes": [42]}) is not None
    assert _validate_polygons_payload(
        {"shapes": [{"label": "c", "points": "x"}]}
    ) is not None
    assert _validate_polygons_payload(
        {"shapes": [{"label": "c", "points": [["a", "b"]]}]}
    ) is not None


def test_validate_polygons_payload_fills_missing_shapes():
    data: dict = {}
    err = _validate_polygons_payload(data)
    assert err is None
    assert data["shapes"] == []


# ---------------------------------------------------------------------------
# Config endpoint reports polygons_dir
# ---------------------------------------------------------------------------

def test_api_config_exposes_polygons_dir(client_cfg):
    client, cfg, _ = client_cfg
    body = client.get("/api/config").get_json()
    assert body["polygons_dir"] == str(cfg.polygons_dir)
    assert "seed_from_mask" in body["features"]
    assert body["features"]["cleanup"] is not None  # sanity


def test_workspace_pick_folder_switches_runtime_config(client_cfg, tmp_path):
    client, cfg, state = client_cfg
    ws = tmp_path / "team_workspace"

    resp = client.post("/api/workspace/pick-folder", json={"path": str(ws)})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["workspace_dir"] == str(ws.resolve())
    assert cfg.workspace_dir == ws.resolve()
    assert cfg.images_dir == ws.resolve() / "images"
    assert cfg.selected_dir == ws.resolve() / "selected"
    assert state.path == ws.resolve() / "selections.json"
    assert cfg.images_dir.exists()
    assert cfg.output_root.exists()
    assert cfg.polygons_dir.exists()

    cfg_body = client.get("/api/config").get_json()
    assert cfg_body["workspace_mode"] is True
    assert cfg_body["workspace_dir"] == str(ws.resolve())


# ---------------------------------------------------------------------------
# Label classes
# ---------------------------------------------------------------------------

def test_labels_rename_updates_all_polygon_files(client_cfg):
    client, cfg, _ = client_cfg
    payload_a = {
        "version": "5.0.1",
        "flags": {},
        "shapes": [
            {
                "label": "cell",
                "points": [[1, 1], [5, 1], [5, 5]],
                "shape_type": "polygon",
                "flags": {},
            },
            {
                "label": "debris",
                "points": [[10, 10], [15, 10], [15, 15]],
                "shape_type": "polygon",
                "flags": {},
            },
        ],
        "imagePath": "a.jpg",
        "imageData": None,
        "imageHeight": 64,
        "imageWidth": 64,
    }
    payload_b = {
        "version": "5.0.1",
        "flags": {},
        "shapes": [{
            "label": "cell",
            "points": [[2, 2], [6, 2], [6, 6]],
            "shape_type": "polygon",
            "flags": {},
        }],
        "imagePath": "b.jpg",
        "imageData": None,
        "imageHeight": 64,
        "imageWidth": 64,
    }
    (cfg.polygons_dir / "a.json").write_text(
        json.dumps(payload_a, ensure_ascii=False), encoding="utf-8"
    )
    (cfg.polygons_dir / "b.json").write_text(
        json.dumps(payload_b, ensure_ascii=False), encoding="utf-8"
    )

    resp = client.post(
        "/api/labels/rename",
        json={"renames": [{"from": "cell", "to": "nucleus"}]},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["files_changed"] == 2
    assert body["shapes_changed"] == 2
    assert body["renamed"] == {"cell": "nucleus"}

    a = json.loads((cfg.polygons_dir / "a.json").read_text(encoding="utf-8"))
    b = json.loads((cfg.polygons_dir / "b.json").read_text(encoding="utf-8"))
    assert [sh["label"] for sh in a["shapes"]] == ["nucleus", "debris"]
    assert b["shapes"][0]["label"] == "nucleus"
    assert (cfg.polygons_dir / "_backups" / "a").exists()
    assert (cfg.polygons_dir / "_backups" / "b").exists()


def test_labels_rename_rejects_bad_payload(client_cfg):
    client, _, _ = client_cfg
    resp = client.post("/api/labels/rename", json={"renames": ["bad"]})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# Polygon Baking (Task 3)
# ---------------------------------------------------------------------------

def _poly_export(client, stem, shapes, model=MODEL, rejected=None):
    """Хелпер: POST /api/polygons-export/<stem>."""
    payload = {
        "version": "5.0.1", "flags": {},
        "shapes": shapes,
        "imageHeight": 64, "imageWidth": 64,
        "imagePath": f"{stem}.jpg", "imageData": None,
        "model": model,
        "rejected_instances": rejected or [],
    }
    return client.post(
        f"/api/polygons-export/{stem}",
        json=payload,
        content_type="application/json",
    )


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_polygon_fully_inside(client_cfg):
    """Полігон у порожній зоні → baked_count=1, новий ID у npy."""
    client, cfg, state = client_cfg
    state._data[STEM] = {"status": "selected", "model": MODEL}

    # Квадрат у порожній зоні (рядки 55:63, стовпці 50:63)
    shapes = [{"label": "nucleus", "points": [[50, 55], [62, 55], [62, 62], [50, 62]],
               "shape_type": "polygon", "group_id": None, "flags": {}}]
    resp = _poly_export(client, STEM, shapes)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["baked"] is True
    assert body["baked_count"] == 1
    assert body["skipped_reasons"] == []

    npy_out = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    assert npy_out.exists()
    saved = np.load(str(npy_out))
    # Всередині полігону — новий ID (не 0)
    assert saved[58, 55] > 3   # max original id = 3
    # Оригінальні інстанси не чіпали
    assert saved[7, 7] == 1


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_polygon_prefer_polygon_over_cellpose(client_cfg):
    """Полігон перекриває cellpose-інстанс → prefer polygon (пікселі мають новий ID)."""
    client, cfg, state = client_cfg
    state._data[STEM] = {"status": "selected", "model": MODEL}

    # Покриває instance 1 (рядки 5:15, стовпці 5:15)
    shapes = [{"label": "nucleus",
               "points": [[5, 5], [14, 5], [14, 14], [5, 14]],
               "shape_type": "polygon", "group_id": None, "flags": {}}]
    resp = _poly_export(client, STEM, shapes)
    body = resp.get_json()
    assert body["baked_count"] == 1

    saved = np.load(str(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"))
    # Пікселі що були instance 1 тепер мають новий ID (prefer polygon)
    assert saved[7, 7] > 3


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_polygon_out_of_bounds_warns(client_cfg):
    """Полігон з вершинами поза межами → warning + baked (fillPoly клампить)."""
    client, cfg, state = client_cfg
    state._data[STEM] = {"status": "selected", "model": MODEL}

    shapes = [{"label": "nucleus",
               "points": [[-5, 55], [70, 55], [70, 62], [-5, 62]],
               "shape_type": "polygon", "group_id": None, "flags": {}}]
    resp = _poly_export(client, STEM, shapes)
    body = resp.get_json()
    assert body["baked_count"] == 1
    warns = [w for w in body["overlap_warnings"] if w.get("note") == "out_of_bounds_clamped"]
    assert len(warns) == 1


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_two_polygons_small_overlap_both_baked(client_cfg):
    """Два полігони з ~17% IoU → обидва baked, overlap_warning."""
    client, cfg, state = client_cfg
    state._data[STEM] = {"status": "selected", "model": MODEL}

    # A: x[0:10), y[55:63]  B: x[7:17), y[55:63]  → перетин x[7:10) = 3 cols
    shapes = [
        {"label": "nucleus", "points": [[0, 55], [9, 55], [9, 62], [0, 62]],
         "shape_type": "polygon", "group_id": None, "flags": {}},
        {"label": "nucleus", "points": [[7, 55], [16, 55], [16, 62], [7, 62]],
         "shape_type": "polygon", "group_id": None, "flags": {}},
    ]
    resp = _poly_export(client, STEM, shapes)
    body = resp.get_json()
    assert body["baked_count"] == 2
    assert len(body["overlap_warnings"]) >= 1
    assert body["skipped_reasons"] == []


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_two_polygons_large_overlap_second_skipped(client_cfg):
    """Два полігони з >60% IoU → другий потрапляє у skipped_reasons."""
    client, cfg, state = client_cfg
    state._data[STEM] = {"status": "selected", "model": MODEL}

    # A і B майже ідентичні → IoU ≈ 64/66 > 60%
    shapes = [
        {"label": "nucleus", "points": [[0, 55], [9, 55], [9, 62], [0, 62]],
         "shape_type": "polygon", "group_id": None, "flags": {}},
        {"label": "nucleus", "points": [[1, 55], [10, 55], [10, 62], [1, 62]],
         "shape_type": "polygon", "group_id": None, "flags": {}},
    ]
    resp = _poly_export(client, STEM, shapes)
    body = resp.get_json()
    assert body["baked_count"] == 1
    assert len(body["skipped_reasons"]) == 1
    assert "overlap_over_60pct" in body["skipped_reasons"][0]["reason"]


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_default_label_is_nucleus(client_cfg):
    """seed-from-mask повертає label='nucleus' (не 'cell')."""
    client, _, _ = client_cfg
    resp = client.post(
        f"/api/polygons/{STEM}/seed-from-mask",
        json={"model": MODEL},
    )
    body = resp.get_json()
    assert body["ok"] is True
    for sh in body["envelope"]["shapes"]:
        assert sh["label"] == "nucleus"


def test_api_version(client_cfg):
    """GET /api/version → {name, version}; version slate-узгоджена з APP_VERSION."""
    client, _, _ = client_cfg
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Mask Picker"
    assert body["version"] == mask_picker_app.APP_VERSION
    assert body["version"] == "2.0.0"


def test_write_polygons_json_atomic(tmp_path):
    """_write_polygons_json: round-trip, перезапис, без .tmp-сміття (Day 9 Bug 2)."""
    from app import _write_polygons_json
    polydir = tmp_path / "polygons"

    def _payload(label):
        return {
            "version": "5.0.1", "flags": {},
            "shapes": [{"label": label,
                        "points": [[1, 2], [3, 4], [5, 6]],
                        "shape_type": "polygon"}],
            "imagePath": "db_img_x.jpg", "imageData": None,
            "imageHeight": 64, "imageWidth": 64,
        }

    path = _write_polygons_json(polydir, "db_img_x", _payload("nucleus"))
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["shapes"][0]["label"] == "nucleus"

    # Перезапис — нова версія перемагає.
    _write_polygons_json(polydir, "db_img_x", _payload("vesicle"))
    assert json.loads(path.read_text(encoding="utf-8"))["shapes"][0]["label"] == "vesicle"

    # Атомарний запис не лишає тимчасових файлів у папці.
    leftovers = [p.name for p in polydir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"leftover tmp files: {leftovers}"


# ---------------------------------------------------------------------------
# Multi-class seed (v1.6.0)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_multi_seed_two_classes_no_overlap(client_cfg):
    """2 mappings, 2 різні моделі (тут одна тестова), різні label → shapes_added > 0."""
    client, cfg, _ = client_cfg
    # У client_cfg є тільки одна модель MODEL=cyto2 з 3 інстансами,
    # але endpoint приймає список mappings — використовуємо одну модель з 2 lables
    # щоб переконатись що логіка циклу працює (instance-rejected dedupe не застосовується
    # для різних labels — overlap detect spotаt).
    resp = client.post(
        f"/api/polygons/{STEM}/multi-seed",
        json={"mappings": [
            {"label": "nucleus", "model": MODEL},
            {"label": "vesicle", "model": MODEL},
        ]},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    # Перший mapping додає 3 shapes (3 instances у фікстурі).
    # Другий mapping — ті ж самі області → IoU=100% → всі skipped.
    assert body["shapes_added"] == 3
    assert body["shapes_skipped_overlap"] == 3
    # envelope містить 3 shapes з label=nucleus
    labels = [sh["label"] for sh in body["envelope"]["shapes"]]
    assert labels.count("nucleus") == 3
    assert labels.count("vesicle") == 0


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_multi_seed_unknown_model_400(client_cfg):
    """Невідома модель у mappings → 400."""
    client, _, _ = client_cfg
    resp = client.post(
        f"/api/polygons/{STEM}/multi-seed",
        json={"mappings": [{"label": "nucleus", "model": "nonexistent_model"}]},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "unknown model" in body["error"]


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_multi_seed_empty_mappings_400(client_cfg):
    """Порожній mappings → 400."""
    client, _, _ = client_cfg
    resp = client.post(
        f"/api/polygons/{STEM}/multi-seed",
        json={"mappings": []},
    )
    assert resp.status_code == 400
    assert "mappings" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Workspace import (v1.6.1)
# ---------------------------------------------------------------------------

def _make_zip(members: dict[str, bytes]) -> io.BytesIO:
    """In-memory ZIP з {arcname: bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in members.items():
            zf.writestr(arcname, data)
    buf.seek(0)
    return buf


def _switch_to_workspace(client, ws_path: Path):
    """Активує workspace mode для тестів імпорту."""
    resp = client.post("/api/workspace/pick-folder", json={"path": str(ws_path)})
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_workspace_import_flat_zip_extracts_images(client_cfg, tmp_path):
    """Baseline: плоский ZIP з зображеннями на корені → у images/."""
    client, cfg, _ = client_cfg
    _switch_to_workspace(client, tmp_path / "ws_flat")

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    zbuf = _make_zip({"a.jpg": b"jpegdata", "b.jpg": b"jpegdata2", "ignored.txt": b"x"})

    resp = client.post(
        "/api/workspace/import",
        data={"files": (zbuf, "flat.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["imported"] == 2
    assert (cfg.images_dir / "a.jpg").exists()
    assert (cfg.images_dir / "b.jpg").exists()


def test_workspace_import_structural_zip_no_prefix(client_cfg, tmp_path):
    """Baseline: structural ZIP без обгортки → файли зберігають структуру + cfg.models updated."""
    client, cfg, _ = client_cfg
    _switch_to_workspace(client, tmp_path / "ws_struct")

    zbuf = _make_zip({
        "images/x.jpg": b"jpegdata",
        "output/cyto2/overlay/x.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    })

    resp = client.post(
        "/api/workspace/import",
        data={"files": (zbuf, "struct.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["imported"] == 2
    assert (cfg.workspace_dir / "images" / "x.jpg").exists()
    assert (cfg.workspace_dir / "output" / "cyto2" / "overlay" / "x.png").exists()
    assert "cyto2" in [m.name for m in cfg.models]


def test_workspace_import_zip_with_prefix_extracts_output(client_cfg, tmp_path):
    """Головний фікс v1.6.1: ZIP з обгорткою `_workspace/` → префікс стрипається."""
    client, cfg, _ = client_cfg
    _switch_to_workspace(client, tmp_path / "ws_prefix")

    zbuf = _make_zip({
        "_workspace/images/x.jpg": b"jpegdata",
        "_workspace/output/cyto2/overlay/x.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    })

    resp = client.post(
        "/api/workspace/import",
        data={"files": (zbuf, "prefixed.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["imported"] == 2
    # Файли лежать БЕЗ префіксу `_workspace/`
    assert (cfg.workspace_dir / "images" / "x.jpg").exists()
    assert (cfg.workspace_dir / "output" / "cyto2" / "overlay" / "x.png").exists()
    assert not (cfg.workspace_dir / "_workspace").exists()
    # cfg.models оновлено без switch_workspace
    assert "cyto2" in [m.name for m in cfg.models]


def test_api_exclude_moves_image_and_marks_state(client_cfg):
    """v1.6.6: POST /api/exclude/<stem> переносить файл у _excluded/ + status='excluded'."""
    client, cfg, state = client_cfg

    src = cfg.images_dir / f"{STEM}.jpg"
    assert src.exists(), "fixture повинна створити jpg"

    resp = client.post(f"/api/exclude/{STEM}", json={"user": "test"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert f"{STEM}.jpg" in body["moved"]

    # Файл переїхав у _excluded/
    excluded = cfg.images_dir.parent / "_excluded" / f"{STEM}.jpg"
    assert excluded.exists()
    assert not src.exists()

    # State оновлено
    entry = state.get(STEM)
    assert entry["status"] == "excluded"

    # Catalog показує excluded item з прапорцем
    catalog = client.get("/api/catalog?fresh=1").get_json()
    items = catalog["items"]
    excluded_items = [it for it in items if it.get("excluded")]
    assert any(it["stem"] == STEM for it in excluded_items)


def test_api_exclude_unknown_stem_404(client_cfg):
    """v1.6.6: exclude неіснуючого стему → 404."""
    client, _, _ = client_cfg
    resp = client.post("/api/exclude/db_img_nonexistent", json={})
    assert resp.status_code == 404


def test_api_shutdown_returns_ok(client_cfg, monkeypatch):
    """v1.6.5: POST /api/shutdown → {ok:true}. os._exit замоканий, інакше вб'є pytest."""
    import os as _os
    killed = []
    monkeypatch.setattr(_os, "_exit", lambda code: killed.append(code))

    client, _, _ = client_cfg
    resp = client.post("/api/shutdown")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    # Поток чекає 0.2 c — підождемо трохи більше і перевіримо що kill викликався.
    time.sleep(0.4)
    assert killed == [0]


def test_overlay_clean_when_rejected_present(client_cfg):
    """v1.6.4: GET /api/overlay/<model>/<stem> повертає clean overlay (без rejected)
    якщо у selections.json для цієї моделі є rejected_instances."""
    client, cfg, state = client_cfg

    # Спершу — raw bytes для baseline
    resp_raw = client.get(f"/api/overlay/{MODEL}/{STEM}")
    assert resp_raw.status_code == 200
    raw_bytes = resp_raw.data

    # Запишемо cleanup з 1 rejected ID. Fixture _build_project малює labels 1,2,3.
    state.set_cleanup(STEM, MODEL, rejected_instances=[2], user="test")

    resp_clean = client.get(f"/api/overlay/{MODEL}/{STEM}")
    assert resp_clean.status_code == 200
    clean_bytes = resp_clean.data
    assert clean_bytes != raw_bytes, "clean overlay має відрізнятись від raw"

    # Прибираємо cleanup → знову raw
    state.set_cleanup(STEM, MODEL, rejected_instances=[], user="test")
    resp_back = client.get(f"/api/overlay/{MODEL}/{STEM}")
    assert resp_back.status_code == 200
    assert resp_back.data == raw_bytes


def test_workspace_import_zip_with_prefix_meta_files_at_root(client_cfg, tmp_path):
    """selections.json/labels.json з префіксом → у корінь workspace."""
    client, cfg, _ = client_cfg
    _switch_to_workspace(client, tmp_path / "ws_meta")

    zbuf = _make_zip({
        "_workspace/selections.json": b'{"version": 1, "items": {}}',
        "_workspace/labels.json": b'[]',
        "_workspace/images/y.jpg": b"jpegdata",
    })

    resp = client.post(
        "/api/workspace/import",
        data={"files": (zbuf, "meta.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["imported"] == 3
    assert (cfg.workspace_dir / "selections.json").exists()
    assert (cfg.workspace_dir / "labels.json").exists()
    assert (cfg.workspace_dir / "images" / "y.jpg").exists()


def test_workspace_finalize_current_exports_single_photo_bundle(client_cfg):
    client, cfg, _ = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([{"id": 1, "name": "nucleus", "color": "#4488ff"}]),
        encoding="utf-8",
    )

    resp = client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "tester"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    payload = {
        "shapes": [{
            "label": "nucleus",
            "points": [[1.0, 1.0], [8.0, 1.0], [8.0, 8.0]],
            "shape_type": "polygon",
            "flags": {},
        }],
        "imageHeight": 64,
        "imageWidth": 64,
    }
    resp = client.post(f"/api/polygons/{STEM}", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    resp = client.get(f"/api/workspace/finalize/{STEM}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())
        selections = json.loads(zf.read("selections.json").decode("utf-8"))

    assert f"images/{STEM}.jpg" in names
    assert f"polygons/{STEM}.json" in names
    assert f"selected/{MODEL}/overlay/{STEM}.png" in names
    assert f"selected/{MODEL}/npy/{STEM}.npy" in names
    assert "labels.json" in names
    assert set(selections) == {STEM}
    assert selections[STEM]["copied_files"]["overlay"] == (
        f"selected/{MODEL}/overlay/{STEM}.png"
    )


# ---------------------------------------------------------------------------
# v1.7 render / generic bake_all
# ---------------------------------------------------------------------------

def test_export_segmentation_bundle_matches_pipeline(tmp_path):
    """v1.7: pipeline and Mask Picker baking share the same render/export helper."""
    from cellsegkit.exporter.exporter import export_segmentation_bundle
    from cellsegkit.pipeline.pipeline import run_segmentation

    input_dir = tmp_path / "images"
    input_dir.mkdir()
    image = np.full((32, 32), 40, dtype=np.uint8)
    Image.fromarray(image, mode="L").save(input_dir / "sample.jpg")

    labels = np.zeros((32, 32), dtype=np.int32)
    labels[4:12, 4:12] = 1
    labels[18:26, 18:26] = 2

    class FakeSegmenter:
        def load_image(self, file_path):
            return np.array(Image.open(file_path))

        def segment(self, image_arr):
            return labels

    out_pipeline = tmp_path / "pipeline"
    out_helper = tmp_path / "helper"
    run_segmentation(
        FakeSegmenter(),
        str(input_dir),
        str(out_pipeline),
        export_formats=("overlay", "npy", "png", "yolo"),
    )
    result = export_segmentation_bundle(
        labels,
        out_helper,
        "sample",
        image=np.array(Image.open(input_dir / "sample.jpg")),
        export_formats=("overlay", "npy", "png", "yolo"),
        silent=True,
    )
    assert result["errors"] == []

    for rel in [
        "npy/sample.npy",
        "png/sample.png",
        "yolo/sample.txt",
        "overlay/sample.png",
    ]:
        assert (out_pipeline / rel).read_bytes() == (out_helper / rel).read_bytes()


def test_bake_all_pack_zip_uses_data_dir_name(tmp_path):
    """bake_all.py is generic: --pack layout follows the selected data-dir name."""
    root = Path(__file__).resolve().parents[3]
    bake_path = root / "tools" / "launchers" / "bake_all.py"
    spec = importlib.util.spec_from_file_location("bake_all_tool", bake_path)
    bake_all_tool = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(bake_all_tool)

    data_dir = tmp_path / "my_dataset"
    model = "cyto2"
    stem = "sample"
    for d in [
        data_dir / "images",
        data_dir / "selected" / model / "png",
        data_dir / "selected" / model / "npy",
        data_dir / "selected" / model / "yolo",
        data_dir / "selected" / model / "overlay",
        data_dir / "polygons",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    Image.fromarray(np.zeros((8, 8), dtype=np.uint8), mode="L").save(data_dir / "images" / f"{stem}.jpg")
    Image.fromarray(np.zeros((8, 8), dtype=np.uint8), mode="P").save(data_dir / "selected" / model / "png" / f"{stem}.png")
    Image.fromarray(np.zeros((8, 8), dtype=np.uint8), mode="L").save(data_dir / "selected" / model / "overlay" / f"{stem}.png")
    np.save(data_dir / "selected" / model / "npy" / f"{stem}.npy", np.zeros((8, 8), dtype=np.int32))
    (data_dir / "selected" / model / "yolo" / f"{stem}.txt").write_text("", encoding="utf-8")
    (data_dir / "polygons" / f"{stem}.json").write_text('{"shapes":[]}', encoding="utf-8")
    (data_dir / "labels.json").write_text("[]", encoding="utf-8")
    (data_dir / "selections.json").write_text(
        json.dumps({stem: {"status": "selected", "model": model}}),
        encoding="utf-8",
    )

    archive_dir = tmp_path / "archive"
    zip_path = bake_all_tool.pack_zip(data_dir, archive_dir)
    assert zip_path.name == "dataset_my_dataset.zip"

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "dataset_my_dataset/labels.json" in names
    assert "dataset_my_dataset/selections.json" in names
    assert f"dataset_my_dataset/images/{stem}.jpg" in names
    assert f"dataset_my_dataset/masks/{stem}.png" in names
    assert f"dataset_my_dataset/masks_npy/{stem}.npy" in names
    assert f"dataset_my_dataset/yolo/{stem}.txt" in names
    assert f"dataset_my_dataset/overlays/{stem}.png" in names
    assert f"dataset_my_dataset/polygons/{stem}.json" in names


# ---------------------------------------------------------------------------
# v1.7.1 regression tests (BOM, base_label, finalize rebake, manual-over-rejected)
# ---------------------------------------------------------------------------

def test_state_store_loads_utf8_bom_json(tmp_path):
    """PowerShell ConvertTo-Json пише UTF-8 з BOM. _load() мусить читати без помилок."""
    sel = tmp_path / "selections.json"
    payload = {"db_img_0084": {"status": "selected", "model": "instanseg",
                                "base_label": "vesicle"}}
    sel.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    store = StateStore(sel)
    entry = store.get("db_img_0084")
    assert entry is not None, "Entry not loaded — BOM-парсер впав?"
    assert entry["base_label"] == "vesicle"
    assert entry["model"] == "instanseg"


def test_polygons_export_uses_base_label_for_model_instances(client_cfg):
    """base_label='vesicle' → YOLO class_id=1 для модельних (немануальних) інстансів.

    Класи: nucleus=index 0, vesicle=index 1.
    Фікстурний npy має 3 модельні інстанси у [5:15,5:15], [20:30,20:30], [40:50,40:50].
    Додаємо 1 manual vesicle-полігон у вільній зоні [55:60,0:5] — щоб bake запустився
    (без shapes export пропускає bake). Усі yolo лінії = class 1 (vesicle).
    """
    client, cfg, _ = client_cfg
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([
            {"id": 1, "name": "nucleus", "color": "#4488ff"},
            {"id": 2, "name": "vesicle", "color": "#ff8844"},
        ]),
        encoding="utf-8",
    )
    resp = client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "tester"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    payload = {
        "version": "5.0.1",
        "flags": {},
        "shapes": [{
            "label": "vesicle",
            "points": [[55.0, 0.0], [60.0, 0.0], [60.0, 5.0], [55.0, 5.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imagePath": f"{STEM}.jpg",
        "imageData": None,
        "imageHeight": 64,
        "imageWidth": 64,
        "model": MODEL,
        "base_label": "vesicle",
    }
    resp = client.post(f"/api/polygons-export/{STEM}", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["base_label"] == "vesicle"
    assert body["base_class_id"] == 1
    assert body["baked"] is True

    yolo_path = cfg.selected_dir / MODEL / "yolo" / f"{STEM}.txt"
    assert yolo_path.exists(), "yolo file not written"
    lines = [ln for ln in yolo_path.read_text(encoding="utf-8").splitlines() if ln]
    # 3 модельні + 1 manual = 4 рядки
    assert len(lines) == 4, f"expected 4 yolo lines (3 model + 1 manual), got {lines!r}"
    assert all(ln.split()[0] == "1" for ln in lines), \
        f"усі (model + manual vesicle) → class 1, got: {lines!r}"


def test_polygons_export_default_base_label_is_class_zero(client_cfg):
    """Без base_label → default_class_id=0 (nucleus). Регресія для старих стемів."""
    client, cfg, _ = client_cfg
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([
            {"id": 1, "name": "nucleus", "color": "#4488ff"},
            {"id": 2, "name": "vesicle", "color": "#ff8844"},
        ]),
        encoding="utf-8",
    )
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    payload = {
        "shapes": [{
            "label": "nucleus",
            "points": [[55.0, 0.0], [60.0, 0.0], [60.0, 5.0], [55.0, 5.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
        "model": MODEL,
    }
    resp = client.post(f"/api/polygons-export/{STEM}", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["base_class_id"] == 0
    yolo_path = cfg.selected_dir / MODEL / "yolo" / f"{STEM}.txt"
    lines = [ln for ln in yolo_path.read_text(encoding="utf-8").splitlines() if ln]
    assert lines
    assert all(ln.split()[0] == "0" for ln in lines), \
        f"без base_label всі інстанси мусять бути class 0, got: {lines!r}"


def test_finalize_rebakes_when_polygons_changed_externally(client_cfg):
    """Finalize rebake'ає selected/<stem>.npy навіть якщо polygons.json змінено
    зовні (autosave з фронтенду залишив dirty=False)."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([{"id": 1, "name": "nucleus", "color": "#4488ff"}]),
        encoding="utf-8",
    )
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})

    # 1. Перший save+bake через polygons-export з одним малим shape
    initial = {
        "shapes": [{
            "label": "nucleus",
            "points": [[1.0, 1.0], [5.0, 1.0], [5.0, 5.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
        "model": MODEL,
    }
    resp = client.post(f"/api/polygons-export/{STEM}", json=initial)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    npy_path = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    first_npy = npy_path.read_bytes()

    # 2. Імітуємо autosave з фронтенду (POST /api/polygons/<stem> — без bake!).
    # Великий manual shape, який реально перекриє якісь модельні пікселі.
    autosaved = {
        "shapes": [{
            "label": "nucleus",
            "points": [[35.0, 35.0], [60.0, 35.0], [60.0, 60.0], [35.0, 60.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
    }
    resp = client.post(f"/api/polygons/{STEM}", json=autosaved)
    assert resp.status_code == 200

    # 3. Finalize мусить зчитати свіжий polygons.json і rebake'нути
    resp = client.get(f"/api/workspace/finalize/{STEM}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    second_npy = npy_path.read_bytes()
    assert second_npy != first_npy, \
        "Finalize не зробив rebake — selected/.npy не оновився після зміни polygons.json"


def test_polygons_export_keeps_manual_over_rejected_zone(client_cfg):
    """Regression: manual polygon у тій самій зоні, що й rejected instance,
    мусить запектися (rejected стосується лише модельної маски, не manual)."""
    client, cfg, _ = client_cfg
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([{"id": 1, "name": "nucleus", "color": "#4488ff"}]),
        encoding="utf-8",
    )
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})

    # Інстанс id=1 у фікстурі лежить на пікселях [5:15, 5:15].
    # Manual polygon у тій самій зоні + rejected_instances=[1].
    payload = {
        "shapes": [{
            "label": "nucleus",
            "points": [[6.0, 6.0], [12.0, 6.0], [12.0, 12.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
        "model": MODEL,
        "rejected_instances": [1],
    }
    resp = client.post(f"/api/polygons-export/{STEM}", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["baked"] is True
    assert body["baked_count"] == 1, \
        f"manual polygon мусить запектися навіть над rejected, got {body!r}"


# ---------------------------------------------------------------------------
# v2.0.0 Day 3a — Auto-bake on Pick (POST /api/rebake/<stem>)
# ---------------------------------------------------------------------------

def _setup_labels_file(cfg):
    """Allocate labels.json with 2 classes for rebake tests."""
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([
            {"id": 1, "name": "nucleus", "color": "#4488ff"},
            {"id": 2, "name": "vesicle", "color": "#ff8844"},
        ]),
        encoding="utf-8",
    )


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_no_data_skips(client_cfg):
    """Fresh Pick без polygons + без cleanup → skipped='no_data', нема backup."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["skipped"] == "no_data"
    assert body["baked"] is False
    assert body["baked_count"] == 0
    assert body["shapes_count"] == 0
    assert body["rejected_count"] == 0

    backup_dir = cfg.selected_dir / MODEL / "_backups" / STEM
    assert not backup_dir.exists(), "skipped rebake не повинен робити backup"


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_with_polygons_only(client_cfg):
    """Є shapes у polygons.json, нема rejected → bake'ається."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})

    # Polygon у вільній зоні (далеко від модельних інстансів)
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [{
            "label": "nucleus",
            "points": [[50.0, 55.0], [62.0, 55.0], [62.0, 62.0], [50.0, 62.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
    })

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["skipped"] is None
    assert body["baked"] is True
    assert body["baked_count"] == 1
    assert body["shapes_count"] == 1
    assert body["rejected_count"] == 0

    npy_out = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    assert npy_out.exists()
    saved = np.load(str(npy_out))
    assert saved[58, 55] > 3, "polygon pixel мусить мати новий ID"


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_with_rejected_only(client_cfg):
    """Нема shapes, є rejected у state.cleanup → bake фільтрує rejected."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})

    # Reject instance #2 (рядки 20:30, стовпці 20:30 у фікстурі)
    client.post(f"/api/cleanup/{STEM}",
                json={"model": MODEL, "rejected_instances": [2]})

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["skipped"] is None
    assert body["baked"] is True
    assert body["baked_count"] == 0  # нема manual shapes
    assert body["rejected_count"] == 1

    saved = np.load(str(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"))
    assert 2 not in [int(i) for i in np.unique(saved)], \
        "rejected instance 2 мусить бути викинутий"
    # Інші instance ID лишаються
    remaining = sorted(int(i) for i in np.unique(saved) if i > 0)
    assert 1 in remaining and 3 in remaining


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_with_polygons_and_rejected(client_cfg):
    """Обидва — shapes + rejected — bake включає обидва."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})

    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [{
            "label": "nucleus",
            "points": [[50.0, 55.0], [62.0, 55.0], [62.0, 62.0], [50.0, 62.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
    })
    client.post(f"/api/cleanup/{STEM}",
                json={"model": MODEL, "rejected_instances": [2]})

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["baked"] is True
    assert body["baked_count"] == 1
    assert body["rejected_count"] == 1

    saved = np.load(str(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"))
    assert 2 not in [int(i) for i in np.unique(saved)]
    assert saved[58, 55] > 3


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_unknown_model_400(client_cfg):
    """Невідома model → 400."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    resp = client.post(f"/api/rebake/{STEM}", json={"model": "nonexistent_model"})
    assert resp.status_code == 400
    assert "unknown model" in resp.get_json()["error"]


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_no_model_uses_state_fallback(client_cfg):
    """Без body.model, але є state[stem].model → bake."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [{
            "label": "nucleus",
            "points": [[50.0, 55.0], [62.0, 55.0], [62.0, 62.0], [50.0, 62.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
    })

    resp = client.post(f"/api/rebake/{STEM}", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["baked"] is True
    assert body["baked_count"] == 1


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_no_model_anywhere_400(client_cfg):
    """Без body.model і без state[stem] → 400."""
    client, _, _ = client_cfg
    resp = client.post(f"/api/rebake/{STEM}", json={})
    assert resp.status_code == 400
    assert "model required" in resp.get_json()["error"]


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_skips_cleanup_for_different_model(client_cfg):
    """state.cleanup.model='other' + active='cyto2' → rejected ignored, safety-skipped."""
    client, cfg, state = client_cfg
    _setup_labels_file(cfg)
    # Симулюємо state з cleanup, прив'язаним до ІНШОЇ моделі (не активної).
    state._data[STEM] = {
        "status": "selected",
        "model": MODEL,
        "cleanup": {"model": "other_model", "rejected_instances": [1, 2]},
    }

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["skipped"] == "no_data", \
        f"rejected з іншої моделі мусять бути проігноровані, got {body!r}"
    assert body["rejected_count"] == 0
    assert body["baked"] is False


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_creates_backup_when_bakes(client_cfg):
    """Successful rebake → backup у selected/<model>/_backups/<stem>/."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [{
            "label": "nucleus",
            "points": [[50.0, 55.0], [62.0, 55.0], [62.0, 62.0], [50.0, 62.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
    })

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["baked"] is True

    backup_dir = cfg.selected_dir / MODEL / "_backups" / STEM
    assert backup_dir.exists(), "bake мусить створити backup"
    subs = [p for p in backup_dir.iterdir() if p.is_dir()]
    assert len(subs) >= 1, f"очікую ≥1 timestamped backup subdir, got {subs!r}"


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_no_npy_returns_404(client_cfg):
    """Якщо .npy зник з диску (модель видалили) → 404 з skipped='no_npy'."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [{
            "label": "nucleus",
            "points": [[50.0, 55.0], [62.0, 55.0], [62.0, 62.0], [50.0, 62.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
    })
    # Видаляємо .npy щоб симулювати "no .npy for model/stem"
    npy_path = cfg.output_root / MODEL / "npy" / f"{STEM}.npy"
    npy_path.unlink()

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 404, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["skipped"] == "no_npy"
    assert "no .npy" in body["error"]


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_rebake_respects_base_label(client_cfg):
    """base_label з state → YOLO class ID для модельних інстансів."""
    client, cfg, _ = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/base-label/{STEM}", json={"label": "vesicle"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [{
            "label": "nucleus",
            "points": [[55.0, 0.0], [60.0, 0.0], [60.0, 5.0], [55.0, 5.0]],
            "shape_type": "polygon", "flags": {},
        }],
        "imageHeight": 64, "imageWidth": 64,
    })

    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["base_label"] == "vesicle"
    assert body["base_class_id"] == 1

    yolo = cfg.selected_dir / MODEL / "yolo" / f"{STEM}.txt"
    lines = [ln for ln in yolo.read_text(encoding="utf-8").splitlines() if ln]
    classes = sorted(int(ln.split()[0]) for ln in lines)
    # 3 model instances → class 1 (vesicle), 1 manual nucleus → class 0
    assert classes == [0, 1, 1, 1], f"got {classes} from {lines!r}"


# ---------------------------------------------------------------------------
# v1.7.1 QA pass — additional regression
# ---------------------------------------------------------------------------

def test_base_label_persisted_via_api_round_trip(client_cfg):
    """POST /api/base-label/<stem> → selections.json[stem].base_label,
    видно в /api/catalog?fresh=1 → item.state.base_label.
    Покриває per-image baseLabel UI persistence."""
    client, cfg, state = client_cfg
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([
            {"id": 1, "name": "nucleus", "color": "#4488ff"},
            {"id": 2, "name": "vesicle", "color": "#ff8844"},
        ]),
        encoding="utf-8",
    )
    # Pick ОБОВ'ЯЗКОВО перед base-label, інакше state["status"] != "selected"
    resp = client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # POST base-label
    resp = client.post(f"/api/base-label/{STEM}", json={"label": "vesicle"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # Перевірка через StateStore
    entry = state.get(STEM)
    assert entry["base_label"] == "vesicle", entry

    # Перевірка через /api/catalog?fresh=1 (як це робить frontend на завантаженні)
    catalog = client.get("/api/catalog?fresh=1").get_json()
    item = next(it for it in catalog["items"] if it["stem"] == STEM)
    assert item["state"]["base_label"] == "vesicle", item


def test_finalize_zip_yolo_class_distribution_matches_base_label(client_cfg):
    """Regression для refactor v1.7.1 (3.1): Finalize ZIP має правильний
    YOLO class розподіл коли base_label = vesicle. 3 модельні інстанси у
    fixture + 1 manual nucleus = всі модельні в class 1, manual у class 0."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    cfg.labels_file = cfg.selected_dir.parent / "labels.json"
    cfg.labels_file.write_text(
        json.dumps([
            {"id": 1, "name": "nucleus", "color": "#4488ff"},
            {"id": 2, "name": "vesicle", "color": "#ff8844"},
        ]),
        encoding="utf-8",
    )
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/base-label/{STEM}", json={"label": "vesicle"})

    # Один manual nucleus polygon у вільній зоні fixture (далеко від модельних інстансів)
    resp = client.post(f"/api/polygons/{STEM}", json={
        "shapes": [{
            "label": "nucleus",
            "points": [[55.0, 0.0], [60.0, 0.0], [60.0, 5.0], [55.0, 5.0]],
            "shape_type": "polygon",
            "flags": {},
        }],
        "imageHeight": 64,
        "imageWidth": 64,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # Finalize → ZIP має містити YOLO з 3 model class-1 + 1 manual class-0
    resp = client.get(f"/api/workspace/finalize/{STEM}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        yolo_path = f"selected/{MODEL}/yolo/{STEM}.txt"
        assert yolo_path in zf.namelist(), zf.namelist()
        yolo_text = zf.read(yolo_path).decode("utf-8")

    lines = [ln for ln in yolo_text.splitlines() if ln.strip()]
    classes = sorted(int(ln.split()[0]) for ln in lines)
    # 3 model instances → class 1 (vesicle), 1 manual nucleus → class 0
    assert classes == [0, 1, 1, 1], (
        f"expected [0,1,1,1] (1 manual nucleus + 3 model vesicle), "
        f"got {classes} from lines: {lines!r}"
    )


def test_exclude_then_filesystem_restore_reappears_in_catalog(client_cfg):
    """v1.6.6: exclude → file moves to _excluded/. Manual move back → catalog
    знов показує item (без excluded прапорця).
    Regression — restore working при ручному moveʼі назад."""
    client, cfg, state = client_cfg
    src = cfg.images_dir / f"{STEM}.jpg"

    # 1. Exclude
    resp = client.post(f"/api/exclude/{STEM}", json={"user": "t"})
    assert resp.status_code == 200
    excluded = cfg.images_dir.parent / "_excluded" / f"{STEM}.jpg"
    assert excluded.exists()
    assert not src.exists()

    catalog = client.get("/api/catalog?fresh=1").get_json()
    excluded_items = [it for it in catalog["items"] if it.get("excluded")]
    assert any(it["stem"] == STEM for it in excluded_items)

    # 2. Manual restore: move file back з _excluded/ у images/
    excluded.rename(src)
    assert src.exists()
    assert not excluded.exists()

    # User також повинен прибрати "excluded" status вручну (це частина known
    # restore flow). Імітуємо це через unset.
    resp = client.post("/api/unset", json={"stem": STEM, "user": "t"})
    assert resp.status_code == 200

    # 3. Catalog знов має item без excluded прапорця
    catalog = client.get("/api/catalog?fresh=1").get_json()
    items = catalog["items"]
    item = next((it for it in items if it["stem"] == STEM), None)
    assert item is not None, "after restore item must reappear in catalog"
    assert not item.get("excluded"), f"excluded flag must be cleared, got: {item!r}"


# ---------------------------------------------------------------------------
# Day 7 — lazy-bake: dirty flag + Save All (bake-all)
# ---------------------------------------------------------------------------

_FREE_ZONE_POLYGON = {
    "label": "nucleus",
    "points": [[50.0, 55.0], [62.0, 55.0], [62.0, 62.0], [50.0, 62.0]],
    "shape_type": "polygon", "flags": {},
}


def _wait_bake_done(client, timeout=30.0):
    """Polling /api/workspace/bake-progress поки фоновий bake-all завершиться."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = client.get("/api/workspace/bake-progress").get_json()
        if p["done"] and not p["running"]:
            return p
        time.sleep(0.1)
    raise AssertionError("bake-all не завершився за timeout")


def test_dirty_set_on_pick_with_existing_polygons(client_cfg):
    """Pick на фото, де вже є polygons.json → stem стає dirty (selected/ застарів)."""
    client, cfg, store = client_cfg
    # polygons.json створюється ДО Pick (stem ще без entry — mark_dirty no-op).
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [_FREE_ZONE_POLYGON], "imageHeight": 64, "imageWidth": 64,
    })
    resp = client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["dirty"] is True
    assert store.is_dirty(STEM) is True


def test_pick_clean_photo_not_dirty(client_cfg):
    """Перший Pick чистого фото (без polygons/rejected) → НЕ dirty: raw == baked."""
    client, cfg, store = client_cfg
    resp = client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    assert resp.status_code == 200
    assert resp.get_json()["dirty"] is False
    assert store.is_dirty(STEM) is False


def test_dirty_set_on_polygon_save(client_cfg):
    """POST /api/polygons (чисте збереження JSON) → stem dirty, БЕЗ bake."""
    client, cfg, store = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    assert store.is_dirty(STEM) is False
    resp = client.post(f"/api/polygons/{STEM}", json={
        "shapes": [_FREE_ZONE_POLYGON], "imageHeight": 64, "imageWidth": 64,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["dirty"] is True
    assert store.is_dirty(STEM) is True


def test_dirty_set_on_cleanup_reject(client_cfg):
    """POST /api/cleanup (зміна rejected) → stem dirty, БЕЗ перепікання selected/."""
    client, cfg, store = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    resp = client.post(f"/api/cleanup/{STEM}",
                       json={"model": MODEL, "rejected_instances": [2]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["dirty"] is True
    assert store.is_dirty(STEM) is True


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_dirty_cleared_after_rebake(client_cfg):
    """Після успішного bake (/api/rebake) dirty-прапор знімається."""
    client, cfg, store = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [_FREE_ZONE_POLYGON], "imageHeight": 64, "imageWidth": 64,
    })
    assert store.is_dirty(STEM) is True
    resp = client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["baked"] is True
    assert store.is_dirty(STEM) is False


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_all_only_dirty_skips_when_clean(client_cfg):
    """bake-all у dirty-режимі: якщо незапечених фото немає → started=False."""
    client, cfg, store = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    assert store.is_dirty(STEM) is False  # чистий Pick
    resp = client.post("/api/workspace/bake-all", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["started"] is False
    assert body["photo_total"] == 0


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_all_bakes_and_clears_dirty(client_cfg):
    """bake-all запікає dirty stem у фоні, знімає dirty, оновлює selected/.npy."""
    client, cfg, store = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [_FREE_ZONE_POLYGON], "imageHeight": 64, "imageWidth": 64,
    })
    assert store.is_dirty(STEM) is True

    resp = client.post("/api/workspace/bake-all", json={})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["started"] is True
    assert body["photo_total"] == 1
    assert body["stems"] == [STEM]

    progress = _wait_bake_done(client)
    assert progress["ok_count"] == 1
    assert progress["errors"] == []
    assert progress["skipped"] == []

    assert store.is_dirty(STEM) is False
    saved = np.load(str(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"))
    assert saved[58, 55] > 3, "polygon pixel мусить мати новий ID після bake-all"


# ---------------------------------------------------------------------------
# Day 8 — workspace flow: stats per-user / export-subset / per-label overlays /
#         merge import з range-picker
# ---------------------------------------------------------------------------

def test_stats_includes_by_user(client_cfg):
    """GET /api/stats повертає per-user розбивку (хто скільки розмітив)."""
    client, cfg, _ = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "annotator1"})
    body = client.get("/api/stats").get_json()
    assert "by_user" in body
    assert body["by_user"].get("annotator1", {}).get("selected") == 1


def test_export_subset_filters_by_stems(client_cfg):
    """GET /api/workspace/export?stems=... обмежує ZIP лише вказаними фото."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [], "imageHeight": 64, "imageWidth": 64,
    })
    # Повний експорт містить polygons поточного stem.
    full = client.get("/api/workspace/export")
    with zipfile.ZipFile(io.BytesIO(full.data)) as zf:
        assert f"polygons/{STEM}.json" in zf.namelist()
    # Subset на інший stem — наш stem не потрапляє.
    sub = client.get("/api/workspace/export?stems=db_img_OTHER")
    with zipfile.ZipFile(io.BytesIO(sub.data)) as zf:
        assert f"polygons/{STEM}.json" not in zf.namelist()


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_export_includes_per_label_overlays(client_cfg):
    """Day 8: export генерує overlays/<stem>__<label>.png для кожного класу."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [_FREE_ZONE_POLYGON], "imageHeight": 64, "imageWidth": 64,
    })
    # rebake → selected/<model>/{npy,yolo} наповнюються (потрібно для overlay).
    client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    resp = client.get("/api/workspace/export")
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
    assert any(n.startswith(f"overlays/{STEM}__") for n in names), names


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_export_includes_derived_masks(client_cfg):
    """2026-05-22: export?masks=1 додає semantic + group-instance маски
    у selected/<model>/; без параметра — не додає."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    cfg.groups_dir = cfg.selected_dir.parent / "groups"
    cfg.groups_dir.mkdir(exist_ok=True)
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [_FREE_ZONE_POLYGON], "imageHeight": 64, "imageWidth": 64,
    })
    # rebake → selected/<model>/{npy,yolo} наповнюються (потрібно для масок).
    client.post(f"/api/rebake/{STEM}", json={"model": MODEL})
    (cfg.groups_dir / f"{STEM}.json").write_text(
        json.dumps({"version": "1.1", "stem": STEM, "model": MODEL, "groups": []}),
        encoding="utf-8",
    )

    # Без ?masks=1 — похідних масок у ZIP нема.
    plain = client.get("/api/workspace/export")
    with zipfile.ZipFile(io.BytesIO(plain.data)) as zf:
        assert not any("/semantic/" in n for n in zf.namelist())

    # З ?masks=1 — semantic + mask_groups присутні у selected/<model>/.
    resp = client.get("/api/workspace/export?masks=1")
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
    assert f"selected/{MODEL}/semantic/{STEM}.png" in names, names
    assert f"selected/{MODEL}/mask_groups/{STEM}.png" in names, names


def test_import_scan_lists_stems(client_cfg):
    """Day 8: /api/workspace/import-scan повертає список фото у ZIP."""
    client, cfg, _ = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("images/db_img_0101.jpg", b"img")
        zf.writestr("images/db_img_0102.jpg", b"img")
        zf.writestr("polygons/db_img_0101.json", '{"shapes":[]}')
    buf.seek(0)
    resp = client.post(
        "/api/workspace/import-scan",
        data={"files": (buf, "task.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] and len(body["sources"]) == 1
    assert set(body["sources"][0]["stems"]) == {"db_img_0101", "db_img_0102"}


def test_import_apply_merges_selected_stems(client_cfg):
    """Day 8: import-apply копіює лише вибрані stem-и + merge selections."""
    client, cfg, store = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("images/db_img_0201.jpg", b"img")
        zf.writestr("images/db_img_0202.jpg", b"img")
        zf.writestr("selections.json", json.dumps({
            "db_img_0201": {"status": "selected", "model": MODEL, "user": "annotator2"},
            "db_img_0202": {"status": "selected", "model": MODEL, "user": "annotator2"},
        }))
    buf.seek(0)
    scan = client.post(
        "/api/workspace/import-scan",
        data={"files": (buf, "artem.zip")},
        content_type="multipart/form-data",
    ).get_json()
    src_idx = scan["sources"][0]["idx"]

    # Застосовуємо лише одне з двох фото (range-pick).
    resp = client.post("/api/workspace/import-apply", json={
        "scan_id": scan["scan_id"],
        "picks": [{"idx": src_idx, "stems": ["db_img_0201"]}],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"]
    assert body["merged_stems"] == ["db_img_0201"]
    # Скопійовано лише db_img_0201; db_img_0202 — пропущено.
    assert (cfg.images_dir / "db_img_0201.jpg").exists()
    assert not (cfg.images_dir / "db_img_0202.jpg").exists()
    assert store.get("db_img_0201") is not None
    assert store.get("db_img_0202") is None


@pytest.mark.skipif(not CV2_AVAILABLE, reason="opencv-python не встановлено")
def test_bake_all_explicit_stems(client_cfg):
    """Day 8.5: bake-all з {stems:[...]} запікає саме вказані фото (Export+Finalize)."""
    client, cfg, store = client_cfg
    _setup_labels_file(cfg)
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    client.post(f"/api/polygons/{STEM}", json={
        "shapes": [_FREE_ZONE_POLYGON], "imageHeight": 64, "imageWidth": 64,
    })
    assert store.is_dirty(STEM) is True

    resp = client.post("/api/workspace/bake-all", json={"stems": [STEM]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["started"] is True
    assert body["stems"] == [STEM]

    progress = _wait_bake_done(client)
    assert progress["ok_count"] == 1
    assert store.is_dirty(STEM) is False


def test_import_apply_newest_wins_on_conflict(client_cfg):
    """Day 8.5: при конфлікті merge-import бере новіший за ts (newest wins)."""
    client, cfg, store = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "local"})
    e = store.get(STEM)
    e["ts"] = "2020-01-01T00:00:00Z"  # локальна розмітка — стара
    store.set(STEM, e)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"images/{STEM}.jpg", b"img")
        zf.writestr("selections.json", json.dumps({
            STEM: {"status": "selected", "model": MODEL,
                   "user": "imported", "ts": "2099-01-01T00:00:00Z"},
        }))
    buf.seek(0)
    scan = client.post(
        "/api/workspace/import-scan",
        data={"files": (buf, "newer.zip")},
        content_type="multipart/form-data",
    ).get_json()
    resp = client.post("/api/workspace/import-apply", json={
        "scan_id": scan["scan_id"],
        "picks": [{"idx": scan["sources"][0]["idx"], "stems": [STEM]}],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["merged_stems"] == [STEM]  # imported новіший → переміг
    assert store.get(STEM)["user"] == "imported"


def test_import_apply_keeps_local_when_newer(client_cfg):
    """Day 8.5: якщо локальна розмітка новіша — imported пропускається."""
    client, cfg, store = client_cfg
    cfg.workspace_dir = cfg.selected_dir.parent
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "local"})
    e = store.get(STEM)
    e["ts"] = "2099-01-01T00:00:00Z"  # локальна розмітка — свіжа
    store.set(STEM, e)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("selections.json", json.dumps({
            STEM: {"status": "selected", "model": MODEL,
                   "user": "imported", "ts": "2020-01-01T00:00:00Z"},
        }))
    buf.seek(0)
    scan = client.post(
        "/api/workspace/import-scan",
        data={"files": (buf, "older.zip")},
        content_type="multipart/form-data",
    ).get_json()
    resp = client.post("/api/workspace/import-apply", json={
        "scan_id": scan["scan_id"],
        "picks": [{"idx": scan["sources"][0]["idx"], "stems": [STEM]}],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["kept_local"] == [STEM]  # local новіший → залишено
    assert store.get(STEM)["user"] == "local"


def test_restore_brings_back_excluded_photo(client_cfg):
    """Day 8.5: /api/restore повертає виключене фото з _excluded/ у images/."""
    client, cfg, store = client_cfg
    img = cfg.images_dir / f"{STEM}.jpg"
    assert img.exists()

    resp = client.post(f"/api/exclude/{STEM}", json={"user": "t"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert not img.exists()
    assert (store.get(STEM) or {}).get("status") == "excluded"

    resp = client.post(f"/api/restore/{STEM}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["ok"]
    assert img.exists(), "фото має повернутися у images/"
    assert store.get(STEM) is None, "excluded-стан має зніматися"


def test_bulk_user_assigns_annotator(client_cfg):
    """Day 8.5: /api/bulk-user записує user у entries; фото без рішення пропускає."""
    client, cfg, store = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "old"})
    resp = client.post("/api/bulk-user", json={
        "stems": [STEM, "db_img_NOSTATE"], "user": "annotator1",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"]
    assert body["updated"] == [STEM]
    assert body["skipped"] == ["db_img_NOSTATE"]
    assert store.get(STEM)["user"] == "annotator1"


def test_hard_reset_clears_state_polygons_groups_selected(client_cfg):
    """v2.0.0 Day 3c′ CP6: /api/hard-reset/<stem> видаляє state + polygons.json
    + groups.json + selected/<model>/<stem>.*. Оригінали images/ та
    output/<model>/npy/ — read-only, не торкаються. Backup у _backups/."""
    client, cfg, store = client_cfg
    cfg.groups_dir = cfg.selected_dir.parent / "groups"
    cfg.groups_dir.mkdir(exist_ok=True)

    resp = client.post("/api/select", json={"stem": STEM, "model": MODEL, "user": "t"})
    assert resp.status_code == 200

    polygons_payload = {
        "version": "5.5.0",
        "imageHeight": 64, "imageWidth": 64,
        "imagePath": f"{STEM}.jpg",
        "shapes": [{"label": "nucleus", "points": [[5, 5], [15, 5], [10, 15]],
                    "shape_type": "polygon", "group_id": None, "flags": {}}],
        "flags": {}, "imageData": None,
    }
    resp = client.post(f"/api/polygons/{STEM}", json=polygons_payload)
    assert resp.status_code == 200

    # Manual group — hard-reset must clear it too, else it orphans onto the
    # polygons we just deleted.
    resp = client.post(f"/api/groups/{STEM}", json={
        "model": MODEL,
        "groups": [{"id": "g_001", "type": "cell", "instance_ids": [1, 2],
                    "polygon_indices": [], "color_hue": 0}],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)

    polygons_file = cfg.polygons_dir / f"{STEM}.json"
    groups_file = cfg.groups_dir / f"{STEM}.json"
    raw_npy = cfg.output_root / MODEL / "npy" / f"{STEM}.npy"
    raw_image = cfg.images_dir / f"{STEM}.jpg"
    selected_npy = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    assert polygons_file.exists()
    assert groups_file.exists()
    assert raw_npy.exists() and raw_image.exists()
    assert selected_npy.exists(), "Pick має скопіювати у selected/"
    assert store.get(STEM) is not None

    resp = client.post(f"/api/hard-reset/{STEM}")
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["ok"]
    assert data["removed"]["state"]
    assert data["removed"]["polygons"]
    assert data["removed"]["groups"]
    assert MODEL in data["removed"]["selected_models"]

    assert store.get(STEM) is None
    assert not polygons_file.exists()
    backup_dir = cfg.polygons_dir / "_backups" / STEM
    assert backup_dir.exists() and any(backup_dir.iterdir()), \
        "polygon backup must be created before deletion"
    assert not groups_file.exists()
    groups_backup_dir = cfg.groups_dir / "_backups" / STEM
    assert groups_backup_dir.exists() and any(groups_backup_dir.iterdir()), \
        "groups backup must be created before deletion"
    assert not selected_npy.exists()
    assert raw_npy.exists() and raw_image.exists(), \
        "originals must NEVER be touched by hard-reset"

