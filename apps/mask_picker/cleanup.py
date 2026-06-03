"""
cleanup.py — RGB-encoded labels + per-model rejected + backup rotation.

== Що це ==
Бекенд для "Cleanup" таба редактора: rejected_instances per stem (юзер
відмічає погані маски, які треба викинути перед bake). Plus технічні
helper'и: encoding instance ID у RGB PNG (для frontend hit-test),
backup rotation у selected/<model>/_backups/<stem>/<ts>/.

== Файли на диску ==
- selected/<model>/cleanup.json                — SoT rejected per stem
  (per-model — кожна модель має свій файл).
- selections.json[stem].cleanup.rejected_instances — mirror SoT (для
  швидкого read у UI без скану всіх моделей).
- selected/<model>/_backups/<stem>/<ts>/      — rotational backup перед bake.

== Контент cleanup.json ==
```json
{
  "<stem>": {
    "rejected": [12, 34, 56],
    "markers": [{"x": 100.0, "y": 200.0}],
    "updated_at": "2026-05-28T01:00:00Z",
    "user": "annotator"
  }
}
```

== ID-простір rejected ==
**raw_iid** — IDs у `output/<model>/npy` (raw output від моделі). Стабільні
(Mask Picker не пише сюди). Frontend читає raw через `/api/labels-rgb/<model>/<stem>.png`
(`_labels_to_rgb_png_bytes`), тому `cu.rejectedSet` у JS — у raw просторі.

Bake застосовує rejected до raw: `cleaned[np.isin(cleaned, rejected)] = 0`,
тобто `cleaned` стає без rejected pixel'ів → нумерування `cleaned` лишається
1:1 з raw для non-rejected. Polygon-shape бейкаються у reserved-range id
`POLYGON_ID_BASE + shape_index` (50000+, v1.16.0); `next_id = max(cleaned)+1` —
лише graceful fallback (raw_max≥BASE або забагато shapes).

== Хто пише ==
- POST /api/cleanup/<stem>     — toggle reject (frontend autosave).
- data_sync.bake_with_resync   — (зараз НЕ, але майбутні reseat-міграції).

== Хто читає ==
- baking._bake_polygons_to_selected — фільтрує rejected при bake.
- routes/api_polygons.py::api_rebake — підтягує state[stem].cleanup.rejected.

== Key функції ==
- `_labels_to_rgb_png_bytes(path)` — npy → RGB PNG (3 канали = 24-bit ID).
- `_instance_stats(path)` — швидкий count + shape для UI.
- `_write_cleanup_json(...)` — atomic merge у cleanup.json.
- `_RGB_CACHE` (module-level dict) — кеш RGB-png bytes per (model, stem).
  Інвалідація у `_bake_polygons_to_selected` після bake.
- `_make_backup`, `_rotate_backups` — backup rotation (BACKUP_KEEP=3).

== Залежності ==
state.py (CLEANUP_AVAILABLE, np, Image, OVERLAY_EXTS, _utc_iso, _utc_stamp,
_atomic_write_json).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Optional

from state import (
    CLEANUP_AVAILABLE,
    OVERLAY_EXTS,
    Image,
    ModelSource,
    _atomic_write_json,
    _utc_iso,
    _utc_stamp,
    np,
)


# ---------------------------------------------------------------------------
# Cleanup helpers: labels.npy <-> RGB-encoded PNG
# ---------------------------------------------------------------------------

def _find_npy_for(model: ModelSource, stem: str) -> Optional[Path]:
    """Шукаємо .npy для цього stem у npy_dir моделі (враховуємо 'Копия ...')."""
    if not model.npy_dir or not model.npy_dir.exists():
        return None
    for s in (stem, f"Копия {stem}"):
        p = model.npy_dir / f"{s}.npy"
        if p.exists():
            return p
    return None


def _labels_to_rgb_png_bytes(labels_path: Path) -> bytes:
    """
    Зчитує npy-файл instance-маски (int H×W) і кодує в RGB PNG, де кожен
    піксель = (R << 16 | G << 8 | B) == instance_id.

    Instance ID 0 = фон і кодується чорним (0,0,0). Усі instance ID < 16M.
    """
    if not CLEANUP_AVAILABLE:
        raise RuntimeError("numpy/Pillow недоступні — cleanup не працює")
    labels = np.load(str(labels_path))
    if labels.ndim != 2:
        # іноді npy зберігає (1, H, W) — зріжемо до 2D
        while labels.ndim > 2:
            labels = labels[0]
    labels32 = labels.astype(np.int32, copy=False)
    h, w = labels32.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:, :, 0] = (labels32 >> 16) & 0xFF
    rgb[:, :, 1] = (labels32 >> 8) & 0xFF
    rgb[:, :, 2] = labels32 & 0xFF
    img = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=1)
    return buf.getvalue()


def _instance_stats(labels_path: Path) -> dict:
    """Повертає {instance_count, shape: [H, W]} для швидкого UI."""
    if not CLEANUP_AVAILABLE:
        return {}
    labels = np.load(str(labels_path))
    while labels.ndim > 2:
        labels = labels[0]
    uniq = np.unique(labels)
    # Фон (0) не рахуємо
    return {
        "instance_count": int((uniq != 0).sum()),
        "shape": [int(labels.shape[0]), int(labels.shape[1])],
    }


# Простий in-memory кеш для PNG-кодованих масок (щоб не читати npy на кожен запит).
_RGB_CACHE: dict[tuple[str, str], bytes] = {}
_RGB_CACHE_LOCK = threading.Lock()
_RGB_CACHE_MAX = 32  # ~32 × 1-5 МБ = до 150 МБ памʼяті


def _get_rgb_png_cached(model_name: str, stem: str, labels_path: Path) -> bytes:
    key = (model_name, stem)
    with _RGB_CACHE_LOCK:
        data = _RGB_CACHE.get(key)
        if data is not None:
            return data
    data = _labels_to_rgb_png_bytes(labels_path)
    with _RGB_CACHE_LOCK:
        if len(_RGB_CACHE) >= _RGB_CACHE_MAX:
            # викинемо найстаріший (FIFO)
            try:
                _RGB_CACHE.pop(next(iter(_RGB_CACHE)))
            except StopIteration:
                pass
        _RGB_CACHE[key] = data
    return data


# ---------------------------------------------------------------------------
# Cleanup export helpers: backup rotation + cleanup.json writer
# ---------------------------------------------------------------------------

BACKUP_KEEP = 2
_BACKUP_FORMATS = ("npy", "png", "yolo", "overlay")


def _backup_dir_for_stem(selected_dir: Path, model_name: str, stem: str) -> Path:
    return selected_dir / model_name / "_backups" / stem


def _find_existing_selected(selected_dir: Path, model_name: str, stem: str) -> dict[str, Path]:
    """Знайти існуючі файли у selected/<model>/<fmt>/<stem>.<ext>. Для backup."""
    found: dict[str, Path] = {}
    root = selected_dir / model_name
    candidates = {
        "npy":     (root / "npy",     {".npy"}),
        "png":     (root / "png",     {".png"}),
        "yolo":    (root / "yolo",    {".txt"}),
        "overlay": (root / "overlay", OVERLAY_EXTS),
    }
    for fmt, (d, exts) in candidates.items():
        if not d.exists():
            continue
        for ext in exts:
            p = d / f"{stem}{ext}"
            if p.exists():
                found[fmt] = p
                break
    return found


def _make_backup(selected_dir: Path, model_name: str, stem: str) -> Optional[Path]:
    """
    Копіює поточні файли selected/<model>/<fmt>/<stem>.<ext> у
    _backups/<stem>/<ts>/<fmt>.<ext>. Повертає шлях до timestamp-папки,
    або None якщо нічого бекапити.
    """
    existing = _find_existing_selected(selected_dir, model_name, stem)
    if not existing:
        return None
    ts = _utc_stamp()
    dst_root = _backup_dir_for_stem(selected_dir, model_name, stem) / ts
    dst_root.mkdir(parents=True, exist_ok=True)
    for fmt, src in existing.items():
        dst = dst_root / f"{fmt}{src.suffix}"
        shutil.copy2(src, dst)
    return dst_root


def _rotate_backups(backup_dir_for_stem: Path, keep: int = BACKUP_KEEP) -> None:
    """Залишити тільки `keep` найновіших timestamp-папок. Решту видалити."""
    if not backup_dir_for_stem.exists():
        return
    subs = [p for p in backup_dir_for_stem.iterdir() if p.is_dir()]
    # ISO-timestamp лексикографічний = хронологічний
    subs.sort(key=lambda p: p.name)
    for old in subs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _write_cleanup_json(selected_dir: Path, model_name: str, stem: str,
                        rejected: list[int], user: str,
                        markers: Optional[list[dict]] = None) -> Path:
    """
    Merge-запис у selected/<model>/cleanup.json у форматі
    { "<stem>": { "rejected": [...], "markers": [...], "updated_at": "...", "user": "..." } }.
    Atomically (.tmp + os.replace).
    """
    path = selected_dir / model_name / "cleanup.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            print(f"[cleanup.json] parse failed ({e}), перезапишу з нуля")
            data = {}
    entry: dict = {
        "rejected": sorted(set(int(i) for i in rejected)),
        "updated_at": _utc_iso(),
        "user": user,
    }
    if markers is not None:
        entry["markers"] = [
            {"x": float(m["x"]), "y": float(m["y"])}
            for m in markers
            if isinstance(m, dict) and "x" in m and "y" in m
        ]
    else:
        # Зберегти існуючі markers, якщо вони були
        prev = data.get(stem) or {}
        if "markers" in prev:
            entry["markers"] = prev["markers"]
    data[stem] = entry
    return _atomic_write_json(path, data, sort_keys=True)
