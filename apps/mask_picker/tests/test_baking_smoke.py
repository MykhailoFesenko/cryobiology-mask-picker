"""
Smoke тести для baking.export_derived_masks — derived masks export
(semantic + mask_groups.png).

Bug 1 (2026-05-26): mask_groups.png має враховувати polygon_indices
у групах — раніше polygon-shapes тихо випадали з експорту.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from baking import (  # noqa: E402
    POLYGON_ID_BASE,
    export_derived_masks,
    _bake_polygons_to_selected,
)
from state import Config  # noqa: E402


STEM = "db_img_test"
MODEL = "test_model"


def _make_workspace(tmp_path: Path) -> Config:
    """Створює мінімальний workspace для baking tests."""
    images_dir = tmp_path / "images"
    selected_dir = tmp_path / "selected"
    skipped_dir = tmp_path / "skipped"
    polygons_dir = tmp_path / "polygons"
    groups_dir = tmp_path / "groups"
    npy_dir = selected_dir / MODEL / "npy"
    yolo_dir = selected_dir / MODEL / "yolo"
    for d in (images_dir, selected_dir, skipped_dir, polygons_dir,
              groups_dir, npy_dir, yolo_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 64x64 image з 2 квадратами instance — top-left (iid=1) і top-right (iid=2)
    labels = np.zeros((64, 64), dtype=np.int32)
    labels[5:25, 5:25] = 1
    labels[5:25, 40:60] = 2
    np.save(str(npy_dir / f"{STEM}.npy"), labels)

    # YOLO multiclass: обидва instance клас 0 (nucleus)
    (yolo_dir / f"{STEM}.txt").write_text(
        "0 0.234 0.234 0.32 0.32\n"
        "0 0.781 0.234 0.32 0.32\n",
        encoding="utf-8",
    )

    return Config(
        images_dir=images_dir,
        output_root=tmp_path / "_out",
        selected_dir=selected_dir,
        skipped_dir=skipped_dir,
        polygons_dir=polygons_dir,
        groups_dir=groups_dir,
    )


def _write_groups(cfg: Config, groups: list) -> None:
    payload = {"version": "1.1", "stem": STEM, "model": MODEL, "groups": groups}
    (cfg.groups_dir / f"{STEM}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _write_polygons(cfg: Config, shapes: list) -> None:
    payload = {
        "version": "5.0.1", "flags": {}, "shapes": shapes,
        "imagePath": f"{STEM}.jpg", "imageData": None,
        "imageHeight": 64, "imageWidth": 64,
    }
    (cfg.polygons_dir / f"{STEM}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _read_mask_groups(out_dir: Path) -> np.ndarray:
    p = out_dir / "mask_groups" / f"{STEM}.png"
    assert p.exists(), f"mask_groups.png not written at {p}"
    with Image.open(str(p)) as img:
        return np.array(img)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_mask_groups_includes_polygon_indices(tmp_path):
    """Bug 1: група з polygon_index → polygon-пікселі ляжуть у mask_groups.png
    з номером цієї групи."""
    cfg = _make_workspace(tmp_path)
    # Група g_001 = instance 1 + polygon shape #0 (нижня область, бекграунд)
    _write_groups(cfg, [{
        "id": "g_001",
        "class_id": "cls_001",
        "instance_ids": [1],
        "polygon_indices": [0],
    }])
    # Polygon (нижня частина) — поза існуючими instance, у bg
    _write_polygons(cfg, [{
        "label": "nucleus",
        "points": [[10, 40], [30, 40], [30, 60], [10, 60]],
        "shape_type": "polygon",
    }])

    out_dir = tmp_path / "_derived"
    written = export_derived_masks(cfg, MODEL, STEM, out_dir)
    assert any(p.name == f"{STEM}.png" and p.parent.name == "mask_groups"
               for p in written)

    gmask = _read_mask_groups(out_dir)
    # Instance 1 пікселі (top-left square) → group 1
    assert (gmask[5:25, 5:25] == 1).all()
    # Polygon пікселі (bottom-left rect) → теж group 1
    assert (gmask[40:60, 10:30] == 1).all()
    # Instance 2 поза групою → 0
    assert (gmask[5:25, 40:60] == 0).all()


def test_mask_groups_polygon_does_not_overwrite_other_group(tmp_path):
    """Polygon однієї групи не повинен перекривати instance іншої групи
    (fill тільки на gmask==0)."""
    cfg = _make_workspace(tmp_path)
    # g_001 = instance 2 (top-right). g_002 = polygon перекриває instance 2.
    _write_groups(cfg, [
        {"id": "g_001", "class_id": "cls_001", "instance_ids": [2],
         "polygon_indices": []},
        {"id": "g_002", "class_id": "cls_002", "instance_ids": [],
         "polygon_indices": [0]},
    ])
    # Polygon охоплює всю верхню половину — включаючи instance 2 area
    _write_polygons(cfg, [{
        "label": "vesicle",
        "points": [[0, 0], [63, 0], [63, 30], [0, 30]],
        "shape_type": "polygon",
    }])

    out_dir = tmp_path / "_derived"
    export_derived_masks(cfg, MODEL, STEM, out_dir)
    gmask = _read_mask_groups(out_dir)
    # Instance 2 area повинна лишатись group 1 (НЕ перезаписана group 2)
    assert (gmask[5:25, 40:60] == 1).all(), \
        "polygon group 2 overwrote instance 2 of group 1"
    # Поза instance 2, у верхньому bg, polygon group 2 повинен залити bg
    bg_in_poly = (gmask[0:30, 0:5] == 2)
    assert bg_in_poly.all(), \
        "polygon group 2 не залив bg під polygon area"


def test_mask_groups_no_polygons_dir(tmp_path):
    """Якщо polygons/ немає або файлу немає — поведінка як раніше
    (тільки instance_ids)."""
    cfg = _make_workspace(tmp_path)
    # Видаляємо polygons файл (його ще немає; директорія існує — ок)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1, 2], "polygon_indices": [],
    }])

    out_dir = tmp_path / "_derived"
    export_derived_masks(cfg, MODEL, STEM, out_dir)
    gmask = _read_mask_groups(out_dir)
    assert (gmask[5:25, 5:25] == 1).all()
    assert (gmask[5:25, 40:60] == 1).all()
    # Bg лишається 0
    assert (gmask[30:60, :] == 0).all()


def test_mask_groups_polygon_only_group(tmp_path):
    """Група без жодного instance_id, лише polygon_indices — pix-ли polygon
    мають лягти у mask_groups.png як ця група."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_003",
        "instance_ids": [], "polygon_indices": [0],
    }])
    _write_polygons(cfg, [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "shape_type": "polygon",
    }])

    out_dir = tmp_path / "_derived"
    export_derived_masks(cfg, MODEL, STEM, out_dir)
    gmask = _read_mask_groups(out_dir)
    assert (gmask[35:55, 35:55] == 1).all(), \
        "polygon-only group has not been written into mask_groups.png"
    # Instance 1, 2 поза групами → 0
    assert (gmask[5:25, 5:25] == 0).all()
    assert (gmask[5:25, 40:60] == 0).all()


