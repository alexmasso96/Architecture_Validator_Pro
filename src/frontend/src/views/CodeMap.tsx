import Editor, { type OnMount } from "@monaco-editor/react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { ApiError, api } from "../api/client";
import { useSSE } from "../api/useSSE";
import type {
  CodeMapGraph,
  CodeMapResponse,
  FunctionDetail,
  GraphNode,
  MindmapStatus,
  ModelsResponse,
  ReleasesResponse,
  SourceResponse,
} from "../api/types";
import { FolderPicker } from "../components/FolderPicker";
import { persistKey } from "../prefs";

type ListState = "loading" | "ready" | "needs-build" | "error";

// Monaco theme follows the app's resolved light/dark (<html data-theme>).
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

// ---------------------------------------------------------------------------
// Call-graph SVG — manual column layout: callers on the left, focus in the
// centre, callees on the right (one column per graph level). The variables the
// focus reads sit above it and the ones it writes below it, joined by arcs.
// Edges are curved arrows; double-clicking a function node re-focuses on it.
// ---------------------------------------------------------------------------
const NODE_H = 34;
const COL_GAP = 76;
const ROW_GAP = 14;
const PARAM_GAP = 22; // focus ↔ first variable row
const PAD = 24;
const CHAR_W = 7.3; // 12px monospace advance
const MIN_W = 120;
const MAX_W = 320;
const ACCESS_TAG_W = 26; // "R" / "W" / "RW" tag inside variable nodes
const MIN_SCALE = 0.5; // shrink big graphs to fit, but never below this

const ACCESS_TAG = { read: "R", write: "W", readwrite: "RW" } as const;
const ACCESS_TEXT = {
  read: "read by",
  write: "written by",
  readwrite: "read and written by",
} as const;

function nodeWidth(n: GraphNode): number {
  const tag = n.type === "param" ? ACCESS_TAG_W : 0;
  return Math.min(MAX_W, Math.max(MIN_W, n.name.length * CHAR_W + 26 + tag));
}

function fitLabel(name: string, room: number): string {
  const max = Math.max(4, Math.floor(room / CHAR_W));
  return name.length > max ? name.slice(0, max - 1) + "…" : name;
}

type Box = { x: number; y: number; w: number; node: GraphNode };

