---
title: "Stretch Editor"
description: "Write a document or poem once, then zoom it to any length -- one word, a tweet, a sonnet, three paragraphs -- with every size written ahead by the models you choose."
thumbnail: "template.svg"
version: v1
format: v2
---

# Stretch Editor

This file is the manifest for the **Stretch Editor** template (slug:
`stretch-editor`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Write a document or poem once, then zoom it to any length -- one word, a tweet, a sonnet, three paragraphs -- with every size written ahead by the models you choose.

Stretch Editor is a writing desk with a continuous zoom between meanings at different lengths. Keep your own wording locked, then choose models to write the other sizes ahead of time. Read prose as a word, a tweet, or paragraphs; explore poetry by lines, syllables, rhythm, rhyme, and 19 classic forms. The reading view animates words between versions and shows how much of each version came from the author.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/stretch_editor/`
- `system/supervisord.conf.d/stretch-editor.conf`
- `system/vendor/stretchtext/`

`system/apps/stretch_editor/` contains the Flask app, browser interface, background writers, prompts, and the forms catalogue at `static/forms.json`. The `stretch-editor` supervisord program in `system/supervisord.conf.d/stretch-editor.conf` synchronizes the app's locked Python environment, registers its manifest through `forward_port.py`, and serves on `127.0.0.1:8080`. The browser uses the app's API; background writers dispatch each requested size to each selected model. The service stays running when its window is absent so writing can continue.

`system/vendor/stretchtext/` is the MIT-licensed stretchtext project (https://github.com/mrjf/stretchtext), including the Word Spring animation and gesture code. Words glide between versions; newly appearing words briefly gain an icy-blue outline.

On first launch, `src/stretch_editor/seed/` is copied into `data/.apps/stretch-editor/`: three documents, their existing versions, and a gzipped archive of the original generation records. The examples are “The case for slow mornings” (Claude demo essay and poem), “What lips my lips have kissed” (Edna St. Vincent Millay, Sonnet XIX, 1923, public domain), and “Stretch Text” (the stretchtext author's one-line description). Existing installations are left alone. No live workspace data directory is included.

The tweet preview downloads @xeet/core 0.1.2 once from registry.npmjs.org into the app's data folder and verifies its SHA-512 digest. Public handle lookups use api.fxtwitter.com, with unavatar.io as the avatar fallback. If an X account is already connected through latchkey, the preview attempts to use its own-profile endpoint; otherwise it uses placeholders. No X connection is needed to write or preview text.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


- requires_llm: keyless Claude calls through `claude -p` using a Claude account connected in Imbue Studio; connect that account before enabling background writing. OpenAI models are optional and appear automatically when an OpenAI/ChatGPT account is connected; these use `codex exec` on that account.

No API-key secret is required. X profile personalization only uses an existing connection; do not request an X connection as a prerequisite. There is no built-in X service in the current connector catalogue, so leave profile placeholders unless the adopter already has a compatible connection.

- The provider catalogue hardcodes Claude model IDs and assumes Studio account folders under `~/.minds/accounts/`. Verify those model IDs on the adopter's connected account. If the adopter uses keyed model access instead of the CLI account route, adapt the provider calls before enabling writers.
- Some poetry-grid cells in the bundled examples are empty. Review each document's write-ahead options and writers-at-once setting with the adopter before allowing their writers to fill the remaining cells; generating these versions consumes their model usage. The source workspace's private documents and runtime settings are not included.

Known maintenance gap: the app's quality-ratchet tests still flag while-true loops, broad exception handling, and a dataclasses import in the copied `claude_p.py` helper. This template preserves the app unchanged; a passing packaging check does not mean those quality checks pass.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment.

The app uses the stock Python/uv tools, Claude CLI, optional Codex CLI, and optional latchkey CLI. Python dependencies are installed from the app's own locked environment by its supervisord command. The pinned tweet-rendering package is downloaded as a runtime asset, not installed as a global npm tool.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `connect-external-service` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-11-20) -- Semantic-zoom writing desk with prose and poetry, multiple model writers, and three bundled examples.

Publishes the app, service configuration, and MIT-licensed stretchtext dependency unchanged on the clean workspace base. No additional data paths or published-version modifications.

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
