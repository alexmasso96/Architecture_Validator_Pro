"""
AI chat tools for Target Tests: the agent drives the ECU through the target
bridge, tries test scripts and saves what works.

The agent works in a feedback loop — set a breakpoint, run, and when the
function isn't reached read the code around the call, find the condition
(resolving macros from the source), change the variable, run again — and
then writes the steps down as a test script. What it saves is always a
**draft** (a new test) or a **proposal** (a change to an existing test); the
user accepts, approves or rejects it in the Target Tests view.

Guardrails: there are no flash, erase or option-byte tools; every run is
time-limited; memory writes need the user's "Allow memory writes" switch;
every target action is written to an audit log.
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from . import Logic_Target_Tests as tt
from .Logic_AI_Providers import Tool
from .Logic_WinIdea_Bridge import TargetBackend, TargetError

TARGET_TOOL_NAMES = (
    "target_check_connection", "target_reset", "target_set_breakpoint",
    "target_clear_breakpoints", "target_run_until_stop", "target_step", "target_status",
    "target_call_stack", "target_read", "target_write", "target_try_test",
    "list_target_tests", "get_target_test", "save_target_test",
    "propose_target_test_change",
)


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


def target_tools() -> List[Tool]:
    s, i, n = {"type": "string"}, {"type": "integer"}, {"type": "number"}
    return [
        Tool("target_check_connection", "Check the debugger connection: winIDEA, SDK, "
             "target and memory access.", _obj({})),
        Tool("target_reset", "Reset the target (CPU reset; the program starts again).",
             _obj({})),
        Tool("target_set_breakpoint", "Set a breakpoint on a function, or on file + line "
             "(e.g. location 'speed.c', line 123).",
             _obj({"location": s, "line": i}, ["location"])),
        Tool("target_clear_breakpoints", "Remove all breakpoints.", _obj({})),
        Tool("target_run_until_stop", "Run until a breakpoint stops the target, at most "
             "timeout_s seconds (default 2, max 60). Says where it stopped, or that it "
             "timed out (the target is then stopped).", _obj({"timeout_s": n})),
        Tool("target_step", "Step one source line: kind 'over', 'into' or 'out'.",
             _obj({"kind": {"type": "string", "enum": ["over", "into", "out"]}})),
        Tool("target_status", "Is the target running or stopped, and where.", _obj({})),
        Tool("target_call_stack", "The call stack (outermost first) while stopped.",
             _obj({})),
        Tool("target_read", "Read a variable or expression on the target.",
             _obj({"expr": s}, ["expr"])),
        Tool("target_write", "Write a variable on the target (needs the user's 'Allow "
             "memory writes'). Only RAM variables can change; a macro or #if can't.",
             _obj({"expr": s, "value": s}, ["expr", "value"])),
        Tool("target_try_test", "Run a test script (the target_tests API) on the target "
             "now, from a reset, and get its verdict, steps and expectations. Nothing is "
             "saved. Use it to check a script before saving it.",
             _obj({"code": s}, ["code"])),
        Tool("list_target_tests", "List the project's target tests with their status and "
             "last verdict.", _obj({})),
        Tool("get_target_test", "Get a target test's script, status and last results.",
             _obj({"id": i}, ["id"])),
        Tool("save_target_test", "Save a NEW test script as a draft for the user to review "
             "and approve. rationale: why the steps are needed (conditions, macro values).",
             _obj({"code": s, "rationale": s}, ["code", "rationale"])),
        Tool("propose_target_test_change", "Propose a change to an existing test. The user "
             "sees it as a diff, accepts or rejects it, then approves it separately.",
             _obj({"id": i, "code": s, "rationale": s}, ["id", "code", "rationale"])),
    ]


SYSTEM_PROMPT = """# TARGET TESTS — LIVE ECU
You can control the real ECU through a debugger (winIDEA) with the target_* tools,
and write architecture test cases that replay without you.

