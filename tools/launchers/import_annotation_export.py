"""
Import a Mask Picker participant export ZIP into a workspace.

The built-in Export button returns selected/, polygons/, and selections.json.
Participant machines may write absolute local paths into selections.json; this
script merges the useful state and rewrites those paths to workspace-relative
paths so the archive can be moved between computers safely.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "data" / "vesicles_good"
MARKERS = {"selected", "polygons", "skipped"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
FORMAT_EXTS = {
    "overlay": ".png",
    "png": ".png",
    "npy": ".npy",
    "yolo": ".txt",
}


def marker_index(parts: tuple[str, ...]) -> int | None:
    for i, part in enumerate(parts):
        if part in MARKERS or part == "selections.json":
            return i
    return None


def safe_extract(zip_path: Path, dst: Path) -> list[Path]:
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            member = Path(info.filename)
            if member.is_absolute() or ".." in member.parts:
                continue
            idx = marker_index(member.parts)
            if idx is None:
                continue
            rel = Path(*member.parts[idx:])
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out.open("wb") as fh:
                shutil.copyfileobj(src, fh)
            extracted.append(rel)
    return extracted


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def find_image_name(data_dir: Path, stem: str) -> str:
    for ext in IMAGE_EXTS:
        p = data_dir / "images" / f"{stem}{ext}"
        if p.exists():
            return p.name
    return f"{stem}.jpg"


def normalize_polygon_json(path: Path, data_dir: Path) -> dict:
    data = load_json(path, {})
    stem = path.stem
    data.setdefault("version", "5.0.1")
    data.setdefault("flags", {})
    data.setdefault("shapes", [])
    data["imagePath"] = find_image_name(data_dir, stem)
    data["imageData"] = None
    return data


def relative_selected_files(data_dir: Path, stem: str, model: str | None) -> dict[str, str]:
    if not model:
        return {}
    copied: dict[str, str] = {}
    for fmt, ext in FORMAT_EXTS.items():
        p = data_dir / "selected" / model / fmt / f"{stem}{ext}"
        if p.exists():
            copied[fmt] = p.relative_to(data_dir).as_posix()
    return copied


def normalize_selection_entry(data_dir: Path, stem: str, entry: dict) -> dict:
    clean = dict(entry)
    model = clean.get("model")
    if clean.get("status") == "selected" and model:
        clean["copied_files"] = relative_selected_files(data_dir, stem, model)
    return clean


def copy_tree(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    count = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        count += 1
    return count


def backup_workspace(data_dir: Path, backup_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = backup_dir / f"{data_dir.name}_pre_import_{ts}.zip"
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for rel in ["selections.json", "polygons", "selected"]:
            p = data_dir / rel
            if not p.exists():
                continue
            if p.is_file():
                zf.write(p, rel)
            else:
                for child in sorted(p.rglob("*")):
                    if child.is_file():
                        zf.write(child, child.relative_to(data_dir))
    return dst


def import_export(zip_path: Path, data_dir: Path, do_backup: bool = True) -> dict:
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)
    if not data_dir.exists():
        raise FileNotFoundError(data_dir)

    backup = backup_workspace(data_dir, ROOT / "_backups") if do_backup else None
    with tempfile.TemporaryDirectory(prefix="mask_picker_import_") as tmp:
        tmp_dir = Path(tmp)
        extracted = safe_extract(zip_path, tmp_dir)

        copied_selected = copy_tree(tmp_dir / "selected", data_dir / "selected")
        copied_skipped = copy_tree(tmp_dir / "skipped", data_dir / "skipped")

        copied_polygons = 0
        polygon_dir = tmp_dir / "polygons"
        if polygon_dir.exists():
            for p in sorted(polygon_dir.glob("*.json")):
                normalized = normalize_polygon_json(p, data_dir)
                write_json(data_dir / "polygons" / p.name, normalized)
                copied_polygons += 1

        imported_sel = load_json(tmp_dir / "selections.json", {})
        current_sel = load_json(data_dir / "selections.json", {})
        for stem, entry in imported_sel.items():
            if not isinstance(entry, dict):
                continue
            current_sel[stem] = normalize_selection_entry(data_dir, stem, entry)
        write_json(data_dir / "selections.json", current_sel)

    return {
        "zip": str(zip_path),
        "data_dir": str(data_dir),
        "backup": str(backup) if backup else None,
        "extracted": len(extracted),
        "selected_files": copied_selected,
        "polygons": copied_polygons,
        "skipped_files": copied_skipped,
        "selection_entries": len(imported_sel),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import a Mask Picker participant export ZIP.")
    p.add_argument("zip", type=Path, help="Export ZIP from a participant.")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                   help="Target workspace/data dir.")
    p.add_argument("--no-backup", action="store_true",
                   help="Do not create _backups/<data>_pre_import_*.zip first.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        stats = import_export(args.zip.resolve(), args.data_dir.resolve(), not args.no_backup)
    except Exception as exc:
        print(f"[!] import failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
