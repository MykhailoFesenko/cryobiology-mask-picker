"""
setup_gpu.py — увімкнути GPU-прискорення сегментації (NVIDIA CUDA).

ПРОБЛЕМА, яку лікує цей скрипт:
  Дефолтний `pip install torch` (його тягне cellsegkit) на Windows ставить **CPU-only**
  збірку torch. Тому навіть з NVIDIA-картою сегментація рахує на CPU
  (`torch.cuda.is_available() == False`), а Cellpose-SAM на CPU = десятки хвилин на фото.

ЩО РОБИТЬ:
  1. визначає відеокарту (NVIDIA / AMD / Intel / нема) і версію CUDA драйвера;
  2. перевіряє, чи поточний torch уміє CUDA;
  3. якщо є NVIDIA, а torch CPU-only → ставить CUDA-збірку torch під драйвер;
  4. перевіряє, що після цього `torch.cuda.is_available() == True`;
  5. усе детально друкує і дублює у `_logs/gpu_setup.log` — щоб по логам було видно
     причину, якщо щось пішло не так.

⚠️ CUDA — ТІЛЬКИ для NVIDIA. AMD (ROCm) та Intel (XPU/oneAPI) ця збірка torch НЕ
   прискорить — на таких ПК лишаємось на CPU (скрипт це чітко скаже, не впаде).

ВИКОРИСТАННЯ:
  python tools/setup_gpu.py            # діагностика + (якщо треба й можна) поставити CUDA-torch
  python tools/setup_gpu.py --check    # ТІЛЬКИ діагностика, нічого не ставити
  python tools/setup_gpu.py --dry-run  # показати команду pip, але не виконувати
  python tools/setup_gpu.py --cuda cu124   # форсувати конкретний CUDA-канал
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows cp1251/cp866 консоль не вміє друкувати кирилицю/emoji → UnicodeEncodeError.
# Реконфігуруємо у UTF-8 (errors='replace' → '?' замість краху).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "_logs" / "gpu_setup.log"

# Мапа: мінімальна версія CUDA драйвера (major, minor) -> канал колес torch.
# Беремо найвищий канал, який драйвер ще тягне (CUDA forward-compat у межах 12.x).
_CHANNEL_BY_DRIVER = [
    ((12, 8), "cu128"),
    ((12, 6), "cu126"),
    ((12, 4), "cu124"),
    ((12, 1), "cu121"),
    ((11, 8), "cu118"),
]
# Якщо NVIDIA є, але версію CUDA драйвера прочитати не вдалось — широко-сумісний дефолт.
_FALLBACK_CHANNEL = "cu121"

_LOG_BUF: list[str] = []


def log(msg: str = "", level: str = "INFO") -> None:
    """Друк у консоль + у буфер (потім скидається у _logs/gpu_setup.log)."""
    line = msg if level == "RAW" else f"[gpu-setup] {level}: {msg}"
    print(line)
    _LOG_BUF.append(line)


def flush_log() -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now(timezone.utc).isoformat()} =====\n")
            f.write("\n".join(_LOG_BUF) + "\n")
        print(f"[gpu-setup] (повний лог дописано у {LOG_PATH})")
    except Exception as e:  # лог — не критичний; не валимось через нього
        print(f"[gpu-setup] (не зміг записати лог-файл: {e})")


def _run(cmd: list[str], timeout: int = 25):
    """Запустити команду → (returncode, stdout, stderr). Ніколи не кидає виняток."""
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def detect_nvidia():
    """nvidia-smi → (present: bool, cuda: (maj,min)|None, name: str|None)."""
    rc, out, _ = _run(["nvidia-smi"])
    if rc != 0:
        return False, None, None
    cuda = None
    m = re.search(r"CUDA Version:\s*([0-9]+)\.([0-9]+)", out)
    if m:
        cuda = (int(m.group(1)), int(m.group(2)))
    name = None
    rc2, out2, _ = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if rc2 == 0 and out2.strip():
        name = out2.strip().splitlines()[0].strip()
    return True, cuda, name


def list_gpu_adapters() -> list[str]:
    """Best-effort назви відеоадаптерів (для діагностики не-NVIDIA карт)."""
    names: list[str] = []
    sysname = platform.system()
    try:
        if sysname == "Windows":
            rc, out, _ = _run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | "
                 "Select-Object -ExpandProperty Name"],
                timeout=30,
            )
            if rc == 0:
                names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        elif sysname == "Linux":
            rc, out, _ = _run(["bash", "-lc", "lspci | grep -iE 'vga|3d|display'"])
            if rc == 0:
                names = [ln.split(":")[-1].strip() for ln in out.splitlines() if ln.strip()]
        elif sysname == "Darwin":
            rc, out, _ = _run(["system_profiler", "SPDisplaysDataType"], timeout=30)
            if rc == 0:
                names = [ln.split(":", 1)[1].strip()
                         for ln in out.splitlines() if "Chipset Model:" in ln]
    except Exception:
        pass
    return names


def classify_vendors(names: list[str]) -> set[str]:
    vendors: set[str] = set()
    for n in names:
        u = n.upper()
        if any(k in u for k in ("NVIDIA", "GEFORCE", "RTX", "QUADRO", "TESLA")):
            vendors.add("NVIDIA")
        elif any(k in u for k in ("AMD", "RADEON", "ATI")):
            vendors.add("AMD")
        elif any(k in u for k in ("INTEL", "ARC", "IRIS", "UHD", "HD GRAPHICS")):
            vendors.add("INTEL")
    return vendors


_TORCH_PROBE = (
    "import json\n"
    "try:\n"
    "    import torch\n"
    "    print(json.dumps({'installed': True, 'version': torch.__version__,\n"
    "        'cuda_build': torch.version.cuda,\n"
    "        'cuda_available': bool(torch.cuda.is_available()),\n"
    "        'device': (torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)}))\n"
    "except Exception as e:\n"
    "    print(json.dumps({'installed': False, 'error': str(e)}))\n"
)


def torch_status() -> dict:
    """Стан torch У ПОТОЧНОМУ venv (через subprocess, щоб бачити реальний стан)."""
    rc, out, err = _run([sys.executable, "-c", _TORCH_PROBE], timeout=180)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"installed": False, "error": (err or out or "torch probe failed").strip()[:300]}


def channel_for_driver(cuda, override: str | None) -> str | None:
    if override:
        return override
    if not cuda:
        return None  # NVIDIA є, але CUDA-версію не прочитали → caller візьме fallback
    for min_ver, ch in _CHANNEL_BY_DRIVER:
        if cuda >= min_ver:
            return ch
    return None  # драйвер CUDA < 11.8 — застарий


def pip_install_cuda_torch(channel: str, dry_run: bool):
    """Поставити CUDA-збірку torch/torchvision із pytorch-каналу. → (ok, info)."""
    url = f"https://download.pytorch.org/whl/{channel}"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
           "torch", "torchvision", "--index-url", url]
    log(f"$ {' '.join(cmd)}", "RAW")
    if dry_run:
        log("(--dry-run: команду НЕ виконую)")
        return True, "dry-run"
    log("Качаю CUDA-збірку torch (сотні МБ — може зайняти кілька хвилин)…")
    try:
        rc = subprocess.run(cmd).returncode  # вивід pip стрімиться напряму в консоль
        return rc == 0, f"pip returncode={rc}"
    except Exception as e:
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Увімкнути GPU (NVIDIA CUDA) для сегментації — поставити CUDA-збірку torch."
    )
    ap.add_argument("--check", action="store_true", help="лише діагностика, нічого не ставити")
    ap.add_argument("--dry-run", action="store_true", help="показати pip-команду, але не виконувати")
    ap.add_argument("--cuda", metavar="cuXXX",
                    help="форсувати CUDA-канал (cu118/cu121/cu124/cu126/cu128)")
    args = ap.parse_args()

    log("=== Діагностика GPU / torch ===")
    log(f"ОС: {platform.platform()}")
    log(f"Python: {platform.python_version()}  ({sys.executable})")

    # 1) Поточний torch
    ts = torch_status()
    if ts.get("installed"):
        log(f"torch: {ts['version']} | CUDA-build={ts.get('cuda_build')} | "
            f"cuda_available={ts.get('cuda_available')} | device={ts.get('device')}")
    else:
        log(f"torch НЕ імпортується: {ts.get('error')}", "WARN")

    # 2) Яка відеокарта
    nv_present, driver_cuda, nv_name = detect_nvidia()
    if nv_present:
        cv = f"{driver_cuda[0]}.{driver_cuda[1]}" if driver_cuda else "?? (не прочитав)"
        log(f"NVIDIA GPU: {nv_name or 'знайдено'} | драйвер підтримує CUDA {cv}")
    else:
        adapters = list_gpu_adapters()
        vendors = classify_vendors(adapters)
        log(f"nvidia-smi не знайдено. Відеоадаптери: {adapters or 'не визначено'}")
        non_nv = sorted(vendors - {"NVIDIA"})
        if non_nv:
            log(f"Виявлено НЕ-NVIDIA GPU ({', '.join(non_nv)}). CUDA для них НЕ підходить:", "WARN")
            log("  AMD → лише ROCm (Linux); Intel → XPU/oneAPI. Тут це не підтримується.")
        log("ВЕРДИКТ: GPU-прискорення (CUDA) недоступне → сегментація піде на CPU.", "RESULT")
        log("  На CPU спершу прожени легку модель (хвилини, не години):")
        log("    python apps/segmentation/run_segmentation.py --model instanseg --data-dir <dir>")
        flush_log()
        return 0

    # 3) NVIDIA є. Якщо torch уже бачить CUDA — нічого робити.
    if ts.get("installed") and ts.get("cuda_available"):
        log("ВЕРДИКТ: усе вже ОК — torch бачить CUDA, сегментація піде на GPU. ✅", "RESULT")
        flush_log()
        return 0

    log("torch не бачить CUDA, хоча NVIDIA GPU є → потрібна CUDA-збірка torch.", "WARN")

    # Свіжий Python часто ще не має CUDA-колес torch на pytorch.org (саме випадок Python 3.14).
    if sys.version_info[:2] >= (3, 13):
        log(f"⚠️ Python {platform.python_version()} дуже новий — CUDA-колеса torch під нього "
            "можуть ще не існувати. Якщо встановлення впаде з 'No matching distribution' — "
            "постав Python 3.10–3.12 у venv і повтори.", "WARN")

    channel = channel_for_driver(driver_cuda, args.cuda)
    if channel is None and driver_cuda is None:
        channel = _FALLBACK_CHANNEL  # NVIDIA є, версію не прочитали → широко-сумісний канал
        log(f"Версію CUDA драйвера не прочитав — беру дефолтний канал {channel}.", "WARN")
    elif channel is None:
        log(f"Драйвер CUDA {driver_cuda[0]}.{driver_cuda[1]} застарий для готових колес "
            "(треба ≥ 11.8). Онови драйвер NVIDIA і повтори.", "ERROR")
        log("ВЕРДИКТ: лишаємось на CPU.", "RESULT")
        flush_log()
        return 1

    log(f"Обраний CUDA-канал: {channel}"
        + (f" (форсовано --cuda)" if args.cuda else
           f" (під драйвер CUDA {driver_cuda[0]}.{driver_cuda[1]})" if driver_cuda else ""))

    if args.check:
        log("(--check) Встановлення пропускаю. Щоб увімкнути GPU — запусти без --check.", "RESULT")
        flush_log()
        return 0

    ok, info = pip_install_cuda_torch(channel, args.dry_run)
    if not ok:
        log(f"Встановлення CUDA-torch НЕ вдалося ({info}).", "ERROR")
        log("  Часті причини: нема інтернету / SSL; немає колеса під твій Python "
            "(див. ⚠️ вище); pip-конфлікт. Повний вивід pip — вище у консолі.", "ERROR")
        log("ВЕРДИКТ: лишаємось на CPU. Сегментація все одно працюватиме (повільно).", "RESULT")
        flush_log()
        return 1

    if args.dry_run:
        log("(--dry-run завершено — нічого не змінено.)", "RESULT")
        flush_log()
        return 0

    # 4) Перевірка після встановлення
    log("Перевіряю, чи torch тепер бачить CUDA…")
    ts2 = torch_status()
    if ts2.get("installed") and ts2.get("cuda_available"):
        log(f"✅ Готово! torch {ts2['version']} (CUDA {ts2.get('cuda_build')}) бачить GPU "
            f"'{ts2.get('device')}'. Сегментація піде на GPU.", "RESULT")
        flush_log()
        return 0

    log(f"Поставив CUDA-torch, але cuda_available досі False "
        f"(torch={ts2.get('version')}, cuda_build={ts2.get('cuda_build')}).", "ERROR")
    log("  Імовірно: драйвер NVIDIA застарий під цю CUDA, або стало CPU-колесо. "
        "Онови драйвер NVIDIA; або спробуй інший канал, напр. "
        "python tools/setup_gpu.py --cuda cu121.", "ERROR")
    log("ВЕРДИКТ: поки що CPU.", "RESULT")
    flush_log()
    return 1


if __name__ == "__main__":
    sys.exit(main())