How to work:
1. Read the code first (get_function, get_call_graph, read_file, search_code).
2. Set a breakpoint where the behaviour should happen and run with a timeout.
3. If it isn't reached, find out why: break at the condition, read the variables,
   resolve macros from the source (#define), and write the variable that changes
   the branch. Run again. Check the call stack and values.
4. If the condition can't change at runtime (#if, a constant the compiler folded,
   code missing from the binary), say "needs a rebuild with …" — don't work around it.
5. Write what worked as a test script, check it with target_try_test, then save it
   with save_target_test (new) or propose_target_test_change (existing), with a
   rationale that explains the condition and where each value comes from.

The runner resets the target before every test; a script doesn't call reset.
Never leave the target running without a timeout. Don't guess values: read them.
Base your answer on what the ECU actually did and say what you observed.

Script format (only this API, literals and simple if/for; no imports or while):
```python
from target_tests import test, target, expect

@test(id="TT-0042", title="…", port="…", requirement="…")
def tt_0042():
    target.break_at("speed.c", 123)
    target.run_until_stop(timeout_s=2)
    target.write("a", 5)
    target.break_at("Com_SendSignal")
    target.run_until_stop(timeout_s=2)
    expect.stopped_at("Com_SendSignal")
    expect.call_stack(["main", "Os_Task10ms", "Speed_Main", "Com_SendSignal"])
```
target: {target}
expect: {expect}
"""


def system_prompt(selected: Optional[dict] = None, allow_writes: bool = False) -> str:
    text = SYSTEM_PROMPT.format(
        target="; ".join(tt.TARGET_API.values()), expect="; ".join(tt.EXPECT_API.values()))
    text += ("\nMemory writes are " + ("ALLOWED." if allow_writes else
             "NOT allowed yet: if you need one, ask the user to switch on "
             "'Allow memory writes'.") + "\n")
    if selected:
        text += (f"\n# SELECTED TEST (id {selected['id']}, {tt.status_of(selected)})\n"
                 f"```python\n{selected.get('code', '')}\n```\n")
    return text


class TargetToolExecutor:
    """Runs the target tools and hands everything else to the code tools."""

    def __init__(self, base, get_backend: Callable[[], TargetBackend], db=None,
                 user: str = "", allow_writes: bool = False, can_edit: bool = True,
                 release_id=None, model_id=None,
                 on_change: Optional[Callable[[Optional[int]], None]] = None,
                 audit: Optional[Callable[[dict], None]] = None):
        self.base = base
        self.get_backend = get_backend
        self.db = db
        self.user = user
        self.allow_writes = allow_writes
        self.can_edit = can_edit
        self.release_id = release_id
        self.model_id = model_id
        self.on_change = on_change or (lambda _id: None)
        self.audit = audit or (lambda _e: None)

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        if name not in TARGET_TOOL_NAMES:
            if self.base is None:
                return "ERROR: no code tools in this chat."
            return self.base.execute(name, args)
        try:
            out = getattr(self, "_" + name)(args)
        except (TargetError, tt.ApprovalError) as e:
            out = f"ERROR: {e}"
        if name.startswith("target_"):
            self.audit({"tool": name, "args": args, "result": out[:300]})
        return out

    # -- target -----------------------------------------------------------
    def _b(self) -> TargetBackend:
        return self.get_backend()

    def _target_check_connection(self, a):
        checks = self._b().checks()
        return "\n".join(f"{'OK  ' if c['ok'] else ('--  ' if c['ok'] is None else 'FAIL')} "
                         f"{c['name']}: {c.get('detail', '')}" for c in checks)

    def _target_reset(self, a):
        b = self._b()
        b.clear_breakpoints()
        b.reset()
        return "Target reset; breakpoints cleared."

    def _target_set_breakpoint(self, a):
        line = a.get("line")
        self._b().set_breakpoint(str(a.get("location", "")),
                                 int(line) if line not in (None, "") else None)
        return f"Breakpoint set at {a.get('location')}" + (f":{line}" if line else "") + "."

    def _target_clear_breakpoints(self, a):
        self._b().clear_breakpoints()
        return "Breakpoints cleared."

    def _where(self, b) -> str:
        st = b.status()
        if st["state"] != "stopped":
            return "running"
        loc = st.get("function") or "?"
        if st.get("file"):
            loc += f" ({st['file']}:{st.get('line')})"
        return f"stopped in {loc}"

    def _target_run_until_stop(self, a):
        b = self._b()
        timeout = min(float(a.get("timeout_s") or 2), tt.MAX_RUN_TIMEOUT_S)
        b.run()
        if not b.wait_until_stopped(timeout):
            b.stop()
            return (f"No breakpoint was hit within {timeout:g} s; the target was stopped "
                    f"({self._where(b)}).")
        return "Hit a breakpoint: " + self._where(b) + "."

    def _target_step(self, a):
        b = self._b()
        b.step(a.get("kind") or "over")
        return self._where(b)

    def _target_status(self, a):
        return self._where(self._b())

    def _target_call_stack(self, a):
        return " -> ".join(self._b().call_stack())

    def _target_read(self, a):
        expr = str(a.get("expr", ""))
        return f"{expr} = {self._b().read(expr)}"

    def _target_write(self, a):
        if not self.allow_writes:
            return ("ERROR: memory writes are off. Ask the user to switch on 'Allow memory "
                    "writes' in the Target Tests chat.")
        expr, value = str(a.get("expr", "")), a.get("value")
        b = self._b()
        b.write(expr, value)
        return f"{expr} = {b.read(expr)} (written)"

    def _target_try_test(self, a):
        code = str(a.get("code", ""))
        if not self.allow_writes and "target.write" in code:
            return ("ERROR: the script writes memory and memory writes are off. Ask the user "
                    "to switch on 'Allow memory writes'.")
        r = tt.execute(code, self._b())
        return json.dumps({"verdict": r["verdict"], "message": r["message"],
                           "expectations": r["expectations"],
                           "steps": r["steps"][-30:]}, default=str)

    # -- tests in the project ------------------------------------------
    def _require_db(self):
        if self.db is None:
            raise tt.ApprovalError("No project is open.")
        return self.db

    def _list_target_tests(self, a):
        db = self._require_db()
        last = db.latest_target_test_results()
        rows = []
        for t in db.list_target_tests():
            r = last.get(t["id"])
            rows.append(f"{t['id']}: {t['test_key']} — {t['title']} [{tt.status_of(t)}]"
                        + (f" last: {r['verdict']}" if r else "")
                        + (" (has a proposal)" if t["pending_code"] else ""))
        return "\n".join(rows) or "(no target tests yet)"

    def _get_target_test(self, a):
        db = self._require_db()
        t = db.get_target_test(int(a.get("id", 0)))
        if t is None:
            return "ERROR: no such test."
        results = db.list_target_test_results(t["id"], limit=3)
        return json.dumps({"id": t["id"], "status": tt.status_of(t), "code": t["code"],
                           "rationale": t["rationale"],
                           "pending_change": t["pending_code"] or None,
                           "last_results": [{"verdict": r["verdict"], "message": r["message"],
                                             "at": r["started_at"]} for r in results]})

    def _check_saveable(self, code: str):
        if not self.can_edit:
            raise tt.ApprovalError("The project is open read-only; the test can't be saved.")
        issues = tt.validate(code)
        if issues:
            raise tt.ApprovalError("The script isn't allowed: " + "; ".join(
                f"line {i['line']}: {i['message']}" for i in issues))

    def _save_target_test(self, a):
        db = self._require_db()
        code = str(a.get("code", ""))
        self._check_saveable(code)
        tid = tt.Approvals(db, self.user).create(code, tt.ORIGIN_AI,
                                                 rationale=str(a.get("rationale", "")),
                                                 release_id=self.release_id,
                                                 model_id=self.model_id)
        self.on_change(tid)
        return f"Saved as draft test {tid}. The user reviews and approves it."

    def _propose_target_test_change(self, a):
        db = self._require_db()
        code = str(a.get("code", ""))
        self._check_saveable(code)
        tid = int(a.get("id", 0))
        tt.Approvals(db, self.user).propose(tid, code, str(a.get("rationale", "")))
        self.on_change(tid)
        return (f"Proposed a change to test {tid}. The user sees the diff, accepts or rejects "
                "it, and approves it separately.")
