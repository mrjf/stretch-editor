"""Documents on disk and the background writer that keeps every size up to date.

A document's ``rev`` goes up whenever something every size depends on changes
(a locked version, the overall brief). A version written against an older rev
is stale: it stays visible but is rewritten in the background. Requests for one
specific size jump the queue.
"""

import difflib
import math
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

from stretch_editor import prompts
from stretch_editor import providers
from stretch_editor.providers import ProviderError
from stretch_editor.sizes import (
    Mode,
    Spec,
    distance,
    ladder,
    normalize,
    poem,
    prose,
    size_of,
    FORMS,
    metered,
    spec_for_text,
    verse_form,
    version_key,
)

DEFAULT_MODELS = ["claude-opus-5-5"]
DEFAULT_FILL = {"prose": "stops", "poetry": "grid"}
FILL_LEVELS = {"prose": ("open", "stops", "dense"), "poetry": ("open", "stops", "grid")}
GRID_COLS = 16
# Background writers at once: a setting (default 10, up to MAX_WRITERS), changeable while running.
DEFAULT_WRITERS = 10
MAX_WRITERS = 32
# Writers held back for the sizes the author is looking at, so those start at once however busy the
# background is.
FOREGROUND_WORKERS = 2
BACKGROUND_DELAY_SECONDS = 2.5
NOTHING_TO_WRITE_FROM = "Nothing to write from yet: write your own version, or describe what you want in Prompts."


class Version(FrozenModel):
    """One size of a document."""

    spec: Spec = Field(description="The size this version is written at")
    text: str = Field(description="The text of this version")
    status: Literal["locked", "ready", "error"] = Field(
        description="locked: the author's own or approved text, never rewritten; ready: written by the model; "
        "error: writing failed"
    )
    rev: int = Field(description="The document revision this version was written against")
    written_at: str = Field(description="When this version was written (UTC, ISO 8601)")
    log_id: str | None = Field(default=None, description="The id of the model call that wrote it, if any")
    model: str | None = Field(default=None, description="The model that wrote it; none for the author's own text")
    human_share: float = Field(
        default=0.0, description="How much of the text the author wrote themselves: 1 typed from scratch, 0 untouched model text"
    )
    error: str | None = Field(default=None, description="Why writing failed, when status is error")


class Pill(FrozenModel):
    """A saved size the author can jump to."""

    id: str = Field(description="Stable id")
    label: str = Field(description="What the pill says")
    spec: Spec = Field(description="The size it jumps to")
    note: str = Field(default="", description="The author's instructions for this size")


class Doc(FrozenModel):
    """A document and all its sizes."""

    id: str = Field(description="Stable id")
    title: str = Field(description="Title")
    mode: Mode = Field(description="The mode currently shown")
    brief: str = Field(default="", description="What the author is going for, applied to every size")
    notes: dict[str, str] = Field(default_factory=dict, description="Author's notes per size, by version key")
    pills: list[Pill] = Field(default_factory=list, description="Saved sizes")
    versions: dict[str, Version] = Field(default_factory=dict, description="Every written size, by version key")
    rev: int = Field(default=1, description="Goes up when a locked version or the brief changes")
    core: str = Field(default="", description="The editor's brief on the piece every size is written against")
    core_rev: int = Field(default=0, description="The revision the core brief was written for")
    spent_usd: float = Field(default=0.0, description="Total model cost so far")
    models: list[str] = Field(default_factory=lambda: list(DEFAULT_MODELS), description="Models that write every size")
    cores: dict[str, str] = Field(default_factory=dict, description="Each model's brief on the piece, keyed 'model@rev'")
    spent_by_model: dict[str, float] = Field(default_factory=dict, description="Model cost per model")
    fill: dict[str, str] = Field(
        # Documents saved before this setting existed keep writing just the slider stops; new documents
        # are created with DEFAULT_FILL.
        default_factory=lambda: {"prose": "stops", "poetry": "stops"},
        description="How much each mode writes ahead in the background: 'open' (only sizes the author opens), "
        "'stops' (slider stops and pills), 'dense' (prose: many more lengths) or 'grid' (poetry: every grid cell)",
    )
    meters: list[str] = Field(default_factory=list, description="Meters the author has opened; written ahead too")
    prompt_version: int = Field(default=1, description="The instructions version the current revision was written under")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_pills() -> list[Pill]:
    return [
        Pill(id="one-word", label="One word", spec=prose("words", 1)),
        Pill(id="one-sentence", label="One sentence", spec=prose("sentences", 1)),
        Pill(id="tweet", label="Tweet", spec=prose("chars", 280), note="Fits in a tweet. No hashtags or emoji."),
        Pill(id="one-paragraph", label="One paragraph", spec=prose("paragraphs", 1)),
        Pill(id="one-line", label="One line", spec=poem(1, 8)),
        Pill(id="couplet", label="Couplet", spec=poem(2, 8), note="A rhymed couplet."),
        Pill(id="quatrain", label="Quatrain", spec=poem(4, 8)),
        Pill(id="haiku", label="Haiku", spec=verse_form("haiku")),
        Pill(id="limerick", label="Limerick", spec=verse_form("limerick")),
    ]


