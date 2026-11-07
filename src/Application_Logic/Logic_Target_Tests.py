"""
Target Tests — architecture tests that run on the real ECU.

A test case is a short Python script written against the ``target_tests`` API::

    from target_tests import test, target, expect

    @test(id="TT-0042", title="Speed reaches Com_SendSignal when a == B",
          port="SpeedProvider.Speed", requirement="REQ-123")
    def tt_0042():
        target.break_at("speed.c", 123)
        target.run_until_stop(timeout_s=2)
        target.write("a", 5)
        target.break_at("Com_SendSignal")
        target.run_until_stop(timeout_s=2)
        expect.stopped_at("Com_SendSignal")
        expect.call_stack(["main", "Os_Task10ms", "Speed_Main", "Com_SendSignal"])

The AI writes a test once (as a draft); the user reviews, corrects and
approves it; approved tests replay with no AI. This module holds:

* the **allow-list check** of a script (``validate``): only the API, literals,
  simple control flow — no imports, attributes, dunders or ``while``;
* the **runtime** (``execute``): runs a script against a ``TargetBackend``,
  recording every step and expectation as evidence, with an optional line hook
  that the debugger uses to pause;
* the **isolated runner** (``run_in_process``): runs a test in a child process
  that opens its own target connection, so a hung test can't freeze the app,
  and relays debug pauses and commands over a pipe;
* the **approval rules** (``Approvals``): AI changes are drafts, manual edits
  are approved, and accepting an AI proposal and approving it are two steps.

Pure module: the DB is passed in.
"""
from __future__ import annotations

import ast
import datetime as _dt
import multiprocessing as mp
import re
import sys
import time
import traceback
from typing import Callable, Dict, List, Optional

from .Logic_WinIdea_Bridge import (TargetBackend, TargetError, TargetUnavailable,
                                   make_backend, summarize_checks)

SCRIPT_NAME = "<target-test>"
MODULE_NAME = "target_tests"

STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
ORIGIN_AI = "ai"
ORIGIN_USER = "user"
ORIGIN_REVERT = "revert"

VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"
VERDICT_ERROR = "error"
VERDICT_STOPPED = "stopped"

DEFAULT_TEST_TIMEOUT_S = 120.0
MAX_RUN_TIMEOUT_S = 60.0

TARGET_API = {
    "break_at": "break_at(location, line=None) — breakpoint on a function, or on file + line",
    "clear_breakpoints": "clear_breakpoints() — remove every breakpoint the test set",
    "run_until_stop": "run_until_stop(timeout_s=2) — run; True if a breakpoint stopped "
                      "the target, False on timeout (the target is then stopped)",
    "run_to": "run_to(location, line=None, timeout_s=2) — break_at + run_until_stop",
    "step": "step(kind='over') — 'over', 'into' or 'out'",
    "read": "read(expr) — a variable's or expression's value, as text",
    "write": "write(expr, value) — set a variable",
    "call_stack": "call_stack() — function names, outermost first",
    "status": "status() — {'state', 'function', 'file', 'line'}",
    "wait": "wait(seconds) — let the target run (max 10 s)",
}
EXPECT_API = {
    "stopped_at": "stopped_at(function) — the target is stopped in this function",
    "call_stack": "call_stack(frames, exact=False) — these frames, in this order "
                  "(exact: nothing else)",
    "value": "value(expr, condition) — e.g. value('speed_out', '> 0') or value('a', 5)",
    "true": "true(condition, message='') — any condition computed in the script",
    "not_reached": "not_reached(location, line=None, timeout_s=2) — running doesn't "
                   "stop there within the timeout",
}
SAFE_CALLS = {"range", "len", "int", "float", "str", "abs", "min", "max", "round",
              "hex", "bool"}
_TEST_META = ("id", "title", "port", "requirement", "model")

