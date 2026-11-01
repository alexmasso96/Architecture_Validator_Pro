import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { BuildSettings, ProjectMode, TerminalKind } from "../api/types";
import { BUILD_SETTINGS_EVENT } from "../settings";
import { FolderPicker } from "./FolderPicker";
import { TerminalIcon } from "./Icons";

// Visual terminal options for the build runner. Each carries a brand-ish tint so
// the picker reads at a glance (issue 8).
const TERMINALS: { key: TerminalKind; label: string; tint: string }[] = [
  { key: "cmd", label: "Command Prompt", tint: "#3b4252" },
  { key: "powershell", label: "PowerShell", tint: "#1f4f8b" },
  { key: "bash", label: "Bash", tint: "#43a047" },
  { key: "wsl", label: "WSL", tint: "#e95420" },
];

type BrowseTarget = "build_cwd" | "source_path";

// Preferences → Paths. Build-tooling paths are project-scoped (stored in the
// open project's meta via /injection/settings), so this panel only works with a
// project open — it shows a notice otherwise.
export function PathsSettings() {
  const [settings, setSettings] = useState<BuildSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [browse, setBrowse] = useState<BrowseTarget | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setSettings(await api.get<BuildSettings>("/injection/settings"));
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          setUnavailable("Open a project to configure build paths.");
        } else {
          setUnavailable((e as Error).message);
        }
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  async function patch(p: Partial<BuildSettings>) {
    setSettings((s) => (s ? { ...s, ...p } : s));
    try {
      await api.post("/injection/settings", p);
      window.dispatchEvent(new Event(BUILD_SETTINGS_EVENT));
      setNote("Saved");
      window.setTimeout(() => setNote(null), 1400);
    } catch (e) {
      setNote(
        e instanceof ApiError && e.status === 409
          ? "Open the project for editing to change paths."
          : `Save failed: ${(e as Error).message}`,
      );
    }
  }

  function onBrowsed(path: string, _mode: ProjectMode) {
    const target = browse;
    setBrowse(null);
    if (target) patch({ [target]: path } as Partial<BuildSettings>);
  }

  if (loading) {
    return (
      <div className="prefs-body">
        <div className="center-msg">
          <span className="spin" /> Loading…
        </div>
      </div>
    );
  }
  if (!settings) {
    return (
      <div className="prefs-body">
        <div className="prefs-placeholder">{unavailable}</div>
      </div>
    );
  }

  return (
    <div className="prefs-body">
      <PathField
        label="Source code (on disk)"
        hint="Starting source folder for new test builds. Test Injection shows the source imported into the release, not this folder."
        value={settings.source_path}
        placeholder="/path/to/src"
        onChange={(v) => patch({ source_path: v })}
        onBrowse={() => setBrowse("source_path")}
      />

      <PathField
        label="Build folder"
        hint="Your make script or the folder it's in. Builds run from that folder."
        value={settings.build_cwd}
        placeholder="/path/to/make.cmd"
        onChange={(v) => patch({ build_cwd: v })}
        onBrowse={() => setBrowse("build_cwd")}
      />

      <div className="prefs-field">
        <div className="prefs-label">Terminal</div>
        <div className="term-grid">
          {TERMINALS.map((t) => (
            <button
              key={t.key}
              className={"term-card" + (settings.terminal === t.key ? " active" : "")}
              onClick={() => patch({ terminal: t.key })}
            >
              <TerminalIcon tint={t.tint} />
              <span>{t.label}</span>
            </button>
          ))}
        </div>
        {settings.terminal === "wsl" && (
          <input
            className="prefs-input"
            placeholder="WSL distro (optional, e.g. Ubuntu)"
            value={settings.wsl_distro}
            onChange={(e) => setSettings({ ...settings, wsl_distro: e.target.value })}
            onBlur={(e) => patch({ wsl_distro: e.target.value })}
          />
        )}
      </div>

      <div className="prefs-field">
        <div className="prefs-label">Build command</div>
        <input
          className="prefs-input"
          placeholder="e.g. make 0"
          value={settings.build_command}
          onChange={(e) => setSettings({ ...settings, build_command: e.target.value })}
          onBlur={(e) => patch({ build_command: e.target.value })}
        />
        <div className="prefs-hint">
          What you'd type in that terminal. A script works too, such as ./build.sh.
        </div>
      </div>

      {note && <div className="ai-prov-toast">{note}</div>}

      {browse && (
        <FolderPicker
          mode="folder"
          title={browse === "source_path" ? "Choose source folder" : "Choose build folder"}
          onCancel={() => setBrowse(null)}
          onConfirm={onBrowsed}
        />
      )}
    </div>
  );
}

function PathField({
  label,
  hint,
  value,
  placeholder,
  onChange,
  onBrowse,
}: {
  label: string;
  hint: string;
  value: string;
  placeholder: string;
  onChange: (v: string) => void;
  onBrowse: () => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <div className="prefs-field">
      <div className="prefs-label">{label}</div>
      <div className="path-row">
        <input
          className="prefs-input"
          placeholder={placeholder}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => onChange(draft)}
        />
        <button className="path-browse" onClick={onBrowse}>
          Browse…
        </button>
      </div>
      <div className="prefs-hint">{hint}</div>
    </div>
  );
}
