"""LLM backends. All return schema-conformant JSON plus token usage.

  claude-cli  `claude -p --output-format json --json-schema ...` (default; runs on a
              Claude subscription; tools, MCP, settings and CLAUDE.md are disabled)
  anthropic   Anthropic Python SDK with structured outputs (`uv sync --extra anthropic`,
              needs ANTHROPIC_API_KEY or an `ant auth login` profile)
  replay      reads recorded responses from a fixtures directory (tests, `make sample`)

Every call is appended to work/llm_log.jsonl.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import append_jsonl, read_json, write_json

# List prices per million tokens (input, output, cache read). Used for estimates and
# for the SDK backend's cost line; `claude -p` reports its own figure.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}
ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
DEFAULT_MODEL = "sonnet"


def model_id(model: str) -> str:
    return ALIASES.get(model, model)


def list_price(model: str, tokens_in: int, tokens_out: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p_in, p_out, p_cr = PRICES.get(model_id(model), PRICES["claude-sonnet-5-5"])
    return (tokens_in * p_in + cache_write * p_in * 1.25 + cache_read * p_cr + tokens_out * p_out) / 1e6


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    extra: dict = field(default_factory=dict)


class Backend:
    name = "base"

    def __init__(self, model: str = DEFAULT_MODEL, log_path: Path | None = None):
        self.model = model
        self.log_path = log_path

    def complete_json(self, system: str, user: str, schema: dict, tag: dict) -> LLMResult:
        t0 = time.time()
        ok, err = True, None
        res = None
        try:
            res = self._call(system, user, schema, tag)
            res.duration_ms = int((time.time() - t0) * 1000)
            return res
        except Exception as exc:
            ok, err = False, f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self.log_path is not None:
                append_jsonl(
                    self.log_path,
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "backend": self.name,
                        "model": res.model if res else model_id(self.model),
                        "purpose": tag.get("purpose", "extract"),
                        "doc": tag.get("doc"),
                        "chunk_id": tag.get("chunk"),
                        "chunk_tokens": tag.get("chunk_tokens"),
                        "tokens_in": res.tokens_in if res else None,
                        "tokens_out": res.tokens_out if res else None,
                        "cache_read": res.cache_read if res else None,
                        "cache_write": res.cache_write if res else None,
                        "cost_usd": round(res.cost_usd, 6) if res else None,
                        "duration_ms": res.duration_ms if res else int((time.time() - t0) * 1000),
                        "ok": ok,
                        "error": err,
                    },
                )

    def _call(self, system: str, user: str, schema: dict, tag: dict) -> LLMResult:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- claude -p


class ClaudeCLIBackend(Backend):
    name = "claude-cli"

    def __init__(self, model: str = DEFAULT_MODEL, log_path: Path | None = None, effort: str | None = None,
                 timeout: int = 900, retries: int = 1, executable: str | None = None):
        super().__init__(model, log_path)
        self.effort = effort
        self.timeout = timeout
        self.retries = retries
        self.exe = executable or os.environ.get("CORPUS_GRAPH_CLAUDE", "claude")
        if not shutil.which(self.exe):
            raise LLMError(f"`{self.exe}` not found on PATH; install Claude Code or use --backend anthropic")

    def _call(self, system, user, schema, tag):
        cmd = [
            self.exe, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", system,
            "--json-schema", json.dumps(schema, ensure_ascii=False),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--disable-slash-commands",
        ]
        if self.effort:
            cmd += ["--effort", self.effort]
        last = None
        for attempt in range(self.retries + 1):
            # run from an empty directory so no CLAUDE.md or project skills are picked up
            with tempfile.TemporaryDirectory(prefix="cg-claude-") as cwd:
                try:
                    proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=self.timeout, cwd=cwd)
                except subprocess.TimeoutExpired:
                    last = LLMError(f"claude -p timed out after {self.timeout}s")
                    continue
            if proc.returncode != 0 and not proc.stdout.strip():
                last = LLMError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}")
                continue
            try:
                out = json.loads(proc.stdout)
            except json.JSONDecodeError:
                last = LLMError(f"claude -p returned non-JSON: {proc.stdout[:300]}")
                continue
            if out.get("is_error"):
                last = LLMError(f"claude -p error: {out.get('result') or out.get('subtype')}")
                continue
            data = out.get("structured_output")
            if data is None:
                try:
                    data = json.loads(out.get("result") or "")
                except json.JSONDecodeError:
                    last = LLMError("claude -p returned no structured_output")
                    continue
            usage = out.get("usage") or {}
            models = list((out.get("modelUsage") or {}).keys())
            return LLMResult(
                data=data,
                model=models[0] if models else model_id(self.model),
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
                cache_read=int(usage.get("cache_read_input_tokens") or 0),
                cache_write=int(usage.get("cache_creation_input_tokens") or 0),
                cost_usd=float(out.get("total_cost_usd") or 0.0),
                extra={"num_turns": out.get("num_turns"), "attempt": attempt},
            )
        raise last or LLMError("claude -p failed")


# --------------------------------------------------------------------------- Anthropic SDK


class AnthropicBackend(Backend):
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, log_path: Path | None = None, effort: str | None = None,
                 max_tokens: int = 64000):
        super().__init__(model, log_path)
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("the anthropic SDK is not installed: `uv sync --extra anthropic`") from exc
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.effort = effort
        self.max_tokens = max_tokens

    def _call(self, system, user, schema, tag):
        mid = model_id(self.model)
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        kwargs = dict(
            model=mid,
            max_tokens=self.max_tokens,
            # the system prompt is identical for every chunk: cache it
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        # long outputs: stream to avoid HTTP timeouts
        with self.client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise LLMError(f"model declined the request ({getattr(msg, 'stop_details', None)})")
        if msg.stop_reason == "max_tokens":
            raise LLMError("output truncated at max_tokens; lower --chunk-tokens")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        data = json.loads(text)
        u = msg.usage
        tin = u.input_tokens or 0
        tout = u.output_tokens or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        return LLMResult(data, msg.model, tin, tout, cr, cw, list_price(mid, tin, tout, cr, cw))


# --------------------------------------------------------------------------- replay / record


class ReplayBackend(Backend):
    """Serve recorded responses: <fixtures>/<source stem>/<chunk id>.json (purpose=extract)
    or <fixtures>/_merge/<key>.json (purpose=merge)."""

    name = "replay"

    def __init__(self, fixtures: Path, model: str = DEFAULT_MODEL, log_path: Path | None = None):
        super().__init__(model, log_path)
        self.fixtures = fixtures

    def _path(self, tag):
        if tag.get("purpose") == "merge":
            return self.fixtures / "_merge" / f"{tag['key']}.json"
        return self.fixtures / tag["source_stem"] / f"{tag['chunk']}.json"

    def _call(self, system, user, schema, tag):
        p = self._path(tag)
        rec = read_json(p)
        if rec is None:
            raise LLMError(f"no recorded response at {p}")
        u = rec.get("usage", {})
        return LLMResult(rec["data"], rec.get("model", "replay"), u.get("tokens_in", 0), u.get("tokens_out", 0),
                         u.get("cache_read", 0), u.get("cache_write", 0), 0.0)


class RecordingBackend(Backend):
    """Wrap a live backend and save each response as a replay fixture."""

    def __init__(self, inner: Backend, fixtures: Path):
        super().__init__(inner.model, None)
        self.inner = inner
        self.fixtures = fixtures
        self.name = f"{inner.name}+record"

    def complete_json(self, system, user, schema, tag):
        res = self.inner.complete_json(system, user, schema, tag)
        rp = ReplayBackend(self.fixtures)._path(tag)
        write_json(rp, {
            "model": res.model,
            "usage": {"tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
                      "cache_read": res.cache_read, "cache_write": res.cache_write, "cost_usd": res.cost_usd},
            "data": res.data,
        })
        return res


def make_backend(name: str, model: str, log_path: Path, effort: str | None = None,
                 fixtures: Path | None = None, record: Path | None = None) -> Backend:
    if name == "claude-cli":
        b: Backend = ClaudeCLIBackend(model, log_path, effort=effort)
    elif name == "anthropic":
        b = AnthropicBackend(model, log_path, effort=effort)
    elif name == "replay":
        if not fixtures:
            raise LLMError("--backend replay needs --fixtures DIR")
        b = ReplayBackend(fixtures, model, log_path)
    else:
        raise LLMError(f"unknown backend {name!r}")
    if record:
        b = RecordingBackend(b, record)
    return b
