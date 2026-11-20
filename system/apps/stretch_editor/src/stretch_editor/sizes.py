"""Sizes: what a version's shape is called, how text is counted, and which sizes get written ahead.

Mirrors ``static/textkit.js`` (the browser's copy) -- keys must match exactly.
"""

import json
import math
import re
from pathlib import Path
from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

Mode = Literal["prose", "poetry"]


Unit = Literal["chars", "words", "sentences", "paragraphs"]
# The classic verse forms (line count, rhythm, rhyme schemes, how to write them), shared with the page.
_CATALOG = json.loads((Path(__file__).parent / "static" / "forms.json").read_text())
FORMS: dict[str, dict] = {f["id"]: f for f in _CATALOG["forms"]}
RHYMES: dict[str, str] = {r["id"]: r["label"] for r in _CATALOG["rhymes"] if r["id"]}
Meter = Literal["iambic-pentameter", "trochaic-tetrameter", "anapestic-tetrameter"]
# Syllables a line of each meter usually has (trochaic lines often drop the last weak beat: 7 or 8).
METER_SYLLABLES = {"iambic-pentameter": 10, "trochaic-tetrameter": 7, "anapestic-tetrameter": 12}
METER_NAME = {
    "iambic-pentameter": "iambic pentameter",
    "trochaic-tetrameter": "trochaic tetrameter",
    "anapestic-tetrameter": "anapestic tetrameter",
}
UNIT_LETTER = {"chars": "c", "words": "w", "sentences": "s", "paragraphs": "p"}
# Rough words per unit, used only to order sizes from shortest to longest.
WORDS_PER_UNIT = {"chars": 1 / 5.8, "words": 1.0, "sentences": 18.0, "paragraphs": 70.0}


class Spec(FrozenModel):
    """One size of a document. Prose is one length in one unit (e.g. 280 characters, 3 paragraphs);
    verse is lines x syllables per line."""

    mode: Mode = Field(description="prose or poetry")
    unit: Unit | None = Field(default=None, description="Prose: what the length is measured in")
    value: int | None = Field(default=None, ge=1, le=100000, description="Prose: the length in that unit")
    lines: int | None = Field(default=None, ge=1, le=80, description="Poetry: number of lines")
    syllables: int | None = Field(default=None, ge=1, le=30, description="Poetry: syllables per line")
    form: str | None = Field(default=None, description="Poetry: a classic form (haiku, sonnet, ...) instead of a grid size")
    scheme: str | None = Field(default=None, description="Poetry: the form's rhyme scheme, when it has several")
    rhyme: str | None = Field(default=None, description="Poetry: rhyme pattern for a grid or metered poem (aabb, abab, abba, none)")
    meter: Meter | None = Field(default=None, description="Poetry: a set meter; then only the number of lines varies")
    # Sizes saved before prose became one length; read only to carry them over.
    paragraphs: int | None = Field(default=None, description="Older prose sizes only")
    sentences: int | None = Field(default=None, description="Older prose sizes only")
    words: int | None = Field(default=None, description="Older prose sizes only")
    chars: int | None = Field(default=None, description="Older prose sizes only")


def prose(unit: Unit, value: int) -> Spec:
    return Spec(mode="prose", unit=unit, value=value)


def poem(lines: int, syllables: int) -> Spec:
    return Spec(mode="poetry", lines=lines, syllables=syllables)


def metered(meter: Meter, lines: int, rhyme: str | None = None) -> Spec:
    return Spec(mode="poetry", meter=meter, lines=lines, syllables=METER_SYLLABLES[meter], rhyme=rhyme)


def verse_form(form: str, scheme: str | None = None) -> Spec:
    """A classic form. ``scheme`` is kept only when it is one of the form's own and not its first (default)."""
    info = FORMS[form]
    schemes = [x["id"] for x in info["schemes"]]
    keep = scheme if scheme in schemes[1:] else None
    pattern = info.get("syllables")
    syllables = round(sum(pattern) / len(pattern)) if pattern else 10
    return Spec(mode="poetry", form=form, scheme=keep, lines=info["lines"], syllables=syllables)


def normalize(spec: Spec) -> Spec:
    """Keep only the fields of the spec's mode, carrying an older prose size over to one length."""
    if spec.mode == "poetry":
        if spec.form in FORMS:
            return verse_form(spec.form, spec.scheme)
        rhyme = spec.rhyme if spec.rhyme in RHYMES else None
        if spec.meter:
            return metered(spec.meter, spec.lines or 1, rhyme)
        return poem(spec.lines or 1, spec.syllables or 1).model_copy(update={"rhyme": rhyme})
    if spec.unit and spec.value:
        return prose(spec.unit, spec.value)
    p, s, w = spec.paragraphs or 1, spec.sentences or 1, spec.words or 1
    if spec.chars:
        return prose("chars", spec.chars)
    if p > 1:
        return prose("paragraphs", p)
    if s >= 5:
        return prose("paragraphs", 1)
    if s > 1:
        return prose("sentences", s)
    if w > 12:
        return prose("sentences", 1)
    return prose("words", w)


def key_for(spec: Spec) -> str:
    if spec.mode == "prose":
        return f"{UNIT_LETTER[spec.unit or 'words']}{spec.value or 1}"
    if spec.form:
        return spec.form + (f"~{spec.scheme}" if spec.scheme else "")
    suffix = f"-r{spec.rhyme}" if spec.rhyme else ""
    if spec.meter:
        return f"{spec.meter}-l{spec.lines}{suffix}"
    return f"l{spec.lines}-y{spec.syllables}{suffix}"


