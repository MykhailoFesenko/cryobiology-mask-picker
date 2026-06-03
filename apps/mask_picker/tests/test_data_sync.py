"""
Smoke + unit тести для `data_sync.py` (Phase 4 — convergence point для
cross-cutting mutations).

Покриває:
- `reseat_rejected_after_bake` (pure function — повний матрикс overlap).
- `bake_with_resync` (drop-in заміна `_bake_polygons_to_selected` + strip
  orphan iid з groups.json).
- `_strip_orphans_in_groups_file` (private helper, але цінний для tested).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from data_sync import (  # noqa: E402
    _strip_orphans_in_groups_file,
    bake_with_resync,
    compact_instance_ids,
    reseat_rejected_after_bake,
)
from state import Config  # noqa: E402


STEM = "db_img_test"
MODEL = "test_model"


# ---------------------------------------------------------------------------
# Shared workspace fixtures (узгоджено зі стилем test_baking_smoke.py)
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path: Path) -> Config:
    """Створює мінімальний workspace для bake tests."""
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

    # 64x64 image, 2 instance: квадрат 5..25 = iid 1, квадрат 40..60 = iid 2
    labels = np.zeros((64, 64), dtype=np.int32)
    labels[5:25, 5:25] = 1
    labels[5:25, 40:60] = 2
    np.save(str(npy_dir / f"{STEM}.npy"), labels)

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


def _read_groups_json(cfg: Config) -> dict:
    with open(cfg.groups_dir / f"{STEM}.json", "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _write_polygons(cfg: Config, shapes: list) -> None:
    payload = {
        "version": "5.0.1", "flags": {}, "shapes": shapes,
        "imagePath": f"{STEM}.jpg", "imageData": None,
        "imageHeight": 64, "imageWidth": 64,
    }
    (cfg.polygons_dir / f"{STEM}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _bake_via_data_sync(cfg: Config, shapes: list,
                         rejected: list = None) -> dict:
    """Викликає `bake_with_resync` як його викликають routes."""
    src_npy = cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy"
    return bake_with_resync(
        cfg, STEM, MODEL, src_npy, shapes,
        rejected=(rejected or []), base_label=None, do_backup=False,
    )


# ===========================================================================
# reseat_rejected_after_bake — pure function (без I/O)
# ===========================================================================

def test_reseat_pixel_perfect_overlap():
    """100% overlap → dominant у NEW = mapping для перенесення."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    old[2:5, 2:5] = 42   # клітина 42 у OLD
    new[2:5, 2:5] = 99   # та сама геометрія, новий ID 99 у NEW

    kept, dropped = reseat_rejected_after_bake(old, new, [42])
    assert kept == [99]
    assert dropped == []


def test_reseat_60_percent_overlap_kept():
    """60% overlap (> 0.5 default) → перенесено."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    # OLD клітина 10 px², NEW клітина — 6 з тих самих pixel'ів = 60%
    old[0, 0:10] = 5
    new[0, 0:6] = 77
    new[0, 6:10] = 0    # решта стала фоном

    kept, dropped = reseat_rejected_after_bake(old, new, [5])
    assert kept == [77]
    assert dropped == []


def test_reseat_30_percent_overlap_dropped():
    """30% overlap (< 0.5 default) → dropped (не та сама клітина)."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    old[0, 0:10] = 5
    new[0, 0:3] = 88   # лише 30% перетин

    kept, dropped = reseat_rejected_after_bake(old, new, [5])
    assert kept == []
    assert dropped == [5]


def test_reseat_full_background_dropped():
    """NEW pixel'и стали фоном (clean cleaned[isin(rejected)]=0) → dropped."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    old[2:5, 2:5] = 7

    kept, dropped = reseat_rejected_after_bake(old, new, [7])
    assert kept == []
    assert dropped == [7]


def test_reseat_old_iid_not_present_dropped():
    """rejected iid якого не було у OLD → dropped (mask.sum()==0)."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    old[2:5, 2:5] = 42

    kept, dropped = reseat_rejected_after_bake(old, new, [999])
    assert kept == []
    assert dropped == [999]


