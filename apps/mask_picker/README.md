# Mask Picker

Flask-редактор для відбору й доразмітки масок сегментації клітин (три інструменти:
Cleanup / Polygons / Groups) + запікання фінального датасету.

Це частина пайплайну **Cryobiology V**. Повна інструкція встановлення/запуску — у
кореневому [`../../INSTALL.md`](../../INSTALL.md) та [`../../README.md`](../../README.md).

## Запуск
```bash
pip install -r requirements.txt
pip install -e ../../shared/cellsegkit      # для bake/export
python app.py --workspace ../../data/<dataset>   # → http://127.0.0.1:5000
```

## Тести
```bash
# Windows: $env:MPLBACKEND="Agg"  |  Linux/macOS: export MPLBACKEND=Agg
python -m pytest tests/ -q          # 237 passed
```

## Структура
- `app.py` — Flask wiring + CLI.
- `state.py` — Config / StateStore / atomic write (foundation).
- `cleanup.py` / `polygons.py` / `groups.py` / `group_classes.py` — доменна логіка.
- `baking.py` / `data_sync.py` — запікання + cross-cutting синхронізація.
- `routes/` — HTTP API (blueprints).
- `static/` — frontend (vanilla ES-модулі, Canvas/SVG).
- `tests/` — pytest.

## Архітектура
Детально — [`../../docs/architecture/README.md`](../../docs/architecture/README.md)
(14 підсистем, front↔back) і [`../../docs/TECHNICAL_REPORT.md`](../../docs/TECHNICAL_REPORT.md).