class Store:
    """Documents as one JSON file each under ``<data>/docs/``, plus a log of every model call."""

    def __init__(self, data_dir: Path) -> None:
        self.docs_dir = data_dir / "docs"
        self.log_dir = data_dir / "log"
        self.lock = threading.RLock()

    def path(self, doc_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]{1,80}", doc_id):
            raise KeyError(doc_id)
        return self.docs_dir / f"{doc_id}.json"

    def list(self) -> list[Doc]:
        with self.lock:
            if not self.docs_dir.is_dir():
                return []
            docs = [Doc.model_validate_json(p.read_text()) for p in self.docs_dir.glob("*.json")]
        return sorted(docs, key=lambda d: d.title.lower())

    def get(self, doc_id: str) -> Doc:
        with self.lock:
            p = self.path(doc_id)
            if not p.is_file():
                raise KeyError(doc_id)
            return Doc.model_validate_json(p.read_text())

    def put(self, doc: Doc) -> None:
        with self.lock:
            self.docs_dir.mkdir(parents=True, exist_ok=True)
            p = self.path(doc.id)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(doc.model_dump_json(indent=1, exclude_none=True))
            tmp.replace(p)

    def update(self, doc_id: str, change: Callable[[Doc], Doc]) -> Doc:
        with self.lock:
            doc = change(self.get(doc_id))
            self.put(doc)
            return doc

    def write_log(self, entry: dict[str, object]) -> str:
        log_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / f"{log_id}.json").write_text(json.dumps(entry, indent=1))
        return log_id

    def read_log(self, log_id: str) -> dict[str, object]:
        if not re.fullmatch(r"[0-9a-f-]{1,40}", log_id):
            raise KeyError(log_id)
        p = self.log_dir / f"{log_id}.json"
        if not p.is_file():
            raise KeyError(log_id)
        return json.loads(p.read_text())


def is_stale(doc: Doc, v: Version) -> bool:
    return v.status != "locked" and v.rev != doc.rev


def usable_models(doc: Doc) -> list[str]:
    """The document's chosen models that a connected account can run right now."""
    available = {m.id for m in providers.available_models()}
    return [m for m in doc.models if m in available]


def draft_key(spec: Spec, model: str) -> str:
    """Where one model's version of a size is kept. The author's (and every locked) version is at version_key."""
    return version_key(spec) + "@" + model


def locked_versions(doc: Doc) -> list[Version]:
    return [v for v in doc.versions.values() if v.status == "locked"]


def grid_rows(doc: Doc) -> int:
    """How many lines the poetry grid offers: a few more than the longest locked poem (the page's grid matches)."""
    tallest = max([v.spec.lines or 1 for v in locked_versions(doc) if v.spec.mode == "poetry"] + [8])
    return min(32, max(16, tallest + 6))


