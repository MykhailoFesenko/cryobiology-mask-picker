// Labels: active label picker (Polygons toolbar) + Labels Manager modal.
// Editor reference is injected via setEditor() because setActiveLabel writes
// to editor.state.polygons.activeLabel and saveLabels touches editor's shapes.

import { $, closeModal, showToast } from "./util.js";
import { api } from "./api.js";
import { appLabels, setAppLabels, getLabelByName } from "./state.js";

let editorRef = null;
export function setEditor(ed) { editorRef = ed; }

let _modalLabels = [];

export function setActiveLabel(name) {
  if (editorRef) editorRef.state.polygons.activeLabel = name;
  const lbl = getLabelByName(name);
  const dot = $("#polyLabelDot");
  const nm  = $("#polyLabelName");
  if (dot) dot.style.background = lbl.color;
  if (nm)  nm.textContent = lbl.name;
  const dd = $("#labelDropdown");
  if (dd) {
    dd.querySelectorAll(".label-picker__item").forEach((el) => {
      el.classList.toggle("label-picker__item--active", el.dataset.name === name);
    });
  }
}

export function _buildLabelDropdown() {
  // Day 3b: items list moved into nested #labelDropdownItems; #labelDropdown is popover root.
  const dd = $("#labelDropdownItems") || $("#labelDropdown");
  if (!dd) return;
  const activeLabel = editorRef && editorRef.state.polygons.activeLabel;
  dd.innerHTML = "";
  appLabels.forEach((lbl) => {
    const item = document.createElement("div");
    item.className = "label-picker__item" +
      (lbl.name === activeLabel ? " label-picker__item--active" : "");
    item.dataset.name = lbl.name;
    item.innerHTML =
      `<span class="label-dot" style="background:${lbl.color}"></span>` +
      `<span>${lbl.name}</span>` +
      (lbl.shortcut ? `<span class="label-picker__shortcut">${lbl.shortcut}</span>` : "");
    item.addEventListener("click", () => {
      setActiveLabel(lbl.name);
      $("#labelDropdown").style.display = "none";
    });
    dd.appendChild(item);
  });
}

// Day 3c′: окремий popover для base chip (replaces popover Advanced).
// Click на label → set editorRef.state.baseLabel + POST /api/base-label + close.
function _buildBaseChipDropdown() {
  const dd = $("#baseChipDropdownItems");
  if (!dd) return;
  const cur = editorRef && editorRef.state.baseLabel;
  dd.innerHTML = "";
  appLabels.forEach((lbl) => {
    const item = document.createElement("div");
    item.className = "label-picker__item" +
      (lbl.name === cur ? " label-picker__item--active" : "");
    item.dataset.name = lbl.name;
    item.innerHTML =
      `<span class="label-dot" style="background:${lbl.color}"></span>` +
      `<span>${lbl.name}</span>`;
    item.addEventListener("click", async () => {
      if (!editorRef) return;
      const label = lbl.name;
      editorRef.state.baseLabel = label;
      try {
        await api(`/api/base-label/${encodeURIComponent(editorRef.state.stem)}`, {
          method: "POST",
          body: JSON.stringify({ label }),
        });
        showToast(`Модельні інстанси → ${label}`, "ok", 1600);
      } catch (err) {
        showToast(`Base label: ${err.message || err}`, "err", 3000);
      }
      editorRef._refreshBaseChip();
      $("#baseChipDropdown").style.display = "none";
    });
    dd.appendChild(item);
  });
}

export function openLabelsModal() {
  _modalLabels = [...appLabels];
  const pg = editorRef && editorRef.state.polygons;
  if (pg && pg.shapes && pg.shapes.length) {
    const autoColors = ["#ee7744","#44aaee","#44ee88","#cc44ff","#eecc44","#44eeff","#ff4488"];
    let ci = 0;
    const seen = new Set(appLabels.map((l) => l.name));
    pg.shapes.forEach((sh) => {
      if (sh.label && !seen.has(sh.label)) {
        seen.add(sh.label);
        _modalLabels.push({ id: `auto_${sh.label}`, name: sh.label,
          color: autoColors[ci++ % autoColors.length], shortcut: "" });
      }
    });
  }
  _renderLabelsList();
  $("#labelsModal").classList.add("open");
}

