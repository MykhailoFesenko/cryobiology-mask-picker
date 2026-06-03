"""
download_weights.py — авто-завантаження ваг ML-моделей з GitHub Release.

ЧОМУ так (а не git-репо / git-LFS):
  - Ваги ~1.8 ГБ, найбільший файл 1.1 ГБ. GitHub блокує файли >100MB у звичайному git.
  - git-LFS дозволив би «авто на clone», АЛЕ безкоштовний ліміт = 1ГБ сховища + 1ГБ трафіку/міс →
    1.8 ГБ одразу його перевищує (платно), і кожен clone їсть трафік.
  - GitHub Release: безкоштовно, до 2 ГБ/файл, необмежений публічний трафік. Ваги — як assets Release,
    а цей скрипт тягне відсутні АВТОМАТИЧНО при першому запуску сегментації → тестувальник нічого не
    шукає й не може пропустити.

ВИКОРИСТАННЯ:
  python tools/download_weights.py                 # завантажити ВСІ відсутні ваги
  python tools/download_weights.py --models yolo11_512 cpsam_finetuned   # лише потрібні
  python tools/download_weights.py --check         # лише показати, чого бракує

  # або з коду (run_segmentation викликає авто):
  from download_weights import ensure_weights
  ensure_weights(models=["cpsam_finetuned"])

НАЛАШТУВАННЯ URL:
  Заповни "release_base_url" у cryobiology4/weights_manifest.json,
  АБО встанови env CRYOBIOLOGY_WEIGHTS_URL=https://github.com/<USER>/<REPO>/releases/download/<TAG>/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "cryobiology4" / "weights_manifest.json"


def _load_manifest() -> dict:
    with open(MANIFEST, "r", encoding="utf-8") as f:
        return json.load(f)


def _base_url(man: dict) -> str | None:
    url = os.environ.get("CRYOBIOLOGY_WEIGHTS_URL") or man.get("release_base_url", "")
    url = url.strip()
    if not url or "<USER>" in url or "<REPO>" in url:
        return None          # ще не налаштовано
    return url.rstrip("/") + "/"


def _entries_for(man: dict, models: list[str] | None):
    """Повертає (files, archives), відфільтровані за списком моделей (або всі)."""
    files = man.get("files", [])
    archives = man.get("archives", [])
    if models:
        want = set(models)
        files = [e for e in files if not e.get("models") or want & set(e["models"])]
        archives = [e for e in archives if not e.get("models") or want & set(e["models"])]
    return files, archives


def _missing(man: dict, models: list[str] | None):
    dest_root = ROOT / man.get("dest_dir", "cryobiology4/weights")
    files, archives = _entries_for(man, models)
    miss_files = [e for e in files if not (dest_root / e["dest"]).exists()]
    miss_arch = [e for e in archives if not (dest_root / e["dest_dir"]).exists()]
    return dest_root, miss_files, miss_arch


def _download(url: str, dst: Path, size_mb: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    print(f"  ↓ {dst.name}  (~{size_mb:.0f} MB)")

    def _hook(block, bs, total):
        if total > 0:
            pct = min(100, block * bs * 100 // total)
            print(f"\r    {pct:3d}%", end="", flush=True)

    urllib.request.urlretrieve(url, tmp, _hook)
    print()
    tmp.replace(dst)


def ensure_weights(models: list[str] | None = None, quiet: bool = False) -> bool:
    """Завантажує відсутні ваги для вказаних моделей (або всіх). Повертає True якщо
    усе на місці після виклику. Якщо URL не налаштовано АБО немає інтернету —
    друкує підказку й повертає False (НЕ кидає виняток → сегментація built-in
    моделей працює далі)."""
    try:
        man = _load_manifest()
    except Exception as e:
        if not quiet:
            print(f"[weights] не зміг прочитати manifest: {e}")
        return False
    dest_root, miss_files, miss_arch = _missing(man, models)
    if not miss_files and not miss_arch:
        return True
    base = _base_url(man)
    if base is None:
        if not quiet:
            print("\n[weights] Бракує ваг для кастомних моделей, а URL Release НЕ налаштовано.")
            print("          Заповни release_base_url у cryobiology4/weights_manifest.json")
            print("          або встанови env CRYOBIOLOGY_WEIGHTS_URL. Деталі — у README/INSTALL.")
            print("          (Built-in моделі типу cyto2 працюють і без цих ваг.)\n")
        return False
    print(f"[weights] Завантажую {len(miss_files)+len(miss_arch)} відсутніх файлів ваг із Release...")
    ok = True
    for e in miss_files:
        try:
            _download(base + e["asset"], dest_root / e["dest"], e.get("size_mb", 0))
        except Exception as ex:
            print(f"  [!] {e['asset']}: {ex}"); ok = False
    for e in miss_arch:
        try:
            with tempfile.TemporaryDirectory() as td:
                zp = Path(td) / e["asset"]
                _download(base + e["asset"], zp, e.get("size_mb", 0))
                target = dest_root / e["dest_dir"]
                target.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zp) as zf:
                    zf.extractall(target)
        except Exception as ex:
            print(f"  [!] {e['asset']}: {ex}"); ok = False
    if ok:
        print("[weights] Готово — усі потрібні ваги на місці.")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Завантажити ваги моделей з GitHub Release.")
    ap.add_argument("--models", nargs="*", default=None, help="Лише ці моделі (інакше всі).")
    ap.add_argument("--check", action="store_true", help="Лише показати, чого бракує.")
    args = ap.parse_args()
    man = _load_manifest()
    dest_root, miss_files, miss_arch = _missing(man, args.models)
    if args.check:
        miss = [e["dest"] for e in miss_files] + [e["dest_dir"] + "/" for e in miss_arch]
        print("Бракує:" if miss else "Усі ваги на місці.")
        for m in miss:
            print(f"  - {m}")
        return 0
    return 0 if ensure_weights(args.models) else 1


if __name__ == "__main__":
    sys.exit(main())
