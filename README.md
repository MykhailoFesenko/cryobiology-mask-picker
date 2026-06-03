# Cryobiology V — Cell Mask Annotation Pipeline

Human-in-the-loop пайплайн для побудови ground-truth датасетів сегментації клітинної
мікроскопії: ML-моделі дають чорнові маски → людина у браузері відбирає/виправляє/групує →
система запікає узгоджений датасет (instance + semantic маски + групи «1 ядро + N везикул»).

```
 apps/segmentation  ──►  data/<dataset>/output/  ──►  apps/mask_picker  ──►  deliverable
 (ML: Cellpose/InstanSeg/         (чорнові маски)      (Cleanup/Polygons/     (dense npy +
  StarDist/YOLO)                                        Groups + bake)         semantic + groups)
```

## Складові
- **`apps/mask_picker/`** — Flask-редактор з трьома інструментами (Cleanup / Polygons / Groups)
  + запікання фінального датасету. Серце системи.
- **`apps/segmentation/`** — драйвер ML-моделей, що наповнює `output/` чорновими масками.
- **`shared/cellsegkit/`** — запозичений тулкіт рендеру/експорту (Cryobiology III, MIT — див. `NOTICE`).

Два застосунки звʼязані лише **спільною папкою даних** `data/<dataset>/` (файловий контракт,
не прямий виклик): сегментація пише `output/`, Mask Picker його читає.

## Швидкий старт
Повна інструкція (встановлення, ваги, запуск, чистий ПК) → **[`INSTALL.md`](INSTALL.md)**.
```bash
pip install -r apps/mask_picker/requirements.txt
pip install -e ./shared/cellsegkit
python apps/mask_picker/app.py --workspace data/my_dataset   # → http://127.0.0.1:5000
```

## Документація
- **[`docs/TECHNICAL_REPORT.md`](docs/TECHNICAL_REPORT.md)** — цілісна технічна записка.
- **[`docs/architecture/`](docs/architecture/README.md)** — детальна архітектура (14 підсистем, front↔back).
- **[`docs/PROJECT_JOURNEY.md`](docs/PROJECT_JOURNEY.md)** — шлях проєкту, складнощі.
- **[`docs/ROADMAP.md`](docs/ROADMAP.md)** — плани.

## Дані та ваги моделей
Датасети та ваги моделей (~1.8 ГБ) **не зберігаються в репо**. Доступні окремо:
- Розмічені датасети (vesicles, nuclei): _[посилання на Google Drive — додати]_.
- Кастомні ваги Cryobiology IV → `cryobiology4/weights/`: _[посилання — додати]_.
- Built-in моделі (cyto2 тощо) працюють без окремих ваг.

## Статус
APP_VERSION 1.16.2 · pytest 237/237 · аудит пройдено (`docs/architecture/_AUDIT_FINDINGS.md`).

## Ліцензія та атрибуція
Apache License 2.0 (`LICENSE`). Запозичені компоненти — `NOTICE` (cellsegkit / Cryobiology III,
ML-моделі Cellpose/InstanSeg/StarDist/YOLO/SAM).