def planned(doc: Doc, mode: str) -> list[Spec]:
    """The sizes the background keeps written for a mode, at the document's write-ahead setting."""
    level = doc.fill.get(mode, DEFAULT_FILL[mode])
    if level == "open":
        return []
    stops = ladders(doc, include_present=False)[mode]
    if mode == "prose" and level == "dense":
        extra = [prose("words", n) for n in (3, 15, 25, 40, 60, 100, 150, 250, 400)]
        extra += [prose("sentences", n) for n in (2, 4, 5, 6, 8)]
        extra += [prose("chars", n) for n in (100, 500, 1000)]
        top = max([s.value or 1 for s in stops if s.unit == "paragraphs"] + [1])
        extra += [prose("paragraphs", n) for n in range(1, top + 3)]
        stops = stops + extra
    if mode == "poetry" and level == "grid":
        stops = stops + [poem(n, y) for n in range(1, grid_rows(doc) + 1) for y in range(1, GRID_COLS + 1)]
        stops += [metered(m, n) for m in doc.meters for n in range(1, grid_rows(doc) + 1)]  # type: ignore[arg-type]
        stops += [verse_form(f) for f in FORMS]
    elif mode == "poetry":
        stops += [metered(m, n) for m in doc.meters for n in (2, 4, 8, 14)]  # type: ignore[arg-type]
    unique = {version_key(s): s for s in stops}
    return list(unique.values())


def grid_distance(a: Spec, b: Spec) -> float:
    """How far apart two poem sizes are on the grid (rows and columns), on the same rough scale as distance()."""
    return (abs((a.lines or 1) - (b.lines or 1)) + abs((a.syllables or 1) - (b.syllables or 1))) / 8


def ladders(doc: Doc, include_present: bool = True) -> dict[str, list[Spec]]:
    """The zoom slider's stops per mode. Without include_present, just the stops the background writes."""
    specs = [v.spec for v in doc.versions.values()] if include_present else []
    pills = [p.spec for p in doc.pills]
    locked = [v.spec for v in locked_versions(doc)]
    return {m: ladder(m, specs + locked, pills, locked) for m in ("prose", "poetry")}


class Job(FrozenModel):
    doc_id: str
    spec: Spec
    model: str
    priority: float
    seq: int