def test_mask_groups_invalid_polygon_index_safe(tmp_path):
    """Невалідний polygon_index (out of range, не int) — не падає, ігнорується."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1],
        "polygon_indices": [999, "garbage", -1, None, 0],
    }])
    _write_polygons(cfg, [{
        "label": "nucleus",
        "points": [[35, 40], [50, 40], [50, 55]],
        "shape_type": "polygon",
    }])

    out_dir = tmp_path / "_derived"
    export_derived_masks(cfg, MODEL, STEM, out_dir)  # не має кидати
    gmask = _read_mask_groups(out_dir)
    assert (gmask[5:25, 5:25] == 1).all()
    # Valid polygon (#0) теж залив group 1
    assert gmask[45, 40] == 1


# ---------------------------------------------------------------------------
# Bug 3 (v1.13.0) — sync resolved instance_id у group.instance_ids після bake
#
# Контекст: ядро/везикулу можна задати polygon-shape з `label="nucleus"`. Bake
# запікає її у `npy/` (отримує свіжий instance_id), АЛЕ цей iid не дописується
# у `group.instance_ids`. Downstream-код (наприклад замовницький
# clusterization.py), що шукає ядро лише серед instance_ids, втрачає
# 12.4% cells (94 з 758 на vesicles_good 2026-05).
#
# Fix: `_sync_groups_instance_ids_after_bake` — set union, idempotent, atomic.
# ---------------------------------------------------------------------------

def _read_groups_json(cfg: Config) -> dict:
    p = cfg.groups_dir / f"{STEM}.json"
    import json as _json
    with open(p, "r", encoding="utf-8-sig") as f:
        return _json.load(f)


def _bake_via_orchestrator(cfg: Config, shapes: list) -> dict:
    """Викликає `_bake_polygons_to_selected` як його викликають routes."""
    src_npy = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    return _bake_polygons_to_selected(
        cfg, STEM, MODEL, src_npy, shapes,
        rejected=[], base_label=None, do_backup=False,
    )


def test_bake_syncs_polygon_only_nucleus_into_group_instance_ids(tmp_path):
    """Phase 1 / Bug 3: група, де ядро задане ЛИШЕ через polygon_indices,
    після bake має у `instance_ids` resolved iid того polygon-shape."""
    cfg = _make_workspace(tmp_path)
    # Група без жодного instance_id, лише polygon_indices=[0]
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [],
        "polygon_indices": [0],
    }])
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None,
        "shape_type": "polygon",
        "flags": {},
    }]
    _write_polygons(cfg, shapes)

    result = _bake_via_orchestrator(cfg, shapes)
    assert result["ok"], result["errors"]
    assert result["baked_count"] == 1
    assert result["groups_sync_added"] == 1

    groups = _read_groups_json(cfg)["groups"]
    assert len(groups[0]["instance_ids"]) == 1
    iid = groups[0]["instance_ids"][0]
    # polygon_indices лишається — це окремий контракт (lasso двійник = Bug 4)
    assert groups[0]["polygon_indices"] == [0]
    # Resolved iid > max instance що був до bake (1, 2) — це новий instance
    assert iid > 2


def test_bake_idempotent_no_duplicate_iids(tmp_path):
    """Повторний bake не повинен дублювати iid у `instance_ids`."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [],
        "polygon_indices": [0],
    }])
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None,
        "shape_type": "polygon",
        "flags": {},
    }]
    _write_polygons(cfg, shapes)

    r1 = _bake_via_orchestrator(cfg, shapes)
    iids_after_first = _read_groups_json(cfg)["groups"][0]["instance_ids"]
    assert len(iids_after_first) == 1

    # Друга прогонка → той же polygon → новий instance_id у npy, але
    # idempotence політика: сумарна множина без дублів. Bake завжди генерує
    # новий next_id з npy.max()+1, тож після другого bake instance_ids
    # розширюється на новий iid (це очікувано — `instance_ids` тримає
    # союз; ручні правки не загублені).
    r2 = _bake_via_orchestrator(cfg, shapes)
    iids_after_second = _read_groups_json(cfg)["groups"][0]["instance_ids"]
    # set union → без дублікатів (sorted unique)
    assert len(set(iids_after_second)) == len(iids_after_second), \
        f"duplicates у instance_ids: {iids_after_second}"
    # Перша баковка повинна бути присутня (manual збережено)
    for iid in iids_after_first:
        assert iid in iids_after_second


