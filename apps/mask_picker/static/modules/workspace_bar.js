// Workspace bar: import / export / finalize / change-workspace buttons.
// Editor reference needed для flushIfDirty() before finalize. catalog functions
// reloadCatalog/renderAll injected щоб не тягнути catalog.js → workspace_bar.

import { $, showToast } from "./util.js";
import { api } from "./api.js";
import { state, currentItem } from "./state.js";

let editorRef = null;
let reloadCatalogFn = null;
let renderAllFn = null;
export function setEditor(ed) { editorRef = ed; }
export function setCatalogCallbacks(reloadFn, renderFn) {
  reloadCatalogFn = reloadFn;
  renderAllFn = renderFn;
}

// ---------------------------------------------------------------------------
// Day 7 — Save All: запуск batch-bake + polling прогресу (2 progress bars).
// ---------------------------------------------------------------------------

let bakePollTimer = null;
// Day 8.5: дія, що виконується після завершення bake-all (Export+Finalize).
let _afterBakeAction = null;

function _setBakeBar(fillEl, frac) {
  if (!fillEl) return;
  const pct = Math.round(Math.max(0, Math.min(1, frac || 0)) * 100);
  fillEl.style.width = `${pct}%`;
}

function _renderBakeProgress(p) {
  const photoFrac = p.photo_total ? p.photo_done / p.photo_total : 0;
  _setBakeBar($("#bakePhotoFill"), photoFrac);
  $("#bakePhotoText").textContent = `${p.photo_done} / ${p.photo_total}`;
  _setBakeBar($("#bakeInstFill"), p.phase_frac || 0);
  $("#bakeInstName").textContent = p.current_stem
    ? `Фото: ${p.current_stem}` : "Поточне фото";
  $("#bakeInstText").textContent = p.phase || "—";
}

function _finishBake(p) {
  if (bakePollTimer) { clearInterval(bakePollTimer); bakePollTimer = null; }
  _setBakeBar($("#bakePhotoFill"), 1);
  _setBakeBar($("#bakeInstFill"), 1);
  $("#bakeInstText").textContent = "Завершено";
  const errs = (p.errors || []).length;
  const skips = (p.skipped || []).length;
  let head = `✓ Запечено ${p.ok_count} фото`;
  if (skips) head += `, пропущено ${skips}`;
  if (errs)  head += `, з помилками ${errs}`;
  const lines = [head];
  for (const e of (p.skipped || [])) lines.push(`⊘ ${e.stem}: ${e.reason}`);
  for (const e of (p.errors  || [])) lines.push(`✗ ${e.stem}: ${e.error}`);
  const statusEl = $("#bakeAllStatus");
  statusEl.textContent = lines.join("\n");
  statusEl.classList.toggle("bake__status--err", errs > 0);
  $("#bakeAllClose").disabled = false;
  showToast(head, errs ? "warn" : "ok", 5000);

  // Day 8.5: Export+Finalize — після запікання виконати відкладену дію
  // (download export або split) і закрити bake-modal.
  if (_afterBakeAction) {
    const fn = _afterBakeAction;
    _afterBakeAction = null;
    $("#bakeAllIntro").textContent = "Запечено. Продовжую експорт…";
    setTimeout(() => {
      $("#bakeAllModal").classList.remove("open");
      fn();
    }, 1000);
  } else {
    $("#bakeAllIntro").textContent = "Готово. Можна закрити це вікно.";
  }
}

