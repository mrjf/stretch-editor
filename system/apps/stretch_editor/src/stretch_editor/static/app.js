import { Stage, bindSurface } from './stage.js';
import { renderTweet } from './tweet.js';
import { api } from './api.js';
import { PROSE_LIMITS, POETRY_LIMITS, METER_NAME, METER_SYLLABLES, UNITS, catalog, setCatalog, UNIT_LABEL, describe, measure, proseCounts, shortLabel, sizeOf, toRows } from './textkit.js';

const $ = (id) => document.getElementById(id);
const PERSON =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M8 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm-5 6.5C3 11.6 5.2 10 8 10s5 1.6 5 4.5a.5.5 0 0 1-.5.5h-9a.5.5 0 0 1-.5-.5Z"/></svg>';
const LOCK =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M5 7V5a3 3 0 0 1 6 0v2h.5A1.5 1.5 0 0 1 13 8.5v5A1.5 1.5 0 0 1 11.5 15h-7A1.5 1.5 0 0 1 3 13.5v-5A1.5 1.5 0 0 1 4.5 7H5Zm1.5 0h3V5a1.5 1.5 0 0 0-3 0v2Z"/></svg>';
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

// Settings kept in this browser.
const settings = { showCosts: localStorage.getItem('stretch-editor:show-costs') === '1' };

const state = {
  doc: null,
  model: localStorage.getItem('stretch-editor:model'),
  spec: { prose: null, poetry: null },
  editing: false,
  shownKey: null,
  requested: new Set()
};

const stage = new Stage($('stage'));
const mode = () => state.doc.mode;
const spec = () => state.spec[mode()];
const vkey = (m = mode(), s = spec()) => api.versionKey(m, s);
// The model whose versions are on screen.
const model = () => (state.doc.models.includes(state.model) ? state.model : state.doc.models[0]);
const modelLabel = (id) => state.doc.available_models.find((m) => m.id === id)?.label || id || 'you';
// What a size shows: its locked text if locked, else the shown model's version, else the author's unlocked text.
function lookup(key) {
  const own = state.doc.versions[key];
  if (own?.status === 'locked') return own;
  return state.doc.versions[key + '@' + model()] || own;
}
const version = (m = mode(), s = spec()) => lookup(vkey(m, s));
const modeVersions = (m = mode()) => {
  const keys = new Set(Object.keys(state.doc.versions).map((k) => k.split('@')[0]).filter((k) => k.startsWith(m + ':')));
  return [...keys].map(lookup).filter((v) => v?.text);
};
const steps = () => state.doc.ladder[mode()] || [];
const jobKey = (key) => key + '@' + model();
const isWriting = (key) => state.doc.writing.includes(jobKey(key));
const isQueued = (key) => state.doc.queued.includes(jobKey(key));
const plainSpec = (s) => {
  const { mode: _m, ...rest } = s;
  for (const k of Object.keys(rest)) if (rest[k] === null || rest[k] === undefined) delete rest[k];
  return rest;
};

// Who wrote a version, in words: the author, a model, or both.
function authorship(v) {
  const share = v.human_share ?? 0;
  if (share >= 0.995) return { cls: 'human', text: `${PERSON} Written by you` };
  if (share > 0.005) return { cls: 'mixed', text: `${PERSON} Edited by you · ${Math.round(share * 100)}% yours` };
  return { cls: 'model', text: `Written by ${escapeHtml(modelLabel(v.model))}` };
}

// The version the author wrote the most of, for the "Most human" pill.
function mostHuman() {
  const all = Object.values(state.doc.versions).filter((v) => v.spec.mode === mode() && v.text && (v.human_share ?? 0) > 0);
  return all.sort((a, b) => b.human_share - a.human_share || b.text.length - a.text.length)[0];
}

// ---------- saving settings (the page owns title, mode, brief, notes and pills) ----------

const pending = {};
let saveTimer;
function saveMeta(fields, delay = 500) {
  Object.assign(pending, fields);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushMeta, delay);
}
async function flushMeta() {
  clearTimeout(saveTimer);
  const fields = { ...pending };
  for (const k of Object.keys(pending)) delete pending[k];
  if (!Object.keys(fields).length) return;
  try {
    merge(await api.updateDoc(state.doc.id, fields));
  } catch (e) {
    flash(`Couldn't save: ${e.message}`);
  }
}

// A server copy replaces versions and job state; settings typed since the last save stay as typed.
function merge(server) {
  if (!state.doc || server.id !== state.doc.id) return;
  const local = state.doc;
  state.doc = {
    ...server,
    title: 'title' in pending ? local.title : server.title,
    mode: local.mode,
    brief: 'brief' in pending ? local.brief : server.brief,
    notes: 'notes' in pending ? local.notes : server.notes,
    pills: 'pills' in pending ? local.pills : server.pills,
    models: 'models' in pending ? local.models : server.models,
    fill: 'fill' in pending ? local.fill : server.fill
  };
  for (const key of [...state.requested]) if (lookup(key)?.text && !lookup(key).stale) state.requested.delete(key);
  // A title the editor chose (from what you wrote) shows up here and in the document menu.
  if (!('title' in pending) && document.activeElement !== $('doc-title') && $('doc-title').value !== state.doc.title) {
    $('doc-title').value = state.doc.title;
    const opt = $('doc-select').querySelector(`option[value="${state.doc.id}"]`);
    if (opt) opt.textContent = state.doc.title || 'Untitled';
  }
  if (!state.editing) render(true);
  if (tweetState.open) drawTweet();
}

async function poll() {
  if (!state.doc || document.hidden) return;
  try {
    merge(await api.getDoc(state.doc.id));
  } catch {}
}
setInterval(poll, 1500);

function flash(text) {
  const el = $('flash');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(flash.t);
  flash.t = setTimeout(() => (el.hidden = true), 5000);
}

// ---------- documents ----------

async function loadDocList(selectId) {
  const list = await api.listDocs();
  $('doc-select').innerHTML = list.map((d) => `<option value="${d.id}">${escapeHtml(d.title || 'Untitled')}</option>`).join('');
  if (selectId) $('doc-select').value = selectId;
  return list;
}

async function openDoc(id) {
  await flushMeta();
  state.doc = await api.getDoc(id);
  state.shownKey = null;
  state.requested.clear();
  for (const m of ['prose', 'poetry']) state.spec[m] = startingSpec(m);
  $('doc-title').value = state.doc.title;
  $('brief').value = state.doc.brief || '';
  localStorage.setItem('stretch-editor:doc', id);
  if (state.editing) closeEditor();
  render(false);
  if (!Object.values(state.doc.versions).some((v) => v.status === 'locked')) openEditor();
}