class Engine:
    """The background writer: a small pool of threads working through a priority queue of sizes."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.cond = threading.Condition()
        self.queue: dict[tuple[str, str], Job] = {}
        self.running: set[tuple[str, str]] = set()
        self.timers: dict[str, threading.Timer] = {}
        self.focus: dict[str, Spec] = {}
        self.focus_model: dict[str, str] = {}
        self.core_locks: dict[str, threading.Lock] = {}
        self.seq = 0
        self.started = False
        self.writers = self._load_writers()

    def _settings_path(self) -> Path:
        return self.store.docs_dir.parent / "settings.json"

    def _load_writers(self) -> int:
        try:
            return max(1, min(MAX_WRITERS, int(json.loads(self._settings_path().read_text())["writers"])))
        except (OSError, ValueError, KeyError, TypeError):
            return DEFAULT_WRITERS

    def set_writers(self, n: int) -> int:
        """Change how many background writers run at once; takes effect immediately."""
        with self.cond:
            self.writers = max(1, min(MAX_WRITERS, n))
            self._settings_path().parent.mkdir(parents=True, exist_ok=True)
            self._settings_path().write_text(json.dumps({"writers": self.writers}))
            self.cond.notify_all()
        return self.writers

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        for i in range(FOREGROUND_WORKERS, FOREGROUND_WORKERS + MAX_WRITERS):
            threading.Thread(target=self._work, args=(i,), name=f"writer-{i}", daemon=True).start()
        for doc in self.store.list():
            if not any(p.spec.form for p in doc.pills):
                doc = self.store.update(doc.id, add_form_pills)
            if has_legacy_prose(doc):
                doc = self.store.update(doc.id, migrate_prose_sizes)
            if doc.id == "slow-mornings":
                self.store.update(doc.id, mark_sample_authorship)
            if any("@" not in k and v.status != "locked" for k, v in doc.versions.items()):
                self.store.update(doc.id, migrate)
            if doc.prompt_version != prompts.PROMPT_VERSION:
                # New instructions: treat it like an edit, so every model-written size is rewritten.
                self.store.update(
                    doc.id,
                    lambda d: d.model_copy(update={"rev": d.rev + 1, "prompt_version": prompts.PROMPT_VERSION}),
                )
            self.schedule_background(doc.id, delay=1.0)

    # ---------- queueing ----------

    def jobs(self, doc_id: str) -> tuple[list[str], list[str]]:
        with self.cond:
            writing = [k for d, k in self.running if d == doc_id]
            queued = [k for (d, k) in self.queue if d == doc_id and (d, k) not in self.running]
        return writing, queued

    def _enqueue(self, doc_id: str, spec: Spec, model: str, priority: float) -> None:
        key = (doc_id, draft_key(spec, model))
        with self.cond:
            existing = self.queue.get(key)
            if existing and existing.priority <= priority:
                return
            self.seq += 1
            self.queue[key] = Job(doc_id=doc_id, spec=spec, model=model, priority=priority, seq=self.seq)
            self.cond.notify()

    def _model_offset(self, doc: Doc, model: str) -> float:
        """A hair of priority for the model on screen, so every model's versions arrive together."""
        shown = self.focus_model.get(doc.id, doc.models[0] if doc.models else model)
        # Every chosen model fills in together; the one on screen only wins ties.
        return 0.0 if model == shown else 0.01 + 0.001 * (doc.models.index(model) if model in doc.models else 9)

    def request(self, doc_id: str, spec: Spec, model: str | None) -> None:
        """Write this size next (the author is looking at it), by every chosen model, the shown one first."""
        spec = normalize(spec)
        doc = self.store.get(doc_id)
        if spec.meter and spec.meter not in doc.meters:
            meter = spec.meter
            doc = self.store.update(doc_id, lambda d: d.model_copy(update={"meters": [*d.meters, meter]}))
            self.schedule_background(doc_id)
        self.focus[doc_id] = spec
        if model:
            self.focus_model[doc_id] = model
        shown = model if model in usable_models(doc) else (usable_models(doc) or [None])[0]
        current = draft_key(spec, shown) if shown else None
        with self.cond:
            # Only the version on screen keeps the held-back writers; anything requested earlier drops
            # back to the front of the regular queue.
            for key, job in list(self.queue.items()):
                if key[0] == doc_id and job.priority < 0 and key[1] != current:
                    self.queue[key] = job.model_copy(update={"priority": 0.0})
        for m in usable_models(doc):
            if m != shown:
                # Other models' versions of this size go first in the regular queue.
                self._enqueue(doc_id, spec, m, priority=0.0)
        if shown:
            self._start_now(doc_id, spec, shown)

    def _start_now(self, doc_id: str, spec: Spec, model: str) -> None:
        """Write the version on screen on its own writer, right now -- never behind the queue.

        If it is already being written (by the background), that writer simply finishes it.
        """
        key = (doc_id, draft_key(spec, model))
        with self.cond:
            if key in self.running:
                return
            self.queue.pop(key, None)
            self.running.add(key)

        def run() -> None:
            try:
                self.write(doc_id, spec, model, viewing=True)
            except Exception as exc:  # one failed size must not stop the editor
                logger.exception("Writing {} failed: {}", key, exc)
            finally:
                with self.cond:
                    self.running.discard(key)
                    self.cond.notify_all()

        threading.Thread(target=run, name=f"viewing-{key[1]}", daemon=True).start()

    def schedule_background(self, doc_id: str, delay: float = BACKGROUND_DELAY_SECONDS) -> None:
        """Queue every missing or stale size once the document has been quiet for ``delay`` seconds."""
        with self.cond:
            old = self.timers.pop(doc_id, None)
            if old:
                old.cancel()
            timer = threading.Timer(delay, self._fill, args=(doc_id,))
            timer.daemon = True
            self.timers[doc_id] = timer
            timer.start()

    def _fill(self, doc_id: str) -> None:
        try:
            doc = self.store.get(doc_id)
        except KeyError:
            return
        if not locked_versions(doc) and not doc.brief.strip():
            return
        modes = {doc.mode} | {v.spec.mode for v in doc.versions.values()}
        locked = [v.spec for v in locked_versions(doc)]
        focus = self.focus.get(doc_id)
        with self.cond:
            # Drop queued background work that is no longer needed; it is re-added below if still wanted.
            for key in [k for k, j in self.queue.items() if k[0] == doc_id and j.priority >= 0]:
                del self.queue[key]
        # The background keeps the sizes the document's write-ahead setting asks for current; any other
        # size is rewritten when the author goes back to it. Nearest the author's position goes first.
        models = usable_models(doc)
        for mode in modes:
            measure = grid_distance if mode == "poetry" else distance
            anchors = [s for s in locked if s.mode == mode] or locked
            if not anchors:
                # Written from the brief alone: start near the shortest sizes.
                anchors = [prose("words", 1) if mode == "prose" else poem(1, 1)]
            # Each model grows outward from what it has already written (and the locked versions).
            written = {
                m: [v.spec for k, v in doc.versions.items()
                    if k.endswith("@" + m) and v.spec.mode == mode and v.text and not is_stale(doc, v)]
                for m in models
            }
            for spec in planned(doc, mode):
                if (lk := doc.versions.get(version_key(spec))) and lk.status == "locked":
                    continue
                base = min(measure(spec, a) if a.mode == spec.mode else distance(spec, a) for a in anchors)
                if focus and focus.mode == mode:
                    base = min(base, measure(spec, focus))
                # Shorter sizes are quicker, cheaper and most often wanted, so they go first.
                shortness = 0.25 * math.log10(size_of(spec) + 1)
                for model in models:
                    near = min([base] + [measure(spec, w) for w in written[model]]) + shortness
                    v = doc.versions.get(draft_key(spec, model))
                    if v and not is_stale(doc, v) and v.status != "error":
                        continue
                    if v and v.status == "error" and v.rev == doc.rev:
                        continue
                    priority = near + (0 if mode == doc.mode else 5) + self._model_offset(doc, model)
                    self._enqueue(doc_id, spec, model, priority=priority)

    def _take(self, index: int) -> Job:
        """The next job for writer ``index``. The first FOREGROUND_WORKERS only take what the author is
        viewing; the rest take anything, and only as many as the writers setting allows are active."""
        foreground_only = index < FOREGROUND_WORKERS
        with self.cond:
            while True:
                if not foreground_only and index - FOREGROUND_WORKERS >= self.writers:
                    self.cond.wait()
                    continue
                ready = [
                    j for k, j in self.queue.items() if k not in self.running and (j.priority < 0 or not foreground_only)
                ]
                if ready:
                    job = min(ready, key=lambda j: (j.priority, j.seq))
                    key = (job.doc_id, draft_key(job.spec, job.model))
                    del self.queue[key]
                    self.running.add(key)
                    return job
                self.cond.wait()

    def _work(self, index: int) -> None:
        while True:
            job = self._take(index)
            key = (job.doc_id, draft_key(job.spec, job.model))
            try:
                self.write(job.doc_id, job.spec, job.model)
            except Exception as exc:  # one failed size must not stop the writer
                logger.exception("Writing {} failed: {}", key, exc)
            finally:
                with self.cond:
                    self.running.discard(key)
                    self.cond.notify_all()

    # ---------- writing ----------

    def _core(self, doc: Doc, model: str) -> str:
        """This model's brief on the piece for the current revision, written once per revision."""
        core_key = f"{model}@{doc.rev}"
        if doc.cores.get(core_key):
            return doc.cores[core_key]
        lock = self.core_locks.setdefault(doc.id + model, threading.Lock())
        with lock:
            doc = self.store.get(doc.id)
            if doc.cores.get(core_key):
                return doc.cores[core_key]
            rev = doc.rev
            locked = [(v.spec, v.text) for v in locked_versions(doc)]
            prompt = prompts.distill_prompt(doc.mode, locked, doc.brief)
            started = time.time()
            result = providers.complete(model, prompt, prompts.DISTILL_SYSTEM)
            self.store.write_log(
                {
                    "kind": "brief",
                    "doc_id": doc.id,
                    "rev": rev,
                    "model": model,
                    "system": prompts.DISTILL_SYSTEM,
                    "prompt": prompt,
                    "response": result.text,
                    "cost_usd": result.cost_usd,
                    "seconds": round(time.time() - started, 1),
                }
            )

            def apply(d: Doc) -> Doc:
                update = charge(d, model, result.cost_usd)
                # Keep only the current revision's briefs.
                cores = {k: v for k, v in d.cores.items() if k.endswith(f"@{d.rev}")}
                if d.rev == rev:
                    cores[core_key] = result.text.strip()
                return d.model_copy(update={**update, "cores": cores})

            self.store.update(doc.id, apply)
            return result.text.strip()

    def _core_quietly(self, doc_id: str, model: str) -> None:
        try:
            self._core(self.store.get(doc_id), model)
        except (ProviderError, KeyError) as exc:
            logger.warning("Could not prepare {}'s read of {}: {}", model, doc_id, exc)

    def _save_draft(self, doc_id: str, spec: Spec, model: str, rev: int, text: str) -> None:
        key = draft_key(spec, model)

        def apply(d: Doc) -> Doc:
            v = Version(spec=spec, text=text, status="ready", rev=rev, written_at=now(), model=model)
            return d.model_copy(update={"versions": {**d.versions, key: v}})

        self.store.update(doc_id, apply)

    def _fail(self, doc_id: str, spec: Spec, model: str, rev: int, error: str) -> None:
        key = draft_key(spec, model)

        def apply(d: Doc) -> Doc:
            existing = d.versions.get(key)
            v = Version(spec=spec, text=existing.text if existing else "", status="error", rev=rev, written_at=now(),
                        error=error[:300], model=model)
            return d.model_copy(update={"versions": {**d.versions, key: v}})

        self.store.update(doc_id, apply)

    def write(self, doc_id: str, spec: Spec, model: str, viewing: bool = False) -> None:
        doc = self.store.get(doc_id)
        if model not in doc.models:
            return
        lk = doc.versions.get(version_key(spec))
        if lk and lk.status == "locked":
            return
        key = draft_key(spec, model)
        current = doc.versions.get(key)
        if current and current.status == "ready" and not is_stale(doc, current):
            return
        locked = locked_versions(doc)
        if not locked and not doc.brief.strip():
            # Nothing to write from: say so instead of leaving the size spinning.
            self._fail(doc_id, spec, model, doc.rev, NOTHING_TO_WRITE_FROM)
            return
        try:
            if not locked:
                core = ""
            elif viewing and not doc.cores.get(f"{model}@{doc.rev}"):
                # The version on screen never waits for the model's read of the whole piece; that is
                # prepared in the background for the versions that follow.
                core = ""
                threading.Thread(target=self._core_quietly, args=(doc_id, model), daemon=True).start()
            else:
                core = self._core(doc, model)
        except ProviderError as exc:
            self._fail(doc_id, spec, model, doc.rev, f"Couldn't read the piece first: {exc}")
            return
        doc = self.store.get(doc_id)
        rev = doc.rev
        same_mode = [(v.spec, v.text) for v in locked if v.spec.mode == spec.mode]
        anchors = same_mode or [(v.spec, v.text) for v in locked]
        source_mode = spec.mode if same_mode or not anchors else anchors[0][0].mode
        fresh = [
            v
            for v in doc.versions.values()
            if v.status == "ready" and v.model == model and v.spec.mode == spec.mode and not is_stale(doc, v)
            and version_key(v.spec) != version_key(spec)
        ]
        smaller = sorted((v for v in fresh if size_of(v.spec) < size_of(spec)), key=lambda v: -size_of(v.spec))[:1]
        larger = sorted((v for v in fresh if size_of(v.spec) > size_of(spec)), key=lambda v: size_of(v.spec))[:1]
        # Short sizes are summaries: showing them nearby text invites lifting a sentence from it.
        neighbours = [] if prompts.is_short(spec) else [(v.spec, v.text) for v in smaller + larger]
        vkey = version_key(spec)
        notes = [doc.notes.get(vkey, "")] + [p.note for p in doc.pills if version_key(p.spec) == vkey]
        note = "\n".join(n for n in notes if n.strip())

        attempts = []
        text = ""
        cost = 0.0
        feedback = ""
        started = time.time()
        error: str | None = None
        shown_draft = False
        for _ in range(2 if spec.form else 3):
            prompt = prompts.write_prompt(
                target=spec,
                brief=doc.brief,
                note=note,
                core=core,
                anchors=anchors,
                source_mode=source_mode,
                neighbours=neighbours,
                feedback=feedback,
            )
            try:
                result = providers.complete(model, prompt, prompts.WRITE_SYSTEM)
            except ProviderError as exc:
                error = str(exc)
                attempts.append({"prompt": prompt, "error": error})
                break
            cost += result.cost_usd or 0
            text = clean(result.text)
            problems = prompts.shape_problems(spec, text)
            review = ""
            if not problems and not shown_draft:
                # Show the first draft with the right shape at once; the review and any resend refine it.
                self._save_draft(doc_id, spec, model, rev, text)
                shown_draft = True
            if not problems and prompts.is_short(spec):
                try:
                    verdict = providers.complete(
                        model, prompts.review_prompt(spec, text, anchors, doc.brief), prompts.REVIEW_SYSTEM
                    )
                    cost += verdict.cost_usd or 0
                    review = verdict.text.strip()
                    if not review.upper().startswith("PASS"):
                        problems = [review.removeprefix("REVISE:").strip()]
                except ProviderError as exc:
                    review = f"review failed: {exc}"
            attempts.append({"prompt": prompt, "response": result.text, "problems": problems, "review": review})
            if not problems:
                error = None
                break
            feedback = "\n".join(problems) + "\n\nYour previous attempt:\n" + text
        log_id = self.store.write_log(
            {
                "kind": "size",
                "doc_id": doc_id,
                "rev": rev,
                "spec": spec.model_dump(exclude_none=True),
                "model": model,
                "system": prompts.WRITE_SYSTEM,
                "attempts": attempts,
                "cost_usd": cost,
                "seconds": round(time.time() - started, 1),
            }
        )

        def apply(d: Doc) -> Doc:
            existing = d.versions.get(key)
            update = charge(d, model, cost)
            if error and not text:
                v = Version(spec=spec, text=existing.text if existing else "", status="error", rev=rev,
                            written_at=now(), log_id=log_id, error=error[:300], model=model)
            else:
                v = Version(spec=spec, text=text, status="ready", rev=rev, written_at=now(), log_id=log_id, model=model)
            return d.model_copy(update={**update, "versions": {**d.versions, key: v}})

        self.store.update(doc_id, apply)


