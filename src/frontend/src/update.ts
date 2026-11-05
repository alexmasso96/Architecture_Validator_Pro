// In-app updates. Two parts:
//
// * useUpdateCheck — asks the worker (/api/update/check) what's newest on the
//   chosen channel: the latest GitHub release, or the latest CI build of a
//   branch. App mounts it once and threads the result to the launcher pill,
//   Preferences and the changelog.
// * useUpdateInstaller — downloads that build (the update_download job: SHA-256
//   checked, unpacked into the app-data folder) and then asks the desktop shell
//   to install it, which closes the app and starts the new one. A shared store,
//   so the pill, the changelog and Preferences show the same progress.
//
// Everything goes through the worker: the desktop shell runs WebView2 with
// --no-proxy-server (loopback must stay direct on corporate proxy networks), so
// the webview itself may have no internet access — the worker resolves the
// system proxy instead. A direct GitHub release fetch remains as fallback for a
// plain dev browser without the relay (the Releases API serves permissive CORS).

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { api, ApiError } from "./api/client";

// The version of this SPA. Keep in lockstep with package.json /
// RELEASE_NOTES.md on every release. The worker reports the build's own
// version too (from build_info.json); that one wins when present.
export const APP_VERSION = "3.4.0";

export const GITHUB_REPO = "alexmasso96/Architecture_Validator_Pro";
export const RELEASES_API = `https://api.github.com/repos/${GITHUB_REPO}/releases/latest`;

// The worker gives up on GitHub after 15 s per call; a branch check makes a few.
const RELAY_TIMEOUT_MS = 45_000;
// A network that isn't up yet at launch shouldn't leave the pill failed all session.
const RETRY_DELAY_MS = 30_000;
const JOB_POLL_MS = 500;

export type UpdateStatus =
  | "idle"
  | "checking"
  | "latest"
  | "update-available"
  | "error";

export type UpdateChannel = "release" | "branch";

export interface UpdateInfo {
  status: UpdateStatus;
  channel?: UpdateChannel;
  branch?: string;
  latestVersion?: string; // releases: normalised, no leading "v"
  label?: string; // "v3.5.0" or "main@1a2b3c4"
  body?: string; // release notes markdown (releases only)
  url?: string; // the release / Actions run on GitHub
  title?: string; // branch builds: the commit's title
  conclusion?: string | null; // branch builds: the run's result
  publishedAt?: string | null;
  current?: { version: string; commit: string | null; branch: string | null; frozen: boolean };
  canInstall?: boolean; // this build can update itself
  installReason?: string; // why not, when it can't
  downloadable?: boolean; // the newest build has a download for this OS
  tokenConfigured?: boolean;
  staged?: StagedUpdate | null;
  errorMsg?: string;
}

export interface StagedUpdate {
  channel: UpdateChannel;
  version: string | null;
  label: string;
  commit: string | null;
}

interface CheckResponse {
  channel: UpdateChannel;
  branch: string;
  token_configured: boolean;
  current: { version: string; commit: string | null; branch: string | null; frozen: boolean };
  install: { supported: boolean; reason: string };
  available: boolean;
  latest: null | {
    version: string | null;
    label: string;
    body: string;
    url: string;
    title?: string;
    conclusion?: string | null;
    published_at?: string | null;
    downloadable: boolean;
  };
  staged: StagedUpdate | null;
}

// GitHub → compare URL between the running version and the latest release.
export function compareUrl(latestVersion: string, current = APP_VERSION): string {
  return `https://github.com/${GITHUB_REPO}/compare/V${current}...V${latestVersion}`;
}

// "v2.1.1" / "2.1.1-rc1" → [2, 1, 1] (pre-release suffix dropped for ordering).
function parseVersion(v: string): number[] {
  return v.trim().replace(/^v/i, "").split("-")[0].split(".").map((n) => parseInt(n, 10) || 0);
}

