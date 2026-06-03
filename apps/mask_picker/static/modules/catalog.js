// Catalog: sidebar list, workspace tiles, progress bar, navigation actions.
// Editor reference is injected via setEditor() to break the catalog↔editor
// circular import (editor.close() calls renderAll, catalog actions call
// editor.flushIfDirty before nav).

import { $, $$, fmtTime, showToast } from "./util.js";
import { api } from "./api.js";
import { state, currentItem, filtered } from "./state.js";
import { refreshPolygonOverlaysForCurrent } from "./polyoverlay.js";

let editorRef = null;
export function setEditor(ed) { editorRef = ed; }

async function flushEditorIfOpen() {
  if (editorRef && editorRef.flushIfDirty) await editorRef.flushIfDirty();
}

export async function reloadCatalog(fresh = false) {
  const data = await api(`/api/catalog${fresh ? "?fresh=1" : ""}`);
  state.catalog = data.items;
  if (state.idx >= filtered().length) state.idx = Math.max(0, filtered().length - 1);
}

// Persist last-viewed stem across page reloads. Saved on every renderAll (after any nav).
const _STEM_KEY = "mp.current.stem";
function _saveCurrentStem() {
  try {
    const it = currentItem();
    if (it && it.stem) localStorage.setItem(_STEM_KEY, it.stem);
  } catch (_) {}
}
export function restoreLastStem() {
  try {
    const stem = localStorage.getItem(_STEM_KEY);
    if (!stem) return false;
    const list = filtered();
    const i = list.findIndex((it) => it.stem === stem);
    if (i >= 0) { state.idx = i; return true; }
  } catch (_) {}
  return false;
}

export function renderAll() {
  _saveCurrentStem();
  renderSidebar();
  renderWorkspace();
  renderProgress();
  refreshPolygonOverlaysForCurrent().catch(() => {});
}

export function renderSidebar() {
  const list = filtered();
  const cur = currentItem();
  const html = list.map((it, i) => {
    const st = it.state && it.state.status;
    const badgeClass =
      st === "selected" ? "image-list__badge--selected"
      : st === "skipped" ? "image-list__badge--skipped"
      : "";
    const model = it.state && it.state.model ? it.state.model : "";
    const isCurrent = cur && it.stem === cur.stem;
    // Day 7: жовта крапка = є незапечені зміни (dirty).
    const isDirty = !!(it.state && it.state.dirty);
    const dirtyDot = isDirty
      ? `<span class="image-list__dirty" title="Незапечені зміни — натисни «Зберегти все»">●</span>`
      : "";
    // Day 8.5: для excluded-фото — кнопка «повернути з смітника».
    // Вибір для експорту переїхав у Export-modal (Day 8.5 rework).
    const leftCtrl = st === "excluded"
      ? `<button class="image-list__restore" data-restore-stem="${it.stem}"
                 title="Повернути фото з смітника">♻</button>`
      : "";
    return `
      <div class="image-list__item ${isCurrent ? "image-list__item--current" : ""}" data-idx="${i}">
        ${leftCtrl}
        <span class="image-list__badge ${badgeClass}"></span>
        <span class="image-list__name" title="${it.stem}">${it.stem}</span>
        ${dirtyDot}
        ${model ? `<span class="image-list__model">${model}</span>` : ""}
      </div>`;
  }).join("");
  $("#imageList").innerHTML = html || `<div class="muted" style="padding: 14px;">Нічого не знайдено у цьому фільтрі.</div>`;

  const el = $(".image-list__item--current");
  if (el) el.scrollIntoView({ block: "nearest" });
}