def human_share_after_edit(before: str, before_share: float, after: str) -> float:
    """The author's share of ``after`` when they edited ``before`` into it.

    Words kept from ``before`` carry its share; every word the author added or changed is theirs.
    """
    new = after.split()
    if not new:
        return 0.0
    old = before.split()
    if not old:
        return 1.0
    kept = sum(block.size for block in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_matching_blocks())
    return round(((len(new) - kept) + kept * before_share) / len(new), 3)


def mark_sample_authorship(doc: Doc) -> Doc:
    """The bundled sample's locked texts were written by Claude for the demo, not by a person."""
    samples = {(Path(__file__).parent / "samples" / n).read_text().strip() for n in ("slow-mornings.txt", "kettle.txt")}
    versions = {
        k: v.model_copy(update={"model": "claude-opus-5-5", "human_share": 0.0})
        if v.model is None and v.text.strip() in samples
        else v
        for k, v in doc.versions.items()
    }
    return doc.model_copy(update={"versions": versions})


def charge(doc: Doc, model: str, cost: float) -> dict[str, object]:
    """The cost fields after spending ``cost`` on ``model``."""
    by_model = {**doc.spent_by_model, model: round(doc.spent_by_model.get(model, 0.0) + cost, 4)}
    return {"spent_usd": doc.spent_usd + cost, "spent_by_model": by_model}


