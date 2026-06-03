"""
state.py — фундамент: Config, StateStore, версія, atomic write, image helpers.

== Що це ==
Самодостатній модуль (foundation layer) — імпортується усіма іншими
backend модулями (baking/groups/cleanup/polygons/data_sync/routes).
**Не залежить** від жодного з них (захист від circular imports).

== Що лежить тут ==
- **APP_VERSION** = "1.15.0" — глобальна версія Mask Picker.
- **Config** dataclass — шляхи workspace + workspace_dir + models list.
- **ModelSource** dataclass — name + paths (npy_dir, png_dir, yolo_dir, ...).
- **StateStore** — wrap навколо `selections.json` (status/model/cleanup/dirty).
- **_atomic_write_json(path, payload)** — atomic JSON write через tempfile +
  os.replace. Безпечно при Flask threaded=True (унікальний tmp на кожен
  запис, конкурентні writes у той самий файл не клобають один одного).
- **_load_label_classes(labels_file)** — labels.json reader.
- **_load_original_image_array(images_dir, stem)** — opencv image read з
  кириличним workaround (`cv2.imdecode(np.fromfile())`).
- **CLEANUP_AVAILABLE / CV2_AVAILABLE / EXPORT_AVAILABLE** — feature flags
  для optional deps (numpy/PIL/cv2/cellsegkit).

== APP_VERSION ==
Bumped at кожний minor release. Frontend читає через GET /api/version.
Test: `tests/test_polygons_smoke.py::test_api_version`.

== StateStore ==
Concurrency: один global threading.Lock на всі operations. Flask threaded —
тому всі mutate операції повинні бути крізь StateStore (НЕ напряму у JSON).
Persist — atomic merge на disk через `_atomic_write_json`.

Поля per-stem entry у selections.json:
- status: "selected" | "skipped" | None
- model: ім'я selected моделі
- cleanup: {model, rejected_instances, markers, updated_at, user}
- polygons: {ts}                          (legacy marker)
- base_label: "nucleus" | "vesicle"       (default label для bake)
- ts: ISO timestamp last update
- user: ім'я анотатора
- dirty: bool                             (Day 7 — Save All workflow)

== Залежності ==
- numpy, PIL.Image (CLEANUP), cv2 (CV2), cellsegkit (EXPORT) — optional.
- НЕ імпортується ніхто з інших v2 модулів (foundation).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

# Numpy/PIL для cleanup-режиму (labels → RGB PNG encoding).
# Імпортуються ліниво — якщо пакети відсутні, просто не буде cleanup feature.
try:
    import numpy as np
    from PIL import Image
    CLEANUP_AVAILABLE = True
except ImportError:
    np = None        # type: ignore
    Image = None     # type: ignore
    CLEANUP_AVAILABLE = False

# cellsegkit.exporter — для регенерації чистих масок/YOLO/overlay при export.
# Ліниво: якщо пакет не встановлено, /api/cleanup-export повертає 503.
try:
    from cellsegkit.exporter.exporter import export_segmentation_bundle, draw_overlay
    EXPORT_AVAILABLE = CLEANUP_AVAILABLE
except ImportError:
    export_segmentation_bundle = None  # type: ignore
    draw_overlay = None  # type: ignore
    EXPORT_AVAILABLE = False

# OpenCV для seed-from-mask (Stage C — контури з instance-маски).
try:
    import cv2  # type: ignore
    CV2_AVAILABLE = CLEANUP_AVAILABLE
except ImportError:
    cv2 = None                # type: ignore
    CV2_AVAILABLE = False


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
# apps/mask_picker/state.py → корінь проєкту = три рівні вгору
PROJECT_ROOT = HERE.parent.parent.parent
DATA_ROOT = PROJECT_ROOT / "data" / "nuclei"

APP_NAME = "Mask Picker"
APP_VERSION = "1.16.2"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
OVERLAY_EXTS = {".png", ".jpg", ".jpeg"}


def _utc_iso() -> str:
    """UTC timestamp у старому форматі app-а: ISO без offset + Z."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _utc_stamp() -> str:
    """UTC timestamp для backup-папок."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write_json(path: Path, payload, *, sort_keys: bool = False) -> Path:
    """
    Атомарний запис JSON: унікальний tmp-файл у тій самій папці + os.replace.

    Унікальний tmp (tempfile.mkstemp) критичний при Flask threaded=True —
    кілька паралельних записів того самого файлу інакше клобають спільний
    `.json.tmp`. os.replace атомарний у межах однієї файлової системи.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=sort_keys)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