def test_reseat_shape_mismatch_drops_all():
    """OLD і NEW різного розміру (resize) → drop усе."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((20, 20), dtype=np.int32)

    kept, dropped = reseat_rejected_after_bake(old, new, [1, 2, 3])
    assert kept == []
    assert dropped == [1, 2, 3]


def test_reseat_empty_rejected():
    """Порожній rejected list → порожні результати."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)

    kept, dropped = reseat_rejected_after_bake(old, new, [])
    assert kept == []
    assert dropped == []


def test_reseat_dedup_when_two_old_map_to_same_new():
    """Два OLD rejected → один dominant NEW → set semantics → 1 запис."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    # OLD: дві сусідні клітини 5 і 6
    old[0, 0:5] = 5
    old[0, 5:10] = 6
    # NEW: merge у одну клітину з ID 77
    new[0, 0:10] = 77

    kept, dropped = reseat_rejected_after_bake(old, new, [5, 6])
    assert kept == [77]      # set semantics — один dominant
    assert dropped == []


def test_reseat_custom_overlap_min():
    """`overlap_min=0.2` дозволяє перенести при низькому overlap."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    old[0, 0:10] = 5
    new[0, 0:3] = 88  # 30% overlap

    kept, dropped = reseat_rejected_after_bake(old, new, [5], overlap_min=0.2)
    assert kept == [88]
    assert dropped == []


def test_reseat_ignores_invalid_input():
    """non-int, ≤0, None у rejected — ігноруються без падіння."""
    old = np.zeros((10, 10), dtype=np.int32)
    new = np.zeros((10, 10), dtype=np.int32)
    old[2:5, 2:5] = 42
    new[2:5, 2:5] = 42

    kept, dropped = reseat_rejected_after_bake(old, new, [42, 0, -1, "abc", None])
    # 42 → перенесено, 0/негативні/невалідні — silently ignored
    assert kept == [42]
    assert 0 not in kept
    assert -1 not in kept


# ===========================================================================
# bake_with_resync — integration (drop-in замість _bake_polygons_to_selected)
# ===========================================================================

def test_bake_with_resync_happy_path_no_orphans(tmp_path):
    """Bake без сирітських iid у groups → orphan_iids_stripped == 0."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1, 2],  # реальні iid з npy
        "polygon_indices": [],
    }])
    _write_polygons(cfg, [])

    result = _bake_via_data_sync(cfg, [])
    assert result["ok"], result["errors"]
    assert result["orphan_iids_stripped"] == 0
    # Групи не змінились (обидва iid існують у baked npy)
    groups = _read_groups_json(cfg)["groups"]
    assert set(groups[0]["instance_ids"]) == {1, 2}


def test_bake_with_resync_strips_orphan_iids(tmp_path):
    """Manual iid що не існує у baked npy → strip-нуто з groups.json."""
    cfg = _make_workspace(tmp_path)
    # 9999 — не існує у npy (де є тільки 1, 2)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1, 9999, 8888],
        "polygon_indices": [],
    }])
    _write_polygons(cfg, [])

    result = _bake_via_data_sync(cfg, [])
    assert result["ok"]
    # Дві сирітки strip-нуто
    assert result["orphan_iids_stripped"] == 2
    groups = _read_groups_json(cfg)["groups"]
    assert set(groups[0]["instance_ids"]) == {1}
    assert 9999 not in groups[0]["instance_ids"]
    assert 8888 not in groups[0]["instance_ids"]


def test_bake_with_resync_no_groups_dir_safe(tmp_path):
    """Якщо `cfg.groups_dir` не встановлено — strip пропускається без падіння."""
    cfg = _make_workspace(tmp_path)
    cfg.groups_dir = None

    result = _bake_via_data_sync(cfg, [])
    assert result["ok"]
    assert result["orphan_iids_stripped"] == 0


def test_bake_with_resync_idempotent(tmp_path):
    """Повторний bake → strip уже clean → 0 strip."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1, 9999],
        "polygon_indices": [],
    }])
    _write_polygons(cfg, [])

    r1 = _bake_via_data_sync(cfg, [])
    assert r1["orphan_iids_stripped"] == 1
    r2 = _bake_via_data_sync(cfg, [])
    # Друга прогонка — orphan'ів уже немає
    assert r2["orphan_iids_stripped"] == 0


