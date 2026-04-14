"""Focused tests for the localhost clarification companion bridge."""

import json
import threading
from typing import List, Optional
from unittest.mock import patch

import pytest

from gateway import companion_bridge
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


@pytest.fixture(autouse=True)
def _reset_companion_bridge(monkeypatch):
    companion_bridge.stop_reply_server()
    monkeypatch.setenv("HERMES_COMPANION_BRIDGE", "1")
    monkeypatch.setenv("HERMES_COMPANION_REPLY_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_COMPANION_REPLY_PORT", "0")
    yield
    companion_bridge.stop_reply_server()


def _build_agent(callback, tmp_path):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("clarify")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("run_agent.fetch_model_metadata", return_value={}),
    ):
        return AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            clarify_callback=callback,
        )


def test_remote_companion_reply_resumes_waiting_clarify(monkeypatch, tmp_path):
    emitted = {}
    emitted_event = threading.Event()

    def fake_send(question, choices, correlation_id, source="chat"):
        emitted.update(
            {
                "question": question,
                "choices": choices,
                "correlation_id": correlation_id,
                "source": source,
            }
        )
        emitted_event.set()
        return True

    def unexpected_local_callback(question: str, choices: Optional[List[str]]) -> str:
        raise AssertionError("local clarify callback should not run when remote companion reply wins")

    agent = _build_agent(unexpected_local_callback, tmp_path)
    result_holder = {}

    def _run_tool():
        result_holder["result"] = json.loads(agent._run_clarify_tool("Need input?", ["alpha", "beta"]))

    worker = threading.Thread(target=_run_tool, daemon=True)
    monkeypatch.setattr(companion_bridge, "send_clarification_event", fake_send)
    worker.start()

    assert emitted_event.wait(timeout=2.0), "expected clarification event to be emitted"
    assert companion_bridge.resolve_pending_clarification(
        {
            "correlation_id": emitted["correlation_id"],
            "reply": "remote answer",
            "timestamp": "2026-04-15T00:00:00Z",
            "source": "hermes-on-desk",
        }
    )

    worker.join(timeout=2.0)

    assert result_holder["result"]["user_response"] == "remote answer"
    assert emitted["question"] == "Need input?"
    assert emitted["choices"] == ["alpha", "beta"]
    assert emitted["source"] == "chat"


def test_local_clarify_fallback_still_works_when_companion_is_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_COMPANION_EVENTS_URL", "http://127.0.0.1:9/events")
    local_called = threading.Event()

    def local_callback(question: str, choices: Optional[List[str]]) -> str:
        local_called.set()
        assert question == "Fallback?"
        assert choices is None
        return "local answer"

    agent = _build_agent(local_callback, tmp_path)

    result = json.loads(agent._run_clarify_tool("Fallback?", None))

    assert local_called.is_set()
    assert result["user_response"] == "local answer"
    assert result["question"] == "Fallback?"


def test_send_clarification_event_builds_expected_payload(monkeypatch):
    captured = {}

    class _Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(companion_bridge, "ensure_reply_server", lambda: "http://127.0.0.1:8765/replies/clarification")
    monkeypatch.setenv("HERMES_COMPANION_EVENTS_URL", "http://127.0.0.1:8757/events")
    monkeypatch.setattr(companion_bridge.request, "urlopen", fake_urlopen)

    assert companion_bridge.send_clarification_event(
        "Question?",
        ["red", "blue"],
        "corr-123",
        source="cli",
    )

    assert captured["url"] == "http://127.0.0.1:8757/events"
    assert captured["timeout"] == 0.25
    assert captured["payload"]["type"] == "clarification"
    assert captured["payload"]["state"] == "waiting"
    assert captured["payload"]["title"] == "Need input"
    assert captured["payload"]["summary"] == "Question?"
    assert captured["payload"]["choices"] == ["red", "blue"]
    assert captured["payload"]["correlation_id"] == "corr-123"
    assert captured["payload"]["requires_input"] is True
    assert captured["payload"]["reply_target"] == "http://127.0.0.1:8765/replies/clarification"
    assert captured["payload"]["source"] == "cli"
    assert captured["payload"]["timestamp"].endswith("Z")
