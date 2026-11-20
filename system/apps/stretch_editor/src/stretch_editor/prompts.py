"""What the model is told when it writes one size of a document.

Every size is a complete rendering of the whole piece -- compression or
expansion, never truncation -- anchored on the author's locked versions and
kept continuous with its neighbours so the reader sees the text grow and
shrink when zooming. Each prompt is stored with the version it produced, so
the exact instructions behind any version can be shown to the user.
"""

# Bump when the instructions change: every model-written size is then rewritten under the new ones.
PROMPT_VERSION = 2

from stretch_editor.sizes import FORMS, WORD_RE, Spec, describe_spec, measure_text, size_of, syllables

DISTILL_SYSTEM = """You are a meticulous literary editor preparing a brief for writers who will \
rewrite a piece at many different lengths, from a single word up to longer than the original. \
Your brief is the shared understanding every length must honour, so it must be precise, \
faithful to the author, and free of your own opinions about the piece."""

DISTILL_INSTRUCTIONS = """Read the author's versions of the piece below and write the brief.

Cover, in this order, under these plain headings:

THESIS: the whole point of the piece in one standalone sentence that a reader who has never seen it \
would fully understand: name the subject outright, state the claim, and include what the piece asks \
of the reader or where it lands. Not a quotation and not a sentence that could appear inside the piece.
WHOLE PIECE IN ONE SENTENCE: three different candidate sentences, each a complete, natural, \
grammatical sentence that renders the entire piece -- its premise, its claim and its close -- for a \
cold reader. Best first. These are summaries, like a subtitle or an abstract, never excerpts.
ARC: how the piece moves from beginning to end (opening move, development, turn, close), as a \
short numbered list. Note where the turn or ending gets its force.
POINTS BY IMPORTANCE: every substantive idea, claim, example or image, ranked from essential to \
expendable. Mark which ones must survive even in a single sentence.
VOICE: person, tense, register, rhythm, sentence habits, humour or warmth, what it avoids.
SIGNATURE LANGUAGE: the distinctive phrases, images and words that make it recognisably this \
piece and should be reused at longer sizes where they fit.
FORM: for verse, the stanza shape, rhyme scheme (with the rhyme words), meter and line lengths. \
For prose, paragraphing habits.
ONE WORD: the three best single words for the whole piece, best first, with one line each on why.
DO NOT: anything a rewriter might be tempted to add or change that would betray the piece \
(invented facts, a moral the author didn't draw, a change of stance).

Be concrete. Quote the text. No preamble."""