def test_bake_with_resync_preserves_polygon_resolved_iid(tmp_path):
    """Bug 3 sync + strip orphan — обидва кроки спрацьовують узгоджено."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [9999],  # сирітка
        "polygon_indices": [0],  # polygon-shape резолвиться у новий iid
    }])
    shapes = [{
        "label": "nucleus",
        "points": [[35, 35], [55, 35], [55, 55], [35, 55]],
        "group_id": None,
        "shape_type": "polygon",
        "flags": {},
    }]
    _write_polygons(cfg, shapes)

    result = _bake_via_data_sync(cfg, shapes)
    assert result["ok"]
    # Bug 3 sync додав polygon-resolved iid у group.instance_ids.
    # Потім strip викинув 9999 (orphan).
    assert result["groups_sync_added"] >= 1
    assert result["orphan_iids_stripped"] == 1
    groups = _read_groups_json(cfg)["groups"]
    assert 9999 not in groups[0]["instance_ids"]
    # Resolved polygon iid у наявності
    assert len(groups[0]["instance_ids"]) >= 1
    assert all(iid in (1, 2) or iid > 2 for iid in groups[0]["instance_ids"])


# ===========================================================================
# _strip_orphans_in_groups_file — private helper (smoke)
# ===========================================================================

def test_strip_orphans_in_file_clean(tmp_path):
    """Group iid усі реальні → strip 0."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1, 2], "polygon_indices": [],
    }])

    stripped = _strip_orphans_in_groups_file(cfg, STEM, MODEL)
    assert stripped == 0


def test_strip_orphans_in_file_groups_missing(tmp_path):
    """Groups файл не існує → return 0."""
    cfg = _make_workspace(tmp_path)
    # Не пишемо groups
    stripped = _strip_orphans_in_groups_file(cfg, STEM, MODEL)
    assert stripped == 0


def test_strip_orphans_in_file_npy_missing(tmp_path):
    """Filtered npy не існує → return 0 (защита від wipe)."""
    cfg = _make_workspace(tmp_path)
    # Видаляємо filtered npy
    (cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy").unlink()
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1, 9999], "polygon_indices": [],
    }])

    stripped = _strip_orphans_in_groups_file(cfg, STEM, MODEL)
    assert stripped == 0