_ALLOWED_NODES = (
    ast.Expr, ast.Assign, ast.AugAssign, ast.For, ast.If, ast.Pass, ast.Break,
    ast.Continue, ast.Call, ast.keyword, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.Slice, ast.Compare, ast.BoolOp,
    ast.BinOp, ast.UnaryOp, ast.IfExp, ast.JoinedStr, ast.FormattedValue, ast.Attribute,
    ast.And, ast.Or, ast.Not, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,
    ast.USub, ast.UAdd, ast.Invert, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt,
    ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ======================================================================
# Validation
# ======================================================================
def _issue(node, message) -> dict:
    return {"line": getattr(node, "lineno", 0), "message": message}


def validate(code: str) -> List[dict]:
    """Problems that stop a script from running; empty when it's allowed."""
    try:
        tree = ast.parse(code or "", filename=SCRIPT_NAME)
    except SyntaxError as e:
        return [{"line": e.lineno or 0, "message": f"Syntax error: {e.msg}"}]
    issues: List[dict] = []
    tests = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == MODULE_NAME and not node.level:
            bad = [a.name for a in node.names if a.name not in ("test", "target", "expect")
                   or a.asname]
            if bad:
                issues.append(_issue(node, f"Only test, target and expect can be imported "
                                           f"from {MODULE_NAME}."))
        elif isinstance(node, ast.FunctionDef):
            tests.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            pass  # a docstring
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            issues.append(_issue(node, f"Only 'from {MODULE_NAME} import test, target, "
                                       "expect' is allowed."))
        else:
            issues.append(_issue(node, "Only the import and one @test function may be at "
                                       "the top level."))
    if len(tests) != 1:
        issues.append({"line": 0, "message": "A script holds exactly one @test function."})
    for fn in tests:
        issues.extend(_check_function(fn))
    return issues


def _check_function(fn: ast.FunctionDef) -> List[dict]:
    issues = []
    decos = fn.decorator_list
    if len(decos) != 1 or not (isinstance(decos[0], ast.Call)
                               and isinstance(decos[0].func, ast.Name)
                               and decos[0].func.id == "test"):
        issues.append(_issue(fn, "The test function needs exactly one @test(...) decorator."))
    else:
        for kw in decos[0].keywords:
            if kw.arg not in _TEST_META:
                issues.append(_issue(kw, f"@test takes {', '.join(_TEST_META)}."))
            elif not isinstance(kw.value, ast.Constant):
                issues.append(_issue(kw, f"@test {kw.arg} must be a plain value."))
        if decos[0].args:
            issues.append(_issue(decos[0], "@test takes keyword arguments only."))
    a = fn.args
    if a.args or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg:
        issues.append(_issue(fn, "The test function takes no arguments."))
    for stmt in fn.body:
        for node in ast.walk(stmt):
            issues.extend(_check_node(node))
    return issues


def _check_node(node) -> List[dict]:
    if isinstance(node, ast.While):
        return [_issue(node, "'while' loops aren't allowed; use for … in range(n).")]
    if not isinstance(node, _ALLOWED_NODES):
        return [_issue(node, f"'{type(node).__name__}' isn't allowed in a test.")]
    if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in
                                       ("exec", "eval", "open", "compile", "globals",
                                        "locals", "vars", "getattr", "setattr",
                                        "__import__", "breakpoint", "input")):
        return [_issue(node, f"'{node.id}' isn't allowed in a test.")]
    if isinstance(node, ast.Attribute):
        if not (isinstance(node.value, ast.Name) and node.value.id in ("target", "expect")):
            return [_issue(node, "Only target.… and expect.… can be used with a dot.")]
        api = TARGET_API if node.value.id == "target" else EXPECT_API
        if node.attr not in api:
            return [_issue(node, f"{node.value.id}.{node.attr} doesn't exist. "
                                 f"Available: {', '.join(sorted(api))}.")]
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id not in SAFE_CALLS:
            return [_issue(node, f"Calling '{f.id}' isn't allowed.")]
        if not isinstance(f, (ast.Name, ast.Attribute)):
            return [_issue(node, "Only target.…, expect.… and simple built-ins can be called.")]
        if any(k.arg is None for k in node.keywords) or \
                any(isinstance(x, ast.Starred) for x in node.args):
            return [_issue(node, "* and ** arguments aren't allowed.")]
    for target in getattr(node, "targets", []) or ([node.target] if isinstance(
            node, (ast.AugAssign, ast.For)) else []):
        if not isinstance(target, (ast.Name, ast.Tuple)):
            return [_issue(node, "Only plain names can be assigned.")]
    return []


