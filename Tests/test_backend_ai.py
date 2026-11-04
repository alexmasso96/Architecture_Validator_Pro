"""
Phase 1 — AI router tests: provider config (GET/PUT/DELETE) and the SSE chat
token stream. Credentials are redirected to a temp dir via
ARCHVALIDATOR_CONFIG_DIR so tests never touch the real machine store, and the
provider call is faked so no network is needed.
"""
import asyncio
import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

import httpx
import uvicorn
from fastapi.testclient import TestClient

from backend.app import create_app
from Application_Logic import Logic_AI_Providers as providers

TOKEN = "ai-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def isolated_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHVALIDATOR_CONFIG_DIR", str(tmp_path / "creds"))
    yield


@pytest.fixture()
def client():
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Providers config
# ---------------------------------------------------------------------------
def test_list_providers(client):
    body = client.get("/api/ai/providers", headers=AUTH).json()
    ids = {p["id"] for p in body["providers"]}
    assert {"copilot", "anthropic", "openai", "gemini"} <= ids
    anth = next(p for p in body["providers"] if p["id"] == "anthropic")
    assert anth["configured"] is False
    assert anth["supports_tools"] is True
    assert any(m["id"] for m in anth["models"])


def test_set_and_clear_key(client, monkeypatch):
    # Stay offline: with a key set, listing models queries the provider's API.
    monkeypatch.setattr(providers.AnthropicProvider, "discover_models", lambda self: [])
    r = client.put("/api/ai/providers/anthropic", json={"api_key": "sk-test"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["configured"] is True
    # Reflected in the list.
    body = client.get("/api/ai/providers", headers=AUTH).json()
    assert next(p for p in body["providers"] if p["id"] == "anthropic")["configured"] is True
    # Clear it.
    r = client.delete("/api/ai/providers/anthropic", headers=AUTH)
    assert r.json()["configured"] is False


def test_set_key_copilot_400(client):
    assert client.put("/api/ai/providers/copilot", json={"api_key": "x"}, headers=AUTH).status_code == 400


def test_set_key_unknown_provider_404(client):
    assert client.put("/api/ai/providers/bogus", json={"api_key": "x"}, headers=AUTH).status_code == 404


def test_chat_unknown_provider_404(client):
    r = client.post("/api/ai/chat",
                    json={"provider_id": "bogus", "model": "m", "messages": []}, headers=AUTH)
    assert r.status_code == 404


def test_chat_unconfigured_provider_409(client):
    r = client.post("/api/ai/chat",
                    json={"provider_id": "anthropic", "model": "m",
                          "messages": [{"role": "user", "content": "hi"}],
                          "ground_in_mindmap": False}, headers=AUTH)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Chat SSE token stream (real server + faked provider)
# ---------------------------------------------------------------------------
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


def test_chat_streams_tokens(tmp_path, monkeypatch):
    """The chat runs the agent's tool loop and streams its progress: the
    context it sends, each tool call and result, token usage, then the answer."""
    monkeypatch.setenv("ARCHVALIDATOR_CONFIG_DIR", str(tmp_path / "creds2"))
    from Application_Logic import Logic_AI_Credentials as creds
    creds.set_key("anthropic", "sk-fake")   # makes is_configured() True
    seen = {}

    def fake_loop(provider_id, model, messages, tools, tool_executor,
                  system_prompt=None, on_tool_call=None, on_tool_result=None,
                  on_usage=None, options=None, stop_check=None, **kw):
        seen.update(messages=messages, options=options, system=system_prompt)
        on_tool_call("search_code", {"query": "Motor"})
        on_tool_result("search_code", {"query": "Motor"}, "a.c:1: Motor_Init()")
        on_usage({"input_tokens": 120, "output_tokens": 30})
        return "Hello world"

    monkeypatch.setattr(providers, "generate_with_tools", fake_loop)

    port = _free_port()
    app = create_app(token=TOKEN)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    assert srv.started

    async def run():
        events = []
        async with httpx.AsyncClient(timeout=10.0) as http:
            async with http.stream(
                "POST", f"http://127.0.0.1:{port}/api/ai/chat",
                headers=AUTH,
                json={"provider_id": "anthropic", "model": "claude-3-5-haiku-latest",
                      "messages": [{"role": "user", "content": "hi"}],
                      "ground_in_mindmap": False,
                      "options": {"effort": "low"}},
            ) as resp:
                assert resp.status_code == 200
                event = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        events.append((event, json.loads(line.split(":", 1)[1].strip())))
                        if event in ("done", "error"):
                            break
        return events

    try:
        events = asyncio.run(run())
    finally:
        srv.should_exit = True
        thread.join(timeout=5)

    kinds = [k for k, _ in events]
    assert kinds[0] == "context"
    assert kinds[-1] == "done", events
    assert "tool" in kinds and "tool_result" in kinds and "usage" in kinds
    assert dict(events)["token"] == "Hello world"
    assert dict(events)["usage"] == {"input_tokens": 120, "output_tokens": 30}
    assert seen["options"] == {"effort": "low"}
    # The agent's citation rules always reach the model.
    assert "CITING CODE" in seen["system"]


def test_copilot_host_can_be_set_and_cleared(client):
    r = client.put("/api/ai/providers/copilot/host", json={"host": "https://acme.ghe.com/"},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["github_host"] == "acme.ghe.com"
    listed = {p["id"]: p for p in client.get("/api/ai/providers", headers=AUTH).json()["providers"]}
    assert listed["copilot"]["github_host"] == "acme.ghe.com"
    assert client.put("/api/ai/providers/copilot/host", json={"host": "not a host"},
                      headers=AUTH).status_code == 400
    r = client.put("/api/ai/providers/copilot/host", json={"host": ""}, headers=AUTH)
    assert r.json()["github_host"] == "github.com"
