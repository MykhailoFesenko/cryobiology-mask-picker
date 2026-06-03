"""
verify_bug3_clusterization.py — перевірка acceptance Phase 1 / Bug 3.

Імітує логіку `_inbox/clusterization.py::extract_ground_truth` (БЕЗ patch'у з
`_inbox/clusterization_patch.md`) на поточному workspace `data/vesicles_good/`
і друкує, скільки cell-груп (`class_id == "cls_001"`) втрачено через
відсутність nucleus у `group.instance_ids`. Очікувано після v1.13.0 bake
sync — TOTAL LOST = 0.

Запуск з кореня проекту:
    python _tmp/verify_bug3_clusterization.py
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
import sys as _sys
WORKSPACE = Path(_sys.argv[1]) if len(_sys.argv) > 1 else ROOT / "data" / "vesicles_good"
GROUPS_DIR = WORKSPACE / "groups"
SELECTED_DIR = WORKSPACE / "selected"


def _semantic_from_workspace(model: str, stem: str) -> np.ndarray | None:
    """Будує семантичну маску з YOLO multiclass + npy, як `export_derived_masks`."""
    npy_path = SELECTED_DIR / model / "npy" / f"{stem}.npy"
    yolo_path = SELECTED_DIR / model / "yolo" / f"{stem}.txt"
    if not npy_path.exists() or not yolo_path.exists():
        return None
    labels = np.load(str(npy_path))
    while labels.ndim > 2:
        labels = labels[0]
    inst_ids = [int(i) for i in np.unique(labels) if int(i) > 0]
    if not inst_ids:
        return None
    lines = [
        ln for ln in yolo_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if len(lines) != len(inst_ids):
        return None
    max_id = int(labels.max())
    lut = np.zeros(max_id + 1, dtype=np.uint8)
    for iid, line in zip(inst_ids, lines):
        try:
            cid = int(line.split()[0])
        except (ValueError, IndexError):
            continue
        if 0 <= cid <= 254:
            lut[iid] = cid + 1
    return lut[labels], labels


def _selections() -> dict:
    p = WORKSPACE / "selections.json"
    return json.loads(p.read_text(encoding="utf-8-sig"))


def check_stem(stem: str, model: str) -> tuple[int, int]:
    """Повертає (total_cells, lost_cells) для одного stem.

    Логіка точно як у clusterization.py::extract_ground_truth без patch:
      - беремо групи з class_id == 'cls_001'
      - шукаємо group_nuclei серед `instance_ids` (де sem[inst==iid][0] == 1)
      - якщо group_nuclei порожній → група lost.
    """
    groups_path = GROUPS_DIR / f"{stem}.json"
    if not groups_path.exists():
        return (0, 0)
    sem_pair = _semantic_from_workspace(model, stem)
    if sem_pair is None:
        return (0, 0)
    sem, inst = sem_pair

    groups = json.loads(groups_path.read_text(encoding="utf-8-sig")).get("groups") or []
    total, lost = 0, 0
    for g in groups:
        if g.get("class_id") != "cls_001":
            continue
        total += 1
        iids = [int(i) for i in (g.get("instance_ids") or [])]
        has_nucleus = False
        for iid in iids:
            if iid <= 0:
                continue
            sub = sem[inst == iid]
            if sub.size and sub[0] == 1:
                has_nucleus = True
                break
        if not has_nucleus:
            lost += 1
    return (total, lost)


def main() -> int:
    sel = _selections()
    items = [(k, v.get("model")) for k, v in sel.items()
             if v.get("status") == "selected"]
    print(f"=== verify_bug3 — checking {len(items)} stems ===")
    print(f"{'stem':<15} {'cells':>6} {'found':>6} {'lost':>5}")
    print("-" * 40)
    grand_total = grand_lost = 0
    for stem, model in items:
        total, lost = check_stem(stem, model)
        found = total - lost
        grand_total += total
        grand_lost += lost
        marker = "" if lost == 0 else "  <- LOSS"
        print(f"{stem:<15} {total:>6} {found:>6} {lost:>5}{marker}")
    print("-" * 40)
    pct = (grand_lost / grand_total * 100) if grand_total else 0.0
    print(f"TOTAL: {grand_total} cells -> {grand_total - grand_lost} found, "
          f"{grand_lost} LOST ({pct:.1f}%)")
    return 0 if grand_lost == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