def parse_meta(code: str) -> dict:
    """The @test(...) keywords of a script ({} when it can't be read)."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "test":
                    meta = {"function": node.name}
                    for kw in d.keywords:
                        if kw.arg in _TEST_META and isinstance(kw.value, ast.Constant):
                            meta[kw.arg] = kw.value.value
                    return meta
    return {}


def referenced_functions(code: str) -> List[str]:
    """Function names a script uses: break_at/run_to targets and expected frames."""
    names: List[str] = []
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            args = node.args
            if attr in ("break_at", "run_to", "stopped_at", "not_reached") and args \
                    and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str) \
                    and not (len(args) > 1 or any(k.arg == "line" for k in node.keywords)):
                names.append(args[0].value)
            if attr == "call_stack" and args and isinstance(args[0], (ast.List, ast.Tuple)):
                names.extend(e.value for e in args[0].elts
                             if isinstance(e, ast.Constant) and isinstance(e.value, str))
    seen = set()
    return [n for n in names if not (n in seen or seen.add(n))]


# ======================================================================
# Runtime
# ======================================================================
class StopRequested(Exception):
    """The user pressed Stop."""


def _script_line() -> int:
    f = sys._getframe(1)
    while f is not None:
        if f.f_code.co_filename == SCRIPT_NAME:
            return f.f_lineno
        f = f.f_back
    return 0


def _compare(actual: str, condition) -> bool:
    """``condition`` is a value (equality) or text like '> 0', '!= 3', '== 0x10'."""
    if not isinstance(condition, str):
        return _num(actual) == condition if _num(actual) is not None else str(actual) == str(condition)
    m = re.match(r"^\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$", condition)
    if not m:
        a, b = _num(actual), _num(condition)
        return a == b if a is not None and b is not None else str(actual).strip() == condition.strip()
    op, want = m.groups()
    a, b = _num(actual), _num(want)
    if a is None or b is None:
        a, b = str(actual).strip(), want.strip().strip("'\"")
        if op not in ("==", "!="):
            return False
    return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b,
            ">": a > b, ">=": a >= b}[op]


def _num(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().split()[0] if str(v).strip() else ""
    try:
        return int(s, 0)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def _in_order(expected: List[str], actual: List[str]) -> bool:
    it = iter(actual)
    return all(any(a == e for a in it) for e in expected)


class Recorder:
    def __init__(self):
        self.steps: List[dict] = []
        self.expectations: List[dict] = []
        self.t0 = time.monotonic()

    def step(self, call: str, args, **extra) -> dict:
        s = {"line": _script_line(), "call": call, "args": args,
             "t_ms": int((time.monotonic() - self.t0) * 1000), **extra}
        self.steps.append(s)
        return s


class TargetAPI:
    """``target`` inside a script."""

    def __init__(self, backend: TargetBackend, rec: Recorder):
        self._b = backend
        self._r = rec

    def _do(self, call, args, fn):
        s = self._r.step(call, args)
        try:
            out = fn()
        except TargetError as e:
            s["error"] = str(e)
            raise
        if out is not None:
            s["result"] = out
        return out

    def break_at(self, location, line=None):
        args = [location] + ([line] if line is not None else [])
        return self._do("target.break_at", args,
                        lambda: self._b.set_breakpoint(str(location), line))

    def clear_breakpoints(self):
        return self._do("target.clear_breakpoints", [], self._b.clear_breakpoints)

    def run_until_stop(self, timeout_s=2.0):
        timeout_s = min(float(timeout_s), MAX_RUN_TIMEOUT_S)

        def go():
            self._b.run()
            hit = self._b.wait_until_stopped(timeout_s)
            if not hit:
                self._b.stop()
            st = self._b.status()
            return {"stopped": hit, "at": st.get("function"), "line": st.get("line")}
        out = self._do("target.run_until_stop", [timeout_s], go)
        return bool(out["stopped"])

    def run_to(self, location, line=None, timeout_s=2.0):
        self.break_at(location, line)
        return self.run_until_stop(timeout_s)

    def step(self, kind="over"):
        if kind not in ("over", "into", "out"):
            raise TargetError("step kind is 'over', 'into' or 'out'.")
        return self._do("target.step", [kind], lambda: self._b.step(kind))

    def read(self, expr):
        return self._do("target.read", [expr], lambda: self._b.read(str(expr)))

    def write(self, expr, value):
        return self._do("target.write", [expr, value],
                        lambda: self._b.write(str(expr), value))

    def call_stack(self):
        return self._do("target.call_stack", [], self._b.call_stack)

    def status(self):
        return self._do("target.status", [], self._b.status)

    def wait(self, seconds):
        seconds = max(0.0, min(float(seconds), 10.0))

        def go():
            self._b.run()
            if self._b.kind != "simulator":
                time.sleep(seconds)
            self._b.stop()
        return self._do("target.wait", [seconds], go)


class ExpectAPI:
    """``expect`` inside a script. A failed expectation doesn't stop the test."""

    def __init__(self, backend: TargetBackend, rec: Recorder):
        self._b = backend
        self._r = rec

    def _record(self, call, args, ok, actual, message=""):
        e = {"line": _script_line(), "call": call, "args": args, "ok": bool(ok),
             "actual": actual, "message": message}
        self._r.expectations.append(e)
        self._r.step(call, args, result={"ok": bool(ok), "actual": actual})
        return bool(ok)

    def stopped_at(self, function):
        st = self._b.status()
        ok = st.get("state") == "stopped" and st.get("function") == function
        actual = st.get("function") if st.get("state") == "stopped" else "running"
        return self._record("expect.stopped_at", [function], ok, actual)

    def call_stack(self, frames, exact=False):
        try:
            actual = self._b.call_stack()
        except TargetError as e:
            return self._record("expect.call_stack", [list(frames)], False, None, str(e))
        frames = [str(f) for f in frames]
        ok = actual == frames if exact else _in_order(frames, actual)
        return self._record("expect.call_stack", [frames], ok, actual)

    def value(self, expr, condition):
        try:
            actual = self._b.read(str(expr))
        except TargetError as e:
            return self._record("expect.value", [expr, condition], False, None, str(e))
        return self._record("expect.value", [expr, condition], _compare(actual, condition),
                            actual)

    def true(self, condition, message=""):
        return self._record("expect.true", [message], bool(condition), bool(condition),
                            message)

    def not_reached(self, location, line=None, timeout_s=2.0):
        self._b.set_breakpoint(str(location), line)
        self._b.run()
        hit = self._b.wait_until_stopped(min(float(timeout_s), MAX_RUN_TIMEOUT_S))
        if not hit:
            self._b.stop()
        st = self._b.status()
        reached = hit and (line is not None or st.get("function") == location)
        return self._record("expect.not_reached", [location] + ([line] if line else []),
                            not reached, st.get("function") if hit else "not reached")


