"""
Self-hosted AI providers: Ollama (native /api/chat) and vLLM (OpenAI-compatible).

Pure request/response mapping is tested directly; discovery, proxy bypass and
the agentic tool loop run against a real local HTTP stub standing in for the
server (no Ollama/vLLM install needed).
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.abspath("src"))

from Application_Logic import Logic_AI_Credentials as creds
from Application_Logic import Logic_AI_Providers as prov


@pytest.fixture(autouse=True)
def isolated_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHVALIDATOR_CONFIG_DIR", str(tmp_path))
    for p in prov.list_providers():
        if isinstance(p, prov.SelfHostedProvider):
            p._model_cache = (0.0, [])
    yield


# ---------------------------------------------------------------------------
# A minimal stand-in for an Ollama / vLLM server.
# ---------------------------------------------------------------------------
class _Stub(BaseHTTPRequestHandler):
    requests_seen: list = []
    script: list = []            # queued JSON responses for POSTs

    def log_message(self, *a):   # keep pytest output quiet
        pass

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _Stub.requests_seen.append(("GET", self.path, dict(self.headers), None))
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen2.5-coder:7b"},
                                   {"name": "nomic-embed-text:latest"}]})
        elif self.path == "/v1/models":
            self._send({"data": [{"id": "Qwen/Qwen2.5-Coder-32B-Instruct",
                                  "max_model_len": 32768}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Stub.requests_seen.append(("POST", self.path, dict(self.headers), body))
        self._send(_Stub.script.pop(0) if _Stub.script else {"error": "no script"})


@pytest.fixture()
def stub_server():
    _Stub.requests_seen = []
    _Stub.script = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("http://localhost:11434", "http://localhost:11434"),
    ("http://localhost:11434/", "http://localhost:11434"),
    ("  http://gpu-box:8000/v1/ ", "http://gpu-box:8000"),
    ("https://llm.corp.example/v1", "https://llm.corp.example"),
])
def test_normalize_base_url(raw, expected):
    assert prov.normalize_base_url(raw) == expected


def test_validate_base_url():
    assert prov.validate_base_url("http://localhost:11434") is None
    assert prov.validate_base_url("localhost:11434") is not None
    assert prov.validate_base_url("") is not None


def test_unconfigured_providers_are_inert():
    for pid in ("ollama", "vllm"):
        p = prov.get_provider(pid)
        assert p.is_configured() is False
        assert p.list_models() == []          # no network attempt when unset
        assert p.SUPPORTS_TOOLS is False


def test_ollama_request_uses_native_api_with_num_ctx():
    creds.set_endpoint("ollama", {"base_url": "http://localhost:11434",
                                  "num_ctx": 32768})
    p = prov.get_provider("ollama")
    url, headers, body = p.build_request(
        [{"role": "user", "content": "hi"}], "qwen2.5-coder:7b", "be brief")
    assert url == "http://localhost:11434/api/chat"
    assert "Authorization" not in headers
    assert body["stream"] is False
    assert body["options"]["num_ctx"] == 32768
    assert body["messages"][0] == {"role": "system", "content": "be brief"}


def test_ollama_tool_call_parsing_and_results():
    p = prov.get_provider("ollama")
    data = {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": "read_file",
                                                     "arguments": {"path": "a.c"}}}]}}
    calls = p.parse_tool_calls(data)
    assert len(calls) == 1 and calls[0].name == "read_file"
    assert calls[0].input == {"path": "a.c"} and calls[0].id == "call_0"
    out = p.format_tool_results({"call_0": ("read_file", "int x;")})
    assert out == [{"role": "tool", "content": "int x;", "tool_name": "read_file"}]


def test_vllm_request_is_openai_shaped_without_max_tokens():
    creds.set_endpoint("vllm", {"base_url": "http://gpu-box:8000"})
    creds.set_key("vllm", "secret-key")
    p = prov.get_provider("vllm")
    url, headers, body = p.build_request(
        [{"role": "user", "content": "hi"}], "orca-mini", None)
    assert url == "http://gpu-box:8000/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret-key"
    # A model id starting with "o" must NOT get OpenAI's o-series treatment,
    # and no max_tokens (vLLM rejects prompt + max_tokens > max_model_len).
    assert "max_completion_tokens" not in body and "max_tokens" not in body


def test_supports_tools_follows_endpoint_config():
    creds.set_endpoint("vllm", {"base_url": "http://x:8000", "supports_tools": True})
    assert prov.get_provider("vllm").SUPPORTS_TOOLS is True
    creds.set_endpoint("vllm", {"base_url": "http://x:8000", "supports_tools": False})
    assert prov.get_provider("vllm").SUPPORTS_TOOLS is False


def test_endpoint_store_is_not_listed_as_a_key():
    creds.set_endpoint("ollama", {"base_url": "http://localhost:11434"})
    assert "ollama" not in creds.list_configured()
    assert creds.get_endpoint("ollama")["base_url"] == "http://localhost:11434"
    creds.clear_endpoint("ollama")
    assert creds.get_endpoint("ollama") is None


# ---------------------------------------------------------------------------
# Against a live stub server
# ---------------------------------------------------------------------------
def test_discovery_and_proxy_bypass(stub_server, monkeypatch):
    # A dead corporate proxy in the environment: if requests used it, every
    # call below would fail. The self-hosted default must connect directly.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    creds.set_endpoint("ollama", {"base_url": stub_server})
    models = prov.get_provider("ollama").list_models()
    assert [m["id"] for m in models] == ["qwen2.5-coder:7b"]   # embed model hidden

    creds.set_endpoint("vllm", {"base_url": stub_server + "/v1"})
    models = prov.get_provider("vllm").list_models()
    assert models == [{"id": "Qwen/Qwen2.5-Coder-32B-Instruct",
                       "name": "Qwen/Qwen2.5-Coder-32B-Instruct",
                       "context_tokens": 32768}]


def test_unreachable_server_yields_no_models_and_is_cached(monkeypatch):
    creds.set_endpoint("vllm", {"base_url": "http://127.0.0.1:9"})
    p = prov.get_provider("vllm")
    assert p.list_models() == []
    # The failure is cached: a second listing must not hit the network again
    # (otherwise every provider-list request pays the discovery timeout).
    calls = []
    monkeypatch.setattr(p, "discover_models", lambda: calls.append(1) or [])
    assert p.list_models() == []
    assert calls == []
    # An explicit refresh (Save & test / Test connection) bypasses the cache.
    p.refresh_models()
    assert calls == [1]


def test_ollama_agent_loop_with_native_tools(stub_server):
    creds.set_endpoint("ollama", {"base_url": stub_server, "supports_tools": True})
    _Stub.script = [
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "read_file",
                                                  "arguments": {"path": "m.c"}}}]}},
        {"message": {"role": "assistant", "content": "m.c defines motor_stop."}},
    ]
    executed = []

    def executor(name, args):
        executed.append((name, args))
        return "void motor_stop(void) {}"

    answer = prov.generate_with_tools(
        "ollama", "qwen2.5-coder:7b", [{"role": "user", "content": "what is in m.c?"}],
        tools=[prov.Tool("read_file", "Read a file",
                         {"type": "object", "properties": {"path": {"type": "string"}}})],
        tool_executor=executor)
    assert answer == "m.c defines motor_stop."
    assert executed == [("read_file", {"path": "m.c"})]
    posts = [r for r in _Stub.requests_seen if r[0] == "POST"]
    assert posts[0][3]["tools"][0]["function"]["name"] == "read_file"
    # Second turn replays the assistant tool call then the tool result.
    second = posts[1][3]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert second[-1] == {"role": "tool", "content": "void motor_stop(void) {}",
                          "tool_name": "read_file"}


def test_vllm_text_fallback_when_tools_disabled(stub_server):
    creds.set_endpoint("vllm", {"base_url": stub_server, "supports_tools": False})
    _Stub.script = [
        {"choices": [{"message": {"role": "assistant", "content": "[READ: m.c]"}}]},
        {"choices": [{"message": {"role": "assistant", "content": "Done."}}]},
    ]
    answer = prov.generate_with_tools(
        "vllm", "Qwen/Qwen2.5-Coder-32B-Instruct",
        [{"role": "user", "content": "read m.c"}],
        tools=[prov.Tool("read_file", "Read a file",
                         {"type": "object", "properties": {"path": {"type": "string"}}})],
        tool_executor=lambda n, a: "int x;")
    assert answer == "Done."
    posts = [r for r in _Stub.requests_seen if r[0] == "POST"]
    assert "tools" not in posts[0][3]     # no native tools param sent


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
def test_endpoint_routes(stub_server):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    auth = {"Authorization": "Bearer t"}
    with TestClient(create_app(token="t")) as c:
        listed = {p["id"]: p for p in c.get("/api/ai/providers", headers=auth).json()["providers"]}
        assert listed["ollama"]["kind"] == "self_hosted"
        assert listed["ollama"]["configured"] is False
        assert listed["openai"]["kind"] == "cloud"

        r = c.put("/api/ai/providers/vllm/endpoint",
                  json={"base_url": stub_server + "/v1/", "api_key": "k1",
                        "supports_tools": True}, headers=auth)
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["configured"] is True and v["supports_tools"] is True
        assert v["endpoint"]["base_url"] == stub_server
        assert v["endpoint"]["has_api_key"] is True
        assert "k1" not in json.dumps(v)                 # key never echoed
        assert v["models"][0]["context_tokens"] == 32768

        t = c.post("/api/ai/providers/vllm/test", headers=auth).json()
        assert t["ok"] is True

        # api_key omitted → stored key kept; "" → cleared.
        c.put("/api/ai/providers/vllm/endpoint",
              json={"base_url": stub_server}, headers=auth)
        assert creds.get_key("vllm") == "k1"
        c.put("/api/ai/providers/vllm/endpoint",
              json={"base_url": stub_server, "api_key": ""}, headers=auth)
        assert creds.get_key("vllm") is None

        assert c.put("/api/ai/providers/vllm/endpoint", json={"base_url": "gpu:8000"},
                     headers=auth).status_code == 400
        assert c.put("/api/ai/providers/ollama/endpoint",
                     json={"base_url": stub_server, "num_ctx": 100},
                     headers=auth).status_code == 400
        assert c.put("/api/ai/providers/openai/endpoint",
                     json={"base_url": stub_server}, headers=auth).status_code == 400

        d = c.delete("/api/ai/providers/vllm/endpoint", headers=auth).json()
        assert d["configured"] is False and d["models"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
