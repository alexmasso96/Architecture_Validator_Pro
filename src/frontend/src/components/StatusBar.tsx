import type { ProjectStatus, SyncStatus } from "../api/types";

export function StatusBar({
  status,
  modelName,
  modelStatus,
  rowCount,
  reviewedCount,
  sync,
  onSync,
}: {
  status: ProjectStatus;
  modelName: string | null;
  modelStatus: string | null;
  rowCount: number;
  reviewedCount: number;
  sync: SyncStatus | null;
  onSync: () => void;
}) {
  let syncIcon = "⇅";
  let syncText = "Not shared";
  const syncing = ["pulling", "pushing", "copying"].includes(sync?.state ?? "");
  if (status.tracked) {
    syncText = "Waiting for sync";
    if (sync?.state === "synced") syncText = `Synced ${sync.last_synced ?? ""}`.trim();
    else if (syncing) syncText = sync?.message ?? "Syncing";
    else if (sync?.state === "offline") {
      syncIcon = "⚠";
      syncText = `Offline, last synced ${sync.last_synced || "never"}`;
    } else if (sync?.state) {
      syncIcon = "⚠";
      syncText = "Sync needs attention";
    }
  }
  let lockClass = "lock";
  let lockText = "🔒 Exclusive Edit";
  if (status.lock_lost) {
    lockClass = "lock lost";
    lockText = "⚠ Lock lost — view only";
  } else if (status.mode === "view") {
    lockClass = "lock viewonly";
    lockText = "👁 View Only";
  }

  return (
    <div className="statusbar">
      <span className={lockClass}>{lockText}</span>
      <button className="statusbar-sync" title={sync?.message || "Shared projects"} onClick={onSync}>
        <span className={syncing ? "sync-turning" : ""}>{syncIcon}</span> {syncText}
      </button>
      <span className="right">
        <span>
          {status.active_release ?? "—"}
          {modelName
            ? ` · Model: ${modelName}${modelStatus ? ` (${modelStatus})` : ""}`
            : ""}
        </span>
        <span>
          {rowCount} ports · {reviewedCount} reviewed
        </span>
      </span>
    </div>
  );
}