export function renderWorkspace() {
  const it = currentItem();
  if (!it) {
    $("#currentName").textContent = "—";
    $("#variants").innerHTML = `<div class="hint">Немає фото для перегляду.</div>`;
    $("#stateHint").textContent = "";
    return;
  }
  $("#currentName").textContent = it.stem;

  const variants = [];
  variants.push({
    key: "original",
    keyLabel: "orig",
    name: "Оригінал",
    src: `/api/image/${encodeURIComponent(it.stem)}`,
    selectable: false,
    isOriginal: true,
  });
  it.available_models.forEach((m, idx) => {
    variants.push({
      key: m,
      keyLabel: String(idx + 1),
      name: m,
      src: `/api/overlay/${encodeURIComponent(m)}/${encodeURIComponent(it.stem)}`,
      selectable: true,
    });
  });

  const selectedModel = it.state && it.state.status === "selected" ? it.state.model : null;
  const isDirty = !!(it.state && it.state.dirty);

  $("#variants").innerHTML = variants.map((v) => {
    const isSel = selectedModel === v.key;
    const cls = ["variant"];
    if (v.isOriginal) cls.push("variant--original");
    if (isSel) cls.push("variant--selected");
    let editBtns = "";
    if (isSel && v.selectable) {
      editBtns = `
        <button class="variant__cleanup" data-edit-tab="cleanup" data-edit-model="${v.key}" title="Cleanup інстансів (C)">🧹</button>
        <button class="variant__cleanup" data-edit-tab="polygons" data-edit-model="${v.key}" title="Polygons (P)">✏️</button>
        <button class="variant__cleanup" data-edit-tab="groups" data-edit-model="${v.key}" title="Cell grouping (G)">🔗</button>`;
    } else if (v.isOriginal) {
      editBtns = `
        <button class="variant__cleanup" data-edit-tab="polygons" data-edit-model="" title="Polygons (P)">✏️</button>
        <button class="variant__cleanup" data-edit-tab="groups" data-edit-model="" title="Cell grouping (G)">🔗</button>`;
    }
    const pickBtn = v.selectable
      ? `<button class="variant__pick ${isSel ? "variant__pick--on" : ""}" data-pick="${v.key}" title="Обрати маску">${isSel ? "☑" : "☐"}</button>`
      : "";
    return `
      <div class="${cls.join(" ")}" data-key="${v.key}" data-selectable="${v.selectable ? 1 : 0}">
        <div class="variant__header">
          <span class="variant__key">${v.keyLabel}</span>
          <span class="variant__name">${v.name}</span>
          ${editBtns}
          ${pickBtn}
          <button class="variant__zoom" data-zoom="${v.key}" title="Збільшити">⤢</button>
        </div>
        <div class="variant__image-wrap">
          <img loading="lazy" src="${v.src}" alt="${v.name}" draggable="false" />
          ${isSel ? `<div class="variant__badge-selected ${isDirty ? "variant__badge-selected--dirty" : ""}">${isDirty ? "🟡 не запечено" : "✓ обрано"}</div>` : ""}
        </div>
      </div>`;
  }).join("");

  const st = it.state;
  const hintEl = $("#stateHint");
  hintEl.className = "hint";
  if (!st) {
    hintEl.textContent = `Клікай на варіант маски (або тисни 1–${it.available_models.length}) щоб його зафіксувати. «S» — пропустити.`;
  } else if (st.status === "selected") {
    hintEl.classList.add("hint--selected");
    hintEl.innerHTML = `✓ Обрано <b>${st.model}</b>. ${st.user ? `(${st.user})` : ""} <span class="muted">${fmtTime(st.ts)}</span>. Натисни <kbd>U</kbd> щоб скинути.`;
  } else if (st.status === "skipped") {
    hintEl.classList.add("hint--skipped");
    hintEl.innerHTML = `⊘ Пропущено. ${st.reason ? `Причина: «${st.reason}»` : ""} <span class="muted">${fmtTime(st.ts)}</span>. Натисни <kbd>U</kbd> щоб скинути.`;
  }
}

export function renderProgress() {
  let total = 0, reviewed = 0, selected = 0, skipped = 0;
  state.catalog.forEach((it) => {
    const s = it.state && it.state.status;
    if (s === "excluded") return;
    total++;
    if (s === "selected") { reviewed++; selected++; }
    else if (s === "skipped") { reviewed++; skipped++; }
  });
  const pct = total ? Math.round(reviewed * 100 / total) : 0;
  $("#progressFill").style.width = `${pct}%`;
  $("#progressLabel").textContent = `${reviewed}/${total}  •  ✓${selected}  ⊘${skipped}  (${pct}%)`;
}

