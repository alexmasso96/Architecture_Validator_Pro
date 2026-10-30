import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ModelsResponse } from "../api/types";
import { FolderPicker } from "./FolderPicker";

// #8 Phase D: profile-driven table import — pick a file, auto-match a profile
// by its filename pattern, review the pre-import diff, commit. The legacy
// ImportWizard stays untouched; this dialog is the new engine's front door.

export interface ImportProfile {
  id: string;
  name: string;
  builtin?: boolean;
  filename_pattern?: string;
  sheet?: string | null;
  header_row?: number | null;
  mode: "append" | "update";
  key_columns: string[];
  rules: Record<string, unknown>[];
  mapping: Record<string, string>;
  locked_targets: string[];
}

interface AppendDiff {
  diff_id: string;
  mode: "append";
  models: { name: string; exists: boolean; rows: number }[];
  total_rows: number;
}

interface UpdateDiff {
  diff_id: string;
  mode: "update";
  model_id: number;
  updates: number;
  cells: number;
  locked: { row_index: number; column: string; old: string; import_value: string }[];
  conflicts: { key: string[]; columns: string[] }[];
  orphans: number;
  unmatched: number;
  update_preview: { row_index: number; changes: Record<string, { old: string; new: string }> }[];
}

type Diff = AppendDiff | UpdateDiff;

function globToRegex(glob: string): RegExp {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  return new RegExp("^" + esc.replace(/\*/g, ".*").replace(/\?/g, ".") + "$", "i");
}