function startingSpec(m) {
  const vs = Object.values(state.doc.versions).filter((v) => v.spec.mode === m && v.text);
  const locked = vs.filter((v) => v.status === 'locked');
  const pick = (locked.length ? locked : vs).sort((a, b) => sizeOf(plainSpec(b.spec), m) - sizeOf(plainSpec(a.spec), m))[0];
  if (pick) return plainSpec(pick.spec);
  return m === 'prose' ? { unit: 'paragraphs', value: 2 } : { lines: 8, syllables: 10 };
}

// ---------- moving around ----------

function go(next, { fromInput = false } = {}) {
  // Moving by any other means (scroll, pills, slider stops) lets a number box show the new value.
  if (!fromInput && document.activeElement?.classList.contains('ctl-num')) document.activeElement.blur();
  state.spec[mode()] = plainSpec({ ...next, mode: mode() });
  render(true);
  if (!state.editing) requestCurrent();
}

let requestTimer;
function requestCurrent() {
  clearTimeout(requestTimer);
  const v = version();
  if (v?.text && !v.stale && v.status !== 'error') return;
  const m = mode(),
    s = { ...spec() },
    key = vkey();
  if (isWriting(key) && !v?.stale) return;
  state.requested.add(key);
  requestTimer = setTimeout(async () => {
    try {
      merge(await api.write(state.doc.id, m, s, false, model()));
    } catch (e) {
      flash(`Couldn't ask for that size: ${e.message}`);
    }
  }, 250);
}

// ---------- rendering ----------

function render(animate) {
  renderMode();
  renderFillSettings();
  renderModels();
  renderPills();
  renderSliders();
  renderStage(animate);
  renderStatus();
  renderActions();
  renderBackground();
  renderVersions();
  renderZoom();
  renderGrid();
  renderNotes();
}

function renderModels() {
  const shown = model();
  $('model-switch').innerHTML = state.doc.models
    .map((id) => `<button data-model="${id}" class="${id === shown ? 'active' : ''}">${escapeHtml(modelLabel(id))}</button>`)
    .join('');
  const picker = $('model-list');
  if (!picker.contains(document.activeElement))
    picker.innerHTML = [...new Set(state.doc.available_models.map((m) => m.provider))]
      .map(
        (provider) =>
          `<h4>${escapeHtml(provider)}</h4>` +
          state.doc.available_models
            .filter((m) => m.provider === provider)
            .map((m) => {
              const spent = state.doc.spent_by_model[m.id];
              const note = !settings.showCosts ? '' : m.billed === 'subscription' ? 'plan' : spent ? '$' + spent.toFixed(2) : '';
              return `<label class="model-row" title="${escapeHtml(m.description)}"><input type="checkbox" data-pick="${m.id}" ${state.doc.models.includes(m.id) ? 'checked' : ''}>
        <span>${escapeHtml(m.label)}</span><em>${note}</em></label>`;
            })
            .join('')
      )
      .join('');
}

function renderFillSettings() {
  for (const m of ['prose', 'poetry']) {
    const sel = $(`fill-${m}`);
    if (document.activeElement !== sel) sel.value = state.doc.fill?.[m] || 'stops';
  }
  const per = ['prose', 'poetry'].map((m) => (state.doc.planned?.[m] || []).length);
  const n = state.doc.models.length;
  $('fill-count').textContent = `Keeps ${per[0]} prose and ${per[1]} poetry sizes written, for each of ${n} model${n === 1 ? '' : 's'}.`;
}

function renderMode() {
  for (const b of document.querySelectorAll('#mode-prose, #mode-poetry')) b.classList.toggle('active', b.dataset.mode === mode());
}

function renderStage(animate) {
  const v = version();
  const key = vkey();
  const hasText = Boolean(v?.text);
  const busy = isWriting(key) || isQueued(key) || state.requested.has(key);
  $('writing-badge').hidden = hasText || !busy;
  $('writing-text').textContent = isWriting(key) ? 'Writing this version…' : 'Up next…';
  $('stage').classList.toggle('pending', !hasText);
  const full = modeVersions()
    .filter((x) => x.status === 'locked')
    .sort((a, b) => b.text.length - a.text.length)[0];
  if (full) stage.setReference(full.text.length);
  if (hasText && state.shownKey !== key + '|' + v.text) {
    stage.show(toRows(v.text, mode()), animate && state.shownKey !== null);
    state.shownKey = key + '|' + v.text;
  } else if (!hasText && !state.shownKey) {
    stage.show({ texts: [''], keys: ['p0'], breaks: [], mode: mode() }, false);
  }
}

function renderStatus() {
  const v = version();
  const s = spec();
  const key = vkey();
  const parts = [];
  if (v?.text) {
    const m = measure(v.text, mode());
    if (mode() === 'prose')
      parts.push(
        `${m.paragraphs} ¶`,
        `${m.sentences} sentences`,
        `${m.words} words`,
        `<span class="${s.chars && m.chars > s.chars ? 'over' : ''}">${m.chars}${s.chars ? ' / ' + s.chars : ''} characters</span>`
      );
    else parts.push(`${m.lines} lines`, `${m.perLine.toFixed(1)} syllables per line`, `${m.words} words`);
    const who = authorship(v);
    if (v.status === 'locked') parts.push(`<span class="tag locked">${LOCK} Locked</span>`, `<span class="tag ${who.cls}">${who.text}</span>`);
    else if (v.status === 'error') parts.push(`<span class="tag error">Couldn't rewrite: ${escapeHtml(v.error || 'unknown error')}</span>`);
    else if (v.stale) parts.push(`<span class="tag stale"><span class="spinner tiny"></span>Updating to match your changes</span>`);
    else parts.push(`<span class="tag ${who.cls}">${who.text}</span>`);
    if (v.log_id) parts.push(`<button class="link" id="show-log" data-log="${v.log_id}">How this was written</button>`);
  } else if (v?.status === 'error' && (v.error || '').startsWith('Nothing to write from')) {
    parts.push(`<span class="tag stale">${escapeHtml(v.error)}</span>`);
  } else if (v?.status === 'error') {
    parts.push(`<span class="tag error">Couldn't write this size: ${escapeHtml(v.error || 'unknown error')}</span>`);
    if (v.log_id) parts.push(`<button class="link" id="show-log" data-log="${v.log_id}">Details</button>`);
  } else if (isWriting(key)) parts.push('Writing…');
  else if (isQueued(key) || state.requested.has(key)) parts.push('Starting…');
  else parts.push('Not written yet');
  $('status').innerHTML = parts.join('<span class="sep">·</span>');
}

