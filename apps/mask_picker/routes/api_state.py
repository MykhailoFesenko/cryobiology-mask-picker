"""State routes: select, skip, exclude, unset, hard-reset, stats, image."""
from __future__ import annotations

import shutil
import traceback

from flask import Blueprint, abort, jsonify, request, send_file

from state import (
    Config,
    IMAGE_EXTS,
    StateStore,
    _copy_model_files_for_stem,
    _utc_iso,
)
from catalog import CatalogService
from cleanup import (
    BACKUP_KEEP,
    _backup_dir_for_stem,
    _find_existing_selected,
    _make_backup,
    _rotate_backups,
)
from polygons import (
    POLYGON_BACKUP_KEEP,
    _backup_polygons,
    _polygons_backup_dir,
    _polygons_path,
)
from groups import (
    GROUPS_BACKUP_KEEP,
    _backup_groups,
    _groups_backup_dir,
    _groups_path,
)


def make_blueprint(cfg: Config, state: StateStore, catalog: CatalogService) -> Blueprint:
    bp = Blueprint("api_state", __name__)

    @bp.route("/api/image/<stem>")
    def api_image(stem: str):
        # знайти фізичний файл картинки
        for ext in IMAGE_EXTS:
            for candidate in (stem, f"Копия {stem}"):
                p = cfg.images_dir / f"{candidate}{ext}"
                if p.exists():
                    return send_file(p)
        abort(404)

    @bp.route("/api/select", methods=["POST"])
    def api_select():
        """
        Body: { "stem": "db_img_0277", "model": "cyto2", "user": "annotator" }
        Копіює файли вибраної моделі у selected/<model>/<format>/.
        """
        body = request.get_json(force=True) or {}
        stem = body.get("stem")
        model_name = body.get("model")
        user = body.get("user") or "anonymous"
        if not stem or not model_name:
            return jsonify({"error": "stem and model required"}), 400
        m = next((mm for mm in cfg.models if mm.name == model_name), None)
        if not m:
            return jsonify({"error": f"unknown model {model_name}"}), 400

        # Якщо юзер перемикає model — викидаємо застарілий cleanup
        # (він прив'язаний до конкретних instance IDs іншої моделі).
        prev = state.get(stem) or {}
        prev_cleanup = prev.get("cleanup") or {}
        carry_cleanup = None
        if prev_cleanup.get("model") == model_name:
            carry_cleanup = prev_cleanup  # та сама модель — зберігаємо rejected

        copied = _copy_model_files_for_stem(cfg, m, stem)

        entry = {
            "status": "selected",
            "model": model_name,
            "ts": _utc_iso(),
            "user": user,
            "copied_files": copied,
        }
        if carry_cleanup is not None:
            entry["cleanup"] = carry_cleanup
        state.set(stem, entry)

        # Day 7 lazy-bake: Pick більше НЕ запікає одразу. selected/ зараз містить
        # raw output моделі. Якщо для stem уже є polygons.json або cleanup
        # rejected — це означає, що raw output ≠ бажаний результат → dirty,
        # юзер запече через Save All. Чисте фото (перший Pick, без правок) —
        # raw output і є фінал → не dirty.
        has_polys = bool(
            cfg.polygons_dir and (cfg.polygons_dir / f"{stem}.json").exists()
        )
        has_rejected = bool((entry.get("cleanup") or {}).get("rejected_instances"))
        if has_polys or has_rejected:
            state.mark_dirty(stem)
        return jsonify({
            "ok": True,
            "copied": copied,
            "dirty": state.is_dirty(stem),
        })

    @bp.route("/api/skip", methods=["POST"])
    def api_skip():
        """Позначити фото як 'не підходить для датасету'."""
        body = request.get_json(force=True) or {}
        stem = body.get("stem")
        reason = body.get("reason") or ""
        user = body.get("user") or "anonymous"
        if not stem:
            return jsonify({"error": "stem required"}), 400
        # створимо порожній файл-маркер у skipped/
        cfg.skipped_dir.mkdir(parents=True, exist_ok=True)
        marker = cfg.skipped_dir / f"{stem}.skipped.txt"
        marker.write_text(
            f"stem: {stem}\nuser: {user}\nreason: {reason}\nts: {_utc_iso()}\n",
            encoding="utf-8",
        )
        state.set(stem, {
            "status": "skipped",
            "model": None,
            "ts": _utc_iso(),
            "user": user,
            "reason": reason,
        })
        return jsonify({"ok": True})

    @bp.route("/api/exclude/<stem>", methods=["POST"])
    def api_exclude(stem: str):
        """v1.6.6: Перенести фото з images/ у _excluded/, позначити status='excluded'.

        Не видаляє назавжди — файл лежить у _excluded/ для ручного відновлення.
        Catalog сканує _excluded/ окремо, фільтр 'excluded' показує їх; в інших
        фільтрах і в лічильнику зверху ці items приховуються.
        """
        if not stem:
            return jsonify({"error": "stem required"}), 400
        body = request.get_json(silent=True) or {}
        user = body.get("user") or "anonymous"

        excluded_dir = cfg.images_dir.parent / "_excluded"
        excluded_dir.mkdir(parents=True, exist_ok=True)
        moved = []
        # Переносимо і основний файл, і "Копия " варіант якщо є.
        for s in (stem, f"Копия {stem}"):
            for ext in IMAGE_EXTS:
                p = cfg.images_dir / f"{s}{ext}"
                if p.exists():
                    dst = excluded_dir / p.name
                    try:
                        shutil.move(str(p), str(dst))
                        moved.append(p.name)
                    except OSError as e:
                        return jsonify({"error": f"move failed: {e}"}), 500
        if not moved:
            return jsonify({"error": f"no image files found for stem {stem}"}), 404

        state.set(stem, {
            "status": "excluded",
            "model": None,
            "ts": _utc_iso(),
            "user": user,
        })
        catalog.invalidate()
        return jsonify({"ok": True, "moved": moved})

    @bp.route("/api/restore/<stem>", methods=["POST"])
    def api_restore(stem: str):
        """
        Day 8.5: повернути помилково виключене фото з `_excluded/` назад у
        `images/`. Знімає status='excluded' (фото знову стає unreviewed).
        """
        if not stem:
            return jsonify({"error": "stem required"}), 400
        excluded_dir = cfg.images_dir.parent / "_excluded"
        if not excluded_dir.exists():
            return jsonify({"error": "немає _excluded/ — нічого повертати"}), 404

        moved = []
        for s in (stem, f"Копия {stem}"):
            for ext in IMAGE_EXTS:
                p = excluded_dir / f"{s}{ext}"
                if p.exists():
                    dst = cfg.images_dir / p.name
                    try:
                        shutil.move(str(p), str(dst))
                        moved.append(p.name)
                    except OSError as e:
                        return jsonify({"error": f"move failed: {e}"}), 500
        if not moved:
            return jsonify({"error": f"у _excluded/ нема файлів для {stem}"}), 404

        # Прибрати excluded-стан — фото знову у звичайному пулі.
        if state.get(stem):
            state.remove(stem)
        catalog.invalidate()
        return jsonify({"ok": True, "restored": moved, "stem": stem})

    @bp.route("/api/bulk-user", methods=["POST"])
    def api_bulk_user():
        """
        Day 8.5: bulk-призначення анотатора діапазону фото.

        Body: {"stems": ["db_img_0084", ...], "user": "annotator"}
        Записує `user` у selections.json для кожного stem, що має entry
        (тобто фото з рішенням — Pick або Skip). Фото без рішення
        пропускаються (user = хто прийняв рішення, тут його ще нема).
        """
        body = request.get_json(force=True) or {}
        stems = body.get("stems") or []
        user = str(body.get("user") or "").strip()
        if not user:
            return jsonify({"error": "user (ім'я анотатора) обов'язкове"}), 400
        if not isinstance(stems, list) or not stems:
            return jsonify({"error": "stems: непорожній список обов'язковий"}), 400

        updated, skipped = [], []
        for stem in stems:
            entry = state.get(stem)
            if entry is None:
                skipped.append(str(stem))
                continue
            entry["user"] = user
            state.set(stem, entry)
            updated.append(str(stem))
        catalog.invalidate()
        return jsonify({
            "ok": True, "user": user,
            "updated": updated, "count": len(updated),
            "skipped": skipped,
        })

    @bp.route("/api/unset", methods=["POST"])
    def api_unset():
        """Скинути рішення по фото (НЕ видаляє скопійовані файли — команда обережна)."""
        body = request.get_json(force=True) or {}
        stem = body.get("stem")
        if not stem:
            return jsonify({"error": "stem required"}), 400
        state.remove(stem)
        return jsonify({"ok": True})

    @bp.route("/api/hard-reset/<stem>", methods=["POST"])
    def api_hard_reset(stem: str):
        """
        Day 3c′ CP6: hard reset — видаляє ВСІ ручні правки для stem.

        - selections.json[stem] (status + cleanup rejected + base_label) — cleared
        - polygons/<stem>.json — removed (backup у polygons/_backups/<stem>/<ts>/)
        - groups/<stem>.json — removed (backup у groups/_backups/<stem>/<ts>/).
          Групи — теж ручна правка; без видалення вони лишались би orphan
          (посилались на щойно видалені polygon-инстанси).
        - selected/<model>/<fmt>/<stem>.* — removed для усіх моделей
          (backup у selected/<model>/_backups/<stem>/<ts>/)

        Original images/ та output/<model>/npy/ — НЕ торкаються (read-only).
        Після hard-reset → наступний Pick відновить selected/ через
        Day 3a auto-rebake з raw output/ (без manual polygon overlay).

        Returns: {ok, removed: {state, polygons, groups, selected_models}}
        """
        try:
            removed = {
                "state": False,
                "polygons": False,
                "groups": False,
                "selected_models": [],
            }

            if state.get(stem):
                state.remove(stem)
                removed["state"] = True

            pj = _polygons_path(cfg.polygons_dir, stem)
            if pj.exists():
                _backup_polygons(cfg.polygons_dir, stem)
                _rotate_backups(
                    _polygons_backup_dir(cfg.polygons_dir, stem),
                    keep=POLYGON_BACKUP_KEEP,
                )
                pj.unlink()
                removed["polygons"] = True

            if cfg.groups_dir:
                gj = _groups_path(cfg.groups_dir, stem)
                if gj.exists():
                    _backup_groups(cfg.groups_dir, stem)
                    _rotate_backups(
                        _groups_backup_dir(cfg.groups_dir, stem),
                        keep=GROUPS_BACKUP_KEEP,
                    )
                    gj.unlink()
                    removed["groups"] = True

            for m in cfg.models:
                existing = _find_existing_selected(cfg.selected_dir, m.name, stem)
                if not existing:
                    continue
                _make_backup(cfg.selected_dir, m.name, stem)
                _rotate_backups(
                    _backup_dir_for_stem(cfg.selected_dir, m.name, stem),
                    keep=BACKUP_KEEP,
                )
                for fmt, src in existing.items():
                    try:
                        src.unlink()
                    except OSError:
                        pass
                removed["selected_models"].append(m.name)

            catalog.invalidate()
            return jsonify({"ok": True, "removed": removed, "stem": stem})
        except Exception as e:
            print(f"[hard-reset] {stem}: {e}")
            traceback.print_exc()
            return jsonify({"error": f"hard-reset failed: {e}"}), 500

    @bp.route("/api/stats")
    def api_stats():
        items = catalog.get(fresh=True)
        total = len(items)
        by_model: dict[str, int] = {}
        # Day 8: per-user статистика — {user: {selected, skipped, dirty}}.
        by_user: dict[str, dict] = {}
        skipped = 0
        unreviewed = 0
        dirty = 0

        def _user_bucket(name: str) -> dict:
            return by_user.setdefault(
                name or "—", {"selected": 0, "skipped": 0, "dirty": 0}
            )

        for it in items:
            st = it.get("state")
            if not st:
                unreviewed += 1
                continue
            if st.get("dirty"):
                dirty += 1
            if st["status"] == "skipped":
                skipped += 1
                b = _user_bucket(st.get("user"))
                b["skipped"] += 1
            elif st["status"] == "selected":
                by_model[st["model"]] = by_model.get(st["model"], 0) + 1
                b = _user_bucket(st.get("user"))
                b["selected"] += 1
                if st.get("dirty"):
                    b["dirty"] += 1
        return jsonify({
            "total": total,
            "unreviewed": unreviewed,
            "skipped": skipped,
            "dirty": dirty,
            "by_model": by_model,
            "by_user": by_user,
            "reviewed": total - unreviewed,
        })

    return bp
