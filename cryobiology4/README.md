# Криобиология 4 — ваги + референс-код

Консолідована папка з результатами попередньої команди (Cryobiology 4).
Зібрана **2026-04-20** з трьох оригінальних архівів (v2/v3/v3.1), щоб
оригінальні папки `Cells-Calculator.v2/v3/v3.1/` можна було безпечно видалити.

## Структура

```
Криобиология 4/
├── weights/                               ← всі ваги моделей (1.8 GB)
│   ├── YOLO11x-512-seg.pt                 238 MB  (v3.1)
│   ├── YOLO11x-680-seg.pt                 119 MB  (v3.1 Full)
│   ├── YOLO11x-sphero-seg.pt              119 MB  (v3.1)
│   ├── cpsam_finetuned.pth                1.2 GB  Cellpose-SAM finetuned (diameter=40)
│   ├── instanseg_20250605.pt               16 MB  InstanSeg TorchScript
│   ├── Instanseg-Neuroblastoma-v3.1.pt     16 MB  InstanSeg custom (нейробластома)
│   ├── yolov8m-det.onnx                    99 MB  bbox detector (ONNX)
│   └── stardist0602/                       12 MB  StarDist 2D (grayscale, 32 rays, 800 epochs)
│       ├── config.json
│       ├── thresholds.json
│       ├── weights_best.h5
│       ├── weights_last.h5
│       └── metrics.txt.REMOVED.md         ← витікший OpenAI-ключ, дивись файл
├── reference_code/                        ← референс-код з v3.1 Cells-Calculator.3.1/model/
│   ├── YOLOSegmenter.py                   ultralytics.YOLO + SAHI slicing
│   ├── CellposeSegmenter.py               підтримує pretrained_model=<path>
│   ├── InstanSegSegmenter.py              torch.jit.load для custom .pt
│   ├── StardistSegmenter.py               StarDist2D(None, name=..., basedir=...)
│   ├── BaseModel.py
│   ├── convert_instanseg_model.py         конвертер старих ваг → TorchScript
│   └── sahi/                              локальна копія SAHI tiling
└── config/
    ├── modelconfig-all.json               JSON-blueprint як запускати всі моделі
    ├── modelconfig.json                   скорочений конфіг
    └── requirements-v3.1.txt              requirements v3.1 (для перевірки deps)
```

## Що було в оригінальних папках і що ми НЕ взяли

- **CellsCalculatorV2.exe** — GUI-додаток, нам не потрібен.
- **CellsCalculatorV3Portable20250603/** — portable build, нам не потрібен.
- **Cells-calculator.3.1.RC1/** — release candidate з тим же кодом.
- **testimages/, UI/, scripts/, main.py** — GUI / тестові утиліти.
- **zip-архіви** — вже розпаковані.
- **metrics.txt** у stardist0602 — витікший API-ключ OpenAI (див. REMOVED.md).

## Провенанс вагів

Всі ваги беруться з **v3.1.Full** (найповніший і найсвіжіший набір).
Єдиний виняток — `stardist0602/` береться з v3 `TrainedModels.20250602/`,
бо в v3.1 його не переклали повністю.

## Використання в `run_segmentation.py`

Див. `cryobiology3/cellsegkit/loader/model_loader.py` — нові ключі
Factory використовують абсолютні шляхи до `weights/` цієї папки.