def test_bake_preserves_manual_instance_ids(tmp_path):
    """Manual instance_id (доданий поза bake) не повинен бути втрачений після
    bake polygon-shape — set union, не replace."""
    cfg = _make_workspace(tmp_path)
    # Manual: юзер додав instance #1 (запечений раніше) і polygon-shape #0
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1],
        "polygon_indices": [0],
    }])
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None,
        "shape_type": "polygon",
        "flags": {},
    }]
    _write_polygons(cfg, shapes)

    result = _bake_via_orchestrator(cfg, shapes)
    assert result["ok"]
    groups = _read_groups_json(cfg)["groups"]
    iids = groups[0]["instance_ids"]
    # Manual #1 збережено
    assert 1 in iids
    # Новий resolved iid доданий
    assert any(i > 2 for i in iids)


def test_bake_removes_stale_reserved_iid_when_polygon_left_group(tmp_path):
    """Регресія db_img_0171 g_008 (раунд 2): полігон ПРИБРАНО з групи (немає у
    polygon_indices), але його baked reserved iid лишився stale у instance_ids.
    Bake-sync має зняти його — інакше пікселі полігона дістануть group_id цієї
    групи у mask_groups, а класифікація рахує «привид» прибраного ядра.
    NB: знімаємо лише polygon-resolved iid ЦЬОГО bake без власника, не «iid>=BASE»
    (інакше зламали б fallback-union — test_bake_idempotent_no_duplicate_iids)."""
    cfg = _make_workspace(tmp_path)
    shapes = [
        {"label": "nucleus", "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
         "group_id": None, "shape_type": "polygon", "flags": {}},
        {"label": "nucleus", "points": [[60, 60], [80, 60], [80, 80], [60, 80]],
         "group_id": None, "shape_type": "polygon", "flags": {}},
    ]
    _write_polygons(cfg, shapes)
    # g_001 тримає полігон #0. Reserved iid полігона #1 (BASE+1) лишився STALE
    # (юзер прибрав полігон #1 з групи). Полігон #1 НЕ в жодній групі.
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [POLYGON_ID_BASE + 1],
        "polygon_indices": [0],
    }])
    result = _bake_via_orchestrator(cfg, shapes)
    assert result["ok"]
    iids = _read_groups_json(cfg)["groups"][0]["instance_ids"]
    assert (POLYGON_ID_BASE + 1) not in iids, f"stale reserved iid лишився: {iids}"
    assert (POLYGON_ID_BASE + 0) in iids, f"власний polygon iid відсутній: {iids}"


