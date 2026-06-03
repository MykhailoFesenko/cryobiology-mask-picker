// Hold-O preview: swap variant tiles' img.src to the original. Works in both
// catalog grid and zoom modal. Editor-mode hold-O is a separate CSS layer in
// editor/keys.js (sets .hold-orig on cleanupCanvasWrap).

import { $, $$ } from "./util.js";
import { currentItem } from "./state.js";

export function showOriginalPreview(show) {
  const it = currentItem();
  if (!it) return;
  const origSrc = `/api/image/${encodeURIComponent(it.stem)}`;
  const targets = [];
  $$("#variants .variant").forEach((v) => {
    if (v.classList.contains("variant--original")) return;
    const img = v.querySelector("img");
    if (img) targets.push({ el: v, img });
  });
  if ($("#zoomModal").classList.contains("open")) {
    $$("#zoomImages figure").forEach((fig) => {
      const cap = fig.querySelector("figcaption");
      if (cap && cap.textContent.trim() === "Оригінал") return;
      const img = fig.querySelector("img");
      if (img) targets.push({ el: fig, img });
    });
  }
  targets.forEach(({ el, img }) => {
    if (show) {
      if (!img.dataset.origBackupSrc) img.dataset.origBackupSrc = img.src;
      if (img.src !== origSrc) img.src = origSrc;
      el.classList.add("variant--preview-orig");
    } else {
      if (img.dataset.origBackupSrc) {
        img.src = img.dataset.origBackupSrc;
        delete img.dataset.origBackupSrc;
      }
      el.classList.remove("variant--preview-orig");
    }
  });
}
