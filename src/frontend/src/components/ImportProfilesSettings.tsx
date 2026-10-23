import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { ColumnsResponse } from "../api/types";
import { FolderPicker } from "./FolderPicker";
import type { ImportProfile } from "./ProfileImportDialog";

// #8 Phase D: the Import Profiles editor (Preferences → Import Profiles).
// Left: profile list. Right: pipeline editor + live preview backed by an
// in-memory sample session — the sample file is read ONCE; every preview
// refresh afterwards is pure in-memory computation (EDR-friendly).

type Rule = Record<string, any>;

interface SampleInfo {
  sample_id: string;
  sheets: string[];
  sheet: string;
  header_row: number;
  columns: string[];
  row_count: number;
  truncated: boolean;
  rows: Record<string, string>[];
}

interface PreviewResp {
  stages: { index: number; type: string; rows_in: number; rows_out: number }[];
  columns: string[];
  partition_column: string | null;
  partitions: Record<string, number>;
  row_count: number;
  truncated: boolean;
  rows: Record<string, string>[];
  mapped_rows?: Record<string, { text: string; origin?: string }>[];
}

const CONDITION_OPS = [
  "contains", "not_contains", "equals", "not_equals",
  "starts_with", "ends_with", "regex", "is_empty", "not_empty",
];
const CLEAN_OPS = ["trim", "upper", "lower", "replace", "fill_down"];

function emptyRule(type: string): Rule {
  switch (type) {
    case "filter":
      return { type, action: "keep", match: "all",
               conditions: [{ column: "", op: "contains", value: "" }] };
    case "derive":
      return { type, target: "", default: "",
               cases: [{ column: "", op: "contains", value: "", result: "" }] };
    case "split_cell":
      return { type, column: "", delimiter: "::", segment: 0, target: "" };
    case "expand":
      return { type, column: "", delimiter: "\n", take_before: "" };
    case "partition":
      return { type, column: "" };
    default:
      return { type: "clean",
               ops: [{ op: "trim", column: "*", find: "", replace: "" }] };
  }
}

function newProfile(): ImportProfile {
  return { id: "", name: "New profile", filename_pattern: "", mode: "append",
           key_columns: [], rules: [], mapping: {}, locked_targets: [] };
}