def test_bake_no_groups_dir_safe(tmp_path):
    """Якщо `cfg.groups_dir` не встановлено — bake не повинен падати, просто
    повертає `groups_sync_added=0`."""
    cfg = _make_workspace(tmp_path)
    cfg.groups_dir = None  # імітуємо workspace без груп
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None,
        "shape_type": "polygon",
        "flags": {},
    }]
    _write_polygons(cfg, shapes)

    result = _bake_via_orchestrator(cfg, shapes)
    assert result["ok"]
    assert result["groups_sync_added"] == 0


def test_bake_groups_json_missing_safe(tmp_path):
    """`cfg.groups_dir` є, але `groups/<stem>.json` не існує — sync no-op."""
    cfg = _make_workspace(tmp_path)
    # Не пишемо groups файл взагалі
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None,
        "shape_type": "polygon",
        "flags": {},
    }]
    _write_polygons(cfg, shapes)

    result = _bake_via_orchestrator(cfg, shapes)
    assert result["ok"]
    assert result["groups_sync_added"] == 0


def test_bake_skipped_polygon_not_synced(tmp_path):
    """Polygon, що skipped під час bake (overlap > 60%), не повинен попадати
    у `shape_idx_to_iid` → `instance_ids` не отримає iid за skipped pi."""
    cfg = _make_workspace(tmp_path)
    # Group має 2 polygon_indices: один валідний, другий ідентичний (overlap 100%).
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [],
        "polygon_indices": [0, 1],
    }])
    pts = [[35, 35], [55, 35], [55, 55], [35, 55]]
    shapes = [
        {"label": "nucleus", "points": pts, "shape_type": "polygon",
         "group_id": None, "flags": {}},
        # Дублікат → IoU=100% → skipped
        {"label": "nucleus", "points": pts, "shape_type": "polygon",
         "group_id": None, "flags": {}},
    ]
    _write_polygons(cfg, shapes)

    result = _bake_via_orchestrator(cfg, shapes)
    assert result["baked_count"] == 1            # лише перший запечений
    assert result["groups_sync_added"] == 1      # лише resolved iid #0
    iids = _read_groups_json(cfg)["groups"][0]["instance_ids"]
    assert len(iids) == 1


