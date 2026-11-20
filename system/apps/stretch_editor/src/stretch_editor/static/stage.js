// The reading stage: stretchtext's Word Spring renderer, adapted so prose
// paragraphs wrap like ordinary text while words still glide, open and fold
// between versions. Poetry keeps one line per row, as stretchtext does.
import { createWordSpring } from '/vendor/stretchtext/src/word-spring.js';
import { bindGestures, createTension } from '/vendor/stretchtext/src/gestures.js';

const PROSE_BASE = 21;
const POETRY_BASE = 28;

// New words get a soft glow that fades after they land, to draw the eye to what changed.
// The word's own entrance plays in the first part of the track; the glow fades over the rest.
const GLOW_MS = 1200;
// A crisp icy-blue outline around the letters: two tight rings, no wide haze.
const GLOW = (alpha) =>
  [`0 0 1px rgb(40 140 230 / ${alpha})`, `0 0 2.5px rgb(60 160 240 / ${alpha * 0.9})`, `0 0 4px rgb(110 190 250 / ${alpha * 0.5})`].join(', ');
function withGlow(keyframes, timing) {
  const enter = Number(timing.duration);
  const total = enter + GLOW_MS;
  const split = enter / total;
  const last = keyframes[keyframes.length - 1];
  const frames = keyframes.map((frame, i) => ({
    ...frame,
    offset: (i / (keyframes.length - 1)) * split,
    textShadow: GLOW(0.9 * (i / (keyframes.length - 1)))
  }));
  const settled = { transform: last.transform, opacity: last.opacity ?? 1 };
  frames.push(
    { ...settled, offset: Math.min(0.999, split + 0.15), textShadow: GLOW(0.9) },
    { ...settled, offset: 1, textShadow: GLOW(0) }
  );
  return { keyframes: frames, timing: { ...timing, duration: total, easing: 'linear' } };
}

// Scrolling and pinching on another surface (the poetry grid) with stretchtext's own resistance, but
// without its stretch effect: the grid never changes size.
const stillMotion = { set() {}, commit() {}, reset() {}, release() {} };
export function bindSurface(surface, { read, move }) {
  return bindGestures(surface, { read, move, axesAt: () => 'xy', motion: stillMotion });
}

export class Stage {
  constructor(host) {
    this.host = host;
    this.stage = document.createElement('div');
    this.stage.className = 'st-stage';
    this.block = document.createElement('div');
    this.block.className = 'st-block';
    this.overlay = document.createElement('div');
    this.overlay.className = 'st-departures';
    this.overlay.setAttribute('aria-hidden', 'true');
    this.stage.append(this.block, this.overlay);
    this.measureBox = document.createElement('div');
    this.measureBox.className = 'st-block st-measure';
    this.stage.append(this.measureBox);
    host.append(this.stage);
    this.spring = createWordSpring(this.stage, this.block, this.overlay);
    this.reduced = matchMedia('(prefers-reduced-motion: reduce)');
    this.current = null;
    this.referenceChars = 600;
    new ResizeObserver(() => this.current && this.#paint(this.current, false)).observe(this.stage);
  }

  // texts: one string per row (a paragraph in prose, a line in poetry).
  show({ texts, keys, breaks, mode }, animate = true) {
    this.current = { texts, keys, breaks, mode };
    this.#paint(this.current, animate);
  }

  setReference(chars) {
    this.referenceChars = Math.max(1, chars);
  }

  #fits(texts, breaks, mode, size) {
    const box = this.measureBox;
    box.style.fontSize = size + 'px';
    box.replaceChildren(
      ...texts.map((text, i) => {
        const row = document.createElement('div');
        row.className = 'st-line';
        if (breaks.includes(i)) row.style.paddingTop = '0.6528em';
        row.textContent = text || ' ';
        return row;
      })
    );
    const h = this.stage.clientHeight * 0.9;
    const w = this.stage.clientWidth * 0.94;
    return box.scrollHeight <= h && box.scrollWidth <= w;
  }

  #paint({ texts, keys, breaks, mode }, animate) {
    this.stage.dataset.mode = mode;
    const chars = texts.join(' ').length || 1;
    const base = mode === 'poetry' ? POETRY_BASE : PROSE_BASE;
    // Shorter versions grow, the way stretchtext zooms its shortest forms.
    const preferred = Math.min(base * 2.6, base * Math.sqrt(Math.max(1, this.referenceChars / chars)));
    let lo = 9,
      hi = preferred;
    if (!this.#fits(texts, breaks, mode, lo)) hi = lo;
    else if (this.#fits(texts, breaks, mode, hi)) lo = hi;
    else {
      for (let i = 0; i < 14; i++) {
        const mid = (lo + hi) / 2;
        if (this.#fits(texts, breaks, mode, mid)) lo = mid;
        else hi = mid;
      }
    }
    this.measureBox.replaceChildren();
    this.spring.render(texts, keys, breaks, lo, animate && !this.reduced.matches, this.#cascade(texts));
  }

  // Word Spring times every word alike, which reads as a cut when two versions share few words
  // (one word to a tweet). Departing words fold a little slower and new words open one after
  // another, so every change visibly grows or shrinks, whatever the distance.
  #cascade(texts) {
    const incoming = texts.join(' ').split(/\s+/).filter(Boolean).length || 1;
    const spread = Math.min(520, 40 + incoming * 14);
    const gap = spread / incoming;
    let order = 0;
    return ({ phase, keyframes, timing }) => {
      const duration = Number(timing.duration);
      if (phase === 'leave') return { keyframes, timing: { ...timing, duration: duration * 1.8 } };
      if (phase === 'enter') {
        const delay = 90 + order++ * gap;
        return withGlow(keyframes, { ...timing, duration: duration * 1.5, delay });
      }
      if (phase === 'move') return { keyframes, timing: { ...timing, duration: duration * 1.4 } };
      return undefined;
    };
  }

  // Two-finger scroll, pinch and touch, using stretchtext's own gesture handling and its
  // stretchy tension feedback. read() returns {x, y, maxX, maxY} (1-based) or null.
  bindGestures({ read, move }) {
    const motion = createTension(this.stage, this.block, this.reduced);
    this.gestures = bindGestures(this.stage, {
      read,
      move,
      axesAt: (x, y) => {
        const r = this.block.getBoundingClientRect();
        if (!r.width || !r.height) return 'y';
        const u = Math.abs((2 * (x - r.x)) / r.width - 1),
          v = Math.abs((2 * (y - r.y)) / r.height - 1);
        if (u > 0.5 && u > v * 1.4) return 'x';
        return 'y';
      },
      motion
    });
  }
}
