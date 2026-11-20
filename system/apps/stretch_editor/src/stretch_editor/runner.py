"""Write a document once, then stretch it to any length -- one word, a tweet, a paragraph, or longer -- with locked versions and per-size prompts..

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/stretch-editor/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/stretch-editor/`` but
  honors the ``STRETCH_EDITOR_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``STRETCH_EDITOR_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://stretch-editor.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import gzip
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import error_response, model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

from stretch_editor.engine import (
    Doc,
    Engine,
    Pill,
    Store,
    Version,
    default_pills,
    DEFAULT_FILL,
    FILL_LEVELS,
    draft_key,
    grid_rows,
    human_share_after_edit,
    planned,
    is_stale,
    ladders,
    now,
    seed_doc,
)
from stretch_editor import providers
from stretch_editor.xpreview import HandleProfile, XProfile, ensure_xeet, profile_for_handle, x_profile, xeet_file
from stretch_editor.prompts import PROMPT_VERSION
from stretch_editor.sizes import Mode, Spec, normalize, spec_for_text, version_key

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/stretch-editor/`` but is overridable via the ``STRETCH_EDITOR_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/stretch-editor/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("STRETCH_EDITOR_DATA_DIR", "data/.apps/stretch-editor"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``STRETCH_EDITOR_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("STRETCH_EDITOR_PORT", "8080"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' 'wasm-unsafe-eval' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class DocSummary(FrozenModel):
    """A document in the list."""

    id: str = Field(description="Stable id")
    title: str = Field(description="Title")


class DocList(FrozenModel):
    """Every document."""

    docs: list[DocSummary] = Field(description="Documents, by title")


class VersionView(FrozenModel):
    """One size as the editor shows it."""

    spec: Spec = Field(description="The size")
    text: str = Field(description="The text")
    status: str = Field(description="locked, ready or error")
    stale: bool = Field(description="Written before the latest change; being rewritten in the background")
    written_at: str = Field(description="When it was written")
    log_id: str | None = Field(default=None, description="The model call that wrote it, for 'how this was written'")
    error: str | None = Field(default=None, description="Why writing failed")
    model: str | None = Field(default=None, description="The model that wrote it; none for the author's own text")
    human_share: float = Field(description="How much of it the author wrote: 1 all theirs, 0 all model")


class ModelChoice(FrozenModel):
    """A model the editor can use."""

    id: str = Field(description="Model id")
    label: str = Field(description="Name to show")
    provider: str = Field(description="Who makes it")
    description: str = Field(description="One line about it")
    billed: str = Field(description="'per call' or 'subscription'")


class DocView(FrozenModel):
    """A document with all its sizes and what the background writer is doing."""

    id: str = Field(description="Stable id")
    title: str = Field(description="Title")
    mode: Mode = Field(description="The mode shown")
    brief: str = Field(description="What the author is going for")
    notes: dict[str, str] = Field(description="Per-size notes by version key")
    pills: list[Pill] = Field(description="Saved sizes")
    versions: dict[str, VersionView] = Field(description="Every written size by version key")
    rev: int = Field(description="Document revision")
    writing: list[str] = Field(description="Version keys being written right now")
    queued: list[str] = Field(description="Version keys waiting to be written")
    ladder: dict[str, list[Spec]] = Field(description="Sizes on the zoom slider per mode, shortest first")
    spent_usd: float = Field(description="Total model cost for this document")
    spent_by_model: dict[str, float] = Field(description="Model cost per model")
    models: list[str] = Field(description="The models writing every size, in order")
    available_models: list[ModelChoice] = Field(description="Every model the editor can use")
    fill: dict[str, str] = Field(description="How much each mode writes ahead in the background")
    planned: dict[str, list[str]] = Field(description="Version keys the background keeps written, per mode")
    grid_rows: int = Field(description="How many lines the poetry grid offers")


class CreateDocRequest(FrozenModel):
    """A new document."""

    title: str = Field(default="Untitled", description="Title")


class DocUpdate(FrozenModel):
    """Changes to a document's settings. Omitted fields stay as they are."""

    title: str | None = Field(default=None, description="New title")
    mode: Mode | None = Field(default=None, description="Mode to show")
    brief: str | None = Field(default=None, description="What the author is going for")
    notes: dict[str, str] | None = Field(default=None, description="Per-size notes")
    pills: list[Pill] | None = Field(default=None, description="Saved sizes")
    models: list[str] | None = Field(default=None, description="Models that write every size (at least one)")
    fill: dict[str, str] | None = Field(
        default=None, description="Write-ahead per mode: prose open/stops/dense, poetry open/stops/grid"
    )


