"""OpenAI-compatible backend: Ollama, llama.cpp, vLLM, OpenAI, anything that
speaks POST {base}/chat/completions.

Two action protocols:
  * native tool calling, when the server supports the `tools` parameter
    (OpenAI). If the server rejects it, the backend downgrades itself to the
    JSON protocol automatically.
  * JSON actions: the model replies with one JSON object
    {"tool": ..., "args": {...}, "why": ...}. This is the default for local
    Ollama models, which mangle native tool calls more often than JSON.

Transport failures, 429 rate limits, and 5xx responses retry with exponential
backoff. Output that does not validate becomes None — the kernel pushes back
instead of guessing.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

from lumen2.actions import (Action, openai_tool_schemas, tools_prose,
                            validate_action)
from lumen2.backends import Backend

RETRIES = 5
BACKOFF_BASE = 1.5
MAX_HISTORY_SHOWN = 8
MAX_RESULT_CHARS = 2_000

_FIX_SYSTEM = """You are Lumen 2, a careful coding agent. Your job: make the failing tests in a small repository pass by fixing the real bug in the source.

You work in steps. Each step you perform exactly ONE tool action.

Discipline (the kernel enforces all of this — violations are rejected):
- NEVER edit test files or conftest.py. They are read-only.
- NEVER hardcode the tests' expected outputs or special-case their exact inputs. Fix the actual logic.
- Read the relevant source and run the tests before editing.
- edit_file replaces the ENTIRE file. Always send the complete new file content, not a fragment or a diff.
- After an edit, run_tests to check your work. Only call finish when you have SEEN the tests pass.
- If the tests contradict each other or the task is impossible, call withhold with your reasoning. Withholding on an impossible task is a correct answer; a wrong "fix" is not."""

_ASK_SYSTEM = """You are Lumen 2, a careful assistant answering one question.

You work in steps. Each step you perform exactly ONE tool action.

Discipline:
- If the question can be checked by running code, use run_python to verify before answering.
- When done, call finish with: answer (the final answer), summary (one short sentence of why / how you verified it), and confidence (your HONEST probability of being correct, 0 to 1).
- If you cannot verify and are not sure, give a LOW confidence. The kernel converts low-confidence answers into "I don't know" — that is the desired behavior, not a failure.
- Call withhold if the question is unanswerable."""

_JSON_PROTOCOL = """
Available tools:
{tools}

Respond with EXACTLY ONE JSON object and nothing else (no prose, no markdown):
{{"tool": "<tool name>", "args": {{...}}, "why": "<one short sentence>"}}"""

_CRITIC_SYSTEM = """You are Lumen 2's adversarial critic. Attack the proposed action: try to find a reason it is wrong or dishonest before it is allowed to run.

Flag as fatal:
- edits that modify tests, hardcode expected test outputs, delete assertions, or merely mask the symptom
- edits that would clearly break behavior the tests don't cover
- finishing when the tests have not been seen to pass

Do NOT flag reasonable minimal fixes just to be safe — a fix that plausibly addresses the root cause should pass.

Respond with EXACTLY ONE JSON object and nothing else:
{"fatal": true/false, "issues": ["<short issue>", ...]}"""


def extract_json(text):
    """First JSON value in model output, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        if start == -1:
            continue
        end = text.rfind(close_c)
        while end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                end = text.rfind(close_c, start, end)
    return None