// Day 8.5: відкрити bake-modal і запустити polling (спільне для Save All
// та Export+Finalize).
function _showBakeModal(photoTotal, title) {
  $("#bakeAllTitle").textContent = title;
  $("#bakeAllIntro").textContent =
    `Запікаю ${photoTotal} фото у фінальні маски. Це може зайняти ` +
    `кілька хвилин — будь ласка, не закривай вкладку.`;
  $("#bakeAllStatus").textContent = "";
  $("#bakeAllStatus").classList.remove("bake__status--err");
  $("#bakeAllClose").disabled = true;
  _setBakeBar($("#bakePhotoFill"), 0);
  _setBakeBar($("#bakeInstFill"), 0);
  $("#bakePhotoText").textContent = `0 / ${photoTotal}`;
  $("#bakeInstText").textContent = "—";
  $("#bakeAllModal").classList.add("open");
  if (bakePollTimer) clearInterval(bakePollTimer);
  bakePollTimer = setInterval(_pollBake, 400);
  _pollBake();
}

async function _pollBake() {
  try {
    const p = await api("/api/workspace/bake-progress");
    _renderBakeProgress(p);
    if (p.done && !p.running) _finishBake(p);
  } catch (e) {
    console.warn("bake-progress poll failed:", e);
  }
}

async function runBakeAll(rebakeAll) {
  if (rebakeAll && !confirm(
    "Перепекти ВСІ обрані фото з нуля?\n\n" +
    "Це перерахує весь датасет і може зайняти багато часу. Робити варто " +
    "лише після зміни класів (labels.json) або якщо selected/ зіпсувалось.")) {
    return;
  }

  let resp;
  try {
    resp = await api("/api/workspace/bake-all", {
      method: "POST",
      body: JSON.stringify({ all: !!rebakeAll }),
    });
  } catch (e) {
    showToast(`Запікання: ${e.message || e}`, "err", 5000);
    return;
  }
  if (!resp.ok) {
    showToast(resp.error || "Не вдалося стартувати запікання", "err", 5000);
    return;
  }
  if (!resp.started) {
    showToast(resp.message || "Нема чого запікати", "ok", 3000);
    return;
  }

  _showBakeModal(
    resp.photo_total,
    rebakeAll ? "🔥 Перезапікання всіх фото" : "💾 Запікання у selected/",
  );
}

// ---------------------------------------------------------------------------
// Day 8 — merge import: scan ZIP → range-picker modal → selective apply.
// ---------------------------------------------------------------------------

let _importScanId = null;
let _importSources = [];

function _stemNum(stem) {
  const m = String(stem).match(/\d+/);
  return m ? parseInt(m[0], 10) : 0;
}

function _renderImportMergeRows() {
  const body = $("#importMergeBody");
  body.innerHTML = _importSources.map((src) => {
    if (src.error) {
      return `<div class="import-src import-src--err"><b>${src.name}</b> — ⚠ ${src.error}</div>`;
    }
    if (!src.count) {
      return `<div class="import-src import-src--err"><b>${src.name}</b> — фото не знайдено</div>`;
    }
    const nums = src.stems.map(_stemNum);
    const mn = Math.min(...nums), mx = Math.max(...nums);
    return `
      <div class="import-src" data-src-idx="${src.idx}">
        <div class="import-src__name">
          <b>${src.name}</b>
          <span class="muted">${src.count} фото (№${mn}–${mx})</span>
        </div>
        <div class="import-src__range">
          від <input type="number" class="import-src__from" value="${mn}" min="0" />
          до <input type="number" class="import-src__to" value="${mx}" min="0" />
          <span class="import-src__count"></span>
        </div>
      </div>`;
  }).join("");

  body.querySelectorAll(".import-src[data-src-idx]").forEach((row) => {
    const idx = parseInt(row.dataset.srcIdx, 10);
    const src = _importSources.find((s) => s.idx === idx);
    if (!src) return;
    const fromI = row.querySelector(".import-src__from");
    const toI = row.querySelector(".import-src__to");
    const cnt = row.querySelector(".import-src__count");
    const upd = () => {
      const f = parseInt(fromI.value, 10);
      const t = parseInt(toI.value, 10);
      const n = src.stems.filter((s) => {
        const x = _stemNum(s);
        return x >= f && x <= t;
      }).length;
      cnt.textContent = `→ ${n} обрано`;
    };
    fromI.oninput = upd;
    toI.oninput = upd;
    upd();
  });
}