function renderActions() {
  const v = version();
  const locked = v?.status === 'locked';
  $('act-lock').innerHTML = locked ? 'Unlock' : `${LOCK} Lock`;
  $('act-lock').disabled = !v?.text;
  $('act-rewrite').disabled = !v?.text || locked || isWriting(vkey());
  $('act-edit').textContent = v?.text ? 'Edit' : 'Write it myself';
  $('act-tweet').classList.toggle('primary', Boolean(v?.text && v.text.length <= 280));
}

function renderBackground() {
  const sizes = ['prose', 'poetry'].flatMap((m) => (m === mode() || modeVersions(m).length ? state.doc.planned[m] || [] : []));
  const all = sizes.flatMap((k) => (state.doc.versions[k]?.status === 'locked' ? [k] : state.doc.models.map((id) => k + '@' + id)));
  const fresh = all.filter((k) => {
    const v = state.doc.versions[k];
    return v?.text && (v.status === 'locked' || !v.stale);
  }).length;
  const writing = state.doc.writing.length;
  const waiting = state.doc.queued.length;
  const hasSource = Object.values(state.doc.versions).some((v) => v.status === 'locked');
  let text;
  if (!hasSource) text = 'Write and lock a first version; every other size is written from it.';
  else if (writing || waiting)
    text = `${fresh} of ${all.length} sizes ready. Writing ${writing}${waiting ? `, ${waiting} waiting` : ''}.`;
  else if (!all.length) text = 'Sizes are written when you open them. Change this in settings.';
  else text = `All ${all.length} sizes are ready. They're rewritten automatically when you change a locked version or the brief.`;
  $('bg-text').textContent = text;
  $('bg-progress').hidden = !(writing || waiting);
  $('bg-progress').firstElementChild.style.width = (100 * fresh) / Math.max(1, all.length) + '%';
  $('bg-cost').textContent =
    settings.showCosts && state.doc.spent_usd ? `Model cost for this document so far: $${state.doc.spent_usd.toFixed(2)}` : '';
}

// One size control: a slider and a number box showing the same value. Wide ranges use a
// logarithmic slider so small values stay easy to hit.
const LOG_DIMS = new Set(['chars', 'words', 'sentences']);
const toPos = (dim, v, [lo, hi]) => (LOG_DIMS.has(dim) ? Math.round((1000 * Math.log(v / lo)) / Math.log(hi / lo)) : v);
const fromPos = (dim, p, [lo, hi]) => (LOG_DIMS.has(dim) ? Math.round(lo * Math.exp((p / 1000) * Math.log(hi / lo))) : p);
const limitsOf = (dim) => PROSE_LIMITS[dim] || POETRY_LIMITS[dim];

function sliderRow(dim, label) {
  const [lo, hi] = limitsOf(dim);
  const range = LOG_DIMS.has(dim) ? 'min="0" max="1000"' : `min="${lo}" max="${hi}"`;
  return `<div class="ctl" data-ctl="${dim}">
    <div class="ctl-head"><span class="ctl-label">${label}</span>
      <input type="number" class="ctl-num" data-dim="${dim}" data-kind="num" min="${lo}" max="${hi}"></div>
    <input type="range" data-dim="${dim}" data-kind="range" ${range}>
  </div>`;
}

function renderSliders() {
  const s = spec();
  const host = $('sliders');
  if (host.dataset.mode !== mode()) {
    host.dataset.mode = mode();
    host.innerHTML =
      mode() === 'prose'
        ? '<p class="hint ctl-hint">Set the length in any one of these. The one you set is in charge; the others show the counts of the text on screen.</p>' +
          UNITS.map((u) => sliderRow(u, UNIT_LABEL[u])).join('')
        : sliderRow('lines', 'Lines') + sliderRow('syllables', 'Syllables per line');
  }
  // Prose: the unit in charge shows its target; the others show the text's actual counts.
  const v = version();
  const counts = mode() === 'prose' && v?.text ? proseCounts(v.text) : {};
  // Poetry: a form fixes both, a meter fixes the line length; those show the text's real counts.
  const verse = mode() === 'poetry' && v?.text ? measure(v.text, 'poetry') : null;
  const verseCounts = verse ? { lines: verse.lines, syllables: Math.round(verse.perLine) } : {};
  for (const ctl of host.querySelectorAll('[data-ctl]')) {
    const dim = ctl.dataset.ctl;
    const fixed = mode() === 'poetry' && (s.form || (s.meter && dim === 'syllables'));
    const active = mode() === 'poetry' ? !fixed : s.unit === dim;
    const value =
      mode() === 'poetry' ? (fixed ? verseCounts[dim] ?? s[dim] : s[dim]) : active ? s.value : counts[dim];
    ctl.classList.toggle('active', mode() === 'prose' && active);
    ctl.classList.toggle('readout', !active);
    const num = ctl.querySelector('[data-kind="num"]');
    const range = ctl.querySelector('[data-kind="range"]');
    if (document.activeElement !== num) num.value = value ?? '';
    if (value) range.value = toPos(dim, Math.max(limitsOf(dim)[0], value), limitsOf(dim));
  }
}

function pillIcon(key) {
  const v = lookup(key);
  if (isWriting(key)) return '<span class="spinner small"></span>';
  if (v?.status === 'locked') return LOCK;
  if (v?.text && v.stale) return '<span class="dot stale"></span>';
  if (v?.text) return '<span class="dot"></span>';
  return '<span class="dot hollow"></span>';
}

function renderPills() {
  const host = $('pills');
  if (host.querySelector('#pill-name:not([hidden])')) return;
  const cur = vkey();
  const pills = state.doc.pills.filter((p) => p.spec.mode === mode());
  const human = mostHuman();
  const humanKey = human && api.versionKey(mode(), plainSpec(human.spec));
  host.innerHTML =
    (human
      ? `<span class="pill human ${humanKey === cur ? 'active' : ''}" title="The version you wrote the most of (${Math.round(human.human_share * 100)}% yours)">
          <button class="pill-go" data-human="1">${PERSON}Most human</button></span>`
      : '') +
    pills
      .map((p) => {
        const key = api.versionKey(mode(), plainSpec(p.spec));
        return `<span class="pill ${key === cur ? 'active' : ''}" title="${escapeHtml(describe(plainSpec(p.spec), mode()))}">
          <button class="pill-go" data-pill="${p.id}">${pillIcon(key)}${escapeHtml(p.label)}</button>
          <button class="pill-x" data-remove="${p.id}" aria-label="Remove ${escapeHtml(p.label)}">×</button>
        </span>`;
      })
      .join('') +
    `<span class="pill-add"><button id="pill-add" class="ghost">+ Save this size</button>
      <input id="pill-name" placeholder="Name this size" hidden></span>`;
}

