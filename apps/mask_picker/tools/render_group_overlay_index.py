"""
render_group_overlay_index.py — QA visualization для Mask Picker groups.

Для кожного stem у `<workspace>/groups/` будує overlay (оригінал + per-group
color masks + білі контури) і збирає все у один HTML grid.

Standalone CLI, не імпортує з Mask Picker app. Колірна формула — копія
`apps/mask_picker/static/modules/groups.js::effectiveHSL` (index-based
per-class spread, Bug 2 fix 2026-05-26).

Use:
    python render_group_overlay_index.py --workspace data/vesicles_good
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import cv2
from PIL import Image, ImageDraw


# ---------------------------------------------------------------------------
# Color helpers — index-based per-class spread (Bug 2 fix)
# ---------------------------------------------------------------------------

# Hue spread ±50° — зелений 130° → 80° жовто-салатовий / 180° бірюзовий.
# Не їде у жовтий чи синій. Основна різнокольоровість через sat/light.
HUE_SPREAD = 100   # full range (±50°)
LIGHT_SPREAD = 70  # full range (±35%)
SAT_SPREAD = 80    # full range (±40%)
GOLDEN_HUE = 137.5
LIGHT_STEP = 23
SAT_STEP = 47


def effective_hsl(group: dict, classes: list, index: int) -> tuple:
    """Повертає (h, s, l). Index — позиція групи у межах класу (0-based).
    Golden-angle hue розкид + index-based light/sat варіація для розрізнення
    сусідніх груп одного класу. Mirror groups.js::effectiveHSL."""
    base_h, base_s, base_l = 200, 45, 45
    cid = group.get("class_id")
    cls = None
    if cid and isinstance(classes, list):
        for c in classes:
            if isinstance(c, dict) and c.get("id") == cid:
                cls = c
                break
    if cls:
        if isinstance(cls.get("color_hue"), (int, float)):
            base_h = cls["color_hue"]
        if isinstance(cls.get("color_sat"), (int, float)):
            base_s = cls["color_sat"]
        if isinstance(cls.get("color_light"), (int, float)):
            base_l = cls["color_light"]
    elif isinstance(group.get("color_hue"), (int, float)):
        base_h = group["color_hue"]

    h_offset = (index * GOLDEN_HUE) % HUE_SPREAD - (HUE_SPREAD / 2)
    l_offset = (index * LIGHT_STEP) % LIGHT_SPREAD - (LIGHT_SPREAD / 2)
    s_offset = (index * SAT_STEP) % SAT_SPREAD - (SAT_SPREAD / 2)
    h = ((base_h + h_offset) % 360 + 360) % 360
    s = max(15.0, min(92.0, base_s + s_offset))
    l = max(22.0, min(78.0, base_l + l_offset))
    return (round(h), round(s), round(l))


def build_class_index(groups: list) -> dict:
    """{group_id: index_within_class}. Порядок — як у списку groups."""
    out: dict = {}
    counters: dict = {}
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        gid = grp.get("id")
        if gid is None:
            continue
        cid = grp.get("class_id") or "_none"
        i = counters.get(cid, 0)
        out[gid] = i
        counters[cid] = i + 1
    return out


def hsl_to_rgb(h: float, s: float, l: float) -> tuple:
    """HSL [0-360, 0-100, 0-100] → RGB [0-255, 0-255, 0-255]."""
    h = (h % 360) / 360.0
    s /= 100.0
    l /= 100.0
    if s == 0:
        v = round(l * 255)
        return (v, v, v)

    def hue_to_rgb(p, q, t):
        if t < 0: t += 1
        if t > 1: t -= 1
        if t < 1/6: return p + (q - p) * 6 * t
        if t < 1/2: return q
        if t < 2/3: return p + (q - p) * (2/3 - t) * 6
        return p

    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    r = hue_to_rgb(p, q, h + 1/3)
    g = hue_to_rgb(p, q, h)
    b = hue_to_rgb(p, q, h - 1/3)
    return (round(r * 255), round(g * 255), round(b * 255))


# ---------------------------------------------------------------------------
# Workspace I/O
# ---------------------------------------------------------------------------

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _detect_model(workspace: Path, override: Optional[str]) -> Optional[str]:
    sel = workspace / "selected"
    if not sel.exists():
        return override
    if override:
        if (sel / override).exists():
            return override
        print(f"warning: --model {override} not found under {sel}", file=sys.stderr)
        return None
    models = [p.name for p in sel.iterdir() if p.is_dir() and (p / "npy").exists()]
    if len(models) == 1:
        return models[0]
    if len(models) > 1:
        print(f"warning: multiple models under {sel}: {models}. Pass --model.", file=sys.stderr)
    return None


def _find_image(workspace: Path, stem: str) -> Optional[Path]:
    img_dir = workspace / "images"
    for ext in IMAGE_EXTS:
        p = img_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def _load_labels(workspace: Path, stem: str, model: str) -> Optional[np.ndarray]:
    npy_path = workspace / "selected" / model / "npy" / f"{stem}.npy"
    if not npy_path.exists():
        return None
    try:
        arr = np.load(str(npy_path))
        while arr.ndim > 2:
            arr = arr[0]
        return arr.astype(np.int32)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Overlay rendering
# ---------------------------------------------------------------------------

ALPHA = 102  # ~0.4 alpha


def render_overlay(
    image_path: Path,
    labels: Optional[np.ndarray],
    groups: list,
    shapes: list,
    classes: list,
) -> Image.Image:
    """Builds RGB Image — base + colored masks + white contours."""
    base = Image.open(str(image_path)).convert("RGB")
    W, H = base.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    class_index = build_class_index(groups)

    # 1) Color fill per group: instance masks
    if labels is not None and labels.shape == (H, W):
        max_id = int(labels.max()) if labels.size else 0
        if max_id > 0:
            for g_idx, grp in enumerate(groups):
                if not isinstance(grp, dict):
                    continue
                iids = grp.get("instance_ids") or []
                if not iids:
                    continue
                idx = class_index.get(grp.get("id"), 0)
                h, s, l = effective_hsl(grp, classes, idx)
                r, g, b = hsl_to_rgb(h, s, l)
                grp_mask = np.zeros(labels.shape, dtype=bool)
                for iid in iids:
                    try:
                        iid_int = int(iid)
                    except (TypeError, ValueError):
                        continue
                    if 0 < iid_int <= max_id:
                        grp_mask |= (labels == iid_int)
                if not grp_mask.any():
                    continue
                # Paste color через RGBA Image, який потім compositions
                color_arr = np.zeros((H, W, 4), dtype=np.uint8)
                color_arr[grp_mask] = (r, g, b, ALPHA)
                color_layer = Image.fromarray(color_arr, mode="RGBA")
                overlay = Image.alpha_composite(overlay, color_layer)

                # Контури білі
                u8_mask = grp_mask.astype(np.uint8) * 255
                contours, _ = cv2.findContours(u8_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                outline_arr = np.zeros((H, W, 4), dtype=np.uint8)
                cv2.drawContours(outline_arr, contours, -1, (255, 255, 255, 220), 1)
                outline_layer = Image.fromarray(outline_arr, mode="RGBA")
                overlay = Image.alpha_composite(overlay, outline_layer)

    # 2) Color fill per group: polygon shapes (для груп з polygon_indices)
    if shapes:
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            pis = grp.get("polygon_indices") or []
            if not pis:
                continue
            idx = class_index.get(grp.get("id"), 0)
            h, s, l = effective_hsl(grp, classes, idx)
            r, g, b = hsl_to_rgb(h, s, l)
            for pi in pis:
                try:
                    pi_int = int(pi)
                except (TypeError, ValueError):
                    continue
                if not (0 <= pi_int < len(shapes)):
                    continue
                sh = shapes[pi_int]
                if not isinstance(sh, dict):
                    continue
                pts = sh.get("points") or []
                if len(pts) < 3:
                    continue
                try:
                    poly_pts = [(int(round(float(x))), int(round(float(y)))) for x, y in pts]
                except (TypeError, ValueError):
                    continue
                # Fill
                shape_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                d = ImageDraw.Draw(shape_layer)
                d.polygon(poly_pts, fill=(r, g, b, ALPHA))
                # Outline (closed)
                d.line(poly_pts + [poly_pts[0]], fill=(255, 255, 255, 220), width=1)
                overlay = Image.alpha_composite(overlay, shape_layer)

    base_rgba = base.convert("RGBA")
    composed = Image.alpha_composite(base_rgba, overlay)
    return composed.convert("RGB")


# ---------------------------------------------------------------------------
# Stats per stem
# ---------------------------------------------------------------------------

def summarize_groups(groups: list, classes: list) -> dict:
    out = {"total": 0, "by_class": {}, "polygon_only": 0, "polygon_count": 0}
    name_by_id = {c["id"]: c.get("name", c["id"]) for c in classes if isinstance(c, dict) and c.get("id")}
    for g in groups:
        if not isinstance(g, dict):
            continue
        out["total"] += 1
        cid = g.get("class_id") or "unclassified"
        cname = name_by_id.get(cid, cid)
        out["by_class"][cname] = out["by_class"].get(cname, 0) + 1
        pis = g.get("polygon_indices") or []
        iids = g.get("instance_ids") or []
        if pis:
            out["polygon_count"] += len(pis)
            if not iids:
                out["polygon_only"] += 1
    return out


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8"/>
<title>Group Overlay Index — {ws_name}</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background:#1a1a1a; color:#eee; margin:0; padding:24px; }
  h1 { font-size: 18px; margin: 0 0 8px 0; }
  .meta { color:#888; font-size: 12px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 20px; }
  .cell { background:#222; border-radius:8px; padding:12px; }
  .cell h3 { margin:0 0 6px 0; font-size: 14px; font-weight: 600; }
  .cell .stats { color:#aaa; font-size: 11px; margin-bottom: 8px; line-height: 1.5; }
  .cell img { width: 100%; height: auto; display:block; border-radius:4px; }
  .warn { color: #ff9966; }
</style></head>
<body>
<h1>Group Overlay Index — {ws_name}</h1>
<div class="meta">model: {model} · stems: {n_stems} · generated by render_group_overlay_index.py</div>
<div class="grid">
"""

