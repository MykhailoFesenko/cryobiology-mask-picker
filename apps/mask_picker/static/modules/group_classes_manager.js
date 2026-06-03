/**
 * Group Classes Manager (Day 4-5 v2 redesign).
 *
 * Modal-based CRUD для group_classes.json. Парний з Labels Manager.
 * Юзер визначає user-defined класи (name + HSL color + constraints на
 * довільні label-и). Backend auto-creates 3 defaults якщо нема.
 */
import { $, $$, closeModal, showToast } from "./util.js";
import { api } from "./api.js";

let _editor = null;  // injected from app.js (для refresh після save)
let _classes = [];   // current edit-buffer

export function setEditor(editor) { _editor = editor; }

/** Cache доступний усім модулям після loadClasses. */
let _cached = { version: "1.0", classes: [] };
export function getCachedClasses() { return _cached.classes || []; }

// ---------------------------------------------------------------------------
// Backend bridge
// ---------------------------------------------------------------------------

export async function loadClasses() {
  try {
    const env = await api("/api/group-classes");
    _cached = { version: env.version, classes: env.classes || [] };
    return _cached;
  } catch (e) {
    console.warn("group-classes load failed:", e);
    return _cached;
  }
}

async function saveClasses(classes) {
  try {
    const resp = await api("/api/group-classes", {
      method: "POST",
      body: JSON.stringify({ classes }),
    });
    _cached = { version: "1.0", classes: resp.classes || [] };
    return resp;
  } catch (e) {
    showToast(`Save classes: ${e.message}`, "err", 3500);
    throw e;
  }
}

// ---------------------------------------------------------------------------
// Constraints parser/formatter
// ---------------------------------------------------------------------------

/** "nucleus=1, vesicle=2" → {nucleus: 1, vesicle: 2} */
function parseConstraintsString(text) {
  const out = {};
  if (!text || !text.trim()) return out;
  const parts = text.split(",");
  for (const p of parts) {
    const m = /^\s*([^=:\s]+)\s*[=:]\s*(\d+)\s*$/.exec(p);
    if (m) {
      const lbl = m[1].trim();
      const n = parseInt(m[2], 10);
      if (lbl && !Number.isNaN(n)) out[lbl] = n;
    }
  }
  return out;
}

function formatConstraintsDict(d) {
  if (!d || typeof d !== "object") return "";
  return Object.entries(d).map(([k, v]) => `${k}=${v}`).join(", ");
}

// ---------------------------------------------------------------------------
// Modal render
// ---------------------------------------------------------------------------

function _row(cls, idx) {
  const tr = document.createElement("tr");
  tr.dataset.idx = String(idx);

  const hue = Number.isFinite(cls.color_hue) ? cls.color_hue : 0;
  const sat = Number.isFinite(cls.color_sat) ? cls.color_sat : 50;
  const light = Number.isFinite(cls.color_light) ? cls.color_light : 45;

  // Color cell: swatch + hue slider (sat/light fixed-ish — но editable у tooltip)
  const tdColor = document.createElement("td");
  tdColor.className = "gc-color-cell";
  const swatch = document.createElement("div");
  swatch.className = "gc-color-swatch";
  swatch.style.background = `hsl(${hue}, ${sat}%, ${light}%)`;
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0"; slider.max = "359"; slider.value = String(hue);
  slider.className = "gc-hue-slider";
  slider.title = `Hue: ${hue}°  Sat: ${sat}%  Light: ${light}%`;
  slider.oninput = () => {
    cls.color_hue = parseInt(slider.value, 10);
    swatch.style.background = `hsl(${cls.color_hue}, ${sat}%, ${light}%)`;
    slider.title = `Hue: ${cls.color_hue}°  Sat: ${sat}%  Light: ${light}%`;
  };
  tdColor.appendChild(swatch);
  tdColor.appendChild(slider);

  // Name
  const tdName = document.createElement("td");
  const nameIn = document.createElement("input");
  nameIn.type = "text";
  nameIn.value = cls.name || "";
  nameIn.placeholder = "class name";
  nameIn.oninput = () => { cls.name = nameIn.value.trim(); };
  tdName.appendChild(nameIn);

  // Min constraints
  const tdMin = document.createElement("td");
  const minIn = document.createElement("input");
  minIn.type = "text";
  const cn = cls.constraints || {};
  minIn.value = formatConstraintsDict(cn.min);
  minIn.placeholder = "nucleus=1, vesicle=1";
  minIn.oninput = () => {
    cls.constraints = cls.constraints || {};
    cls.constraints.min = parseConstraintsString(minIn.value);
  };
  tdMin.appendChild(minIn);

  // Max constraints
  const tdMax = document.createElement("td");
  const maxIn = document.createElement("input");
  maxIn.type = "text";
  maxIn.value = formatConstraintsDict(cn.max);
  maxIn.placeholder = "nucleus=0";
  maxIn.oninput = () => {
    cls.constraints = cls.constraints || {};
    cls.constraints.max = parseConstraintsString(maxIn.value);
  };
  tdMax.appendChild(maxIn);

  // Delete
  const tdDel = document.createElement("td");
  const delBtn = document.createElement("button");
  delBtn.className = "btn btn--ghost btn--mini";
  delBtn.textContent = "🗑";
  delBtn.title = "Видалити клас";
  delBtn.onclick = () => {
    _classes.splice(idx, 1);
    _renderTable();
  };
  tdDel.appendChild(delBtn);

  tr.append(tdColor, tdName, tdMin, tdMax, tdDel);
  return tr;
}

function _renderTable() {
  const tbody = $("#groupClassesList");
  if (!tbody) return;
  tbody.innerHTML = "";
  _classes.forEach((c, i) => tbody.appendChild(_row(c, i)));
}

function _nextClsId() {
  const used = new Set();
  for (const c of _classes) {
    const m = /^cls_(\d+)$/.exec(c.id || "");
    if (m) used.add(parseInt(m[1], 10));
  }
  let n = 1;
  while (used.has(n)) n++;
  return `cls_${String(n).padStart(3, "0")}`;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function openGroupClassesModal() {
  await loadClasses();
  _classes = JSON.parse(JSON.stringify(_cached.classes));
  _renderTable();
  $("#groupClassesModal").classList.add("open");
}

export function bindEvents() {
  $("#btnAddGroupClass").onclick = () => {
    _classes.push({
      id: _nextClsId(),
      name: "",
      color_hue: 200,
      color_sat: 50,
      color_light: 45,
      constraints: { min: {}, max: {} },
    });
    _renderTable();
  };
  $("#btnSaveGroupClasses").onclick = async () => {
    // Drop empty-name rows
    const valid = _classes.filter((c) => c.name && c.name.trim());
    try {
      await saveClasses(valid);
      showToast(`✓ Saved ${valid.length} класів`, "ok", 1800);
      closeModal($("#groupClassesModal"));
      // Refresh editor якщо відкрите
      if (_editor && _editor.state && _editor.state.open
          && typeof _editor._groupsLoad === "function") {
        await _editor._groupsLoad();
      }
    } catch (e) {
      // toast вже показано
    }
  };
}
