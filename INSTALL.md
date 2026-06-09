# Cryobiology V — встановлення та запуск

Два застосунки в одному репозиторії, повний пайплайн розмітки:

```
   apps/segmentation/         apps/mask_picker/
   (ML → чорнові маски)  ──►   (людина доводить + групує + запікає)
        крок 1                        крок 2
```

**Як вони звʼязані:** прямого виклику коду між ними НЕМАЄ. Звʼязок — через **спільну
папку даних** `data/<dataset>/`:
- `apps/segmentation` запускає моделі й пише результат у `data/<dataset>/output/<model>/{npy,png,yolo,overlay}`.
- `apps/mask_picker` читає той самий `output/`, дає редагувати, і пише фінал у `data/<dataset>/selected/`.

Тобто: **спершу сегментація заповнює `output/`, потім Mask Picker його споживає.** Контракт —
формат файлів у `output/<model>/` (instance-маска `npy` + overlay PNG).

---

## 1. Передумови
- **Python 3.10+** (тестовано на 3.13), `pip`, `git`.
- Windows / Linux / macOS. (Розробка велась на Windows; шляхи в коді відносні — переносно.)
- **Windows:** PyTorch (його використовують моделі сегментації / cellsegkit) потребує **Microsoft Visual C++
  Redistributable (x64)** — на свіжій системі чи віртуалці його зазвичай немає. Постав один раз:
  <https://aka.ms/vs/17/release/vc_redist.x64.exe> — інакше `import torch` падає з `OSError: [WinError 126]
  ... c10.dll`. (Linux/macOS це не стосується.)
- GPU — **опційно, лише NVIDIA (CUDA)**: дуже пришвидшує сегментацію. ⚠️ Підступ: дефолтний
  `pip install torch` ставить **CPU-only** збірку, тож навіть із NVIDIA-картою рахунок піде на CPU.
  Щоб увімкнути GPU — `python tools/setup_gpu.py` (визначить карту, поставить CUDA-torch, перевірить;
  на AMD/Intel чесно скаже, що CUDA не підходить → лишається CPU). Без GPU усе працює, лише повільніше.

## 2. Встановлення
```bash
git clone https://github.com/MykhailoFesenko/cryobiology-mask-picker.git cryobiology
cd cryobiology

# (рекомендовано) віртуальне середовище
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate

# 2a. Mask Picker (редактор) — мінімум для розмітки/доразмітки:
pip install -r apps/mask_picker/requirements.txt

# 2b. cellsegkit (рендер/експорт масок; потрібен для bake та для сегментації):
pip install -e ./shared/cellsegkit

