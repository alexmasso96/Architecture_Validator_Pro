"""
Target bridge for Target Tests — the one place that talks to a debugger.

``TargetBackend`` is the small interface everything else uses: the test
runner, the debug session and the AI chat's target tools. Two backends:

``WinIdeaBridge``
    winIDEA through its ``isystem.connect`` Python SDK. The SDK must match the
    winIDEA build, so it's loaded from the selected winIDEA installation first
    and only then from whatever is installed in Python. winIDEA runs on Windows
    only; elsewhere the import fails and the bridge reports "not available".
    Calls that differ between winIDEA versions stay inside this class: the rest
    of the app never sees a version number.

``SimulatedTarget``
    A tiny in-process model of a program (functions, variables, ``if``
    branches, calls). It drives the tests of this feature and the in-app
    tutorial, and lets someone try the view without an ECU.

Pure module: no DB, no HTTP.
"""
from __future__ import annotations

import ast
import glob
import importlib
import os
import sys
import time
from typing import Callable, Dict, List, Optional

# A check result: ok True = passed, False = failed, None = not checked.
Check = Dict[str, object]

DEFAULT_PROBE = "main"


class TargetError(Exception):
    """A debugger operation failed; the message is shown to the user / model."""


class TargetUnavailable(TargetError):
    """No debugger can be reached (SDK missing, winIDEA not running, …)."""


