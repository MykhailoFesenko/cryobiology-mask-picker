# Инструкция для разметки в Mask Picker

Эта папка - переносимая задача для разметки. Внутри уже лежат исходные фото и
результаты моделей, поэтому участнику не нужно запускать сегментацию заново.

## Что внутри

```text
START_MASK_PICKER.bat            быстрый запуск этой задачи на Windows
README_FOR_ANNOTATOR.md          эта инструкция
MANIFEST.json                    список фото в задаче
images/                          исходные фото
output/<model>/overlay/           превью масок модели
output/<model>/npy/               instance-mask модели
output/<model>/png/               mask png
output/<model>/yolo/              YOLO polygons/boxes модели
selected/                         то, что выбрано/исправлено человеком
polygons/                         ручная polygon-разметка LabelMe
skipped/                          пометки пропущенных фото
labels.json                       классы: nucleus и vesicle
selections.json                   журнал решений
```

`output/` - исходные результаты моделей. Их не редактируем руками и не удаляем.
Все человеческие правки пишутся в `selected/`, `polygons/` и
`selections.json`.

## Важное про запуск

Task zip содержит фото и model-output, но не содержит весь код проекта и веса
моделей. Для запуска нужен полный проект Cryobiology с папками
`apps/mask_picker/` и `shared/cellsegkit/`.

Самый простой вариант:

1. Распаковать task zip внутрь или рядом с полной папкой Cryobiology.
2. Дважды кликнуть `START_MASK_PICKER.bat` внутри task-папки.

Launcher сам:

- считает папку с `START_MASK_PICKER.bat` workspace-папкой;
- ищет корень проекта в родительских папках;
- если нужно, ставит `apps/mask_picker/requirements.txt`;
- подключает `shared/cellsegkit` через `PYTHONPATH`;
- запускает Mask Picker с `--workspace <эта task-папка>`.

Если task-папка лежит совсем отдельно, перед запуском можно задать путь к
проекту:

```bat
set CRYOBIOLOGY_ROOT=C:\path\to\Cryobiology
START_MASK_PICKER.bat
```

Ручной запуск из корня проекта:

```powershell
python -m pip install --user -r apps/mask_picker/requirements.txt
set PYTHONPATH=%CD%\shared\cellsegkit;%PYTHONPATH%
python apps/mask_picker/app.py --workspace <путь-к-task-папке>
```

Если Windows не видит Python, нужно установить Python 3.10+ с python.org и
включить опцию "Add python.exe to PATH". Абсолютного пути к Python из компьютера
Михаила в task-пакете нет.

## Базовый порядок работы

1. Впиши свое имя в поле "Кто размечает".
2. Для фото выбери лучший вариант модели кнопкой/цифрой `1`..`9`.
3. Если у выбранной маски есть лишние объекты, открой Cleanup кнопкой с метлой
   или клавишей `C`, кликни по false-positive объектам и нажми Save/Enter.
4. Если нужно дорисовать или исправить контуры, открой Polygons.
5. В Polygons выбери активный label: `nucleus` или `vesicle`.
6. Нарисуй/исправь полигоны, затем нажми Save Polygons. Это запекает правки в
   `selected/<model>/`.
7. После обработки задачи нажми Export в верхней панели и отправь архив назад.

## Два лейбла на одном фото

Да, текущий формат это поддерживает. Один файл
`polygons/<имя_фото>.json` может содержать одновременно shapes с label
`nucleus` и shapes с label `vesicle`.

Важно:

- label задается на уровне каждого polygon-shape, а не на уровне всего фото;
- одно фото может иметь оба класса;
- при bake/export в `selected/<model>/yolo/<stem>.txt` классы пишутся разными
  class id из `labels.json`;
- в маске `npy/png` это общий instance-mask, а классовая семантика живет в
  polygons/YOLO;
- если модель для nuclei и модель для vesicles разные, используй Multi-seed и
  явно выбери модель для каждого label.

