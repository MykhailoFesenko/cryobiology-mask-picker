/**
 * eventsMixin — підписка/відписка DOM-обробників для модалки редактора:
 * pan/wheel на wrap, mouse на base canvas (cleanup), pointer на SVG (polygons),
 * tabs, cleanup toolbar, polygons toolbar, close buttons.
 *
 * Викликає cleanup+polygons handlers через `this` — polygons-частини у CP5a
 * прикручені до editor через Object.assign у app.js, у CP5b будуть у
 * `./polygons.js` як polygonsMixin.
 */

import { $, showToast } from "../util.js";
import { api } from "../api.js";
import { DEFAULT_LABEL, currentItem } from "../state.js";
import { openMultiSeedModal } from "../multiseed.js";

export const eventsMixin = {
  _bindEvents() {
    this._unbindEvents();
    const wrap = this._wrap();
    const base = this._cBase();
    const svg  = this._svg();

    const btnPeek = $("#groupsPeekUngrouped");
    const h = {
      // Pan + wheel — завжди активні (на wrap).
      wrapMouseDown:  (e) => this._onPanStart(e),
      winMouseMove:   (e) => this._onPanMove(e),
      winMouseUp:     (e) => this._onPanEnd(e),
      wrapWheel:      (e) => this._onWheel(e),
      wrapContextMenu:(e) => e.preventDefault(),
      // Cleanup: hover/click на base canvas.
      baseMouseMove:  (e) => this._onBaseMouseMove(e),
      baseMouseLeave: ()  => this._onBaseMouseLeave(),
      baseClick:      (e) => this._onBaseClick(e),
      // Polygons: pointer events на SVG.
      svgMouseDown:   (e) => this._onSvgMouseDown(e),
      svgMouseMove:   (e) => this._onSvgMouseMove(e),
      svgMouseUp:     (e) => this._onSvgMouseUp(e),
      svgDblClick:    (e) => this._onSvgDblClick(e),
      // Groups: hold-кнопка «Необведені» (закриті у handlers щоб
      // _unbindEvents зняв і window.mouseup, і btn listeners — інакше leak).
      btnPeek,
      peekOn: (e) => { e.preventDefault(); this._setPeek(true); },
      peekOff: () => { this._setPeek(false); },
    };
    this.state.handlers = h;

    wrap.addEventListener("mousedown",   h.wrapMouseDown);
    wrap.addEventListener("wheel",       h.wrapWheel, { passive: false });
    wrap.addEventListener("contextmenu", h.wrapContextMenu);
    window.addEventListener("mousemove", h.winMouseMove);
    window.addEventListener("mouseup",   h.winMouseUp);

    base.addEventListener("mousemove", h.baseMouseMove);
    base.addEventListener("mouseleave", h.baseMouseLeave);
    base.addEventListener("click", h.baseClick);

    svg.addEventListener("mousedown", h.svgMouseDown);
    svg.addEventListener("mousemove", h.svgMouseMove);
    svg.addEventListener("mouseup",   h.svgMouseUp);
    svg.addEventListener("dblclick",  h.svgDblClick);

    // Tabs
    this._modal().querySelectorAll(".tab").forEach((t) => {
      t.onclick = () => this.switchTab(t.dataset.tab);
    });

    // Cleanup toolbar
    $("#cleanupUndo").onclick   = () => this._historyUndo();
    $("#cleanupRedo").onclick   = () => this._historyRedo();
    $("#cleanupReset").onclick  = () => this._cleanupBulkClearRejected();
    $("#cleanupClearMarkers").onclick = () => this._cleanupBulkClearMarkers();
    $("#cleanupSave").onclick   = () => this._cleanupExportSave(false, true);
    $("#cleanupZoomReset").onclick = () => this.resetZoom();
    this._toolbarCleanup().querySelectorAll("[data-bg]").forEach((btn) => {
      btn.onclick = () => this.setBgSource(btn.dataset.bg);
    });
    this._toolbarCleanup().querySelectorAll("[data-cleanup-tool]").forEach((btn) => {
      btn.onclick = () => this._setCleanupTool(btn.dataset.cleanupTool);
    });
    $("#cleanupOpacity").oninput = (e) => {
      this._cMarks().style.opacity = String(e.target.value / 100);
    };
    this._cMarks().style.opacity = String($("#cleanupOpacity").value / 100);
    $("#cleanupShowMask").onchange = (e) => {
      this._cMarks().style.display = (e.target.checked && this.state.activeTab === "cleanup") ? "block" : "none";
    };
    $("#cleanupShowMask").checked = true;

    // Polygons toolbar
    $("#polyUndo").onclick = () => this._historyUndo();
    $("#polyRedo").onclick = () => this._historyRedo();
    $("#polySave").onclick = () => this._polySave(false);
    $("#polySeed").onclick = () => this._polySeedFromMask();
    $("#polyMultiSeed").onclick = () => openMultiSeedModal();
    $("#polyAlign").onclick = () => this._polyAlignToLine();
    $("#polyDeleteShape").onclick = () => this._polyDeleteSelectedShape();
    $("#polyZoomReset").onclick = () => this.resetZoom();
    // Day 3c′: base label set+persist перенесено у labels.js _buildBaseChipDropdown handler.
    this._toolbarPolygons().querySelectorAll("[data-poly-bg]").forEach((btn) => {
      btn.onclick = () => this.setBgSource(btn.dataset.polyBg);
    });
    this._toolbarPolygons().querySelectorAll("[data-poly-tool]").forEach((btn) => {
      btn.onclick = () => this._setPolyTool(btn.dataset.polyTool);
    });
    $("#polyShowMarkers").onchange = (e) => {
      this._layerMarkers().style.display = e.target.checked ? "" : "none";
    };

    // Groups toolbar (Day 4-5)
    const tbGroups = this._toolbarGroups();
    if (tbGroups) {
      const btnNew = $("#groupsNew");
      if (btnNew) btnNew.onclick = () => this._groupsNew();
      const btnDelete = $("#groupsDelete");
      if (btnDelete) btnDelete.onclick = () => this._groupsDeleteActive();
      // Undo/Redo — глобальний стек (раніше НЕ були прив'язані → мертва кнопка;
      // groups покладались лише на Ctrl+Z). enable/disable керує _historyUpdateButtons.
      const btnGrUndo = $("#groupsUndo");
      if (btnGrUndo) btnGrUndo.onclick = () => this._historyUndo();
      const btnGrRedo = $("#groupsRedo");
      if (btnGrRedo) btnGrRedo.onclick = () => this._historyRedo();
      const btnSave = $("#groupsSave");
      if (btnSave) btnSave.onclick = () => this._groupsSave(true);
      const btnZoomReset = $("#groupsZoomReset");
      if (btnZoomReset) btnZoomReset.onclick = () => this.resetZoom();
      // Hold-кнопка «Необведені»: поки затиснута — підсвітити інстанси
      // поза будь-якою групою (механіка як hold-O для оригіналу). Хендлери
      // зберігаємо у `h` щоб _unbindEvents їх зняв — інакше window.mouseup
      // і btn listeners ллялись би при кожному відкритті редактора.
      if (h.btnPeek) {
        h.btnPeek.addEventListener("mousedown",  h.peekOn);
        h.btnPeek.addEventListener("mouseleave", h.peekOff);
        window.addEventListener("mouseup", h.peekOff);
      }
      const classSel = $("#groupsClassSelect");
      if (classSel) classSel.onchange = (e) => this._groupsSetClassIdForActive(e.target.value);
      const btnClassesMgr = $("#groupsClassesManager");
      if (btnClassesMgr) btnClassesMgr.onclick = () => this._groupsOpenClassesManager();
      tbGroups.querySelectorAll("[data-groups-bg]").forEach((btn) => {
        btn.onclick = () => this.setBgSource(btn.dataset.groupsBg);
      });
      tbGroups.querySelectorAll("[data-groups-tool]").forEach((btn) => {
        btn.onclick = () => this._groupsSetTool(btn.dataset.groupsTool);
      });
    }

    // Close (× та backdrop) — робити flushIfDirty.
    // 2026-05-21: escape hatch — якщо flush у процесі (наприклад повільний
    // POST groups з 40+ груп через polygon override на великому фото) і юзер
    // повторно клікає × — робимо force close (skipFlush=true). Без цього
    // модал виглядає замороженим, навіть якщо причина — мережа/повільний бекенд.
    this._modal().querySelectorAll("[data-close]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        if (this._flushPromise) {
          await this.close(true);   // force close — pending save лишається у chain
          return;
        }
        await this.close();
      };
    });
  },

  _unbindEvents() {
    const h = this.state.handlers;
    if (!h) return;
    const wrap = this._wrap();
    const base = this._cBase();
    const svg  = this._svg();
    wrap.removeEventListener("mousedown",   h.wrapMouseDown);
    wrap.removeEventListener("wheel",       h.wrapWheel);
    wrap.removeEventListener("contextmenu", h.wrapContextMenu);
    window.removeEventListener("mousemove", h.winMouseMove);
    window.removeEventListener("mouseup",   h.winMouseUp);
    base.removeEventListener("mousemove", h.baseMouseMove);
    base.removeEventListener("mouseleave", h.baseMouseLeave);
    base.removeEventListener("click", h.baseClick);
    svg.removeEventListener("mousedown", h.svgMouseDown);
    svg.removeEventListener("mousemove", h.svgMouseMove);
    svg.removeEventListener("mouseup",   h.svgMouseUp);
    svg.removeEventListener("dblclick",  h.svgDblClick);
    if (h.btnPeek) {
      h.btnPeek.removeEventListener("mousedown",  h.peekOn);
      h.btnPeek.removeEventListener("mouseleave", h.peekOff);
      h.btnPeek.classList.remove("btn--mini-active");
    }
    window.removeEventListener("mouseup", h.peekOff);
    this.state.handlers = null;
  },
};