def test_bake_authoritative_resolve_steals_legacy_iid(tmp_path):
    """v1.13.1 hotfix (оновлено v1.16.0 reserved-range): polygon-resolved
    iid авторитарно належить «своїй» групі. Legacy iid того самого номера
    у іншій групі видаляється.

    З reserved-range polygon shape #0 → POLYGON_ID_BASE (50000) детерміновано.
    Симулюємо legacy collision: g_002 має instance_ids=[50000] (наприклад
    залишок з попереднього bake/manual). Authoritative resolve має забрати
    50000 у g_001 (власник polygon-shape #0) і прибрати з g_002."""
    cfg = _make_workspace(tmp_path)
    legacy_id = POLYGON_ID_BASE  # 50000 — id який отримає polygon shape #0
    _write_groups(cfg, [
        {"id": "g_001", "class_id": "cls_001",
         "instance_ids": [], "polygon_indices": [0]},
        {"id": "g_002", "class_id": "cls_002",
         "instance_ids": [legacy_id], "polygon_indices": []},
    ])
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None, "shape_type": "polygon", "flags": {},
    }]
    _write_polygons(cfg, shapes)

    result = _bake_via_orchestrator(cfg, shapes)
    assert result["ok"]

    groups = _read_groups_json(cfg)["groups"]
    g1 = next(g for g in groups if g["id"] == "g_001")
    g2 = next(g for g in groups if g["id"] == "g_002")
    # polygon #0 → resolved iid = POLYGON_ID_BASE → owner=g_001 → лише там
    assert legacy_id in g1["instance_ids"]
    assert legacy_id not in g2["instance_ids"], (
        f"legacy iid {legacy_id} повинен бути видалений з g_002 (бо resolve"
        f" власник = g_001), але є: {g2['instance_ids']}"
    )


def test_bake_polygon_id_in_reserved_range(tmp_path):
    """v1.16.0: polygon-shape отримує iid у зарезервованому діапазоні
    (≥ POLYGON_ID_BASE), не next_id=max+1. Це усуває колізію з raw id."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [], "polygon_indices": [0],
    }])
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None, "shape_type": "polygon", "flags": {},
    }]
    _write_polygons(cfg, shapes)

    result = _bake_via_orchestrator(cfg, shapes)
    assert result["ok"]
    groups = _read_groups_json(cfg)["groups"]
    iids = groups[0]["instance_ids"]
    assert len(iids) == 1
    # Reserved-range: shape #0 → POLYGON_ID_BASE (не 3 = max(1,2)+1)
    assert iids[0] == POLYGON_ID_BASE, (
        f"polygon iid має бути {POLYGON_ID_BASE} (reserved), got {iids[0]}"
    )


def test_bake_polygon_ids_deterministic_by_index(tmp_path):
    """v1.16.0: shape index i → POLYGON_ID_BASE + i. Детерміновано і
    стабільно між bake (idempotence без накопичення нових id)."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [])
    shapes = [
        {"label": "nucleus", "points": [[5, 5], [12, 5], [12, 12], [5, 12]],
         "group_id": None, "shape_type": "polygon", "flags": {}},
        {"label": "vesicle", "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
         "group_id": None, "shape_type": "polygon", "flags": {}},
    ]
    _write_polygons(cfg, shapes)

    # Зберігаємо оригінальний RAW (у проді bake завжди читає стабільний
    # output/<model>/npy, не свій же результат — _find_npy_for → m.npy_dir).
    npy_path = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    raw_labels = np.load(str(npy_path)).copy()

    _bake_via_orchestrator(cfg, shapes)
    npy = np.load(str(npy_path))
    ids = set(int(i) for i in np.unique(npy) if i > 0)
    # Обидва polygon-shape отримали reserved ids BASE+0 і BASE+1
    assert POLYGON_ID_BASE in ids
    assert POLYGON_ID_BASE + 1 in ids

    # Відновлюємо стабільний raw (емулюємо production: bake з output/, не selected/)
    np.save(str(npy_path), raw_labels)

    # Повторний bake — ті самі reserved ids (без накопичення max+1 дрейфу)
    _bake_via_orchestrator(cfg, shapes)
    npy2 = np.load(str(npy_path))
    ids2 = set(int(i) for i in np.unique(npy2) if i > 0)
    assert POLYGON_ID_BASE in ids2
    assert POLYGON_ID_BASE + 1 in ids2
    # Жодних "блукаючих" високих id понад BASE+1
    poly_ids = {i for i in ids2 if i >= POLYGON_ID_BASE}
    assert poly_ids == {POLYGON_ID_BASE, POLYGON_ID_BASE + 1}


