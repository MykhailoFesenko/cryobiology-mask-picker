/**
 * Editor — composite object з трьома табами (Cleanup + Polygons + Groups).
 *
 * Композиція через mixin spread: tabs / zoompan / events / cleanup /
 * polygons / groups / keys — кожен mixin використовує `this` для shared state.
 *
 * == Стан (this.state) ==
 *   this.state.cleanup  — rejectedSet, markers (Cleanup tab)
 *   this.state.polygons — shapes, draft, selectedShape, lasso (Polygons tab)
 *   this.state.groups   — list, active, tool, lasso (Groups tab)
 *   this.state.history  — ГЛОБАЛЬНИЙ хронологічний undo/redo (historyMixin) —
 *                         єдиний стек на всі 3 домени (замінив per-tab стеки).
 *
 * == Backend endpoints які кличе ==
 *   GET  /api/labels-rgb/<model>/<stem>.png  — raw npy → RGB → cu.labelsInt32.
 *   GET/POST /api/cleanup/<stem>             — rejected_instances + markers.
 *   GET/POST /api/polygons/<stem>            — LabelMe envelope.
 *   POST /api/polygons-export/<stem>         — Save Polygons + bake
 *                                              (через data_sync.bake_with_resync).
 *   GET/POST /api/groups/<stem>              — групи + classifications.
 *
 * == ID-простори ==
 *   cu.labelsInt32 — instance ID у raw output просторі (читається з
 *                    /api/labels-rgb/). cu.rejectedSet — теж raw IDs.
 *   group.instance_ids — IDs у baked filtered npy (оновлюється після bake
 *                        через server-side strip orphan у bake_with_resync).
 *
 * == Autosave ==
 *   5-секундний debounce → POST. flushIfDirty() при close/tab-switch/nav.
 *   _enqueueSave проміс-черга (Day 9 Bug 2 — без race).
 *
 * Публічне API (споживачі — catalog, keyboard, workspace_bar, multiseed, labels):
 *   editor.open(modelName, { tab?: "cleanup" | "polygons" })
 *   editor.close(skipFlush=false)
 *   editor.flushIfDirty()
 *   editor.state.open
 *   editor.onKey(e), editor.onKeyUp(e)
 */

import { $, showToast } from "../util.js";
import { api } from "../api.js";
import {
  appLabels, currentItem,
  DEFAULT_LABEL,
} from "../state.js";
import { renderAll } from "../catalog.js";
import { _clearPolygonsCache } from "../polyoverlay.js";

import { tabsMixin } from "./tabs.js";
import { zoompanMixin } from "./zoompan.js";
import { eventsMixin } from "./events.js";
import { cleanupMixin } from "./cleanup.js";
import { polygonsMixin } from "./polygons.js";
import { groupsMixin } from "./groups.js";
import { keysMixin } from "./keys.js";
import { historyMixin } from "./history.js";