# 2c. (опційно) ML-моделі для сегментації — лише ті, що потрібні:
pip install cellpose            # cyto2/cyto/nuclei (built-in, без окремих ваг)
pip install instanseg-torch     # instanseg
pip install ultralytics         # yolo11_*
```

## 3. Ваги моделей (важливо — відповідь на часте питання)
| Моделі | Потрібні ваги з `cryobiology4/weights/`? |
|---|---|
| `cyto2`, `cyto`, `nuclei`, `instanseg` (public) | **НІ** — качаються самими пакетами при першому запуску |
| `cpsam_finetuned`, `instanseg_neuroblastoma`, `instanseg_0605`, `yolo11_512`, `yolo11_680` | **ТАК** — кастомні ваги Cryobiology IV (~1.8 ГБ) |

**Ваги НЕ зберігаються у git-репо** (1.8 ГБ — задорого для git). Вони доступні окремо
(GitHub Release / Google Drive — посилання у `README.md`). Поклади їх у `./cryobiology4/weights/`
(або вкажи `CRYOBIOLOGY4_WEIGHTS` env var). **Без кастомних ваг сегментація все одно працює** — на
built-in моделях (`instanseg`, `cyto2`). Зверни увагу: built-in моделі тягнуть свою модель при першому
запуску (`instanseg` кешується в пакеті; `cyto2` = Cellpose-SAM качає з HuggingFace) — тобто **потрібен
інтернет**. Якщо мережа/сертифікати підводять — поклади ваги вручну (офлайн), а SSL-помилки див. §5.

> 💡 **Перший прогін «застряг на першій моделі»?** Майже завжди це НЕ зависання. Розрізняй
> два різні «0%»:
> - **Бар завантаження** (`… 1.15G/1.15G`) стоїть на 0% → це мережа/SSL (§5): йде качання
>   `cyto2`=Cellpose-SAM (**~1.2 ГБ**) з HuggingFace. cellpose показує власний tqdm у консолі.
> - **Бар `Processing images 0/N`** стоїть на 0% вже ПІСЛЯ того, як завантаження дійшло до 100% →
>   це **інференс на CPU**. Без NVIDIA GPU `cyto2/cyto/nuclei` (у cellpose ≥4 = Cellpose-SAM) рахують
>   **кілька-десятки хвилин на ОДНЕ фото**; бар і таймер часу не рухаються, поки не дорахується перше
>   фото — це нормально, дай йому час. Швидка перевірка пайплайну за хвилини, а не години:
>   `python apps/segmentation/run_segmentation.py --model instanseg --data-dir <dir>` (instanseg на CPU у рази швидший).
> - **Скачати все наперед (Release-ваги + base-моделі cpsam/instanseg), далі без пауз:**
>   `python tools/download_weights.py --warmup`. Завантаження разове (кеш у `~/.cellpose/` тощо).

## 4. Запуск
### Крок 1 — сегментація (заповнити `output/`)
```bash
# одна модель (built-in, без ваг):
python apps/segmentation/run_segmentation.py --data-dir data/my_dataset
# всі моделі підряд:
python apps/segmentation/run_segmentation.py --all --data-dir data/my_dataset
python apps/segmentation/run_segmentation.py --help   # усі опції
```
Покласти вхідні фото у `data/my_dataset/images/` (або ZIP у `data/my_dataset/` — розпакується сам).

### Крок 2 — Mask Picker (відбір + доразмітка)
```bash
python apps/mask_picker/app.py --workspace data/my_dataset
# → відкрий http://127.0.0.1:5000
# Windows-зручність: run_mask_picker.bat  (дефолт: data/vesicles_good)
```

### Фінальний датасет замовнику
```bash
python tools/launchers/bake_all.py --data-dir data/my_dataset --pack
# + --group-masks якщо потрібні per-pixel маски груп (за замовч. вимкнено)
```

## 5. Поведінка на чистому ПК (перевірено по коду)
- **Шляхи переносні:** усе через `__file__`-відносні шляхи + `config.yaml` з відносними шляхами +
  `.bat` через `%~dp0`. Папку репо можна покласти будь-куди; Python — будь-де.
- **Нема папки `data/`:** Mask Picker у `--workspace` режимі **сам створює** структуру (порожній старт ОК);
  сегментація теж створює `data-dir`. Порожній каталог → застосунок стартує без помилки.
- **Нема залежності:** Mask Picker деградує мʼяко — без `opencv`/`cellsegkit` малювання/збереження
  працюють, а seed/bake повертають 503 (не падає). Сегментація **пропускає** моделі, чиїх бібліотек нема
  (ImportError → skip, не крах). Але `Flask`+`numpy` — обовʼязкові (з `requirements.txt`).
- **Windows `WinError 126` / не вантажиться `c10.dll` при `import torch`:** не встановлено Microsoft Visual C++
  Redistributable (див. §1) → постав `vc_redist.x64.exe` і повтори. Це системна вимога PyTorch, не баг застосунку.
- **`SSL: CERTIFICATE_VERIFY_FAILED` при завантаженні ваг/моделей (свіжа Windows/ВМ):** неповне сховище
  кореневих сертифікатів. Завантажувач авто-підставляє повний набір `certifi` (`SSL_CERT_FILE`); якщо не
  помогло — `pip install --upgrade certifi` і повтори, **або** поклади файли ваг вручну в
  `cryobiology4/weights/` (з Google Drive — тоді мережа для ваг не потрібна). Не баг застосунку.
- **Передача масок між застосунками:** лише через спільний `data/<dataset>/output/` — запусти сегментацію
  ДО Mask Picker. Mask Picker авто-знаходить моделі (підпапки з `overlay/` всередині `output/`).
- **Рекомендований smoke-тест чистого середовища:** `python -m venv` → `pip install -r ...` →
  `pip install -e ./shared/cellsegkit` → `python apps/mask_picker/app.py --workspace data/_test` →
  відкрити localhost:5000 (має стартувати порожнім). Тоді `cd apps/mask_picker && python -m pytest tests/ -q` (237 green).

## 6. Тести
```bash
cd apps/mask_picker
# Windows: $env:MPLBACKEND="Agg"   |   Linux: export MPLBACKEND=Agg
python -m pytest tests/ -q          # очікувано: 237 passed
```

## 7. Структура (коротко)
```
apps/mask_picker/   — Flask-редактор (Cleanup/Polygons/Groups) + bake
apps/segmentation/  — драйвер ML-моделей → output/
shared/cellsegkit/  — запозичений тулкіт рендеру/експорту (Cryobiology III, MIT — див. NOTICE)
cryobiology4/        — reference-код моделей + config (ваги — окремо, не в git)
tools/launchers/     — bake_all.py (--pack deliverable)
docs/                — документація (architecture/, TECHNICAL_REPORT, PROJECT_JOURNEY)
data/<dataset>/      — дані (НЕ в git): images/ output/ selected/ polygons/ groups/
```
Глибше: `docs/TECHNICAL_REPORT.md`, `docs/architecture/README.md`.
