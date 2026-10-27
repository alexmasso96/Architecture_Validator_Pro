import { useEffect, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Cell, PortRow, TestColumnStatus } from "../api/types";
import {
  cellText,
  confClass,
  deriveResult,
  initCyclicValue,
  isConflict,
  isResultEditable,
  NO_RESULT,
  parseMatch,
  pillTone,
  PORT_STATE_CLASS,
  PORT_STATE_OPTIONS,
  RESULT_CLASS,
  RESULT_OPTIONS,
  REVIEW_CLASS,
  REVIEW_OPTIONS,
  scoreTone,
  isTestStale,
  TEST_CLASS,
  TEST_MANUAL_OPTIONS,
  TEST_NOT_RUN,
  type ResolvedColumn,
  type Tone,
} from "../columns";
import { Menu, type MenuItem } from "./Menu";

const ROW_H = 37;

// Roles whose cells are plain values (no dedicated picker) and therefore get
// inline free-text editing — ported from the old QTableWidgetItem ItemIsEditable
// behaviour. state/review/match keep their richer editors (pills, MatchPicker).
const TEXT_EDIT_ROLES: ReadonlyArray<string> = ["text", "tcid", "port", "init", "cyclic"];

interface EditTarget {
  rowIndex: number;
  colName: string;
  value: string;
  orig: string;
}

interface PillTarget {
  x: number;
  y: number;
  rowIndex: number;
  colName: string;
  options: string[];
  current: string;
  // Call-tree Test cell: the tool's own verdict (for "Reset to tool verdict").
  testAuto?: string;
}

// Hover text for a Test cell: verdict, why, callers (✓ = verified by .o files).
function testTooltip(cell: Cell | undefined, stale: boolean): string {
  if (!cell?.tested_at) return "Not run yet — use ▶ in the column header.";
  const lines: string[] = [];
  const auto = String(cell.auto ?? "");
  if (cell.user_changed) {
    lines.push(`Set manually to ${cell.text} by ${cell.override_by ?? "?"} ` +
               `(tool verdict: ${auto}).`);
  }
  if (cell.function) lines.push(`Function: ${cell.function}`);
  if (cell.reason) lines.push(String(cell.reason));
  const callers = (cell.callers as { name: string; verified: boolean }[] | undefined) ?? [];
  const total = Number(cell.callers_total ?? 0);
  if (callers.length) {
    lines.push("Callers: " + callers.map((c) => (c.verified ? "✓ " : "") + c.name).join(", ") +
               (total > callers.length ? ` … (+${total - callers.length} more)` : ""));
  }
  lines.push(`Tested ${cell.tested_at}${cell.strict ? " (strict: verified calls only)" : ""}`);
  if (stale) lines.push("⟳ Stale — the code map was rebuilt after this run. Run again.");
  return lines.join("\n");
}

