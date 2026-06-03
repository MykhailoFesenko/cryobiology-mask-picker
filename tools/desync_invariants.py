"""
desync_invariants.py — Phase 2 smoke validator проти data/vesicles_good/.

Перевіряє 5 інваріантів з NEXT_SESSION_PROMPT_bug3_and_desync.md:

  I1. sum(len(g.instance_ids) for g in groups) ==
      len(unique baked instances assigned to groups)
      (тобто iid не дублюється всередині однієї групи; instance_ids — set-like)

  I2. all(label == "nucleus" → instance has sem == 1)
      (label↔semantic alignment між polygon-shape labels та YOLO multiclass)

  I3. all(polygon_indices unique within group)
      (no double-listing у polygon_indices групи)

  I4. all(instance_ids unique across the whole groups list)
      (одне iid не може бути у двох групах — enforce_single_membership)

  I5. cleanup.rejected ∩ groups[*].instance_ids == ∅
      (rejected instance не повинен числитися у жодній групі)

  + бонус: polygon_indices, що вказують на out-of-range або відсутні shapes
  + бонус: polygon-shapes з group_id у polygons.json, що не існують у groups.json

Запуск з кореня:
    python _tmp/desync_invariants.py
"""
from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
# Workspace path: argv[1] override (для sandbox-тестування), інакше default.
import sys as _sys
WS = Path(_sys.argv[1]) if len(_sys.argv) > 1 else ROOT / "data" / "vesicles_good"
GROUPS_DIR = WS / "groups"
POLYS_DIR = WS / "polygons"
SELECTED_DIR = WS / "selected"


def _load_selections() -> dict:
    return json.loads((WS / "selections.json").read_text(encoding="utf-8-sig"))


def _load_groups(stem: str) -> dict:
    p = GROUPS_DIR / f"{stem}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _load_polygons(stem: str) -> dict:
    p = POLYS_DIR / f"{stem}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _load_cleanup_rejected(stem: str, model: str) -> set:
    """Збирає rejected iids: спершу per-model cleanup.json, потім fallback на selections.json."""
    out: set = set()
    p = SELECTED_DIR / model / "cleanup.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            entry = (data or {}).get(stem) or {}
            for iid in (entry.get("rejected") or []):
                try:
                    out.add(int(iid))
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
    # fallback: state.cleanup.rejected_instances у selections.json
    if not out:
        try:
            sel = _load_selections().get(stem) or {}
            cu = sel.get("cleanup") or {}
            if cu.get("model") == model:
                for iid in (cu.get("rejected_instances") or []):
                    try:
                        out.add(int(iid))
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
    return out


def _load_inst_sem(stem: str, model: str):
    """Повертає (inst, sem) арреї, або None якщо данні відсутні."""
    npy_path = SELECTED_DIR / model / "npy" / f"{stem}.npy"
    yolo_path = SELECTED_DIR / model / "yolo" / f"{stem}.txt"
    if not npy_path.exists() or not yolo_path.exists():
        return None
    labels = np.load(str(npy_path))
    while labels.ndim > 2:
        labels = labels[0]
    inst_ids = [int(i) for i in np.unique(labels) if int(i) > 0]
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
    sem = lut[labels]
    return labels, sem


