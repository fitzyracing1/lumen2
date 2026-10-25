import urllib.error

import pytest

from lumen2.actions import ALL_TOOLS, validate_action
from lumen2.backends import BackendError, from_spec
from lumen2.backends.openai_compat import OpenAICompatBackend, extract_json
from lumen2.backends.stub import StubBackend


def test_from_spec():
    assert isinstance(from_spec("stub"), StubBackend)
    b = from_spec("ollama:qwen2.5-coder:1.5b")
    assert isinstance(b, OpenAICompatBackend)
    assert b.model == "qwen2.5-coder:1.5b"
    assert not b.native_tools
    a = from_spec("amalia")
    assert a.model == "amalia:latest"
    assert "11434" in a.base_url
    u = from_spec("url:http://10.0.0.5:8000/v1::mymodel")
    assert u.base_url == "http://10.0.0.5:8000/v1" and u.model == "mymodel"
    with pytest.raises(BackendError):
        from_spec("teapot:9000")


def test_openai_spec_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(BackendError):
        from_spec("openai:gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    b = from_spec("openai:gpt-4.1-mini")
    assert b.native_tools


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"a": {"b": 2}} hope it helps') \
        == {"a": {"b": 2}}
    assert extract_json("no json here") is None
    assert extract_json("") is None


def test_validate_action():
    ok = validate_action({"tool": "read_file", "args": {"path": "x.py"},
                          "why": "look"}, ALL_TOOLS)
    assert ok.tool == "read_file" and ok.args["path"] == "x.py"
    # arguments-as-string (native tool calls deliver JSON strings)
    ok = validate_action({"tool": "edit_file",
                          "args": '{"path": "a.py", "content": "x = 1\\n"}'},
                         ALL_TOOLS)
    assert ok.args["content"] == "x = 1\n"
    # missing confidence on finish defaults to 0 (maximally uncertain)
    ok = validate_action({"tool": "finish", "args": {"summary": "done"}},
                         ALL_TOOLS)
    assert ok.args["confidence"] == 0.0
    assert validate_action({"tool": "rm -rf", "args": {}}, ALL_TOOLS) is None
    assert validate_action({"args": {}}, ALL_TOOLS) is None
    assert validate_action("finish", ALL_TOOLS) is None
    assert validate_action({"tool": "read_file", "args": {}}, ALL_TOOLS) is None


def _response(payload: bytes, code: int = 200):
    import io

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R(payload)


def test_retry_on_429_then_success(monkeypatch):
    backend = OpenAICompatBackend("http://example.invalid/v1", model="m")
    calls = {"n": 0}
    reply = (b'{"choices": [{"message": {"content": "{\\"tool\\": \\"run_tests\\"'
             b', \\"args\\": {}}"}}], "usage": {"total_tokens": 7}}')

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(request.full_url, 429,
                                         "rate limited", {}, None)
        return _response(reply)

    sleeps = []
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    message = backend.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential backoff
    assert "run_tests" in message["content"]
    assert backend.tokens_used == 7


def test_gives_up_after_retry_budget(monkeypatch):
    backend = OpenAICompatBackend("http://example.invalid/v1", model="m")

    def always_500(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", always_500)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert backend.chat([{"role": "user", "content": "hi"}]) is None


def test_native_tools_downgrade_on_400(monkeypatch):
    backend = OpenAICompatBackend("http://example.invalid/v1", model="m",
                                  native_tools=True)
    reply = (b'{"choices": [{"message": {"content": '
             b'"{\\"tool\\": \\"list_files\\", \\"args\\": {}}"}}]}')
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.data)
        if b'"tools"' in request.data:
            raise urllib.error.HTTPError(request.full_url, 400,
                                         "tools unsupported", {}, None)
        return _response(reply)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    action = backend.next_action({"task": "fix", "history": []},
                                 ALL_TOOLS, 0.2)
    assert action is not None and action.tool == "list_files"
    assert backend.native_tools is False  # downgraded permanently


def test_next_action_parses_native_tool_call(monkeypatch):
    backend = OpenAICompatBackend("http://example.invalid/v1", model="m",
                                  native_tools=True)
    reply = (b'{"choices": [{"message": {"tool_calls": [{"function": '
             b'{"name": "read_file", "arguments": "{\\"path\\": \\"a.py\\"}"'
             b'}}]}}]}')
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=None: _response(reply))
    action = backend.next_action({"task": "fix", "history": []},
                                 ALL_TOOLS, 0.2)
    assert action.tool == "read_file" and action.args["path"] == "a.py"
