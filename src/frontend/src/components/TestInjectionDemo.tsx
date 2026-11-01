import { TutorialShell, type TutorialStep } from "./tutorial/TutorialShell";

// Self-contained, click-through tutorial for the Test Injection workflow.
// Nothing here touches real data or the backend — it renders a simplified mock
// of the Test Injection screen and reveals the workflow one step at a time, so
// the user can learn it safely even with no project open. The mock's state is
// derived purely from the current step index, so Back/Next stay consistent.

type Highlight =
  | "stage"
  | "projects"
  | "source"
  | "modeswitch"
  | "snippet"
  | "testfiles"
  | "export";

const STEPS: TutorialStep<Highlight>[] = [
  {
    title: "What Test Injection does",
    highlight: "stage",
    body: (
      <>
        It lets you add extra C code — instrumentation, stubs, a test harness — to
        your firmware <strong>without ever editing the real source files</strong>.
        Your code is saved as <em>hooks</em> in the project and applied only to
        generated copies. This walkthrough shows the full flow end to end.
      </>
    ),
  },
  {
    title: "1 · Import the source",
    highlight: "source",
    body: (
      <>
        Use <strong>Import Source</strong> in the Release Manager. This view reads
        that stored copy of the release's code, not a folder on disk, so import again
        when the code changes.
      </>
    ),
  },
  {
    title: "2 · Create a test project",
    highlight: "projects",
    body: (
      <>
        Click <strong>＋</strong> next to <strong>Test Projects</strong>. A test
        project holds one set of hooks and test files, stored in your{" "}
        <code>.arch</code>. You can keep several (e.g. a coverage build vs. a unit
        harness), and use one on any release.
      </>
    ),
  },
  {
    title: "3 · Find the code",
    highlight: "source",
    body: (
      <>
        Folders start closed. Open them, or search by <strong>function or file
        name</strong>. Picking a function — here <code>Adc_ReadChannel</code> — opens
        its file at that line, in <strong>Inject</strong> mode, a preview that shows
        your hooks in place.
      </>
    ),
  },
  {
    title: "4 · Switch to Edit and add a hook",
    highlight: "modeswitch",
    body: (
      <>
        Switch to <strong>Edit</strong>, click the line where the code should go, and
        click <strong>＋ Hook at cursor</strong>. The file stays read-only. A hook
        remembers the code around it, so it finds its spot again if the source shifts.
      </>
    ),
  },
  {
    title: "5 · Write the snippet",
    highlight: "snippet",
    body: (
      <>
        Write your code in the panel on the right and click{" "}
        <strong>Save snippet</strong>. It appears in the preview (highlighted green),
        but the real <code>adc.c</code> on disk is untouched. <strong>Exact</strong> on
        a hook means the app is sure of its place; check any marked{" "}
        <strong>fuzzy</strong>.
      </>
    ),
  },
  {
    title: "6 · Add your own test files (optional)",
    highlight: "testfiles",
    body: (
      <>
        For mocks or a harness, click <strong>⤓</strong> under{" "}
        <strong>Test Files</strong>, choose a folder, and say where the files go in
        the source tree. They're copies, kept in the test project. Your build still
        has to compile them, through your makefile or an <code>#include</code> in a
        hook.
      </>
    ),
  },
  {
    title: "7 · Build and export",
    highlight: "export",
    body: (
      <>
        Set your make script and <code>make 0</code> in{" "}
        <strong>Preferences → Paths</strong>. <strong>Build</strong> runs that command
        as is, and <strong>Terminal</strong> opens one in that folder. To use your
        hooks, click <strong>Export</strong> to write the changed files to a folder, or
        let <strong>Test Builds</strong> build them for you.
      </>
    ),
  },
  {
    title: "You're ready 🎉",
    highlight: null,
    body: (
      <>
        That's the whole loop: <strong>import source → find the code → hook it →
        export or build</strong>. Because hooks live in the project (not the files),
        you can change or remove them any time. Replay this from{" "}
        <strong>Preferences → Tutorials</strong>.
      </>
    ),
  },
];

const SRC_LINES = [
  '#include "adc.h"',
  "",
  "uint16_t Adc_ReadChannel(uint8_t ch) {",
  "    Adc_StartConversion(ch);",
  "    while (!Adc_Ready()) { }",
];
const SRC_TAIL = ["    return Adc_GetResult();", "}"];

