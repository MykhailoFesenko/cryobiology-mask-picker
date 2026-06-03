/**
 * tabsMixin — методи перемикання табів Cleanup ⇄ Polygons + оновлення
 * заголовку / hint / stats у топ-барі редактора. Розраховано на spread у
 * `editor/index.js`, тому всі методи звертаються до `this.state` (shared
 * через composite) та DOM refs (`this._modal`, `this._toolbarCleanup` тощо).
 */

import { $ } from "../util.js";
import { state, DEFAULT_LABEL } from "../state.js";
import { setActiveLabel } from "../labels.js";

export const tabsMixin = {
  switchTab(tab) {
    if (tab !== "cleanup" && tab !== "polygons" && tab !== "groups") return;
    if (tab === this.state.activeTab) return;
    // Flush-on-switch: dirty → autosave (без close).
    const flushPoly = this.state.activeTab === "polygons" && this.state.polygons.dirty;
    const flushCleanup = this.state.activeTab === "cleanup" && this.state.cleanup.dirty;
    if (flushPoly) this._polyAutosave();
    if (flushCleanup) this._cleanupAutosave();
    if (this.state.activeTab === "groups" && this.state.groups.dirty) {
      this._groupsScheduleAutosave();
    }
    // Save Polygons/Cleanup НЕ запікає (Day 7 lazy-bake) — фото стає
    // незапеченим. Маркуємо stem dirty одразу, щоб попередження у
    // Groups-табі (_updateHint) бачило актуальний стан і при autosave-flush.
    if (flushPoly || flushCleanup) {
      const it = state.catalog.find((x) => x.stem === this.state.stem);
      if (it && it.state) it.state.dirty = true;
    }
    this._deactivateTab(this.state.activeTab);
    this.state.activeTab = tab;
    this._renderTabs();
    this._activateTab(tab);
    this._updateHint();
  },

  _renderTabs() {
    const active = this.state.activeTab;
    this._modal().querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("tab--active", t.dataset.tab === active);
    });
    this._toolbarCleanup().style.display  = active === "cleanup"  ? "" : "none";
    this._toolbarPolygons().style.display = active === "polygons" ? "" : "none";
    const tbGroups = this._toolbarGroups();
    if (tbGroups) tbGroups.style.display = active === "groups" ? "" : "none";
    const cleanupSave = $("#cleanupSave");
    const polySave    = $("#polySave");
    if (cleanupSave) {
      cleanupSave.disabled = active !== "cleanup";
      cleanupSave.title = active === "cleanup"
        ? "Зберегти cleanup (Enter)"
        : "Доступно тільки на таб Cleanup";
    }
    if (polySave) {
      polySave.disabled = active !== "polygons";
      polySave.title = active === "polygons"
        ? "Зберегти полігони + запекти в маску (Enter)"
        : "Доступно тільки на таб Polygons";
    }
    this._svg().classList.toggle("svg--active", active === "polygons" || active === "groups");
    this._svg().classList.toggle("svg--groups", active === "groups");
    this._wrap().classList.toggle("polygon-mode", active === "polygons");
    if (active === "polygons") {
      setActiveLabel(this.state.polygons.activeLabel || DEFAULT_LABEL);
    }
  },

  _activateTab(tab) {
    if (tab === "cleanup") {
      if (!this.state.cleanup.available && this.state.model) {
        $("#cleanupHint").textContent = "Маска .npy для обраної моделі не знайдена — cleanup недоступний.";
      }
      this._setBgFromActive();
      this._cleanupRedraw();
      this._polyRedrawMarkersOnly();
      this._layerShapes().style.display = "none";
      this._layerVertices().style.display = "none";
      this._layerDraft().style.display = "none";
      this._layerMarkers().style.display = "";
      // Bug F (v1.16.2): відновити видимість маски (marks canvas) за станом
      // галки. _activateTab(groups/polygons) ставить display:none, а повернення
      // на Cleanup раніше НЕ відновлювало → маска лишалась прихована попри
      // ввімкнену галку (юзер мусив клікати галку двічі щоб побачити).
      this._cMarks().style.display = $("#cleanupShowMask").checked ? "block" : "none";
      this._groupsClearVisualLayers();  // Bug D/J: groups overlay + boundary + canvas
    } else if (tab === "polygons") {
      this._setBgFromActive();
      this._layerShapes().style.display = "";
      this._layerVertices().style.display = "";
      this._layerDraft().style.display = "";
      this._layerMarkers().style.display = this.state.polygons.bgSource === "overlay" ? "" : "";
      this._cMarks().style.display = "none";
      this._groupsClearVisualLayers();  // Bug D/J: groups overlay + boundary + canvas
      this._polyRedraw();
    } else if (tab === "groups") {
      this._setBgFromActive();
      // Shapes/vertices видимі read-only (overlay поверх baked masks),
      // marker layer ховаємо.
      this._layerShapes().style.display = "";
      this._layerVertices().style.display = "none";
      this._layerDraft().style.display = "none";
      this._layerMarkers().style.display = "none";
      this._cMarks().style.display = "none";
      const lg = this._groupsLayer(); if (lg) lg.style.display = "";
      // Mask-fill canvas вмикається у _groupsRedrawMaskCanvas
      this._groupsRender();
    }
  },

  _deactivateTab(tab) {
    if (tab === "cleanup") {
      // marks canvas приховається в _activateTab(polygons)
    } else if (tab === "polygons") {
      this.state.polygons.hoverShapeIdx = -1;
      this.state.polygons.hoverVertex = null;
      this._cMarks().style.display = "";
    } else if (tab === "groups") {
      // Bug D/J (v1.16.2): чистимо ВСІ візуальні шари груп (overlay + білий
      // boundary-контур #groupsBoundaryLayer + mask canvas) одним хелпером.
      // Раніше boundary layer не чистився ніде → біла обводка active-групи
      // лишалась на Cleanup/Polygons «ні в якому випадку».
      this._groupsClearVisualLayers();
      this.state.groups.peekUngrouped = false;
    }
  },

  _updateTitle() {
    const { stem, model } = this.state;
    const t = $("#cleanupTitle");
    t.textContent = `Editor: ${stem}${model ? " — " + model : ""}`;
  },

  _updateHint() {
    const hint = $("#cleanupHint");
    if (this.state.activeTab === "groups") {
      const g = this.state.groups;
      const n = g.list.length;
      // Групувати коректно можна лише по ЗАПЕЧЕНІЙ масці. Якщо фото має
      // незапечені зміни (полігони/cleanup) — bake зрушить instance ID і
      // групи «з'їдуть». Попереджаємо, щоб юзер спершу запік.
      // Solution B (v1.16.2): групування рахується з РОБОЧИХ даних (raw labels +
      // полігони), не з запеченої маски. Завдяки reserved-ID (raw==baked) групи
      // не «з'їжджають» при фінальному bake → старе попередження «спершу
      // Зберегти все» більше не потрібне і лише заважало.
      const warn = "";
      const toolHint = g.tool === "picker"
        ? `<kbd>G</kbd> Picker: клік на інстанс → обрати його групу. <kbd>I</kbd> (hold) — необведені.`
        : `<kbd>A</kbd> Edit: клік = toggle, потягни = lasso. <kbd>G</kbd> — Picker. <kbd>I</kbd> (hold) — необведені.`;
      hint.innerHTML = warn +
        `🔗 <b>Groups</b>: створи групу (＋ Нова). ${toolHint} ` +
        `Зараз груп: ${n}${g.activeId ? ", активна: " + g.activeId : ""}.`;
      return;
    }
    if (this.state.activeTab === "cleanup") {
      if (this.state.cleanup.tool === "reject") {
        hint.innerHTML = `🧹 <b>Reject</b>: клік по клітині → викинути/повернути. ` +
          `<kbd>M</kbd> — перемкнути на маркери. <kbd>O</kbd> (hold) — оригінал. ` +
          `Колесо — зум, ПКМ/СКМ — pan.`;
      } else {
        hint.innerHTML = `📍 <b>Mark missing</b>: клік по місцю = нова помітка "тут треба дорозмітити". ` +
          `Клік по існуючій — видалити. <kbd>R</kbd> — назад до reject.`;
      }
    } else {
      if (this.state.polygons.tool === "draw") {
        hint.innerHTML = `✏️ <b>Draw</b>: клік = вершина. <kbd>Shift</kbd> (hold) = freehand. ` +
          `<kbd>Enter</kbd>/dblclick — замкнути. <kbd>Backspace</kbd> — прибрати вершину. ` +
          `<kbd>Esc</kbd> — скасувати. <kbd>E</kbd> — до edit.`;
      } else if (this.state.polygons.tool === "pick") {
        hint.innerHTML = `🎯 <b>Pick</b>: клік по клітині на масці → seed тільки цього інстанса. ` +
          `Rejected не беруться. <kbd>D</kbd> — до draw, <kbd>E</kbd> — до edit.`;
      } else {
        hint.innerHTML = `✋ <b>Edit</b>: drag = перенести вершину. dblclick на ребрі = вставити вершину. ` +
          `<kbd>Alt</kbd>+клік — видалити вершину. <kbd>Delete</kbd> — обраний полігон. ` +
          `Drag по порожньому = lasso multi-select (<kbd>Shift</kbd> додати, <kbd>Alt</kbd> відняти). ` +
          `<kbd>D</kbd> — до draw.`;
      }
    }
  },

  _updateStats() {
    const parts = [];
    const cu = this.state.cleanup;
    const pg = this.state.polygons;
    const gr = this.state.groups;
    if (this.state.activeTab === "groups") {
      const invalid = gr.classifications.filter((c) => c && c.valid === false).length;
      parts.push(`groups: ${gr.list.length}`);
      if (invalid > 0) parts.push(`invalid: ${invalid}`);
      if (gr.activeId) parts.push(`active: ${gr.activeId}`);
      if (gr.dirty) parts.push("● dirty");
      $("#cleanupStats").textContent = parts.join(" · ") || "—";
      return;
    }
    if (this.state.activeTab === "cleanup") {
      if (cu.available) {
        const total = cu.allIds ? cu.allIds.size : 0;
        const rej = cu.rejectedSet ? cu.rejectedSet.size : 0;
        parts.push(`інст: ${total}`, `rejected: ${rej}`, `markers: ${cu.markers.length}`, `undo: ${this._historyDepth()}`);
      } else {
        parts.push(`cleanup недоступний`);
      }
    } else {
      const totalPts = pg.shapes.reduce((s, sh) => s + sh.points.length, 0);
      parts.push(`shapes: ${pg.shapes.length}`, `points: ${totalPts}`);
      if (pg.draft) parts.push(`draft: ${pg.draft.points.length}`);
      if (pg.selectedVertices.size) parts.push(`sel: ${pg.selectedVertices.size}`);
      parts.push(`undo: ${this._historyDepth()}`);
    }
    $("#cleanupStats").textContent = parts.join(" · ");
  },
};
