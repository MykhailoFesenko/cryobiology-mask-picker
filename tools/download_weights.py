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
  python tools/download_weights.py --warmup        # + прогріти base-моделі (cyto2→Cellpose-SAM, instanseg)

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

# Windows cp1251/cp866 консоль не вміє кодувати кирилицю у print()/argparse-help →
# UnicodeEncodeError. Реконфігуруємо у UTF-8 (errors='replace' → '?' замість краху).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "cryobiology4" / "weights_manifest.json"

_SSL_HINT = (
    "\n  [SSL] HTTPS-сертифікат не пройшов перевірку (часто на свіжій Windows/ВМ —\n"
    "        неповне сховище кореневих сертифікатів). Спробуй одне з:\n"
    "          - pip install --upgrade certifi   (і запусти ще раз)\n"
    "          - поклади файли ваг вручну в cryobiology4/weights/ (з Google Drive,\n"
    "            див. README) — тоді інтернет для ваг не потрібен.\n"
)


def _prime_ca_certs() -> None:
    """На свіжих Windows/ВМ системне сховище кореневих сертифікатів буває неповним →
    HTTPS падає з `SSL: CERTIFICATE_VERIFY_FAILED`. Вказуємо urllib повний набір
    коренів із certifi через SSL_CERT_FILE — його читає `ssl.create_default_context()`,
    тож запрацюють і наші завантаження, і Cellpose (тягне свою модель з HuggingFace у
    тому ж процесі). `setdefault` → не перетираємо кастомний CA (напр. корпоративний
    проксі), а certifi-корені ДОДАЮТЬСЯ до системних, не замінюють їх."""
    try:
        import certifi
    except Exception:
        return
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())


def _looks_like_ssl(ex: Exception) -> bool:
    s = str(ex).lower()
    return "certificate_verify_failed" in s or ("ssl" in s and "certificate" in s)


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
    _prime_ca_certs()
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
    ssl_err = False
    for e in miss_files:
        try:
            _download(base + e["asset"], dest_root / e["dest"], e.get("size_mb", 0))
        except Exception as ex:
            print(f"  [!] {e['asset']}: {ex}"); ok = False
            ssl_err = ssl_err or _looks_like_ssl(ex)
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
            ssl_err = ssl_err or _looks_like_ssl(ex)
    if ssl_err:
        print(_SSL_HINT)
    if ok:
        print("[weights] Готово — усі потрібні ваги на місці.")
    return ok


def warmup_base_models(models: list[str] | None = None) -> None:
    """Прогріває base-моделі, які cellpose/instanseg качають при ПЕРШОМУ використанні
    (поза GitHub Release): cyto2 → Cellpose-SAM (`cpsam`, ~1.2 ГБ) та instanseg built-in.
    Вони кешуються у домашній теці пакета → наступні запуски стабільні навіть офлайн.
    Кожна модель — окремо; помилка (нема пакета / нема мережі) НЕ валить решту, лише
    друкує підказку (як і ensure_weights — preflight не має падати)."""
    sys.path.insert(0, str(ROOT / "shared" / "cellsegkit"))
    try:
        from cellsegkit import SegmenterFactory
    except Exception as e:
        print(f"[warmup] cellsegkit недоступний ({e}) → прогрів base-моделей пропущено.")
        return
    want = set(models) if models else None
    plan: list[str] = []
    if want is None or (want & {"cyto2", "cyto", "nuclei"}):
        plan.append("cyto2")            # → Cellpose-SAM (cpsam)
    if want is None or (want & {"instanseg", "instanseg:brightfield"}):
        plan.append("instanseg")
    if not plan:
        return
    print(f"[warmup] прогріваю base-моделі (качаються при першому запуску): {', '.join(plan)}")
    for m in plan:
        try:
            print(f"[warmup]   ↓ {m} …")
            SegmenterFactory.create(m, use_gpu=False)
            print(f"[warmup]   ✓ {m} готова (кешовано)")
        except Exception as e:
            print(f"[warmup]   [!] {m} пропущено: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Завантажити ваги моделей з GitHub Release.")
    ap.add_argument("--models", nargs="*", default=None, help="Лише ці моделі (інакше всі).")
    ap.add_argument("--check", action="store_true", help="Лише показати, чого бракує.")
    ap.add_argument(
        "--warmup", action="store_true",
        help="Після Release-ваг ще й прогріти base-моделі, що качаються при першому "
             "використанні (cyto2 → Cellpose-SAM ~1.2 ГБ, instanseg).",
    )
    args = ap.parse_args()
    man = _load_manifest()
    dest_root, miss_files, miss_arch = _missing(man, args.models)
    if args.check:
        miss = [e["dest"] for e in miss_files] + [e["dest_dir"] + "/" for e in miss_arch]
        print("Бракує:" if miss else "Усі ваги на місці.")
        for m in miss:
            print(f"  - {m}")
        return 0
    ok = ensure_weights(args.models)
    if args.warmup:
        warmup_base_models(args.models)
        print("\n[preflight] Готово — далі сегментацію можна ганяти без пауз на завантаження.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