function _openImportMergeModal(scanId, sources) {
  _importScanId = scanId;
  _importSources = sources || [];
  _renderImportMergeRows();
  $("#importMergeModal").classList.add("open");
}

// ---------------------------------------------------------------------------
// Day 8.5 — bulk-призначення анотатора діапазону фото.
// ---------------------------------------------------------------------------

function _bulkUserStemsInRange() {
  const f = parseInt($("#bulkUserFrom").value, 10);
  const t = parseInt($("#bulkUserTo").value, 10);
  return (state.catalog || [])
    .filter((it) => {
      const n = _stemNum(it.stem);
      return n >= f && n <= t;
    })
    .map((it) => it.stem);
}

function _openBulkUserModal() {
  const nums = (state.catalog || [])
    .map((it) => _stemNum(it.stem))
    .filter((n) => n > 0);
  const mn = nums.length ? Math.min(...nums) : 0;
  const mx = nums.length ? Math.max(...nums) : 0;
  $("#bulkUserName").value = "";
  $("#bulkUserFrom").value = mn;
  $("#bulkUserTo").value = mx;
  const upd = () => {
    $("#bulkUserCount").textContent = `→ ${_bulkUserStemsInRange().length} фото`;
  };
  $("#bulkUserFrom").oninput = upd;
  $("#bulkUserTo").oninput = upd;
  upd();
  $("#bulkUserModal").classList.add("open");
}

async function _applyBulkUser() {
  const user = $("#bulkUserName").value.trim();
  if (!user) {
    showToast("Введи ім'я анотатора", "warn", 2500);
    return;
  }
  const stems = _bulkUserStemsInRange();
  if (!stems.length) {
    showToast("У вказаному діапазоні немає фото", "warn", 2500);
    return;
  }
  const btn = $("#bulkUserApply");
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const resp = await api("/api/bulk-user", {
      method: "POST",
      body: JSON.stringify({ stems, user }),
    });
    if (resp.ok) {
      let msg = `✓ ${user}: призначено ${resp.count} фото`;
      if (resp.skipped && resp.skipped.length) {
        msg += ` (${resp.skipped.length} без рішення — пропущено)`;
      }
      showToast(msg, "ok", 4500);
      $("#bulkUserModal").classList.remove("open");
      if (reloadCatalogFn) await reloadCatalogFn(true);
      if (renderAllFn) renderAllFn();
    } else {
      showToast(`Помилка: ${resp.error || "?"}`, "err", 5000);
    }
  } catch (e) {
    showToast(`Мережа: ${e.message || e}`, "err", 5000);
  } finally {
    btn.disabled = false;
    btn.textContent = "Призначити";
  }
}

// ---------------------------------------------------------------------------
// Day 8.5 — Export modal: вибір фото + опції finalize / split.
// ---------------------------------------------------------------------------

const _exportSel = new Set();

function _exportVisiblePhotos() {
  return (state.catalog || [])
    .filter((it) => !(it.state && it.state.status === "excluded"));
}

function _updateExportSelCount() {
  const all = _exportVisiblePhotos();
  $("#exportSelCount").textContent = `(${_exportSel.size} з ${all.length})`;
  const allChecked = all.length > 0 && all.every((it) => _exportSel.has(it.stem));
  $("#exportSelectAll").checked = allChecked;
  const btn = $("#exportApply");
  if (btn) btn.disabled = _exportSel.size === 0;
}