function renderVersions() {
  const cur = vkey();
  const vs = modeVersions().sort((a, b) => sizeOf(plainSpec(a.spec), mode()) - sizeOf(plainSpec(b.spec), mode()));
  $('versions').innerHTML = vs
    .map((v) => {
      const s = plainSpec(v.spec);
      const key = api.versionKey(mode(), s);
      const icon = isWriting(key) ? '<span class="spinner small"></span>' : v.status === 'locked' ? LOCK : `<span class="dot ${v.stale ? 'stale' : ''}"></span>`;
      const mark = (v.human_share ?? 0) > 0.005 ? `<span class="v-human" title="${Math.round(v.human_share * 100)}% yours">${PERSON}</span>` : '';
      return `<li class="${key === cur ? 'active' : ''}" data-key="${key}">
          <span class="v-icon">${icon}</span>
          <span class="v-body"><span class="v-label">${escapeHtml(shortLabel(s, mode()))} ${mark}</span>
          <span class="v-preview">${escapeHtml(v.text.replace(/\s+/g, ' ').slice(0, 70))}</span></span></li>`;
    })
    .join('');
}

// The named stops under the zoom slider: saved pills by a short name, other sizes by paragraphs or lines.
const SHORT = { 'one word': 'Word', 'one sentence': '1S', 'one paragraph': '1P', 'one line': '1L', couplet: '2L', quatrain: '4L', 'full poem': 'Full' };
function detentLabel(s) {
  const key = api.versionKey(mode(), s);
  const pill = state.doc.pills.find((p) => p.spec.mode === mode() && api.versionKey(mode(), plainSpec(p.spec)) === key);
  if (pill) return SHORT[pill.label.toLowerCase()] || pill.label;
  if (mode() === 'prose') {
    const letter = { chars: 'c', words: 'w', sentences: 'S', paragraphs: 'P' }[s.unit];
    if (s.unit === 'words' && s.value === 1) return 'Word';
    return `${s.value}${letter}`;
  }
  return s.syllables === 10 ? `${s.lines}L` : '';
}

function currentStep() {
  const list = steps().map(plainSpec);
  const key = vkey();
  const exact = list.findIndex((s) => api.versionKey(mode(), s) === key);
  if (exact >= 0) return exact;
  const size = sizeOf(spec(), mode());
  let best = 0;
  list.forEach((s, i) => {
    if (Math.abs(sizeOf(s, mode()) - size) < Math.abs(sizeOf(list[best], mode()) - size)) best = i;
  });
  return best;
}

function renderZoom() {
  const list = steps().map(plainSpec);
  const z = $('zoom');
  z.max = Math.max(0, list.length - 1);
  if (document.activeElement !== z || !z.matches(':active')) z.value = currentStep();
  const at = (i) => (list.length > 1 ? (100 * i) / (list.length - 1) : 50);
  const cur = vkey();
  let lastLabel = -100;
  $('zoom-ticks').innerHTML = list
    .map((s, i) => {
      const key = api.versionKey(mode(), s);
      const v = lookup(key);
      const cls = isWriting(key) ? 'writing' : v?.status === 'locked' ? 'locked' : v?.text ? (v.stale ? 'stale' : 'ready') : 'empty';
      let label = detentLabel(s);
      if (label && at(i) - lastLabel < 7 && key !== cur) label = '';
      if (label) lastLabel = at(i);
      const human = (v?.human_share ?? 0) > 0.005 ? 'human' : '';
      return `<button class="tick ${cls} ${human} ${key === cur ? 'current' : ''}" data-step="${i}" style="left:${at(i)}%" title="${escapeHtml(describe(s, mode()))}">
        <i></i>${label ? `<span>${escapeHtml(label)}</span>` : ''}</button>`;
    })
    .join('');
}

// Poetry sizes on a grid: down for more lines, right for longer lines. Each cell is one size.
const GRID_COLS = POETRY_LIMITS.syllables[1];
function gridRows() {
  return state.doc.grid_rows || 16;
}

function renderGrid() {
  const poetry = mode() === 'poetry';
  $('grid-pad').hidden = !poetry;
  $('meter-row').hidden = !poetry;
  $('form-row').hidden = !poetry;
  $('rhyme-row').hidden = !poetry;
  $('zoom-row').hidden = poetry;
  document.querySelector('.reader').classList.toggle('poetry', poetry);
  if (!poetry) return;
  const rows = gridRows();
  const s = spec();
  for (const b of $('meter-switch').children) b.classList.toggle('active', !s.form && (b.dataset.meter || null) === (s.meter || null));
  renderForms(s);
  $('rhyme-row').classList.toggle('dim', Boolean(s.form));
  for (const b of $('rhyme-switch').children) b.classList.toggle('active', !s.form && (b.dataset.rhyme || '') === (s.rhyme || ''));
  const host = $('grid-cells');
  // With a meter the line length is set, so the grid is one column: the number of lines.
  const cols = s.meter ? 1 : GRID_COLS;
  host.classList.toggle('meter', Boolean(s.meter));
  host.style.gridTemplateColumns = s.meter ? '18px 1fr' : `18px repeat(${GRID_COLS}, 1fr)`;
  host.style.gridTemplateRows = '';
  document.querySelector('.grid-x span').textContent = s.meter ? `Lines of ${METER_NAME[s.meter]}` : 'Syllables per line';
  const cells = [];
  for (let l = 1; l <= rows; l++) {
    cells.push(`<span class="grid-label">${l === 1 || l % 4 === 0 || l === s.lines ? l : ''}</span>`);
    for (let y = 1; y <= cols; y++) {
      const cellSpec = { ...(s.meter ? { meter: s.meter, lines: l } : { lines: l, syllables: y }), ...(s.rhyme ? { rhyme: s.rhyme } : {}) };
      const key = api.versionKey('poetry', cellSpec);
      const v = lookup(key);
      const cls = isWriting(key) ? 'writing' : v?.status === 'locked' ? 'locked' : v?.text ? (v.stale ? 'stale' : 'ready') : '';
      const human = (v?.human_share ?? 0) > 0.005 ? 'human' : '';
      const here = !s.form && l === s.lines && (s.meter || y === s.syllables);
      const cross = !s.form && !s.meter && (l === s.lines || y === s.syllables);
      cells.push(`<button class="cell ${cls} ${human} ${here ? 'current' : cross ? 'cross' : ''}" data-l="${l}" data-y="${y}" title="${escapeHtml(describe(cellSpec, 'poetry'))}"></button>`);
    }
  }
  cells.push('<span></span>');
  for (let y = 1; y <= cols; y++) cells.push(`<span class="grid-label x">${s.meter ? '' : y === 1 || y % 4 === 0 || y === s.syllables ? y : ''}</span>`);
  host.innerHTML = cells.join('');
}

let lastFreeSyllables = 10;
let lastRhyme = '';
function poemAt(lines, syllables) {
  const s = spec();
  const rhyme = s.form ? lastRhyme : s.rhyme || '';
  const base = s.meter ? { meter: s.meter, lines } : { lines, syllables };
  return rhyme ? { ...base, rhyme } : base;
}

