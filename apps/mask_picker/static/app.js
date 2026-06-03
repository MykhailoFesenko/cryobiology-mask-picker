/**
 * Mask Picker — frontend entry.
 *
 * Тонкий wrapper над ES-modules:
 *   ./modules/{util,api,state,catalog,polyoverlay,holdo,zoom_modal,stats,
 *              labels,multiseed,workspace_bar,keyboard}.js
 *   ./modules/editor/{index,tabs,zoompan,events,cleanup,polygons,keys}.js
 *
 * Публічне API модуля `editor` (споживачі: catalog, keyboard, workspace_bar,
 * multiseed, labels):
 *   editor.open(modelName, { tab?: "cleanup" | "polygons" })
 *   editor.close(skipFlush=false)
 *   editor.flushIfDirty()              — викликається перед навігацією Pick/Skip/Unset/next/prev
 *   editor.state.open
 *   editor.onKey(e), editor.onKeyUp(e) — обробники клавіш поки модалка відкрита
 */

import { $, $$, closeModal } from "./modules/util.js";
import { api } from "./modules/api.js";
import { state, appLabels, setAppLabels } from "./modules/state.js";
import { openZoom, quitApp } from "./modules/zoom_modal.js";
import { openStats } from "./modules/stats.js";
import {
  renderAll, reloadCatalog, restoreLastStem,
  selectModel, excludePhoto, skip, unset, hardReset, next, prev, goto,
  restorePhoto,
  setEditor as setCatalogEditor,
} from "./modules/catalog.js";
import {
  setEditor as setLabelsEditor,
  bindEvents as bindLabelsEvents,
  setActiveLabel,
  resetModalLabels,
} from "./modules/labels.js";
import {
  setEditor as setMultiseedEditor,
  bindEvents as bindMultiseedEvents,
} from "./modules/multiseed.js";
import {
  setEditor as setGroupClassesEditor,
  bindEvents as bindGroupClassesEvents,
  loadClasses as loadGroupClasses,
} from "./modules/group_classes_manager.js";
import {
  setEditor as setWorkspaceEditor,
  bindEvents as bindWorkspaceEvents,
  setCatalogCallbacks as setWorkspaceCatalogCallbacks,
} from "./modules/workspace_bar.js";
import {
  setEditor as setKeyboardEditor,
  bindEvents as bindKeyboardEvents,
} from "./modules/keyboard.js";
import { editor } from "./modules/editor/index.js";

setCatalogEditor(editor);
setLabelsEditor(editor);
setMultiseedEditor(editor);
setWorkspaceEditor(editor);
setKeyboardEditor(editor);
setGroupClassesEditor(editor);
setWorkspaceCatalogCallbacks(reloadCatalog, renderAll);

async function init() {
  state.config = await api("/api/config");
  state.models = state.config.models;
  $("#projectBadge").textContent = `• ${state.models.length} model(s) • ${state.config.output_root.split(/[\\/]/).slice(-2).join("/")}`;

  if (state.config.workspace_mode) {
    const bar = $("#workspaceBar");
    bar.style.display = "flex";
    const ws = state.config.workspace_dir || "";
    const shortWs = ws.split(/[\\/]/).slice(-2).join("/");
    $("#workspacePath").textContent = shortWs;
    $("#workspacePath").title = ws;
  }

  try {
    state.user = localStorage.getItem("mask_picker_user") || "";
  } catch (_) {}
  $("#userInput").value = state.user;
  $("#userInput").addEventListener("change", (e) => {
    state.user = e.target.value.trim();
    try { localStorage.setItem("mask_picker_user", state.user); } catch (_) {}
  });

  setAppLabels((state.config.labels && state.config.labels.length)
    ? state.config.labels
    : [{ id: 1, name: "nucleus", color: "#4488ff", shortcut: "1" }]);
  setActiveLabel(appLabels[0].name);

  await reloadCatalog();
  restoreLastStem();
  renderAll();
  bindEvents();
}

function bindEvents() {
  $("#btnPrev").onclick = prev;
  $("#btnNext").onclick = next;
  $("#btnSkip").onclick = () => skip();
  const btnHardReset = $("#btnHardReset");
  if (btnHardReset) btnHardReset.onclick = hardReset;
  const btnExclude = $("#btnExclude");
  if (btnExclude) btnExclude.onclick = excludePhoto;
  $("#btnStats").onclick = openStats;
  $("#btnHelp").onclick = () => $("#helpModal").classList.add("open");
  const btnQuit = $("#btnQuit");
  if (btnQuit) btnQuit.onclick = quitApp;

  $("#imageList").addEventListener("click", (e) => {
    // Day 8.5: клік по ♻ — повернути фото з смітника, не навігація.
    const rb = e.target.closest(".image-list__restore");
    if (rb) { restorePhoto(rb.dataset.restoreStem); return; }
    const it = e.target.closest(".image-list__item");
    if (it) goto(parseInt(it.dataset.idx, 10));
  });

  $$(".chip").forEach((c) => {
    c.addEventListener("click", () => {
      $$(".chip").forEach((cc) => cc.classList.remove("chip--active"));
      c.classList.add("chip--active");
      state.filter = c.dataset.filter;
      state.idx = 0;
      renderAll();
    });
  });

  $("#variants").addEventListener("click", (e) => {
    const editBtn = e.target.closest("[data-edit-tab]");
    if (editBtn) {
      e.stopPropagation();
      const tab = editBtn.dataset.editTab;
      const model = editBtn.dataset.editModel || null;
      editor.open(model, { tab });
      return;
    }
    const pickBtn = e.target.closest("[data-pick]");
    if (pickBtn) { e.stopPropagation(); selectModel(pickBtn.dataset.pick); return; }
    const zoomBtn = e.target.closest("[data-zoom]");
    if (zoomBtn) { e.stopPropagation(); openZoom(zoomBtn.dataset.zoom); return; }
    const v = e.target.closest(".variant");
    if (!v) return;
    openZoom(v.dataset.key);
  });

  $$(".modal").forEach((m) => {
    m.addEventListener("click", (e) => {
      if (e.target.dataset.close !== undefined) {
        closeModal(m);
        if (m.id === "labelsModal") resetModalLabels();
      }
    });
  });

  bindLabelsEvents();
  bindMultiseedEvents();
  bindWorkspaceEvents();
  bindKeyboardEvents();
  bindGroupClassesEvents();
  // Preload classes для frontend cache (без блокування init)
  loadGroupClasses().catch(() => {});
}

init().catch((e) => {
  document.body.innerHTML = `<pre style="padding: 20px; color: #ff6b6b;">Init error: ${e.message}\n\nПеревір що сервер запущений і config.yaml (або авто-дискавері) знаходить моделі у output/.</pre>`;
});