function _renderExportList() {
  const list = $("#exportPhotoList");
  const photos = _exportVisiblePhotos();
  list.innerHTML = photos.map((it) => {
    const checked = _exportSel.has(it.stem) ? "checked" : "";
    const st = it.state && it.state.status;
    const dirty = it.state && it.state.dirty ? " 🟡" : "";
    const badge = st === "selected" ? "✓" : st === "skipped" ? "⊘" : "·";
    return `
      <label class="export__item">
        <input type="checkbox" class="export__check" data-stem="${it.stem}" ${checked} />
        <span class="export__badge">${badge}</span>
        <span class="export__stemname">${it.stem}${dirty}</span>
      </label>`;
  }).join("") || `<div class="muted" style="padding:8px;">Немає фото.</div>`;
  _updateExportSelCount();
}

function _openExportModal() {
  _exportSel.clear();
  // Day 8.5 default — усі переглянуті фото (selected / skipped).
  for (const it of _exportVisiblePhotos()) {
    if (it.state && it.state.status) _exportSel.add(it.stem);
  }
  const nums = _exportVisiblePhotos()
    .map((it) => _stemNum(it.stem)).filter((n) => n > 0);
  $("#exportRangeFrom").value = nums.length ? Math.min(...nums) : 0;
  $("#exportRangeTo").value = nums.length ? Math.max(...nums) : 0;
  $("#exportFinalize").checked = false;
  $("#exportSplitOn").checked = false;
  $("#exportMasks").checked = false;
  $("#exportDest").value = localStorage.getItem("exportDest") || "";
  _renderExportList();
  $("#exportModal").classList.add("open");
}

function _doSplitExport(n, stems) {
  api("/api/workspace/split", {
    method: "POST",
    body: JSON.stringify({ n, stems }),
  }).then((resp) => {
    if (resp.ok) {
      showToast(
        `✓ Створено ${resp.zips.length} task-pack у ${resp.out_dir}`,
        "ok", 8000,
      );
    } else {
      showToast(`Помилка розбиття: ${resp.error || "?"}`, "err", 6000);
    }
  }).catch((e) => showToast(`Мережа: ${e.message || e}`, "err", 5000));
}

async function _applyExport() {
  const stems = [..._exportSel];
  if (!stems.length) {
    showToast("Обери хоча б одне фото", "warn", 2500);
    return;
  }
  const doFinalize = $("#exportFinalize").checked;
  const doSplit = $("#exportSplitOn").checked;
  const doMasks = $("#exportMasks").checked;
  const dest = $("#exportDest").value.trim();
  const splitN = parseInt($("#exportSplitN").value, 10);
  if (doSplit && (!Number.isInteger(splitN) || splitN < 2 || splitN > 20)) {
    showToast("Розбиття: введи число 2..20", "err", 3000);
    return;
  }

  // Без finalize — preflight попередження про незапечені фото.
  if (!doFinalize) {
    const dirtyN = stems.filter((s) => {
      const it = (state.catalog || []).find((x) => x.stem === s);
      return it && it.state && it.state.dirty;
    }).length;
    if (dirtyN > 0 && !confirm(
      `${dirtyN} обраних фото мають незапечені зміни (🟡).\n` +
      `Експортувати без фіналізації (застарілі маски)?`)) {
      return;
    }
  }

  $("#exportModal").classList.remove("open");

  // Кінцева дія: split (task-packs) або звичайний download ZIP.
  const finalAction = doSplit
    ? () => _doSplitExport(splitN, stems)
    : () => {
        const url = "/api/workspace/export?stems="
          + encodeURIComponent(stems.join(","))
          + (doMasks ? "&masks=1" : "")
          + (dest ? "&dest=" + encodeURIComponent(dest) : "");
        if (dest) {
          api(url)
            .then((r) => {
              if (r && r.ok) showToast(`✓ ZIP збережено: ${r.path}`, "ok", 8000);
              else showToast(`Експорт: ${(r && r.error) || "?"}`, "err", 6000);
            })
            .catch((e) => showToast(`Експорт: ${e.message || e}`, "err", 6000));
        } else {
          window.location.href = url;
        }
      };

  if (!doFinalize) {
    finalAction();
    return;
  }

  // Finalize → запекти обрані фото, потім finalAction (після bake-modal).
  let resp;
  try {
    resp = await api("/api/workspace/bake-all", {
      method: "POST",
      body: JSON.stringify({ stems }),
    });
  } catch (e) {
    showToast(`Запікання: ${e.message || e}`, "err", 5000);
    return;
  }
  if (!resp.ok) {
    showToast(resp.error || "Не вдалося стартувати запікання", "err", 5000);
    return;
  }
  if (!resp.started) {
    finalAction();  // нема dirty серед обраних — одразу
    return;
  }
  _afterBakeAction = finalAction;
  _showBakeModal(resp.photo_total, "💾 Фіналізація перед експортом");
}

