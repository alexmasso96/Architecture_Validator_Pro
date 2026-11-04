import Editor, { type OnMount } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api, getToken } from "../api/client";
import { useSSE } from "../api/useSSE";
import type {
  AgentsResponse,
  AIModelInfo,
  AIProvider,
  ChatAgent,
  ChatContextBreakdown,
  ChatSession,
  ChatSessionSummary,
  ChatSettings,
  ChatToolUse,
  ChatTurn,
  ChatUsage,
  ProviderUsage,
  ProvidersResponse,
  SourceFileInfo,
  SourceFunction,
} from "../api/types";
import { AgentsDialog } from "../components/AgentsDialog";
import { useConfirm } from "../components/ConfirmDialog";
import { FileIcon, FolderIcon } from "../components/Icons";
import { buildRefIndex, EMPTY_REFS, renderChatMarkdown, type RefIndex } from "../chatMarkdown";
import { buildFileTree, type TreeNode } from "../fileTree";
import { persistKey } from "../prefs";

// ===========================================================================
// AI Chat — laid out like VS Code: an explorer (files, functions, chat
// history) on the left, the code in the middle, the agent on the right.
// Answers link the functions and file:line references they mention; a click
// opens the code in the middle and highlights it.
// ===========================================================================

type MonacoEditor = Parameters<OnMount>[0];

// --- remembered settings ---------------------------------------------------
const PREFS_KEY = "arch.ai.chat.v2";

interface ChatPrefs {
  providerId: string;
  model: string;
  agentId: string;
  settings: ChatSettings;
  /** The last model used with each provider. */
  modelByProvider: Record<string, string>;
  explorerW: number;
  chatW: number;
}

const DEFAULT_SETTINGS: ChatSettings = {
  effort: null,
  num_ctx: null,
  context_limit: null,
  max_output_tokens: null,
  ground: true,
  map_scope: "model",
  use_tools: true,
};

function loadPrefs(): ChatPrefs {
  const base: ChatPrefs = {
    providerId: "",
    model: "",
    agentId: "",
    settings: { ...DEFAULT_SETTINGS },
    modelByProvider: {},
    explorerW: 250,
    chatW: 460,
  };
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return base;
    const p = JSON.parse(raw) as Partial<ChatPrefs>;
    return { ...base, ...p, settings: { ...DEFAULT_SETTINGS, ...(p.settings ?? {}) } };
  } catch {
    return base;
  }
}

function savePrefs(p: ChatPrefs) {
  const v = JSON.stringify(p);
  try {
    localStorage.setItem(PREFS_KEY, v);
  } catch {
    /* storage disabled */
  }
  persistKey(PREFS_KEY, v);
}

// --- helpers -----------------------------------------------------------------
const uid = () =>
  (crypto.randomUUID?.() ?? `${Date.now()}${Math.random()}`).replace(/-/g, "").slice(0, 16);