class _Module:
    """What ``from target_tests import …`` returns inside a script."""

    def __init__(self, target, expect):
        self.target = target
        self.expect = expect
        self.tests: List[Callable] = []

    def test(self, **meta):
        def deco(fn):
            fn.meta = meta
            self.tests.append(fn)
            return fn
        return deco


_BUILTINS = {n: __builtins__[n] if isinstance(__builtins__, dict) else getattr(__builtins__, n)
             for n in SAFE_CALLS}


def execute(code: str, backend: TargetBackend,
            on_line: Optional[Callable[[int], None]] = None,
            reset: bool = True) -> dict:
    """Run one script against an open backend. Returns the result record:
    ``{verdict, message, steps, expectations, duration_ms}``.

    ``on_line(lineno)`` is called before each script line runs; it may block
    (a debug pause) or raise ``StopRequested``.
    """
    issues = validate(code)
    if issues:
        return {"verdict": VERDICT_ERROR, "steps": [], "expectations": [], "duration_ms": 0,
                "message": "; ".join(f"line {i['line']}: {i['message']}" for i in issues)}
    rec = Recorder()
    mod = _Module(TargetAPI(backend, rec), ExpectAPI(backend, rec))

    def _import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name != MODULE_NAME:
            raise ImportError(f"Only {MODULE_NAME} can be imported.")
        return mod

    env = {"__builtins__": {**_BUILTINS, "__import__": _import}, "__name__": "target_test"}
    verdict, message = VERDICT_ERROR, ""
    old_trace = sys.gettrace()
    try:
        exec(compile(code, SCRIPT_NAME, "exec"), env)  # noqa: S102 — allow-listed above
        if not mod.tests:
            raise TargetError("The script has no @test function.")
        if reset:
            backend.clear_breakpoints()
            backend.reset()
            rec.step("reset", [])

        if on_line is not None:
            def tracer(frame, event, arg):
                if frame.f_code.co_filename != SCRIPT_NAME:
                    return None
                if event == "line":
                    on_line(frame.f_lineno)
                return tracer
            sys.settrace(tracer)
        try:
            mod.tests[0]()
        finally:
            sys.settrace(old_trace)
        failed = [e for e in rec.expectations if not e["ok"]]
        if not rec.expectations:
            verdict, message = VERDICT_FAILED, "The test checks nothing (no expect.… call)."
        elif failed:
            verdict = VERDICT_FAILED
            message = "; ".join(
                f"line {e['line']}: {e['call']}({', '.join(map(repr, e['args']))}) "
                f"got {e['actual']!r}" + (f" — {e['message']}" if e["message"] else "")
                for e in failed)
        else:
            verdict, message = VERDICT_PASSED, f"{len(rec.expectations)} expectation(s) met."
    except StopRequested:
        verdict, message = VERDICT_STOPPED, "Stopped by the user."
    except TargetError as e:
        verdict, message = VERDICT_ERROR, f"line {_last_line(rec)}: {e}"
    except Exception as e:  # noqa: BLE001 — a script bug is a test error, not a crash
        tb = traceback.extract_tb(e.__traceback__)
        line = next((f.lineno for f in reversed(tb) if f.filename == SCRIPT_NAME), 0)
        verdict, message = VERDICT_ERROR, f"line {line}: {type(e).__name__}: {e}"
    finally:
        sys.settrace(old_trace)
        try:
            backend.clear_breakpoints()
        except TargetError:
            pass
    return {"verdict": verdict, "message": message, "steps": rec.steps,
            "expectations": rec.expectations,
            "duration_ms": int((time.monotonic() - rec.t0) * 1000)}