LEGACY_PROSE_KEY = re.compile(r"^prose:p(\d+)-s(\d+)-w(\d+)(?:-c(\d+))?$")


def _legacy_spec(vkey: str) -> Spec | None:
    m = LEGACY_PROSE_KEY.match(vkey)
    if not m:
        return None
    p, s, w, c = (int(x) if x else None for x in m.groups())
    return Spec(mode="prose", paragraphs=p, sentences=s, words=w, chars=c)


def has_legacy_prose(doc: Doc) -> bool:
    return any(v.spec.mode == "prose" and not v.spec.unit for v in doc.versions.values()) or any(
        p.spec.mode == "prose" and not p.spec.unit for p in doc.pills
    )


def migrate_prose_sizes(doc: Doc) -> Doc:
    """Carry prose sizes saved as paragraphs x sentences x words over to one length (e.g. 280 characters).

    Where two old sizes land on the same new one, a locked version wins, then the most recently written.
    """
    versions: dict[str, Version] = {}
    ranked = sorted(doc.versions.items(), key=lambda kv: (kv[1].status == "locked", kv[1].written_at))
    for key, v in ranked:
        if v.spec.mode != "prose" or v.spec.unit:
            versions[key] = v
            continue
        spec = normalize(v.spec)
        model = key.split("@", 1)[1] if "@" in key else None
        new_key = draft_key(spec, model) if model else version_key(spec)
        versions[new_key] = v.model_copy(update={"spec": spec})
    pills = []
    seen = set()
    for p in doc.pills:
        spec = normalize(p.spec)
        if (p.label, version_key(spec)) in seen:
            continue
        seen.add((p.label, version_key(spec)))
        pills.append(p.model_copy(update={"spec": spec}))
    notes = {}
    for key, note in doc.notes.items():
        legacy = _legacy_spec(key)
        notes[version_key(normalize(legacy)) if legacy else key] = note
    return doc.model_copy(update={"versions": versions, "pills": pills, "notes": notes})