function fmtTokens(n: number | undefined | null): string {
  if (!n) return "0";
  if (n < 1000) return String(n);
  if (n < 100_000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function fmtCredits(n: number | undefined): string {
  if (!n) return "0";
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

const EFFORT_LABEL: Record<string, string> = {
  none: "No reasoning",
  low: "Low effort",
  medium: "Medium effort",
  high: "High effort",
  xhigh: "Extra-high effort",
  max: "Max effort",
};

const TOOL_VERB: Record<string, string> = {
  read_file: "Read",
  list_files: "Listed files",
  search_code: "Searched",
  get_function: "Looked up",
  get_call_graph: "Call graph of",
  get_mind_map: "Read the mind map",
  get_requirements: "Read the requirements",
  get_diff: "Read the diff",
};

function toolLabel(t: ChatToolUse): { verb: string; arg: string } {
  const a = t.args ?? {};
  const arg = String(a.path ?? a.query ?? a.name ?? a.pattern ?? a.file_path ?? "");
  return { verb: TOOL_VERB[t.name] ?? t.name, arg };
}

function addUsage(a: Partial<ChatUsage>, b: Partial<ChatUsage>): ChatUsage {
  const credits: Record<string, number> = { ...(a.credits ?? {}) };
  for (const [k, v] of Object.entries(b.credits ?? {})) credits[k] = (credits[k] ?? 0) + v;
  return {
    input_tokens: (a.input_tokens ?? 0) + (b.input_tokens ?? 0),
    output_tokens: (a.output_tokens ?? 0) + (b.output_tokens ?? 0),
    requests: (a.requests ?? 0) + (b.requests ?? 0),
    credits,
    last_input_tokens: b.last_input_tokens ?? a.last_input_tokens,
  };
}

/** The line where the function starting at `line` (1-based) ends: brace matching. */
function functionEnd(text: string, line: number): number {
  const lines = text.split("\n");
  let depth = 0;
  let opened = false;
  for (let i = Math.max(0, line - 1); i < lines.length && i < line + 3000; i++) {
    const l = lines[i].replace(/\/\/.*$/, "").replace(/"(?:[^"\\]|\\.)*"/g, "");
    for (const ch of l) {
      if (ch === "{") {
        depth++;
        opened = true;
      } else if (ch === "}") {
        depth--;
        if (opened && depth <= 0) return i + 1;
      }
    }
    if (!opened && i > line + 15) break; // a prototype, not a definition
  }
  return line;
}

function useMonacoTheme(): "vs" | "vs-dark" {
  const read = (): "vs" | "vs-dark" =>
    document.documentElement.getAttribute("data-theme") === "dark" ? "vs-dark" : "vs";
  const [theme, setTheme] = useState<"vs" | "vs-dark">(read);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(read()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return theme;
}

// --- SSE over POST -------------------------------------------------------------
type ChatEvent =
  | { kind: "context"; data: ChatContextBreakdown }
  | { kind: "tool"; data: { name: string; args: Record<string, unknown> } }
  | { kind: "tool_result"; data: ChatToolUse }
  | { kind: "usage"; data: { input_tokens?: number; output_tokens?: number } }
  | { kind: "token"; data: string }
  | { kind: "credits"; data: { delta: Record<string, number>; quota: ProviderUsage } }
  | { kind: "done"; data: string }
  | { kind: "error"; data: string };

async function streamChat(
  body: unknown,
  onEvent: (e: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/ai/chat", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON */
    }
    onEvent({ kind: "error", data: String(detail) });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split(/\r?\n\r?\n/);
    buf = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let payload: unknown = data;
      try {
        payload = JSON.parse(data);
      } catch {
        /* raw */
      }
      onEvent({ kind: event, data: payload } as ChatEvent);
    }
  }
}

// ===========================================================================
// Model picker — every configured provider's models, grouped.
// ===========================================================================
function ModelSelect({
  providers,
  providerId,
  model,
  onChange,
  className,
  title,
}: {
  providers: AIProvider[];
  providerId: string;
  model: string;
  onChange: (providerId: string, model: string) => void;
  className?: string;
  title?: string;
}) {
  const usable = providers.filter((p) => p.configured && p.models.length);
  const value = `${providerId}\u0001${model}`;
  const known = usable.some((p) => p.id === providerId && p.models.some((m) => m.id === model));
  return (
    <select
      className={className ?? "chat-pick"}
      value={value}
      title={title ?? "Model"}
      onChange={(e) => {
        const [pid, mid] = e.target.value.split("\u0001");
        onChange(pid, mid);
      }}
    >
      {!known && (
        <option value={value}>{model ? model : usable.length ? "Pick a model" : "No AI account set up"}</option>
      )}
      {usable.map((p) => (
        <optgroup key={p.id} label={p.label}>
          {p.models.map((m) => (
            <option key={m.id} value={`${p.id}\u0001${m.id}`}>
              {m.name || m.id}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

// ===========================================================================
// Context meter (ring) and credits
// ===========================================================================
function ContextMeter({
  estimate,
  lastInput,
}: {
  estimate: ChatContextBreakdown | null;
  lastInput?: number;
}) {
  if (!estimate) return null;
  const used = estimate.total;
  const pct = estimate.window ? Math.min(1, used / estimate.window) : 0;
  const r = 7;
  const c = 2 * Math.PI * r;
  const tone = pct > 0.9 ? "bad" : pct > 0.7 ? "warn" : "ok";
  const tip = [
    `Context: ~${fmtTokens(used)} of ${fmtTokens(estimate.window)} tokens (${Math.round(pct * 100)}%)`,
    `  Rules & agent: ${fmtTokens(estimate.system)}`,
    `  Mind map: ${fmtTokens(estimate.mind_map)}`,
    `  Tool definitions: ${fmtTokens(estimate.tools)}`,
    `  Conversation: ${fmtTokens(estimate.history)}`,
    lastInput ? `Last request (as the provider counted it, incl. tool results): ${fmtTokens(lastInput)}` : "",
    estimate.dropped ? `Oldest ${estimate.dropped} message(s) left out to fit.` : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span className={`chat-ctx ${tone}`} title={tip}>
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden>
        <circle cx="9" cy="9" r={r} className="chat-ctx-bg" />
        <circle
          cx="9"
          cy="9"
          r={r}
          className="chat-ctx-fg"
          strokeDasharray={`${c * pct} ${c}`}
          transform="rotate(-90 9 9)"
        />
      </svg>
      {fmtTokens(used)}/{fmtTokens(estimate.window)}
    </span>
  );
}

function CreditsBar({
  providerId,
  usage,
  session,
  onRefresh,
}: {
  providerId: string;
  usage: ProviderUsage | null;
  session: Partial<ChatUsage>;
  onRefresh: () => void;
}) {
  const tokens = `${fmtTokens(session.input_tokens)} in · ${fmtTokens(session.output_tokens)} out · ${session.requests ?? 0} request${session.requests === 1 ? "" : "s"}`;
  let account: React.ReactNode = null;
  if (usage?.kind === "quota") {
    if (!usage.signed_in) account = <span>Copilot: not signed in</span>;
    else {
      const chat = usage.quotas?.chat;
      const prem = usage.quotas?.premium_interactions;
      const parts: string[] = [];
      if (chat) parts.push(chat.unlimited ? "Chat: unlimited" : `Chat credits: ${fmtCredits(chat.remaining)}/${fmtCredits(chat.entitlement)} left`);
      if (prem && prem.has_quota) parts.push(prem.unlimited ? "Premium: unlimited" : `Premium: ${fmtCredits(prem.remaining)}/${fmtCredits(prem.entitlement)}`);
      account = (
        <span title={`Plan: ${usage.plan || "?"}${usage.sku ? ` (${usage.sku})` : ""}${usage.reset_date ? `\nResets on ${usage.reset_date}` : ""}`}>
          {parts.join(" · ") || "Copilot"}
          {usage.reset_date ? ` · resets ${usage.reset_date}` : ""}
        </span>
      );
    }
  } else if (usage?.kind === "local") {
    account = <span>Self-hosted — no credits</span>;
  } else if (usage?.kind === "tokens") {
    account = <span title="This provider doesn't report a balance; the chat counts tokens.">Billed per token</span>;
  }
  const spent = Object.entries(session.credits ?? {}).filter(([, v]) => v > 0);
  return (
    <div className="chat-credits">
      <span className="chat-credits-acct">
        {account}
        {providerId === "copilot" && (
          <button className="chat-mini-btn" title="Refresh credits" onClick={onRefresh}>⟳</button>
        )}
      </span>
      <span className="chat-credits-sess" title="Used by this chat">
        This chat: {spent.length ? spent.map(([k, v]) => `${fmtCredits(v)} ${k === "chat" ? "credit" + (v === 1 ? "" : "s") : k}`).join(", ") + " · " : ""}
        {tokens}
      </span>
    </div>
  );
}

// ===========================================================================
// One message
// ===========================================================================
const MessageView = memo(function MessageView({
  turn,
  index,
  refs,
  streaming,
  providers,
  editing,
  onStartEdit,
  onCancelEdit,
  onSubmitEdit,
  onRollback,
  onRetry,
  onOpenFile,
}: {
  turn: ChatTurn;
  index: number;
  refs: RefIndex;
  streaming: boolean;
  providers: AIProvider[];
  editing: boolean;
  onStartEdit: (i: number) => void;
  onCancelEdit: () => void;
  onSubmitEdit: (i: number, text: string, providerId: string, model: string) => void;
  onRollback: (i: number) => void;
  onRetry: (i: number, providerId?: string, model?: string) => void;
  onOpenFile: (file: string, line?: number) => void;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const html = useMemo(
    () => (turn.role === "assistant" && turn.content ? renderChatMarkdown(turn.content, refs) : ""),
    [turn.role, turn.content, refs],
  );
  // Colour the code blocks with Monaco's tokenizer (same colours as the editor).
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    el.querySelectorAll<HTMLElement>(".chat-code:not(.streaming) pre code[data-lang]").forEach((code) => {
      if (code.dataset.colored) return;
      code.dataset.colored = "1";
      const lang = code.dataset.lang || "c";
      const text = code.textContent ?? "";
      monaco.editor
        .colorize(text, lang, { tabSize: 4 })
        .then((h) => {
          code.innerHTML = h;
        })
        .catch(() => {});
    });
  }, [html]);

  const [draft, setDraft] = useState(turn.content);
  const [pid, setPid] = useState(turn.provider_id ?? "");
  const [mid, setMid] = useState(turn.model ?? "");
  useEffect(() => {
    if (editing) {
      setDraft(turn.content);
      setPid(turn.provider_id ?? "");
      setMid(turn.model ?? "");
    }
  }, [editing, turn]);

  if (turn.role === "user") {
    if (editing) {
      return (
        <div className="chat-msg user editing">
          <textarea
            className="chat-input chat-edit"
            value={draft}
            autoFocus
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (draft.trim()) onSubmitEdit(index, draft.trim(), pid, mid);
              } else if (e.key === "Escape") onCancelEdit();
            }}
          />
          <div className="chat-edit-bar">
            <ModelSelect providers={providers} providerId={pid} model={mid}
              onChange={(p, m) => { setPid(p); setMid(m); }}
              title="Send the edited prompt to this model" />
            <span className="spacer" />
            <button className="scope-btn" onClick={onCancelEdit}>Cancel</button>
            <button className="save-btn" disabled={!draft.trim() || !mid}
              onClick={() => onSubmitEdit(index, draft.trim(), pid, mid)}>
              Send
            </button>
          </div>
          <div className="chat-edit-note">Sending replaces this prompt and everything after it.</div>
        </div>
      );
    }
    return (
      <div className="chat-msg user">
        <div className="chat-bubble">{turn.content}</div>
        {!streaming && (
          <div className="chat-actions">
            <button className="chat-mini-btn" title="Edit this prompt and send it again" onClick={() => onStartEdit(index)}>✎ Edit</button>
            <button className="chat-mini-btn" title="Remove this prompt and everything after it; its text goes back into the box" onClick={() => onRollback(index)}>⟲ Roll back to here</button>
          </div>
        )}
      </div>
    );
  }

  const pending = streaming && !turn.content && !turn.error;
  return (
    <div className="chat-msg assistant">
      <div className="chat-role">
        {turn.model ? <span title={turn.provider_id}>{turn.model}</span> : "Assistant"}
        {turn.effort ? <span className="chat-role-tag">{turn.effort}</span> : null}
        {turn.usage && (turn.usage.input_tokens || turn.usage.output_tokens) ? (
          <span className="chat-role-tag" title="Tokens this answer used (all its requests)">
            {fmtTokens(turn.usage.input_tokens)} in · {fmtTokens(turn.usage.output_tokens)} out
            {turn.usage.credits?.chat ? ` · ${fmtCredits(turn.usage.credits.chat)} credit` : ""}
          </span>
        ) : null}
      </div>
      {turn.tools && turn.tools.length > 0 && (
        <details className="chat-tools" open={pending}>
          <summary>
            {pending ? "Working… " : ""}Used {turn.tools.length} tool{turn.tools.length === 1 ? "" : "s"}
          </summary>
          <ul>
            {turn.tools.map((t, i) => {
              const { verb, arg } = toolLabel(t);
              const file = t.name === "read_file" ? String(t.args?.path ?? "") : "";
              const fnRef = t.name === "get_function" || t.name === "get_call_graph"
                ? refs.functions.get(String(t.args?.name ?? "")) : undefined;
              return (
                <li key={i} className={t.error ? "err" : ""} title={t.preview ? t.preview : undefined}>
                  <span className="chat-tool-verb">{verb}</span>{" "}
                  {file ? (
                    <a className="code-ref" onClick={() => onOpenFile(file)}>{arg}</a>
                  ) : fnRef ? (
                    <a className="code-ref fn" onClick={() => onOpenFile(fnRef.file, fnRef.line)}>{arg}</a>
                  ) : (
                    arg && <code>{arg}</code>
                  )}
                  {t.chars !== undefined && <span className="chat-tool-size"> · {fmtTokens(Math.ceil(t.chars / 4))} tok</span>}
                </li>
              );
            })}
          </ul>
        </details>
      )}
      {turn.dropped ? (
        <div className="chat-note">The oldest {turn.dropped} message(s) were left out to fit the context window.</div>
      ) : null}
      <div ref={bodyRef} className="chat-bubble md">
        {pending ? (
          <span className="chat-typing">{turn.tools?.length ? "Reading the code…" : "Thinking…"}</span>
        ) : turn.error && !turn.content ? (
          <span className="chat-err">⚠ {turn.error}</span>
        ) : (
          <div dangerouslySetInnerHTML={{ __html: html }} />
        )}
      </div>
      {!streaming && (
        <div className="chat-actions">
          <button className="chat-mini-btn" title="Ask the same question again" onClick={() => onRetry(index)}>↻ Retry</button>
          <RetryWith providers={providers} onPick={(p, m) => onRetry(index, p, m)} />
          {turn.content && (
            <button className="chat-mini-btn" title="Copy the answer" onClick={() => navigator.clipboard?.writeText(turn.content)}>⧉ Copy</button>
          )}
        </div>
      )}
    </div>
  );
});

function RetryWith({ providers, onPick }: { providers: AIProvider[]; onPick: (p: string, m: string) => void }) {
  return (
    <ModelSelect
      className="chat-mini-select"
      providers={providers}
      providerId=""
      model=""
      title="Ask again with another model"
      onChange={onPick}
    />
  );
}

// ===========================================================================
// Explorer: files, functions, chats
// ===========================================================================
function FileTreeView({
  files,
  active,
  onOpen,
}: {
  files: string[];
  active: string | null;
  onOpen: (path: string) => void;
}) {
  const tree = useMemo(() => buildFileTree(files), [files]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Open the folders down to the active file.
  useEffect(() => {
    if (!active) return;
    const parts = active.split("/");
    setExpanded((prev) => {
      const next = new Set(prev);
      let acc = "";
      for (const p of parts.slice(0, -1)) {
        acc = acc ? `${acc}/${p}` : p;
        next.add(acc);
      }
      return next;
    });
  }, [active]);
  const render = (nodes: TreeNode[], depth: number): React.ReactNode =>
    nodes.map((n) => {
      const indent = 8 + depth * 12;
      if (n.isDir) {
        const open = expanded.has(n.path);
        return (
          <Fragment key={`d:${n.path}`}>
            <button className="tci-tree-row dir" style={{ paddingLeft: indent }}
              onClick={() => setExpanded((prev) => {
                const next = new Set(prev);
                if (next.has(n.path)) next.delete(n.path);
                else next.add(n.path);
                return next;
              })}>
              <span className="tci-tree-chev">{open ? "▾" : "▸"}</span>
              <FolderIcon size={13} />
              <span className="tci-tree-name">{n.name}</span>
            </button>
            {open && render(n.children, depth + 1)}
          </Fragment>
        );
      }
      return (
        <button key={`f:${n.path}`} className={"tci-tree-row file" + (active === n.path ? " sel" : "")}
          style={{ paddingLeft: indent + 14 }} title={n.path} onClick={() => onOpen(n.path)}>
          <FileIcon size={13} />
          <span className="tci-tree-name">{n.name}</span>
        </button>
      );
    });
  return <>{render(tree, 0)}</>;
}

function Splitter({ onDrag }: { onDrag: (dx: number) => void }) {
  return (
    <div
      className="chat-splitter"
      onPointerDown={(e) => {
        const el = e.currentTarget;
        el.setPointerCapture(e.pointerId);
        let last = e.clientX;
        const move = (ev: PointerEvent) => {
          onDrag(ev.clientX - last);
          last = ev.clientX;
        };
        const up = () => {
          el.removeEventListener("pointermove", move);
          el.removeEventListener("pointerup", up);
        };
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", up);
      }}
    />
  );
}

// ===========================================================================
// The view
// ===========================================================================
export function AIChat({ toast }: { toast: (msg: string) => void }) {
  const { confirm, confirmNode } = useConfirm();
  const theme = useMonacoTheme();
  const [prefs, setPrefsState] = useState<ChatPrefs>(loadPrefs);
  const setPrefs = useCallback((patch: Partial<ChatPrefs>) => {
    setPrefsState((p) => {
      const next = { ...p, ...patch };
      savePrefs(next);
      return next;
    });
  }, []);
  const setSettings = (patch: Partial<ChatSettings>) =>
    setPrefs({ settings: { ...prefs.settings, ...patch } });

  const [providers, setProviders] = useState<AIProvider[]>([]);
  const [agentsResp, setAgentsResp] = useState<AgentsResponse | null>(null);
  const [agentsOpen, setAgentsOpen] = useState(false);
  const [canEditProject, setCanEditProject] = useState(false);
  const [modelInfo, setModelInfo] = useState<AIModelInfo | null>(null);
  const [account, setAccount] = useState<ProviderUsage | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // conversation
  const [sessionId, setSessionId] = useState<string>(uid);
  const [title, setTitle] = useState("");
  const [messages, setMessages] = useState<ChatTurn[]>([]);
  const [sessionUsage, setSessionUsage] = useState<ChatUsage>({ input_tokens: 0, output_tokens: 0, requests: 0, credits: {} });
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [estimate, setEstimate] = useState<ChatContextBreakdown | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // code
  const [explorerTab, setExplorerTab] = useState<"files" | "functions" | "chats">("files");
  const [files, setFiles] = useState<string[]>([]);
  const [functions, setFunctions] = useState<SourceFunction[]>([]);
  const [sourceMsg, setSourceMsg] = useState<string | null>(null);
  const [fnFilter, setFnFilter] = useState("");
  const [tabs, setTabs] = useState<string[]>([]);
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [contents, setContents] = useState<Record<string, string>>({});
  const [highlight, setHighlight] = useState<{ file: string; start: number; end: number } | null>(null);
  const editorRef = useRef<MonacoEditor | null>(null);
  // Bumped when Monaco mounts: a highlight asked for before that is applied then.
  const [editorReady, setEditorReady] = useState(0);
  const decoRef = useRef<ReturnType<MonacoEditor["createDecorationsCollection"]> | null>(null);
  const [selection, setSelection] = useState<{ text: string; start: number; end: number } | null>(null);

  const refs = useMemo(
    () => (files.length || functions.length ? buildRefIndex(files, functions) : EMPTY_REFS),
    [files, functions],
  );
  const agents = agentsResp?.agents ?? [];
  const agent: ChatAgent | undefined =
    agents.find((a) => a.id === prefs.agentId) ?? agents.find((a) => a.id === agentsResp?.default_agent_id) ?? agents[0];

  // --- loading -------------------------------------------------------------
  const loadSessions = useCallback(() => {
    api.get<{ sessions: ChatSessionSummary[] }>("/ai/chats")
      .then((r) => setSessions(r.sessions))
      .catch(() => {});
  }, []);

  const loadSource = useCallback(() => {
    Promise.all([
      api.get<{ files: SourceFileInfo[] }>("/injection/source/files"),
      api.get<{ functions: SourceFunction[] }>("/injection/source/functions"),
    ])
      .then(([f, fn]) => {
        setFiles(f.files.map((x) => x.rel_path));
        setFunctions(fn.functions);
        setSourceMsg(null);
      })
      .catch((e) => {
        setFiles([]);
        setFunctions([]);
        setSourceMsg((e as Error).message.replace(/^\d+: /, ""));
      });
  }, []);

  const loadAgents = useCallback(() => {
    api.get<AgentsResponse>("/ai/agents").then(setAgentsResp).catch(() => {});
    api.get<{ open: boolean; can_edit?: boolean }>("/project/status")
      .then((s) => setCanEditProject(!!s.open && !!s.can_edit))
      .catch(() => setCanEditProject(false));
  }, []);

  useEffect(() => {
    let alive = true;
    api.get<ProvidersResponse>("/ai/providers")
      .then((r) => {
        if (!alive) return;
        setProviders(r.providers);
        const usable = r.providers.filter((p) => p.configured && p.models.length);
        const keep = usable.find((p) => p.id === prefs.providerId);
        if (keep) {
          if (!keep.models.some((m) => m.id === prefs.model)) {
            setPrefs({ model: prefs.modelByProvider[keep.id] ?? keep.models[0].id });
          }
        } else if (usable[0]) {
          const p = usable[0];
          setPrefs({ providerId: p.id, model: prefs.modelByProvider[p.id] ?? p.models[0].id });
        }
      })
      .catch((e) => alive && toast(`AI providers: ${(e as Error).message}`));
    loadAgents();
    loadSessions();
    loadSource();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useSSE(useCallback((e) => {
    if (e.event === "db-changed") {
      const reason = String(e.data?.reason ?? "");
      if (/source|release/.test(reason) || !reason) loadSource();
    }
  }, [loadSource]));

  useEffect(() => () => abortRef.current?.abort(), []);

  // model info + account credits follow the picked model
  useEffect(() => {
    if (!prefs.providerId || !prefs.model) {
      setModelInfo(null);
      return;
    }
    let alive = true;
    api.get<AIModelInfo>(`/ai/model-info?provider_id=${encodeURIComponent(prefs.providerId)}&model=${encodeURIComponent(prefs.model)}`)
      .then((i) => alive && setModelInfo(i))
      .catch(() => alive && setModelInfo(null));
    return () => {
      alive = false;
    };
  }, [prefs.providerId, prefs.model]);

  const refreshAccount = useCallback((refresh = false) => {
    if (!prefs.providerId) return;
    api.get<ProviderUsage>(`/ai/usage/${prefs.providerId}${refresh ? "?refresh=true" : ""}`)
      .then(setAccount)
      .catch(() => setAccount(null));
  }, [prefs.providerId]);
  useEffect(() => refreshAccount(), [refreshAccount]);

  // an effort the new model doesn't take is dropped
  useEffect(() => {
    if (modelInfo && prefs.settings.effort && !modelInfo.efforts.includes(prefs.settings.effort)) {
      setSettings({ effort: null });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelInfo]);

  // --- request body -----------------------------------------------------------
  const chatBody = useCallback(
    (msgs: { role: string; content: string }[], providerId: string, model: string) => ({
      provider_id: providerId,
      model,
      messages: msgs,
      ground_in_mindmap: prefs.settings.ground,
      map_scope: prefs.settings.map_scope,
      use_tools: prefs.settings.use_tools,
      agent_id: agent?.id ?? null,
      open_files: activeFile ? [activeFile, ...tabs.filter((t) => t !== activeFile)] : tabs,
      options: {
        effort: prefs.settings.effort,
        num_ctx: providerId === "ollama" ? prefs.settings.num_ctx : null,
        context_limit: providerId === "ollama" ? null : prefs.settings.context_limit,
        max_output_tokens: prefs.settings.max_output_tokens,
      },
    }),
    [prefs.settings, agent, activeFile, tabs],
  );

  // live context estimate (debounced)
  useEffect(() => {
    if (!prefs.providerId || !prefs.model) return;
    const msgs = messages.filter((m) => m.content).map((m) => ({ role: m.role, content: m.content }));
    if (input.trim()) msgs.push({ role: "user", content: input });
    const t = setTimeout(() => {
      api.post<ChatContextBreakdown>("/ai/context", chatBody(msgs, prefs.providerId, prefs.model))
        .then(setEstimate)
        .catch(() => {});
    }, 350);
    return () => clearTimeout(t);
  }, [messages, input, prefs.providerId, prefs.model, chatBody]);

  // --- saving -------------------------------------------------------------------
  const saveSession = useCallback(
    (msgs: ChatTurn[], usage: ChatUsage, id: string, providerId: string, model: string) => {
      const body: ChatSession = {
        id,
        title: title || undefined,
        provider_id: providerId,
        model,
        agent_id: agent?.id ?? "",
        settings: prefs.settings,
        usage,
        messages: msgs,
      };
      return api.put<ChatSessionSummary>(`/ai/chats/${id}`, body)
        .then((s) => {
          setTitle(s.title);
          loadSessions();
        })
        .catch((e) => toast(`Couldn't save the chat: ${(e as Error).message}`));
    },
    [title, agent, prefs.settings, loadSessions, toast],
  );

  // --- sending ------------------------------------------------------------------
  const send = useCallback(
    async (text: string, opts: { base?: ChatTurn[]; providerId?: string; model?: string } = {}) => {
      const providerId = opts.providerId || prefs.providerId;
      const model = opts.model || prefs.model;
      if (!text.trim() || !providerId || !model || streaming) return;
      if (opts.providerId && (opts.providerId !== prefs.providerId || opts.model !== prefs.model)) {
        setPrefs({ providerId, model, modelByProvider: { ...prefs.modelByProvider, [providerId]: model } });
      }
      const base = opts.base ?? messages;
      const user: ChatTurn = { id: uid(), role: "user", content: text, provider_id: providerId, model, ts: new Date().toISOString() };
      let asst: ChatTurn = {
        id: uid(), role: "assistant", content: "", provider_id: providerId, model,
        agent_id: agent?.id, effort: prefs.settings.effort, tools: [],
        usage: { input_tokens: 0, output_tokens: 0, requests: 0, credits: {} },
      };
      let usage = sessionUsage;
      const id = sessionId;
      const sync = () => setMessages([...base, user, { ...asst }]);
      stickRef.current = true;
      sync();
      setEditingIdx(null);
      setStreaming(true);
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const history = [...base, user]
        .filter((m) => m.content && !(m.role === "assistant" && m.error && !m.content))
        .map((m) => ({ role: m.role, content: m.content }));
      try {
        await streamChat(chatBody(history, providerId, model), (ev) => {
          switch (ev.kind) {
            case "context":
              setEstimate(ev.data);
              if (ev.data.dropped) asst = { ...asst, dropped: ev.data.dropped };
              break;
            case "tool":
              asst = { ...asst, tools: [...(asst.tools ?? []), { name: ev.data.name, args: ev.data.args }] };
              break;
            case "tool_result": {
              const ts = [...(asst.tools ?? [])];
              const idx = ts.map((t) => t.name).lastIndexOf(ev.data.name);
              if (idx >= 0) ts[idx] = { ...ts[idx], ...ev.data };
              asst = { ...asst, tools: ts };
              break;
            }
            case "usage": {
              const u = { input_tokens: ev.data.input_tokens ?? 0, output_tokens: ev.data.output_tokens ?? 0, requests: 1, last_input_tokens: ev.data.input_tokens };
              asst = { ...asst, usage: addUsage(asst.usage ?? {}, u) };
              usage = addUsage(usage, u);
              break;
            }
            case "token":
              asst = { ...asst, content: asst.content + ev.data };
              break;
            case "credits":
              asst = { ...asst, usage: addUsage(asst.usage ?? {}, { credits: ev.data.delta }) };
              usage = addUsage(usage, { credits: ev.data.delta });
              if (ev.data.quota) setAccount({ ...ev.data.quota, kind: "quota", signed_in: true });
              break;
            case "error":
              asst = { ...asst, error: ev.data };
              if (ev.data !== "Stopped.") toast(`Chat: ${ev.data}`);
              break;
            case "done":
              if (!asst.content && ev.data) asst = { ...asst, content: ev.data };
              break;
          }
          sync();
          setSessionUsage(usage);
        }, ctrl.signal);
      } catch (e) {
        if ((e as Error).name === "AbortError") asst = { ...asst, error: asst.error ?? "Stopped." };
        else {
          asst = { ...asst, error: (e as Error).message };
          toast(`Chat failed: ${(e as Error).message}`);
        }
        sync();
      } finally {
        setStreaming(false);
        abortRef.current = null;
        saveSession([...base, user, asst], usage, id, providerId, model);
      }
    },
    [prefs, streaming, messages, agent, sessionUsage, sessionId, chatBody, saveSession, setPrefs, toast],
  );

  const stop = () => abortRef.current?.abort();

  const sendInput = () => {
    const text = input.trim();
    if (!text) return;
    setInput("");
    send(text);
  };

  // --- edit / rollback / retry -----------------------------------------------------
  const onSubmitEdit = useCallback(
    (i: number, text: string, providerId: string, model: string) =>
      send(text, { base: messages.slice(0, i), providerId, model }),
    [send, messages],
  );
  const onRollback = useCallback(async (i: number) => {
    const after = messages.length - i;
    const ok = await confirm({
      title: "Roll back the conversation?",
      message: `This removes this prompt and the ${after - 1} message(s) after it. The prompt's text goes back into the box so you can change it.`,
      confirmLabel: "Roll back",
      danger: true,
    });
    if (!ok) return;
    const kept = messages.slice(0, i);
    setInput(messages[i].content);
    setMessages(kept);
    saveSession(kept, sessionUsage, sessionId, prefs.providerId, prefs.model);
  }, [messages, confirm, saveSession, sessionUsage, sessionId, prefs.providerId, prefs.model]);
  const onRetry = useCallback((i: number, providerId?: string, model?: string) => {
    // the user prompt this answer replied to
    let u = i - 1;
    while (u >= 0 && messages[u].role !== "user") u--;
    if (u < 0) return;
    send(messages[u].content, { base: messages.slice(0, u), providerId, model });
  }, [messages, send]);

  // --- sessions ----------------------------------------------------------------------
  const newChat = () => {
    if (streaming) stop();
    setSessionId(uid());
    setTitle("");
    setMessages([]);
    setSessionUsage({ input_tokens: 0, output_tokens: 0, requests: 0, credits: {} });
    setEditingIdx(null);
  };
  const openSession = async (id: string) => {
    if (streaming) stop();
    try {
      const s = await api.get<ChatSession>(`/ai/chats/${id}`);
      setSessionId(s.id);
      setTitle(s.title ?? "");
      setMessages(s.messages ?? []);
      setSessionUsage({ input_tokens: 0, output_tokens: 0, requests: 0, credits: {}, ...(s.usage ?? {}) });
      setEditingIdx(null);
      // pick up where the chat left off: its model and agent
      if (s.provider_id && providers.some((p) => p.id === s.provider_id && p.configured)) {
        setPrefs({ providerId: s.provider_id, model: s.model, agentId: s.agent_id || prefs.agentId });
      }
    } catch (e) {
      toast(`Couldn't open the chat: ${(e as Error).message}`);
    }
  };
  const renameSession = async (s: ChatSessionSummary) => {
    const t = window.prompt("Rename chat", s.title);
    if (t === null) return;
    await api.patch(`/ai/chats/${s.id}`, { title: t }).catch(() => {});
    if (s.id === sessionId) setTitle(t);
    loadSessions();
  };
  const deleteSession = async (s: ChatSessionSummary) => {
    const ok = await confirm({ title: "Delete chat?", message: `"${s.title}" will be deleted.`, confirmLabel: "Delete", danger: true });
    if (!ok) return;
    await api.del(`/ai/chats/${s.id}`).catch(() => {});
    if (s.id === sessionId) newChat();
    loadSessions();
  };

  // --- code pane -------------------------------------------------------------------
  const openFile = useCallback(async (file: string, line?: number, end?: number, fn?: string) => {
    setTabs((t) => (t.includes(file) ? t : [...t, file]));
    setActiveFile(file);
    let text = contents[file];
    if (text === undefined) {
      try {
        const r = await api.get<{ content: string }>(`/injection/source/content?rel_path=${encodeURIComponent(file)}`);
        text = r.content;
        setContents((c) => ({ ...c, [file]: r.content }));
      } catch (e) {
        toast(`Couldn't open ${file}: ${(e as Error).message}`);
        return;
      }
    }
    if (line) {
      const stop = end ?? (fn ? functionEnd(text, line) : line);
      setHighlight({ file, start: line, end: Math.max(line, stop) });
    } else setHighlight(null);
  }, [contents, toast]);

  // apply the highlight once the editor shows the file
  useEffect(() => {
    const ed = editorRef.current;
    if (!ed || !highlight || highlight.file !== activeFile || contents[activeFile] === undefined) {
      decoRef.current?.clear();
      return;
    }
    const t = setTimeout(() => {
      const { start, end } = highlight;
      ed.revealLineInCenter(start);
      ed.setPosition({ lineNumber: start, column: 1 });
      decoRef.current?.set([
        { range: new monaco.Range(start, 1, end, 1), options: { isWholeLine: true, className: "chat-hl-line", linesDecorationsClassName: "chat-hl-gutter" } },
      ]);
    }, 30);
    return () => clearTimeout(t);
  }, [highlight, activeFile, contents, editorReady]);

  const closeTab = (f: string) => {
    setTabs((t) => {
      const next = t.filter((x) => x !== f);
      if (activeFile === f) setActiveFile(next[next.length - 1] ?? null);
      return next;
    });
  };

  const onEditorMount: OnMount = (ed) => {
    editorRef.current = ed;
    decoRef.current = ed.createDecorationsCollection();
    setEditorReady((n) => n + 1);
    ed.onDidChangeCursorSelection(() => {
      const sel = ed.getSelection();
      const model = ed.getModel();
      if (!sel || !model || sel.isEmpty()) {
        setSelection(null);
        return;
      }
      setSelection({ text: model.getValueInRange(sel), start: sel.startLineNumber, end: sel.endLineNumber });
    });
  };

  const askAboutSelection = () => {
    if (!selection || !activeFile) return;
    const ref = `\`${activeFile}:${selection.start}${selection.end !== selection.start ? `-${selection.end}` : ""}\``;
    setInput((v) => `${v ? v + "\n\n" : ""}${ref}\n\`\`\`c\n${selection.text}\n\`\`\`\n`);
  };

  // clicks on links / copy buttons inside answers
  const onThreadClick = (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    const copy = target.closest<HTMLElement>("[data-copy]");
    if (copy) {
      const code = copy.closest(".chat-code")?.querySelector("pre code");
      navigator.clipboard?.writeText(code?.textContent ?? "");
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy"), 1200);
      return;
    }
    const a = target.closest<HTMLElement>("a.code-ref[data-file]");
    if (a) {
      e.preventDefault();
      const line = a.dataset.line ? +a.dataset.line : undefined;
      const end = a.dataset.end ? +a.dataset.end : undefined;
      openFile(a.dataset.file!, line, end, a.dataset.fn);
    }
  };

  // keep the thread scrolled to the bottom while the answer grows, unless
  // the user scrolled up to read
  useEffect(() => {
    const el = threadRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const pickAgent = (id: string) => {
    const a = agents.find((x) => x.id === id);
    const patch: Partial<ChatPrefs> = { agentId: id };
    if (a?.model) {
      // The agent's preferred model, if an account offers it (by id or name).
      const want = a.model.toLowerCase().replace(/\s*\(.*\)\s*$/, "").trim();
      for (const p of providers.filter((x) => x.configured)) {
        const m = p.models.find((x) => x.id.toLowerCase() === want || (x.name ?? "").toLowerCase().replace(/\s*\(.*\)\s*$/, "") === want);
        if (m) {
          patch.providerId = p.id;
          patch.model = m.id;
          break;
        }
      }
    }
    setPrefs(patch);
  };

  const pickModel = (providerId: string, model: string) =>
    setPrefs({ providerId, model, modelByProvider: { ...prefs.modelByProvider, [providerId]: model } });

  const fnMatches = useMemo(() => {
    const q = fnFilter.trim().toLowerCase();
    const list = q ? functions.filter((f) => f.name.toLowerCase().includes(q)) : functions;
    return list.slice(0, 400);
  }, [functions, fnFilter]);

  const last = [...messages].reverse().find((m) => m.role === "assistant" && m.usage?.last_input_tokens);
  const noProviders = providers.length > 0 && !providers.some((p) => p.configured && p.models.length);
  const ctxChoices = [4096, 8192, 16384, 32768, 65536, 131072, 262144];
  const isOllama = prefs.providerId === "ollama";
  const maxCtx = isOllama ? modelInfo?.max_context_tokens ?? 131072 : modelInfo?.context_tokens ?? 128000;

  return (
    <div className="chatx-view">
      {/* ---------------- explorer ---------------- */}
      <aside className="chatx-explorer" style={{ width: prefs.explorerW }}>
        <div className="chatx-tabs">
          {(["files", "functions", "chats"] as const).map((t) => (
            <button key={t} className={"chatx-tab" + (explorerTab === t ? " sel" : "")} onClick={() => setExplorerTab(t)}>
              {t === "files" ? "Files" : t === "functions" ? "Functions" : "Chats"}
            </button>
          ))}
        </div>
        <div className="chatx-explorer-body">
          {explorerTab === "files" && (
            sourceMsg ? <div className="chatx-empty">{sourceMsg}</div>
              : files.length === 0 ? <div className="chatx-empty">Loading the release's source…</div>
              : <FileTreeView files={files} active={activeFile} onOpen={(f) => openFile(f)} />
          )}
          {explorerTab === "functions" && (
            <>
              <input className="chatx-filter" placeholder={`Filter ${functions.length} functions…`} value={fnFilter} onChange={(e) => setFnFilter(e.target.value)} />
              {sourceMsg && <div className="chatx-empty">{sourceMsg}</div>}
              {fnMatches.map((f) => (
                <button key={`${f.rel_path}:${f.name}:${f.line}`} className="chatx-fn" title={`${f.rel_path}:${f.line}`} onClick={() => openFile(f.rel_path, f.line, undefined, f.name)}>
                  <span className="chatx-fn-name">{f.name}</span>
                  <span className="chatx-fn-file">{f.rel_path.split("/").pop()}:{f.line}</span>
                </button>
              ))}
            </>
          )}
          {explorerTab === "chats" && (
            <>
              <button className="scope-btn chatx-new" onClick={newChat}>＋ New chat</button>
              {sessions.length === 0 && <div className="chatx-empty">No saved chats for this project yet.</div>}
              {sessions.map((s) => (
                <div key={s.id} className={"chatx-sess" + (s.id === sessionId ? " sel" : "")}>
                  <button className="chatx-sess-main" onClick={() => openSession(s.id)} onDoubleClick={() => renameSession(s)} title={`${s.title}\n${s.model} · ${s.message_count} messages\n${s.updated_at ?? ""}`}>
                    <span className="chatx-sess-title">{s.title}</span>
                    <span className="chatx-sess-meta">{(s.updated_at ?? "").replace("T", " ").slice(0, 16)} · {s.model}</span>
                  </button>
                  <button className="tci-icon-btn" title="Rename" onClick={() => renameSession(s)}>✎</button>
                  <button className="tci-icon-btn danger" title="Delete" onClick={() => deleteSession(s)}>✕</button>
                </div>
              ))}
            </>
          )}
        </div>
      </aside>
      <Splitter onDrag={(dx) => setPrefs({ explorerW: Math.max(160, Math.min(520, prefs.explorerW + dx)) })} />

      {/* ---------------- code ---------------- */}
      <section className="chatx-code">
        <div className="chatx-code-tabs">
          {tabs.map((t) => (
            <div key={t} className={"chatx-code-tab" + (t === activeFile ? " sel" : "")} title={t}>
              <button className="chatx-code-tab-name" onClick={() => { setActiveFile(t); }}>{t.split("/").pop()}</button>
              <button className="chatx-code-tab-x" onClick={() => closeTab(t)}>×</button>
            </div>
          ))}
          <span className="spacer" />
          {selection && activeFile && (
            <button className="chat-mini-btn chatx-ask-sel" onClick={askAboutSelection} title="Put the selected code into the chat box">
              ➜ Ask about selection
            </button>
          )}
        </div>
        <div className="chatx-editor">
          {activeFile && contents[activeFile] !== undefined ? (
            <Editor
              path={activeFile}
              language={/\.(py)$/i.test(activeFile) ? "python" : /\.(cpp|hpp|cc|hh|cxx)$/i.test(activeFile) ? "cpp" : "c"}
              theme={theme}
              value={contents[activeFile]}
              onMount={onEditorMount}
              options={{ readOnly: true, domReadOnly: true, minimap: { enabled: true }, fontSize: 12, scrollBeyondLastLine: false, automaticLayout: true, renderLineHighlight: "none" }}
            />
          ) : (
            <div className="chatx-code-empty">
              <div className="chatx-code-empty-h">No file open</div>
              Pick a file or function on the left, or click a function or <code>file.c:123</code> in an answer to see the code here.
            </div>
          )}
        </div>
        {activeFile && <div className="chatx-code-status">{activeFile}{highlight?.file === activeFile ? ` · lines ${highlight.start}–${highlight.end}` : ""}</div>}
      </section>
      <Splitter onDrag={(dx) => setPrefs({ chatW: Math.max(320, Math.min(900, prefs.chatW - dx)) })} />

      {/* ---------------- chat ---------------- */}
      <section className="chatx-chat" style={{ width: prefs.chatW }}>
        <div className="chatx-chat-head">
          <span className="chatx-chat-title" title={title}>{title || "New chat"}</span>
          <span className="spacer" />
          <button className="chat-mini-btn" title="Chat history" onClick={() => setExplorerTab("chats")}>History</button>
          <button className="chat-mini-btn" title="Start a new chat" onClick={newChat}>＋ New</button>
        </div>
        <CreditsBar providerId={prefs.providerId} usage={account} session={sessionUsage} onRefresh={() => refreshAccount(true)} />

        <div
          className="chat-thread chatx-thread"
          ref={threadRef}
          onClick={onThreadClick}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
        >
          {messages.length === 0 ? (
            <div className="chat-empty chatx-welcome">
              <div className="chatx-welcome-h">{agent?.name ?? "Ask"}</div>
              <div>{agent?.description || "Ask about the architecture and the code."}</div>
              {noProviders && <div className="chatx-warn">No AI account is set up. Add one in Preferences → AI Settings.</div>}
              {estimate && !estimate.project_open && <div className="chatx-warn">No project is open: the agent can't see any code.</div>}
              {estimate && estimate.project_open && !estimate.has_source && (
                <div className="chatx-warn">The active release has no source imported, so the agent can't read the code. Import it in the Release Manager.</div>
              )}
              {estimate && estimate.project_open && prefs.settings.ground && !estimate.has_mind_map && (
                <div className="chatx-warn">No mind map for this release yet. Build the Code Map and mind map (Code Map tab) — the agent then knows the structure before it reads files.</div>
              )}
              {estimate && estimate.project_open && !prefs.settings.use_tools && (
                <div className="chatx-warn">Code tools are off: the agent only sees the mind map.</div>
              )}
              <div className="chatx-suggest">
                {["What does the main loop call, in order?", "Which functions write the motor speed variables?", "Explain the selected function and its callers."].map((s) => (
                  <button key={s} className="chat-mini-btn" onClick={() => setInput(s)}>{s}</button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => (
              <MessageView
                key={m.id ?? i}
                turn={m}
                index={i}
                refs={refs}
                streaming={streaming}
                providers={providers}
                editing={editingIdx === i}
                onStartEdit={setEditingIdx}
                onCancelEdit={() => setEditingIdx(null)}
                onSubmitEdit={onSubmitEdit}
                onRollback={onRollback}
                onRetry={onRetry}
                onOpenFile={(f, l) => openFile(f, l, undefined, l ? "fn" : undefined)}
              />
            ))
          )}
        </div>

        <div className="chatx-composer">
          <textarea
            className="chat-input chatx-input"
            placeholder={`Ask ${agent?.name ?? "the agent"}…  (Enter to send, Shift+Enter for a new line)`}
            spellCheck={false}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!streaming) sendInput();
              }
            }}
            rows={3}
          />
          {activeFile && (
            <div className="chatx-ctx-files" title="Files open in the editor go with the request (rules with applyTo match them)">
              📎 {activeFile.split("/").pop()}{tabs.length > 1 ? ` +${tabs.length - 1}` : ""}
            </div>
          )}
          <div className="chatx-bar">
            <select className="chat-pick" value={agent?.id ?? ""} onChange={(e) => pickAgent(e.target.value)} title={agent?.description || "Agent"}>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
            <button className="chat-mini-btn" title="Set up agents and rules" onClick={() => setAgentsOpen(true)}>⚙</button>
            <ModelSelect providers={providers} providerId={prefs.providerId} model={prefs.model} onChange={pickModel} />
            {modelInfo && modelInfo.efforts.length > 0 && (
              <select className="chat-pick" value={prefs.settings.effort ?? ""} title="Reasoning effort" onChange={(e) => setSettings({ effort: e.target.value || null })}>
                <option value="">Default effort</option>
                {modelInfo.efforts.map((ef) => (
                  <option key={ef} value={ef}>{EFFORT_LABEL[ef] ?? ef}</option>
                ))}
              </select>
            )}
            <select
              className="chat-pick"
              title={isOllama ? "Context window Ollama runs the model with (num_ctx)" : "How much of the model's context window the chat may fill"}
              value={String((isOllama ? prefs.settings.num_ctx : prefs.settings.context_limit) ?? "")}
              onChange={(e) => {
                const v = e.target.value ? +e.target.value : null;
                setSettings(isOllama ? { num_ctx: v } : { context_limit: v });
              }}
            >
              <option value="">{isOllama ? `Context: server (${fmtTokens(modelInfo?.context_tokens)})` : `Context: full (${fmtTokens(modelInfo?.context_tokens)})`}</option>
              {ctxChoices.filter((n) => n <= (maxCtx || 0) || n <= 8192).map((n) => (
                <option key={n} value={n}>Context: {fmtTokens(n)}</option>
              ))}
            </select>
            <span className="spacer" />
            <ContextMeter estimate={estimate} lastInput={last?.usage?.last_input_tokens} />
            <div className="chatx-settings-wrap">
              <button className="chat-mini-btn" title="Chat settings" onClick={() => setSettingsOpen((v) => !v)}>⋯</button>
              {settingsOpen && (
                <div className="chatx-settings" onMouseLeave={() => setSettingsOpen(false)}>
                  <label className="chat-ground">
                    <input type="checkbox" checked={prefs.settings.use_tools} onChange={(e) => setSettings({ use_tools: e.target.checked })} />
                    Let the agent read the code (tools)
                  </label>
                  <label className="chat-ground">
                    <input type="checkbox" checked={prefs.settings.ground} onChange={(e) => setSettings({ ground: e.target.checked })} />
                    Send the Code Mind Map
                  </label>
                  <label className="aig-field">
                    <span>Mind map</span>
                    <select value={prefs.settings.map_scope} disabled={!prefs.settings.ground} onChange={(e) => setSettings({ map_scope: e.target.value as "model" | "project" })}>
                      <option value="model">This model</option>
                      <option value="project">Whole project</option>
                    </select>
                  </label>
                  <label className="aig-field">
                    <span>Longest answer</span>
                    <select value={String(prefs.settings.max_output_tokens ?? "")} onChange={(e) => setSettings({ max_output_tokens: e.target.value ? +e.target.value : null })}>
                      <option value="">Model default</option>
                      {[1024, 2048, 4096, 8192, 16384, 32768].map((n) => (
                        <option key={n} value={n}>{fmtTokens(n)} tokens</option>
                      ))}
                    </select>
                  </label>
                  {modelInfo && (
                    <div className="chatx-settings-note">
                      {modelInfo.tools ? "This model calls the tools natively." : "This model gets the tools as text tags."}
                      {estimate?.tool_names?.length ? ` Tools: ${estimate.tool_names.length}.` : ""}
                    </div>
                  )}
                </div>
              )}
            </div>
            {streaming ? (
              <button className="scope-btn" onClick={stop}>Stop</button>
            ) : (
              <button className="save-btn" disabled={!input.trim() || !prefs.model} onClick={sendInput}>Send</button>
            )}
          </div>
        </div>
      </section>

      {agentsOpen && agentsResp && (
        <AgentsDialog
          initial={agentsResp}
          canEdit={canEditProject}
          onClose={() => setAgentsOpen(false)}
          onSaved={(r) => setAgentsResp(r)}
          toast={toast}
        />
      )}
      {confirmNode}
    </div>
  );
}
