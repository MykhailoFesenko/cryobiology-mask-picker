"""
bake_all.py — batch-перепікання всіх selected фото з cleanup + polygons.

Що робить:
  1. Запускає Mask Picker сервер у фоні з MPLBACKEND=Agg (без tkinter-крашу).
  2. Чекає поки сервер відповість HTTP 200.
  3. Для кожного selected у <data-dir>/selections.json:
       - читає <data-dir>/polygons/<stem>.json
       - читає <data-dir>/selected/<model>/cleanup.json -> rejected_instances
       - POST /api/polygons-export/<stem>
       - сервер перезаписує selected/<model>/{png,npy,yolo,overlay}/<stem>.*
  4. (опційно --pack) пакує _archive/dataset_<data-dir-name>.zip
     (dense 1..N npy + masks + semantic ЗАВЖДИ; mask_groups лише за --group-masks).
  5. Зупиняє сервер.

Використання:
  python tools/launchers/bake_all.py                         # data/vesicles_good
  python tools/launchers/bake_all.py --data-dir data/my_set
  python tools/launchers/bake_all.py --data-dir data/my_set --pack
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "data" / "vesicles_good"
APP_PY = ROOT / "apps" / "mask_picker" / "app.py"
ARCHIVE = ROOT / "_archive"

# v1.16.0: compaction (sparse reserved-range working id → dense 1..N) при pack.
# Імпортуємо ядро-функцію з пакета mask_picker (numpy/PIL ліниво у pack_zip).
sys.path.insert(0, str(ROOT / "apps" / "mask_picker"))


def start_server(data_dir: Path, host: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["MPLBACKEND"] = "Agg"
    shared_path = str(ROOT / "shared" / "cellsegkit")
    env["PYTHONPATH"] = shared_path + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(APP_PY),
            "--workspace",
            str(data_dir),
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=str(APP_PY.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    print(f"[server] started pid={proc.pid}")
    return proc


def wait_ready(base_url: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/labels", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            pass
        time.sleep(0.5)
    return False


def stop_server(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("[server] stopped")


def post_export(base_url: str, stem: str, payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        f"{base_url}/api/polygons-export/{stem}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def bake_all(data_dir: Path, base_url: str) -> dict:
    sel = _load_json(data_dir / "selections.json", {})
    items = [(k, v) for k, v in sel.items() if v.get("status") == "selected"]

    cleanup_by_model: dict[str, dict] = {}
    for model in {v.get("model") for _, v in items if v.get("model")}:
        p = data_dir / "selected" / model / "cleanup.json"
        cleanup_by_model[model] = _load_json(p, {})

    ok = warn = fail = 0
    baked_total = 0
    rejected_total = 0
    fails: list[tuple[str, str]] = []
    start = time.time()

    for i, (stem, entry) in enumerate(items, 1):
        model = entry.get("model")
        poly_path = data_dir / "polygons" / f"{stem}.json"
        if not poly_path.exists():
            warn += 1
            print(f"  [{i:3d}/{len(items)}] SKIP {stem}: no polygon JSON")
            continue
        payload = json.loads(poly_path.read_text(encoding="utf-8"))
        payload["model"] = model
        cl_entry = (cleanup_by_model.get(model) or {}).get(stem) or {}
        rejected = cl_entry.get("rejected") or []
        payload["rejected_instances"] = rejected
        rejected_total += len(rejected)
        base_label = entry.get("base_label")
        if base_label:
            payload["base_label"] = base_label

        last_err = None
        for attempt in range(3):
            try:
                resp = post_export(base_url, stem, payload)
                last_err = None
                break
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                last_err = e
                time.sleep(2 ** attempt)

        if last_err is not None:
            fail += 1
            fails.append((stem, repr(last_err)))
            print(f"  [{i:3d}/{len(items)}] FAIL {stem}: {last_err}")
            continue

        if resp.get("baked"):
            ok += 1
            baked_total += resp.get("baked_count", 0)
        else:
            warn += 1
            print(f"  [{i:3d}/{len(items)}] WARN {stem}: {resp.get('warn')}")

        if i % 25 == 0:
            print(f"  ...{i}/{len(items)}  ok={ok} fail={fail}  poly={baked_total}  rej={rejected_total}  {time.time()-start:.0f}s")

    elapsed = time.time() - start
    return {
        "ok": ok,
        "warn": warn,
        "fail": fail,
        "baked_polygons": baked_total,
        "rejected_passed": rejected_total,
        "elapsed_sec": elapsed,
        "failures": fails,
    }


def pack_zip(data_dir: Path, archive_dir: Path, zip_name: str | None = None,
             group_masks: bool = False) -> Path:
    sel = _load_json(data_dir / "selections.json", {})
    selected = {k: v for k, v in sel.items() if v.get("status") == "selected"}

    dataset_name = data_dir.name
    archive_root = f"dataset_{dataset_name}"
    dst = archive_dir / (zip_name or f"{archive_root}.zip")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    img_exts = [".jpg", ".jpeg", ".png", ".tif", ".tiff"]

    def find_image(stem: str) -> Path | None:
        for ext in img_exts:
            p = data_dir / "images" / f"{stem}{ext}"
            if p.exists():
                return p
        return None

    readme = archive_dir / "DATASET_README.md"
    readme_text = readme.read_text(encoding="utf-8") if readme.exists() else f"# {archive_root}\n"

    # v1.16.0 compaction: робоче selected/npy у reserved-range просторі
    # (sparse: модель 1..~7000 + полігони 50000+). Для замовника
    # переномеровуємо у щільні 1..N (npy + png + groups РАЗОМ, той самий
    # remap). yolo/overlay/polygons id-agnostic — копіюємо як є.
    import io
    import shutil
    import tempfile
    import numpy as np
    from PIL import Image
    from data_sync import compact_instance_ids
    from baking import export_derived_masks
    from state import Config, _discover_models

    # F-008: Config для export_derived_masks (semantic / mask_groups). Шляхи — як у
    # app.py workspace-mode (усе під data_dir).
    _cfg = Config(
        images_dir=data_dir / "images",
        output_root=data_dir / "output",
        selected_dir=data_dir / "selected",
        skipped_dir=data_dir / "skipped",
        polygons_dir=data_dir / "polygons",
        groups_dir=data_dir / "groups",
        labels_file=data_dir / "labels.json",
        group_classes_file=data_dir / "group_classes.json",
        workspace_dir=data_dir,
        models=_discover_models(data_dir / "output"),
    )

    def _load_npy_2d(p: Path):
        arr = np.load(str(p))
        while arr.ndim > 2:
            arr = arr[0]
        return arr

    n_compacted = 0
    print(f"[pack] writing {dst} ...")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        zf.writestr(f"{archive_root}/README.md", readme_text)
        labels_path = data_dir / "labels.json"
        if labels_path.exists():
            zf.write(labels_path, f"{archive_root}/labels.json")
        zf.writestr(f"{archive_root}/selections.json", json.dumps(selected, ensure_ascii=False, indent=2))

        for stem, entry in selected.items():
            model = entry.get("model")
            img = find_image(stem)
            if img:
                zf.write(img, f"{archive_root}/images/{img.name}")

            npy_path = data_dir / "selected" / model / "npy" / f"{stem}.npy"
            groups_path = data_dir / "groups" / f"{stem}.json"

            # --- COMPACTION: dense npy + png + groups (deliverable 1..N) ---
            dense_npy = None
            if npy_path.exists():
                try:
                    sparse = _load_npy_2d(npy_path)
                    groups_payload = None
                    if groups_path.exists():
                        groups_payload = _load_json(groups_path, None)
                    grp_list = (groups_payload or {}).get("groups") if isinstance(groups_payload, dict) else None
                    dense_npy, _remap = compact_instance_ids(sparse, grp_list)
                    n_compacted += 1
                    # dense npy
                    buf = io.BytesIO()
                    np.save(buf, dense_npy.astype(np.int32, copy=False))
                    zf.writestr(f"{archive_root}/masks_npy/{stem}.npy", buf.getvalue())
                    # dense png (16-bit mirror)
                    pbuf = io.BytesIO()
                    Image.fromarray(dense_npy.astype(np.uint16), mode="I;16").save(pbuf, format="PNG")
                    zf.writestr(f"{archive_root}/masks/{stem}.png", pbuf.getvalue())
                    # dense groups.json (instance_ids переномеровані in-place)
                    if isinstance(groups_payload, dict):
                        zf.writestr(f"{archive_root}/groups/{stem}.json",
                                    json.dumps(groups_payload, ensure_ascii=False, indent=2))
                except Exception as e:
                    print(f"[pack] compaction failed for {stem} ({e}) — fallback copy sparse")
                    dense_npy = None

            # Fallback (compaction не вдалась): копіюємо sparse як є.
            if dense_npy is None:
                for sub_in, sub_out, ext in [("png", "masks", ".png"), ("npy", "masks_npy", ".npy")]:
                    p = data_dir / "selected" / model / sub_in / f"{stem}{ext}"
                    if p.exists():
                        zf.write(p, f"{archive_root}/{sub_out}/{stem}{ext}")
                if groups_path.exists():
                    zf.write(groups_path, f"{archive_root}/groups/{stem}.json")

            # id-agnostic артефакти — копіюємо як є.
            for sub_in, sub_out, ext in [("yolo", "yolo", ".txt"), ("overlay", "overlays", ".png")]:
                p = data_dir / "selected" / model / sub_in / f"{stem}{ext}"
                if p.exists():
                    zf.write(p, f"{archive_root}/{sub_out}/{stem}{ext}")
            pj = data_dir / "polygons" / f"{stem}.json"
            if pj.exists():
                zf.write(pj, f"{archive_root}/polygons/{stem}.json")

            # F-008: derived masks у deliverable. semantic (per-pixel КЛАС) — ЗАВЖДИ;
            # mask_groups (per-pixel group_id) — лише за --group-masks. Обидва
            # id-value-НЕзалежні (semantic=клас; mask_groups=порядок групи у файлі),
            # тож коректні і для dense deliverable, хоч рендеряться з sparse selected/.
            # Реюз tested baking.export_derived_masks.
            if model:
                dm_tmp = Path(tempfile.mkdtemp(prefix="dmask_"))
                try:
                    for f in export_derived_masks(_cfg, model, stem, dm_tmp):
                        kind = f.parent.name   # "semantic" | "mask_groups"
                        if kind == "mask_groups" and not group_masks:
                            continue
                        zf.writestr(f"{archive_root}/{kind}/{stem}.png", f.read_bytes())
                finally:
                    shutil.rmtree(dm_tmp, ignore_errors=True)

    gm_note = "mask_groups INCLUDED" if group_masks else "mask_groups EXCLUDED (--group-masks щоб додати)"
    print(f"[pack] done — {dst.stat().st_size/1024/1024:.0f} MB "
          f"(compacted {n_compacted} stems → dense 1..N; semantic always; {gm_note})")
    return dst


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bake selected masks for any Cryobiology data-dir.")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                   help="Dataset/workspace dir with images/output/selected/polygons.")
    p.add_argument("--pack", action="store_true",
                   help="Also write _archive/dataset_<data-dir-name>.zip.")
    p.add_argument("--archive-dir", type=Path, default=ARCHIVE,
                   help="Where --pack writes the dataset zip.")
    p.add_argument("--zip-name", default=None,
                   help="Optional explicit zip filename for --pack.")
    p.add_argument("--group-masks", action="store_true",
                   help="Include per-pixel GROUP masks (mask_groups/) у --pack deliverable. "
                        "Instance (dense npy) + semantic маски включаються ЗАВЖДИ.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument(
        "--reuse-server",
        metavar="URL",
        default=None,
        help=(
            "Use an already-running Mask Picker (e.g. http://127.0.0.1:5000) "
            "instead of starting a subprocess. Useful when the desktop UI is open."
        ),
    )
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    if not data_dir.exists():
        print(f"[!] data-dir does not exist: {data_dir}", file=sys.stderr)
        return 2

    if args.reuse_server:
        base_url = args.reuse_server.rstrip("/")
        if not wait_ready(base_url, timeout=5):
            print(f"[!] --reuse-server: cannot reach {base_url}", file=sys.stderr)
            return 2
        print(f"[server] reusing already-running server at {base_url}")
        proc = None
    else:
        base_url = f"http://{args.host}:{args.port}"
        proc = start_server(data_dir, args.host, args.port)

    try:
        if proc is not None and not wait_ready(base_url):
            print("[server] timeout waiting for HTTP 200", file=sys.stderr)
            return 2
        print(f"[server] ready, starting bake for {data_dir}")
        stats = bake_all(data_dir, base_url)
        print()
        print("=== BAKE SUMMARY ===")
        for k, v in stats.items():
            if k != "failures":
                print(f"  {k}: {v}")
        if stats["failures"]:
            print("  FAILURES:")
            for s, msg in stats["failures"]:
                print(f"    {s}: {msg}")
        if stats["fail"] > 0:
            print("[!] some images failed — fix and re-run", file=sys.stderr)
    finally:
        if proc is not None:
            stop_server(proc)

    if args.pack:
        pack_zip(data_dir, args.archive_dir.resolve(), args.zip_name,
                 group_masks=args.group_masks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
