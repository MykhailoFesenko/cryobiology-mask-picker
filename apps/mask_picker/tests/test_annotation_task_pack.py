from __future__ import annotations

import argparse
import importlib.util
import json
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "launchers" / "make_annotation_task.py"

spec = importlib.util.spec_from_file_location("make_annotation_task", SCRIPT)
make_annotation_task = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(make_annotation_task)


def _build_workspace(root: Path) -> Path:
    data_dir = root / "data"
    for rel in [
        "images",
        "output/model_a/overlay",
        "output/model_a/npy",
        "output/model_a/png",
        "output/model_a/yolo",
        "output/model_b/overlay",
        "output/model_b/npy",
        "output/model_b/png",
        "output/model_b/yolo",
        "selected/model_a/overlay",
        "selected/model_a/npy",
        "selected/model_a/png",
        "selected/model_a/yolo",
        "polygons",
        "skipped",
    ]:
        (data_dir / rel).mkdir(parents=True, exist_ok=True)

    for stem in ["img_001", "img_002", "img_003"]:
        Image.fromarray(np.full((16, 16), 40, dtype=np.uint8), mode="L").save(
            data_dir / "images" / f"{stem}.jpg"
        )
        labels = np.zeros((16, 16), dtype=np.int32)
        labels[2:8, 2:8] = 1
        for model in ["model_a", "model_b"]:
            Image.fromarray(np.full((16, 16), 80, dtype=np.uint8), mode="L").save(
                data_dir / "output" / model / "overlay" / f"{stem}.png"
            )
            Image.fromarray(labels.astype(np.uint8), mode="L").save(
                data_dir / "output" / model / "png" / f"{stem}.png"
            )
            np.save(data_dir / "output" / model / "npy" / f"{stem}.npy", labels)
            (data_dir / "output" / model / "yolo" / f"{stem}.txt").write_text(
                "0 0.5 0.5 0.5 0.5\n",
                encoding="utf-8",
            )

    (data_dir / "labels.json").write_text(
        json.dumps([
            {"id": 1, "name": "nucleus", "color": "#4488ff", "shortcut": "1"},
            {"id": 2, "name": "vesicle", "color": "#ff8844", "shortcut": "2"},
        ]),
        encoding="utf-8",
    )
    (data_dir / "selections.json").write_text(
        json.dumps({
            "img_001": {"status": "selected", "model": "model_a"},
            "img_003": {"status": "skipped", "model": None},
        }),
        encoding="utf-8",
    )
    (data_dir / "polygons" / "img_001.json").write_text(
        json.dumps({"shapes": [{"label": "nucleus"}, {"label": "vesicle"}]}),
        encoding="utf-8",
    )
    (data_dir / "selected" / "model_a" / "cleanup.json").write_text(
        json.dumps({"img_001": {"rejected": [1]}, "img_003": {"rejected": [2]}}),
        encoding="utf-8",
    )
    return data_dir


def test_prepare_task_copies_subset_model_outputs_and_filters_state(tmp_path):
    data_dir = _build_workspace(tmp_path)
    task_dir = tmp_path / "task"

    manifest = make_annotation_task.prepare_task(
        data_dir,
        task_dir,
        ["img_001", "img_002"],
        readme_source=None,
    )

    assert manifest["stems"] == ["img_001", "img_002"]
    assert manifest["counts"]["images"] == 2
    assert manifest["counts"]["polygons"] == 1
    assert manifest["counts"]["output_files_by_model"] == {"model_a": 8, "model_b": 8}
    assert (task_dir / "images" / "img_001.jpg").exists()
    assert (task_dir / "START_MASK_PICKER.bat").exists()
    assert "--workspace" in (task_dir / "START_MASK_PICKER.bat").read_text(encoding="utf-8")
    assert (task_dir / "output" / "model_b" / "npy" / "img_002.npy").exists()
    assert not (task_dir / "images" / "img_003.jpg").exists()
    assert not (task_dir / "output" / "model_a" / "overlay" / "img_003.png").exists()
    assert manifest["source_data_dir"] == data_dir.name

    selections = json.loads((task_dir / "selections.json").read_text(encoding="utf-8"))
    assert set(selections) == {"img_001"}
    cleanup = json.loads((task_dir / "selected" / "model_a" / "cleanup.json").read_text(encoding="utf-8"))
    assert cleanup == {"img_001": {"rejected": [1]}}


def test_cli_parts_create_separate_zip_packages(tmp_path):
    data_dir = _build_workspace(tmp_path)
    output_dir = tmp_path / "out"
    args = argparse.Namespace(
        all=True,
        stems=None,
        list=None,
        parts=2,
    )
    stems = make_annotation_task.resolve_stems(data_dir, args)
    assert stems == ["img_001", "img_002", "img_003"]
    assert make_annotation_task.chunks(stems, 2) == [["img_001", "img_002"], ["img_003"]]

    code = make_annotation_task.main([
        "--data-dir", str(data_dir),
        "--all",
        "--parts", "2",
        "--name", "vesicles_test",
        "--output-dir", str(output_dir),
    ])
    assert code == 0
    zips = sorted(output_dir.glob("*.zip"))
    assert [p.name for p in zips] == [
        "vesicles_test_part01_of_02.zip",
        "vesicles_test_part02_of_02.zip",
    ]
    with zipfile.ZipFile(zips[0]) as zf:
        names = set(zf.namelist())
    assert "vesicles_test_part01_of_02/MANIFEST.json" in names
    assert "vesicles_test_part01_of_02/output/model_a/overlay/img_001.png" in names
    assert "vesicles_test_part01_of_02/images/img_003.jpg" not in names
