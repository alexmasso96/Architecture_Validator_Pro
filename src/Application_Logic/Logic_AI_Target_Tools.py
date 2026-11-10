"""
AI chat tools for Target Tests: the agent drives the ECU through the target
bridge, tries test scripts and saves what works.

The agent works in a feedback loop — set a breakpoint, run, and when the
function isn't reached read the code around the call, find the condition
(resolving macros from the source), change the variable, run again — and
then writes the steps down as a test script. What it saves is always a
**draft** (a new test) or a **proposal** (a change to an existing test); the
user accepts, approves or rejects it in the Target Tests view. A new test goes
into the test suite of the architecture model it checks.

Guardrails: every run is time-limited; memory writes and function calls need
the user's "Allow memory writes" switch, and flashing its own "Allow flashing"
switch; there are no erase or option-byte tools; every target action is
written to an audit log.
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from . import Logic_Target_Tests as tt
from .Logic_AI_Providers import Tool
from .Logic_WinIdea_Bridge import TargetBackend, TargetError

TARGET_TOOL_NAMES = (
    "target_check_connection", "target_reset", "target_set_breakpoint",
    "target_set_data_breakpoint", "target_clear_breakpoints", "target_run_until_stop",
    "target_step", "target_status", "target_call_stack", "target_read",
    "target_symbol_info", "target_read_register", "target_read_memory",
    "target_verify_image", "target_write", "target_call_function", "target_flash",
    "target_try_test",
    "list_target_tests", "get_target_test", "save_target_test",
    "propose_target_test_change",
)


MAX_MEMORY_TOOL_BYTES = 256


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
        Tool("target_set_data_breakpoint", "Stop when a variable is accessed: access "
             "'write' (default), 'read' or 'any'. Run, then read the call stack to see "
             "who accessed it. The probe has only a few of these; clear them when done.",
             _obj({"variable": s, "access": {"type": "string",
                                              "enum": ["write", "read", "any"]}},
                  ["variable"])),
        Tool("target_clear_breakpoints", "Remove all breakpoints, data breakpoints too.",
             _obj({})),
        Tool("target_run_until_stop", "Run until a breakpoint stops the target, at most "
             "timeout_s seconds (default 2, max 60). Says where it stopped, or that it "
             "timed out (the target is then stopped).", _obj({"timeout_s": n})),
        Tool("target_step", "Step one source line: kind 'over', 'into' or 'out'.",
             _obj({"kind": {"type": "string", "enum": ["over", "into", "out"]}})),
        Tool("target_status", "Is the target running or stopped, and where.", _obj({})),
        Tool("target_call_stack", "The call stack (outermost first) while stopped.",
             _obj({})),
        Tool("target_read", "Read a variable or expression on the target. frame: 0 "
             "(default) is where it stopped; 1 reads the caller's locals and arguments, "
             "2 its caller's.", _obj({"expr": s, "frame": i}, ["expr"])),
        Tool("target_symbol_info", "Address, size and type of a variable or function in "
             "the program on the target. An error means the symbol isn't in it.",
             _obj({"name": s}, ["name"])),
        Tool("target_read_register", "Read a core register by name (e.g. PC, SP).",
             _obj({"name": s}, ["name"])),
        Tool("target_read_memory", "Read bytes at an address (max 256), as a hex dump. "
             "address: a number or '0x…'.", _obj({"address": s, "size": i},
                                                 ["address", "size"])),
        Tool("target_verify_image", "Check that the program on the target is the one "
             "winIDEA is set up to download. Slow: it reads the flash back.", _obj({})),
        Tool("target_write", "Write a variable on the target (needs the user's 'Allow "
             "memory writes'). Only RAM variables can change; a macro or #if can't.",
             _obj({"expr": s, "value": s}, ["expr", "value"])),
        Tool("target_call_function", "Run a function on the target with arguments of "
             "simple types and get its return value (needs the user's 'Allow memory "
             "writes'). The target must be stopped after its start-up code.",
             _obj({"function": s, "args": {"type": "array", "items": s}}, ["function"])),
        Tool("target_flash", "Program the target with the files winIDEA is set up to "
             "download, and leave it at reset (needs the user's 'Allow flashing'). Only "
             "when the user asks for it.", _obj({})),
        Tool("target_try_test", "Run a test script (the target_tests API) on the target "
             "now, from a reset, and get its verdict, steps and expectations. Nothing is "
             "saved. Use it to check a script before saving it.",
             _obj({"code": s}, ["code"])),
        Tool("list_target_tests", "List the project's target tests by test suite (one "
             "suite per architecture model) with their status and last verdict.", _obj({})),
        Tool("get_target_test", "Get a target test's script, status and last results.",
             _obj({"id": i}, ["id"])),
        Tool("save_target_test", "Save a NEW test script as a draft for the user to review "
             "and approve. model: the architecture model whose port or function the test "
             "checks; the test goes into that model's test suite. rationale: why the steps "
             "are needed (conditions, macro values).",
             _obj({"code": s, "model": s, "rationale": s}, ["code", "model", "rationale"])),
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
   For a variable or parameter port, set a data breakpoint on the variable
   (target_set_data_breakpoint) and run: the call stack then shows who reads or
   writes it. target_symbol_info tells whether a symbol is in the program at all.
4. If the condition can't change at runtime (#if, a constant the compiler folded,
   code missing from the binary), say "needs a rebuild with …" — don't work around it.
5. Write what worked as a test script, check it with target_try_test, then save it
   with save_target_test (new) or propose_target_test_change (existing), with a
   rationale that explains the condition and where each value comes from.

Test suites: the tests are grouped into one test suite per architecture model.
Every new test names its model (save_target_test's `model`): the model that owns
the port or function under test, not the one the call happens to pass through.
Write one test per behaviour; when asked to test a whole model, save each test
into that model's suite.

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


# A project can have hundreds of models; the prompt names this many.
MAX_SUITES_IN_PROMPT = 150


def system_prompt(selected: Optional[dict] = None, allow_writes: bool = False,
                  suites: Optional[List[str]] = None, current: str = "",
                  allow_flash: bool = False) -> str:
    """``suites``: the architecture models' names; ``current``: the model the
    user is working on."""
    text = SYSTEM_PROMPT.format(
        target="; ".join(tt.TARGET_API.values()), expect="; ".join(tt.EXPECT_API.values()))
    if suites:
        more = len(suites) - MAX_SUITES_IN_PROMPT
        text += ("\n# TEST SUITES (architecture models)\n"
                 + ", ".join(suites[:MAX_SUITES_IN_PROMPT])
                 + (f", and {more} more (list_target_tests names them all)" if more > 0 else "")
                 + "\n")
        if current:
            text += f"The user is working on: {current}.\n"
    text += ("\nMemory writes and function calls are " + ("ALLOWED." if allow_writes else
             "NOT allowed yet: if you need one, ask the user to switch on "
             "'Allow memory writes'.") + "\n")
    text += ("Flashing is " + ("ALLOWED, but only when the user asks for it." if allow_flash
             else "NOT allowed: if the target has to be programmed, ask the user to do "
             "it or to switch on 'Allow flashing'.") + "\n")
    if selected:
        text += (f"\n# SELECTED TEST (id {selected['id']}, {tt.status_of(selected)}"
                 + (f", suite {selected['suite']}" if selected.get("suite") else "") + ")\n"
                 f"```python\n{selected.get('code', '')}\n```\n")
    return text


class TargetToolExecutor:
    """Runs the target tools and hands everything else to the code tools."""

    def __init__(self, base, get_backend: Callable[[], TargetBackend], db=None,
                 user: str = "", allow_writes: bool = False, can_edit: bool = True,
                 allow_flash: bool = False,
                 release_id=None, model_id=None,
                 on_change: Optional[Callable[[Optional[int]], None]] = None,
                 audit: Optional[Callable[[dict], None]] = None):
        self.base = base
        self.get_backend = get_backend
        self.db = db
        self.user = user
        self.allow_writes = allow_writes
        self.allow_flash = allow_flash
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

    def _target_set_data_breakpoint(self, a):
        variable, access = str(a.get("variable", "")), str(a.get("access") or "write")
        self._b().set_data_breakpoint(variable, access)
        return f"Data breakpoint set: stops on {access} access to {variable}."

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
        expr, frame = str(a.get("expr", "")), int(a.get("frame") or 0)
        if frame:
            return f"{expr} (frame {frame}) = {self._b().read_frame(expr, frame)}"
        return f"{expr} = {self._b().read(expr)}"

    def _target_symbol_info(self, a):
        return json.dumps(self._b().symbol_info(str(a.get("name", ""))))

    def _target_read_register(self, a):
        name = str(a.get("name", ""))
        return f"{name} = {self._b().read_register(name)}"

    def _target_read_memory(self, a):
        try:
            address = int(str(a.get("address", "")), 0)
            size = max(1, min(int(a.get("size") or 16), MAX_MEMORY_TOOL_BYTES))
        except ValueError:
            return "ERROR: address and size must be numbers (address may be '0x…')."
        data = self._b().read_memory(address, size)
        return "\n".join(f"0x{address + i:08X}: {data[i:i + 16].hex(' ')}"
                         for i in range(0, len(data), 16))

    def _target_verify_image(self, a):
        return self._b().verify_image()

    def _target_call_function(self, a):
        if not self.allow_writes:
            return ("ERROR: calling a function changes the target and memory writes are "
                    "off. Ask the user to switch on 'Allow memory writes'.")
        name, args = str(a.get("function", "")), [str(x) for x in a.get("args") or []]
        return f"{name}({', '.join(args)}) returned: {self._b().call_function(name, args)}"

    def _target_flash(self, a):
        if not self.allow_flash:
            return ("ERROR: flashing is off. Ask the user to switch on 'Allow flashing' "
                    "in the Target Tests chat, or to flash the target themselves.")
        b = self._b()
        b.flash()
        return "The target was programmed with winIDEA's download files and is at reset."

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
        if not self.allow_writes and any(call in code for call in tt.WRITING_CALLS):
            return ("ERROR: the script changes the target (target.write or target.call) "
                    "and memory writes are off. Ask the user to switch on 'Allow memory "
                    "writes'.")
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
        tests = db.list_target_tests()
        suites = tt.suites(db, tests)
        known = {s["model_id"] for s in suites}
        rows = []
        for suite in suites:
            mine = [t for t in tests if (t["model_id"] if t["model_id"] in known else None)
                    == suite["model_id"]]
            name = suite["name"] or "(model removed)"
            rows.append(f"Suite {name}" + (" (model deleted)" if suite["deleted"] else "")
                        + f": {len(mine)} test{'' if len(mine) == 1 else 's'}")
            for t in mine:
                r = last.get(t["id"])
                rows.append(f"  {t['id']}: {t['test_key']} — {t['title']} [{tt.status_of(t)}]"
                            + (f" last: {r['verdict']}" if r else "")
                            + (" (has a proposal)" if t["pending_code"] else ""))
        return "\n".join(rows) or "(no target tests yet)"

    def _suite_name(self, model_id) -> str:
        return next((m["name"] for m in self._require_db().get_all_models()
                     if m["id"] == model_id), "")

    def _get_target_test(self, a):
        db = self._require_db()
        t = db.get_target_test(int(a.get("id", 0)))
        if t is None:
            return "ERROR: no such test."
        results = db.list_target_test_results(t["id"], limit=3)
        return json.dumps({"id": t["id"], "status": tt.status_of(t),
                           "suite": self._suite_name(t["model_id"]), "code": t["code"],
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
        model_id = tt.resolve_suite(db, str(a.get("model") or ""),
                                    tt.parse_meta(code).get("port") or "", self.model_id)
        if model_id is None:
            raise tt.ApprovalError(
                "Say which architecture model the test belongs to (model). The test "
                f"suites are: {', '.join(tt.suite_names(db)) or '(none)'}.")
        tid = tt.Approvals(db, self.user).create(code, tt.ORIGIN_AI,
                                                 rationale=str(a.get("rationale", "")),
                                                 release_id=self.release_id,
                                                 model_id=model_id)
        self.on_change(tid)
        return (f"Saved as draft test {tid} in the test suite {self._suite_name(model_id)}. "
                "The user reviews and approves it.")

    def _propose_target_test_change(self, a):
        db = self._require_db()
        code = str(a.get("code", ""))
        self._check_saveable(code)
        tid = int(a.get("id", 0))
        tt.Approvals(db, self.user).propose(tid, code, str(a.get("rationale", "")))
        self.on_change(tid)
        return (f"Proposed a change to test {tid}. The user sees the diff, accepts or rejects "
                "it, and approves it separately.")