Мы не храним `default_seed_model` в `labels.json`: набор моделей рабочий и
может меняться. Поэтому Multi-seed не выбирает первую модель молча. Если для
лейбла нет сохраненного локального выбора в браузере, строка стартует без
модели, и модель нужно выбрать явно.

## Модель → label (per-image base_label, v1.7.1)

В Polygons-toolbar справа от Active label есть селектор `Модель → <label>`.
Это класс, в который запекается **остаточная модельная маска**: те instance-ы
из `output/<model>/npy/<stem>.npy`, которые НЕ были rejected в Cleanup и НЕ
перекрыты ручным polygon-shape.

Зачем это нужно: если модель ловит vesicle, а ты не хочешь обводить каждую
из них руками, оставь их как модельную маску, но укажи `Модель → vesicle`.
В YOLO-export они пойдут с правильным class id, а не дефолтным `0` (nucleus).

Правила:

- значение хранится **per image** в `selections.json[stem].base_label`;
- меняй селектор → автосохраняется (`POST /api/base-label/<stem>`);
- если не выбрать ничего — default остаточная маска идет в class 0
  (первый класс из `labels.json`);
- ручные polygon-shape всегда несут свой собственный label, независимо от
  base_label.

Для vesicles_good-датасета почти всегда `base_label=vesicle`, потому что
manual-polygon-ами быстрее обвести редкие nuclei, а везикулы остаются от
модели.

## Multi-seed

В Polygons нажми Multi-seed.

Для каждого label:

- включи строку только если хочешь добавить shapes этого класса;
- выбери модель явно;
- например, `nucleus` можно взять из одной модели, `vesicle` из другой;
- Apply добавит полигоны в текущий `polygons/<stem>.json`;
- почти одинаковые shapes с IoU больше 60% пропускаются, чтобы не плодить
  дубли.

После Multi-seed все равно проверь руками: убрать лишнее, поправить контур,
дорисовать пропуски.

## Cleanup

Cleanup работает от выбранной модели. Сначала выбери модель для фото, затем
открой Cleanup.

Save/Enter переписывает:

```text
selected/<model>/npy/<stem>.npy
selected/<model>/png/<stem>.png
selected/<model>/yolo/<stem>.txt
selected/<model>/overlay/<stem>.png
selected/<model>/cleanup.json
```

`output/<model>/...` не меняется. Перед перезаписью старые файлы в `selected/`
уходят в backup.

## Polygons и bake

Polygons - главный источник ручной точной разметки. Save Polygons сохраняет
`polygons/<stem>.json` и сразу запекает его в raster/YOLO-выходы выбранной
модели в `selected/<model>/`.

Перепечь все обработанные фото пачкой:

```powershell
python tools/launchers/bake_all.py --data-dir <путь-к-task-папке>
```

Перепечь и сразу собрать финальный dataset zip:

```powershell
python tools/launchers/bake_all.py --data-dir <путь-к-task-папке> --pack
```

Архив появится в `_archive/dataset_<имя-папки>.zip`.

## Экспорт и отправка назад

В workspace-режиме верхняя кнопка Export отдает архив с:

```text
selected/
polygons/
selections.json
```

Этого достаточно, чтобы объединить результат с основной папкой. Фото и
модельные `output/` обратно обычно отправлять не нужно, потому что они уже есть
у автора задачи.

## Как автору делить задачу

Собрать отдельные фото:

```powershell
python tools/launchers/make_annotation_task.py --data-dir data/vesicles_good --stems db_img_0084,db_img_0169 --name marina_part01
```

Разбить весь набор на несколько архивов:

```powershell
python tools/launchers/make_annotation_task.py --data-dir data/vesicles_good --all --parts 4 --name vesicles_good
```

Каждый архив будет самостоятельной workspace-папкой с исходными фото,
лейблами, уже прогнанными моделями и `START_MASK_PICKER.bat`.
