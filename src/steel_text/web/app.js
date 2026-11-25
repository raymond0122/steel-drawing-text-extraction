// The web UI's behaviour: runs the pipeline, draws each step on the page, and the auth API tab.
// index.html holds the markup, style.css the styles.
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const NS = 'http://www.w3.org/2000/svg';
  // Same palette as steel_text/overlay.py, so the UI matches the PNGs in docs/.
  const TILE_COLORS = ['rgb(230,57,70)', 'rgb(29,120,116)', 'rgb(69,123,157)', 'rgb(244,162,97)', 'rgb(131,56,236)', 'rgb(42,157,143)'];
  const C = { merged: '#168c3c', dropped: '#dc2626', label: '#e66e00', other: '#8a94a3', skipped: '#b45309', focus: '#2457d6', notRead: '#c62828' };
  const tileColor = i => TILE_COLORS[((i % TILE_COLORS.length) + TILE_COLORS.length) % TILE_COLORS.length];
  const fmt = n => n.toLocaleString('en-US');
  const dur = ms => (ms === undefined ? '' : ms < 1000 ? `${fmt(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
  const esc = s => String(s).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const BATCH_SIZE_LIVE = '25', BATCH_SIZE_OFFLINE = '10'; // the server's defaults; tests/test_web.py checks they match

  function el(tag, attrs = {}, parent) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  }
  const intersects = (a, b) => Math.min(a[2], b[2]) > Math.max(a[0], b[0]) && Math.min(a[3], b[3]) > Math.max(a[1], b[1]);
  const chunkKey = c => `${c.tile}|${c.bbox.join(',')}|${c.text}`;

  // ------------------------------------------------------------------ top tabs
  function showView(view) {
    document.querySelectorAll('.tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.view === view)));
    $('#view-pipeline').hidden = view !== 'pipeline';
    $('#view-auth').hidden = view !== 'auth';
    history.replaceState(null, '', view === 'auth' ? '#auth' : `#${state.step}`);
    if (view === 'pipeline') { renderLayer(); refreshLiveStatus(); } // a login on the Auth tab unlocks live runs
  }
  document.querySelectorAll('.tabs button').forEach(btn => btn.addEventListener('click', () => showView(btn.dataset.view)));

  // Tokens from the Auth API tab. Declared up here because live runs on the Pipeline tab use the access token.
  const auth = { access: null, refresh: null };

  // ================================================================== PIPELINE
  const state = { data: null, step: 'tiles', vb: null, focus: null, droppedBy: new Map() };
  const svg = $('#page-svg');
  const stage = $('#stage');
  const tooltip = $('#tooltip');

  // ---------- controls
  const mode = () => document.querySelector('input[name="mode"]:checked').value;
  function syncMode() {
    const live = mode() === 'live';
    // Always visible so it can be found; only live mode can read an uploaded page.
    $('#image').disabled = !live;
    $('#image-label').textContent = live ? 'Page image (optional, PNG/JPG)' : 'Page image (Live mode only)';
    $('#fail-wrap').hidden = live;
    $('#ocr-fail-wrap').hidden = live;
    $('#mode-note').hidden = live; // in live mode, the key and login status line below says what matters
    // Left empty, the server uses its default for the mode: DEFAULT_BATCH_SIZE live, OFFLINE_BATCH_SIZE offline.
    $('#batch-size').placeholder = live ? BATCH_SIZE_LIVE : BATCH_SIZE_OFFLINE;
    $('#batch-hint').textContent = `Recommended: ${$('#batch-size').placeholder}`;
    refreshLiveStatus();
  }
  document.querySelectorAll('input[name="mode"]').forEach(r => r.addEventListener('change', syncMode));

  // Live mode: show which keys the server found in .env (names only, never values).
  async function refreshLiveStatus() {
    const box = $('#live-status');
    if (mode() !== 'live') { box.hidden = true; return; }
    try {
      const s = await (await fetch('/api/live-status')).json();
      if (!s.env_file) {
        box.innerHTML = '<span class="pill bad">no .env file</span><span class="muted">Copy <code>.env.example</code> to <code>.env</code> and fill in the keys.</span>';
      } else {
        const keys = [['AZURE_DOCINT_ENDPOINT', s.azure_endpoint], ['AZURE_DOCINT_KEY', s.azure_key], ['OPENAI_API_KEY', s.openai_key]];
        const missing = keys.filter(([, ok]) => !ok).length;
        box.innerHTML = '<span class="muted">Keys in <code>.env</code>:</span>'
          + keys.map(([name, ok]) => `<span class="pill ${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'} ${name}</span>`).join('')
          + `<span class="muted">model ${esc(s.openai_model)}.</span>`
          + (missing ? '<span class="muted">Fill in the missing ones and save; the next run picks them up without a restart.</span>' : '');
      }
      box.insertAdjacentHTML('beforeend', loginStatus());
      box.hidden = false;
    } catch {
      box.hidden = true;
    }
  }

  // Live runs need an access token from the Auth API tab.
  function loginStatus() {
    const user = auth.access && (decodeJwt(auth.access) || {}).sub;
    return user
      ? `<span class="pill ok">✓ logged in as ${esc(user)}</span>`
      : '<span class="pill bad">✗ not logged in</span><span class="muted">Live runs need a login: use <b>Log in</b> at the top right.</span>';
  }
  syncMode();

  $('#controls').addEventListener('submit', e => { e.preventDefault(); run(); });

  async function run() {
    const live = mode() === 'live';
    const params = new URLSearchParams({
      live,
      simulate_ocr_failure: !live && $('#simulate-ocr-failure').checked,
      simulate_llm_failure: !live && $('#simulate-failure').checked,
    });
    // A field left empty is not sent, so the server's default applies.
    if ($('#batch-size').value) params.set('batch_size', $('#batch-size').value);
    if ($('#max-tiles').value) params.set('max_tiles', $('#max-tiles').value);
    if ($('#overlap').value) params.set('overlap', $('#overlap').value);
    const file = live ? $('#image').files[0] : null;
    $('#run-btn').disabled = true;
    $('#loading').hidden = false;
    $('#empty').hidden = true;
    $('#error').hidden = true;
    try {
      const headers = live && auth.access ? { Authorization: `Bearer ${auth.access}` } : {};
      const r = await fetch('/api/run?' + params, { method: 'POST', body: file || undefined, headers });
      const body = await r.json().catch(() => null);
      if (r.status === 401 && live) signedOut(); // expired or refused: show "Log in" again
      if (!r.ok) throw new Error(body && body.detail ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : `HTTP ${r.status}`);
      state.fileName = file ? file.name : null; // names the Excel download
      load(body);
    } catch (err) {
      $('#error').textContent = err.message;
      $('#error').hidden = false;
    } finally {
      $('#run-btn').disabled = false;
      $('#loading').hidden = true;
      $('#empty').hidden = Boolean(state.data);
      if (live) refreshLiveStatus();
    }
  }

  function load(d) {
    const firstLoad = !state.data;
    const samePage = state.data && state.data.page.width === d.page.width && state.data.page.height === d.page.height;
    state.data = d;
    state.focus = null;
    state.droppedBy = new Map(d.dedup.decisions.map(x => [chunkKey(x.dropped), x]));
    stage.style.setProperty('--ar', d.page.width / d.page.height);
    $('#empty').hidden = true;

    svg.replaceChildren();
    // Red 45-degree stripes for tiles that were not read, sized in page pixels.
    const hatch = el('pattern', { id: 'not-read', patternUnits: 'userSpaceOnUse', width: 80, height: 80, patternTransform: 'rotate(45)' }, el('defs', {}, svg));
    el('line', { x1: 0, y1: 0, x2: 0, y2: 80, stroke: C.notRead, 'stroke-width': 24, 'stroke-opacity': 0.4 }, hatch);
    el('image', { href: d.page.preview, x: 0, y: 0, width: d.page.width, height: d.page.height, preserveAspectRatio: 'none' }, svg);
    el('g', { id: 'layer' }, svg);
    if (firstLoad || !samePage) state.vb = { x: 0, y: 0, w: d.page.width, h: d.page.height };
    applyVB();
    renderStats();
    setStep(state.step);
  }

  // ---------- stats
  function xlsxName(d) {
    const base = d.source === 'upload' && state.fileName ? state.fileName.replace(/\.[^.]+$/, '') : 'synthetic_drawing';
    const incomplete = d.tiling.not_read.length || d.llm.batches.some(b => b.error);
    return `${base}_labels${incomplete ? '_incomplete' : ''}.xlsx`;
  }
  const xlsxLink = (d, cls, text) => `<a class="${cls}" href="${d.labels_xlsx}" download="${esc(xlsxName(d))}">${text}</a>`;

  function renderStats() {
    const d = state.data;
    const removed = d.raw.length - d.merged.length;
    const failed = d.llm.batches.filter(b => b.error).length;
    let truth = { v: d.labels.length, s: 'no ground truth for this page', tone: '' };
    if (d.truth_check) {
      const n = d.truth_check.expected.length, found = n - d.truth_check.missing.length;
      truth.s = `${found} of ${n} expected labels found`;
      truth.tone = d.truth_check.missing.length || d.truth_check.unexpected.length ? 'warn' : 'ok';
    }
    const notRead = d.tiling.not_read.length;
    const tiling = d.tiling.overlap ? `${fmt(d.tiling.tile_size)} px, ${d.tiling.overlap} px overlap` : 'page sent whole';
    const cards = [
      { k: 'Tiles', v: d.tiling.tiles.length, s: notRead ? `${notRead} not read: text there is missing` : tiling, tone: notRead ? 'warn' : '' },
      { k: 'OCR lines', v: `${d.raw.length} → ${d.merged.length}`, s: `${removed} removed by dedup` },
      { k: 'LLM batches', v: d.llm.batches.length, s: failed ? `${failed} failed and skipped` : 'all succeeded', tone: failed ? 'warn' : 'ok' },
      { k: 'Steel labels', v: truth.v, s: truth.s, tone: truth.tone, html: xlsxLink(d, 'stat-link', 'Download Excel ↓') },
      { k: `Time (${d.mode})`, v: `${((d.timings_ms.ocr + d.timings_ms.llm) / 1000).toFixed(2)} s`, s: `OCR ${dur(d.timings_ms.ocr)} · LLM ${dur(d.timings_ms.llm)} (details in steps 1 and 4)` },
    ];
    $('#stats').innerHTML = cards.map(c => `
      <div class="card stat ${c.tone || ''}"><div class="k">${esc(c.k)}</div><div class="v">${esc(c.v)}</div><div class="s">${esc(c.s)}</div>${c.html || ''}</div>`).join('');
  }

  // ---------- steps
  document.querySelectorAll('#steps button').forEach(b => b.addEventListener('click', () => setStep(b.dataset.step)));

  function setStep(step) {
    state.step = step;
    state.focus = null;
    history.replaceState(null, '', `#${step}`); // so a step can be linked or reloaded
    document.querySelectorAll('#steps button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.step === step)));
    renderLegend();
    renderPanel();
    renderLayer();
  }

  function renderLegend() {
    const items = {
      tiles: [['Tile outline (colour = tile)', TILE_COLORS[0]]],
      raw: [['OCR line (colour = tile)', TILE_COLORS[1]], ['Tile edge', TILE_COLORS[2], true]],
      dedup: [['Kept', C.merged], ['Dropped', C.dropped, true]],
      labels: [['Steel label', C.label], ['Not a label', C.other], ['In a skipped batch', C.skipped, true]],
    }[state.step];
    if (state.data && state.data.tiling.not_read.length) items.push(['Not read (tile failed)', C.notRead]);
    $('#legend').innerHTML = items.map(([t, c, dash]) => `<span><i class="${dash ? 'dash' : ''}" style="border-color:${c}"></i>${esc(t)}</span>`).join('');
  }

  // ---------- viewer: drawing
  function scale() { return state.vb.w / (svg.clientWidth || 1); } // page px per screen px

  function box(g, b, k, { stroke, width = 2, pad = 4, dash, fill = 'none', fillOpacity, tip, opacity }) {
    const p = pad * k;
    const r = el('rect', {
      x: b[0] - p, y: b[1] - p, width: b[2] - b[0] + 2 * p, height: b[3] - b[1] + 2 * p,
      fill, 'fill-opacity': fillOpacity, stroke, 'stroke-width': width, 'stroke-dasharray': dash,
      'stroke-opacity': opacity, 'vector-effect': 'non-scaling-stroke', 'pointer-events': tip ? 'all' : 'none',
    }, g);
    if (tip) r.dataset.tip = tip;
    return r;
  }

  // A small filled tag with white monospace text. y is the tag's top edge.
  function tag(g, x, y, text, color, k, size = 12) {
    const fs = size * k, h = fs + 6 * k, w = text.length * fs * 0.6 + 8 * k;
    el('rect', { x, y, width: w, height: h, rx: 3 * k, fill: color, 'pointer-events': 'none' }, g);
    const t = el('text', {
      x: x + 4 * k, y: y + h / 2, 'font-size': fs, fill: '#fff', 'dominant-baseline': 'central',
      'font-family': 'ui-monospace, Consolas, Menlo, monospace', 'pointer-events': 'none',
    }, g);
    t.textContent = text;
    return h;
  }
  const tagAbove = (g, b, text, color, k) => tag(g, b[0] - 4 * k, b[1] - 4 * k - (18 * k) - 2 * k, text, color, k);
  const tagBelow = (g, b, text, color, k) => tag(g, b[0] - 4 * k, b[3] + 6 * k, text, color, k);

  // The view width the layer was last drawn for. Box padding and tag sizes depend only on
  // the zoom, so a pan moves the view without drawing the layer again.
  let drawnForWidth = null;

  function renderLayer() {
    const d = state.data;
    const g = $('#layer');
    if (!d || !g || !svg.clientWidth) return;
    g.replaceChildren();
    drawnForWidth = state.vb.w;
    const k = scale();
    const zoomedIn = k < 4;
    const tiles = d.tiling.tiles;

    // On every step, first, so the boxes drawn on top keep their tooltips.
    for (const f of d.tiling.not_read) {
      box(g, f.box, k, {
        stroke: C.notRead, width: 3, pad: 0, fill: 'url(#not-read)',
        tip: `tile ${f.tile} was not read\n${f.error}\nany text only this tile covers is missing`,
      });
      tag(g, f.box[0] + 10 * k, f.box[1] + 10 * k, `tile ${f.tile} not read`, C.notRead, k, 14);
    }

    if (state.step === 'tiles') {
      tiles.forEach((t, i) => box(g, t, k, {
        stroke: tileColor(i), fill: tileColor(i), fillOpacity: 0.11, width: 3, pad: 0,
        tip: `tile ${i}\nx ${fmt(t[0])}–${fmt(t[2])}, y ${fmt(t[1])}–${fmt(t[3])}`,
      }));
      tiles.forEach((t, i) => tag(g, t[0] + 10 * k, t[1] + 10 * k, `tile ${i}`, tileColor(i), k, 14));
    }

    if (state.step === 'raw') {
      tiles.forEach((t, i) => box(g, t, k, { stroke: tileColor(i), width: 1.5, pad: 0, dash: '6 5', opacity: 0.6 }));
      for (const c of d.raw) {
        const dec = state.droppedBy.get(chunkKey(c));
        box(g, c.bbox, k, {
          stroke: tileColor(c.tile), width: 2.5,
          tip: `"${c.text}"  ·  tile ${c.tile}` + (dec ? `\nlater dropped: ${dec.reason}` : ''),
        });
        if (zoomedIn) tagAbove(g, c.bbox, `t${c.tile} ${c.text}`, tileColor(c.tile), k);
      }
    }

    if (state.step === 'dedup') {
      for (const c of d.merged) {
        box(g, c.bbox, k, { stroke: C.merged, width: 2.5, tip: `"${c.text}"  ·  kept (tile ${c.tile})` });
        if (zoomedIn) tagAbove(g, c.bbox, c.text, C.merged, k);
      }
      for (const x of d.dedup.decisions) {
        box(g, x.dropped.bbox, k, {
          stroke: C.dropped, width: 2, pad: 9, dash: '5 4',
          tip: `"${x.dropped.text}" (tile ${x.dropped.tile}) dropped\n${x.reason}\nkept "${x.kept.text}" (tile ${x.kept.tile})`,
        });
        if (zoomedIn) tagBelow(g, x.dropped.bbox, `✕ ${x.dropped.text}`, C.dropped, k);
      }
    }

    if (state.step === 'labels') {
      const skipped = skippedIds();
      const byId = new Map(d.labels.map(l => [l.str_id, l]));
      for (const c of d.merged) {
        if (byId.has(c.id)) continue;
        if (skipped.has(c.id)) {
          box(g, c.bbox, k, { stroke: C.skipped, width: 2, dash: '5 4', tip: `"${c.text}"\nbatch ${batchOf(c.id)} failed, so this line was never checked` });
        } else {
          box(g, c.bbox, k, { stroke: C.other, width: 1.5, opacity: 0.8, tip: `"${c.text}"\nnot a steel label (the model returned nothing for it)` });
        }
      }
      for (const l of d.labels) {
        const fixed = l.label !== l.raw_text.replace(/\s+/g, '').toUpperCase();
        box(g, l.bbox, k, { stroke: C.label, width: 3, tip: `${l.label}  ·  kind ${l.kind}\nOCR read "${l.raw_text}"${fixed ? ' (corrected)' : ''}` });
        tagAbove(g, l.bbox, l.label, C.label, k); // kind is in the tooltip and the table; keeps tags from overlapping
      }
    }

    if (state.focus) box(g, state.focus, k, { stroke: C.focus, width: 3, pad: 16, dash: '8 5' });
  }

  // ---------- viewer: zoom and pan
  function clampVB(v) {
    const { width: W, height: H } = state.data.page;
    const w = Math.min(W, Math.max(W / 60, v.w)), h = w * H / W;
    return { w, h, x: Math.min(Math.max(0, v.x), W - w), y: Math.min(Math.max(0, v.y), H - h) };
  }
  function applyVB() {
    const v = state.vb;
    svg.setAttribute('viewBox', `${v.x} ${v.y} ${v.w} ${v.h}`);
    if (v.w !== drawnForWidth) renderLayer(); // a zoom; a pan keeps the width
  }
  function setVB(v) { state.vb = clampVB(v); applyVB(); }

  let anim = 0;
  function animateTo(target) {
    target = clampVB(target);
    cancelAnimationFrame(anim);
    if (reducedMotion) return setVB(target);
    const from = { ...state.vb }, t0 = performance.now(), dur = 320;
    const step = now => {
      const t = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - t, 3);
      setVB({ x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, w: from.w + (target.w - from.w) * e });
      if (t < 1) anim = requestAnimationFrame(step);
    };
    anim = requestAnimationFrame(step);
  }
  function fit() { if (state.data) animateTo({ x: 0, y: 0, w: state.data.page.width }); }
  function zoomBy(f, cx, cy) {
    const v = state.vb;
    if (cx === undefined) { cx = v.x + v.w / 2; cy = v.y + v.h / 2; }
    const w = v.w * f, h = w * v.h / v.w;
    setVB({ x: cx - (cx - v.x) * (w / v.w), y: cy - (cy - v.y) * (h / v.h), w });
  }
  // Zoom so that box b (plus margin) fills the view, then outline it.
  function focusOn(b) {
    if (!state.data) return;
    const { width: W, height: H } = state.data.page;
    const m = Math.max(250, (b[2] - b[0]) * 0.6);
    let w = b[2] - b[0] + 2 * m, h = b[3] - b[1] + 2 * m;
    w = Math.max(w, h * W / H);
    h = w * H / W;
    state.focus = b;
    renderLayer(); // the outline moves even when the zoom stays the same
    animateTo({ x: (b[0] + b[2]) / 2 - w / 2, y: (b[1] + b[3]) / 2 - h / 2, w });
  }

  function toPage(e) {
    const r = svg.getBoundingClientRect(), v = state.vb;
    return [v.x + (e.clientX - r.left) / r.width * v.w, v.y + (e.clientY - r.top) / r.height * v.h];
  }
  stage.addEventListener('wheel', e => {
    if (!state.data) return;
    e.preventDefault();
    cancelAnimationFrame(anim);
    const [x, y] = toPage(e);
    zoomBy(e.deltaY < 0 ? 0.85 : 1 / 0.85, x, y);
  }, { passive: false });

  let drag = null;
  stage.addEventListener('pointerdown', e => {
    if (!state.data || e.button !== 0) return;
    cancelAnimationFrame(anim);
    drag = { x: e.clientX, y: e.clientY, vb: { ...state.vb } };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add('dragging');
    tooltip.hidden = true;
  });
  stage.addEventListener('pointermove', e => {
    if (drag) {
      const r = svg.getBoundingClientRect();
      setVB({ ...drag.vb, x: drag.vb.x - (e.clientX - drag.x) / r.width * drag.vb.w, y: drag.vb.y - (e.clientY - drag.y) / r.height * drag.vb.h });
      return;
    }
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (!t) { tooltip.hidden = true; return; }
    tooltip.textContent = t.dataset.tip;
    tooltip.hidden = false;
    const sr = stage.getBoundingClientRect();
    let x = e.clientX - sr.left + 14, y = e.clientY - sr.top + 14;
    if (x + tooltip.offsetWidth > sr.width - 4) x = e.clientX - sr.left - tooltip.offsetWidth - 10;
    if (y + tooltip.offsetHeight > sr.height - 4) y = e.clientY - sr.top - tooltip.offsetHeight - 10;
    tooltip.style.left = `${Math.max(4, x)}px`;
    tooltip.style.top = `${Math.max(4, y)}px`;
  });
  const endDrag = () => { drag = null; stage.classList.remove('dragging'); };
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);
  stage.addEventListener('pointerleave', () => { tooltip.hidden = true; });
  stage.addEventListener('dblclick', fit);
  $('#zoom-in').addEventListener('click', () => state.data && zoomBy(0.7));
  $('#zoom-out').addEventListener('click', () => state.data && zoomBy(1 / 0.7));
  $('#zoom-fit').addEventListener('click', () => { state.focus = null; renderLayer(); fit(); });
  new ResizeObserver(() => renderLayer()).observe(svg);

  // ---------- side panel
  const zoomAttr = b => `data-zoom="${b.join(',')}"`;
  $('#panel').addEventListener('click', e => {
    const z = e.target.closest('[data-zoom]');
    if (z) focusOn(z.dataset.zoom.split(',').map(Number));
  });

  function batchOf(id) { return state.data.llm.batches.findIndex(b => b.ids.includes(id)); }
  function skippedIds() { return new Set(state.data.llm.batches.filter(b => b.error).flatMap(b => b.ids)); }

  function renderPanel() {
    const d = state.data, p = $('#panel');
    if (!d) { p.innerHTML = '<p class="muted">No results yet.</p>'; return; }
    p.innerHTML = notReadBanner(d) + { tiles: panelTiles, raw: panelRaw, dedup: panelDedup, labels: panelLabels }[state.step](d);
    if (state.step === 'dedup') p.querySelectorAll('svg.mini').forEach(renderMini);
  }

  // Above every step: a failed tile's text is missing from all of them, not only from step 1.
  function notReadBanner(d) {
    const f = d.tiling.not_read;
    if (!f.length) return '';
    const which = f.map(x => `tile ${x.tile}`).join(', ');
    return `<p class="banner bad">${f.length === 1 ? 'One tile' : `${f.length} tiles`} (${which}) could not be read, so text in the
      hatched area may be missing from every step: only the strips it shares with neighbouring tiles were read.
      The rest of the page is complete. Error: ${esc(f[0].error)}</p>`;
  }

  // Live runs only: the offline simulator takes no real time.
  function ocrTimingNote(d) {
    const tt = d.timings_ms.tiles || [];
    if (d.mode !== 'live' || !tt.length) return '';
    return `<p class="banner info">OCR took ${dur(d.timings_ms.ocr)} for ${tt.length} tiles, read one after another (slowest ${dur(Math.max(...tt))}).
      Most of it is waiting for Azure, which is polled every 2 s. Retries and rate limiting are logged in the server's terminal.</p>`;
  }

  function panelTiles(d) {
    const perTile = d.tiling.tiles.map((_, i) => d.raw.filter(c => c.tile === i).length);
    const tileMs = d.timings_ms.tiles || [];
    return `
      <h2>Step 1 · Tiling</h2>
      <p class="lead">${esc(d.tiling.reason)}</p>
      <p class="note">Overlap bands look darker. Text inside a band is read whole by both tiles, which creates duplicates. Text that crosses a tile edge comes back cut off, which creates fragments.</p>
      <dl class="kv">
        <dt>Page</dt><dd>${fmt(d.page.width)} × ${fmt(d.page.height)} px</dd>
        <dt>Tile size</dt><dd>${fmt(d.tiling.tile_size)} px</dd>
        <dt>Overlap</dt><dd>${d.tiling.overlap} px</dd>
        <dt>Source</dt><dd>${d.source === 'synthetic' ? 'synthetic drawing' : 'uploaded image'}</dd>
      </dl>
      ${ocrTimingNote(d)}
      <h3>Tiles</h3>
      <ul class="list">${d.tiling.tiles.map((t, i) => {
        const failed = d.tiling.not_read.find(f => f.tile === i);
        const result = failed
          ? `<span class="pill bad" title="${esc(failed.error)}">not read</span>`
          : `<span class="muted">${perTile[i]} line${perTile[i] === 1 ? '' : 's'} · ${dur(tileMs[i])}</span>`;
        return `
        <li ${zoomAttr(t)}><span class="swatch" style="background:${tileColor(i)}"></span>
          <span class="grow">tile ${i} <span class="muted mono">${t[0]},${t[1]} → ${t[2]},${t[3]}</span></span>
          ${result}</li>`;
      }).join('')}
      </ul>`;
  }

  function panelRaw(d) {
    const dropped = d.raw.filter(c => state.droppedBy.has(chunkKey(c))).length;
    return `
      <h2>Step 2 · Raw OCR</h2>
      <p class="lead">Each tile is read on its own. Boxes come back in tile coordinates and are shifted by the tile's offset onto the page.</p>
      <p class="note">${d.raw.length} lines from ${d.tiling.tiles.length} tiles. ${dropped} of them are duplicates or fragments that the next step removes. Lines shorter than 2 characters are skipped.</p>
      <ul class="list">${d.raw.map(c => {
        const dec = state.droppedBy.get(chunkKey(c));
        return `<li ${zoomAttr(c.bbox)} class="${dec ? 'dropped' : ''}" title="${dec ? esc('dropped: ' + dec.reason) : ''}">
          <span class="chip" style="--c:${tileColor(c.tile)}">t${c.tile}</span>
          <span class="grow mono">${esc(c.text)}</span>
          ${dec ? '<span class="pill bad">dropped</span>' : ''}</li>`;
      }).join('')}</ul>`;
  }

  function panelDedup(d) {
    const cfg = d.dedup.config;
    const decisions = d.dedup.decisions.map((x, i) => `
      <div class="decision" ${zoomAttr(x.region)}>
        <div class="decision-head"><span class="num">${i + 1}</span>${esc(x.reason)}</div>
        <div class="ba">
          <figure><svg class="mini" data-i="${i}" data-when="before"></svg><figcaption>Before (colour = tile)</figcaption></figure>
          <span class="arrow" aria-hidden="true">→</span>
          <figure><svg class="mini" data-i="${i}" data-when="after"></svg><figcaption>After</figcaption></figure>
        </div>
        <p class="decision-text">Dropped <code>${esc(x.dropped.text)}</code><span class="chip" style="--c:${tileColor(x.dropped.tile)}">t${x.dropped.tile}</span>
          kept <code>${esc(x.kept.text)}</code><span class="chip" style="--c:${tileColor(x.kept.tile)}">t${x.kept.tile}</span></p>
      </div>`).join('');
    return `
      <h2>Step 3 · Merge and dedup</h2>
      <p class="lead">Only lines from two different tiles, inside the area both tiles cover, are compared; lines from one tile are always kept. Such a pair is merged when both say the same thing in the same place (IoU ≥ ${cfg.iou}), or when one box covers ≥ ${cfg.overlap_min} of the smaller one, so the dropped reading lies (almost) inside the kept one.</p>
      <p class="note">${d.raw.length} → ${d.merged.length} lines. The longest text is processed first, so a full string is kept before its fragments come up. Click a card to zoom to it.</p>
      ${decisions || '<p class="banner info">Nothing to merge on this page.</p>'}`;
  }

  function renderMini(s) {
    const d = state.data, x = d.dedup.decisions[Number(s.dataset.i)], before = s.dataset.when === 'before';
    const [x0, y0, x1, y1] = x.region, w = x1 - x0, h = y1 - y0;
    s.setAttribute('viewBox', `${x0} ${y0} ${w} ${h}`);
    s.style.aspectRatio = `${w} / ${h}`;
    el('image', { href: x.crop, x: x0, y: y0, width: w, height: h }, s);
    const k = w / (s.clientWidth || 150);
    for (const c of before ? d.raw : d.merged) {
      if (intersects(c.bbox, x.region)) box(s, c.bbox, k, { stroke: before ? tileColor(c.tile) : C.merged, width: 2, pad: 3 });
    }
  }

  function panelLabels(d) {
    const anyFailed = d.llm.batches.some(b => b.error);
    const batches = d.llm.batches.map(({ ids, error }, b) => {
      const lo = ids[0], hi = ids[ids.length - 1];
      const range = lo === hi ? `line ${lo}` : `lines ${lo}–${hi}`;
      const n = d.labels.filter(l => ids.includes(l.str_id)).length;
      const ms = dur((d.timings_ms.llm_batches || [])[b]);
      return error
        ? `<li title="${esc(error)}"><span class="pill bad">failed</span><span class="grow">Batch ${b} <span class="muted">· ${range} · ${ms} · ${esc(error)}</span></span><span class="muted">skipped</span></li>`
        : `<li><span class="pill ok">ok</span><span class="grow">Batch ${b} <span class="muted">· ${range} · ${ms}</span></span><span class="muted">${n} label${n === 1 ? '' : 's'}</span></li>`;
    }).join('');

    let banner = '<p class="banner info">No ground truth for this page.</p>';
    if (d.truth_check) {
      const t = d.truth_check, found = t.expected.length - t.missing.length;
      if (!t.missing.length && !t.unexpected.length) banner = `<p class="banner ok">All ${t.expected.length} expected labels found, nothing extra.</p>`;
      else banner = `<p class="banner warn">${found} of ${t.expected.length} expected labels found.`
        + (t.missing.length ? ` Missing: ${t.missing.map(esc).join(', ')}.` : '')
        + (t.unexpected.length ? ` Unexpected: ${t.unexpected.map(esc).join(', ')}.` : '')
        + (anyFailed ? ' A skipped batch is never checked, so its labels are lost; the rest of the page is still processed.' : '')
        + '</p>';
    }
    if (!d.llm.ok) banner += '<p class="banner warn">Every batch failed, so the page is marked as failed.</p>';

    const rows = d.labels.map(l => {
      const fixed = l.label !== l.raw_text.replace(/\s+/g, '').toUpperCase();
      return `<tr ${zoomAttr(l.bbox)}><td class="mono">${esc(l.label)}</td><td><span class="kind">${esc(l.kind)}</span></td>
        <td class="mono">${esc(l.raw_text)}${fixed ? '<span class="fixed">corrected</span>' : ''}</td></tr>`;
    }).join('');

    return `
      <h2>Step 4 · LLM label filter</h2>
      <p class="lead">Merged lines go to the model in batches (${d.llm.batches.length} on this page). It returns only an ID, a normalized label and a kind. Boxes and raw text are joined back from the OCR results by ID, so the model cannot invent a location.</p>
      ${banner}
      <h3>Batches</h3>
      <ul class="list">${batches}</ul>
      <div class="section-head"><h3>Labels (${d.labels.length})</h3>${xlsxLink(d, 'btn small', 'Download Excel')}</div>
      ${rows ? `<table class="labels"><thead><tr><th>Label</th><th>Kind</th><th>OCR read</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="muted">No labels.</p>'}
      <p class="note" style="margin-top:12px">The Excel file has one column, Profile, with one row per label found. It is a label list, not a bill of materials: lengths and quantities would need member geometry and label-to-member linking, which this repository does not include.</p>
      ${d.mode === 'offline' ? '<p class="note" style="margin-top:12px">Offline mode uses a rule-based stand-in with the same output format as the LLM.</p>' : ''}`;
  }

  const fromHash = location.hash.slice(1);
  if (['tiles', 'raw', 'dedup', 'labels'].includes(fromHash)) state.step = fromHash;
  renderPanel();
  run();
  if (fromHash === 'auth') showView('auth');
  addEventListener('hashchange', () => {
    const h = location.hash.slice(1);
    if (h === 'auth') showView('auth');
    else if (['tiles', 'raw', 'dedup', 'labels'].includes(h)) { showView('pipeline'); setStep(h); }
  });

  // ================================================================== AUTH
  const creds =() => ({ username: $('#au-user').value, password: $('#au-pass').value });

  function decodeJwt(token) {
    try {
      const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      return JSON.parse(atob(part + '==='.slice((part.length + 3) % 4)));
    } catch { return null; }
  }
  const shortToken = t => (t.length > 40 ? `${t.slice(0, 24)}…${t.slice(-8)}` : t);

  function renderTokens() {
    const rows = [['Access token', auth.access], ['Refresh token', auth.refresh]].filter(([, t]) => t).map(([name, t]) => {
      const p = decodeJwt(t) || {};
      const exp = p.exp ? new Date(p.exp * 1000) : null;
      const mins = exp ? Math.round((exp - Date.now()) / 60000) : null;
      const left = mins === null ? '' : mins >= 120 ? `${Math.round(mins / 60 / 24 * 10) / 10} days` : `${mins} min`;
      return `<div class="token"><div class="head">${name}<span class="pill ${p.type === 'access' ? 'ok' : 'warn'}">type: ${esc(p.type)}</span></div>
        <dl class="kv"><dt>sub</dt><dd class="mono">${esc(p.sub)}</dd><dt>expires</dt><dd>${exp ? esc(exp.toLocaleString()) + ` (in ${left})` : '—'}</dd></dl>
        <code class="raw">${esc(shortToken(t))}</code></div>`;
    });
    $('#tokens').innerHTML = rows.join('') || '<p class="muted">Log in to get an access token and a refresh token.</p>';
  }

  function logRequest(method, path, status, body, note) {
    const shown = JSON.stringify(body, (k, v) => (typeof v === 'string' && k.endsWith('_token') ? shortToken(v) : v), 2);
    const tone = status < 300 ? 'ok' : status < 500 ? 'bad' : 'warn';
    const li = document.createElement('li');
    li.className = 'req';
    li.innerHTML = `<div class="req-head"><span class="method">${method}</span><code>${esc(path)}</code><span class="pill ${tone} status">${status}</span></div>
      ${note ? `<p>${esc(note)}</p>` : ''}<pre>${esc(shown)}</pre>`;
    $('#auth-log').prepend(li);
    $('#log-empty').hidden = true;
  }

  async function call(method, path, { body, token } = {}, note) {
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = `Bearer ${token}`;
    let status = 0, json = null;
    try {
      const r = await fetch('/auth-api' + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
      status = r.status;
      json = await r.json().catch(() => null);
    } catch (err) {
      json = { error: err.message };
    }
    logRequest(method, path, status, json, note);
    return { status, json };
  }

  const ACTIONS = {
    register: () => call('POST', '/auth/register', { body: creds() },
      'Creates the user. The password is stored as a PBKDF2-SHA256 hash. 409 if the name is taken, 422 if the password is under 8 characters.'),
    login: async () => {
      const r = await call('POST', '/auth/login', { body: creds() },
        'Returns an access token (60 min) and a refresh token (7 days). An unknown user and a wrong password get the same 401.');
      if (r.status === 200) signedIn(r.json);
    },
    'me-access': () => call('GET', '/me', { token: auth.access },
      auth.access ? 'Protected route: a valid access token is required.' : 'No access token yet. Log in first; this call is expected to fail.'),
    'me-refresh': () => call('GET', '/me', { token: auth.refresh },
      auth.refresh ? 'Expected 401: this token\'s type claim is "refresh", so it is not accepted as an access token.' : 'No refresh token yet. Log in first.'),
    'me-none': () => call('GET', '/me', {}, 'Expected 401: no bearer token.'),
    refresh: async () => {
      const r = await call('POST', '/auth/refresh', { body: { refresh_token: auth.refresh || '' } },
        auth.refresh ? 'Swaps the refresh token for a new access token.' : 'No refresh token yet. Log in first; this call is expected to fail.');
      if (r.status === 200) signedIn(r.json);
    },
  };
  document.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => ACTIONS[b.dataset.act]()));

  // ---------- account (top right): the login that live runs need
  function signedIn(json) { // after a login, or a refresh (which returns only a new access token)
    auth.access = json.access_token;
    if (json.refresh_token) auth.refresh = json.refresh_token;
    renderTokens(); renderAccount(); refreshLiveStatus();
  }
  function signedOut() {
    auth.access = auth.refresh = null;
    renderTokens(); renderAccount(); refreshLiveStatus();
  }
  function renderAccount() {
    const user = auth.access && (decodeJwt(auth.access) || {}).sub;
    $('#login-open').hidden = Boolean(user);
    $('#who').hidden = $('#logout').hidden = !user;
    $('#who').textContent = user ? `✓ ${user}` : '';
    if (user) $('#login-pop').hidden = true;
  }
  const errorText = r => (typeof (r.json || {}).detail === 'string' ? r.json.detail
    : r.status === 422 ? 'Enter a username and a password of at least 8 characters.' : `HTTP ${r.status}`);

  async function topBarLogin(register) {
    const body = { username: $('#lg-user').value, password: $('#lg-pass').value };
    const err = $('#lg-err');
    err.hidden = true;
    if (register) { // a taken username (409) is fine: just log in
      const r = await call('POST', '/auth/register', { body }, 'Register, from the top bar.');
      if (r.status !== 201 && r.status !== 409) { err.textContent = errorText(r); err.hidden = false; return; }
    }
    const r = await call('POST', '/auth/login', { body }, 'Log in, from the top bar.');
    if (r.status === 200) signedIn(r.json);
    else { err.textContent = errorText(r); err.hidden = false; }
  }
  $('#login-open').addEventListener('click', () => {
    const pop = $('#login-pop');
    pop.hidden = !pop.hidden;
    $('#login-open').setAttribute('aria-expanded', String(!pop.hidden));
    if (!pop.hidden) $('#lg-user').focus();
  });
  $('#login-pop').addEventListener('submit', e => { e.preventDefault(); topBarLogin(false); });
  $('#lg-register').addEventListener('click', () => topBarLogin(true));
  $('#logout').addEventListener('click', signedOut);
  addEventListener('keydown', e => { if (e.key === 'Escape') $('#login-pop').hidden = true; });
  renderAccount();
  $('#clear-log').addEventListener('click', () => { $('#auth-log').replaceChildren(); $('#log-empty').hidden = false; });
})();