export function ProfileImportDialog({
  onClose,
  onChanged,
  toast,
}: {
  onClose: () => void;
  onChanged: () => void;
  toast: (msg: string) => void;
}) {
  const [profiles, setProfiles] = useState<ImportProfile[]>([]);
  const [models, setModels] = useState<{ id: number; name: string }[]>([]);
  const [pickerOpen, setPickerOpen] = useState(true);
  const [filePath, setFilePath] = useState<string | null>(null);
  const [profileId, setProfileId] = useState<string>("");
  const [modelId, setModelId] = useState<number | null>(null);
  const [diff, setDiff] = useState<Diff | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<{ profiles: ImportProfile[] }>("/import/profiles")
      .then((r) => setProfiles(r.profiles.filter((p) => Object.keys(p.mapping ?? {}).length > 0 || !p.builtin)))
      .catch(() => setProfiles([]));
    api
      .get<ModelsResponse>("/models")
      .then((r) => {
        setModels(r.models.map((m) => ({ id: m.id, name: m.name })));
        if (r.active_model_id != null) setModelId(r.active_model_id);
      })
      .catch(() => setModels([]));
  }, []);

  const profile = useMemo(
    () => profiles.find((p) => p.id === profileId) ?? null,
    [profiles, profileId],
  );

  function onFilePicked(path: string) {
    setPickerOpen(false);
    setFilePath(path);
    setDiff(null);
    setError(null);
    // Filename auto-apply: a unique pattern match preselects the profile.
    const base = path.split(/[\\/]/).pop() ?? "";
    const matches = profiles.filter(
      (p) => p.filename_pattern && globToRegex(p.filename_pattern).test(base),
    );
    if (matches.length >= 1) setProfileId(matches[0].id);
  }

  async function runDiff() {
    if (!filePath || !profile) return;
    setBusy(true);
    setError(null);
    try {
      const d = await api.post<Diff>("/import/diff", {
        path: filePath,
        sheet: profile.sheet ?? null,
        header_row: profile.header_row ?? 0,
        rules: profile.rules,
        mapping: profile.mapping,
        locked_targets: profile.locked_targets,
        mode: profile.mode,
        key_columns: profile.key_columns,
        model_id: profile.mode === "update" ? modelId : null,
        profile_name: profile.name,
      });
      setDiff(d);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    if (!diff) return;
    setBusy(true);
    try {
      const r = await api.post<Record<string, unknown>>("/import/commit", {
        diff_id: diff.diff_id,
      });
      if (diff.mode === "append") {
        const ids = (r.model_ids as number[]) ?? [];
        toast(`Imported ${r.total_added} row(s) into ${ids.length} model(s).`);
        if (ids.length) {
          // Re-run fuzzy matching over the affected models (legacy behaviour).
          api.post("/jobs/fuzzy_rematch", { model_ids: ids }).catch(() => {});
        }
      } else {
        toast(
          `Updated ${r.cells_updated} cell(s)` +
            ((r.locked_skipped as unknown[])?.length
              ? ` — ${(r.locked_skipped as unknown[]).length} locked cell(s) untouched.`
              : "."),
        );
      }
      onChanged();
      onClose();
    } catch (e) {
      setError((e as Error).message);
      setDiff(null); // diff ids are one-shot — force a re-diff
    } finally {
      setBusy(false);
    }
  }

  if (pickerOpen) {
    return (
      <FolderPicker
        mode="import"
        exts={[".xlsx", ".xls", ".csv"]}
        title="Import table — choose an Excel/CSV file"
        hint="Pick the exported table (interface export, DNG, …)"
        onCancel={onClose}
        onConfirm={onFilePicked}
      />
    );
  }

  const fileName = filePath?.split(/[\\/]/).pop() ?? "";

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div className="modal profimp" onMouseDown={(e) => e.stopPropagation()}>
        <div className="profimp-head">
          Import from profile
          <button className="prefs-close" onClick={onClose} title="Close">✕</button>
        </div>

        <div className="profimp-body">
          <div className="profimp-row">
            <span className="profimp-label">File</span>
            <span className="mono profimp-file" title={filePath ?? ""}>{fileName}</span>
            <button className="link-btn" onClick={() => setPickerOpen(true)}>
              Change…
            </button>
          </div>

          <div className="profimp-row">
            <span className="profimp-label">Profile</span>
            <select
              value={profileId}
              onChange={(e) => {
                setProfileId(e.target.value);
                setDiff(null);
              }}
            >
              <option value="">— choose —</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                  {p.builtin ? " (built-in)" : ""}
                </option>
              ))}
            </select>
          </div>

          {profile?.mode === "update" && (
            <div className="profimp-row">
              <span className="profimp-label">Target model</span>
              <select
                value={modelId ?? ""}
                onChange={(e) => {
                  setModelId(e.target.value ? Number(e.target.value) : null);
                  setDiff(null);
                }}
              >
                <option value="">— choose —</option>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
            </div>
          )}

          {error && <div className="profimp-error">{error}</div>}

          {diff?.mode === "append" && (
            <div className="profimp-diff">
              <div className="profimp-diff-title">
                Will add {diff.total_rows} row(s) to {diff.models.length} model(s):
              </div>
              <table className="profimp-table">
                <thead>
                  <tr><th>Model</th><th>Rows</th><th>Status</th></tr>
                </thead>
                <tbody>
                  {diff.models.map((m) => (
                    <tr key={m.name}>
                      <td>{m.name}</td>
                      <td>{m.rows}</td>
                      <td>{m.exists ? "append to existing" : "new model"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {diff?.mode === "update" && (
            <div className="profimp-diff">
              <div className="profimp-diff-title">
                Will update {diff.cells} cell(s) in {diff.updates} row(s) ·{" "}
                {diff.unmatched} table row(s) untouched · {diff.orphans} imported
                key(s) match nothing
              </div>
              {diff.locked.length > 0 && (
                <div className="profimp-warn">
                  ⚠ {diff.locked.length} locked cell(s) differ from the import and
                  will NOT be changed:
                  {" "}
                  {diff.locked.slice(0, 5).map((l) =>
                    `row ${l.row_index + 1} ${l.column} (${l.old} ≠ ${l.import_value})`,
                  ).join("; ")}
                  {diff.locked.length > 5 ? " …" : ""}
                </div>
              )}
              {diff.conflicts.length > 0 && (
                <div className="profimp-warn">
                  ⚠ {diff.conflicts.length} duplicate key(s) in the import carry
                  differing values — the first occurrence wins.
                </div>
              )}
              {diff.update_preview.length > 0 && (
                <table className="profimp-table">
                  <thead>
                    <tr><th>Row</th><th>Column</th><th>Old</th><th>New</th></tr>
                  </thead>
                  <tbody>
                    {diff.update_preview.flatMap((u) =>
                      Object.entries(u.changes).map(([col, ch]) => (
                        <tr key={`${u.row_index}-${col}`}>
                          <td>{u.row_index + 1}</td>
                          <td>{col}</td>
                          <td className="dim">{ch.old || "—"}</td>
                          <td>{ch.new}</td>
                        </tr>
                      )),
                    )}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>

        <div className="profimp-foot">
          <button className="link-btn" onClick={onClose}>Cancel</button>
          <div style={{ flex: 1 }} />
          {!diff ? (
            <button
              className="save-btn"
              disabled={busy || !profile ||
                        (profile.mode === "update" && modelId == null)}
              onClick={runDiff}
            >
              {busy ? "Analyzing…" : "Preview changes"}
            </button>
          ) : (
            <button className="save-btn" disabled={busy} onClick={commit}>
              {busy ? "Importing…" : "Apply import"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
