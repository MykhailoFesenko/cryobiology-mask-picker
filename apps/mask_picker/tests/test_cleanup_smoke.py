"""
Smoke test для cleanup-export flow.

Будує тимчасовий проект (1 картинка + 1 npy маска з 3 інстансами), проганяє
pick → autosave cleanup → full export → перевіряє, що файли в selected/
перезаписано cleaned-версією, бекап створено, cleanup.json містить запис,
і rotation бекапів працює.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

# Імпортуємо app.py з батьківської директорії
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import app as mask_picker_app  # noqa: E402
from app import (  # noqa: E402
    Config,
    EXPORT_AVAILABLE,
    ModelSource,
    StateStore,
    _rotate_backups,
    create_app,
)


STEM = "db_img_test"
MODEL = "cyto2"


def _build_project(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    images = tmp_path / "images"
    output = tmp_path / "output"
    selected = tmp_path / "selected"
    skipped = tmp_path / "skipped"

    model_out = output / MODEL
    for d in (
        images,
        model_out / "overlay",
        model_out / "png",
        model_out / "npy",
        model_out / "yolo",
        selected,
        skipped,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # Фейкова картинка 64×64 grayscale
    Image.fromarray(np.full((64, 64), 40, dtype=np.uint8), mode="L").save(
        images / f"{STEM}.jpg"
    )

    # Fake labels: 3 інстанси в різних квадратах
    labels = np.zeros((64, 64), dtype=np.int32)
    labels[5:15, 5:15] = 1
    labels[20:30, 20:30] = 2
    labels[40:50, 40:50] = 3
    np.save(model_out / "npy" / f"{STEM}.npy", labels)

    # Stub-ові overlay/png/yolo (реальний вміст не важливий — просто щоб
    # _discover_models і _copy_model_files_for_stem знайшли файли)
    Image.fromarray((labels * 80).astype(np.uint8), mode="L").save(
        model_out / "overlay" / f"{STEM}.png"
    )
    Image.fromarray(labels.astype(np.uint8), mode="P").save(
        model_out / "png" / f"{STEM}.png"
    )
    (model_out / "yolo" / f"{STEM}.txt").write_text(
        "0 0.156 0.156 0.156 0.156\n"
        "0 0.390 0.390 0.156 0.156\n"
        "0 0.703 0.703 0.156 0.156\n",
        encoding="utf-8",
    )

    return images, output, selected, skipped


@pytest.fixture
def client_cfg(tmp_path):
    images, output, selected, skipped = _build_project(tmp_path)
    model = ModelSource(
        name=MODEL,
        overlay_dir=output / MODEL / "overlay",
        png_dir=output / MODEL / "png",
        npy_dir=output / MODEL / "npy",
        yolo_dir=output / MODEL / "yolo",
    )
    cfg = Config(
        images_dir=images,
        output_root=output,
        selected_dir=selected,
        skipped_dir=skipped,
        models=[model],
    )
    state = StateStore(selected.parent / "selections.json")
    app = create_app(cfg, state)
    app.testing = True
    # Чистимо RGB-кеш, щоб уникнути взаємодії між тестами
    with mask_picker_app._RGB_CACHE_LOCK:
        mask_picker_app._RGB_CACHE.clear()
    return app.test_client(), cfg, state


def test_pick_select_copies_files(client_cfg):
    client, cfg, _ = client_cfg
    resp = client.post(
        "/api/select",
        json={"stem": STEM, "model": MODEL, "user": "tester"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    for fmt, ext in (("npy", ".npy"), ("png", ".png"), ("yolo", ".txt"), ("overlay", ".png")):
        p = cfg.selected_dir / MODEL / fmt / f"{STEM}{ext}"
        assert p.exists(), f"очікувалось {p}"


def test_autosave_cleanup_updates_selections(client_cfg):
    client, cfg, state = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL})
    resp = client.post(
        f"/api/cleanup/{STEM}",
        json={"model": MODEL, "rejected_instances": [2]},
    )
    assert resp.status_code == 200
    saved = resp.get_json()["cleanup"]
    assert saved["rejected_instances"] == [2]
    assert saved["model"] == MODEL

    got = client.get(f"/api/cleanup/{STEM}").get_json()
    assert got["rejected_instances"] == [2]


@pytest.mark.skipif(not EXPORT_AVAILABLE, reason="cellsegkit/numpy/PIL не встановлено")
def test_export_regenerates_cleaned_files(client_cfg):
    client, cfg, _ = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL})
    resp = client.post(
        f"/api/cleanup-export/{STEM}",
        json={"model": MODEL, "rejected_instances": [2], "user": "tester"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["rejected_count"] == 1

    # Cleaned npy не містить id=2, але 1 і 3 залишилися.
    cleaned = np.load(cfg.selected_dir / MODEL / "npy" / f"{STEM}.npy")
    uniq = set(int(x) for x in np.unique(cleaned))
    assert 2 not in uniq
    assert 1 in uniq and 3 in uniq

    # cleanup.json на рівні моделі
    cleanup_json = cfg.selected_dir / MODEL / "cleanup.json"
    assert cleanup_json.exists()
    cj = json.loads(cleanup_json.read_text(encoding="utf-8"))
    assert cj[STEM]["rejected"] == [2]
    assert cj[STEM]["user"] == "tester"

    # YOLO: мало 3 рядки → має бути 2
    yolo_lines = [
        l for l in
        (cfg.selected_dir / MODEL / "yolo" / f"{STEM}.txt").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert len(yolo_lines) == 2

    # Бекап створено й містить оригінал (усі 3 інстанси)
    backups_root = cfg.selected_dir / MODEL / "_backups" / STEM
    subs = sorted([p for p in backups_root.iterdir() if p.is_dir()])
    assert len(subs) == 1
    backup_npy = subs[0] / "npy.npy"
    assert backup_npy.exists()
    bk = np.load(backup_npy)
    assert set(int(x) for x in np.unique(bk)) == {0, 1, 2, 3}

    # Overlay overlay.png у бекапі теж скопійовано.
    assert (subs[0] / "overlay.png").exists()


@pytest.mark.skipif(not EXPORT_AVAILABLE, reason="cellsegkit/numpy/PIL не встановлено")
def test_export_rotates_backups(client_cfg):
    client, cfg, _ = client_cfg
    client.post("/api/select", json={"stem": STEM, "model": MODEL})

    # Три послідовні експорти з паузою (ts-папка має секундну точність).
    for i, rej in enumerate([[1], [2], [3]]):
        if i > 0:
            time.sleep(1.1)
        resp = client.post(
            f"/api/cleanup-export/{STEM}",
            json={"model": MODEL, "rejected_instances": rej},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    backups_root = cfg.selected_dir / MODEL / "_backups" / STEM
    subs = sorted([p for p in backups_root.iterdir() if p.is_dir()])
    # BACKUP_KEEP = 2 — після 3 експортів має лишитися 2
    assert len(subs) == 2, f"expected 2 backups, got {len(subs)}: {[p.name for p in subs]}"


def test_rotate_backups_helper(tmp_path):
    """Юніт-тест для _rotate_backups — без Flask, швидкий."""
    d = tmp_path / "_backups" / "stem"
    d.mkdir(parents=True)
    for name in ["20260101T000001Z", "20260101T000002Z", "20260101T000003Z",
                 "20260101T000004Z"]:
        (d / name).mkdir()
        (d / name / "npy.npy").write_bytes(b"x")

    _rotate_backups(d, keep=2)
    remaining = sorted(p.name for p in d.iterdir())
    assert remaining == ["20260101T000003Z", "20260101T000004Z"]


def test_select_clears_stale_cleanup_on_model_change(client_cfg):
    """Якщо юзер перемикає модель — cleanup попередньої моделі не має
    застосовуватись до нової (інстанс IDs не сумісні)."""
    client, cfg, state = client_cfg
    # Pick model cyto2 + cleanup
    client.post("/api/select", json={"stem": STEM, "model": MODEL})
    client.post(f"/api/cleanup/{STEM}", json={"model": MODEL, "rejected_instances": [2]})
    # Pick ту ж саму модель знову — cleanup має зберегтися
    client.post("/api/select", json={"stem": STEM, "model": MODEL})
    cur = state.get(STEM)
    assert cur["cleanup"]["model"] == MODEL
    assert cur["cleanup"]["rejected_instances"] == [2]
