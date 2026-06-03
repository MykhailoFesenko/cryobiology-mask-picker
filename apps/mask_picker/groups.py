"""
groups.py — cell grouping (Day 4-5 v2.0.0).

== Що це ==
Групи поверх baked instance + drawn polygon-shape: одна "клітина" =
ядро (nucleus) + 1-N везикул (vesicles). Дає бекенд для 3-го таба
"Groups" редактора: lasso/click для batch-додавання, classification
(cell/vesicle_cluster/nucleus_only), constraint check.

== Файли на диску ==
- groups/<stem>.json                          — SoT (per-image групи).
- polygons/<stem>.json.shapes[i].group_id     — derived mirror (LabelMe v5).

Mirror у polygons.json — це додаток для сторонніх LabelMe-тулів, ми його
**не парсимо**. SoT — тільки `groups/<stem>.json`.

== Контент структура groups/<stem>.json ==
```json
{
  "version": "1.1",
  "stem": "db_img_0084",
  "model": "instanseg",
  "groups": [
    {
      "id": "g_001",
      "class_id": "cls_001",       // ↔ group_classes.json
      "type": null,                // legacy, мігрується у class_id
      "instance_ids": [12, 34, 7], // baked iid з filtered npy
      "polygon_indices": [0, 5]    // індекси у polygons.json.shapes
    }
  ]
}
```

== Хто пише ==
- POST /api/groups/<stem>           — основне (через _enforce_single_membership).
- baking._sync_groups_instance_ids_after_bake — post-bake резолв polygon→iid.
- data_sync._strip_orphans_in_groups_file     — self-heal B3 на disk.

== Хто читає ==
- GET /api/groups/<stem>            — повертає envelope + classifications.
- baking.export_derived_masks       — для mask_groups.png + semantic.
- audit_export.py                   — інваріант check.

== Key helpers (експорт) ==
- `_enforce_single_membership(groups)` → moves[] (POST /api/groups path)
- `_classify_group_membership(...)` → counts + valid/invalid + suggested_class_id
- `_strip_orphan_instance_ids(groups, known_iids)` → log of stripped
  (in-memory; data_sync пише напряму на disk)
- `_instance_labels_from_polygons(...)` — Round 2 fix v1.12.0 (label overrides)
- `_migrate_groups_type_to_class_id(groups, classes)` — backward compat

== Invariants ==
- I1 strict : iid у group.instance_ids унікальний у межах групи.
- I3 strict : polygon_index у group.polygon_indices унікальний.
- I4 strict : жоден iid не у двох групах (single-membership).
- I5 lazy   : rejected ∩ group.instance_ids == ∅ (self-heal at bake).
- B3 lazy   : group.instance_ids ⊆ unique(baked npy) (self-heal at bake/GET).

== Що може зламатись ==
- Якщо polygon-shape видалено з polygons.json — `polygon_index` у group
  out-of-range або вказує на іншу форму. v1.14.0 фронт-fix Bug 5 запобігає
  цьому: `_polyRemapGroupsAfterShapeDelete` у editor/polygons.js
  автоматично оновлює polygon_indices при splice.
- Якщо rejected iid випадково == polygon-resolved iid (next_id collision)
  — I5 формальне порушення, але семантично OK. Не виправити простим strip
  (Bug 7 revert). Workaround: документується.

Legacy (hardcoded 3 типи — fallback якщо немає group_classes.json):
  * cell           — ≥1 nucleus AND ≥1 vesicle
  * vesicle_cluster— ≥1 vesicle, 0 nuclei
  * nucleus_only   — ≥1 nucleus, 0 vesicles

Single-membership: instance_id або polygon_index належить тільки до однієї
групи. Поведінка "add to new → remove from old" реалізується через
`_enforce_single_membership` на write.

Soft validation: невалідні групи зберігаються, лише отримують `valid=False`
з полем `reason`. Backend не блокує save — frontend показує badge ✗.

Залежить від: state.py (np, _utc_stamp), cleanup.py (_rotate_backups).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from state import _atomic_write_json, _utc_stamp, np
from group_classes import (
    _class_by_id,
    _class_by_name,
    _suggest_class_for_counts,
    _validate_class_against_counts,
)


GROUPS_BACKUP_KEEP = 3
GROUPS_VERSION = "1.1"  # 1.1: class_id заміняє hardcoded type (legacy ok)
GROUP_TYPES = ("cell", "vesicle_cluster", "nucleus_only")

NUCLEUS_LABEL = "nucleus"
VESICLE_LABEL = "vesicle"

# Reserved-ID база для polygon-baked instance. КАНОНІЧНО визначена у
# baking.POLYGON_ID_BASE; дублюємо локально, щоб не тягнути важкий baking
# (PIL/cleanup/polygons) у легкий SoT-модуль groups (layering: groups залежить
# лише від state/group_classes). test_groups_smoke має guard на рівність.
# Полігон shape #k бейкається у instance id POLYGON_ID_BASE + k.
POLYGON_ID_BASE = 50000

# Palette — 8 evenly-spaced hues. New group без override бере наступний free.
# При >8 груп — повертається на початок, frontend застосовує per-group jitter
# (±15° hue, ±10% sat/lightness) на основі hash(group_id) щоб розрізняти.
PALETTE_HUES = (0, 30, 60, 120, 180, 210, 270, 330)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _groups_path(groups_dir: Path, stem: str) -> Path:
    return groups_dir / f"{stem}.json"


def _groups_backup_dir(groups_dir: Path, stem: str) -> Path:
    return groups_dir / "_backups" / stem


def _backup_groups(groups_dir: Path, stem: str) -> Optional[Path]:
    """Копія існуючого groups/<stem>.json у _backups/<stem>/<ts>/groups.json."""
    src = _groups_path(groups_dir, stem)
    if not src.exists():
        return None
    ts = _utc_stamp()
    dst_dir = _groups_backup_dir(groups_dir, stem) / ts
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "groups.json"
    shutil.copy2(src, dst)
    return dst_dir


# ---------------------------------------------------------------------------
# Envelope & I/O
# ---------------------------------------------------------------------------

def _empty_groups_envelope(stem: str, model: Optional[str] = None) -> dict:
    return {
        "version": GROUPS_VERSION,
        "stem": stem,
        "model": model,
        "groups": [],
    }


def _read_groups(groups_dir: Path, stem: str) -> dict:
    """
    Завантаження groups/<stem>.json. Якщо файла нема або payload зіпсований —
    повертається порожній envelope (lenient — той самий контракт, що у
    polygons / cleanup).
    """
    path = _groups_path(groups_dir, stem)
    if not path.exists():
        return _empty_groups_envelope(stem)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return _empty_groups_envelope(stem)
    if not isinstance(data, dict):
        return _empty_groups_envelope(stem)
    data.setdefault("version", GROUPS_VERSION)
    data.setdefault("stem", stem)
    data.setdefault("model", None)
    groups = data.get("groups")
    if not isinstance(groups, list):
        data["groups"] = []
    return data


def _write_groups(groups_dir: Path, stem: str, payload: dict) -> Path:
    """
    Атомарно перезаписує groups/<stem>.json. Очікує валідний payload.

    Унікальний tmp (_atomic_write_json) — захист від паралельних autosave
    на Flask threaded-сервері (Day 9 Bug 2).
    """
    return _atomic_write_json(_groups_path(groups_dir, stem), payload)


# ---------------------------------------------------------------------------
# Validation (структурна)
# ---------------------------------------------------------------------------

def _validate_groups_payload(data: dict) -> Optional[str]:
    """
    Легка валідація structure. Повертає str-помилку або None якщо ок.
    Soft semantics: constraint порушення (cell без nucleus) тут НЕ перевіряємо
    — це доменна валідація, через `_classify_group_membership`.

    Group мусить мати АБО `class_id` (нове, v1.1) АБО legacy `type` (v1.0).
    """
    if not isinstance(data, dict):
        return "payload must be an object"
    groups = data.get("groups")
    if groups is None:
        data["groups"] = []
        return None
    if not isinstance(groups, list):
        return "'groups' must be a list"
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            return f"groups[{i}] must be object"
        gid = g.get("id")
        if not isinstance(gid, str) or not gid:
            return f"groups[{i}].id must be non-empty string"
        # Або class_id (нове), або type (legacy) — один обов'язковий
        cid = g.get("class_id")
        gtype = g.get("type")
        if cid is not None:
            if not isinstance(cid, str) or not cid:
                return f"groups[{i}].class_id must be non-empty string"
        elif gtype is not None:
            if gtype not in GROUP_TYPES:
                return (f"groups[{i}].type must be one of {GROUP_TYPES} "
                        f"(legacy) — або використай class_id")
        else:
            return f"groups[{i}] must have class_id or type"
        iids = g.get("instance_ids")
        if iids is not None and not isinstance(iids, list):
            return f"groups[{i}].instance_ids must be a list"
        if isinstance(iids, list):
            for iid in iids:
                if not isinstance(iid, int) or isinstance(iid, bool):
                    return f"groups[{i}].instance_ids contains non-int"
        pidx = g.get("polygon_indices")
        if pidx is not None and not isinstance(pidx, list):
            return f"groups[{i}].polygon_indices must be a list"
        if isinstance(pidx, list):
            for pi in pidx:
                if not isinstance(pi, int) or isinstance(pi, bool):
                    return f"groups[{i}].polygon_indices contains non-int"
        hue = g.get("color_hue")
        if hue is not None and not isinstance(hue, (int, float)):
            return f"groups[{i}].color_hue must be number"
    return None


def _resolve_class_id(group: dict, classes: list) -> Optional[str]:
    """
    Migration helper: якщо group has class_id — return it.
    Якщо group has legacy `type` — resolves через class name match.
    """
    cid = group.get("class_id")
    if isinstance(cid, str) and cid:
        return cid
    gtype = group.get("type")
    if isinstance(gtype, str) and classes:
        c = _class_by_name(classes, gtype)
        if c:
            return c.get("id")
    return None


def _migrate_groups_type_to_class_id(groups: list, classes: list) -> int:
    """
    In-place migration: для кожної group без `class_id`, якщо її legacy `type`
    matches class name → set `class_id`. Returns кількість груп що мігрували.
    """
    if not classes:
        return 0
    migrated = 0
    for g in groups:
        if g.get("class_id"):
            continue
        gtype = g.get("type")
        if not isinstance(gtype, str):
            continue
        c = _class_by_name(classes, gtype)
        if c:
            g["class_id"] = c.get("id")
            migrated += 1
    return migrated


# ---------------------------------------------------------------------------
# Soft domain validation + type auto-suggest
# ---------------------------------------------------------------------------

def _count_labels_in_group(
    instance_labels: dict,
    polygon_labels: list,
    group: dict,
) -> dict:
    """Counts {label_name → int} з instance_ids + polygon_indices.

    Reserved-range iid (>= POLYGON_ID_BASE) ЗАВЖДИ пропускається у підрахунку
    по instance_ids — полігони рахуються ЛИШЕ через `polygon_indices` (джерело
    правди для полігонів у групі). Reserved iid — bake-артефакт того ж полігона
    (sync кладе його у instance_ids для deliverable npy). Без цього:
      (а) подвійний підрахунок коли полігон у групі (полігон + його baked iid);
      (б) «привид» коли полігон ПРИБРАНО з групи (polygon_indices порожнє), але
          baked iid лишився stale у instance_ids (db_img_0171 g_008: прибрані 2
          ядра все ще рахувались). Model-instance завжди < POLYGON_ID_BASE.
    """
    counts: dict = {}
    for iid in group.get("instance_ids") or []:
        try:
            iid_int = int(iid)
        except (TypeError, ValueError):
            continue
        if iid_int >= POLYGON_ID_BASE:
            continue
        lbl = instance_labels.get(iid_int)
        if isinstance(lbl, str) and lbl:
            counts[lbl] = counts.get(lbl, 0) + 1
    for pi in group.get("polygon_indices") or []:
        try:
            pi_int = int(pi)
        except (TypeError, ValueError):
            continue
        if 0 <= pi_int < len(polygon_labels):
            lbl = polygon_labels[pi_int]
            if isinstance(lbl, str) and lbl:
                counts[lbl] = counts.get(lbl, 0) + 1
    return counts


def _iids_by_label_in_group(instance_labels: dict, group: dict) -> dict:
    """
    {label → [instance_id, ...]} для всіх instance_id з групи, що мають лейблу
    у `instance_labels`. Для frontend: «винуватці» invalid-групи. Без полігонів —
    polygon-indices мають свій label-list і не плутаються з instance.
    """
    by_label: dict = {}
    for iid in group.get("instance_ids") or []:
        try:
            iid_int = int(iid)
        except (TypeError, ValueError):
            continue
        if iid_int >= POLYGON_ID_BASE:   # полігон bake-артефакт — через polygon_indices
            continue
        lbl = instance_labels.get(iid_int)
        if isinstance(lbl, str) and lbl:
            by_label.setdefault(lbl, []).append(iid_int)
    for lst in by_label.values():
        lst.sort()
    return by_label


def _violating_iids_for_class(cls: dict, counts: dict, iids_by_label: dict) -> list:
    """Повертає sorted list iid, які порушують constraints класу.

    Pre-задача — звузити список до конкретних instance_id, щоб юзер знав
    кого видалити. Покриває обидві сторони порушень:
      * `max[label] = N` → надлишкові iid цієї лейбли над N.
      * `min[label] = N` → нічого окремо не повертає (нема нічого «зайвого»;
        треба ДОДАТИ instance, тут не лікуємо).
    """
    if not isinstance(cls, dict):
        return []
    cn = cls.get("constraints") or {}
    maxc = cn.get("max") or {}
    out: list = []
    for lbl, threshold in maxc.items():
        try:
            t = int(threshold)
        except (TypeError, ValueError):
            continue
        ids = iids_by_label.get(lbl) or []
        if len(ids) > t:
            out.extend(ids)   # усі instance цієї лейбли — порушники
    return sorted(set(int(i) for i in out))


def _strip_orphan_instance_ids(groups: list, known_iids: set) -> list:
    """In-place видаляє з `group.instance_ids` посилання на iid, яких немає у
    `known_iids` (множина instance ID з baked npy). Повертає журнал
    `[{group_id, removed: [iid, ...]}]` — для toast у frontend.

    Use case (db_img_0169 g_045): після rebake / cleanup-reject у групі
    залишилися stale iid (3077, 3078, 3082) на які тепер немає
    реального instance у masks. Це псує classify (counts=N, але реально
    N-M, де M orphans). Без авто-strip юзеру треба руками щоразу шукати
    «прізраки» і видаляти. Авто-strip = invariant: group state == reality.
    """
    log: list = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        ids = g.get("instance_ids")
        if not isinstance(ids, list):
            continue
        removed: list = []
        kept: list = []
        for iid in ids:
            try:
                iid_int = int(iid)
            except (TypeError, ValueError):
                removed.append(iid)
                continue
            if iid_int in known_iids:
                kept.append(iid_int)
            else:
                removed.append(iid_int)
        if removed:
            g["instance_ids"] = sorted(set(kept))
            log.append({"group_id": g.get("id"), "removed": sorted(set(int(i) for i in removed if isinstance(i, int)))})
    return log


def _orphan_iids_in_group(instance_labels: dict, group: dict) -> list:
    """instance_id з групи, яких немає у npy → невідома лейбла (mismatch
    стану: bake переписав npy, але group.instance_ids посилається на старі
    iid). Frontend підсвітить → юзер видалить.
    """
    orphans: list = []
    for iid in group.get("instance_ids") or []:
        try:
            iid_int = int(iid)
        except (TypeError, ValueError):
            continue
        if iid_int not in instance_labels:
            orphans.append(iid_int)
    orphans.sort()
    return orphans


def _classify_group_membership(
    instance_labels: dict,
    polygon_labels: list,
    group: dict,
    classes: Optional[list] = None,
) -> dict:
    """
    Підраховує label counts у групі + повертає `suggested_class_id` + `valid`
    для current group's class.

    Args:
      instance_labels : {instance_id (int) → label (str)} — з npy + base_label.
      polygon_labels  : list[str] aligned with polygons.json.shapes order.
      group           : dict із полями {class_id або type, instance_ids, polygon_indices}.
      classes         : group_classes list. Якщо None — legacy fallback на
                        hardcoded 3 типи (cell/vesicle_cluster/nucleus_only).

    Returns dict:
      counts                : {label_name → int}     (всі labels)
      n_nucleus, n_vesicle  : legacy aliases (для backwards compat у tests/UI)
      n_other               : sum усіх інших label-ів
      suggested_class_id    : str | None   (для user-defined classes)
      suggested_type        : str          (для hardcoded fallback)
      valid                 : bool
      reason                : Optional[str]
    """
    counts = _count_labels_in_group(instance_labels, polygon_labels, group)
    n_nuc = counts.get(NUCLEUS_LABEL, 0)
    n_ves = counts.get(VESICLE_LABEL, 0)
    n_other = sum(v for k, v in counts.items()
                  if k not in (NUCLEUS_LABEL, VESICLE_LABEL))
    iids_by_label = _iids_by_label_in_group(instance_labels, group)
    orphans = _orphan_iids_in_group(instance_labels, group)

    # ----- User-defined classes path -----
    if classes:
        suggested_cid = _suggest_class_for_counts(counts, classes)
        # resolve current class
        cls_id = group.get("class_id") or _resolve_class_id(group, classes)
        cur = _class_by_id(classes, cls_id) if cls_id else None
        if cur is None:
            valid, reason = True, None
        else:
            valid, reason = _validate_class_against_counts(cur, counts)
        # 2026-05-21 round 4: збагачуємо reason конкретними iid порушниками,
        # щоб юзер міг знайти і видалити «зайвий» nucleus у vesicle_cluster
        # (приклад db_img_0169 g_035: iid 2990 — запечений nucleus-polygon
        # випадково потрапив у lasso vesicle_cluster).
        rogue: list = []
        if not valid and reason and cur is not None:
            rogue = _violating_iids_for_class(cur, counts, iids_by_label)
            if rogue:
                preview = ", ".join(str(i) for i in rogue[:5])
                more = f" (+{len(rogue) - 5})" if len(rogue) > 5 else ""
                reason = f"{reason} — iids: {preview}{more}"
        # Map suggested back to legacy type for old code paths
        suggested_type = None
        if suggested_cid:
            sc = _class_by_id(classes, suggested_cid)
            if sc:
                suggested_type = sc.get("name")
        return {
            "counts": counts,
            "n_nucleus": n_nuc,
            "n_vesicle": n_ves,
            "n_other": n_other,
            "suggested_class_id": suggested_cid,
            "suggested_type": suggested_type or (cur.get("name") if cur else None),
            "valid": valid,
            "reason": reason,
            "iids_by_label": iids_by_label,
            "orphan_iids": orphans,
            "rogue_iids": rogue,    # flat list для frontend підсвітки червоним
        }

    # ----- Legacy hardcoded fallback (tests, no classes loaded) -----
    if n_nuc >= 1 and n_ves >= 1:
        suggested = "cell"
    elif n_ves >= 1 and n_nuc == 0:
        suggested = "vesicle_cluster"
    elif n_nuc >= 1 and n_ves == 0:
        suggested = "nucleus_only"
    else:
        suggested = group.get("type") or "cell"

    gtype = group.get("type")
    valid = True
    reason: Optional[str] = None
    rogue: list = []
    if gtype == "cell":
        if n_nuc < 1:
            valid, reason = False, "cell requires ≥1 nucleus"
        elif n_ves < 1:
            valid, reason = False, "cell requires ≥1 vesicle"
    elif gtype == "vesicle_cluster":
        if n_ves < 1:
            valid, reason = False, "vesicle_cluster requires ≥1 vesicle"
        elif n_nuc > 0:
            valid, reason = False, "vesicle_cluster must have 0 nuclei"
            rogue = iids_by_label.get(NUCLEUS_LABEL, [])
    elif gtype == "nucleus_only":
        if n_nuc < 1:
            valid, reason = False, "nucleus_only requires ≥1 nucleus"
        elif n_ves > 0:
            valid, reason = False, "nucleus_only must have 0 vesicles"
            rogue = iids_by_label.get(VESICLE_LABEL, [])

    return {
        "counts": counts,
        "n_nucleus": n_nuc,
        "n_vesicle": n_ves,
        "n_other": n_other,
        "suggested_class_id": None,
        "suggested_type": suggested,
        "valid": valid,
        "reason": reason,
        "iids_by_label": iids_by_label,
        "orphan_iids": orphans,
        "rogue_iids": list(rogue),
    }


# ---------------------------------------------------------------------------
# Single-membership enforcement
# ---------------------------------------------------------------------------

def _enforce_single_membership(groups: list) -> list:
    """
    Гарантує що кожен instance_id / polygon_index належить лише одній групі.

    Стратегія "last wins": якщо iid фігурує у g_001 і у g_005 — лишається у
    g_005 (останнє згадування за порядком списку). Це збігається з UX: коли
    юзер додає інстанс у нову групу, попередня має його втратити.

    Returns: list[{"kind": "instance"|"polygon", "id": int, "from": gid, "to": gid}]
             — журнал переміщень для toast у frontend.
    """
    moves: list = []
    seen_iid: dict = {}   # iid → group_id (latest)
    seen_pi: dict = {}    # polygon_index → group_id (latest)

    # Pass 1 — пройти по групах за порядком, фіксувати "latest"
    for g in groups:
        gid = g.get("id", "")
        for iid in g.get("instance_ids") or []:
            try:
                iid_int = int(iid)
            except (TypeError, ValueError):
                continue
            if iid_int in seen_iid and seen_iid[iid_int] != gid:
                moves.append({
                    "kind": "instance",
                    "id": iid_int,
                    "from": seen_iid[iid_int],
                    "to": gid,
                })
            seen_iid[iid_int] = gid
        for pi in g.get("polygon_indices") or []:
            try:
                pi_int = int(pi)
            except (TypeError, ValueError):
                continue
            if pi_int in seen_pi and seen_pi[pi_int] != gid:
                moves.append({
                    "kind": "polygon",
                    "id": pi_int,
                    "from": seen_pi[pi_int],
                    "to": gid,
                })
            seen_pi[pi_int] = gid

    # Pass 2 — переписати кожну групу, лишити тільки ті iid/pi, чий "latest" — це вона
    for g in groups:
        gid = g.get("id", "")
        g["instance_ids"] = sorted({
            int(i) for i in (g.get("instance_ids") or [])
            if seen_iid.get(int(i)) == gid
        })
        g["polygon_indices"] = sorted({
            int(p) for p in (g.get("polygon_indices") or [])
            if seen_pi.get(int(p)) == gid
        })

    return moves


# ---------------------------------------------------------------------------
# ID + color allocation
# ---------------------------------------------------------------------------

def _next_group_id(groups: list) -> str:
    """Перший вільний g_NNN. Skipping gaps: g_001, g_003 → next = g_002."""
    used = set()
    for g in groups:
        gid = g.get("id", "")
        if isinstance(gid, str) and gid.startswith("g_"):
            try:
                used.add(int(gid[2:]))
            except ValueError:
                continue
    n = 1
    while n in used:
        n += 1
    return f"g_{n:03d}"


def _next_color_hue(groups: list) -> int:
    """
    Наступний free hue з PALETTE_HUES. Якщо всі 8 використані — повертається
    PALETTE_HUES[len(groups) % 8], frontend застосовує jitter поверх.
    """
    used = set()
    for g in groups:
        h = g.get("color_hue")
        if isinstance(h, (int, float)):
            used.add(int(h))
    for h in PALETTE_HUES:
        if h not in used:
            return h
    return PALETTE_HUES[len(groups) % len(PALETTE_HUES)]


# ---------------------------------------------------------------------------
# Instance label lookup (npy + polygons fallback)
# ---------------------------------------------------------------------------

def _instance_label_lookup(
    labels_arr,
    polygons_payload: Optional[dict] = None,
    base_label: Optional[str] = None,
    per_instance_overrides: Optional[dict] = None,
) -> dict:
    """
    Build mapping {instance_id (int) → label (str)} для baked instances у
    selected/<model>/npy/<stem>.npy.

    Стратегія (2026-05-21 fix):
    1. Кожен непорожній instance ID з npy → отримує `base_label` за замовч.
       (з polygons.base_label, або "nucleus" як остання fallback).
    2. Якщо передано `per_instance_overrides` (наприклад зчитані з YOLO txt
       multiclass-export), то лейбли з нього **перебивають** `base_label` —
       це дозволяє правильно класифікувати запечені маски, де частина
       інстансів — везикули, частина — ядра. Без цього групи cell завжди
       не валідні бо всі baked інстанси отримували одну лейблу.
    3. polygons_payload.shapes[].label — використовується для polygon-indices
       (не instance_ids), це передається окремо як polygon_labels.

    base_label — explicit override (з cleanup.json або polygons.base_label).
    per_instance_overrides — {iid: label_name} з yolo multiclass (опц.).
    """
    result: dict = {}
    if labels_arr is None:
        return result

    if base_label is None and isinstance(polygons_payload, dict):
        base_label = polygons_payload.get("base_label")
    if not base_label:
        base_label = NUCLEUS_LABEL

    ids = [int(i) for i in np.unique(labels_arr) if int(i) > 0]
    for iid in ids:
        result[iid] = base_label

    if isinstance(per_instance_overrides, dict):
        for iid, lbl in per_instance_overrides.items():
            try:
                iid_int = int(iid)
            except (TypeError, ValueError):
                continue
            if isinstance(lbl, str) and lbl:
                result[iid_int] = lbl
    return result


def _instance_labels_from_polygons(
    labels_arr,
    polygons_payload: Optional[dict],
) -> dict:
    """
    Дає мапінг {instance_id → label} на основі **актуальних** шейпів у
    `polygons/<stem>.json`. Для кожного shape з непорожньою `label`
    залиаємо лейблу всім instance, що лежать під цим полігоном (≥1 px).

    Призначення (2026-05-21 hotfix після першого fix):
      Якщо bake застарілий (YOLO ще не оновлений після того як юзер
      додав чи перейменував шейп), per-instance class_id у YOLO стають
      stale. Polygon-шейпи — це найсвіжіший «правдивий» розмітковий
      артефакт, тому їхні лейбли повинні перебивати YOLO у GET
      classification. Без цього vesicle_cluster-група з інстансами, що
      реально під vesicle-шейпами, помилково помічається як «всі
      nucleus» (бо такий стан у stale YOLO).

    Returns: {iid (int) → label_name (str)}. Якщо OpenCV недоступний
    або payload без shapes — `{}`.
    """
    if labels_arr is None or not isinstance(polygons_payload, dict):
        return {}
    shapes = polygons_payload.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        return {}
    try:
        import cv2 as _cv2  # noqa
    except Exception:
        return {}
    try:
        H = int(labels_arr.shape[0])
        W = int(labels_arr.shape[1])
    except Exception:
        return {}
    result: dict = {}
    # bbox-ROI оптимізація: fillPoly + лейбл-зчитування лише на bbox шейпа,
    # не на повний 2572×1956. Для 78 малих шейпів — ~500× менше памʼяті
    # і близько того ж по часу. Без цього GET/POST на 40+ груп × повний
    # canvas роздуває час реакції до секунд і блокує UI close().
    for sh in shapes:
        if not isinstance(sh, dict):
            continue
        label = sh.get("label")
        if not isinstance(label, str) or not label:
            continue
        pts = sh.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        try:
            arr_pts = np.array(
                [[int(round(float(x))), int(round(float(y)))] for x, y in pts],
                dtype=np.int32,
            )
        except Exception:
            continue
        x_min = int(max(0, arr_pts[:, 0].min()))
        x_max = int(min(W - 1, arr_pts[:, 0].max()))
        y_min = int(max(0, arr_pts[:, 1].min()))
        y_max = int(min(H - 1, arr_pts[:, 1].max()))
        if x_max < x_min or y_max < y_min:
            continue
        roi_h = y_max - y_min + 1
        roi_w = x_max - x_min + 1
        local = np.zeros((roi_h, roi_w), dtype=np.uint8)
        local_pts = arr_pts.copy()
        local_pts[:, 0] -= x_min
        local_pts[:, 1] -= y_min
        try:
            _cv2.fillPoly(local, [local_pts], 1)
        except Exception:
            continue
        sub_labels = labels_arr[y_min:y_max + 1, x_min:x_max + 1]
        under = sub_labels[local > 0]
        under = under[under > 0]
        if under.size == 0:
            continue
        for iid in np.unique(under).tolist():
            result[int(iid)] = label
    return result


def _instance_labels_from_yolo(
    labels_arr,
    yolo_path: Path,
    label_classes: list,
) -> dict:
    """
    Читає `selected/<model>/yolo/<stem>.txt` і будує мапінг
    {instance_id → label_name} для запечених інстансів.

    YOLO multiclass-формат (див. baking.py:_write_yolo_multiclass): рядок i
    відповідає `sorted(np.unique(labels))[i]` (без фону 0). Перший токен
    кожного рядка — class_id у порядку labels.json (як його повертає
    `_load_label_classes` → enumerate).

    Якщо файл відсутній, або кількість рядків не співпадає з кількістю
    instance-ів, або labels.json порожній — повертаємо {} (caller fallback
    до `base_label`).

    Returns: {instance_id (int) → label_name (str)}
    """
    if labels_arr is None or not label_classes:
        return {}
    try:
        if not yolo_path.exists():
            return {}
    except Exception:
        return {}
    try:
        inst_ids = [int(i) for i in np.unique(labels_arr) if int(i) > 0]
    except Exception:
        return {}
    if not inst_ids:
        return {}
    try:
        lines = [
            ln for ln in yolo_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    except Exception:
        return {}
    if len(lines) != len(inst_ids):
        return {}
    cid_to_name = {}
    for i, c in enumerate(label_classes):
        name = c.get("name") if isinstance(c, dict) else None
        if isinstance(name, str) and name:
            cid_to_name[i] = name
    result: dict = {}
    for iid, line in zip(inst_ids, lines):
        try:
            cid = int(line.split()[0])
        except (ValueError, IndexError):
            continue
        name = cid_to_name.get(cid)
        if name:
            result[iid] = name
    return result


def _polygon_labels_from_payload(polygons_payload: Optional[dict]) -> list:
    """Список labels у тому ж порядку як shapes у polygons.json."""
    if not isinstance(polygons_payload, dict):
        return []
    shapes = polygons_payload.get("shapes")
    if not isinstance(shapes, list):
        return []
    out: list = []
    for sh in shapes:
        if isinstance(sh, dict):
            lbl = sh.get("label")
            out.append(str(lbl) if isinstance(lbl, str) else "")
        else:
            out.append("")
    return out


# ---------------------------------------------------------------------------
# polygons.json mirror sync (gibrid C)
# ---------------------------------------------------------------------------

def _sync_polygons_group_id_mirror(polygons_payload: dict, groups: list) -> int:
    """
    Записує group_id у polygons.json.shapes[i] на основі groups[].polygon_indices.
    Це derived mirror для LabelMe-сумісності, не source of truth.

    Returns: кількість shapes у яких group_id було змінено.
    """
    if not isinstance(polygons_payload, dict):
        return 0
    shapes = polygons_payload.get("shapes")
    if not isinstance(shapes, list):
        return 0

    # Build {polygon_index → group_id}
    pi_to_gid: dict = {}
    for g in groups:
        gid = g.get("id")
        for pi in g.get("polygon_indices") or []:
            try:
                pi_to_gid[int(pi)] = gid
            except (TypeError, ValueError):
                continue

    changed = 0
    for i, sh in enumerate(shapes):
        if not isinstance(sh, dict):
            continue
        new_gid = pi_to_gid.get(i)
        if sh.get("group_id") != new_gid:
            sh["group_id"] = new_gid
            changed += 1
    return changed
