/**
 * zoompanMixin — спільний для обох табів zoom/pan + промальовка base canvas
 * (з overlay або з оригіналу) + перемикання background source.
 *
 * Cross-tab read: `_paintRejectedFromOriginal()` витирає rejected-інстанси з
 * overlay-PNG поверх original-зображення, читаючи `this.state.cleanup.bboxes`
 * + `rejectedSet` — це збережена з v1.7.1 поведінка щоб rejected не "світилися"
 * у Polygons-табі.
 *
 * Викликає `_polyResizeVertices` (polygons mixin) та `_cleanupRedraw`
 * (cleanup mixin) через `this`.
 */

export const zoompanMixin = {
  _applyZoomTransform() {
    const z = this._zoom();
    if (!z) return;
    z.style.transform = `translate(${this.state.panX}px, ${this.state.panY}px) scale(${this.state.scale})`;
    if (this.state.open && this.state.activeTab === "polygons") {
      this._polyResizeVertices();
    }
  },

  resetZoom() {
    this.state.scale = 1; this.state.panX = 0; this.state.panY = 0;
    this._applyZoomTransform();
  },

  _onWheel(e) {
    e.preventDefault();
    const wrap = this._wrap();
    const rect = wrap.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    const newScale = Math.max(0.25, Math.min(20, this.state.scale * factor));
    const ptX = (mx - this.state.panX) / this.state.scale;
    const ptY = (my - this.state.panY) / this.state.scale;
    this.state.panX = mx - ptX * newScale;
    this.state.panY = my - ptY * newScale;
    this.state.scale = newScale;
    this._applyZoomTransform();
  },

  _onPanStart(e) {
    const isPanBtn = e.button === 1 || e.button === 2
      || (e.button === 0 && this.state.spaceDown);
    if (!isPanBtn) return;
    e.preventDefault();
    this.state.isPanning = true;
    this.state.panStartX = e.clientX;
    this.state.panStartY = e.clientY;
    this.state.panOrigX = this.state.panX;
    this.state.panOrigY = this.state.panY;
    this._wrap().classList.add("is-panning");
  },

  _onPanMove(e) {
    if (!this.state.isPanning) return;
    this.state.panX = this.state.panOrigX + (e.clientX - this.state.panStartX);
    this.state.panY = this.state.panOrigY + (e.clientY - this.state.panStartY);
    this._applyZoomTransform();
  },

  _onPanEnd() {
    if (!this.state.isPanning) return;
    this.state.isPanning = false;
    this._wrap().classList.remove("is-panning");
  },

  _drawBase() {
    const { W, H, originalImage, overlayImage } = this.state;
    const ctx = this._cBase().getContext("2d");
    // Bug 1 (Day 9): фон — фотографія/overlay. Гладке масштабування —
    // якщо overlay-PNG дрібний, true дає плавний апскейл замість блочного
    // nearest-neighbor. _paintRejectedFromOriginal копіює 1:1, тож не страждає.
    ctx.imageSmoothingEnabled = true;
    ctx.clearRect(0, 0, W, H);
    const wantOverlay = this._currentBgSource() === "overlay";
    const img = (wantOverlay && overlayImage) ? overlayImage : originalImage;
    if (img) ctx.drawImage(img, 0, 0, W, H);
    // Ховаємо «прибрані» інстанси: малюємо поверх їх пікселі з оригіналу. Це
    // прибирає червону cellpose-обводку з overlay-PNG + заливку, щоб клітина не
    // "світилася". Набір що ховаємо залежить від табу (_overlayHideSet):
    //   • cleanup           — ЛИШЕ covered (instance замінений полігоном); явні
    //                         rejected лишаються видимими (червоні марки) для
    //                         керування у Reject-tool.
    //   • polygons / groups — rejected ∪ covered (нема сенсу показувати жодне).
    // covered ховається на ВСІХ табах: інстанс під полігоном візуально зникає
    // (полігон його заміщає), не лишає червоного контуру при перекритті.
    const hideIds = this._overlayHideSet();
    let coveredSum = 0;
    const cuC = this.state.cleanup.coveredCache;
    if (cuC) for (const id of cuC) coveredSum += id;
    this.state.cleanup._coveredBaseSig = `${cuC ? cuC.size : 0}|${coveredSum}`;
    if (originalImage && originalImage !== img
        && this.state.cleanup.labelsInt32
        && hideIds && hideIds.size) {
      this._paintRejectedFromOriginal(ctx, hideIds);
    }
  },

  // Набір instance-id, чий overlay треба замінити чистим оригіналом (сховати).
  // covered = «instance заміщений полігоном» (derived rejected) — ховаємо завжди.
  // На cleanup явні rejected НЕ ховаємо (лишаються червоні марки для керування).
  _overlayHideSet() {
    const cu = this.state.cleanup;
    const covered = this._polyCoveredInstances();   // кешований
    if (this.state.activeTab === "cleanup") return covered;
    if (!cu.rejectedSet || !cu.rejectedSet.size) return covered;
    const set = new Set(covered);
    for (const id of cu.rejectedSet) set.add(id);
    return set;
  },

  // Перемалювати базу ЛИШЕ якщо covered-набір змінився (полігон додано/прибрано).
  // Викликається з _polyRedraw → covered-instance візуально зникає одразу при
  // малюванні полігона, не лише при перемиканні табу/bg. Дешево: covered
  // кешований, при незмінних shapes — sig збігається → no-op.
  _drawBaseIfCoveredChanged() {
    const cu = this.state.cleanup;
    const covered = this._polyCoveredInstances();
    let sum = 0; for (const id of covered) sum += id;
    const sig = `${covered.size}|${sum}`;
    if (sig !== cu._coveredBaseSig) this._drawBase();   // _drawBase оновить _coveredBaseSig
  },

  _paintRejectedFromOriginal(ctx, hideIds) {
    // "Стираємо" приховані інстанси (hideIds) з overlay-PNG, перемальовуючи їх
    // original-ом. Bug C (v1.16.2): по ФОРМІ інстанса, не по прямокутному bbox
    // (раніше bbox+pad зачіпав сусідні kept-клітини у щільних кластерах). Шейп-
    // шар кешується (rebuild лише коли змінився hideIds/фото) → per-frame це
    // ОДИН blit, не повільніше за старі N bbox-blit (фактично швидше).
    const { W, H, originalImage } = this.state;
    const cu = this.state.cleanup;
    if (!originalImage || !hideIds || !hideIds.size) return;
    const patch = this._ensureRejectedPatch(hideIds);
    if (patch) { ctx.drawImage(patch, 0, 0); return; }
    // Fallback (немає labelsInt32): старий bbox-підхід.
    const pad = 4;
    if (cu.bboxes) {
      hideIds.forEach((id) => {
        const bb = cu.bboxes.get(id);
        if (!bb) return;
        const x0 = Math.max(0, (bb.x0 | 0) - pad);
        const y0 = Math.max(0, (bb.y0 | 0) - pad);
        const x1 = Math.min(W, (bb.x1 | 0) + 1 + pad);
        const y1 = Math.min(H, (bb.y1 | 0) + 1 + pad);
        const bw = x1 - x0, bh = y1 - y0;
        if (bw <= 0 || bh <= 0) return;
        ctx.drawImage(originalImage, x0, y0, bw, bh, x0, y0, bw, bh);
      });
    } else {
      ctx.drawImage(originalImage, 0, 0, W, H);
    }
  },

  // Bug C (v1.16.2): кешований offscreen-шар, де original видно ЛИШЕ на
  // пікселях rejected-інстансів (+2px дилатація — стерти cellpose-обводку, що
  // лежить за краєм маски). Rebuild ледачий: лише коли змінилась сигнатура
  // (stem + кількість + сума id) або розмір. Per-frame викликач робить 1 blit.
  _ensureRejectedPatch(hideIds) {
    const s = this.state;
    const cu = s.cleanup;
    const { W, H, originalImage } = s;
    if (!originalImage || !cu.labelsInt32 || !hideIds || !hideIds.size) return null;
    let sum = 0;
    for (const id of hideIds) sum += id;
    const sig = `${s.stem}|${W}x${H}|${hideIds.size}|${sum}`;
    if (cu._rejectedPatch && cu._rejectedPatchSig === sig
        && cu._rejectedPatch.width === W && cu._rejectedPatch.height === H) {
      return cu._rejectedPatch;
    }
    let cv = cu._rejectedPatch;
    if (!cv || cv.width !== W || cv.height !== H) {
      cv = document.createElement("canvas");
      cv.width = W; cv.height = H;
    }
    const octx = cv.getContext("2d");
    octx.clearRect(0, 0, W, H);
    octx.drawImage(originalImage, 0, 0, W, H);
    const labels = cu.labelsInt32;
    const n = W * H;
    const reveal = new Uint8Array(n);
    for (let i = 0; i < n; i++) { const id = labels[i]; if (id > 0 && hideIds.has(id)) reveal[i] = 1; }
    // 4px дилатація (4-сусіди ×4) — cellpose-обводка у overlay товста (~2-3px),
    // 2px не вистачало (юзер: лишалась червона лінія). Легке зачіпання сусідів
    // прийнятне — краще ніж лишати багато червоного (і незрівнянно краще за
    // старий прямокутний bbox).
    for (let pass = 0; pass < 4; pass++) {
      const src = reveal.slice();
      for (let y = 0; y < H; y++) {
        const yW = y * W;
        for (let x = 0; x < W; x++) {
          const i = yW + x;
          if (src[i]) continue;
          if ((x > 0 && src[i - 1]) || (x < W - 1 && src[i + 1])
              || (y > 0 && src[i - W]) || (y < H - 1 && src[i + W])) reveal[i] = 1;
        }
      }
    }
    const img = octx.getImageData(0, 0, W, H);
    const d = img.data;
    for (let i = 0; i < n; i++) { if (!reveal[i]) d[(i << 2) + 3] = 0; }
    octx.putImageData(img, 0, 0);
    cu._rejectedPatch = cv;
    cu._rejectedPatchSig = sig;
    return cv;
  },

  _currentBgSource() {
    const tab = this.state.activeTab;
    if (tab === "cleanup") return this.state.cleanup.bgSource;
    if (tab === "groups")  return this.state.groups.bgSource;
    return this.state.polygons.bgSource;
  },

  setBgSource(src) {
    if (src !== "original" && src !== "overlay") return;
    const tab = this.state.activeTab;
    if (tab === "cleanup")      this.state.cleanup.bgSource = src;
    else if (tab === "groups")  this.state.groups.bgSource = src;
    else                        this.state.polygons.bgSource = src;
    this._refreshBgToggles();
    this._drawBase();
    // QA-2e: marks canvas теж перерендерити, інакше при швидкому open() marks
    // можуть бути порожні (race з _reloadCleanupData), і тільки toggle
    // bg "включити-виключити" їх показував.
    if (tab === "cleanup") this._cleanupRedraw();
  },

  _setBgFromActive() {
    this._refreshBgToggles();
    this._drawBase();
    if (this.state.activeTab === "cleanup") this._cleanupRedraw();
  },

  _refreshBgToggles() {
    const cuSrc = this.state.cleanup.bgSource;
    const pgSrc = this.state.polygons.bgSource;
    const grSrc = this.state.groups.bgSource;
    this._toolbarCleanup().querySelectorAll("[data-bg]").forEach((btn) => {
      btn.classList.toggle("btn--mini-active", btn.dataset.bg === cuSrc);
    });
    this._toolbarPolygons().querySelectorAll("[data-poly-bg]").forEach((btn) => {
      btn.classList.toggle("btn--mini-active", btn.dataset.polyBg === pgSrc);
    });
    const tbGroups = this._toolbarGroups();
    if (tbGroups) {
      tbGroups.querySelectorAll("[data-groups-bg]").forEach((btn) => {
        btn.classList.toggle("btn--mini-active", btn.dataset.groupsBg === grSrc);
      });
    }
  },

  // Від canvas-evt до image-coords (0..W, 0..H).
  // SVG-evt працює так само — бо viewBox=0 0 W H і preserveAspectRatio=xMidYMid meet.
  _canvasCoordsFromEvent(e) {
    const c = this._cBase();
    const rect = c.getBoundingClientRect();
    const scale = Math.min(rect.width / c.width, rect.height / c.height);
    const drawnW = c.width * scale;
    const drawnH = c.height * scale;
    const offX = (rect.width - drawnW) / 2;
    const offY = (rect.height - drawnH) / 2;
    const cx = (e.clientX - rect.left - offX) / scale;
    const cy = (e.clientY - rect.top - offY) / scale;
    if (cx < 0 || cy < 0 || cx >= c.width || cy >= c.height) return null;
    return { x: cx, y: cy };
  },

  // Скільки image-пікселів припадає на 1 CSS-піксель на екрані з урахуванням zoom transform.
  _pxPerCss() {
    const c = this._cBase();
    const rect = c.getBoundingClientRect();
    if (!rect.width || !rect.height) return 1;
    // Quick win #1 (Day 9): canvas має object-fit: contain — бітмап вписаний
    // у елемент із letterbox. Справжній масштаб задає лімітуючий вимір
    // (max з двох співвідношень), як у _canvasCoordsFromEvent. Раніше брали
    // лише ширину → на широкій модалці поріг кліку (вершина/ребро/маркер)
    // занижувався у ~1.7×, через що по тонкій лінії треба було влучати
    // майже піксель-в-піксель.
    return Math.max(c.width / rect.width, c.height / rect.height);
  },
};
