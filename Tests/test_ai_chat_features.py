"""
AI Chat features: chat settings (effort, output cap, context size), token
usage, Copilot's native tools on both its APIs, Copilot credits, the
VS Code-style agents and rules, the encrypted chat history, and the API
around them. No network: provider HTTP is faked or never reached.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient

from Application_Logic import Logic_AI_Agents as agents
from Application_Logic import Logic_AI_Chat_History as history
from Application_Logic import Logic_AI_Providers as providers
from Application_Logic.Logic_AI_Providers import Tool, ToolCall

TOKEN = "chat-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHVALIDATOR_CONFIG_DIR", str(tmp_path / "creds"))
    monkeypatch.setenv("ARCH_CHAT_HISTORY_DIR", str(tmp_path / "chats"))
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "prefs.json"))
    yield


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
def test_parse_usage_every_shape():
    assert providers.parse_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 3}}) \
        == {"input_tokens": 10, "output_tokens": 3}
    # Responses API / Anthropic (cached tokens count as input)
    assert providers.parse_usage({"usage": {"input_tokens": 5, "output_tokens": 2,
                                            "cache_read_input_tokens": 7}}) \
        == {"input_tokens": 12, "output_tokens": 2}
    assert providers.parse_usage({"usageMetadata": {"promptTokenCount": 4,
                                                    "candidatesTokenCount": 1,
                                                    "thoughtsTokenCount": 2}}) \
        == {"input_tokens": 4, "output_tokens": 3}
    assert providers.parse_usage({"prompt_eval_count": 9, "eval_count": 8}) \
        == {"input_tokens": 9, "output_tokens": 8}
    assert providers.parse_usage({"choices": []}) == {}


def test_estimate_tokens():
    assert providers.estimate_tokens("") == 0
    assert providers.estimate_tokens("abcd" * 10) == 10
    assert providers.estimate_tokens({"a": "b"}) > 0


# ---------------------------------------------------------------------------
# Options → request bodies
# ---------------------------------------------------------------------------
def test_openai_effort_only_on_reasoning_models():
    p = providers.get_provider("openai")
    body = {"model": "gpt-5", "messages": [], "temperature": 0.2, "max_tokens": 100}
    p.apply_options(body, "gpt-5", {"effort": "high", "max_output_tokens": 500})
    assert body["reasoning_effort"] == "high" and "temperature" not in body
    assert body["max_tokens"] == 500
    body = {"model": "gpt-4o", "messages": [], "temperature": 0.2, "max_tokens": 100}
    p.apply_options(body, "gpt-4o", {"effort": "high"})
    assert "reasoning_effort" not in body


def test_anthropic_effort_becomes_thinking_budget():
    p = providers.get_provider("anthropic")
    body = {"model": "claude-sonnet-4", "max_tokens": 8192, "messages": []}
    p.apply_options(body, "claude-sonnet-4", {"effort": "medium"})
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert body["max_tokens"] > 8192
    body = {"model": "claude-3-5-haiku-latest", "max_tokens": 8192, "messages": []}
    p.apply_options(body, "claude-3-5-haiku-latest", {"effort": "medium"})
    assert "thinking" not in body


def test_gemini_effort_and_output_cap():
    p = providers.get_provider("gemini")
    body = {"contents": []}
    p.apply_options(body, "gemini-2.5-pro", {"effort": "low", "max_output_tokens": 256})
    assert body["generationConfig"] == {"maxOutputTokens": 256,
                                        "thinkingConfig": {"thinkingBudget": 1024}}
    body = {"contents": []}
    p.apply_options(body, "gemini-2.0-flash", {"effort": "low"})
    assert "generationConfig" not in body


def test_copilot_effort_on_both_apis(monkeypatch):
    p = providers.get_provider("copilot")
    monkeypatch.setattr(type(p), "_caps", {"gpt-6-luna": {"efforts": ["low", "high"],
                                                          "tools": True},
                                           "gpt-4.1": {"efforts": [], "tools": True}})
    body = {"model": "gpt-6-luna", "input": [], "max_output_tokens": 100}
    p.apply_options(body, "gpt-6-luna", {"effort": "high", "max_output_tokens": 50})
    assert body["reasoning"] == {"effort": "high"} and body["max_output_tokens"] == 50
    body = {"model": "gpt-4.1", "messages": [], "temperature": 0.2, "max_tokens": 9}
    p.apply_options(body, "gpt-4.1", {"effort": "high"})
    assert "reasoning_effort" not in body and body["temperature"] == 0.2
    # A level the model doesn't list isn't sent.
    body = {"model": "gpt-6-luna", "input": []}
    p.apply_options(body, "gpt-6-luna", {"effort": "max"})
    assert "reasoning" not in body


def test_ollama_options(monkeypatch):
    p = providers.get_provider("ollama")
    monkeypatch.setattr(type(p), "_show", lambda self, m: {
        "capabilities": ["completion", "tools", "thinking"], "context_length": 32768})
    body = {"model": "qwen3", "messages": [], "options": {"num_ctx": 16384}}
    p.apply_options(body, "qwen3", {"num_ctx": 999_999, "effort": "none",
                                    "max_output_tokens": 300})
    assert body["options"]["num_ctx"] == 32768        # clamped to the model's window
    assert body["options"]["num_predict"] == 300
    assert body["think"] is False
    assert p.supports_tools_for("qwen3") is True
    info = p.model_info("qwen3")
    assert info["configurable_context"] and info["max_context_tokens"] == 32768
    assert info["efforts"] == ["none", "low", "medium", "high"]


def test_ollama_no_think_for_models_without_thinking(monkeypatch):
    p = providers.get_provider("ollama")
    monkeypatch.setattr(type(p), "_show", lambda self, m: {"capabilities": ["completion"]})
    body = {"model": "llama3.2", "messages": [], "options": {}}
    p.apply_options(body, "llama3.2", {"effort": "high"})
    assert "think" not in body


# ---------------------------------------------------------------------------
# Copilot tools on the Responses API
# ---------------------------------------------------------------------------
def test_responses_body_converts_tool_turns():
    convo = [
        {"role": "user", "content": "where is Motor_Init?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "search_code", "arguments": "{\"query\": \"Motor_Init\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "motor.c:10: void Motor_Init(void)"},
    ]
    tools = providers.get_provider("copilot").build_tools_param(
        [Tool("search_code", "Search", {"type": "object", "properties": {}})])
    body = providers._responses_body("gpt-6-luna", "sys", convo, tools)
    assert body["instructions"] == "sys"
    assert body["input"][0] == {"role": "user", "content": "where is Motor_Init?"}
    assert body["input"][1] == {"type": "function_call", "call_id": "c1",
                                "name": "search_code",
                                "arguments": "{\"query\": \"Motor_Init\"}"}
    assert body["input"][2]["type"] == "function_call_output"
    assert body["tools"][0] == {"type": "function", "name": "search_code",
                                "description": "Search",
                                "parameters": {"type": "object", "properties": {}}}


def test_copilot_reads_responses_tool_calls():
    p = providers.get_provider("copilot")
    data = {"output": [
        {"type": "reasoning", "id": "r1"},
        {"type": "function_call", "call_id": "c9", "name": "read_file",
         "arguments": "{\"path\": \"a.c\"}"}]}
    assert p.parse_tool_calls(data) == [ToolCall(id="c9", name="read_file",
                                                 input={"path": "a.c"})]
    turn = p.capture_assistant_turn(data)
    assert turn["tool_calls"][0]["id"] == "c9"
    assert p.format_tool_results({"c9": ("read_file", "int x;")}) == [
        {"role": "tool", "tool_call_id": "c9", "content": "int x;"}]


def test_copilot_tool_loop_on_responses(monkeypatch):
    """A whole turn: the model asks for a tool on /responses, gets the result
    as function_call_output, then answers."""
    p = providers.get_provider("copilot")
    monkeypatch.setattr(type(p), "_responses_only", {"gpt-6-luna"})
    monkeypatch.setattr(type(p), "_fresh_copilot_token", lambda self: "tok")
    sent = []
    replies = [
        {"output": [{"type": "function_call", "call_id": "c1", "name": "search_code",
                     "arguments": "{\"query\": \"x\"}"}],
         "usage": {"input_tokens": 50, "output_tokens": 5}},
        {"output": [{"type": "message", "content": [{"type": "output_text",
                                                     "text": "Found it in `a.c:1`."}]}],
         "usage": {"input_tokens": 80, "output_tokens": 9}},
    ]

    def fake_post(self, url, headers, body, stop_check=None):
        sent.append((url, json.loads(json.dumps(body))))
        return replies[len(sent) - 1]

    monkeypatch.setattr(providers.AIProvider, "_post_and_json", fake_post)
    usage = []
    text = providers.generate_with_tools(
        "copilot", "gpt-6-luna", [{"role": "user", "content": "q"}],
        [Tool("search_code", "s", {"type": "object", "properties": {}})],
        tool_executor=lambda n, a: "a.c:1: x", options={}, on_usage=usage.append)
    assert text == "Found it in `a.c:1`."
    assert all(u.endswith("/responses") for u, _ in sent)
    assert sent[0][1]["tools"][0]["name"] == "search_code"
    second = sent[1][1]["input"]
    assert second[1]["type"] == "function_call"
    assert second[2] == {"type": "function_call_output", "call_id": "c1", "output": "a.c:1: x"}
    assert usage == [{"input_tokens": 50, "output_tokens": 5},
                     {"input_tokens": 80, "output_tokens": 9}]


def test_fallback_grammar_covers_function_tags():
    calls = providers._parse_fallback_calls("[FUNCTION: Motor_Init]\n[CALLGRAPH: Main]")
    assert [(c.name, c.input) for c in calls] == [("get_function", {"name": "Motor_Init"}),
                                                  ("get_call_graph", {"name": "Main"})]


def test_parse_copilot_quota():
    q = providers.parse_copilot_quota({
        "copilot_plan": "individual", "access_type_sku": "free_limited_copilot",
        "quota_reset_date": "2026-12-01", "token_based_billing": True,
        "quota_snapshots": {"chat": {"entitlement": 200, "remaining": 150,
                                     "credits_used": 50, "percent_remaining": 75.0,
                                     "unlimited": False, "has_quota": True}}})
    assert q["plan"] == "individual" and q["reset_date"] == "2026-12-01"
    assert q["quotas"]["chat"]["used"] == 50 and q["quotas"]["chat"]["remaining"] == 150
    old = providers.parse_copilot_quota({"monthly_quotas": {"chat": 50},
                                         "limited_user_quotas": {"chat": 40}})
    assert old["quotas"]["chat"]["used"] == 10


# ---------------------------------------------------------------------------
# Agents and rules
# ---------------------------------------------------------------------------
AGENT_MD = """---
description: 'Plans a change'
tools: ['codebase', 'fetch']
model: GPT-4.1 (copilot)
---
Make a plan first.
"""

RULE_MD = """---
applyTo:
  - "**/*.c"
  - "drivers/*.h"
