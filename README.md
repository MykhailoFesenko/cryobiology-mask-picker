# Cryobiology V — Cell Mask Annotation Pipeline

> Human-in-the-loop інструмент для побудови **ground-truth датасетів** сегментації
> клітинної мікроскопії: ML дає чорнові маски → людина у браузері їх відбирає,
> виправляє й групує → система запікає узгоджений датасет для аналізу.
>
> *Human-in-the-loop pipeline to turn rough ML cell-segmentation masks into a clean,
> grouped ground-truth dataset (instance + semantic masks + cell groups).*

**Версія 2.0.0** · Apache-2.0 · pytest **237/237** · Python 3.10+

```
 apps/segmentation  ─▶  data/<dataset>/output/  ─▶  apps/mask_picker  ─▶  deliverable
   (ML-моделі:           (чорнові instance-маски)    (Cleanup·Polygons·    (dense npy +
    Cellpose/InstanSeg/                               Groups + bake)        semantic + groups)
    StarDist/YOLO)
```

---

## Що це
Сегментаційні моделі помиляються на складних знімках (пропуски, хибні обʼєкти, злиплі
клітини). Розмічати з нуля — дорого. Mask Picker бере **найкращий ML-вихід** і дає
анотатору швидко довести його до ідеалу та структурувати у клітини («1 ядро + N везикул»).

### Три інструменти в одному редакторі
- **Cleanup** — відхилити погані маски + позначити пропуски.
- **Polygons** — домалювати/виправити маски (draw / pick / seed-from-mask).
- **Groups** — обʼєднати інстанси у клітини, класифікувати за правилами.

### Особливості
- Робота з виходами **7 моделей** одночасно (відбір найкращої per-фото).
- Стабільні ID між запіканнями (reserved-range) → групи не «дрейфують».
- Глобальний хронологічний undo/redo на всі інструменти.
- Lazy-bake + автозбереження без гонок; фінальний deliverable у dense `1..N`.
- **237 автотестів**, повна архітектурна документація, browser-верифіковані UI-фікси.

## Складові
| Шлях | Що |
|---|---|
| `apps/mask_picker/` | Flask-редактор (Cleanup/Polygons/Groups) + bake |
| `apps/segmentation/` | драйвер ML-моделей → `output/` |
| `shared/cellsegkit/` | запозичений тулкіт рендеру/експорту (Cryobiology III, MIT — див. `NOTICE`) |
| `cryobiology4/` | reference-код моделей + config (ваги — окремо, не в git) |
| `tools/` | `bake_all.py` (`--pack` deliverable), інваріант-верифікатори |
| `docs/` | технічна документація |

## Швидкий старт
Повна інструкція (встановлення, ваги, чистий ПК) → **[`INSTALL.md`](INSTALL.md)**.
```bash
pip install -r apps/mask_picker/requirements.txt
pip install -e ./shared/cellsegkit
python apps/mask_picker/app.py --workspace data/my_dataset   # → http://127.0.0.1:5000
```
Фінальний датасет: `python tools/launchers/bake_all.py --data-dir data/my_dataset --pack`.

## Документація
- **[`docs/TECHNICAL_REPORT.md`](docs/TECHNICAL_REPORT.md)** — цілісна технічна записка.
- **[`docs/architecture/`](docs/architecture/README.md)** — детальна архітектура (14 підсистем, front↔back).
- **[`docs/PROJECT_JOURNEY.md`](docs/PROJECT_JOURNEY.md)** — шлях проєкту й складнощі.

## Дані та ваги моделей
Не зберігаються в репо (великі/чутливі), доступні окремо:
- **Розмічені датасети** (vesicles, nuclei): [Google Drive](https://drive.google.com/drive/folders/1jWNuxl-E7uaGRc3ubgug4RKz8cem_QTA).
- **Ваги моделей** (~1.8 ГБ): у **GitHub Releases** — завантажуються **автоматично** при першому
  запуску сегментації (`tools/download_weights.py`). Built-in моделі (cyto2) ваг не потребують.

## Ліцензія
Apache License 2.0 (`LICENSE`). Запозичені компоненти й ML-моделі — `NOTICE`.