WRITE_SYSTEM = """You are a master writer and editor who rewrites a piece at an exact length while \
keeping it unmistakably the same piece. You are one part of a "stretchtext" editor: the author \
writes a piece once, and the reader can zoom smoothly from a single word up to a long version and \
back. You write one size at a time.

The governing rule: every size is the WHOLE piece, rendered at that length. It is a compression or \
an expansion, never an excerpt or a truncation. A reader who sees only this version should get the \
complete thesis, the arc and the ending, in the author's voice, as well as this length allows.

How to compress:
- Short versions (one to three sentences) are summaries of the whole piece, never excerpts. Write \
them for a reader who has never seen the piece: name the subject outright, and use nothing that \
depends on context the reader doesn't have ("the rush", "that hour", "it", "this"). Cover the \
premise, the claim and where the piece lands (its ending or what it asks of the reader). The test: \
it could serve as the piece's subtitle or abstract. If it reads like a sentence lifted from the \
middle of the piece, or could have been one, it is wrong -- start again.
- At one paragraph, the paragraph carries the thesis, the strongest support and the ending's turn, \
and still opens by telling a cold reader what the piece is about.
- Keep the ending. The close or turn of the piece must survive at every size above a single word; \
merge or drop middle material first.
- Merge, don't list. Fold several points into one well-made sentence rather than dropping the \
ending or producing a summary that reads like notes.
- Use the author's images and signature phrases where they serve the version, but never at the \
cost of it standing on its own. At a paragraph and longer, reuse wording from the nearest written \
sizes so zooming in and out reads as growth and shrinkage, not replacement.
- At a single word, choose the one word that, read alone, carries the whole piece. Give the word \
alone, with a full stop.

Grammar is never traded for length:
- Every sentence is complete, grammatical, natural English that a good editor would sign off on. \
Never drop a verb, never use telegraphic or elliptical constructions ("not how much"), never \
fragments, to hit a word count.
- Word counts are soft targets. When a small count can't hold everything, carry fewer ideas in a \
complete sentence rather than more ideas in a broken one.

How to expand (when asked for more than the author has written):
- Develop only what is already there: make implicit reasoning explicit, give existing examples \
more texture, add the transitions a longer piece needs, let images breathe.
- Never invent facts, figures, names, quotations, events, studies or claims the author did not make \
or clearly imply. Never repeat yourself or restate a point to fill space.

Always:
- Same voice, person, tense, register and stance as the author. Same audience. No new moral.
- It must read as finished, polished writing on its own -- good enough to publish at this size. \
No ellipses standing in for cut material, no "in short", no meta-commentary.
- Hit the requested shape (paragraph, sentence and line counts are hard requirements; word \
and syllable counts are targets to stay close to; a character limit is a hard ceiling).

Verse:
- Every line must be new. Never repeat a line, a half-line or a refrain, and never pad with echoes, \
unless the author's original itself repeats that line.
- Keep the author's rhyme scheme and meter when the line count allows. For a different line count, \
use the natural form for it (one line: a single complete image; two: a rhymed couplet; four: a \
quatrain keeping the original's rhyme pattern) and keep the original's rhyme words where you can.
- Keep the central images and the final turn. Count syllables aloud, line by line.

Output only the text of this version: no title, no notes, no quotation marks around it, no \
markdown. Prose paragraphs are separated by one blank line. Verse lines are separated by line \
breaks, stanzas by one blank line."""


def rhyme_words(rhyme: str | None) -> str:
    """A rhyme pattern for grid or metered poems, in words."""
    return {
        "aabb": "rhymed couplets (AABB)",
        "abab": "alternating rhyme (ABAB)",
        "abba": "enclosed rhyme (ABBA)",
        "none": "no rhyme at all (blank or free verse)",
    }.get(rhyme or "", "")


def distill_prompt(mode: str, anchors: list[tuple[Spec, str]], brief: str) -> str:
    """The prompt asking for the shared brief every size is written against."""
    parts = []
    if brief.strip():
        parts.append(f"THE AUTHOR'S OWN DESCRIPTION OF WHAT THEY ARE GOING FOR:\n{brief.strip()}")
    for spec, text in sorted(anchors, key=lambda a: -size_of(a[0])):
        parts.append(f"AUTHOR'S VERSION ({describe_spec(spec)}):\n{text}")
    parts.append(DISTILL_INSTRUCTIONS)
    return "\n\n---\n\n".join(parts)


