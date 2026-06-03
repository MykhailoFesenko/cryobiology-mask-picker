"""
api_workspace.py — HTTP-маршрути workspace flow.

Endpoints:
  GET  /api/workspace/info             — workspace_dir + workspace_mode.
  POST /api/workspace/pick-folder      — Tkinter dialog для select dir.
  POST /api/workspace/pick-dir         — ручний type-in шлях.
  POST /api/workspace/import           — replace import (ZIP).
  POST /api/workspace/import-scan      — Day 8 merge-import range-picker.
  POST /api/workspace/import-apply     — apply per-stem merge (newest-wins).
  POST /api/workspace/export           — ZIP export (з опціями Finalize/Split).
  GET  /api/workspace/finalize/<stem>  — стрим ZIP одного фото (bake через
                                          `data_sync.bake_with_resync`).
  POST /api/workspace/split            — auto-розбиття на N parts.
  POST /api/workspace/bake-all         — background batch bake (Day 7 Save All).
  GET  /api/workspace/bake-progress    — polling прогресу bake-all.

Background bake-all: `_run_bake_all` у окремому threading.Thread,
прогрес у `_BAKE_PROGRESS` (module-level singleton під `_BAKE_LOCK`).
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import threading
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from state import (
    Config,
    StateStore,
    _discover_models,
    _find_image_filename,
    _load_json_file,
)
from cleanup import _find_npy_for
from polygons import _validate_polygons_payload
from baking import (
    _bake_polygons_to_selected,
    export_derived_masks,
    export_per_label_overlays,
)
from data_sync import bake_with_resync
from catalog import CatalogService
from workspace import WORKSPACE_META_FILES, _find_marker_index, switch_workspace


# ---------------------------------------------------------------------------
# Day 7 — Save All: background bake queue + progress (module-level singleton).
# Один процес = одна черга bake. Polling через GET /api/workspace/bake-progress.
# ---------------------------------------------------------------------------

_BAKE_LOCK = threading.Lock()
_BAKE_PROGRESS: dict = {
    "running": False,        # bake-thread активний
    "done": False,           # останній прогон завершився (для фінального poll)
    "photo_done": 0,         # скільки фото повністю запечено
    "photo_total": 0,        # усього фото в черзі
    "current_stem": "",      # яке фото зараз
    "phase": "",             # людська назва фази поточного фото
    "phase_frac": 0.0,       # прогрес усередині поточного фото (0..1)
    "ok_count": 0,
    "skipped": [],           # [{stem, reason}] — пропущені (нема моделі/npy)
    "errors": [],            # [{stem, error}] — впали з винятком
    "started_at": None,
    "finished_at": None,
}


def _bake_progress_snapshot() -> dict:
    with _BAKE_LOCK:
        return dict(_BAKE_PROGRESS)


def _collect_bake_job(cfg: Config, state: StateStore, stem: str):
    """
    Збирає все потрібне для bake одного stem (з state + диска).

    Повертає dict {model_name, src_npy, shapes, rejected, base_label}
    або ("skip", reason) якщо stem не запікається (нема Pick / моделі / npy).
    """
    entry = state.get(stem) or {}
    model_name = entry.get("model")
    if not model_name:
        return ("skip", "немає обраної моделі (Pick)")
    m = next((mm for mm in cfg.models if mm.name == model_name), None)
    if not m:
        return ("skip", f"модель {model_name!r} недоступна")
    src_npy = _find_npy_for(m, stem)
    if not src_npy:
        return ("skip", f"немає .npy для {model_name}")

    shapes: list = []
    if cfg.polygons_dir:
        poly_path = cfg.polygons_dir / f"{stem}.json"
        if poly_path.exists():
            try:
                with open(poly_path, "r", encoding="utf-8") as f:
                    shapes = (json.load(f) or {}).get("shapes") or []
            except Exception as e:
                return ("skip", f"polygons.json пошкоджено: {e}")

    cleanup = entry.get("cleanup") or {}
    rejected: list = []
    if cleanup.get("model") == model_name:
        rejected = list(cleanup.get("rejected_instances") or [])

    return {
        "model_name": model_name,
        "src_npy": src_npy,
        "shapes": shapes,
        "rejected": rejected,
        "base_label": entry.get("base_label") or None,
    }


def _run_bake_all(cfg: Config, state: StateStore, catalog: CatalogService,
                  stems: list[str]) -> None:
    """Thread target: послідовно запікає всі stems, оновлює _BAKE_PROGRESS."""
    for i, stem in enumerate(stems):
        with _BAKE_LOCK:
            _BAKE_PROGRESS["photo_done"] = i
            _BAKE_PROGRESS["current_stem"] = stem
            _BAKE_PROGRESS["phase"] = "Підготовка"
            _BAKE_PROGRESS["phase_frac"] = 0.0

        job = _collect_bake_job(cfg, state, stem)
        if isinstance(job, tuple):  # ("skip", reason)
            with _BAKE_LOCK:
                _BAKE_PROGRESS["skipped"].append({"stem": stem, "reason": job[1]})
            continue

        def _cb(phase: str, frac: float, _stem=stem) -> None:
            with _BAKE_LOCK:
                # ігноруємо запізнілий callback від попереднього фото
                if _BAKE_PROGRESS["current_stem"] == _stem:
                    _BAKE_PROGRESS["phase"] = phase
                    _BAKE_PROGRESS["phase_frac"] = float(frac)

        try:
            result = bake_with_resync(
                cfg, stem, job["model_name"], job["src_npy"], job["shapes"],
                rejected=job["rejected"], base_label=job["base_label"],
                do_backup=True, progress_cb=_cb,
            )
            if result["errors"]:
                with _BAKE_LOCK:
                    _BAKE_PROGRESS["errors"].append({
                        "stem": stem,
                        "error": "export failed: " + ", ".join(result["errors"]),
                    })
            else:
                state.clear_dirty(stem)
                with _BAKE_LOCK:
                    _BAKE_PROGRESS["ok_count"] += 1
        except Exception as e:
            traceback.print_exc()
            with _BAKE_LOCK:
                _BAKE_PROGRESS["errors"].append({"stem": stem, "error": str(e)})

    catalog.invalidate()
    with _BAKE_LOCK:
        _BAKE_PROGRESS["photo_done"] = len(stems)
        _BAKE_PROGRESS["current_stem"] = ""
        _BAKE_PROGRESS["phase"] = "Завершено"
        _BAKE_PROGRESS["phase_frac"] = 1.0
        _BAKE_PROGRESS["running"] = False
        _BAKE_PROGRESS["done"] = True
        _BAKE_PROGRESS["finished_at"] = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Day 8 — selective merge import: scan ZIP → range-picker → per-stem merge.
# ---------------------------------------------------------------------------

_IMPORT_IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def _norm_stem(name: str) -> str:
    """Нормалізує stem: прибирає префікс 'Копия ' (дублікати датасету)."""
    return name.replace("Копия ", "").strip()


def _scan_stems_in_dir(root: Path) -> list[str]:
    """
    Усі унікальні stem-и у розпакованій папці — з images/, polygons/,
    selected/ растрів і selections.json. Відсортовані.
    """
    stems: set[str] = set()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        parts_lower = {x.lower() for x in p.parts}
        suf = p.suffix.lower()
        if p.name == "selections.json":
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig")) or {}
                stems.update(data.keys())
            except Exception:
                pass
        elif "images" in parts_lower and suf in _IMPORT_IMG_EXTS:
            stems.add(_norm_stem(p.stem))
        elif "polygons" in parts_lower and suf == ".json":
            stems.add(_norm_stem(p.stem))
        elif "groups" in parts_lower and suf == ".json":
            stems.add(_norm_stem(p.stem))
        elif "selected" in parts_lower and suf in {".npy", ".png", ".txt"}:
            if p.stem != "cleanup":
                stems.add(_norm_stem(p.stem))
    return sorted(s for s in stems if s)


def _copy_stem_artifacts(src_root: Path, dst_ws: Path, stem: str) -> int:
    """
    Копіює всі файли одного stem з розпакованого src у workspace dst,
    зберігаючи structural-структуру (images/output/selected/polygons/groups).
    selections.json / cleanup.json — НЕ копіює (merge окремо). Повертає к-сть.
    """
    copied = 0
    for p in src_root.rglob("*"):
        if not p.is_file() or _norm_stem(p.stem) != stem:
            continue
        if p.name in ("selections.json", "cleanup.json"):
            continue
        idx = _find_marker_index(p.parts)
        if idx is None:
            continue
        rel = Path(*p.parts[idx:])
        # Нормалізуємо ім'я файлу (прибираємо 'Копия ' у leaf).
        rel = rel.with_name(rel.name.replace("Копия ", "").strip())
        dst = dst_ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        copied += 1
    return copied


def _read_scan_selections(src_root: Path) -> dict:
    """Читає selections.json з розпакованого scan-джерела ({} якщо нема)."""
    src_sel = next(src_root.rglob("selections.json"), None)
    if src_sel is None:
        return {}
    try:
        return json.loads(src_sel.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return {}


def _decide_import_winner(imp_entry, loc_entry) -> str:
    """
    Merge-політика «newest wins» (рішення автора 2026-05-19).

    - imported має стан, local — нема (або фото взагалі нема) → imported
    - local має, imported — нема → local (пропускаємо)
    - обидва мають → новіший за `ts` (при рівних — imported, бо свіжий імпорт)

    Повертає 'imported' | 'local'.
    """
    if imp_entry is None:
        return "local"
    if loc_entry is None:
        return "imported"
    imp_ts = str(imp_entry.get("ts") or "")
    loc_ts = str(loc_entry.get("ts") or "")
    # ISO-timestamp лексикографічний = хронологічний.
    return "imported" if imp_ts >= loc_ts else "local"


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_workspace", __name__)

    @bp.route("/api/workspace/info")
    def api_workspace_info():
        ws = cfg.workspace_dir
        return jsonify({
            "workspace_dir": str(ws) if ws else None,
            "workspace_mode": ws is not None,
            "images_dir": str(cfg.images_dir),
        })

    @bp.route("/api/workspace/pick-folder", methods=["POST"])
    def api_workspace_pick_folder():
        """
        Перемикає workspace без перезапуску сервера.

        Body optional:
          {"path": "C:/path/to/workspace"}  # для тестів / advanced-викликів

        Якщо path не передано — відкриває native folder picker на машині,
        де запущений Flask.
        """
        body = request.get_json(silent=True) or {}
        raw_path = str(body.get("path") or "").strip()

        if raw_path:
            picked = Path(raw_path)
        else:
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                chosen = filedialog.askdirectory(
                    title="Вибери workspace папку для Mask Picker"
                )
                root.destroy()
            except Exception as e:
                return jsonify({"error": f"folder picker unavailable: {e}"}), 500
            if not chosen:
                return jsonify({"ok": False, "cancelled": True})
            picked = Path(chosen)

        try:
            info = switch_workspace(cfg, state, catalog, picked)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, **info})

    @bp.route("/api/workspace/pick-dir", methods=["POST"])
    def api_workspace_pick_dir():
        """
        Native folder picker — повертає обрану папку, БЕЗ зміни workspace.
        Для поля «Зберегти у» у вікні Експорту (місце збереження export-ZIP).
        """
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askdirectory(title="Куди зберегти export-ZIP")
            root.destroy()
        except Exception as e:
            return jsonify({"error": f"folder picker unavailable: {e}"}), 500
        if not chosen:
            return jsonify({"ok": False, "cancelled": True})
        return jsonify({"ok": True, "path": str(Path(chosen))})

    @bp.route("/api/workspace/import", methods=["POST"])
    def api_workspace_import():
        """
        Приймає один або кілька файлів через multipart/form-data (field name: "files").
        Підтримує: .jpg/.jpeg/.png (кладе в images/) та .zip (розпаковує).

        ZIP може бути:
          - структурним (містить images/output/selected/polygons/ у будь-якій
            частині шляху, в т.ч. з обгорткою типу `_workspace/`) —
            розпаковується у workspace зі стрипом префіксу до маркера;
          - плоским (тільки зображення) — все кладеться в images/.

        Повертає: {"ok": true, "imported": N, "skipped": [...]}
        """
        if cfg.workspace_dir is None:
            return jsonify({"error": "Не в workspace-режимі. Запусти з --workspace PATH."}), 400

        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "Файли не передано (поле 'files')."}), 400

        images_dir = cfg.images_dir
        images_dir.mkdir(parents=True, exist_ok=True)

        imported = 0
        skipped = []
        IMAGE_EXTS_IMPORT = {".jpg", ".jpeg", ".png"}

        for f in files:
            name = Path(f.filename).name if f.filename else ""
            ext = Path(name).suffix.lower()

            if ext in IMAGE_EXTS_IMPORT:
                dst = images_dir / name
                f.save(str(dst))
                imported += 1

            elif ext == ".zip":
                data = f.read()
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        names = zf.namelist()
                        # structural: будь-яка частина шляху одного з members є маркером
                        structural = any(
                            _find_marker_index(Path(n).parts) is not None for n in names
                        )
                        for member in names:
                            mpath = Path(member)
                            if mpath.name == "" or mpath.name.startswith("."):
                                continue  # skip dirs and hidden
                            if structural:
                                parts = mpath.parts
                                idx = _find_marker_index(parts)
                                if idx is not None:
                                    dst = cfg.workspace_dir / Path(*parts[idx:])
                                elif mpath.name in WORKSPACE_META_FILES:
                                    # selections.json/labels.json з префіксом → у корінь
                                    dst = cfg.workspace_dir / mpath.name
                                else:
                                    skipped.append({"file": member, "reason": "no workspace marker in path"})
                                    continue
                            else:
                                # flat ZIP: беремо тільки зображення
                                if mpath.suffix.lower() not in IMAGE_EXTS_IMPORT:
                                    continue
                                dst = images_dir / mpath.name
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as src, open(dst, "wb") as out:
                                shutil.copyfileobj(src, out)
                            imported += 1
                except zipfile.BadZipFile as e:
                    skipped.append({"file": name, "reason": str(e)})
            else:
                skipped.append({"file": name, "reason": f"unsupported extension {ext}"})

        # Перевідкриваємо моделі (на випадок нових output/<model>/), перечитуємо
        # selections.json у пам'ять (інакше catalog.build() використає старий стейт
        # і UI не показує ні Selected, ні cleanup rejected, ні базовий model
        # variant — лише фото + полігони) + інвалідуємо catalog.
        cfg.models = _discover_models(cfg.output_root)
        state.reload(cfg.selected_dir.parent / "selections.json")
        catalog.invalidate()

        return jsonify({"ok": True, "imported": imported, "skipped": skipped})

    @bp.route("/api/workspace/export")
    def api_workspace_export():
        """
        Стримить ZIP з: selected/, polygons/, groups/, selections.json.
        Призначений для передачі результатів роботи команди.

        Day 8 — вибірковий експорт: query `?stems=a,b,c` обмежує ZIP лише
        вказаними фото. Без параметра — експортується все.
        """
        if cfg.workspace_dir is None:
            return jsonify({"error": "Не в workspace-режимі."}), 400

        stems_param = (request.args.get("stems") or "").strip()
        stem_filter = (
            {s.strip() for s in stems_param.split(",") if s.strip()}
            if stems_param else None
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            selections_path = cfg.selected_dir.parent / "selections.json"
            if selections_path.exists():
                if stem_filter is None:
                    zf.write(selections_path, "selections.json")
                else:
                    # Відфільтрувати selections.json до обраних stem-ів.
                    data = _load_json_file(selections_path, {})
                    filtered = {s: e for s, e in data.items() if s in stem_filter}
                    zf.writestr(
                        "selections.json",
                        json.dumps(filtered, ensure_ascii=False, indent=2),
                    )

            for root_dir, arc_prefix in [
                (cfg.selected_dir, "selected"),
                (cfg.polygons_dir, "polygons"),
                (cfg.groups_dir, "groups"),
            ]:
                if root_dir and root_dir.exists():
                    for p in sorted(root_dir.rglob("*")):
                        if not p.is_file() or "_backups" in p.parts:
                            continue
                        # p.stem == ім'я фото для polygons/groups/selected растрів.
                        if stem_filter is not None and p.stem not in stem_filter:
                            continue
                        rel = p.relative_to(root_dir.parent)
                        zf.write(p, str(rel).replace("\\", "/"))

            # Day 8: per-label overlays — overlays/<stem>__<label>.png.
            ov_tmp = cfg.workspace_dir / "_tmp" / f"plov_{datetime.now():%Y%m%d%H%M%S}"
            try:
                if stem_filter is not None:
                    ov_stems = sorted(stem_filter)
                else:
                    ov_stems = sorted(
                        s for s, e in state.all().items()
                        if e.get("status") == "selected" and e.get("model")
                    )
                for s in ov_stems:
                    model = (state.get(s) or {}).get("model")
                    if not model:
                        continue
                    for f in export_per_label_overlays(cfg, model, s, ov_tmp):
                        zf.write(f, f"overlays/{f.name}")
            finally:
                shutil.rmtree(ov_tmp, ignore_errors=True)

            # 2026-05-22: опційні похідні маски — semantic (per-label) +
            # group-instance. Вмикається галочкою «Маски» у вікні Експорту
            # (?masks=1). Кладуться поряд з instance-маскою у selected/<model>/.
            if (request.args.get("masks") or "").strip() in ("1", "true", "yes"):
                dm_tmp = (cfg.workspace_dir / "_tmp"
                          / f"dmask_{datetime.now():%Y%m%d%H%M%S}")
                try:
                    for s in ov_stems:
                        model = (state.get(s) or {}).get("model")
                        if not model:
                            continue
                        for f in export_derived_masks(cfg, model, s, dm_tmp):
                            zf.write(f, f"selected/{model}/{f.parent.name}/{f.name}")
                finally:
                    shutil.rmtree(dm_tmp, ignore_errors=True)

        buf.seek(0)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{len(stem_filter)}sel" if stem_filter else ""
        download_name = f"mask_picker_export{suffix}_{ts}.zip"

        # Опційний серверний шлях збереження (поле «Зберегти у» вікна
        # Експорту). Якщо заданий — пишемо ZIP туди й повертаємо JSON;
        # інакше — звичайне завантаження браузером (as_attachment).
        dest = (request.args.get("dest") or "").strip()
        if dest:
            try:
                dest_dir = Path(dest)
                dest_dir.mkdir(parents=True, exist_ok=True)
                out_path = dest_dir / download_name
                with open(out_path, "wb") as fh:
                    fh.write(buf.getvalue())
            except Exception as e:
                return jsonify({"error": f"Не вдалося зберегти у «{dest}»: {e}"}), 500
            return jsonify({"ok": True, "path": str(out_path)})

        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=download_name,
        )

    @bp.route("/api/workspace/finalize/<stem>")
    def api_workspace_finalize(stem: str):
        """
        Стримить ZIP тільки для одного фото: image + selected files + polygon +
        filtered selections.json/cleanup.json. Це зручний "send back" пакет.
        """
        if cfg.workspace_dir is None:
            return jsonify({"error": "Не в workspace-режимі."}), 400

        entry = state.get(stem) or {}
        poly_path = cfg.polygons_dir / f"{stem}.json" if cfg.polygons_dir else None
        has_poly = bool(poly_path and poly_path.exists())
        has_state = bool(entry)
        if not has_poly and not has_state:
            return jsonify({"error": f"Немає збереженої розмітки для {stem}."}), 404

        if has_poly and entry.get("model"):
            try:
                body = _load_json_file(poly_path, {})
                err = _validate_polygons_payload(body)
                if err:
                    return jsonify({"error": f"polygon JSON invalid: {err}"}), 400
                shapes = body.get("shapes") or []
                if shapes:
                    model_name = entry["model"]
                    m = next((mm for mm in cfg.models if mm.name == model_name), None)
                    src_npy = _find_npy_for(m, stem) if m else None
                    if not m or not src_npy:
                        return jsonify({
                            "error": f"Cannot finalize {stem}: selected model {model_name!r} has no source .npy"
                        }), 400

                    rejected = (
                        (entry.get("cleanup") or {}).get("rejected_instances")
                        or []
                    )
                    base_label = entry.get("base_label") or None

                    result = bake_with_resync(
                        cfg, stem, model_name, src_npy, shapes,
                        rejected=rejected, base_label=base_label, do_backup=False,
                    )
                    if result["errors"]:
                        return jsonify({
                            "error": "finalize bake failed for formats: "
                                     + ", ".join(result["errors"])
                        }), 500
                    # Day 7: finalize запік поточне фото — dirty знято.
                    state.clear_dirty(stem)
            except Exception as e:
                print(f"[workspace-finalize] bake failed for {stem}: {e}")
                traceback.print_exc()
                return jsonify({"error": f"finalize bake failed: {e}"}), 500

        def _write_if_exists(zf: zipfile.ZipFile, path: Path, arcname: str) -> bool:
            if path.exists() and path.is_file():
                zf.write(path, arcname.replace("\\", "/"))
                return True
            return False

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            labels_path = cfg.selected_dir.parent / "labels.json"
            _write_if_exists(zf, labels_path, "labels.json")

            image_name = _find_image_filename(cfg.images_dir, stem)
            if image_name:
                _write_if_exists(zf, cfg.images_dir / image_name, f"images/{image_name}")

            if has_poly and poly_path is not None:
                _write_if_exists(zf, poly_path, f"polygons/{stem}.json")

            # Day 4-5: include groups/<stem>.json якщо існує
            if cfg.groups_dir:
                groups_path = cfg.groups_dir / f"{stem}.json"
                _write_if_exists(zf, groups_path, f"groups/{stem}.json")

            skipped_path = cfg.skipped_dir / f"{stem}.skipped.txt"
            _write_if_exists(zf, skipped_path, f"skipped/{stem}.skipped.txt")

            models_to_scan = set()
            if entry.get("model"):
                models_to_scan.add(entry["model"])
            if cfg.selected_dir.exists():
                for model_dir in cfg.selected_dir.iterdir():
                    if model_dir.is_dir() and not model_dir.name.startswith("_"):
                        models_to_scan.add(model_dir.name)

            for model_name in sorted(models_to_scan):
                model_dir = cfg.selected_dir / model_name
                if not model_dir.exists():
                    continue
                for fmt in ("overlay", "png", "npy", "yolo"):
                    fmt_dir = model_dir / fmt
                    if not fmt_dir.exists():
                        continue
                    for p in sorted(fmt_dir.glob(f"{stem}.*")):
                        _write_if_exists(
                            zf,
                            p,
                            f"selected/{model_name}/{fmt}/{p.name}",
                        )

                cleanup_path = model_dir / "cleanup.json"
                cleanup_data = _load_json_file(cleanup_path, {})
                if stem in cleanup_data:
                    zf.writestr(
                        f"selected/{model_name}/cleanup.json",
                        json.dumps({stem: cleanup_data[stem]}, ensure_ascii=False, indent=2),
                    )

            # Day 8: per-label overlays для цього фото.
            if entry.get("model"):
                ov_tmp = (cfg.workspace_dir / "_tmp"
                          / f"plov_fin_{datetime.now():%Y%m%d%H%M%S}")
                try:
                    for f in export_per_label_overlays(
                        cfg, entry["model"], stem, ov_tmp
                    ):
                        zf.write(f, f"overlays/{f.name}")
                finally:
                    shutil.rmtree(ov_tmp, ignore_errors=True)

            filtered_entry = dict(entry)
            if filtered_entry.get("status") == "selected" and filtered_entry.get("model"):
                model_name = filtered_entry["model"]
                copied = {}
                for fmt, ext in {
                    "overlay": ".png",
                    "png": ".png",
                    "npy": ".npy",
                    "yolo": ".txt",
                }.items():
                    p = cfg.selected_dir / model_name / fmt / f"{stem}{ext}"
                    if p.exists():
                        copied[fmt] = p.relative_to(cfg.selected_dir.parent).as_posix()
                filtered_entry["copied_files"] = copied
            zf.writestr(
                "selections.json",
                json.dumps({stem: filtered_entry}, ensure_ascii=False, indent=2),
            )

        buf.seek(0)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"mask_picker_final_{stem}_{ts}.zip",
        )

    @bp.route("/api/workspace/split", methods=["POST"])
    def api_workspace_split():
        """
        Розбиває workspace на N task pack-ів для роздачі учасникам.

        Делегує у `tools/launchers/make_annotation_task.py --parts N`
        (`--all` або `--stems <list>`), який генерує N структурних папок з
        images/output/selected/polygons + README + START_MASK_PICKER.bat.
        Результат — N окремих ZIP-ів у `<workspace>/_split/`.

        Body: {"n": 3, "stems": [...]}  # N ∈ [2, 20]; stems optional —
              без нього розбиваються ВСІ фото.
        Response: {"ok": true, "n": 3, "out_dir": "...", "zips": [...]}
        """
        if cfg.workspace_dir is None:
            return jsonify({"error": "Не в workspace-режимі."}), 400
        body = request.get_json(silent=True) or {}
        try:
            n = int(body.get("n") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "n повинен бути числом"}), 400
        if n < 2 or n > 20:
            return jsonify({"error": "n повинен бути у межах 2..20"}), 400
        # Day 8.5: опційний список stem-ів (Export з опцією Split).
        split_stems = [str(s) for s in (body.get("stems") or []) if s]

        # Знайти make_annotation_task.py: апксі від routes/ → apps/mask_picker/ →
        # apps/ → project root → tools/launchers/. Підтримує запуск як з
        # проєкту, так і з send-bundle (де runtime у корені бандла).
        here = Path(__file__).resolve().parent
        mat_py = None
        for parent in [here] + list(here.parents):
            candidate = parent / "tools" / "launchers" / "make_annotation_task.py"
            if candidate.exists():
                mat_py = candidate
                break
        if mat_py is None:
            return jsonify({
                "error": "make_annotation_task.py не знайдено. Перевір що tools/launchers/ лежить поряд з apps/."
            }), 500

        out_dir = cfg.workspace_dir / "_split"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable, str(mat_py),
            "--data-dir", str(cfg.workspace_dir),
            "--output-dir", str(out_dir),
            "--parts", str(n),
        ]
        if split_stems:
            cmd += ["--stems", ",".join(split_stems)]
        else:
            cmd += ["--all"]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            return jsonify({"error": "split timeout (>10хв) — занадто багато фото?"}), 500
        except Exception as e:
            return jsonify({"error": f"subprocess failed: {e}"}), 500

        if result.returncode != 0:
            return jsonify({
                "error": "make_annotation_task завершився з помилкою",
                "stderr": result.stderr[-2000:] if result.stderr else "",
                "stdout": result.stdout[-2000:] if result.stdout else "",
            }), 500

        zips = sorted(
            p.name for p in out_dir.glob("*.zip") if p.is_file()
        )
        return jsonify({
            "ok": True,
            "n": n,
            "out_dir": str(out_dir),
            "zips": zips,
            "stdout_tail": result.stdout[-500:] if result.stdout else "",
        })

    # -----------------------------------------------------------------------
    # Day 7 — Save All: batch bake усіх dirty фото у фоні з progress.
    # -----------------------------------------------------------------------

    @bp.route("/api/workspace/bake-all", methods=["POST"])
    def api_workspace_bake_all():
        """
        Запікає у selected/ усі фото з незапеченими змінами (dirty).

        Body (optional):
          {"all": true}   — перепекти ВСІ обрані фото, не лише dirty
                            (для випадку коли змінили labels.json і треба
                             перерахувати весь датасет).

        Запускає bake у фоновому потоці й одразу повертає. Прогрес — через
        GET /api/workspace/bake-progress.

        Response: {"ok": true, "started": bool, "photo_total": int,
                   "stems": [...], "message": str}
        """
        body = request.get_json(silent=True) or {}
        rebake_all = bool(body.get("all"))
        explicit_stems = body.get("stems")

        with _BAKE_LOCK:
            if _BAKE_PROGRESS["running"]:
                return jsonify({
                    "ok": False,
                    "error": "Запікання вже триває — дочекайся завершення.",
                }), 409

        if explicit_stems:
            # Day 8.5: явний список (Export з опцією Finalize) — лише ті, що
            # мають обрану модель.
            stems = sorted(
                str(s) for s in explicit_stems
                if (state.get(str(s)) or {}).get("model")
            )
        elif rebake_all:
            stems = sorted(
                s for s, e in state.all().items()
                if e.get("status") == "selected" and e.get("model")
            )
        else:
            stems = state.list_dirty()

        if not stems:
            msg = ("Немає обраних фото для запікання."
                   if rebake_all else "Немає незапечених змін — усе вже актуальне.")
            return jsonify({
                "ok": True, "started": False, "photo_total": 0,
                "stems": [], "message": msg,
            })

        # Reset progress і старт фонового потоку.
        with _BAKE_LOCK:
            _BAKE_PROGRESS.update({
                "running": True,
                "done": False,
                "photo_done": 0,
                "photo_total": len(stems),
                "current_stem": "",
                "phase": "Старт…",
                "phase_frac": 0.0,
                "ok_count": 0,
                "skipped": [],
                "errors": [],
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
            })

        t = threading.Thread(
            target=_run_bake_all,
            args=(cfg, state, catalog, stems),
            daemon=True,
        )
        t.start()

        return jsonify({
            "ok": True,
            "started": True,
            "photo_total": len(stems),
            "stems": stems,
            "mode": "all" if rebake_all else "dirty",
        })

    @bp.route("/api/workspace/bake-progress")
    def api_workspace_bake_progress():
        """Поточний стан Save All (polling кожні ~400мс з фронта)."""
        return jsonify(_bake_progress_snapshot())

    # -----------------------------------------------------------------------
    # Day 8 — selective merge import (scan → range-picker → apply).
    # -----------------------------------------------------------------------

    @bp.route("/api/workspace/import-scan", methods=["POST"])
    def api_workspace_import_scan():
        """
        Фаза 1 merge-import: приймає файли (multipart `files`), розпаковує
        ZIP-и у `<workspace>/_tmp/import_scan_<id>/src<idx>/` і повертає, які
        фото містить кожне джерело. Фронт показує range-picker, далі —
        /api/workspace/import-apply.

        Response: {ok, scan_id, sources: [{idx, name, stems:[...], count}]}
        """
        if cfg.workspace_dir is None:
            return jsonify({"error": "Не в workspace-режимі."}), 400
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "Файли не передано (поле 'files')."}), 400

        scan_id = datetime.now().strftime("%Y%m%d%H%M%S")
        scan_root = cfg.workspace_dir / "_tmp" / f"import_scan_{scan_id}"
        if scan_root.exists():
            shutil.rmtree(scan_root)
        scan_root.mkdir(parents=True, exist_ok=True)

        sources = []
        for idx, f in enumerate(files):
            name = Path(f.filename or "").name or f"file{idx}"
            ext = Path(name).suffix.lower()
            src_dir = scan_root / f"src{idx}"
            src_dir.mkdir(parents=True, exist_ok=True)
            if ext == ".zip":
                try:
                    with zipfile.ZipFile(io.BytesIO(f.read())) as zf:
                        zf.extractall(src_dir)
                except zipfile.BadZipFile as e:
                    sources.append({"idx": idx, "name": name, "stems": [],
                                    "count": 0, "error": f"пошкоджений ZIP: {e}"})
                    continue
                stems = _scan_stems_in_dir(src_dir)
            elif ext in _IMPORT_IMG_EXTS:
                f.save(str(src_dir / name))
                stems = [_norm_stem(Path(name).stem)]
            else:
                sources.append({"idx": idx, "name": name, "stems": [],
                                "count": 0, "error": f"непідтримуване {ext}"})
                continue
            sources.append({"idx": idx, "name": name, "stems": stems,
                            "count": len(stems)})

        return jsonify({"ok": True, "scan_id": scan_id, "sources": sources})

    @bp.route("/api/workspace/import-apply", methods=["POST"])
    def api_workspace_import_apply():
        """
        Фаза 2 merge-import: застосовує вибрані фото з кожного джерела.

        Body: {scan_id, picks: [{idx, stems: [...]}]}

        Merge-семантика «newest wins» (рішення автора 2026-05-19):
        - фото зі змінами vs без змін (або відсутнє) → береться зі змінами;
        - обидва зі змінами → новіший за `ts`;
        - фото, яких нема у picks — у workspace недоторкані.
        Файли stem копіюються ТІЛЬКИ якщо переміг imported.

        Response: {ok, files_copied, merged_stems, merged_count, kept_local}
        """
        if cfg.workspace_dir is None:
            return jsonify({"error": "Не в workspace-режимі."}), 400
        body = request.get_json(silent=True) or {}
        scan_id = str(body.get("scan_id") or "")
        picks = body.get("picks") or []
        scan_root = cfg.workspace_dir / "_tmp" / f"import_scan_{scan_id}"
        if not scan_id or not scan_root.exists():
            return jsonify({
                "error": "scan застарів або не знайдений — повтори імпорт."
            }), 400

        ws_selections = cfg.selected_dir.parent / "selections.json"
        current: dict = {}
        if ws_selections.exists():
            try:
                current = json.loads(ws_selections.read_text(encoding="utf-8-sig")) or {}
            except Exception:
                current = {}

        total_files = 0
        merged: list[str] = []      # imported переміг → застосовано
        kept_local: list[str] = []  # local новіший → залишено
        try:
            for pick in picks:
                try:
                    idx = int(pick.get("idx"))
                except (TypeError, ValueError):
                    continue
                wanted = sorted({str(s) for s in (pick.get("stems") or []) if s})
                src_dir = scan_root / f"src{idx}"
                if not src_dir.exists() or not wanted:
                    continue
                imported_sel = _read_scan_selections(src_dir)
                for stem in wanted:
                    imp = imported_sel.get(stem)
                    loc = current.get(stem)
                    winner = _decide_import_winner(imp, loc)
                    if winner == "imported":
                        total_files += _copy_stem_artifacts(
                            src_dir, cfg.workspace_dir, stem)
                        if imp is not None:
                            current[stem] = imp
                        merged.append(stem)
                    else:
                        kept_local.append(stem)
        finally:
            shutil.rmtree(scan_root, ignore_errors=True)

        tmp = ws_selections.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ws_selections)

        # Перевідкрити моделі (нові output/<model>/) + перечитати state.
        cfg.models = _discover_models(cfg.output_root)
        state.reload(ws_selections)
        catalog.invalidate()

        merged_unique = sorted(set(merged))
        return jsonify({
            "ok": True,
            "files_copied": total_files,
            "merged_stems": merged_unique,
            "merged_count": len(merged_unique),
            "kept_local": sorted(set(kept_local)),
        })

    return bp
