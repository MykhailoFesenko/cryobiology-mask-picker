"""
make_sandbox.py — зробити пісочницю-копію датасета для безпечних експериментів.

НАВІЩО: щоб можна було вільно «псувати» розмітку (polygons/groups/selections),
перевіряти bake/finalize тощо — і не боятися за робочий data/vesicles_good.
Пісочниця — це ПОВНА копія датасета, яку відкриваєш у Mask Picker через
`--workspace`. Зламав — видали папку або перероби (--force). Оригінал не чіпається.

⚠️ OneDrive: за замовчуванням пісочниця кладеться ПОЗА OneDrive
   (`%USERPROFILE%/cryobiology_sandboxes/<name>`), щоб 3.4 ГБ копія НЕ синкалась
   у хмару. Якщо вкажеш --dest усередині OneDrive — скрипт попередить.

ВИКОРИСТАННЯ:
  python tools/make_sandbox.py                       # vesicles_good -> .../cryobiology_sandboxes/vesicles_sandbox
  python tools/make_sandbox.py --name try2           # інша назва пісочниці
  python tools/make_sandbox.py --source data/other   # інший датасет-джерело
  python tools/make_sandbox.py --dest D:/sb/exp1      # власний шлях (будь-який диск)
  python tools/make_sandbox.py --lean                # без selected/ (~1.2 ГБ; rebake відновить)
  python tools/make_sandbox.py --force               # перезаписати наявну пісочницю
  python tools/make_sandbox.py --list                # показати наявні пісочниці
  python tools/make_sandbox.py --dry-run             # лише показати, що скопіює (нічого не копіює)

ПІСЛЯ створення відкрий так:
  python apps/mask_picker/app.py --workspace "<шлях-до-пісочниці>"
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "vesicles_good"
# Поза OneDrive: ~/cryobiology_sandboxes/<name>  (Path.home() = %USERPROFILE%)
DEFAULT_BASE = Path.home() / "cryobiology_sandboxes"

# Що копіюємо у пісочницю (решта — локальна історія/сміття, не потрібне).
#   selected/ — великий (~1.2 ГБ), але регенерується bake/rebake → опційно (--lean прибирає).
COPY_DIRS = ["images", "output", "polygons", "groups", "selected", "skipped"]
COPY_FILES = ["labels.json", "group_classes.json", "selections.json"]
# Всередині дерева НЕ копіюємо (rolling-бекапи й тимчасове — у пісочниці не треба).
SKIP_NAMES = {"_backups", "_tmp"}


def _ignore(_dir, names):
    return [n for n in names if n in SKIP_NAMES]


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _on_onedrive(path: Path) -> bool:
    return "onedrive" in str(path.resolve()).lower()


def do_list(base: Path) -> int:
    if not base.exists():
        print(f"Пісочниць ще нема (папка {base} не існує).")
        return 0
    subs = [p for p in base.iterdir() if p.is_dir()]
    if not subs:
        print(f"Пісочниць нема у {base}.")
        return 0
    print(f"Пісочниці у {base}:")
    for p in sorted(subs):
        print(f"  {p.name:24s}  {_human(_dir_size(p)):>10s}  ({p})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Зробити пісочницю-копію датасета (безпечно псувати розмітку)."
    )
    ap.add_argument("--source", default=str(DEFAULT_SOURCE),
                    help="датасет-джерело (default: data/vesicles_good)")
    ap.add_argument("--name", default="vesicles_sandbox",
                    help="назва пісочниці (default: vesicles_sandbox)")
    ap.add_argument("--dest", default=None,
                    help="повний шлях пісочниці (інакше ~/cryobiology_sandboxes/<name>)")
    ap.add_argument("--lean", action="store_true",
                    help="без selected/ (~1.2 ГБ; rebake у Mask Picker відновить)")
    ap.add_argument("--force", action="store_true", help="перезаписати, якщо вже існує")
    ap.add_argument("--list", action="store_true", help="показати наявні пісочниці й вийти")
    ap.add_argument("--dry-run", action="store_true",
                    help="лише показати план (нічого не копіювати)")
    args = ap.parse_args()

    if args.list:
        return do_list(DEFAULT_BASE)

    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve() if args.dest else (DEFAULT_BASE / args.name).resolve()

    # --- перевірки ---
    if not source.exists() or not source.is_dir():
        print(f"[!] Джерело не знайдено: {source}")
        return 1
    if _is_under(dest, source) or _is_under(source, dest):
        print(f"[!] dest і source не можуть бути вкладені один в одного:\n    source={source}\n    dest={dest}")
        return 1
    if dest.exists():
        if not args.force:
            print(f"[!] Пісочниця вже існує: {dest}\n    Додай --force щоб перезаписати, "
                  f"або --name <інша> / --dest <інший шлях>.")
            return 1
        if not args.dry_run:
            print(f"[force] видаляю стару пісочницю {dest} …")
            shutil.rmtree(dest, ignore_errors=True)

    dirs = [d for d in COPY_DIRS if not (args.lean and d == "selected")]

    # --- план + розміри ---
    print(f"Джерело:   {source}")
    print(f"Пісочниця: {dest}")
    if _on_onedrive(dest):
        print("⚠️ УВАГА: пісочниця ВСЕРЕДИНІ OneDrive → копія буде синкатись у хмару.")
        print("   Краще задай --dest поза OneDrive (напр. D:\\ або C:\\Users\\...\\cryobiology_sandboxes).")
    print("Копіюю:")
    planned = 0
    for d in dirs:
        src_d = source / d
        if src_d.exists():
            sz = _dir_size(src_d)
            planned += sz
            print(f"   {d + '/':12s} {_human(sz):>10s}")
        else:
            print(f"   {d + '/':12s} {'(нема)':>10s}")
    for f in COPY_FILES:
        if (source / f).exists():
            sz = (source / f).stat().st_size
            planned += sz
            print(f"   {f:12s} {_human(sz):>10s}")
    print(f"   {'РАЗОМ':12s} {_human(planned):>10s}")
    if args.lean:
        print("   (--lean: selected/ пропущено — rebake у Mask Picker його відновить)")

    if args.dry_run:
        print("\n(--dry-run: нічого не скопійовано.)")
        return 0

    # --- копіювання ---
    print()
    dest.mkdir(parents=True, exist_ok=True)
    for d in dirs:
        src_d = source / d
        if not src_d.exists():
            continue
        print(f"  → копіюю {d}/ …", flush=True)
        shutil.copytree(src_d, dest / d, ignore=_ignore, dirs_exist_ok=True)
    for f in COPY_FILES:
        if (source / f).exists():
            shutil.copy2(source / f, dest / f)

    copied = _dir_size(dest)
    print(f"\n✅ Пісочниця готова: {dest}  ({_human(copied)})")
    print(f"   Відкрити:  python apps/mask_picker/app.py --workspace \"{dest}\"")
    print(f"   Зламав — не страшно: видали папку або перероби `--force`. "
          f"Оригінал {source.name} не чіпався.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