def shape_instructions(spec: Spec) -> str:
    """The exact shape a version must have, in words the model can follow. Prose has one target length."""
    if spec.mode == "prose":
        n, unit = spec.value or 1, spec.unit or "words"
        if unit == "words" and n == 1:
            return "A single word, followed by a full stop. Nothing else."
        if unit == "chars":
            return (
                f"HARD LIMIT: at most {n} characters in total, counting spaces and punctuation, and use most of "
                f"that room (at least {round(n * 0.8)} characters). Sentence and paragraph count are up to you."
            )
        if unit == "words":
            lo, hi = max(1, round(n * 0.9)), max(2, round(n * 1.1))
            return (
                f"About {n} words in total (between {lo} and {hi}). Vary sentence length naturally; sentence and "
                "paragraph count are up to you."
            )
        if unit == "sentences":
            return (
                f"Exactly {n} sentence{'s' if n != 1 else ''}. Let sentence lengths vary naturally"
                + ("; paragraph them where the piece naturally divides." if n > 4 else ", in one paragraph.")
            )
        lines = [
            f"Exactly {n} paragraph{'s' if n != 1 else ''}, each of natural length for this piece "
            "(as long as the author's own paragraphs tend to be, varying as the content needs)."
        ]
        if n > 1:
            lines.append(
                "Map the piece's arc onto the paragraphs: the first opens with the thesis or the original's "
                "opening move, the last delivers the closing turn, the middle carries the most important "
                "remaining points in order."
            )
        return "\n".join(lines)
    if spec.form in FORMS:
        info = FORMS[spec.form]
        scheme = next((x for x in info["schemes"] if x["id"] == spec.scheme), info["schemes"][0])
        parts = [f"A {info['name']}: exactly {info['lines']} lines.", info["guide"]]
        if scheme["id"] != "none":
            parts.append(
                f"Rhyme scheme: {scheme['label']} (each letter is a rhyme sound; a capital R is a refrain; "
                "lower-case letters in a repeating form mark a rhyme without the refrain line)."
            )
        if info.get("meter"):
            parts.append(f"Meter: {info['meter']}. Read each line aloud and mark its stresses.")
        if info.get("syllables"):
            parts.append("Syllables per line: " + ", ".join(str(n) for n in info["syllables"]) + " (count them aloud).")
        if not info.get("repeats"):
            parts.append("No line may repeat or echo another.")
        return "\n".join(parts)
    if spec.meter:
        n = spec.lines or 1
        meter = {
            "iambic-pentameter": "iambic pentameter: five iambs per line, da-DUM da-DUM da-DUM da-DUM da-DUM, "
            "ten syllables (an occasional feminine ending or inverted first foot is fine)",
            "trochaic-tetrameter": "trochaic tetrameter: four trochees per line, DUM-da DUM-da DUM-da DUM(-da), "
            "seven or eight syllables, the falling chant of 'Tyger Tyger, burning bright'",
            "anapestic-tetrameter": "anapestic tetrameter, the Dr. Seuss rhythm: four anapests per line, "
            "da-da-DUM da-da-DUM da-da-DUM da-da-DUM, eleven or twelve syllables (the first foot may drop a "
            "syllable), as in 'The sun did not shine, it was too wet to play'",
        }[spec.meter]
        rhyme = rhyme_words(spec.rhyme) or (
            "rhymed couplets" if spec.meter == "anapestic-tetrameter" else "a rhyme scheme suited to the piece"
        )
        return (
            f"Exactly {n} line{'s' if n != 1 else ''}, every one in {meter}. Read each line aloud and mark its "
            f"stresses; the meter must be audible on every line. Use {rhyme} unless one line makes rhyme "
            "impossible. No line may repeat or echo another."
        )
    n, y = spec.lines or 1, spec.syllables or 1
    lines = [f"Exactly {n} line{'s' if n != 1 else ''} of verse."]
    if spec.rhyme:
        lines.append(f"Use {rhyme_words(spec.rhyme)} throughout.")
    lines.append(f"Each line about {y} syllable{'s' if y != 1 else ''} (stay within one syllable either way).")
    if n == 1:
        lines.append("One complete image that carries the whole poem.")
    elif n >= 6:
        lines.append("Use stanza breaks where the original's form suggests them.")
    lines.append("No line may repeat or echo another line.")
    return "\n".join(lines)