async function _applyImportMerge() {
  if (!_importScanId) return;
  const picks = [];
  $("#importMergeBody").querySelectorAll(".import-src[data-src-idx]").forEach((row) => {
    const idx = parseInt(row.dataset.srcIdx, 10);
    const src = _importSources.find((s) => s.idx === idx);
    if (!src) return;
    const f = parseInt(row.querySelector(".import-src__from").value, 10);
    const t = parseInt(row.querySelector(".import-src__to").value, 10);
    const stems = src.stems.filter((s) => {
      const x = _stemNum(s);
      return x >= f && x <= t;
    });
    if (stems.length) picks.push({ idx, stems });
  });
  if (!picks.length) {
    showToast("Жодного фото не потрапило у вказані діапазони", "warn", 3000);
    return;
  }
  const applyBtn = $("#importMergeApply");
  applyBtn.disabled = true;
  applyBtn.textContent = "Імпортую…";
  try {
    const resp = await api("/api/workspace/import-apply", {
      method: "POST",
      body: JSON.stringify({ scan_id: _importScanId, picks }),
    });
    if (resp.ok) {
      showToast(
        `✓ Імпортовано ${resp.merged_count} фото (${resp.files_copied} файлів)`,
        "ok", 5000,
      );
      $("#importMergeModal").classList.remove("open");
      if (reloadCatalogFn) await reloadCatalogFn(true);
      if (renderAllFn) renderAllFn();
    } else {
      showToast(`Помилка імпорту: ${resp.error || "?"}`, "err", 6000);
    }
  } catch (e) {
    showToast(`Мережа: ${e.message || e}`, "err", 6000);
  } finally {
    applyBtn.disabled = false;
    applyBtn.textContent = "Імпортувати";
    _importScanId = null;
  }
}