export function resetModalLabels() { _modalLabels = []; }

function _renderLabelsList() {
  const labels = _modalLabels.length ? _modalLabels : appLabels;
  const shortcuts = ["", "1","2","3","4","5","6","7","8","9"];
  const tbody = $("#labelsList");
  if (!tbody) return;
  tbody.innerHTML = labels.map((lbl, i) => `
    <tr data-idx="${i}">
      <td><input type="color" class="label-color-input" value="${lbl.color}" data-field="color" /></td>
      <td><input type="text" class="label-name-input" value="${lbl.name}" data-field="name" /></td>
      <td>
        <select class="label-shortcut-select" data-field="shortcut">
          ${shortcuts.map((s) => `<option value="${s}"${lbl.shortcut === s ? " selected" : ""}>${s || "—"}</option>`).join("")}
        </select>
      </td>
      <td><button class="label-delete-btn" title="Видалити">×</button></td>
    </tr>
  `).join("");
  tbody.querySelectorAll(".label-delete-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest("tr");
      if (tbody.querySelectorAll("tr").length <= 1) {
        showToast("Потрібен хоча б один клас", "err", 2000);
        return;
      }
      row.remove();
    });
  });
}

function _collectLabelsFromModal() {
  const baseLabels = _modalLabels.length ? _modalLabels : appLabels;
  const rows = [...document.querySelectorAll("#labelsList tr")];
  return rows.map((row, i) => ({
    id: baseLabels[i] ? baseLabels[i].id : Date.now() + i,
    name: (row.querySelector("[data-field=name]").value || "").trim() || `label${i + 1}`,
    color: row.querySelector("[data-field=color]").value,
    shortcut: row.querySelector("[data-field=shortcut]").value,
  })).filter((l) => l.name);
}

export function saveLabels() {
  const newLabels = _collectLabelsFromModal();
  if (!newLabels.length) { showToast("Потрібен хоча б один клас", "err"); return; }

  const renameMap = new Map();
  appLabels.forEach((old) => {
    const nu = newLabels.find((n) => n.id === old.id);
    if (nu && nu.name !== old.name) renameMap.set(old.name, nu.name);
  });

  api("/api/labels", { method: "POST", body: JSON.stringify(newLabels) })
    .then(() => {
      if (renameMap.size === 0) return null;
      const renames = [...renameMap.entries()].map(([from, to]) => ({ from, to }));
      return api("/api/labels/rename", {
        method: "POST",
        body: JSON.stringify({ renames }),
      });
    })
    .then((renameResult) => {
      if (renameMap.size > 0 && editorRef) {
        const pg = editorRef.state.polygons;
        let changed = 0;
        pg.shapes.forEach((sh) => {
          if (renameMap.has(sh.label)) { sh.label = renameMap.get(sh.label); changed++; }
        });
        if (changed) { editorRef._polyMarkDirty(); editorRef._polyRedraw(); }
      }
      setAppLabels(newLabels);
      _modalLabels = [];
      const pg = editorRef && editorRef.state.polygons;
      const stillValid = appLabels.find((l) => l.name === (pg && pg.activeLabel));
      setActiveLabel(stillValid ? pg.activeLabel : appLabels[0].name);
      _buildLabelDropdown();
      closeModal($("#labelsModal"));
      const changedFiles = renameResult ? renameResult.files_changed || 0 : 0;
      const note = renameMap.size > 0
        ? ` (${renameMap.size} перейменовано, файлів: ${changedFiles})`
        : "";
      showToast(`✓ ${newLabels.length} класи збережено${note}`, "ok");
    })
    .catch((e) => showToast(`Labels save: ${e.message}`, "err", 3000));
}

