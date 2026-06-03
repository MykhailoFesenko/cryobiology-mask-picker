"""
polygons.py — LabelMe-сумісні polygon-shapes per-image (Stage C).

== Що це ==
Manual / seed-from-mask polygon-shape для доразмітки фото. Юзер малює
у "Polygons" табі: nucleus (ядро) і vesicle (везикула). Контракт —
LabelMe v5.0.1 envelope (сумісність зі сторонніми тулами).

== Файли на диску ==
- polygons/<stem>.json                — SoT polygon-shape per stem.
- polygons/_backups/<stem>/<ts>/      — rotational backup перед write.

== LabelMe envelope ==
```json
{
  "version": "5.0.1",
  "flags": {},
  "shapes": [
    {
      "label": "nucleus",            // ↔ labels.json
      "points": [[x1, y1], [x2, y2], ...],
      "group_id": "g_001",           // optional — mirror з groups.json
      "shape_type": "polygon",
      "flags": {}
    }
  ],
  "imagePath": "db_img_0084.jpg",
  "imageData": null,
  "imageHeight": 1956,
  "imageWidth": 2572
}
```

== ID-простір polygon-shape ==
Polygon-shape **не має** stable instance ID. Вони ідентифікуються через
**index у масиві shapes** (`polygon_index`). При bake polygon-shape #k отримує
reserved baked_iid `POLYGON_ID_BASE + k` (50000+, v1.16.0; `next_id=max+1` —
лише fallback). `polygon_index` → `baked_iid` mapping повертається з
`_bake_polygons_into_labels` як `shape_idx_to_iid`.

ВАЖЛИВО: `polygon_index` НЕ стабільний при splice. Видалення shape
зрушує всі вищі індекси на -1 → `group.polygon_indices` стає невалідним.
Bug 5 fix v1.14.0: фронт `_polyRemapGroupsAfterShapeDelete` оновлює
`groups.json.polygon_indices` при delete shape.

== Хто пише ==
- POST /api/polygons-export/<stem>   — Save Polygons (drawing flow).
- POST /api/polygons/<stem>          — autosave.
- POST /api/polygons/<stem>/multi-seed — bulk seed-from-mask.

== Хто читає ==
- baking._bake_polygons_to_selected — головний споживач.
- groups._instance_labels_from_polygons — Round 2 fix label override.
- audit_export.py — інваріант I7 (polygon_index валідний для shapes).

== Key функції ==
- `_load_labels(path)`           — np.load + 2D normalize.
- `_polygons_path(dir, stem)`    — Path resolution.
- `_write_polygons_json(...)`    — atomic write LabelMe envelope.
- `_validate_polygons_payload(body)` — повертає error str або None.
- `_backup_polygons(dir, stem)`  — backup rotation (POLYGON_BACKUP_KEEP=3).
- `_labelme_envelope(...)`       — формує envelope для seed-from-mask.
- `rename_polygon_labels(...)`   — bulk rename labels (Labels Manager).

== Залежності ==
state.py (np, _utc_stamp). НЕ залежить від baking.py.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from state import _atomic_write_json, _utc_stamp, np


POLYGON_BACKUP_KEEP = 3


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _polygons_path(polygons_dir: Path, stem: str) -> Path:
    return polygons_dir / f"{stem}.json"


def _polygons_backup_dir(polygons_dir: Path, stem: str) -> Path:
    return polygons_dir / "_backups" / stem


def _backup_polygons(polygons_dir: Path, stem: str) -> Optional[Path]:
    """Копіює існуючий polygons/<stem>.json у _backups/<stem>/<ts>/polygons.json."""
    src = _polygons_path(polygons_dir, stem)
    if not src.exists():
        return None
    ts = _utc_stamp()
    dst_dir = _polygons_backup_dir(polygons_dir, stem) / ts
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "polygons.json"
    shutil.copy2(src, dst)
    return dst_dir


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _validate_polygons_payload(data: dict) -> Optional[str]:
    """
    Легка валідація LabelMe-like JSON. Повертає str-помилку або None якщо ок.
    Ми не жорсткі: shapes має бути списком, кожна — dict з points-списком пар.
    """
    if not isinstance(data, dict):
        return "payload must be an object"
    shapes = data.get("shapes")
    if shapes is None:
        data["shapes"] = []
        return None
    if not isinstance(shapes, list):
        return "'shapes' must be a list"
    for i, sh in enumerate(shapes):
        if not isinstance(sh, dict):
            return f"shape[{i}] must be object"
        pts = sh.get("points")
        if not isinstance(pts, list):
            return f"shape[{i}].points must be a list"
        for j, pt in enumerate(pts):
            if (not isinstance(pt, (list, tuple))) or len(pt) != 2:
                return f"shape[{i}].points[{j}] must be [x, y]"
            try:
                float(pt[0]); float(pt[1])
            except Exception:
                return f"shape[{i}].points[{j}] not numeric"
    return None


def _normalize_label_renames(data: object) -> tuple[Optional[dict], Optional[str]]:
    """
    Приймає payload для глобального rename лейблів.

    Підтримує:
      {"from": "cell", "to": "nucleus"}
      {"renames": [{"from": "cell", "to": "nucleus"}, ...]}
      {"renames": {"cell": "nucleus"}}
    """
    if not isinstance(data, dict):
        return None, "payload must be an object"

    raw = data.get("renames")
    if raw is None and "from" in data and "to" in data:
        raw = [{"from": data.get("from"), "to": data.get("to")}]

    renames: dict[str, str] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = []
        for item in raw:
            if not isinstance(item, dict):
                return None, "each rename must be an object"
            items.append((item.get("from"), item.get("to")))
    else:
        return None, "expected 'renames' or 'from'/'to'"

    for old, new in items:
        old_s = str(old or "").strip()
        new_s = str(new or "").strip()
        if not old_s or not new_s:
            return None, "rename labels must be non-empty"
        if old_s != new_s:
            renames[old_s] = new_s
    return renames, None


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_polygons_json(polygons_dir: Path, stem: str, payload: dict) -> Path:
    """
    Atomically перезаписує polygons/<stem>.json. Очікує, що payload вже валідний.

    Унікальний tmp (_atomic_write_json) — щоб паралельні autosave того самого
    stem на Flask threaded-сервері не клобали один одного (Day 9 Bug 2).
    """
    return _atomic_write_json(_polygons_path(polygons_dir, stem), payload)


def _rename_labels_in_polygon_files(polygons_dir: Path, renames: dict[str, str]) -> dict:
    """Перейменовує labels у всіх polygons/*.json, не заходячи в _backups."""
    # Avoid circular import via cleanup.py — _rotate_backups not needed here, але
    # ми використовуємо polygon-bekup семантику.
    files_changed = 0
    shapes_changed = 0
    skipped_files: list[dict] = []

    if not renames:
        return {
            "files_changed": 0,
            "shapes_changed": 0,
            "skipped_files": [],
        }

    # Lazy import to avoid cycles (cleanup imports nothing from polygons,
    # but keep modules independent).
    from cleanup import _rotate_backups

    for path in sorted(polygons_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            skipped_files.append({"path": str(path), "reason": f"read_error: {e}"})
            continue

        if not isinstance(payload, dict):
            skipped_files.append({"path": str(path), "reason": "payload_not_object"})
            continue

        shapes = payload.get("shapes")
        if not isinstance(shapes, list):
            continue

        changed_here = 0
        for shape in shapes:
            if not isinstance(shape, dict):
                continue
            label = shape.get("label")
            if label in renames:
                shape["label"] = renames[label]
                changed_here += 1

        if changed_here == 0:
            continue

        stem = path.stem
        _backup_polygons(polygons_dir, stem)
        _rotate_backups(_polygons_backup_dir(polygons_dir, stem),
                        keep=POLYGON_BACKUP_KEEP)
        _write_polygons_json(polygons_dir, stem, payload)
        files_changed += 1
        shapes_changed += changed_here

    return {
        "files_changed": files_changed,
        "shapes_changed": shapes_changed,
        "skipped_files": skipped_files,
    }


# ---------------------------------------------------------------------------
# LabelMe envelope + npy loader (shared with baking)
# ---------------------------------------------------------------------------

def _load_labels(npy_path: Path):
    """np.load + squeeze до 2D."""
    labels = np.load(str(npy_path))
    while labels.ndim > 2:
        labels = labels[0]
    return labels


def _labelme_envelope(stem: str, image_path: Optional[str], H: int, W: int,
                      shapes: list[dict]) -> dict:
    """Стандартний LabelMe-like JSON."""
    return {
        "version": "5.0.1",
        "flags": {},
        "shapes": shapes,
        "imagePath": image_path or f"{stem}.jpg",
        "imageData": None,
        "imageHeight": int(H),
        "imageWidth": int(W),
    }
