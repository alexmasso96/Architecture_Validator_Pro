import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { AgentsResponse, ChatAgent, ChatRule } from "../api/types";
import { FolderPicker } from "./FolderPicker";

// Agents & Rules setup, the way VS Code's Copilot does it: custom agents
// (.agent.md — instructions, a tool list, a preferred model) and rules
// (.instructions.md — applied to every request, or only when the files in view
// match their applyTo globs). Built-in agents are read-only; the project's
// agents and rules are saved in the project, so the team shares them. Import
// reads a checkout's .github folder; Export writes the same files back.

const TOOL_LABELS: Record<string, string> = {
  read_file: "Read files",
  list_files: "List files",
  search_code: "Search the code",
  get_function: "Function details",
  get_call_graph: "Call graph",
  get_mind_map: "Mind map",
  get_requirements: "Requirements",
  get_diff: "Diff vs previous release",
};

type Sel = { kind: "agent" | "rule" | "project"; id: string } | null;

function blankAgent(n: number): ChatAgent {
  return {
    id: `new:${Date.now()}:${n}`,
    name: `New agent ${n}`,
    description: "",
    instructions: "",
    tools: null,
    model: "",
    source: "project",
  };
}

function blankRule(n: number): ChatRule {
  return {
    id: `new:${Date.now()}:${n}`,
    name: `rules-${n}`,
    description: "",
    apply_to: "",
    body: "",
    enabled: true,
  };
}

