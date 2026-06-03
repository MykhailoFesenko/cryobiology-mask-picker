// Multi-class seed: для кожного класу — своя модель-джерело. Backend об'єднує
// shapes у єдиний LabelMe envelope (overlap > 60% IoU = skip).
// Editor reference потрібен бо applyMultiSeed пише в editor.state.polygons.shapes
// + викликає editor._polyMarkDirty/Redraw.

import { $, showToast } from "./util.js";
import { api } from "./api.js";
import { state, appLabels, DEFAULT_LABEL } from "./state.js";

let editorRef = null;
export function setEditor(ed) { editorRef = ed; }

// Sentinel value у dropdown для "не seed-ити з моделі — лишити порожнім".
// Юзер обирає для класу, який хоче ВРУЧНУ намалювати поверх оригіналу.
export const MULTISEED_ORIGINAL = "__original__";

function _multiseedLoadPrefs() {
  try { return JSON.parse(localStorage.getItem("mask_picker_multiseed") || "{}"); }
  catch (_) { return {}; }
}
function _multiseedSavePrefs(map) {
  try { localStorage.setItem("mask_picker_multiseed", JSON.stringify(map)); }
  catch (_) {}
}

export function openMultiSeedModal() {
  if (!editorRef || !editorRef.state.open) {
    showToast("Спочатку відкрий editor (полігон-таб)", "err", 2000);
    return;
  }
  const tbody = $("#multiSeedList");
  if (!tbody) return;
  const models = (state.config && state.config.models) || [];
  if (!models.length) {
    showToast("Немає доступних моделей у workspace", "err", 2000);
    return;
  }
  const prefs = _multiseedLoadPrefs();
  // Day 3c′: defaults — усі dropdowns на поточну обрану модель, усі checkboxes OFF.
  // Юзер свідомо вмикає лише ті класи, які хоче seed-ити. Випадковий Apply
  // нічого не змінює. localStorage prefs зберігає лише останній model вибір
  // per-label, не checked-state (так само як перед v2.0.0).
  const activeModel = editorRef.state.model || "";
  tbody.innerHTML = "";
  appLabels.forEach((lbl) => {
    const tr = document.createElement("tr");
    let pref = prefs[lbl.name];
    if (pref == null || pref === "") {
      pref = activeModel || MULTISEED_ORIGINAL;
    }
    const opts = [
      `<option value="${MULTISEED_ORIGINAL}"${pref === MULTISEED_ORIGINAL ? " selected" : ""}>(оригінал — не seed-ити, малюватиму руками)</option>`,
    ].concat(
      models.map((m) => `<option value="${m}"${pref === m ? " selected" : ""}>${m}</option>`)
    );
    const sel = `<select data-label="${lbl.name}" class="label-select" style="min-width:160px">${opts.join("")}</select>`;
    tr.innerHTML = `
      <td><span style="display:inline-block;width:14px;height:14px;border-radius:3px;background:${lbl.color || "#888"};margin-right:6px;vertical-align:middle"></span>${lbl.name}</td>
      <td>${sel}</td>
      <td><input type="checkbox" data-enable="${lbl.name}"></td>
    `;
    tbody.appendChild(tr);
  });
  // Day 3c′: live counter оновлюється на toggle checkbox / change select.
  tbody.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", _updateMultiSeedCounter);
  });
  tbody.querySelectorAll("select").forEach((sel) => {
    sel.addEventListener("change", _updateMultiSeedCounter);
  });
  _updateMultiSeedCounter();
  $("#multiSeedReplace").checked = false;
  $("#multiSeedModal").classList.add("open");
}

// Day 3c′: counter "N клас(и) → M модел(і)" поряд з Apply.
// Викликається при open + при кожному toggle checkbox / change select.
function _updateMultiSeedCounter() {
  const counter = $("#multiSeedCounter");
  const tbody = $("#multiSeedList");
  if (!counter || !tbody) return;
  let enabled = 0;
  const usedModels = new Set();
  tbody.querySelectorAll("tr").forEach((tr) => {
    const cb = tr.querySelector("input[type=checkbox]");
    if (!cb || !cb.checked) return;
    const sel = tr.querySelector("select");
    if (!sel || !sel.value || sel.value === MULTISEED_ORIGINAL) return;
    enabled++;
    usedModels.add(sel.value);
  });
  if (enabled === 0) {
    counter.textContent = "0 активних";
    counter.classList.add("muted");
  } else {
    counter.textContent = `${enabled} клас(и) → ${usedModels.size} модел(і)`;
    counter.classList.remove("muted");
  }
}

