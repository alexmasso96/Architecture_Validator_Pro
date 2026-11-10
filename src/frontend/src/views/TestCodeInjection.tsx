import Editor, { DiffEditor, type OnMount } from "@monaco-editor/react";
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api, ApiError } from "../api/client";
import { useSSE } from "../api/useSSE";
import type {
  BuildSettings,
  ChangedFile,
  HelperImportPreview,
  InjectionHook,
  ProjectTree as ProjectTreeData,
  ProjectTreeEntry,
  SourceFileInfo,
  SourceFunction,
  TerminalKind,
  TestProject,
  TestProjectFile,
} from "../api/types";
import { BUILD_SETTINGS_EVENT } from "../settings";
import { useConfirm } from "../components/ConfirmDialog";
import { FolderPicker } from "../components/FolderPicker";
import { FileIcon, FolderIcon } from "../components/Icons";
import { buildFileTree, type TreeNode } from "../fileTree";

// ---------------------------------------------------------------------------
// File tree (issue 4) — a collapsible VS Code-style tree of rel-paths.
// ---------------------------------------------------------------------------
function FileTree({
  files,
  kind,
  activeTab,
  onOpen,
  onDelete,
}: {
  files: string[];
  kind: TabKind;
  activeTab: string | null;
  onOpen: (kind: TabKind, relPath: string) => void;
  onDelete?: (relPath: string) => void;
}) {
  const tree = useMemo(() => buildFileTree(files), [files]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggle = (path: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const renderNodes = (nodes: TreeNode[], depth: number): React.ReactNode =>
    nodes.map((node) => {
      const indent = 8 + depth * 12;
      if (node.isDir) {
        const isCollapsed = !expanded.has(node.path);
        return (
          <Fragment key={`d:${node.path}`}>
            <button
              className="tci-tree-row dir"
              style={{ paddingLeft: indent }}
              onClick={() => toggle(node.path)}
            >
              <span className="tci-tree-chev">{isCollapsed ? "▸" : "▾"}</span>
              <FolderIcon size={13} />
              <span className="tci-tree-name">{node.name}</span>
            </button>
            {!isCollapsed && renderNodes(node.children, depth + 1)}
          </Fragment>
        );
      }
      const key = `${kind}:${node.path}`;
      return (
        <div key={`f:${node.path}`} className="tci-tree-filerow">
          <button
            className={"tci-tree-row file" + (activeTab === key ? " sel" : "")}
            style={{ paddingLeft: indent + 14 }}
            title={node.path}
            onClick={() => onOpen(kind, node.path)}
          >
            <FileIcon size={13} />
            <span className="tci-tree-name">{node.name}</span>
          </button>
          {onDelete && (
            <button
              className="tci-icon-btn danger tci-tree-del"
              title="Remove from this test project"
              onClick={() => onDelete(node.path)}
            >
              ✕
            </button>
          )}
        </div>
      );
    });

  return <>{renderNodes(tree, 0)}</>;
}

// ---------------------------------------------------------------------------
// Project tree — the whole project folder on disk, one folder at a time. A
// folder's entries are read when it is unfolded, so a big project costs nothing
// until then. Files the test project changes are marked.
// ---------------------------------------------------------------------------
function ProjectTree({
  projectId,
  root,
  changed,
  hooked,
  activeTab,
  refreshKey,
  onOpen,
  onError,
}: {
  projectId: number;
  root: ProjectTreeEntry[];
  changed: Set<string>;
  hooked: Map<string, number>;
  activeTab: string | null;
  refreshKey: number;
  onOpen: (relPath: string) => void;
  onError: (message: string) => void;
}) {
  const [children, setChildren] = useState<Record<string, ProjectTreeEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const load = useCallback(
    async (dir: string) => {
      try {
        const r = await api.get<ProjectTreeData>(
          `/injection/projects/${projectId}/tree?dir=${encodeURIComponent(dir)}`,
        );
        setChildren((prev) => ({ ...prev, [dir]: r.entries }));
      } catch (e) {
        onError(e instanceof ApiError ? e.detail : (e as Error).message);
      }
    },
    [projectId, onError],
  );

  // A refresh reads the unfolded folders again; another project starts folded.
  useEffect(() => {
    setChildren({});
    setExpanded(new Set());
  }, [projectId]);
  useEffect(() => {
    expanded.forEach((dir) => load(dir));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  const toggle = (dir: string) => {
    const next = new Set(expanded);
    if (next.has(dir)) next.delete(dir);
    else {
      next.add(dir);
      if (!children[dir]) load(dir);
    }
    setExpanded(next);
  };

  // A folder is marked when something under it is changed or hooked.
  const marked = useMemo(() => {
    const dirs = new Set<string>();
    for (const path of [...changed, ...hooked.keys()]) {
      const parts = path.split("/").slice(0, -1);
      parts.forEach((_, i) => dirs.add(parts.slice(0, i + 1).join("/")));
    }
    return dirs;
  }, [changed, hooked]);

  const renderEntries = (entries: ProjectTreeEntry[], depth: number): React.ReactNode =>
    entries.map((e) => {
      const indent = 8 + depth * 12;
      if (e.is_dir) {
        const open = expanded.has(e.rel_path);
        return (
          <Fragment key={`d:${e.rel_path}`}>
            <button
              className="tci-tree-row dir"
              style={{ paddingLeft: indent }}
              onClick={() => toggle(e.rel_path)}
            >
              <span className="tci-tree-chev">{open ? "▾" : "▸"}</span>
              <FolderIcon size={13} />
              <span className="tci-tree-name">{e.name}</span>
              {marked.has(e.rel_path) && <span className="tci-tree-mark dot" title="Has changes" />}
            </button>
            {open &&
              (children[e.rel_path] ? (
                renderEntries(children[e.rel_path], depth + 1)
              ) : (
                <div className="tci-empty" style={{ paddingLeft: indent + 26 }}>
                  Reading…
                </div>
              ))}
          </Fragment>
        );
      }
      const isChanged = changed.has(e.rel_path);
      const hooks = hooked.get(e.rel_path) ?? 0;
      const sel =
        activeTab === `test:${e.rel_path}` ||
        activeTab === `src:${e.rel_path}` ||
        activeTab === `disk:${e.rel_path}`;
      return (
        <button
          key={`f:${e.rel_path}`}
          className={"tci-tree-row file" + (sel ? " sel" : "") + (isChanged ? " changed" : "")}
          style={{ paddingLeft: indent + 14 }}
          title={e.rel_path}
          onClick={() => onOpen(e.rel_path)}
        >
          <FileIcon size={13} />
          <span className="tci-tree-name">{e.name}</span>
          {isChanged && (
            <span className="tci-tree-mark" title="Changed for this test project">
              M
            </span>
          )}
          {!isChanged && hooks > 0 && (
            <span className="tci-tree-mark" title={`${hooks} hook${hooks === 1 ? "" : "s"}`}>
              H
            </span>
          )}
        </button>
      );
    });

  return <>{renderEntries(root, 0)}</>;
}

// The editor's language for a file, by its name.
function languageOf(relPath: string): string {
  const name = relPath.split("/").pop()?.toLowerCase() ?? "";
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : "";
  if (["c", "h"].includes(ext)) return "c";
  if (["cpp", "hpp", "cc", "hh", "cxx"].includes(ext)) return "cpp";
  if (["arxml", "xml", "xdm", "xsd", "epc"].includes(ext)) return "xml";
  if (ext === "json") return "json";
  if (["yml", "yaml"].includes(ext)) return "yaml";
  if (ext === "py") return "python";
  if (["bat", "cmd"].includes(ext)) return "bat";
  if (["sh", "bash"].includes(ext)) return "shell";
  if (["ini", "cfg", "properties"].includes(ext)) return "ini";
  return "plaintext";
}

// Monaco's editor instance type, kept loose to avoid importing monaco's full
// type surface (it ships its own .d.ts but we only touch a few members).
type MonacoEditor = Parameters<OnMount>[0];
type MonacoNs = Parameters<OnMount>[1];

// Monaco theme follows the app's resolved light/dark (mirrors CodeMap.tsx).
function useMonacoTheme(): "vs" | "vs-dark" {
  const read = (): "vs" | "vs-dark" =>
    document.documentElement.getAttribute("data-theme") === "dark"
      ? "vs-dark"
      : "vs";
  const [theme, setTheme] = useState<"vs" | "vs-dark">(read);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(read()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => obs.disconnect();
  }, []);
  return theme;
}

const TERMINALS: { value: TerminalKind; label: string }[] = [
  { value: "cmd", label: "CMD" },
  { value: "powershell", label: "PowerShell" },
  { value: "bash", label: "Bash" },
  { value: "wsl", label: "WSL" },
];

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const SEARCH_LIMIT = 200;
// Bus reasons after which the Production Source list can be out of date.
const SOURCE_CHANGES = ["source-imported", "source-dropped", "release-activated"];

// "src": a C file imported into the release (hooks go here). "test": a file of
// the test project (a changed or added file; it can be edited). "disk": any
// other file of the project folder, read-only until it is changed.
type TabKind = "src" | "test" | "disk";
interface OpenTab {
  key: string;
  kind: TabKind;
  relPath: string;
}

// A contiguous block in the *preview* document, mapped back to its hook.
interface PreviewBlock {
  hookId: number;
  start: number; // 1-based first line in the preview
  end: number; // 1-based last line in the preview
  conflict: boolean;
}

interface Preview {
  value: string;
  blocks: PreviewBlock[];
}

// Splice every resolvable hook's snippet into the source for the read-only
// inject preview; flag conflicts as a single "+line" placeholder near the target
// function. Returns the preview text plus the per-hook line ranges for
// decoration / shift overlays.
function buildPreview(content: string, hooks: InjectionHook[]): Preview {
  const lines = content.split("\n");
  const resolved = hooks
    .filter((h) => h.resolved_index != null)
    .sort((a, b) => (a.resolved_index as number) - (b.resolved_index as number));

  const byIndex = new Map<number, InjectionHook[]>();
  for (const h of resolved) {
    const i = h.resolved_index as number;
    const arr = byIndex.get(i) ?? [];
    arr.push(h);
    byIndex.set(i, arr);
  }

  const out: string[] = [];
  const blocks: PreviewBlock[] = [];
  for (let i = 0; i <= lines.length; i++) {
    for (const h of byIndex.get(i) ?? []) {
      const snip = (h.injected_code || "").split("\n");
      const start = out.length + 1;
      for (const s of snip) out.push(s);
      blocks.push({ hookId: h.id, start, end: out.length, conflict: false });
    }
    if (i < lines.length) out.push(lines[i]);
  }

  // Conflicts: drop a placeholder line just inside the named function (or at the
  // file end if we can't locate it). Shift any block recorded after it down 1.
  for (const h of hooks.filter((x) => x.resolved_index == null)) {
    let at = out.length;
    if (h.function_name) {
      const re = new RegExp("\\b" + escapeRe(h.function_name) + "\\s*\\(");
      const fi = out.findIndex((l) => re.test(l));
      if (fi >= 0) at = fi + 1;
    }
    const label = `    /* +inject (conflict): ${h.function_name || "unanchored"} — reposition this block */`;
    out.splice(at, 0, label);
    for (const b of blocks) {
      if (b.start >= at + 1) {
        b.start += 1;
        b.end += 1;
      }
    }
    blocks.push({ hookId: h.id, start: at + 1, end: at + 1, conflict: true });
  }

  return { value: out.join("\n"), blocks };
}

// The preview holds snippets the source doesn't, so its line numbers run ahead.
function sourceLineToPreview(line: number, blocks: PreviewBlock[]): number {
  let at = line;
  for (const b of [...blocks].sort((x, y) => x.start - y.start)) {
    if (b.start <= at) at += b.end - b.start + 1;
  }
  return at;
}

// 0-based source index for a click on a preview line. A click inside a snippet
// means the place that snippet sits at.
function previewLineToSourceIndex(line: number, blocks: PreviewBlock[]): number {
  let above = 0;
  for (const b of blocks) {
    if (b.end < line) above += b.end - b.start + 1;
    else if (b.start <= line) above += line - b.start;
  }
  return line - 1 - above;
}

function confidenceBadge(c: number): { text: string; cls: string } {
  if (c >= 4) return { text: "exact", cls: "ok" };
  if (c >= 3) return { text: "fuzzy", cls: "warn" };
  return { text: "conflict", cls: "err" };
}

export function TestCodeInjection({
  toast,
  canEdit,
}: {
  toast: (m: string) => void;
  canEdit: boolean;
}) {
  const theme = useMonacoTheme();
  const { confirm, confirmNode } = useConfirm();

  // -- data ----------------------------------------------------------------
  const [projects, setProjects] = useState<TestProject[]>([]);
  const [activeProject, setActiveProject] = useState<number | null>(null);
  const [sourceFiles, setSourceFiles] = useState<SourceFileInfo[]>([]);
  const [sourceErr, setSourceErr] = useState<string | null>(null);
  const [functions, setFunctions] = useState<SourceFunction[]>([]);
  const [query, setQuery] = useState("");
  const [testFiles, setTestFiles] = useState<TestProjectFile[]>([]);
  const [changes, setChanges] = useState<ChangedFile[]>([]);
  const [tree, setTree] = useState<ProjectTreeData | null>(null);
  const [treeKey, setTreeKey] = useState(0);
  // Unsaved text of test-project files, by tab key.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [diskInfo, setDiskInfo] = useState<{ key: string; editable: boolean; reason: string } | null>(null);
  const [compare, setCompare] = useState<{ key: string; original: string | null } | null>(null);
  const [hooks, setHooks] = useState<InjectionHook[]>([]);

  // -- editor / tabs -------------------------------------------------------
  const [tabs, setTabs] = useState<OpenTab[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [docContent, setDocContent] = useState<string>("");
  const [docKey, setDocKey] = useState<string | null>(null); // tab docContent is from
  const [reveal, setReveal] = useState<{ key: string; line: number } | null>(null);
  const [mode, setMode] = useState<"inject" | "edit">("inject");
  const [selectedHook, setSelectedHook] = useState<number | null>(null);
  const [snippetDraft, setSnippetDraft] = useState<string>("");
  const [placing, setPlacing] = useState<{ hookId: number; line: number } | null>(
    null,
  );
  const [editorReady, setEditorReady] = useState(0); // bumps when Editor mounts

  // -- build console -------------------------------------------------------
  const [consoleOpen, setConsoleOpen] = useState(true);
  const [buildLogs, setBuildLogs] = useState<string[]>([]);
  const [building, setBuilding] = useState(false);
  const [settings, setSettings] = useState<BuildSettings>({
    terminal: "bash",
    wsl_distro: "",
    build_command: "",
    build_cwd: "",
    source_path: "",
  });
  const [importOpen, setImportOpen] = useState(false);
  const [importing, setImporting] = useState<{
    folder: string;
    dest: string;
    files: HelperImportPreview[];
    error: string | null;
  } | null>(null);

  // -- sidebar panes -------------------------------------------------------
  // The three sidebar sections (Test Projects / Production Source / Test Files)
  // are independently scrollable panes separated by draggable splitters. The
  // numbers are flex-grow weights; because each pane has flex-basis:0 they act
  // as a height ratio that always fills the sidebar. A drag re-seeds the weights
  // from the panes' live pixel heights so the maths stays linear.
  const [paneFlex, setPaneFlex] = useState<number[]>([1, 1, 1]);
  const paneRefs = useRef<(HTMLDivElement | null)[]>([]);

  function startPaneResize(e: React.MouseEvent, top: number) {
    e.preventDefault();
    const MIN = 56;
    const heights = paneRefs.current.map(
      (el) => el?.getBoundingClientRect().height ?? 0,
    );
    const startY = e.clientY;
    const a = heights[top];
    const b = heights[top + 1];
    const onMove = (ev: MouseEvent) => {
      let d = ev.clientY - startY;
      d = Math.max(-(a - MIN), Math.min(b - MIN, d));
      const next = [...heights];
      next[top] = a + d;
      next[top + 1] = b - d;
      setPaneFlex(next);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
  }

  const editorRef = useRef<MonacoEditor | null>(null);
  const monacoRef = useRef<MonacoNs | null>(null);
  const decoRef = useRef<ReturnType<MonacoEditor["createDecorationsCollection"]> | null>(
    null,
  );
  const symbolsRef = useRef<Set<string>>(new Set());
  const logRef = useRef<HTMLDivElement>(null);
  const completionRegistered = useRef(false);
  // The editor puts its cursor on line 1 whenever its text changes, so a position
  // only counts once the user has chosen it.
  const cursorChosen = useRef(false);
  // The editor's save shortcut is bound once; this always holds the current save.
  const saveRef = useRef<() => void>(() => {});

  const activeTabObj = tabs.find((t) => t.key === activeTab) ?? null;
  const isSrcTab = activeTabObj?.kind === "src";
  const isTestTab = activeTabObj?.kind === "test";
  const dirty = activeTab != null && drafts[activeTab] !== undefined;

  // -- loaders -------------------------------------------------------------
  const loadProjects = useCallback(async () => {
    try {
      const r = await api.get<{ projects: TestProject[] }>(
        "/injection/projects",
      );
      setProjects(r.projects);
      setActiveProject((cur) =>
        cur != null && r.projects.some((p) => p.id === cur)
          ? cur
          : (r.projects[0]?.id ?? null),
      );
    } catch (e) {
      toast(`Failed to load test projects: ${(e as Error).message}`);
    }
  }, [toast]);

  const loadSource = useCallback(async () => {
    try {
      const r = await api.get<{ files: SourceFileInfo[] }>(
        "/injection/source/files",
      );
      setSourceFiles(r.files);
      setSourceErr(null);
      // Reading every file can take a moment, so the tree doesn't wait for it.
      api
        .get<{ functions: SourceFunction[] }>("/injection/source/functions")
        .then((f) => setFunctions(f.functions))
        .catch(() => setFunctions([]));
    } catch (e) {
      setSourceFiles([]);
      setFunctions([]);
      setSourceErr(
        e instanceof ApiError ? e.detail : (e as Error).message,
      );
    }
  }, []);

  const loadProjectFiles = useCallback(async (pid: number) => {
    const r = await api.get<{ files: TestProjectFile[] }>(
      `/injection/projects/${pid}/files`,
    );
    setTestFiles(r.files);
  }, []);

  // The project folder's top level. Its folders are read as they're unfolded.
  const loadTree = useCallback(async (pid: number) => {
    try {
      setTree(await api.get<ProjectTreeData>(`/injection/projects/${pid}/tree`));
      setTreeKey((n) => n + 1);
    } catch {
      setTree(null);
    }
  }, []);

  const loadHooks = useCallback(async (pid: number) => {
    const r = await api.get<{ injections: InjectionHook[] }>(
      `/injection/projects/${pid}/injections`,
    );
    setHooks(r.injections);
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      setSettings(await api.get<BuildSettings>("/injection/settings"));
    } catch {
      /* keep defaults */
    }
  }, []);

  useEffect(() => {
    loadProjects();
    loadSource();
    loadSettings();
  }, [loadProjects, loadSource, loadSettings]);

  // Preferences → Paths edits these while this view stays mounted behind it.
  useEffect(() => {
    window.addEventListener(BUILD_SETTINGS_EVENT, loadSettings);
    return () => window.removeEventListener(BUILD_SETTINGS_EVENT, loadSettings);
  }, [loadSettings]);

  useEffect(() => {
    setDrafts({});
    setCompare(null);
    if (activeProject == null) {
      setTestFiles([]);
      setChanges([]);
      setHooks([]);
      setTree(null);
      return;
    }
    loadProjectFiles(activeProject);
    loadHooks(activeProject);
    loadTree(activeProject);
  }, [activeProject, loadProjectFiles, loadHooks, loadTree]);

  // What the test project changes in a build follows its files and its hooks.
  useEffect(() => {
    if (activeProject == null) return;
    api
      .get<{ files: ChangedFile[] }>(`/injection/projects/${activeProject}/changes`)
      .then((c) => setChanges(c.files))
      .catch(() => setChanges([]));
  }, [activeProject, testFiles, hooks]);

  const changedSet = useMemo(() => new Set(testFiles.map((f) => f.rel_path)), [testFiles]);
  const loadedSet = useMemo(() => new Set(sourceFiles.map((f) => f.rel_path)), [sourceFiles]);
  const hookedMap = useMemo(() => {
    const m = new Map<string, number>();
    for (const h of hooks) m.set(h.src_file_path, (m.get(h.src_file_path) ?? 0) + 1);
    return m;
  }, [hooks]);

  // Hooks for the file currently open in the editor.
  const sourceFolders = useMemo(() => {
    const folders = new Set<string>();
    for (const f of sourceFiles) {
      const parts = f.rel_path.split("/").slice(0, -1);
      parts.forEach((_, i) => folders.add(parts.slice(0, i + 1).join("/")));
    }
    return [...folders].sort();
  }, [sourceFiles]);

  // Search: functions first, because that's what a hook is placed in.
  const search = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return null;
    return {
      functions: functions
        .filter((f) => f.name.toLowerCase().includes(q))
        .slice(0, SEARCH_LIMIT),
      files: sourceFiles
        .filter((f) => f.rel_path.toLowerCase().includes(q))
        .slice(0, SEARCH_LIMIT),
    };
  }, [query, functions, sourceFiles]);

  const fileHooks = useMemo(
    () =>
      activeTabObj && isSrcTab
        ? hooks.filter((h) => h.src_file_path === activeTabObj.relPath)
        : [],
    [hooks, activeTabObj, isSrcTab],
  );

  // -- document loading ----------------------------------------------------
  const openFile = useCallback(
    async (kind: TabKind, relPath: string) => {
      const key = `${kind}:${relPath}`;
      setTabs((prev) =>
        prev.some((t) => t.key === key) ? prev : [...prev, { key, kind, relPath }],
      );
      setActiveTab(key);
    },
    [],
  );

  // A file of the project tree: the test project's copy if it has one, else
  // the imported source (where hooks go), else the file on disk.
  const openPath = useCallback(
    (relPath: string) =>
      openFile(changedSet.has(relPath) ? "test" : loadedSet.has(relPath) ? "src" : "disk", relPath),
    [openFile, changedSet, loadedSet],
  );

  useEffect(() => {
    if (!activeTabObj) {
      setDocContent("");
      setDocKey(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const path = encodeURIComponent(activeTabObj.relPath);
        const url =
          activeTabObj.kind === "src"
            ? `/injection/source/content?rel_path=${path}`
            : activeTabObj.kind === "disk"
              ? `/injection/projects/${activeProject}/disk/content?rel_path=${path}`
              : `/injection/projects/${activeProject}/files/content?rel_path=${path}`;
        const r = await api.get<{ content: string; editable?: boolean; reason?: string }>(url);
        if (!cancelled) {
          setDiskInfo(
            activeTabObj.kind === "disk"
              ? { key: activeTabObj.key, editable: r.editable !== false, reason: r.reason ?? "" }
              : null,
          );
          setDocContent(r.content);
          setDocKey(activeTabObj.key);
          // Feed the autocomplete provider with identifiers from this file.
          harvestSymbols(r.content);
        }
      } catch (e) {
        if (!cancelled) {
          setDocContent(`/* Failed to load: ${(e as Error).message} */`);
          setDocKey(activeTabObj.key);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, activeProject]);

  function harvestSymbols(text: string) {
    const set = symbolsRef.current;
    const re = /[A-Za-z_]\w{2,}/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) set.add(m[0]);
    for (const f of testFiles) set.add(f.rel_path.split("/").pop() ?? "");
  }

  // -- preview + decorations ----------------------------------------------
  const preview = useMemo<Preview>(() => {
    if (!isSrcTab) return { value: docContent, blocks: [] };
    return buildPreview(docContent, fileHooks);
  }, [docContent, fileHooks, isSrcTab]);

  const editorValue =
    mode === "inject" && isSrcTab
      ? preview.value
      : isTestTab && activeTab != null && drafts[activeTab] !== undefined
        ? drafts[activeTab]
        : docContent;

  // Apply inject-preview decorations directly in an effect. Parent effects run
  // after the child Editor has committed its controlled value, so the model line
  // count already matches `preview` here — no rAF/stale-closure dance needed.
  useEffect(() => {
    const ed = editorRef.current;
    const monaco = monacoRef.current;
    if (!ed || !monaco || !ed.getModel()) return;
    if (!decoRef.current) decoRef.current = ed.createDecorationsCollection();
    if (mode !== "inject" || !isSrcTab) {
      decoRef.current.set([]);
      return;
    }
    const decos = preview.blocks.map((b) => ({
      range: new monaco.Range(b.start, 1, b.end, 1),
      options: {
        isWholeLine: true,
        className: b.conflict ? "tci-line-conflict" : "tci-line-inject",
        linesDecorationsClassName: b.conflict
          ? "tci-glyph-conflict"
          : "tci-glyph-inject",
        hoverMessage: {
          value: b.conflict
            ? "Conflict — anchors no longer match. Reposition this block."
            : "Injected test code (preview).",
        },
      },
    }));
    if (placing) {
      decos.push({
        range: new monaco.Range(placing.line, 1, placing.line, 1),
        options: {
          isWholeLine: true,
          className: "tci-line-placing",
          linesDecorationsClassName: "tci-glyph-placing",
          hoverMessage: { value: "Click ↑/↓ then Place to anchor here." },
        },
      });
    }
    decoRef.current.set(decos);
  }, [preview, mode, isSrcTab, placing, editorValue, editorReady]);

  useEffect(() => {
    cursorChosen.current = false;
  }, [mode, activeTab]);

  // Jump to a searched function once its file is in the editor.
  useEffect(() => {
    const ed = editorRef.current;
    if (!reveal || reveal.key !== docKey || !ed?.getModel()) return;
    const line =
      mode === "inject" ? sourceLineToPreview(reveal.line, preview.blocks) : reveal.line;
    ed.setPosition({ lineNumber: line, column: 1 });
    ed.revealLineInCenter(line);
    ed.focus();
    setReveal(null);
  }, [reveal, docKey, mode, preview, editorValue, editorReady]);

  // -- SSE: build logs -----------------------------------------------------
  useSSE(
    useCallback((e) => {
      const d = e.data as Record<string, unknown>;
      if (e.event === "db-changed") {
        if (SOURCE_CHANGES.includes(d.reason as string)) loadSource();
        return;
      }
      if (e.event !== "build") return;
      const kind = d.event as string;
      if (kind === "start") {
        setBuilding(true);
        setConsoleOpen(true);
        setBuildLogs((p) => [...p, `$ ${(d.argv as string[])?.join(" ") ?? ""}`]);
      } else if (kind === "log" || kind === "error") {
        setBuildLogs((p) => [...p, String(d.line ?? "")]);
      } else if (kind === "done") {
        setBuilding(false);
        setBuildLogs((p) => [...p, `— build exited (code ${d.returncode}) —`]);
      }
    }, [loadSource]),
    true,
  );

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [buildLogs]);

  // -- editor mount --------------------------------------------------------
  const onEditorMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    // Bind a fresh decorations collection to THIS editor instance (a stale one
    // from a previous mount would silently apply to a disposed editor).
    decoRef.current = editor.createDecorationsCollection();
    requestAnimationFrame(() => editor.layout());
    setTimeout(() => editor.layout(), 100);

    editor.onDidChangeCursorPosition((e) => {
      if (e.source === "mouse" || e.source === "keyboard") cursorChosen.current = true;
    });

    // Repositioning a conflicted hook: clicking a line sets the candidate anchor.
    editor.onMouseDown((e) => {
      const pos = e.target.position;
      if (!pos) return;
      setPlacing((cur) => (cur ? { ...cur, line: pos.lineNumber } : cur));
    });

    if (!completionRegistered.current) {
      completionRegistered.current = true;
      const provider: import("monaco-editor").languages.CompletionItemProvider = {
        provideCompletionItems(model, position) {
          const word = model.getWordUntilPosition(position);
          const range = {
            startLineNumber: position.lineNumber,
            endLineNumber: position.lineNumber,
            startColumn: word.startColumn,
            endColumn: word.endColumn,
          };
          const suggestions = Array.from(symbolsRef.current)
            .filter(Boolean)
            .slice(0, 2000)
            .map((label) => ({
              label,
              kind: monaco.languages.CompletionItemKind.Function,
              insertText: label,
              range,
              detail: "CodeIndex symbol",
            }));
          return { suggestions };
        },
      };
      monaco.languages.registerCompletionItemProvider("c", provider);
    }
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current());
    // Decorations are applied by the effect below once preview/model are ready.
    setEditorReady((n) => n + 1);
  };

  // -- mutations -----------------------------------------------------------
  async function createProject() {
    const name = window.prompt("New test project name:")?.trim();
    if (!name) return;
    try {
      await api.post("/injection/projects", { name });
      toast(`Created “${name}”`);
      await loadProjects();
    } catch (e) {
      toast(`Create failed: ${(e as Error).message}`);
    }
  }

  async function deleteProject(pid: number) {
    if (!window.confirm("Delete this test project and all its hooks?")) return;
    try {
      await api.del(`/injection/projects/${pid}`);
      await loadProjects();
    } catch (e) {
      toast(`Delete failed: ${(e as Error).message}`);
    }
  }

  // What the chosen folder would become at this destination. Nothing is stored.
  async function previewImport(folder: string, dest: string) {
    if (activeProject == null) return;
    setImporting({ folder, dest, files: importing?.files ?? [], error: null });
    try {
      const r = await api.post<{ files: HelperImportPreview[] }>(
        `/injection/projects/${activeProject}/import`,
        { folder, dest, preview: true },
      );
      setImporting({ folder, dest, files: r.files, error: null });
    } catch (e) {
      const error = e instanceof ApiError ? e.detail : (e as Error).message;
      setImporting({ folder, dest, files: [], error });
    }
  }

  async function importHelpers() {
    if (activeProject == null || !importing) return;
    const { folder, dest } = importing;
    setImporting(null);
    try {
      const r = await api.post<{ imported: number }>(
        `/injection/projects/${activeProject}/import`,
        { folder, dest },
      );
      toast(`Imported ${r.imported} helper file(s)`);
      await loadProjectFiles(activeProject);
      await loadProjects();
    } catch (e) {
      toast(`Import failed: ${(e as Error).message}`);
    }
  }

  async function removeHelper(relPath: string) {
    if (activeProject == null) return;
    const change = changes.find((c) => c.rel_path === relPath);
    const ok = await confirm({
      title: change?.status === "added" ? `Remove ${relPath}?` : `Discard the changes to ${relPath}?`,
      message:
        change?.status === "added"
          ? "The file is removed from this test project."
          : "This test project's copy is removed. Its builds use the original again.",
      confirmLabel: change?.status === "added" ? "Remove" : "Discard changes",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(
        `/injection/projects/${activeProject}/files?rel_path=${encodeURIComponent(relPath)}`,
      );
      const key = `test:${relPath}`;
      setTabs((prev) => prev.filter((t) => t.key !== key));
      if (activeTab === key) setActiveTab(null);
      setDrafts(({ [key]: _gone, ...rest }) => rest);
      await loadProjectFiles(activeProject);
      await loadProjects();
    } catch (e) {
      toast(`Remove failed: ${(e as Error).message}`);
    }
  }

  // Make the open file a file of the test project: a full copy that its
  // builds use in place of the original. The original on disk isn't touched.
  async function changeFile() {
    if (activeProject == null || !activeTabObj || activeTabObj.kind === "test") return;
    const { relPath, key } = activeTabObj;
    try {
      await api.put(`/injection/projects/${activeProject}/files/content`, {
        rel_path: relPath,
        content: docContent,
      });
      const next = `test:${relPath}`;
      setTabs((prev) => prev.map((t) => (t.key === key ? { key: next, kind: "test", relPath } : t)));
      setActiveTab(next);
      await loadProjectFiles(activeProject);
      await loadProjects();
    } catch (e) {
      toast(`Change failed: ${e instanceof ApiError ? e.detail : (e as Error).message}`);
    }
  }

  async function saveFile() {
    if (activeProject == null || !activeTabObj || activeTabObj.kind !== "test") return;
    const { relPath, key } = activeTabObj;
    const text = drafts[key];
    if (text === undefined) return;
    try {
      await api.put(`/injection/projects/${activeProject}/files/content`, {
        rel_path: relPath,
        content: text,
      });
      setDocContent(text);
      setDrafts(({ [key]: _saved, ...rest }) => rest);
      await loadProjectFiles(activeProject);
    } catch (e) {
      toast(`Save failed: ${e instanceof ApiError ? e.detail : (e as Error).message}`);
    }
  }
  saveRef.current = saveFile;

  // Show the open test-project file next to what it replaces.
  async function toggleCompare() {
    if (activeProject == null || !activeTabObj) return;
    if (compare?.key === activeTabObj.key) {
      setCompare(null);
      return;
    }
    try {
      const r = await api.get<{ content: string | null }>(
        `/injection/projects/${activeProject}/files/original?rel_path=${encodeURIComponent(activeTabObj.relPath)}`,
      );
      setCompare({ key: activeTabObj.key, original: r.content });
    } catch (e) {
      toast(`Compare failed: ${(e as Error).message}`);
    }
  }

  // Create a hook at the editor cursor (Edit Mode): anchor to the surrounding
  // source lines so the fuzzy resolver can re-find the splice point.
  async function addHookAtCursor() {
    if (activeProject == null || !activeTabObj || !isSrcTab) return;
    const ed = editorRef.current;
    const pos = ed?.getPosition();
    if (!pos) return;
    if (!cursorChosen.current) {
      toast("Click the line where the code should go first.");
      return;
    }
    try {
      const r = await api.post<{ injection_id: number }>(
        `/injection/projects/${activeProject}/injections`,
        {
          src_file_path: activeTabObj.relPath,
          function_name: "",
          at_index: pos.lineNumber - 1, // splice before this line
          injected_code: "/* test code */",
        },
      );
      await loadHooks(activeProject);
      setSelectedHook(r.injection_id);
      setSnippetDraft("/* test code */");
      setMode("inject");
      toast("Hook added — edit the snippet on the right");
    } catch (e) {
      toast(`Add hook failed: ${(e as Error).message}`);
    }
  }

  async function saveSnippet() {
    if (activeProject == null || selectedHook == null) return;
    const h = hooks.find((x) => x.id === selectedHook);
    if (!h) return;
    try {
      await api.post(`/injection/projects/${activeProject}/injections`, {
        injection_id: selectedHook,
        src_file_path: h.src_file_path,
        function_name: h.function_name,
        line_above_code: h.line_above_code,
        line_below_code: h.line_below_code,
        injected_code: snippetDraft,
        offset_lines: h.offset_lines,
      });
      await loadHooks(activeProject);
      toast("Snippet saved");
    } catch (e) {
      toast(`Save failed: ${(e as Error).message}`);
    }
  }

  async function shiftHook(id: number, direction: "up" | "down") {
    try {
      await api.post(`/injection/injections/${id}/shift`, { direction });
      if (activeProject != null) await loadHooks(activeProject);
    } catch (e) {
      toast(
        e instanceof ApiError ? e.detail : `Shift failed: ${(e as Error).message}`,
      );
    }
  }

  async function deleteHook(id: number) {
    try {
      await api.del(`/injection/injections/${id}`);
      if (activeProject != null) await loadHooks(activeProject);
      if (selectedHook === id) setSelectedHook(null);
    } catch (e) {
      toast(`Delete failed: ${(e as Error).message}`);
    }
  }

  // Re-anchor a conflicted hook to the currently-highlighted line.
  async function placeHere() {
    if (!placing || activeProject == null) return;
    const h = hooks.find((x) => x.id === placing.hookId);
    if (!h) return;
    const atIndex =
      mode === "inject"
        ? previewLineToSourceIndex(placing.line, preview.blocks)
        : placing.line - 1;
    try {
      await api.post(`/injection/projects/${activeProject}/injections`, {
        injection_id: h.id,
        src_file_path: h.src_file_path,
        function_name: h.function_name,
        at_index: atIndex,
        injected_code: h.injected_code,
        offset_lines: h.offset_lines,
      });
      setPlacing(null);
      await loadHooks(activeProject);
      toast("Hook re-anchored");
    } catch (e) {
      toast(`Reposition failed: ${(e as Error).message}`);
    }
  }

  async function saveSettings(patch: Partial<BuildSettings>) {
    const next = { ...settings, ...patch };
    setSettings(next);
    try {
      await api.post("/injection/settings", patch);
    } catch (e) {
      toast(`Settings save failed: ${(e as Error).message}`);
    }
  }

  async function runBuild() {
    if (activeProject == null) return;
    try {
      // Sent as shown: a save from the last keystroke may still be on its way.
      await api.post(`/injection/projects/${activeProject}/build`, {
        command: settings.build_command,
        cwd: settings.build_cwd,
        terminal: settings.terminal,
      });
    } catch (e) {
      toast(
        e instanceof ApiError ? e.detail : `Build failed: ${(e as Error).message}`,
      );
    }
  }

  async function openTerminal() {
    try {
      await api.post("/injection/terminal", {
        cwd: settings.build_cwd,
        terminal: settings.terminal,
      });
    } catch (e) {
      toast(
        e instanceof ApiError
          ? e.detail
          : `Couldn't open a terminal: ${(e as Error).message}`,
      );
    }
  }

  async function runExport(
    modeSel: "modified" | "reconstruct",
    outDir: string,
    continueAnyway = false,
  ) {
    if (activeProject == null) return;
    try {
      const r = await api.post<{ count: number; conflicts: unknown[] }>(
        `/injection/projects/${activeProject}/export`,
        {
          mode: modeSel,
          out_dir: outDir,
          overwrite: true,
          continue_anyway: continueAnyway,
        },
      );
      toast(
        `Exported ${r.count} file(s)` +
          (r.conflicts.length ? ` (${r.conflicts.length} conflict(s) skipped)` : ""),
      );
    } catch (e) {
      const lock = (e as ApiError).body as { code?: string; message?: string } | undefined;
      if (lock?.code === "clean_lock") {
        const ok = await confirm({
          title: "Clean release data is missing",
          message: `${lock.message} You can import them later from Test Builds. Continuing is your responsibility and is recorded under your name.`,
          confirmLabel: "Continue anyway",
          danger: true,
        });
        if (ok) await runExport(modeSel, outDir, true);
        return;
      }
      toast(`Export failed: ${(e as Error).message}`);
    }
  }

  const selected = hooks.find((h) => h.id === selectedHook) ?? null;
  useEffect(() => {
    setSnippetDraft(selected?.injected_code ?? "");
  }, [selectedHook]); // eslint-disable-line react-hooks/exhaustive-deps

  // -- render --------------------------------------------------------------
  return (
    <div className="tci-root">
      {/* Sidebar: three independently scrollable, resizable panes —
          Test Projects, Production Source, Test Files. */}
      <aside className="tci-sidebar">
        <div
          className="tci-pane"
          ref={(el) => (paneRefs.current[0] = el)}
          style={{ flexGrow: paneFlex[0] }}
        >
          <div className="tci-sect-head">
            <span>Test Projects</span>
            <button
              className="tci-icon-btn"
              title="New test project"
              disabled={!canEdit}
              onClick={createProject}
            >
              ＋
            </button>
          </div>
          <div className="tci-pane-body">
            <div className="tci-projlist">
              {projects.length === 0 && (
                <div className="tci-empty">No test projects yet.</div>
              )}
              {projects.map((p) => (
                <div
                  key={p.id}
                  className={"tci-proj" + (p.id === activeProject ? " sel" : "")}
                  onClick={() => setActiveProject(p.id)}
                >
                  <span className="tci-proj-name">{p.name}</span>
                  <span className="tci-proj-meta">
                    {p.file_count}f · {p.injection_count}h
                  </span>
                  <button
                    className="tci-icon-btn danger"
                    title="Delete"
                    disabled={!canEdit}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      deleteProject(p.id);
                    }}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div
          className="tci-vsplit"
          title="Drag to resize"
          onMouseDown={(e) => startPaneResize(e, 0)}
        />

        <div
          className="tci-pane"
          ref={(el) => (paneRefs.current[1] = el)}
          style={{ flexGrow: paneFlex[1] }}
        >
          <div className="tci-sect-head">
            <span title={tree?.available ? tree.folder : undefined}>
              {tree?.available ? "Project" : "Production Source"}
            </span>
            {tree?.available && activeProject != null && (
              <button
                className="tci-icon-btn"
                title={`Read the project folder again\n${tree.folder}`}
                onClick={() => loadTree(activeProject)}
              >
                ⟳
              </button>
            )}
          </div>
          {sourceFiles.length > 0 && (
            <input
              className="tci-search"
              type="search"
              placeholder="Search functions and files"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          )}
          <div className="tci-pane-body">
            {sourceErr && !tree?.available && <div className="tci-empty err">{sourceErr}</div>}
            {!sourceErr && !tree?.available && sourceFiles.length === 0 && (
              <div className="tci-empty">No source files.</div>
            )}
            {tree && !tree.available && activeProject != null && sourceFiles.length > 0 && (
              <div className="tci-empty">
                Only the imported C sources are listed: this PC has no project folder. Set
                the test build's source folder to see every file.
              </div>
            )}
            {search ? (
              <>
                {search.functions.map((f) => (
                  <button
                    key={`${f.rel_path}:${f.line}:${f.name}`}
                    className="tci-tree-row tci-result"
                    title={`${f.rel_path}:${f.line}`}
                    onClick={() => {
                      setReveal({ key: `src:${f.rel_path}`, line: f.line });
                      openFile("src", f.rel_path);
                    }}
                  >
                    <span className="tci-tree-name">{f.name}</span>
                    <span className="tci-result-file">
                      {f.rel_path.split("/").pop()}:{f.line}
                    </span>
                  </button>
                ))}
                {search.files.map((f) => (
                  <button
                    key={f.rel_path}
                    className="tci-tree-row tci-result"
                    title={f.rel_path}
                    onClick={() => openFile("src", f.rel_path)}
                  >
                    <FileIcon size={13} />
                    <span className="tci-tree-name">{f.rel_path}</span>
                  </button>
                ))}
                {search.functions.length + search.files.length === 0 && (
                  <div className="tci-empty">Nothing found.</div>
                )}
              </>
            ) : tree?.available && activeProject != null ? (
              <ProjectTree
                projectId={activeProject}
                root={tree.entries}
                changed={changedSet}
                hooked={hookedMap}
                activeTab={activeTab}
                refreshKey={treeKey}
                onOpen={openPath}
                onError={(m) => toast(`Project folder: ${m}`)}
              />
            ) : (
              <FileTree
                files={sourceFiles.map((f) => f.rel_path)}
                kind="src"
                activeTab={activeTab}
                onOpen={openFile}
              />
            )}
          </div>
        </div>

        <div
          className="tci-vsplit"
          title="Drag to resize"
          onMouseDown={(e) => startPaneResize(e, 1)}
        />

        <div
          className="tci-pane"
          ref={(el) => (paneRefs.current[2] = el)}
          style={{ flexGrow: paneFlex[2] }}
        >
          <div className="tci-sect-head">
            <span title="Everything this test project changes in a build">
              Changed Files{changes.length ? ` (${changes.length})` : ""}
            </span>
            <button
              className="tci-icon-btn"
              title="Import .c/.h helpers"
              disabled={!canEdit || activeProject == null}
              onClick={() => setImportOpen(true)}
            >
              ⤓
            </button>
          </div>
          <div className="tci-pane-body">
            {changes.length === 0 && (
              <div className="tci-empty">
                Nothing changed yet. Open a file and click “Change file”, or add a hook.
              </div>
            )}
            {changes.map((c) => {
              const kind: TabKind = c.status === "hooks" ? "src" : "test";
              const key = `${kind}:${c.rel_path}`;
              const warn = c.original_changed
                ? "The original has changed since this file was changed. A build still uses this copy."
                : c.hooks_lost
                  ? `This copy replaces the whole file, so its ${c.hooks_lost} hook(s) aren't applied.`
                  : "";
              return (
                <div key={key} className="tci-tree-filerow">
                  <button
                    className={"tci-tree-row file" + (activeTab === key ? " sel" : "")}
                    style={{ paddingLeft: 8 }}
                    title={warn ? `${c.rel_path}\n${warn}` : c.rel_path}
                    onClick={() => openFile(kind, c.rel_path)}
                  >
                    <span className={"tci-change " + c.status}>
                      {c.status === "modified" ? "M" : c.status === "added" ? "A" : "H"}
                    </span>
                    <span className="tci-tree-name">{c.rel_path.split("/").pop()}</span>
                    <span className="tci-change-dir">
                      {c.status === "hooks"
                        ? `${c.hooks} hook${c.hooks === 1 ? "" : "s"}`
                        : c.rel_path.split("/").slice(0, -1).join("/")}
                    </span>
                    {warn && <span className="tci-change-warn">⚠</span>}
                  </button>
                  {canEdit && c.status !== "hooks" && (
                    <button
                      className="tci-icon-btn danger tci-tree-del"
                      title={c.status === "added" ? "Remove from this test project" : "Discard the changes"}
                      onClick={() => removeHelper(c.rel_path)}
                    >
                      ✕
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </aside>

      {/* Center: tabs + editor + build console */}
      <div className="tci-center">
        <div className="tci-tabbar">
          {tabs.map((t) => (
            <div
              key={t.key}
              className={"tci-tab" + (t.key === activeTab ? " active" : "")}
              onClick={() => setActiveTab(t.key)}
            >
              <span className={"tci-tab-dot " + t.kind} />
              <span className="tci-tab-label">
                {t.relPath.split("/").pop()}
                {drafts[t.key] !== undefined && " ●"}
              </span>
              <button
                className="tci-tab-x"
                onClick={(e) => {
                  e.stopPropagation();
                  setTabs((prev) => prev.filter((x) => x.key !== t.key));
                  if (activeTab === t.key) setActiveTab(null);
                }}
              >
                ✕
              </button>
            </div>
          ))}
          <div className="tci-tab-spacer" />
          {activeTabObj && !isTestTab && activeProject != null && (
            <button
              className="tci-mini-btn"
              disabled={
                !canEdit ||
                docKey !== activeTabObj.key ||
                (activeTabObj.kind === "disk" && diskInfo?.key === activeTabObj.key && !diskInfo.editable)
              }
              title={
                activeTabObj.kind === "disk" && diskInfo?.key === activeTabObj.key && !diskInfo.editable
                  ? diskInfo.reason
                  : "Keep a copy of this file in the test project and edit it. Its builds use the copy; the original isn't touched."
              }
              onClick={changeFile}
            >
              ✎ Change file
            </button>
          )}
          {isTestTab && (
            <>
              <button
                className={"tci-mini-btn" + (compare?.key === activeTab ? " on" : "")}
                onClick={toggleCompare}
                title="Show this file next to the original it replaces"
              >
                ⇄ Compare
              </button>
              <button className="tci-mini-btn" disabled={!canEdit || !dirty} onClick={saveFile}>
                Save
              </button>
              <button
                className="tci-mini-btn"
                disabled={!canEdit}
                onClick={() => activeTabObj && removeHelper(activeTabObj.relPath)}
                title="Remove this test project's copy"
              >
                Discard
              </button>
            </>
          )}
          {isSrcTab && (
            <div className="tci-modeswitch">
              <button
                className={mode === "inject" ? "active" : ""}
                onClick={() => setMode("inject")}
              >
                Inject
              </button>
              <button
                className={mode === "edit" ? "active" : ""}
                disabled={!canEdit}
                onClick={() => setMode("edit")}
              >
                Edit
              </button>
            </div>
          )}
          {isSrcTab && mode === "edit" && (
            <button className="tci-mini-btn" onClick={addHookAtCursor}>
              ＋ Hook at cursor
            </button>
          )}
        </div>

        <div className="tci-editor-wrap">
          <div className="tci-editor">
            {activeTabObj && isTestTab && compare?.key === activeTab ? (
              compare.original === null ? (
                <div className="center-msg">
                  This file is added by the test project: there is no original to compare with.
                </div>
              ) : (
                <DiffEditor
                  language={languageOf(activeTabObj.relPath)}
                  theme={theme}
                  original={compare.original}
                  modified={editorValue}
                  options={{
                    readOnly: true,
                    renderSideBySide: true,
                    minimap: { enabled: false },
                    fontSize: 12,
                    automaticLayout: true,
                    scrollBeyondLastLine: false,
                  }}
                />
              )
            ) : activeTabObj ? (
              <Editor
                language={languageOf(activeTabObj.relPath)}
                theme={theme}
                value={editorValue}
                onMount={onEditorMount}
                onChange={(v) => {
                  if (!isTestTab || activeTab == null || v === undefined) return;
                  // Back to the saved text is not a change.
                  setDrafts(({ [activeTab]: _old, ...rest }) =>
                    v === docContent ? rest : { ...rest, [activeTab]: v },
                  );
                }}
                options={{
                  // A source file only shows where hooks go: code goes in a
                  // hook's snippet, and typing there would move the lines under
                  // the cursor. A test-project file is edited directly.
                  readOnly: !(isTestTab && canEdit && docKey === activeTab),
                  domReadOnly: !(isTestTab && canEdit && docKey === activeTab),
                  minimap: { enabled: false },
                  fontSize: 12,
                  lineNumbers: "on",
                  glyphMargin: true,
                  scrollBeyondLastLine: false,
                  automaticLayout: true,
                  // Code completion (issue 3): the symbol completer was firing
                  // but the suggest widget was invisible — clipped by the
                  // editor container and rendered with no label. Render it in
                  // document.body and drop the kind-icons (mirrors TestDesign).
                  quickSuggestions: true,
                  suggestOnTriggerCharacters: true,
                  fixedOverflowWidgets: true,
                  suggest: { showIcons: false, showStatusBar: false },
                }}
              />
            ) : (
              <div className="center-msg">
                Select a file to begin.
              </div>
            )}
          </div>

          {/* Hook inspector */}
          {isSrcTab && (
            <div className="tci-hooks">
              <div className="tci-panel-head">
                Injection Hooks ({fileHooks.length})
              </div>
              <div className="tci-hooklist">
                {fileHooks.length === 0 && (
                  <div className="tci-empty">
                    {mode === "edit"
                      ? "Place the cursor and click “Hook at cursor”."
                      : "Switch to Edit mode to add a hook."}
                  </div>
                )}
                {fileHooks.map((h) => {
                  const badge = confidenceBadge(h.confidence);
                  const conflict = h.confidence < 3;
                  return (
                    <div
                      key={h.id}
                      className={
                        "tci-hook" + (h.id === selectedHook ? " sel" : "")
                      }
                      onClick={() => setSelectedHook(h.id)}
                    >
                      <div className="tci-hook-top">
                        <span className={"tci-badge " + badge.cls}>
                          {badge.text}
                        </span>
                        <code className="tci-hook-snip">
                          {(h.injected_code || "").split("\n")[0] || "—"}
                        </code>
                      </div>
                      <div className="tci-hook-ctl">
                        {conflict ? (
                          <button
                            className="tci-mini-btn"
                            disabled={!canEdit}
                            onClick={(e) => {
                              e.stopPropagation();
                              setPlacing({ hookId: h.id, line: 1 });
                              setSelectedHook(h.id);
                            }}
                          >
                            Reposition
                          </button>
                        ) : (
                          <>
                            <button
                              className="tci-mini-btn"
                              title="Shift up"
                              disabled={!canEdit}
                              onClick={(e) => {
                                e.stopPropagation();
                                shiftHook(h.id, "up");
                              }}
                            >
                              ↑
                            </button>
                            <button
                              className="tci-mini-btn"
                              title="Shift down"
                              disabled={!canEdit}
                              onClick={(e) => {
                                e.stopPropagation();
                                shiftHook(h.id, "down");
                              }}
                            >
                              ↓
                            </button>
                          </>
                        )}
                        <button
                          className="tci-mini-btn danger"
                          disabled={!canEdit}
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteHook(h.id);
                          }}
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>

              {placing && (
                <div className="tci-placing-bar">
                  <span>Repositioning to line {placing.line}</span>
                  <button
                    className="tci-mini-btn"
                    onClick={() =>
                      setPlacing((p) =>
                        p ? { ...p, line: Math.max(1, p.line - 1) } : p,
                      )
                    }
                  >
                    ↑
                  </button>
                  <button
                    className="tci-mini-btn"
                    onClick={() =>
                      setPlacing((p) => (p ? { ...p, line: p.line + 1 } : p))
                    }
                  >
                    ↓
                  </button>
                  <button className="save-btn" onClick={placeHere}>
                    Place
                  </button>
                  <button className="tci-mini-btn" onClick={() => setPlacing(null)}>
                    Cancel
                  </button>
                </div>
              )}

              {selected && (
                <div className="tci-snippet">
                  <div className="tci-panel-head">Snippet</div>
                  <textarea
                    className="tci-snippet-area"
                    spellCheck={false}
                    value={snippetDraft}
                    disabled={!canEdit}
                    onChange={(e) => setSnippetDraft(e.target.value)}
                  />
                  <button
                    className="save-btn"
                    disabled={!canEdit}
                    onClick={saveSnippet}
                  >
                    Save snippet
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Build console */}
        <div className={"tci-console" + (consoleOpen ? " open" : "")}>
          <div className="tci-console-bar">
            <button
              className="tci-mini-btn"
              onClick={() => setConsoleOpen((o) => !o)}
            >
              {consoleOpen ? "▾" : "▸"} Build Console
            </button>
            <select
              className="tci-select"
              value={settings.terminal}
              disabled={!canEdit}
              onChange={(e) =>
                saveSettings({ terminal: e.target.value as TerminalKind })
              }
            >
              {TERMINALS.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
            {settings.terminal === "wsl" && (
              <input
                className="tci-input"
                placeholder="distro"
                value={settings.wsl_distro}
                disabled={!canEdit}
                onChange={(e) => saveSettings({ wsl_distro: e.target.value })}
              />
            )}
            <input
              className="tci-input grow"
              placeholder="build command (e.g. make 0)"
              value={settings.build_command}
              disabled={!canEdit}
              onChange={(e) => saveSettings({ build_command: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !building && activeProject != null) runBuild();
              }}
            />
            <input
              className="tci-input"
              placeholder="build folder"
              value={settings.build_cwd}
              disabled={!canEdit}
              onChange={(e) => saveSettings({ build_cwd: e.target.value })}
            />
            <button
              className="save-btn"
              disabled={building || activeProject == null}
              onClick={runBuild}
            >
              {building ? "Building…" : "Build"}
            </button>
            <button
              className="tci-mini-btn"
              title="Open an empty terminal in the build folder"
              onClick={openTerminal}
            >
              Terminal
            </button>
            <button
              className="tci-mini-btn"
              disabled={activeProject == null}
              title="Export injected source"
              onClick={() => {
                const out = window.prompt("Export to directory:");
                if (out) runExport("modified", out);
              }}
            >
              Export
            </button>
            <button
              className="tci-mini-btn"
              onClick={() => setBuildLogs([])}
              title="Clear"
            >
              Clear
            </button>
          </div>
          {consoleOpen && (
            <div className="tci-console-log" ref={logRef}>
              {buildLogs.length === 0 ? (
                <div className="tci-empty">No build output yet.</div>
              ) : (
                buildLogs.map((line, i) => (
                  <div key={i} className="tci-log-line">
                    {line}
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>

      {importOpen && (
        <FolderPicker
          mode="folder"
          title="Import test helpers — choose a folder of .c/.h files"
          hint="Navigate to the folder containing the test helper sources"
          onCancel={() => setImportOpen(false)}
          onConfirm={(path) => {
            setImportOpen(false);
            previewImport(path, path.split(/[\\/]/).filter(Boolean).pop() ?? "");
          }}
        />
      )}
      {importing && (
        <div className="modal-overlay confirm-overlay">
          <div className="modal confirm-modal">
            <div className="modal-head">Where should the test files go?</div>
            <div className="confirm-body">
              <label className="tci-dest">
                <span>Folder in the source tree (empty is the top folder)</span>
                <input
                  className="tci-input"
                  list="tci-source-folders"
                  value={importing.dest}
                  onChange={(e) => previewImport(importing.folder, e.target.value)}
                  autoFocus
                />
                <datalist id="tci-source-folders">
                  {sourceFolders.map((f) => (
                    <option key={f} value={f} />
                  ))}
                </datalist>
              </label>
              {importing.error && <div className="tci-empty err">{importing.error}</div>}
              <div className="tci-dest-list">
                {importing.files.map((f) => (
                  <div key={f.rel_path} className={f.replaces_source ? "warn" : ""}>
                    {f.rel_path}
                    {f.replaces_source && " replaces the source file"}
                  </div>
                ))}
              </div>
            </div>
            <div className="confirm-foot">
              <button className="confirm-cancel" onClick={() => setImporting(null)}>
                Cancel
              </button>
              <button
                className="confirm-ok primary"
                disabled={!!importing.error || importing.files.length === 0}
                onClick={importHelpers}
              >
                Import {importing.files.length} file(s)
              </button>
            </div>
          </div>
        </div>
      )}
      {confirmNode}
    </div>
  );
}
