// Stats modal — GET /api/stats and render summary table.

import { $ } from "./util.js";
import { api } from "./api.js";

export async function openStats() {
  $("#statsBody").innerHTML = "Loading…";
  $("#statsModal").classList.add("open");
  try {
    const s = await api("/api/stats");
    const total = s.total || 1;
    const byModel = Object.entries(s.by_model).sort((a, b) => b[1] - a[1]);
    const lines = [];
    lines.push(`<div class="stats-row"><span>Всього фото</span><b>${s.total}</b></div>`);
    lines.push(`<div class="stats-row"><span>Переглянуто</span><b>${s.reviewed} (${Math.round(s.reviewed*100/total)}%)</b></div>`);
    lines.push(`<div class="stats-row"><span>Чекають</span><b>${s.unreviewed}</b></div>`);
    lines.push(`<div class="stats-row"><span>Пропущено</span><b>${s.skipped}</b></div>`);
    if (s.dirty) {
      lines.push(`<div class="stats-row"><span>🟡 Незапечені зміни</span><b>${s.dirty}</b></div>`);
    }

    // Day 8: per-user статистика — хто скільки розмітив.
    const byUser = Object.entries(s.by_user || {})
      .sort((a, b) => (b[1].selected + b[1].skipped) - (a[1].selected + a[1].skipped));
    if (byUser.length) {
      lines.push(`<div style="padding-top: 12px; font-weight: 600;">Хто скільки розмітив:</div>`);
      lines.push(`
        <div class="stats-row stats-row--head">
          <span>Анотатор</span>
          <b>✓ обрано / ⊘ пропущено${s.dirty ? " / 🟡" : ""}</b>
        </div>`);
      byUser.forEach(([name, u]) => {
        const done = u.selected + u.skipped;
        const pct = Math.round(done * 100 / total);
        const dirtyPart = s.dirty ? ` / 🟡${u.dirty}` : "";
        lines.push(`
          <div class="stats-row"><span>${name}</span><b>✓${u.selected} / ⊘${u.skipped}${dirtyPart}</b></div>
          <div class="stats-bar"><div class="stats-bar__fill" style="width: ${pct}%"></div></div>`);
      });
    }

    lines.push(`<div style="padding-top: 12px; font-weight: 600;">Обрано моделей:</div>`);
    byModel.forEach(([name, count]) => {
      const pct = Math.round(count * 100 / total);
      lines.push(`
        <div class="stats-row"><span>${name}</span><b>${count}</b></div>
        <div class="stats-bar"><div class="stats-bar__fill" style="width: ${pct}%"></div></div>`);
    });
    $("#statsBody").innerHTML = lines.join("");
  } catch (e) {
    $("#statsBody").textContent = `Помилка: ${e.message}`;
  }
}