def test_strip_orphans_in_file_actually_strips(tmp_path):
    """Має сирітку → strip + groups.json оновлений на диску."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [{
        "id": "g_001", "class_id": "cls_001",
        "instance_ids": [1, 9999, 8888], "polygon_indices": [],
    }])

    stripped = _strip_orphans_in_groups_file(cfg, STEM, MODEL)
    assert stripped == 2
    groups = _read_groups_json(cfg)["groups"]
    assert set(groups[0]["instance_ids"]) == {1}


def test_strip_orphans_in_file_empty_groups(tmp_path):
    """Якщо groups.json порожній — return 0 без падіння."""
    cfg = _make_workspace(tmp_path)
    _write_groups(cfg, [])

    stripped = _strip_orphans_in_groups_file(cfg, STEM, MODEL)
    assert stripped == 0


# ===========================================================================
# compact_instance_ids — dense 1..N для deliverable (v1.16.0)
# ===========================================================================

def test_compact_basic_dense_renumber():
    """Sparse id (1, 2, 50000, 50001) → dense 1,2,3,4 без пропусків."""
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[0:2, 0:2] = 1
    labels[0:2, 3:5] = 2
    labels[5:7, 0:2] = 50000   # polygon reserved id
    labels[5:7, 3:5] = 50001
    dense, remap = compact_instance_ids(labels)
    ids = sorted(int(i) for i in np.unique(dense) if i > 0)
    assert ids == [1, 2, 3, 4], ids
    assert remap == {0: 0, 1: 1, 2: 2, 50000: 3, 50001: 4}


def test_compact_preserves_pixels_geometry():
    """Ущільнення зберігає геометрію — кожен old регіон стає своїм new."""
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[0:3, 0:3] = 7
    labels[5:8, 5:8] = 50005
    dense, remap = compact_instance_ids(labels)
    # 7 → 1 (менший old → менший new), 50005 → 2
    assert remap[7] == 1
    assert remap[50005] == 2
    assert (dense[0:3, 0:3] == 1).all()
    assert (dense[5:8, 5:8] == 2).all()
    # фон лишився фоном
    assert dense[9, 9] == 0


def test_compact_remaps_groups_consistently():
    """groups.instance_ids переномеровуються тим самим remap що й npy."""
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[0:2, 0:2] = 3
    labels[5:7, 5:7] = 50000
    groups = [
        {"id": "g_001", "instance_ids": [3, 50000], "polygon_indices": [0]},
        {"id": "g_002", "instance_ids": [3], "polygon_indices": []},
    ]
    dense, remap = compact_instance_ids(labels, groups)
    # 3 → 1, 50000 → 2
    assert groups[0]["instance_ids"] == [1, 2]
    assert groups[1]["instance_ids"] == [1]
    # npy теж
    assert sorted(int(i) for i in np.unique(dense) if i > 0) == [1, 2]


def test_compact_groups_drop_orphan_refs():
    """instance_id у групі, якого немає у labels → випадає (не у remap)."""
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[0:2, 0:2] = 5
    groups = [{"id": "g_001", "instance_ids": [5, 9999], "polygon_indices": []}]
    dense, remap = compact_instance_ids(labels, groups)
    # 9999 немає у labels → не мапиться → випадає
    assert groups[0]["instance_ids"] == [1]


def test_compact_empty_labels():
    """Порожня маска (лише фон) → лишається фоном, без падіння."""
    labels = np.zeros((10, 10), dtype=np.int32)
    dense, remap = compact_instance_ids(labels)
    assert int(dense.max()) == 0
    assert remap == {0: 0}


def test_compact_already_dense_noop():
    """Вже dense 1..N → лишається 1..N (ідемпотентно)."""
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[0:2, 0:2] = 1
    labels[5:7, 5:7] = 2
    dense, remap = compact_instance_ids(labels)
    assert remap == {0: 0, 1: 1, 2: 2}
    assert sorted(int(i) for i in np.unique(dense) if i > 0) == [1, 2]


def test_compact_no_gaps_after_rejected_middle():
    """Модель id з дірками (1,2,5 — 3,4 reject) + polygon → щільні 1,2,3,4."""
    labels = np.zeros((12, 12), dtype=np.int32)
    labels[0:2, 0:2] = 1
    labels[0:2, 3:5] = 2
    labels[0:2, 6:8] = 5       # 3,4 були reject-нуті (відсутні)
    labels[8:10, 0:2] = 50000  # polygon
    dense, remap = compact_instance_ids(labels)
    ids = sorted(int(i) for i in np.unique(dense) if i > 0)
    assert ids == [1, 2, 3, 4], ids  # без пропусків
    assert max(ids) == len(ids)      # max == count (стандарт instance-маски)


# ===========================================================================
# Robustness / edge cases (Phase 9 — hardening перед GitHub)
# ===========================================================================

def test_compact_none_labels_safe():
    """compact з None → (None, {0:0}) без падіння."""
    dense, remap = compact_instance_ids(None)
    assert dense is None
    assert remap == {0: 0}


def test_compact_preserves_dtype():
    """compact зберігає dtype (uint16 deliverable не ламається)."""
    labels = np.zeros((8, 8), dtype=np.uint16)
    labels[0:2, 0:2] = 50000
    dense, _ = compact_instance_ids(labels)
    assert dense.dtype == np.uint16
    assert int(dense.max()) == 1


def test_strip_orphans_broken_groups_json_safe(tmp_path):
    """Биті groups.json → _strip_orphans повертає 0, не падає."""
    cfg = _make_workspace(tmp_path)
    (cfg.groups_dir / f"{STEM}.json").write_text("{ broken json ///", encoding="utf-8")
    stripped = _strip_orphans_in_groups_file(cfg, STEM, MODEL)
    assert stripped == 0


def test_reseat_all_zero_arrays():
    """reseat з порожніми (фон) масивами → нічого не переноситься."""
    old = np.zeros((8, 8), dtype=np.int32)
    new = np.zeros((8, 8), dtype=np.int32)
    kept, dropped = reseat_rejected_after_bake(old, new, [1, 2])
    assert kept == []
    assert sorted(dropped) == [1, 2]


def test_bake_with_resync_missing_src_npy_raises_clean(tmp_path):
    """Відсутній src_npy → bake_with_resync кидає чітку помилку (route ловить),
    не лишає половинчастого стану."""
    cfg = _make_workspace(tmp_path)
    missing = cfg.selected_dir / MODEL / "npy" / "does_not_exist.npy"
    with pytest.raises(Exception):
        bake_with_resync(
            cfg, "does_not_exist", MODEL, missing, [],
            rejected=[], base_label=None, do_backup=False,
        )