class SaveVersionRequest(FrozenModel):
    """The author's own text for one size; saved locked."""

    spec: Spec | None = Field(default=None, description="The size; omitted to work it out from the text")
    text: str = Field(description="The text")
    based_on_model: str | None = Field(
        default=None, description="The model whose version the author started editing from; none if they typed it fresh"
    )


class LockRequest(FrozenModel):
    """Lock or unlock one size."""

    spec: Spec = Field(description="The size")
    model: str | None = Field(default=None, description="Lock this model's version (when locking)")
    locked: bool = Field(description="True to lock")


class WriteRequest(FrozenModel):
    """Ask for one size to be written next."""

    spec: Spec = Field(description="The size")
    fresh: bool = Field(default=False, description="Discard the current model-written text and write it again")
    model: str | None = Field(default=None, description="The model on screen, written first")


class AppSettings(FrozenModel):
    """Settings for the whole editor."""

    writers: int = Field(ge=1, le=32, description="Background writers running at once")


class LogEntry(FrozenModel):
    """The exact instructions and response behind a version."""

    entry: dict[str, object] = Field(description="The model call record: system prompt, prompts, responses, cost")


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/static/{filename}"): SchemalessRoute(reason="the editor's own scripts and styles"),
    ("GET", "/vendor/stretchtext/{filepath}"): SchemalessRoute(reason="the vendored stretchtext animation code and font"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
    ("GET", "/vendor/xeet/{filepath}"): SchemalessRoute(reason="the tweet preview renderer's scripts, WebAssembly and fonts"),
    ("GET", "/api/x-profile"): RouteModels(response_model=XProfile),
    ("GET", "/api/x-profile/{handle}"): RouteModels(response_model=HandleProfile),
    ("GET", "/api/settings"): RouteModels(response_model=AppSettings),
    ("PUT", "/api/settings"): RouteModels(request_model=AppSettings, response_model=AppSettings),
    ("GET", "/api/docs"): RouteModels(response_model=DocList),
    ("POST", "/api/docs"): RouteModels(request_model=CreateDocRequest, response_model=DocView, success_status=201),
    ("GET", "/api/docs/{doc_id}"): RouteModels(response_model=DocView),
    ("PATCH", "/api/docs/{doc_id}"): RouteModels(request_model=DocUpdate, response_model=DocView),
    ("DELETE", "/api/docs/{doc_id}"): RouteModels(response_model=DocList),
    ("PUT", "/api/docs/{doc_id}/versions"): RouteModels(request_model=SaveVersionRequest, response_model=DocView),
    ("POST", "/api/docs/{doc_id}/lock"): RouteModels(request_model=LockRequest, response_model=DocView),
    ("POST", "/api/docs/{doc_id}/write"): RouteModels(request_model=WriteRequest, response_model=DocView),
    ("GET", "/api/docs/{doc_id}/log/{log_id}"): RouteModels(response_model=LogEntry),
}