def check_stem(stem: str, model: str) -> dict:
    """Повертає dict з лічильниками порушень + детальний список (першi 5)."""
    rep = {
        "I1_dup_iid_in_group": [],          # (gid, [duplicate iids])
        "I2_label_mismatch": [],            # (pi, label_in_polygon, expected_sem_class)
        "I3_dup_pi_in_group": [],           # (gid, [duplicate pi])
        "I4_iid_in_multiple_groups": [],    # (iid, [gids])
        "I5_rejected_in_groups": [],        # (iid, gid)
        "B1_pi_out_of_range": [],           # (gid, pi)
        "B2_polygon_group_id_unknown": [],  # (shape_idx, group_id_in_polygons)
        "B3_orphan_iid_in_groups": [],      # (gid, iid)  — iid не існує у npy
    }
    g_data = _load_groups(stem)
    groups = g_data.get("groups") or []
    p_data = _load_polygons(stem)
    shapes = p_data.get("shapes") or []

    pair = _load_inst_sem(stem, model)
    inst, sem = (pair if pair is not None else (None, None))
    known_iids: set = set()
    if inst is not None:
        known_iids = {int(i) for i in np.unique(inst) if int(i) > 0}

    rejected = _load_cleanup_rejected(stem, model)
    iid_to_groups: dict = defaultdict(list)
    known_gids = {g.get("id") for g in groups if isinstance(g.get("id"), str)}

    for g in groups:
        gid = g.get("id")
        iids = list(g.get("instance_ids") or [])
        pidxs = list(g.get("polygon_indices") or [])

        # I1: дублі iid усередині однієї групи
        seen = {}
        for iid in iids:
            try:
                k = int(iid)
            except (TypeError, ValueError):
                continue
            seen[k] = seen.get(k, 0) + 1
        dup_iid = [k for k, v in seen.items() if v > 1]
        if dup_iid:
            rep["I1_dup_iid_in_group"].append((gid, dup_iid))

        # I3: дублі polygon_index усередині однієї групи
        seen_p = {}
        for pi in pidxs:
            try:
                k = int(pi)
            except (TypeError, ValueError):
                continue
            seen_p[k] = seen_p.get(k, 0) + 1
        dup_pi = [k for k, v in seen_p.items() if v > 1]
        if dup_pi:
            rep["I3_dup_pi_in_group"].append((gid, dup_pi))

        # I4: фіксуємо кожен iid → group_id для cross-group check
        for iid in iids:
            try:
                iid_to_groups[int(iid)].append(gid)
            except (TypeError, ValueError):
                pass

        # I5: rejected у instance_ids
        for iid in iids:
            try:
                if int(iid) in rejected:
                    rep["I5_rejected_in_groups"].append((int(iid), gid))
            except (TypeError, ValueError):
                pass

        # B1: polygon_indices out-of-range або не int
        for pi in pidxs:
            try:
                pi_int = int(pi)
            except (TypeError, ValueError):
                rep["B1_pi_out_of_range"].append((gid, pi))
                continue
            if not (0 <= pi_int < len(shapes)):
                rep["B1_pi_out_of_range"].append((gid, pi_int))

        # B3: orphan iid (не у npy)
        if known_iids:
            for iid in iids:
                try:
                    iid_int = int(iid)
                except (TypeError, ValueError):
                    continue
                if iid_int not in known_iids:
                    rep["B3_orphan_iid_in_groups"].append((gid, iid_int))

    # I4 cross-group:
    for iid, gids in iid_to_groups.items():
        unique_gids = list(dict.fromkeys(gids))
        if len(unique_gids) > 1:
            rep["I4_iid_in_multiple_groups"].append((iid, unique_gids))

    # I2: label_in_polygon vs semantic class of baked instance under polygon
    if inst is not None and sem is not None and shapes:
        import cv2
        H, W = inst.shape
        # nucleus → expected sem == 1; vesicle → expected sem == 2
        expected = {"nucleus": 1, "vesicle": 2}
        for pi, sh in enumerate(shapes):
            lbl = sh.get("label") if isinstance(sh, dict) else None
            if not isinstance(lbl, str) or lbl not in expected:
                continue
            pts = sh.get("points") or []
            if len(pts) < 3:
                continue
            try:
                arr_pts = np.array(
                    [[int(round(float(x))), int(round(float(y)))] for x, y in pts],
                    dtype=np.int32,
                )
            except Exception:
                continue
            m = np.zeros((H, W), dtype=np.uint8)
            cv2.fillPoly(m, [arr_pts], 1)
            sel = (m > 0) & (inst > 0)
            if not sel.any():
                continue
            # під polygon-шейпом, скільки пікселів кожного семантичного класу
            sub_sem = sem[sel]
            unique_sem, counts_sem = np.unique(sub_sem, return_counts=True)
            # домінуючий sem-клас
            dom_idx = int(counts_sem.argmax())
            dom_sem = int(unique_sem[dom_idx])
            if dom_sem != 0 and dom_sem != expected[lbl]:
                rep["I2_label_mismatch"].append((pi, lbl, dom_sem))

    # B2: polygons.json.shapes[i].group_id посилається на групу, що не існує
    for i, sh in enumerate(shapes):
        if not isinstance(sh, dict):
            continue
        gid = sh.get("group_id")
        if isinstance(gid, str) and gid and gid not in known_gids:
            rep["B2_polygon_group_id_unknown"].append((i, gid))

    return rep


def main() -> int:
    sel = _load_selections()
    items = [(k, v.get("model")) for k, v in sel.items()
             if v.get("status") == "selected"]

    aggregate = defaultdict(int)
    samples = defaultdict(list)

    for stem, model in items:
        rep = check_stem(stem, model)
        for k, lst in rep.items():
            aggregate[k] += len(lst)
            if len(samples[k]) < 5:
                for entry in lst:
                    if len(samples[k]) >= 5:
                        break
                    samples[k].append((stem, entry))

    print(f"=== desync_invariants — {len(items)} stems checked ===\n")
    keys_ordered = [
        ("I1_dup_iid_in_group",        "I1 — iid duplicated within group"),
        ("I3_dup_pi_in_group",         "I3 — polygon_index duplicated within group"),
        ("I4_iid_in_multiple_groups",  "I4 — iid in multiple groups (broken single-membership)"),
        ("I5_rejected_in_groups",      "I5 — rejected iid still in group.instance_ids"),
        ("I2_label_mismatch",          "I2 — polygon label != semantic class under it"),
        ("B1_pi_out_of_range",         "B1 — polygon_index out of range (shape doesn't exist)"),
        ("B2_polygon_group_id_unknown","B2 — polygon.group_id refers to unknown group"),
        ("B3_orphan_iid_in_groups",    "B3 — orphan iid (not in current npy) in group"),
    ]
    width = max(len(label) for _, label in keys_ordered)
    for key, label in keys_ordered:
        n = aggregate[key]
        flag = "OK" if n == 0 else f"!! {n} violations"
        print(f"  {label.ljust(width)} : {flag}")
        if n > 0 and samples[key]:
            print(f"    examples (up to 5):")
            for stem, entry in samples[key]:
                print(f"      {stem}: {entry}")
    grand_violations = sum(aggregate.values())
    print(f"\nGRAND TOTAL violations: {grand_violations}")
    return 0 if grand_violations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