export function ImportProfilesSettings() {
  const [profiles, setProfiles] = useState<ImportProfile[]>([]);
  const [draft, setDraft] = useState<ImportProfile | null>(null);
  const [sample, setSample] = useState<SampleInfo | null>(null);
  const [preview, setPreview] = useState<PreviewResp | null>(null);
  const [live, setLive] = useState(true);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [renderAll, setRenderAll] = useState<{
    rows: Record<string, string>[];
    columns: string[];
    total: number;
    offset: number;
  } | null>(null);
  const [targets, setTargets] = useState<string[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const sampleRef = useRef<string | null>(null);

  const readOnly = Boolean(draft?.builtin);

  const loadProfiles = useCallback(() => {
    api.get<{ profiles: ImportProfile[] }>("/import/profiles")
      .then((r) => setProfiles(r.profiles))
      .catch(() => setProfiles([]));
  }, []);

  useEffect(() => {
    loadProfiles();
    // Target table columns for the mapping dropdowns (best-effort: a project
    // must be open; otherwise the mapping stays free-text via "custom").
    api.get<ColumnsResponse>("/columns")
      .then((r) => setTargets(r.columns.map((c) => c.name)))
      .catch(() => setTargets([]));
    return () => {
      if (sampleRef.current) {
        api.del(`/import/sample/${sampleRef.current}`).catch(() => {});
      }
    };
  }, [loadProfiles]);

  // Debounced live preview — pure in-memory on the server, so "live" is safe
  // even on EDR machines. The manual Refresh button covers live=off.
  const refreshPreview = useCallback(async () => {
    if (!sample || !draft) return;
    try {
      const r = await api.post<PreviewResp>("/import/preview", {
        sample_id: sample.sample_id,
        rules: draft.rules,
        mapping: Object.keys(draft.mapping).length ? draft.mapping : null,
        locked_targets: draft.locked_targets,
        limit: 10,
      });
      setPreview(r);
      setMsg(null);
    } catch (e) {
      setMsg(`Preview failed: ${(e as Error).message}`);
    }
  }, [sample, draft]);

  useEffect(() => {
    if (!live || !sample || !draft) return;
    const t = setTimeout(refreshPreview, 400);
    return () => clearTimeout(t);
  }, [live, sample, draft, refreshPreview]);

  async function loadSample(path: string) {
    setPickerOpen(false);
    try {
      if (sampleRef.current) {
        api.del(`/import/sample/${sampleRef.current}`).catch(() => {});
      }
      const s = await api.post<SampleInfo>("/import/sample", {
        path,
        sheet: draft?.sheet ?? null,
        header_row: draft?.header_row ?? null,
      });
      sampleRef.current = s.sample_id;
      setSample(s);
      setPreview(null);
      setMsg(null);
    } catch (e) {
      setMsg(`Sample load failed: ${(e as Error).message}`);
    }
  }

  async function showAll(offset = 0) {
    if (!sample || !draft) return;
    try {
      const r = await api.post<{
        rows: Record<string, string>[];
        columns: string[];
        total: number;
        offset: number;
      }>("/import/preview/all", {
        sample_id: sample.sample_id, rules: draft.rules, offset, limit: 200,
      });
      setRenderAll(r);
    } catch (e) {
      setMsg(`Render failed: ${(e as Error).message}`);
    }
  }

  async function save() {
    if (!draft || readOnly) return;
    try {
      const body = { ...draft, id: draft.id || undefined };
      const r = await api.post<{ profile: ImportProfile }>("/import/profiles", body);
      setDraft(r.profile);
      loadProfiles();
      setMsg("Profile saved.");
    } catch (e) {
      setMsg(`Save failed: ${(e as Error).message}`);
    }
  }

  async function remove(p: ImportProfile) {
    if (p.builtin) return;
    try {
      await api.del(`/import/profiles/${p.id}`);
      if (draft?.id === p.id) setDraft(null);
      loadProfiles();
    } catch (e) {
      setMsg(`Delete failed: ${(e as Error).message}`);
    }
  }

  function duplicate(p: ImportProfile) {
    setDraft({ ...structuredClone(p), id: "", builtin: false,
               name: `${p.name} (copy)` });
  }

  function patchDraft(patch: Partial<ImportProfile>) {
    setDraft((d) => (d ? { ...d, ...patch } : d));
  }

  function patchRule(i: number, patch: Rule) {
    setDraft((d) => {
      if (!d) return d;
      const rules = d.rules.slice();
      rules[i] = { ...rules[i], ...patch };
      return { ...d, rules };
    });
  }

  function moveRule(i: number, dir: -1 | 1) {
    setDraft((d) => {
      if (!d) return d;
      const j = i + dir;
      if (j < 0 || j >= d.rules.length) return d;
      const rules = d.rules.slice();
      [rules[i], rules[j]] = [rules[j], rules[i]];
      return { ...d, rules };
    });
  }

  // Column candidates: sample columns + derived ones the pipeline created.
  const colOptions = useMemo(() => {
    const s = new Set<string>(sample?.columns ?? []);
    for (const c of preview?.columns ?? []) s.add(c);
    for (const r of draft?.rules ?? []) {
      if (typeof r.target === "string" && r.target) s.add(r.target);
    }
    return [...s];
  }, [sample, preview, draft]);

  const mappedTargets = Object.values(draft?.mapping ?? {});

  function colSelect(value: string, onPick: (v: string) => void) {
    return (
      <select value={value} disabled={readOnly}
              onChange={(e) => onPick(e.target.value)}>
        <option value="">— column —</option>
        {colOptions.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    );
  }

  function conditionRows(rule: Rule, i: number, field: "conditions" | "cases") {
    const list: Rule[] = rule[field] ?? [];
    const patchList = (next: Rule[]) => patchRule(i, { [field]: next });
    return (
      <div className="ips-conds">
        {list.map((c, ci) => (
          <div className="ips-cond" key={ci}>
            {colSelect(c.column ?? "", (v) => {
              const next = list.slice(); next[ci] = { ...c, column: v }; patchList(next);
            })}
            <select value={c.op ?? "contains"} disabled={readOnly}
                    onChange={(e) => {
                      const next = list.slice();
                      next[ci] = { ...c, op: e.target.value }; patchList(next);
                    }}>
              {CONDITION_OPS.map((o) => <option key={o} value={o}>{o.replace("_", " ")}</option>)}
            </select>
            {!["is_empty", "not_empty"].includes(c.op) && (
              <input placeholder="text" value={c.value ?? ""} disabled={readOnly}
                     onChange={(e) => {
                       const next = list.slice();
                       next[ci] = { ...c, value: e.target.value }; patchList(next);
                     }} />
            )}
            {field === "cases" && (
              <input placeholder="→ result" value={c.result ?? ""} disabled={readOnly}
                     onChange={(e) => {
                       const next = list.slice();
                       next[ci] = { ...c, result: e.target.value }; patchList(next);
                     }} />
            )}
            {!readOnly && (
              <button className="ips-x" title="Remove"
                      onClick={() => patchList(list.filter((_, k) => k !== ci))}>✕</button>
            )}
          </div>
        ))}
        {!readOnly && (
          <button className="link-btn" onClick={() =>
            patchList([...list, field === "cases"
              ? { column: "", op: "contains", value: "", result: "" }
              : { column: "", op: "contains", value: "" }])}>
            + condition
          </button>
        )}
      </div>
    );
  }

  function ruleCard(rule: Rule, i: number) {
    return (
      <div className="ips-rule" key={i}>
        <div className="ips-rule-head">
          <span className="mm-tag">{rule.type}</span>
          {preview?.stages?.[i] && (
            <span className="ips-count">
              {preview.stages[i].rows_in} → {preview.stages[i].rows_out} rows
            </span>
          )}
          <div style={{ flex: 1 }} />
          {!readOnly && (
            <>
              <button className="ips-x" title="Move up" onClick={() => moveRule(i, -1)}>↑</button>
              <button className="ips-x" title="Move down" onClick={() => moveRule(i, 1)}>↓</button>
              <button className="ips-x" title="Remove rule" onClick={() =>
                patchDraft({ rules: draft!.rules.filter((_, k) => k !== i) })}>✕</button>
            </>
          )}
        </div>

        {rule.type === "filter" && (
          <>
            <div className="ips-line">
              <select value={rule.action} disabled={readOnly}
                      onChange={(e) => patchRule(i, { action: e.target.value })}>
                <option value="keep">keep rows</option>
                <option value="drop">drop rows</option>
              </select>
              <span>matching</span>
              <select value={rule.match} disabled={readOnly}
                      onChange={(e) => patchRule(i, { match: e.target.value })}>
                <option value="all">all</option>
                <option value="any">any</option>
              </select>
              <span>of:</span>
            </div>
            {conditionRows(rule, i, "conditions")}
          </>
        )}

        {rule.type === "derive" && (
          <>
            <div className="ips-line">
              <span>set</span>
              <input placeholder="new column" value={rule.target ?? ""} disabled={readOnly}
                     onChange={(e) => patchRule(i, { target: e.target.value })} />
              <span>by first matching case (else</span>
              <input placeholder="default" value={rule.default ?? ""} disabled={readOnly}
                     onChange={(e) => patchRule(i, { default: e.target.value })} />
              <span>):</span>
            </div>
            {conditionRows(rule, i, "cases")}
          </>
        )}

        {rule.type === "split_cell" && (
          <div className="ips-line">
            <span>split</span>
            {colSelect(rule.column ?? "", (v) => patchRule(i, { column: v }))}
            <span>by</span>
            <input className="ips-narrow" value={rule.delimiter ?? "::"} disabled={readOnly}
                   onChange={(e) => patchRule(i, { delimiter: e.target.value })} />
            <span>take segment</span>
            <input className="ips-narrow" type="number" value={rule.segment ?? 0} disabled={readOnly}
                   onChange={(e) => patchRule(i, { segment: Number(e.target.value) })} />
            <span>→</span>
            <input placeholder="target column" value={rule.target ?? ""} disabled={readOnly}
                   onChange={(e) => patchRule(i, { target: e.target.value })} />
          </div>
        )}

        {rule.type === "expand" && (
          <div className="ips-line">
            <span>one row per part of</span>
            {colSelect(rule.column ?? "", (v) => patchRule(i, { column: v }))}
            <span>split by</span>
            <input className="ips-narrow" value={rule.delimiter === "\n" ? "\\n" : rule.delimiter ?? "\\n"}
                   disabled={readOnly}
                   onChange={(e) => patchRule(i, {
                     delimiter: e.target.value === "\\n" ? "\n" : e.target.value })} />
            <span>keep before</span>
            <input className="ips-narrow" placeholder="(none)" value={rule.take_before ?? ""}
                   disabled={readOnly}
                   onChange={(e) => patchRule(i, { take_before: e.target.value })} />
          </div>
        )}

        {rule.type === "partition" && (
          <div className="ips-line">
            <span>split the dataset into one model per value of</span>
            {colSelect(rule.column ?? "", (v) => patchRule(i, { column: v }))}
          </div>
        )}

        {rule.type === "clean" && (
          <div className="ips-conds">
            {(rule.ops ?? []).map((op: Rule, oi: number) => (
              <div className="ips-cond" key={oi}>
                <select value={op.op} disabled={readOnly}
                        onChange={(e) => {
                          const ops = rule.ops.slice();
                          ops[oi] = { ...op, op: e.target.value };
                          patchRule(i, { ops });
                        }}>
                  {CLEAN_OPS.map((o) => <option key={o} value={o}>{o.replace("_", " ")}</option>)}
                </select>
                <select value={op.column ?? "*"} disabled={readOnly}
                        onChange={(e) => {
                          const ops = rule.ops.slice();
                          ops[oi] = { ...op, column: e.target.value };
                          patchRule(i, { ops });
                        }}>
                  <option value="*">all columns</option>
                  {colOptions.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                {op.op === "replace" && (
                  <>
                    <input placeholder="find" value={op.find ?? ""} disabled={readOnly}
                           onChange={(e) => {
                             const ops = rule.ops.slice();
                             ops[oi] = { ...op, find: e.target.value };
                             patchRule(i, { ops });
                           }} />
                    <input placeholder="replace" value={op.replace ?? ""} disabled={readOnly}
                           onChange={(e) => {
                             const ops = rule.ops.slice();
                             ops[oi] = { ...op, replace: e.target.value };
                             patchRule(i, { ops });
                           }} />
                  </>
                )}
                {!readOnly && (
                  <button className="ips-x" onClick={() =>
                    patchRule(i, { ops: rule.ops.filter((_: Rule, k: number) => k !== oi) })}>✕</button>
                )}
              </div>
            ))}
            {!readOnly && (
              <button className="link-btn" onClick={() =>
                patchRule(i, { ops: [...(rule.ops ?? []),
                                     { op: "trim", column: "*" }] })}>
                + operation
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  const previewTable = (() => {
    if (!preview) return null;
    // Target-shaped preview when a mapping exists; else the pipeline columns.
    const useMapped = preview.mapped_rows && preview.mapped_rows.length > 0;
    const cols = useMapped
      ? [...new Set(Object.values(draft?.mapping ?? {}))]
      : preview.columns;
    const rows = useMapped ? preview.mapped_rows! : preview.rows;
    return (
      <table className="profimp-table ips-preview-table">
        <thead>
          <tr>{cols.map((c) => (
            <th key={c}>
              {c}
              {draft?.locked_targets.includes(c) && " 🔒"}
            </th>
          ))}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => {
                const v = useMapped
                  ? (r as Record<string, { text: string }>)[c]?.text ?? ""
                  : (r as Record<string, string>)[c] ?? "";
                return <td key={c}>{v || <span className="dim">—</span>}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    );
  })();

  return (
    <div className="prefs-body ips-root">
      <div className="ips-cols">
        <div className="ips-list">
          <div className="ips-list-head">
            Profiles
            <button className="link-btn" onClick={() => setDraft(newProfile())}>+ New</button>
          </div>
          {profiles.map((p) => (
            <div key={p.id}
                 className={"ips-item" + (draft?.id === p.id ? " sel" : "")}
                 onClick={() => { setDraft(structuredClone(p)); setPreview(null); }}>
              <span className="ips-item-name">{p.name}</span>
              {p.builtin && <span className="mm-tag">built-in</span>}
            </div>
          ))}
        </div>

        {!draft ? (
          <div className="ips-empty">
            Select a profile, or create a new one. Built-in profiles are
            read-only — duplicate one to customize it.
          </div>
        ) : (
          <div className="ips-editor">
            {readOnly && (
              <div className="profimp-warn">
                Built-in profile (read-only).{" "}
                <button className="link-btn" onClick={() => duplicate(draft)}>
                  Duplicate to edit
                </button>
              </div>
            )}

            <div className="ips-line">
              <span className="profimp-label">Name</span>
              <input value={draft.name} disabled={readOnly}
                     onChange={(e) => patchDraft({ name: e.target.value })} />
              <span className="profimp-label">Filename pattern</span>
              <input placeholder="INTERFACE_EXPORT_*.xls*" value={draft.filename_pattern ?? ""}
                     disabled={readOnly}
                     onChange={(e) => patchDraft({ filename_pattern: e.target.value })} />
            </div>

            <div className="ips-line">
              <span className="profimp-label">Mode</span>
              <select value={draft.mode} disabled={readOnly}
                      onChange={(e) => patchDraft({
                        mode: e.target.value as "append" | "update" })}>
                <option value="append">Append new rows</option>
                <option value="update">Match & update existing rows</option>
              </select>
              {draft.mode === "update" && (
                <>
                  <span className="profimp-label">Key column(s)</span>
                  <select multiple value={draft.key_columns} disabled={readOnly}
                          onChange={(e) => patchDraft({
                            key_columns: [...e.target.selectedOptions].map((o) => o.value) })}>
                    {mappedTargets.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </>
              )}
            </div>

            <div className="ips-section">
              <div className="ips-section-head">
                Sample file
                <button className="link-btn" onClick={() => setPickerOpen(true)}>
                  {sample ? "Change…" : "Load sample…"}
                </button>
                {sample && (
                  <span className="ips-count">
                    {sample.row_count} row(s){sample.truncated ? " (first 1000)" : ""} ·
                    sheet {sample.sheet} · header row {sample.header_row + 1}
                  </span>
                )}
              </div>
            </div>

            <div className="ips-section">
              <div className="ips-section-head">
                Rules
                {!readOnly && (
                  <span className="ips-addrule">
                    {["filter", "derive", "split_cell", "expand", "partition", "clean"].map((t) => (
                      <button key={t} className="link-btn"
                              onClick={() => patchDraft({ rules: [...draft.rules, emptyRule(t)] })}>
                        + {t.replace("_", " ")}
                      </button>
                    ))}
                  </span>
                )}
              </div>
              {draft.rules.map((r, i) => ruleCard(r, i))}
              {draft.rules.length === 0 && (
                <div className="dim">No rules — every row passes through.</div>
              )}
            </div>

            <div className="ips-section">
              <div className="ips-section-head">Column mapping</div>
              {(sample?.columns ?? colOptions).map((src) => (
                <div className="ips-cond" key={src}>
                  <span className="mono ips-src">{src}</span>
                  <span>→</span>
                  <select value={draft.mapping[src] ?? ""} disabled={readOnly}
                          onChange={(e) => {
                            const mapping = { ...draft.mapping };
                            if (e.target.value) mapping[src] = e.target.value;
                            else delete mapping[src];
                            patchDraft({ mapping });
                          }}>
                    <option value="">(skip)</option>
                    {[...new Set([...targets, ...mappedTargets])].map((t) => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                  {draft.mapping[src] && (
                    <label className="ips-lock">
                      <input type="checkbox" disabled={readOnly}
                             checked={draft.locked_targets.includes(draft.mapping[src])}
                             onChange={(e) => {
                               const t = draft.mapping[src];
                               patchDraft({
                                 locked_targets: e.target.checked
                                   ? [...draft.locked_targets, t]
                                   : draft.locked_targets.filter((x) => x !== t),
                               });
                             }} />
                      🔒 lock imported cells
                    </label>
                  )}
                </div>
              ))}
              {!sample && colOptions.length === 0 && (
                <div className="dim">Load a sample file to map its columns.</div>
              )}
            </div>

            <div className="ips-section">
              <div className="ips-section-head">
                Live preview
                <label className="ips-lock">
                  <input type="checkbox" checked={live}
                         onChange={(e) => setLive(e.target.checked)} />
                  live
                </label>
                <button className="link-btn" onClick={refreshPreview}
                        disabled={!sample}>Refresh</button>
                <button className="link-btn" onClick={() => showAll(0)}
                        disabled={!sample}>Show all filtered rows…</button>
                {preview && (
                  <span className="ips-count">
                    {preview.row_count} row(s) after rules
                    {preview.partition_column &&
                      ` · ${Object.keys(preview.partitions).length} partition(s)`}
                  </span>
                )}
              </div>
              {!sample ? (
                <div className="dim">Load a sample file to preview.</div>
              ) : (
                previewTable
              )}
              {preview?.partition_column && (
                <div className="ips-count">
                  {Object.entries(preview.partitions).map(([k, n]) =>
                    `${k || "(blank)"}: ${n}`).join(" · ")}
                </div>
              )}
            </div>

            {msg && <div className="profimp-warn">{msg}</div>}

            <div className="ips-foot">
              {!readOnly && (
                <button className="save-btn" onClick={save}>Save profile</button>
              )}
              <button className="link-btn" onClick={() => duplicate(draft)}>Duplicate</button>
              {!readOnly && draft.id && (
                <button className="link-btn danger" onClick={() => remove(draft)}>Delete</button>
              )}
            </div>
          </div>
        )}
      </div>

      {pickerOpen && (
        <FolderPicker
          mode="import"
          exts={[".xlsx", ".xls", ".csv"]}
          title="Load a sample Excel/CSV for the live preview"
          hint="Read once into memory — nothing is stored"
          onCancel={() => setPickerOpen(false)}
          onConfirm={loadSample}
        />
      )}

      {renderAll && (
        <div className="modal-overlay" onMouseDown={() => setRenderAll(null)}>
          <div className="modal profimp" onMouseDown={(e) => e.stopPropagation()}>
            <div className="profimp-head">
              All filtered rows ({renderAll.total})
              <button className="prefs-close" onClick={() => setRenderAll(null)}>✕</button>
            </div>
            <div className="profimp-body ips-all-body">
              <table className="profimp-table">
                <thead>
                  <tr>{renderAll.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {renderAll.rows.map((r, i) => (
                    <tr key={i}>
                      {renderAll.columns.map((c) => <td key={c}>{r[c] ?? ""}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="profimp-foot">
              <button className="link-btn"
                      disabled={renderAll.offset === 0}
                      onClick={() => showAll(Math.max(0, renderAll.offset - 200))}>
                ← Prev
              </button>
              <span className="ips-count">
                {renderAll.offset + 1}–{renderAll.offset + renderAll.rows.length} of {renderAll.total}
              </span>
              <button className="link-btn"
                      disabled={renderAll.offset + 200 >= renderAll.total}
                      onClick={() => showAll(renderAll.offset + 200)}>
                Next →
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
