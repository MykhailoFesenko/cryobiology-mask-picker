/**
 * groupsMixin — методи 3-го таба (cell grouping).
 *
 * Composite через spread у editor/index.js. Усі методи звертаються до
 * `this.state.groups` (живе поряд з `cleanup`/`polygons`).
 *
 * Tools (2026-05-26): "edit" (click = toggle, drag = lasso) + "picker"
 * (клік на iid/polygon → обрати його групу, далі auto-switch у edit).
 *
 * == Single-membership ==
 *   Один iid або polygon_index у одній групі. Last-write-wins при ре-додаванні
 *   через `_groupsMoveInstanceToActive` / `_groupsMovePolygonToActive`.
 *   Backend `_enforce_single_membership` дублює це у POST.
 *
 * == Lasso flow (Solution B, v1.16.2 — client-side) ==
 *   `_groupsApplyLasso` рахує bakedIds ЛОКАЛЬНО (`_groupsLassoHitTestLocal`:
 *   скан cu.labelsInt32 (raw) у lasso-шляху, ratio ≥ LASSO_MIN_OVERLAP, реюз
 *   cu.pixelCounts) + polygonHits через centroid. reserved-ID (v1.16.0) робить
 *   raw_iid==baked_iid → id збігаються з фінальним bake, БЕЗ серверного bake/
 *   раунд-трипу у циклі групування. (Старий POST .../lasso-hit-test більше НЕ
 *   викликається — лишений у бекенді лише для тестів/сумісності.)
 *
 * == Backend endpoints ==
 *   GET  /api/groups/<stem>            — envelope + classifications.
 *   POST /api/groups/<stem>            — write + classify + strip_orphan
 *                                        (in-memory; data_sync пише на disk).
 *   POST /api/groups/<stem>/lasso-hit-test — LEGACY (фронт не кличе; Solution B).
 *   GET  /api/group-classes            — кастомні класи груп.
 */
import { $, showToast, _pointInPoly } from "../util.js";
import {
  loadGroups, saveGroups,
  effectiveHSL, hslCss, nextGroupId, classIndexMap,
  groupMemberCount, firstClassId,
} from "../groups.js";
import { openGroupClassesModal } from "../group_classes_manager.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const LASSO_MIN_DIST_SQ = 4;       // не додавати точку якщо dx²+dy² < 4 (image coords)
const LASSO_MIN_OVERLAP  = 0.30;   // ≥30% pixels інстанса всередині lasso
const AUTOSAVE_DEBOUNCE_MS = 800;
const EDIT_DRAG_THRESHOLD_PX = 5;
const EDIT_DRAG_THRESHOLD_SQ = EDIT_DRAG_THRESHOLD_PX * EDIT_DRAG_THRESHOLD_PX;