app = Flask("stretch_editor", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title='Stretch Editor', description='Write a document once, then stretch it to any length -- one word, a tweet, a paragraph, or longer -- with locked versions and per-size prompts.', route_table=ROUTE_TABLE
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


STATIC_DIR = Path(__file__).parent / "static"
# The vendored stretchtext checkout (relative to the repo root the service runs from).
STRETCHTEXT_DIR = Path("system/vendor/stretchtext")
STRETCHTEXT_FILES = (
    "src/word-spring.js",
    "src/gestures.js",
    "src/index.js",
    "site/fonts/crimson-text-regular.ttf",
)


@app.route("/")
def index() -> Response:
    """The editor page."""
    html = (STATIC_DIR / "index.html").read_text()
    html = html.replace("</body>", SHELL_PAGE_SCRIPT + "\n</body>")
    return Response(html, mimetype="text/html")


@app.route("/static/<filename>")
def static_file(filename: str) -> Response:
    """One of the editor's own scripts or stylesheets."""
    path = STATIC_DIR / filename
    if "/" in filename or not path.is_file():
        abort(404)
    response = send_file(path.absolute())
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/vendor/stretchtext/<path:filepath>")
def stretchtext_file(filepath: str) -> Response:
    """A file from the vendored stretchtext project: its word animation code or its reading font."""
    if filepath not in STRETCHTEXT_FILES:
        abort(404)
    mimetype = "text/javascript" if filepath.endswith(".js") else None
    return send_file((STRETCHTEXT_DIR / filepath).absolute(), mimetype=mimetype)


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in ("blocked-uri", "document-uri", "source-file", "effective-directive", "line-number", "script-sample")
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(BROWSER_EXTENSION_SCHEMES):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {!r} on {!r} ({}){}{}; {}", blocked, page, directive, where, starting, remedy
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


STORE = Store(DATA_DIR)
ENGINE = Engine(STORE)


def authored_share(v: Version) -> float:
    """The author's share of a version. Text saved before shares were tracked, with no model, is the author's."""
    return v.human_share if v.model else (v.human_share or 1.0)


def view(doc: Doc) -> DocView:
    writing, queued = ENGINE.jobs(doc.id)
    return DocView(
        id=doc.id,
        title=doc.title,
        mode=doc.mode,
        brief=doc.brief,
        notes=doc.notes,
        pills=doc.pills,
        versions={
            k: VersionView(
                spec=v.spec, text=v.text, status=v.status, stale=is_stale(doc, v), written_at=v.written_at,
                log_id=v.log_id, error=v.error, model=v.model, human_share=authored_share(v),
            )
            for k, v in doc.versions.items()
        },
        rev=doc.rev,
        writing=writing,
        queued=queued,
        ladder=ladders(doc),
        spent_usd=round(doc.spent_usd, 4),
        spent_by_model=doc.spent_by_model,
        models=doc.models,
        fill={**DEFAULT_FILL, **doc.fill},
        planned={m: [version_key(s) for s in planned(doc, m)] for m in ("prose", "poetry")},
        grid_rows=grid_rows(doc),
        available_models=[
            ModelChoice(id=m.id, label=m.label, provider=m.provider, description=m.description, billed=m.billed)
            for m in providers.available_models()
        ],
    )


def load(doc_id: str) -> Doc:
    try:
        return STORE.get(doc_id)
    except KeyError:
        abort(404)


@app.route("/vendor/xeet/<path:filepath>")
def xeet_asset(filepath: str) -> Response:
    """A file of the tweet preview renderer (xeet): its scripts, WebAssembly or a font pack."""
    path = xeet_file(DATA_DIR, filepath)
    if path is None:
        abort(404)
    mimetype = {".js": "text/javascript", ".wasm": "application/wasm", ".json": "application/json"}.get(
        path.suffix, "application/octet-stream"
    )
    response = send_file(path, mimetype=mimetype)
    response.headers["Cache-Control"] = "max-age=86400"
    return response


@app.route("/api/x-profile")
def get_x_profile() -> Response:
    """Who the tweet preview is drawn as: the connected X account, or placeholders."""
    return model_response(x_profile())


@app.route("/api/x-profile/<handle>")
def get_handle_profile(handle: str) -> Response:
    """The public X profile behind a handle (name, picture, badge), so the tweet preview looks like you."""
    return model_response(profile_for_handle(handle))


@app.route("/api/settings")
def get_settings() -> Response:
    """The editor-wide settings."""
    return model_response(AppSettings(writers=ENGINE.writers))


@app.route("/api/settings", methods=["PUT"])
def put_settings() -> Response:
    """Change the editor-wide settings (how many background writers run at once)."""
    body = parse_request_body(AppSettings)
    return model_response(AppSettings(writers=ENGINE.set_writers(body.writers)))


@app.route("/api/docs")
def list_docs() -> Response:
    """List every document."""
    return model_response(DocList(docs=[DocSummary(id=d.id, title=d.title) for d in STORE.list()]))


@app.route("/api/docs", methods=["POST"])
def create_doc() -> Response:
    """Start a new, empty document with the standard saved sizes."""
    body = parse_request_body(CreateDocRequest)
    slug = re.sub(r"[^a-z0-9]+", "-", body.title.lower()).strip("-")[:40] or "untitled"
    doc = Doc(
        id=f"{slug}-{uuid.uuid4().hex[:6]}", title=body.title, mode="prose", pills=default_pills(),
        prompt_version=PROMPT_VERSION,
        fill=dict(DEFAULT_FILL),
    )
    STORE.put(doc)
    return model_response(view(doc), 201)


@app.route("/api/docs/<doc_id>")
def get_doc(doc_id: str) -> Response:
    """A document with all its sizes and what is being written."""
    return model_response(view(load(doc_id)))


@app.route("/api/docs/<doc_id>", methods=["DELETE"])
def delete_doc(doc_id: str) -> Response:
    """Delete a document."""
    load(doc_id)
    STORE.path(doc_id).unlink()
    return model_response(DocList(docs=[DocSummary(id=d.id, title=d.title) for d in STORE.list()]))


@app.route("/api/docs/<doc_id>", methods=["PATCH"])
def update_doc(doc_id: str) -> Response:
    """Change a document's title, mode, brief, notes or saved sizes.

    A new brief makes every model-written size stale; a changed note makes that size stale. Stale sizes are
    rewritten in the background once the document has been quiet for a moment.
    """
    body = parse_request_body(DocUpdate)
    load(doc_id)

    def change(doc: Doc) -> Doc:
        update: dict[str, object] = {}
        versions = dict(doc.versions)
        if body.title is not None:
            update["title"] = body.title
        if body.mode is not None:
            update["mode"] = body.mode
        if body.brief is not None and body.brief != doc.brief:
            update["brief"] = body.brief
            update["rev"] = doc.rev + 1
        old_notes = {**doc.notes, **{version_key(p.spec): p.note for p in doc.pills}}
        if body.notes is not None:
            update["notes"] = {k: v for k, v in body.notes.items() if v.strip()}
        if body.fill is not None:
            update["fill"] = {
                **doc.fill,
                **{m: lvl for m, lvl in body.fill.items() if m in FILL_LEVELS and lvl in FILL_LEVELS[m]},
            }
        if body.models is not None:
            available = {m.id for m in providers.available_models()}
            chosen = [m for m in body.models if m in available]
            if chosen:
                update["models"] = list(dict.fromkeys(chosen))
        if body.pills is not None:
            update["pills"] = [p.model_copy(update={"spec": normalize(p.spec)}) for p in body.pills]
        new_doc = doc.model_copy(update=update)
        new_notes = {**new_doc.notes, **{version_key(p.spec): p.note for p in new_doc.pills}}
        for key in set(old_notes) | set(new_notes):
            if old_notes.get(key, "") != new_notes.get(key, "") and key in versions:
                v = versions[key]
                if v.status != "locked":
                    versions[key] = v.model_copy(update={"rev": -1})
        return new_doc.model_copy(update={"versions": versions})

    doc = STORE.update(doc_id, change)
    ENGINE.schedule_background(doc_id)
    return model_response(view(doc))


@app.route("/api/docs/<doc_id>/versions", methods=["PUT"])
def save_version(doc_id: str) -> Response:
    """Save the author's own text for one size, locked. Every model-written size is then rewritten to match."""
    body = parse_request_body(SaveVersionRequest)
    text = body.text.strip()
    if not text:
        return error_response("The text is empty.", 400)
    doc = load(doc_id)
    spec = normalize(body.spec) if body.spec else spec_for_text(text, doc.mode)

    def change(d: Doc) -> Doc:
        key = version_key(spec)
        # Who wrote it: the words the author kept from the version they started from carry that
        # version's share; everything they typed is theirs.
        locked = d.versions.get(key)
        start = locked if locked and locked.status == "locked" else None
        if start is None and body.based_on_model:
            start = d.versions.get(draft_key(spec, body.based_on_model))
        if start is None:
            start = locked
        share = human_share_after_edit(start.text, authored_share(start), text) if start and start.text else 1.0
        v = Version(spec=spec, text=text, status="locked", rev=d.rev + 1, written_at=now(), human_share=share,
                    model=None if share > 0 else (start.model if start else None))
        pills = list(d.pills)
        label = "Full" if spec.mode == "prose" else "Full poem"
        if not any(p.label == label for p in pills):
            pills.append(Pill(id=label.lower().replace(" ", "-"), label=label, spec=spec))
        return d.model_copy(update={"versions": {**d.versions, key: v}, "rev": d.rev + 1, "pills": pills, "mode": spec.mode})

    doc = STORE.update(doc_id, change)
    if is_untitled(doc.title):
        doc = name_document(doc, text)
    ENGINE.schedule_background(doc_id, delay=0.5)
    return model_response(view(doc))


@app.route("/api/docs/<doc_id>/lock", methods=["POST"])
def lock_version(doc_id: str) -> Response:
    """Lock a size so it is never rewritten and every other size is fitted to it, or unlock it."""
    body = parse_request_body(LockRequest)
    load(doc_id)
    key = version_key(normalize(body.spec))

    def change(d: Doc) -> Doc:
        rev = d.rev + 1
        current = d.versions.get(key)
        if body.locked:
            if current and current.status == "locked":
                return d
            # Lock the version on screen: a model's version is copied in as the size's locked text.
            source = d.versions.get(draft_key(normalize(body.spec), body.model)) if body.model else None
            source = source if source and source.text else current
            if not source or not source.text:
                return d
            locked = source.model_copy(update={"status": "locked", "rev": rev, "error": None})
            return d.model_copy(update={"versions": {**d.versions, key: locked}, "rev": rev})
        if not current or current.status != "locked":
            return d
        versions = dict(d.versions)
        if current.model:
            # A locked model version goes back to being just that model's version.
            versions.pop(key)
        else:
            versions[key] = current.model_copy(update={"status": "ready", "rev": rev})
        return d.model_copy(update={"versions": versions, "rev": rev})

    doc = STORE.update(doc_id, change)
    ENGINE.schedule_background(doc_id)
    return model_response(view(doc))


@app.route("/api/docs/<doc_id>/write", methods=["POST"])
def write_size(doc_id: str) -> Response:
    """Write one size next, ahead of the background queue."""
    body = parse_request_body(WriteRequest)
    load(doc_id)
    spec = normalize(body.spec)
    if body.fresh:
        def change(d: Doc) -> Doc:
            versions = dict(d.versions)
            for m in [body.model] if body.model else d.models:
                k = draft_key(spec, m)
                if k in versions:
                    versions[k] = versions[k].model_copy(update={"rev": -1})
            return d.model_copy(update={"versions": versions})
        STORE.update(doc_id, change)
    ENGINE.request(doc_id, spec, body.model)
    return model_response(view(load(doc_id)))


@app.route("/api/docs/<doc_id>/log/<log_id>")
def read_log(doc_id: str, log_id: str) -> Response:
    """The exact instructions the model was given for a version, and what it answered."""
    load(doc_id)
    try:
        entry = STORE.read_log(log_id)
    except KeyError:
        abort(404)
    return model_response(LogEntry(entry=entry))


TITLE_SYSTEM = "You name pieces of writing. Reply with the title only: no quotes, no punctuation at the end."
TITLE_MODELS = ("claude-haiku-5-5", "gpt-6-luna", "claude-sonnet-5-5", "claude-opus-5-5")


def is_untitled(title: str) -> bool:
    return not title.strip() or title.strip().lower().startswith("untitled")


def quick_title(text: str) -> str:
    """A name from the opening words, shown at once while a better one is written."""
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0].strip(" .,:;!?\"'")
    words = first.split()
    return (" ".join(words[:7]) + ("…" if len(words) > 7 else "")) or "Untitled"


