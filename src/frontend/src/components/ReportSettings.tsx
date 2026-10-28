import { useState } from "react";
import { api, ApiError } from "../api/client";
import {
  cacheReportLogo,
  clearReportLogo,
  getReportLogo,
  getReportOperator,
  setReportOperator,
  type OperatorField,
} from "../settings";
import { FolderPicker } from "./FolderPicker";

// Suggested rows when nothing is saved yet — all editable / removable.
const STARTER: OperatorField[] = [
  { label: "Operator", value: "" },
  { label: "Email", value: "" },
  { label: "Team", value: "" },
];
const MAX_FIELDS = 12;
const LOGO_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"];

// Preferences → Reports. The logo and operator details printed in the header
// of every call-tree HTML report (operator instead of the computer's user
// account name). Stored per user (app-data prefs store), not in the project.
export function ReportSettings() {
  const [fields, setFields] = useState<OperatorField[]>(() => {
    const saved = getReportOperator();
    return saved.length ? saved : STARTER;
  });
  const [note, setNote] = useState<string | null>(null);
  const [logo, setLogo] = useState(getReportLogo);
  const [logoNote, setLogoNote] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  async function chooseLogo(path: string) {
    setLogoNote(null);
    try {
      const r = await api.post<{ value: string }>("/prefs/report-logo", { path });
      cacheReportLogo(r.value);
      setLogo(r.value);
      setLogoNote("Saved. New reports show this logo.");
    } catch (e) {
      setLogoNote(e instanceof ApiError ? e.detail : String(e));
    }
  }

  function update(i: number, patch: Partial<OperatorField>) {
    setNote(null);
    setFields((fs) => fs.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  }

  function save() {
    const kept = fields
      .map((f) => ({ label: f.label.trim(), value: f.value.trim() }))
      .filter((f) => f.label || f.value);
    setReportOperator(kept);
    setFields(kept.length ? kept : STARTER);
    setNote(
      kept.some((f) => f.value)
        ? "Saved. New reports show these details."
        : "Saved. With no details filled in, reports show the computer's user name.",
    );
  }

  return (
    <div className="prefs-body">
      <div className="prefs-field">
        <div className="prefs-label">Logo</div>
        <div className="prefs-hint">
          Shown in the top-right corner of every call-tree HTML report, e.g. your
          company or team logo. PNG, JPEG, GIF, WebP or SVG, up to 1 MB. The image is
          embedded in the report, so the file stays self-contained.
        </div>
        <div className="prefs-row logo-row">
          <div className="logo-preview">
            {logo ? <img src={logo} alt="Report logo" /> : <span>No logo</span>}
          </div>
          <button className="prefs-prefill" onClick={() => setPicking(true)}>
            {logo ? "Change…" : "Choose image…"}
          </button>
          {logo && (
            <button
              className="prefs-prefill"
              onClick={() => {
                clearReportLogo();
                setLogo("");
                setLogoNote("Removed. New reports have no logo.");
              }}
            >
              Remove
            </button>
          )}
        </div>
        {logoNote && <div className="update-detail">{logoNote}</div>}
      </div>
      <div className="prefs-field">
        <div className="prefs-label">Operator details</div>
        <div className="prefs-hint">
          Shown at the top of every call-tree HTML report, in place of the computer's
          user name. Add whatever identifies you, e.g. your real name, email or team.
          Fields left empty are not printed. Stored for your user on this computer,
          not in the project.
        </div>
        <div className="op-fields">
          {fields.map((f, i) => (
            <div className="prefs-row op-field" key={i}>
              <input
                className="prefs-input op-label"
                placeholder="Field (e.g. Email)"
                value={f.label}
                maxLength={200}
                onChange={(e) => update(i, { label: e.target.value })}
              />
              <input
                className="prefs-input"
                placeholder="Value"
                value={f.value}
                maxLength={200}
                onChange={(e) => update(i, { value: e.target.value })}
              />
              <button
                className="prefs-prefill"
                title="Remove this field"
                onClick={() => {
                  setNote(null);
                  setFields((fs) => fs.filter((_, j) => j !== i));
                }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
        <div className="prefs-row">
          <button
            className="prefs-prefill"
            disabled={fields.length >= MAX_FIELDS}
            onClick={() => {
              setNote(null);
              setFields((fs) => [...fs, { label: "", value: "" }]);
            }}
          >
            ＋ Add field
          </button>
          <button className="save-btn" onClick={save}>
            Save
          </button>
        </div>
        {note && <div className="update-detail">{note}</div>}
      </div>
      {picking && (
        <FolderPicker
          mode="import"
          exts={LOGO_EXTS}
          title="Choose a report logo"
          hint="Select a PNG, JPEG, GIF, WebP or SVG image (up to 1 MB)"
          onCancel={() => setPicking(false)}
          onConfirm={(path) => {
            setPicking(false);
            chooseLogo(path);
          }}
        />
      )}
    </div>
  );
}
