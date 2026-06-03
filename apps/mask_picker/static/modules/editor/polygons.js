/**
 * Polygons mixin для editor composite — методи Polygons-tab:
 *   tool switch, SVG hit-test (vertex/shape/edge), draw draft, lasso
 *   multi-select (Day 6: замінив rubber-band), Align,
 *   seed-from-mask + Pick (seed з instance ID), autosave, bake-save,
 *   dropdown refresh, redraw layers (shapes/vertices/draft/markers).
 *   Undo/redo — глобальний historyMixin (_historyPush("polygons");
 *   обгортка _polyPushUndoSnapshot лишена для зовн. викликів multiseed/labels).
 *
 * Усі методи через `this` (composed editor object) — shared
 * `this.state.polygons` між табами.
 *
 * == Cross-tab mutations (v1.14.0 Bug 5/13) ==
 *   - Pick (_polyPickSeed): додає polygon-shape; instance під ним стає
 *     DERIVED rejected з геометрії (_polyCoveredInstances >50%, v1.16.1) —
 *     НЕ пишеться у cu.rejectedSet (видалення полігона коректно повертає).
 *   - Delete (_polyRemapGroupsAfterShapeDelete): після splice оновлює
 *     state.groups.list[*].polygon_indices (remove + shift > removed). Це
 *     крос-доменна СКЛАДЕНА дія → groups before-snap кладеться у ТОЙ САМИЙ
 *     undo-запис (_historyAttachSnap), тож один Ctrl+Z відкочує обидва.
 *     Покриває обидва delete-шляхи: _polyDeleteSelectedShape і
 *     _polyDeleteSelectedVertices (shape з <3 точок).
 *
 * == Backend endpoints ==
 *   POST /api/polygons/<stem>            — autosave (тільки JSON).
 *   POST /api/polygons-export/<stem>     — Save + bake (через server
 *                                          `data_sync.bake_with_resync`).
 *   POST /api/polygons/<stem>/seed-from-mask — Pick → polygon contours.
 *
 * УВАГА: `state` (з ../state.js) — singleton (config.models, тощо).
 * `this.state` — editor composite. Не плутати.
 */

import {
  $, _hexToRgba, _pointInPoly, _projectPointOnSegment, showToast,
} from "../util.js";
import { api } from "../api.js";
import { state, appLabels, getLabelColor, DEFAULT_LABEL } from "../state.js";

const FREEHAND_MIN_DIST = 15;          // px у image-space між точками freehand
const VERTEX_CLICK_PX    = 10;         // поріг "клік по вершині" у CSS-пікселях
const EDGE_INSERT_PX     = 14;         // поріг dblclick на ребрі у CSS-пікселях (v1.6.2: 8→14)

const DBL_CLICK_MS       = 350;        // вікно ручної детекції double-click (Bug G v1.16.2)