---
Follow MISRA C:2012.
"""


def test_parse_agent_md_maps_vscode_tools():
    a = agents.parse_agent_md(AGENT_MD, "planner.agent.md")
    assert a["name"] == "planner" and a["description"] == "Plans a change"
    assert a["model"] == "GPT-4.1 (copilot)"
    assert a["instructions"] == "Make a plan first."
    assert "search_code" in a["tools"] and "read_file" in a["tools"]
    # No tools key → every tool; only unknown tools → every tool too.
    assert agents.parse_agent_md("hello", "x.agent.md")["tools"] is None
    assert agents.map_tools(["runCommands", "edit"]) is None
    assert agents.map_tools([]) is None
    # Round trip through the VS Code format.
    again = agents.parse_agent_md(agents.agent_to_md(a), "planner.agent.md")
    assert again["tools"] == a["tools"] and again["instructions"] == a["instructions"]


def test_rules_apply_by_glob():
    r = agents.parse_instructions_md(RULE_MD, "misra.instructions.md")
    assert r["apply_to"] == "**/*.c,drivers/*.h"
    assert agents.rule_applies(r, ["app/motor.c"])
    assert agents.rule_applies(r, ["drivers/can.h"])
    assert not agents.rule_applies(r, ["app/motor.h"])
    always = agents.parse_instructions_md("Be brief.", "AGENTS.md")
    assert agents.rule_applies(always, [])
    assert not agents.rule_applies({**always, "enabled": False}, [])


def test_import_from_folder(tmp_path):
    gh = tmp_path / ".github"
    (gh / "agents").mkdir(parents=True)
    (gh / "instructions").mkdir()
    (gh / "agents" / "planner.agent.md").write_text(AGENT_MD)
    (gh / "chatmodes").mkdir()
    (gh / "chatmodes" / "old.chatmode.md").write_text("Old mode.")
    (gh / "instructions" / "misra.instructions.md").write_text(RULE_MD)
    (gh / "copilot-instructions.md").write_text("Always cite files.")
    (tmp_path / "node_modules" / ".github").mkdir(parents=True)
    (tmp_path / "node_modules" / ".github" / "x.agent.md").write_text("skip me")
    found = agents.import_from_folder(str(tmp_path))
    assert sorted(a["name"] for a in found["agents"]) == ["old", "planner"]
    names = sorted(r["name"] for r in found["rules"])
    assert names == ["copilot-instructions", "misra"]
    with pytest.raises(FileNotFoundError):
        agents.import_from_folder(str(tmp_path / "nope"))


class _MetaDB:
    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value


def test_save_and_find_project_agents():
    db = _MetaDB()
    saved = agents.save_project_agents(db, [
        {"name": "Planner", "instructions": "plan", "tools": ["read_file", "bogus"]},
        {"name": "Planner", "instructions": "again"},
        {"id": "builtin:ask", "name": "Ask", "source": "builtin"},   # never stored
    ])
    assert [a["id"] for a in saved] == ["project:planner", "project:planner-2"]
    assert saved[0]["tools"] == ["read_file"]
    ids = [a["id"] for a in agents.all_agents(db)]
    assert ids[:3] == ["builtin:ask", "builtin:test-engineer", "builtin:reviewer"]
    assert agents.find_agent(db, "project:planner")["instructions"] == "plan"
    assert agents.find_agent(db, "missing")["id"] == "builtin:ask"
    assert agents.find_agent(None, None)["id"] == "builtin:ask"


def test_compose_system_prompt():
    rule_c = {"name": "misra", "apply_to": "**/*.c", "body": "MISRA!", "enabled": True}
    rule_all = {"name": "brief", "apply_to": "", "body": "Be brief.", "enabled": True}
    agent = {"name": "Reviewer", "instructions": "Review it."}
    text = agents.compose_system_prompt("Project rules.", agent, [rule_c, rule_all],
                                        files=["a/b.c"], extra="# CODE MIND MAP\nx")
    assert text.index("Project rules.") < text.index("# AGENT: Reviewer") \
        < text.index("MISRA!") < text.index("# CITING CODE") < text.index("# CODE MIND MAP")
    assert "Be brief." in text
    assert "MISRA!" not in agents.compose_system_prompt("r", agent, [rule_c], files=["a.py"])


def test_tools_for_agent():
    tools = [Tool(n, n) for n in agents.ALL_TOOLS]
    assert len(agents.tools_for_agent({"tools": None}, tools)) == len(tools)
    assert [t.name for t in agents.tools_for_agent({"tools": ["read_file"]}, tools)] \
        == ["read_file"]


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------
def test_history_round_trip_is_encrypted(tmp_path):
    key = history.project_key("/projects/vcu.arch")
    assert key != history.project_key("/projects/other.arch")
    s = history.save_session(key, {"id": "abc", "provider_id": "ollama", "model": "m",
                                   "messages": [{"role": "user", "content": "What calls Motor_Init?"},
                                                {"role": "assistant", "content": "Main."}],
                                   "usage": {"input_tokens": 10}})
    assert s["title"] == "What calls Motor_Init?" and s["message_count"] == 2
    raw = (tmp_path / "chats" / f"{key}.aichats").read_bytes()
    assert b"Motor_Init" not in raw                      # encrypted at rest
    assert history.get_session(key, "abc")["messages"][1]["content"] == "Main."
    # Rollback = save the shorter list.
    history.save_session(key, {"id": "abc", "messages": [{"role": "user", "content": "Q2"}]})
    assert history.get_session(key, "abc")["messages"] == [{"role": "user", "content": "Q2"}]
    assert history.rename_session(key, "abc", "  My   chat ")["title"] == "My chat"
    # A renamed chat keeps its title on later saves.
    history.save_session(key, {"id": "abc", "title": "My chat", "messages": []})
    assert history.list_sessions(key)[0]["title"] == "My chat"
    assert history.delete_session(key, "abc") is True
    assert history.list_sessions(key) == []
    assert history.delete_session(key, "abc") is False


def test_history_unreadable_file_starts_fresh(tmp_path):
    key = "deadbeef"
    d = tmp_path / "chats"
    d.mkdir()
    (d / f"{key}.aichats").write_bytes(b"not a fernet token")
    assert history.list_sessions(key) == []
    assert (d / f"{key}.unreadable").exists()


def test_history_keeps_newest_sessions(monkeypatch):
    monkeypatch.setattr(history, "MAX_SESSIONS", 3)
    key = "k"
    for i in range(5):
        history.save_session(key, {"id": f"s{i}", "messages": [{"role": "user",
                                                                 "content": f"q{i}"}]})
    assert len(history.list_sessions(key)) == 3


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture()
def client():
    from backend.app import create_app
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


def test_chats_api(client):
    r = client.put("/api/ai/chats/s1", headers=AUTH, json={
        "provider_id": "ollama", "model": "llama3.2",
        "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200 and r.json()["title"] == "hello"
    assert [s["id"] for s in client.get("/api/ai/chats", headers=AUTH).json()["sessions"]] \
        == ["s1"]
    assert client.get("/api/ai/chats/s1", headers=AUTH).json()["model"] == "llama3.2"
    assert client.patch("/api/ai/chats/s1", headers=AUTH,
                        json={"title": "Renamed"}).json()["title"] == "Renamed"
    assert client.delete("/api/ai/chats/s1", headers=AUTH).json() == {"deleted": True}
    assert client.get("/api/ai/chats/s1", headers=AUTH).status_code == 404


def test_agents_api_without_project(client, tmp_path):
    body = client.get("/api/ai/agents", headers=AUTH).json()
    assert body["default_agent_id"] == "builtin:ask"
    assert [a["id"] for a in body["agents"]][0] == "builtin:ask"
    assert "read_file" in body["tools"]
    # Saving needs an open project.
    assert client.put("/api/ai/agents", headers=AUTH, json={"agents": []}).status_code == 409
    gh = tmp_path / "repo" / ".github" / "agents"
    gh.mkdir(parents=True)
    (gh / "planner.agent.md").write_text(AGENT_MD)
    found = client.post("/api/ai/agents/import", headers=AUTH,
                        json={"path": str(tmp_path / "repo")}).json()
    assert found["agents"][0]["name"] == "planner"


def test_context_and_usage_api(client):
    body = {"provider_id": "ollama", "model": "llama3.2",
            "messages": [{"role": "user", "content": "x" * 400}],
            "options": {"num_ctx": 8192}}
    ctx = client.post("/api/ai/context", headers=AUTH, json=body).json()
    assert ctx["window"] == 8192
    assert ctx["history"] == 100 and ctx["total"] >= ctx["system"] + 100
    assert ctx["project_open"] is False and ctx["tool_names"] == []
    assert client.get("/api/ai/usage/ollama", headers=AUTH).json() == {"kind": "local"}
    assert client.get("/api/ai/usage/openai", headers=AUTH).json() == {"kind": "tokens"}
    assert client.get("/api/ai/usage/copilot", headers=AUTH).json() == {
        "kind": "quota", "signed_in": False}


def test_fit_history_drops_oldest_turns():
    from backend.routers.ai import _fit_history
    msgs = [{"role": "user", "content": "a" * 4000}, {"role": "assistant", "content": "b" * 4000},
            {"role": "user", "content": "c" * 40}]
    kept, dropped = _fit_history(msgs, fixed_tokens=0, window=4096 + 500)
    assert kept == [msgs[2]] and dropped == 2
    kept, dropped = _fit_history(msgs, fixed_tokens=0, window=1_000_000)
    assert kept == msgs and dropped == 0


def test_quota_delta():
    from backend.routers.ai import _quota_delta
    before = {"quotas": {"chat": {"used": 10, "remaining": 190}}}
    after = {"quotas": {"chat": {"used": 11.5, "remaining": 188.5}}}
    assert _quota_delta(before, after) == {"chat": 1.5}
    assert _quota_delta(None, after) == {}


def test_tool_calls_written_as_json_text_are_run(monkeypatch):
    """qwen2.5-coder on Ollama writes calls as JSON text; a real tool name runs,
    anything else stays the answer."""
    calls = providers._text_json_calls(
        '```json\n{"name": "search_code", "arguments": {"query": "Torque"}}\n```', {"search_code"})
    assert [(c.name, c.input) for c in calls] == [("search_code", {"query": "Torque"})]
    assert providers._text_json_calls('{"name": "Pwt_Torque_Compute", "arguments": {}}',
                                      {"search_code"}) == []
    assert providers._text_json_calls("plain answer", {"search_code"}) == []


class _FakeSource:
    def __init__(self, files):
        self.files = files

    def list_files(self, exts=None):
        from Application_Logic.Logic_Source_Store import SourceFile
        return [SourceFile(p, len(t), "." + p.rsplit(".", 1)[-1]) for p, t in self.files.items()]

    def read_file(self, rel):
        return self.files.get(rel)


def test_tools_forgive_near_miss_names():
    from Application_Logic.Logic_AI_Tools import ToolExecutor, ToolError, find_function
    src = _FakeSource({"powertrain/torque.c": "int Pwt_Torque_Compute(void){}",
                       "powertrain/throttle.c": "x", "body/torque.c": "y"})
    ex = ToolExecutor(None, provider=src)
    # A plain word lists files containing it; globs still work.
    assert ex.list_files("throttle") == "powertrain/throttle.c"
    assert ex.list_files("POWERTRAIN/*.c").count("\n") == 1
    # A wrong prefix or a bare unique name still reads the file.
    assert ex.read_file("src/powertrain/throttle.c") == "x"
    assert ex.read_file("throttle.c") == "x"
    with pytest.raises(ToolError, match="Did you mean: powertrain/torque.c, body/torque.c"):
        ex.read_file("torque.c")          # two files of that name
    funcs = {"Pwt_Torque_Compute": {}, "Pwt_Torque_Init": {}}
    assert find_function(funcs, "p_torque_Compute") == ("Pwt_Torque_Compute", [])
    assert find_function(funcs, "pwt_torque_compute") == ("Pwt_Torque_Compute", [])
    assert find_function(funcs, "Torque")[0] is None
    assert find_function(funcs, "Nope") == (None, [])