// The forms list: grouped by family; a form with several rhyme schemes gets a scheme menu.
function renderForms(s) {
  const host = $('form-list');
  if (!host.dataset.built && catalog.list.length) {
    host.dataset.built = '1';
    const families = [...new Set(catalog.list.map((f) => f.family))];
    host.innerHTML = families
      .map(
        (fam) =>
          `<div class="form-family"><span class="form-family-name">${escapeHtml(fam)}</span>` +
          catalog.list
            .filter((f) => f.family === fam)
            .map((f) => `<button class="form-chip" data-form="${f.id}" title="${escapeHtml(f.guide)}">${escapeHtml(f.name)}</button>`)
            .join('') +
          '</div>'
      )
      .join('');
    $('rhyme-switch').innerHTML = catalog.rhymes.map((r) => `<button data-rhyme="${r.id}">${escapeHtml(r.label)}</button>`).join('');
  }
  for (const b of host.querySelectorAll('[data-form]')) {
    b.classList.toggle('active', s.form === b.dataset.form);
    const v = lookup(api.versionKey('poetry', { form: b.dataset.form }));
    b.classList.toggle('ready', Boolean(v?.text));
  }
  const f = s.form && catalog.forms[s.form];
  const sel = $('scheme-select');
  sel.hidden = !(f && f.schemes.length > 1);
  if (f && f.schemes.length > 1) {
    const opts = f.schemes.map((x) => `<option value="${x.id}">${escapeHtml(x.label)}</option>`).join('');
    if (sel.dataset.form !== f.id) {
      sel.innerHTML = opts;
      sel.dataset.form = f.id;
    }
    sel.value = s.scheme || f.schemes[0].id;
  }
}

let gridDragging = false;
function gridPick(e) {
  const cell = e.target.closest('.cell');
  if (!cell) return;
  const next = poemAt(Number(cell.dataset.l), Number(cell.dataset.y));
  if (api.versionKey('poetry', next) !== vkey()) go(next);
}
$('meter-switch').addEventListener('click', (e) => {
  const b = e.target.closest('[data-meter]');
  if (!b) return;
  const s = spec();
  if (!s.meter && !s.form) lastFreeSyllables = s.syllables;
  const lines = s.form ? 8 : s.lines || 8;
  const rhyme = s.form ? lastRhyme : s.rhyme || '';
  const base = b.dataset.meter ? { meter: b.dataset.meter, lines } : { lines, syllables: lastFreeSyllables };
  go(rhyme ? { ...base, rhyme } : base);
});
$('form-row').addEventListener('click', (e) => {
  const b = e.target.closest('[data-form]');
  if (!b) return;
  const s = spec();
  if (!s.meter && !s.form) lastFreeSyllables = s.syllables;
  if (!s.form) lastRhyme = s.rhyme || '';
  go({ form: b.dataset.form });
});
$('scheme-select').addEventListener('change', (e) => {
  const f = catalog.forms[spec().form];
  if (!f) return;
  go(e.target.value === f.schemes[0].id ? { form: f.id } : { form: f.id, scheme: e.target.value });
});
$('rhyme-switch').addEventListener('click', (e) => {
  const b = e.target.closest('[data-rhyme]');
  if (!b) return;
  const s = spec();
  lastRhyme = b.dataset.rhyme;
  const base = s.form ? { lines: 8, syllables: lastFreeSyllables } : s.meter ? { meter: s.meter, lines: s.lines } : { lines: s.lines, syllables: s.syllables };
  go(b.dataset.rhyme ? { ...base, rhyme: b.dataset.rhyme } : base);
});
$('grid-cells').addEventListener('pointerdown', (e) => {
  gridDragging = true;
  gridPick(e);
});
$('grid-cells').addEventListener('pointerover', (e) => gridDragging && gridPick(e));
window.addEventListener('pointerup', () => (gridDragging = false));

function renderNotes() {
  const key = vkey();
  $('note-size').textContent = '(' + describe(spec(), mode()) + ')';
  if (document.activeElement !== $('size-note')) $('size-note').value = state.doc.notes[key] || '';
  if (document.activeElement !== $('brief')) $('brief').value = state.doc.brief || '';
  const pills = state.doc.pills.filter((p) => p.spec.mode === mode());
  const host = $('pill-notes');
  const sig = pills.map((p) => p.id).join() + mode();
  if (host.dataset.sig !== sig) {
    host.dataset.sig = sig;
    host.innerHTML = pills.length
      ? pills
          .map(
            (p) => `<label class="field compact"><span>${escapeHtml(p.label)} <em>${escapeHtml(describe(plainSpec(p.spec), mode()))}</em></span>
        <textarea rows="2" data-pill-note="${p.id}" placeholder="What this size should do.">${escapeHtml(p.note || '')}</textarea></label>`
          )
          .join('')
      : '<p class="hint">Save a size as a pill to give it its own note.</p>';
  }
}

// ---------- editor ----------

function openEditor() {
  state.editing = true;
  const v = version();
  state.editFrom = v?.status !== 'locked' && v?.model ? v.model : null;
  $('editor-text').value = v?.text || '';
  $('editor').hidden = false;
  $('stage').hidden = true;
  $('writing-badge').hidden = true;
  const first = !modeVersions().some((x) => x.status === 'locked');
  $('editor-hint').textContent = first
    ? "Write at any length. Its size is worked out from what you write, and every other size is written from it."
    : `Your text for ${describe(spec(), mode())}. Saving locks it, and the other sizes are rewritten to match.`;
  $('editor-cancel').hidden = !Object.values(state.doc.versions).some((x) => x.status === 'locked');
  $('editor-text').focus();
}

function closeEditor() {
  state.editing = false;
  $('editor').hidden = true;
  $('stage').hidden = false;
  state.shownKey = null;
  render(false);
}

async function saveEditor() {
  const text = $('editor-text').value.trim();
  if (!text) return;
  const first = !modeVersions().some((x) => x.status === 'locked');
  $('editor-save').disabled = true;
  try {
    await flushMeta();
    const server = await api.saveVersion(state.doc.id, mode(), first ? null : spec(), text, state.editFrom);
    if (first) {
      const saved = Object.values(server.versions).find((v) => v.spec.mode === mode() && v.status === 'locked' && v.text === text);
      if (saved) state.spec[mode()] = plainSpec(saved.spec);
    }
    state.doc = { ...server, mode: state.doc.mode };
    $('doc-title').value = state.doc.title;
    const opt = $('doc-select').querySelector(`option[value="${state.doc.id}"]`);
    if (opt) opt.textContent = state.doc.title || 'Untitled';
    closeEditor();
  } catch (e) {
    flash(`Couldn't save: ${e.message}`);
  } finally {
    $('editor-save').disabled = false;
  }
}

