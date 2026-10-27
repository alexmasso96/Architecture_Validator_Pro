import { useCallback, useEffect, useState } from "react";
import { api, waitForJob } from "../api/client";
import type { ModelsResponse, SymbolFileInfo, SymbolFilesResponse } from "../api/types";
import { FolderPicker } from "./FolderPicker";
import { stripSymbolExt } from "../fileTypes";

// Per-release symbol files, one per component (e.g. FBL + APP). Symbol search,
// matching, the Code Map and call-tree tests use the union of all of them; a
// name defined in several components merges into the earliest one's entry.
export function SymbolFilesDialog({
  releaseId,
  releaseName,
  isActive,
  editable,
  onClose,
  onChanged,
  toast,
}: {
  releaseId: number;
  releaseName: string;
  isActive: boolean;
  editable: boolean;
  onClose: () => void;
  onChanged: () => void;
  toast: (msg: string) => void;
}) {
  const [data, setData] = useState<SymbolFilesResponse | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [newComp, setNewComp] = useState("");
  // Picking a file for this component: a new one (the "add" row) or a replace.
  const [pickFor, setPickFor] = useState<{ component: string; replace: boolean } | null>(null);

  const reload = useCallback(async () => {
    try {
      setData(await api.get<SymbolFilesResponse>(`/releases/${releaseId}/symbol-files`));
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [releaseId]);

  useEffect(() => {
    reload();
  }, [reload]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !pickFor) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, pickFor]);

  const canEdit = editable && !!data && !data.is_frozen;
  const files = data?.files ?? [];

  // Re-match the table so Match cells reflect the new symbol pool — only for
  // the active release (its rows are the ones on screen).
  async function rematch() {
    if (!isActive) return;
    const models = await api.get<ModelsResponse>("/models");
    const ids = models.models.filter((m) => !m.is_deleted).map((m) => m.id);
    if (!ids.length) return;
    setBusy("Re-matching symbols…");
    const job = await api.post<{ job_id: string }>("/jobs/fuzzy_rematch", { model_ids: ids });
    await waitForJob(job.job_id, "Matching failed.");
  }

  async function importFile(component: string, path: string) {
    setErr(null);
    setBusy(`Importing ${component}…`);
    try {
      const job = await api.post<{ job_id: string }>(`/releases/${releaseId}/symbol-files`, {
        component,
        file_path: path,
      });
      const res = await waitForJob(job.job_id, "Import failed.");
      const fn = Number(res?.functions ?? 0);
      const shared = Number(res?.shared_functions ?? 0);
      await rematch();
      toast(
        `${component}: ${fn} functions ${res?.action === "replaced" ? "replaced" : "added"}` +
          (shared ? ` · ${shared} also in another component` : ""),
      );
      setNewComp("");
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
      reload();
    }
  }

  async function rename(f: SymbolFileInfo) {
    const name = window.prompt(`Rename component “${f.component}”:`, f.component);
    if (name == null || !name.trim() || name.trim() === f.component) return;
    setErr(null);
    try {
      setData(
        await api.patch<SymbolFilesResponse>(
          `/releases/${releaseId}/symbol-files/${encodeURIComponent(f.component)}`,
          { name: name.trim() },
        ),
      );
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function remove(f: SymbolFileInfo) {
    if (
      !window.confirm(
        `Remove component “${f.component}” (${f.file_name || "symbol file"}) from ` +
          `“${releaseName}”? Its symbols leave the search/match pool; rebuild the ` +
          `Code Map afterwards.`,
      )
    )
      return;
    setErr(null);
    setBusy(`Removing ${f.component}…`);
    try {
      setData(
        await api.del<SymbolFilesResponse>(
          `/releases/${releaseId}/symbol-files/${encodeURIComponent(f.component)}`,
        ),
      );
      await rematch();
      toast(`Removed component “${f.component}”`);
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const trimmed = newComp.trim();
  const nameTaken = files.some((f) => f.component === trimmed);

  return (
    <>
      <div className="modal-overlay" onMouseDown={onClose}>
        <div className="modal symfiles" onMouseDown={(e) => e.stopPropagation()}>
          <div className="modal-head">Symbol Files — {releaseName}</div>
          <div className="symfiles-body">
            <p className="symfiles-hint">
              One symbol file per component (e.g. FBL and APP). Search, matching, the Code
              Map and call-tree tests use all of them; a name defined in several components
              is listed once, taking its address from the first component below.
            </p>
            {data === null ? (
              <div className="mm-empty">
                <span className="spin" />
                &nbsp;Loading…
              </div>
            ) : files.length === 0 ? (
              <div className="mm-empty">No symbol file attached to this release.</div>
            ) : (
              <table className="rm-table">
                <thead>
                  <tr>
                    <th>Component</th>
                    <th>File</th>
                    <th>Symbols</th>
                    <th className="rm-actions-col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {files.map((f, i) => (
                    <tr key={f.component}>
                      <td>
                        <span className="rm-name">
                          {f.component}
                          {i === 0 && files.length > 1 && (
                            <span className="mm-tag" title="First component: wins duplicate names">
                              primary
                            </span>
                          )}
                        </span>
                      </td>
                      <td title={f.elf_path}>
                        {f.file_name || "—"}
                        {!f.file_exists && (
                          <span
                            className="mm-tag del"
                            title="The original file is not at this path — symbols are stored in the project, but disassembly (ELF call tree) is unavailable"
                          >
                            file missing
                          </span>
                        )}
                      </td>
                      <td>
                        {f.functions} fn · {f.variables} var
                        {f.duplicates_of_earlier > 0 && (
                          <span
                            className="mm-tag"
                            title="Function names also defined by an earlier component — merged into that entry"
                          >
                            {f.duplicates_of_earlier} shared
                          </span>
                        )}
                        {f.parser_backend && (
                          <span className="mm-tag">
                            ⚙ {f.parser_backend === "rust_elf_parser" ? "native" : "python"}
                          </span>
                        )}
                      </td>
                      <td className="rm-actions-col">
                        <button disabled={!canEdit || !!busy} onClick={() => rename(f)}>
                          Rename
                        </button>
                        <button
                          disabled={!canEdit || !!busy}
                          onClick={() => setPickFor({ component: f.component, replace: true })}
                        >
                          Replace…
                        </button>
                        <button
                          className="danger"
                          disabled={!canEdit || !!busy}
                          onClick={() => remove(f)}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {canEdit && (
              <div className="symfiles-add">
                <input
                  placeholder="Component name (e.g. FBL)"
                  value={newComp}
                  maxLength={64}
                  disabled={!!busy}
                  onChange={(e) => setNewComp(e.target.value)}
                />
                <button
                  className="primary"
                  disabled={!!busy || nameTaken}
                  title={nameTaken ? "That component exists — use Replace…" : undefined}
                  onClick={() => setPickFor({ component: trimmed, replace: false })}
                >
                  Add Symbol File…
                </button>
              </div>
            )}
            {busy && (
              <div className="symfiles-busy">
                <span className="spin" />
                &nbsp;{busy}
              </div>
            )}
            {err && <div className="symfiles-err">{err}</div>}
          </div>
          <div className="mm-foot">
            {!editable && (
              <span className="mm-vo">View-only — open for editing to change symbol files.</span>
            )}
            {editable && data?.is_frozen && (
              <span className="mm-vo">Frozen baseline — its symbol files are locked.</span>
            )}
            <div className="spacer" />
            <button className="primary" disabled={!!busy} onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      </div>

      {pickFor && (
        <FolderPicker
          mode="import"
          title={
            pickFor.replace
              ? `Replace symbol file of “${pickFor.component}”`
              : `Add symbol file${pickFor.component ? ` for “${pickFor.component}”` : ""}`
          }
          hint="Select an ELF image (.elf/.out/.abs/.axf) or a .json symbol cache"
          onCancel={() => setPickFor(null)}
          onConfirm={(path) => {
            const target = pickFor;
            setPickFor(null);
            // No name typed: default the component to the file's base name.
            const base = path.split(/[\\/]/).pop() ?? path;
            const comp = target.component || stripSymbolExt(base);
            importFile(comp, path);
          }}
        />
      )}
    </>
  );
}
