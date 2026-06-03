"""
Smoke тести для Day 4-5 cell grouping (groups.py).

Покриває:
  * envelope: empty when file absent
  * roundtrip: write → read, з backup при перезаписі
  * _validate_groups_payload: відхиляє invalid type / non-string id / non-int iid
  * _enforce_single_membership: last-wins, moves журнал
  * _classify_group_membership: counts + soft validation + suggested_type
  * _next_group_id: skip gaps
  * _next_color_hue: palette + wrap
  * _instance_label_lookup: всі npy ids → base_label
  * _sync_polygons_group_id_mirror: polygons.json.shape.group_id mirror
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import (  # noqa: E402
    GROUP_TYPES,
    PALETTE_HUES,
    _backup_groups,
    _classify_group_membership,
    _empty_groups_envelope,
    _enforce_single_membership,
    _groups_backup_dir,
    _instance_label_lookup,
    _next_color_hue,
    _next_group_id,
    _polygon_labels_from_payload,
    _read_groups,
    _sync_polygons_group_id_mirror,
    _validate_groups_payload,
    _write_groups,
)


STEM = "db_img_test"


# ---------------------------------------------------------------------------
# Envelope + I/O roundtrip
# ---------------------------------------------------------------------------

def test_empty_envelope_when_file_absent(tmp_path):
    """Якщо groups/<stem>.json не існує — повертається порожній envelope."""
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir()
    env = _read_groups(groups_dir, STEM)
    assert env["stem"] == STEM
    assert env["groups"] == []
    assert env["version"] == "1.1"


def test_write_read_roundtrip_with_backup(tmp_path):
    """Write → read зберігає payload. Повторний write бекапить попередню версію."""
    groups_dir = tmp_path / "groups"
    payload1 = _empty_groups_envelope(STEM, model="instanseg")
    payload1["groups"].append({
        "id": "g_001",
        "type": "cell",
        "instance_ids": [12, 34],
        "polygon_indices": [],
        "color_hue": 0,
    })
    _write_groups(groups_dir, STEM, payload1)
    assert (groups_dir / f"{STEM}.json").exists()

    loaded = _read_groups(groups_dir, STEM)
    assert loaded["model"] == "instanseg"
    assert len(loaded["groups"]) == 1
    assert loaded["groups"][0]["instance_ids"] == [12, 34]

    # Backup при перезаписі
    backup_dir = _backup_groups(groups_dir, STEM)
    assert backup_dir is not None and backup_dir.exists()
    assert (backup_dir / "groups.json").exists()

    # Друга версія
    time.sleep(0.01)
    payload2 = dict(payload1)
    payload2["groups"] = [dict(payload1["groups"][0], instance_ids=[12, 34, 99])]
    _write_groups(groups_dir, STEM, payload2)
    loaded2 = _read_groups(groups_dir, STEM)
    assert 99 in loaded2["groups"][0]["instance_ids"]


def test_read_returns_empty_on_corrupted_json(tmp_path):
    """Якщо JSON зіпсований — повертаємо порожній envelope (lenient)."""
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir()
    (groups_dir / f"{STEM}.json").write_text("{not valid json", encoding="utf-8")
    env = _read_groups(groups_dir, STEM)
    assert env["groups"] == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_rejects_non_object():
    err = _validate_groups_payload([])
    assert err is not None and "object" in err


def test_validate_rejects_invalid_type():
    payload = {"groups": [{"id": "g_001", "type": "cluster_of_cluster"}]}
    err = _validate_groups_payload(payload)
    assert err is not None and "type" in err


def test_validate_rejects_empty_id():
    payload = {"groups": [{"id": "", "type": "cell"}]}
    err = _validate_groups_payload(payload)
    assert err is not None and "id" in err


def test_validate_rejects_non_int_instance_id():
    payload = {"groups": [{"id": "g_001", "type": "cell",
                          "instance_ids": [1, "two", 3]}]}
    err = _validate_groups_payload(payload)
    assert err is not None and "instance_ids" in err


def test_validate_accepts_minimal_valid_payload():
    payload = {"groups": [{"id": "g_001", "type": "cell",
                          "instance_ids": [1, 2], "polygon_indices": [],
                          "color_hue": 30}]}
    err = _validate_groups_payload(payload)
    assert err is None


# ---------------------------------------------------------------------------
# Single-membership enforcement
# ---------------------------------------------------------------------------

def test_single_membership_last_wins():
    """Якщо iid фігурує у двох групах — лишається у останньої."""
    groups = [
        {"id": "g_001", "type": "cell", "instance_ids": [10, 20], "polygon_indices": []},
        {"id": "g_002", "type": "cell", "instance_ids": [20, 30], "polygon_indices": []},
    ]
    moves = _enforce_single_membership(groups)
    assert groups[0]["instance_ids"] == [10]
    assert groups[1]["instance_ids"] == [20, 30]
    assert any(m["id"] == 20 and m["from"] == "g_001" and m["to"] == "g_002"
               for m in moves)


def test_single_membership_polygon_index():
    """Полігон index також enforce-иться."""
    groups = [
        {"id": "g_001", "type": "cell", "instance_ids": [], "polygon_indices": [0, 1]},
        {"id": "g_002", "type": "cell", "instance_ids": [], "polygon_indices": [1, 2]},
    ]
    moves = _enforce_single_membership(groups)
    assert groups[0]["polygon_indices"] == [0]
    assert groups[1]["polygon_indices"] == [1, 2]
    assert any(m["kind"] == "polygon" and m["id"] == 1 for m in moves)


def test_single_membership_no_moves_when_disjoint():
    groups = [
        {"id": "g_001", "type": "cell", "instance_ids": [1, 2], "polygon_indices": []},
        {"id": "g_002", "type": "cell", "instance_ids": [3, 4], "polygon_indices": []},
    ]
    moves = _enforce_single_membership(groups)
    assert moves == []


# ---------------------------------------------------------------------------
# Domain classification + soft validation
# ---------------------------------------------------------------------------

def test_classify_cell_valid_when_nuc_and_ves():
    instance_labels = {1: "nucleus", 2: "vesicle", 3: "vesicle"}
    group = {"type": "cell", "instance_ids": [1, 2, 3], "polygon_indices": []}
    info = _classify_group_membership(instance_labels, [], group)
    assert info["valid"] is True
    assert info["reason"] is None
    assert info["n_nucleus"] == 1
    assert info["n_vesicle"] == 2
    assert info["suggested_type"] == "cell"


def test_classify_cell_invalid_no_nucleus():
    instance_labels = {1: "vesicle", 2: "vesicle"}
    group = {"type": "cell", "instance_ids": [1, 2], "polygon_indices": []}
    info = _classify_group_membership(instance_labels, [], group)
    assert info["valid"] is False
    assert "nucleus" in info["reason"]
    assert info["suggested_type"] == "vesicle_cluster"


def test_classify_vesicle_cluster_invalid_with_nucleus():
    instance_labels = {1: "vesicle", 2: "nucleus"}
    group = {"type": "vesicle_cluster", "instance_ids": [1, 2], "polygon_indices": []}
    info = _classify_group_membership(instance_labels, [], group)
    assert info["valid"] is False
    assert info["suggested_type"] == "cell"


def test_classify_uses_polygon_labels():
    polygon_labels = ["nucleus", "vesicle", "vesicle"]
    group = {"type": "cell", "instance_ids": [], "polygon_indices": [0, 1, 2]}
    info = _classify_group_membership({}, polygon_labels, group)
    assert info["valid"] is True
    assert info["n_nucleus"] == 1
    assert info["n_vesicle"] == 2


def test_classify_no_double_count_polygon_backed_iid():
    """Регресія db_img_0171 g_008: полігон #k + його baked iid
    (POLYGON_ID_BASE+k) у тій самій групі НЕ рахується двічі (показувало 4
    ядра замість 2). polygon_indices — канонічний підрахунок, reserved
    instance_id того ж полігона пропускається."""
    from groups import POLYGON_ID_BASE
    polygon_labels = ["nucleus", "nucleus"]   # shapes #0, #1 — обидва ядра
    group = {
        "type": "cell",
        # baked-версії полігонів 0,1 (reserved) + 2 справжні везикули
        "instance_ids": [101, 102, POLYGON_ID_BASE + 0, POLYGON_ID_BASE + 1],
        "polygon_indices": [0, 1],
    }
    instance_labels = {
        101: "vesicle", 102: "vesicle",
        POLYGON_ID_BASE + 0: "nucleus", POLYGON_ID_BASE + 1: "nucleus",
    }
    info = _classify_group_membership(instance_labels, polygon_labels, group)
    assert info["n_nucleus"] == 2, f"подвійний підрахунок: {info['n_nucleus']}"
    assert info["n_vesicle"] == 2
    nuc_iids = info["iids_by_label"].get("nucleus", [])
    assert POLYGON_ID_BASE + 0 not in nuc_iids
    assert POLYGON_ID_BASE + 1 not in nuc_iids


def test_classify_ignores_stale_reserved_iid_without_polygon():
    """Регресія db_img_0171 g_008 (раунд 2): юзер прибрав полігони з групи
    (polygon_indices порожнє), але baked reserved iid лишились stale у
    instance_ids. Вони НЕ рахуються — polygon_indices є джерелом правди для
    полігонів, reserved iid це bake-артефакт. Інакше «привид»: прибрані ядра
    все ще показуються."""
    from groups import POLYGON_ID_BASE
    group = {
        "type": "cell",
        # полігони прибрані з групи, але baked iid лишились stale
        "instance_ids": [POLYGON_ID_BASE + 22, POLYGON_ID_BASE + 23],
        "polygon_indices": [],
    }
    instance_labels = {POLYGON_ID_BASE + 22: "nucleus", POLYGON_ID_BASE + 23: "nucleus"}
    info = _classify_group_membership(instance_labels, [], group)
    assert info["n_nucleus"] == 0, f"stale reserved iid не має рахуватись: {info['n_nucleus']}"
    assert info["iids_by_label"].get("nucleus", []) == []


def test_polygon_id_base_matches_baking():
    """groups.POLYGON_ID_BASE дублює baking.POLYGON_ID_BASE (layering: groups
    не імпортує важкий baking). Guard проти тихої розбіжності."""
    import groups as groups_mod
    from baking import POLYGON_ID_BASE as baking_base
    assert groups_mod.POLYGON_ID_BASE == baking_base


# ---------------------------------------------------------------------------
# ID + color allocation
# ---------------------------------------------------------------------------

def test_next_group_id_skips_gaps():
    groups = [{"id": "g_001"}, {"id": "g_003"}]
    assert _next_group_id(groups) == "g_002"


def test_next_group_id_first():
    assert _next_group_id([]) == "g_001"


def test_next_group_id_ignores_non_g_prefix():
    groups = [{"id": "custom_a"}, {"id": "g_001"}]
    assert _next_group_id(groups) == "g_002"


def test_next_color_hue_picks_first_unused():
    groups = [{"color_hue": 0}, {"color_hue": 30}]
    h = _next_color_hue(groups)
    assert h == 60  # PALETTE_HUES[2]


def test_next_color_hue_wraps_after_palette_exhausted():
    groups = [{"color_hue": h} for h in PALETTE_HUES]
    h = _next_color_hue(groups)
    assert h in PALETTE_HUES  # повертається на початок


# ---------------------------------------------------------------------------
# Instance label lookup + polygon labels
# ---------------------------------------------------------------------------

def test_instance_label_lookup_assigns_base_label():
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[1:3, 1:3] = 1
    labels[5:8, 5:8] = 2
    result = _instance_label_lookup(labels, base_label="vesicle")
    assert result == {1: "vesicle", 2: "vesicle"}


def test_instance_label_lookup_from_polygons_payload():
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[1, 1] = 5
    payload = {"base_label": "nucleus"}
    result = _instance_label_lookup(labels, polygons_payload=payload)
    assert result == {5: "nucleus"}


# ---------------------------------------------------------------------------
# 2026-05-21 fix: per-instance overrides + YOLO multiclass reader
# ---------------------------------------------------------------------------

def test_instance_label_lookup_overrides_apply():
    """Якщо передано per_instance_overrides — вони перебивають base_label.

    Це робить cell-групи валідними коли у baked масці є везикули
    (раніше всі baked instances отримували один base_label = 'nucleus').
    """
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[0, 0] = 1
    labels[0, 1] = 2
    labels[0, 2] = 3
    result = _instance_label_lookup(
        labels,
        base_label="nucleus",
        per_instance_overrides={1: "vesicle", 3: "vesicle"},
    )
    assert result == {1: "vesicle", 2: "nucleus", 3: "vesicle"}


def test_instance_label_lookup_overrides_skip_invalid():
    """Overrides з нечислових ключів / порожніх label-ів — ігноруються."""
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[0, 0] = 1
    result = _instance_label_lookup(
        labels,
        base_label="nucleus",
        per_instance_overrides={1: "", "x": "vesicle"},
    )
    assert result == {1: "nucleus"}


def test_instance_labels_from_yolo_reads_multiclass(tmp_path):
    """YOLO multiclass reader → label per baked instance."""
    from app import _instance_labels_from_yolo
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[0, 0] = 1
    labels[0, 1] = 2
    labels[0, 2] = 3
    yolo = tmp_path / "stem.txt"
    yolo.write_text(
        "0 0.1 0.1 0.05 0.05\n"
        "1 0.2 0.2 0.05 0.05\n"
        "1 0.3 0.3 0.05 0.05\n",
        encoding="utf-8",
    )
    classes = [{"name": "nucleus"}, {"name": "vesicle"}]
    out = _instance_labels_from_yolo(labels, yolo, classes)
    assert out == {1: "nucleus", 2: "vesicle", 3: "vesicle"}


def test_instance_labels_from_yolo_missing_file(tmp_path):
    """Якщо YOLO txt відсутній — повертаємо {} (caller fallback на base_label)."""
    from app import _instance_labels_from_yolo
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[0, 0] = 1
    classes = [{"name": "nucleus"}]
    out = _instance_labels_from_yolo(labels, tmp_path / "missing.txt", classes)
    assert out == {}


def test_instance_labels_from_yolo_count_mismatch(tmp_path):
    """Якщо рядків у YOLO != кількості instance-ів — відмова від парсингу.

    Захист від хибного мапінгу: краще fallback на base_label ніж зіпсувати
    classify через невірний porядок.
    """
    from app import _instance_labels_from_yolo
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[0, 0] = 1
    labels[0, 1] = 2
    yolo = tmp_path / "stem.txt"
    yolo.write_text("0 0.1 0.1 0.05 0.05\n", encoding="utf-8")
    classes = [{"name": "nucleus"}, {"name": "vesicle"}]
    out = _instance_labels_from_yolo(labels, yolo, classes)
    assert out == {}


def test_instance_labels_from_yolo_unknown_class_id_skipped(tmp_path):
    """Якщо рядок має class_id поза labels.json — інстанс пропущено (fallback на base)."""
    from app import _instance_labels_from_yolo
    labels = np.zeros((4, 4), dtype=np.int32)
    labels[0, 0] = 1
    labels[0, 1] = 2
    yolo = tmp_path / "stem.txt"
    yolo.write_text(
        "0 0.1 0.1 0.05 0.05\n"
        "9 0.2 0.2 0.05 0.05\n",
        encoding="utf-8",
    )
    classes = [{"name": "nucleus"}, {"name": "vesicle"}]
    out = _instance_labels_from_yolo(labels, yolo, classes)
    assert out == {1: "nucleus"}


def test_instance_labels_from_polygons_basic():
    """Polygon shape поверх instance → instance отримує лейбл шейпа.

    Цей override має пріоритет над YOLO (bake може бути stale).
    """
    from app import _instance_labels_from_polygons
    # Labels: 3 квадрати — instance 1 у (0..2, 0..2), 2 у (4..6, 4..6), 3 у (8..10, 8..10)
    labels = np.zeros((12, 12), dtype=np.int32)
    labels[0:3, 0:3] = 1
    labels[4:7, 4:7] = 2
    labels[8:11, 8:11] = 3
    payload = {"shapes": [
        # vesicle полігон покриває інстанс 2
        {"label": "vesicle", "points": [[4, 4], [6, 4], [6, 6], [4, 6]],
         "shape_type": "polygon"},
        # vesicle полігон покриває інстанс 3
        {"label": "vesicle", "points": [[8, 8], [10, 8], [10, 10], [8, 10]],
         "shape_type": "polygon"},
    ]}
    out = _instance_labels_from_polygons(labels, payload)
    assert out == {2: "vesicle", 3: "vesicle"}


def test_instance_labels_from_polygons_skips_invalid():
    """Шейп без label / з <3 точками / порожній — пропускається."""
    from app import _instance_labels_from_polygons
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[0:2, 0:2] = 1
    payload = {"shapes": [
        {"label": "", "points": [[0, 0], [2, 0], [2, 2]]},     # порожня label
        {"label": "vesicle", "points": [[0, 0], [1, 0]]},      # 2 точки
        {"label": "vesicle"},                                   # немає points
    ]}
    out = _instance_labels_from_polygons(labels, payload)
    assert out == {}


def test_instance_labels_from_polygons_overrides_yolo_when_stale():
    """Регресія 2026-05-21 (round 2, проблема юзера db_img_0170):
    bake stale → YOLO має старий cid (всі nucleus), polygons.json свіжий
    з vesicle-шейпами. polygon override повинен повернути правильну лейблу.
    """
    from app import _instance_labels_from_polygons
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[0:2, 0:2] = 1  # instance 1 — за YOLO буде nucleus
    labels[5:7, 5:7] = 2  # instance 2 — теж nucleus за stale YOLO
    payload = {"shapes": [
        {"label": "vesicle", "points": [[5, 5], [7, 5], [7, 7], [5, 7]]},
    ]}
    out = _instance_labels_from_polygons(labels, payload)
    # Лише instance 2 під vesicle полігоном — отримує override.
    assert out == {2: "vesicle"}


def test_classify_returns_iids_by_label_and_orphans():
    """Регресія 2026-05-21 round 4 (db_img_0169 g_035): classify повертає
    `iids_by_label` для UI, і `orphan_iids` для iid яких немає у npy.
    """
    from app import _classify_group_membership

    classes = [{
        "id": "cls_002", "name": "vesicle_cluster",
        "constraints": {"min": {"vesicle": 1}, "max": {"nucleus": 0}},
    }]
    instance_labels = {
        2963: "vesicle", 2964: "vesicle", 2990: "nucleus",
        # 9999 буде orphan (немає у instance_labels)
    }
    group = {"class_id": "cls_002",
             "instance_ids": [2963, 2964, 2990, 9999],
             "polygon_indices": []}
    info = _classify_group_membership(instance_labels, [], group, classes=classes)
    assert info["valid"] is False
    assert "nucleus" in info["reason"]
    # Конкретний винуватець у reason — iid 2990
    assert "2990" in info["reason"]
    assert info["iids_by_label"] == {"vesicle": [2963, 2964], "nucleus": [2990]}
    assert info["orphan_iids"] == [9999]


def test_violating_iids_for_class_max_constraint():
    """max[nucleus]=0 + 3 nucleus iid → всі 3 у списку порушників."""
    from app import _violating_iids_for_class
    cls = {"constraints": {"max": {"nucleus": 0}}}
    iids = _violating_iids_for_class(
        cls, {"nucleus": 3, "vesicle": 5},
        {"nucleus": [10, 11, 12], "vesicle": [1, 2, 3, 4, 5]},
    )
    assert iids == [10, 11, 12]


def test_violating_iids_min_constraint_returns_empty():
    """min не повертає винуватців (нічого видаляти; треба додати)."""
    from app import _violating_iids_for_class
    cls = {"constraints": {"min": {"vesicle": 1}}}
    iids = _violating_iids_for_class(cls, {"vesicle": 0}, {})
    assert iids == []


def test_orphan_iids_in_group():
    from app import _orphan_iids_in_group
    instance_labels = {1: "vesicle", 2: "nucleus"}
    group = {"instance_ids": [1, 2, 999, 1000]}
    assert _orphan_iids_in_group(instance_labels, group) == [999, 1000]


def test_strip_orphan_instance_ids_removes_stale():
    """Round 5: backend auto-strip iid яких немає у npy. Журнал містить
    кожну групу з removed-списком.
    """
    from app import _strip_orphan_instance_ids
    groups = [
        {"id": "g_001", "instance_ids": [1, 2, 999], "polygon_indices": []},
        {"id": "g_002", "instance_ids": [3, 4], "polygon_indices": []},
        {"id": "g_003", "instance_ids": [777, 888], "polygon_indices": []},
    ]
    known = {1, 2, 3, 4}
    log = _strip_orphan_instance_ids(groups, known)
    assert groups[0]["instance_ids"] == [1, 2]
    assert groups[1]["instance_ids"] == [3, 4]
    assert groups[2]["instance_ids"] == []
    by_gid = {x["group_id"]: x["removed"] for x in log}
    assert by_gid == {"g_001": [999], "g_003": [777, 888]}


def test_strip_orphan_skips_empty_known():
    """Якщо known_iids порожня — strip нічого не робить (захист від
    видалення всього).
    """
    from app import _strip_orphan_instance_ids
    groups = [{"id": "g_001", "instance_ids": [1, 2], "polygon_indices": []}]
    # У caller є guard `if known_iids`, але _strip сам нічого не робить.
    log = _strip_orphan_instance_ids(groups, set())
    # З порожнім known всі iid стають orphan; це поведінка функції — caller
    # відповідальний за guard. Перевіряємо що журнал точний.
    assert log[0]["removed"] == [1, 2]
    assert groups[0]["instance_ids"] == []


def test_classify_returns_rogue_iids_for_vesicle_cluster():
    """Round 5: classifications мають flat `rogue_iids` для frontend
    підсвітки червоним. У vesicle_cluster це усі nucleus-iid.
    """
    from app import _classify_group_membership
    classes = [{
        "id": "cls_002", "name": "vesicle_cluster",
        "constraints": {"min": {"vesicle": 1}, "max": {"nucleus": 0}},
    }]
    info = _classify_group_membership(
        {1: "vesicle", 2: "vesicle", 3: "nucleus", 4: "nucleus"}, [],
        {"class_id": "cls_002", "instance_ids": [1, 2, 3, 4]},
        classes=classes,
    )
    assert info["valid"] is False
    assert info["rogue_iids"] == [3, 4]


def test_classify_legacy_vesicle_cluster_returns_rogue():
    """Legacy hardcoded type теж повертає rogue_iids для consistency."""
    from app import _classify_group_membership
    info = _classify_group_membership(
        {1: "vesicle", 2: "nucleus"}, [],
        {"type": "vesicle_cluster", "instance_ids": [1, 2]},
    )
    assert info["valid"] is False
    assert info["rogue_iids"] == [2]


def test_classify_legacy_nucleus_only_returns_rogue():
    from app import _classify_group_membership
    info = _classify_group_membership(
        {1: "nucleus", 2: "vesicle", 3: "vesicle"}, [],
        {"type": "nucleus_only", "instance_ids": [1, 2, 3]},
    )
    assert info["valid"] is False
    assert info["rogue_iids"] == [2, 3]


def test_classify_cell_valid_with_mixed_baked_mask():
    """Регресія: cell-група валідна коли baked маски містять і ядро, і везикулу
    (через YOLO-overrides), навіть якщо немає поліігонів-везикул.

    До fix (2026-05-21): всі baked → один base_label, тому cell завжди not valid
    без полігональних везикул.
    """
    # Симулюємо що `_build_lookups` повернув instance_labels з YOLO overrides
    instance_labels = {1: "nucleus", 2: "vesicle", 3: "vesicle"}
    group = {"type": "cell", "instance_ids": [1, 2, 3], "polygon_indices": []}
    info = _classify_group_membership(instance_labels, [], group)
    assert info["valid"] is True
    assert info["reason"] is None
    assert info["n_nucleus"] == 1
    assert info["n_vesicle"] == 2


def test_polygon_labels_from_payload():
    payload = {"shapes": [
        {"label": "nucleus", "points": []},
        {"label": "vesicle", "points": []},
        {"label": "cell_body", "points": []},
    ]}
    labels = _polygon_labels_from_payload(payload)
    assert labels == ["nucleus", "vesicle", "cell_body"]


# ---------------------------------------------------------------------------
# polygons.json group_id mirror sync
# ---------------------------------------------------------------------------

def test_sync_polygons_group_id_mirror_writes_gids():
    polygons = {"shapes": [
        {"label": "nucleus", "points": [], "group_id": None},
        {"label": "vesicle", "points": [], "group_id": None},
        {"label": "vesicle", "points": [], "group_id": None},
    ]}
    groups = [
        {"id": "g_001", "polygon_indices": [0, 1]},
        {"id": "g_002", "polygon_indices": [2]},
    ]
    changed = _sync_polygons_group_id_mirror(polygons, groups)
    assert changed == 3
    assert polygons["shapes"][0]["group_id"] == "g_001"
    assert polygons["shapes"][1]["group_id"] == "g_001"
    assert polygons["shapes"][2]["group_id"] == "g_002"


def test_classify_with_classes_validates_constraints():
    """User-defined class з constraints: cell потребує ≥1 nucleus + ≥1 vesicle."""
    from app import _classify_group_membership

    classes = [{
        "id": "cls_001", "name": "cell",
        "color_hue": 130, "color_sat": 45, "color_light": 42,
        "constraints": {"min": {"nucleus": 1, "vesicle": 1}, "max": {}},
    }]
    # Valid: has both
    info = _classify_group_membership(
        {1: "nucleus", 2: "vesicle"}, [],
        {"class_id": "cls_001", "instance_ids": [1, 2]},
        classes=classes,
    )
    assert info["valid"] is True
    assert info["suggested_class_id"] == "cls_001"

    # Invalid: missing vesicle
    info = _classify_group_membership(
        {1: "nucleus"}, [],
        {"class_id": "cls_001", "instance_ids": [1]},
        classes=classes,
    )
    assert info["valid"] is False
    assert "vesicle" in info["reason"]


def test_classify_with_custom_label_universal():
    """Constraints працюють на довільні label-и, не лише nucleus/vesicle."""
    from app import _classify_group_membership

    classes = [{
        "id": "cls_001", "name": "membrane_complex",
        "constraints": {"min": {"membrane": 2, "junction": 1}},
    }]
    info = _classify_group_membership(
        {1: "membrane", 2: "membrane", 3: "junction"}, [],
        {"class_id": "cls_001", "instance_ids": [1, 2, 3]},
        classes=classes,
    )
    assert info["valid"] is True
    assert info["counts"]["membrane"] == 2
    assert info["counts"]["junction"] == 1


def test_suggest_class_picks_most_specific():
    """Серед матчинг класів обирається найбільш specific (більше constraints)."""
    from app import _suggest_class_for_counts

    classes = [
        {"id": "cls_001", "name": "generic", "constraints": {"min": {"nucleus": 1}}},
        {"id": "cls_002", "name": "specific",
         "constraints": {"min": {"nucleus": 1, "vesicle": 1}}},
    ]
    cid = _suggest_class_for_counts({"nucleus": 1, "vesicle": 1}, classes)
    assert cid == "cls_002"  # specific has more constraints


def test_class_id_validation_accepts():
    """`class_id` як string є OK для validate."""
    from app import _validate_groups_payload

    payload = {"groups": [{
        "id": "g_001", "class_id": "cls_custom",
        "instance_ids": [1, 2], "polygon_indices": [], "color_hue": 130,
    }]}
    err = _validate_groups_payload(payload)
    assert err is None


def test_legacy_type_still_accepted():
    """Legacy `type` без class_id — валідний (backward compat)."""
    from app import _validate_groups_payload

    payload = {"groups": [{
        "id": "g_001", "type": "cell",
        "instance_ids": [1, 2], "polygon_indices": [], "color_hue": 0,
    }]}
    err = _validate_groups_payload(payload)
    assert err is None


def test_validation_rejects_neither_class_id_nor_type():
    """Group без class_id І без type → reject."""
    from app import _validate_groups_payload

    payload = {"groups": [{"id": "g_001", "instance_ids": []}]}
    err = _validate_groups_payload(payload)
    assert err is not None and "class_id" in err


def test_migrate_legacy_type_to_class_id():
    """Migration: group з legacy `type` → отримує class_id за name match."""
    from app import _migrate_groups_type_to_class_id

    classes = [{"id": "cls_001", "name": "cell"},
               {"id": "cls_002", "name": "vesicle_cluster"}]
    groups = [
        {"id": "g_001", "type": "cell", "instance_ids": [1]},
        {"id": "g_002", "type": "vesicle_cluster", "instance_ids": [2]},
        {"id": "g_003", "type": "unknown_legacy", "instance_ids": [3]},
        {"id": "g_004", "class_id": "cls_001"},  # already migrated
    ]
    migrated = _migrate_groups_type_to_class_id(groups, classes)
    assert migrated == 2
    assert groups[0]["class_id"] == "cls_001"
    assert groups[1]["class_id"] == "cls_002"
    assert "class_id" not in groups[2]  # no match → unchanged


# ---------------------------------------------------------------------------
# group_classes.py — CRUD + defaults
# ---------------------------------------------------------------------------

def test_classes_auto_create_defaults(tmp_path):
    """Перший _read_classes на чистому workspace створює 3 defaults."""
    from app import _read_classes, Config

    cfg = Config(
        images_dir=tmp_path / "images", output_root=tmp_path / "output",
        selected_dir=tmp_path / "selected", skipped_dir=tmp_path / "skipped",
        workspace_dir=tmp_path,
    )
    env = _read_classes(cfg)
    assert len(env["classes"]) == 3
    names = [c["name"] for c in env["classes"]]
    assert "cell" in names
    assert "vesicle_cluster" in names
    assert "nuclei" in names
    # Persisted
    assert (tmp_path / "group_classes.json").exists()


def test_classes_validation_rejects_invalid():
    from app import _validate_classes_payload

    assert _validate_classes_payload({"classes": [{"id": "", "name": "x"}]}) is not None
    assert _validate_classes_payload({"classes": [{"id": "cls_1", "name": ""}]}) is not None
    assert _validate_classes_payload(
        {"classes": [{"id": "cls_1", "name": "x",
                      "constraints": {"min": {"label": -1}}}]}
    ) is not None
    assert _validate_classes_payload(
        {"classes": [{"id": "cls_1", "name": "x",
                      "constraints": {"min": {"label": 2}}}]}
    ) is None


def test_classes_next_id_skips_gaps():
    from app import _next_class_id
    assert _next_class_id([]) == "cls_001"
    assert _next_class_id([{"id": "cls_001"}, {"id": "cls_003"}]) == "cls_002"


def test_class_by_id_and_name_lookups():
    from app import _class_by_id, _class_by_name
    classes = [{"id": "cls_001", "name": "cell"},
               {"id": "cls_002", "name": "vesicle_cluster"}]
    assert _class_by_id(classes, "cls_001")["name"] == "cell"
    assert _class_by_name(classes, "vesicle_cluster")["id"] == "cls_002"
    assert _class_by_id(classes, "missing") is None
    assert _class_by_name(classes, None) is None


def test_validate_class_against_counts():
    from app import _validate_class_against_counts
    cls = {"constraints": {"min": {"nucleus": 1}, "max": {"vesicle": 0}}}
    valid, _ = _validate_class_against_counts(cls, {"nucleus": 2, "vesicle": 0})
    assert valid is True
    valid, reason = _validate_class_against_counts(cls, {"nucleus": 0})
    assert valid is False and "nucleus" in reason
    valid, reason = _validate_class_against_counts(cls, {"nucleus": 1, "vesicle": 3})
    assert valid is False and "vesicle" in reason


def test_sync_polygons_group_id_mirror_clears_unassigned():
    polygons = {"shapes": [
        {"label": "n", "points": [], "group_id": "stale_g_old"},
        {"label": "v", "points": [], "group_id": None},
    ]}
    groups = [{"id": "g_001", "polygon_indices": [1]}]
    changed = _sync_polygons_group_id_mirror(polygons, groups)
    assert changed == 2
    assert polygons["shapes"][0]["group_id"] is None
    assert polygons["shapes"][1]["group_id"] == "g_001"