def _last_line(rec: Recorder) -> int:
    return rec.steps[-1]["line"] if rec.steps else 0


def snapshot(backend: TargetBackend, watches: List[str]) -> dict:
    """Target state for a debug pause: status, call stack and watched values."""
    out: dict = {}
    try:
        out["status"] = backend.status()
    except TargetError as e:
        out["status"] = {"state": "unknown", "error": str(e)}
    try:
        out["call_stack"] = backend.call_stack()
    except TargetError as e:
        out["call_stack"] = []
        out["call_stack_error"] = str(e)
    vals = {}
    for w in watches or []:
        try:
            vals[w] = backend.read(w)
        except TargetError as e:
            vals[w] = f"<{e}>"
    out["watches"] = vals
    try:
        out["registers"] = backend.registers()
    except TargetError:
        out["registers"] = {}
    return out


# ======================================================================
# Isolated runner (child process)
# ======================================================================
def _child_main(conn, config: dict, tests: List[dict], debug: Optional[dict]) -> None:
    """Child process: connect, check, then run each test (reset before each)."""
    backend = None
    try:
        backend = make_backend(config)
        try:
            checks = backend.checks()
        except TargetError as e:
            checks = [{"name": "Target", "ok": False, "detail": str(e)}]
        summary = summarize_checks(checks)
        conn.send({"type": "checks", **summary, "info": _safe_info(backend)})
        if not summary["ok"]:
            return
        state = {"breakpoints": set((debug or {}).get("breakpoints") or []),
                 "watches": list((debug or {}).get("watches") or []),
                 "stepping": bool((debug or {}).get("pause_at_start"))}

        def on_line(line: int):
            if conn.poll():
                _apply(conn.recv(), state)
            if not (state["stepping"] or line in state["breakpoints"]):
                return
            conn.send({"type": "paused", "line": line,
                       "snapshot": snapshot(backend, state["watches"])})
            while True:
                cmd = conn.recv()
                action = _apply(cmd, state)
                if action == "continue":
                    state["stepping"] = False
                    return
                if action == "step":
                    state["stepping"] = True
                    return
                if action == "snapshot":
                    conn.send({"type": "paused", "line": line,
                               "snapshot": snapshot(backend, state["watches"])})

        for t in tests:
            conn.send({"type": "started", "test_id": t.get("id")})
            result = execute(t["code"], backend, on_line=on_line if debug is not None else None)
            conn.send({"type": "result", "test_id": t.get("id"), "result": result})
            if result["verdict"] == VERDICT_STOPPED:
                break
    except TargetUnavailable as e:
        conn.send({"type": "checks", "ok": False, "message": str(e),
                   "checks": [{"name": "Target", "ok": False, "detail": str(e)}]})
    except Exception as e:  # noqa: BLE001 — report, never hang the parent
        conn.send({"type": "fatal", "message": f"{type(e).__name__}: {e}"})
    finally:
        if backend is not None:
            backend.close()
        try:
            conn.send({"type": "exit"})
        except (OSError, EOFError):
            pass
        conn.close()


