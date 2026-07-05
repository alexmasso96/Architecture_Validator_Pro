import { useEffect, useState } from "react";
import { api } from "../api/client";

interface ProxyStatus {
  source: "environment" | "manual" | "system" | "direct";
  proxy_url: string | null;
  manual_configured: boolean;
}

const SOURCE_LABEL: Record<ProxyStatus["source"], string> = {
  environment: "From environment variables",
  manual: "Manual (configured below)",
  system: "System proxy (Windows settings)",
  direct: "Direct connection (no proxy)",
};

// Outbound proxy for the worker's internet traffic (AI providers + update
// check). The embedded webview is deliberately proxy-free — it only renders
// the local UI — so corporate-network access is configured here instead.
// Lives in Preferences → Network; backs onto /api/net/proxy.
export function NetworkSettings() {
  const [status, setStatus] = useState<ProxyStatus | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setStatus(await api.get<ProxyStatus>("/net/proxy"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function save() {
    setBusy(true);
    setNote(null);
    try {
      setStatus(await api.put<ProxyStatus>("/net/proxy", { url: draft }));
      setDraft("");
      setNote("Proxy saved. It applies immediately — no restart needed.");
    } catch (e) {
      setNote(`Could not save proxy: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    setBusy(true);
    setNote(null);
    try {
      setStatus(await api.del<ProxyStatus>("/net/proxy"));
      setNote("Manual proxy removed; back to system configuration.");
    } catch (e) {
      setNote(`Could not remove proxy: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="prefs-body">
      <div className="prefs-field">
        <div className="prefs-label">Outbound connection</div>
        <div className="prefs-hint">
          AI providers and the update check connect from the app's background
          worker. On corporate networks that require a proxy, configure it
          here — the in-app browser itself never uses a proxy (it only renders
          the local UI).
        </div>
        {status && (
          <div className="update-row">
            <span className="update-badge ok">{SOURCE_LABEL[status.source]}</span>
            {status.proxy_url && (
              <span className="update-detail">{status.proxy_url}</span>
            )}
          </div>
        )}
        {error && <div className="update-detail">⚠ {error}</div>}
      </div>

      <div className="prefs-field">
        <div className="prefs-label">Manual proxy URL</div>
        <div className="prefs-hint">
          Format: <code>http://proxy.company.com:8080</code> — include
          credentials as <code>http://user:password@proxy.company.com:8080</code>{" "}
          if your proxy asks for a login (stored encrypted, per user, never in
          the project). Kerberos/NTLM-only proxies are not supported yet.
        </div>
        <div className="prefs-row">
          <input
            className="prefs-input"
            type="text"
            placeholder="http://user:password@proxy.company.com:8080"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={busy}
          />
          <button
            className="save-btn"
            onClick={save}
            disabled={busy || !draft.trim()}
          >
            Save
          </button>
          {status?.manual_configured && (
            <button className="prefs-prefill" onClick={clear} disabled={busy}>
              Remove
            </button>
          )}
        </div>
        {note && <div className="update-detail">{note}</div>}
      </div>
    </div>
  );
}