// Cubic path between two boxes. Forward edges leave the right side and enter
// the left side; back/same-column edges and self-calls loop underneath.
function edgePath(a: Box, b: Box): string {
  if (a === b) {
    const x = a.x + a.w - 18;
    const y = a.y + NODE_H;
    return `M ${x} ${y} C ${x} ${y + 26}, ${x + 30} ${y + 18}, ${a.x + a.w} ${a.y + NODE_H / 2 + 4}`;
  }
  if (b.x > a.x + a.w) {
    const x1 = a.x + a.w;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    const dx = Math.max(28, (x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }
  const x1 = a.x + a.w / 2;
  const x2 = b.x + b.w / 2;
  const y1 = a.y + NODE_H;
  const y2 = b.y + NODE_H;
  const dip = Math.max(y1, y2) + 28 + Math.abs(x2 - x1) * 0.08;
  return `M ${x1} ${y1} C ${x1} ${dip}, ${x2} ${dip}, ${x2} ${y2}`;
}

// Arc between the focus and one of its variables, bulging out on the left
// (read: variable → focus) or the right (write: focus → variable).
function paramArc(focus: Box, v: Box, kind: "read" | "write"): string {
  const above = v.y < focus.y;
  const fy = focus.y + (above ? 9 : NODE_H - 9);
  const vy = v.y + NODE_H / 2;
  const k = Math.min(COL_GAP * 0.85, 22 + Math.abs(vy - fy) * 0.22);
  if (kind === "read") {
    const x = focus.x;
    return `M ${x} ${vy} C ${x - k} ${vy}, ${x - k} ${fy}, ${x} ${fy}`;
  }
  const x = focus.x + focus.w;
  return `M ${x} ${fy} C ${x + k} ${fy}, ${x + k} ${vy}, ${x} ${vy}`;
}

function CallGraph({
  graph,
  focus,
  onFocus,
}: {
  graph: CodeMapGraph | null;
  focus: string | null;
  onFocus: (name: string) => void;
}) {
  const uid = useId().replace(/:/g, "");
  const bodyRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const [hover, setHover] = useState<string | null>(null);

  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() =>
      setBox({ w: el.clientWidth, h: el.clientHeight }),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layout = useMemo(() => {
    if (!graph || graph.nodes.length === 0) return null;
    // Functions grouped by level, columns ordered left→right by ascending level.
    const byLevel = new Map<number, GraphNode[]>();
    const reads: GraphNode[] = [];
    const writes: GraphNode[] = [];
    for (const n of graph.nodes) {
      if (n.type === "param") {
        (n.access === "read" ? reads : writes).push(n);
        continue;
      }
      const arr = byLevel.get(n.level) ?? [];
      arr.push(n);
      byLevel.set(n.level, arr);
    }
    if (!byLevel.has(0)) return null;
    const levels = [...byLevel.keys()].sort((a, b) => a - b);
    for (const lvl of levels) {
      byLevel.get(lvl)!.sort((a, b) => a.name.localeCompare(b.name));
    }
    reads.sort((a, b) => a.name.localeCompare(b.name));
    writes.sort((a, b) => a.name.localeCompare(b.name));

    // One width per column (widest label, clamped) so columns stay aligned.
    const colW = levels.map((lvl) =>
      Math.max(
        ...byLevel.get(lvl)!.map(nodeWidth),
        ...(lvl === 0 ? [...reads, ...writes].map(nodeWidth) : []),
      ),
    );
    const stack = (k: number) => (k ? k * NODE_H + (k - 1) * ROW_GAP + PARAM_GAP : 0);
    const colH = (k: number) => k * NODE_H + (k - 1) * ROW_GAP;
    // The focus stays vertically centred, so its column needs room for the
    // taller of the two variable stacks on both sides.
    const centreH = NODE_H + 2 * Math.max(stack(reads.length), stack(writes.length));
    const height =
      PAD * 2 +
      Math.max(centreH, ...levels.map((l) => colH(byLevel.get(l)!.length)));
    const width =
      PAD * 2 + colW.reduce((s, w) => s + w, 0) + (levels.length - 1) * COL_GAP;

    const pos = new Map<string, Box>();
    let x = PAD;
    levels.forEach((lvl, ci) => {
      const w = colW[ci];
      const col = byLevel.get(lvl)!;
      const y0 = (height - colH(col.length)) / 2;
      col.forEach((node, ri) => {
        pos.set(node.name, { x, y: y0 + ri * (NODE_H + ROW_GAP), w, node });
      });
      if (lvl === 0) {
        const fy = (height - NODE_H) / 2;
        reads.forEach((node, i) => {
          const fromFocus = reads.length - i;
          pos.set(node.name, {
            x, w, node,
            y: fy - PARAM_GAP - fromFocus * NODE_H - (fromFocus - 1) * ROW_GAP,
          });
        });
        writes.forEach((node, i) => {
          pos.set(node.name, {
            x, w, node,
            y: fy + NODE_H + PARAM_GAP + i * (NODE_H + ROW_GAP),
          });
        });
      }
      x += w + COL_GAP;
    });

    const centre = [...pos.values()].find((p) => p.node.type === "center")!;
    const edges = graph.edges.flatMap((e) => {
      const a = pos.get(e.source);
      const b = pos.get(e.target);
      if (!a || !b) return [];
      const isParam = e.kind === "read" || e.kind === "write";
      const v = e.kind === "read" ? a : b;
      return [
        {
          a,
          b,
          kind: e.kind,
          verified: e.verified,
          d: isParam ? paramArc(centre, v, e.kind as "read" | "write") : edgePath(a, b),
        },
      ];
    });

    return { pos, edges, width, height };
  }, [graph]);

  if (!layout) {
    return (
      <div className="cm-graph-body" ref={bodyRef}>
        <div className="cm-graph-empty">No call graph for this function.</div>
      </div>
    );
  }

  const { pos, edges, width, height } = layout;
  // 1:1 when it fits; otherwise shrink (down to MIN_SCALE) and let it scroll.
  const scale =
    box.w && box.h
      ? Math.max(MIN_SCALE, Math.min(1, (box.w - 8) / width, (box.h - 8) / height))
      : 1;
  const verification = !!graph?.edge_verification;

  const edgeClass = (e: (typeof edges)[number]) => {
    if (e.kind === "read") return "cm-edge-read";
    if (e.kind === "write") return "cm-edge-write";
    // #6C: with a .o dataset present, compiler-confirmed edges render solid
    // and source-only edges dashed. Without a dataset every edge stays plain —
    // no false "unverified" signal.
    if (!verification) return "cm-edge-plain";
    return e.verified ? "cm-edge-verified" : "cm-edge-unverified";
  };
  const markerFor = (cls: string) =>
    `url(#${uid}-${cls === "cm-edge-unverified" ? "cm-edge-plain" : cls})`;

  return (
    <div className="cm-graph-body" ref={bodyRef}>
      <svg
        className={"cm-graph-svg" + (hover ? " hovering" : "")}
        viewBox={`0 0 ${width} ${height}`}
        width={width * scale}
        height={height * scale}
      >
        <defs>
          {["cm-edge-plain", "cm-edge-verified", "cm-edge-read", "cm-edge-write"].map(
            (cls) => (
              <marker
                key={cls}
                id={`${uid}-${cls}`}
                className={"cm-arrow " + cls}
                viewBox="0 0 10 10"
                refX={9}
                refY={5}
                markerUnits="userSpaceOnUse"
                markerWidth={10}
                markerHeight={10}
                orient="auto-start-reverse"
              >
                <path d="M1.5 1.5 L9 5 L1.5 8.5 Q3.6 5 1.5 1.5 Z" />
              </marker>
            ),
          )}
        </defs>
        <g className="cm-edges">
          {edges.map((e, i) => {
            const cls = edgeClass(e);
            const lit = hover !== null && (e.a.node.name === hover || e.b.node.name === hover);
            return (
              <path
                key={i}
                className={cls + (lit ? " lit" : "")}
                d={e.d}
                markerEnd={markerFor(cls)}
              >
                {(cls === "cm-edge-verified" || cls === "cm-edge-unverified") && (
                  <title>
                    {e.verified
                      ? "Verified by the object files (compiler-confirmed call)"
                      : "Seen in source only — not confirmed by the object files " +
                        "(possibly disabled by #ifdef, or an intra-file call the " +
                        "relocations cannot see)"}
                  </title>
                )}
              </path>
            );
          })}
        </g>
        <g className="cm-nodes">
          {[...pos.values()].map(({ x, y, w, node }) => {
            const isParam = node.type === "param";
            const isFocus = !isParam && (node.type === "center" || node.name === focus);
            const tagW = isParam ? ACCESS_TAG_W : 0;
            const access = node.access ?? "read";
            return (
              <g
                key={node.type + ":" + node.name}
                className={"cm-node " + node.type + (isFocus ? " focus" : "")}
                transform={`translate(${x},${y})`}
                onMouseEnter={() => setHover(node.name)}
                onMouseLeave={() => setHover(null)}
                onDoubleClick={() => !isFocus && !isParam && onFocus(node.name)}
              >
                <title>
                  {isParam
                    ? `${node.name} — variable ${ACCESS_TEXT[access]} ${focus ?? "the focus"}`
                    : node.name + (isFocus ? "" : " — double-click to focus")}
                </title>
                <rect rx={isParam ? NODE_H / 2 : 8} width={w} height={NODE_H} />
                {isParam && (
                  <text className="cm-access" x={8 + tagW / 2} y={NODE_H / 2}>
                    {ACCESS_TAG[access]}
                  </text>
                )}
                <text x={tagW + (w - tagW) / 2} y={NODE_H / 2}>
                  {fitLabel(node.name, w - tagW - 20)}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
      {graph?.truncated && (
        <div className="cm-graph-note">
          Showing {graph.max_nodes} of {graph.total_nodes} functions — reduce the
          layers to see the full neighbourhood.
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Call-graph view options (the sliders button in the graph header). Persisted
// like the other UI prefs: localStorage + the durable backend mirror.
// ---------------------------------------------------------------------------
interface GraphOptions {
  callers: boolean;
  callees: boolean;
  params: boolean;
  back: number;
  fwd: number;
}

const GRAPH_OPTS_KEY = "arch.codemap.graph";
const MAX_LAYERS = 5; // backend cap on back/fwd
const DEFAULT_GRAPH_OPTS: GraphOptions = {
  callers: true,
  callees: true,
  params: true,
  back: 1,
  fwd: 1,
};

function loadGraphOptions(): GraphOptions {
  try {
    const raw = localStorage.getItem(GRAPH_OPTS_KEY);
    return raw ? { ...DEFAULT_GRAPH_OPTS, ...JSON.parse(raw) } : DEFAULT_GRAPH_OPTS;
  } catch {
    return DEFAULT_GRAPH_OPTS;
  }
}

function saveGraphOptions(o: GraphOptions) {
  const v = JSON.stringify(o);
  try {
    localStorage.setItem(GRAPH_OPTS_KEY, v);
  } catch {
    /* storage disabled — the backend mirror still has it */
  }
  persistKey(GRAPH_OPTS_KEY, v);
}

function SlidersIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.5" strokeLinecap="round">
      <path d="M2 4h7M13 4h1M2 12h1M7 12h7" />
      <circle cx="11" cy="4" r="2" />
      <circle cx="5" cy="12" r="2" />
    </svg>
  );
}

function LayerStepper({
  value,
  disabled,
  onChange,
}: {
  value: number;
  disabled: boolean;
  onChange: (n: number) => void;
}) {
  return (
    <span className={"cm-stepper" + (disabled ? " off" : "")}>
      <button
        disabled={disabled || value <= 1}
        onClick={() => onChange(value - 1)}
        aria-label="Fewer layers"
      >
        −
      </button>
      <span className="cm-stepper-val">{value}</span>
      <button
        disabled={disabled || value >= MAX_LAYERS}
        onClick={() => onChange(value + 1)}
        aria-label="More layers"
      >
        +
      </button>
    </span>
  );
}

function GraphOptionsPopover({
  opts,
  onChange,
  onClose,
}: {
  opts: GraphOptions;
  onChange: (o: GraphOptions) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (ref.current?.contains(t) || t.closest(".cm-opts-btn")) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const set = (patch: Partial<GraphOptions>) => onChange({ ...opts, ...patch });
  const toggle = (k: "callers" | "callees" | "params", label: string) => (
    <label className="cm-opt-toggle">
      <input type="checkbox" checked={opts[k]} onChange={(e) => set({ [k]: e.target.checked })} />
      <span className="cm-switch" />
      {label}
    </label>
  );

  return (
    <div className="cm-opts" ref={ref} role="dialog" aria-label="Call graph options">
      <div className="cm-opts-title">Call Graph View</div>
      <div className="cm-opt-row">
        {toggle("callers", "Show callers")}
        <LayerStepper value={opts.back} disabled={!opts.callers}
          onChange={(back) => set({ back })} />
      </div>
      <div className="cm-opt-row">
        {toggle("callees", "Show called")}
        <LayerStepper value={opts.fwd} disabled={!opts.callees}
          onChange={(fwd) => set({ fwd })} />
      </div>
      <div className="cm-opt-row">
        {toggle("params", "Show parameters")}
      </div>
      <div className="cm-opts-foot">
        <span>Layers = call levels followed in each direction.</span>
        <button className="link-btn" onClick={() => onChange(DEFAULT_GRAPH_OPTS)}>
          Reset
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Draggable divider between the Code Map panes. Widths persist across launches.
// ---------------------------------------------------------------------------
const LAYOUT_KEY = "arch.codemap.layout";
const SIDEBAR_W = 240;

function loadPaneLayout(): { sidebar: number; graph: number | null } {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    const v = raw ? JSON.parse(raw) : {};
    return {
      sidebar: typeof v.sidebar === "number" ? v.sidebar : SIDEBAR_W,
      graph: typeof v.graph === "number" ? v.graph : null,
    };
  } catch {
    return { sidebar: SIDEBAR_W, graph: null };
  }
}

function Splitter({
  start,
  onResize,
  onDone,
  onReset,
}: {
  // Width of the pane left of the divider when the drag begins.
  start: () => number;
  onResize: (w: number) => void;
  onDone: () => void;
  onReset: () => void;
}) {
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const el = e.currentTarget;
    const w0 = start();
    const x0 = e.clientX;
    el.setPointerCapture(e.pointerId);
    el.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    const move = (ev: PointerEvent) => onResize(w0 + ev.clientX - x0);
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      el.classList.remove("dragging");
      document.body.style.cursor = "";
      onDone();
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };
  return (
    <div
      className="cm-splitter"
      role="separator"
      aria-orientation="vertical"
      title="Drag to resize · double-click to reset"
      onPointerDown={onPointerDown}
      onDoubleClick={onReset}
    />
  );
}

// ---------------------------------------------------------------------------
// Details inspector card — metadata + clickable callers/callees + globals.
// ---------------------------------------------------------------------------
function DetailsCard({
  detail,
  source,
  onFocus,
}: {
  detail: FunctionDetail | null;
  source: SourceResponse | null;
  onFocus: (name: string) => void;
}) {
  if (!detail) return null;
  const hexAddr =
    detail.address && detail.address > 0
      ? "0x" + detail.address.toString(16)
      : "—";
  return (
    <div className="cm-details">
      <div className="cm-det-title">{detail.name}</div>
      {detail.signature && <code className="cm-det-sig">{detail.signature}</code>}

      <div className="cm-det-meta">
        <span title="Address">addr {hexAddr}</span>
        <span title="Size">size {detail.size || 0} B</span>
        {detail.return_type && <span>returns {detail.return_type}</span>}
        {detail.component && (
          <span
            title={
              (detail.components?.length ?? 0) > 1
                ? `Defined in ${detail.components!.join(", ")} — merged; address from ${detail.component}`
                : "Component symbol file"
            }
          >
            ◆ {(detail.components?.length ?? 0) > 1 ? detail.components!.join(" · ") : detail.component}
          </span>
        )}
        {detail.file && (
          <span title={detail.file}>
            {detail.file.split(/[\\/]/).pop()}
            {detail.line_start ? `:${detail.line_start}` : ""}
          </span>
        )}
      </div>

      {source && !source.found && source.reason && (
        <div className="cm-det-note">Source: {source.reason}</div>
      )}

      <div className="cm-det-cols">
        <div>
          <div className="cm-det-h">Callers ({detail.callers.length})</div>
          {detail.callers.length === 0 ? (
            <div className="cm-det-empty">none</div>
          ) : (
            detail.callers.map((c) => (
              <button key={c} className="cm-chip" onClick={() => onFocus(c)}>
                {c}
              </button>
            ))
          )}
        </div>
        <div>
          <div className="cm-det-h">Callees ({detail.callees.length})</div>
          {detail.callees.length === 0 ? (
            <div className="cm-det-empty">none</div>
          ) : (
            detail.callees.map((c) => (
              <button key={c} className="cm-chip" onClick={() => onFocus(c)}>
                {c}
              </button>
            ))
          )}
        </div>
        <div>
          <div className="cm-det-h">Globals ({detail.globals.length})</div>
          {detail.globals.length === 0 ? (
            <div className="cm-det-empty">none</div>
          ) : (
            detail.globals.map((g) => (
              <div key={g.name} className="cm-global" title={g.type}>
                <span className="cm-global-name">{g.name}</span>
                <span className="cm-global-type">{g.type}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Code Map view.
// ---------------------------------------------------------------------------
export function CodeMap({
  toast,
  focusTarget = null,
  onFocusConsumed,
}: {
  toast: (msg: string) => void;
  // A function name to jump to, set when the user picks "Show in Code Map" in
  // the Workspace. Consumed once (cleared via onFocusConsumed) so it doesn't
  // re-fire on later re-renders.
  focusTarget?: string | null;
  onFocusConsumed?: () => void;
}) {
  const [listState, setListState] = useState<ListState>("loading");
  const [listError, setListError] = useState<string | null>(null);
  const [functions, setFunctions] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [focused, setFocused] = useState<string | null>(null);

  const [graph, setGraph] = useState<CodeMapGraph | null>(null);
  const [detail, setDetail] = useState<FunctionDetail | null>(null);
  const [source, setSource] = useState<SourceResponse | null>(null);

  const [buildOpen, setBuildOpen] = useState(false);
  const [building, setBuilding] = useState(false);
  // Whether the active release already has source stored in the DB — when true
  // the build uses it directly instead of prompting for a folder.
  const [relHasSource, setRelHasSource] = useState(false);
  // Whether a code mind map exists for the active model/release (drives the
  // availability badge so the user can see what AI grounding is ready).
  const [mindmapOn, setMindmapOn] = useState(false);
  // #6: .o enrichment dataset for the active release — drives the Objects
  // badge and the verified-edge rendering after a rebuild.
  const [activeRelId, setActiveRelId] = useState<number | null>(null);
  const [objSummary, setObjSummary] = useState<{
    objects: number;
    edges: number;
    inlined: number;
    failed: number;
  } | null>(null);
  const [objPickerOpen, setObjPickerOpen] = useState(false);

  const [graphOpts, setGraphOpts] = useState<GraphOptions>(loadGraphOptions);
  const [optsOpen, setOptsOpen] = useState(false);
  const updateGraphOpts = useCallback((o: GraphOptions) => {
    setGraphOpts(o);
    saveGraphOptions(o);
  }, []);
  const closeOpts = useCallback(() => setOptsOpen(false), []);

  // Pane widths: function list (px) and call graph (px, null = default split).
  const [panes, setPanes] = useState(loadPaneLayout);
  const panesRef = useRef(panes);
  panesRef.current = panes;
  const sidebarRef = useRef<HTMLElement>(null);
  const centerRef = useRef<HTMLDivElement>(null);
  const graphPanelRef = useRef<HTMLDivElement>(null);
  const savePanes = useCallback(() => {
    const v = JSON.stringify(panesRef.current);
    try {
      localStorage.setItem(LAYOUT_KEY, v);
    } catch {
      /* ignore */
    }
    persistKey(LAYOUT_KEY, v);
  }, []);
  const resizeSidebar = (w: number) =>
    setPanes((p) => ({
      ...p,
      sidebar: Math.round(Math.min(Math.max(w, 160), window.innerWidth * 0.45)),
    }));
  const resizeGraph = (w: number) => {
    const total = centerRef.current?.clientWidth ?? 0;
    setPanes((p) => ({
      ...p,
      // Keep both the graph and the source pane usable.
      graph: Math.round(Math.min(Math.max(w, 240), Math.max(240, total - 300))),
    }));
  };

  const theme = useMonacoTheme();

  // Function-name set, read by the Monaco link provider / navigation (kept in a
  // ref so the editor providers — registered once — always see the latest list).
  const functionSetRef = useRef<Set<string>>(new Set());
  const navigateRef = useRef<(name: string) => void>(() => {});
  // Monaco disposables (link provider, opener, listeners) torn down on unmount,
  // and a recompute hook so the highlight decorations refresh when the source or
  // function set changes (the model content may be identical across switches).
  const disposablesRef = useRef<{ dispose(): void }[]>([]);
  const recomputeHighlightsRef = useRef<() => void>(() => {});

  const loadList = useCallback(async () => {
    setListState("loading");
    try {
      const cm = await api.get<CodeMapResponse>("/codemap");
      functionSetRef.current = new Set(cm.functions);
      setFunctions(cm.functions);
      setFocused((prev) =>
        prev && cm.functions.includes(prev)
          ? prev
          : cm.functions.includes("main")
            ? "main"
            : (cm.functions[0] ?? null),
      );
      setListState("ready");
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setListState("needs-build");
      } else {
        setListError((e as Error).message);
        setListState("error");
      }
    }
  }, []);

  // Track whether the active release already has source stored in the DB so the
  // build flow can index it directly (no folder prompt). Stays in sync with the
  // Release Manager's Import/Drop Source actions via the db-changed SSE below.
  const refreshSourceStatus = useCallback(async () => {
    try {
      const [releases, mm] = await Promise.all([
        api.get<ReleasesResponse>("/releases"),
        api.get<MindmapStatus>("/ai/mindmap").catch(() => null),
      ]);
      const rel = releases.releases.find(
        (r) => r.id === releases.active_release_id,
      );
      setRelHasSource(Boolean(rel?.has_source));
      setMindmapOn(Boolean(mm?.has_mindmap));
      setActiveRelId(rel?.id ?? null);
      if (rel?.id != null) {
        const oi = await api
          .get<{ summary: { objects: number; edges: number; inlined: number; failed: number } }>(
            `/releases/${rel.id}/objects`,
          )
          .catch(() => null);
        setObjSummary(oi && oi.summary.objects > 0 ? oi.summary : null);
      } else {
        setObjSummary(null);
      }
    } catch {
      /* leave previous value on a transient failure */
    }
  }, []);

  useEffect(() => {
    loadList();
    refreshSourceStatus();
  }, [loadList, refreshSourceStatus]);

  // Honor a "Show in Code Map" jump from the Workspace: focus the requested
  // function and clear the search box, then tell the parent it's consumed.
  useEffect(() => {
    if (!focusTarget) return;
    setSearch("");
    setFocused(focusTarget);
    onFocusConsumed?.();
  }, [focusTarget, onFocusConsumed]);

  // Fetch the graph whenever the focus or the graph view options change.
  const { callers, callees, params, back, fwd } = graphOpts;
  useEffect(() => {
    if (listState !== "ready" || !focused) return;
    let alive = true;
    const q = new URLSearchParams({
      fn: focused,
      back: String(callers ? back : 0),
      fwd: String(callees ? fwd : 0),
      params: String(params),
    });
    api
      .get<CodeMapGraph>(`/codemap/graph?${q}`)
      .then((g) => alive && setGraph(g))
      .catch((e) => alive && toast(`Code Map: ${(e as Error).message}`));
    return () => {
      alive = false;
    };
  }, [focused, listState, callers, callees, params, back, fwd, toast]);

  // Fetch details + source whenever the focused function changes.
  useEffect(() => {
    if (listState !== "ready" || !focused) return;
    let alive = true;
    const fn = encodeURIComponent(focused);
    (async () => {
      try {
        const [d, s] = await Promise.all([
          api.get<FunctionDetail>(`/codemap/function/${fn}`),
          api.get<SourceResponse>(`/source/function/${fn}`),
        ]);
        if (!alive) return;
        setDetail(d);
        setSource(s);
      } catch (e) {
        if (alive) toast(`Code Map: ${(e as Error).message}`);
      }
    })();
    return () => {
      alive = false;
    };
  }, [focused, listState, toast]);

  const navigate = useCallback(
    (name: string) => {
      if (functionSetRef.current.has(name)) setFocused(name);
    },
    [],
  );
  navigateRef.current = navigate;

  // Refresh the navigable-function highlight decorations when the loaded source
  // or the known-function set changes (content can be identical across switches,
  // so an onDidChangeModelContent listener alone would miss some updates).
  useEffect(() => {
    recomputeHighlightsRef.current();
  }, [source, functions]);

  // Tear down all Monaco registrations when the view unmounts (e.g. tab switch),
  // so we don't leak duplicate "c" link providers across remounts.
  useEffect(() => {
    const bag = disposablesRef.current;
    return () => {
      bag.forEach((d) => d.dispose());
      bag.length = 0;
    };
  }, []);

  // Reload the map when a build_code_map job finishes, and keep the release's
  // source status fresh when source is imported/dropped (Release Manager) or a
  // build auto-imports it.
  useSSE(
    useCallback(
      (e) => {
        if (e.event === "db-changed") {
          refreshSourceStatus();
          return;
        }
        if (e.event !== "job" || e.data?.kind !== "build_code_map") return;
        const st = e.data.status;
        if (st === "done") {
          setBuilding(false);
          refreshSourceStatus();
          loadList();
        } else if (st === "failed" || st === "cancelled") {
          setBuilding(false);
        }
      },
      [loadList, refreshSourceStatus],
    ),
  );

  async function startBuild(sourceDir: string) {
    setBuildOpen(false);
    try {
      const [models, releases] = await Promise.all([
        api.get<ModelsResponse>("/models"),
        api.get<ReleasesResponse>("/releases"),
      ]);
      const relId = releases.active_release_id;
      const rel = releases.releases.find((r) => r.id === relId);
      await api.post("/jobs/build_code_map", {
        model_id: models.active_model_id,
        release_id: relId,
        elf_hash: rel?.elf_hash ?? null,
        elf_path: rel?.elf_path ?? null,
        source_dir: sourceDir,
      });
      setBuilding(true);
      toast("Building Code Map…");
    } catch (e) {
      toast(`Build failed to start: ${(e as Error).message}`);
    }
  }

  async function importObjects(dir: string) {
    setObjPickerOpen(false);
    if (activeRelId == null) {
      toast("No active release to import objects for.");
      return;
    }
    try {
      await api.post(`/releases/${activeRelId}/objects`, { obj_dir: dir });
      toast(
        "Importing object files… rebuild the Code Map afterwards to see " +
          "verified call edges.",
      );
    } catch (e) {
      toast(`Object import failed to start: ${(e as Error).message}`);
    }
  }

  const onEditorMount: OnMount = (editor, monaco) => {
    editor.updateOptions({ readOnly: true });
    // Guard against mounting inside a still-collapsing flex container: Monaco
    // caches the container size at mount and `automaticLayout` can miss a late
    // first sizing, leaving the editor stuck at its 5×5 minimum. Re-measure on
    // the next frames so it fills the pane once layout settles.
    requestAnimationFrame(() => editor.layout());
    setTimeout(() => editor.layout(), 100);

    // Single pass over the file's identifiers, keeping only those that name a
    // known function. Shared by the link provider (native Ctrl/Cmd-hover
    // underline + click) and the always-on highlight decorations — O(file)
    // instead of O(functions × file) re-scans.
    const collect = (model: ReturnType<typeof editor.getModel>) => {
      const out: { fn: string; range: InstanceType<typeof monaco.Range> }[] = [];
      if (!model) return out;
      const fns = functionSetRef.current;
      if (fns.size === 0) return out;
      const text = model.getValue();
      const re = /[A-Za-z_]\w*/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(text)) !== null) {
        if (!fns.has(m[0])) continue;
        const s = model.getPositionAt(m.index);
        const e = model.getPositionAt(m.index + m[0].length);
        out.push({
          fn: m[0],
          range: new monaco.Range(s.lineNumber, s.column, e.lineNumber, e.column),
        });
      }
      return out;
    };

    // Native links: Monaco underlines these and shows a pointer when Ctrl/Cmd is
    // held, and opens them on Ctrl/Cmd+click — exactly the navigation gesture.
    const NAV_SCHEME = "codemap";
    const linkProvider = monaco.languages.registerLinkProvider("c", {
      provideLinks(model: import("monaco-editor").editor.ITextModel) {
        return {
          links: collect(model).map(({ fn, range }) => ({
            range,
            url: monaco.Uri.from({ scheme: NAV_SCHEME, path: "/" + encodeURIComponent(fn) }),
            tooltip: `Ctrl/Cmd + Click to navigate to ${fn}`,
          })),
        };
      },
    });

    // Route our codemap: links back into the React focus state.
    const linkOpener = monaco.editor.registerLinkOpener({
      open(resource: import("monaco-editor").Uri) {
        if (resource.scheme !== NAV_SCHEME) return false;
        navigateRef.current(decodeURIComponent(resource.path.replace(/^\//, "")));
        return true;
      },
    });

    // Always-on rich highlight + discoverability hover for navigable calls.
    const decorations = editor.createDecorationsCollection();
    const recompute = () => {
      const model = editor.getModel();
      decorations.set(
        collect(model).map(({ fn, range }) => ({
          range,
          options: {
            inlineClassName: "cm-navigable-fn-token",
            hoverMessage: { value: `**${fn}**\n\nCtrl/Cmd + Click to navigate` },
            stickiness:
              monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges,
          },
        })),
      );
    };
    recomputeHighlightsRef.current = recompute;
    const contentSub = editor.onDidChangeModelContent(() => recompute());
    recompute();

    disposablesRef.current.push(linkProvider, linkOpener, contentSub, {
      dispose: () => decorations.clear(),
    });
  };

  // ---- Render branches -----------------------------------------------------
  if (listState === "loading") {
    return (
      <div className="center-msg">
        <span className="spin" /> Loading Code Map…
      </div>
    );
  }

  if (listState === "error") {
    return (
      <div className="center-msg" style={{ color: "var(--red)" }}>
        Failed to load Code Map: {listError}
      </div>
    );
  }

  if (listState === "needs-build") {
    return (
      <>
        <div className="cm-empty">
          <div className="cm-empty-card">
            <div className="cm-empty-title">No Code Map for this release</div>
            <p className="cm-empty-sub">
              {relHasSource
                ? "Source is imported for this release — build the code map to " +
                  "explore the call graph and browse each function."
                : "Build a code map to explore the call graph and browse the C " +
                  "source for each function. The source folder you choose is " +
                  "imported and saved with this release."}
            </p>
            <button
              className="save-btn"
              disabled={building}
              onClick={() => (relHasSource ? startBuild("") : setBuildOpen(true))}
            >
              {building
                ? "Building…"
                : relHasSource
                  ? "Build Code Map"
                  : "Choose Source & Build"}
            </button>
            {relHasSource && (
              <button
                className="link-btn"
                disabled={building}
                onClick={() => setBuildOpen(true)}
              >
                Use a different source folder…
              </button>
            )}
          </div>
        </div>
        {buildOpen && (
          <FolderPicker
            mode="folder"
            title="Build Code Map — choose the C source folder"
            hint="Navigate to the folder containing the C source"
            onCancel={() => setBuildOpen(false)}
            onConfirm={(path) => startBuild(path)}
          />
        )}
      </>
    );
  }

  // ready
  const q = search.trim().toLowerCase();
  const shown = q
    ? functions.filter((f) => f.toLowerCase().includes(q))
    : functions;

  return (
    <div className="cm-root">
      <aside className="cm-sidebar" ref={sidebarRef} style={{ flexBasis: panes.sidebar }}>
        <div className="cm-status">
          <span
            className={"cm-stat-badge " + (relHasSource ? "on" : "off")}
            title={
              relHasSource
                ? "C/H source is imported for the active release"
                : "No source imported for the active release"
            }
          >
            <span className="cm-stat-dot" /> Source
          </span>
          <span
            className={"cm-stat-badge " + (mindmapOn ? "on" : "off")}
            title={
              mindmapOn
                ? "A code mind map is available for AI grounding"
                : "No mind map yet — build one in AI Generation"
            }
          >
            <span className="cm-stat-dot" /> Mind Map
          </span>
          <span
            className={"cm-stat-badge " + (objSummary ? "on" : "off")}
            role="button"
            style={{ cursor: "pointer" }}
            title={
              objSummary
                ? `${objSummary.objects} object file(s): ${objSummary.edges} ` +
                  `compiler-verified edges, ${objSummary.inlined} inlined ` +
                  `function(s)` +
                  (objSummary.failed ? `, ${objSummary.failed} failed` : "") +
                  " — click to re-import from a build-output folder"
                : "No .o files imported for the active release — click to " +
                  "import a build-output folder (verifies call edges and " +
                  "recovers optimized-away functions)"
            }
            onClick={() => setObjPickerOpen(true)}
          >
            <span className="cm-stat-dot" /> Objects
          </span>
        </div>
        <div className="cm-search">
          <input
            type="text"
            placeholder={`Search ${functions.length} functions…`}
            spellCheck={false}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="cm-fnlist">
          {shown.length === 0 ? (
            <div className="cm-det-empty">No matches</div>
          ) : (
            shown.map((f) => (
              <button
                key={f}
                className={"cm-fn" + (f === focused ? " sel" : "")}
                title={f}
                onClick={() => setFocused(f)}
              >
                {f}
              </button>
            ))
          )}
        </div>
      </aside>
      <Splitter
        start={() => sidebarRef.current?.offsetWidth ?? panes.sidebar}
        onResize={resizeSidebar}
        onDone={savePanes}
        onReset={() => {
          setPanes((p) => ({ ...p, sidebar: SIDEBAR_W }));
          setTimeout(savePanes);
        }}
      />

      <div className="cm-center" ref={centerRef}>
        <div
          className="cm-graph-panel"
          ref={graphPanelRef}
          style={panes.graph != null ? { flex: `0 1 ${panes.graph}px` } : undefined}
        >
          <div className="cm-panel-head">
            Call Graph
            <div className="cm-legend">
              {callers && (
                <span className="cm-leg caller">
                  <i /> Callers
                </span>
              )}
              <span className="cm-leg center">
                <i /> Focus
              </span>
              {callees && (
                <span className="cm-leg callee">
                  <i /> Called
                </span>
              )}
              {params && (
                <span
                  className="cm-leg param"
                  title="Variables the focus reads (R, above) or writes (W, below)"
                >
                  <i /> Parameters
                </span>
              )}
              {graph?.edge_verification && (
                <>
                  <span
                    className="cm-leg verified"
                    title="Edge confirmed by the imported object files"
                  >
                    <i /> Verified
                  </span>
                  <span
                    className="cm-leg unverified"
                    title="Edge seen in source only — not confirmed by the object files"
                  >
                    <i /> Source-only
                  </span>
                </>
              )}
            </div>
            <button
              className={"cm-opts-btn" + (optsOpen ? " on" : "")}
              title="Call graph view options"
              aria-expanded={optsOpen}
              onClick={() => setOptsOpen((o) => !o)}
            >
              <SlidersIcon />
            </button>
          </div>
          {optsOpen && (
            <GraphOptionsPopover
              opts={graphOpts}
              onChange={updateGraphOpts}
              onClose={closeOpts}
            />
          )}
          <CallGraph graph={graph} focus={focused} onFocus={setFocused} />
        </div>
        <Splitter
          start={() => graphPanelRef.current?.offsetWidth ?? 400}
          onResize={resizeGraph}
          onDone={savePanes}
          onReset={() => {
            setPanes((p) => ({ ...p, graph: null }));
            setTimeout(savePanes);
          }}
        />

        <div className="cm-ide-panel">
          <div className="cm-panel-head">
            {focused ?? "—"}
            {detail?.file && (
              <span className="cm-panel-sub">{detail.file}</span>
            )}
          </div>
          <div className="cm-editor">
            <Editor
              language="c"
              theme={theme}
              value={source?.found ? source.source : ""}
              onMount={onEditorMount}
              options={{
                readOnly: true,
                domReadOnly: true,
                minimap: { enabled: false },
                fontSize: 12,
                lineNumbers: "on",
                scrollBeyondLastLine: false,
                renderLineHighlight: "none",
                automaticLayout: true,
              }}
            />
            {source && !source.found && (
              <div className="cm-nosource">
                {source.reason ?? "No source available for this function."}
              </div>
            )}
          </div>
          <DetailsCard detail={detail} source={source} onFocus={setFocused} />
        </div>
      </div>
      {objPickerOpen && (
        <FolderPicker
          mode="folder"
          title="Import object files (.o) — choose the build-output folder"
          hint="Navigate to the folder containing the compiled .o files"
          onCancel={() => setObjPickerOpen(false)}
          onConfirm={(path) => importObjects(path)}
        />
      )}
    </div>
  );
}
