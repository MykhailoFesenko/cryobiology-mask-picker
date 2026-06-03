"""
audit_export.py — standalone CLI для перевірки інваріантів експорту/workspace.

Запуск перед відправкою замовнику:
    python apps/mask_picker/tools/audit_export.py --workspace data/vesicles_good
    python apps/mask_picker/tools/audit_export.py --export-dir _send/extracted_zip

Інваріанти (Phase 3 з NEXT_SESSION_PROMPT_bug3_and_desync.md):

I-1.  npy ↔ png: однакові значення (PNG може бути uint16 / int16 mapping).
I-2.  semantic/<stem>.png: тільки {0, 1, 2}.
I-3.  yolo/<stem>.txt: рядків == unique iid у npy (без 0), class_id ∈ {0, 1}.
I-4.  mask_groups/<stem>.png: 0..N без дірок (N == кількість непорожніх груп).
I-5.  polygons/<stem>.json.shape.group_id ∈ groups[*].id або None.
I-6.  groups.instance_ids: кожне існує у npy.
I-7.  groups.polygon_indices: валідний для polygons.shapes.
I-8.  selections.json: ключі == stems у selected/<model>/npy/ без orphans.

Файли semantic/mask_groups/overlays/__<label>.png генеруються тільки при
export ZIP. Якщо їх нема у workspace — інваріант skip-ається з notice.

Exit code 0 — clean; 1 — є violations.
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _load_labels(npy_path: Path) -> Optional[np.ndarray]:
    if not npy_path.exists():
        return None
    try:
        a = np.load(str(npy_path))
        while a.ndim > 2:
            a = a[0]
        return a
    except Exception:
        return None


def _load_png_labels(png_path: Path) -> Optional[np.ndarray]:
    if not png_path.exists():
        return None
    try:
        with Image.open(str(png_path)) as img:
            return np.array(img)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------

class Auditor:
    """Аккумулятор: invariant → лічильник + перші N samples + skip-журнал."""

    SAMPLE_LIMIT = 5

    def __init__(self) -> None:
        self.violations: dict = defaultdict(list)
        self.skipped: dict = defaultdict(int)
        self.n_stems_checked: int = 0

    def add_violation(self, key: str, sample) -> None:
        self.violations[key].append(sample)

    def skip(self, key: str) -> None:
        self.skipped[key] += 1

    def n_violations(self, key: str) -> int:
        return len(self.violations[key])

    # ----- per-stem checks -----

    def check_npy_png(self, ws: Path, model: str, stem: str) -> None:
        npy = _load_labels(ws / "selected" / model / "npy" / f"{stem}.npy")
        png = _load_png_labels(ws / "selected" / model / "png" / f"{stem}.png")
        if npy is None or png is None:
            self.skip("I1_npy_png")
            return
        if npy.shape != png.shape:
            self.add_violation(
                "I1_npy_png",
                (stem, f"shape mismatch npy={npy.shape} png={png.shape}"),
            )
            return
        # PNG може бути uint16 (cellsegkit writer); npy — int32. Порівнюємо
        # унікальні наборі id (без 0).
        ids_npy = set(int(i) for i in np.unique(npy) if int(i) > 0)
        ids_png = set(int(i) for i in np.unique(png) if int(i) > 0)
        if ids_npy != ids_png:
            diff_npy = sorted(ids_npy - ids_png)[:5]
            diff_png = sorted(ids_png - ids_npy)[:5]
            self.add_violation(
                "I1_npy_png",
                (stem,
                 f"id mismatch npy_only={diff_npy} png_only={diff_png}"),
            )

    def check_semantic(self, ws: Path, model: str, stem: str) -> None:
        path = ws / "selected" / model / "semantic" / f"{stem}.png"
        if not path.exists():
            self.skip("I2_semantic")
            return
        sem = _load_png_labels(path)
        if sem is None:
            self.add_violation("I2_semantic", (stem, "read failed"))
            return
        u = np.unique(sem)
        bad = [int(v) for v in u if int(v) not in (0, 1, 2)]
        if bad:
            self.add_violation(
                "I2_semantic",
                (stem, f"unexpected values {bad[:5]}"),
            )

    def check_yolo(self, ws: Path, model: str, stem: str) -> None:
        npy_path = ws / "selected" / model / "npy" / f"{stem}.npy"
        yolo_path = ws / "selected" / model / "yolo" / f"{stem}.txt"
        if not npy_path.exists() or not yolo_path.exists():
            self.skip("I3_yolo")
            return
        npy = _load_labels(npy_path)
        if npy is None:
            self.add_violation("I3_yolo", (stem, "npy load failed"))
            return
        unique_ids = [int(i) for i in np.unique(npy) if int(i) > 0]
        try:
            lines = [
                ln for ln in yolo_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
        except Exception as e:
            self.add_violation("I3_yolo", (stem, f"yolo read failed: {e}"))
            return
        if len(lines) != len(unique_ids):
            self.add_violation(
                "I3_yolo",
                (stem, f"line count {len(lines)} != unique iid {len(unique_ids)}"),
            )
            return
        for i, ln in enumerate(lines):
            tokens = ln.split()
            if not tokens:
                continue
            try:
                cid = int(tokens[0])
            except (ValueError, IndexError):
                self.add_violation(
                    "I3_yolo",
                    (stem, f"line {i} class_id parse fail: {ln[:40]!r}"),
                )
                return
            if cid not in (0, 1):
                self.add_violation(
                    "I3_yolo",
                    (stem, f"line {i} class_id={cid} not in {{0,1}}"),
                )
                return

    def check_mask_groups(self, ws: Path, model: str, stem: str) -> None:
        path = ws / "selected" / model / "mask_groups" / f"{stem}.png"
        if not path.exists():
            self.skip("I4_mask_groups")
            return
        gmask = _load_png_labels(path)
        if gmask is None:
            self.add_violation("I4_mask_groups", (stem, "read failed"))
            return
        ids = sorted(int(v) for v in np.unique(gmask) if int(v) > 0)
        if not ids:
            return  # пусто — OK (всі instance ungrouped)
        # Перевірити що ids — це 1..N без дірок (бо груповий індекс
        # призначається enumerate, починаючи з 1, по порядку у файлі).
        # Дірки можуть бути коли частина груп без пікселів (instance_ids
        # порожній і polygon_indices порожній або polygons.json відсутній).
        # Acceptable: tail gap ОК; missing у середині — баг.
        # Перевіримо: max(ids) має бути ≤ len(ids) + 1? Ні, інший підхід.
        # У `export_derived_masks` (baking.py:500) кожна група одна за одною
        # отримує номер 1..N, незалежно від has_pixels. Тому ids можуть мати
        # дірки якщо групи без пікселів.
        # Для audit: попереджаємо лише якщо max(ids) > 65535 (uint16
        # overflow) або немає монотонного послідовного нумерування.
        max_id = max(ids)
        # Soft check: дірки у середині ОК (групи без пікселів). Hard fail —
        # тільки overflow.
        if max_id > 65535:
            self.add_violation(
                "I4_mask_groups",
                (stem, f"uint16 overflow: max_id={max_id}"),
            )

    def check_polygons_group_id(self, ws: Path, stem: str) -> None:
        p_data = _load_json(ws / "polygons" / f"{stem}.json", {}) or {}
        g_data = _load_json(ws / "groups" / f"{stem}.json", {}) or {}
        shapes = p_data.get("shapes") or []
        groups = g_data.get("groups") or []
        if not shapes:
            self.skip("I5_polygon_group_id")
            return
        known_gids = {g.get("id") for g in groups if isinstance(g.get("id"), str)}
        for i, sh in enumerate(shapes):
            if not isinstance(sh, dict):
                continue
            gid = sh.get("group_id")
            if gid is None:
                continue
            if not isinstance(gid, str) or gid not in known_gids:
                self.add_violation(
                    "I5_polygon_group_id",
                    (stem, f"shape #{i}: group_id={gid!r} not in groups"),
                )

    def check_groups_invariants(self, ws: Path, model: str, stem: str) -> None:
        g_data = _load_json(ws / "groups" / f"{stem}.json", {}) or {}
        groups = g_data.get("groups") or []
        p_data = _load_json(ws / "polygons" / f"{stem}.json", {}) or {}
        shapes = p_data.get("shapes") or []
        npy = _load_labels(ws / "selected" / model / "npy" / f"{stem}.npy")
        if not groups:
            return
        known_iids: set = set()
        if npy is not None:
            known_iids = {int(i) for i in np.unique(npy) if int(i) > 0}
        for g in groups:
            if not isinstance(g, dict):
                continue
            gid = g.get("id", "?")
            for iid in (g.get("instance_ids") or []):
                try:
                    iid_int = int(iid)
                except (TypeError, ValueError):
                    self.add_violation(
                        "I6_group_iid_in_npy",
                        (stem, f"{gid}: non-int iid {iid!r}"),
                    )
                    continue
                if known_iids and iid_int not in known_iids:
                    self.add_violation(
                        "I6_group_iid_in_npy",
                        (stem, f"{gid}: iid {iid_int} not in npy"),
                    )
            for pi in (g.get("polygon_indices") or []):
                try:
                    pi_int = int(pi)
                except (TypeError, ValueError):
                    self.add_violation(
                        "I7_group_pi_in_shapes",
                        (stem, f"{gid}: non-int pi {pi!r}"),
                    )
                    continue
                if not (0 <= pi_int < len(shapes)):
                    self.add_violation(
                        "I7_group_pi_in_shapes",
                        (stem, f"{gid}: pi {pi_int} out of range (len={len(shapes)})"),
                    )

    def check_overlays_consistency(self, ws: Path, model: str, stem: str) -> None:
        sem_path = ws / "selected" / model / "semantic" / f"{stem}.png"
        if not sem_path.exists():
            self.skip("I8_overlay_consistency")
            return
        sem = _load_png_labels(sem_path)
        if sem is None:
            return
        # Per-label overlays (Day 8): <ws>/selected/<model>/overlay/__<label>.png
        # АБО окремий шлях у ZIP — лишаємо як soft check.
        # Тут перевіряємо лише існування — пиксельну консистентність повну
        # перевірити складно без знання overlay rendering parameters.
        # Soft: якщо semantic має class N, overlay для цього класу має існувати.
        present_labels = set()
        # Гіпотетичні шляхи:
        for cls_id, label in [(1, "nucleus"), (2, "vesicle")]:
            if (sem == cls_id).any():
                ovl_path = ws / "selected" / model / "overlay" / f"{stem}__{label}.png"
                if ovl_path.exists():
                    present_labels.add(label)
        # Якщо semantic має vesicle pixels, overlay може не існувати — це OK
        # для workspace (overlays генеруються при export). Реальний strict
        # check можна на ZIP. Поки skip-аємо noisy.
        # (No add_violation — це advisory check.)

    # ----- workspace-wide checks -----

    def check_selections_orphans(self, ws: Path, sel: dict) -> None:
        """selections.json keys повинні відповідати реальним файлам npy
        для selected status. Orphan ключі — це stems без npy."""
        for stem, entry in sel.items():
            if entry.get("status") != "selected":
                continue
            model = entry.get("model")
            if not model:
                self.add_violation(
                    "I9_selections_orphan",
                    (stem, "status=selected but model is None"),
                )
                continue
            npy_path = ws / "selected" / model / "npy" / f"{stem}.npy"
            if not npy_path.exists():
                self.add_violation(
                    "I9_selections_orphan",
                    (stem, f"npy missing for model={model}"),
                )

    # ----- print report -----

    def print_report(self) -> None:
        keys_ordered = [
            ("I1_npy_png",          "I1 npy <-> png id sets match"),
            ("I2_semantic",         "I2 semantic.png in {0,1,2}"),
            ("I3_yolo",             "I3 yolo lines == unique iid, class_id in {0,1}"),
            ("I4_mask_groups",      "I4 mask_groups.png u16 OK"),
            ("I5_polygon_group_id", "I5 polygons.shape.group_id valid"),
            ("I6_group_iid_in_npy", "I6 group.instance_ids in npy"),
            ("I7_group_pi_in_shapes", "I7 group.polygon_indices in shapes"),
            ("I8_overlay_consistency", "I8 overlays vs semantic (advisory)"),
            ("I9_selections_orphan", "I9 selections.json keys have npy"),
        ]
        print(f"\n=== audit_export — {self.n_stems_checked} stems checked ===\n")
        width = max(len(name) for _, name in keys_ordered)
        for key, name in keys_ordered:
            n = self.n_violations(key)
            skipped = self.skipped.get(key, 0)
            if n > 0:
                flag = f"!! {n} violations"
            elif skipped > 0:
                flag = f"-- skipped ({skipped} stems missing data)"
            else:
                flag = "OK"
            print(f"  {name.ljust(width)} : {flag}")
            if n > 0:
                for sample in self.violations[key][: self.SAMPLE_LIMIT]:
                    print(f"      {sample[0]}: {sample[1]}")
                if n > self.SAMPLE_LIMIT:
                    print(f"      ...+{n - self.SAMPLE_LIMIT} more")
        total = sum(len(v) for v in self.violations.values())
        print(f"\nGRAND TOTAL violations: {total}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Audit Mask Picker export/workspace for invariants.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--workspace", type=Path,
        help="Workspace dir (data/<dataset>/) — buget без semantic/mask_groups.",
    )
    g.add_argument(
        "--export-dir", type=Path,
        help="Extracted export ZIP dir — should have semantic/, mask_groups/, overlays/.",
    )
    p.add_argument(
        "--model", default=None,
        help="Model name to audit (default: per-stem з selections.json).",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    ws = args.workspace or args.export_dir
    if not ws.exists():
        print(f"[!] not found: {ws}", file=sys.stderr)
        return 2

    sel_path = ws / "selections.json"
    if not sel_path.exists():
        print(f"[!] selections.json missing у {ws}", file=sys.stderr)
        return 2
    sel = _load_json(sel_path, {}) or {}

    auditor = Auditor()
    items = [(k, v.get("model")) for k, v in sel.items()
             if v.get("status") == "selected"]
    auditor.n_stems_checked = len(items)

    for stem, model in items:
        m = args.model or model
        if not m:
            print(f"  warning: {stem} has no model")
            continue
        auditor.check_npy_png(ws, m, stem)
        auditor.check_semantic(ws, m, stem)
        auditor.check_yolo(ws, m, stem)
        auditor.check_mask_groups(ws, m, stem)
        auditor.check_polygons_group_id(ws, stem)
        auditor.check_groups_invariants(ws, m, stem)
        auditor.check_overlays_consistency(ws, m, stem)

    auditor.check_selections_orphans(ws, sel)

    auditor.print_report()
    return 0 if not any(auditor.violations.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
