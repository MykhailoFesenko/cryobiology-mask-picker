// Global keyboard shortcuts in catalog mode. Delegates to editor.onKey/onKeyUp
// when editor.state.open. Editor + catalog actions injected.

import { $, $$, closeModal } from "./util.js";
import { currentItem } from "./state.js";
import { showOriginalPreview } from "./holdo.js";
import { openZoom } from "./zoom_modal.js";
import { next, prev, skip, unset, hardReset, selectModel } from "./catalog.js";

let editorRef = null;
export function setEditor(ed) { editorRef = ed; }

export function bindEvents() {
  document.addEventListener("keydown", (e) => {
    if (e.target && ["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
    if (editorRef && editorRef.state.open) { editorRef.onKey(e); return; }

    if (e.key === "ArrowRight") { e.preventDefault(); next(); return; }
    if (e.key === "ArrowLeft") { e.preventDefault(); prev(); return; }
    if (e.key === " " || e.code === "KeyS") { e.preventDefault(); skip(); return; }
    if (e.code === "KeyU" && e.shiftKey) { e.preventDefault(); hardReset(); return; }
    if (e.code === "KeyC") {
      e.preventDefault();
      const it = currentItem();
      const selModel = it && it.state && it.state.status === "selected" ? it.state.model : null;
      if (selModel && editorRef) editorRef.open(selModel, { tab: "cleanup" });
      return;
    }
    if (e.code === "KeyP") {
      e.preventDefault();
      const it = currentItem();
      if (!it) return;
      const selModel = it.state && it.state.status === "selected" ? it.state.model : null;
      if (editorRef) editorRef.open(selModel, { tab: "polygons" });
      return;
    }
    if (e.code === "KeyZ") { e.preventDefault(); openZoom(); return; }
    if (e.code === "KeyO") {
      if (e.repeat) { e.preventDefault(); return; }
      e.preventDefault();
      showOriginalPreview(true);
      return;
    }
    if (e.key === "?" || (e.shiftKey && e.key === "/")) { e.preventDefault(); $("#helpModal").classList.add("open"); return; }
    if (e.key === "Escape") { $$(".modal.open").forEach(closeModal); return; }

    const n = parseInt(e.key, 10);
    if (!isNaN(n) && n >= 1 && n <= 9) {
      const it = currentItem();
      if (!it) return;
      const m = it.available_models[n - 1];
      if (m) { e.preventDefault(); selectModel(m); }
    }
  });

  document.addEventListener("keyup", (e) => {
    if (e.target && ["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
    if (editorRef && editorRef.state.open) { editorRef.onKeyUp(e); return; }
    if (e.code === "KeyO") showOriginalPreview(false);
  });
}
