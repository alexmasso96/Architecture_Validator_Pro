import { useEffect, useState } from "react";

import { renderMarkdown } from "../markdown";
import {
  APP_VERSION,
  compareUrl,
  saveUpdateChannel,
  useUpdateInstaller,
  type UpdateChannel,
  type UpdateInfo,
  type UpdateVersion,
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

function shortDate(iso?: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

// The download → restart controls for a version. Shared by Preferences →
// Updates and the changelog opened from the launcher pill.
//
// Without `version` they are for the channel's newest version and only show
// when it's newer than this build. With `version` (one picked from the beta
// list) they show for any version that isn't the one installed.
export function UpdateActions({ update, version }: { update: UpdateInfo; version?: UpdateVersion }) {
  const { state, download, cancelDownload, install, busyJobs, adoptStaged, reset } =
    useUpdateInstaller();
  const { confirm, confirmNode } = useConfirm();

  // A build downloaded before the app was restarted can still be installed.
  useEffect(() => {
    if (update.status === "update-available" || update.status === "latest") {
      adoptStaged(update.staged, update.checkedAt);
    }
  }, [update.status, update.staged, update.checkedAt, adoptStaged]);

  if (version ? version.relation === "installed" : update.status !== "update-available") return null;

  const label = version?.label ?? update.label ?? "the update";
  const older = version?.relation === "older";

  if (!update.canInstall) {
    return update.installReason ? (
      <span className="update-detail">{update.installReason}</span>
    ) : null;
  }
  if (!(version ? version.downloadable : update.downloadable)) {
    return <span className="update-detail">{label} has no download for your system.</span>;
  }

  async function start() {
    if (older) {
      const ok = await confirm({
        title: `Go back to ${label}?`,
        message: (
          <>
            {label} is older than the version you're running. A project saved with the
            newer version may not open in it.
          </>
        ),
        confirmLabel: `Download ${label}`,
        danger: true,
      });
      if (!ok) return;
    }
    await download(version?.tag, label);
  }

  async function restart() {
    const busy = await busyJobs();
    const what = busy.map((k) => JOB_NAMES[k] ?? k).join(", ");
    const ok = await confirm({
      title: "Restart to update?",
      message: (
        <>
          Architecture Validator closes, installs {state.label ?? label} and opens
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

  // What's downloaded is another version than the one these controls are for.
  const other = state.phase === "ready" && state.label !== label;

  return (
    <div className="update-install">
      {(state.phase === "idle" || other) && (
        <button className="save-btn" onClick={start}>
          ⤓ Download {version ? label : "update"}
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
      {state.phase === "ready" && !other && (
        <button className="save-btn" onClick={restart}>
          ↻ Restart to install {state.label}
        </button>
      )}
      {other && (
        <span className="update-detail">
          {state.label} is downloaded. Downloading {label} replaces it.
        </span>
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

// The beta versions to pick from: the list, then the picked one's change log
// and its download.
function BetaVersions({ update, locked }: { update: UpdateInfo; locked: boolean }) {
  const versions = update.versions ?? [];
  const [picked, setPicked] = useState<string | null>(null);
  const version = versions.find((v) => v.tag === picked) ?? versions[0];
  if (!version) return null;

  return (
    <div className="prefs-field">
      <div className="prefs-label">Beta versions</div>
      <div className="update-versions" role="listbox" aria-label="Beta versions">
        {versions.map((v, i) => (
          <button
            key={v.tag}
            role="option"
            aria-selected={v.tag === version.tag}
            className={"update-version-row" + (v.tag === version.tag ? " sel" : "")}
            disabled={locked}
            onClick={() => setPicked(v.tag)}
          >
            <span className="update-version-name">{v.label}</span>
            <span className="update-version-date">{shortDate(v.published_at)}</span>
            <span className="update-version-tags">
              {!v.prerelease && <span className="update-badge ok">Release</span>}
              {v.relation === "installed" && <span className="update-badge ok">Installed</span>}
              {i === 0 && v.relation === "newer" && <span className="update-badge warn">Newest</span>}
              {v.relation === "older" && <span className="update-version-date">Older</span>}
            </span>
          </button>
        ))}
      </div>

      <div className="update-notes-head">
        <span className="prefs-label">What's new in {version.label}</span>
        {version.url && (
          <a className="update-compare" href={version.url} target="_blank" rel="noreferrer">
            View on GitHub →
          </a>
        )}
      </div>
      {version.body ? (
        <div
          className="changelog-body md update-notes"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(version.body) }}
        />
      ) : (
        <div className="update-detail">This version has no change log.</div>
      )}
      <div className="update-row">
        {version.relation === "installed" ? (
          <span className="update-detail">This is the version you're running.</span>
        ) : (
          <UpdateActions update={update} version={version} />
        )}
      </div>
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
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const { state: installer } = useUpdateInstaller();
  const locked = installer.phase === "downloading" || installer.phase === "installing";

  // Follow the check once it has reported the stored channel.
  useEffect(() => {
    if (update.channel) setChannel(update.channel);
  }, [update.channel]);

  async function pickChannel(c: UpdateChannel) {
    if (c === channel) return;
    setChannel(c);
    setSaving(true);
    setNote(null);
    try {
      await saveUpdateChannel(c);
      onRecheck();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const current = update.current;
  const currentVersion = current?.version ?? APP_VERSION;
  const latest = update.versions?.[0];
  const listed = update.channel === channel && (update.status === "update-available" || update.status === "latest");

  return (
    <div className="prefs-body">
      <div className="prefs-field">
        <div className="prefs-label">Current version</div>
        <div className="update-version">
          v{currentVersion}
          {current?.prerelease && <span className="update-badge warn update-beta">Beta</span>}
        </div>
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
          Releases are the published versions. Beta versions come out before a release,
          for trying changes early. Each one has its own change log.
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
              className={channel === "beta" ? "active" : ""}
              disabled={saving || locked}
              onClick={() => pickChannel("beta")}
            >
              Beta versions
            </button>
          </div>
        </div>
        {note && <div className="update-detail">{note}</div>}
      </div>

      {channel === "beta" && listed && <BetaVersions update={update} locked={locked} />}

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
              {update.label} is the{" "}
              {update.channel === "beta" && latest?.prerelease ? "newest beta version" : "latest release"}.
            </span>
            {/* On the beta channel the list above has the download and the change log. */}
            {update.channel !== "beta" && (
              <>
                <UpdateActions update={update} />
                <div className="update-actions">
                  {update.body && (
                    <button className="prefs-prefill" onClick={onShowChangelog}>
                      View changelog
                    </button>
                  )}
                  {update.latestVersion && (
                    <a
                      className="update-compare"
                      href={compareUrl(update.latestVersion, currentVersion)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Compare on GitHub →
                    </a>
                  )}
                </div>
              </>
            )}
          </div>
        )}
        {update.status === "latest" && (
          <div className="update-row">
            <span className="update-badge ok">✓ Up to date</span>
            <span className="update-detail">
              {update.channel === "beta"
                ? latest?.prerelease
                  ? "You're running the newest beta version."
                  : `No beta version is newer than ${latest?.label ?? "the latest release"}.`
                : latest?.relation === "older"
                  ? `You're running a beta version that's newer than the latest release, ${latest.label}.`
                  : "You're running the latest published release."}
            </span>
            {update.channel === "release" && latest?.relation === "older" && (
              <UpdateActions update={update} version={latest} />
            )}
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
