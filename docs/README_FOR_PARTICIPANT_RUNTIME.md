# Cryobiology Mask Picker runtime

Этот zip нужен участнику, если у него нет полной папки проекта Cryobiology.
Он содержит только код инструмента разметки, bake-скрипт и локальный пакет
`cellsegkit`.
Веса моделей и скрипты для повторного прогона ML сюда не входят: для текущей
разметки они не нужны, потому что task zip уже содержит `output/<model>/...`.

**Текущая версия runtime: v1.7.1** (2026-05-08). Новое в этой версии:
per-image `base_label` (селектор `Модель → label` в Polygons toolbar) — задает
class id для остаточной модельной маски. Подробности — в
`docs/README_FOR_ANNOTATOR_VESICLES.md`, секция "Модель → label".

## Самый простой запуск

1. Распакуй runtime zip.
2. Внутрь распакованной папки положи task-папку из zip вида
   `vesicles_good_annotation_full_2026-05-07.zip`.
3. Открой task-папку.
4. Дважды кликни `START_MASK_PICKER.bat`.

Пример структуры:

```text
Cryobiology_mask_picker_runtime_2026-05-07/
  apps/
  shared/
  tools/
    launchers/bake_all.py
  README_FOR_PARTICIPANT_RUNTIME.md
  vesicles_good_annotation_full_2026-05-07/
    START_MASK_PICKER.bat
    images/
    output/
    labels.json
```

`START_MASK_PICKER.bat` сам найдет `apps/mask_picker/app.py` выше по папкам,
проверит зависимости и запустит Mask Picker с этой task-папкой как workspace.

## Если task-папка лежит отдельно

Перед запуском можно указать путь к runtime/project root:

```bat
set CRYOBIOLOGY_ROOT=C:\path\to\Cryobiology_mask_picker_runtime_2026-05-07
START_MASK_PICKER.bat
```

## Требования

- Windows.
- Python 3.10+.
- При установке Python включить "Add python.exe to PATH".
- Интернет на первом запуске, если Flask/numpy/Pillow/opencv еще не установлены.

Launcher пробует `python`, затем `py -3`. Абсолютных путей к компьютеру автора
в runtime/task-пакете нет.

## Bake после разметки

Если нужно перепечь все выбранные cleanup/polygon-правки и собрать финальный zip,
запусти из этой runtime-папки:

```powershell
python tools/launchers/bake_all.py --data-dir <путь-к-task-папке> --pack
```

`bake_all.py` сам подставляет `shared/cellsegkit` через `PYTHONPATH`, поэтому
ставить тяжелые ML-зависимости cellsegkit не нужно.

## Что отправлять обратно

После работы в Mask Picker нажать Export в верхней панели и отправить полученный
архив. Обычно он содержит:

```text
selected/
polygons/
selections.json
```

Фото и `output/` обратно отправлять не нужно, если автор задачи не попросил.
