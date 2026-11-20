// Counting, splitting and naming sizes. Shared by the editor and the stage.

export const UNITS = ['chars', 'words', 'sentences', 'paragraphs'];
export const UNIT_LETTER = { chars: 'c', words: 'w', sentences: 's', paragraphs: 'p' };
export const UNIT_LABEL = { chars: 'Characters', words: 'Words', sentences: 'Sentences', paragraphs: 'Paragraphs' };
const UNIT_NAME = { chars: 'character', words: 'word', sentences: 'sentence', paragraphs: 'paragraph' };
const WORDS_PER_UNIT = { chars: 1 / 5.8, words: 1, sentences: 18, paragraphs: 70 };
export const PROSE_LIMITS = { chars: [1, 10000], words: [1, 2000], sentences: [1, 120], paragraphs: [1, 30] };
// The verse forms catalogue (static/forms.json), loaded by the page at start.
export const catalog = { forms: {}, list: [], rhymes: [] };
export function setCatalog(data) {
  catalog.list = data.forms;
  catalog.forms = Object.fromEntries(data.forms.map((f) => [f.id, f]));
  catalog.rhymes = data.rhymes;
}

export const METER_NAME = {
  'iambic-pentameter': 'iambic pentameter',
  'trochaic-tetrameter': 'trochaic tetrameter',
  'anapestic-tetrameter': 'anapestic tetrameter'
};
export const METER_SYLLABLES = { 'iambic-pentameter': 10, 'trochaic-tetrameter': 7, 'anapestic-tetrameter': 12 };
export const POETRY_LIMITS = { lines: [1, 32], syllables: [1, 16] };

export function words(text) {
  return text.match(/[\p{L}\p{N}][\p{L}\p{N}'’\-]*/gu) || [];
}

export function sentences(paragraph) {
  return (paragraph.match(/[^.!?…]+(?:[.!?…]+["'”’)]*|$)/gu) || []).map((s) => s.trim()).filter(Boolean);
}

export function paragraphs(text) {
  return text.split(/\n\s*\n/).map((p) => p.replace(/\s*\n\s*/g, ' ').trim()).filter(Boolean);
}

export function syllables(word) {
  let w = word.toLowerCase().replace(/[^a-z]/g, '');
  if (!w) return 0;
  if (w.length <= 3) return 1;
  w = w.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/, '').replace(/^y/, '');
  const groups = w.match(/[aeiouy]{1,2}/g);
  return Math.max(1, groups ? groups.length : 1);
}

export function lineSyllables(line) {
  return words(line).reduce((sum, w) => sum + syllables(w), 0);
}

// Rows the stage draws: paragraphs for prose, lines (with stanza breaks) for poetry.
export function toRows(text, mode) {
  if (mode === 'prose') {
    const texts = paragraphs(text);
    return { texts, keys: texts.map((_, i) => 'p' + i), breaks: texts.map((_, i) => i).slice(1), mode };
  }
  const texts = [],
    breaks = [];
  let gap = false;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) {
      gap = texts.length > 0;
      continue;
    }
    if (gap) breaks.push(texts.length);
    gap = false;
    texts.push(line);
  }
  return { texts, keys: texts.map((_, i) => 'l' + i), breaks, mode };
}

// The size a piece of text actually has.
export function measure(text, mode) {
  if (mode === 'prose') {
    const ps = paragraphs(text);
    const ss = ps.flatMap(sentences);
    const ws = words(text).length;
    return {
      paragraphs: ps.length,
      sentences: ss.length,
      words: ws,
      chars: text.trim().length,
      perParagraph: ps.length ? ss.length / ps.length : 0,
      perSentence: ss.length ? ws / ss.length : 0
    };
  }
  const { texts } = toRows(text, 'poetry');
  const syl = texts.map(lineSyllables);
  return {
    lines: texts.length,
    syllables: syl.reduce((a, b) => a + b, 0),
    perLine: texts.length ? syl.reduce((a, b) => a + b, 0) / texts.length : 0,
    words: words(text).length,
    chars: text.trim().length
  };
}

const clamp = (v, [lo, hi]) => Math.min(hi, Math.max(lo, Math.round(v)));


export function keyFor(spec, mode) {
  if (mode === 'prose') return `${UNIT_LETTER[spec.unit || 'words']}${spec.value || 1}`;
  if (spec.form) return spec.form + (spec.scheme ? `~${spec.scheme}` : '');
  const suffix = spec.rhyme ? `-r${spec.rhyme}` : '';
  if (spec.meter) return `${spec.meter}-l${spec.lines}${suffix}`;
  return `l${spec.lines}-y${spec.syllables}${suffix}`;
}

export function sizeOf(spec, mode) {
  if (mode === 'prose') return (spec.value || 1) * WORDS_PER_UNIT[spec.unit || 'words'];
  return spec.lines * spec.syllables;
}

const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;

export function describe(spec, mode) {
  if (mode === 'prose') {
    if (spec.unit === 'words' && spec.value === 1) return 'One word';
    return plural(spec.value || 1, UNIT_NAME[spec.unit || 'words']);
  }
  if (spec.form) {
    const f = catalog.forms[spec.form];
    if (!f) return spec.form;
    const scheme = f.schemes.find((x) => x.id === spec.scheme) || f.schemes[0];
    return `${f.name} (${scheme.label})`;
  }
  const rhyme = spec.rhyme ? ` · ${catalog.rhymes.find((r) => r.id === spec.rhyme)?.label || spec.rhyme}` : '';
  if (spec.meter) return `${plural(spec.lines, 'line')} of ${METER_NAME[spec.meter]}${rhyme}`;
  return `${plural(spec.lines, 'line')} · ${plural(spec.syllables, 'syllable')} per line${rhyme}`;
}

export function shortLabel(spec, mode) {
  if (mode === 'prose') return describe(spec, mode).replace('One word', '1 word');
  if (spec.form) return describe(spec, mode);
  return `${spec.lines} line${spec.lines === 1 ? '' : 's'} × ${spec.syllables} syl`;
}

// A text's length in each prose unit.
export function proseCounts(text) {
  const m = measure(text, 'prose');
  return { chars: m.chars, words: m.words, sentences: m.sentences, paragraphs: m.paragraphs };
}

