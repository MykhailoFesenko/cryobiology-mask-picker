#!/usr/bin/env python3
"""
check_roundtrip.py — перевіряє втрату якості при round-trip:
  instance mask → polygons (approxPolyDP) → bake back → compare

Запуск:
    python check_roundtrip.py                         # усі npy з cpsam_finetuned
    python check_roundtrip.py --model cyto2           # інша модель
    python check_roundtrip.py --epsilon 1.5           # змінити спрощення
    python check_roundtrip.py --save-diff             # зберегти diff-зображення
"""
import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2
import numpy as np

BASE = Path(__file__).parent
OUTPUT_DIR = BASE / "nuclei" / "output"


def mask_to_polygons(labels: np.ndarray, epsilon: float = 1.5) -> list[list]:
    """Повертає список polygon-points для кожного інстансу."""
    polys = []
    for iid in np.unique(labels):
        if iid == 0:
            continue
        mask = (labels == iid).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        if len(approx) >= 3:
            polys.append(approx.reshape(-1, 2).tolist())
    return polys


def bake_polygons(polys: list[list], H: int, W: int) -> np.ndarray:
    """Запікає полігони у нову маску."""
    out = np.zeros((H, W), dtype=np.int32)
    for i, pts in enumerate(polys, start=1):
        arr = np.array([[int(round(x)), int(round(y))] for x, y in pts], dtype=np.int32)
        cv2.fillPoly(out, [arr], i)
    return out


def instance_iou(orig: np.ndarray, baked: np.ndarray) -> list[float]:
    """Per-instance IoU між оригіналом і запеченою маскою."""
    ious = []
    for iid in np.unique(orig):
        if iid == 0:
            continue
        a = orig == iid
        # В baked інстанс може мати інший ID — знаходимо by majority overlap
        candidate_ids = np.unique(baked[a])
        candidate_ids = candidate_ids[candidate_ids != 0]
        if len(candidate_ids) == 0:
            ious.append(0.0)
            continue
        best_iou = 0.0
        for bid in candidate_ids:
            b = baked == bid
            inter = np.sum(a & b)
            union = np.sum(a | b)
            best_iou = max(best_iou, inter / union if union else 0.0)
        ious.append(best_iou)
    return ious


def process_npy(npy_path: Path, epsilon: float, save_diff: bool) -> dict:
    labels = np.load(str(npy_path))
    while labels.ndim > 2:
        labels = labels[0]
    labels = labels.astype(np.int32)
    H, W = labels.shape

    polys = mask_to_polygons(labels, epsilon=epsilon)
    baked = bake_polygons(polys, H, W)

    n_orig  = len(np.unique(labels)) - 1   # мінус фон
    n_baked = len(polys)
    ious    = instance_iou(labels, baked)

    # Pixel-level stats
    orig_fg   = labels > 0
    baked_fg  = baked > 0
    px_added  = int(np.sum(baked_fg & ~orig_fg))
    px_removed= int(np.sum(orig_fg & ~baked_fg))
    px_total  = int(np.sum(orig_fg))

    if save_diff:
        diff = np.zeros((H, W, 3), dtype=np.uint8)
        diff[orig_fg & baked_fg]  = [0, 180, 0]    # зелений — збіг
        diff[orig_fg & ~baked_fg] = [255, 80, 0]   # оранжевий — втрачено
        diff[~orig_fg & baked_fg] = [0, 120, 255]  # синій — додано
        diff_path = npy_path.with_suffix(".diff.png")
        cv2.imwrite(str(diff_path), cv2.cvtColor(diff, cv2.COLOR_RGB2BGR))

    return {
        "stem":        npy_path.stem,
        "instances":   n_orig,
        "mean_iou":    float(np.mean(ious)) if ious else 0.0,
        "min_iou":     float(np.min(ious))  if ious else 0.0,
        "px_removed":  px_removed,
        "px_added":    px_added,
        "px_total":    px_total,
        "pct_removed": 100 * px_removed / px_total if px_total else 0.0,
        "pct_added":   100 * px_added   / px_total if px_total else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model",   default="cpsam_finetuned")
    parser.add_argument("--epsilon", type=float, default=1.5)
    parser.add_argument("--limit",   type=int,   default=10,
                        help="скільки файлів перевірити (0 = всі)")
    parser.add_argument("--save-diff", action="store_true",
                        help="зберегти diff-PNG поряд з npy")
    args = parser.parse_args()

    npy_dir = OUTPUT_DIR / args.model / "npy"
    if not npy_dir.exists():
        print(f"[ERROR] не знайдено: {npy_dir}")
        sys.exit(1)

    npy_files = sorted(npy_dir.glob("*.npy"))
    if args.limit:
        npy_files = npy_files[:args.limit]

    print(f"Модель: {args.model} | epsilon: {args.epsilon} | файлів: {len(npy_files)}\n")
    print(f"{'stem':<22} {'inst':>5} {'mean IoU':>9} {'min IoU':>8} {'lost%':>7} {'added%':>7}")
    print("-" * 65)

    all_ious, all_lost, all_added = [], [], []

    for npy_path in npy_files:
        r = process_npy(npy_path, args.epsilon, args.save_diff)
        all_ious.append(r["mean_iou"])
        all_lost.append(r["pct_removed"])
        all_added.append(r["pct_added"])
        flag = " ⚠" if r["min_iou"] < 0.85 or r["pct_removed"] > 5 else ""
        print(f"{r['stem']:<22} {r['instances']:>5} {r['mean_iou']:>8.3f} "
              f"{r['min_iou']:>8.3f} {r['pct_removed']:>6.2f}% {r['pct_added']:>6.2f}%{flag}")

    print("-" * 65)
    print(f"{'СЕРЕДНЄ':<22} {'':>5} {np.mean(all_ious):>8.3f} "
          f"{'':>8} {np.mean(all_lost):>6.2f}% {np.mean(all_added):>6.2f}%")
    print()
    print("Легенда: mean IoU — серед. якість по інстансах (1.0 = ідеально)")
    print("         lost%  — % пікселів втрачено після round-trip")
    print("         added% — % зайвих пікселів доданих (rounded edges)")
    if args.save_diff:
        print(f"\nDiff-PNG збережено поряд з .npy файлами у {npy_dir}")


if __name__ == "__main__":
    main()