def write_prompt(
    *,
    target: Spec,
    brief: str,
    note: str,
    core: str,
    anchors: list[tuple[Spec, str]],
    source_mode: str,
    neighbours: list[tuple[Spec, str]],
    feedback: str = "",
) -> str:
    """The prompt for writing one size."""
    parts = []
    if brief.strip():
        parts.append(f"WHAT THE AUTHOR IS GOING FOR (applies to every size):\n{brief.strip()}")
    if core.strip():
        parts.append(f"EDITOR'S BRIEF ON THE PIECE (shared by every size):\n{core.strip()}")

    locked = sorted(anchors, key=lambda a: size_of(a[0]))
    if locked:
        intro = (
            "THE AUTHOR'S LOCKED VERSIONS. These are the source of truth for content, tone and wording. "
            "Never contradict them."
        )
        if source_mode != target.mode:
            intro += (
                f" They are {source_mode}; you are rendering the piece as {target.mode}, keeping its thesis, "
                "images and tone."
            )
        parts.append(intro)
        for spec, text in locked:
            parts.append(f"LOCKED VERSION ({describe_spec(spec)}):\n{text}")

    target_size = size_of(target)
    smaller = [a for a in locked if a[0].mode == target.mode and size_of(a[0]) < target_size]
    larger = [a for a in locked if a[0].mode == target.mode and size_of(a[0]) > target_size]
    if smaller and larger:
        parts.append(
            "YOUR SIZE SITS BETWEEN TWO LOCKED VERSIONS: include everything the smaller one says, and nothing "
            "the larger one doesn't support. Where you can, keep the smaller one's phrasing as the skeleton "
            "and add from the larger one."
        )
    elif larger:
        parts.append("YOU ARE COMPRESSING: your size is shorter than the author's locked version(s).")
    elif smaller or (locked and source_mode == target.mode):
        parts.append(
            "YOU ARE EXPANDING beyond everything the author has written. Develop what is there; invent "
            "nothing."
        )

    if neighbours:
        parts.append(
            "NEARBY SIZES ALREADY WRITTEN (not locked). Stay consistent with them and reuse their wording "
            "where it fits, so zooming between sizes reads as the same text growing or shrinking:"
        )
        for spec, text in neighbours:
            parts.append(f"NEARBY ({describe_spec(spec)}):\n{text}")

    if not locked:
        parts.append(
            "THERE IS NO TEXT YET. The author has only described what they want (above). Write an original "
            "piece that fulfils that description, at this size, as if it were the author's own."
        )
    if note.strip():
        parts.append(f"THE AUTHOR'S NOTE FOR THIS SIZE:\n{note.strip()}")

    parts.append(f"WRITE THIS SIZE: {describe_spec(target)}\n{shape_instructions(target)}")
    if feedback:
        parts.append(f"YOUR PREVIOUS ATTEMPT WAS SENT BACK. Fix this and write it again:\n{feedback}")
    return "\n\n---\n\n".join(parts)


