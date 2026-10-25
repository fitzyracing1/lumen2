"""The action vocabulary shared by every backend.

An Action is one tool call proposed by the model. Backends produce Actions
either from native tool calls (OpenAI) or from a validated JSON object
(local models). Anything that does not validate becomes None, and the kernel
treats None as a reason to push back — never as a reason to guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Tool names. The agent decides which subset a task exposes.
READ_FILE = "read_file"
LIST_FILES = "list_files"
SEARCH = "search"
RUN_TESTS = "run_tests"
RUN_PYTHON = "run_python"
EDIT_FILE = "edit_file"
FINISH = "finish"
WITHHOLD = "withhold"

ALL_TOOLS = (READ_FILE, LIST_FILES, SEARCH, RUN_TESTS, RUN_PYTHON,
             EDIT_FILE, FINISH, WITHHOLD)

# Tools whose effects the kernel must attack before they run.
RISKY_TOOLS = (EDIT_FILE, FINISH)


@dataclass
class Action:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def public(self) -> dict:
        shown = dict(self.args)
        content = shown.get("content")
        if isinstance(content, str) and len(content) > 400:
            shown["content"] = content[:400] + f"... [{len(content)} chars]"
        return {"tool": self.tool, "args": shown, "why": self.why}


# One source of truth for tool documentation. Rendered two ways: as OpenAI
# native tool schemas, and as prose for the JSON-action fallback prompt.
TOOL_SPECS: dict[str, dict] = {
    READ_FILE: {
        "description": "Read one file from the workspace. Returns its content.",
        "params": {"path": {"type": "string",
                            "description": "Relative path inside the workspace."}},
        "required": ["path"],
    },
    LIST_FILES: {
        "description": "List every file in the workspace with sizes.",
        "params": {},
        "required": [],
    },
    SEARCH: {
        "description": "Search all workspace files for a regex or plain text. "
                       "Returns matching lines with file and line number.",
        "params": {"pattern": {"type": "string",
                               "description": "Regex (or literal text) to find."}},
        "required": ["pattern"],
    },
    RUN_TESTS: {
        "description": "Run the test suite (pytest) in the workspace. Returns "
                       "pass/fail counts and the failure output.",
        "params": {},
        "required": [],
    },
    RUN_PYTHON: {
        "description": "Run a short Python snippet in the workspace (imports "
                       "from workspace files work). Returns stdout/stderr. "
                       "Use it to reproduce bugs and verify claims.",
        "params": {"code": {"type": "string",
                            "description": "Python source to execute."}},
        "required": ["code"],
    },
    EDIT_FILE: {
        "description": "Replace the full content of one workspace file (or "
                       "create it). Test files are read-only; editing them is "
                       "a violation and will be rejected.",
        "params": {"path": {"type": "string",
                            "description": "Relative path inside the workspace."},
                   "content": {"type": "string",
                               "description": "The complete new file content."}},
        "required": ["path", "content"],
    },
    FINISH: {
        "description": "Declare the task done. For a fix task the kernel "
                       "re-runs the tests and commits only if they pass "
                       "verification. For a question, 'answer' is your final "
                       "answer. Give an honest confidence in [0,1].",
        "params": {"summary": {"type": "string",
                               "description": "What you did / what the answer is, briefly."},
                   "answer": {"type": "string",
                              "description": "Final answer text (question tasks)."},
                   "confidence": {"type": "number",
                                  "description": "Your honest confidence, 0 to 1."}},
        "required": ["summary", "confidence"],
    },
    WITHHOLD: {
        "description": "Give up explicitly instead of guessing: the task looks "
                       "unsolvable, contradictory, or beyond the budget.",
        "params": {"reason": {"type": "string",
                              "description": "Why committing would be wrong."}},
        "required": ["reason"],
    },
}


def openai_tool_schemas(tools: tuple[str, ...]) -> list[dict]:
    """Native tool-calling schemas for OpenAI-compatible endpoints."""
    out = []
    for name in tools:
        spec = TOOL_SPECS[name]
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": {
                    "type": "object",
                    "properties": spec["params"],
                    "required": spec["required"],
                },
            },
        })
    return out


def tools_prose(tools: tuple[str, ...]) -> str:
    """Tool documentation for the JSON-action fallback prompt."""
    lines = []
    for name in tools:
        spec = TOOL_SPECS[name]
        params = ", ".join(
            f'"{p}": {meta["type"]}' + (" (required)" if p in spec["required"] else "")
            for p, meta in spec["params"].items())
        lines.append(f'- {name}({{{params}}}): {spec["description"]}')
    return "\n".join(lines)


def validate_action(obj: Any, tools: tuple[str, ...]) -> Action | None:
    """Validate one parsed JSON action. Returns None for anything malformed."""
    if not isinstance(obj, dict):
        return None
    tool = obj.get("tool") or obj.get("name") or obj.get("action")
    if not isinstance(tool, str):
        return None
    tool = tool.strip().lower()
    if tool not in tools:
        return None
    args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
    if isinstance(args, str):
        # Some models emit arguments as a JSON string.
        import json
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    spec = TOOL_SPECS[tool]
    clean: dict[str, Any] = {}
    for param, meta in spec["params"].items():
        if param in args:
            value = args[param]
            if meta["type"] == "string":
                if not isinstance(value, str):
                    value = str(value)
                clean[param] = value
            elif meta["type"] == "number":
                try:
                    clean[param] = min(1.0, max(0.0, float(value)))
                except (TypeError, ValueError):
                    return None
    for param in spec["required"]:
        if param not in clean:
            # Tolerate a missing confidence by treating it as maximally
            # uncertain, not by inventing confidence.
            if param == "confidence":
                clean[param] = 0.0
            else:
                return None
    why = obj.get("why") or obj.get("reason") or obj.get("thought") or ""
    return Action(tool=tool, args=clean, why=str(why)[:500])
