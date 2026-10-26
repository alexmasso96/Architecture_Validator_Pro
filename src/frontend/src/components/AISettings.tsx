import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AIProvider, AIModel, ProvidersResponse } from "../api/types";

// Providers that authenticate with a pasted API key (vs. Copilot's device flow).
const KEY_PROVIDERS = new Set(["anthropic", "openai", "gemini"]);

// In-flight GitHub Copilot device-flow sign-in.
interface CopilotFlow {
  user_code: string;
  verification_uri: string;
}
interface CopilotLoginStatus {
  status: "idle" | "pending" | "done" | "error";
  user_code?: string;
  verification_uri?: string;
  error?: string | null;
}

// AI provider linkage — manage the credential for each provider and see which
// models it exposes. Lives in Preferences → AI Settings. Backs onto the
// machine-global credential store via /api/ai/providers (PUT/DELETE key).
export function AISettings() {
  const [providers, setProviders] = useState<AIProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [copilotFlow, setCopilotFlow] = useState<CopilotFlow | null>(null);
  const pollTimer = useRef<number | null>(null);

  // Stop polling the device-flow status on unmount.
  useEffect(() => {
    return () => {
      if (pollTimer.current) window.clearInterval(pollTimer.current);
    };
  }, []);

  async function startCopilotLogin() {
    setBusy("copilot");
    setNote(null);
    try {
      const r = await api.post<CopilotFlow & { interval: number }>(
        "/ai/providers/copilot/login",
      );
      setCopilotFlow({ user_code: r.user_code, verification_uri: r.verification_uri });
      window.open(r.verification_uri, "_blank", "noopener");
      // Poll until the user approves (or it errors out).
      if (pollTimer.current) window.clearInterval(pollTimer.current);
      pollTimer.current = window.setInterval(async () => {
        try {
          const s = await api.get<CopilotLoginStatus>("/ai/providers/copilot/login");
          if (s.status === "done") {
            stopCopilotPoll();
            setCopilotFlow(null);
            setNote("Signed in to GitHub Copilot.");
            await load();
          } else if (s.status === "error") {
            stopCopilotPoll();
            setCopilotFlow(null);
            setNote(`Sign-in failed: ${s.error ?? "unknown error"}`);
          }
        } catch {
          /* transient — keep polling */
        }
      }, 2500);
    } catch (e) {
      setNote(`Sign-in failed: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  }

  function stopCopilotPoll() {
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    pollTimer.current = null;
  }

  async function cancelCopilotLogin() {
    stopCopilotPoll();
    setCopilotFlow(null);
    try {
      await api.del("/ai/providers/copilot/login");
    } catch {
      /* best effort */
    }
  }

  async function signOutCopilot() {
    setBusy("copilot");
    setNote(null);
    try {
      await api.del("/ai/providers/copilot");
      setNote("Signed out of GitHub Copilot.");
      await load();
    } catch (e) {
      setNote(`Sign-out failed: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  }

  // silent: refresh after a mutation without swapping the screen for the
  // loading spinner (that unmounts the cards and drops their local state,
  // e.g. the self-hosted connection-test result).
  async function load(silent = false) {
    if (!silent) setLoading(true);
    try {
      const r = await api.get<ProvidersResponse>("/ai/providers");
      setProviders(r.providers);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function saveKey(id: string) {
    const key = (drafts[id] ?? "").trim();
    if (!key) return;
    setBusy(id);
    setNote(null);
    try {
      await api.put(`/ai/providers/${id}`, { api_key: key });
      setDrafts((d) => ({ ...d, [id]: "" }));
      setNote(`Saved key for ${id}.`);
      await load();
    } catch (e) {
      setNote(`Save failed: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  }

  async function clearKey(id: string) {
    setBusy(id);
    setNote(null);
    try {
      await api.del(`/ai/providers/${id}`);
      setNote(`Cleared key for ${id}.`);
      await load();
    } catch (e) {
      setNote(`Clear failed: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  }

  if (loading) {
    return (
      <div className="prefs-body">
        <div className="center-msg">
          <span className="spin" /> Loading providers…
        </div>
      </div>
    );
  }
  if (error) {
    return (
      <div className="prefs-body">
        <div className="prefs-placeholder" style={{ color: "var(--red)" }}>
          {error}
        </div>
      </div>
    );
  }

  return (
    <div className="prefs-body">
      <div className="prefs-field">
        <div className="prefs-label">AI Providers</div>
        <p className="prefs-hint">
          Link an AI account by storing its API key, or point the app at a
          self-hosted Ollama / vLLM server so source code never leaves your
          infrastructure. Credentials and endpoints are kept in the
          machine-global encrypted store, independent of the open project.
        </p>
      </div>

      <div className="ai-prov-list">
        {providers.map((p) => {
          const keyed = KEY_PROVIDERS.has(p.id);
          return (
            <div key={p.id} className="ai-prov-card">
              <div className="ai-prov-head">
                <span className="ai-prov-name">{p.label}</span>
                <span
                  className={
                    "ai-prov-badge " + (p.configured ? "ok" : "off")
                  }
                >
                  {p.configured ? "● Linked" : "○ Not linked"}
                </span>
                <span className="ai-prov-models">{p.models.length} models</span>
              </div>

              {p.kind === "self_hosted" && p.endpoint ? (
                <SelfHostedConfig
                  provider={p}
                  onChanged={() => load(true)}
                  onNote={setNote}
                />
              ) : keyed ? (
                <div className="ai-prov-keyrow">
                  <input
                    type="password"
                    className="ai-prov-key"
                    placeholder={p.configured ? "Replace API key…" : "Paste API key…"}
                    autoComplete="off"
                    spellCheck={false}
                    value={drafts[p.id] ?? ""}
                    onChange={(e) =>
                      setDrafts((d) => ({ ...d, [p.id]: e.target.value }))
                    }
                    onKeyDown={(e) => e.key === "Enter" && saveKey(p.id)}
                  />
                  <button
                    className="save-btn"
                    disabled={busy === p.id || !(drafts[p.id] ?? "").trim()}
                    onClick={() => saveKey(p.id)}
                  >
                    {busy === p.id ? "…" : "Save"}
                  </button>
                  {p.configured && (
                    <button
                      className="scope-btn"
                      disabled={busy === p.id}
                      onClick={() => clearKey(p.id)}
                    >
                      Clear
                    </button>
                  )}
                </div>
              ) : p.id === "copilot" ? (
                <div className="ai-copilot">
                  {copilotFlow ? (
                    <div className="ai-copilot-flow">
                      <p className="ai-prov-note">
                        Open{" "}
                        <a
                          href={copilotFlow.verification_uri}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {copilotFlow.verification_uri}
                        </a>{" "}
                        and enter this code to authorise:
                      </p>
                      <div className="ai-copilot-code">{copilotFlow.user_code}</div>
                      <div className="ai-copilot-actions">
                        <span className="ai-copilot-wait">
                          <span className="spin" /> Waiting for approval…
                        </span>
                        <button className="scope-btn" onClick={cancelCopilotLogin}>
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : p.configured ? (
                    <div className="ai-copilot-actions">
                      <span className="ai-prov-note">Signed in with GitHub.</span>
                      <button
                        className="scope-btn"
                        disabled={busy === "copilot"}
                        onClick={signOutCopilot}
                      >
                        Sign out
                      </button>
                    </div>
                  ) : (
                    <div className="ai-copilot-actions">
                      <span className="ai-prov-note">
                        Authorise with your GitHub account — no API key needed.
                      </span>
                      <button
                        className="save-btn ai-copilot-signin"
                        disabled={busy === "copilot"}
                        onClick={startCopilotLogin}
                      >
                        {busy === "copilot" ? "…" : "Sign in with GitHub"}
                      </button>
                    </div>
                  )}
                </div>
              ) : (
                <div className="ai-prov-note">
                  This provider is configured outside the key store.
                </div>
              )}
            </div>
          );
        })}
      </div>

      {note && <div className="ai-prov-toast">{note}</div>}
    </div>
  );
}

// Ollama / vLLM: a base URL instead of an account. Optional API key (vLLM
// --api-key), opt-in native tool calling, proxy routing, and (Ollama) the
// context size — Ollama truncates prompts to a small default otherwise.
function SelfHostedConfig({
  provider,
  onChanged,
  onNote,
}: {
  provider: AIProvider;
  onChanged: () => Promise<void> | void;
  onNote: (msg: string | null) => void;
}) {
  const ep = provider.endpoint!;
  const isOllama = provider.id === "ollama";
  const [baseUrl, setBaseUrl] = useState(ep.base_url || ep.default_base_url);
  const [apiKey, setApiKey] = useState("");
  const [tools, setTools] = useState(ep.supports_tools);
  const [useProxy, setUseProxy] = useState(ep.use_proxy);
  const [numCtx, setNumCtx] = useState<string>(String(ep.num_ctx ?? 16384));
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; detail: string } | null>(null);

  async function save() {
    setBusy(true);
    setTest(null);
    onNote(null);
    try {
      await api.put(`/ai/providers/${provider.id}/endpoint`, {
        base_url: baseUrl.trim(),
        // Only send a key when one was typed — empty input keeps the stored key.
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        supports_tools: tools,
        use_proxy: useProxy,
        ...(isOllama ? { num_ctx: Number(numCtx) || 16384 } : {}),
      });
      setApiKey("");
      onNote(`Saved ${provider.label} endpoint.`);
      await onChanged();
      await runTest();
    } catch (e) {
      onNote(`Save failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function runTest() {
    try {
      const r = await api.post<{ ok: boolean; detail: string; models: AIModel[] }>(
        `/ai/providers/${provider.id}/test`,
      );
      setTest({ ok: r.ok, detail: r.detail });
    } catch (e) {
      setTest({ ok: false, detail: (e as Error).message });
    }
  }

  async function clear() {
    setBusy(true);
    setTest(null);
    try {
      await api.del(`/ai/providers/${provider.id}/endpoint`);
      onNote(`Removed ${provider.label} endpoint.`);
      await onChanged();
    } catch (e) {
      onNote(`Clear failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ai-selfhost">
      <div className="ai-prov-keyrow">
        <input
          className="ai-prov-key"
          placeholder={ep.default_base_url}
          spellCheck={false}
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          title="Base URL of the server (with or without /v1)"
        />
        {!isOllama && (
          <input
            type="password"
            className="ai-prov-key"
            placeholder={ep.has_api_key ? "API key set — type to replace" : "API key (optional)"}
            autoComplete="off"
            spellCheck={false}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            title="Only needed if the vLLM server was started with --api-key"
          />
        )}
      </div>
      <div className="ai-selfhost-opts">
        <label title="Turn on only if the model and server support function calling (vLLM: --enable-auto-tool-choice --tool-call-parser …). Off uses a text-based fallback that works with any model.">
          <input type="checkbox" checked={tools} onChange={(e) => setTools(e.target.checked)} />
          Native tool calling
        </label>
        <label title="Off: connect directly (typical for a server on this machine or the company LAN). On: route through the configured network proxy.">
          <input type="checkbox" checked={useProxy} onChange={(e) => setUseProxy(e.target.checked)} />
          Use network proxy
        </label>
        {isOllama && (
          <label title="Context window Ollama allocates per request (num_ctx). Larger fits more source/mind-map context but needs more GPU/RAM.">
            Context
            <input
              type="number"
              className="ai-prov-key ai-selfhost-ctx"
              min={2048}
              step={2048}
              value={numCtx}
              onChange={(e) => setNumCtx(e.target.value)}
            />
            tokens
          </label>
        )}
      </div>
      <div className="ai-copilot-actions">
        <button className="save-btn" disabled={busy || !baseUrl.trim()} onClick={save}>
          {busy ? "…" : "Save & test"}
        </button>
        {provider.configured && (
          <>
            <button className="scope-btn" disabled={busy} onClick={runTest}>
              Test connection
            </button>
            <button className="scope-btn" disabled={busy} onClick={clear}>
              Remove
            </button>
          </>
        )}
        {test && (
          <span className={"ai-selfhost-test " + (test.ok ? "ok" : "err")}>
            {test.ok ? "✓ " : "⚠ "}
            {test.detail}
          </span>
        )}
      </div>
    </div>
  );
}
