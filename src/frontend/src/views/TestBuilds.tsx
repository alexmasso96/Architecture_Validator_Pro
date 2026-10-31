import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { useSSE } from "../api/useSSE";
import type {
  BuildRun,
  CleanLockStatus,
  JobPayload,
  MappedStatus,
  PreflightReport,
  TerminalKind,
  TestBuild,
  TestProject,
  TrackedArtifact,
} from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { FolderPicker } from "../components/FolderPicker";

// Test Builds (#15): named builds per release, an overnight run, and switching
// what the debugger's mapped folder holds. Builds shown here always belong to
// the release selected in the table.

const ROLES = ["Flash", "Symbols", "Source", "Objects"];
const TERMINALS: TerminalKind[] = ["bash", "cmd", "powershell", "wsl"];
const MAX_LOG_LINES = 2000;
const ARTIFACT_EXTS = [
  ".hex", ".s19", ".srec", ".mot", ".bin", ".elf", ".out", ".abs", ".axf", ".map", ".sym", ".c", ".h",
];

type Picking =
  | { what: "mapped" }
  | { what: "main" }
  | { what: "field"; field: "source_dir" | "output_dir" | "build_cwd" }
  | { what: "import"; kind: "source" | "objects" }
  | { what: "artifact"; isFolder: boolean };

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function statusLabel(status: string): string {
  return status === "none" ? "Not captured" : status[0].toUpperCase() + status.slice(1);
}