export async function applyMultiSeed() {
  const tbody = $("#multiSeedList");
  if (!tbody) return;
  const mappings = [];
  const skippedOriginal = [];
  const prefs = {};
  tbody.querySelectorAll("tr").forEach((tr) => {
    const enableEl = tr.querySelector("input[data-enable]");
    const selEl = tr.querySelector("select[data-label]");
    if (!enableEl || !selEl) return;
    const label = enableEl.dataset.enable;
    const model = selEl.value;
    if (model) prefs[label] = model;
    if (!enableEl.checked) return;
    if (model === MULTISEED_ORIGINAL || !model) {
      skippedOriginal.push(label);
      return;
    }
    mappings.push({ label, model });
  });
  _multiseedSavePrefs(prefs);
  if (!mappings.length && !skippedOriginal.length) {
    showToast("Жоден клас не активований", "err", 1800);
    return;
  }
  if (!mappings.length) {
    $("#multiSeedModal").classList.remove("open");
    showToast(`OK, малюй вручну для: ${skippedOriginal.join(", ")}`, "ok", 2400);
    return;
  }
  const replace = $("#multiSeedReplace").checked;
  if (!editorRef) return;
  const s = editorRef.state;
  if (!s.open || !s.stem) {
    showToast("Editor закритий", "err", 1500);
    return;
  }
  if (replace && s.polygons.shapes.length
      && !confirm(`Замінити поточні ${s.polygons.shapes.length} полігонів?`)) {
    return;
  }
  try {
    const resp = await api(`/api/polygons/${encodeURIComponent(s.stem)}/multi-seed`, {
      method: "POST",
      body: JSON.stringify({ mappings, iou_threshold: 0.6 }),
    });
    if (!resp.ok) {
      showToast(`Multi-seed не вдався: ${resp.error || "?"}`, "err", 3000);
      return;
    }
    editorRef._polyPushUndoSnapshot();
    const newShapes = (resp.envelope.shapes || []).map((sh) => ({
      label: sh.label || DEFAULT_LABEL,
      points: sh.points.map((p) => [+p[0], +p[1]]),
      shape_type: sh.shape_type || "polygon",
      group_id: sh.group_id ?? null,
      flags: sh.flags || {},
    }));
    if (replace) s.polygons.shapes = newShapes;
    else s.polygons.shapes.push(...newShapes);
    s.polygons.selectedShape = -1;
    s.polygons.selectedVertices.clear();
    editorRef._polyMarkDirty();   // інвалідує coveredCache → derived rejection
    // Bug A/B fix (v1.16.1) — DERIVED rejection (костиль explicit-add
    // прибрано). Multi-seed додає полігони; instance під ними стають
    // covered (_polyCoveredInstances, >50%) → автоматично червоні +
    // невибірні. Видалення полігона коректно повертає instance (бо стан
    // derived, не у rejectedSet). _polyMarkDirty вже інвалідував кеш.
    editorRef._cleanupRedraw();
    editorRef._polyRedraw();
    $("#multiSeedModal").classList.remove("open");
    const skipMsg = resp.shapes_skipped_overlap > 0
      ? ` (skipped ${resp.shapes_skipped_overlap} через overlap >60%)`
      : "";
    const origMsg = skippedOriginal.length
      ? `; ${skippedOriginal.join(", ")} — малюй руками`
      : "";
    showToast(`Multi-seed: +${resp.shapes_added} полігонів${skipMsg}${origMsg}`, "ok", 3000);
  } catch (e) {
    showToast(`Multi-seed помилка: ${e.message}`, "err", 4000);
    console.error(e);
  }
}

export function bindEvents() {
  const btnMSApply = $("#btnMultiSeedApply");
  if (btnMSApply) btnMSApply.onclick = applyMultiSeed;
}
