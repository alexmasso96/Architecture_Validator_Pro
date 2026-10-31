import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ProjectStatus, SyncOverview } from "../api/types";
import { FolderPicker } from "./FolderPicker";

// The open project's sharing only (#16). The tracked list and the change log live
// in the Sync Worker's own window, which runs without the app.
export function SyncPanel({
  status,
  toast,
  onClose,
  onChanged,
}: {
  status: ProjectStatus;
  toast: (m: string) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [overview, setOverview] = useState<SyncOverview | null>(null);
  const [picking, setPicking] = useState(false);

  const reload = useCallback(async () => {
    setOverview(await api.get<SyncOverview>("/sync/status"));
  }, []);

  useEffect(() => {
    reload().catch(() => setOverview(null));
  }, [reload]);

  async function act(fn: () => Promise<unknown>) {
    try {
      await fn();
      await reload();
      onChanged();
    } catch (e) {
      toast((e as ApiError).detail ?? (e as Error).message);
    }
  }

  async function share(folder: string) {
    setPicking(false);
    const name = (status.path ?? "project.arch").split(/[/\\]/).pop();
    const separator = folder.includes("\\") ? "\\" : "/";
    await act(() => api.post("/sync/publish", { remote: `${folder}${separator}${name}` }));
  }

  const current = overview?.current;

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div className="modal tb-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">Sharing</div>
        <div className="tb-modal-body">
          {status.tracked ? (
            <>
              <div>This project is shared.</div>
              <code className="tb-path">{status.path}</code>
              {current?.message && <div className="tb-hint">{current.message}</div>}
            </>
          ) : (
            <div>This project is only on this PC.</div>
          )}
          <div className="tb-hint">
            The Sync Worker keeps shared projects current in the background, and shows
            what changed in each one.
          </div>
          {current?.state === "blocked" && (
            <div className="tb-note warn">
              <button
                className="tci-mini-btn"
                onClick={() => act(() => api.post("/sync/republish"))}
              >
                Republish from this copy
              </button>
            </div>
          )}
        </div>
        <div className="confirm-foot">
          <button
            className="tci-mini-btn"
            onClick={() => act(() => api.post("/sync/open-worker"))}
          >
            Open Sync Worker
          </button>
          {status.can_edit && !status.tracked && (
            <button className="tci-mini-btn" onClick={() => setPicking(true)}>
              Save to a shared folder…
            </button>
          )}
          <button className="tci-mini-btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {picking && (
        <FolderPicker
          mode="folder"
          title="Choose the shared folder"
          onCancel={() => setPicking(false)}
          onConfirm={(path) => share(path)}
        />
      )}
    </div>
  );
}
