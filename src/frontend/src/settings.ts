// UI preferences that aren't theme colours: toolbar icon labels and which file
// browser to use. Persisted in localStorage; changes broadcast a window event so
// any subscribed component re-renders live (see useSetting).

import { useEffect, useState } from "react";
import { persistKey, removePersistedKey } from "./prefs";

const ICON_LABELS_KEY = "arch.ui.iconLabels";
const FILE_EXPLORER_KEY = "arch.ui.fileExplorer";
const CHANGED_EVENT = "arch-settings-changed";

export type FileExplorerMode = "custom" | "system";

function emit() {
  window.dispatchEvent(new Event(CHANGED_EVENT));
}

// Toolbar icons: show text labels under the glyphs, or icons only. Default on
// (labels) — more discoverable, which is what issue 5 asks for.
export function getIconLabels(): boolean {
  return localStorage.getItem(ICON_LABELS_KEY) !== "0";
}
export function setIconLabels(v: boolean): void {
  const blob = v ? "1" : "0";
  localStorage.setItem(ICON_LABELS_KEY, blob);
  persistKey(ICON_LABELS_KEY, blob);
  emit();
}

// "custom" → the in-app Finder-style FolderPicker. "system" → the OS-native file
// dialog (desktop build only; falls back to custom in a plain browser).
export function getFileExplorer(): FileExplorerMode {
  return localStorage.getItem(FILE_EXPLORER_KEY) === "system" ? "system" : "custom";
}
export function setFileExplorer(mode: FileExplorerMode): void {
  localStorage.setItem(FILE_EXPLORER_KEY, mode);
  persistKey(FILE_EXPLORER_KEY, mode);
  emit();
}

// Subscribe a component to a setting so it re-renders when the value changes
// anywhere (e.g. the Preferences dialog flips it).
export function useSetting<T>(read: () => T): T {
  const [value, setValue] = useState<T>(read);
  useEffect(() => {
    const handler = () => setValue(read());
    window.addEventListener(CHANGED_EVENT, handler);
    return () => window.removeEventListener(CHANGED_EVENT, handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return value;
}


// Operator details for the call-tree HTML report header (Preferences →
// Reports): [{label, value}], e.g. Operator / Email / Team. The backend reads
// the same key from the prefs store when it writes a report.
export const REPORT_OPERATOR_KEY = "arch.report.operator";

export interface OperatorField {
  label: string;
  value: string;
}

export function getReportOperator(): OperatorField[] {
  try {
    const raw = JSON.parse(localStorage.getItem(REPORT_OPERATOR_KEY) ?? "[]");
    return Array.isArray(raw)
      ? raw
          .filter((f) => f && typeof f === "object")
          .map((f) => ({ label: String(f.label ?? ""), value: String(f.value ?? "") }))
      : [];
  } catch {
    return [];
  }
}

export function setReportOperator(fields: OperatorField[]): void {
  const blob = JSON.stringify(fields);
  localStorage.setItem(REPORT_OPERATOR_KEY, blob);
  persistKey(REPORT_OPERATOR_KEY, blob);
  emit();
}

// Report logo (Preferences → Reports): an image data URI shown in the top-right
// corner of every call-tree HTML report. The backend stores it (it reads the
// chosen image file, POST /prefs/report-logo); this is the in-session cache.
export const REPORT_LOGO_KEY = "arch.report.logo";

export function getReportLogo(): string {
  return localStorage.getItem(REPORT_LOGO_KEY) ?? "";
}

export function cacheReportLogo(dataUri: string): void {
  try {
    localStorage.setItem(REPORT_LOGO_KEY, dataUri);
  } catch {
    /* quota — the backend copy is what reports use */
  }
  emit();
}

export function clearReportLogo(): void {
  localStorage.removeItem(REPORT_LOGO_KEY);
  removePersistedKey(REPORT_LOGO_KEY);
  emit();
}
