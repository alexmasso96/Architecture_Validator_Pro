import { TutorialShell, type TutorialStep } from "./TutorialShell";

// Interactive walkthrough of Test Builds — named builds for a real ECU, built
// overnight and switched into the folder the debugger points at.

type Highlight = "stage" | "builds" | "settings" | "tracked" | "check" | "runs" | "mapped";

const STEPS: TutorialStep<Highlight>[] = [
  {
    title: "What Test Builds do",
    highlight: "stage",
    body: (
      <>
        Prepare named builds for testing on a real ECU, build them{" "}
        <strong>one after another overnight</strong>, and swap them into the one
        folder your debugger points at. They can hold code a normal pipeline would
        fail, such as a <code>while(1)</code> for a watchdog test, so a build is
        judged only on whether its build command succeeded.
      </>
    ),
  },
  {
    title: "1 · Add a build",
    highlight: "builds",
    body: (
      <>
        Under <strong>Builds for the selected release</strong>, enter a name, choose
        a test project, and click <strong>Add build</strong>. Builds belong to one
        release. <strong>Main</strong> is the release's clean build, which you
        switch back to.
      </>
    ),
  },
  {
    title: "2 · Check its settings",
    highlight: "settings",
    body: (
      <>
        A new build starts with your <strong>Preferences → Paths</strong> settings.
        Add a <strong>clean command</strong>, the <strong>source folder</strong> its
        hooks go into, and the <strong>build output folder</strong>. Without a clean
        command, objects from the previous build can end up in this one.
      </>
    ),
  },
  {
    title: "3 · Choose the files to keep",
    highlight: "tracked",
    body: (
      <>
        Under <strong>Tracked files</strong>, list what each build should save:{" "}
        <strong>Flash</strong>, <strong>Symbols</strong>, <strong>Source</strong> or{" "}
        <strong>Objects</strong>. Folders accept patterns such as{" "}
        <code>**/*.hex</code>.
      </>
    ),
  },
  {
    title: "4 · Check and run",
    highlight: "check",
    body: (
      <>
        Tick the builds and click <strong>Check and run…</strong>. The app lists
        anything that would fail, such as a hook that doesn't fit this release. Drop
        those builds and start the rest.
      </>
    ),
  },
  {
    title: "5 · Let it run overnight",
    highlight: "runs",
    body: (
      <>
        <strong>Main</strong> builds first if it has no capture yet. Each build then
        gets its hooks, is cleaned and built, and has its files saved. Your source
        folder is put back after every build. A failed build doesn't stop the rest,
        and if the PC stops, <strong>Resume</strong> continues where it ended.
      </>
    ),
  },
  {
    title: "6 · Switch the mapped release",
    highlight: "mapped",
    body: (
      <>
        Under <strong>Mapped release</strong>, choose the folder your debugger points
        at, pick a build, and click <strong>Switch</strong>. Only files that differ
        are transferred. Then flash from your debugger as usual. Going back to{" "}
        <strong>Main</strong> is the same action.
      </>
    ),
  },
  {
    title: "You're ready 🎉",
    highlight: null,
    body: (
      <>
        <strong>Add builds → track files → check and run → switch</strong>. One
        folder for your debugger, with any build a click away. Replay any time from{" "}
        <strong>Preferences → Tutorials</strong>.
      </>
    ),
  },
];

const RESULTS = [
  ["Main", "Passed", "pass"],
  ["Watchdog build", "Passed", "pass"],
  ["MPU violation", "Failed", "block"],
  ["Stack check", "Passed", "pass"],
];

export function TestBuildsDemo({ onClose }: { onClose: () => void }) {
  return (
    <TutorialShell title="Test Builds — interactive walkthrough" steps={STEPS} onClose={onClose}>
      {({ step, isRecap, hl }) => {
        const added = step >= 1;
        const configured = step >= 2;
        const tracked = step >= 3;
        const ran = step >= 5;
        const switched = step >= 6;
        const main = ran ? "Complete" : "None";

        return (
          <div className="demo-td-full demo-tb">
            <div className={"demo-tb-card" + hl("mapped")} data-demo="mapped">
              <div className="demo-panel-head">Mapped release</div>
              <div className="demo-tb-row">
                <span>
                  {switched ? "Holds R2.0 Watchdog build" : "Holds nothing from the app yet"}
                </span>
                <code className="demo-mono">{switched ? "D:\\debug\\ecu" : "No folder chosen"}</code>
                <div className="demo-spacer" />
                <span className="demo-btn sm">Choose folder…</span>
                <span className={"demo-btn sm" + (switched ? " primary" : "")}>Switch</span>
              </div>
            </div>

            <div className={"demo-tb-card" + hl("builds")} data-demo="builds">
              <div className="demo-panel-head">
                Builds for the selected release
                <div className="demo-spacer" />
                <span className={"demo-btn sm" + hl("check")} data-demo="check">
                  Check and run…
                </span>
              </div>
              <div className="demo-tb-row">
                <span className="demo-check" />
                <span className="demo-tb-name">Main</span>
                <span className="demo-muted">Capture: {main}</span>
              </div>
              {added ? (
                RESULTS.slice(1).map(([name, text]) => (
                  <div className="demo-tb-row" key={name}>
                    <span className={"demo-check" + (step >= 4 ? " on" : "")} />
                    <span className="demo-tb-name">{name}</span>
                    <span className="demo-muted">
                      Capture: {ran && text === "Passed" ? "Complete" : "None"}
                    </span>
                  </div>
                ))
              ) : (
                <div className="demo-tb-row">
                  <span className="demo-muted">New build name, such as Watchdog build</span>
                  <div className="demo-spacer" />
                  <span className="demo-btn sm primary">Add build</span>
                </div>
              )}
            </div>

            <div className="demo-tb-split">
              <div className={"demo-tb-card" + hl("settings")} data-demo="settings">
                <div className="demo-panel-head">Watchdog build</div>
                {added && (
                  <>
                    <div className="demo-field">Build: <code>make 0</code></div>
                    <div className="demo-field">Clean: <code>{configured ? "make clean" : "—"}</code></div>
                    <div className="demo-field">Source: <code>{configured ? "X:\\src" : "—"}</code></div>
                    <div className="demo-field">Output: <code>{configured ? "X:\\out" : "—"}</code></div>
                  </>
                )}
              </div>
              <div className={"demo-tb-card" + hl("tracked")} data-demo="tracked">
                <div className="demo-panel-head">Tracked files</div>
                {tracked ? (
                  <>
                    <div className="demo-field"><code>**/*.hex</code> Flash</div>
                    <div className="demo-field"><code>app.elf</code> Symbols</div>
                    <div className="demo-field"><code>**/*.o</code> Objects</div>
                  </>
                ) : (
                  <div className="demo-muted">Nothing tracked yet.</div>
                )}
              </div>
              <div className={"demo-tb-card" + hl("runs")} data-demo="runs">
                <div className="demo-panel-head">Runs</div>
                {ran ? (
                  RESULTS.map(([name, text, kind]) => (
                    <div className="demo-tb-result" key={name}>
                      <span>{name}</span>
                      <span className={"demo-result " + kind}>{text}</span>
                    </div>
                  ))
                ) : (
                  <div className="demo-muted">No runs yet.</div>
                )}
              </div>
            </div>

            {step === 4 && !isRecap && (
              <div className="demo-modalcard">
                <div className="demo-modalcard-head">Before the run</div>
                <div className="demo-field">Main isn't captured yet, so it builds first.</div>
                <div className="demo-field">No problems found.</div>
                <div className="demo-export-ok">✓ 3 builds are ready to start</div>
              </div>
            )}
          </div>
        );
      }}
    </TutorialShell>
  );
}