def name_document(doc: Doc, text: str) -> Doc:
    """Give an untitled document a name: the opening words now, a model-written title in a moment.

    The model's title only replaces the quick one if the author hasn't renamed the document meanwhile.
    """
    quick = quick_title(text)
    doc = STORE.update(doc.id, lambda d: d.model_copy(update={"title": quick}) if is_untitled(d.title) else d)

    def better() -> None:
        available = {m.id for m in providers.available_models()}
        model = next((m for m in TITLE_MODELS if m in available), None)
        if not model:
            return
        try:
            result = providers.complete(
                model,
                "Write a short, evocative title (2 to 6 words) for this piece, in its own spirit:\n\n" + text[:6000],
                TITLE_SYSTEM,
            )
        except providers.ProviderError as exc:
            logger.warning("Could not title {}: {}", doc.id, exc)
            return
        title = result.text.strip().strip("\"'“”*#").splitlines()[0][:80].strip()
        if title:
            STORE.update(doc.id, lambda d: d.model_copy(update={"title": title}) if d.title == quick else d)

    threading.Thread(target=better, daemon=True).start()
    return doc


SEED_DIR = Path(__file__).parent / "seed"


def ensure_seed() -> None:
    """On a fresh install, start with the bundled documents (every version already written) and the
    records of how each was written; with no bundle, the single sample document."""
    if STORE.list() or (DATA_DIR / "docs").exists():
        return
    bundled = sorted((SEED_DIR / "docs").glob("*.json")) if (SEED_DIR / "docs").is_dir() else []
    if not bundled:
        STORE.put(seed_doc())
        return
    for path in bundled:
        STORE.put(Doc.model_validate_json(path.read_text()))
    archive = SEED_DIR / "logs.json.gz"
    if archive.is_file():
        STORE.log_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            for log_id, entry in json.load(handle).items():
                (STORE.log_dir / f"{log_id}.json").write_text(json.dumps(entry, indent=1))


def main() -> None:
    setup_logging()
    ensure_seed()
    ENGINE.start()
    threading.Thread(target=ensure_xeet, args=(DATA_DIR,), daemon=True).start()
    # Documents that already have the author's text but no title get one.
    for doc in STORE.list():
        own = [v for v in doc.versions.values() if v.status == "locked" and v.text]
        if is_untitled(doc.title) and own:
            name_document(doc, max(own, key=lambda v: len(v.text)).text)
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
