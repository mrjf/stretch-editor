"""Every model the studio can reach, read live from its connected accounts.

The studio keeps one folder per connected provider under ``~/.minds/accounts/``
with an ``index.json`` naming each account's lane. Connecting a new provider
adds a row there, so the list is re-read every few seconds and new models show
up without a restart:

- ``anthropic``: Claude models, run through ``claude -p`` on that account.
- ``openai``: the models the account's Codex sign-in offers (its
  ``minds_codex_model_options.json``), run through ``codex exec`` with
  ``CODEX_HOME`` pointed at the account folder.

Lanes this file does not know yet are skipped and logged once.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

from stretch_editor.claude_p import ClaudeCLIError, claude_p_completion

ACCOUNTS_DIR = Path(os.environ.get("STRETCH_EDITOR_ACCOUNTS_DIR", str(Path.home() / ".minds" / "accounts")))
CLAUDE_MODELS = [
    ("claude-opus-5-5", "Opus 5.5", "Anthropic's most capable model"),
    ("claude-fable-5-2", "Fable 5.2", "Anthropic's model for long-form and creative writing"),
    ("claude-sonnet-5-5", "Sonnet 5.5", "Fast and capable"),
    ("claude-haiku-5-5", "Haiku 5.5", "Fastest and cheapest"),
]
CODEX_EFFORT = os.environ.get("STRETCH_EDITOR_CODEX_EFFORT", "high")
CACHE_SECONDS = 10.0


class ProviderError(RuntimeError):
    """A model call failed."""


class ModelInfo(FrozenModel):
    """A model the editor can use."""

    id: str = Field(description="Model id")
    label: str = Field(description="Name to show")
    provider: str = Field(description="Who makes it, as shown in the picker")
    description: str = Field(default="", description="One line about it")
    lane: str = Field(description="The studio account lane that runs it")
    account_dir: str = Field(description="The account folder it runs on")
    billed: str = Field(description="'per call' when each call costs money, 'subscription' when covered by a plan")


class Completion(FrozenModel):
    text: str
    cost_usd: float
    usage: dict[str, int] = Field(default_factory=dict)


_lock = threading.Lock()
_cache: tuple[float, list[ModelInfo]] = (0.0, [])
_warned: set[str] = set()


def _codex_models(account: Path) -> list[tuple[str, str, str]]:
    try:
        data = json.loads((account / "minds_codex_model_options.json").read_text())
    except (OSError, ValueError):
        return []
    return [
        (m["model"], m.get("displayName") or m["model"], m.get("description", ""))
        for m in data.get("models", [])
        if not m.get("hidden") and m.get("model")
    ]


def available_models() -> list[ModelInfo]:
    """Every model on every connected account, Claude first. Re-read every few seconds."""
    global _cache
    with _lock:
        if time.time() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        models: list[ModelInfo] = []
        try:
            index = json.loads((ACCOUNTS_DIR / "index.json").read_text())
        except (OSError, ValueError):
            index = {"accounts": []}
        rows = index.get("accounts", [])
        seen_lanes: set[str] = set()
        for lane in ("anthropic", "openai"):
            accounts = [a for a in rows if a.get("lane") == lane]
            if not accounts:
                continue
            # The account the studio would pick for this lane: pinned default, else most recent, else first.
            by_id = {a["id"]: a for a in accounts}
            chosen = by_id.get(index.get("default_account")) or by_id.get(index.get("mru")) or accounts[0]
            account = ACCOUNTS_DIR / str(chosen["id"])
            if not account.is_dir():
                continue
            seen_lanes.add(lane)
            if lane == "anthropic":
                for mid, label, desc in CLAUDE_MODELS:
                    models.append(ModelInfo(id=mid, label=label, provider="Claude", description=desc, lane=lane,
                                            account_dir=str(account), billed="per call"))
            else:
                for mid, label, desc in _codex_models(account):
                    models.append(ModelInfo(id=mid, label=label, provider="OpenAI", description=desc, lane=lane,
                                            account_dir=str(account), billed="subscription"))
        for a in rows:
            lane = str(a.get("lane"))
            if lane not in seen_lanes and lane not in ("anthropic", "openai") and lane not in _warned:
                _warned.add(lane)
                logger.warning("Connected account lane {!r} is not supported by the editor yet", lane)
        _cache = (time.time(), models)
        return models


def find(model_id: str) -> ModelInfo | None:
    return next((m for m in available_models() if m.id == model_id), None)


def complete(model_id: str, prompt: str, system: str) -> Completion:
    """One plain completion (no tools) from whichever provider runs ``model_id``."""
    info = find(model_id)
    if info is None:
        raise ProviderError(f"{model_id} is not available on any connected account")
    if info.lane == "anthropic":
        try:
            result = claude_p_completion(prompt, system=system, model=model_id)
        except ClaudeCLIError as exc:
            raise ProviderError(str(exc)) from exc
        return Completion(text=result.text, cost_usd=result.cost_usd or 0.0)
    return _codex(info, prompt, system)


def _codex(info: ModelInfo, prompt: str, system: str) -> Completion:
    """Run ``codex exec`` read-only in an empty folder, on the account's sign-in, and return its answer."""
    env = dict(os.environ)
    env["CODEX_HOME"] = info.account_dir
    with tempfile.TemporaryDirectory(prefix="stretch_codex_") as cwd:
        out = Path(cwd) / "answer.txt"
        argv = [
            "codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-rules",
            "-s", "read-only", "-m", info.id,
            "-c", f"model_reasoning_effort={json.dumps(CODEX_EFFORT)}",
            "-c", f"developer_instructions={json.dumps(system)}",
            "--json", "-o", str(out), "--", prompt,
        ]
        proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=600, check=False)
        usage: dict[str, int] = {}
        error = ""
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                usage = {k: int(v) for k, v in (event.get("usage") or {}).items() if isinstance(v, int)}
            elif event.get("type") in ("error", "turn.failed"):
                error = json.dumps(event)[:400]
        text = out.read_text().strip() if out.is_file() else ""
        if proc.returncode != 0 or not text:
            raise ProviderError(f"codex exited {proc.returncode}: {error or proc.stderr.strip()[-400:]}")
        return Completion(text=text, cost_usd=0.0, usage=usage)