HTML_TAIL = """</div></body></html>
"""


def _img_to_data_uri(img: Image.Image, fmt: str = "JPEG", quality: int = 80) -> str:
    buf = io.BytesIO()
    save_kwargs = {}
    if fmt.upper() == "JPEG":
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    img.save(buf, format=fmt, **save_kwargs)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _stats_html(stats: dict) -> str:
    parts = [f'<b>{stats["total"]}</b> groups']
    if stats["by_class"]:
        cls_parts = [f"{n} {name}" for name, n in sorted(stats["by_class"].items())]
        parts.append(" · ".join(cls_parts))
    if stats["polygon_count"]:
        msg = f'{stats["polygon_count"]} polygon shape(s)'
        if stats["polygon_only"]:
            msg += f' · <span class="warn">{stats["polygon_only"]} polygon-only group(s) — won\'t appear in mask_groups.png</span>'
        parts.append(msg)
    return "<br/>".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Render Mask Picker group overlays into one HTML grid.")
    ap.add_argument("--workspace", required=True, type=Path,
                    help="Workspace dir with images/, groups/, selected/<model>/npy/")
    ap.add_argument("--model", type=str, default=None,
                    help="Model name under selected/ (auto-detect if exactly one).")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output HTML path. Default: <workspace>/group_overlay_index.html")
    ap.add_argument("--per-image-dir", type=Path, default=None,
                    help="If set — also save per-stem PNGs (full res) into this directory.")
    ap.add_argument("--jpeg-quality", type=int, default=75,
                    help="JPEG quality для embedded images (default 75).")
    ap.add_argument("--max-dim", type=int, default=1280,
                    help="Downscale embedded images so max(width,height) ≤ max-dim. 0 = no resize. Default 1280.")
    args = ap.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"error: workspace not a directory: {workspace}", file=sys.stderr)
        return 2

    groups_dir = workspace / "groups"
    if not groups_dir.is_dir():
        print(f"error: missing {groups_dir}", file=sys.stderr)
        return 2

    model = _detect_model(workspace, args.model)
    if not model:
        print("error: could not resolve model — pass --model explicitly.", file=sys.stderr)
        return 2

    classes_path = workspace / "group_classes.json"
    classes_payload = _read_json(classes_path) or {}
    classes = classes_payload.get("classes") or []

    out_html = args.out.resolve() if args.out else (workspace / "group_overlay_index.html")
    per_dir = args.per_image_dir.resolve() if args.per_image_dir else None
    if per_dir:
        per_dir.mkdir(parents=True, exist_ok=True)

    stems = sorted(p.stem for p in groups_dir.iterdir() if p.is_file() and p.suffix.lower() == ".json")
    if not stems:
        print(f"error: no group jsons under {groups_dir}", file=sys.stderr)
        return 2

    cells_html: list = []
    skipped: list = []

    for i, stem in enumerate(stems, start=1):
        img_path = _find_image(workspace, stem)
        if img_path is None:
            skipped.append((stem, "no image"))
            continue
        groups_payload = _read_json(groups_dir / f"{stem}.json") or {}
        groups = groups_payload.get("groups") or []

        polygons_payload = _read_json(workspace / "polygons" / f"{stem}.json") or {}
        shapes = polygons_payload.get("shapes") or []

        labels = _load_labels(workspace, stem, model)

        try:
            overlay_img = render_overlay(img_path, labels, groups, shapes, classes)
        except Exception as e:
            skipped.append((stem, f"render failed: {e}"))
            continue

        if per_dir:
            overlay_img.save(str(per_dir / f"{stem}.png"), format="PNG")

        stats = summarize_groups(groups, classes)
        embed_img = overlay_img
        if args.max_dim and max(embed_img.size) > args.max_dim:
            ratio = args.max_dim / max(embed_img.size)
            new_size = (int(round(embed_img.size[0] * ratio)),
                        int(round(embed_img.size[1] * ratio)))
            embed_img = embed_img.resize(new_size, Image.LANCZOS)
        data_uri = _img_to_data_uri(embed_img, fmt="JPEG", quality=args.jpeg_quality)
        cells_html.append(
            f'<div class="cell"><h3>{stem}</h3>'
            f'<div class="stats">{_stats_html(stats)}</div>'
            f'<img src="{data_uri}" alt="{stem}"/></div>'
        )
        print(f"[{i}/{len(stems)}] {stem} — {stats['total']} groups", file=sys.stderr)

    html = (
        HTML_HEAD.replace("{ws_name}", workspace.name)
                 .replace("{model}", model)
                 .replace("{n_stems}", str(len(cells_html)))
        + "\n".join(cells_html)
        + HTML_TAIL
    )
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html, encoding="utf-8")

    print(f"\nWrote {out_html} ({len(cells_html)} stems, {len(skipped)} skipped)", file=sys.stderr)
    for stem, reason in skipped:
        print(f"  skipped {stem}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
