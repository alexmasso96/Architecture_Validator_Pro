"""
AI router (plan §3.2): provider config + SSE-streamed chat.

    GET    /api/ai/providers           → providers with configured flag + model catalogue
    PUT    /api/ai/providers/{id}       → set the API key (api-key providers; Copilot uses device flow)
    DELETE /api/ai/providers/{id}       → clear the stored key
    POST   /api/ai/chat                 → SSE stream: context, tool*, usage*, token, credits?, done|error
    POST   /api/ai/context              → estimated context use of a chat request
    GET    /api/ai/model-info           → context window / efforts / tools of one model
    GET    /api/ai/usage/{id}           → the account's credits (Copilot quota)
    GET|PUT|PATCH|DELETE /api/ai/chats  → this user's chat history for the open project
    GET|PUT /api/ai/agents, PUT /api/ai/rules, POST /api/ai/agents/import|export

Provider credentials are machine-global (encrypted file via Logic_AI_Credentials,
honoring ARCHVALIDATOR_CONFIG_DIR), independent of the open project. Chat
streams tokens by running the blocking provider call on a worker thread and
hopping each chunk onto the event loop (the same thread→loop bridge the global
SSE bus uses).
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from Application_Logic import Logic_AI_Providers as providers
from Application_Logic import Logic_AI_Credentials as creds

from ..deps import get_state
from ..security import require_token
from ..state import AppState, ProjectError

router = APIRouter(prefix="/api/ai", tags=["ai"],
                   dependencies=[Depends(require_token)])


def _guard(fn):
    try:
        return fn()
    except ProjectError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class SetKeyBody(BaseModel):
    api_key: str


def _provider_view(p) -> dict:
    view = {
        "id": p.id,
        "label": p.label,
        "configured": p.is_configured(),
        "supports_tools": getattr(p, "SUPPORTS_TOOLS", False),
        "models": p.list_models(),
        "kind": "self_hosted" if isinstance(p, providers.SelfHostedProvider) else "cloud",
    }
    if p.id == "copilot":
        view["github_host"] = providers.github_host()
    if isinstance(p, providers.SelfHostedProvider):
        ep = p.endpoint()
        view["endpoint"] = {
            "base_url": ep.get("base_url") or "",
            "default_base_url": p.default_base_url,
            "supports_tools": bool(ep.get("supports_tools")),
            "use_proxy": bool(ep.get("use_proxy")),
            "num_ctx": ep.get("num_ctx"),
            "has_api_key": bool(creds.get_key(p.id)),  # never the key itself
        }
    return view


class EndpointBody(BaseModel):
    base_url: str
    # None = keep the stored key; "" = clear it (vLLM --api-key; Ollama: unused).
    api_key: Optional[str] = None
    supports_tools: bool = False
    use_proxy: bool = False
    num_ctx: Optional[int] = None


def _self_hosted(provider_id: str):
    try:
        p = providers.get_provider(provider_id)
    except providers.AIError:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")
    if not isinstance(p, providers.SelfHostedProvider):
        raise HTTPException(status_code=400,
                            detail=f"{p.label} is not a self-hosted provider.")
    return p


@router.put("/providers/{provider_id}/endpoint")
def set_provider_endpoint(provider_id: str, body: EndpointBody) -> dict:
    """Configure a self-hosted provider (Ollama / vLLM)."""
    p = _self_hosted(provider_id)
    err = providers.validate_base_url(body.base_url)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if body.num_ctx is not None and not (2048 <= body.num_ctx <= 1_048_576):
        raise HTTPException(status_code=400,
                            detail="Context size must be between 2048 and 1048576 tokens.")
    cfg = {"base_url": providers.normalize_base_url(body.base_url),
           "supports_tools": body.supports_tools, "use_proxy": body.use_proxy}
    if body.num_ctx is not None:
        cfg["num_ctx"] = body.num_ctx
    creds.set_endpoint(provider_id, cfg)
    if body.api_key is not None:
        creds.set_key(provider_id, body.api_key)   # "" clears
    p.refresh_models()
    return _provider_view(p)


@router.delete("/providers/{provider_id}/endpoint")
def clear_provider_endpoint(provider_id: str) -> dict:
    p = _self_hosted(provider_id)
    creds.clear_endpoint(provider_id)
    creds.delete_key(provider_id)
    p.refresh_models()
    return _provider_view(p)


@router.post("/providers/{provider_id}/test")
def test_provider_endpoint(provider_id: str) -> dict:
    """Live connection check: re-discover the model list now."""
    p = _self_hosted(provider_id)
    if not p.is_configured():
        raise HTTPException(status_code=400, detail="Set a base URL first.")
    models = p.refresh_models()
    return {"ok": bool(models), "models": models,
            "detail": (f"Connected — {len(models)} model(s) available." if models
                       else f"No models found at {p.base_url()} — is the server "
                            "running and reachable, and does it have a model loaded?")}


@router.get("/providers")
def list_providers() -> dict:
    return {"providers": [_provider_view(p) for p in providers.list_providers()]}


@router.put("/providers/{provider_id}")
def set_provider_key(provider_id: str, body: SetKeyBody) -> dict:
    try:
        p = providers.get_provider(provider_id)
    except providers.AIError:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")
    if provider_id == "copilot":
        raise HTTPException(status_code=400,
                            detail="Copilot uses the OAuth device flow, not an API key.")
    creds.set_key(provider_id, body.api_key)
    return _provider_view(p)


@router.delete("/providers/{provider_id}")
def clear_provider_key(provider_id: str) -> dict:
    try:
        p = providers.get_provider(provider_id)
    except providers.AIError:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")
    # Copilot has no stored API key — "clear" means sign out of the OAuth session.
    if provider_id == "copilot" and hasattr(p, "sign_out"):
        p.sign_out()
    else:
        creds.delete_key(provider_id)
    return _provider_view(p)


# ---------------------------------------------------------------------------
# GitHub Copilot OAuth device flow.
#
# Copilot can't take a pasted API key — the user authorises a device code at
# <host>/login/device, on github.com or a GitHub Enterprise host. start() kicks
# off the flow and a background poller waits for the user to approve; the
# frontend polls status() until it flips to
# "done" (token stored) or "error". Single user → a single module-level slot.
# ---------------------------------------------------------------------------
_copilot_lock = threading.Lock()
_copilot_signin: Optional[dict] = None


@router.post("/providers/copilot/login")
def copilot_login_start() -> dict:
    global _copilot_signin
    try:
        p = providers.get_provider("copilot")
    except providers.AIError:
        raise HTTPException(status_code=404, detail="Copilot provider unavailable.")
    try:
        data = p.start_device_flow()
    except Exception as e:  # noqa: BLE001 — surface network/auth failures
        raise HTTPException(status_code=502, detail=f"Could not start sign-in: {e}")

    device_code = data["device_code"]
    interval = int(data.get("interval", 5))
    expires_in = int(data.get("expires_in", 900))
    session = {
        "status": "pending",
        "user_code": data.get("user_code", ""),
        "verification_uri": data.get("verification_uri")
        or data.get("verification_uri_complete")
        or providers.github_urls(providers.github_host())["verification"],
        "error": None,
    }
    with _copilot_lock:
        _copilot_signin = session

    def worker():
        try:
            p.poll_for_token(
                device_code, interval=interval, expires_in=expires_in,
                stop_check=lambda: _copilot_signin is not session,
            )
            session["status"] = "done"
        except Exception as e:  # noqa: BLE001 — report to the status endpoint
            session["status"] = "error"
            session["error"] = str(e)

    threading.Thread(target=worker, name="copilot-login", daemon=True).start()
    return {
        "user_code": session["user_code"],
        "verification_uri": session["verification_uri"],
        "interval": interval,
        "expires_in": expires_in,
    }


class CopilotHostBody(BaseModel):
    host: str    # "" or github.com, or a GitHub Enterprise host such as acme.ghe.com


@router.put("/providers/copilot/host")
def set_copilot_host(body: CopilotHostBody) -> dict:
    """Where Copilot signs in. Changing it signs out, and stops a pending sign-in."""
    global _copilot_signin
    p = providers.get_provider("copilot")
    try:
        p.set_host(body.host)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    with _copilot_lock:
        _copilot_signin = None
    return _provider_view(p)


@router.get("/providers/copilot/login")
def copilot_login_status() -> dict:
    with _copilot_lock:
        s = _copilot_signin
    if s is None:
        return {"status": "idle"}
    return {
        "status": s["status"],
        "user_code": s["user_code"],
        "verification_uri": s["verification_uri"],
        "error": s["error"],
    }


@router.delete("/providers/copilot/login")
def copilot_login_cancel() -> dict:
    """Abandon a pending sign-in (the poller's stop_check trips on the next tick)."""
    global _copilot_signin
    with _copilot_lock:
        _copilot_signin = None
    return {"status": "idle"}


# ---------------------------------------------------------------------------
# Prompt / rules editing (AI Generation + AI Chat). These live in project_meta
# (de-roleplayed defaults via Logic_AI_Context) so they travel with the .arch.
# ---------------------------------------------------------------------------
class PromptsBody(BaseModel):
    rules: Optional[str] = None
    prompt: Optional[str] = None
    chat_rules: Optional[str] = None


@router.get("/prompts")
def get_prompts(state: AppState = Depends(get_state)) -> dict:
    def go():
        from Application_Logic import Logic_AI_Context as ctx
        db = state.require_open()
        return {
            "rules": ctx.get_rules(db),
            "prompt": ctx.get_prompt(db),
            "chat_rules": ctx.get_chat_rules(db),
        }
    return _guard(go)


@router.put("/prompts")
def set_prompts(body: PromptsBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        from Application_Logic import Logic_AI_Context as ctx
        db = state.require_edit()
        if body.rules is not None:
            ctx.set_rules(db, body.rules)
        if body.prompt is not None:
            ctx.set_prompt(db, body.prompt)
        if body.chat_rules is not None:
            ctx.set_chat_rules(db, body.chat_rules)
        db.commit()
        return {
            "rules": ctx.get_rules(db),
            "prompt": ctx.get_prompt(db),
            "chat_rules": ctx.get_chat_rules(db),
        }
    return _guard(go)


# ---------------------------------------------------------------------------
# Mind-map status (AI Generation grounds + regenerates the model/release map).
# ---------------------------------------------------------------------------
@router.get("/mindmap")
def mindmap_status(model_id: Optional[int] = None, release_id: Optional[int] = None,
                   state: AppState = Depends(get_state)) -> dict:
    def go():
        db = state.require_open()
        mid = model_id
        if mid is None and state.arch_manager is not None:
            mid = state.arch_manager.active_model_id
        rid = release_id if release_id is not None else db.get_active_release_id()
        if mid is None:
            return {"model_id": None, "release_id": rid, "has_mindmap": False, "meta": None}
        meta = db.get_model_mindmap_meta(mid, release_id=rid)
        return {
            "model_id": mid,
            "release_id": rid,
            "has_mindmap": db.has_model_mindmap(mid, release_id=rid),
            "has_project_map": db.get_project_map(rid) is not None,
            "has_source": db.has_release_source(rid) if rid is not None else False,
            "meta": meta,
        }
    return _guard(go)


# ---------------------------------------------------------------------------
# HLT design-file parsing (AI Generation checklist)
# ---------------------------------------------------------------------------
class ParseHltBody(BaseModel):
    file_path: str


@router.post("/parse-hlt")
def parse_hlt(body: ParseHltBody) -> dict:
    """Parse one HLT ``*_Test_Case_Design.md`` into its title/model + test-case
    checklist so the frontend can show them for selection before generation."""
    import os
    from Application_Logic import Logic_AI_Context as ctx
    if not body.file_path or not os.path.isfile(body.file_path):
        raise HTTPException(status_code=404, detail=f"No such file: {body.file_path}")
    try:
        return ctx.parse_hlt_file(body.file_path)
    except Exception as e:  # noqa: BLE001 — surface parse failures as 400
        raise HTTPException(status_code=400, detail=f"Could not parse HLT file: {e}") from e


# ---------------------------------------------------------------------------
# Chat (SSE stream of the agent's progress and answer)
# ---------------------------------------------------------------------------
class ChatOptions(BaseModel):
    # Effort level ("none".."max"); None = the model's default.
    effort: Optional[str] = None
    # Ollama: the context window to run with (num_ctx). Other providers: a cap on
    # how much of the model's window the conversation may fill (history is
    # trimmed, oldest first, to stay under it).
    num_ctx: Optional[int] = None
    context_limit: Optional[int] = None
    max_output_tokens: Optional[int] = None


class ChatBody(BaseModel):
    provider_id: str
    model: str
    messages: list[dict]
    system_prompt: Optional[str] = None
    # Optional grounding: inject the model/release mind map as standing context.
    model_id: Optional[int] = None
    release_id: Optional[int] = None
    ground_in_mindmap: bool = True
    # "model": the active model's map; "project": the whole release's overview,
    # which is less to read for a small model that doesn't need one model's detail.
    map_scope: str = "model"
    agent_id: Optional[str] = None
    # Let the model read the code (read-only tools). Off = mind map only.
    use_tools: bool = True
    # The files open in the chat's editor: rules with applyTo match them.
    open_files: list[str] = []
    options: ChatOptions = ChatOptions()
    # Target Tests: give the agent the live-ECU tools (and the selected test).
    live_target: bool = False
    allow_target_writes: bool = False
    allow_target_flash: bool = False
    target_test_id: Optional[int] = None


_FILE_TOOLS = {"read_file", "list_files", "search_code"}
# Room kept free for the answer when fitting the history into the window.
_OUTPUT_RESERVE = 4096


def _chat_scope(state: AppState, body: ChatBody):
    """(db or None, model id, release id) the chat is about."""
    db = state.db
    if db is None or not db.is_open:
        return None, None, None
    mid = body.model_id
    if mid is None and state.arch_manager is not None:
        mid = state.arch_manager.active_model_id
    rid = body.release_id if body.release_id is not None else db.get_active_release_id()
    return db, mid, rid


def _chat_parts(state: AppState, body: ChatBody) -> dict:
    """The pieces of the request, each kept apart so the context meter can
    show what fills the window: rules (project + agent + rule files), mind
    map, tools and the executor that runs them."""
    from Application_Logic import Logic_AI_Agents as agents
    from Application_Logic import Logic_AI_Context as ctx
    from Application_Logic import Logic_AI_Tools as ai_tools
    from Application_Logic.Logic_Source_Store import release_source_provider

    db, mid, rid = _chat_scope(state, body)
    agent = agents.find_agent(db, body.agent_id)
    base_rules = body.system_prompt or (ctx.get_chat_rules(db) if db is not None
                                        else ctx.DEFAULT_CHAT_RULES)
    rules = agents.project_rules(db) if db is not None else []
    mind_map = ""
    if db is not None and body.ground_in_mindmap:
        if body.map_scope == "project":
            mm = db.get_project_map(rid)
        else:
            mm = db.get_model_mindmap(mid, release_id=rid) if mid is not None else None
        if mm:
            mind_map = "# CODE MIND MAP\n" + ctx.mind_map_to_text(mm)
    system = agents.compose_system_prompt(base_rules, agent, rules, body.open_files)
    tools, executor, has_source = [], None, False
    if body.use_tools:
        source = release_source_provider(db, rid) if db is not None else None
        has_source = source is not None
        tools = agents.tools_for_agent(agent, ai_tools.default_tools())
        if source is None:
            tools = [t for t in tools if t.name not in _FILE_TOOLS]
        if db is None:
            tools = []
        if tools:
            diff_hash = db.get_meta("ai_last_diff_hash") or ""
            executor = ai_tools.ToolExecutor(None, db=db, model_id=mid if mid is not None else -1,
                                             diff_hash=diff_hash, provider=source,
                                             release_id=rid)
    if body.live_target and db is not None:
        system, tools, executor = _with_target_tools(state, body, db, mid, rid, system,
                                                     tools, executor)
    return {"agent": agent, "system": system, "mind_map": mind_map, "tools": tools,
            "executor": executor, "has_source": has_source,
            "has_mind_map": bool(mind_map), "project_open": db is not None}


def _with_target_tools(state: AppState, body: ChatBody, db, mid, rid, system, tools,
                       executor):
    """Add the live-ECU tools, their rules and the selected test to a chat."""
    from Application_Logic import Logic_AI_Target_Tools as target_tools
    from Application_Logic.Logic_File_Locking import FileLockManager
    from .. import target_session as ts

    from Application_Logic import Logic_Target_Tests as tt

    models = {m["id"]: m["name"] for m in db.get_all_models() if not m["is_deleted"]}
    selected = db.get_target_test(body.target_test_id) if body.target_test_id else None
    if selected:
        selected["suite"] = models.get(selected["model_id"], "")
    system = system + "\n\n" + target_tools.system_prompt(
        selected, body.allow_target_writes, suites=tt.suite_names(db),
        current=models.get(mid, ""), allow_flash=body.allow_target_flash)
    names = {t.name for t in tools}
    tools = list(tools) + [t for t in target_tools.target_tools() if t.name not in names]
    executor = target_tools.TargetToolExecutor(
        executor, get_backend=lambda: ts.AI_SESSION.get(ts.get_config(state)), db=db,
        user=FileLockManager.get_username(), allow_writes=body.allow_target_writes,
        allow_flash=body.allow_target_flash,
        can_edit=bool(state.can_edit),
        release_id=rid, model_id=mid,
        on_change=lambda tid: state.bus.publish(
            "db-changed", {"reason": "target-tests-changed", "test_id": tid}),
        audit=ts.AI_SESSION.log)
    return system, tools, executor


def _context_window(body: ChatBody) -> int:
    try:
        info = providers.get_provider(body.provider_id).model_info(body.model)
    except Exception:  # noqa: BLE001 — an unknown model still gets a meter
        info = {"context_tokens": providers.context_window_for(body.model)}
    window = int(info.get("context_tokens") or 0) or providers.context_window_for(body.model)
    if body.provider_id == "ollama" and body.options.num_ctx:
        window = int(body.options.num_ctx)
    if body.options.context_limit:
        window = min(window, int(body.options.context_limit))
    return window


def _breakdown(parts: dict, messages: list[dict], window: int) -> dict:
    est = providers.estimate_tokens
    tools_tok = est([{"name": t.name, "description": t.description,
                      "parameters": t.input_schema} for t in parts["tools"]])
    history = _history_tokens(messages)
    system = est(parts["system"])
    mind = est(parts["mind_map"])
    total = system + mind + tools_tok + history
    return {"system": system, "mind_map": mind, "tools": tools_tok, "history": history,
            "total": total, "window": window,
            "has_source": parts["has_source"], "has_mind_map": parts["has_mind_map"],
            "project_open": parts["project_open"], "agent": parts["agent"].get("name"),
            "tool_names": [t.name for t in parts["tools"]]}


def _history_tokens(messages: list[dict]) -> int:
    return sum(providers.estimate_tokens(m.get("content", "")) for m in messages)


def _fit_history(messages: list[dict], fixed_tokens: int, window: int) -> tuple[list[dict], int]:
    """Drop the oldest turns until the request fits the window (keeping the
    last user message). Returns (messages, how many were dropped)."""
    msgs = list(messages)
    budget = window - _OUTPUT_RESERVE - fixed_tokens
    dropped = 0
    while len(msgs) > 1 and _history_tokens(msgs) > budget:
        msgs.pop(0)
        dropped += 1
        # Never start on an assistant turn.
        while len(msgs) > 1 and msgs[0].get("role") != "user":
            msgs.pop(0)
            dropped += 1
    return msgs, dropped


@router.post("/context")
def chat_context(body: ChatBody, state: AppState = Depends(get_state)) -> dict:
    """What a chat request with this body would put in the model's context
    (estimated tokens per part), before sending it."""
    parts = _chat_parts(state, body)
    return _breakdown(parts, body.messages, _context_window(body))


@router.get("/model-info")
def model_info(provider_id: str, model: str) -> dict:
    """Context window, output cap, effort levels and tool support of one model."""
    try:
        p = providers.get_provider(provider_id)
    except providers.AIError:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")
    return p.model_info(model)


# Last quota seen per provider — the credits a turn used are the change.
_quota_cache: dict = {}
_quota_lock = threading.Lock()


def _fetch_quota(provider_id: str) -> Optional[dict]:
    if provider_id != "copilot":
        return None
    p = providers.get_provider("copilot")
    if not p.is_configured():
        return None
    q = p.quota()
    with _quota_lock:
        _quota_cache[provider_id] = q
    return q


def _quota_delta(before: Optional[dict], after: Optional[dict]) -> dict:
    out = {}
    if not before or not after:
        return out
    for qid, a in (after.get("quotas") or {}).items():
        b = (before.get("quotas") or {}).get(qid)
        if b is None:
            continue
        d = round(float(a.get("used", 0)) - float(b.get("used", 0)), 4)
        if not d:
            d = round(float(b.get("remaining", 0)) - float(a.get("remaining", 0)), 4)
        if d > 0:
            out[qid] = d
    return out


@router.get("/usage/{provider_id}")
def provider_usage(provider_id: str, refresh: bool = False) -> dict:
    """The account's credits: Copilot's plan quotas. Providers without a
    quota API answer {"kind": "tokens"} (the chat counts tokens instead);
    self-hosted ones {"kind": "local"}."""
    try:
        p = providers.get_provider(provider_id)
    except providers.AIError:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")
    if isinstance(p, providers.SelfHostedProvider):
        return {"kind": "local"}
    if provider_id != "copilot":
        return {"kind": "tokens"}
    if not p.is_configured():
        return {"kind": "quota", "signed_in": False}
    with _quota_lock:
        cached = _quota_cache.get(provider_id)
    try:
        q = cached if (cached and not refresh) else _fetch_quota(provider_id)
    except providers.AIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"kind": "quota", "signed_in": True, **(q or {})}


@router.post("/chat")
async def chat(body: ChatBody, request: Request,
               state: AppState = Depends(get_state)) -> Response:
    """One agent turn, streamed as SSE events:
    ``context`` (what the request holds), ``tool`` / ``tool_result`` (each
    code lookup), ``usage`` (tokens per model request), ``token`` (answer
    text), ``credits`` (Copilot quota used by the turn), then ``done`` or
    ``error``."""
    try:
        provider = providers.get_provider(body.provider_id)
    except providers.AIError:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {body.provider_id}")
    if not provider.is_configured():
        raise HTTPException(status_code=409,
                            detail=f"Provider '{body.provider_id}' is not configured.")

    parts = _chat_parts(state, body)
    window = _context_window(body)
    first = _breakdown(parts, body.messages, window)
    fixed = first["system"] + first["mind_map"] + first["tools"]
    messages, dropped = _fit_history(body.messages, fixed, window)
    system_prompt = parts["system"] + ("\n\n" + parts["mind_map"] if parts["mind_map"] else "")
    options = body.options.model_dump(exclude_none=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    _DONE = object()

    def push(kind, payload):
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def worker():
        before = None
        try:
            if body.provider_id == "copilot":
                with _quota_lock:
                    before = _quota_cache.get("copilot")
                if before is None:
                    try:
                        before = _fetch_quota("copilot")
                    except providers.AIError:
                        before = None
            push("context", {**_breakdown(parts, messages, window), "dropped": dropped})
            executor = parts["executor"]
            text = providers.generate_with_tools(
                body.provider_id, body.model, messages, parts["tools"],
                tool_executor=(executor.execute if executor else (lambda n, a: "(no tools)")),
                system_prompt=system_prompt, options=options,
                on_tool_call=lambda name, args: push("tool", {"name": name, "args": args}),
                on_tool_result=lambda name, args, out: push("tool_result", {
                    "name": name, "args": args, "chars": len(out),
                    "preview": out[:400], "error": out.startswith("ERROR:")}),
                on_usage=lambda u: push("usage", u),
                stop_check=stop.is_set)
            if not text:
                raise providers.AIGenerationError(
                    f"{provider.label} returned no answer — try again, or another model.")
            push("token", text)
            if body.provider_id == "copilot":
                try:
                    after = _fetch_quota("copilot")
                    push("credits", {"delta": _quota_delta(before, after), "quota": after})
                except providers.AIError:
                    pass
            push("done", text)
        except providers.AIStopped:
            push("error", "Stopped.")
        except Exception as e:  # noqa: BLE001 — surface to the stream as an error event
            push("error", str(e))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    threading.Thread(target=worker, name="ai-chat", daemon=True).start()

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    stop.set()
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if item is _DONE:
                    break
                kind, payload = item
                yield {"event": kind, "data": json.dumps(payload)}
        finally:
            stop.set()

    return EventSourceResponse(generator())


# ---------------------------------------------------------------------------
# Chat history (per user, per project; encrypted in app data)
# ---------------------------------------------------------------------------
def _history_key(state: AppState) -> str:
    from Application_Logic import Logic_AI_Chat_History as history
    path = None
    if state.db is not None and state.db.is_open:
        # The shared location, so a synced copy keeps the same history.
        path = state.shared_path
    return history.project_key(path)


class ChatSessionBody(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    provider_id: str = ""
    model: str = ""
    agent_id: str = ""
    settings: dict = {}
    usage: dict = {}
    messages: list[dict] = []


class RenameBody(BaseModel):
    title: str


@router.get("/chats")
def list_chats(state: AppState = Depends(get_state)) -> dict:
    from Application_Logic import Logic_AI_Chat_History as history
    return {"sessions": history.list_sessions(_history_key(state))}


@router.get("/chats/{session_id}")
def get_chat(session_id: str, state: AppState = Depends(get_state)) -> dict:
    from Application_Logic import Logic_AI_Chat_History as history
    s = history.get_session(_history_key(state), session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="No such chat.")
    return s


@router.put("/chats/{session_id}")
def save_chat(session_id: str, body: ChatSessionBody,
              state: AppState = Depends(get_state)) -> dict:
    from Application_Logic import Logic_AI_Chat_History as history
    data = body.model_dump()
    data["id"] = session_id
    return history.save_session(_history_key(state), data)


@router.patch("/chats/{session_id}")
def rename_chat(session_id: str, body: RenameBody,
                state: AppState = Depends(get_state)) -> dict:
    from Application_Logic import Logic_AI_Chat_History as history
    s = history.rename_session(_history_key(state), session_id, body.title)
    if s is None:
        raise HTTPException(status_code=404, detail="No such chat.")
    return s


@router.delete("/chats/{session_id}")
def delete_chat(session_id: str, state: AppState = Depends(get_state)) -> dict:
    from Application_Logic import Logic_AI_Chat_History as history
    return {"deleted": history.delete_session(_history_key(state), session_id)}


# ---------------------------------------------------------------------------
# Agents and rules (VS Code's .agent.md / .instructions.md, kept in the project)
# ---------------------------------------------------------------------------
class AgentsBody(BaseModel):
    agents: list[dict]


class RulesBody(BaseModel):
    rules: list[dict]


class FolderBody(BaseModel):
    path: str


def _agents_view(db) -> dict:
    from Application_Logic import Logic_AI_Agents as agents
    return {"agents": agents.all_agents(db), "rules": agents.project_rules(db),
            "default_agent_id": agents.DEFAULT_AGENT_ID,
            "tools": list(agents.ALL_TOOLS)}


@router.get("/agents")
def list_agents(state: AppState = Depends(get_state)) -> dict:
    db = state.db if (state.db is not None and state.db.is_open) else None
    return _agents_view(db)


@router.put("/agents")
def save_agents(body: AgentsBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        from Application_Logic import Logic_AI_Agents as agents
        db = state.require_edit()
        agents.save_project_agents(db, body.agents)
        db.commit()
        return _agents_view(db)
    return _guard(go)


@router.put("/rules")
def save_rules(body: RulesBody, state: AppState = Depends(get_state)) -> dict:
    def go():
        from Application_Logic import Logic_AI_Agents as agents
        db = state.require_edit()
        agents.save_project_rules(db, body.rules)
        db.commit()
        return _agents_view(db)
    return _guard(go)


@router.post("/agents/import")
def import_agents(body: FolderBody) -> dict:
    """Agents and rules found in a checkout's .github folder (not saved)."""
    from Application_Logic import Logic_AI_Agents as agents
    try:
        return agents.import_from_folder(body.path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/agents/export")
def export_agents(body: FolderBody, state: AppState = Depends(get_state)) -> dict:
    """Write the project's agents and rules into <folder>/.github the way VS
    Code reads them. Existing files of the same name are replaced."""
    import os
    from Application_Logic import Logic_AI_Agents as agents

    def go():
        db = state.require_open()
        if not body.path or not os.path.isdir(body.path):
            raise HTTPException(status_code=404, detail=f"No such folder: {body.path}")
        root = body.path
        if os.path.basename(os.path.normpath(root)) != ".github":
            root = os.path.join(root, ".github")
        written = []
        for a in agents.project_agents(db):
            path = os.path.join(root, "agents", agents._slug(a["name"]) + ".agent.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(agents.agent_to_md(a))
            written.append(path)
        for r in agents.project_rules(db):
            name = agents._slug(r["name"])
            if name == "copilot-instructions":
                path = os.path.join(root, "copilot-instructions.md")
            else:
                path = os.path.join(root, "instructions", name + ".instructions.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(agents.instructions_to_md(r))
            written.append(path)
        return {"written": written}
    return _guard(go)
