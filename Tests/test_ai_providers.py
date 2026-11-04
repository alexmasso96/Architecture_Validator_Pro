"""
Tests for Logic_AI_Providers — provider adapters.

Network is never touched: request-building and response-parsing are pure and
tested directly; generate() is tested with a stubbed requests.post.
"""
import os
import sys
import importlib

sys.path.append(os.path.abspath("src"))

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Credential store in a temp dir + freshly reloaded providers module."""
    monkeypatch.setenv("ARCHVALIDATOR_CONFIG_DIR", str(tmp_path))
    import Application_Logic.Logic_AI_Credentials as creds
    importlib.reload(creds)
    import Application_Logic.Logic_AI_Providers as prov
    importlib.reload(prov)
    return prov, creds


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# Registry / gating
# ---------------------------------------------------------------------------

def test_registry(env):
    prov, _ = env
    ids = {p.id for p in prov.list_providers()}
    assert ids == {"copilot", "anthropic", "openai", "gemini", "ollama", "vllm"}
    with pytest.raises(prov.AIError):
        prov.get_provider("nope")


def test_is_configured_gating(env):
    prov, creds = env
    assert prov.get_provider("anthropic").is_configured() is False
    creds.set_key("anthropic", "sk-ant-x")
    assert prov.get_provider("anthropic").is_configured() is True
    assert prov.get_provider("copilot").is_configured() is False
    creds.set_copilot_oauth_token("ghu_x")
    assert prov.get_provider("copilot").is_configured() is True


# ---------------------------------------------------------------------------
# build_request — per provider schema
# ---------------------------------------------------------------------------

def test_anthropic_build_request(env):
    prov, creds = env
    creds.set_key("anthropic", "sk-ant-KEY")
    p = prov.get_provider("anthropic")
    url, headers, body = p.build_request(
        [{"role": "user", "content": "hi"}], "claude-sonnet-4-20250514", "be brief")
    assert url == prov.ANTHROPIC_URL
    assert headers["x-api-key"] == "sk-ant-KEY"
    assert headers["anthropic-version"] == prov.ANTHROPIC_VERSION
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "max_tokens" in body


def test_openai_build_request_standard(env):
    prov, creds = env
    creds.set_key("openai", "sk-oai-KEY")
    p = prov.get_provider("openai")
    url, headers, body = p.build_request(
        [{"role": "user", "content": "hi"}], "gpt-4.1", "sys")
    assert url == prov.OPENAI_URL
    assert headers["Authorization"] == "Bearer sk-oai-KEY"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["temperature"] == prov._DEFAULT_TEMPERATURE
    assert "max_tokens" in body


def test_openai_build_request_reasoning_model(env):
    prov, creds = env
    creds.set_key("openai", "k")
    p = prov.get_provider("openai")
    _, _, body = p.build_request([{"role": "user", "content": "x"}], "o3-mini", None)
    # o-series: no temperature/max_tokens, uses max_completion_tokens.
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert "max_completion_tokens" in body


def test_gemini_build_request(env):
    prov, creds = env
    creds.set_key("gemini", "G-KEY")
    p = prov.get_provider("gemini")
    url, headers, body = p.build_request(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
        "gemini-2.0-flash", "system text")
    assert url.endswith("gemini-2.0-flash:generateContent?key=G-KEY")
    assert body["systemInstruction"]["parts"][0]["text"] == "system text"
    # assistant role maps to 'model'
    assert body["contents"][0]["role"] == "user"
    assert body["contents"][1]["role"] == "model"
    assert body["contents"][0]["parts"][0]["text"] == "hi"


def test_copilot_build_request_uses_cached_token(env, monkeypatch):
    prov, creds = env
    creds.set_copilot_oauth_token("ghu_x")
    p = prov.get_provider("copilot")
    # Pretend the short-lived copilot token is already cached (skip network).
    monkeypatch.setattr(p, "_fresh_copilot_token", lambda: "CPTOK")
    url, headers, body = p.build_request(
        [{"role": "user", "content": "hi"}], "gpt-4.1", "sys")
    assert url.endswith("/chat/completions")
    assert headers["Authorization"] == "Bearer CPTOK"
    assert headers["Copilot-Integration-Id"] == "vscode-chat"
    assert headers["Editor-Version"] == "vscode/1.100.0"
    assert body["messages"][0]["role"] == "system"
    assert body["stream"] is False


def test_copilot_reasoning_model_no_temperature(env, monkeypatch):
    prov, creds = env
    creds.set_copilot_oauth_token("ghu_x")
    p = prov.get_provider("copilot")
    monkeypatch.setattr(p, "_fresh_copilot_token", lambda: "CPTOK")
    _, _, body = p.build_request([{"role": "user", "content": "x"}], "o3-mini", None)
    assert "temperature" not in body and "max_tokens" not in body
    assert "max_completion_tokens" in body


def test_build_request_without_key_raises(env):
    prov, _ = env
    for pid in ("anthropic", "openai", "gemini"):
        with pytest.raises(prov.AIAuthError):
            prov.get_provider(pid).build_request(
                [{"role": "user", "content": "x"}], "m", None)


# ---------------------------------------------------------------------------
# parse_response — per provider schema
# ---------------------------------------------------------------------------

def test_parse_openai_and_copilot(env):
    prov, _ = env
    payload = {"choices": [{"message": {"content": "hello world"}}]}
    assert prov.get_provider("openai").parse_response(payload) == "hello world"
    assert prov.get_provider("copilot").parse_response(payload) == "hello world"


def test_parse_anthropic(env):
    prov, _ = env
    payload = {"content": [{"type": "text", "text": "claude says hi"}]}
    assert prov.get_provider("anthropic").parse_response(payload) == "claude says hi"


def test_parse_gemini(env):
    prov, _ = env
    payload = {"candidates": [{"content": {"parts": [{"text": "gem"}, {"text": "ini"}]}}]}
    assert prov.get_provider("gemini").parse_response(payload) == "gemini"


def test_parse_malformed_returns_empty(env):
    prov, _ = env
    for pid in ("openai", "anthropic", "gemini", "copilot"):
        assert prov.get_provider(pid).parse_response({}) == ""


# ---------------------------------------------------------------------------
# generate() dispatch with stubbed network
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dynamic model discovery
# ---------------------------------------------------------------------------

def test_openai_models_list_filters_non_chat(env):
    prov, _ = env
    items = [
        {"id": "gpt-4.1"}, {"id": "gpt-4o"}, {"id": "text-embedding-3-large"},
        {"id": "whisper-1"}, {"id": "gpt-4o"},  # dup
    ]
    out = prov._models_from_openai_list(items)
    ids = [m["id"] for m in out]
    assert ids == ["gpt-4.1", "gpt-4o"]  # embeddings/whisper dropped, dedup'd


def test_openai_discover_models(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    payload = {"data": [{"id": "gpt-4.1"}, {"id": "o3-mini"},
                        {"id": "text-embedding-3-small"}, {"id": "dall-e-3"}]}
    monkeypatch.setattr(prov.requests, "get", lambda *a, **k: FakeResp(200, payload))
    ids = [m["id"] for m in prov.get_provider("openai").discover_models()]
    assert "gpt-4.1" in ids and "o3-mini" in ids
    assert "text-embedding-3-small" not in ids and "dall-e-3" not in ids


def test_gemini_discover_models(env, monkeypatch):
    prov, creds = env
    creds.set_key("gemini", "k")
    payload = {"models": [
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
    ]}
    monkeypatch.setattr(prov.requests, "get", lambda *a, **k: FakeResp(200, payload))
    ids = [m["id"] for m in prov.get_provider("gemini").discover_models()]
    assert ids == ["gemini-2.0-flash"]  # embedding-only model excluded


def test_anthropic_discover_models(env, monkeypatch):
    prov, creds = env
    creds.set_key("anthropic", "k")
    payload = {"data": [{"id": "claude-opus-4-20250514", "display_name": "Claude Opus 4"}]}
    monkeypatch.setattr(prov.requests, "get", lambda *a, **k: FakeResp(200, payload))
    out = prov.get_provider("anthropic").discover_models()
    assert out[0]["id"] == "claude-opus-4-20250514"
    assert out[0]["name"] == "Claude Opus 4"


def test_discover_returns_empty_on_error(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    monkeypatch.setattr(prov.requests, "get", lambda *a, **k: FakeResp(500, {}, "err"))
    assert prov.get_provider("openai").discover_models() == []


def test_discover_without_key_returns_empty(env):
    prov, _ = env
    assert prov.get_provider("openai").discover_models() == []
    assert prov.get_provider("gemini").discover_models() == []


def test_generate_success(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    payload = {"choices": [{"message": {"content": "generated"}}]}
    monkeypatch.setattr(prov.requests, "post",
                        lambda *a, **k: FakeResp(200, payload))
    chunks = []
    out = prov.generate("openai", "gpt-4.1", [{"role": "user", "content": "go"}],
                        system_prompt="sys", stream_cb=chunks.append)
    assert out == "generated"
    assert chunks == ["generated"]


def test_generate_auth_error(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    monkeypatch.setattr(prov.requests, "post",
                        lambda *a, **k: FakeResp(401, {}, "unauthorized"))
    with pytest.raises(prov.AIAuthError):
        prov.generate("openai", "gpt-4.1", [{"role": "user", "content": "x"}])


def test_generate_stopped(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    with pytest.raises(prov.AIStopped):
        prov.generate("openai", "gpt-4.1", [{"role": "user", "content": "x"}],
                      stop_check=lambda: True)


def test_generate_empty_content_raises(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    monkeypatch.setattr(prov.requests, "post",
                        lambda *a, **k: FakeResp(200, {"choices": [{"message": {"content": ""}}]}))
    with pytest.raises(prov.AIGenerationError):
        prov.generate("openai", "gpt-4.1", [{"role": "user", "content": "x"}])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# GitHub Enterprise sign-in (#13)
# ---------------------------------------------------------------------------

def test_github_host_is_normalized(env):
    prov, _ = env
    assert prov.normalize_github_host("") == "github.com"
    assert prov.normalize_github_host("  https://Acme.GHE.com/login ") == "acme.ghe.com"
    assert prov.normalize_github_host("acme.ghe.com:443") == "acme.ghe.com"
    for bad in ("acme", "not a host", "acme..ghe.com", "-acme.ghe.com"):
        with pytest.raises(ValueError):
            prov.normalize_github_host(bad)


def test_enterprise_host_endpoints(env):
    prov, _ = env
    assert prov.github_urls("github.com")["device_code"] == prov.DEVICE_CODE_URL
    urls = prov.github_urls("acme.ghe.com")
    assert urls["device_code"] == "https://acme.ghe.com/login/device/code"
    assert urls["oauth_token"] == "https://acme.ghe.com/login/oauth/access_token"
    assert urls["copilot_token"] == "https://api.acme.ghe.com/copilot_internal/v2/token"
    assert urls["api_base"] == "https://copilot-api.acme.ghe.com"


def test_sign_in_goes_to_the_enterprise_host(env, monkeypatch):
    prov, creds = env
    p = prov.get_provider("copilot")
    p.set_host("acme.ghe.com")
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("/device/code"):
            return FakeResp(200, {"device_code": "D", "user_code": "U", "interval": 0})
        return FakeResp(200, {"access_token": "ghu_acme"})

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResp(200, {"token": "CP", "expires_at": 9e12})

    monkeypatch.setattr(prov.requests, "post", fake_post)
    monkeypatch.setattr(prov.requests, "get", fake_get)
    monkeypatch.setattr(prov.time, "sleep", lambda _s: None)
    flow = p.start_device_flow()
    assert flow["verification_uri"] == "https://acme.ghe.com/login/device"
    assert p.poll_for_token("D", interval=0) == "ghu_acme"
    assert p._fresh_copilot_token() == "CP"
    # No endpoints in the reply: Copilot is on the enterprise host's own API.
    assert type(p)._cp_api_base == "https://copilot-api.acme.ghe.com"
    assert calls == ["https://acme.ghe.com/login/device/code",
                     "https://acme.ghe.com/login/oauth/access_token",
                     "https://api.acme.ghe.com/copilot_internal/v2/token"]


def test_changing_the_host_signs_out(env):
    prov, creds = env
    p = prov.get_provider("copilot")
    creds.set_copilot_oauth_token("ghu_dotcom")
    p.set_host("github.com")    # unchanged: still signed in
    assert p.is_configured()
    p.set_host("acme.ghe.com")
    assert not p.is_configured()
    assert prov.github_host() == "acme.ghe.com"
    creds.set_copilot_oauth_token("ghu_acme")
    p.set_host("")
    assert not p.is_configured()
    assert prov.github_host() == "github.com"


def test_enterprise_server_without_copilot_says_so(env, monkeypatch):
    prov, creds = env
    p = prov.get_provider("copilot")
    p.set_host("github.acme.com")
    creds.set_copilot_oauth_token("ghu_x")
    monkeypatch.setattr(prov.requests, "get", lambda url, **kw: FakeResp(404))
    with pytest.raises(prov.AIAuthError, match="doesn't offer Copilot"):
        p._fresh_copilot_token()


# ---------------------------------------------------------------------------
# list_models() uses live discovery for cloud providers
# ---------------------------------------------------------------------------

def test_list_models_static_when_not_configured(env, monkeypatch):
    prov, _ = env
    def boom(*a, **k):
        raise AssertionError("no network when signed out")
    monkeypatch.setattr(prov.requests, "get", boom)
    p = prov.get_provider("openai")
    assert p.list_models() == p._models


def test_list_models_uses_discovery_and_caches(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    calls = []
    payload = {"data": [{"id": "gpt-6"}]}
    monkeypatch.setattr(prov.requests, "get",
                        lambda *a, **k: calls.append(1) or FakeResp(200, payload))
    p = prov.get_provider("openai")
    assert [m["id"] for m in p.list_models()] == ["gpt-6"]
    assert [m["id"] for m in p.list_models()] == ["gpt-6"]
    assert len(calls) == 1                      # second call served from cache
    p.refresh_models()
    assert len(calls) == 2


def test_list_models_rediscovers_after_key_change(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k1")
    calls = []
    monkeypatch.setattr(prov.requests, "get",
                        lambda *a, **k: calls.append(1) or FakeResp(200, {"data": [{"id": "gpt-6"}]}))
    p = prov.get_provider("openai")
    p.list_models()
    creds.set_key("openai", "k2")
    p.list_models()
    assert len(calls) == 2


def test_list_models_falls_back_to_static_on_failure(env, monkeypatch):
    prov, creds = env
    creds.set_key("openai", "k")
    monkeypatch.setattr(prov.requests, "get", lambda *a, **k: FakeResp(500, {}, "err"))
    p = prov.get_provider("openai")
    assert p.list_models() == p._models


def test_copilot_discovery_filters_picker_models(env, monkeypatch):
    prov, creds = env
    creds.set_copilot_oauth_token("gho_x")
    token = {"token": "cp", "expires_at": 9e12,
             "endpoints": {"api": "https://api.individual.githubcopilot.com"}}
    models = {"data": [
        {"id": "claude-opus-4.5", "name": "Claude Opus 4.5", "model_picker_enabled": True,
         "capabilities": {"type": "chat", "limits": {"max_prompt_tokens": 128000}},
         "policy": {"state": "enabled"}},
        {"id": "gpt-5", "name": "GPT-5", "model_picker_enabled": True,
         "capabilities": {"type": "chat"}, "policy": {"state": "unconfigured"}},
        {"id": "gpt-4o-2024-05-13", "model_picker_enabled": False,
         "capabilities": {"type": "chat"}},
        {"id": "secret-model", "capabilities": {"type": "chat"},
         "policy": {"state": "disabled"}},
        {"id": "text-embedding-3-small", "capabilities": {"type": "embeddings"}},
    ]}
    seen = []
    def fake_get(url, *a, **k):
        seen.append(url)
        return FakeResp(200, token if "copilot_internal" in url else models)
    monkeypatch.setattr(prov.requests, "get", fake_get)
    p = prov.get_provider("copilot")
    type(p)._cp_token = ""
    out = p.list_models()
    assert [m["id"] for m in out] == ["claude-opus-4.5", "gpt-5"]
    assert out[0]["context_tokens"] == 128000
    assert seen[-1] == "https://api.individual.githubcopilot.com/models"