export async function selectModel(modelName) {
  const it = currentItem();
  if (!it) return;
  if (!it.available_models.includes(modelName)) return;
  // Quick win #2 (Day 9): повторний клік по вже-обраній моделі знімає вибір
  // (чекбокс ☑ → ☐ toggle). Раніше зняти можна було лише кнопкою «↺ Скинути».
  if (it.state && it.state.status === "selected" && it.state.model === modelName) {
    await unset();
    return;
  }
  await flushEditorIfOpen();

  // Day 7 lazy-bake: Pick більше НЕ запікає одразу (auto-rebake прибрано —
  // на фото з 2000+ клітинами це блокувало UI на десятки секунд). selected/
  // тепер отримує raw output моделі. Якщо для stem уже були правки
  // (polygons / cleanup rejected) — backend позначає stem dirty, юзер запече
  // все разом через «💾 Зберегти все».
  try {
    const resp = await api("/api/select", {
      method: "POST",
      body: JSON.stringify({ stem: it.stem, model: modelName, user: state.user }),
    });
    it.state = {
      status: "selected",
      model: modelName,
      user: state.user,
      ts: new Date().toISOString(),
      dirty: !!resp.dirty,
    };
    renderAll();
    if (resp.dirty) {
      showToast(`✓ Pick ${modelName} — є незапечені зміни, натисни «Зберегти все»`,
                "warn", 3500);
    } else {
      showToast(`✓ Pick ${modelName}`, "ok", 1500);
    }
  } catch (e) {
    alert(`Не зміг зафіксувати вибір: ${e.message}`);
  }
}

// Day 8.5: повернути помилково виключене фото з _excluded/ назад.
export async function restorePhoto(stem) {
  if (!stem) return;
  try {
    await api(`/api/restore/${encodeURIComponent(stem)}`, { method: "POST" });
    await reloadCatalog(true);
    renderAll();
    showToast(`♻ ${stem} повернено з смітника`, "ok", 2500);
  } catch (e) {
    alert(`Не зміг повернути ${stem}: ${e.message}`);
  }
}

export async function excludePhoto() {
  const it = currentItem();
  if (!it) return;
  if (!confirm(`Прибрати ${it.stem} з датасету? Файл переноситься у _excluded/, можна повернути кнопкою ♻ у фільтрі 🗑.`)) return;
  await flushEditorIfOpen();
  try {
    await api(`/api/exclude/${encodeURIComponent(it.stem)}`, {
      method: "POST",
      body: JSON.stringify({ user: state.user }),
    });
    await reloadCatalog(true);
    renderAll();
  } catch (e) {
    alert(`Не зміг прибрати: ${e.message}`);
  }
}

export async function skip(reason = "") {
  const it = currentItem();
  if (!it) return;
  await flushEditorIfOpen();
  try {
    await api("/api/skip", {
      method: "POST",
      body: JSON.stringify({ stem: it.stem, reason, user: state.user }),
    });
    it.state = { status: "skipped", model: null, reason, user: state.user, ts: new Date().toISOString() };
    next();
  } catch (e) {
    alert(`Не зміг пропустити: ${e.message}`);
  }
}

export async function unset() {
  const it = currentItem();
  if (!it) return;
  await flushEditorIfOpen();
  try {
    await api("/api/unset", {
      method: "POST",
      body: JSON.stringify({ stem: it.stem }),
    });
    it.state = null;
    renderAll();
  } catch (e) {
    alert(`Не зміг скинути: ${e.message}`);
  }
}

// Day 3c′ CP6: повний reset — state + polygons + selected/. Backup у _backups/.
export async function hardReset() {
  const it = currentItem();
  if (!it) return;
  if (!confirm(`Видалити ВСІ ручні правки для ${it.stem}?\n\nБуде видалено:\n• state (вибір моделі, cleanup rejected, base_label)\n• polygons/${it.stem}.json (manual polygons)\n• selected/<model>/${it.stem}.{npy,png,yolo,overlay}\n\nБекап у _backups/. Оригінали images/ та output/ — НЕ видаляються.`)) {
    return;
  }
  await flushEditorIfOpen();
  try {
    const resp = await api(`/api/hard-reset/${encodeURIComponent(it.stem)}`, {
      method: "POST",
    });
    if (!resp.ok) {
      alert(`Hard reset не вдався: ${resp.error || "?"}`);
      return;
    }
    it.state = null;
    await reloadCatalog();
    renderAll();
  } catch (e) {
    alert(`Hard reset помилка: ${e.message}`);
  }
}

export async function next() { await flushEditorIfOpen(); state.idx += 1; if (state.idx >= filtered().length) state.idx = filtered().length - 1; renderAll(); }
export async function prev() { await flushEditorIfOpen(); state.idx = Math.max(0, state.idx - 1); renderAll(); }
export async function goto(i) { await flushEditorIfOpen(); state.idx = i; renderAll(); }