def version_key(spec: Spec) -> str:
    return spec.mode + ":" + key_for(spec)


def size_of(spec: Spec) -> float:
    if spec.mode == "prose":
        return (spec.value or 1) * WORDS_PER_UNIT[spec.unit or "words"]
    return float((spec.lines or 1) * (spec.syllables or 1))


def _plural(n: int, one: str) -> str:
    return f"{n} {one}" + ("" if n == 1 else "s")


UNIT_NAME = {"chars": "character", "words": "word", "sentences": "sentence", "paragraphs": "paragraph"}


def describe_spec(spec: Spec) -> str:
    if spec.mode == "prose":
        n = spec.value or 1
        if spec.unit == "words" and n == 1:
            return "one word"
        return _plural(n, UNIT_NAME[spec.unit or "words"])
    if spec.form in FORMS:
        info = FORMS[spec.form]
        scheme = next((x["label"] for x in info["schemes"] if x["id"] == spec.scheme), info["schemes"][0]["label"])
        return f"a {info['name']} ({info['lines']} lines, {scheme})"
    rhyme = f", rhymed {RHYMES[spec.rhyme]}" if spec.rhyme in RHYMES and spec.rhyme != "none" else (
        ", unrhymed" if spec.rhyme == "none" else "")
    if spec.meter:
        return f"{_plural(spec.lines or 1, 'line')} of {METER_NAME[spec.meter]}{rhyme}"
    return f"{_plural(spec.lines or 1, 'line')} of verse, about {spec.syllables} syllables per line{rhyme}"
    return f"{_plural(spec.lines or 1, 'line')} of verse, about {spec.syllables} syllables per line"


WORD_RE = re.compile(r"[^\W_][\w'’\-]*", re.UNICODE)
SENTENCE_RE = re.compile(r"[^.!?…]+(?:[.!?…]+[\"'”’)]*|$)")


def split_paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in SENTENCE_RE.findall(paragraph) if s.strip()]


def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    groups = re.findall(r"[aeiouy]{1,2}", w)
    return max(1, len(groups))


class Measure(FrozenModel):
    paragraphs: int = 0
    sentences: int = 0
    words: int = 0
    chars: int = 0
    lines: int = 0
    syllables_per_line: float = 0.0
    line_texts: tuple[str, ...] = ()


def measure_text(text: str, mode: str) -> Measure:
    words = len(WORD_RE.findall(text))
    if mode == "prose":
        ps = split_paragraphs(text)
        return Measure(
            paragraphs=len(ps),
            sentences=sum(len(split_sentences(p)) for p in ps),
            words=words,
            chars=len(text.strip()),
        )
    lines = tuple(line.strip() for line in text.split("\n") if line.strip())
    total = sum(syllables(w) for line in lines for w in WORD_RE.findall(line))
    return Measure(
        words=words,
        chars=len(text.strip()),
        lines=len(lines),
        syllables_per_line=total / len(lines) if lines else 0.0,
        line_texts=lines,
    )


def spec_for_text(text: str, mode: Mode) -> Spec:
    """The size that best describes a piece of text: its paragraph count when it has several, else its words."""
    m = measure_text(text, mode)
    if mode == "prose":
        if m.paragraphs > 1:
            return prose("paragraphs", m.paragraphs)
        return prose("words", max(1, m.words))
    syl = max(1, round(m.syllables_per_line)) if m.lines else 1
    return poem(max(1, min(m.lines, 80)), min(syl, 30))


def ladder(mode: Mode, specs_present: list[Spec], pill_specs: list[Spec], locked: list[Spec]) -> list[Spec]:
    """The sizes written ahead of time for a mode, shortest first: a ladder from one word up a little
    past the largest locked version, plus every saved pill and every version already written.

    The top is measured from locked versions only, so writing the longest size never extends the ladder.
    """
    mine = [s for s in specs_present if s.mode == mode]
    anchors = [s for s in locked if s.mode == mode]
    steps: list[Spec] = []
    if mode == "prose":
        top = max([s.value or 1 for s in anchors if s.unit == "paragraphs"] + [1])
        steps += [
            prose("words", 1),
            prose("words", 8),
            prose("sentences", 1),
            prose("chars", 280),
            prose("sentences", 3),
            prose("paragraphs", 1),
        ]
        # Paragraph stops spread out as they grow (1, 2, 3, 4, 6, 8, 12, 16, 24, 32 ...), up to one past the
        # longest locked version, so a long piece doesn't get a stop at every paragraph count.
        spread = [2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64]
        steps += [prose("paragraphs", p) for p in spread if p <= max(top + 1, 3)]
        if top + 1 not in spread:
            steps.append(prose("paragraphs", top + 1))
    else:
        top = max([s.lines or 1 for s in anchors] + [2])
        steps += [poem(1, 1), poem(1, 4), poem(1, 8), poem(2, 8), poem(4, 8)]
        steps += [poem(n, 10) for n in range(6, max(top + 2, 8) + 1, 2)]
    steps += mine + [s for s in pill_specs if s.mode == mode]
    unique = {version_key(normalize(s)): normalize(s) for s in steps}
    return sorted(unique.values(), key=size_of)


def distance(a: Spec, b: Spec) -> float:
    """How far apart two sizes are, on a log scale (so 1 word vs 8 is as far as 100 vs 800)."""
    return abs(math.log(size_of(a) + 1) - math.log(size_of(b) + 1))