# ---------------------------------------------------------------------------
# ModelSource & Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ModelSource:
    """Одна модель-кандидат з її масками/оверлеями."""
    name: str
    overlay_dir: Optional[Path] = None
    png_dir: Optional[Path] = None
    npy_dir: Optional[Path] = None
    yolo_dir: Optional[Path] = None

    def has_image(self, stem: str) -> bool:
        """Чи є overlay для цього фото у цієї моделі?"""
        if not self.overlay_dir or not self.overlay_dir.exists():
            return False
        for ext in OVERLAY_EXTS:
            if (self.overlay_dir / f"{stem}{ext}").exists():
                return True
        return False

    def overlay_path(self, stem: str) -> Optional[Path]:
        if not self.overlay_dir:
            return None
        for ext in OVERLAY_EXTS:
            p = self.overlay_dir / f"{stem}{ext}"
            if p.exists():
                return p
        return None


@dataclass
class Config:
    images_dir: Path
    output_root: Path
    selected_dir: Path
    skipped_dir: Path
    polygons_dir: Optional[Path] = None
    groups_dir: Optional[Path] = None
    workspace_dir: Optional[Path] = None
    labels_file: Optional[Path] = None
    group_classes_file: Optional[Path] = None
    models: list[ModelSource] = field(default_factory=list)
    formats_to_copy: tuple[str, ...] = ("overlay", "png", "npy", "yolo")
    deduplicate_by_stem: bool = True  # dedupe "Копия db_img_XXX" vs "db_img_XXX"


# ---------------------------------------------------------------------------
# Model discovery & config loader
# ---------------------------------------------------------------------------