def add_form_pills(doc: Doc) -> Doc:
    """Give a document the haiku and limerick pills (documents made before the forms existed)."""
    have = {p.id for p in doc.pills}
    extra = [p for p in default_pills() if p.spec.form and p.id not in have]
    return doc.model_copy(update={"pills": [*doc.pills, *extra]})


def migrate(doc: Doc) -> Doc:
    """Move model-written versions saved before there were several models under the model that wrote them."""
    versions: dict[str, Version] = {}
    for key, v in doc.versions.items():
        if "@" in key or v.status == "locked":
            versions[key] = v
        else:
            model = v.model or DEFAULT_MODELS[0]
            versions[draft_key(v.spec, model)] = v.model_copy(update={"model": model})
    return doc.model_copy(update={"versions": versions})


def clean(text: str) -> str:
    """Strip wrappers a model sometimes adds around the text."""
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    text = re.sub(r"^```\w*\n|\n```$", "", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def seed_doc() -> Doc:
    """The sample document a fresh install starts with."""
    essay = (Path(__file__).parent / "samples" / "slow-mornings.txt").read_text().strip()
    verse = (Path(__file__).parent / "samples" / "kettle.txt").read_text().strip()
    p_spec, v_spec = spec_for_text(essay, "prose"), spec_for_text(verse, "poetry")
    pills = default_pills() + [
        Pill(id="full", label="Full", spec=p_spec, note="My original draft."),
        Pill(id="full-poem", label="Full poem", spec=v_spec),
    ]
    return Doc(
        id="slow-mornings",
        prompt_version=prompts.PROMPT_VERSION,
        fill=dict(DEFAULT_FILL),
        title="The case for slow mornings",
        mode="prose",
        brief="A short, warm essay for a team newsletter. Gentle, not preachy.",
        pills=pills,
        versions={
            version_key(p_spec): Version(spec=p_spec, text=essay, status="locked", rev=1, written_at=now(),
                                         model="claude-opus-5-5"),
            version_key(v_spec): Version(spec=v_spec, text=verse, status="locked", rev=1, written_at=now(),
                                         model="claude-opus-5-5"),
        },
    )
