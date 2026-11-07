import Editor, { DiffEditor, type OnMount } from "@monaco-editor/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { useSSE } from "../api/useSSE";
import type {
  AIProvider,
  JobPayload,
  ProvidersResponse,
  TargetCheckReport,
  TargetConfig,
  TargetSnapshot,
  TargetTestDetail,
  TargetTestResult,
  TargetTestSummary,
} from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { renderMarkdown } from "../markdown";
import { ModelSelect, streamChat } from "./AIChat";

// Target Tests: architecture tests on the real ECU through winIDEA.
// Left: the tests. Middle: the selected test's script, its runs and the
// debugger. Right: the AI, which can drive the target and propose tests.
//
// Approval: the AI's tests and changes are drafts; a manual save is approved
// by you; accepting an AI proposal and approving it are two separate steps.

type MonacoEditor = Parameters<OnMount>[0];
type MonacoNs = Parameters<OnMount>[1];

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

const VERDICT_TONE: Record<string, string> = {
  passed: "ok",
  failed: "err",
  error: "warn",
  stopped: "grey",
};

function StatusBadge({ t }: { t: Pick<TargetTestSummary, "status" | "has_proposal"> }) {
  return (
    <>
      <span className={`tt-badge ${t.status === "approved" ? "ok" : "purple"}`}>
        {t.status === "approved" ? "Approved" : "Draft (AI)"}
      </span>
      {t.has_proposal && <span className="tt-badge warn">Proposal</span>}
    </>
  );
}

function VerdictBadge({ r }: { r: { verdict: string; approved: boolean } | null }) {
  if (!r) return <span className="tt-badge grey">Not run</span>;
  return (
    <span
      className={`tt-badge ${VERDICT_TONE[r.verdict] ?? "grey"}`}
      title={r.approved ? "Verdict of the approved test" : "Run of a draft: not a verdict"}
    >
      {r.verdict[0].toUpperCase() + r.verdict.slice(1)}
      {!r.approved && " (draft)"}
    </span>
  );
}

const fmt = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