def shape_problems(spec: Spec, text: str) -> list[str]:
    """What, if anything, is wrong with a version's shape. Empty when it is acceptable."""
    problems: list[str] = []
    m = measure_text(text, spec.mode)
    if not text.strip():
        return ["The version was empty."]
    if spec.mode == "prose":
        n, unit = spec.value or 1, spec.unit or "words"
        if unit == "words" and n == 1:
            if m.words != 1:
                problems.append(f"It must be a single word; it had {m.words} words.")
        elif unit == "chars":
            if m.chars > n:
                problems.append(f"It must be at most {n} characters; it was {m.chars}.")
            elif m.chars < n * 0.6:
                problems.append(f"It should use most of its {n} characters; it was only {m.chars}.")
        elif unit == "words":
            if not (0.8 * n <= m.words <= 1.2 * n + 1):
                problems.append(f"It should be about {n} words; it was {m.words}.")
        elif unit == "sentences":
            if m.sentences != n:
                problems.append(f"It needs exactly {n} sentences; it had {m.sentences}.")
        elif m.paragraphs != n:
            problems.append(f"It needs exactly {n} paragraphs; it had {m.paragraphs}.")
    elif spec.meter:
        want = {"iambic-pentameter": (9, 11), "trochaic-tetrameter": (6.5, 8.5), "anapestic-tetrameter": (10, 13)}[spec.meter]
        if m.lines != (spec.lines or 1):
            problems.append(f"It needs exactly {spec.lines} lines; it had {m.lines}.")
        elif not (want[0] <= m.syllables_per_line <= want[1]):
            problems.append(
                f"Lines in this meter run {want[0]:g}-{want[1]:g} syllables; these averaged {m.syllables_per_line:.1f}."
            )
    elif spec.form in FORMS:
        info = FORMS[spec.form]
        if m.lines != info["lines"]:
            problems.append(f"A {info['name'].lower()} has exactly {info['lines']} lines; it had {m.lines}.")
        elif info.get("syllables"):
            pattern = info["syllables"]
            counts = [sum(syllables(w) for w in WORD_RE.findall(line)) for line in m.line_texts]
            slack = 1 if len(pattern) <= 5 and max(pattern) <= 8 else 2
            off = [f"line {i + 1} has about {c} (wants {want})" for i, (c, want) in enumerate(zip(counts, pattern))
                   if abs(c - want) > slack]
            if off:
                problems.append("Syllable counts are off: " + "; ".join(off) + ".")
    else:
        n, y = spec.lines or 1, spec.syllables or 1
        if m.lines != n:
            problems.append(f"It needs exactly {n} lines; it had {m.lines}.")
        seen: dict[str, int] = {}
        for line in m.line_texts:
            norm = "".join(ch for ch in line.lower() if ch.isalnum())
            seen[norm] = seen.get(norm, 0) + 1
        repeats = [line for line in m.line_texts if seen["".join(ch for ch in line.lower() if ch.isalnum())] > 1]
        if repeats:
            problems.append(f"Lines repeat ({repeats[0]!r}); every line must be new.")
        if m.lines and abs(m.syllables_per_line - y) > 2:
            problems.append(f"Lines should be about {y} syllables; they averaged {m.syllables_per_line:.1f}.")
    return problems


REVIEW_SYSTEM = """You are a demanding senior editor reviewing one short version of a piece that an \
author's tool produced. You judge it against the author's full text. You are hard to satisfy: you \
pass only writing you would publish under the author's name."""


def review_prompt(target: Spec, text: str, anchors: list[tuple[Spec, str]], brief: str) -> str:
    """Ask an editor whether a short version really renders the whole piece."""
    parts = []
    if brief.strip():
        parts.append(f"WHAT THE AUTHOR IS GOING FOR:\n{brief.strip()}")
    for spec, anchor in sorted(anchors, key=lambda a: -size_of(a[0])):
        parts.append(f"THE AUTHOR'S VERSION ({describe_spec(spec)}):\n{anchor}")
    parts.append(f"THE SHORT VERSION TO REVIEW (asked for: {describe_spec(target)}):\n{text}")
    parts.append(f"THE SHAPE IT WAS ASKED FOR:\n{shape_instructions(target)}")
    parts.append(
        """Check each of these:
1. Is every sentence complete, grammatical, natural English -- no dropped verbs, no telegraphic or \
elliptical phrasing, no fragments?
2. Does it stand on its own for a reader who has never seen the piece -- subject named, nothing that \
depends on missing context?
3. Does it render the WHOLE piece (premise, claim, and where it lands), rather than reading like a \
sentence pulled out of it?
4. Is it in the author's tone, without adding anything the author didn't say?
5. For verse: does it follow the asked-for form exactly -- line count, rhyme scheme, refrains, meter?
(For a single word, judge only whether it is the best word for the whole piece.)

If it passes all four, answer exactly: PASS
Otherwise answer: REVISE: followed by one short paragraph saying precisely what is wrong and what a \
better version must do. Do not write the replacement yourself."""
    )
    return "\n\n---\n\n".join(parts)


def is_short(spec: Spec) -> bool:
    """Short sizes are summaries; they get an editor's review and are written without neighbours."""
    if spec.mode == "prose":
        n, unit = spec.value or 1, spec.unit or "words"
        return (unit == "words" and n <= 40) or (unit == "sentences" and n <= 3) or (unit == "chars" and n <= 300)
    return bool(spec.form) or (spec.lines or 1) <= 2