def test_bake_backstop_removes_phantom_fragment(tmp_path):
    """v1.16.0 backstop: raw instance перекритий полігоном >85% → залишковий
    фрагмент стирається (не лишається напів-стертий «фантом»)."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [])
    # instance 1 = rows 5..24, cols 5..24 (400px). Polygon покриває
    # y 5..23 (≈19 рядків) → лишається ~1 рядок (~5% < 50% поріг).
    shapes = [{
        "label": "nucleus",
        "points": [[5, 5], [24, 5], [24, 23], [5, 23]],
        "group_id": None, "shape_type": "polygon", "flags": {},
    }]
    _write_polygons(cfg, shapes)
    _bake_via_orchestrator(cfg, shapes)
    npy = np.load(str(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"))
    ids = set(int(i) for i in np.unique(npy) if i > 0)
    assert 1 not in ids, f"phantom instance 1 мав бути стертий, ids={ids}"
    assert 2 in ids, "instance 2 (не зачеплений) має лишитись"
    assert POLYGON_ID_BASE in ids, "polygon має бути у reserved range"


def test_bake_backstop_keeps_partially_overlapped_neighbor(tmp_path):
    """backstop (поріг 50% з audit-fix F-011): instance перекритий лише частково
    (≤50%) НЕ видаляється (великий полігон, що зачепив край сусіда)."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [])
    # instance 2 = rows 5..24, cols 40..59. Polygon покриває лише ~чверть
    # (cols 40..48, ~9 з 20 колонок ≈ 45% < 50% → instance 2 лишається).
    shapes = [{
        "label": "vesicle",
        "points": [[40, 5], [48, 5], [48, 24], [40, 24]],
        "group_id": None, "shape_type": "polygon", "flags": {},
    }]
    _write_polygons(cfg, shapes)
    _bake_via_orchestrator(cfg, shapes)
    npy = np.load(str(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"))
    ids = set(int(i) for i in np.unique(npy) if i > 0)
    assert 2 in ids, f"instance 2 частково перекритий — НЕ має видалятись, ids={ids}"
    assert 1 in ids, "instance 1 (не зачеплений) лишається"


def test_bake_backstop_erases_over_half_covered(tmp_path):
    """F-011 (audit-fix): поріг backstop вирівняно до 50% (= UI covered>0.5).
    Instance, перекритий полігоном >50% (тут ~70%), тепер стирається — раніше
    (поріг 15%) інстанси 50–85% перекриття виживали як «огризок», хоч редактор
    ховав їх як covered."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [])
    # instance 1 = rows 5..24, cols 5..24 (20 колонок). Polygon покриває
    # cols 5..18 (~14 з 20 ≈ 70% > 50% → instance 1 стирається).
    shapes = [{
        "label": "nucleus",
        "points": [[5, 5], [18, 5], [18, 24], [5, 24]],
        "group_id": None, "shape_type": "polygon", "flags": {},
    }]
    _write_polygons(cfg, shapes)
    _bake_via_orchestrator(cfg, shapes)
    npy = np.load(str(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"))
    ids = set(int(i) for i in np.unique(npy) if i > 0)
    assert 1 not in ids, f"instance 1 (~70% covered) мав стертись при порозі 50%, ids={ids}"
    assert 2 in ids, "instance 2 (не зачеплений) лишається"
