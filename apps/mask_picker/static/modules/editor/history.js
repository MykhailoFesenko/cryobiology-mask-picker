/**
 * historyMixin — ГЛОБАЛЬНИЙ хронологічний undo/redo для всіх трьох доменів
 * редактора (cleanup / polygons / groups). Єдине джерело істини:
 * `this.state.history = { undo: [], redo: [] }`. Замінює три окремі per-tab
 * стеки (v1.16.2 stopgap: cu/pg/gr.undoStack) — task #14.
 *
 * == Модель ==
 * Кожен запис = BEFORE-snapshot одного або кількох доменів:
 *   { snaps: { <domain>: <snap>, ... }, primary: <domain> }
 * Стек строго LIFO (хронологічний), тому undo завжди знімає глобально-останню
 * дію → для її домену поточний стан == AFTER цієї дії (усі пізніші дії цього
 * домену вже відкочені). Тому undo робить classic swap: знімаємо поточний
 * (AFTER) у redo, відновлюємо BEFORE зі знятого запису. Це працює навіть при
 * перемішаних доменах, бо інваріант «поточний==AFTER» тримається per-domain
 * незалежно від чергування.
 *
 * == Складені (крос-доменні) дії ==
 * Видалення polygon-shape ремапить `group.polygon_indices` (Bug 5) — це ОДИН
 * жест, що чіпає ДВА домени. `_polyRemapGroupsAfterShapeDelete` кладе before-
 * snap груп у ТОЙ САМИЙ запис (`_historyAttachSnap`), тож один Ctrl+Z відкочує
 * і полігон, і ремап груп. (Pick більше НЕ складена дія — instance під
 * полігоном rejected derived з геометрії, v1.16.1, не пишеться в rejectedSet.)
 *
 * == Точки входу (увесь ввід — кнопки+клавіші — через global) ==
 *   _historyPush(domain)         — ПЕРЕД мутацією: знімок before, новий запис.
 *   _historyAttachSnap(d, snap)  — додати before-знімок домену до ВЕРХНЬОГО
 *                                  запису (крос-доменний ремап → один undo).
 *   _historyUndo() / _historyRedo() — Ctrl+Z/Y (keys.js) + кнопки (events.js).
 *   _historyClear()              — на open/close (per-stem межа = commit point;
 *                                  bake завжди при закритому редакторі —
 *                                  lazy-bake, Day 7 → close() чистить).
 *   _historyDepth()              — глибина для stats (tabs.js).
 *
 * Тонкі сумісні обгортки лишаються у polygons/groups (`_polyPushUndoSnapshot`,
 * `_groupsPushUndo`) для зовнішніх викликів (multiseed.js, labels.js).
 */

import { $, showToast } from "../util.js";

const HISTORY_CAP = 100;   // ≥10 «все після bake»; знімки дешеві (per-gesture)

// Deep-copy helpers (раніше module-private у polygons.js / groups.js).
export function _snapshotShapes(shapes) {
  return (shapes || []).map((sh) => ({
    label: sh.label,
    points: sh.points.map((p) => [p[0], p[1]]),
    shape_type: sh.shape_type,
    group_id: sh.group_id ?? null,
    flags: { ...(sh.flags || {}) },
  }));
}

export function _snapshotGroups(list) {
  return (list || []).map((g) => ({
    ...g,
    instance_ids: [...(g.instance_ids || [])],
    polygon_indices: [...(g.polygon_indices || [])],
  }));
}