def _safe_info(backend) -> dict:
    try:
        return backend.info()
    except Exception:  # noqa: BLE001
        return {}


def _apply(cmd: dict, state: dict) -> str:
    """Update debug state from a command; returns its action."""
    if "breakpoints" in cmd:
        state["breakpoints"] = set(int(x) for x in cmd["breakpoints"])
    if "watches" in cmd:
        state["watches"] = [str(w) for w in cmd["watches"]]
    action = cmd.get("action", "")
    if action == "stop":
        raise StopRequested()
    return action


def run_in_process(config: dict, tests: List[dict],
                   on_event: Callable[[dict], None],
                   commands: Optional[Callable[[float], Optional[dict]]] = None,
                   cancel: Optional[Callable[[], bool]] = None,
                   debug: Optional[dict] = None,
                   test_timeout_s: float = DEFAULT_TEST_TIMEOUT_S,
                   isolate: bool = True) -> List[dict]:
    """Run ``tests`` ([{id, code}]) on a fresh connection; return their results.

    The connection check runs first; if it fails, no test runs and the
    ``checks`` event says why. Every test starts from a reset. ``on_event``
    sees every message (checks, started, paused, result). ``commands(timeout)``
    returns the user's next debug command (``{"action": "continue"|"step"|
    "stop"|"snapshot", "breakpoints": [...], "watches": [...]}``) or None.
    A test that runs longer than ``test_timeout_s`` (debug pauses not
    counted) is killed and recorded as an error.
    """
    if not isolate:
        return _run_inline(config, tests, on_event, commands, debug)
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_child_main, args=(child, config, tests, debug),
                       name="target-test", daemon=True)
    proc.start()
    child.close()
    results: Dict[object, dict] = {}
    current = None
    paused = False
    started = time.monotonic()
    try:
        while True:
            if cancel is not None and cancel():
                if current is not None:
                    results[current] = _error_result("Stopped by the user.", VERDICT_STOPPED)
                break
            if paused and commands is not None:
                cmd = commands(0.2)
                if cmd is not None:
                    parent.send(cmd)
                    if cmd.get("action") in ("continue", "step", "stop"):
                        paused = False
                        started = time.monotonic()
                continue
            if commands is not None and current is not None:
                cmd = commands(0)
                if cmd is not None:
                    parent.send(cmd)
            if not parent.poll(0.1):
                if not proc.is_alive():
                    break
                if current is not None and time.monotonic() - started > test_timeout_s:
                    results[current] = _error_result(
                        f"The test ran longer than {int(test_timeout_s)} s and was stopped.")
                    on_event({"type": "result", "test_id": current, "result": results[current]})
                    break
                continue
            try:
                msg = parent.recv()
            except EOFError:
                break
            if msg.get("type") == "exit":
                break
            if msg.get("type") == "started":
                current = msg.get("test_id")
                started = time.monotonic()
            elif msg.get("type") == "paused":
                paused = True
            elif msg.get("type") == "result":
                results[msg.get("test_id")] = msg["result"]
                current = None
            on_event(msg)
    finally:
        try:
            parent.close()
        except OSError:
            pass
        proc.join(timeout=2)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)
    return [results[t.get("id")] for t in tests if t.get("id") in results]


