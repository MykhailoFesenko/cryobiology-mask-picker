"""
Catalog: image grid + clean overlay rendering (приховує rejected-інстанси
у каталог-тайлах через PIL replace patches).

`CatalogService(cfg, state)` — заміна закритого `build_catalog/get_catalog`
з create_app. Має `.build()`, `.get(fresh)`, `.invalidate()` для зовнішнього
очищення кешу (наприклад, після workspace import).

Залежить від: state.py, cleanup.py (через _RGB_CACHE invalidation немає,
але stem normalisation тут).
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Optional

from state import (
    CLEANUP_AVAILABLE,
    Config,
    IMAGE_EXTS,
    Image,
    ModelSource,
    StateStore,
    _load_original_image_array,
    np,
)


# ---------------------------------------------------------------------------
# Stem normalisation
# ---------------------------------------------------------------------------

def _normalize_stem(stem: str, deduplicate: bool) -> str:
    """'Копия db_img_0277' -> 'db_img_0277' якщо deduplicate=True."""
    if deduplicate and stem.startswith("Копия "):
        return stem[len("Копия "):]
    return stem


# ---------------------------------------------------------------------------
# Clean overlay rendering — приховує rejected-інстанси у каталог-тайлах
# ---------------------------------------------------------------------------

# Cache: (model, stem) → (selections_mtime, png_bytes). Інвалідується через mtime.
_CLEAN_OVERLAY_CACHE: dict[tuple[str, str], tuple[float, bytes]] = {}
_CLEAN_OVERLAY_PAD = 4  # px, як у JS _paintRejectedFromOriginal


def _render_clean_overlay_bytes(overlay_path: Path, npy_path: Path,
                                original_arr, rejected_ids: set[int]) -> bytes:
    """
    Перемальовує overlay-PNG, копіюючи pixels оригіналу у bbox кожного
    rejected-інстанса (як `_paintRejectedFromOriginal` у frontend).
    Повертає PNG bytes.
    """
    labels = np.load(npy_path)
    overlay = Image.open(overlay_path).convert("RGBA")

    # Якщо розміри original не співпадають з overlay — приводимо.
    if original_arr.ndim == 2:
        original = Image.fromarray(original_arr).convert("RGBA")
    else:
        original = Image.fromarray(original_arr[..., :3]).convert("RGBA")
    if original.size != overlay.size:
        original = original.resize(overlay.size, Image.NEAREST)

    H, W = labels.shape
    pad = _CLEAN_OVERLAY_PAD
    for rid in rejected_ids:
        mask = (labels == int(rid))
        if not mask.any():
            continue
        ys, xs = np.where(mask)
        y0 = max(0, int(ys.min()) - pad)
        x0 = max(0, int(xs.min()) - pad)
        y1 = min(H - 1, int(ys.max()) + pad)
        x1 = min(W - 1, int(xs.max()) + pad)
        bbox = (x0, y0, x1 + 1, y1 + 1)
        overlay.paste(original.crop(bbox), bbox)

    buf = io.BytesIO()
    overlay.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _get_clean_overlay_bytes(state: StateStore, cfg: Config, m: ModelSource,
                              stem: str) -> Optional[bytes]:
    """
    Повертає PNG bytes clean-overlay для (stem, m.name), або None якщо
    нема rejected у selections.json для цієї моделі (або відсутні npy/original).
    """
    cleanup = state.get_cleanup(stem)
    if not cleanup or cleanup.get("model") != m.name:
        return None
    rejected = set(int(x) for x in (cleanup.get("rejected_instances") or []))
    if not rejected:
        return None

    npy_dir = m.npy_dir
    if not npy_dir:
        return None
    npy_path = npy_dir / f"{stem}.npy"
    if not npy_path.is_file():
        return None
    overlay_path = m.overlay_path(stem)
    if not overlay_path:
        return None

    # Cache-check за mtime selections.json + npy + original (комбінований ключ).
    selections_mtime = state.path.stat().st_mtime if state.path.exists() else 0.0
    npy_mtime = npy_path.stat().st_mtime
    cache_key = (m.name, stem)
    cached = _CLEAN_OVERLAY_CACHE.get(cache_key)
    combined_mtime = selections_mtime + npy_mtime
    if cached and cached[0] == combined_mtime:
        return cached[1]

    original_arr = _load_original_image_array(cfg.images_dir, stem)
    if original_arr is None:
        return None

    rendered = _render_clean_overlay_bytes(overlay_path, npy_path, original_arr, rejected)
    _CLEAN_OVERLAY_CACHE[cache_key] = (combined_mtime, rendered)
    return rendered


# ---------------------------------------------------------------------------
# CatalogService — replaces build_catalog/get_catalog closures from create_app
# ---------------------------------------------------------------------------

class CatalogService:
    """
    Кеш-обгортка над `build_catalog()`. 30-секундний TTL + явна
    `invalidate()` (викликається з workspace import / exclude / switch).

    Використання:
        catalog = CatalogService(cfg, state)
        items = catalog.get(fresh=False)
        catalog.invalidate()  # після зміни (exclude, import, switch)
    """

    _CACHE_TTL = 30  # seconds

    def __init__(self, cfg: Config, state: StateStore):
        self.cfg = cfg
        self.state = state
        self._items: Optional[list[dict]] = None
        self._ts: float = 0.0

    def build(self) -> list[dict]:
        """Build catalog from disk (no cache)."""
        cfg = self.cfg
        state = self.state
        items: list[dict] = []
        if not cfg.images_dir.exists():
            return items
        seen_stems = set()
        for p in sorted(cfg.images_dir.iterdir()):
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            stem = _normalize_stem(p.stem, cfg.deduplicate_by_stem)
            if cfg.deduplicate_by_stem and stem in seen_stems:
                continue
            seen_stems.add(stem)
            # яку саме фізичну картинку будемо показувати?
            chosen_path = p
            # Якщо dedupe і це "Копия", перевір чи є не-копія — віддамо не-копію
            if cfg.deduplicate_by_stem and p.stem != stem:
                non_copy = cfg.images_dir / f"{stem}{p.suffix}"
                if non_copy.exists():
                    chosen_path = non_copy
            # Перевіряємо обидва варіанти stem'а (звичайний і "Копия ...") — для
            # legacy-запусків, де файли збережені тільки з префіксом "Копия ".
            stems_to_check = {stem, p.stem, f"Копия {stem}"}
            available = [m.name for m in cfg.models
                         if any(m.has_image(s) for s in stems_to_check)]
            items.append({
                "stem": stem,
                "image_filename": chosen_path.name,
                "available_models": available,
                "state": state.get(stem),
            })

        # v1.6.6: додаємо excluded items з _excluded/ (їх нема в images_dir).
        excluded_dir = cfg.images_dir.parent / "_excluded"
        if excluded_dir.exists():
            for p in sorted(excluded_dir.iterdir()):
                if p.suffix.lower() not in IMAGE_EXTS:
                    continue
                stem = _normalize_stem(p.stem, cfg.deduplicate_by_stem)
                if cfg.deduplicate_by_stem and stem in seen_stems:
                    continue
                seen_stems.add(stem)
                # state може бути відсутній (файл перенесений вручну) — ставимо minimal.
                st = state.get(stem) or {"status": "excluded", "model": None}
                items.append({
                    "stem": stem,
                    "image_filename": p.name,
                    "available_models": [],
                    "state": st,
                    "excluded": True,
                })
        return items

    def get(self, fresh: bool = False) -> list[dict]:
        """Cached read with 30s TTL. Use fresh=True to force rebuild."""
        now = time.time()
        if fresh or self._items is None or now - self._ts > self._CACHE_TTL:
            self._items = self.build()
            self._ts = now
        return self._items

    def invalidate(self) -> None:
        """Explicit cache reset (called after workspace switch / exclude / import)."""
        self._items = None
        self._ts = 0.0