// ===========================================================================
// Connection settings + check
// ===========================================================================
function ConnectionPanel({
  toast,
  busy,
}: {
  toast: (m: string) => void;
  busy: string | null;
}) {
  const [cfg, setCfg] = useState<TargetConfig | null>(null);
  const [report, setReport] = useState<TargetCheckReport | null>(null);
  const [checking, setChecking] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    api.get<TargetConfig>("/target-tests/config/target").then(setCfg).catch(() => {});
  }, []);

  const save = async (patch: Partial<TargetConfig>) => {
    try {
      setCfg(await api.put<TargetConfig>("/target-tests/config/target", patch));
      setReport(null);
    } catch (e) {
      toast(`Settings: ${(e as Error).message}`);
    }
  };

  const check = async () => {
    setChecking(true);
    try {
      setReport(await api.post<TargetCheckReport>("/target-tests/connection/check"));
    } catch (e) {
      toast(`Connection check: ${(e as ApiError).detail ?? (e as Error).message}`);
    } finally {
      setChecking(false);
    }
  };

  if (!cfg) return null;
  const state = busy ? `In use: ${busy}` : report ? (report.ok ? "Connected" : "Check failed") : "Not checked";
  const tone = busy ? "warn" : report ? (report.ok ? "ok" : "err") : "grey";
  return (
    <div className="tt-conn">
      <div className="tt-conn-head" onClick={() => setOpen(!open)}>
        <span className={`tt-dot ${tone}`} />
        <span className="tt-conn-title">
          {cfg.kind === "simulator" ? "Simulator" : "winIDEA"} — {state}
        </span>
        <span className="tt-spacer" />
        <span className="tt-muted">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <div className="tt-conn-body">
          <label>
            Target
            <select value={cfg.kind} onChange={(e) => save({ kind: e.target.value as TargetConfig["kind"] })}>
              <option value="winidea">winIDEA</option>
              <option value="simulator">Simulator (no ECU)</option>
            </select>
          </label>
          {cfg.kind === "winidea" && (
            <>
              <label title="The winIDEA installation; its own isystem.connect SDK is used so the versions match.">
                winIDEA folder
                <input
                  defaultValue={cfg.winidea_dir}
                  placeholder="C:\iSYSTEM\winIDEA9"
                  onBlur={(e) => e.target.value !== cfg.winidea_dir && save({ winidea_dir: e.target.value })}
                />
              </label>
              <label title="Empty: the winIDEA instance you used last.">
                Workspace
                <input
                  defaultValue={cfg.workspace}
                  placeholder="…\project.xjrf (empty = last used)"
                  onBlur={(e) => e.target.value !== cfg.workspace && save({ workspace: e.target.value })}
                />
              </label>
              <label title="A symbol the check reads to prove memory access works.">
                Probe symbol
                <input
                  defaultValue={cfg.probe}
                  onBlur={(e) => e.target.value !== cfg.probe && save({ probe: e.target.value })}
                />
              </label>
            </>
          )}
          <label title="A test that runs longer is stopped and recorded as an error. Debug pauses don't count.">
            Test timeout (s)
            <input
              type="number"
              defaultValue={cfg.test_timeout_s}
              onBlur={(e) => save({ test_timeout_s: Number(e.target.value) })}
            />
          </label>
          <button onClick={check} disabled={checking || !!busy}>
            {checking ? "Checking…" : "Check connection"}
          </button>
          {report && (
            <ul className="tt-checks">
              {report.checks.map((c) => (
                <li key={c.name} className={c.ok === false ? "err" : c.ok ? "ok" : "grey"}>
                  {c.ok === false ? "✕" : c.ok ? "✓" : "–"} <b>{c.name}</b> {c.detail}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// ===========================================================================
// Chat (right pane)
// ===========================================================================
interface TTurn {
  role: "user" | "assistant";
  content: string;
  tools?: { name: string; args: Record<string, unknown>; preview?: string; error?: boolean }[];
  error?: string;
}

const CHAT_PREFS = "arch.targettests.chat.v1";

function ChatPanel({
  selected,
  toast,
}: {
  selected: TargetTestDetail | null;
  toast: (m: string) => void;
}) {
  const [providers, setProviders] = useState<AIProvider[]>([]);
  const [pick, setPick] = useState<{ providerId: string; model: string }>(() => {
    try {
      return JSON.parse(localStorage.getItem(CHAT_PREFS) || "") as { providerId: string; model: string };
    } catch {
      return { providerId: "", model: "" };
    }
  });
  const [allowWrites, setAllowWrites] = useState(false);
  const [messages, setMessages] = useState<TTurn[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    api
      .get<ProvidersResponse>("/ai/providers")
      .then((r) => {
        setProviders(r.providers);
        const usable = r.providers.filter((p) => p.configured && p.models.length);
        setPick((cur) =>
          usable.some((p) => p.id === cur.providerId && p.models.some((m) => m.id === cur.model)) ||
          !usable.length
            ? cur
            : { providerId: usable[0].id, model: usable[0].models[0].id },
        );
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    localStorage.setItem(CHAT_PREFS, JSON.stringify(pick));
  }, [pick]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const send = async (text: string) => {
    if (!text.trim() || !pick.providerId || !pick.model || streaming) return;
    const user: TTurn = { role: "user", content: text };
    let asst: TTurn = { role: "assistant", content: "", tools: [] };
    const base = [...messages, user];
    const sync = () => setMessages([...base, { ...asst }]);
    sync();
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamChat(
        {
          provider_id: pick.providerId,
          model: pick.model,
          messages: base.filter((m) => m.content).map((m) => ({ role: m.role, content: m.content })),
          live_target: true,
          allow_target_writes: allowWrites,
          target_test_id: selected?.id ?? null,
          use_tools: true,
          ground_in_mindmap: true,
        },
        (ev) => {
          switch (ev.kind) {
            case "tool":
              asst = { ...asst, tools: [...(asst.tools ?? []), { name: ev.data.name, args: ev.data.args }] };
              break;
            case "tool_result": {
              const ts = [...(asst.tools ?? [])];
              const i = ts.map((t) => t.name).lastIndexOf(ev.data.name);
              if (i >= 0) ts[i] = { ...ts[i], preview: ev.data.preview, error: ev.data.error };
              asst = { ...asst, tools: ts };
              break;
            }
            case "token":
              asst = { ...asst, content: asst.content + ev.data };
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
        },
        ctrl.signal,
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") asst = { ...asst, error: (e as Error).message };
      sync();
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const suggestions = selected
    ? ["Why does this test fail?", "Fix this test.", "Explain what this test checks on the ECU."]
    : [
        "Generate a test that the selected port's function is reached from main.",
        "How can I test requirement REQ-… on the ECU?",
        "Is Com_SendSignal ever called? Find out on the target.",
      ];

  return (
    <div className="tt-chat">
      <div className="tt-pane-head">
        <span>AI</span>
        <span className="tt-spacer" />
        <ModelSelect
          providers={providers}
          providerId={pick.providerId}
          model={pick.model}
          onChange={(providerId, model) => setPick({ providerId, model })}
        />
      </div>
      <label
        className={`tt-writes ${allowWrites ? "on" : ""}`}
        title="Lets the AI change variables on the target. Every write is logged."
      >
        <input type="checkbox" checked={allowWrites} onChange={(e) => setAllowWrites(e.target.checked)} />
        Allow memory writes
      </label>
      <div className="tt-chat-msgs">
        {messages.length === 0 && (
          <div className="tt-chat-empty">
            <p>
              The AI can read the code and drive the target: breakpoints, run, step, read
              {allowWrites ? " and write" : ""} variables. Tests it writes are saved as drafts for you to
              review.
            </p>
            {suggestions.map((s) => (
              <button key={s} className="tt-suggest" onClick={() => send(s)}>
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`tt-msg ${m.role}`}>
            {m.tools && m.tools.length > 0 && (
              <div className="tt-tools">
                {m.tools.map((t, j) => (
                  <div key={j} className={`tt-tool ${t.error ? "err" : ""}`} title={t.preview}>
                    {t.name.startsWith("target_") ? "⚙" : "🔎"} {t.name}
                    <span className="tt-muted"> {Object.values(t.args ?? {}).map(fmt).join(", ").slice(0, 60)}</span>
                  </div>
                ))}
              </div>
            )}
            {m.content &&
              (m.role === "assistant" ? (
                <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(m.content) }} />
              ) : (
                <div>{m.content}</div>
              ))}
            {m.error && <div className="tt-err">{m.error}</div>}
          </div>
        ))}
        {streaming && <div className="tt-muted tt-thinking">Working…</div>}
        <div ref={endRef} />
      </div>
      <div className="tt-chat-input">
        <textarea
          value={input}
          placeholder={selected ? `Ask about ${selected.test_key || "this test"}…` : "Ask the AI…"}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              const t = input;
              setInput("");
              send(t);
            }
          }}
        />
        {streaming ? (
          <button onClick={() => abortRef.current?.abort()}>Stop</button>
        ) : (
          <button
            className="primary"
            disabled={!input.trim()}
            onClick={() => {
              const t = input;
              setInput("");
              send(t);
            }}
          >
            Send
          </button>
        )}
        {messages.length > 0 && !streaming && (
          <button title="New conversation" onClick={() => setMessages([])}>
            Clear
          </button>
        )}
      </div>
    </div>
  );
}

// ===========================================================================
// The view
// ===========================================================================
export function TargetTests({ toast, canEdit }: { toast: (m: string) => void; canEdit: boolean }) {
  const theme = useMonacoTheme();
  const { confirm, confirmNode } = useConfirm();
  const [tests, setTests] = useState<TargetTestSummary[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [selId, setSelId] = useState<number | null>(null);
  const [detail, setDetail] = useState<TargetTestDetail | null>(null);
  const [code, setCode] = useState("");
  const [issues, setIssues] = useState<{ line: number; message: string }[]>([]);
  const [result, setResult] = useState<TargetTestResult | null>(null);
  const [runJob, setRunJob] = useState<JobPayload | null>(null);
  const [breakpoints, setBreakpoints] = useState<Set<number>>(new Set());
  const [watches, setWatches] = useState<string[]>([]);
  const [watchInput, setWatchInput] = useState("");
  const [debug, setDebug] = useState<{ session: string; jobId: string } | null>(null);
  const [paused, setPaused] = useState<{ line: number; snapshot: TargetSnapshot } | null>(null);
  const [debugLog, setDebugLog] = useState<string[]>([]);
  const [bottom, setBottom] = useState<"result" | "debug" | "history">("result");

  const editorRef = useRef<MonacoEditor | null>(null);
  const monacoRef = useRef<MonacoNs | null>(null);
  const decoRef = useRef<ReturnType<MonacoEditor["createDecorationsCollection"]> | null>(null);
  const bpRef = useRef(breakpoints);
  bpRef.current = breakpoints;

  const dirty = detail !== null && code !== detail.code;
  const running = runJob !== null && (runJob.status === "queued" || runJob.status === "running");

  // --- data ---------------------------------------------------------------
  const reloadList = useCallback(() => {
    api
      .get<{ tests: TargetTestSummary[]; busy: string | null }>("/target-tests")
      .then((r) => {
        setTests(r.tests);
        setBusy(r.busy);
      })
      .catch(() => {});
  }, []);

  const loadDetail = useCallback(
    async (id: number, keepCode = false) => {
      try {
        const d = await api.get<TargetTestDetail>(`/target-tests/${id}`);
        setDetail(d);
        if (!keepCode) setCode(d.code);
        setIssues(d.issues);
        if (d.results[0]) {
          setResult(await api.get<TargetTestResult>(`/target-tests/results/${d.results[0].id}`));
        } else setResult(null);
      } catch (e) {
        toast(`Target test: ${(e as Error).message}`);
      }
    },
    [toast],
  );

  useEffect(reloadList, [reloadList]);
  useEffect(() => {
    if (selId !== null) {
      setBreakpoints(new Set());
      loadDetail(selId);
    } else {
      setDetail(null);
      setCode("");
      setResult(null);
    }
  }, [selId, loadDetail]);

  // Validate the editor's code as it changes.
  useEffect(() => {
    if (!detail) return;
    const h = setTimeout(() => {
      api
        .post<{ issues: { line: number; message: string }[] }>("/target-tests/validate", { code })
        .then((r) => setIssues(r.issues))
        .catch(() => {});
    }, 400);
    return () => clearTimeout(h);
  }, [code, detail]);

  useSSE(
    useCallback(
      (e) => {
        if (e.event === "db-changed") {
          const reason = e.data.reason as string;
          if (reason === "target-tests-changed" || reason === "target-tests-run") {
            reloadList();
            if (selId !== null && (e.data.test_id === undefined || e.data.test_id === selId || reason === "target-tests-run")) {
              loadDetail(selId, dirty);
            }
          }
        } else if (e.event === "job") {
          const job = e.data as unknown as JobPayload;
          if (job.kind === "target_test_run") {
            setRunJob(job);
            if (job.status === "failed") toast(`Run: ${job.error}`);
            if (job.status !== "running" && job.status !== "queued") reloadList();
          } else if (job.kind === "target_test_debug" && debug && job.job_id === debug.jobId) {
            if (job.status === "failed") toast(`Debug: ${job.error}`);
            if (job.status !== "running" && job.status !== "queued") {
              type Raw = { verdict: TargetTestResult["verdict"]; message: string; duration_ms: number } & Pick<
                TargetTestResult["evidence"],
                "steps" | "expectations"
              >;
              const r = (job as JobPayload & { result?: { result?: Raw } }).result?.result;
              if (r) {
                setDebugLog((l) => [...l, `Finished: ${r.verdict} — ${r.message}`]);
                setResult({
                  id: 0, test_id: detail?.id ?? 0, version: detail?.version ?? 0, approved: false,
                  release_id: null, verdict: r.verdict, message: `Debug run (not recorded): ${r.message}`,
                  started_at: new Date().toISOString(), duration_ms: r.duration_ms, username: "",
                  target_info: {}, evidence: { steps: r.steps ?? [], expectations: r.expectations ?? [], checks: [] },
                });
              }
              setDebug(null);
              setPaused(null);
            }
          }
        } else if (e.event === "target-debug" && debug && e.data.session === debug.session) {
          const d = e.data as { type: string; line?: number; snapshot?: TargetSnapshot; message?: string; ok?: boolean };
          if (d.type === "paused" && d.line && d.snapshot) {
            setPaused({ line: d.line, snapshot: d.snapshot });
            setBottom("debug");
            editorRef.current?.revealLineInCenter(d.line);
          } else if (d.type === "checks" && !d.ok) {
            setDebugLog((l) => [...l, d.message ?? "Connection check failed."]);
          } else if (d.type === "started") {
            setDebugLog((l) => [...l, "Connected; target reset."]);
          }
        }
      },
      [reloadList, loadDetail, selId, dirty, toast, debug, detail],
    ),
  );

  // --- editor decorations: breakpoints + current debug line ---------------
  useEffect(() => {
    const ed = editorRef.current;
    const monaco = monacoRef.current;
    if (!ed || !monaco) return;
    const decos = [...breakpoints].map((line) => ({
      range: new monaco.Range(line, 1, line, 1),
      options: { glyphMarginClassName: "tt-bp-glyph", glyphMarginHoverMessage: { value: "Breakpoint" } },
    }));
    if (paused) {
      decos.push({
        range: new monaco.Range(paused.line, 1, paused.line, 1),
        options: { isWholeLine: true, className: "tt-paused-line", glyphMarginClassName: "tt-paused-glyph" } as never,
      });
    }
    for (const i of issues) {
      if (i.line > 0)
        decos.push({
          range: new monaco.Range(i.line, 1, i.line, 1),
          options: { isWholeLine: true, className: "tt-issue-line", hoverMessage: { value: i.message } } as never,
        });
    }
    if (!decoRef.current) decoRef.current = ed.createDecorationsCollection(decos);
    else decoRef.current.set(decos);
  }, [breakpoints, paused, issues, code, detail]);

  const onMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    decoRef.current = null;
    editor.onMouseDown((ev) => {
      const t = ev.target;
      if (
        t.type === monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN ||
        t.type === monaco.editor.MouseTargetType.GUTTER_LINE_NUMBERS
      ) {
        const line = t.position?.lineNumber;
        if (!line) return;
        const next = new Set(bpRef.current);
        if (next.has(line)) next.delete(line);
        else next.add(line);
        setBreakpoints(next);
      }
    });
  };

  // Tell a running debug session about breakpoint / watch changes.
  useEffect(() => {
    if (!debug) return;
    api
      .post(`/target-tests/debug/${debug.session}/command`, { breakpoints: [...breakpoints], watches })
      .catch(() => {});
  }, [breakpoints, watches, debug]);

  // --- actions ------------------------------------------------------------
  const guard = async (what: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
    } catch (e) {
      toast(`${what}: ${(e as ApiError).detail ?? (e as Error).message}`);
    }
  };

  const save = () =>
    guard("Save", async () => {
      if (!detail) return;
      const d = await api.put<TargetTestDetail>(`/target-tests/${detail.id}`, { code });
      setDetail(d);
      setCode(d.code);
      toast("Saved and approved by you.");
    });

  const act = (path: string, label: string, done?: string) =>
    guard(label, async () => {
      if (!detail) return;
      const d = await api.post<TargetTestDetail>(`/target-tests/${detail.id}/${path}`);
      setDetail(d);
      setCode(d.code);
      if (done) toast(done);
    });

  const create = () =>
    guard("New test", async () => {
      const d = await api.post<TargetTestDetail>("/target-tests", { title: "New test" });
      reloadList();
      setSelId(d.id);
    });

  const remove = async () => {
    if (!detail) return;
    const ok = await confirm({
      title: `Delete ${detail.test_key || "this test"}?`,
      message: "The test, its versions and its results are removed from the project.",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    guard("Delete", async () => {
      await api.del(`/target-tests/${detail.id}`);
      setSelId(null);
      reloadList();
    });
  };

  const run = (ids: number[]) =>
    guard("Run", async () => {
      if (!ids.length) return;
      if (dirty && detail && ids.includes(detail.id)) {
        const ok = await confirm({
          title: "Save before running?",
          message: "Runs use the saved script. Saving your changes also approves them.",
          confirmLabel: "Save and run",
        });
        if (!ok) return;
        await save();
      }
      const r = await api.post<{ job_id: string }>("/target-tests/run", { test_ids: ids });
      setRunJob({ job_id: r.job_id, kind: "target_test_run", status: "queued", progress: null, message: "Starting…" });
      setBottom("result");
    });

  const stopRun = () => runJob && guard("Stop", () => api.post(`/jobs/${runJob.job_id}/cancel`));

  const startDebug = (pauseAtStart: boolean) =>
    guard("Debug", async () => {
      const r = await api.post<{ job_id: string; session: string }>("/target-tests/debug", {
        code,
        breakpoints: [...breakpoints],
        watches,
        pause_at_start: pauseAtStart,
      });
      setDebug({ session: r.session, jobId: r.job_id });
      setPaused(null);
      setDebugLog(["Checking the connection…"]);
      setBottom("debug");
    });

  const debugCmd = (action: string) =>
    debug &&
    guard("Debug", async () => {
      await api.post(`/target-tests/debug/${debug.session}/command`, { action });
      if (action !== "snapshot") setPaused(null);
    });

  // --- list ---------------------------------------------------------------
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return tests.filter(
      (t) => !f || `${t.test_key} ${t.title} ${t.port} ${t.requirement}`.toLowerCase().includes(f),
    );
  }, [tests, filter]);

  const toggle = (id: number) => {
    const next = new Set(checked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setChecked(next);
  };

  const proposal = detail?.pending_code ? detail : null;

  return (
    <div className="tt-root">
      {confirmNode}
      {/* ---------------- left: tests ---------------- */}
      <div className="tt-list">
        <div className="tt-pane-head">
          <span>Target Tests</span>
          <span className="tt-spacer" />
          <button className="tt-mini" disabled={!canEdit} onClick={create} title="New test (yours: approved)">
            ＋
          </button>
        </div>
        <input
          className="tt-filter"
          placeholder="Filter by ID, title, port, requirement"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <div className="tt-items">
          {shown.length === 0 && (
            <div className="tt-empty">
              {tests.length ? "No test matches." : "No tests yet. Ask the AI on the right to generate one, or click ＋."}
            </div>
          )}
          {shown.map((t) => (
            <div
              key={t.id}
              className={`tt-item ${t.id === selId ? "sel" : ""}`}
              onClick={() => setSelId(t.id)}
            >
              <input
                type="checkbox"
                checked={checked.has(t.id)}
                onClick={(e) => e.stopPropagation()}
                onChange={() => toggle(t.id)}
              />
              <div className="tt-item-main">
                <div className="tt-item-title">
                  <b>{t.test_key || `#${t.id}`}</b> {t.title}
                </div>
                <div className="tt-item-sub">
                  {t.port && <span className="tt-muted">{t.port}</span>}
                  <StatusBadge t={t} />
                  <VerdictBadge r={t.last_result} />
                </div>
              </div>
            </div>
          ))}
        </div>
        <div className="tt-list-foot">
          <button
            disabled={running || !!debug || checked.size === 0}
            onClick={() => run([...checked])}
            title="Run the checked tests, each from a reset"
          >
            ▶▶ Run selected ({checked.size})
          </button>
          <button
            disabled={running || !!debug || tests.length === 0}
            onClick={() => run(tests.filter((t) => t.status === "approved").map((t) => t.id))}
            title="Run every approved test"
          >
            Run approved
          </button>
        </div>
        <ConnectionPanel toast={toast} busy={busy} />
      </div>

      {/* ---------------- middle: script ---------------- */}
      <div className="tt-main">
        {!detail ? (
          <div className="center-msg">Select a test on the left, or ask the AI to generate one.</div>
        ) : (
          <>
            <div className="tt-toolbar">
              <div className="tt-title">
                <b>{detail.test_key || `#${detail.id}`}</b> {detail.title}
                <span className="tt-muted"> v{detail.version}</span>
                <StatusBadge t={detail} />
                {dirty && <span className="tt-badge grey">Unsaved</span>}
              </div>
              <span className="tt-spacer" />
              {running ? (
                <button onClick={stopRun} title="Stop the run">■ Stop</button>
              ) : (
                <button
                  className="primary"
                  disabled={!!debug || issues.length > 0}
                  onClick={() => run([detail.id])}
                  title="Run this test from a reset and record the result"
                >
                  ▶ Run
                </button>
              )}
              {debug ? (
                <button onClick={() => debugCmd("stop")}>■ Stop debug</button>
              ) : (
                <button
                  disabled={running || issues.length > 0}
                  onClick={() => startDebug(breakpoints.size === 0)}
                  title="Run the editor's script and pause at its breakpoints (click the gutter to set one). Without breakpoints it pauses on the first line. Nothing is recorded."
                >
                  🐞 Debug
                </button>
              )}
              <button disabled={!canEdit || !dirty} onClick={save} title="Save your correction; it's approved by you">
                Save
              </button>
              {detail.status === "draft" && !proposal && (
                <button
                  disabled={!canEdit || dirty || issues.length > 0}
                  onClick={() => act("approve", "Approve", "Approved.")}
                  title="Approve the AI's version so its runs count as verdicts"
                >
                  ✓ Approve
                </button>
              )}
              {detail.status === "draft" && detail.approved_version && (
                <button disabled={!canEdit} onClick={() => act("revert", "Revert", "Reverted to the approved version.")}>
                  Revert to approved
                </button>
              )}
              <button className="tt-mini" disabled={!canEdit} onClick={remove} title="Delete the test">
                🗑
              </button>
            </div>

            {proposal ? (
              <div className="tt-proposal">
                <div className="tt-proposal-bar">
                  <b>The AI proposes a change.</b>
                  <span className="tt-muted">
                    {" "}
                    Accepting makes it a draft; you approve it separately.
                  </span>
                  <span className="tt-spacer" />
                  <button className="primary" disabled={!canEdit} onClick={() => act("proposal/accept", "Accept")}>
                    Accept change
                  </button>
                  <button disabled={!canEdit} onClick={() => act("proposal/reject", "Reject")}>
                    Reject
                  </button>
                </div>
                {proposal.pending_rationale && <div className="tt-rationale">{proposal.pending_rationale}</div>}
                <div className="tt-editor">
                  <DiffEditor
                    language="python"
                    theme={theme}
                    original={proposal.code}
                    modified={proposal.pending_code}
                    keepCurrentOriginalModel
                    keepCurrentModifiedModel
                    options={{ readOnly: true, renderSideBySide: false, minimap: { enabled: false }, fontSize: 12, automaticLayout: true }}
                  />
                </div>
              </div>
            ) : (
              <>
                {detail.rationale && detail.origin === "ai" && (
                  <div className="tt-rationale" title="Why the AI wrote these steps">
                    <b>AI rationale:</b> {detail.rationale}
                  </div>
                )}
                <div className="tt-editor">
                  <Editor
                    language="python"
                    theme={theme}
                    value={code}
                    onChange={(v) => setCode(v ?? "")}
                    onMount={onMount}
                    options={{
                      readOnly: !canEdit,
                      minimap: { enabled: false },
                      fontSize: 12,
                      glyphMargin: true,
                      lineNumbers: "on",
                      scrollBeyondLastLine: false,
                      automaticLayout: true,
                      fixedOverflowWidgets: true,
                    }}
                  />
                </div>
              </>
            )}
            {issues.length > 0 && (
              <div className="tt-issues">
                {issues.map((i, k) => (
                  <div key={k}>
                    ⚠ line {i.line}: {i.message}
                  </div>
                ))}
              </div>
            )}

            {/* ------------- bottom: results / debug / history ------------- */}
            <div className="tt-bottom">
              <div className="tt-tabs">
                {(["result", "debug", "history"] as const).map((k) => (
                  <button key={k} className={bottom === k ? "active" : ""} onClick={() => setBottom(k)}>
                    {k === "result" ? "Result" : k === "debug" ? "Debug" : "History"}
                  </button>
                ))}
                <span className="tt-spacer" />
                {running && <span className="tt-muted">{runJob?.message}</span>}
              </div>
              {bottom === "result" && (
                <div className="tt-bottom-body">
                  {!result ? (
                    <div className="tt-muted">Not run yet. ▶ Run checks the connection, resets the target and runs the test.</div>
                  ) : (
                    <>
                      <div className="tt-result-head">
                        <VerdictBadge r={result} /> {result.message}
                        <span className="tt-muted">
                          {" "}
                          · v{result.version} · {result.started_at ? new Date(result.started_at).toLocaleString() : ""} ·{" "}
                          {result.duration_ms} ms {result.username && `· ${result.username}`}
                          {result.target_info?.winidea_version && ` · winIDEA ${result.target_info.winidea_version}`}
                        </span>
                      </div>
                      <table className="tt-table">
                        <thead>
                          <tr>
                            <th>Line</th>
                            <th>Step</th>
                            <th>Result</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(result.evidence?.steps ?? []).map((s, k) => {
                            const exp = s.call.startsWith("expect.")
                              ? (s.result as { ok?: boolean; actual?: unknown } | undefined)
                              : undefined;
                            return (
                              <tr
                                key={k}
                                className={exp ? (exp.ok ? "ok" : "err") : s.error ? "err" : ""}
                                onClick={() => s.line && editorRef.current?.revealLineInCenter(s.line)}
                              >
                                <td>{s.line || ""}</td>
                                <td>
                                  <code>
                                    {s.call}({s.args.map(fmt).join(", ")})
                                  </code>
                                </td>
                                <td>
                                  {exp ? (exp.ok ? "✓ " : "✕ ") + fmt(exp.actual) : s.error ?? (s.result !== undefined ? fmt(s.result) : "")}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </>
                  )}
                </div>
              )}
              {bottom === "debug" && (
                <div className="tt-bottom-body tt-debug">
                  <div className="tt-debug-bar">
                    <button disabled={!paused} onClick={() => debugCmd("continue")}>▶ Continue</button>
                    <button disabled={!paused} onClick={() => debugCmd("step")}>⤵ Step</button>
                    <button disabled={!debug} onClick={() => debugCmd("stop")}>■ Stop</button>
                    <span className="tt-muted">
                      {debug ? (paused ? `Paused before line ${paused.line}` : "Running…") : "Click the gutter to set breakpoints, then 🐞 Debug."}
                    </span>
                  </div>
                  <div className="tt-debug-grid">
                    <div>
                      <div className="tt-sub">Target</div>
                      {paused ? (
                        <div>
                          {paused.snapshot.status.state}
                          {paused.snapshot.status.function && ` in ${paused.snapshot.status.function}`}
                          {paused.snapshot.status.file && ` (${paused.snapshot.status.file}:${paused.snapshot.status.line})`}
                        </div>
                      ) : (
                        <div className="tt-muted">—</div>
                      )}
                      <div className="tt-sub">Call stack</div>
                      {paused?.snapshot.call_stack.length ? (
                        <ol className="tt-stack">
                          {[...paused.snapshot.call_stack].reverse().map((f, k) => (
                            <li key={k}>{f}</li>
                          ))}
                        </ol>
                      ) : (
                        <div className="tt-muted">{paused?.snapshot.call_stack_error ?? "—"}</div>
                      )}
                    </div>
                    <div>
                      <div className="tt-sub">Watches</div>
                      {watches.map((w) => (
                        <div key={w} className="tt-watch">
                          <code>{w}</code> = <b>{paused?.snapshot.watches[w] ?? "…"}</b>
                          <button className="tt-mini" onClick={() => setWatches(watches.filter((x) => x !== w))}>
                            ✕
                          </button>
                        </div>
                      ))}
                      <input
                        placeholder="Add a watch (variable or expression)"
                        value={watchInput}
                        onChange={(e) => setWatchInput(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && watchInput.trim()) {
                            setWatches([...watches.filter((w) => w !== watchInput.trim()), watchInput.trim()]);
                            setWatchInput("");
                          }
                        }}
                      />
                      {paused && Object.keys(paused.snapshot.registers).length > 0 && (
                        <>
                          <div className="tt-sub">Registers</div>
                          {Object.entries(paused.snapshot.registers).map(([k, v]) => (
                            <div key={k}>
                              <code>{k}</code> {v}
                            </div>
                          ))}
                        </>
                      )}
                    </div>
                  </div>
                  {debugLog.length > 0 && (
                    <div className="tt-debug-log">
                      {debugLog.map((l, k) => (
                        <div key={k}>{l}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {bottom === "history" && (
                <div className="tt-bottom-body">
                  <div className="tt-sub">Versions</div>
                  <table className="tt-table">
                    <tbody>
                      {detail.versions.map((v) => (
                        <tr key={v.version}>
                          <td>v{v.version}</td>
                          <td>{v.origin === "ai" ? "AI" : v.origin === "revert" ? "Revert" : "Manual"}</td>
                          <td>{v.author}</td>
                          <td>{v.note}</td>
                          <td>{new Date(v.created_at).toLocaleString()}</td>
                          <td>{v.version === detail.approved_version && <span className="tt-badge ok">Approved</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="tt-sub">Runs</div>
                  <table className="tt-table">
                    <tbody>
                      {detail.results.map((r) => (
                        <tr
                          key={r.id}
                          onClick={() =>
                            api.get<TargetTestResult>(`/target-tests/results/${r.id}`).then((x) => {
                              setResult(x);
                              setBottom("result");
                            })
                          }
                        >
                          <td>
                            <VerdictBadge r={r} />
                          </td>
                          <td>v{r.version}</td>
                          <td>{new Date(r.started_at).toLocaleString()}</td>
                          <td>{r.username}</td>
                          <td>{r.message}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {detail.approved_by && (
                    <div className="tt-muted">
                      Approved by {detail.approved_by} on {new Date(detail.approved_at).toLocaleString()}.
                    </div>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* ---------------- right: AI ---------------- */}
      <ChatPanel selected={detail} toast={toast} />
    </div>
  );
}