// ---------- log viewer ----------

async function showLog(logId) {
  const entry = await api.log(state.doc.id, logId);
  const attempts = entry.attempts || [{ prompt: entry.prompt, response: entry.response }];
  $('log-body').innerHTML =
    `<p class="hint">Model: ${escapeHtml(entry.model)} · ${entry.seconds}s${settings.showCosts ? ` · $${Number(entry.cost_usd || 0).toFixed(3)}` : ''}</p>` +
    `<details><summary>Standing instructions</summary><pre>${escapeHtml(entry.system || '')}</pre></details>` +
    attempts
      .map(
        (a, i) => `<h4>${attempts.length > 1 ? `Attempt ${i + 1}` : 'Request'}</h4><pre>${escapeHtml(a.prompt || '')}</pre>
        <h4>Answer</h4><pre class="answer">${escapeHtml(a.response || a.error || '')}</pre>
        ${a.problems?.length ? `<p class="hint">Sent back because: ${escapeHtml(a.problems.join(' '))}</p>` : ''}`
      )
      .join('');
  $('log').hidden = false;
}

// ---------- tweet preview ----------

const TWEET_SPEC = { unit: 'chars', value: 280 };
const tweetState = { open: false, theme: 'light', profile: null, text: '', url: null };

// The text to preview: the version on screen if it fits in a tweet, else the document's tweet version.
function tweetSource() {
  const v = version();
  if (v?.text && v.text.length <= 280) return { text: v.text, note: '' };
  const pill = state.doc.pills.find((p) => p.spec.mode === 'prose' && p.spec.unit === 'chars' && p.spec.value <= 280);
  const s = pill ? plainSpec(pill.spec) : TWEET_SPEC;
  const t = lookup(api.versionKey('prose', s));
  if (t?.text) return { text: t.text, note: 'This version is too long for a tweet, so this is the document\'s tweet version.' };
  return { text: '', note: 'Writing the tweet version of this document…', spec: s };
}

async function drawTweet() {
  if (!tweetState.open) return;
  const src = tweetSource();
  $('tweet-note').textContent = src.note;
  if (!src.text) {
    $('tweet-frame').innerHTML = '<span class="spinner"></span>';
    if (src.spec && !isWriting(api.versionKey('prose', src.spec))) api.write(state.doc.id, 'prose', src.spec, false, model()).then(merge);
    return;
  }
  const p = tweetState.profile || {};
  const key = JSON.stringify([src.text, $('tweet-name').value, $('tweet-handle').value, tweetState.theme]);
  if (key === tweetState.drawn) return;
  tweetState.drawn = key;
  tweetState.text = src.text;
  try {
    const url = await renderTweet({
      text: src.text,
      name: $('tweet-name').value || 'Your Name',
      handle: ($('tweet-handle').value || 'yourhandle').replace(/^@/, ''),
      verified: p.connected || p.lookedUp ? p.verified : 'none',
      avatar: p.avatar,
      theme: tweetState.theme
    });
    if (tweetState.url) URL.revokeObjectURL(tweetState.url);
    tweetState.url = url;
    $('tweet-frame').innerHTML = `<img alt="Tweet preview" src="${url}">`;
    $('tweet-frame').classList.toggle('dark', tweetState.theme === 'dark');
  } catch (e) {
    tweetState.drawn = null;
    $('tweet-frame').textContent = `Couldn't draw the preview: ${e.message}`;
  }
}

async function openTweet() {
  tweetState.open = true;
  tweetState.drawn = null;
  $('tweet').hidden = false;
  $('tweet-frame').innerHTML = '<span class="spinner"></span>';
  if (!tweetState.profile) {
    try {
      tweetState.profile = await (await fetch('/api/x-profile')).json();
    } catch {
      tweetState.profile = { connected: false, name: 'Your Name', handle: 'yourhandle' };
    }
  }
  const p = tweetState.profile;
  $('tweet-name').value = p.connected ? p.name : localStorage.getItem('stretch-editor:x-name') || '';
  $('tweet-handle').value = p.connected ? p.handle : localStorage.getItem('stretch-editor:x-handle') || '';
  $('tweet-name').placeholder = 'Your Name';
  $('tweet-handle').placeholder = 'yourhandle';
  $('tweet-who').textContent = p.connected
    ? `Shown as your X account, @${p.handle}.`
    : 'Type your X handle to use your real picture and name.';
  if (!p.connected && $('tweet-handle').value) lookupHandle();
  else drawTweet();
}

$('act-tweet').addEventListener('click', openTweet);
$('tweet-close').addEventListener('click', () => {
  tweetState.open = false;
  $('tweet').hidden = true;
});
$('tweet').addEventListener('click', (e) => {
  if (e.target.id === 'tweet') $('tweet-close').click();
});
// Typing a handle looks up that public X profile, so the preview shows your real picture, name and badge.
async function lookupHandle() {
  const handle = $('tweet-handle').value.trim().replace(/^@/, '');
  if (!handle || tweetState.profile?.connected) return drawTweet();
  if (tweetState.looked === handle) return drawTweet();
  tweetState.looked = handle;
  $('tweet-who').textContent = `Looking up @${handle} on X…`;
  try {
    const found = await (await fetch(`/api/x-profile/${encodeURIComponent(handle)}`)).json();
    if (tweetState.looked !== handle) return;
    if (found.found) {
      tweetState.profile = { ...tweetState.profile, avatar: found.avatar, verified: found.verified, lookedUp: true };
      // A new handle brings its account's display name; you can still edit the name afterwards.
      if (found.name) $('tweet-name').value = found.name;
      $('tweet-who').textContent = `Showing @${found.handle}'s public X picture and name.`;
    } else {
      tweetState.profile = { ...tweetState.profile, avatar: null, verified: 'none' };
      $('tweet-who').textContent = `Couldn't find @${handle} on X, so this uses a default picture.`;
    }
  } catch {
    $('tweet-who').textContent = "Couldn't reach X to look up that handle.";
  }
  localStorage.setItem('stretch-editor:x-name', $('tweet-name').value);
  tweetState.drawn = null;
  drawTweet();
}

let tweetTimer;
for (const id of ['tweet-name', 'tweet-handle'])
  $(id).addEventListener('input', () => {
    localStorage.setItem('stretch-editor:x-name', $('tweet-name').value);
    localStorage.setItem('stretch-editor:x-handle', $('tweet-handle').value);
    clearTimeout(tweetTimer);
    tweetTimer = setTimeout(id === 'tweet-handle' ? lookupHandle : drawTweet, id === 'tweet-handle' ? 600 : 300);
  });