export const polygonsMixin = {
  _setPolyTool(tool) {
    // Tools: "draw", "pick", or null. null = default "navigate + edit":
    // click a polygon/vertex/edge to edit it, click empty does nothing. Edit is
    // no longer a separate button — it is simply the state when no tool is on.
    if (tool !== "draw" && tool !== "pick" && tool !== null) return;
    const pg = this.state.polygons;
    // Toggle off: clicking the already-active tool (or its hotkey) -> default.
    if (tool !== null && pg.tool === tool) tool = null;
    if (pg.tool === "draw" && tool !== "draw") this._polyCancelDraft();
    pg.tool = tool;
    this._toolbarPolygons().querySelectorAll("[data-poly-tool]").forEach((b) => {
      b.classList.toggle("btn--mini-active", b.dataset.polyTool === tool);
    });
    this._updateHint();
    this._polyRedraw();
  },

  _svgCoordsFromEvent(e) {
    const svg = this._svg();
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const p = pt.matrixTransform(ctm.inverse());
    return { x: p.x, y: p.y };
  },

  _onSvgMouseDown(e) {
    if (this.state.activeTab === "groups") {
      this._onGroupsSvgMouseDown(e);
      return;
    }
    if (this.state.activeTab !== "polygons") return;
    if (this.state.spaceDown || e.button === 1 || e.button === 2) return; // pan
    const pg = this.state.polygons;
    const pt = this._svgCoordsFromEvent(e);
    if (!pt) return;
    pg.cursor = pt;

    // Bug G (v1.16.2): edit-only ручна детекція double-click. Native dblclick
    // не долітає — _polyRedraw (innerHTML="") перебудовує SVG-ціль між
    // кліками. Якщо 2-й mousedown ≤ DBL_CLICK_MS і поруч із 1-м — це dblclick:
    // вставляємо вершину і виходимо (без звичайної select/lasso-реакції).
    if (pg.tool === null || (pg.tool === "draw" && !pg.draft)) {
      const _now = performance.now();
      const _moveTol = VERTEX_CLICK_PX * this._pxPerCss();
      if (pg._lastDownT != null && (_now - pg._lastDownT) < DBL_CLICK_MS
          && pg._lastDownPt
          && Math.abs(pt.x - pg._lastDownPt[0]) <= _moveTol
          && Math.abs(pt.y - pg._lastDownPt[1]) <= _moveTol) {
        pg._lastDownT = null;          // consume — не ланцюжити triple-click
        e.preventDefault();
        this._polyOnDblClick(pt);
        return;
      }
      pg._lastDownT = _now;
      pg._lastDownPt = [pt.x, pt.y];
    }

    if (pg.tool === "draw") {
      e.preventDefault();
      // edit-in-draw (v1.16.2): not mid-draft + clicked an existing vertex ->
      // grab it for dragging (edit) instead of adding a new point. Lets you fix
      // vertices without leaving Draw. Mid-draft clicks keep drawing.
      if (!pg.draft) {
        const _hv = this._polyHitVertex(pt);
        if (_hv) {
          pg.selectedShape = _hv.si;
          pg.selectedVertices.clear();
          pg.selectedVertices.add(`${_hv.si}:${_hv.vi}`);
          pg.draggingVertex = { si: _hv.si, vi: _hv.vi };
          this._polyUpdateButtons();
          this._polyRedraw();
          return;
        }
        // click on polygon body OR edge -> select it (don't start drawing).
        const _hs = this._polyHitShape(pt);
        const _he = _hs === -1 ? this._polyHitEdge(pt) : null;
        if (_hs !== -1 || _he) {
          pg.selectedShape = _hs !== -1 ? _hs : _he.si;
          pg.selectedVertices.clear();
          this._polyUpdateButtons();
          this._polyRedraw();
          return;
        }
      }
      if (!pg.draft) pg.draft = { points: [] };
      pg.draft.points.push([pt.x, pt.y]);
      this._polyMarkDirty();
      this._polyRedraw();
      return;
    }

    if (pg.tool === "pick") {
      // QA-2c: якщо клікнули на ВЖЕ існуючий polygon-shape — авто-перемикаємось
      // на Edit і обираємо його.
      const hitShape = this._polyHitShape(pt);
      if (hitShape !== -1) {
        e.preventDefault();
        this._setPolyTool(null);   // jump to default edit/navigate, select shape
        pg.selectedShape = hitShape;
        pg.selectedVertices.clear();
        this._polyUpdateButtons();
        this._polyRedraw();
        return;
      }
      e.preventDefault();
      this._polyPickSeed(pt);
      return;
    }

    // tool === "edit"
    if (e.altKey) e.preventDefault();
    const hitVertex = this._polyHitVertex(pt);
    if (hitVertex) {
      const { si, vi } = hitVertex;
      if (e.altKey) {
        e.preventDefault();
        this._polyDeleteVertex(si, vi, true);
        return;
      }
      if (e.shiftKey) {
        e.preventDefault();
        this._polyToggleVertexSelected(si, vi);
        this._polyRedraw();
        return;
      }
      e.preventDefault();
      const key = `${si}:${vi}`;
      if (pg.selectedVertices.has(key)) {
        this._polyStartGroupDrag(pt);
      } else {
        pg.selectedVertices.clear();
        pg.selectedVertices.add(key);
        pg.selectedShape = si;
        pg.draggingVertex = { si, vi };
      }
      this._polyUpdateButtons();
      this._polyRedraw();
      return;
    }

    const hitShape = this._polyHitShape(pt);
    if (hitShape !== -1) {
      e.preventDefault();
      pg.selectedShape = hitShape;
      pg.selectedVertices.clear();
      this._polyUpdateButtons();
      this._polyRedraw();
      return;
    }

    // Порожнє — start lasso (Day 6: замінив rubber-band).
    // Shift = додати до існуючого, Alt = відняти, без модифікатора = replace.
    e.preventDefault();
    pg.selectedShape = -1;
    const mode = e.shiftKey ? "add" : (e.altKey ? "sub" : "replace");
    if (mode === "replace") pg.selectedVertices.clear();
    pg.lasso = { points: [[pt.x, pt.y]], mode };
    this._polyUpdateButtons();
    this._polyRedraw();
  },

  _onSvgMouseMove(e) {
    if (this.state.activeTab === "groups") {
      this._onGroupsSvgMouseMove(e);
      return;
    }
    if (this.state.activeTab !== "polygons") return;
    if (this.state.isPanning) return;
    const pg = this.state.polygons;
    const pt = this._svgCoordsFromEvent(e);
    if (!pt) return;
    pg.cursor = pt;

    if (pg.tool === "draw") {
      // edit-in-draw: drag a vertex grabbed in Draw (mirror edit-mode drag).
      if (pg.draggingVertex) {
        if (!pg.draggingVertex.pushed) { this._polyPushUndoSnapshot(); pg.draggingVertex.pushed = true; }
        const { si, vi } = pg.draggingVertex;
        pg.shapes[si].points[vi] = [pt.x, pt.y];
        this._polyMarkDirty();
        this._polyRedraw();
        return;
      }
      // Freehand: якщо Shift тримається і є draft — автоматично додаємо вершини
      // на відстані FREEHAND_MIN_DIST.
      if (e.shiftKey && pg.draft && pg.draft.points.length > 0) {
        const last = pg.draft.points[pg.draft.points.length - 1];
        const dx = pt.x - last[0], dy = pt.y - last[1];
        if (dx * dx + dy * dy >= FREEHAND_MIN_DIST * FREEHAND_MIN_DIST) {
          pg.draft.points.push([pt.x, pt.y]);
          this._polyMarkDirty();
        }
      }
      this._polyRedrawDraft();
      return;
    }
    if (pg.tool === "pick") return;

    // edit
    if (pg.draggingVertex) {
      // Undo: знімок на ПЕРШОМУ русі (стан ще ДО зсуву), не на mouseup —
      // інакше захопили б ВЖЕ зміщений стан (off-by-one: перший Ctrl+Z по
      // драгу нічого не робив). `pushed` на самому drag-об'єкті; клік без
      // руху (немає mousemove) → нема push → нема фантомного запису.
      if (!pg.draggingVertex.pushed) { this._polyPushUndoSnapshot(); pg.draggingVertex.pushed = true; }
      const { si, vi } = pg.draggingVertex;
      pg.shapes[si].points[vi] = [pt.x, pt.y];
      this._polyMarkDirty();
      this._polyRedraw();
      return;
    }
    if (pg.draggingGroup) {
      if (!pg.draggingGroup.pushed) { this._polyPushUndoSnapshot(); pg.draggingGroup.pushed = true; }
      const dx = pt.x - pg.draggingGroup.startX;
      const dy = pt.y - pg.draggingGroup.startY;
      for (const [key, [x0, y0]] of pg.draggingGroup.orig.entries()) {
        const [si, vi] = key.split(":").map(Number);
        if (pg.shapes[si] && pg.shapes[si].points[vi]) {
          pg.shapes[si].points[vi] = [x0 + dx, y0 + dy];
        }
      }
      this._polyMarkDirty();
      this._polyRedraw();
      return;
    }
    if (pg.lasso) {
      const pts = pg.lasso.points;
      const last = pts[pts.length - 1];
      const dx = pt.x - last[0], dy = pt.y - last[1];
      if (dx * dx + dy * dy >= FREEHAND_MIN_DIST * FREEHAND_MIN_DIST) {
        pts.push([pt.x, pt.y]);
      }
      this._polyDrawLasso();
      return;
    }

    // Hover
    const prevVert = pg.hoverVertex;
    const prevShp = pg.hoverShapeIdx;
    const hv = this._polyHitVertex(pt);
    if (hv) {
      pg.hoverVertex = hv;
      pg.hoverShapeIdx = -1;
    } else {
      pg.hoverVertex = null;
      pg.hoverShapeIdx = this._polyHitShape(pt);
    }
    if (JSON.stringify(prevVert) !== JSON.stringify(pg.hoverVertex) || prevShp !== pg.hoverShapeIdx) {
      this._polyRedrawShapes();
      this._polyRedrawVertices();
    }
  },

  _onSvgMouseUp(e) {
    if (this.state.activeTab === "groups") {
      this._onGroupsSvgMouseUp(e);
      return;
    }
    if (this.state.activeTab !== "polygons") return;
    const pg = this.state.polygons;

    if (pg.tool === "draw" || pg.tool === "pick") {
      // edit-in-draw: finish a vertex drag started in Draw. Undo вже запушено
      // на першому mousemove (стан ДО зсуву) — тут лише завершуємо drag.
      if (pg.tool === "draw" && pg.draggingVertex) {
        pg.draggingVertex = null;
        this._polyRedraw();
      }
      return;
    }

    // edit (undo вже запушено на першому mousemove drag-у — не на mouseup)
    if (pg.draggingVertex) {
      pg.draggingVertex = null;
      this._polyRedraw();
      return;
    }
    if (pg.draggingGroup) {
      pg.draggingGroup = null;
      this._polyRedraw();
      return;
    }
    if (pg.lasso) {
      this._polyApplyLasso();
      return;
    }
  },

  _polyApplyLasso() {
    const pg = this.state.polygons;
    const lasso = pg.lasso;
    pg.lasso = null;
    if (!lasso) { this._polyRedraw(); return; }
    const path = lasso.points;
    // Короткий drag (≈ клік) — selection вже clear'нули на mousedown (mode=replace),
    // або залишили як було (add/sub) — нічого не додаємо.
    if (path.length < 3) {
      this._polyUpdateButtons();
      this._polyRedraw();
      return;
    }
    const hits = new Set();
    pg.shapes.forEach((sh, si) => {
      sh.points.forEach(([x, y], vi) => {
        if (_pointInPoly(x, y, path)) hits.add(`${si}:${vi}`);
      });
    });
    if (lasso.mode === "sub") {
      hits.forEach((k) => pg.selectedVertices.delete(k));
    } else {
      // "replace" — на mousedown clear'нули; "add" — додаємо до existing.
      hits.forEach((k) => pg.selectedVertices.add(k));
    }
    showToast(`Lasso: ${hits.size} вершин`, "ok", 1500);
    this._polyUpdateButtons();
    this._polyRedraw();
  },

  _onSvgDblClick(e) {
    // Native dblclick — НЕнадійний на polygon/vertex: кожен mousedown робить
    // innerHTML="" у shapes/vertices layer (_polyRedraw) → ціль першого кліку
    // зникає, браузер не генерує dblclick (Bug G, підтверджено в браузері).
    // Справжній тригер — ручна детекція у _onSvgMouseDown. Цей хендлер
    // лишаємо як запасний шлях (порожній SVG-фон, де ціль стабільна).
    if (this.state.activeTab !== "polygons") return;
    const pt = this._svgCoordsFromEvent(e);
    if (!pt) return;
    e.preventDefault();
    this._polyOnDblClick(pt);
  },

  // Дія double-click у polygons. pt — image-coords. Викликається І native
  // dblclick, І ручною детекцією (mousedown timing). Guard _dblHandledT не
  // дає спрацювати двічі на один жест (manual спрацьовує перший, native по
  // тому ж жесту приходить пізніше і відсікається).
  _polyOnDblClick(pt) {
    const pg = this.state.polygons;
    const now = performance.now();
    if (now - (pg._dblHandledT || 0) < DBL_CLICK_MS) return;
    pg._dblHandledT = now;

    if (pg.tool === "draw" && pg.draft) {   // mid-draft: dblclick closes polygon
      if (pg.draft.points.length >= 3) {
        pg.draft.points.pop();
        if (pg.draft.points.length >= 3) this._polyCloseDraft();
      }
      return;
    }
    // draw + NOT drafting (or default null) -> fall through to edge-insert below

    // QA-2b: insert vertex (dblclick edge) — ТІЛЬКИ в edit mode.
    if (pg.tool === "pick") return;   // default(null) edit inserts; pick does not

    const hit = this._polyHitEdge(pt);
    if (hit) {
      const { si, vi, insertPt } = hit;
      this._polyPushUndoSnapshot();
      pg.shapes[si].points.splice(vi, 0, [insertPt.x, insertPt.y]);
      pg.draggingVertex = null;
      this._polyMarkDirty();
      this._polyRedraw();
    }
  },

  // --- hit-test у polygon ---

  _polyHitVertex(pt) {
    const pg = this.state.polygons;
    const threshold = VERTEX_CLICK_PX * this._pxPerCss();
    const t2 = threshold * threshold;
    for (let si = pg.shapes.length - 1; si >= 0; si--) {
      const points = pg.shapes[si].points;
      for (let vi = 0; vi < points.length; vi++) {
        const dx = points[vi][0] - pt.x, dy = points[vi][1] - pt.y;
        if (dx * dx + dy * dy <= t2) return { si, vi };
      }
    }
    return null;
  },

  _polyHitShape(pt) {
    const pg = this.state.polygons;
    for (let si = pg.shapes.length - 1; si >= 0; si--) {
      if (_pointInPoly(pt.x, pt.y, pg.shapes[si].points)) return si;
    }
    return -1;
  },

  _polyHitEdge(pt) {
    const pg = this.state.polygons;
    const pxScale = this._pxPerCss();
    const threshold = EDGE_INSERT_PX * pxScale;
    // UX #1 fix (v1.16.1): exclusion біля вершин АДАПТИВНА до довжини ребра.
    // Баг v1.16.0: фіксований `3 * pxScale` (≈9-10 image-px) для ДРІБНИХ
    // полігонів (везикули!) з'їдав усе ребро — навіть середина ребра
    // коротшого за ~2×exclude потрапляла у мертву зону → вставка неможлива.
    // Тепер мертва зона ≤ 30% довжини ребра з кожного боку → середні 40%
    // будь-якого ребра завжди доступні для вставки.
    // Bug G re-fix #2 (v1.16.1 r2): корінь — vtxExclude фільтр відкидав
    // короткі сегменти densних seed-полігонів (проєкція клампилась на
    // вершину → ребро ігнорувалось → клік "по ребру" не реєструвався
    // взагалі). Нова стратегія: clamp=false → беремо ребро лише якщо нога
    // перпендикуляра РЕАЛЬНО на сегменті (0<t<1), серед них мінімальна
    // перпенд. відстань у межах threshold. Мікро-епсилон лише проти точного
    // дубля на вершині — НЕ блокує нормальні кліки на коротких ребрах.
    let best = null, bestD = threshold * threshold;
    for (let si = 0; si < pg.shapes.length; si++) {
      const pts = pg.shapes[si].points;
      const n = pts.length;
      for (let i = 0; i < n; i++) {
        const a = pts[i], b = pts[(i + 1) % n];
        const proj = _projectPointOnSegment(pt.x, pt.y, a[0], a[1], b[0], b[1], false);
        if (proj.t <= 0.0005 || proj.t >= 0.9995) continue;
        const dx = proj.x - pt.x, dy = proj.y - pt.y;
        const d = dx * dx + dy * dy;
        if (d < bestD) {
          bestD = d;
          best = { si, vi: (i + 1), insertPt: { x: proj.x, y: proj.y } };
        }
      }
    }
    return best;
  },

  _polyStartGroupDrag(pt) {
    const pg = this.state.polygons;
    const orig = new Map();
    for (const key of pg.selectedVertices) {
      const [si, vi] = key.split(":").map(Number);
      if (pg.shapes[si] && pg.shapes[si].points[vi]) {
        orig.set(key, [pg.shapes[si].points[vi][0], pg.shapes[si].points[vi][1]]);
      }
    }
    pg.draggingGroup = { startX: pt.x, startY: pt.y, orig };
  },

  _polyToggleVertexSelected(si, vi) {
    const pg = this.state.polygons;
    const key = `${si}:${vi}`;
    if (pg.selectedVertices.has(key)) pg.selectedVertices.delete(key);
    else pg.selectedVertices.add(key);
  },

  _polyDeleteVertex(si, vi, recordUndo) {
    const pg = this.state.polygons;
    const sh = pg.shapes[si];
    if (!sh) return;
    if (sh.points.length <= 3) {
      this._polyPushUndoSnapshot();
      pg.shapes.splice(si, 1);
      pg.selectedShape = -1;
      pg.selectedVertices.clear();
    } else {
      this._polyPushUndoSnapshot();
      sh.points.splice(vi, 1);
      const updated = new Set();
      pg.selectedVertices.forEach((k) => {
        const [s, v] = k.split(":").map(Number);
        if (s !== si) { updated.add(k); return; }
        if (v === vi) return;
        if (v > vi) updated.add(`${s}:${v - 1}`);
        else updated.add(k);
      });
      pg.selectedVertices = updated;
    }
    this._polyMarkDirty();
    this._polyUpdateButtons();
    this._polyRedraw();
  },

  _polyDeleteSelectedShape() {
    const pg = this.state.polygons;
    if (pg.selectedShape < 0) return;
    this._polyPushUndoSnapshot();
    const removedIdx = pg.selectedShape;
    pg.shapes.splice(removedIdx, 1);
    pg.selectedShape = -1;
    pg.selectedVertices.clear();
    // Bug 5 fix (v1.14.0): polygon_indices у groups зрушуються після
    // splice — оновити локальний groups state + mark dirty.
    this._polyRemapGroupsAfterShapeDelete([removedIdx]);
    this._polyMarkDirty();
    this._polyUpdateButtons();
    this._polyRedraw();
  },

  _polyDeleteSelectedVertices() {
    const pg = this.state.polygons;
    if (!pg.selectedVertices.size) {
      if (pg.selectedShape >= 0) this._polyDeleteSelectedShape();
      return;
    }
    this._polyPushUndoSnapshot();
    const byShape = new Map();
    pg.selectedVertices.forEach((k) => {
      const [si, vi] = k.split(":").map(Number);
      if (!byShape.has(si)) byShape.set(si, []);
      byShape.get(si).push(vi);
    });
    // Bug 5 fix (v1.14.0): vertex-delete може видалити цілу shape якщо
    // points<3 — зберемо список removed idx для remap groups.
    const removedShapeIdxs = [];
    const siKeys = [...byShape.keys()].sort((a, b) => b - a);
    for (const si of siKeys) {
      const sh = pg.shapes[si];
      if (!sh) continue;
      const vis = byShape.get(si).sort((a, b) => b - a);
      for (const vi of vis) sh.points.splice(vi, 1);
      if (sh.points.length < 3) {
        pg.shapes.splice(si, 1);
        removedShapeIdxs.push(si);
      }
    }
    pg.selectedVertices.clear();
    pg.selectedShape = -1;
    if (removedShapeIdxs.length) {
      this._polyRemapGroupsAfterShapeDelete(removedShapeIdxs);
    }
    this._polyMarkDirty();
    this._polyUpdateButtons();
    this._polyRedraw();
  },

  /**
   * Bug 5 fix: після `pg.shapes.splice(si, 1)` всі polygon_indices > si
   * у `state.groups.list` зрушуються на -1. Без цього `polygon_indices`
   * мовчки вказує на іншу форму або out-of-range.
   *
   * Стратегія: для кожного removed idx (відсортованих за спаданням, щоб
   * shift був стабільний) — фільтрувати точне співпадіння (видалити з
   * group.polygon_indices) і зрушити більші на -1. Mark groups dirty
   * для autosave.
   *
   * Idempotent: повторний виклик з порожнім списком — no-op.
   */
  _polyRemapGroupsAfterShapeDelete(removedIdxs) {
    if (!removedIdxs || !removedIdxs.length) return;
    const g = this.state.groups;
    if (!Array.isArray(g.list) || g.list.length === 0) return;
    // Складена дія: delete polygon-shape (polygons-домен) ремапить групи
    // (groups-домен). Знімок груп ДО ремапу — щоб прикріпити його до того
    // самого undo-запису, який щойно створив delete-метод (_polyPushUndoSnapshot
    // → polygons). Тоді один Ctrl+Z відкочує і shape, і polygon_indices.
    const groupsBefore = this._historySnap("groups");
    // Відсортовано за спаданням — застосовуємо shifts по одному
    // (як `_polyDeleteSelectedVertices` сам перебирає shape-індекси).
    const sortedDesc = [...removedIdxs].sort((a, b) => b - a);
    let anyChange = false;
    for (const group of g.list) {
      const pidxs = Array.isArray(group.polygon_indices)
        ? group.polygon_indices : [];
      if (pidxs.length === 0) continue;
      let updated = pidxs.slice();
      for (const removed of sortedDesc) {
        updated = updated
          .filter((pi) => pi !== removed)        // видалити точне співпадіння
          .map((pi) => (pi > removed ? pi - 1 : pi));  // зрушити більші
      }
      // Дедуп після фільтру (про всяк випадок) + sort
      const dedup = Array.from(new Set(updated)).sort((a, b) => a - b);
      if (dedup.length !== pidxs.length ||
          dedup.some((v, i) => v !== pidxs[i])) {
        group.polygon_indices = dedup;
        anyChange = true;
      }
    }
    if (anyChange) {
      g.dirty = true;
      this._groupsScheduleAutosave();
      // Прикріпити before-знімок груп до верхнього (polygon-delete) запису →
      // складена дія = ОДИН undo-запис (polygons + groups).
      this._historyAttachSnap("groups", groupsBefore);
    }
  },

  _polyAlignToLine() {
    const pg = this.state.polygons;
    if (pg.selectedVertices.size < 3) {
      showToast("Для Align потрібно ≥3 вибраних вершин (2 опорні + інші)", "err", 2500);
      return;
    }
    const arr = [...pg.selectedVertices];
    const [aKey, bKey] = [arr[0], arr[arr.length - 1]];
    const [sa, va] = aKey.split(":").map(Number);
    const [sb, vb] = bKey.split(":").map(Number);
    const A = pg.shapes[sa].points[va];
    const B = pg.shapes[sb].points[vb];
    this._polyPushUndoSnapshot();
    for (const key of arr) {
      if (key === aKey || key === bKey) continue;
      const [si, vi] = key.split(":").map(Number);
      const P = pg.shapes[si].points[vi];
      const proj = _projectPointOnSegment(P[0], P[1], A[0], A[1], B[0], B[1], false);
      pg.shapes[si].points[vi] = [proj.x, proj.y];
    }
    this._polyMarkDirty();
    this._polyRedraw();
    showToast(`Align: ${arr.length - 2} вершин підтягнуто`, "ok", 1800);
  },

  _polyCloseDraft() {
    const pg = this.state.polygons;
    if (!pg.draft || pg.draft.points.length < 3) {
      pg.draft = null;
      pg.freehand = null;
      this._polyRedraw();
      return;
    }
    this._polyPushUndoSnapshot();
    pg.shapes.push({
      label: pg.activeLabel || DEFAULT_LABEL,
      points: pg.draft.points.map((p) => [+p[0], +p[1]]),
      shape_type: "polygon",
      group_id: null,
      flags: {},
    });
    pg.draft = null;
    pg.freehand = null;
    this._polyMarkDirty();
    this._polyRedraw();
  },

  _polyCancelDraft() {
    // Скасування draft НЕ змінює pg.shapes (draft окремий до замикання) → НЕ
    // пушимо undo: інакше Esc по чернетці створював фантомний запис, і перший
    // Ctrl+Z після нього «нічого не робив». Ctrl+Z тепер відкочує останню
    // реальну зміну shapes.
    const pg = this.state.polygons;
    if (!pg.draft) return;
    pg.draft = null;
    pg.freehand = null;
    this._polyRedraw();
  },

  _polyPopDraftVertex() {
    const pg = this.state.polygons;
    if (!pg.draft || !pg.draft.points.length) return;
    pg.draft.points.pop();
    this._polyMarkDirty();
    this._polyRedraw();
  },

  // --- undo: тонка обгортка над глобальним historyMixin ---
  // Викликати ПЕРЕД мутацією shapes. Зовнішні виклики (multiseed.js,
  // labels.js) кличуть _polyPushUndoSnapshot — ім'я лишаємо. Сам undo/redo —
  // _historyUndo/_historyRedo (keys.js + events.js, глобальний стек).
  _polyPushUndoSnapshot() {
    this._historyPush("polygons");
  },

  // --- seed from mask ---

  async _polySeedFromMask() {
    const s = this.state;
    if (!s.model) {
      showToast("Спершу обери модель (Pick або dropdown) — Seed потребує npy-маски", "err", 2500);
      return;
    }
    if (s.polygons.shapes.length) {
      if (!confirm(`Замінити поточні ${s.polygons.shapes.length} полігонів контурами з ${s.model}?`)) return;
    }
    try {
      const resp = await api(`/api/polygons/${encodeURIComponent(s.stem)}/seed-from-mask`, {
        method: "POST",
        body: JSON.stringify({ model: s.model, label: s.polygons.activeLabel || DEFAULT_LABEL }),
      });
      if (!resp.ok) {
        showToast(`Seed не вдався: ${resp.error || "?"}`, "err");
        return;
      }
      this._polyPushUndoSnapshot();
      const newShapes = (resp.envelope.shapes || []).map((sh) => ({
        label: sh.label || DEFAULT_LABEL,
        points: sh.points.map((p) => [+p[0], +p[1]]),
        shape_type: sh.shape_type || "polygon",
        group_id: sh.group_id ?? null,
        flags: sh.flags || {},
      }));
      s.polygons.shapes = newShapes;
      s.polygons.selectedShape = -1;
      s.polygons.selectedVertices.clear();
      this._polyMarkDirty();
      this._polyRedraw();
      showToast(`Seed: ${resp.shape_count} полігонів з ${s.model}`, "ok");
    } catch (e) {
      showToast(`Seed помилка: ${e.message}`, "err", 4000);
      console.error(e);
    }
  },

  async _polyPickSeed(pt) {
    const s = this.state;
    const cu = s.cleanup;
    if (!s.model) {
      showToast("Pick працює лише з активною моделлю (npy-маска)", "err", 2500);
      return;
    }
    if (!cu.labelsInt32) {
      showToast("Маска ще не завантажена — перемкнися на Cleanup раз", "err", 2500);
      return;
    }
    const W = s.W, H = s.H;
    const x = Math.floor(pt.x), y = Math.floor(pt.y);
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    const id = cu.labelsInt32[y * W + x] | 0;
    if (!id) { showToast("Тут немає інстанса", "err", 1500); return; }
    const seedFromRejected = cu.rejectedSet && cu.rejectedSet.has(id);
    try {
      const resp = await api(`/api/polygons/${encodeURIComponent(s.stem)}/seed-from-mask`, {
        method: "POST",
        body: JSON.stringify({ model: s.model, instance_ids: [id], label: s.polygons.activeLabel || DEFAULT_LABEL }),
      });
      if (!resp.ok) {
        showToast(`Pick не вдався: ${resp.error || "?"}`, "err");
        return;
      }
      const newShapes = (resp.envelope.shapes || []).map((sh) => ({
        label: sh.label || s.polygons.activeLabel || DEFAULT_LABEL,
        points: sh.points.map((p) => [+p[0], +p[1]]),
        shape_type: sh.shape_type || "polygon",
        group_id: sh.group_id ?? null,
        flags: sh.flags || {},
      }));
      if (!newShapes.length) {
        showToast("Контур інстанса порожній (занадто дрібний?)", "err", 2000);
        return;
      }
      this._polyPushUndoSnapshot();
      s.polygons.shapes.push(...newShapes);
      s.polygons.selectedShape = s.polygons.shapes.length - 1;
      s.polygons.selectedVertices.clear();
      this._polyMarkDirty();   // інвалідує coveredCache → derived rejection
      // Bug A/B fix (v1.16.1) — DERIVED rejection, не explicit (костиль
      // прибрано). Pick створює polygon; instance під ним стає covered
      // (_polyCoveredInstances, >50%) → автоматично червоний + невибірний.
      // НЕ додаємо у rejectedSet: інакше після видалення полігона instance
      // лишався б застряглим rejected (баг A). Тепер видалення полігона →
      // markDirty → coveredCache recompute → instance повертається сам.
      // _polyMarkDirty вище вже інвалідував кеш; перемальовуємо cleanup.
      this._cleanupRedraw();
      this._polyRedraw();
      const note = seedFromRejected
        ? ", був rejected"
        : " — instance заміщений полігоном (derived rejected)";
      showToast(`Pick: +${newShapes.length} полігон${newShapes.length === 1 ? "" : "и"} (інстанс #${id}${note})`, "ok", 1800);
    } catch (e) {
      showToast(`Pick помилка: ${e.message}`, "err", 4000);
      console.error(e);
    }
  },

  // --- save ---

  _polyMarkDirty() {
    this.state.polygons.dirty = true;
    // Bug A/B (v1.16.1): полігони змінились → інвалідувати кеш covered-
    // instance (derived rejection rendering/toggle/groups перерахується).
    this.state.cleanup.coveredCache = null;
    clearTimeout(this.state.polygons.autosaveTimer);
    this.state.polygons.autosaveTimer = setTimeout(() => this._polyAutosave(), 5000);
    this._updateStats();
  },

  async _polyAutosave() {
    // Day 9 Bug 2: через проміс-чергу редактора — без паралельних POST.
    return this._enqueueSave(async () => {
      const pg = this.state.polygons;
      if (!pg.dirty || !this.state.open) return;
      try {
        const payload = this._polyBuildPayload();
        await api(`/api/polygons/${encodeURIComponent(this.state.stem)}`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        pg.dirty = false;
        // Day 7: save без bake → stem незапечений. Тримаємо catalog у курсі
        // (жовта крапка + попередження у Groups-табі) навіть при autosave.
        const it = state.catalog.find((x) => x.stem === this.state.stem);
        if (it && it.state) it.state.dirty = true;
      } catch (e) {
        console.warn("polygons autosave failed:", e);
      }
    });
  },

  async _polySave(silent) {
    // Day 9 Bug 2: через проміс-чергу — швидкий tab-switch autosave і
    // flushIfDirty більше не клобають один одного паралельними POST.
    return this._enqueueSave(async () => {
      const pg = this.state.polygons;
      if (!this.state.open) return;
      try {
        const payload = this._polyBuildPayload();
        // Day 7 lazy-bake: Save Polygons лише зберігає polygons/<stem>.json
        // (швидко). Запікання у selected/ — через «💾 Зберегти все» / Finalize.
        const resp = await api(
          `/api/polygons/${encodeURIComponent(this.state.stem)}`,
          { method: "POST", body: JSON.stringify(payload) },
        );
        pg.dirty = false;
        // Позначити stem dirty у catalog → жовта крапка з'являється одразу.
        const it = state.catalog.find((x) => x.stem === this.state.stem);
        if (it && it.state) it.state.dirty = true;
        if (!silent) {
          showToast(`✓ Полігони збережено (${resp.shape_count})`, "ok");
        }
      } catch (e) {
        showToast(`Polygons save: ${e.message}`, "err", 4000);
        console.error(e);
      }
    });
  },

  _polyBuildPayload() {
    const pg = this.state.polygons;
    return {
      ...pg.envelope,
      shapes: pg.shapes.map((sh) => ({
        label: sh.label || DEFAULT_LABEL,
        points: sh.points.map((p) => [+p[0], +p[1]]),
        shape_type: sh.shape_type || "polygon",
        group_id: sh.group_id ?? null,
        flags: sh.flags || {},
      })),
      imageHeight: this.state.H,
      imageWidth:  this.state.W,
    };
  },

  _polyUpdateButtons() {
    const pg = this.state.polygons;
    $("#polyDeleteShape").disabled = pg.selectedShape < 0 && pg.selectedVertices.size === 0;
    $("#polyAlign").disabled = pg.selectedVertices.size < 3;
    this._polyUpdateShapeLabelSelect();
  },

  _refreshBaseChip() {
    // Day 3b: read-only indicator that mirrors state.baseLabel (per-image).
    const dot = $("#polyBaseChipDot");
    const name = $("#polyBaseChipName");
    if (!dot || !name) return;
    const label = this.state.baseLabel || DEFAULT_LABEL;
    const lbl = appLabels.find((l) => l.name === label);
    dot.style.background = (lbl && lbl.color) || "#888";
    name.textContent = label;
  },

  _polyUpdateShapeLabelSelect() {
    const pg = this.state.polygons;
    const sel = $("#polyShapeLabelSelect");
    const wrap = $("#labelPickerReassign"); // Day 3b: popover section wrapping the select
    if (!sel) return;

    const idxSet = new Set();
    if (pg.selectedShape >= 0) idxSet.add(pg.selectedShape);
    for (const key of pg.selectedVertices) idxSet.add(parseInt(key.split(":")[0], 10));

    if (idxSet.size === 0 || pg.tool !== null) {
      if (wrap) wrap.style.display = "none";
      return;
    }

    const expected = appLabels.map((l) => l.name).join(",");
    if (sel.dataset.builtFor !== expected) {
      sel.innerHTML = appLabels.map((l) =>
        `<option value="${l.name}">${l.name}</option>`
      ).join("");
      sel.dataset.builtFor = expected;
    }

    const labels = [...idxSet].map((i) => (pg.shapes[i] && pg.shapes[i].label) || DEFAULT_LABEL);
    const allSame = labels.every((l) => l === labels[0]);
    sel.value = allSame ? labels[0] : "";
    sel.title = idxSet.size > 1
      ? `Змінити клас ${idxSet.size} полігонів`
      : "Змінити клас виділеного полігону";

    sel.dataset.selectedShapes = JSON.stringify([...idxSet]);
    if (wrap) wrap.style.display = "";
  },

  // --- rendering ---

  _polyRedraw() {
    this._polyRedrawShapes();
    this._polyRedrawVertices();
    this._polyRedrawDraft();
    this._polyRedrawMarkersOnly();
    this._polyDrawLasso();
    // Перемалювати базу якщо covered-набір змінився (новий/видалений полігон
    // змінив перекриття) → covered-instance зникає з overlay ОДРАЗУ. No-op при
    // незмінних covered (hover/draft) — дешева перевірка сигнатури.
    this._drawBaseIfCoveredChanged();
  },

  _polyResizeVertices() {
    const r = Math.max(3, 7 / this.state.scale);
    this._layerVertices().querySelectorAll("circle").forEach((c) => {
      c.setAttribute("r", String(r));
    });
    this._layerDraft().querySelectorAll("circle").forEach((c) => {
      c.setAttribute("r", String(r));
    });
    this._layerMarkers().querySelectorAll("circle").forEach((c) => {
      c.setAttribute("r", String(Math.max(4, 8 / this.state.scale)));
    });
  },

  _polyShapeIsRejected(sh) {
    // Cleanup видаляє тільки базові model instances перед bake.
    // Manual polygons — окремий шар і мають зберігатися навіть поверх rejected
    // zones, щоб можна було замінити погану модельну область ручною розміткою.
    return false;
  },

  _polyRedrawShapes() {
    const pg = this.state.polygons;
    const layer = this._layerShapes();
    layer.innerHTML = "";
    pg.shapes.forEach((sh, si) => {
      if (this._polyShapeIsRejected(sh)) return;
      const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
      const pts = sh.points.map((p) => `${p[0]},${p[1]}`).join(" ");
      poly.setAttribute("points", pts);
      const isSelected = si === pg.selectedShape;
      const isHover = si === pg.hoverShapeIdx;
      const color = getLabelColor(sh.label || DEFAULT_LABEL);
      const fillAlpha = isSelected ? 0.32 : isHover ? 0.28 : 0.15;
      const strokeAlpha = isSelected || isHover ? 0.95 : 0.85;
      const strokeW = isSelected ? 2.5 : isHover ? 2 : 1.5;
      poly.setAttribute("style",
        `fill:${_hexToRgba(color, fillAlpha)};stroke:${_hexToRgba(color, strokeAlpha)};` +
        `stroke-width:${strokeW};vector-effect:non-scaling-stroke;cursor:pointer;`);
      poly.setAttribute("class", "polygon-shape");
      poly.dataset.si = String(si);
      layer.appendChild(poly);
    });
  },

  _polyRedrawVertices() {
    const pg = this.state.polygons;
    const layer = this._layerVertices();
    layer.innerHTML = "";
    const r = Math.max(3, 7 / this.state.scale);
    pg.shapes.forEach((sh, si) => {
      if (this._polyShapeIsRejected(sh)) return;
      const color = getLabelColor(sh.label || DEFAULT_LABEL);
      sh.points.forEach(([x, y], vi) => {
        const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        c.setAttribute("cx", String(x));
        c.setAttribute("cy", String(y));
        c.setAttribute("r", String(r));
        const key = `${si}:${vi}`;
        const isSelected = pg.selectedVertices.has(key);
        const isHover = pg.hoverVertex && pg.hoverVertex.si === si && pg.hoverVertex.vi === vi;
        let cls = "polygon-vertex";
        if (isSelected) cls += " polygon-vertex--selected";
        if (isHover)    cls += " polygon-vertex--hover";
        if (pg.draggingVertex && pg.draggingVertex.si === si && pg.draggingVertex.vi === vi) cls += " polygon-vertex--dragging";
        c.setAttribute("class", cls);
        c.style.fill = isSelected ? "#ffffff" : isHover ? "#9cc5ff" : _hexToRgba(color, 0.9);
        c.dataset.si = String(si);
        c.dataset.vi = String(vi);
        layer.appendChild(c);
      });
    });
  },

  _polyRedrawDraft() {
    const pg = this.state.polygons;
    const layer = this._layerDraft();
    layer.innerHTML = "";
    if (!pg.draft || !pg.draft.points.length) return;
    const cursorPt = pg.cursor || pg.draft.points[pg.draft.points.length - 1];
    const polyPts = pg.draft.points.map((p) => `${p[0]},${p[1]}`);
    const last = pg.draft.points[pg.draft.points.length - 1];
    const dx = cursorPt.x - last[0], dy = cursorPt.y - last[1];
    if (dx * dx + dy * dy > 0.5) {
      polyPts.push(`${cursorPt.x},${cursorPt.y}`);
    }
    // v1.16.2: draft line + vertices in the ACTIVE LABEL colour (was fixed blue),
    // so you see which class you're drawing.
    const draftColor = getLabelColor(pg.activeLabel || DEFAULT_LABEL);
    const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
    poly.setAttribute("points", polyPts.join(" "));
    poly.setAttribute("class", "polygon-shape polygon-shape--draft");
    poly.style.stroke = draftColor;
    poly.style.fill = draftColor;
    poly.style.fillOpacity = "0.18";   // translucent label colour (matches CSS draft fill)
    layer.appendChild(poly);
    const r = Math.max(3, 7 / this.state.scale);
    pg.draft.points.forEach(([x, y]) => {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", String(x));
      c.setAttribute("cy", String(y));
      c.setAttribute("r", String(r));
      c.setAttribute("class", "polygon-vertex");
      c.style.fill = draftColor;
      layer.appendChild(c);
    });
  },

  _polyDrawLasso() {
    const pg = this.state.polygons;
    const el = this._lasso();
    if (!el) return;
    if (!pg.lasso || pg.lasso.points.length === 0) {
      el.setAttribute("d", "");
      el.style.display = "none";
      return;
    }
    const pts = pg.lasso.points;
    let d = `M ${pts[0][0]} ${pts[0][1]}`;
    for (let i = 1; i < pts.length; i++) d += ` L ${pts[i][0]} ${pts[i][1]}`;
    d += " Z";   // closed для fill (visual feedback "це область виділення")
    el.setAttribute("d", d);
    el.style.display = "";
  },

  // Markers (cleanup) — малюємо у SVG, видно з обох табів.
  _polyRedrawMarkersOnly() {
    const cu = this.state.cleanup;
    const layer = this._layerMarkers();
    layer.innerHTML = "";
    const r = Math.max(3, 7 / this.state.scale);
    cu.markers.forEach((m, i) => {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", String(m.x));
      c.setAttribute("cy", String(m.y));
      c.setAttribute("r", String(r));
      let cls = "polygon-marker";
      if (this.state.activeTab === "cleanup" && this.state.cleanup.tool === "marker"
          && i === cu.hoverMarkerIdx) cls += " polygon-marker--hover";
      if (this.state.activeTab !== "cleanup") cls += " polygon-marker--ghost";
      c.setAttribute("class", cls);
      c.dataset.markerIdx = String(i);
      layer.appendChild(c);
    });
  },
};