def _discover_models(output_root: Path) -> list[ModelSource]:
    """
    Авто-пошук моделей в output/.

    Правила:
    1) Якщо є підпапка з підпапкою overlay/ всередині — це модель.
       Назва моделі = ім'я підпапки (cyto2, instanseg, stardist, ...).
    2) Якщо в самому output/ лежать overlay/, png/, ... — це legacy запуск,
       називаємо його '_legacy_root'.
    3) Порожні моделі (без жодного overlay-файлу) ігноруємо.
    """
    models: list[ModelSource] = []
    if not output_root.exists():
        return models

    # Legacy: overlay/ прямо в output/
    legacy_overlay = output_root / "overlay"
    if legacy_overlay.is_dir() and any(legacy_overlay.iterdir()):
        models.append(
            ModelSource(
                name="_legacy_root",
                overlay_dir=legacy_overlay,
                png_dir=output_root / "png" if (output_root / "png").is_dir() else None,
                npy_dir=output_root / "npy" if (output_root / "npy").is_dir() else None,
                yolo_dir=output_root / "yolo" if (output_root / "yolo").is_dir() else None,
            )
        )

    # Модель-підпапки
    for sub in sorted(output_root.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name in {"overlay", "png", "npy", "yolo", "_legacy", "_selected", "_skipped"}:
            continue
        ov = sub / "overlay"
        if ov.is_dir() and any(ov.iterdir()):
            models.append(
                ModelSource(
                    name=sub.name,
                    overlay_dir=ov,
                    png_dir=sub / "png" if (sub / "png").is_dir() else None,
                    npy_dir=sub / "npy" if (sub / "npy").is_dir() else None,
                    yolo_dir=sub / "yolo" if (sub / "yolo").is_dir() else None,
                )
            )
    return models


def load_config(config_path: Optional[Path]) -> Config:
    """
    Завантажити конфіг з yaml АБО авто-вивести з typical Cryobiology layout.
    """
    # Defaults — DATA_ROOT = PROJECT_ROOT / data / nuclei
    images_dir = DATA_ROOT / "images"
    output_root = DATA_ROOT / "output"
    selected_dir = DATA_ROOT / "selected"
    skipped_dir = DATA_ROOT / "skipped"
    polygons_dir = DATA_ROOT / "polygons"
    groups_dir = DATA_ROOT / "groups"
    labels_file = DATA_ROOT / "labels.json"
    formats_to_copy = ("overlay", "png", "npy", "yolo")
    deduplicate_by_stem = True
    explicit_models: list[ModelSource] = []

    if config_path and config_path.exists() and yaml is not None:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        def _path(key, default):
            v = data.get(key)
            if v is None:
                return default
            p = Path(v)
            if not p.is_absolute():
                p = (config_path.parent / p).resolve()
            return p

        images_dir = _path("images_dir", images_dir)
        output_root = _path("output_root", output_root)
        selected_dir = _path("selected_dir", selected_dir)
        skipped_dir = _path("skipped_dir", skipped_dir)
        polygons_dir = _path("polygons_dir", polygons_dir)
        groups_dir = _path("groups_dir", groups_dir)
        formats_to_copy = tuple(data.get("formats_to_copy", formats_to_copy))
        deduplicate_by_stem = bool(data.get("deduplicate_by_stem", True))

        for m in data.get("models", []) or []:
            mdir = Path(m["dir"])
            if not mdir.is_absolute():
                mdir = (config_path.parent / mdir).resolve()
            explicit_models.append(
                ModelSource(
                    name=m["name"],
                    overlay_dir=mdir / "overlay" if (mdir / "overlay").is_dir() else None,
                    png_dir=mdir / "png" if (mdir / "png").is_dir() else None,
                    npy_dir=mdir / "npy" if (mdir / "npy").is_dir() else None,
                    yolo_dir=mdir / "yolo" if (mdir / "yolo").is_dir() else None,
                )
            )

    models = explicit_models or _discover_models(output_root)

    return Config(
        images_dir=images_dir,
        output_root=output_root,
        selected_dir=selected_dir,
        skipped_dir=skipped_dir,
        polygons_dir=polygons_dir,
        groups_dir=groups_dir,
        labels_file=labels_file,
        models=models,
        formats_to_copy=formats_to_copy,
        deduplicate_by_stem=deduplicate_by_stem,
    )


# ---------------------------------------------------------------------------
# StateStore — selections.json persistence
# ---------------------------------------------------------------------------

class StateStore:
    """
    Зберігає стан відбору у selections.json поряд з selected_dir.

    Формат:
    {
      "db_img_0277": {"model": "cyto2",     "status": "selected", "ts": "...", "user": "..."},
      "db_img_0278": {"model": null,        "status": "skipped",  "ts": "...", "reason": "..."},
      "db_img_0279": {"model": "instanseg", "status": "selected", "ts": "..."}
    }
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    self._data = json.load(f)
            except Exception as e:
                print(f"[WARN] Не зміг прочитати {self.path}: {e}. Починаю з порожнього стану.")
                self._data = {}
        else:
            self._data = {}

    def reload(self, path: Path) -> None:
        """Перемкнути store на інший selections.json (workspace picker)."""
        with self._lock:
            self.path = path
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    def get(self, stem: str) -> Optional[dict]:
        with self._lock:
            return dict(self._data.get(stem)) if stem in self._data else None

    def set(self, stem: str, entry: dict) -> None:
        with self._lock:
            self._data[stem] = entry
            self._flush()

    def remove(self, stem: str) -> None:
        with self._lock:
            self._data.pop(stem, None)
            self._flush()

    def all(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._data)

    # ---- Cleanup (етап A + маркери Stage C) ------------------------------
    def get_cleanup(self, stem: str) -> dict:
        """Повертає {model, rejected_instances, markers, updated_at} або порожній dict."""
        with self._lock:
            entry = self._data.get(stem) or {}
            return dict(entry.get("cleanup") or {})

    def set_cleanup(self, stem: str, model: str, rejected_instances: list[int],
                    markers: Optional[list[dict]] = None,
                    user: str = "anonymous") -> dict:
        """
        Зберігає список відхилених instance ID + опційні "missing cell" маркери
        для конкретної моделі. Merge-семантика: інші поля entry не чіпаються.

        markers: list of {"x": float, "y": float} в координатах оригінальної маски
                 (0..W, 0..H). None = не чіпати існуючі маркери.
        """
        with self._lock:
            entry = dict(self._data.get(stem) or {})
            prev = entry.get("cleanup") or {}
            existing_markers = prev.get("markers", []) if isinstance(prev, dict) else []
            if markers is None:
                use_markers = existing_markers
            else:
                use_markers = [
                    {"x": float(m["x"]), "y": float(m["y"])}
                    for m in markers
                    if isinstance(m, dict) and "x" in m and "y" in m
                ]
            entry["cleanup"] = {
                "model": model,
                "rejected_instances": sorted(set(int(i) for i in rejected_instances)),
                "markers": use_markers,
                "updated_at": _utc_iso(),
                "user": user,
            }
            self._data[stem] = entry
            self._flush()
            return dict(entry["cleanup"])

    # ---- Dirty flag (Day 7 lazy-bake) -----------------------------------
    # `dirty=True` означає: у stem є збережені зміни (polygons.json /
    # cleanup rejected / base_label), які ще НЕ запечені у selected/.
    # Виставляється write-операціями, знімається після успішного bake.
    # Працює тільки для stem-ів, що вже мають entry (тобто пройшли Pick) —
    # bake без обраної моделі неможливий, тому dirty для них безсенсовий.

    def mark_dirty(self, stem: str) -> None:
        """Позначити stem як такий, що потребує bake у selected/."""
        with self._lock:
            entry = self._data.get(stem)
            if entry is None or entry.get("dirty") is True:
                return
            entry["dirty"] = True
            self._flush()

    def clear_dirty(self, stem: str) -> None:
        """Зняти dirty-прапор після успішного bake."""
        with self._lock:
            entry = self._data.get(stem)
            if entry is None or not entry.get("dirty"):
                return
            entry["dirty"] = False
            self._flush()

    def is_dirty(self, stem: str) -> bool:
        with self._lock:
            return bool((self._data.get(stem) or {}).get("dirty"))

    def list_dirty(self) -> list[str]:
        """Усі stem-и з dirty=True (для Save All), відсортовані."""
        with self._lock:
            return sorted(s for s, e in self._data.items() if e.get("dirty"))

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Label classes (labels.json)
# ---------------------------------------------------------------------------

DEFAULT_LABEL_CLASSES: list[dict] = [
    {"id": 1, "name": "nucleus", "color": "#4488ff", "shortcut": "1"},
]


def _load_label_classes(labels_file: Optional[Path]) -> list[dict]:
    """Читає labels.json; повертає DEFAULT_LABEL_CLASSES якщо файл відсутній.

    Формат labels.json (LabelMe-сумісний):
        [{"id": 1, "name": "nucleus", "color": "#4488ff", "shortcut": "1"}, ...]

    Поле "id" зберігається лише як довідкове (так пишуть LabelMe-туліни).
    Реальний YOLO class_id будується з порядкового номера у списку через
    `enumerate(classes)` (порядок у файлі = class_id, починаючи з 0).
    Не модифікувати поле "id" руками — це не впливає на YOLO mapping.
    """
    if labels_file and labels_file.exists():
        try:
            with open(labels_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return list(DEFAULT_LABEL_CLASSES)


def _save_label_classes(labels_file: Path, classes: list[dict]) -> None:
    labels_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = labels_file.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(classes, f, ensure_ascii=False, indent=2)
    os.replace(tmp, labels_file)


# ---------------------------------------------------------------------------
# Image / file helpers
# ---------------------------------------------------------------------------

def _load_original_image_array(images_dir: Path, stem: str):
    """Знайти оригінальну картинку (з урахуванням 'Копия') і завантажити як np.array."""
    if not CLEANUP_AVAILABLE:
        return None
    for ext in IMAGE_EXTS:
        for s in (stem, f"Копия {stem}"):
            p = images_dir / f"{s}{ext}"
            if p.exists():
                img = Image.open(p)
                return np.array(img)
    return None


def _find_image_filename(images_dir: Path, stem: str) -> Optional[str]:
    """Повернути ім'я файлу оригіналу для <stem>, враховуючи 'Копия'."""
    for ext in IMAGE_EXTS:
        for s in (stem, f"Копия {stem}"):
            p = images_dir / f"{s}{ext}"
            if p.exists():
                return p.name
    return None


def _load_json_file(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f) or default


# ---------------------------------------------------------------------------
# Copy model files into selected/ (used by api_select)
# ---------------------------------------------------------------------------

def _copy_model_files_for_stem(cfg: Config, m: ModelSource, stem: str) -> dict:
    """
    Копіює overlay/png/npy/yolo вибраної моделі у cfg.selected_dir/<model>/<format>/.
    Повертає мапу {format: скопійований файл}.
    """
    copied: dict[str, str] = {}

    def _find(src_dir: Optional[Path], exts: set[str]) -> Optional[Path]:
        if not src_dir or not src_dir.exists():
            return None
        for s in (stem, f"Копия {stem}"):
            for ext in exts:
                p = src_dir / f"{s}{ext}"
                if p.exists():
                    return p
        return None

    mapping = {
        "overlay": (m.overlay_dir, OVERLAY_EXTS),
        "png":     (m.png_dir,     {".png"}),
        "npy":     (m.npy_dir,     {".npy"}),
        "yolo":    (m.yolo_dir,    {".txt"}),
    }
    for fmt in cfg.formats_to_copy:
        src_dir, exts = mapping.get(fmt, (None, set()))
        src = _find(src_dir, exts)
        if src is None:
            continue
        dst_dir = cfg.selected_dir / m.name / fmt
        dst_dir.mkdir(parents=True, exist_ok=True)
        # Зберігаємо під нормалізованим stem (без "Копия"), але з оригінальним розширенням
        dst = dst_dir / f"{stem}{src.suffix}"
        shutil.copy2(src, dst)
        try:
            copied[fmt] = dst.relative_to(cfg.selected_dir.parent).as_posix()
        except ValueError:
            copied[fmt] = str(dst)
    return copied