export function bindEvents() {
  const importInput = $("#importFileInput");
  const btnImport   = $("#btnImport");
  const btnExport   = $("#btnExport");
  const btnBakeAll  = $("#btnBakeAll");
  const bakeAllClose = $("#bakeAllClose");
  const btnChangeWorkspace = $("#btnChangeWorkspace");

  // "з нуля" checkbox → full rebake of ALL photos (was the 🔥 button); else
  // bake only dirty stems. runBakeAll(true) confirms first (slow op).
  if (btnBakeAll) btnBakeAll.onclick = () => {
    const scratch = $("#bakeFromScratch");
    runBakeAll(!!(scratch && scratch.checked));
  };
  if (bakeAllClose) {
    bakeAllClose.onclick = async () => {
      $("#bakeAllModal").classList.remove("open");
      // Перечитуємо catalog — dirty-крапки мають зникнути для запечених фото.
      if (reloadCatalogFn) await reloadCatalogFn(true);
      if (renderAllFn) renderAllFn();
    };
  }

  if (btnChangeWorkspace) {
    btnChangeWorkspace.onclick = async () => {
      btnChangeWorkspace.disabled = true;
      btnChangeWorkspace.textContent = "…";
      try {
        const resp = await api("/api/workspace/pick-folder", {
          method: "POST",
          body: JSON.stringify({}),
        });
        if (resp.cancelled) {
          showToast("Workspace не змінено", "warn", 2000);
        } else if (resp.ok) {
          showToast("Workspace змінено. Перезавантажую…", "ok", 2000);
          window.location.reload();
        }
      } catch (e) {
        showToast(`Workspace picker: ${e.message}`, "err", 5000);
      } finally {
        btnChangeWorkspace.disabled = false;
        btnChangeWorkspace.textContent = "Змінити";
      }
    };
  }

  if (btnImport && importInput) {
    btnImport.onclick = () => importInput.click();
    // Day 8: import = scan (фаза 1) → range-picker modal → apply (фаза 2).
    importInput.onchange = async () => {
      const files = importInput.files;
      if (!files || files.length === 0) return;
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      btnImport.disabled = true;
      btnImport.textContent = "📥 …";
      try {
        const res = await fetch("/api/workspace/import-scan", { method: "POST", body: fd });
        const body = await res.json();
        if (res.ok && body.ok) {
          _openImportMergeModal(body.scan_id, body.sources);
        } else {
          showToast(`Помилка сканування: ${body.error || JSON.stringify(body)}`, "err", 6000);
        }
      } catch (e) {
        showToast(`Помилка мережі: ${e}`, "err", 6000);
      } finally {
        btnImport.disabled = false;
        btnImport.textContent = "📥 Імпорт";
        importInput.value = "";
      }
    };
  }

  const importMergeApply = $("#importMergeApply");
  if (importMergeApply) importMergeApply.onclick = _applyImportMerge;

  const btnBulkUser = $("#btnBulkUser");
  if (btnBulkUser) btnBulkUser.onclick = _openBulkUserModal;
  const bulkUserApply = $("#bulkUserApply");
  if (bulkUserApply) bulkUserApply.onclick = _applyBulkUser;

  // Day 8.5: Export — окреме вікно з вибором фото + опціями finalize/split.
  if (btnExport) btnExport.onclick = _openExportModal;
  const exportApply = $("#exportApply");
  if (exportApply) exportApply.onclick = _applyExport;
  const exportDestPick = $("#exportDestPick");
  if (exportDestPick) {
    exportDestPick.onclick = async () => {
      try {
        const r = await api("/api/workspace/pick-dir", {
          method: "POST", body: JSON.stringify({}),
        });
        if (r && r.ok && r.path) {
          $("#exportDest").value = r.path;
          localStorage.setItem("exportDest", r.path);
        }
      } catch (e) {
        showToast(`Вибір папки: ${e.message || e}`, "err", 5000);
      }
    };
  }
  const exportDestClear = $("#exportDestClear");
  if (exportDestClear) {
    exportDestClear.onclick = () => {
      $("#exportDest").value = "";
      localStorage.removeItem("exportDest");
    };
  }
  const exportSelectAll = $("#exportSelectAll");
  if (exportSelectAll) {
    exportSelectAll.onchange = () => {
      if (exportSelectAll.checked) {
        for (const it of _exportVisiblePhotos()) _exportSel.add(it.stem);
      } else {
        _exportSel.clear();
      }
      _renderExportList();
    };
  }
  const exportRangeApply = $("#exportRangeApply");
  if (exportRangeApply) {
    exportRangeApply.onclick = () => {
      const f = parseInt($("#exportRangeFrom").value, 10);
      const t = parseInt($("#exportRangeTo").value, 10);
      for (const it of _exportVisiblePhotos()) {
        const n = _stemNum(it.stem);
        if (n >= f && n <= t) _exportSel.add(it.stem);
      }
      _renderExportList();
    };
  }
  const exportPhotoList = $("#exportPhotoList");
  if (exportPhotoList) {
    exportPhotoList.addEventListener("change", (e) => {
      const cb = e.target.closest(".export__check");
      if (!cb) return;
      if (cb.checked) _exportSel.add(cb.dataset.stem);
      else _exportSel.delete(cb.dataset.stem);
      _updateExportSelCount();
    });
  }

}
