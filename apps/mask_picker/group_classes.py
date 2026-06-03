"""
Group classes (Day 4-5 v2 redesign) — user-defined класифікація для cell grouping.

Замість hardcoded 3 типів ("cell", "vesicle_cluster", "nucleus_only") у v2 redesign:
юзер сам визначає класи (name + color + constraints на довільні label-и) через
"Group Classes Manager" modal (як Labels Manager).

Storage: `<workspace>/group_classes.json` (одна на workspace, не per-image).

Schema:
{
  "version": "1.0",
  "classes": [
    { "id": "cls_001", "name": "cell",
      "color_hue": 130, "color_sat": 50, "color_light": 45,
      "constraints": { "min": {"nucleus": 1, "vesicle": 1}, "max": {} }
    },
    ...
  ]
}

Constraints — універсальні для **будь-якого** label з labels.json. Soft validation:
порушення дає `valid=False` + `reason`, але не блокує save.

Backwards compat: якщо `group_classes.json` нема — auto-creates 3 sensible defaults
з приглушеними HSL кольорами (low neon).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


GROUP_CLASSES_VERSION = "1.0"

# Default classes для першого відкриття workspace. Muted/deep HSL (не neon).
DEFAULT_GROUP_CLASSES = [
    {
        "id": "cls_001",
        "name": "cell",
        "color_hue": 130,
        "color_sat": 45,
        "color_light": 42,
        "constraints": {"min": {"nucleus": 1, "vesicle": 1}, "max": {}},
    },
    {
        "id": "cls_002",
        "name": "vesicle_cluster",
        "color_hue": 28,
        "color_sat": 55,
        "color_light": 48,
        "constraints": {"min": {"vesicle": 1}, "max": {"nucleus": 0}},
    },
    {
        "id": "cls_003",
        "name": "nuclei",
        "color_hue": 280,
        "color_sat": 40,
        "color_light": 48,
        "constraints": {"min": {"nucleus": 1}},
    },
]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _resolve_classes_path(cfg) -> Optional[Path]:
    """
    Resolve cfg.group_classes_file або дефолт `<workspace>/group_classes.json`,
    fallback до `<labels_file_parent>/group_classes.json` якщо немає workspace.
    """
    if cfg.group_classes_file:
        return Path(cfg.group_classes_file)
    if cfg.workspace_dir:
        return cfg.workspace_dir / "group_classes.json"
    if cfg.labels_file:
        return Path(cfg.labels_file).parent / "group_classes.json"
    return None


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _empty_classes_envelope() -> dict:
    return {"version": GROUP_CLASSES_VERSION, "classes": []}


def _read_classes(cfg) -> dict:
    """
    Завантаження group_classes.json. Якщо нема — auto-create defaults (зберігає
    на диск, якщо path resolvable).
    """
    path = _resolve_classes_path(cfg)
    if path is None:
        return {"version": GROUP_CLASSES_VERSION, "classes": list(DEFAULT_GROUP_CLASSES)}
    if not path.exists():
        envelope = {
            "version": GROUP_CLASSES_VERSION,
            "classes": [dict(c) for c in DEFAULT_GROUP_CLASSES],
        }
        try:
            _write_classes(cfg, envelope)
        except Exception as e:
            print(f"[group_classes] auto-create failed: {e}")
        return envelope
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return _empty_classes_envelope()
    if not isinstance(data, dict):
        return _empty_classes_envelope()
    data.setdefault("version", GROUP_CLASSES_VERSION)
    if not isinstance(data.get("classes"), list):
        data["classes"] = []
    return data


def _write_classes(cfg, payload: dict) -> Path:
    """Atomic write."""
    path = _resolve_classes_path(cfg)
    if path is None:
        raise RuntimeError("group_classes path not resolvable")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_classes_payload(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return "payload must be an object"
    classes = data.get("classes")
    if not isinstance(classes, list):
        return "'classes' must be a list"
    for i, c in enumerate(classes):
        if not isinstance(c, dict):
            return f"classes[{i}] must be object"
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            return f"classes[{i}].id must be non-empty string"
        name = c.get("name")
        if not isinstance(name, str) or not name:
            return f"classes[{i}].name must be non-empty string"
        for ck in ("color_hue", "color_sat", "color_light"):
            v = c.get(ck)
            if v is not None and not isinstance(v, (int, float)):
                return f"classes[{i}].{ck} must be number or null"
        cn = c.get("constraints")
        if cn is None:
            continue
        if not isinstance(cn, dict):
            return f"classes[{i}].constraints must be object"
        for slot in ("min", "max"):
            sub = cn.get(slot)
            if sub is None:
                continue
            if not isinstance(sub, dict):
                return f"classes[{i}].constraints.{slot} must be object"
            for lbl, val in sub.items():
                if not isinstance(lbl, str) or not lbl:
                    return f"classes[{i}].constraints.{slot} keys must be non-empty strings"
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    return f"classes[{i}].constraints.{slot}[{lbl}] must be non-negative int"
    return None


# ---------------------------------------------------------------------------
# Lookups + helpers
# ---------------------------------------------------------------------------

def _class_by_id(classes: list, class_id: Optional[str]) -> Optional[dict]:
    if not class_id:
        return None
    for c in classes:
        if c.get("id") == class_id:
            return c
    return None


def _class_by_name(classes: list, name: Optional[str]) -> Optional[dict]:
    if not name:
        return None
    for c in classes:
        if c.get("name") == name:
            return c
    return None


def _next_class_id(classes: list) -> str:
    """Перший вільний cls_NNN (skip gaps)."""
    used = set()
    for c in classes:
        cid = c.get("id", "")
        if isinstance(cid, str) and cid.startswith("cls_"):
            try:
                used.add(int(cid[4:]))
            except ValueError:
                continue
    n = 1
    while n in used:
        n += 1
    return f"cls_{n:03d}"


def _validate_class_against_counts(cls: dict, counts: dict) -> tuple:
    """
    Перевіряє чи counts (label_name → int) задовольняють constraints класу.

    Returns: (valid: bool, reason: Optional[str])
    """
    if not isinstance(cls, dict):
        return True, None
    cn = cls.get("constraints") or {}
    minc = cn.get("min") or {}
    maxc = cn.get("max") or {}
    for lbl, threshold in minc.items():
        if int(counts.get(lbl, 0)) < int(threshold):
            return False, f"need ≥{threshold} {lbl}"
    for lbl, threshold in maxc.items():
        if int(counts.get(lbl, 0)) > int(threshold):
            return False, f"need ≤{threshold} {lbl}"
    return True, None


def _suggest_class_for_counts(counts: dict, classes: list) -> Optional[str]:
    """
    Серед усіх класів повертає id того, який задовольняє constraints найкраще.

    Алгоритм:
    1. Filter to класи що задовольняють constraints.
    2. Якщо одинокий → повертаємо його id.
    3. Якщо кілька — обираємо з найбільшою кількістю constraints (specific > generic).
    4. Якщо жодного — повертаємо None.
    """
    candidates = []
    for c in classes:
        valid, _ = _validate_class_against_counts(c, counts)
        if valid:
            cn = c.get("constraints") or {}
            specificity = len((cn.get("min") or {})) + len((cn.get("max") or {}))
            candidates.append((specificity, c))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])  # most specific first
    return candidates[0][1].get("id")
