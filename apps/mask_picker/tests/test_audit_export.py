"""
Smoke тести для tools/audit_export.py — кожен invariant отримує
broken workspace fixture і перевіряємо що CLI виявляє порушення.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE.parent))

import audit_export  # noqa: E402


STEM = "db_img_test"
MODEL = "mdl"


def _make_workspace(tmp_path: Path, *, with_semantic=False, with_mask_groups=False) -> Path:
    """Будує мінімальний workspace із 1 selected stem."""
    ws = tmp_path
    images = ws / "images"
    selected = ws / "selected" / MODEL
    polygons_dir = ws / "polygons"
    groups_dir = ws / "groups"
    for d in (images, selected / "npy", selected / "png", selected / "yolo",
              polygons_dir, groups_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 32x32 image з 2 instance
    labels = np.zeros((32, 32), dtype=np.int32)
    labels[2:10, 2:10] = 1
    labels[12:20, 12:20] = 2
    np.save(selected / "npy" / f"{STEM}.npy", labels)
    Image.fromarray(labels.astype(np.uint16), mode="I;16").save(
        selected / "png" / f"{STEM}.png"
    )
    # YOLO multiclass: instance 1 = class 0 (nucleus), instance 2 = class 1 (vesicle)
    (selected / "yolo" / f"{STEM}.txt").write_text(
        "0 0.187 0.187 0.25 0.25\n"
        "1 0.5 0.5 0.25 0.25\n",
        encoding="utf-8",
    )
    # selections.json
    (ws / "selections.json").write_text(json.dumps({
        STEM: {"status": "selected", "model": MODEL},
    }), encoding="utf-8")
    # Image placeholder
    Image.fromarray(np.full((32, 32), 40, dtype=np.uint8), mode="L").save(
        images / f"{STEM}.jpg"
    )
    # polygons + groups (empty defaults — invariants skip)
    (polygons_dir / f"{STEM}.json").write_text(json.dumps({
        "version": "5.0.1", "shapes": [],
        "imageHeight": 32, "imageWidth": 32, "imagePath": f"{STEM}.jpg",
    }), encoding="utf-8")
    (groups_dir / f"{STEM}.json").write_text(json.dumps({
        "version": "1.1", "stem": STEM, "model": MODEL, "groups": [],
    }), encoding="utf-8")

    if with_semantic:
        (selected / "semantic").mkdir(parents=True, exist_ok=True)
        sem = np.zeros((32, 32), dtype=np.uint8)
        sem[2:10, 2:10] = 1
        sem[12:20, 12:20] = 2
        Image.fromarray(sem, mode="L").save(
            selected / "semantic" / f"{STEM}.png"
        )
    if with_mask_groups:
        (selected / "mask_groups").mkdir(parents=True, exist_ok=True)
        gm = np.zeros((32, 32), dtype=np.uint16)
        gm[2:10, 2:10] = 1
        Image.fromarray(gm).save(
            selected / "mask_groups" / f"{STEM}.png"
        )
    return ws


def _run_audit(ws: Path) -> audit_export.Auditor:
    """Запускає audit і повертає Auditor instance для inspect."""
    sel = audit_export._load_json(ws / "selections.json", {}) or {}
    auditor = audit_export.Auditor()
    items = [(k, v.get("model")) for k, v in sel.items()
             if v.get("status") == "selected"]
    auditor.n_stems_checked = len(items)
    for stem, model in items:
        auditor.check_npy_png(ws, model, stem)
        auditor.check_semantic(ws, model, stem)
        auditor.check_yolo(ws, model, stem)
        auditor.check_mask_groups(ws, model, stem)
        auditor.check_polygons_group_id(ws, stem)
        auditor.check_groups_invariants(ws, model, stem)
        auditor.check_overlays_consistency(ws, model, stem)
    auditor.check_selections_orphans(ws, sel)
    return auditor


# ---------------------------------------------------------------------------
# Clean baseline
# ---------------------------------------------------------------------------

def test_clean_workspace_no_violations(tmp_path):
    """Чистий workspace без semantic/mask_groups — 0 violations."""
    ws = _make_workspace(tmp_path)
    auditor = _run_audit(ws)
    total = sum(len(v) for v in auditor.violations.values())
    assert total == 0, f"violations: {dict(auditor.violations)}"


def test_clean_workspace_with_derived_no_violations(tmp_path):
    """Workspace з semantic+mask_groups — 0 violations."""
    ws = _make_workspace(tmp_path, with_semantic=True, with_mask_groups=True)
    auditor = _run_audit(ws)
    total = sum(len(v) for v in auditor.violations.values())
    assert total == 0, f"violations: {dict(auditor.violations)}"


# ---------------------------------------------------------------------------
# Invariant violations
# ---------------------------------------------------------------------------

def test_i1_npy_png_id_mismatch(tmp_path):
    """Якщо png має інший набір id ніж npy → I1 violation."""
    ws = _make_workspace(tmp_path)
    # Перепишемо PNG з відсутніми instance id 2
    labels_png = np.zeros((32, 32), dtype=np.uint16)
    labels_png[2:10, 2:10] = 1
    # Без instance 2 у png
    Image.fromarray(labels_png, mode="I;16").save(
        ws / "selected" / MODEL / "png" / f"{STEM}.png"
    )
    auditor = _run_audit(ws)
    assert auditor.n_violations("I1_npy_png") == 1


def test_i2_semantic_unexpected_value(tmp_path):
    """semantic.png зі значенням 5 → I2 violation."""
    ws = _make_workspace(tmp_path, with_semantic=True)
    sem = np.zeros((32, 32), dtype=np.uint8)
    sem[0:5, 0:5] = 5  # неприпустиме
    Image.fromarray(sem, mode="L").save(
        ws / "selected" / MODEL / "semantic" / f"{STEM}.png"
    )
    auditor = _run_audit(ws)
    assert auditor.n_violations("I2_semantic") == 1


def test_i3_yolo_line_count_mismatch(tmp_path):
    """yolo з 3 рядками, а npy має 2 unique iid → I3 violation."""
    ws = _make_workspace(tmp_path)
    (ws / "selected" / MODEL / "yolo" / f"{STEM}.txt").write_text(
        "0 0.1 0.1 0.1 0.1\n"
        "1 0.5 0.5 0.1 0.1\n"
        "0 0.7 0.7 0.1 0.1\n",
        encoding="utf-8",
    )
    auditor = _run_audit(ws)
    assert auditor.n_violations("I3_yolo") == 1


def test_i3_yolo_class_id_out_of_range(tmp_path):
    """yolo з class_id=5 → I3 violation."""
    ws = _make_workspace(tmp_path)
    (ws / "selected" / MODEL / "yolo" / f"{STEM}.txt").write_text(
        "0 0.1 0.1 0.1 0.1\n"
        "5 0.5 0.5 0.1 0.1\n",
        encoding="utf-8",
    )
    auditor = _run_audit(ws)
    assert auditor.n_violations("I3_yolo") == 1


def test_i5_polygon_group_id_unknown(tmp_path):
    """polygons.shape.group_id="g_999" якого нема у groups → I5 violation."""
    ws = _make_workspace(tmp_path)
    (ws / "polygons" / f"{STEM}.json").write_text(json.dumps({
        "version": "5.0.1",
        "shapes": [{
            "label": "nucleus",
            "points": [[1, 1], [5, 1], [5, 5]],
            "shape_type": "polygon",
            "group_id": "g_999",
            "flags": {},
        }],
        "imageHeight": 32, "imageWidth": 32, "imagePath": f"{STEM}.jpg",
    }), encoding="utf-8")
    auditor = _run_audit(ws)
    assert auditor.n_violations("I5_polygon_group_id") == 1


def test_i6_group_iid_not_in_npy(tmp_path):
    """group.instance_ids=[999] якого нема у npy → I6 violation."""
    ws = _make_workspace(tmp_path)
    (ws / "groups" / f"{STEM}.json").write_text(json.dumps({
        "version": "1.1", "stem": STEM, "model": MODEL,
        "groups": [{
            "id": "g_001", "class_id": "cls_001",
            "instance_ids": [999], "polygon_indices": [],
        }],
    }), encoding="utf-8")
    auditor = _run_audit(ws)
    assert auditor.n_violations("I6_group_iid_in_npy") == 1


def test_i7_group_pi_out_of_range(tmp_path):
    """group.polygon_indices=[99] коли shapes=[] → I7 violation."""
    ws = _make_workspace(tmp_path)
    (ws / "groups" / f"{STEM}.json").write_text(json.dumps({
        "version": "1.1", "stem": STEM, "model": MODEL,
        "groups": [{
            "id": "g_001", "class_id": "cls_001",
            "instance_ids": [], "polygon_indices": [99],
        }],
    }), encoding="utf-8")
    auditor = _run_audit(ws)
    assert auditor.n_violations("I7_group_pi_in_shapes") == 1


def test_i9_selections_orphan_no_npy(tmp_path):
    """selections.json має stem='ghost' selected, але npy відсутній → I9."""
    ws = _make_workspace(tmp_path)
    sel = json.loads((ws / "selections.json").read_text(encoding="utf-8-sig"))
    sel["ghost"] = {"status": "selected", "model": MODEL}
    (ws / "selections.json").write_text(json.dumps(sel), encoding="utf-8")
    auditor = _run_audit(ws)
    assert auditor.n_violations("I9_selections_orphan") == 1


# ---------------------------------------------------------------------------
# Exit code
# ---------------------------------------------------------------------------

def test_main_returns_zero_on_clean(tmp_path):
    ws = _make_workspace(tmp_path)
    rc = audit_export.main(["--workspace", str(ws)])
    assert rc == 0


def test_main_returns_one_on_violation(tmp_path):
    ws = _make_workspace(tmp_path)
    # Створимо порушення I6
    (ws / "groups" / f"{STEM}.json").write_text(json.dumps({
        "version": "1.1", "stem": STEM, "model": MODEL,
        "groups": [{
            "id": "g_001", "class_id": "cls_001",
            "instance_ids": [999], "polygon_indices": [],
        }],
    }), encoding="utf-8")
    rc = audit_export.main(["--workspace", str(ws)])
    assert rc == 1
