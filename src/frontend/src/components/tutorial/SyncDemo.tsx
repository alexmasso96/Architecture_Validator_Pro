import { TutorialShell, type TutorialStep } from "./TutorialShell";

// Interactive walkthrough of shared projects — a project on a NAS, kept in step
// with each user's local copy by the Sync Worker.

type Highlight = "stage" | "status" | "sharing" | "tray" | "nas" | "worker" | "problem";

const STEPS: TutorialStep<Highlight>[] = [
  {
    title: "What sharing does",
    highlight: "stage",
    body: (
      <>
        Keep a project on a network share, such as a NAS. You work from{" "}
        <strong>your own disk</strong>, so the app stays fast, and the{" "}
        <strong>Sync Worker</strong> keeps your copy and the shared copy in step.
        Only the changed parts travel.
      </>
    ),
  },
  {
    title: "1 · Share a project",
    highlight: "sharing",
    body: (
      <>
        Click <strong>Not shared</strong> in the Workspace status bar and choose{" "}
        <strong>Save to a shared folder…</strong>. A progress card shows each step,
        and you stay in the project throughout.
      </>
    ),
  },
  {
    title: "2 · The Sync Worker runs by itself",
    highlight: "tray",
    body: (
      <>
        The Sync Worker is a small applet with a tray icon, and it doesn't need the
        app. Turn on <strong>Start at login</strong> in its menu. The icon turns while
        data moves, and changes colour only when a project needs you.
      </>
    ),
  },
  {
    title: "3 · Edit and publish",
    highlight: "nas",
    body: (
      <>
        Opening for editing locks the shared project under your name and brings your
        copy up to date. Others can still view it. <strong>Save</strong> publishes
        your changes, so teammates never receive half-finished work.
      </>
    ),
  },
  {
    title: "4 · Changes arrive by themselves",
    highlight: "status",
    body: (
      <>
        Changes from others arrive within about 30 seconds, whether or not the app is
        open. The status bar shows <strong>Syncing changes from</strong> and the
        user's name, then <strong>Synced</strong> and the time.
      </>
    ),
  },
  {
    title: "5 · See what changed",
    highlight: "worker",
    body: (
      <>
        Click <strong>Open Sync Worker…</strong> in the tray menu to see your shared
        projects and each one's changes: who, where and when. It works with the app
        closed, and keeps the ASPICE change history clean.
      </>
    ),
  },
  {
    title: "6 · When something goes wrong",
    highlight: "problem",
    body: (
      <>
        If the NAS can't be reached, you keep working and your edits are published
        when it's back. A lock left by a crashed PC is free after 10 minutes of
        silence. If someone took over while you were offline, your work is saved as a{" "}
        <strong>conflict copy</strong> next to the project. Nothing is merged
        automatically.
      </>
    ),
  },
  {
    title: "You're ready 🎉",
    highlight: null,
    body: (
      <>
        <strong>Share once, then open, edit and save as usual</strong>. The Sync
        Worker does the rest in the background. Replay any time from{" "}
        <strong>Preferences → Tutorials</strong>.
      </>
    ),
  },
];

const CHANGES = [
  ["09:12", "maria", "Release R2.0", "Code Map"],
  ["09:10", "maria", "Model Powertrain", "Ports"],
  ["08:41", "jonas", "Project", "Test Runs"],
];

export function SyncDemo({ onClose }: { onClose: () => void }) {
  return (
    <TutorialShell title="Shared projects — interactive walkthrough" steps={STEPS} onClose={onClose}>
      {({ step, hl }) => {
        const shared = step >= 1;
        const editing = step === 3;
        const receiving = step === 4;
        const offline = step === 6;
        const status = !shared
          ? "⇅ Not shared"
          : offline
            ? "⚠ Offline, last synced 09:12"
            : receiving
              ? "⇅ Syncing changes from maria"
              : "⇅ Synced 09:12";

        return (
          <div className="demo-td-full demo-sy">
            <div className="demo-sy-top">
              <div className="demo-sy-box">
                <div className="demo-panel-head">Your PC</div>
                <div className="demo-field">Your own copy</div>
                <code className="demo-mono">C:\Users\you\…\Project.arch</code>
                <div className="demo-field">{editing ? "Editing: 3 unsaved changes" : "Fast, always available"}</div>
                <div className={"demo-sy-tray" + hl("tray")} data-demo="tray">
                  <div className="demo-field"><strong>Sync Worker</strong> tray menu</div>
                  <div className="demo-field">Open Sync Worker…</div>
                  <div className="demo-field">Sync now</div>
                  <div className="demo-field">
                    {step >= 2 ? "✓ " : ""}Start at login
                  </div>
                </div>
              </div>

              <div className="demo-sy-arrow">
                {!shared ? "" : offline ? "✕" : editing ? "Save ▸" : receiving ? "◂ changes" : "⇄"}
              </div>

              <div className={"demo-sy-box" + hl("nas")} data-demo="nas">
                <div className="demo-panel-head">NAS</div>
                {shared ? (
                  <>
                    <div className="demo-field">Shared copy</div>
                    <code className="demo-mono">\\nas\team\Project.arch</code>
                    <div className="demo-field">
                      {editing ? "🔒 Locked by you" : "Teammates receive it from here"}
                    </div>
                  </>
                ) : (
                  <div className="demo-muted">Nothing shared yet.</div>
                )}
              </div>
            </div>

            <div className={"demo-sy-worker" + hl("worker") + hl("problem")} data-demo="worker">
              <div className="demo-panel-head">Sync Worker · Changes in Project</div>
              {offline ? (
                <div className="demo-muted">Offline. Your edits wait until the NAS is back.</div>
              ) : step >= 5 ? (
                CHANGES.map(([when, user, where, section]) => (
                  <div className="demo-sy-change" key={when}>
                    <span>{when}</span>
                    <span>{user}</span>
                    <span>{where}</span>
                    <span>{section}</span>
                  </div>
                ))
              ) : (
                <div className="demo-muted">Nothing has been published yet.</div>
              )}
            </div>

            <div className="demo-sy-bar">
              <span className={"demo-sy-status" + hl("status") + hl("sharing")} data-demo="status">
                {status}
              </span>
            </div>

            {step === 1 && (
              <div className="demo-modalcard">
                <div className="demo-modalcard-head">Sharing</div>
                <div className="demo-field">This project is only on this PC.</div>
                <div className="demo-field">Folder: <code>\\nas\team</code></div>
                <div className="demo-export-ok">✓ Saved to a shared folder</div>
              </div>
            )}
          </div>
        );
      }}
    </TutorialShell>
  );
}
