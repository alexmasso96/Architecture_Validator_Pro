import { useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import {
  ACCENTS,
  applyAccent,
  applyMode,
  getAccent,
  getMode,
  isCustomAccent,
  type ThemeMode,
} from "../theme";
import {
  getFileExplorer,
  getIconLabels,
  setFileExplorer,
  setIconLabels,
  useSetting,
  type FileExplorerMode,
} from "../settings";
import { persistKey } from "../prefs";
import { APP_VERSION, compareUrl, type UpdateInfo } from "../update";
import { AISettings } from "./AISettings";
import { ColorPicker } from "./ColorPicker";
import { CustomExplorerArt, SystemExplorerArt } from "./Icons";
import { NetworkSettings } from "./NetworkSettings";
import { ImportProfilesSettings } from "./ImportProfilesSettings";
import { PathsSettings } from "./PathsSettings";
import { ReportSettings } from "./ReportSettings";
import { Tutorials } from "./Tutorials";

type Category =
  | "appearance"
  | "ai"
  | "network"
  | "paths"
  | "import"
  | "reports"
  | "tutorials"
  | "updates";
type PreviewVariant = "light" | "dark" | "system";

const CATEGORIES: { key: Category; label: string; icon: string }[] = [
  { key: "appearance", label: "Appearance", icon: "🎨" },
  { key: "ai", label: "AI Settings", icon: "✦" },
  { key: "network", label: "Network", icon: "🌐" },
  { key: "paths", label: "Paths", icon: "🗂" },
  { key: "import", label: "Import Profiles", icon: "⇪" },
  { key: "reports", label: "Reports", icon: "⎙" },
  { key: "tutorials", label: "Tutorials", icon: "🎓" },
  { key: "updates", label: "Updates", icon: "⤓" },
];

const MODES: { key: ThemeMode; label: string; variant: PreviewVariant }[] = [
  { key: "light", label: "Light", variant: "light" },
  { key: "dark", label: "Dark", variant: "dark" },
  { key: "auto", label: "System", variant: "system" },
];

// Mini app-window thumbnail used inside the theme preview cards.
function MiniWindow({ dark }: { dark: boolean }) {
  return (
    <div className={"mini " + (dark ? "mini-dark" : "mini-light")}>
      <div className="mini-bar">
        <i className="d1" />
        <i className="d2" />
        <i className="d3" />
      </div>
      <div className="mini-body">
        <span className="mini-line accent" />
        <span className="mini-line g" />
        <span className="mini-line r" />
        <span className="mini-line n" />
      </div>
    </div>
  );
}

function ThemePreview({ variant }: { variant: PreviewVariant }) {
  if (variant === "system") {
    return (
      <div className="mini-system">
        <MiniWindow dark={false} />
        <div className="mini-clip">
          <MiniWindow dark />
        </div>
      </div>
    );
  }
  return <MiniWindow dark={variant === "dark"} />;
}

// Split-column preferences dialog: categories on the left, settings on the right.
const SIZE_KEY = "arch.prefs.size";
const DEFAULT_SIZE = { w: 820, h: 560 };
const MIN_SIZE = { w: 600, h: 400 };
const VIEWPORT_MARGIN = 32;

function clampSize(w: number, h: number) {
  return {
    w: Math.max(MIN_SIZE.w, Math.min(w, window.innerWidth - VIEWPORT_MARGIN)),
    h: Math.max(MIN_SIZE.h, Math.min(h, window.innerHeight - VIEWPORT_MARGIN)),
  };
}

function storedSize() {
  try {
    const { w, h } = JSON.parse(localStorage.getItem(SIZE_KEY) ?? "null") ?? DEFAULT_SIZE;
    return clampSize(Number(w) || DEFAULT_SIZE.w, Number(h) || DEFAULT_SIZE.h);
  } catch {
    return clampSize(DEFAULT_SIZE.w, DEFAULT_SIZE.h);
  }
}

export function Preferences({
  onClose,
  onCloseProject,
  update,
  onRecheck,
  onShowChangelog,
}: {
  onClose: () => void;
  onCloseProject?: () => void;
  update: UpdateInfo;
  onRecheck: () => void;
  onShowChangelog: () => void;
}) {
  const [category, setCategory] = useState<Category>("appearance");
  const [mode, setMode] = useState<ThemeMode>(getMode());
  const [accent, setAccent] = useState<string>(getAccent());
  const [picker, setPicker] = useState<{ x: number; y: number } | null>(null);
  const [size, setSize] = useState(storedSize);

  // Drag the corner to resize. The window stays centered, so it grows on both
  // sides: twice the pointer's travel keeps the corner under the pointer.
  function startResize(e: ReactPointerEvent<HTMLDivElement>) {
    const start = { x: e.clientX, y: e.clientY, ...size };
    let latest = size;
    e.currentTarget.setPointerCapture(e.pointerId);
    const target = e.currentTarget;
    const move = (ev: PointerEvent) => {
      latest = clampSize(start.w + (ev.clientX - start.x) * 2,
                         start.h + (ev.clientY - start.y) * 2);
      setSize(latest);
    };
    const stop = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", stop);
      target.removeEventListener("pointercancel", stop);
      const value = JSON.stringify(latest);
      try {
        localStorage.setItem(SIZE_KEY, value);
      } catch {
        /* quota / storage disabled — the backend store still has it */
      }
      persistKey(SIZE_KEY, value);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", stop);
    target.addEventListener("pointercancel", stop);
  }
  const rainbowRef = useRef<HTMLButtonElement>(null);
  const iconLabels = useSetting(getIconLabels);
  const fileExplorer = useSetting(getFileExplorer);

  const EXPLORERS: { key: FileExplorerMode; label: string; art: () => ReactNode }[] = [
    { key: "custom", label: "Custom (in-app)", art: CustomExplorerArt },
    { key: "system", label: "System dialog", art: SystemExplorerArt },
  ];

  function pickMode(m: ThemeMode) {
    setMode(m);
    applyMode(m); // live preview + persist
  }
  function pickAccent(color: string) {
    setAccent(color);
    applyAccent(color); // overrides --accent / --v-accent on :root, live
  }

  const custom = isCustomAccent(accent);
  const selectedAccentName = custom
    ? "Distro Hop (Custom)"
    : ACCENTS.find((a) => a.color.toLowerCase() === accent.toLowerCase())?.label ?? "";

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div className="modal prefs" style={{ width: size.w, height: size.h }}
           onMouseDown={(e) => e.stopPropagation()}>
        <div className="prefs-sidebar">
          <div className="prefs-title">Preferences</div>
          {CATEGORIES.map((c) => (
            <button
              key={c.key}
              className={"prefs-cat" + (c.key === category ? " active" : "")}
              onClick={() => setCategory(c.key)}
            >
              <span className="prefs-cat-icon">{c.icon}</span>
              {c.label}
            </button>
          ))}
          {onCloseProject && (
            <>
              <div style={{ flex: 1 }} />
              <button className="prefs-close-proj-btn" onClick={onCloseProject}>
                Close Project
              </button>
            </>
          )}
        </div>

        <div className="prefs-content">
          <div className="prefs-head">
            <span>{CATEGORIES.find((c) => c.key === category)?.label}</span>
            <button className="prefs-close" onClick={onClose} title="Close">
              ✕
            </button>
          </div>

          {category === "appearance" && (
            <div className="prefs-body">
              <div className="prefs-field">
                <div className="prefs-label">Theme</div>
                <div className="theme-cards">
                  {MODES.map((m) => (
                    <button
                      key={m.key}
                      className={"theme-card" + (mode === m.key ? " active" : "")}
                      onClick={() => pickMode(m.key)}
                    >
                      <span className="theme-thumb">
                        <ThemePreview variant={m.variant} />
                      </span>
                      <span className="theme-radio-row">
                        <span className={"theme-radio" + (mode === m.key ? " on" : "")} />
                        {m.label}
                      </span>
                    </button>
                  ))}
                </div>
              </div>

              <div className="prefs-field">
                <div className="prefs-row">
                  <div className="prefs-label">Accent color</div>
                  <div className="accent-row">
                    {ACCENTS.map((a) => (
                      <button
                        key={a.key}
                        className={
                          "accent-pill" +
                          (!custom && accent.toLowerCase() === a.color.toLowerCase()
                            ? " selected"
                            : "")
                        }
                        style={{ background: a.color }}
                        aria-label={a.label}
                        onClick={() => pickAccent(a.color)}
                      />
                    ))}

                    {/* Distro Hop (Custom): multicolor pill → in-app color picker */}
                    <button
                      ref={rainbowRef}
                      className={"accent-pill rainbow" + (custom ? " selected" : "")}
                      aria-label="Distro Hop (Custom)"
                      onClick={() => {
                        const r = rainbowRef.current!.getBoundingClientRect();
                        setPicker({
                          x: Math.min(r.left, window.innerWidth - 232),
                          y: r.bottom + 8,
                        });
                      }}
                    />
                  </div>
                </div>
                <div className="accent-name">{selectedAccentName}</div>
              </div>

              <div className="prefs-field">
                <label className="prefs-check">
                  <input
                    type="checkbox"
                    checked={iconLabels}
                    onChange={(e) => setIconLabels(e.target.checked)}
                  />
                  <span>
                    <div className="prefs-label">Show toolbar labels</div>
                    <div className="prefs-hint">
                      Display text captions under toolbar icons, or show icons only.
                    </div>
                  </span>
                </label>
              </div>

              <div className="prefs-field">
                <div className="prefs-label">File browser</div>
                <div className="prefs-hint">
                  Use the in-app file browser or your operating system's native
                  dialog (desktop app only).
                </div>
                <div className="explorer-cards">
                  {EXPLORERS.map((ex) => (
                    <button
                      key={ex.key}
                      className={"explorer-card" + (fileExplorer === ex.key ? " active" : "")}
                      onClick={() => setFileExplorer(ex.key)}
                    >
                      <span className="explorer-art">{ex.art()}</span>
                      <span className="explorer-radio-row">
                        <span
                          className={"theme-radio" + (fileExplorer === ex.key ? " on" : "")}
                        />
                        {ex.label}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {category === "ai" && <AISettings />}

          {category === "network" && <NetworkSettings />}

          {category === "paths" && <PathsSettings />}

          {category === "import" && <ImportProfilesSettings />}

          {category === "reports" && <ReportSettings />}

          {category === "tutorials" && <Tutorials />}

          {category === "updates" && (
            <div className="prefs-body">
              <div className="prefs-field">
                <div className="prefs-label">Current version</div>
                <div className="update-version">v{APP_VERSION}</div>
              </div>

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
                      v{update.latestVersion} is the latest release.
                    </span>
                    <div className="update-actions">
                      <button className="save-btn" onClick={onShowChangelog}>
                        View changelog
                      </button>
                      <a
                        className="update-compare"
                        href={compareUrl(update.latestVersion!)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Compare on GitHub →
                      </a>
                    </div>
                  </div>
                )}
                {update.status === "latest" && (
                  <div className="update-row">
                    <span className="update-badge ok">✓ Up to date</span>
                    <span className="update-detail">
                      You're running the latest published release.
                    </span>
                  </div>
                )}
                {update.status === "error" && (
                  <div className="update-row">
                    <span className="update-badge warn">⚠ Check failed</span>
                    <span className="update-detail">
                      {update.errorMsg ?? "Could not reach GitHub."}
                    </span>
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
          )}
        </div>

        <div className="prefs-resize" title="Drag to resize" onPointerDown={startResize} />

        {/* Rendered inside .prefs so its clicks don't bubble to the overlay
            (whose onMouseDown would otherwise close the whole dialog). */}
        {picker && (
          <ColorPicker
            value={custom ? accent : "#888888"}
            x={picker.x}
            y={picker.y}
            onChange={pickAccent}
            onClose={() => setPicker(null)}
          />
        )}
      </div>
    </div>
  );
}