// Is `latest` strictly newer than `current`? Component-wise numeric compare.
export function isNewer(latest: string, current: string): boolean {
  const a = parseVersion(latest);
  const b = parseVersion(current);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0);
    if (diff !== 0) return diff > 0;
  }
  return false;
}

function withTimeout<T>(p: Promise<T>, ms: number, msg: string): Promise<T> {
  return Promise.race([
    p,
    new Promise<never>((_, reject) => setTimeout(() => reject(new ApiError(504, msg)), ms)),
  ]);
}

// Plain dev browser without the worker relay: the latest release, read directly.
async function directReleaseCheck(): Promise<UpdateInfo> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 12_000);
  try {
    const res = await fetch(RELEASES_API, {
      headers: { Accept: "application/vnd.github+json" },
      signal: ctrl.signal,
    });
    if (!res.ok) {
      throw new Error(
        res.status === 404 ? "No published releases found." : `GitHub API responded ${res.status}.`,
      );
    }
    const json = await res.json();
    const tag = String(json.tag_name ?? "");
    if (!tag) throw new Error("Release has no version tag.");
    const latestVersion = tag.replace(/^v/i, "");
    return {
      status: isNewer(tag, APP_VERSION) ? "update-available" : "latest",
      channel: "release",
      latestVersion,
      label: `v${latestVersion}`,
      body: String(json.body ?? ""),
      url: String(json.html_url ?? ""),
      canInstall: false,
      installReason: "Updates install only in the desktop app.",
    };
  } catch (e) {
    if ((e as Error).name === "AbortError") throw new Error("Timed out connecting to GitHub.");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function fromCheck(r: CheckResponse): UpdateInfo {
  const l = r.latest;
  return {
    status: r.available ? "update-available" : "latest",
    channel: r.channel,
    branch: r.branch,
    latestVersion: l?.version ?? undefined,
    label: l?.label,
    body: l?.body || undefined,
    url: l?.url,
    title: l?.title,
    conclusion: l?.conclusion,
    publishedAt: l?.published_at,
    current: r.current,
    canInstall: r.install.supported,
    installReason: r.install.reason,
    downloadable: l?.downloadable ?? false,
    tokenConfigured: r.token_configured,
    staged: r.staged,
  };
}

async function fetchUpdate(): Promise<UpdateInfo> {
  try {
    const r = await withTimeout(
      api.get<CheckResponse>("/update/check"),
      RELAY_TIMEOUT_MS,
      "Timed out waiting for GitHub.",
    );
    return fromCheck(r);
  } catch (e) {
    if (e instanceof ApiError) {
      // The relay answered: its verdict is final (it already timed out /
      // classified the GitHub failure). A direct fetch from the webview
      // would just hang without a proxy.
      throw new Error(e.detail);
    }
    // Relay not reachable (plain dev browser) → fall through to direct fetch.
  }
  return directReleaseCheck();
}

// Owns the update-check lifecycle. App mounts this once and threads the result
// down to the launcher card + Preferences. `check` re-runs it (manual button,
// or after the channel changes).
export function useUpdateCheck(): { update: UpdateInfo; check: () => Promise<boolean> } {
  const [update, setUpdate] = useState<UpdateInfo>({ status: "idle" });

  const check = useCallback(async () => {
    setUpdate((u) => ({ ...u, status: "checking", errorMsg: undefined }));
    try {
      setUpdate(await fetchUpdate());
      return true;
    } catch (e) {
      setUpdate((u) => ({ channel: u.channel, branch: u.branch, status: "error",
        errorMsg: (e as Error).message }));
      return false;
    }
  }, []);

  // Silent background check on first mount, with one retry if it fails.
  useEffect(() => {
    let timer: number | undefined;
    check().then((ok) => {
      if (!ok) timer = window.setTimeout(check, RETRY_DELAY_MS);
    });
    return () => window.clearTimeout(timer);
  }, [check]);

  return { update, check };
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export interface UpdateSettings {
  channel: UpdateChannel;
  branch: string;
  token_configured: boolean;
}

export function saveUpdateSettings(patch: Partial<{ channel: UpdateChannel; branch: string; token: string }>) {
  return api.put<UpdateSettings>("/update/settings", patch);
}

export async function listBranches(): Promise<string[]> {
  return (await api.get<{ branches: string[] }>("/update/branches")).branches;
}

// ---------------------------------------------------------------------------
// Download + install (shared store)
// ---------------------------------------------------------------------------

export type InstallPhase = "idle" | "downloading" | "ready" | "installing" | "error";

export interface InstallerState {
  phase: InstallPhase;
  progress: number | null; // 0..100 while downloading
  message: string;
  label?: string; // what's downloaded / downloading
  error?: string;
}

let state: InstallerState = { phase: "idle", progress: null, message: "" };
const listeners = new Set<() => void>();
let activeJob: string | null = null;

function set(next: Partial<InstallerState>) {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

interface JobSnapshot {
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  progress: number | null;
  message: string;
  result?: StagedUpdate;
  error?: string;
}

async function download(): Promise<boolean> {
  if (state.phase === "downloading") return false;
  set({ phase: "downloading", progress: null, message: "Starting download…", error: undefined });
  try {
    const job = await api.post<{ job_id: string }>("/jobs/update_download", {});
    activeJob = job.job_id;
    for (;;) {
      await new Promise((r) => setTimeout(r, JOB_POLL_MS));
      const s = await api.get<JobSnapshot>(`/jobs/${job.job_id}`);
      if (s.status === "done") {
        activeJob = null;
        set({ phase: "ready", progress: 100, message: "Ready to install", label: s.result?.label });
        return true;
      }
      if (s.status === "failed" || s.status === "cancelled") {
        activeJob = null;
        set({ phase: "error", progress: null, message: "",
          error: s.status === "cancelled" ? "Download cancelled." : s.error || "Download failed." });
        return false;
      }
      set({ progress: s.progress, message: s.message || "Downloading…" });
    }
  } catch (e) {
    activeJob = null;
    set({ phase: "error", progress: null, message: "", error: (e as Error).message });
    return false;
  }
}

async function cancelDownload() {
  if (activeJob) await api.post(`/jobs/${activeJob}/cancel`).catch(() => undefined);
}

// Jobs that would be cut short by the restart (an overnight test build, say).
async function busyJobs(): Promise<string[]> {
  try {
    return (await api.get<{ busy: string[] }>("/update/busy")).busy;
  } catch {
    return [];
  }
}

async function install(): Promise<void> {
  const bridge = window.pywebview?.api;
  if (!bridge?.install_update) {
    set({ phase: "error", error: "Updates install only in the desktop app." });
    return;
  }
  set({ phase: "installing", message: "Closing to install…", error: undefined });
  const res = await bridge.install_update();
  if (!res?.ok) {
    set({ phase: "error", message: "", error: res?.error || "The update couldn't start." });
  }
}

// Called when the check finds a build already downloaded, so a restart of the
// app still offers "Restart to install".
function adoptStaged(staged: StagedUpdate | null | undefined, latestLabel?: string) {
  if (state.phase === "downloading" || state.phase === "installing") return;
  if (staged && staged.label === latestLabel) {
    set({ phase: "ready", progress: 100, message: "Ready to install", label: staged.label });
  } else if (state.phase === "ready" && latestLabel && state.label !== latestLabel) {
    // A newer build appeared since this one was downloaded.
    set({ phase: "idle", progress: null, message: "", label: undefined });
  }
}

function reset() {
  if (state.phase === "error") set({ phase: "idle", error: undefined, message: "" });
}

export function useUpdateInstaller() {
  const s = useSyncExternalStore(subscribe, () => state);
  return { state: s, download, cancelDownload, install, busyJobs, adoptStaged, reset };
}

export async function fetchLastUpdateResult(): Promise<null | {
  ok: boolean;
  error: string;
  to?: { label?: string | null; version?: string | null };
}> {
  try {
    return (await api.get<{ result: never }>("/update/result")).result;
  } catch {
    return null;
  }
}
