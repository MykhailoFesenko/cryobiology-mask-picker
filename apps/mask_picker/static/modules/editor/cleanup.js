/**
 * cleanupMixin — методи Cleanup-табу: tool вибір, hover/click на base canvas,
 * toggle rejected, markers add/remove/clear, marks-canvas redraw,
 * autosave + cleanup-export (rebake) у /api/cleanup* ендпоінти.
 * Undo/redo — глобальний historyMixin (мутації кличуть _historyPush("cleanup")).
 *
 * == ID-простір rejected ==
 *   cu.rejectedSet — instance IDs у raw output просторі (з GET /api/labels-rgb/).
 *   Bake застосовує: cleaned[isin(rejected)]=0 → filtered npy без них.
 *
 * == Cross-tab guards ==
 *   `_polyHasShapeOnInstance(id)` — v1.14.0 Bug 6 fix: pixel-accurate hit-test
 *   (fast path centroid + fallback scan bbox-pixels через _pointInPoly) щоб
 *   не дати toggle reject над уже-полігонізованим instance. Concave shapes
 *   тепер ловляться правильно (раніше mean centroid падав на C-подібних).
 *
 * == Backend endpoints ==
 *   GET  /api/labels-rgb/<model>/<stem>.png  — raw labels.
 *   POST /api/cleanup/<stem>                  — autosave rejected + markers.
 *   POST /api/cleanup-export/<stem>           — full re-bake (рідко — клік
 *                                                "🔥" у workspace).
 */

import { $, _pointInPoly, showToast } from "../util.js";
import { api } from "../api.js";
import { state as appState } from "../state.js";

const MARKER_CLICK_PX = 12;  // поріг "клік по маркеру" у CSS-пікселях

