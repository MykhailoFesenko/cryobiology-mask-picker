"""
Запуск сегментации клеток (v1.6.6+).

Использование (запускать з кореня репо):
    # Дефолт: data/vesicles_good/ (з config.yaml Mask Picker'а)
    python apps/segmentation/run_segmentation.py                     # одна модель (MODEL_TYPE)
    python apps/segmentation/run_segmentation.py --all               # все модели подряд
    python apps/segmentation/run_segmentation.py --clean-duplicates  # + удалить дубли "Копия *.jpg"

    # Інша папка з даними:
    python apps/segmentation/run_segmentation.py --all --data-dir data/2026-06-01_nuclei
    # або через env var:
    set CRYOBIOLOGY_DATA_DIR=data/my_dataset && python apps/segmentation/run_segmentation.py --all

Структура результатів (совместима з apps/mask_picker/):
    <data-dir>/
    ├── images/           ← вхід (.jpg/.png)
    └── output/
        ├── cyto2/
        │   ├── overlay/   ← визуализация (PNG з масками)
        │   ├── png/       ← маски як PNG
        │   ├── npy/       ← маски як numpy (.npy)
        │   └── yolo/      ← аннотации YOLO (.txt)
        ├── instanseg/
        └── ...

Resume: cellsegkit пропускає фото де всі 4 формати вже існують → можна
безпечно докинути нові фото у images/ і запустити --all повторно.

Зависимости:
    pip install -e ./shared/cellsegkit
    pip install instanseg-torch    # для instanseg (built-in + custom TorchScript)
    pip install ultralytics        # для YOLO11-seg (yolo11_512/680/sphero)

Кастомні ваги Cryobiology 4 очікуються в:
    ./cryobiology4/weights/
    (або CRYOBIOLOGY4_WEIGHTS env var)
"""

import os
import sys
import traceback
import zipfile
from pathlib import Path

# Reconfigure stdout/stderr to UTF-8 на Windows cp1251 console — інакше emoji
# у print() падають з UnicodeEncodeError. errors='replace' дає `?` замість краху.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass  # старий Python (<3.7) або вже-перенаправлений stream

# ── --help обробка ДО імпорту ML-залежностей ────────────────────────────────
if any(a in {"--help", "-h"} for a in sys.argv[1:]):
    print(__doc__)
    print("Optional flags:")
    print("  --all                 Run all models in ALL_MODELS")
    print("  --clean-duplicates    Remove 'Копия *.jpg' from images/ before run")
    print("  --data-dir PATH       Override data dir (default: data/vesicles_good)")
    print("  -h, --help            Show this help and exit")
    sys.exit(0)

# ── Пути ────────────────────────────────────────────────────────────────────
# apps/segmentation/run_segmentation.py → корінь = parent.parent
HERE       = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
BASE_DIR   = PROJECT_ROOT  # для backwards-compat у нижньому коді

# Default data dir = data/vesicles_good (актуальний робочий dataset).
# Override через CLI --data-dir PATH або env CRYOBIOLOGY_DATA_DIR.
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "vesicles_good"


