import { useEffect, useState } from "react";

import {
  APP_VERSION,
  compareUrl,
  listBranches,
  saveUpdateSettings,
  useUpdateInstaller,
  type UpdateChannel,
  type UpdateInfo,
} from "../update";
import { useConfirm } from "./ConfirmDialog";

const JOB_NAMES: Record<string, string> = {
  test_build_run: "a test build run",
  generate_tests: "AI test generation",
  build_mind_map: "a mind map build",
  build_code_map: "a code map build",
  parse_elf: "an ELF parse",
  import_symbols: "a symbol import",
  import_source: "a source import",
  import_objects: "an object-file import",
  run_calltree_tests: "call-tree tests",
  share_project: "sharing a project",
  mapped_switch: "a build switch",
};

function shortSha(sha?: string | null) {
  return sha ? sha.slice(0, 7) : "";
}

// The download → restart controls for an available update. Shared by
// Preferences → Updates and the changelog opened from the launcher pill.
export function UpdateActions({ update }: { update: UpdateInfo }) {
  const { state, download, cancelDownload, install, busyJobs, adoptStaged, reset } =
    useUpdateInstaller();
  const { confirm, confirmNode } = useConfirm();

  // A build downloaded before the app was restarted can still be installed.
  useEffect(() => {
    if (update.status === "update-available" || update.status === "latest") {
      adoptStaged(update.staged, update.label);
    }
  }, [update.status, update.staged, update.label, adoptStaged]);

  if (update.status !== "update-available") return null;

  if (!update.canInstall) {
    return update.installReason ? (
      <span className="update-detail">{update.installReason}</span>
    ) : null;
  }
  if (!update.downloadable) {
    return <span className="update-detail">This build has no download for your system yet.</span>;
  }
  if (update.channel === "branch" && !update.tokenConfigured) {
    return (
      <span className="update-detail">
        Add a GitHub token in Preferences → Updates to download branch builds.
      </span>
    );
  }

  async function restart() {
    const busy = await busyJobs();
    const what = busy.map((k) => JOB_NAMES[k] ?? k).join(", ");
    const ok = await confirm({
      title: "Restart to update?",
      message: (
        <>
          Architecture Validator closes, installs {state.label ?? update.label} and opens
          again. Your project is saved as you work, and its lock is released.
          {busy.length > 0 && (
            <>
              <br />
              <br />
              <strong>Still running: {what}.</strong> It will be stopped.
            </>
          )}
        </>
      ),
      confirmLabel: "Restart and install",
      danger: busy.length > 0,
    });
    if (ok) await install();
  }

  return (
    <div className="update-install">
      {state.phase === "idle" && (
        <button className="save-btn" onClick={() => download()}>
          ⤓ Download update
        </button>
      )}
      {state.phase === "downloading" && (
        <>
          <div className="update-detail">{state.message}</div>
          <div className="job-bar">
            <div
              className={"job-bar-fill" + (state.progress == null ? " indeterminate" : "")}
              style={state.progress == null ? undefined : { width: `${state.progress}%` }}
            />
          </div>
          <button className="prefs-prefill" onClick={() => cancelDownload()}>
            Cancel
          </button>
        </>
      )}
      {state.phase === "ready" && (
        <button className="save-btn" onClick={restart}>
          ↻ Restart to install {state.label}
        </button>
      )}
      {state.phase === "installing" && (
        <span className="update-detail">
          <span className="spin" /> Closing to install…
        </span>
      )}
      {state.phase === "error" && (
        <>
          <span className="update-detail">⚠ {state.error}</span>
          <button className="prefs-prefill" onClick={reset}>
            Try again
          </button>
        </>
      )}
      {confirmNode}
    </div>
  );
}