$('tweet-theme').addEventListener('click', (e) => {
  const b = e.target.closest('[data-theme]');
  if (!b) return;
  tweetState.theme = b.dataset.theme;
  for (const x of $('tweet-theme').children) x.classList.toggle('active', x === b);
  drawTweet();
});
$('tweet-copy').addEventListener('click', async () => {
  await navigator.clipboard.writeText(tweetState.text);
  $('tweet-copy').textContent = 'Copied';
  setTimeout(() => ($('tweet-copy').textContent = 'Copy text'), 1500);
});

// ---------- events ----------

$('doc-select').addEventListener('change', (e) => openDoc(e.target.value));
$('new-doc').addEventListener('click', async () => {
  // Show the blank page at once; the document is created behind it.
  const btn = $('new-doc');
  if (btn.disabled) return;
  btn.disabled = true;
  $('doc-title').value = 'Untitled';
  $('editor-text').value = '';
  $('editor').hidden = false;
  $('stage').hidden = true;
  $('editor-hint').textContent = 'Write at any length. Its size is worked out from what you write, and every other size is written from it.';
  $('editor-text').focus();
  try {
    const typed = () => $('editor-text').value;
    const doc = await api.createDoc('Untitled');
    await loadDocList(doc.id);
    const draft = typed();
    await openDoc(doc.id);
    if (draft && !$('editor-text').value) $('editor-text').value = draft;
  } catch (e) {
    flash(`Couldn't create a document: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
});
$('delete-doc').addEventListener('click', async () => {
  if (!confirm(`Delete "${state.doc.title}" and all its sizes?`)) return;
  const docs = await api.deleteDoc(state.doc.id);
  if (!docs.length) {
    const doc = await api.createDoc('Untitled');
    await loadDocList(doc.id);
    return openDoc(doc.id);
  }
  await loadDocList(docs[0].id);
  openDoc(docs[0].id);
});
$('doc-title').addEventListener('input', (e) => {
  state.doc.title = e.target.value;
  $('doc-select').selectedOptions[0].textContent = e.target.value || 'Untitled';
  saveMeta({ title: e.target.value });
});

for (const b of document.querySelectorAll('#mode-prose, #mode-poetry'))
  b.addEventListener('click', () => {
    if (state.editing) closeEditor();
    state.doc.mode = b.dataset.mode;
    state.shownKey = null;
    saveMeta({ mode: b.dataset.mode }, 0);
    render(false);
    requestCurrent();
  });

$('model-switch').addEventListener('click', (e) => {
  const b = e.target.closest('[data-model]');
  if (!b) return;
  state.model = b.dataset.model;
  localStorage.setItem('stretch-editor:model', state.model);
  render(true);
  requestCurrent();
});
$('model-button').addEventListener('click', () => ($('model-popover').hidden = !$('model-popover').hidden));
$('settings-button').addEventListener('click', () => ($('settings-popover').hidden = !$('settings-popover').hidden));
document.addEventListener('click', (e) => {
  if (!e.target.closest('.model-picker')) $('model-popover').hidden = true;
  if (!e.target.closest('.settings')) $('settings-popover').hidden = true;
});
for (const m of ['prose', 'poetry'])
  $(`fill-${m}`).addEventListener('change', (e) => {
    state.doc.fill = { ...state.doc.fill, [m]: e.target.value };
    saveMeta({ fill: state.doc.fill }, 0);
  });
// Writers at once: an editor-wide setting kept on the server.
async function loadWriters() {
  const { writers } = await (await fetch('/api/settings')).json();
  $('writers-range').value = $('writers-num').value = writers;
}
let writersTimer;
function setWriters(n) {
  n = Math.min(32, Math.max(1, Math.round(Number(n) || 10)));
  $('writers-range').value = $('writers-num').value = n;
  clearTimeout(writersTimer);
  writersTimer = setTimeout(
    () => fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ writers: n }) }),
    300
  );
}
$('writers-range').addEventListener('input', (e) => setWriters(e.target.value));
$('writers-num').addEventListener('change', (e) => setWriters(e.target.value));
loadWriters();
$('setting-costs').checked = settings.showCosts;
$('setting-costs').addEventListener('change', (e) => {
  settings.showCosts = e.target.checked;
  localStorage.setItem('stretch-editor:show-costs', settings.showCosts ? '1' : '0');
  $('model-list').innerHTML = '';
  render(false);
});
$('model-list').addEventListener('change', (e) => {
  const id = e.target.dataset.pick;
  if (!id) return;
  let models = state.doc.available_models.map((m) => m.id).filter((m) => (m === id ? e.target.checked : state.doc.models.includes(m)));
  if (!models.length) {
    e.target.checked = true;
    return flash('Keep at least one model.');
  }
  state.doc.models = models;
  saveMeta({ models }, 0);
  render(false);
});

// Dragging a slider or typing a number sets that measure; in prose it also puts that unit in charge.
function onSizeInput(e) {
  const dim = e.target.dataset.dim;
  if (!dim) return;
  const limits = limitsOf(dim);
  const raw = Number(e.target.value);
  if (!raw && e.target.dataset.kind === 'num') return;
  const value = Math.min(limits[1], Math.max(limits[0], e.target.dataset.kind === 'range' ? fromPos(dim, raw, limits) : Math.round(raw)));
  const num = e.target.closest('.ctl').querySelector('[data-kind="num"]');
  if (e.target !== num) num.value = value;
  const cur = spec();
  const rhymed = (x) => (cur.rhyme ? { ...x, rhyme: cur.rhyme } : x);
  const verse = cur.meter
    ? rhymed({ meter: cur.meter, lines: dim === 'lines' ? value : cur.lines })
    : cur.form
      ? poemAt(dim === 'lines' ? value : 8, dim === 'syllables' ? value : lastFreeSyllables)
      : rhymed({ lines: cur.lines, syllables: cur.syllables, [dim]: value });
  go(mode() === 'prose' ? { unit: dim, value } : verse, { fromInput: true });
}
$('sliders').addEventListener('input', (e) => e.target.dataset.kind === 'range' && onSizeInput(e));
$('sliders').addEventListener('change', (e) => e.target.dataset.kind === 'num' && onSizeInput(e));
$('sliders').addEventListener('keydown', (e) => e.key === 'Enter' && e.target.dataset.kind === 'num' && onSizeInput(e));

$('zoom').addEventListener('input', (e) => go(plainSpec(steps()[Number(e.target.value)])));
$('zoom-ticks').addEventListener('click', (e) => {
  const tick = e.target.closest('[data-step]');
  if (tick) go(plainSpec(steps()[Number(tick.dataset.step)]));
});

$('pills').addEventListener('click', (e) => {
  const remove = e.target.closest('[data-remove]');
  if (remove) {
    state.doc.pills = state.doc.pills.filter((p) => p.id !== remove.dataset.remove);
    saveMeta({ pills: state.doc.pills }, 0);
    render(false);
    return;
  }
  if (e.target.closest('[data-human]')) {
    const human = mostHuman();
    if (human) go(plainSpec(human.spec));
    return;
  }
  const pillBtn = e.target.closest('.pill-go');
  if (pillBtn) {
    const p = state.doc.pills.find((x) => x.id === pillBtn.dataset.pill);
    go(plainSpec(p.spec));
    return;
  }
  if (e.target.id === 'pill-add') addPillPrompt();
});

function addPillPrompt() {
  const input = $('pill-name');
  $('pill-add').hidden = true;
  input.hidden = false;
  input.value = '';
  input.focus();
  let finished = false;
  const done = (ok) => {
    if (finished) return;
    finished = true;
    const label = input.value.trim();
    input.hidden = true;
    if (ok && label) {
      state.doc.pills = [
        ...state.doc.pills,
        { id: 'p' + Date.now(), label, spec: api.withMode(mode(), spec()), note: state.doc.notes[vkey()] || '' }
      ];
      saveMeta({ pills: state.doc.pills }, 0);
    }
    render(false);
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter') done(true);
    if (e.key === 'Escape') done(false);
  };
  input.onblur = () => done(true);
}

$('act-pill').addEventListener('click', addPillPrompt);
$('act-edit').addEventListener('click', openEditor);
$('editor-cancel').addEventListener('click', closeEditor);
$('editor-save').addEventListener('click', saveEditor);
$('editor-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) saveEditor();
});
$('act-lock').addEventListener('click', async () => {
  const v = version();
  if (!v?.text) return;
  merge(await api.lock(state.doc.id, mode(), spec(), v.status !== 'locked', model()));
});
$('act-rewrite').addEventListener('click', async () => {
  state.requested.add(vkey());
  merge(await api.write(state.doc.id, mode(), spec(), true, model()));
});

$('versions').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-key]');
  if (!li) return;
  const v = lookup(li.dataset.key);
  if (v) go(plainSpec(v.spec));
});

$('status').addEventListener('click', (e) => {
  if (e.target.id === 'show-log') showLog(e.target.dataset.log);
});
$('log-close').addEventListener('click', () => ($('log').hidden = true));
$('log').addEventListener('click', (e) => {
  if (e.target.id === 'log') $('log').hidden = true;
});

for (const tab of document.querySelectorAll('.tab'))
  tab.addEventListener('click', () => {
    for (const t of document.querySelectorAll('.tab')) t.classList.toggle('active', t === tab);
    for (const body of document.querySelectorAll('.tab-body')) body.hidden = body.dataset.body !== tab.dataset.tab;
  });

$('brief').addEventListener('input', (e) => {
  state.doc.brief = e.target.value;
  saveMeta({ brief: e.target.value }, 1200);
});
$('size-note').addEventListener('input', (e) => {
  state.doc.notes = { ...state.doc.notes, [vkey()]: e.target.value };
  saveMeta({ notes: state.doc.notes }, 1200);
});
$('pill-notes').addEventListener('input', (e) => {
  const id = e.target.dataset.pillNote;
  if (!id) return;
  state.doc.pills = state.doc.pills.map((p) => (p.id === id ? { ...p, note: e.target.value } : p));
  saveMeta({ pills: state.doc.pills }, 1200);
});

// Arrow keys step along the zoom slider, like stretchtext's keyboard reading.
document.addEventListener('keydown', (e) => {
  if (state.editing || e.target.matches('input, textarea, select') || !$('log').hidden || !$('tweet').hidden) return;
  if (mode() === 'poetry') {
    const d = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[e.key];
    if (!d) return;
    e.preventDefault();
    const s = spec();
    return go(poemAt(Math.min(gridRows(), Math.max(1, s.lines + d[0])), Math.min(GRID_COLS, Math.max(1, s.syllables + d[1]))));
  }
  const list = steps();
  const z = currentStep();
  if ((e.key === 'ArrowLeft' || e.key === 'ArrowUp') && z > 0) go(plainSpec(list[z - 1]));
  else if ((e.key === 'ArrowRight' || e.key === 'ArrowDown') && z < list.length - 1) go(plainSpec(list[z + 1]));
  else if (e.key === 'Home') go(plainSpec(list[0]));
  else if (e.key === 'End') go(plainSpec(list[list.length - 1]));
  else return;
  e.preventDefault();
});

// Two-finger scroll and pinch on the text (and on the poetry grid), as on stretchtext.dev, in the
// natural direction, lined up with the grid -- 1 x 1 at the top left, the largest size at the bottom
// right: moving your fingers down grows the number of lines (or zooms longer), moving them right
// lengthens each line (syllables in verse, words per sentence in prose).
const navigation = {
  read: () => {
    if (!state.doc || state.editing) return null;
    if (mode() === 'poetry')
      return spec().meter
        ? { x: 1, y: spec().lines, maxX: 1, maxY: gridRows() }
        : { x: spec().syllables, y: spec().lines, maxX: GRID_COLS, maxY: gridRows() };
    // Prose has one length, so both directions move along the zoom stops.
    const n = Math.max(1, steps().length);
    return { x: currentStep() + 1, y: currentStep() + 1, maxX: n, maxY: n };
  },
  move: (sx, sy) => {
    // Like scrolling a page (the Mac's natural scrolling is already applied by the system):
    // push up to go down (more lines / longer), push left to go right (longer lines).
    const dx = sx,
      dy = sy;
    let next = { ...spec() };
    if (mode() === 'poetry') {
      if (next.form) next = { lines: next.lines, syllables: next.syllables };
      const lines = Math.min(gridRows(), Math.max(1, next.lines + dy));
      const syllables = Math.min(GRID_COLS, Math.max(1, (next.syllables || 10) + dx));
      return go(poemAt(lines, syllables));
    }
    const list = steps();
    const i = Math.min(list.length - 1, Math.max(0, currentStep() + (dy || dx)));
    next = plainSpec(list[i]);
    go(next);
  }
};
stage.bindGestures(navigation);

// The grid scrolls with the same resistance and stretch as the text (stretchtext's gesture code).
bindSurface($('grid-pad'), navigation);

setCatalog(await (await fetch('/static/forms.json')).json());
const docs = await loadDocList();
const remembered = localStorage.getItem('stretch-editor:doc');
const first = docs.find((d) => d.id === remembered) || docs[0];
if (first) {
  $('doc-select').value = first.id;
  await openDoc(first.id);
} else {
  const doc = await api.createDoc('Untitled');
  await loadDocList(doc.id);
  await openDoc(doc.id);
}