export function TestInjectionDemo({ onClose }: { onClose: () => void }) {
  const snippet = 'test_log("ADC read: %d", Adc_GetResult());';

  return (
    <TutorialShell
      title="Test Injection — interactive walkthrough"
      steps={STEPS}
      onClose={onClose}
    >
      {({ step, isRecap, hl }) => {
        // Cumulative state derived from the step index.
        const sourceImported = step >= 1;
        const projectCreated = step >= 2;
        const fileOpen = step >= 3;
        const hookAdded = step >= 4;
        const snippetWritten = step >= 5;
        const helpersImported = step >= 6;
        const showExport = step >= 7;

        return (
          <>
            <div className="demo-sidebar">
              {/* Test Projects */}
              <div className={"demo-pane" + hl("projects")} data-demo="projects">
                <div className="demo-sect-head">
                  <span>Test Projects</span>
                  <span className="demo-iconbtn pulse-if-hl">＋</span>
                </div>
                <div className="demo-pane-body">
                  {projectCreated ? (
                    <div className="demo-proj sel">
                      <span>Demo Test Project</span>
                      <span className="demo-proj-meta">
                        {helpersImported
                          ? "1f · 1h"
                          : "0f · " + (hookAdded ? "1h" : "0h")}
                      </span>
                    </div>
                  ) : (
                    <div className="demo-muted">No test projects yet.</div>
                  )}
                </div>
              </div>

              {/* Production Source */}
              <div className={"demo-pane" + hl("source")} data-demo="source">
                <div className="demo-sect-head">
                  <span>Production Source</span>
                </div>
                <div className="demo-pane-body">
                  {!sourceImported && <div className="demo-muted">No source files.</div>}
                  {sourceImported && (
                    <div className="demo-search demo-ti-search">
                      {step === 3 ? "Adc_Read" : "Search functions and files"}
                    </div>
                  )}
                  {sourceImported && step === 3 && (
                    <div className="demo-file sel">
                      <span className="demo-file-ico src" />
                      Adc_ReadChannel
                    </div>
                  )}
                  {sourceImported &&
                    step !== 3 &&
                    ["drivers", "app"].map((f) => (
                      <div key={f} className="demo-file">
                        ▸ {f}
                      </div>
                    ))}
                </div>
              </div>

              {/* Test Files */}
              <div className={"demo-pane" + hl("testfiles")} data-demo="testfiles">
                <div className="demo-sect-head">
                  <span>Test Files</span>
                  <span className="demo-iconbtn pulse-if-hl">⤓</span>
                </div>
                <div className="demo-pane-body">
                  {helpersImported ? (
                    <div className="demo-file">
                      <span className="demo-file-ico test" />
                      tests/test_helpers.c
                    </div>
                  ) : (
                    <div className="demo-muted">No helper files imported.</div>
                  )}
                </div>
              </div>
            </div>

            {/* Center: tabs + editor */}
            <div className="demo-center">
              <div className="demo-tabbar">
                {fileOpen && (
                  <div className="demo-tab active">
                    <span className="demo-tab-dot src" />
                    adc.c
                  </div>
                )}
                <div className="demo-spacer" />
                {fileOpen && (
                  <div
                    className={"demo-modeswitch" + hl("modeswitch")}
                    data-demo="modeswitch"
                  >
                    <span className={hookAdded ? "" : "active"}>Inject</span>
                    <span className={hookAdded ? "active" : ""}>Edit</span>
                    {hookAdded && (
                      <span className="demo-hookbtn">＋ Hook at cursor</span>
                    )}
                  </div>
                )}
              </div>

              <div className="demo-editorrow">
                <div className="demo-editor">
                  {!fileOpen ? (
                    <div className="demo-editor-empty">
                      Select a source or test file to begin.
                    </div>
                  ) : (
                    <div className="demo-code">
                      {SRC_LINES.map((l, i) => (
                        <div className="demo-codeline" key={i}>
                          <span className="demo-gutter">{i + 1}</span>
                          <span className="demo-codetext">{l}</span>
                        </div>
                      ))}
                      {hookAdded && (
                        <div className="demo-codeline injected">
                          <span className="demo-gutter">+</span>
                          <span className="demo-codetext">
                            {"    "}
                            {snippetWritten ? snippet : "/* test code */"}
                          </span>
                        </div>
                      )}
                      {SRC_TAIL.map((l, i) => (
                        <div className="demo-codeline" key={"t" + i}>
                          <span className="demo-gutter">
                            {SRC_LINES.length + i + 1}
                          </span>
                          <span className="demo-codetext">{l}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Right: snippet editor for the selected hook */}
                {hookAdded && (
                  <div className={"demo-snippet" + hl("snippet")} data-demo="snippet">
                    <div className="demo-snippet-head">Hook snippet</div>
                    <div className="demo-snippet-box">
                      {snippetWritten ? snippet : "/* test code */"}
                      {!snippetWritten && <span className="demo-caret" />}
                    </div>
                    <button className="demo-save">Save</button>
                  </div>
                )}
              </div>

              <div className={"demo-buildbar" + hl("export")} data-demo="export">
                <span className="demo-build-label">Build Console</span>
                <div className="demo-spacer" />
                <code className="demo-mono">make 0</code>
                <span className="demo-btn">Build</span>
                <span className="demo-btn">Terminal</span>
                <span className="demo-btn primary">Export</span>
              </div>
            </div>

            {step === 6 && (
              <div className="demo-modalcard">
                <div className="demo-modalcard-head">Where should the test files go?</div>
                <div className="demo-field">
                  Folder in the source tree: <code>tests</code>
                </div>
                <div className="demo-field">
                  <code>tests/test_helpers.c</code>
                </div>
                <div className="demo-export-ok">✓ Import 1 file(s)</div>
              </div>
            )}

            {showExport && !isRecap && (
              <div className="demo-modalcard">
                <div className="demo-modalcard-head">Export test code</div>
                <div className="demo-field">
                  Output folder: <code>~/build/instrumented</code>
                </div>
                <div className="demo-export-ok">
                  ✓ Exported 2 files (adc.c, tests/test_helpers.c) — originals untouched
                </div>
              </div>
            )}
          </>
        );
      }}
    </TutorialShell>
  );
}
