"""Model backends for Lumen 2.

The agent asks a backend one question per step: "given this state, what is the
next tool action?" Malformed output validates to None; the kernel treats None
as a strike, never as something to guess around.

Backends:
  * stub                 — offline, deterministic. Tests and CI run on it.
  * OpenAI-compatible    — any /chat/completions endpoint (Ollama, vLLM,
                           llama.cpp, OpenAI). Native tool calling when the
                           server supports it, JSON-action fallback otherwise,
                           retries with exponential backoff on 429/5xx.

Spec strings (CLI --model / env LUMEN2_MODEL):
  stub
  ollama:<model>            e.g. ollama:qwen2.5-coder:1.5b  (http://127.0.0.1:11434/v1)
  amalia                    = ollama:amalia:latest
  openai:<model>            e.g. openai:gpt-4.1-mini        (needs OPENAI_API_KEY)
  xai:<model>               e.g. xai:grok-4.6               (needs XAI_API_KEY, https://api.x.ai/v1)
  url:<base_url>::<model>   any other OpenAI-compatible endpoint (key: LUMEN2_API_KEY)

Any backend can take its key from a named env var with api_key_env
(CLI --api-key-env NAME); the key itself is never printed or logged.
"""

from __future__ import annotations

import os

from lumen2.actions import Action


class BackendError(Exception):
    """Configuration errors only — never bad model output."""


class Backend:
    name = "base"

    def next_action(self, context: dict, tools: tuple[str, ...],
                    temperature: float) -> Action | None:
        raise NotImplementedError

    def critique(self, proposal: dict, context: dict) -> dict | None:
        """Adversarial review of a proposed edit/finish.
        {"fatal": bool, "issues": [str, ...]} or None when unavailable."""
        raise NotImplementedError

    def start_task(self) -> None:
        """Reset any per-task state."""

    @property
    def tokens_used(self) -> int:
        return 0


def from_spec(spec: str | None = None, base_url: str | None = None,
              api_key_env: str | None = None) -> Backend:
    """Build a backend from a spec string (or LUMEN2_MODEL, default stub)."""
    def key_from(*names):
        for name in ((api_key_env,) if api_key_env else ()) + names:
            if name and os.environ.get(name):
                return os.environ[name]
        return None
    from lumen2.backends.openai_compat import OpenAICompatBackend
    from lumen2.backends.stub import StubBackend

    spec = (spec or os.environ.get("LUMEN2_MODEL", "stub")).strip()
    if spec in ("", "stub", "offline"):
        return StubBackend()
    if spec == "amalia":
        spec = "ollama:" + os.environ.get("AMALIA_MODEL", "amalia:latest")
        base_url = base_url or os.environ.get(
            "AMALIA_BASE_URL", "http://127.0.0.1:11434/v1")
    if spec.startswith("ollama:"):
        model = spec.split(":", 1)[1]
        return OpenAICompatBackend(
            base_url=base_url or os.environ.get("OLLAMA_BASE_URL",
                                                "http://127.0.0.1:11434/v1"),
            api_key=key_from("OLLAMA_API_KEY"),
            model=model,
            # Small local models mangle native tool calls more often than
            # they mangle JSON; default to the JSON-action protocol.
            native_tools=False,
            label=f"ollama:{model}")
    if spec.startswith("openai:"):
        model = spec.split(":", 1)[1]
        url = base_url or os.environ.get("OPENAI_BASE_URL",
                                         "https://api.openai.com/v1")
        key = key_from("OPENAI_API_KEY", "LUMEN2_API_KEY")
        if "api.openai.com" in url and not key:
            raise BackendError("openai:<model> needs OPENAI_API_KEY")
        return OpenAICompatBackend(base_url=url, api_key=key, model=model,
                                   native_tools=True, label=f"openai:{model}")
    if spec.startswith("xai:"):
        model = spec.split(":", 1)[1]
        url = base_url or os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")
        key = key_from("XAI_API_KEY", "LUMEN2_API_KEY")
        if not key:
            raise BackendError("xai:<model> needs XAI_API_KEY")
        return OpenAICompatBackend(base_url=url, api_key=key, model=model,
                                   native_tools=True, label=f"xai:{model}")
    if spec.startswith("url:"):
        rest = spec[4:]
        if "::" not in rest:
            raise BackendError("url spec is url:<base_url>::<model>")
        url, model = rest.rsplit("::", 1)
        return OpenAICompatBackend(base_url=base_url or url,
                                   api_key=key_from("LUMEN2_API_KEY"),
                                   model=model, native_tools=False,
                                   label=f"url:{model}")
    raise BackendError(
        f"unknown model spec {spec!r} (expected stub, ollama:<model>, amalia, "
        f"openai:<model>, xai:<model>, or url:<base_url>::<model>)")
