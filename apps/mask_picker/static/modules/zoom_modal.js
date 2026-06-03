// Zoom modal (fullscreen variant compare) + app quit. quitApp lives here because
// it shares the "kill the page" UI pattern with closing the zoom modal.

import { $ } from "./util.js";
import { currentItem } from "./state.js";
import { refreshPolygonOverlaysForZoom } from "./polyoverlay.js";

export function openZoom(forKey = null) {
  const it = currentItem();
  if (!it) return;
  const all = [{ key: "original", name: "Оригінал", src: `/api/image/${encodeURIComponent(it.stem)}` }];
  it.available_models.forEach((m) => {
    all.push({ key: m, name: m, src: `/api/overlay/${encodeURIComponent(m)}/${encodeURIComponent(it.stem)}` });
  });
  const list = forKey ? all.filter((v) => v.key === forKey) : all;
  $("#zoomTitle").textContent = `${it.stem} — ${forKey ? forKey : "усі варіанти"}`;
  // UX #3 fix (v1.16.0): figure отримує клас `zoom-figure` — без нього
  // CSS-правило `.zoom-figure.variant--preview-orig .zoom-figure__polyoverlay
  // { display:none }` ніколи не матчилось → при hold-O у zoom-modal polygon
  // overlay лишався поверх оригіналу.
  $("#zoomImages").innerHTML = list.map((v) => `
    <figure class="zoom-figure">
      <div class="zoom-figure__img-wrap">
        <img src="${v.src}" alt="${v.name}" />
      </div>
      <figcaption>${v.name}</figcaption>
    </figure>`).join("");
  $("#zoomModal").classList.add("open");
  refreshPolygonOverlaysForZoom().catch(() => {});
}

export async function quitApp() {
  if (!confirm("Закрити Mask Picker? Сервер буде зупинено.")) return;
  try {
    await fetch("/api/shutdown", { method: "POST" });
  } catch (_) { /* connection drop очікувано — сервер вже падає */ }
  document.body.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;'
    + 'min-height:100vh;font-family:system-ui;font-size:18px;color:#666;">'
    + 'Mask Picker зупинено. Можна закрити вкладку.</div>';
  setTimeout(() => { try { window.close(); } catch (_) {} }, 300);
}