export const cleanupMixin = {
  _setCleanupTool(tool) {
    if (tool !== "reject" && tool !== "marker") return;
    this.state.cleanup.tool = tool;
    this._toolbarCleanup().querySelectorAll("[data-cleanup-tool]").forEach((b) => {
      b.classList.toggle("btn--mini-active", b.dataset.cleanupTool === tool);
    });
    this._updateHint();
  },

  _onBaseMouseMove(e) {
    if (this.state.activeTab !== "cleanup") return;
    if (this.state.isPanning) return;
    const cu = this.state.cleanup;
    const pt = this._canvasCoordsFromEvent(e);
    if (!pt) {
      if (cu.hoverId) { cu.hoverId = 0; this._cleanupRedraw(); }
      if (cu.hoverMarkerIdx !== -1) { cu.hoverMarkerIdx = -1; this._polyRedrawMarkersOnly(); }
      this._wrap().classList.remove("has-cursor-ok");
      return;
    }
    if (cu.tool === "reject") {
      if (!cu.labelsInt32) return;
      const id = cu.labelsInt32[Math.floor(pt.y) * this.state.W + Math.floor(pt.x)] | 0;
      if (id !== cu.hoverId) {
        cu.hoverId = id;
        this._cleanupRedraw();
        this._wrap().classList.toggle("has-cursor-ok", id !== 0);
      }
    } else {
      const idx = this._findMarkerNear(pt.x, pt.y);
      if (idx !== cu.hoverMarkerIdx) {
        cu.hoverMarkerIdx = idx;
        this._polyRedrawMarkersOnly();
        this._wrap().classList.toggle("has-cursor-ok", true);
      }
    }
  },

  _onBaseMouseLeave() {
    const cu = this.state.cleanup;
    if (cu.hoverId) { cu.hoverId = 0; this._cleanupRedraw(); }
    if (cu.hoverMarkerIdx !== -1) { cu.hoverMarkerIdx = -1; this._polyRedrawMarkersOnly(); }
    this._wrap().classList.remove("has-cursor-ok");
  },

  _onBaseClick(e) {
    if (this.state.activeTab !== "cleanup") return;
    if (this.state.isPanning) return;
    const pt = this._canvasCoordsFromEvent(e);
    if (!pt) return;
    const cu = this.state.cleanup;
    if (cu.tool === "reject") {
      if (!cu.labelsInt32) return;
      const id = cu.labelsInt32[Math.floor(pt.y) * this.state.W + Math.floor(pt.x)] | 0;
      if (!id) return;
      this._cleanupToggleInstance(id, true);
    } else {
      const idx = this._findMarkerNear(pt.x, pt.y);
      if (idx >= 0) {
        this._cleanupRemoveMarker(idx, true);
      } else {
        this._cleanupAddMarker(pt.x, pt.y, true);
      }
    }
  },

  _findMarkerNear(x, y) {
    const markers = this.state.cleanup.markers;
    if (!markers || !markers.length) return -1;
    const threshold = MARKER_CLICK_PX * this._pxPerCss();
    let best = -1, bestD = threshold * threshold;
    for (let i = 0; i < markers.length; i++) {
      const dx = markers[i].x - x, dy = markers[i].y - y;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  },

  _cleanupToggleInstance(id, recordUndo) {
    const cu = this.state.cleanup;
    // Bug A/B fix (v1.16.1): інстанс під полігоном — DERIVED rejected
    // (завжди червоний, бо полігон його заміщає). Блокуємо toggle у
    // ОБИДВА боки (раніше блокувало лише додавання → можна було зняти
    // галку, а назад вже ні → застрягав у хибному стані). Зняти reject
    // можна лише прибравши polygon-shape у Polygons-табі.
    if (this._polyCoveredInstances().has(id)) {
      showToast("Цей інстанс заміщений полігоном (завжди rejected). " +
                "Прибери shape у Polygons-табі, щоб повернути.", "err", 2800);
      return;
    }
    if (recordUndo) this._historyPush("cleanup");   // знімок ПЕРЕД мутацією
    if (cu.rejectedSet.has(id)) cu.rejectedSet.delete(id);
    else cu.rejectedSet.add(id);
    this._cleanupMarkDirty();
    this._cleanupRedraw();
    this._updateStats();
  },

  _polyHasShapeOnInstance(id) {
    // Bug 6 fix (v1.14.0): pixel-acurate hit-test замість arithmetic
    // mean centroid. Centroid concave polygon (наприклад C-подібний
    // vesicle cluster) може лежати ПОЗА формою → false negative →
    // блокування reject не спрацьовує, юзер robить reject над
    // вже-polygonized областю → конфлікт станів.
    //
    // Нова стратегія: scan-line у bbox shape — для кожного pixel,
    // що всередині polygon (через ray-casting), перевірити чи
    // labelsInt32[i] == id. Це точно (pixel-accurate), повертає
    // true тільки якщо принаймні один pixel під shape має цей iid.
    //
    // Cost: O(bbox_area × shape_count). Для типового vesicle bbox
    // ~30×30 = 900 px × 200 shapes ≈ 180k ops ≈ <10ms.
    const pg = this.state.polygons;
    const cu = this.state.cleanup;
    if (!pg.shapes.length || !cu.labelsInt32) return false;
    const W = this.state.W, H = this.state.H;
    for (const sh of pg.shapes) {
      const pts = sh.points;
      if (!pts || pts.length < 3) continue;
      // bbox shape (clip до image)
      let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
      for (const [x, y] of pts) {
        if (x < xmin) xmin = x;
        if (x > xmax) xmax = x;
        if (y < ymin) ymin = y;
        if (y > ymax) ymax = y;
      }
      const x0 = Math.max(0, Math.floor(xmin));
      const x1 = Math.min(W - 1, Math.ceil(xmax));
      const y0 = Math.max(0, Math.floor(ymin));
      const y1 = Math.min(H - 1, Math.ceil(ymax));
      if (x0 > x1 || y0 > y1) continue;
      // Sparse scan: для перформенсу, спочатку швидка перевірка центроїда
      // (фаст-шлях для convex shapes), потім fallback на повний scan.
      let sx = 0, sy = 0;
      for (const p of pts) { sx += p[0]; sy += p[1]; }
      const cx = Math.floor(sx / pts.length);
      const cy = Math.floor(sy / pts.length);
      if (cx >= 0 && cx < W && cy >= 0 && cy < H) {
        if ((cu.labelsInt32[cy * W + cx] | 0) === id) return true;
      }
      // Fallback: scan bbox-pixels, ray-cast тест для кожного.
      for (let y = y0; y <= y1; y++) {
        const yW = y * W;
        for (let x = x0; x <= x1; x++) {
          if ((cu.labelsInt32[yW + x] | 0) !== id) continue;
          if (_pointInPoly(x, y, pts)) return true;
        }
      }
    }
    return false;
  },

  /**
   * v1.16.1 (Bug A/B): множина raw instance, які >50% перекриті будь-яким
   * polygon-shape ("derived rejected" — полігон їх заміщає). Кешується у
   * cu.coveredCache; інвалідується при зміні полігонів (_polyMarkDirty) і
   * на open. Сканує УСІ polygon-shapes і повертає множину (НЕ мутує
   * rejectedSet — derived rejection, обчислюється з геометрії на льоту).
   *
   * Використовується для: рендеру (червоний), блокування toggle у Cleanup,
   * блокування вибору у Groups. Єдине джерело істини "instance під полігоном".
   */
  _polyCoveredInstances() {
    const cu = this.state.cleanup;
    const pg = this.state.polygons;
    if (!cu || !cu.labelsInt32 || !cu.available) return new Set();
    if (cu.coveredCache) return cu.coveredCache;
    const W = this.state.W, H = this.state.H;
    const labels = cu.labelsInt32;
    if (!cu.pixelCounts) {
      const counts = new Map();
      for (let i = 0; i < W * H; i++) {
        const id = labels[i];
        if (id > 0) counts.set(id, (counts.get(id) || 0) + 1);
      }
      cu.pixelCounts = counts;
    }
    const covered = new Map();
    for (const sh of (pg.shapes || [])) {
      const pts = sh.points;
      if (!pts || pts.length < 3) continue;
      let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
      for (const [x, y] of pts) {
        if (x < xmin) xmin = x; if (x > xmax) xmax = x;
        if (y < ymin) ymin = y; if (y > ymax) ymax = y;
      }
      const x0 = Math.max(0, Math.floor(xmin)), x1 = Math.min(W - 1, Math.ceil(xmax));
      const y0 = Math.max(0, Math.floor(ymin)), y1 = Math.min(H - 1, Math.ceil(ymax));
      for (let y = y0; y <= y1; y++) {
        const yW = y * W;
        for (let x = x0; x <= x1; x++) {
          const id = labels[yW + x];
          if (id <= 0) continue;
          if (!_pointInPoly(x, y, pts)) continue;
          covered.set(id, (covered.get(id) || 0) + 1);
        }
      }
    }
    const set = new Set();
    for (const [id, cov] of covered) {
      const tot = cu.pixelCounts.get(id) || cov;
      if (cov / tot > 0.5) set.add(id);
    }
    cu.coveredCache = set;
    return set;
  },

  _cleanupAddMarker(x, y, recordUndo) {
    const cu = this.state.cleanup;
    if (recordUndo) this._historyPush("cleanup");   // знімок ПЕРЕД мутацією
    cu.markers.push({ x, y });
    this._cleanupMarkDirty();
    this._polyRedrawMarkersOnly();
    this._updateStats();
  },

  _cleanupRemoveMarker(idx, recordUndo) {
    const cu = this.state.cleanup;
    if (idx < 0 || idx >= cu.markers.length) return;
    if (recordUndo) this._historyPush("cleanup");   // знімок ПЕРЕД мутацією
    cu.markers.splice(idx, 1);
    cu.hoverMarkerIdx = -1;
    this._cleanupMarkDirty();
    this._polyRedrawMarkersOnly();
    this._updateStats();
  },

  // Undo/redo тепер у historyMixin (глобальний стек). Cleanup-знімок —
  // {rejected, markers}; відновлення у _historyRestore("cleanup").

  _cleanupBulkClearRejected() {
    const cu = this.state.cleanup;
    if (!cu.rejectedSet || cu.rejectedSet.size === 0) return;
    this._historyPush("cleanup");
    cu.rejectedSet = new Set();
    this._cleanupMarkDirty();
    this._cleanupRedraw();
    this._updateStats();
  },

  _cleanupBulkClearMarkers() {
    const cu = this.state.cleanup;
    if (!cu.markers.length) return;
    this._historyPush("cleanup");
    cu.markers = [];
    this._cleanupMarkDirty();
    this._polyRedrawMarkersOnly();
    this._updateStats();
  },

  _cleanupRedraw() {
    if (this.state.activeTab !== "cleanup") return;
    const cu = this.state.cleanup;
    if (!cu.available) {
      this._cMarks().getContext("2d").clearRect(0, 0, this.state.W, this.state.H);
      return;
    }
    const { W, H } = this.state;
    const { labelsInt32, rejectedSet, hoverId, bboxes } = cu;
    // Bug B (v1.16.1): covered-полігоном instance рендеримо як rejected
    // (червоний) — derived rejected. Раніше вони лишались звичайними
    // (плутало підсвітку необведених).
    const covered = this._polyCoveredInstances();
    const ctx = this._cMarks().getContext("2d");
    const img = ctx.createImageData(W, H);
    const data = img.data;
    for (let i = 0, p = 0; i < W * H; i++, p += 4) {
      const id = labelsInt32[i];
      if (id === 0) continue;
      // covered (instance заміщений полігоном) — НЕ малюємо марок: база вже
      // показує чистий оригінал на цих пікселях (_paintRejectedFromOriginal),
      // тож instance візуально зник, без червоного контуру (юзер-запит). Явні
      // rejected лишаються червоними (керовані у Reject-tool); kept — faint.
      if (covered.has(id)) continue;
      if (rejectedSet.has(id)) {
        data[p] = 255; data[p + 1] = 60; data[p + 2] = 60; data[p + 3] = 115;
      } else {
        data[p]     = (id * 37)  & 0xff;
        data[p + 1] = (id * 67)  & 0xff;
        data[p + 2] = (id * 113) & 0xff;
        data[p + 3] = 77;
      }
    }
    ctx.putImageData(img, 0, 0);
    if (hoverId && bboxes && bboxes.has(hoverId)) {
      const bb = bboxes.get(hoverId);
      ctx.save();
      ctx.strokeStyle = "rgba(255, 220, 80, 0.95)";
      ctx.lineWidth = 2;
      ctx.strokeRect(bb.x0 - 1, bb.y0 - 1, (bb.x1 - bb.x0) + 3, (bb.y1 - bb.y0) + 3);
      ctx.restore();
    }
  },

  _cleanupMarkDirty() {
    this.state.cleanup.dirty = true;
    this.state.cleanup.dirtyExport = true;
    clearTimeout(this.state.cleanup.autosaveTimer);
    this.state.cleanup.autosaveTimer = setTimeout(() => this._cleanupAutosave(), 5000);
  },

  async _cleanupAutosave(forced = false) {
    // Day 9 Bug 2: через проміс-чергу редактора — без паралельних POST.
    return this._enqueueSave(async () => {
      const cu = this.state.cleanup;
      // forced=true (Save-кнопка) зберігає навіть якщо debounce уже зняв dirty.
      if ((!cu.dirty && !forced) || !this.state.open || !this.state.model) return null;
      try {
        const resp = await api(`/api/cleanup/${encodeURIComponent(this.state.stem)}`, {
          method: "POST",
          body: JSON.stringify({
            model: this.state.model,
            rejected_instances: cu.rejectedSet ? [...cu.rejectedSet] : [],
            markers: cu.markers.map((p) => ({ x: p.x, y: p.y })),
            user: appState.user,
          }),
        });
        cu.dirty = false;
        // Day 7: save без bake → stem незапечений. Catalog у курсі (жовта
        // крапка + попередження у Groups-табі) навіть при autosave.
        const it = appState.catalog.find((x) => x.stem === this.state.stem);
        if (it && it.state) it.state.dirty = true;
        return resp;
      } catch (e) {
        console.warn("cleanup autosave failed:", e);
        return null;
      }
    });
  },

  async _cleanupExportSave(silent, closeAfter) {
    if (!this.state.open || !this.state.model) return;
    const cu = this.state.cleanup;
    // Day 7 lazy-bake: Save у Cleanup-табі лише зберігає rejected/markers
    // у selections.json (швидко). Перепікання selected/ більше тут НЕ
    // відбувається — воно перенесене у «💾 Зберегти все» / Finalize.
    try {
      // forced=true: зберегти навіть якщо cu.dirty уже знятий debounce-таймером.
      const resp = await this._cleanupAutosave(true);
      cu.dirty = false;
      cu.dirtyExport = false;
      // Позначити stem dirty у catalog → жовта крапка одразу + свіжий cleanup.
      const it = appState.catalog.find((x) => x.stem === this.state.stem);
      if (it && it.state) {
        it.state.dirty = true;
        if (resp && resp.cleanup) it.state.cleanup = resp.cleanup;
      }
      if (!silent) {
        showToast(`✓ Cleanup збережено`, "ok");
      }
      if (closeAfter) await this.close(true);
    } catch (e) {
      showToast(`Помилка збереження cleanup: ${e.message}`, "err", 4000);
      console.error(e);
    }
  },

  async _flushCleanup(silent) {
    const cu = this.state.cleanup;
    if (cu.dirtyExport) {
      await this._cleanupExportSave(silent, false);
    } else if (cu.dirty) {
      await this._cleanupAutosave();
    }
  },
};
