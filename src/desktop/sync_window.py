"""
The Sync Worker's window (#16): the shared projects list and what changed in each.

This is where project changes are tracked, so they don't clutter the app or its
ASPICE history. It's a separate short-lived process, because macOS gives the main
thread to the tray icon. Everything it needs is in the worker's folder and the
local copies, so it works with the app closed.
"""
from __future__ import annotations

import os
import socket
from typing import Optional

from Application_Logic import Logic_Sync_Autostart as autostart
from Application_Logic import Logic_Sync_Engine as engine
from Application_Logic.Logic_File_Locking import FileLockManager
from Application_Logic.Logic_Sync_Worker import (
    SyncStore, lock_summary, read_change_log, read_json, request_sync_now, worker_running,
    write_json,
)

WINDOW_PID = "window.pid"


class SyncWindowApi:
    """Called from the page. Keeps a project's problems as text for the page."""

    def __init__(self, store: SyncStore, login_command: list) -> None:
        self._store = store
        self._login_command = login_command
        self._window = None

    def overview(self) -> dict:
        return {
            "worker_running": worker_running(self._store),
            "start_at_login": autostart.is_enabled(),
            "projects": [{"project_uuid": e["project_uuid"], "name": e["name"],
                          "remote": e["remote"], "status": self._store.status(e),
                          "lock": lock_summary(self._store, e)}
                         for e in self._store.tracked()],
        }

    def changes(self, project_uuid: str, limit: int = 500) -> list:
        entry = next((e for e in self._store.tracked()
                      if e["project_uuid"] == project_uuid), None)
        return read_change_log(entry["local"], limit) if entry else []

    def add_path(self, path: str) -> Optional[str]:
        """Returns the problem as text, or None if the project was added."""
        try:
            self._store.add(path, FileLockManager.get_username())
            request_sync_now(self._store)
            return None
        except (engine.SyncError, OSError) as e:
            return str(e)

    def add_database(self) -> Optional[str]:
        import webview
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, file_types=("Project files (*.arch)",))
        return self.add_path(picked[0]) if picked else None

    def stop_tracking(self, project_uuid: str) -> None:
        self._store.remove(project_uuid)

    def sync_now(self) -> None:
        request_sync_now(self._store)

    def set_start_at_login(self, enabled: bool) -> bool:
        if enabled:
            autostart.enable(self._login_command)
        else:
            autostart.disable()
        return autostart.is_enabled()


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Sync Worker</title>
<style>
:root{--bg:#fff;--panel:#f5f5f7;--text:#1d1d1f;--muted:#6e6e73;--line:#d9d9de;--accent:#0a66e8;--warn:#b35c00}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--panel:#2c2c2e;--text:#f5f5f7;--muted:#a1a1a6;--line:#3a3a3c;--accent:#4a90e2;--warn:#e6961e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px -apple-system,"Segoe UI",system-ui,sans-serif;display:grid;grid-template-columns:280px 1fr;height:100vh}
aside{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-height:0}
main{display:flex;flex-direction:column;min-width:0;min-height:0}
header{padding:12px 14px;font-weight:600;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
.grow{flex:1 1 auto}
#list{flex:1 1 auto;overflow:auto}
.project{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.project.sel{background:var(--bg)}
.name{font-weight:600;overflow-wrap:anywhere}
.small{font-size:11px;color:var(--muted);overflow-wrap:anywhere;margin-top:2px}
.warn{color:var(--warn)}
footer{padding:10px 14px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
button,select,input[type=search]{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
button{cursor:pointer}
#scroll{flex:1 1 auto;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{position:sticky;top:0;background:var(--bg);text-align:left;font-weight:500;color:var(--muted);padding:6px 14px;border-bottom:1px solid var(--line)}
td{padding:5px 14px;border-bottom:1px solid var(--line)}
.empty{padding:24px 14px;color:var(--muted)}
</style></head><body>
<aside><header>Shared projects</header><div id="list"></div>
<footer><button id="add">Add database…</button><span class="grow"></span>
<label class="small"><input type="checkbox" id="login"> Start at login</label></footer></aside>
<main><header><span id="title" class="grow">Changes</span>
<select id="filter" aria-label="Show changes from"><option value="">All sections</option></select>
<button id="now">Sync now</button><button id="stop">Stop tracking</button></header>
<div id="scroll"></div></main>
<script>
let selected=null,rows=[];
const api=()=>window.pywebview.api;
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const STATE={synced:"In sync",pulling:"Receiving changes",pushing:"Publishing",offline:"Offline",conflict:"Conflict",blocked:"Sync stopped"};
function where(r){return r.release_name?"Release "+r.release_name:r.model_id!=null?"Model "+r.model_id:"Project"}
function renderRows(){
  const box=document.getElementById("scroll");box.textContent="";
  const f=document.getElementById("filter").value;
  const shown=rows.filter(r=>!f||r.section===f);
  if(!shown.length){box.append(el("div","empty",selected?"Nothing has been published yet.":"Choose a project."));return}
  const t=el("table"),h=el("tr");["When","User","Where","Section","Rows","Revision"].forEach(c=>h.append(el("th",null,c)));
  const head=el("thead");head.append(h);t.append(head);const b=el("tbody");
  shown.forEach(r=>{const tr=el("tr");[r.timestamp.replace("T"," ").slice(0,19),r.username,where(r),r.section,r.rows_changed,r.revision].forEach(v=>tr.append(el("td",null,v)));b.append(tr)});
  t.append(b);box.append(t);
}
async function loadChanges(){
  rows=selected?await api().changes(selected):[];
  const s=document.getElementById("filter"),keep=s.value;
  s.length=1;[...new Set(rows.map(r=>r.section))].sort().forEach(n=>s.add(new Option(n,n)));
  s.value=keep;renderRows();
}
async function refresh(){
  const o=await api().overview();
  document.getElementById("login").checked=o.start_at_login;
  const list=document.getElementById("list");list.textContent="";
  if(selected&&!o.projects.some(p=>p.project_uuid===selected))selected=null;
  if(!selected&&o.projects.length)selected=o.projects[0].project_uuid;
  if(!o.projects.length)list.append(el("div","empty","No shared projects are tracked."));
  o.projects.forEach(p=>{
    const d=el("div","project"+(p.project_uuid===selected?" sel":""));
    d.append(el("div","name",p.name),el("div","small",p.remote));
    const st=p.status||{},bad=["offline","conflict","blocked"].includes(st.state);
    d.append(el("div","small"+(bad?" warn":""),(STATE[st.state]||"Waiting for the worker")+(st.last_synced?" · "+st.last_synced:"")));
    if(st.message)d.append(el("div","small"+(bad?" warn":""),st.message));
    if(p.lock){const m=Math.floor(p.lock.silent_seconds/60);d.append(el("div","small","Edited by "+p.lock.user+(m>=10?", silent for "+m+" min":"")))}
    d.onclick=()=>{selected=p.project_uuid;refresh()};
    list.append(d);
  });
  const cur=o.projects.find(p=>p.project_uuid===selected);
  document.getElementById("title").textContent=cur?"Changes in "+cur.name:"Changes";
  document.getElementById("stop").disabled=!cur;
  document.getElementById("now").disabled=!cur;
  await loadChanges();
}
function start(){
  document.getElementById("add").onclick=async()=>{const e=await api().add_database();if(e)alert(e);refresh()};
  document.getElementById("now").onclick=async()=>{await api().sync_now();setTimeout(refresh,3000)};
  document.getElementById("stop").onclick=async()=>{await api().stop_tracking(selected);selected=null;refresh()};
  document.getElementById("login").onchange=async e=>{e.target.checked=await api().set_start_at_login(e.target.checked)};
  document.getElementById("filter").onchange=renderRows;
  refresh();setInterval(refresh,5000);
}
if(window.pywebview)start();else window.addEventListener("pywebviewready",start);
</script></body></html>"""


def main() -> None:
    import webview

    from desktop.sync_worker import command, sync_store

    store = sync_store()
    pid_path = os.path.join(store.root, WINDOW_PID)
    running = read_json(pid_path)
    if running and running != os.getpid() and FileLockManager.is_process_alive(
            running, socket.gethostname()):
        return
    write_json(pid_path, os.getpid())
    api = SyncWindowApi(store, command())
    api._window = webview.create_window("Sync Worker", html=PAGE, js_api=api,
                                        width=900, height=560)
    webview.start()