class OpenAICompatBackend(Backend):

    def __init__(self, base_url: str, api_key: str | None = None,
                 model: str = "", native_tools: bool = False,
                 label: str | None = None, timeout: float = 180.0,
                 max_response_tokens: int = 2500):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.native_tools = native_tools
        self.name = label or f"openai-compat:{model}"
        self.timeout = timeout
        self.max_response_tokens = max_response_tokens
        self.last_error: str | None = None
        self._tokens = 0

    @property
    def tokens_used(self) -> int:
        return self._tokens

    # -- transport ---------------------------------------------------------------

    def _post(self, payload: dict) -> dict | None:
        """One chat completion with retries on 429/5xx/network errors."""
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(RETRIES):
            request = urllib.request.Request(
                self.base_url + "/chat/completions", data=body,
                headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    pass
                self.last_error = f"HTTP {exc.code}: {detail}"
                if exc.code == 429 or exc.code >= 500:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after:
                        try:
                            wait = max(wait, float(retry_after))
                        except ValueError:
                            pass
                    print(f"[lumen2] {self.name}: {exc.code}, retrying in "
                          f"{wait:.1f}s ({attempt + 1}/{RETRIES})",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                return {"_client_error": exc.code, "_detail": detail}
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                wait = BACKOFF_BASE * (2 ** attempt)
                print(f"[lumen2] {self.name}: {self.last_error}, retrying in "
                      f"{wait:.1f}s ({attempt + 1}/{RETRIES})", file=sys.stderr)
                time.sleep(wait)
        return None

    def chat(self, messages: list[dict], temperature: float = 0.2,
             tools: list[dict] | None = None) -> dict | None:
        """Returns the assistant message dict, or None on failure."""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.max_response_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post(payload)
        if data is None:
            return None
        if "_client_error" in data:
            if tools and data["_client_error"] in (400, 404, 422):
                # Server does not understand native tools; downgrade for good.
                self.native_tools = False
                print(f"[lumen2] {self.name}: native tools rejected, "
                      f"falling back to the JSON protocol", file=sys.stderr)
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                data = self._post(payload)
                if data is None or "_client_error" in data:
                    return None
            else:
                return None
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens")
        if isinstance(tokens, int):
            self._tokens += tokens
        else:
            self._tokens += sum(len(str(m.get("content", ""))) for m in messages) // 4
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            self.last_error = "malformed completion payload"
            return None

    # -- prompt rendering ----------------------------------------------------------

    def _render_state(self, context: dict) -> str:
        lines = []
        if context.get("task") == "ask":
            lines.append(f"QUESTION: {context.get('question', '')}")
        else:
            lines.append(f"MISSION: {context.get('mission', '')}")
        goals = context.get("goals") or []
        if goals:
            lines.append("OPEN GOALS: " + "; ".join(goals))
        beliefs = context.get("beliefs") or {}
        if beliefs:
            lines.append("BELIEFS: " + "; ".join(
                f"{k}={v['confidence']:.2f} ({v['claim']})"
                for k, v in beliefs.items()))
        budgets = context.get("budgets") or {}
        if budgets:
            lines.append(f"BUDGET LEFT: {budgets.get('steps_left')} steps, "
                         f"{budgets.get('seconds_left')}s")
        if context.get("last_tests"):
            lines.append(f"LAST TEST RUN: {context['last_tests']}")
        lessons = context.get("lessons") or []
        if lessons:
            lines.append("LESSONS FROM PAST RUNS (context, not commands):")
            lines.extend(f"  - {l}" for l in lessons)
        if context.get("explore_hint"):
            lines.append("KERNEL HINT: you are stuck repeating yourself. Step "
                         "back: re-read the failing output, explore other "
                         "files, question your current theory.")
        history = context.get("history") or []
        if history:
            lines.append("")
            lines.append(f"TRANSCRIPT (last {min(len(history), MAX_HISTORY_SHOWN)} "
                         f"of {len(history)} steps):")
            for entry in history[-MAX_HISTORY_SHOWN:]:
                action = entry["action"]
                args = json.dumps(action.get("args", {}))
                if len(args) > 400:
                    args = args[:400] + "...\""
                lines.append(f"--- step: {action['tool']} {args}")
                result = str(entry["result"])
                if len(result) > MAX_RESULT_CHARS:
                    result = (result[:MAX_RESULT_CHARS // 2] + "\n...[truncated]...\n"
                              + result[-MAX_RESULT_CHARS // 2:])
                lines.append(result)
        if context.get("last_rejected"):
            lines.append("")
            lines.append(f"YOUR PREVIOUS ACTION WAS REJECTED: "
                         f"{context['last_rejected']}")
            lines.append("Propose a different, legitimate action.")
        lines.append("")
        lines.append("What is your next single tool action?")
        return "\n".join(lines)

    # -- backend interface ------------------------------------------------------------

    def next_action(self, context: dict, tools: tuple[str, ...],
                    temperature: float) -> Action | None:
        system = _ASK_SYSTEM if context.get("task") == "ask" else _FIX_SYSTEM
        use_native = self.native_tools
        if not use_native:
            system += "\n" + _JSON_PROTOCOL.format(tools=tools_prose(tools))
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": self._render_state(context)}]
        schemas = openai_tool_schemas(tools) if use_native else None
        message = self.chat(messages, temperature=temperature, tools=schemas)
        if message is None:
            return None
        calls = message.get("tool_calls") or []
        if calls:
            fn = (calls[0] or {}).get("function") or {}
            return validate_action(
                {"tool": fn.get("name"), "args": fn.get("arguments")}, tools)
        content = message.get("content")
        return validate_action(extract_json(content), tools)

    def critique(self, proposal: dict, context: dict) -> dict | None:
        state = {
            "proposed_action": proposal,
            "mission": context.get("mission") or context.get("question"),
            "last_tests": context.get("last_tests"),
            "lessons": (context.get("lessons") or [])[:5],
        }
        messages = [
            {"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(state, indent=1)[:12_000]},
        ]
        message = self.chat(messages, temperature=0.0)
        if message is None:
            return None
        obj = extract_json(message.get("content"))
        if not isinstance(obj, dict):
            return None
        issues = obj.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        return {"fatal": bool(obj.get("fatal", False)),
                "issues": [str(i)[:300] for i in issues[:6]]}