// Preferences → Updates.
export function UpdatesSettings({
  update,
  onRecheck,
  onShowChangelog,
}: {
  update: UpdateInfo;
  onRecheck: () => void;
  onShowChangelog: () => void;
}) {
  const [channel, setChannel] = useState<UpdateChannel>(update.channel ?? "release");
  const [branch, setBranch] = useState(update.branch ?? "main");
  const [branches, setBranches] = useState<string[] | null>(null);
  const [branchError, setBranchError] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [tokenSet, setTokenSet] = useState(Boolean(update.tokenConfigured));
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const { state: installer } = useUpdateInstaller();
  const locked = installer.phase === "downloading" || installer.phase === "installing";

  // Follow the check once it has reported the stored settings.
  useEffect(() => {
    if (update.channel) setChannel(update.channel);
    if (update.branch) setBranch(update.branch);
    if (update.tokenConfigured !== undefined) setTokenSet(update.tokenConfigured);
  }, [update.channel, update.branch, update.tokenConfigured]);

  useEffect(() => {
    if (channel !== "branch" || branches) return;
    listBranches()
      .then((b) => {
        setBranches(b);
        setBranchError(null);
      })
      .catch((e) => setBranchError((e as Error).message));
  }, [channel, branches]);

  async function save(patch: Parameters<typeof saveUpdateSettings>[0], recheck = true) {
    setSaving(true);
    setNote(null);
    try {
      const s = await saveUpdateSettings(patch);
      setTokenSet(s.token_configured);
      if (recheck) onRecheck();
      return true;
    } catch (e) {
      setNote((e as Error).message);
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function pickChannel(c: UpdateChannel) {
    setChannel(c);
    await save({ channel: c, ...(c === "branch" ? { branch } : {}) });
  }

  async function pickBranch(b: string) {
    setBranch(b);
    await save({ branch: b });
  }

  async function saveToken(value: string) {
    if (await save({ token: value })) {
      setToken("");
      setNote(value ? "Token saved." : "Token removed.");
    }
  }

  const current = update.current;
  const currentVersion = current?.version ?? APP_VERSION;
  const branchOptions = branches ?? [branch];
  if (!branchOptions.includes(branch)) branchOptions.unshift(branch);

  return (
    <div className="prefs-body">
      <div className="prefs-field">
        <div className="prefs-label">Current version</div>
        <div className="update-version">v{currentVersion}</div>
        {current?.commit && (
          <div className="update-detail">
            Built from {current.branch ? `${current.branch}@` : ""}
            {shortSha(current.commit)}
          </div>
        )}
      </div>

      <div className="prefs-field">
        <div className="prefs-label">Update from</div>
        <div className="prefs-hint">
          Releases are the published versions. Branch builds are the latest CI build
          of a branch, for trying out work before it's released.
        </div>
        <div className="prefs-row update-channel">
          <div className="seg">
            <button
              className={channel === "release" ? "active" : ""}
              disabled={saving || locked}
              onClick={() => pickChannel("release")}
            >
              Releases
            </button>
            <button
              className={channel === "branch" ? "active" : ""}
              disabled={saving || locked}
              onClick={() => pickChannel("branch")}
            >
              Branch builds
            </button>
          </div>
          {channel === "branch" && (
            <select
              className="prefs-input"
              value={branch}
              disabled={saving || locked}
              onChange={(e) => pickBranch(e.target.value)}
            >
              {branchOptions.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          )}
        </div>
        {channel === "branch" && branchError && (
          <div className="update-detail">⚠ Couldn't list branches: {branchError}</div>
        )}
      </div>

      {channel === "branch" && (
        <div className="prefs-field">
          <div className="prefs-label">GitHub token</div>
          <div className="prefs-hint">
            GitHub only hands out CI builds to a signed-in user. Any token that can
            read this repository works — a fine-grained token with read-only access
            to public repositories is enough. Stored encrypted, per user.
          </div>
          <div className="prefs-row">
            <input
              className="prefs-input"
              type="password"
              placeholder={tokenSet ? "•••••••• (saved)" : "github_pat_…"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              disabled={saving}
            />
            <button
              className="save-btn"
              disabled={saving || !token.trim()}
              onClick={() => saveToken(token.trim())}
            >
              Save
            </button>
            {tokenSet && (
              <button className="prefs-prefill" disabled={saving} onClick={() => saveToken("")}>
                Remove
              </button>
            )}
          </div>
        </div>
      )}
      {note && <div className="update-detail">{note}</div>}

      <div className="prefs-field">
        <div className="prefs-row">
          <div className="prefs-label">Status</div>
          <button
            className="prefs-prefill"
            disabled={update.status === "checking"}
            onClick={onRecheck}
          >
            {update.status === "checking" ? "Checking…" : "Check for updates"}
          </button>
        </div>

        {update.status === "update-available" && (
          <div className="update-row available">
            <span className="update-badge warn">⤓ Update available</span>
            <span className="update-detail">
              {update.channel === "branch" ? (
                <>
                  {update.label}
                  {update.title ? ` — ${update.title}` : ""}
                  {update.conclusion && update.conclusion !== "success"
                    ? ` (CI: ${update.conclusion})`
                    : ""}
                </>
              ) : (
                <>{update.label} is the latest release.</>
              )}
            </span>
            <UpdateActions update={update} />
            <div className="update-actions">
              {update.body && (
                <button className="prefs-prefill" onClick={onShowChangelog}>
                  View changelog
                </button>
              )}
              {update.channel === "branch" && update.url ? (
                <a className="update-compare" href={update.url} target="_blank" rel="noreferrer">
                  View the CI run →
                </a>
              ) : (
                update.latestVersion && (
                  <a
                    className="update-compare"
                    href={compareUrl(update.latestVersion, currentVersion)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Compare on GitHub →
                  </a>
                )
              )}
            </div>
          </div>
        )}
        {update.status === "latest" && (
          <div className="update-row">
            <span className="update-badge ok">✓ Up to date</span>
            <span className="update-detail">
              {update.channel === "branch"
                ? `You're running the latest build of ${update.branch}.`
                : "You're running the latest published release."}
            </span>
          </div>
        )}
        {update.status === "error" && (
          <div className="update-row">
            <span className="update-badge warn">⚠ Check failed</span>
            <span className="update-detail">{update.errorMsg ?? "Could not reach GitHub."}</span>
          </div>
        )}
        {(update.status === "checking" || update.status === "idle") && (
          <div className="update-row">
            <span className="update-detail">
              <span className="spin" /> Contacting GitHub…
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