export function TestBuilds({
  toast,
  canEdit,
}: {
  toast: (m: string) => void;
  canEdit: boolean;
}) {
  const { confirm, confirmNode } = useConfirm();
  const [builds, setBuilds] = useState<TestBuild[]>([]);
  const [releaseId, setReleaseId] = useState<number | null>(null);
  const [projects, setProjects] = useState<TestProject[]>([]);
  const [lock, setLock] = useState<CleanLockStatus | null>(null);
  const [mapped, setMapped] = useState<MappedStatus | null>(null);
  const [runs, setRuns] = useState<BuildRun[]>([]);
  const [unfinished, setUnfinished] = useState<BuildRun | null>(null);
  const [store, setStore] = useState<{
    path: string;
    blobs: number;
    bytes: number;
    unreferenced: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState<Partial<TestBuild>>({});
  const [artifactScope, setArtifactScope] = useState<number | null>(null);
  const [artifacts, setArtifacts] = useState<TrackedArtifact[]>([]);
  const [newName, setNewName] = useState("");
  const [newProject, setNewProject] = useState<number | null>(null);
  const [mapTarget, setMapTarget] = useState<number | null>(null);
  const [picking, setPicking] = useState<Picking | null>(null);

  const [report, setReport] = useState<PreflightReport | null>(null);
  const [dropped, setDropped] = useState<Set<number>>(new Set());
  const [continueAnyway, setContinueAnyway] = useState(false);
  const [reason, setReason] = useState("");
  const [runJob, setRunJob] = useState<JobPayload | null>(null);
  const [switchJob, setSwitchJob] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [viewedLog, setViewedLog] = useState<{ name: string; text: string } | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  const running = runJob?.status === "running" || runJob?.status === "queued";

  const reload = useCallback(async () => {
    try {
      const listed = await api.get<{ release_id: number; builds: TestBuild[] }>("/test-builds");
      setBuilds(listed.builds);
      setReleaseId(listed.release_id);
      setProjects((await api.get<{ projects: TestProject[] }>("/injection/projects")).projects);
      setLock(await api.get<CleanLockStatus>("/test-builds/clean-lock/status"));
      setMapped(await api.get<MappedStatus>("/test-builds/mapped/status"));
      const history = await api.get<{ runs: BuildRun[]; unfinished: BuildRun | null }>(
        "/test-builds/runs/list",
      );
      setRuns(history.runs);
      setUnfinished(history.unfinished);
      setStore(await api.get("/test-builds/store/summary"));
      setError(null);
    } catch (e) {
      setError((e as ApiError).detail ?? (e as Error).message);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const loadArtifacts = useCallback(async (scope: number | null) => {
    const query = scope == null ? "" : `?test_build_id=${scope}`;
    const r = await api.get<{ artifacts: TrackedArtifact[] }>(
      `/test-builds/artifacts/list${query}`,
    );
    setArtifacts(r.artifacts);
  }, []);

  useEffect(() => {
    loadArtifacts(artifactScope).catch(() => setArtifacts([]));
  }, [artifactScope, loadArtifacts]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logLines]);

  useSSE(
    useCallback(
      (e) => {
        if (e.event === "db-changed") {
          reload();
        } else if (e.event === "build") {
          const d = e.data as { build_name?: string; line?: string; build_id?: string };
          if (d.line !== undefined && String(d.build_id ?? "").startsWith("run-")) {
            setLogLines((prev) =>
              [...prev, `[${d.build_name}] ${d.line}`].slice(-MAX_LOG_LINES),
            );
          }
        } else if (e.event === "job") {
          const job = e.data as unknown as JobPayload;
          if (job.kind === "test_build_run") {
            setRunJob(job);
            if (job.status === "failed") toast(`Run failed: ${job.error}`);
            if (job.status === "done" || job.status === "cancelled") reload();
          } else if (job.kind === "mapped_switch" && job.job_id === switchJob) {
            if (job.status === "done") {
              const result = (job as JobPayload & { result?: { build_name: string } }).result;
              setSwitchJob(null);
              reload();
              confirm({
                title: "Switch finished",
                message: `Mapped release is now ${result?.build_name}.`,
                hideCancel: true,
              });
            } else if (job.status === "failed") {
              setSwitchJob(null);
              reload();
              confirm({
                title: "Switch stopped",
                message: job.error ?? "The switch didn't finish.",
                hideCancel: true,
              });
            }
          } else if (
            (job.kind === "import_source" || job.kind === "import_objects") &&
            job.status === "done"
          ) {
            reload();
          }
        }
      },
      [reload, switchJob, toast, confirm],
    ),
  );

  async function act(fn: () => Promise<unknown>) {
    try {
      await fn();
      await reload();
    } catch (e) {
      toast((e as ApiError).detail ?? (e as Error).message);
    }
  }

  function startEdit(build: TestBuild) {
    setEditing(build.id);
    setDraft(build);
    setArtifactScope(build.id);
  }

  async function saveDraft() {
    if (editing == null) return;
    await act(async () => {
      await api.patch(`/test-builds/${editing}`, {
        name: draft.name,
        build_command: draft.build_command,
        build_cwd: draft.build_cwd,
        clean_command: draft.clean_command,
        terminal: draft.terminal,
        wsl_distro: draft.wsl_distro,
        source_dir: draft.source_dir,
        output_dir: draft.output_dir,
      });
      toast("Saved.");
    });
  }

  async function createBuild() {
    await act(async () => {
      const made = await api.post<TestBuild>("/test-builds", {
        name: newName,
        test_project_id: newProject,
      });
      setNewName("");
      startEdit(made);
    });
  }

  async function deleteBuild(build: TestBuild) {
    const ok = await confirm({
      title: "Delete test build",
      message: `Delete ${build.name}? Its saved files stay in the store until you clean it up.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    if (editing === build.id) {
      setEditing(null);
      setArtifactScope(null);
    }
    await act(() => api.del(`/test-builds/${build.id}`));
  }

  async function saveArtifacts(next: TrackedArtifact[]) {
    setArtifacts(next);
    await act(() =>
      api.put("/test-builds/artifacts/list", {
        test_build_id: artifactScope,
        artifacts: next,
      }),
    );
  }

  function patchArtifact(index: number, patch: Partial<TrackedArtifact>) {
    setArtifacts((prev) => prev.map((a, i) => (i === index ? { ...a, ...patch } : a)));
  }

  async function openPreflight() {
    await act(async () => {
      const r = await api.post<PreflightReport>("/test-builds/preflight", {
        build_ids: [...checked],
      });
      setReport(r);
      setDropped(new Set());
      setContinueAnyway(false);
      setReason("");
    });
  }

  const lockReleases = (report?.locks ?? []).filter((l) => !l.ok).map((l) => l.release_id);

  async function refreshPreflight(continuing: boolean, drop: Set<number>) {
    const ids = [...checked].filter((id) => !drop.has(id));
    if (ids.length === 0) {
      setReport(null);
      return;
    }
    setReport(
      await api.post<PreflightReport>("/test-builds/preflight", {
        build_ids: ids,
        continue_anyway: continuing ? lockReleases : [],
      }),
    );
  }

  async function startRun() {
    const ids = [...checked].filter((id) => !dropped.has(id));
    setLogLines([]);
    setReport(null);
    await act(() =>
      api.post("/jobs/test_build_run", {
        build_ids: ids,
        continue_anyway: continueAnyway ? lockReleases : [],
        continue_reason: reason,
      }),
    );
  }

  async function startSwitch(allowOutside = false) {
    if (mapTarget == null) return;
    try {
      if (!allowOutside) {
        const plan = await api.post<{
          copy: string[];
          remove: string[];
          outside_changes: string[];
          transfer_bytes: number;
        }>("/test-builds/mapped/plan", { build_id: mapTarget });
        if (plan.outside_changes.length) {
          const ok = await confirm({
            title: "Files changed outside the app",
            message: (
              <>
                <p>These files don't match what the app last placed there:</p>
                <ul className="tb-list">
                  {plan.outside_changes.slice(0, 20).map((f) => (
                    <li key={f}>{f}</li>
                  ))}
                </ul>
                <p>Replace them anyway?</p>
              </>
            ),
            confirmLabel: "Replace",
            danger: true,
          });
          if (!ok) return;
          allowOutside = true;
        }
      }
      const job = await api.post<JobPayload>("/jobs/mapped_switch", {
        build_id: mapTarget,
        allow_outside_changes: allowOutside,
      });
      setSwitchJob(job.job_id);
    } catch (e) {
      toast((e as ApiError).detail ?? (e as Error).message);
    }
  }

  async function importObjectsFromMain() {
    await act(async () => {
      const r = await api.post<{ folder: string }>("/test-builds/main/objects");
      await api.post("/jobs/import_objects", { release_id: releaseId, obj_dir: r.folder });
    });
  }

  async function onPicked(path: string) {
    const pick = picking;
    setPicking(null);
    if (!pick) return;
    if (pick.what === "mapped") {
      await act(() => api.put("/test-builds/mapped/folder", { folder: path }));
    } else if (pick.what === "main") {
      await act(async () => {
        const r = await api.post<{ status: string; files: number; missing: string[] }>(
          "/test-builds/main/capture",
          { folder: path },
        );
        toast(
          r.status === "complete"
            ? `Captured Main: ${r.files} files.`
            : `Main is incomplete. Missing: ${r.missing.join(", ")}`,
        );
      });
    } else if (pick.what === "field") {
      setDraft((d) => ({ ...d, [pick.field]: path }));
    } else if (pick.what === "import") {
      const warnOutput = pick.kind === "objects" && main?.capture_status === "complete";
      if (warnOutput) {
        const ok = await confirm({
          title: "Use this folder?",
          message:
            "After a run, a build folder can hold objects from the last test build. " +
            "Import from the Main capture unless this folder is clean.",
          confirmLabel: "Import from this folder",
        });
        if (!ok) return;
      }
      await act(() =>
        pick.kind === "source"
          ? api.post("/jobs/import_source", { release_id: releaseId, source_dir: path })
          : api.post("/jobs/import_objects", { release_id: releaseId, obj_dir: path }),
      );
    } else if (pick.what === "artifact") {
      const owner = builds.find((b) => b.id === artifactScope);
      const base = owner?.output_dir;
      const relative =
        base && path.startsWith(base) ? path.slice(base.length).replace(/^[/\\]+/, "") : path;
      await saveArtifacts([
        ...artifacts,
        {
          path: relative,
          is_folder: pick.isFolder,
          include_patterns: pick.isFolder ? "**/*" : "",
          exclude_patterns: "",
          role: "Flash",
        },
      ]);
    }
  }

  const main = builds.find((b) => b.is_main);
  const tests = builds.filter((b) => !b.is_main);
  const editingBuild = builds.find((b) => b.id === editing);
  const blockingProblems = (report?.problems ?? []).filter(
    (p) => p.test_build_id == null || !dropped.has(p.test_build_id),
  );

  if (error) return <div className="center-msg">{error}</div>;

  return (
    <div className="tb-root">
      <section className="tb-card">
        <div className="tci-panel-head">Mapped release</div>
        <div className="tb-row">
          <span className="tb-mapped-now">
            {mapped?.build_name
              ? `Holds ${mapped.release_name} ${mapped.build_name}`
              : "Holds nothing from the app yet"}
          </span>
          <code className="tb-path">{mapped?.folder || "No folder chosen"}</code>
          <button className="tci-mini-btn" onClick={() => setPicking({ what: "mapped" })}>
            Choose folder…
          </button>
        </div>
        <div className="tb-row">
          <select
            value={mapTarget ?? ""}
            onChange={(e) => setMapTarget(Number(e.target.value) || null)}
          >
            <option value="">Choose a build…</option>
            {mapped?.targets.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
          <button
            className="tci-mini-btn"
            disabled={mapTarget == null || !mapped?.folder || switchJob != null}
            onClick={() => startSwitch()}
          >
            {switchJob ? "Switching…" : "Switch"}
          </button>
        </div>
        {mapped?.warning && <div className="tb-note warn">{mapped.warning}</div>}
        {mapped?.interrupted && (
          <div className="tb-note warn">
            A switch to {mapped.interrupted.target} was interrupted.{" "}
            <button
              className="tci-mini-btn"
              onClick={() =>
                act(() => api.post("/test-builds/mapped/recover", { action: "finish" }))
              }
            >
              Finish it
            </button>{" "}
            <button
              className="tci-mini-btn"
              onClick={() =>
                act(() => api.post("/test-builds/mapped/recover", { action: "back" }))
              }
            >
              Go back to {mapped.interrupted.previous || "the previous files"}
            </button>
          </div>
        )}
      </section>

      {lock && !lock.ok && (
        <section className="tb-card">
          <div className="tb-note warn">
            {lock.message} Importing later is always possible.
            <button
              className="tci-mini-btn"
              disabled={!canEdit || !lock.missing.includes("source")}
              onClick={() => setPicking({ what: "import", kind: "source" })}
            >
              Import source…
            </button>
            <button
              className="tci-mini-btn"
              disabled={!canEdit || !lock.missing.includes(".o files")}
              onClick={() => setPicking({ what: "import", kind: "objects" })}
            >
              Import .o files…
            </button>
            {main?.capture_status === "complete" && lock.missing.includes(".o files") && (
              <button
                className="tci-mini-btn"
                disabled={!canEdit}
                onClick={importObjectsFromMain}
              >
                Import from Main capture
              </button>
            )}
          </div>
        </section>
      )}

      <section className="tb-card">
        <div className="tci-panel-head">Builds for the selected release</div>
        <table className="tb-table">
          <thead>
            <tr>
              <th />
              <th>Build</th>
              <th>Capture</th>
              <th>Files</th>
              <th>Size</th>
              <th>Captured</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr>
              <td />
              <td>Main</td>
              <td>{statusLabel(main?.capture_status ?? "none")}</td>
              <td>{main?.files ?? 0}</td>
              <td>{formatSize(main?.size ?? 0)}</td>
              <td>{main?.captured_at || "—"}</td>
              <td className="tb-actions">
                <button
                  className="tci-mini-btn"
                  disabled={!canEdit}
                  onClick={() => setPicking({ what: "main" })}
                >
                  Capture from folder…
                </button>
                {main && (
                  <button className="tci-mini-btn" onClick={() => startEdit(main)}>
                    Edit
                  </button>
                )}
              </td>
            </tr>
            {tests.map((b) => (
              <tr key={b.id} className={b.id === editing ? "sel" : ""}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Include ${b.name} in the run`}
                    checked={checked.has(b.id)}
                    onChange={(e) => {
                      const next = new Set(checked);
                      if (e.target.checked) next.add(b.id);
                      else next.delete(b.id);
                      setChecked(next);
                    }}
                  />
                </td>
                <td>{b.name}</td>
                <td>{statusLabel(b.capture_status)}</td>
                <td>{b.files}</td>
                <td>{formatSize(b.size)}</td>
                <td>{b.captured_at || "—"}</td>
                <td className="tb-actions">
                  <button className="tci-mini-btn" onClick={() => startEdit(b)}>
                    Edit
                  </button>
                  <button
                    className="tci-mini-btn danger"
                    disabled={!canEdit}
                    onClick={() => deleteBuild(b)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="tb-row">
          <input
            placeholder="New build name, such as Watchdog build"
            value={newName}
            disabled={!canEdit}
            onChange={(e) => setNewName(e.target.value)}
          />
          <select
            value={newProject ?? ""}
            disabled={!canEdit}
            onChange={(e) => setNewProject(Number(e.target.value) || null)}
          >
            <option value="">Test project…</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <button
            className="tci-mini-btn"
            disabled={!canEdit || !newName.trim() || newProject == null}
            onClick={createBuild}
          >
            Add build
          </button>
          <span className="tb-spacer" />
          {running ? (
            <button
              className="tci-mini-btn danger"
              onClick={() => api.post(`/jobs/${runJob?.job_id}/cancel`)}
            >
              Cancel run
            </button>
          ) : (
            <button
              className="tci-mini-btn"
              disabled={!canEdit || checked.size === 0}
              onClick={openPreflight}
            >
              Check and run…
            </button>
          )}
        </div>
      </section>

      {editingBuild && (
        <section className="tb-card">
          <div className="tci-panel-head">{editingBuild.name} settings</div>
          <div className="tb-form">
            {!editingBuild.is_main && (
              <label>
                Name
                <input
                  value={draft.name ?? ""}
                  disabled={!canEdit}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                />
              </label>
            )}
            <label>
              Build command
              <input
                value={draft.build_command ?? ""}
                disabled={!canEdit}
                onChange={(e) => setDraft({ ...draft, build_command: e.target.value })}
              />
            </label>
            <label>
              Clean command
              <input
                value={draft.clean_command ?? ""}
                disabled={!canEdit}
                onChange={(e) => setDraft({ ...draft, clean_command: e.target.value })}
              />
            </label>
            <label>
              Terminal
              <select
                value={draft.terminal ?? "bash"}
                disabled={!canEdit}
                onChange={(e) =>
                  setDraft({ ...draft, terminal: e.target.value as TerminalKind })
                }
              >
                {TERMINALS.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </label>
            {(
              [
                ["build_cwd", "Working directory"],
                ["source_dir", "Source folder"],
                ["output_dir", "Build output folder"],
              ] as const
            ).map(([field, label]) => (
              <label key={field}>
                {label}
                <span className="tb-pickfield">
                  <input
                    value={(draft[field] as string) ?? ""}
                    disabled={!canEdit}
                    onChange={(e) => setDraft({ ...draft, [field]: e.target.value })}
                  />
                  <button
                    className="tci-mini-btn"
                    disabled={!canEdit}
                    onClick={() => setPicking({ what: "field", field })}
                  >
                    Browse…
                  </button>
                </span>
              </label>
            ))}
          </div>
          <div className="tb-row">
            <button className="tci-mini-btn" disabled={!canEdit} onClick={saveDraft}>
              Save settings
            </button>
          </div>
        </section>
      )}

      <section className="tb-card">
        <div className="tci-panel-head">Tracked files</div>
        <div className="tb-row">
          <select
            value={artifactScope ?? ""}
            onChange={(e) => setArtifactScope(Number(e.target.value) || null)}
          >
            <option value="">Project-wide list</option>
            {builds.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name} only
              </option>
            ))}
          </select>
          <span className="tb-hint">
            A build with its own list uses only that list. Paths are relative to the build
            output folder.
          </span>
        </div>
        {artifacts.length > 0 && (
          <table className="tb-table">
            <thead>
              <tr>
                <th>Path</th>
                <th>Role</th>
                <th>Include</th>
                <th>Exclude</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {artifacts.map((a, i) => (
                <tr key={`${a.path}-${i}`}>
                  <td>
                    {a.is_folder ? "📁 " : ""}
                    {a.path}
                  </td>
                  <td>
                    <select
                      value={a.role}
                      disabled={!canEdit}
                      onChange={(e) => {
                        const next = artifacts.map((x, j) =>
                          j === i ? { ...x, role: e.target.value } : x,
                        );
                        saveArtifacts(next);
                      }}
                    >
                      {ROLES.map((r) => (
                        <option key={r}>{r}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      value={a.include_patterns}
                      disabled={!canEdit || !a.is_folder}
                      onChange={(e) => patchArtifact(i, { include_patterns: e.target.value })}
                      onBlur={() => saveArtifacts(artifacts)}
                    />
                  </td>
                  <td>
                    <input
                      value={a.exclude_patterns}
                      disabled={!canEdit || !a.is_folder}
                      onChange={(e) => patchArtifact(i, { exclude_patterns: e.target.value })}
                      onBlur={() => saveArtifacts(artifacts)}
                    />
                  </td>
                  <td className="tb-actions">
                    <button
                      className="tci-mini-btn danger"
                      disabled={!canEdit}
                      onClick={() => saveArtifacts(artifacts.filter((_, j) => j !== i))}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="tb-row">
          <button
            className="tci-mini-btn"
            disabled={!canEdit}
            onClick={() => setPicking({ what: "artifact", isFolder: false })}
          >
            Add file…
          </button>
          <button
            className="tci-mini-btn"
            disabled={!canEdit}
            onClick={() => setPicking({ what: "artifact", isFolder: true })}
          >
            Add folder…
          </button>
        </div>
      </section>

      <section className="tb-card">
        <div className="tci-panel-head">Runs</div>
        {unfinished && !running && (
          <div className="tb-note warn">
            A run from {unfinished.started_at} didn't finish. Resuming starts from the first
            unfinished build.
            <button
              className="tci-mini-btn"
              disabled={!canEdit}
              onClick={() => {
                setLogLines([]);
                act(() =>
                  api.post("/jobs/test_build_run", { resume_run_id: unfinished.id }),
                );
              }}
            >
              Resume
            </button>
          </div>
        )}
        {running && (
          <div className="tb-note">
            {runJob?.message || "Starting…"}
            {runJob?.progress != null ? ` (${Math.round(runJob.progress)}%)` : ""}
          </div>
        )}
        {logLines.length > 0 && (
          <pre className="tb-log" ref={logRef}>
            {logLines.join("\n")}
          </pre>
        )}
        {runs.length === 0 && <div className="tci-empty">No runs yet.</div>}
        {runs.map((run) => (
          <details key={run.id} className="tb-run" open={run === runs[0]}>
            <summary>
              {run.started_at} · {run.username} · {statusLabel(run.status)}
            </summary>
            <table className="tb-table">
              <tbody>
                {run.items.map((item) => (
                  <tr key={item.id}>
                    <td>{item.build_name}</td>
                    <td className={`tb-status ${item.status}`}>{statusLabel(item.status)}</td>
                    <td>
                      {item.message}
                      {item.conflicts.length
                        ? ` ${item.conflicts.length} hook conflict(s).`
                        : ""}
                    </td>
                    <td className="tb-actions">
                      {item.has_log && (
                        <button
                          className="tci-mini-btn"
                          onClick={async () => {
                            const text = await api.text(`/test-builds/runs/log/${item.id}`);
                            setViewedLog({ name: item.build_name, text });
                          }}
                        >
                          Log
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        ))}
      </section>

      {store && (
        <section className="tb-card">
          <div className="tci-panel-head">Binary store</div>
          <div className="tb-row">
            <code className="tb-path">{store.path}</code>
            <span>
              {store.blobs} files, {formatSize(store.bytes)}
              {store.unreferenced ? `, ${store.unreferenced} unused` : ""}
            </span>
            <button
              className="tci-mini-btn"
              disabled={!canEdit || store.unreferenced === 0}
              onClick={() => act(() => api.post("/test-builds/store/cleanup"))}
            >
              Clean up
            </button>
          </div>
        </section>
      )}

      {report && (
        <div className="modal-overlay">
          <div className="modal tb-modal">
            <div className="modal-head">Before the run</div>
            <div className="tb-modal-body">
              <div className="tb-hint">
                Order: {report.items.map((i) => i.build_name).join(", ")}
              </div>
              {lockReleases.length > 0 && (
                <label className="tb-continue">
                  <input
                    type="checkbox"
                    checked={continueAnyway}
                    onChange={(e) => {
                      setContinueAnyway(e.target.checked);
                      refreshPreflight(e.target.checked, dropped);
                    }}
                  />
                  Continue anyway. Clean source or .o files are missing, and this is recorded
                  under your name.
                </label>
              )}
              {continueAnyway && (
                <input
                  placeholder="Reason, such as: the release hasn't been built yet"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              )}
              {report.problems.length === 0 && <div>No problems found.</div>}
              {report.problems.map((p, i) => (
                <div key={i} className="tb-problem">
                  <span>
                    {p.build_name ? `${p.build_name}: ` : ""}
                    {p.problem}
                  </span>
                  {p.test_build_id != null && checked.has(p.test_build_id) && (
                    <button
                      className="tci-mini-btn"
                      onClick={() => {
                        const next = new Set(dropped);
                        next.add(p.test_build_id as number);
                        setDropped(next);
                        refreshPreflight(continueAnyway, next);
                      }}
                    >
                      Drop this build
                    </button>
                  )}
                </div>
              ))}
              {report.warnings.map((w, i) => (
                <div key={i} className="tb-hint">
                  {w.build_name}: {w.warning}
                </div>
              ))}
            </div>
            <div className="confirm-foot">
              <button className="tci-mini-btn" onClick={() => setReport(null)}>
                Cancel
              </button>
              <button
                className="tci-mini-btn"
                disabled={blockingProblems.length > 0}
                onClick={startRun}
              >
                Start run
              </button>
            </div>
          </div>
        </div>
      )}

      {viewedLog && (
        <div className="modal-overlay" onMouseDown={() => setViewedLog(null)}>
          <div className="modal tb-modal" onMouseDown={(e) => e.stopPropagation()}>
            <div className="modal-head">{viewedLog.name} log</div>
            <pre className="tb-log tall">{viewedLog.text}</pre>
            <div className="confirm-foot">
              <button className="tci-mini-btn" onClick={() => setViewedLog(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {picking && (
        <FolderPicker
          mode={picking.what === "artifact" && !picking.isFolder ? "import" : "folder"}
          exts={picking.what === "artifact" && !picking.isFolder ? ARTIFACT_EXTS : undefined}
          title={
            picking.what === "mapped"
              ? "Choose the folder your debugger points at"
              : picking.what === "main"
                ? "Choose the clean build output folder"
                : undefined
          }
          onCancel={() => setPicking(null)}
          onConfirm={(path) => onPicked(path)}
        />
      )}
      {confirmNode}
    </div>
  );
}
