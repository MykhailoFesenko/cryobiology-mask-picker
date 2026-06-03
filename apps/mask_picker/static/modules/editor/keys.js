/**
 * keysMixin — клавіатурні обробники модалки редактора. Делегуються з
 * `keyboard.js` коли `editor.state.open === true`.
 *
 * Spacebar має подвійну роль (close-draft у polygons-draw, інакше pan-mode),
 * O — hold-original-preview, Tab — перемикання табів, цифри 1-9 — active label
 * у polygons. Решта — тулзи / undo/redo / save / esc / delete.
 */

import { appLabels } from "../state.js";
import { setActiveLabel } from "../labels.js";
import { openMultiSeedModal } from "../multiseed.js";

export const keysMixin = {
  onKey(e) {
    const ctrl = e.ctrlKey || e.metaKey;
    // Spacebar: у polygons + draw + draft з ≥3 точками закриває draft,
    // інакше — pan mode (як було, спільне для обох табів).
    if (e.code === "Space") {
      if (this.state.activeTab === "polygons") {
        const pg = this.state.polygons;
        if (pg.tool === "draw" && pg.draft && pg.draft.points.length >= 3) {
          e.preventDefault();
          this._polyCloseDraft();
          return;
        }
      }
      e.preventDefault();
      this.state.spaceDown = true;
      this._wrap().classList.add("can-pan");
      return;
    }
    if (e.code === "Digit0" || e.code === "Numpad0") { e.preventDefault(); this.resetZoom(); return; }
    if (e.code === "KeyO") {
      if (e.repeat) { e.preventDefault(); return; }
      e.preventDefault();
      if (this.state.bgPreviewPrev === null) {
        this.state.bgPreviewPrev = this._currentBgSource();
        this.setBgSource("original");
        this._wrap().classList.add("hold-orig");
      }
      return;
    }
    if (e.code === "Tab") {
      e.preventDefault();
      const order = ["cleanup", "polygons", "groups"];
      const cur = order.indexOf(this.state.activeTab);
      const next = order[(cur + 1) % order.length] || "cleanup";
      this.switchTab(next);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      const pg = this.state.polygons;
      if (this.state.activeTab === "groups" && this.state.groups.lasso) {
        this._onGroupsLassoCancel();
        return;
      }
      if (this.state.activeTab === "polygons" && pg.draft) {
        this._polyCancelDraft();
        return;
      }
      if (this.state.activeTab === "polygons" && (pg.selectedShape >= 0 || pg.selectedVertices.size)) {
        pg.selectedShape = -1;
        pg.selectedVertices.clear();
        this._polyUpdateButtons();
        this._polyRedraw();
        return;
      }
      this.close();
      return;
    }
    // Undo/Redo — ГЛОБАЛЬНИЙ хронологічний стек (historyMixin). Один стек на
    // всі 3 домени: Ctrl+Z знімає глобально-останню дію (автоперемикає таб),
    // Ctrl+Shift+Z / Ctrl+Y — навпаки. Не залежить від activeTab.
    if (ctrl && !e.shiftKey && e.code === "KeyZ") {
      e.preventDefault();
      this._historyUndo();
      return;
    }
    if (ctrl && e.shiftKey && e.code === "KeyZ") {
      e.preventDefault();
      this._historyRedo();
      return;
    }
    if (ctrl && e.code === "KeyY") {
      e.preventDefault();
      this._historyRedo();
      return;
    }

    if (this.state.activeTab === "cleanup") {
      if (e.key === "Enter") { e.preventDefault(); this._cleanupExportSave(false, true); return; }
      if (e.code === "KeyR") { e.preventDefault(); this._setCleanupTool("reject"); return; }
      if (e.code === "KeyM") { e.preventDefault(); this._setCleanupTool("marker"); return; }
      return;
    }

    if (this.state.activeTab === "groups") {
      // Hold-I: підсвітити «Необведені» (peek), механіка як hold-O. Тримай —
      // показує вільні instance; відпусти — гасне (onKeyUp). Дублює hold-кнопку
      // #groupsPeekUngrouped (events.js); спільна логіка у _setPeek.
      if (!ctrl && e.code === "KeyI") { e.preventDefault(); if (!e.repeat) this._setPeek(true); return; }
      if (e.key === "Enter") { e.preventDefault(); this._groupsSave(true); return; }
      if (e.code === "KeyN") { e.preventDefault(); this._groupsNew(); return; }
      if (e.code === "KeyA") { e.preventDefault(); this._groupsSetTool("edit"); return; }
      if (e.code === "KeyL") { e.preventDefault(); this._groupsSetTool("edit"); return; }
      if (e.code === "KeyG") { e.preventDefault(); this._groupsSetTool("picker"); return; }
      if (e.key === "Delete") { e.preventDefault(); this._groupsDeleteActive(); return; }
      return;
    }

    // polygons
    if (e.key === "Enter") {
      e.preventDefault();
      const pg = this.state.polygons;
      if (pg.tool === "draw" && pg.draft && pg.draft.points.length >= 3) {
        this._polyCloseDraft();
      } else {
        this._polySave(false).then(() => this.close(true));
      }
      return;
    }
    if (e.key === "Backspace") {
      e.preventDefault();
      if (this.state.polygons.tool === "draw") this._polyPopDraftVertex();
      return;
    }
    if (e.key === "Delete") {
      e.preventDefault();
      this._polyDeleteSelectedVertices();
      return;
    }
    if (e.code === "KeyD") { e.preventDefault(); this._setPolyTool("draw"); return; }
    if (e.code === "KeyE") { e.preventDefault(); this._setPolyTool(null); return; }
    if (e.code === "KeyP") { e.preventDefault(); this._setPolyTool("pick"); return; }
    // Day 3c′: S = Seed all (одна модель), Shift+S = Multi-seed (cross-model).
    if (e.code === "KeyS" && !ctrl) {
      e.preventDefault();
      if (e.shiftKey) openMultiSeedModal();
      else this._polySeedFromMask();
      return;
    }
    // 1-9: перемикання активного класу
    const digitMatch = e.code.match(/^Digit([1-9])$/);
    if (digitMatch) {
      e.preventDefault();
      const n = parseInt(digitMatch[1], 10);
      const lbl = appLabels.find((l) => l.shortcut === String(n)) || appLabels[n - 1];
      if (lbl) setActiveLabel(lbl.name);
      return;
    }
  },

  onKeyUp(e) {
    if (e.code === "Space") {
      this.state.spaceDown = false;
      this._wrap().classList.remove("can-pan");
      return;
    }
    if (e.code === "KeyO") {
      if (this.state.bgPreviewPrev !== null) {
        this.setBgSource(this.state.bgPreviewPrev);
        this.state.bgPreviewPrev = null;
        this._wrap().classList.remove("hold-orig");
      }
      return;
    }
    // Hold-I release: гасимо peek «Необведені» (no-op якщо вже вимкнено).
    if (e.code === "KeyI") { this._setPeek(false); }
  },
};
