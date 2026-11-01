import { useState } from "react";
import { TestInjectionDemo } from "./TestInjectionDemo";
import { WorkspaceDemo } from "./tutorial/WorkspaceDemo";
import { CodeMapDemo } from "./tutorial/CodeMapDemo";
import { ChangeLogDemo } from "./tutorial/ChangeLogDemo";
import { TestDesignDemo } from "./tutorial/TestDesignDemo";
import { AIGenerationDemo } from "./tutorial/AIGenerationDemo";
import { AIChatDemo } from "./tutorial/AIChatDemo";
import { TestBuildsDemo } from "./tutorial/TestBuildsDemo";
import { SyncDemo } from "./tutorial/SyncDemo";

// "Tutorials" preferences section: a list of interactive guides. Each card
// launches a self-contained, click-through demo (no real data is touched).
interface Tutorial {
  key: string;
  title: string;
  blurb: string;
  icon: string;
  render: (onClose: () => void) => React.ReactNode;
}

const TUTORIALS: Tutorial[] = [
  {
    key: "workspace",
    title: "Workspace & architecture matrix",
    blurb:
      "Match model ports against real ELF symbols, re-match, review, and compare a release to a baseline — on a simulated matrix.",
    icon: "🧩",
    render: (onClose) => <WorkspaceDemo onClose={onClose} />,
  },
  {
    key: "code-map",
    title: "Code Map & call graph",
    blurb:
      "Explore the firmware as a call graph and read-only IDE: focus a function, walk its callers and callees, and inspect the source.",
    icon: "🗺",
    render: (onClose) => <CodeMapDemo onClose={onClose} />,
  },
  {
    key: "change-log",
    title: "Change Log & release diff",
    blurb:
      "Compare two releases side by side, browse changed files by add/modify/delete, and read an AI summary of each change.",
    icon: "📜",
    render: (onClose) => <ChangeLogDemo onClose={onClose} />,
  },
  {
    key: "test-design",
    title: "Test Design templates",
    blurb:
      "Write one markdown template with placeholders and expand it into a consistent test-case document for every validated port.",
    icon: "📐",
    render: (onClose) => <TestDesignDemo onClose={onClose} />,
  },
  {
    key: "ai-generation",
    title: "AI test generation",
    blurb:
      "Generate low-level tests from a high-level design, grounded in a code mind map so the AI writes against real functions.",
    icon: "✨",
    render: (onClose) => <AIGenerationDemo onClose={onClose} />,
  },
  {
    key: "ai-chat",
    title: "AI Chat assistant",
    blurb:
      "Ask questions about your architecture, code, and validation results — grounded in the project's code mind map.",
    icon: "💬",
    render: (onClose) => <AIChatDemo onClose={onClose} />,
  },
  {
    key: "test-injection",
    title: "Test Injection workflow",
    blurb:
      "Find a function, hook test code into it, add your own test files, and export build-ready output — step by step, on a simulated screen.",
    icon: "💉",
    render: (onClose) => <TestInjectionDemo onClose={onClose} />,
  },
  {
    key: "test-builds",
    title: "Test Builds",
    blurb:
      "Set up builds for a real ECU, run them overnight, and switch any of them into the folder your debugger points at.",
    icon: "🔨",
    render: (onClose) => <TestBuildsDemo onClose={onClose} />,
  },
  {
    key: "sync",
    title: "Shared projects & sync",
    blurb:
      "Share a project on a NAS and see how the Sync Worker keeps everyone's copy in step, even when something goes wrong.",
    icon: "🔄",
    render: (onClose) => <SyncDemo onClose={onClose} />,
  },
];

export function Tutorials() {
  const [active, setActive] = useState<Tutorial | null>(null);

  return (
    <div className="prefs-body">
      <div className="prefs-field">
        <div className="prefs-hint">
          Interactive walkthroughs of the app's features. Each one runs in a safe
          sandbox — nothing in your real projects is changed.
        </div>
      </div>

      <div className="tut-list">
        {TUTORIALS.map((t) => (
          <button key={t.key} className="tut-card" onClick={() => setActive(t)}>
            <span className="tut-card-icon">{t.icon}</span>
            <span className="tut-card-text">
              <span className="tut-card-title">{t.title}</span>
              <span className="tut-card-blurb">{t.blurb}</span>
            </span>
            <span className="tut-card-go">Start ▸</span>
          </button>
        ))}
      </div>

      {active && active.render(() => setActive(null))}
    </div>
  );
}