def _error_result(message: str, verdict: str = VERDICT_ERROR) -> dict:
    return {"verdict": verdict, "message": message, "steps": [], "expectations": [],
            "duration_ms": 0}


class _InlineConn:
    """The child's side of the pipe, for running in the caller's process."""

    def __init__(self, on_event, commands):
        self.on_event = on_event
        self.commands = commands

    def send(self, msg):
        self.on_event(msg)

    def poll(self):
        return False

    def recv(self):
        while True:
            cmd = self.commands(0.2) if self.commands else {"action": "continue"}
            if cmd is not None:
                return cmd

    def close(self):
        pass


def _run_inline(config, tests, on_event, commands, debug) -> List[dict]:
    results = {}

    def relay(msg):
        if msg.get("type") == "result":
            results[msg.get("test_id")] = msg["result"]
        if msg.get("type") != "exit":
            on_event(msg)
    _child_main(_InlineConn(relay, commands), config, tests, debug)
    return [results[t.get("id")] for t in tests if t.get("id") in results]


# ======================================================================
# Approval rules
# ======================================================================
class ApprovalError(Exception):
    pass


class Approvals:
    """The approval rules, over the DB's target-test primitives.

    * A test the AI creates, or an AI change to a test, is a **draft**.
    * A manual edit (or a new manual test) is **approved** by that user.
    * Revert restores the last approved version (as a new version).
    * The AI proposes changes to existing tests; **accepting** a proposal makes
      it a draft version and **approving** it is a separate step.
    * Release, code-map or source changes never remove approval.
    """

    def __init__(self, db, user: str):
        self.db = db
        self.user = user

    def _get(self, test_id: int) -> dict:
        t = self.db.get_target_test(test_id)
        if t is None:
            raise ApprovalError(f"No such target test: {test_id}")
        return t

    def create(self, code: str, origin: str, rationale: str = "", release_id=None,
               model_id=None) -> int:
        meta = parse_meta(code)
        approved = origin == ORIGIN_USER
        test_id = self.db.create_target_test(
            test_key=meta.get("id") or "", title=meta.get("title") or "",
            port=meta.get("port") or "", requirement=meta.get("requirement") or "",
            model_id=model_id, release_id=release_id, rationale=rationale)
        self._new_version(test_id, code, origin, rationale, approve=approved)
        return test_id

    def _new_version(self, test_id: int, code: str, origin: str, note: str,
                     approve: bool) -> int:
        v = self.db.add_target_test_version(test_id, code, origin=origin, author=self.user,
                                            note=note)
        meta = parse_meta(code)
        fields = {k: meta[k] for k in ("title", "port", "requirement") if meta.get(k)}
        if meta.get("id"):
            fields["test_key"] = meta["id"]
        if approve:
            fields.update(approved_version=v, approved_by=self.user, approved_at=now_iso())
        self.db.update_target_test(test_id, **fields)
        return v

    def save_manual(self, test_id: int, code: str) -> int:
        """A user's correction: saved and approved by that user."""
        self._get(test_id)
        return self._new_version(test_id, code, ORIGIN_USER, "manual edit", approve=True)

    def save_ai(self, test_id: int, code: str, rationale: str = "") -> int:
        """An AI change applied directly (e.g. an accepted proposal): a draft."""
        self._get(test_id)
        if rationale:
            self.db.update_target_test(test_id, rationale=rationale)
        return self._new_version(test_id, code, ORIGIN_AI, rationale or "AI change",
                                 approve=False)

    def approve(self, test_id: int) -> int:
        t = self._get(test_id)
        if t["pending_code"]:
            raise ApprovalError("Accept or reject the AI's proposed change first.")
        issues = validate(t["code"])
        if issues:
            raise ApprovalError("Fix the script before approving it: "
                                + "; ".join(f"line {i['line']}: {i['message']}" for i in issues))
        self.db.update_target_test(test_id, approved_version=t["version"],
                                   approved_by=self.user, approved_at=now_iso())
        return t["version"]

    def revert(self, test_id: int) -> int:
        t = self._get(test_id)
        if not t["approved_version"]:
            raise ApprovalError("This test has never been approved.")
        if t["approved_version"] == t["version"]:
            return t["version"]
        code = self.db.get_target_test_version(test_id, t["approved_version"])["code"]
        v = self.db.add_target_test_version(test_id, code, origin=ORIGIN_REVERT,
                                            author=self.user,
                                            note=f"revert to v{t['approved_version']}")
        # The same code that was approved stays approved; who and when don't change.
        self.db.update_target_test(test_id, approved_version=v)
        return v

    def propose(self, test_id: int, code: str, rationale: str = "") -> None:
        self._get(test_id)
        self.db.update_target_test(test_id, pending_code=code, pending_rationale=rationale)

    def accept_proposal(self, test_id: int) -> int:
        t = self._get(test_id)
        if not t["pending_code"]:
            raise ApprovalError("There is no proposed change.")
        code, why = t["pending_code"], t["pending_rationale"]
        self.db.update_target_test(test_id, pending_code="", pending_rationale="")
        return self.save_ai(test_id, code, why)

    def reject_proposal(self, test_id: int) -> None:
        self._get(test_id)
        self.db.update_target_test(test_id, pending_code="", pending_rationale="")


def status_of(test: dict) -> str:
    return STATUS_APPROVED if test.get("approved_version") and \
        test["approved_version"] == test.get("version") else STATUS_DRAFT


NEW_TEST_TEMPLATE = '''from target_tests import test, target, expect


@test(id="{id}", title="{title}", port="{port}")
def {func}():
    # The runner resets the target before this line.
    target.break_at("{function}")
    target.run_until_stop(timeout_s=2)

    expect.stopped_at("{function}")
'''


def new_test_code(test_key: str, title: str = "", port: str = "",
                  function: str = "main") -> str:
    func = re.sub(r"\W", "_", test_key.lower()) or "tt"
    if func[0].isdigit():
        func = "tt_" + func
    return NEW_TEST_TEMPLATE.format(id=test_key, title=title.replace('"', "'"),
                                    port=port.replace('"', "'"), func=func,
                                    function=function or "main")
