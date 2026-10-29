import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { MainFunctionsInfo } from "../api/types";

// The main function(s) of an architecture model — e.g. the module's cyclic
// rlsAppl(). A call-tree test passes a port when one of them reaches the
// port's function through any number of calls. Asked on the first Run (or
// when none is in the release's call tree), editable from the ƒ button.
export function MainFunctionsDialog({
  modelId,
  modelName,
  colName,
  runAfter,
  onCancel,
  onSaved,
}: {
  modelId: number;
  modelName: string;
  colName: string;
  runAfter: boolean; // opened by ▶: saving also starts the test
  onCancel: () => void;
  onSaved: (functions: string[]) => void;
}) {
  const [info, setInfo] = useState<MainFunctionsInfo | null>(null);
  const [names, setNames] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .get<MainFunctionsInfo>(
        `/models/${modelId}/main-functions?col_name=${encodeURIComponent(colName)}`,
      )
      .then((r) => {
        setInfo(r);
        setNames(r.functions);
        // The input is disabled until the call tree is known: focus it now.
        setTimeout(() => searchRef.current?.focus(), 0);
      })
      .catch((e) => setErr((e as Error).message));
  }, [modelId, colName]);

  // Autocomplete from the release's call tree.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits([]);
      return;
    }
    const t = setTimeout(() => {
      api
        .get<{ functions: string[] }>(
          `/calltree-tests/functions?col_name=${encodeURIComponent(colName)}&q=${encodeURIComponent(q)}&limit=12`,
        )
        .then((r) => setHits(r.functions))
        .catch(() => setHits([]));
    }, 150);
    return () => clearTimeout(t);
  }, [query, colName]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  function add(name: string) {
    const n = name.trim();
    if (n && !names.includes(n)) setNames((ns) => [...ns, n]);
    setQuery("");
    setHits([]);
  }

  // Names typed by hand are checked against the call tree on save (via the
  // autocomplete list or the GET's found/missing split).
  const known = new Set([...(info?.found ?? []), ...hits, ...(info?.suggestions ?? []).map((s) => s.name)]);
  const missing = new Set(info?.missing ?? []);

  async function save() {
    setErr(null);
    setBusy(true);
    try {
      await api.put(`/models/${modelId}/main-functions`, { functions: names });
      const r = await api.get<MainFunctionsInfo>(
        `/models/${modelId}/main-functions?col_name=${encodeURIComponent(colName)}`,
      );
      setInfo(r);
      if (r.needs_input) {
        setErr(
          `None of these is in ${r.release_name}'s call tree — pick a function from the list.`,
        );
        return;
      }
      onSaved(r.functions);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onMouseDown={onCancel}>
      <div className="modal symfiles mainfn" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">Main function — {modelName}</div>
        <div className="symfiles-body">
          <p className="symfiles-hint">
            The function the module starts from, e.g. its cyclic <code>xxxAppl()</code>. The
            test passes a port when a main function reaches the port's function, directly or
            through any number of calls; otherwise it fails. Add more than one if the module
            has several entries (init, cyclic, interrupt). Saved for this model and used in
            every release.
          </p>
          {info && !info.has_code_map && (
            <div className="symfiles-err">
              No Code Map for {info.release_name} — build it first to pick from its call tree.
            </div>
          )}

          <div className="mainfn-list">
            {names.length === 0 && <span className="mainfn-empty">No main function set.</span>}
            {names.map((n) => (
              <span
                key={n}
                className={`mainfn-chip${missing.has(n) && !known.has(n) ? " bad" : ""}`}
                title={
                  missing.has(n) && !known.has(n)
                    ? `Not in ${info?.release_name}'s call tree`
                    : undefined
                }
              >
                <code>{n}</code>
                <button
                  title="Remove"
                  disabled={busy}
                  onClick={() => setNames((ns) => ns.filter((x) => x !== n))}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>

          <div className="symfiles-add mainfn-search">
            <input
              placeholder="Search the call tree, e.g. rlsAppl"
              ref={searchRef}
              value={query}
              disabled={busy || !info?.has_code_map}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && hits.length) add(hits[0]);
              }}
            />
            {hits.length > 0 && (
              <ul className="mainfn-hits">
                {hits.map((h) => (
                  <li key={h} onMouseDown={() => add(h)}>
                    <code>{h}</code>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {(info?.suggestions.length ?? 0) > 0 && (
            <>
              <div className="mainfn-sub">Suggested from the call tree</div>
              <table className="rm-table mainfn-sugg">
                <tbody>
                  {info!.suggestions.map((s) => (
                    <tr key={s.name}>
                      <td>
                        <code>{s.name}</code>
                      </td>
                      <td className="mainfn-cov">
                        reaches {s.covers} of {s.total} port function{s.total === 1 ? "" : "s"}
                      </td>
                      <td className="rm-actions-col">
                        <button disabled={busy || names.includes(s.name)} onClick={() => add(s.name)}>
                          {names.includes(s.name) ? "Added" : "Add"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {err && <div className="symfiles-err">{err}</div>}
        </div>
        <div className="mm-foot">
          <div className="spacer" />
          <button disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button className="primary" disabled={busy || names.length === 0} onClick={save}>
            {runAfter ? "Save & Run" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