# ======================================================================
# Interface
# ======================================================================
class TargetBackend:
    """What a test, a debug session and the AI may do with a target.

    Locations are a function name, or a file plus a line. Call stacks are
    outermost first: ``["main", "Os_Task10ms", "Speed_Main"]``.
    """

    kind = "abstract"

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def info(self) -> dict:
        return {"kind": self.kind}

    def checks(self) -> List[Check]:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def set_breakpoint(self, location: str, line: Optional[int] = None) -> None:
        raise NotImplementedError

    def clear_breakpoints(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def wait_until_stopped(self, timeout_s: float) -> bool:
        raise NotImplementedError

    def status(self) -> dict:
        """{"state": "running"|"stopped", "function", "file", "line"}."""
        raise NotImplementedError

    def read(self, expr: str) -> str:
        raise NotImplementedError

    def write(self, expr: str, value) -> None:
        raise NotImplementedError

    def call_stack(self) -> List[str]:
        raise NotImplementedError

    def registers(self) -> Dict[str, str]:
        return {}

    def step(self, kind: str = "over") -> None:
        raise NotImplementedError


def summarize_checks(checks: List[Check]) -> dict:
    failed = [c for c in checks if c.get("ok") is False]
    return {"ok": not failed, "checks": checks,
            "message": ("" if not failed else
                        "Connection check failed: " + "; ".join(
                            f"{c['name']} — {c.get('detail', '')}" for c in failed))}


# ======================================================================
# winIDEA (isystem.connect)
# ======================================================================
_SDK_SUBDIRS = ("", "sdk/python", "SDK/Python", "sdk", "SDK", "python", "Python")


def _sdk_paths(winidea_dir: str) -> List[str]:
    """Folders under a winIDEA installation that may hold ``isystem/connect``."""
    if not winidea_dir or not os.path.isdir(winidea_dir):
        return []
    found = []
    for sub in _SDK_SUBDIRS:
        base = os.path.join(winidea_dir, sub)
        if os.path.isdir(os.path.join(base, "isystem")):
            found.append(base)
    if not found:
        for pkg in glob.glob(os.path.join(winidea_dir, "*", "*", "isystem")) + \
                glob.glob(os.path.join(winidea_dir, "*", "isystem")):
            found.append(os.path.dirname(pkg))
    return found


def load_sdk(winidea_dir: str = "", importer: Callable = importlib.import_module):
    """``(module, where)`` for ``isystem.connect``: the installation's copy first,
    then the Python environment's. Raises ``TargetUnavailable`` when neither
    imports (always so on Linux and macOS)."""
    errors = []
    for path in _sdk_paths(winidea_dir):
        sys.path.insert(0, path)
        try:
            for name in [m for m in sys.modules if m == "isystem" or m.startswith("isystem.")]:
                del sys.modules[name]
            return importer("isystem.connect"), path
        except Exception as e:  # noqa: BLE001 — try the next place
            errors.append(f"{path}: {e}")
        finally:
            try:
                sys.path.remove(path)
            except ValueError:
                pass
    try:
        return importer("isystem.connect"), "python environment"
    except Exception as e:  # noqa: BLE001
        errors.append(f"python environment: {e}")
    raise TargetUnavailable(
        "winIDEA's isystem.connect SDK could not be loaded"
        + (" (winIDEA runs on Windows only)" if os.name != "nt" else "")
        + ". " + " | ".join(errors))


def _first(obj, names, *args):
    """Call the first method of ``names`` that ``obj`` has."""
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn(*args)
    raise AttributeError(names[0])


class WinIdeaBridge(TargetBackend):
    """winIDEA over ``isystem.connect``.

    Every SDK call goes through this class. The call-stack read is the one call
    that has to be confirmed against a real installation (``_frames``); it is
    the same in all supported winIDEA versions, so once confirmed it stays.
    """

    kind = "winidea"

    def __init__(self, workspace: str = "", winidea_dir: str = "",
                 probe: str = DEFAULT_PROBE, sdk=None):
        self.workspace = workspace
        self.winidea_dir = winidea_dir
        self.probe = probe or DEFAULT_PROBE
        self.ic = sdk
        self.sdk_where = "given" if sdk is not None else ""
        self.cmgr = None
        self.dbg = None
        self.exe = None
        self.bpc = None

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        if self.ic is None:
            self.ic, self.sdk_where = load_sdk(self.winidea_dir)
        ic = self.ic
        try:
            self.cmgr = ic.ConnectionMgr()
            if self.workspace:
                cfg = ic.CConnectionConfig()
                cfg.workspace(self.workspace)
                self.cmgr.connect(cfg)
            else:
                self.cmgr.connectMRU("")
        except Exception as e:  # noqa: BLE001
            self.cmgr = None
            raise TargetUnavailable(f"Could not connect to winIDEA: {e}") from e
        self.dbg = ic.CDebugFacade(self.cmgr)
        self.exe = ic.CExecutionController(self.cmgr)
        self.bpc = ic.CBreakpointController(self.cmgr)

    def close(self) -> None:
        if self.cmgr is not None:
            try:
                self.cmgr.disconnect(0)
            except Exception:  # noqa: BLE001 — disconnecting must not fail
                pass
        self.cmgr = self.dbg = self.exe = self.bpc = None

    def _require(self):
        if self.dbg is None:
            raise TargetUnavailable("Not connected to winIDEA.")

    def _call(self, what: str, fn, *args):
        self._require()
        try:
            return fn(*args)
        except TargetError:
            raise
        except Exception as e:  # noqa: BLE001 — SDK errors reach the user as text
            raise TargetError(f"{what} failed: {e}") from e

    # -- versions -----------------------------------------------------------
    def winidea_version(self) -> str:
        try:
            ide = self.ic.CIDEController(self.cmgr)
            v = ide.getWinIDEAVersion()
            return f"{v.getMajor()}.{v.getMinor()}.{v.getBuild()}"
        except Exception:  # noqa: BLE001
            return "unknown"

    def sdk_version(self) -> str:
        for name in ("getModuleVersion", "getVersion"):
            fn = getattr(self.ic, name, None)
            if callable(fn):
                try:
                    return str(fn())
                except Exception:  # noqa: BLE001
                    pass
        return str(getattr(self.ic, "__version__", "unknown"))

    def info(self) -> dict:
        return {"kind": self.kind, "workspace": self.workspace,
                "winidea_version": self.winidea_version() if self.cmgr else "",
                "sdk_version": self.sdk_version() if self.ic else "",
                "sdk_location": self.sdk_where}

    # -- connection check ------------------------------------------------
    def checks(self) -> List[Check]:
        out: List[Check] = []
        try:
            if self.dbg is None:
                self.connect()
        except TargetUnavailable as e:
            sdk_ok = self.ic is not None
            out.append({"name": "SDK", "ok": sdk_ok,
                        "detail": (f"isystem.connect from {self.sdk_where}" if sdk_ok
                                   else str(e))})
            if sdk_ok:
                out.append({"name": "winIDEA", "ok": False, "detail": str(e)})
            return out
        win, sdk = self.winidea_version(), self.sdk_version()
        out.append({"name": "SDK", "ok": True,
                    "detail": f"isystem.connect {sdk} from {self.sdk_where}"})
        connected = bool(_safe(lambda: self.cmgr.isConnected(), True))
        out.append({"name": "winIDEA", "ok": connected,
                    "detail": f"winIDEA {win}" + (f", workspace {self.workspace}"
                                                  if self.workspace else "")})
        match = _versions_match(win, sdk)
        out.append({"name": "SDK matches winIDEA", "ok": match,
                    "detail": f"winIDEA {win}, SDK {sdk}"
                    + ("" if match is not False else
                       " — select the winIDEA folder in Preferences so its own SDK is used")})
        try:
            st = self.status()
            out.append({"name": "Target", "ok": True,
                        "detail": f"debugger connected, CPU {st['state']}"})
        except TargetError as e:
            out.append({"name": "Target", "ok": False, "detail": str(e)})
            return out
        try:
            self.read(self.probe)
            out.append({"name": "Memory read", "ok": True, "detail": f"read '{self.probe}'"})
        except TargetError as e:
            out.append({"name": "Memory read", "ok": False, "detail": str(e)})
        return out

    # -- execution -------------------------------------------------------
    def reset(self) -> None:
        self._call("Reset", self.dbg.reset)

    def set_breakpoint(self, location: str, line: Optional[int] = None) -> None:
        if line is None:
            self._call(f"Breakpoint at {location}", self.dbg.setBP, location)
        else:
            self._call(f"Breakpoint at {location}:{line}", self.dbg.setBP, int(line), location)

    def clear_breakpoints(self) -> None:
        self._call("Clearing breakpoints",
                   lambda: _first(self.dbg, ("deleteAll", "deleteAllBPs")))

    def run(self) -> None:
        self._call("Run", self.dbg.run)

    def stop(self) -> None:
        self._call("Stop", self.dbg.stop)

    def wait_until_stopped(self, timeout_s: float) -> bool:
        self._require()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if self.status()["state"] == "stopped":
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def status(self) -> dict:
        st = self._call("Reading the CPU status", self.dbg.getCPUStatus)
        stopped = bool(_safe(st.isStopped, False))
        out = {"state": "stopped" if stopped else "running",
               "function": None, "file": None, "line": None}
        if stopped:
            frames = _safe(self._frames, [])
            if frames:
                out["function"] = frames[-1]
        return out

    def read(self, expr: str) -> str:
        v = self._call(f"Reading '{expr}'", self.dbg.evaluate,
                       self.ic.IConnectDebug.fRealTime, expr)
        return str(_first(v, ("getResult", "toString")) if hasattr(v, "getResult")
                   or hasattr(v, "toString") else v)

    def write(self, expr: str, value) -> None:
        self._call(f"Writing '{expr}'", self.dbg.modify,
                   self.ic.IConnectDebug.fMonitor, expr, str(value))

    def step(self, kind: str = "over") -> None:
        names = {"over": ("stepOverHigh", "stepHigh"), "into": ("stepHigh",),
                 "out": ("stepOutHigh", "runUntilReturn")}.get(kind, ("stepHigh",))
        self._call(f"Step {kind}", lambda: _first(self.dbg, names))

    def call_stack(self) -> List[str]:
        return self._call("Reading the call stack", self._frames)

    def _frames(self) -> List[str]:
        """Function names, outermost first.

        TO CONFIRM on a winIDEA installation: which isystem.connect call returns
        the call stack. The candidates below are tried in order; the first that
        exists is used, and the result is normalized to function names.
        """
        ic, cmgr = self.ic, self.cmgr
        candidates = []
        if hasattr(ic, "CStackFrameController"):
            ctl = ic.CStackFrameController(cmgr)
            candidates.append(lambda: _first(ctl, ("getStackFrames", "getCallStack")))
        for name in ("getCallStack", "getStackFrames"):
            if callable(getattr(self.dbg, name, None)):
                candidates.append(getattr(self.dbg, name))
        if not candidates:
            raise TargetError("Reading the call stack isn't available in this "
                              "isystem.connect version.")
        frames = candidates[0]()
        names = []
        for f in list(frames):
            name = None
            for attr in ("getFunctionName", "getFunction", "functionName", "name"):
                v = getattr(f, attr, None)
                if v is not None:
                    name = v() if callable(v) else v
                    break
            names.append(str(name if name is not None else f))
        # winIDEA lists the current frame first.
        return list(reversed(names))


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _versions_match(winidea: str, sdk: str) -> Optional[bool]:
    """True/False when both are known (major.minor.build), else None."""
    def parts(v):
        nums = [p for p in str(v).replace("-", ".").split(".") if p.isdigit()]
        return tuple(nums[:3]) if len(nums) >= 3 else None
    a, b = parts(winidea), parts(sdk)
    if a is None or b is None:
        return None
    return a == b


# ======================================================================
# Simulator
# ======================================================================
_SAFE_NODES = (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
               ast.Name, ast.Load, ast.Constant, ast.And, ast.Or, ast.Not,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
               ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,
               ast.USub, ast.UAdd, ast.Invert, ast.Eq, ast.NotEq, ast.Lt,
               ast.LtE, ast.Gt, ast.GtE)


def eval_expr(expr: str, variables: Dict[str, object]):
    """A C-like expression over variables: arithmetic, comparisons, and/or."""
    text = str(expr).replace("&&", " and ").replace("||", " or ")
    text = text.replace("!=", "\x00").replace("!", " not ").replace("\x00", "!=")
    tree = ast.parse(text.strip(), mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            raise TargetError(f"Unsupported expression: {expr}")
        if isinstance(node, ast.Name) and node.id not in variables:
            raise TargetError(f"Unknown symbol '{node.id}'")
    return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, dict(variables))  # noqa: S307


class SimulatedTarget(TargetBackend):
    """A program model the tests and the tutorial can debug.

    ``program``::

        {"entry": "main",
         "variables": {"a": 0, "speed_out": 0},
         "functions": {
            "main": {"file": "main.c", "line": 10,
                     "body": [{"call": "Speed_Main", "line": 12}]},
            "Speed_Main": {"file": "speed.c", "line": 120, "body": [
                {"if": "a == 5", "line": 123,
                 "then": [{"call": "Com_SendSignal", "line": 124}]},
                {"set": "speed_out", "expr": "speed_out + 1", "line": 126}]},
            "Com_SendSignal": {"file": "com.c", "line": 40, "body": []}}}

    The entry function runs again when it returns (a cyclic main loop), so a
    breakpoint that's never reached stops on the step budget, which
    ``wait_until_stopped`` reports as a timeout.
    """

    kind = "simulator"
    STEP_BUDGET = 20_000

    def __init__(self, program: dict):
        self.program = program
        self.functions = program.get("functions", {})
        self.entry = program.get("entry", "main")
        self._initial = dict(program.get("variables", {}))
        self.variables: Dict[str, object] = {}
        self.breakpoints: List[tuple] = []
        self.connected = False
        self._gen = None
        self._where = None
        self._state = "stopped"
        self._pending = None
        self.log: List[str] = []

    def connect(self) -> None:
        self.connected = True
        self.reset()

    def info(self) -> dict:
        return {"kind": self.kind, "winidea_version": "simulator", "sdk_version": "-",
                "workspace": ""}

    def checks(self) -> List[Check]:
        if not self.connected:
            self.connect()
        return [{"name": "Simulator", "ok": True, "detail": "simulated target"},
                {"name": "Target", "ok": self.connected,
                 "detail": "connected" if self.connected else "not connected"}]

    def _require(self):
        if not self.connected:
            raise TargetUnavailable("Not connected to the simulator.")

    def reset(self) -> None:
        self._require()
        self.variables = dict(self._initial)
        self._gen = self._program()
        self._where = None
        self._state = "stopped"
        self._pending = None
        self.log.append("reset")

    # -- the program as a generator of (kind, function, file, line, stack) -------
    def _program(self):
        while True:
            yield from self._function(self.entry, [])

    def _function(self, name: str, stack: List[str]):
        fn = self.functions.get(name)
        if fn is None:
            raise TargetError(f"Simulator: no function '{name}'")
        stack = stack + [name]
        yield ("entry", name, fn.get("file", ""), fn.get("line", 0), stack)
        yield from self._block(fn.get("body", []), name, fn.get("file", ""), stack)

    def _block(self, body, name, file, stack):
        for stmt in body:
            yield ("line", name, file, stmt.get("line", 0), stack)
            if "call" in stmt:
                yield from self._function(stmt["call"], stack)
            elif "if" in stmt:
                branch = stmt.get("then", []) if eval_expr(stmt["if"], self.variables) \
                    else stmt.get("else", [])
                yield from self._block(branch, name, file, stack)
            elif "set" in stmt:
                self.variables[stmt["set"]] = eval_expr(stmt.get("expr", "0"), self.variables)

    def _hits(self, ev) -> bool:
        kind, name, file, line, _stack = ev
        for loc, bl in self.breakpoints:
            if bl is None and kind == "entry" and loc == name:
                return True
            if bl is not None and kind == "line" and line == bl and \
                    os.path.basename(file) == os.path.basename(loc):
                return True
        return False

    def set_breakpoint(self, location: str, line: Optional[int] = None) -> None:
        self._require()
        if line is None and location not in self.functions:
            raise TargetError(f"No function '{location}' in the program.")
        self.breakpoints.append((location, None if line is None else int(line)))

    def clear_breakpoints(self) -> None:
        self.breakpoints = []

    def _advance(self, until: Callable[[tuple], bool]) -> bool:
        for _ in range(self.STEP_BUDGET):
            ev = self._pending if self._pending is not None else next(self._gen)
            self._pending = None
            if until(ev):
                self._where = ev
                self._state = "stopped"
                return True
        self._state = "running"
        return False

    def run(self) -> None:
        self._require()
        # Leave the current stop location before looking for the next hit.
        start = self._where
        self._state = "running"
        self._advance(lambda ev: ev is not start and self._hits(ev))

    def stop(self) -> None:
        self._state = "stopped"

    def wait_until_stopped(self, timeout_s: float) -> bool:
        return self._state == "stopped"

    def status(self) -> dict:
        if self._where is None or self._state != "stopped":
            return {"state": self._state, "function": None, "file": None, "line": None}
        _k, name, file, line, _s = self._where
        return {"state": "stopped", "function": name, "file": file, "line": line}

    def read(self, expr: str) -> str:
        self._require()
        if expr in self.functions:
            return f"0x{abs(hash(expr)) % 0xFFFFFF:06X}"
        return str(eval_expr(expr, self.variables))

    def write(self, expr: str, value) -> None:
        self._require()
        if expr not in self.variables:
            raise TargetError(f"Unknown variable '{expr}'")
        self.variables[expr] = eval_expr(str(value), self.variables)
        self.log.append(f"write {expr}={self.variables[expr]}")

    def call_stack(self) -> List[str]:
        if self._where is None or self._state != "stopped":
            raise TargetError("The target is running; stop it to read the call stack.")
        return list(self._where[4])

    def registers(self) -> Dict[str, str]:
        line = self._where[3] if self._where else 0
        return {"PC": f"0x{line:08X}"}

    def step(self, kind: str = "over") -> None:
        self._require()
        start = self._where
        depth = len(start[4]) if start else 0
        if kind == "into":
            self._advance(lambda ev: ev is not start)
        elif kind == "out":
            self._advance(lambda ev: ev is not start and len(ev[4]) < depth)
        else:
            self._advance(lambda ev: ev is not start and len(ev[4]) <= depth)


# ======================================================================
# Factory
# ======================================================================
def make_backend(config: dict) -> TargetBackend:
    """A backend from the saved connection settings::

        {"kind": "winidea", "workspace": "...", "winidea_dir": "...", "probe": "main"}
        {"kind": "simulator", "program": {...}}
    """
    config = config or {}
    kind = config.get("kind", "winidea")
    if kind == "simulator":
        return SimulatedTarget(config.get("program") or DEMO_PROGRAM)
    if kind == "winidea":
        return WinIdeaBridge(workspace=config.get("workspace", ""),
                             winidea_dir=config.get("winidea_dir", ""),
                             probe=config.get("probe", DEFAULT_PROBE))
    raise TargetError(f"Unknown target kind: {kind}")


# The example from the feature ticket: Com_SendSignal only runs when a == B (5).
DEMO_PROGRAM = {
    "entry": "main",
    "variables": {"a": 0, "speed_out": 0},
    "functions": {
        "main": {"file": "main.c", "line": 10,
                 "body": [{"call": "Os_Task10ms", "line": 14}]},
        "Os_Task10ms": {"file": "os.c", "line": 30,
                        "body": [{"call": "Speed_Main", "line": 32}]},
        "Speed_Main": {"file": "speed.c", "line": 120, "body": [
            {"if": "a == 5", "line": 123,
             "then": [{"call": "Com_SendSignal", "line": 124}]},
            {"set": "speed_out", "expr": "speed_out + 1", "line": 126}]},
        "Com_SendSignal": {"file": "com.c", "line": 40, "body": []},
    },
}