export const groupsMixin = {
  // -----------------------------------------------------------------
  // Load / Save
  // -----------------------------------------------------------------

  async _groupsLoad() {
    const s = this.state;
    const g = s.groups;
    g.list = [];
    g.classifications = [];
    g.classes = [];
    g.activeId = null;
    g.dirty = false;
    if (!s.stem) return;
    try {
      const data = await loadGroups(s.stem, s.model);
      g.list = Array.isArray(data.groups) ? data.groups : [];
      g.classifications = Array.isArray(data.classifications)
        ? data.classifications : [];
      g.classes = Array.isArray(data.classes) ? data.classes : [];
      if (g.list.length > 0) g.activeId = g.list[0].id;
      // Round 5: backend міг автоматично прибрати orphan iid (посилання
      // на видалені instance). Якщо було щось видалено — позначаємо
      // dirty щоб save закріпив зміни + toast юзеру.
      if (Array.isArray(data.stale_removed) && data.stale_removed.length > 0) {
        g.dirty = true;
        const totalIids = data.stale_removed.reduce(
          (n, x) => n + ((x.removed || []).length), 0);
        const groupsAff = data.stale_removed.map((x) => x.group_id).filter(Boolean);
        showToast(
          `Прибрано ${totalIids} «привидів» (iid яких немає у масці) з ${groupsAff.length} груп`,
          "ok", 3500);
        this._groupsScheduleAutosave();
      }
    } catch (e) {
      console.warn("groups load failed:", e);
      showToast(`Groups: ${e.message}`, "err", 3000);
    }
    this._groupsRender();
  },

  async _groupsSave(force = false) {
    // Day 9 Bug 2: через проміс-чергу редактора — без паралельних POST.
    return this._enqueueSave(async () => {
      const s = this.state;
      const g = s.groups;
      if (!s.stem) return;
      if (!force && !g.dirty) return;
      // Race-guard (undo clobber): запам'ятовуємо «покоління» стану ПЕРЕД
      // мережею. Якщо під час in-flight POST юзер/undo змінив g.list (gen зріс
      // через _groupsScheduleAutosave) — НЕ перезаписуємо свіжий g.list
      // серверним echo застарілого стану. Без цього Ctrl+Z «відкочувався
      // назад»: autosave, запущений ДОДАВАННЯМ інстанса, резолвився ПІСЛЯ undo
      // і робив g.list = resp (стан З інстансом) → інстанс повертався. Новий
      // autosave вже заплановано тим мутатором/undo — він збереже актуальне.
      const gen0 = g.gen || 0;
      try {
        const resp = await saveGroups(s.stem, s.model, g.list);
        if ((g.gen || 0) !== gen0) return;   // локальний стан випередив — не клобати
        g.list = Array.isArray(resp.groups) ? resp.groups : g.list;
        g.classifications = Array.isArray(resp.classifications)
          ? resp.classifications : g.classifications;
        g.dirty = false;
        if (Array.isArray(resp.moves) && resp.moves.length > 0) {
          const moves = resp.moves
            .map((m) => `${m.kind}#${m.id}: ${m.from} → ${m.to}`)
            .join(", ");
          showToast(`✓ Saved. Перенесено: ${moves}`, "ok", 3500);
        } else {
          showToast(`✓ Saved (${g.list.length} груп)`, "ok", 1800);
        }
        this._groupsRender();
      } catch (e) {
        console.warn("groups save failed:", e);
        showToast(`Save Groups: ${e.message}`, "err", 4000);
      }
    });
  },

  _groupsScheduleAutosave() {
    const g = this.state.groups;
    g.gen = (g.gen || 0) + 1;   // мітка покоління (race-guard у _groupsSave проти clobber undo)
    clearTimeout(g.autosaveTimer);
    g.autosaveTimer = setTimeout(() => this._groupsSave(true), AUTOSAVE_DEBOUNCE_MS);
  },

  // ----- Undo/Redo: тонка обгортка над глобальним historyMixin -----
  // Мутації кличуть _groupsPushUndo() ПЕРЕД зміною g.list. Сам undo/redo —
  // _historyUndo/_historyRedo (keys.js + кнопки); знімок груп —
  // {list, activeId}, відновлення у _historyRestore("groups").
  _groupsPushUndo() {
    this._historyPush("groups");
  },

  // -----------------------------------------------------------------
  // Mutations
  // -----------------------------------------------------------------

  _groupsNew() {
    const g = this.state.groups;
    if (!g.classes || g.classes.length === 0) {
      showToast("Створи спершу класи через ⚙ Classes", "err", 2800);
      return;
    }
    this._groupsPushUndo();
    const id = nextGroupId(g.list);
    const newGroup = {
      id,
      class_id: firstClassId(g.classes),
      instance_ids: [],
      polygon_indices: [],
      label: "",
    };
    g.list.push(newGroup);
    g.activeId = id;
    g.dirty = true;
    this._groupsRender();
    this._groupsScheduleAutosave();
  },

  _groupsDeleteActive() {
    const g = this.state.groups;
    if (!g.activeId) return;
    this._groupsPushUndo();
    const before = g.list.length;
    g.list = g.list.filter((x) => x.id !== g.activeId);
    if (g.list.length === before) return;
    g.activeId = g.list[0]?.id || null;
    g.dirty = true;
    this._groupsRender();
    this._groupsScheduleAutosave();
  },

  _groupsSetActive(gid) {
    const g = this.state.groups;
    if (gid === g.activeId) return;
    g.activeId = g.list.some((x) => x.id === gid) ? gid : null;
    this._groupsRender();
  },

  _groupsSetTool(tool) {
    if (tool !== "edit" && tool !== "picker") return;
    this.state.groups.tool = tool;
    // Cleanup any in-flight lasso/press якщо ми міняємо tool у середині drag.
    this.state.groups.editPress = null;
    if (this.state.groups.lasso) {
      this.state.groups.lasso = null;
      if (typeof this._groupsRedrawLassoPreview === "function") {
        this._groupsRedrawLassoPreview();
      }
    }
    const tb = this._toolbarGroups();
    if (tb) {
      tb.querySelectorAll("[data-groups-tool]").forEach((b) =>
        b.classList.toggle("btn--mini-active", b.dataset.groupsTool === tool));
    }
  },

  _groupsActive() {
    const g = this.state.groups;
    return g.list.find((x) => x.id === g.activeId) || null;
  },

  // -----------------------------------------------------------------
  // Hit-test + toggle
  // -----------------------------------------------------------------

  _groupsHitTest(imgX, imgY) {
    const s = this.state;
    // Baked instance (with cleanup labelsInt32 loaded). Skip rejected iids
    // — вони не рендеряться у Groups overlay (line ~590), отже мають бути
    // прозорі для hit-test теж. Інакше клік по rejected pixel поверне
    // rejected iid, `_groupsToggleInstance` покаже toast "не можна",
    // а polygon під тим самим pixel НЕ перевіриться. Очікувана поведінка:
    // rejected ігноруємо як фон → fallback на polygon.
    const cu = s.cleanup;
    if (cu.labelsInt32) {
      const x = Math.round(imgX);
      const y = Math.round(imgY);
      if (x >= 0 && x < s.W && y >= 0 && y < s.H) {
        const iid = cu.labelsInt32[y * s.W + x];
        const isRejected = iid > 0 && cu.rejectedSet && cu.rejectedSet.has(iid);
        // covered (derived-rejected) теж трактуємо як фон → fallback на polygon:
        // полігон «заміщає» instance, тож клік по covered-зоні має влучати у
        // полігон (toggle полігона), а не повертати instance і toast «не можна».
        const isCovered = iid > 0 && this._polyCoveredInstances && this._polyCoveredInstances().has(iid);
        if (iid > 0 && !isRejected && !isCovered) return { kind: "instance", id: iid };
      }
    }
    // Drawn polygon body
    const shapes = s.polygons.shapes;
    for (let i = shapes.length - 1; i >= 0; i--) {
      const pts = shapes[i].points;
      if (_pointInPoly(imgX, imgY, pts)) return { kind: "polygon", id: i };
    }
    return null;
  },

  _groupsToggleInstance(iid) {
    const g = this.state.groups;
    const active = this._groupsActive();
    if (!active) {
      showToast("Створи групу спершу (＋ Нова)", "err", 2200);
      return;
    }
    // Day 6 CP6 + Bug B (v1.16.1): блок add rejected ТА derived-rejected
    // (covered полігоном). covered instance невибірний у Groups — інакше
    // він блимає вибраним на 1с і збиває підсвітку необведених.
    const cu = this.state.cleanup;
    const isRejected = cu && cu.rejectedSet && cu.rejectedSet.has(iid);
    const isCovered = this._polyCoveredInstances && this._polyCoveredInstances().has(iid);
    if (isRejected || isCovered) {
      showToast(isCovered
        ? "Інстанс заміщений полігоном — не можна додати у групу"
        : "Інстанс rejected у Cleanup — не можна додати у групу", "err", 2200);
      return;
    }
    this._groupsPushUndo();
    const cur = new Set(active.instance_ids || []);
    if (cur.has(iid)) {
      cur.delete(iid);
    } else {
      // Single-membership: видалити з усіх інших груп (frontend optimistic)
      for (const other of g.list) {
        if (other.id === active.id) continue;
        other.instance_ids = (other.instance_ids || []).filter((x) => x !== iid);
      }
      cur.add(iid);
    }
    active.instance_ids = Array.from(cur).sort((a, b) => a - b);
    g.dirty = true;
    this._groupsRender();
    this._groupsScheduleAutosave();
  },

  _groupsTogglePolygon(pi) {
    const g = this.state.groups;
    const active = this._groupsActive();
    if (!active) {
      showToast("Створи групу спершу (＋ Нова)", "err", 2200);
      return;
    }
    this._groupsPushUndo();
    const cur = new Set(active.polygon_indices || []);
    if (cur.has(pi)) {
      cur.delete(pi);
    } else {
      for (const other of g.list) {
        if (other.id === active.id) continue;
        other.polygon_indices = (other.polygon_indices || []).filter((x) => x !== pi);
      }
      cur.add(pi);
    }
    active.polygon_indices = Array.from(cur).sort((a, b) => a - b);
    g.dirty = true;
    this._groupsRender();
    this._groupsScheduleAutosave();
  },

  _onGroupsSvgMouseDown(e) {
    // Pan/middle/right → не наш кейс.
    if (this.state.spaceDown || e.button === 1 || e.button === 2) return;
    const pt = this._svgCoordsFromEvent(e);
    if (!pt) return;
    const g = this.state.groups;

    if (g.tool === "picker") {
      e.preventDefault();
      const hit = this._groupsHitTest(pt.x, pt.y);
      if (!hit) {
        showToast("Клікни по інстансу або polygon", "info", 1500);
        return;
      }
      const list = g.list || [];
      let found = null;
      if (hit.kind === "instance") {
        for (const grp of list) {
          if ((grp.instance_ids || []).indexOf(hit.id) !== -1) { found = grp; break; }
        }
      } else {  // polygon
        for (const grp of list) {
          if ((grp.polygon_indices || []).indexOf(hit.id) !== -1) { found = grp; break; }
        }
      }
      if (!found) {
        showToast("Цей інстанс/polygon не у жодній групі", "info", 1800);
        return;
      }
      this._groupsSetActive(found.id);
      this._groupsSetTool("edit");
      return;
    }

    if (g.tool === "edit") {
      if (!this._groupsActive()) {
        showToast("Створи групу спершу (＋ Нова)", "err", 2200);
        return;
      }
      e.preventDefault();
      // Чекаємо у mousemove: drag-threshold → lasso, інакше click → toggle.
      g.editPress = { startX: pt.x, startY: pt.y, isDrag: false };
    }
  },

  _onGroupsSvgMouseMove(e) {
    const g = this.state.groups;
    const pt = this._svgCoordsFromEvent(e);
    if (!pt) return;

    // Edit-tool: переходимо у lasso коли курсор пройшов > threshold від start.
    if (g.editPress && !g.editPress.isDrag) {
      const dx = pt.x - g.editPress.startX;
      const dy = pt.y - g.editPress.startY;
      if (dx * dx + dy * dy >= EDIT_DRAG_THRESHOLD_SQ) {
        g.editPress.isDrag = true;
        g.lasso = {
          active: true,
          path: [[g.editPress.startX, g.editPress.startY], [pt.x, pt.y]],
        };
        this._groupsRedrawLassoPreview();
      }
      return;
    }

    // Активний lasso — продовжуємо path.
    const lasso = g.lasso;
    if (!lasso || !lasso.active) return;
    const last = lasso.path[lasso.path.length - 1];
    const dx = pt.x - last[0], dy = pt.y - last[1];
    if (dx * dx + dy * dy < LASSO_MIN_DIST_SQ) return;
    lasso.path.push([pt.x, pt.y]);
    this._groupsRedrawLassoPreview();
  },

  async _onGroupsSvgMouseUp(e) {
    const g = this.state.groups;
    const press = g.editPress;
    g.editPress = null;

    // 1) Якщо drag перетворився на lasso → apply.
    const lasso = g.lasso;
    if (lasso && lasso.active) {
      lasso.active = false;
      const path = lasso.path.slice();
      g.lasso = null;
      this._groupsRedrawLassoPreview();
      if (path.length < 3) return;
      await this._groupsApplyLasso(path);
      return;
    }

    // 2) Не drag → click → toggle hit за стартовою точкою.
    if (press && !press.isDrag) {
      const hit = this._groupsHitTest(press.startX, press.startY);
      if (!hit) return;
      if (hit.kind === "instance") this._groupsToggleInstance(hit.id);
      else this._groupsTogglePolygon(hit.id);
    }
  },

  _onGroupsLassoCancel() {
    const g = this.state.groups;
    g.editPress = null;
    const lasso = g.lasso;
    if (!lasso) return;
    g.lasso = null;
    this._groupsRedrawLassoPreview();
  },

  // Solution B (v1.16.2): client-side lasso hit-test — БЕЗ серверного bake.
  // Раніше lasso слало POST /api/groups/<stem>/lasso-hit-test, який читав
  // selected/npy (ЗАПЕЧЕНИЙ) → потребувало ~7с full-bake (cellsegkit export)
  // перед групуванням, інакше дані застарілі. Тепер рахуємо з РОБОЧИХ даних:
  // cu.labelsInt32 (raw) + reserved-ID range (v1.16.0) дає raw_iid==baked_iid
  // для уцілілих → id збігаються з тим, що дасть фінальний bake. Полігони
  // ловляться окремо (centroid). Жодного bake у циклі групування.
  // Семантика 1:1 з бекендом: ratio = pixels_під_lasso / total_pixels(iid) ≥ поріг.
  _groupsLassoHitTestLocal(path) {
    const s = this.state;
    const cu = s.cleanup;
    if (!cu || !cu.labelsInt32 || !s.W || !s.H) return [];
    const labels = cu.labelsInt32;
    const W = s.W, H = s.H;
    let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
    for (const [x, y] of path) {
      if (x < xmin) xmin = x; if (x > xmax) xmax = x;
      if (y < ymin) ymin = y; if (y > ymax) ymax = y;
    }
    const x0 = Math.max(0, Math.floor(xmin)), x1 = Math.min(W - 1, Math.ceil(xmax));
    const y0 = Math.max(0, Math.floor(ymin)), y1 = Math.min(H - 1, Math.ceil(ymax));
    if (x1 < x0 || y1 < y0) return [];
    // total pixels per iid — реюз того ж кешу, що й derived rejection.
    if (!cu.pixelCounts) {
      const counts = new Map();
      for (let i = 0; i < W * H; i++) { const id = labels[i]; if (id > 0) counts.set(id, (counts.get(id) || 0) + 1); }
      cu.pixelCounts = counts;
    }
    const under = new Map();
    for (let y = y0; y <= y1; y++) {
      const yW = y * W;
      for (let x = x0; x <= x1; x++) {
        const id = labels[yW + x];
        if (id <= 0) continue;
        if (!_pointInPoly(x, y, path)) continue;
        under.set(id, (under.get(id) || 0) + 1);
      }
    }
    // id≥POLYGON_ID_BASE у raw labels не буває (raw<7000) — полігони окремо.
    const out = [];
    for (const [id, c] of under) {
      const tot = cu.pixelCounts.get(id) || c;
      if (c / tot >= LASSO_MIN_OVERLAP) out.push(id);
    }
    return out;
  },

  async _groupsApplyLasso(path) {
    const s = this.state;
    const active = this._groupsActive();
    if (!active) return;

    // Solution B: hit-test з робочих даних (без bake, без серверного раунд-трипу).
    let bakedIds = this._groupsLassoHitTestLocal(path);

    // Day 6 CP6: filter rejected з backend результату — щоб lasso у Groups
    // не додавав інстанси, які викинуті у Cleanup.
    let filteredRejected = 0;
    const cu = s.cleanup;
    if (cu && cu.rejectedSet && bakedIds.length) {
      // Bug A/B (v1.16.1): виключаємо і явні rejected, і derived-rejected
      // (covered полігоном) — covered instance не можна додати у групу.
      const cov = this._polyCoveredInstances ? this._polyCoveredInstances() : new Set();
      const before = bakedIds.length;
      bakedIds = bakedIds.filter((iid) => !cu.rejectedSet.has(iid) && !cov.has(iid));
      filteredRejected = before - bakedIds.length;
    }

    // Frontend hit-test для drawn polygons (centroid у lasso path)
    const polygonHits = [];
    const shapes = s.polygons.shapes || [];
    for (let i = 0; i < shapes.length; i++) {
      const pts = shapes[i].points;
      if (!pts || pts.length < 3) continue;
      let sx = 0, sy = 0;
      for (const [x, y] of pts) { sx += x; sy += y; }
      const cx = sx / pts.length, cy = sy / pts.length;
      if (_pointInPoly(cx, cy, path)) polygonHits.push(i);
    }

    if (bakedIds.length === 0 && polygonHits.length === 0) {
      showToast("Lasso: 0 інстансів/полігонів", "ok", 1500);
      return;
    }

    // Чи є що РЕАЛЬНО додати (не всі вже в active)? Інакше — no-op без
    // undo-запису (інакше лассо по вже-згрупованих лишало б фантомний Ctrl+Z).
    const curIids = new Set(active.instance_ids || []);
    const curPolys = new Set(active.polygon_indices || []);
    if (!bakedIds.some((id) => !curIids.has(id)) &&
        !polygonHits.some((pi) => !curPolys.has(pi))) {
      showToast("Lasso: усі вже в цій групі", "ok", 1500);
      return;
    }

    // Додаємо у active з single-membership
    this._groupsPushUndo();
    const g = s.groups;
    let addedInst = 0, addedPoly = 0;

    if (bakedIds.length) {
      const cur = new Set(active.instance_ids || []);
      for (const iid of bakedIds) {
        if (!cur.has(iid)) {
          for (const other of g.list) {
            if (other.id === active.id) continue;
            other.instance_ids = (other.instance_ids || []).filter((x) => x !== iid);
          }
          cur.add(iid);
          addedInst++;
        }
      }
      active.instance_ids = Array.from(cur).sort((a, b) => a - b);
    }
    if (polygonHits.length) {
      const cur = new Set(active.polygon_indices || []);
      for (const pi of polygonHits) {
        if (!cur.has(pi)) {
          for (const other of g.list) {
            if (other.id === active.id) continue;
            other.polygon_indices = (other.polygon_indices || []).filter((x) => x !== pi);
          }
          cur.add(pi);
          addedPoly++;
        }
      }
      active.polygon_indices = Array.from(cur).sort((a, b) => a - b);
    }

    const added = addedInst + addedPoly;
    if (added > 0) g.dirty = true;
    this._groupsRender();
    if (added > 0) this._groupsScheduleAutosave();
    const parts = [];
    if (addedInst) parts.push(`+${addedInst} інст`);
    if (addedPoly) parts.push(`+${addedPoly} полігонів`);
    if (filteredRejected) parts.push(`(${filteredRejected} rejected skip)`);
    showToast(`Lasso: ${parts.join(", ") || "0"}`, "ok", 1800);
  },

  _groupsRedrawLassoPreview() {
    const layer = $("#groupsOverlayLayer");
    if (!layer) return;
    let prev = layer.querySelector("polyline.lasso-preview");
    if (prev) prev.remove();
    const lasso = this.state.groups.lasso;
    if (!lasso || !lasso.active || lasso.path.length < 2) return;
    const pl = document.createElementNS(SVG_NS, "polyline");
    pl.setAttribute("class", "lasso-preview");
    pl.setAttribute("points", lasso.path.map(([x, y]) => `${x},${y}`).join(" "));
    pl.setAttribute("fill", "none");
    pl.setAttribute("stroke", "#ffd84d");
    pl.setAttribute("stroke-width", "2");
    pl.setAttribute("stroke-dasharray", "4 3");
    pl.setAttribute("vector-effect", "non-scaling-stroke");
    pl.setAttribute("pointer-events", "none");
    layer.appendChild(pl);
  },

  _groupsSetClassIdForActive(newClassId) {
    const g = this.state.groups;
    if (!g.activeId || !newClassId) return;
    const target = g.list.find((x) => x.id === g.activeId);
    if (!target || target.class_id === newClassId) return;
    this._groupsPushUndo();
    target.class_id = newClassId;
    g.dirty = true;
    this._groupsRender();
    this._groupsScheduleAutosave();
  },

  _groupsOpenClassesManager() {
    openGroupClassesModal();
  },

  _groupsClassById(cid) {
    return (this.state.groups.classes || []).find((c) => c.id === cid) || null;
  },

  // -----------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------

  _groupsRender() {
    this._groupsRenderChips();
    this._groupsRenderClassSelect();
    this._groupsRenderButtons();
    this._groupsRedrawOverlay();
  },

  _groupsRenderChips() {
    const host = $("#groupsChips");
    if (!host) return;
    const g = this.state.groups;
    host.innerHTML = "";
    const idxMap = classIndexMap(g.list);
    g.list.forEach((group, i) => {
      const cls = g.classifications[i] || {};
      const hsl = effectiveHSL(group, g.classes, idxMap.get(group.id) || 0);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "group-chip";
      if (group.id === g.activeId) chip.classList.add("group-chip--active");
      if (cls.valid === false) chip.classList.add("group-chip--invalid");
      // Round 5.1: CSS vars з повним HSL (hue + sat + light). CSS-правила
      // використовують їх для hover/active/dot — кожен стейт зберігає
      // per-group jitter, а не зводиться до фіксованої saturation.
      chip.style.setProperty("--gh-hue",   String(hsl.h));
      chip.style.setProperty("--gh-sat",   `${hsl.s}%`);
      chip.style.setProperty("--gh-light", `${hsl.l}%`);
      chip.style.color = "var(--fg)";
      chip.dataset.gid = group.id;
      chip.setAttribute("role", "listitem");
      const className = this._groupsClassById(group.class_id)?.name || group.type || "—";
      const reason = cls.valid === false ? ` — ${cls.reason || "invalid"}` : "";
      // Counts string з усіх labels
      const counts = cls.counts || {};
      const countsStr = Object.entries(counts)
        .map(([k, v]) => `${k}:${v}`).join(", ") || "пусто";
      // 2026-05-21 round 4: показати в tooltip конкретні iid по лейблах
      // + orphan-iids (відсутні у npy), щоб юзер міг знайти кого видалити
      // у invalid-групі (типовий кейс db_img_0169 g_035 — захоплений
      // nucleus-instance у vesicle_cluster).
      const byLbl = cls.iids_by_label || {};
      const byLblStr = Object.entries(byLbl)
        .map(([k, v]) => {
          const preview = v.slice(0, 8).join(",");
          const more = v.length > 8 ? `+${v.length - 8}` : "";
          return `  ${k}: ${preview}${more}`;
        }).join("\n");
      const orphans = cls.orphan_iids || [];
      const orphanLine = orphans.length
        ? `\nstale iid (нема у npy, треба видалити): ${orphans.slice(0, 8).join(",")}${orphans.length > 8 ? "+" + (orphans.length - 8) : ""}`
        : "";
      chip.title = `${group.id} · ${className}${reason}\n` +
                   `members: ${groupMemberCount(group)} (${countsStr})` +
                   (byLblStr ? `\n${byLblStr}` : "") +
                   orphanLine;
      chip.innerHTML = "";
      const dot = document.createElement("span");
      dot.className = "group-chip__dot";
      // background береться з CSS правила що використовує --gh-* vars з chip-у
      chip.appendChild(dot);
      const nameSpan = document.createElement("span");
      nameSpan.className = "group-chip__name";
      nameSpan.textContent = `${group.id} · ${className}`;
      chip.appendChild(nameSpan);
      const cntSpan = document.createElement("span");
      cntSpan.className = "group-chip__count";
      cntSpan.textContent = String(groupMemberCount(group));
      chip.appendChild(cntSpan);
      const badge = document.createElement("span");
      badge.className = "group-chip__badge " +
        (cls.valid === false ? "group-chip__badge--invalid" : "group-chip__badge--ok");
      badge.textContent = cls.valid === false ? "✗" : "✓";
      chip.appendChild(badge);
      chip.addEventListener("click", () => this._groupsSetActive(group.id));
      // Round 5: кнопка «🧹 видалити rogue» — одним кліком виправити
      // invalid-групу (видалити iid-порушники).
      const rogue = cls.rogue_iids || [];
      if (rogue.length > 0) {
        const fix = document.createElement("span");
        fix.className = "group-chip__fix";
        fix.textContent = "🧹";
        fix.title = `Видалити ${rogue.length} «зайвих» iid: ${rogue.slice(0, 5).join(", ")}${rogue.length > 5 ? "..." : ""}`;
        fix.addEventListener("click", (e) => {
          e.stopPropagation();
          this._groupsRemoveRogue(group.id, rogue);
        });
        chip.appendChild(fix);
      }
      host.appendChild(chip);
    });
  },

  _groupsRemoveRogue(gid, rogue) {
    const g = this.state.groups;
    const target = g.list.find((x) => x.id === gid);
    if (!target || !rogue || rogue.length === 0) return;
    this._groupsPushUndo();
    const rogueSet = new Set(rogue.map(Number));
    const before = (target.instance_ids || []).length;
    target.instance_ids = (target.instance_ids || [])
      .filter((iid) => !rogueSet.has(Number(iid)));
    const removed = before - target.instance_ids.length;
    if (removed === 0) return;
    g.dirty = true;
    showToast(`Видалено ${removed} порушників з ${gid}`, "ok", 2200);
    this._groupsRender();
    this._groupsScheduleAutosave();
  },

  _groupsRenderClassSelect() {
    const sel = $("#groupsClassSelect");
    if (!sel) return;
    const g = this.state.groups;
    const active = g.list.find((x) => x.id === g.activeId);
    // Rebuild options
    sel.innerHTML = "";
    for (const c of (g.classes || [])) {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name || c.id;
      sel.appendChild(opt);
    }
    if (active && active.class_id) {
      sel.value = active.class_id;
    } else if ((g.classes || []).length > 0) {
      sel.value = g.classes[0].id;
    }
    sel.disabled = !active;
  },

  _groupsRenderButtons() {
    const g = this.state.groups;
    const del = $("#groupsDelete");
    if (del) del.disabled = !g.activeId;
    const save = $("#groupsSave");
    if (save) save.disabled = !g.dirty;
  },

  _groupsRedrawOverlay() {
    const layer = $("#groupsOverlayLayer");
    if (!layer) return;
    while (layer.firstChild) layer.removeChild(layer.firstChild);
    if (this.state.activeTab !== "groups") return;
    const g = this.state.groups;
    const bboxes = this.state.cleanup.bboxes;
    const shapes = this.state.polygons.shapes;
    const idxMap = classIndexMap(g.list);
    g.list.forEach((group, gi) => {
      const hsl = effectiveHSL(group, g.classes, idxMap.get(group.id) || 0);
      const isActive = group.id === g.activeId;
      // Match instance rendering (_groupsRedrawMaskCanvas):
      //   active   → full HSL @ alpha 0.86, білий 1px контур (= cell active);
      //   inactive → s*0.55, l*0.7, alpha 0.43, без контуру (= cell other).
      const fill = isActive
        ? hslCss(hsl, 0.86)
        : hslCss({
            h: hsl.h,
            s: Math.max(40, hsl.s * 0.92),   // match instance canvas (more visible)
            l: hsl.l * 0.9,
          }, 0.62);
      for (const pi of (group.polygon_indices || [])) {
        const shape = shapes[pi];
        if (!shape || !shape.points || shape.points.length < 3) continue;
        const poly = document.createElementNS(SVG_NS, "polygon");
        poly.setAttribute("points",
          shape.points.map(([x, y]) => `${x},${y}`).join(" "));
        poly.setAttribute("fill", fill);
        if (isActive) {
          poly.setAttribute("stroke", "#ffffff");
          poly.setAttribute("stroke-opacity", "0.95");
          poly.setAttribute("stroke-width", "1");
        } else {
          poly.setAttribute("stroke", "none");
        }
        poly.setAttribute("vector-effect", "non-scaling-stroke");
        poly.setAttribute("pointer-events", "none");
        layer.appendChild(poly);
      }
      // Baked instances рендеряться як mask-fill у canvas (_groupsRedrawMaskCanvas).
    });
    // Peek «Необведені»: polygon shapes без group membership — cyan dashed
    // outline (відповідає peek-instance підсвітці у mask canvas).
    if (g.peekUngrouped && Array.isArray(shapes) && shapes.length) {
      const groupedPi = new Set();
      for (const grp of g.list) {
        for (const pi of (grp.polygon_indices || [])) groupedPi.add(pi);
      }
      for (let pi = 0; pi < shapes.length; pi++) {
        if (groupedPi.has(pi)) continue;
        const shape = shapes[pi];
        if (!shape || !Array.isArray(shape.points) || shape.points.length < 3) continue;
        const poly = document.createElementNS(SVG_NS, "polygon");
        poly.setAttribute("points",
          shape.points.map(([x, y]) => `${x},${y}`).join(" "));
        poly.setAttribute("fill", "rgba(0,220,255,0.20)");
        poly.setAttribute("stroke", "rgba(0,220,255,0.95)");
        poly.setAttribute("stroke-width", "2");
        poly.setAttribute("stroke-dasharray", "5,3");
        poly.setAttribute("vector-effect", "non-scaling-stroke");
        poly.setAttribute("pointer-events", "none");
        layer.appendChild(poly);
      }
    }
    // CP6: mask-fill canvas pass — рендер у окремий canvas, не у SVG.
    if (typeof this._groupsRedrawMaskCanvas === "function") {
      this._groupsRedrawMaskCanvas();
    }
  },

  // -----------------------------------------------------------------
  // Flush helper (called from editor.flushIfDirty / close)
  // -----------------------------------------------------------------

  async _groupsFlushIfDirty() {
    const g = this.state.groups;
    clearTimeout(g.autosaveTimer);
    g.autosaveTimer = null;
    if (g.dirty) await this._groupsSave(true);
  },

  // -----------------------------------------------------------------
  // Mask-fill canvas (CP6) — pixel-level tinted instance masks
  // -----------------------------------------------------------------

  // Спільний перемикач peek «Необведені» (hold-кнопка миші #groupsPeekUngrouped
  // + hold-клавіша I). Винесено, щоб events.js (mousedown) і keys.js (keydown)
  // не дублювали логіку. No-op якщо стан уже потрібний.
  _setPeek(on) {
    const g = this.state.groups;
    if (!!g.peekUngrouped === !!on) return;
    g.peekUngrouped = !!on;
    const btn = $("#groupsPeekUngrouped");
    if (btn) btn.classList.toggle("btn--mini-active", !!on);
    this._groupsRedrawMaskCanvas();
  },

  _groupsRedrawMaskCanvas() {
    const canvas = $("#groupsMaskCanvas");
    if (!canvas) return;
    const s = this.state;
    if (s.activeTab !== "groups") {
      canvas.style.display = "none";
      return;
    }
    const labels = s.cleanup.labelsInt32;
    const W = s.W, H = s.H;
    if (!labels || !W || !H) {
      canvas.style.display = "none";
      return;
    }
    if (canvas.width !== W) canvas.width = W;
    if (canvas.height !== H) canvas.height = H;
    canvas.style.display = "";

    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    const img = ctx.createImageData(W, H);
    const buf = img.data;

    // Round 5: чітка ієрархія підсвітки:
    //   - rogue iid (порушники constraints active group) → яскраво-червоний
    //     RGB(255,40,40), alpha 240. Видно одразу.
    //   - active group instance → повний HSL клас, alpha 230.
    //   - інші групи → той же HSL але дуже знижена насиченість + alpha 110.
    //
    // ImageData class кодуємо у typed buffers для speed (5M pixel loop).
    //   iidKind[iid]: 0=skip, 1=rogue, 2=active, 3=other
    //   iidR/G/B[iid]: колір якщо kind 2/3
    const g = s.groups;
    const rejectedSet = s.cleanup && s.cleanup.rejectedSet ? s.cleanup.rejectedSet : null;
    const activeId = g.activeId;
    const classifications = g.classifications || [];

    // Зіставимо classification по index → group.id (paths могли змінитись).
    const clsByGid = new Map();
    g.list.forEach((group, i) => clsByGid.set(group.id, classifications[i] || {}));

    // Розмір словника: max iid у списках. Map швидший за typed array для
    // sparse iids (2k-3k груповано з 2k-3k iid у фото).
    const iidPlan = new Map();   // iid → {kind: "rogue"|"active"|"other", r,g,b, a}

    // Спершу other (inactive), потім active — щоб active перебило колізії.
    // Rogue ставимо ОСТАННЕ (від active group) — обводимо червоним поверх.
    const idxMap = classIndexMap(g.list);
    g.list.forEach((group, gi) => {
      const ids = group.instance_ids || [];
      if (ids.length === 0) return;
      const isActive = group.id === activeId;
      const hsl = effectiveHSL(group, g.classes, idxMap.get(group.id) || 0);
      let rgb;
      let alpha;
      if (isActive) {
        rgb = _hslToRgb(hsl.h, hsl.s, hsl.l);
        alpha = 220;
      } else {
        // 5a (v1.16.2): менш тусклі неактивні групи — тримаємо насиченість
        // (раніше s*0.55+l*0.7 робило всі групи муторно сіро-зеленими, не
        // розрізнити). Тепер відтінок виразний, але alpha нижча за active.
        rgb = _hslToRgb(hsl.h, Math.max(40, hsl.s * 0.92), hsl.l * 0.9);
        alpha = 170;
      }
      for (const iid of ids) {
        if (rejectedSet && rejectedSet.has(iid)) continue;
        // active перебиває other (інстанс у двох групах — пріоритет активної)
        if (isActive || !iidPlan.has(iid)) {
          iidPlan.set(iid, { kind: isActive ? "active" : "other",
                             r: rgb[0], g: rgb[1], b: rgb[2], a: alpha });
        }
      }
    });
    // Round 5: rogue iids активної групи → червоний поверх. Constraint
    // порушники (наприклад nucleus у vesicle_cluster) — найважливіше
    // бачити одразу. Inactive groups rogue не підсвічуємо — лише active,
    // щоб не перевантажувати канвас.
    const activeCls = clsByGid.get(activeId) || {};
    const rogueIids = activeCls.rogue_iids || [];
    for (const iid of rogueIids) {
      if (rejectedSet && rejectedSet.has(iid)) continue;
      iidPlan.set(iid, { kind: "rogue", r: 255, g: 40, b: 40, a: 240 });
    }

    if (iidPlan.size === 0 && !s.groups.peekUngrouped) {
      ctx.putImageData(img, 0, 0);
      return;
    }

    // Single-pass pixel scan. На 2572×1956 — ~50-80ms.
    const n = W * H;
    const peek = !!s.groups.peekUngrouped;
    // Bug M (v1.16.2): «Необведені» (peek) НЕ підсвічує instance, що ВЖЕ
    // покриті полігоном (derived-rejected, модель v1.16.1) — вони «обведені»
    // за визначенням. Раніше виключались лише rejected, тому covered блимали
    // cyan і збивали («необведені» мали б бути ТІЛЬКИ реально вільні).
    const coveredSet = peek && this._polyCoveredInstances ? this._polyCoveredInstances() : null;
    for (let i = 0, p = 0; i < n; i++, p += 4) {
      const iid = labels[i];
      if (iid <= 0) continue;
      const plan = iidPlan.get(iid);
      if (plan) {
        buf[p]     = plan.r;
        buf[p + 1] = plan.g;
        buf[p + 2] = plan.b;
        buf[p + 3] = plan.a;
      } else if (peek && !(rejectedSet && rejectedSet.has(iid))
                 && !(coveredSet && coveredSet.has(iid))) {
        // hold-кнопка «Необведені»: інстанс поза будь-якою групою — cyan.
        buf[p]     = 0;
        buf[p + 1] = 220;
        buf[p + 2] = 255;
        buf[p + 3] = 200;
      }
    }
    ctx.putImageData(img, 0, 0);

    // Round 5: boundary stroke для active+rogue. Передбачаємо ~50-200 iid
    // — обчислюємо bbox-edge через sub-pass: на кожен pixel перевіряємо
    // 4-сусідів; якщо хтось not-same-iid — pixel у границю. Робимо вже
    // АФТЕР putImageData у окрему overlay (top-most), щоб обводка не
    // змішувалася з multiply-blend.
    this._groupsDrawBoundaryStroke(canvas, labels, W, H, iidPlan, activeId);
  },

  _groupsDrawBoundaryStroke(canvas, labels, W, H, iidPlan, activeId) {
    // Знаходимо boundary pixels (active + rogue iid) і малюємо контур у
    // окремий SVG layer щоб не зачіпало multiply-blend mask canvas.
    // Active boundary — білий (контраст до будь-якого HSL).
    // Rogue boundary — яскраво-червоний (помітно одразу).
    const svgLayer = $("#groupsBoundaryLayer");
    if (!svgLayer) return;
    while (svgLayer.firstChild) svgLayer.removeChild(svgLayer.firstChild);

    const targetIids = new Set();
    iidPlan.forEach((plan, iid) => {
      if (plan.kind === "active" || plan.kind === "rogue") targetIids.add(iid);
    });
    if (targetIids.size === 0) return;

    // Round 5 perf: bbox-обмежений scan. cleanup.bboxes пре-обчислений у
    // _reloadCleanupData (`{iid: {x0,y0,x1,y1}}`). Замість 5M-pixel scan
    // — пробігаємо лише по bbox кожного target iid (~30×30..50×50 для
    // везикул/ядер). 100 iid × 2500 px ≈ 250k pixel порівнянь.
    const SVG_NS = "http://www.w3.org/2000/svg";
    const bboxes = this.state.cleanup.bboxes;
    const edgePixelsByIid = new Map();
    for (const iid of targetIids) {
      const bb = bboxes && bboxes.get(iid);
      if (!bb) continue;
      const x0 = Math.max(0, bb.x0 - 1);
      const y0 = Math.max(0, bb.y0 - 1);
      const x1 = Math.min(W - 1, bb.x1 + 1);
      const y1 = Math.min(H - 1, bb.y1 + 1);
      const arr = [];
      for (let y = y0; y <= y1; y++) {
        const yW = y * W;
        for (let x = x0; x <= x1; x++) {
          const i = yW + x;
          if (labels[i] !== iid) continue;
          const up    = (y > 0)     ? labels[i - W] : -1;
          const down  = (y < H - 1) ? labels[i + W] : -1;
          const left  = (x > 0)     ? labels[i - 1] : -1;
          const right = (x < W - 1) ? labels[i + 1] : -1;
          if (up !== iid || down !== iid || left !== iid || right !== iid) {
            arr.push(x, y);
          }
        }
      }
      if (arr.length) edgePixelsByIid.set(iid, arr);
    }

    // Одна <path> per iid через "M x y h 1" — швидкий рендер 1-pixel marks.
    // Для великих iid (тисячі edge pixel) це гарантовано прийнятно.
    for (const [iid, coords] of edgePixelsByIid) {
      const plan = iidPlan.get(iid);
      if (!plan) continue;
      const isRogue = plan.kind === "rogue";
      const path = document.createElementNS(SVG_NS, "path");
      const d = [];
      for (let k = 0; k < coords.length; k += 2) {
        const x = coords[k], y = coords[k + 1];
        d.push(`M${x} ${y}h1v1h-1z`);
      }
      path.setAttribute("d", d.join(""));
      path.setAttribute("fill", isRogue ? "#ff2828" : "#ffffff");
      path.setAttribute("fill-opacity", isRogue ? "1" : "0.95");
      path.setAttribute("pointer-events", "none");
      path.classList.add(isRogue ? "groups-boundary--rogue" : "groups-boundary--active");
      svgLayer.appendChild(path);
    }
  },
};

// HSL (0-360, 0-100, 0-100) → RGB (0-255). Standard algorithm.
function _hslToRgb(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = Math.max(0, Math.min(100, s)) / 100;
  l = Math.max(0, Math.min(100, l)) / 100;
  if (s === 0) {
    const v = Math.round(l * 255);
    return [v, v, v];
  }
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = h / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  let r1, g1, b1;
  if      (hp < 1) { r1 = c; g1 = x; b1 = 0; }
  else if (hp < 2) { r1 = x; g1 = c; b1 = 0; }
  else if (hp < 3) { r1 = 0; g1 = c; b1 = x; }
  else if (hp < 4) { r1 = 0; g1 = x; b1 = c; }
  else if (hp < 5) { r1 = x; g1 = 0; b1 = c; }
  else             { r1 = c; g1 = 0; b1 = x; }
  const m = l - c / 2;
  return [
    Math.round((r1 + m) * 255),
    Math.round((g1 + m) * 255),
    Math.round((b1 + m) * 255),
  ];
}