def _resolve_data_dir() -> Path:
    """Парсить --data-dir з sys.argv або env var CRYOBIOLOGY_DATA_DIR."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--data-dir" and i + 1 < len(args):
            return Path(args[i + 1]).resolve()
        if a.startswith("--data-dir="):
            return Path(a.split("=", 1)[1]).resolve()
    env = os.environ.get("CRYOBIOLOGY_DATA_DIR")
    if env:
        return Path(env).resolve()
    return DEFAULT_DATA_DIR


DATA_DIR    = _resolve_data_dir()
NUCLEI_DIR  = DATA_DIR  # алиас для backwards-compat (нижче в коді)
INPUT_DIR   = DATA_DIR / "images"
OUTPUT_BASE = DATA_DIR / "output"

# ── Выбор модели ────────────────────────────────────────────────────────────
#
#  Built-in / публічні веси:
#  "cyto2"               — Cellpose улучшенный (работает без доп. зависимостей)
#  "cyto"                — Cellpose: клетки целиком
#  "nuclei"              — Cellpose: ядра с видимыми границами
#
#  "instanseg"           — ★ ЛУЧШИЙ F1 (Криобиология III), нужен: pip install instanseg-torch
#  "instanseg:brightfield" — InstanSeg для brightfield-микроскопии
#
#  "cellsam"             — CellSAM (SAM-based)
#
#  Кастомні ваги з Криобиология 4/ (вимагають ./Криобиология 4/weights/):
#  "cpsam_finetuned"        — Cellpose-SAM finetuned (diameter=40), ~1.2 GB
#  "instanseg_neuroblastoma" — InstanSeg custom (нейробластома), TorchScript
#  "instanseg_0605"          — InstanSeg 2025-06-05, TorchScript
#  "yolo11_512"              — YOLO11x-seg (L929 монолайер, 512-trained)
#  "yolo11_680"              — YOLO11x-seg (L929 монолайер, 680-trained, Full)
#  "yolo11_sphero"           — YOLO11x-seg (сфероїди / spherical MSCs)
#
MODEL_TYPE = "cyto2"

# Форматы экспорта (убери ненужные)
EXPORT_FORMATS = ("overlay", "npy", "png", "yolo")

# Список моделей для запуска через --all.
# yolo11_sphero поза списком — натренована на сфероїдах, не на ядрах.
ALL_MODELS = [
    # built-in
    "cyto2",
    "instanseg",
    # Cryobiology 4 custom
    "cpsam_finetuned",
    "instanseg_neuroblastoma",
    "instanseg_0605",
    "yolo11_512",
    "yolo11_680",
]


# ── 1. Распаковка и очистка имён файлов ─────────────────────────────────────
PREFIXES_TO_STRIP = ["Копія ", "Копия ", "Copy of ", "copy of "]
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def clean_filename(name: str) -> str:
    """Убирает служебные префиксы типа 'Копія ', 'Копия ' из имени файла."""
    for prefix in PREFIXES_TO_STRIP:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def count_images(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for p in directory.iterdir()
               if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def clean_duplicates_in_images() -> int:
    """
    Удаляет из images/ файлы с префиксами 'Копия ', 'Копія ' и т.п., если
    есть файл-оригинал без префикса. Возвращает число удалённых файлов.
    """
    if not INPUT_DIR.exists():
        return 0
    removed = 0
    for p in list(INPUT_DIR.iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        clean = clean_filename(p.name)
        if clean == p.name:
            continue  # нет префикса
        original = INPUT_DIR / clean
        if original.exists():
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                print(f"  [!] Не смог удалить {p.name}: {e}")
    return removed


def extract_zip():
    # Если images/ уже содержит файлы — ничего не делаем
    if INPUT_DIR.exists() and any(INPUT_DIR.iterdir()):
        print(f"Изображения уже распакованы: {count_images(INPUT_DIR)} файлов в {INPUT_DIR}")
        return

    zip_files = list(NUCLEI_DIR.glob("*.zip"))
    if not zip_files:
        print(f"ZIP-архив не найден в {DATA_DIR} и папка images/ пуста.")
        return

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    extracted = 0

    for zip_path in zip_files:
        print(f"Распаковываю {zip_path.name} → {INPUT_DIR} ...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                # Корректная обработка кириллических имён
                try:
                    filename = member.filename.encode("cp437").decode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    filename = member.filename

                # Берём только имя файла (без подпапок архива)
                basename = Path(filename).name
                if not basename:
                    continue

                # Убираем префиксы "Копія", "Копия" и т.п.
                clean_name = clean_filename(basename)

                target_path = INPUT_DIR / clean_name
                if not target_path.exists():
                    with zf.open(member) as src, open(target_path, "wb") as dst:
                        dst.write(src.read())
                    extracted += 1

    print(f"Готово: распаковано {extracted} файлов, "
          f"всего {count_images(INPUT_DIR)} изображений в {INPUT_DIR}")


# ── 2. Добавляем репо в путь ─────────────────────────────────────────────────
def add_repo_to_path():
    repo_path = PROJECT_ROOT / "shared" / "cellsegkit"
    if repo_path.exists():
        sys.path.insert(0, str(repo_path))


# ── 3. Запуск одной модели ───────────────────────────────────────────────────
def run_model(model_type: str, segmenter_factory, run_segmentation_fn):
    """Запускает сегментацию одной моделью в её собственную подпапку."""

    # Папка вывода: output/<model_type>/
    safe_name = model_type.replace(":", "_")   # "instanseg:brightfield" → "instanseg_brightfield"
    output_dir = OUTPUT_BASE / safe_name
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Модель:  {model_type}")
    print(f"  Вход:    {INPUT_DIR}")
    print(f"  Выход:   {output_dir}")
    print(f"  Форматы: {', '.join(EXPORT_FORMATS)}")
    print(f"{'='*60}\n")

    try:
        segmenter = segmenter_factory.create(model_type=model_type, use_gpu=True)
    except ImportError as e:
        print(f"[!] Пропускаю {model_type} (нет зависимости): {e}")
        return False
    except Exception as e:
        print(f"[!] Ошибка загрузки {model_type}: {e}")
        traceback.print_exc()
        return False

    try:
        run_segmentation_fn(
            segmenter=segmenter,
            input_dir=str(INPUT_DIR),
            output_dir=str(output_dir),
            export_formats=EXPORT_FORMATS,
        )
    except Exception as e:
        print(f"[!] Ошибка во время сегментации {model_type}: {e}")
        traceback.print_exc()
        return False

    print(f"\n✅ {model_type} готово → {output_dir}")
    return True


# ── 4. Основной запуск ───────────────────────────────────────────────────────
def run():
    run_all = "--all" in sys.argv
    clean_dupes = "--clean-duplicates" in sys.argv

    print(f"📁 Data dir: {DATA_DIR}")
    print(f"   Input:   {INPUT_DIR}")
    print(f"   Output:  {OUTPUT_BASE}")
    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        print(f"   (створено нову папку)")

    extract_zip()

    if clean_dupes:
        removed = clean_duplicates_in_images()
        if removed:
            print(f"🧹 Удалено {removed} дубликатов 'Копия *' из {INPUT_DIR}")
        else:
            print(f"🧹 Дубликатов 'Копия *' не найдено в {INPUT_DIR}")

    add_repo_to_path()

    if not INPUT_DIR.exists() or not any(INPUT_DIR.iterdir()):
        print(f"Папка {INPUT_DIR} пуста или не существует.")
        sys.exit(1)

    try:
        from cellsegkit import SegmenterFactory, run_segmentation
    except ImportError:
        print(
            "\n❌ Пакет cellsegkit не найден.\n"
            "Установи его командой:\n"
            "    pip install -e ./shared/cellsegkit\n"
        )
        sys.exit(1)

    models = ALL_MODELS if run_all else [MODEL_TYPE]

    # Авто-завантаження ваг кастомних моделей з GitHub Release (якщо налаштовано
    # release_base_url / env CRYOBIOLOGY_WEIGHTS_URL). Built-in моделі (cyto2 тощо)
    # ваг не потребують → відсутність/не-налаштованість URL НЕ блокує запуск.
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        from download_weights import ensure_weights
        ensure_weights(models=models)
    except Exception as _e:
        print(f"[weights] авто-завантаження пропущено: {_e}")

    results: list[tuple[str, bool]] = []
    for model in models:
        ok = run_model(model, SegmenterFactory, run_segmentation)
        results.append((model, ok))

    # ── Итоги ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  Сводка:")
    for model, ok in results:
        safe = model.replace(":", "_")
        status = "✅ OK    " if ok else "❌ FAIL  "
        print(f"  {status} {model:22s} → output/{safe}/")
    print("=" * 60)
    print(f"  Результаты в: {OUTPUT_BASE}")
    print()
    print(f"  ▶ Теперь запусти .\\run_mask_picker.bat чтобы отобрать")
    print(f"    лучшие маски и доразметить их в Mask Picker.")
    if not run_all:
        print()
        print(f"  Подсказка: python run_segmentation.py --all — прогнать все модели")
        print(f"             python run_segmentation.py --clean-duplicates — убрать 'Копия *'")


if __name__ == "__main__":
    run()