export const editor = {
  state: {
    open: false,
    stem: null,
    model: null,                    // null коли polygons-only
    activeTab: "cleanup",           // "cleanup" | "polygons" | "groups"
    baseLabel: null,                // class for remaining model instances at bake/finalize
    W: 0, H: 0,

    // спільні — зображення + фон
    originalImage: null,
    overlayImage: null,
    bgSource: "overlay",            // "original" | "overlay" — окремий на cleanup/polygons
    bgPreviewPrev: null,            // під час hold-O

    // zoom/pan
    scale: 1, panX: 0, panY: 0,
    isPanning: false,
    panStartX: 0, panStartY: 0,
    panOrigX: 0, panOrigY: 0,
    spaceDown: false,

    handlers: null,                 // refs для unbind

    // Глобальний хронологічний undo/redo (усі 3 домени; historyMixin).
    // Замінює per-tab cu/pg/gr.undoStack. Чиститься на open/close (per-stem).
    history: { undo: [], redo: [] },

    // --- CLEANUP sub-state ---
    cleanup: {
      tool: "reject",               // "reject" | "marker"
      labelsInt32: null,
      allIds: null,
      bboxes: null,
      rejectedSet: null,
      markers: [],                  // [{x,y}] image-coords
      hoverId: 0,
      hoverMarkerIdx: -1,
      dirty: false,
      dirtyExport: false,
      autosaveTimer: null,
      bgSource: "overlay",
      available: false,             // чи є npy-маска для cleanup
    },

    // --- POLYGONS sub-state ---
    polygons: {
      tool: "draw",                 // "draw" | "edit" | "pick"
      activeLabel: DEFAULT_LABEL,
      shapes: [],                   // [{label, points:[[x,y],...], shape_type}]
      draft: null,                  // {points:[[x,y],...]} під час малювання
      cursor: { x: 0, y: 0 },       // image-coords курсора (для draft-edge)
      hoverShapeIdx: -1,
      hoverVertex: null,            // {si, vi} або null
      selectedShape: -1,
      selectedVertices: new Set(),  // "si:vi"
      draggingVertex: null,         // {si, vi}
      draggingGroup: null,          // {startX,startY, orig: Map<"si:vi",[x,y]>}
      lasso: null,                  // {points:[[x,y],...], mode:"replace"|"add"|"sub"} Day 6
      freehand: null,               // {lastX,lastY}
      dirty: false,
      autosaveTimer: null,
      bgSource: "original",
      envelope: null,               // LabelMe обгортка (крім shapes)
    },

    // --- GROUPS sub-state (Day 4-5 cell grouping) ---
    groups: {
      tool: "edit",                 // "edit" | "picker" (2026-05-26: merged toggle+lasso)
      list: [],                     // [{id, type, instance_ids, polygon_indices, color_hue, label}]
      classifications: [],          // aligned: [{n_nucleus, n_vesicle, valid, reason, suggested_type}]
      activeId: null,               // currently selected group_id
      dirty: false,
      autosaveTimer: null,
      bgSource: "overlay",
      lasso: null,                  // {active: bool, path: [[x,y], ...]} під час drag
      editPress: null,              // {startX, startY, isDrag} для edit-tool drag-detection
      peekUngrouped: false,         // hold-кнопка: підсвітити інстанси поза групами
    },
  },

  // ---- DOM refs ----
  _modal:  () => $("#cleanupModal"),
  _wrap:   () => $("#cleanupCanvasWrap"),
  _zoom:   () => $("#cleanupZoom"),
  _cBase:  () => $("#cleanupCanvasBase"),
  _cMarks: () => $("#cleanupCanvasMarks"),
  _cHit:   () => $("#cleanupCanvasHit"),
  _svg:    () => $("#polygonSvg"),
  _layerMarkers:  () => $("#polygonMarkersLayer"),
  _layerShapes:   () => $("#polygonShapesLayer"),
  _layerDraft:    () => $("#polygonDraftLayer"),
  _layerVertices: () => $("#polygonVerticesLayer"),
  _lasso:  () => $("#polygonLasso"),
  _toolbarCleanup:  () => $(".editor__toolbar--cleanup"),
  _toolbarPolygons: () => $(".editor__toolbar--polygons"),
  _toolbarGroups:   () => $(".editor__toolbar--groups"),
  _groupsLayer:     () => $("#groupsOverlayLayer"),
  _groupsBoundaryLayer: () => $("#groupsBoundaryLayer"),

  // Bug D/J (v1.16.2): очистити ВСІ візуальні шари груп одним хелпером.
  // Білий контур active-групи живе у #groupsBoundaryLayer (path fill #ffffff,
  // _groupsDrawBoundaryStroke). Попередні фікси (v1.16.1) чистили лише
  // #groupsOverlayLayer + #groupsMaskCanvas у 4 розкиданих місцях — і скрізь
  // забули boundary layer, тому біла обводка лишалась на Cleanup/Polygons
  // «ні в якому випадку». Єдина точка очистки усуває цей клас помилок.
  _groupsClearVisualLayers() {
    const ol = this._groupsLayer();
    if (ol) { ol.style.display = "none"; while (ol.firstChild) ol.removeChild(ol.firstChild); }
    const bl = this._groupsBoundaryLayer();
    if (bl) { while (bl.firstChild) bl.removeChild(bl.firstChild); }
    const mc = document.getElementById("groupsMaskCanvas");
    if (mc) {
      mc.style.display = "none";
      const mctx = mc.getContext && mc.getContext("2d");
      if (mctx && mc.width && mc.height) mctx.clearRect(0, 0, mc.width, mc.height);
    }
  },

  // ---- save serialization (Day 9 Bug 2: async-flush race) ----
  // Усі save-операції редактора (poly/cleanup/groups, autosave і явні)
  // проходять через _enqueueSave → один проміс-ланцюг. Гарантія: ніколи
  // не буде 2 паралельних POST на той самий stem (фікс гонки при швидкому
  // tab-switch / навігації / закритті під час debounce-autosave).
  _saveChain: Promise.resolve(),
  _flushPromise: null,
  _enqueueSave(fn) {
    const next = this._saveChain.then(fn, fn);
    this._saveChain = next.catch(() => {});   // tail ковтає помилку — ланцюг живе далі
    return next;
  },

  // ---- mixin composition ----
  ...tabsMixin,
  ...zoompanMixin,
  ...eventsMixin,
  ...cleanupMixin,
  ...polygonsMixin,
  ...groupsMixin,
  ...keysMixin,
  ...historyMixin,

  // =================================================================
  //                              OPEN / CLOSE
  // =================================================================

  async open(modelName, opts = {}) {
    const it = currentItem();
    if (!it) return;
    if (this.state.open) {
      if (this.state.stem === it.stem) {
        if (modelName && modelName !== this.state.model) {
          await this._flushCleanup(true);
          this.state.model = modelName;
          await this._reloadCleanupData();
        }
        if (opts.tab) this.switchTab(opts.tab);
        return;
      }
      await this.close();
    }

    const tab = opts.tab || "cleanup";
    const s = this.state;
    s.open = true;
    s.stem = it.stem;
    s.model = modelName || null;
    s.activeTab = tab;
    s.baseLabel = (it.state && it.state.base_label)
      || (appLabels[0] && appLabels[0].name)
      || DEFAULT_LABEL;
    s.scale = 1; s.panX = 0; s.panY = 0;
    s.isPanning = false; s.spaceDown = false;
    s.bgPreviewPrev = null;
    this._historyClear();   // новий стем = свіжа історія (global undo/redo)

    const cu = s.cleanup;
    cu.rejectedSet = new Set();
    cu.markers = [];
    cu.dirty = false; cu.dirtyExport = false;
    cu.hoverId = 0; cu.hoverMarkerIdx = -1;
    cu.tool = "reject";
    cu.bgSource = "overlay";
    cu.available = false;

    const pg = s.polygons;
    pg.shapes = [];
    pg.draft = null;
    pg.dirty = false;
    pg.hoverShapeIdx = -1; pg.hoverVertex = null;
    pg.selectedShape = -1; pg.selectedVertices = new Set();
    pg.draggingVertex = null; pg.draggingGroup = null;
    pg.lasso = null; pg.freehand = null;
    pg.tool = null;   // default: navigate + edit (no tool active on open)
    // Bug E (v1.16.1): Polygons-таб стартує на OVERLAY (було "original").
    // Юзер: хоче overlay постійно, оригінал лише за явним вибором або hold-O.
    // (Старий коментар Day 9 про пікселізацію overlay більше не актуальний —
    // overlay якісний; за потреби різкого оригіналу є кнопка + hold-O.)
    pg.bgSource = "overlay";
    pg.envelope = null;
    this._refreshBaseChip();

    const gr = s.groups;
    gr.list = [];
    gr.classifications = [];
    gr.activeId = null;
    gr.dirty = false;
    gr.tool = "edit";
    gr.bgSource = "overlay";
    gr.lasso = null;
    gr.peekUngrouped = false;   // hold-state не переноситься між фото

    this._applyZoomTransform();
    this._renderTabs();
    this._updateTitle();
    $("#cleanupStats").textContent = "завантаження…";
    this._modal().classList.add("open");
    this._bindEvents();

    try {
      const [origImg, ovImg] = await Promise.all([
        this._loadImage(`/api/image/${encodeURIComponent(s.stem)}`),
        s.model
          ? this._loadImage(`/api/overlay/${encodeURIComponent(s.model)}/${encodeURIComponent(s.stem)}`).catch(() => null)
          : Promise.resolve(null),
      ]);
      s.originalImage = origImg;
      s.overlayImage = ovImg;

      s.W = origImg.naturalWidth;
      s.H = origImg.naturalHeight;

      if (s.model) {
        await this._reloadCleanupData();
      }

      await this._loadPolygons();
      await this._groupsLoad();

      if (cu.labelsInt32) {
        // cleanup-маска вже встановила W/H — не міняти.
      } else {
        for (const c of [this._cBase(), this._cMarks(), this._cHit()]) {
          c.width = s.W; c.height = s.H;
        }
      }

      const svg = this._svg();
      svg.setAttribute("viewBox", `0 0 ${s.W} ${s.H}`);
      svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

      this._drawBase();
      this._activateTab(tab);
      this._updateStats();
      this._updateHint();
    } catch (e) {
      showToast(`Editor: ${e.message}`, "err", 4000);
      console.error(e);
      await this.close(true);
    }
  },

  async _reloadCleanupData() {
    const s = this.state;
    const cu = s.cleanup;
    if (!s.model) { cu.available = false; return; }
    try {
      const [meta, savedCleanup] = await Promise.all([
        api(`/api/instances/${encodeURIComponent(s.model)}/${encodeURIComponent(s.stem)}`),
        api(`/api/cleanup/${encodeURIComponent(s.stem)}`).catch(() => ({})),
      ]);
      if (!meta || !meta.shape) {
        cu.available = false;
        return;
      }
      const [H, W] = meta.shape;
      s.W = W; s.H = H;
      for (const c of [this._cBase(), this._cMarks(), this._cHit()]) {
        c.width = W; c.height = H;
      }
      const svg = this._svg();
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

      const hitImg = await this._loadImage(
        `/api/labels-rgb/${encodeURIComponent(s.model)}/${encodeURIComponent(s.stem)}.png`);
      const hctx = this._cHit().getContext("2d");
      hctx.imageSmoothingEnabled = false;
      hctx.clearRect(0, 0, W, H);
      hctx.drawImage(hitImg, 0, 0, W, H);
      const hitData = hctx.getImageData(0, 0, W, H).data;
      const labelsInt32 = new Int32Array(W * H);
      const allIds = new Set();
      const bboxes = new Map();
      for (let i = 0, p = 0; i < W * H; i++, p += 4) {
        const id = (hitData[p] << 16) | (hitData[p + 1] << 8) | hitData[p + 2];
        labelsInt32[i] = id;
        if (id === 0) continue;
        if (!allIds.has(id)) {
          allIds.add(id);
          const x = i % W, y = (i / W) | 0;
          bboxes.set(id, { x0: x, y0: y, x1: x, y1: y });
        } else {
          const bb = bboxes.get(id);
          const x = i % W, y = (i / W) | 0;
          if (x < bb.x0) bb.x0 = x;
          if (x > bb.x1) bb.x1 = x;
          if (y < bb.y0) bb.y0 = y;
          if (y > bb.y1) bb.y1 = y;
        }
      }
      cu.labelsInt32 = labelsInt32;
      cu.allIds = allIds;
      cu.bboxes = bboxes;
      cu.available = true;

      if (savedCleanup && savedCleanup.model === s.model) {
        if (Array.isArray(savedCleanup.rejected_instances)) {
          for (const id of savedCleanup.rejected_instances) {
            if (allIds.has(id)) cu.rejectedSet.add(id);
          }
        }
        if (Array.isArray(savedCleanup.markers)) {
          cu.markers = savedCleanup.markers.map((m) => ({ x: +m.x, y: +m.y }));
        }
      }
    } catch (e) {
      console.warn("cleanup data load failed:", e);
      cu.available = false;
    }
  },

  async _loadPolygons() {
    const s = this.state;
    const pg = s.polygons;
    try {
      const data = await api(`/api/polygons/${encodeURIComponent(s.stem)}`);
      const shapes = Array.isArray(data.shapes) ? data.shapes : [];
      const firstLabel = appLabels[0]?.name || DEFAULT_LABEL;
      pg.shapes = shapes.map((sh) => {
        const rawLabel = sh.label || "";
        const resolvedLabel = (rawLabel && appLabels.some((l) => l.name === rawLabel))
          ? rawLabel : firstLabel;
        return {
          label: resolvedLabel,
          points: (sh.points || []).map((p) => [+p[0], +p[1]]),
          shape_type: sh.shape_type || "polygon",
          group_id: sh.group_id ?? null,
          flags: sh.flags || {},
        };
      });
      pg.envelope = {
        version:   data.version   || "5.0.1",
        flags:     data.flags     || {},
        imagePath: data.imagePath || null,
        imageData: null,
        imageHeight: data.imageHeight || s.H,
        imageWidth:  data.imageWidth  || s.W,
      };
    } catch (e) {
      console.warn("polygons load failed:", e);
      pg.shapes = [];
      pg.envelope = {
        version: "5.0.1", flags: {}, imagePath: null, imageData: null,
        imageHeight: s.H, imageWidth: s.W,
      };
    }
  },

  _loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error(`не завантажилось: ${url}`));
      img.src = url;
    });
  },

  async close(skipFlush = false) {
    if (!this.state.open) return;
    if (!skipFlush) {
      // flushIfDirty() сам викликає close(true) у кінці — делегуємо
      // повністю, щоб close-логіка не виконувалась двічі.
      await this.flushIfDirty();
      return;
    }
    clearTimeout(this.state.cleanup.autosaveTimer);
    clearTimeout(this.state.polygons.autosaveTimer);
    clearTimeout(this.state.groups.autosaveTimer);
    this.state.cleanup.autosaveTimer = null;
    this.state.polygons.autosaveTimer = null;
    this.state.groups.autosaveTimer = null;
    this._unbindEvents();
    this.state.open = false;
    this._historyClear();   // закриття редактора = commit-межа (bake при закритому)
    this.state.cleanup.labelsInt32 = null;
    this.state.cleanup.allIds = null;
    this.state.cleanup.bboxes = null;
    this.state.cleanup.pixelCounts = null;   // v1.16.0: per-instance count cache
    this.state.cleanup.coveredCache = null;  // v1.16.1: covered-instance cache
    this.state.cleanup.rejectedSet = null;
    this.state.cleanup.markers = [];
    this.state.cleanup.hoverId = 0;
    this.state.polygons.shapes = [];
    this.state.polygons.draft = null;
    this.state.polygons.selectedVertices.clear();
    this.state.polygons.selectedShape = -1;
    this.state.groups.list = [];
    this.state.groups.classifications = [];
    this.state.groups.activeId = null;
    this.state.groups.dirty = false;
    this.state.groups.lasso = null;
    this.state.groups.peekUngrouped = false;
    this.state.originalImage = null;
    this.state.overlayImage = null;
    this.state.isPanning = false;
    // Bug D/J fix (v1.16.1 r2): clear groups-overlay layer + mask canvas НА
    // ЗАКРИТТІ. Раніше чистилось лише при switchTab → close (з Groups) →
    // reopen на Polygons показував стару білу групову підсвітку (SVG-діти
    // лишались у DOM від попереднього сеансу редактора).
    this._groupsClearVisualLayers();
    this._modal().classList.remove("open");
    this._wrap().classList.remove("has-cursor-ok", "is-panning", "can-pan", "polygon-mode");
    this._svg().classList.remove("svg--active");
    if (this.state.stem) _clearPolygonsCache(this.state.stem);
    renderAll();
  },

  async flushIfDirty() {
    // Day 9 Bug 2: re-entrancy guard — повторний виклик під час активного
    // flush (швидка навігація + закриття) повертає той самий проміс,
    // а не запускає другий flush з паралельними POST.
    if (this._flushPromise) return this._flushPromise;
    this._flushPromise = (async () => {
      try {
        if (!this.state.open) return;
        if (this.state.polygons.dirty) await this._polySave(true);
        if (this.state.cleanup.dirtyExport) {
          await this._cleanupExportSave(true, false);
        } else if (this.state.cleanup.dirty) {
          await this._cleanupAutosave();
        }
        await this._groupsFlushIfDirty();
        await this.close(true);
      } finally {
        this._flushPromise = null;
      }
    })();
    return this._flushPromise;
  },
};