export const historyMixin = {
  // ----- per-domain snapshot (BEFORE/AFTER однакової форми) -----
  _historySnap(domain) {
    const s = this.state;
    if (domain === "cleanup") {
      const cu = s.cleanup;
      return {
        rejected: cu.rejectedSet ? [...cu.rejectedSet] : [],
        markers: (cu.markers || []).map((p) => ({ x: p.x, y: p.y })),
      };
    }
    if (domain === "polygons") {
      return { shapes: _snapshotShapes(s.polygons.shapes) };
    }
    if (domain === "groups") {
      return { list: _snapshotGroups(s.groups.list), activeId: s.groups.activeId };
    }
    return null;
  },

  // ----- per-domain restore (assign live + redraw + mark dirty) -----
  // Знятий запис більше не лежить у жодному стеку, тож пряме присвоєння
  // snap-у як live-стану безпечне (немає аліасингу зі стеком). cleanup робить
  // повністю свіжі копії; polygons/groups присвоюють snap (як старий
  // _polyUndo/_groupsRestore).
  _historyRestore(domain, snap) {
    const s = this.state;
    if (domain === "cleanup") {
      const cu = s.cleanup;
      cu.rejectedSet = new Set(snap.rejected);
      cu.markers = snap.markers.map((p) => ({ x: p.x, y: p.y }));
      this._cleanupMarkDirty();
      this._cleanupRedraw();          // guarded на активний таб
      this._polyRedrawMarkersOnly();
    } else if (domain === "polygons") {
      const pg = s.polygons;
      pg.shapes = snap.shapes;
      pg.selectedShape = -1;
      pg.selectedVertices.clear();
      pg.draft = null;
      pg.freehand = null;
      this._polyMarkDirty();          // + інвалідує coveredCache (derived reject)
      this._polyUpdateButtons();
      this._polyRedraw();
    } else if (domain === "groups") {
      const gr = s.groups;
      gr.list = snap.list;
      // activeId лишаємо лише якщо група ще існує у відновленому списку
      gr.activeId = (snap.list || []).some((x) => x.id === snap.activeId)
        ? snap.activeId : null;
      gr.dirty = true;
      this._groupsRenderClassSelect();
      this._groupsRender();
      this._groupsScheduleAutosave();
    }
  },

  _historyEnsure() {
    if (!this.state.history) this.state.history = { undo: [], redo: [] };
    return this.state.history;
  },

  // Синхронізувати ВСІ 6 undo/redo-кнопок (cleanup/poly/groups × undo/redo) з
  // глобальним стеком — однакова афордація на будь-якому табі. Кнопки лежать у
  // tab-specific тулбарах, але DOM статичний → disabled ставимо завжди.
  _historyUpdateButtons() {
    const h = this.state.history || { undo: [], redo: [] };
    const canUndo = h.undo.length > 0;
    const canRedo = h.redo.length > 0;
    for (const id of ["cleanupUndo", "polyUndo", "groupsUndo"]) {
      const b = $(`#${id}`); if (b) b.disabled = !canUndo;
    }
    for (const id of ["cleanupRedo", "polyRedo", "groupsRedo"]) {
      const b = $(`#${id}`); if (b) b.disabled = !canRedo;
    }
  },

  // Викликати ПЕРЕД мутацією. Запис single-domain; чистить redo.
  _historyPush(domain) {
    const h = this._historyEnsure();
    h.undo.push({ snaps: { [domain]: this._historySnap(domain) }, primary: domain });
    if (h.undo.length > HISTORY_CAP) h.undo.shift();
    h.redo.length = 0;
    this._historyUpdateButtons();
  },

  // Додати ПРЕ-знятий before-snap домену до ВЕРХНЬОГО undo-запису (idempotent).
  // Для крос-доменних ремапів (polygon delete → groups.polygon_indices), щоб
  // складена дія була ОДНИМ undo-записом.
  _historyAttachSnap(domain, snap) {
    const h = this._historyEnsure();
    const top = h.undo[h.undo.length - 1];
    if (!top || (domain in top.snaps)) return;
    top.snaps[domain] = snap;
  },

  _historyUndo() {
    const h = this._historyEnsure();
    const entry = h.undo.pop();
    if (!entry) { showToast("Нема що відмінити", "info", 1000); return; }
    // Поточний стан кожного домену запису == AFTER → у redo.
    const redoSnaps = {};
    for (const domain of Object.keys(entry.snaps)) {
      redoSnaps[domain] = this._historySnap(domain);
    }
    h.redo.push({ snaps: redoSnaps, primary: entry.primary });
    this._historyApply(entry);
  },

  _historyRedo() {
    const h = this._historyEnsure();
    const entry = h.redo.pop();
    if (!entry) return;
    // Поточний стан кожного домену запису == BEFORE → у undo.
    const undoSnaps = {};
    for (const domain of Object.keys(entry.snaps)) {
      undoSnaps[domain] = this._historySnap(domain);
    }
    h.undo.push({ snaps: undoSnaps, primary: entry.primary });
    this._historyApply(entry);
  },

  // Спільне для undo/redo: перемкнути на таб дії (видимість redraw-ів) →
  // відновити всі домени запису. switchTab НЕ пушить історію (лише flush
  // autosave старого табу — безпечно), тож рекурсії немає.
  _historyApply(entry) {
    if (entry.primary && entry.primary !== this.state.activeTab) {
      this.switchTab(entry.primary);
    }
    for (const domain of Object.keys(entry.snaps)) {
      this._historyRestore(domain, entry.snaps[domain]);
    }
    this._updateStats();
    this._updateHint();
    this._historyUpdateButtons();
  },

  _historyClear() {
    const h = this._historyEnsure();
    h.undo.length = 0;
    h.redo.length = 0;
    this._historyUpdateButtons();
  },

  _historyDepth() {
    return this.state.history ? this.state.history.undo.length : 0;
  },
};