export function PortsTable({
  columns,
  rows,
  selectedRowIndex,
  onSelectRow,
  onEditCell,
  onOpenMatch,
  onUnlockCell,
  canEdit,
  modelStatus,
  testStatus,
  onRunTest,
  onReportTest,
}: {
  columns: ResolvedColumn[];
  rows: PortRow[];
  selectedRowIndex: number | null;
  onSelectRow: (rowIndex: number) => void;
  onEditCell: (rowIndex: number, colName: string, value: string) => void;
  onOpenMatch: (rowIndex: number, col: ResolvedColumn, x: number, y: number) => void;
  onUnlockCell?: (rowIndex: number, colName: string) => void;
  canEdit: boolean;
  modelStatus: string;
  testStatus?: Record<string, TestColumnStatus>;
  onRunTest?: (colName: string) => void;
  onReportTest?: (colName: string) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pill, setPill] = useState<PillTarget | null>(null);
  const [edit, setEdit] = useState<EditTarget | null>(null);
  // #7: context menu for import-locked cells (explicit, change-logged unlock).
  const [unlockMenu, setUnlockMenu] = useState<{
    x: number;
    y: number;
    rowIndex: number;
    colName: string;
  } | null>(null);

  function startEdit(rowIndex: number, colName: string, current: string) {
    if (!canEdit) return;
    setEdit({ rowIndex, colName, value: current, orig: current });
  }

  function commitEdit() {
    setEdit((cur) => {
      if (cur && cur.value !== cur.orig) {
        onEditCell(cur.rowIndex, cur.colName, cur.value);
      }
      return null;
    });
  }

  // Look up the Port State / Review Status columns by role — they drive the
  // per-row styling cascades (retired/deleted rows, reviewed match tinting).
  const stateCol = columns.find((c) => c.role === "state");
  const reviewCol = columns.find((c) => c.role === "review");
  // The most-recently-created result column drives the read-only "Last Result".
  const resultCols = columns.filter((c) => c.role === "result");
  const lastResultCol = resultCols.length ? resultCols[resultCols.length - 1] : null;

  // A release-result value: recorded verdict wins, else derived from the row's
  // own Review/State + the model status (ASPICE gate — see deriveResult).
  function resultValue(row: PortRow, col: ResolvedColumn): string {
    const review = reviewCol ? cellText(row.cells[reviewCol.name]) : "";
    const portState = stateCol ? cellText(row.cells[stateCol.name]) : "";
    return deriveResult(modelStatus, portState, review, {
      baselined: col.resultBaselined,
      existing: cellText(row.cells[col.name]),
    });
  }

  // Per-column widths, seeded from the backend layout and adjusted by dragging
  // the header separators. Local-only (a feel-good UX nicety) — persisting back
  // via PUT /columns can come with the column customizer.
  const [widths, setWidths] = useState<Record<string, number>>({});
  useEffect(() => {
    setWidths((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const c of columns) {
        let targetW = c.width || 120;
        if (c.name === "TC. ID") targetW = 65;
        else if (c.name.endsWith(" (Init)")) targetW = 50;
        else if (c.name.endsWith(" (Cyclic)")) targetW = 55;
        else if (c.role === "review") targetW = 95;
        else if (c.role === "state") targetW = 85;

        // Force-override if the width is unset in state OR matches the database default
        if (next[c.name] == null || next[c.name] === c.width) {
          next[c.name] = targetW;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [columns]);
  const colWidth = (name: string, fallback: number) => widths[name] ?? fallback ?? 120;

  function startResize(e: React.MouseEvent, name: string) {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = colWidth(name, 120);
    const onMove = (ev: MouseEvent) => {
      const w = Math.max(56, startW + (ev.clientX - startX));
      setWidths((p) => ({ ...p, [name]: w }));
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_H,
    overscan: 16,
  });

  const items = virtualizer.getVirtualItems();
  const totalH = virtualizer.getTotalSize();
  const padTop = items.length ? items[0].start : 0;
  const padBottom = items.length ? totalH - items[items.length - 1].end : 0;

  function openPill(
    e: React.MouseEvent,
    rowIndex: number,
    col: ResolvedColumn,
    current: string,
    testAuto?: string,
  ) {
    if (!canEdit) return;
    e.stopPropagation();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setPill({
      x: r.left,
      y: r.bottom + 4,
      rowIndex,
      colName: col.name,
      options:
        col.role === "state"
          ? PORT_STATE_OPTIONS
          : col.role === "result"
            ? RESULT_OPTIONS
            : col.role === "calltest"
              ? TEST_MANUAL_OPTIONS
              : REVIEW_OPTIONS,
      current,
      testAuto: testAuto ?? undefined,
    });
  }

  function renderCell(row: PortRow, col: ResolvedColumn, reviewed: boolean) {
    const value = cellText(row.cells[col.name]);

    // #7: cells populated by an import are locked — no inline edit; the only
    // way back to editability is the explicit right-click unlock (audited).
    if (row.cells[col.name]?.origin === "import") {
      return (
        <span
          className="mono editcell locked-cell"
          title={
            "Populated by an import and locked." +
            (canEdit ? " Right-click to unlock (recorded in the change log)." : "")
          }
          onContextMenu={
            canEdit && onUnlockCell
              ? (e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setUnlockMenu({ x: e.clientX, y: e.clientY,
                                  rowIndex: row.row_index, colName: col.name });
                }
              : undefined
          }
        >
          <span className="lock-glyph">🔒</span>
          {value || <span className="dim">—</span>}
        </span>
      );
    }

    // Inline free-text editor: shown when this exact cell is being edited.
    if (
      edit &&
      edit.rowIndex === row.row_index &&
      edit.colName === col.name &&
      TEXT_EDIT_ROLES.includes(col.role)
    ) {
      return (
        <input
          className="cell-edit"
          autoFocus
          value={edit.value}
          onChange={(e) => setEdit({ ...edit, value: e.target.value })}
          onClick={(e) => e.stopPropagation()}
          onBlur={commitEdit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitEdit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              setEdit(null);
            }
          }}
        />
      );
    }

    if (col.role === "state" || col.role === "review") {
      const displayValue = value || (col.role === "state" ? "In Work" : "Not Reviewed");
      const cls =
        col.role === "state"
          ? PORT_STATE_CLASS[displayValue] ?? "p-grey"
          : REVIEW_CLASS[displayValue] ?? "p-grey";
      return (
        <button
          className={`cell-btn ${cls}${canEdit ? " editable" : ""}`}
          onClick={(e) => openPill(e, row.row_index, col, displayValue)}
        >
          {displayValue}
        </button>
      );
    }

    if (col.role === "result") {
      const v = resultValue(row, col);
      const cls = RESULT_CLASS[v] ?? "p-grey";
      const editable = canEdit && isResultEditable(col, v);
      return (
        <button
          className={`cell-btn ${cls}${editable ? " editable" : ""}`}
          disabled={!editable}
          onClick={editable ? (e) => openPill(e, row.row_index, col, v) : undefined}
          title={editable ? undefined : v === NO_RESULT ? "Not applicable" : "Locked — not the active release"}
        >
          {v}
        </button>
      );
    }

    if (col.role === "calltest") {
      const cell = row.cells[col.name];
      const v = cell?.tested_at ? value || TEST_NOT_RUN : TEST_NOT_RUN;
      const cls = TEST_CLASS[v] ?? "p-grey";
      const stale = isTestStale(cell, testStatus?.[col.name]?.code_map_built_at);
      const editable =
        canEdit && !!col.test?.is_active && !col.test?.is_baselined && !!cell?.tested_at;
      return (
        <button
          className={`cell-btn ${cls}${editable ? " editable" : ""}`}
          disabled={!editable}
          onClick={
            editable
              ? (e) => openPill(e, row.row_index, col, v, String(cell?.auto ?? ""))
              : undefined
          }
          title={testTooltip(cell, stale)}
        >
          {v}
          {cell?.user_changed && <span className="test-mark" title="Set manually"> ✎</span>}
          {stale && <span className="test-mark"> ⟳</span>}
        </button>
      );
    }

    if (col.role === "lastresult") {
      if (!lastResultCol) return <span className="dim">—</span>;
      const v = resultValue(row, lastResultCol);
      const cls = RESULT_CLASS[v] ?? "p-grey";
      return (
        <button className={`cell-btn ${cls}`} disabled title="Latest result (read-only)">
          {v}
        </button>
      );
    }

    if (col.role === "match") {
      const { name, score } = parseMatch(value);

      // B6: an empty cell renders the fallback em-dash and exposes no picker
      // trigger, even when the table is editable.
      if (!name) {
        return <span className="dim">—</span>;
      }

      // B5: reviewed rows tint the match green and drop the % score.
      const showScore = !reviewed && score !== null;
      const matchClass = reviewed ? " reviewed-match" : "";

      if (!canEdit) {
        return (
          <div className={"matchcell-readonly" + matchClass}>
            <span className="mono match-name">{name}</span>
            {showScore && <span className={confClass(score)}>{score}%</span>}
          </div>
        );
      }

      return (
        <button
          className={"matchcell" + matchClass}
          title="Pick match candidate"
          onClick={(e) => {
            e.stopPropagation();
            onSelectRow(row.row_index);
            const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
            onOpenMatch(row.row_index, col, r.left, r.bottom + 4);
          }}
        >
          <span className="mono match-name">{name}</span>
          {showScore && <span className={confClass(score)}>{score}%</span>}
        </button>
      );
    }

    if (col.role === "init" || col.role === "cyclic") {
      const { value: v, override } = initCyclicValue(col, row.cells);
      const onDbl = () => startEdit(row.row_index, col.name, v);
      if (!v) return <span className="dim editcell" onDoubleClick={onDbl}>—</span>;
      return (
        <span
          className={"mono initc editcell" + (override ? " override" : "")}
          onDoubleClick={onDbl}
        >
          {v}
        </span>
      );
    }

    if (col.role === "tcid" || col.role === "port") {
      // B6: fallback em-dash for empty non-status cells.
      const onDbl = () => startEdit(row.row_index, col.name, value);
      return value ? (
        <span className="mono editcell" onDoubleClick={onDbl}>{value}</span>
      ) : (
        <span className="dim editcell" onDoubleClick={onDbl}>—</span>
      );
    }

    // Generic free-text column.
    return (
      <span
        className="editcell"
        onDoubleClick={() => startEdit(row.row_index, col.name, value)}
      >
        {value || <span className="dim">—</span>}
      </span>
    );
  }

  // Subtle background gradient for coloured cells (#4) so values read easily.
  function cellTone(row: PortRow, col: ResolvedColumn): Tone {
    const value = cellText(row.cells[col.name]);
    if (col.role === "state") return value ? pillTone(PORT_STATE_CLASS[value] ?? "p-grey") : "";
    if (col.role === "review") return value ? pillTone(REVIEW_CLASS[value] ?? "p-grey") : "";
    if (col.role === "match") return scoreTone(parseMatch(value).score);
    return "";
  }

  const pillMenuItems: MenuItem[] = pill
    ? [
        ...pill.options.map((opt) => ({
          label: opt,
          checked: opt === pill.current,
          onClick: () => onEditCell(pill.rowIndex, pill.colName, opt),
        })),
        ...(pill.testAuto
          ? [
              { separator: true },
              {
                label: `Reset to tool verdict (${pill.testAuto})`,
                onClick: () => onEditCell(pill.rowIndex, pill.colName, ""),
              },
            ]
          : []),
      ]
    : [];

  return (
    <div className="tablewrap" ref={scrollRef}>
      <table className="ptable" style={{ tableLayout: "fixed" }}>
        <colgroup>
          {columns.map((c) => (
            <col key={c.name} style={{ width: colWidth(c.name, c.width) }} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.name}>
                {c.name}
                {c.role === "calltest" && onRunTest && canEdit &&
                  c.test?.is_active && !c.test?.is_baselined && (
                    <button
                      className="test-run-btn"
                      title={
                        testStatus?.[c.name]?.has_code_map === false
                          ? "No code map for this model and release — build it first"
                          : `Run call-tree test against ${c.test.release_name}'s code map`
                      }
                      onClick={(e) => {
                        e.stopPropagation();
                        onRunTest(c.name);
                      }}
                    >
                      ▶
                    </button>
                  )}
                {c.role === "calltest" && onReportTest && c.test?.is_active && (
                  <button
                    className="test-run-btn"
                    title={`Generate an HTML report of ${c.name} (all models)`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onReportTest(c.name);
                    }}
                  >
                    ⎙
                  </button>
                )}
                <span
                  className="col-resizer"
                  title="Drag to resize"
                  onMouseDown={(e) => startResize(e, c.name)}
                />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {padTop > 0 && <tr style={{ height: padTop }} />}
          {items.map((vi) => {
            const row = rows[vi.index];
            const selected = row.row_index === selectedRowIndex;
            const stateVal = stateCol ? cellText(row.cells[stateCol.name]) : "";
            const reviewed =
              !!reviewCol && cellText(row.cells[reviewCol.name]) === "Reviewed";
            const rowCls =
              "prow" +
              (selected ? " selected" : "") +
              (stateVal === "Retired" ? " retired" : "") +
              (stateVal === "Deleted" ? " deleted" : "");
            return (
              <tr
                key={row.row_index}
                className={rowCls}
                style={{ height: ROW_H }}
                onClick={() => onSelectRow(row.row_index)}
              >
                {columns.map((c) => {
                  const conflict = c.role === "match" && isConflict(row.cells[c.name]);
                  // B5: reviewed match cells read green regardless of score.
                  const tone =
                    conflict
                      ? ""
                      : reviewed && c.role === "match"
                        ? "ok"
                        : cellTone(row, c);
                  const isPillCol =
                    c.role === "state" || c.role === "review" ||
                    c.role === "result" || c.role === "lastresult";
                  const cls = [
                    conflict ? "conflict" : tone ? `tone-${tone}` : "",
                    isPillCol ? "cell-button-td" : "",
                  ].filter(Boolean).join(" ");
                  return (
                    <td key={c.name} className={cls || undefined}>
                      {renderCell(row, c, reviewed)}
                    </td>
                  );
                })}
              </tr>
            );
          })}
          {padBottom > 0 && <tr style={{ height: padBottom }} />}
        </tbody>
      </table>

      {pill && (
        <Menu
          x={pill.x}
          y={pill.y}
          items={pillMenuItems}
          onClose={() => setPill(null)}
        />
      )}
      {unlockMenu && (
        <Menu
          x={unlockMenu.x}
          y={unlockMenu.y}
          items={[
            {
              label: "Unlock imported cell…",
              danger: true,
              onClick: () =>
                onUnlockCell?.(unlockMenu.rowIndex, unlockMenu.colName),
            },
          ]}
          onClose={() => setUnlockMenu(null)}
        />
      )}
    </div>
  );
}