export function AgentsDialog({
  initial,
  canEdit,
  onClose,
  onSaved,
  toast,
}: {
  initial: AgentsResponse;
  canEdit: boolean;
  onClose: () => void;
  onSaved: (r: AgentsResponse) => void;
  toast: (msg: string) => void;
}) {
  const builtins = useMemo(
    () => initial.agents.filter((a) => a.source === "builtin"),
    [initial],
  );
  const [agents, setAgents] = useState<ChatAgent[]>(
    initial.agents.filter((a) => a.source !== "builtin"),
  );
  const [rules, setRules] = useState<ChatRule[]>(initial.rules);
  const [sel, setSel] = useState<Sel>({ kind: "agent", id: initial.agents[0]?.id ?? "" });
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [picker, setPicker] = useState<"import" | "export" | null>(null);
  // The project's own chat rules (applied to every agent and request).
  const [projectRules, setProjectRules] = useState<string | null>(null);
  const [projectDirty, setProjectDirty] = useState(false);

  useEffect(() => {
    api
      .get<{ chat_rules: string }>("/ai/prompts")
      .then((r) => setProjectRules(r.chat_rules))
      .catch(() => setProjectRules(null));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !picker) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, picker]);

  const allAgents = [...builtins, ...agents];
  const agent = sel?.kind === "agent" ? allAgents.find((a) => a.id === sel.id) : undefined;
  const rule = sel?.kind === "rule" ? rules.find((r) => r.id === sel.id) : undefined;
  const readOnly = !canEdit || agent?.source === "builtin";

  const patchAgent = (p: Partial<ChatAgent>) => {
    if (!agent || agent.source === "builtin") return;
    setAgents((xs) => xs.map((a) => (a.id === agent.id ? { ...a, ...p } : a)));
    setDirty(true);
  };
  const patchRule = (p: Partial<ChatRule>) => {
    if (!rule) return;
    setRules((xs) => xs.map((r) => (r.id === rule.id ? { ...r, ...p } : r)));
    setDirty(true);
  };

  async function save(): Promise<AgentsResponse | null> {
    setBusy(true);
    try {
      if (projectDirty && projectRules !== null) {
        await api.put("/ai/prompts", { chat_rules: projectRules });
        setProjectDirty(false);
      }
      await api.put<AgentsResponse>("/ai/agents", {
        agents: agents.map((a) => (a.id.startsWith("new:") ? { ...a, id: "" } : a)),
      });
      const r = await api.put<AgentsResponse>("/ai/rules", {
        rules: rules.map((x) => (x.id.startsWith("new:") ? { ...x, id: "" } : x)),
      });
      const savedAgents = r.agents.filter((a) => a.source !== "builtin");
      // Keep the selection: new items get their permanent ids on save.
      if (sel?.kind === "agent" && sel.id.startsWith("new:")) {
        const i = agents.findIndex((a) => a.id === sel.id);
        if (savedAgents[i]) setSel({ kind: "agent", id: savedAgents[i].id });
      } else if (sel?.kind === "rule" && sel.id.startsWith("new:")) {
        const i = rules.findIndex((x) => x.id === sel.id);
        if (r.rules[i]) setSel({ kind: "rule", id: r.rules[i].id });
      }
      setAgents(savedAgents);
      setRules(r.rules);
      setDirty(false);
      onSaved(r);
      toast("Agents and rules saved to the project.");
      return r;
    } catch (e) {
      toast(`Couldn't save: ${(e as Error).message}`);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function doImport(path: string) {
    setPicker(null);
    try {
      const r = await api.post<{ agents: ChatAgent[]; rules: ChatRule[]; truncated: boolean }>(
        "/ai/agents/import",
        { path },
      );
      if (!r.agents.length && !r.rules.length) {
        toast("No .agent.md, .chatmode.md, .instructions.md or AGENTS.md files found there.");
        return;
      }
      const stamp = Date.now();
      setAgents((xs) => {
        const byName = new Map(xs.map((a) => [a.name.toLowerCase(), a]));
        for (const [i, a] of r.agents.entries()) {
          const old = byName.get(a.name.toLowerCase());
          byName.set(a.name.toLowerCase(), { ...a, id: old?.id ?? `new:${stamp}:a${i}` });
        }
        return [...byName.values()];
      });
      setRules((xs) => {
        const byName = new Map(xs.map((x) => [x.name.toLowerCase(), x]));
        for (const [i, x] of r.rules.entries()) {
          const old = byName.get(x.name.toLowerCase());
          byName.set(x.name.toLowerCase(), { ...x, id: old?.id ?? `new:${stamp}:r${i}` });
        }
        return [...byName.values()];
      });
      setDirty(true);
      toast(
        `Imported ${r.agents.length} agent(s) and ${r.rules.length} rule file(s) — Save to keep them.`,
      );
    } catch (e) {
      toast(`Import failed: ${(e as Error).message}`);
    }
  }

  async function doExport(path: string) {
    setPicker(null);
    if (dirty && !(await save())) return;
    try {
      const r = await api.post<{ written: string[] }>("/ai/agents/export", { path });
      toast(`Wrote ${r.written.length} file(s) under ${path}.`);
    } catch (e) {
      toast(`Export failed: ${(e as Error).message}`);
    }
  }

  const toolsChecked = (t: string) => !agent?.tools || agent.tools.includes(t);
  const toggleTool = (t: string) => {
    if (!agent) return;
    const cur = agent.tools ?? [...initial.tools];
    const next = cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t];
    patchAgent({ tools: next.length === initial.tools.length ? null : next });
  };

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal agents-modal" role="dialog" aria-label="Agents and rules">
        <div className="agents-head">
          <div>
            <div className="agents-title">Agents &amp; Rules</div>
            <div className="agents-sub">
              Set up like VS Code's Copilot: agents are <code>.agent.md</code> files, rules are{" "}
              <code>.instructions.md</code> files. Saved in the project for everyone on it.
            </div>
          </div>
          <div className="agents-head-actions">
            <button className="scope-btn" onClick={() => setPicker("import")} disabled={!canEdit}>
              Import from folder…
            </button>
            <button className="scope-btn" onClick={() => setPicker("export")}>
              Export to folder…
            </button>
          </div>
        </div>
        {!canEdit && (
          <div className="agents-ro">
            The project is open read-only (or not open): agents and rules can be used but not changed.
          </div>
        )}
        <div className="agents-body">
          <aside className="agents-list">
            <div className="agents-list-h">
              Agents
              <button
                className="tci-icon-btn"
                title="New agent"
                disabled={!canEdit}
                onClick={() => {
                  const a = blankAgent(agents.length + 1);
                  setAgents((xs) => [...xs, a]);
                  setSel({ kind: "agent", id: a.id });
                  setDirty(true);
                }}
              >
                ＋
              </button>
            </div>
            {allAgents.map((a) => (
              <button
                key={a.id}
                className={"agents-item" + (sel?.id === a.id ? " sel" : "")}
                onClick={() => setSel({ kind: "agent", id: a.id })}
              >
                <span className="agents-item-name">{a.name}</span>
                <span className="agents-item-tag">{a.source === "builtin" ? "built-in" : "project"}</span>
              </button>
            ))}
            <div className="agents-list-h">
              Rules
              <button
                className="tci-icon-btn"
                title="New rules file"
                disabled={!canEdit}
                onClick={() => {
                  const r = blankRule(rules.length + 1);
                  setRules((xs) => [...xs, r]);
                  setSel({ kind: "rule", id: r.id });
                  setDirty(true);
                }}
              >
                ＋
              </button>
            </div>
            {projectRules !== null && (
              <button
                className={"agents-item" + (sel?.kind === "project" ? " sel" : "")}
                onClick={() => setSel({ kind: "project", id: "project" })}
              >
                <span className="agents-item-name">Project rules</span>
                <span className="agents-item-tag">always</span>
              </button>
            )}
            {rules.length === 0 && <div className="agents-empty">No rule files yet.</div>}
            {rules.map((r) => (
              <button
                key={r.id}
                className={"agents-item" + (sel?.id === r.id ? " sel" : "") + (r.enabled ? "" : " off")}
                onClick={() => setSel({ kind: "rule", id: r.id })}
              >
                <span className="agents-item-name">{r.name}</span>
                <span className="agents-item-tag">{r.apply_to || "always"}</span>
              </button>
            ))}
          </aside>

          <section className="agents-edit">
            {agent && (
              <>
                <label className="aig-field">
                  <span>Name</span>
                  <input value={agent.name} disabled={readOnly}
                    onChange={(e) => patchAgent({ name: e.target.value })} />
                </label>
                <label className="aig-field">
                  <span>Description</span>
                  <input value={agent.description} disabled={readOnly}
                    placeholder="Shown in the agent picker"
                    onChange={(e) => patchAgent({ description: e.target.value })} />
                </label>
                <label className="aig-field">
                  <span>Preferred model (optional)</span>
                  <input value={agent.model} disabled={readOnly}
                    placeholder="e.g. gpt-4.1 or Claude Sonnet 4 — picked when you choose this agent"
                    onChange={(e) => patchAgent({ model: e.target.value })} />
                </label>
                <div className="aig-field">
                  <span>Tools</span>
                  <div className="agents-tools">
                    {initial.tools.map((t) => (
                      <label key={t} className="agents-tool">
                        <input type="checkbox" checked={toolsChecked(t)} disabled={readOnly}
                          onChange={() => toggleTool(t)} />
                        {TOOL_LABELS[t] ?? t}
                      </label>
                    ))}
                  </div>
                </div>
                <label className="aig-field agents-grow">
                  <span>Instructions</span>
                  <textarea value={agent.instructions} disabled={readOnly} spellCheck={false}
                    placeholder="What this agent does and how it answers (Markdown)."
                    onChange={(e) => patchAgent({ instructions: e.target.value })} />
                </label>
                {agent.source !== "builtin" && canEdit && (
                  <button className="scope-btn danger agents-del" onClick={() => {
                    setAgents((xs) => xs.filter((a) => a.id !== agent.id));
                    setSel({ kind: "agent", id: builtins[0]?.id ?? "" });
                    setDirty(true);
                  }}>
                    Delete agent
                  </button>
                )}
              </>
            )}
            {sel?.kind === "project" && projectRules !== null && (
              <label className="aig-field agents-grow">
                <span>Project rules — sent with every request, whatever the agent
                  (VS Code's copilot-instructions.md)</span>
                <textarea value={projectRules} disabled={!canEdit} spellCheck={false}
                  onChange={(e) => {
                    setProjectRules(e.target.value);
                    setProjectDirty(true);
                    setDirty(true);
                  }} />
              </label>
            )}
            {rule && (
              <>
                <label className="aig-field">
                  <span>Name</span>
                  <input value={rule.name} disabled={!canEdit}
                    onChange={(e) => patchRule({ name: e.target.value })} />
                </label>
                <label className="aig-field">
                  <span>Apply to (applyTo globs, comma-separated — empty = every request)</span>
                  <input value={rule.apply_to} disabled={!canEdit} placeholder="**/*.c, drivers/**/*.h"
                    onChange={(e) => patchRule({ apply_to: e.target.value })} />
                </label>
                <label className="chat-ground">
                  <input type="checkbox" checked={rule.enabled} disabled={!canEdit}
                    onChange={(e) => patchRule({ enabled: e.target.checked })} />
                  Enabled
                </label>
                <label className="aig-field agents-grow">
                  <span>Rules</span>
                  <textarea value={rule.body} disabled={!canEdit} spellCheck={false}
                    placeholder="Coding standards, naming, what to check… (Markdown)."
                    onChange={(e) => patchRule({ body: e.target.value })} />
                </label>
                {canEdit && (
                  <button className="scope-btn danger agents-del" onClick={() => {
                    setRules((xs) => xs.filter((r) => r.id !== rule.id));
                    setSel({ kind: "agent", id: builtins[0]?.id ?? "" });
                    setDirty(true);
                  }}>
                    Delete rules
                  </button>
                )}
              </>
            )}
          </section>
        </div>
        <div className="agents-foot">
          <span className="agents-hint">
            Rules apply on top of the agent: always, or when the file open in the editor matches.
          </span>
          <button className="scope-btn" onClick={onClose}>{dirty ? "Discard" : "Close"}</button>
          <button className="save-btn" disabled={!dirty || busy || !canEdit} onClick={save}>
            Save
          </button>
        </div>
      </div>
      {picker && (
        <FolderPicker
          mode="folder"
          title={picker === "import"
            ? "Import agents and rules — choose a checkout (or its .github folder)"
            : "Export agents and rules — choose the checkout to write .github/ into"}
          onCancel={() => setPicker(null)}
          onConfirm={(path) => (picker === "import" ? doImport(path) : doExport(path))}
        />
      )}
    </div>
  );
}