export function bindEvents() {
  const btnLabels = $("#btnLabels");
  if (btnLabels) btnLabels.onclick = openLabelsModal;
  const btnSaveLabels = $("#btnSaveLabels");
  if (btnSaveLabels) btnSaveLabels.onclick = saveLabels;

  const btnAddLabel = $("#btnAddLabel");
  if (btnAddLabel) btnAddLabel.onclick = () => {
    const tbody = $("#labelsList");
    if (!tbody) return;
    const i = tbody.querySelectorAll("tr").length;
    const colors = ["#4488ff","#ff6644","#44cc88","#cc44ff","#ffcc44","#44ccff","#ff4488","#88cc44","#ff8844"];
    const color = colors[i % colors.length];
    const tr = document.createElement("tr");
    tr.dataset.idx = String(i);
    tr.innerHTML = `
      <td><input type="color" class="label-color-input" value="${color}" data-field="color" /></td>
      <td><input type="text" class="label-name-input" value="label${i + 1}" data-field="name" /></td>
      <td>
        <select class="label-shortcut-select" data-field="shortcut">
          <option value="">—</option>
          ${["1","2","3","4","5","6","7","8","9"].map((s) => `<option value="${s}">${s}</option>`).join("")}
        </select>
      </td>
      <td><button class="label-delete-btn" title="Видалити">×</button></td>
    `;
    tr.querySelector(".label-delete-btn").addEventListener("click", () => {
      if (tbody.querySelectorAll("tr").length <= 1) {
        showToast("Потрібен хоча б один клас", "err", 2000);
        return;
      }
      tr.remove();
    });
    tbody.appendChild(tr);
  };

  const polyLabelBtn = $("#polyLabelBtn");
  if (polyLabelBtn) {
    polyLabelBtn.onclick = (e) => {
      e.stopPropagation();
      _buildLabelDropdown();
      // Day 3c′: mutex — закрити base popover якщо відкритий (overlap при обох open).
      const baseDd = $("#baseChipDropdown");
      if (baseDd) baseDd.style.display = "none";
      const dd = $("#labelDropdown");
      dd.style.display = dd.style.display === "none" ? "" : "none";
    };
  }
  document.addEventListener("click", (e) => {
    const dd = $("#labelDropdown");
    if (dd && !dd.contains(e.target) && e.target !== polyLabelBtn) {
      dd.style.display = "none";
    }
  });
  // Day 3b: Esc closes popover, swallowing event so editor Esc (cancel draft / deselect) doesn't fire too.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const dd = $("#labelDropdown");
    if (dd && dd.style.display !== "none") {
      dd.style.display = "none";
      e.stopPropagation();
    }
  }, true);

  const shapeLabelSel = $("#polyShapeLabelSelect");
  if (shapeLabelSel) {
    shapeLabelSel.addEventListener("change", () => {
      if (!editorRef) return;
      const pg = editorRef.state.polygons;
      const idxs = JSON.parse(shapeLabelSel.dataset.selectedShapes || "[]");
      if (!idxs.length || !shapeLabelSel.value) return;
      editorRef._polyPushUndoSnapshot();
      idxs.forEach((i) => {
        if (pg.shapes[i]) pg.shapes[i].label = shapeLabelSel.value;
      });
      editorRef._polyMarkDirty();
      editorRef._polyRedraw();
      editorRef._polyUpdateButtons();
    });
  }

  // Day 3c′: base chip має власний popover (decoupled від active-label).
  // Mutex з labelDropdown — відкриваючись закриваємо інший (overlap при обох open).
  const baseChip = $("#polyBaseChip");
  if (baseChip) {
    baseChip.onclick = (e) => {
      e.stopPropagation();
      _buildBaseChipDropdown();
      const labelDd = $("#labelDropdown");
      if (labelDd) labelDd.style.display = "none";
      const dd = $("#baseChipDropdown");
      if (dd) dd.style.display = dd.style.display === "none" ? "" : "none";
    };
  }
  document.addEventListener("click", (e) => {
    const dd = $("#baseChipDropdown");
    if (dd && !dd.contains(e.target) && e.target !== baseChip
        && !(baseChip && baseChip.contains(e.target))) {
      dd.style.display = "none";
    }
  });
  // Esc closes base popover (capture phase — swallows editor Esc deselect/cancel-draft).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const dd = $("#baseChipDropdown");
    if (dd && dd.style.display !== "none") {
      dd.style.display = "none";
      e.stopPropagation();
    }
  }, true);
}
