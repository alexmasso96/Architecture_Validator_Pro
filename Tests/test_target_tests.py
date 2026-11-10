"""Target Tests: script checks, the runtime on the simulator, the isolated
runner with debug pauses, the approval rules and the API."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient

from Application_Logic import Logic_Target_Tests as tt
from Application_Logic import Logic_WinIdea_Bridge as br
from backend.app import create_app
from Tests.test_helpers import make_project_db

GOOD = '''from target_tests import test, target, expect


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
    expect.value("speed_out", ">= 0")
'''

UNREACHED = GOOD.replace('    target.write("a", 5)\n', "")


def sim():
    b = br.SimulatedTarget(br.DEMO_PROGRAM)
    b.connect()
    return b


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def test_example_script_is_allowed_and_its_metadata_is_read():
    assert tt.validate(GOOD) == []
    meta = tt.parse_meta(GOOD)
    assert meta["id"] == "TT-0042" and meta["port"] == "SpeedProvider.Speed"
    assert tt.referenced_functions(GOOD) == [
        "Com_SendSignal", "main", "Os_Task10ms", "Speed_Main"]


@pytest.mark.parametrize("body, needle", [
    ("    import os\n", "Import"),
    ("    open('x')\n", "open"),
    ("    target.__class__\n", "doesn't exist"),
    ("    x = 1\n    x.real\n", "with a dot"),
    ("    while True:\n        pass\n", "while"),
    ("    eval('1')\n", "eval"),
    ("    f = lambda: 1\n", "Lambda"),
    ("    target.read(*['a'])\n", "Starred"),
    ("    target.flash('x')\n", "doesn't exist"),
])
def test_disallowed_code_is_reported(body, needle):
    code = ("from target_tests import test, target, expect\n\n@test(id='T')\n"
            "def t():\n" + body)
    issues = tt.validate(code)
    assert issues, body
    assert any(needle in i["message"] for i in issues), issues


def test_top_level_code_and_other_imports_are_refused():
    assert tt.validate("import os\n")
    assert tt.validate("from target_tests import test\nx = 1\n")
    assert any("exactly one" in i["message"] for i in tt.validate(
        "from target_tests import test\n"))


# ----------------------------------------------------------------------
# Runtime on the simulator
# ----------------------------------------------------------------------
def test_reaching_the_call_behind_the_if_passes():
    r = tt.execute(GOOD, sim())
    assert r["verdict"] == tt.VERDICT_PASSED, r["message"]
    calls = [s["call"] for s in r["steps"]]
    assert calls[0] == "reset" and "target.write" in calls
    assert all(e["ok"] for e in r["expectations"])


def test_without_the_write_the_call_is_not_reached():
    r = tt.execute(UNREACHED, sim())
    assert r["verdict"] == tt.VERDICT_FAILED
    assert "Com_SendSignal" in r["message"]
    assert r["expectations"][0]["actual"] == "Speed_Main"


def test_each_run_starts_from_a_reset():
    b = sim()
    assert tt.execute(GOOD, b)["verdict"] == tt.VERDICT_PASSED
    assert b.breakpoints == []
    # The write from the first run doesn't carry over.
    assert tt.execute(UNREACHED, b)["verdict"] == tt.VERDICT_FAILED


def test_a_target_error_is_an_error_with_its_line():
    code = GOOD.replace('target.write("a", 5)', 'target.write("nope", 5)')
    r = tt.execute(code, sim())
    assert r["verdict"] == tt.VERDICT_ERROR
    assert r["message"].startswith("line 9:") and "nope" in r["message"]


def test_a_script_without_expectations_fails():
    code = ("from target_tests import test, target, expect\n\n@test(id='T')\n"
            "def t():\n    target.run_to('Speed_Main')\n")
    assert tt.execute(code, sim())["verdict"] == tt.VERDICT_FAILED


def test_not_reached_and_conditions():
    code = ("from target_tests import test, target, expect\n\n@test(id='T')\n"
            "def t():\n    expect.not_reached('Com_SendSignal', timeout_s=1)\n"
            "    expect.value('a', 0)\n    expect.value('a', '< 1')\n"
            "    expect.true(int(target.read('a')) == 0, 'a is 0')\n")
    r = tt.execute(code, sim())
    assert r["verdict"] == tt.VERDICT_PASSED, r["message"]


def test_simulator_steps_and_call_stack():
    b = sim()
    b.set_breakpoint("Speed_Main")
    b.run()
    assert b.call_stack() == ["main", "Os_Task10ms", "Speed_Main"]
    b.step("over")
    assert b.status()["line"] == 123
    b.write("a", 5)
    b.step("over")   # the if
    b.step("into")
    assert b.status()["function"] == "Com_SendSignal"


def test_compare_conditions():
    assert tt._compare("5", 5)
    assert tt._compare("0x10", "== 16")
    assert tt._compare("3", "> 2") and not tt._compare("3", "> 3")
    assert tt._compare("on", "== 'on'")


# ----------------------------------------------------------------------
# winIDEA bridge (without winIDEA)
# ----------------------------------------------------------------------
def test_bridge_without_sdk_reports_why():
    def no_sdk(name):
        raise ImportError("No module named 'isystem'")
    with pytest.raises(br.TargetUnavailable) as e:
        br.load_sdk("", importer=no_sdk)
    assert "isystem.connect" in str(e.value)


def test_bridge_checks_with_a_fake_sdk():
    class V:
        def getMajor(self): return 9
        def getMinor(self): return 21
        def getBuild(self): return 121

    class Status:
        def isStopped(self): return True

    class FakeIC:
        class IConnectDebug:
            fRealTime = 1
            fMonitor = 2

        @staticmethod
        def getModuleVersion():
            return "9.21.121"

        class ConnectionMgr:
            def connectMRU(self, _): pass
            def isConnected(self): return True
            def disconnect(self, _): pass

        class CIDEController:
            def __init__(self, _): pass
            def getWinIDEAVersion(self): return V()

        class CDebugFacade:
            def __init__(self, _): pass
            def getCPUStatus(self): return Status()
            def evaluate(self, _flags, expr): return f"0x100 ({expr})"
            def getCallStack(self):
                return ["Speed_Main", "Os_Task10ms", "main"]

        class CExecutionController:
            def __init__(self, _): pass

        class CBreakpointController:
            def __init__(self, _): pass

    b = br.WinIdeaBridge(sdk=FakeIC)
    checks = b.checks()
    assert all(c["ok"] is not False for c in checks), checks
    assert {c["name"] for c in checks} >= {"SDK", "winIDEA", "SDK matches winIDEA",
                                            "Target", "Memory read"}
    assert b.call_stack() == ["main", "Os_Task10ms", "Speed_Main"]
    assert b.info()["winidea_version"] == "9.21.121"
    assert br._versions_match("9.21.110", "9.21.121") is False
    assert br._versions_match("9.21.121", "unknown") is None


def test_bridge_reads_the_stack_the_way_the_sdk_gives_it():
    """isystem.connect 9.26: CDataController.getStackFrames fills a
    StackFrameVector of frames, the current one first."""
    class Frame:
        def __init__(self, fn, file, line): self.fn, self.file, self.line = fn, file, line
        def getFunction(self): return self.fn
        def getFileName(self): return self.file
        def getLineNumber(self): return self.line

    asked = []

    class Status:
        def isStopped(self): return True

    class FakeIC:
        class StackFrameVector(list):
            pass

        class CDataController:
            def __init__(self, _): pass

            def getStackFrames(self, active_only, abs_paths, frames):
                asked.append((active_only, abs_paths))
                all_frames = [Frame("Com_SendSignal", "com.c", 40),
                              Frame("Speed_Main", "speed.c", 123), Frame("main", "main.c", 9)]
                frames.extend(all_frames[:1] if active_only else all_frames)

        class CDebugFacade:
            def __init__(self, _): pass
            def getCPUStatus(self): return Status()

    b = br.WinIdeaBridge(sdk=FakeIC)
    b.cmgr, b.dbg = object(), FakeIC.CDebugFacade(None)
    assert b.call_stack() == ["main", "Speed_Main", "Com_SendSignal"]
    assert b.status() == {"state": "stopped", "function": "Com_SendSignal",
                          "file": "com.c", "line": 40}
    assert asked == [(False, False), (True, False)]


# ----------------------------------------------------------------------
# Isolated runner and debugging
# ----------------------------------------------------------------------
def test_runner_in_a_child_process():
    events = []
    results = tt.run_in_process({"kind": "simulator"},
                                [{"id": 1, "code": GOOD}, {"id": 2, "code": UNREACHED}],
                                events.append)
    assert [r["verdict"] for r in results] == [tt.VERDICT_PASSED, tt.VERDICT_FAILED]
    assert events[0]["type"] == "checks" and events[0]["ok"]


def test_failed_connection_check_runs_nothing():
    events = []
    results = tt.run_in_process({"kind": "winidea", "winidea_dir": "/nowhere"},
                                [{"id": 1, "code": GOOD}], events.append, isolate=False)
    assert results == []
    assert events[0]["type"] == "checks" and not events[0]["ok"]


def test_debug_pauses_at_breakpoints_and_shows_state():
    import queue
    cmds = queue.Queue()
    paused = []

    def on_event(msg):
        if msg["type"] == "paused":
            paused.append(msg)
            cmds.put({"action": "step"} if len(paused) == 1 else {"action": "continue"})

    def commands(timeout):
        try:
            return cmds.get(timeout=timeout) if timeout else cmds.get_nowait()
        except queue.Empty:
            return None

    results = tt.run_in_process({"kind": "simulator"}, [{"id": "d", "code": GOOD}], on_event,
                                commands=commands,
                                debug={"breakpoints": [14], "watches": ["a"]})
    assert results[0]["verdict"] == tt.VERDICT_PASSED
    assert paused[0]["line"] == 14
    assert paused[0]["snapshot"]["watches"] == {"a": "5"}
    assert paused[0]["snapshot"]["call_stack"][-1] == "Com_SendSignal"
    assert paused[1]["line"] == 15      # one step later


def test_stop_while_paused():
    def on_event(msg):
        pass
    results = tt.run_in_process({"kind": "simulator"}, [{"id": "d", "code": GOOD}], on_event,
                                commands=lambda t: {"action": "stop"},
                                debug={"breakpoints": [7]}, isolate=False)
    assert results[0]["verdict"] == tt.VERDICT_STOPPED


# ----------------------------------------------------------------------
# Approval rules (on a real project DB)
# ----------------------------------------------------------------------
@pytest.fixture()
def db(tmp_path):
    d = make_project_db(str(tmp_path / "p.arch"), layout=[("TC. ID", "Static Text", True)],
                        models=[{"name": "A", "status": "In Work", "rows": []}],
                        releases=[{"name": "R1"}])
    yield d
    d.close()


def test_ai_tests_are_drafts_and_manual_edits_are_approved(db):
    ap = tt.Approvals(db, "alex")
    tid = ap.create(GOOD, tt.ORIGIN_AI, rationale="a must equal B")
    t = db.get_target_test(tid)
    assert tt.status_of(t) == tt.STATUS_DRAFT and t["test_key"] == "TT-0042"
    ap.approve(tid)
    t = db.get_target_test(tid)
    assert tt.status_of(t) == tt.STATUS_APPROVED and t["approved_by"] == "alex"

    # A manual correction is approved straight away.
    ap.save_manual(tid, GOOD.replace(">= 0", "> 0"))
    t = db.get_target_test(tid)
    assert t["version"] == 2 and tt.status_of(t) == tt.STATUS_APPROVED


def test_accepting_a_proposal_and_approving_it_are_two_steps(db):
    ap = tt.Approvals(db, "alex")
    tid = ap.create(GOOD, tt.ORIGIN_USER)
    assert tt.status_of(db.get_target_test(tid)) == tt.STATUS_APPROVED

    ap.propose(tid, UNREACHED, "try without the write")
    t = db.get_target_test(tid)
    assert t["pending_code"] == UNREACHED and tt.status_of(t) == tt.STATUS_APPROVED
    with pytest.raises(tt.ApprovalError):
        ap.approve(tid)                      # accept or reject first

    ap.accept_proposal(tid)
    t = db.get_target_test(tid)
    assert t["code"] == UNREACHED and not t["pending_code"]
    assert tt.status_of(t) == tt.STATUS_DRAFT   # accepted, not approved

    ap.revert(tid)
    t = db.get_target_test(tid)
    assert t["code"] == GOOD and tt.status_of(t) == tt.STATUS_APPROVED
    assert [v["origin"] for v in db.list_target_test_versions(tid)] == [
        tt.ORIGIN_REVERT, tt.ORIGIN_AI, tt.ORIGIN_USER]


def test_rejecting_a_proposal_keeps_the_test(db):
    ap = tt.Approvals(db, "alex")
    tid = ap.create(GOOD, tt.ORIGIN_USER)
    ap.propose(tid, UNREACHED)
    ap.reject_proposal(tid)
    t = db.get_target_test(tid)
    assert t["code"] == GOOD and t["version"] == 1 and not t["pending_code"]


def test_an_invalid_script_cannot_be_approved(db):
    ap = tt.Approvals(db, "alex")
    tid = ap.create(GOOD + "\nimport os\n", tt.ORIGIN_AI)
    with pytest.raises(tt.ApprovalError):
        ap.approve(tid)


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
TOKEN = "tt-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_PREFS_FILE", str(tmp_path / "appdata" / "prefs.json"))
    path = str(tmp_path / "proj.arch")
    d = make_project_db(path, layout=[("TC. ID", "Static Text", True)],
                        models=[{"name": "A", "status": "In Work", "rows": []}],
                        releases=[{"name": "R1"}])
    d.close()
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        assert c.post("/api/project/open", json={"path": path, "mode": "exclusive"},
                      headers=AUTH).status_code == 200
        yield c


def _ok(r):
    assert r.status_code < 300, r.text
    return r.json()


def _wait(c, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = _ok(c.get(f"/api/jobs/{job_id}", headers=AUTH))
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_api_create_edit_run_and_record(client):
    c = client
    cfg = _ok(c.put("/api/target-tests/config/target", json={"kind": "simulator"},
                    headers=AUTH))
    assert cfg["kind"] == "simulator"
    check = _ok(c.post("/api/target-tests/connection/check", headers=AUTH))
    assert check["ok"]

    t = _ok(c.post("/api/target-tests", json={"code": GOOD}, headers=AUTH))
    assert t["status"] == "approved" and t["test_key"] == "TT-0042"
    blank = _ok(c.post("/api/target-tests", json={"title": "x", "function": "Speed_Main"},
                       headers=AUTH))
    assert blank["test_key"] == "TT-0043" and blank["issues"] == []

    job = _ok(c.post("/api/target-tests/run", json={"test_ids": [t["id"], blank["id"]]},
                     headers=AUTH))
    j = _wait(c, job["job_id"])
    assert j["status"] == "done", j
    verdicts = {r["test_id"]: r["verdict"] for r in j["result"]["results"]}
    assert verdicts == {t["id"]: "passed", blank["id"]: "passed"}

    listed = _ok(c.get("/api/target-tests", headers=AUTH))["tests"]
    row = next(x for x in listed if x["id"] == t["id"])
    assert row["last_result"]["verdict"] == "passed" and row["last_result"]["approved"]

    detail = _ok(c.get(f"/api/target-tests/{t['id']}", headers=AUTH))
    res = _ok(c.get(f"/api/target-tests/results/{detail['results'][0]['id']}", headers=AUTH))
    assert res["evidence"]["expectations"][1]["actual"][-1] == "Com_SendSignal"

    edited = _ok(c.put(f"/api/target-tests/{t['id']}", json={"code": UNREACHED},
                       headers=AUTH))
    assert edited["status"] == "approved" and edited["version"] == 2


def test_api_failed_connection_check_runs_nothing(client):
    c = client
    _ok(c.put("/api/target-tests/config/target",
              json={"kind": "winidea", "winidea_dir": "/nowhere"}, headers=AUTH))
    check = _ok(c.post("/api/target-tests/connection/check", headers=AUTH))
    assert not check["ok"] and "isystem.connect" in check["message"]
    t = _ok(c.post("/api/target-tests", json={"code": GOOD}, headers=AUTH))
    j = _wait(c, _ok(c.post("/api/target-tests/run", json={"test_ids": [t["id"]]},
                            headers=AUTH))["job_id"])
    assert j["status"] == "failed" and "Connection check failed" in j["error"]
    assert _ok(c.get(f"/api/target-tests/{t['id']}", headers=AUTH))["results"] == []


def test_api_proposal_flow(client):
    c = client
    t = _ok(c.post("/api/target-tests", json={"code": GOOD}, headers=AUTH))
    state = c.app.state.appstate
    tt.Approvals(state.db, "ai").propose(t["id"], UNREACHED, "why")
    d = _ok(c.post(f"/api/target-tests/{t['id']}/proposal/accept", headers=AUTH))
    assert d["status"] == "draft" and d["code"] == UNREACHED
    d = _ok(c.post(f"/api/target-tests/{t['id']}/approve", headers=AUTH))
    assert d["status"] == "approved"


def test_api_debug_session(client):
    c = client
    _ok(c.put("/api/target-tests/config/target", json={"kind": "simulator"}, headers=AUTH))
    bus = c.app.state.bus
    seen = []
    orig = bus.publish

    def spy(kind, payload):
        if kind == "target-debug":
            seen.append(payload)
            if payload.get("type") == "paused":
                from backend import target_session
                target_session.debug_command(payload["session"], {"action": "continue"})
        return orig(kind, payload)
    bus.publish = spy
    d = _ok(c.post("/api/target-tests/debug",
                   json={"code": GOOD, "breakpoints": [14], "watches": ["a"]}, headers=AUTH))
    j = _wait(c, d["job_id"])
    assert j["status"] == "done", j
    assert j["result"]["result"]["verdict"] == "passed"
    assert any(p.get("type") == "paused" and p["line"] == 14 for p in seen)


# ----------------------------------------------------------------------
# AI chat tools
# ----------------------------------------------------------------------
def _ai(db=None, allow_writes=False):
    from Application_Logic import Logic_AI_Target_Tools as at
    backend = sim()
    log = []
    ex = at.TargetToolExecutor(None, get_backend=lambda: backend, db=db, user="ai",
                               allow_writes=allow_writes, audit=log.append)
    return ex, backend, log


def test_ai_feedback_loop_on_the_simulator():
    ex, b, log = _ai(allow_writes=True)
    assert "Breakpoint set" in ex.execute("target_set_breakpoint", {"location": "Com_SendSignal"})
    out = ex.execute("target_run_until_stop", {"timeout_s": 1})
    assert "No breakpoint was hit" in out          # behind the if
    ex.execute("target_set_breakpoint", {"location": "speed.c", "line": 123})
    assert "Speed_Main" in ex.execute("target_run_until_stop", {})
    assert ex.execute("target_read", {"expr": "a"}) == "a = 0"
    assert "written" in ex.execute("target_write", {"expr": "a", "value": "5"})
    assert "Com_SendSignal" in ex.execute("target_run_until_stop", {})
    assert ex.execute("target_call_stack", {}).endswith("Speed_Main -> Com_SendSignal")
    assert len(log) == 8 and log[0]["tool"] == "target_set_breakpoint"


def test_ai_writes_need_the_users_switch():
    ex, b, _ = _ai(allow_writes=False)
    assert ex.execute("target_write", {"expr": "a", "value": "5"}).startswith("ERROR")
    assert b.variables["a"] == 0
    assert ex.execute("target_try_test", {"code": GOOD}).startswith("ERROR")
    assert '"verdict": "failed"' in ex.execute("target_try_test", {"code": UNREACHED})


def test_ai_saves_drafts_and_proposals_only(db):
    ex, _b, _ = _ai(db=db, allow_writes=True)
    assert '"verdict": "passed"' in ex.execute("target_try_test", {"code": GOOD})
    out = ex.execute("save_target_test", {"code": GOOD, "model": "A", "rationale": "B is 5"})
    tid = int(out.split("draft test ")[1].split(" ")[0])
    t = db.get_target_test(tid)
    assert tt.status_of(t) == tt.STATUS_DRAFT and t["rationale"] == "B is 5"

    tt.Approvals(db, "alex").approve(tid)
    ex.execute("propose_target_test_change", {"id": tid, "code": UNREACHED, "rationale": "x"})
    t = db.get_target_test(tid)
    assert tt.status_of(t) == tt.STATUS_APPROVED and t["pending_code"] == UNREACHED
    assert ex.execute("save_target_test", {"code": "import os", "model": "A",
                                           "rationale": ""}).startswith("ERROR")
    assert "TT-0042" in ex.execute("list_target_tests", {})


def test_chat_context_lists_the_target_tools(client):
    body = {"provider_id": "anthropic", "model": "x", "messages": [],
            "live_target": True, "ground_in_mindmap": False}
    ctx = _ok(client.post("/api/ai/context", json=body, headers=AUTH))
    assert "target_run_until_stop" in ctx["tool_names"]
    assert "save_target_test" in ctx["tool_names"]
    body["live_target"] = False
    ctx = _ok(client.post("/api/ai/context", json=body, headers=AUTH))
    assert "target_run_until_stop" not in ctx["tool_names"]


def test_debug_watch_added_while_paused_gets_a_fresh_snapshot():
    import queue
    cmds = queue.Queue()
    paused = []

    def on_event(msg):
        if msg["type"] == "paused":
            paused.append(msg)
            if len(paused) == 1:
                cmds.put({"watches": ["speed_out"]})      # no action: just a new watch
            else:
                cmds.put({"action": "continue"})

    def commands(timeout):
        try:
            return cmds.get(timeout=timeout) if timeout else cmds.get_nowait()
        except queue.Empty:
            return None

    results = tt.run_in_process({"kind": "simulator"}, [{"id": "d", "code": GOOD}], on_event,
                                commands=commands, debug={"breakpoints": [14]})
    assert results[0]["verdict"] == tt.VERDICT_PASSED
    assert paused[0]["snapshot"]["watches"] == {}
    assert paused[1]["line"] == 14 and "speed_out" in paused[1]["snapshot"]["watches"]


# ----------------------------------------------------------------------
# Test suites: one per architecture model
# ----------------------------------------------------------------------
def _port_row(name):
    return {"Port": {"text": name}}


@pytest.fixture()
def suite_db(tmp_path):
    d = make_project_db(
        str(tmp_path / "s.arch"),
        layout=[("TC. ID", "Static Text", True), ("Port", "Port Search", True)],
        models=[{"name": "SpeedProvider", "rows": [_port_row("Speed"), _port_row("Shared")]},
                {"name": "Brake", "rows": [_port_row("Pressure"), _port_row("Shared")]}],
        releases=[{"name": "R1"}])
    yield d
    d.close()


def _ids(db):
    return {m["name"]: m["id"] for m in db.get_all_models()}


def test_a_new_tests_suite_is_the_named_model_then_its_port_then_the_fallback(suite_db):
    ids = _ids(suite_db)
    assert tt.resolve_suite(suite_db, model="brake", port="SpeedProvider.Speed") == ids["Brake"]
    assert tt.resolve_suite(suite_db, port="SpeedProvider.Anything") == ids["SpeedProvider"]
    assert tt.resolve_suite(suite_db, port="Pressure", fallback=ids["SpeedProvider"]) == ids["Brake"]
    # A port two models have doesn't say which: the fallback decides.
    assert tt.resolve_suite(suite_db, port="Shared", fallback=ids["Brake"]) == ids["Brake"]
    assert tt.resolve_suite(suite_db, port="Unknown") is None
    assert tt.resolve_suite(suite_db, fallback=999) is None
    with pytest.raises(tt.ApprovalError, match="SpeedProvider, Brake"):
        tt.resolve_suite(suite_db, model="Nope")


def test_ai_tests_go_into_their_models_suite(suite_db):
    ids = _ids(suite_db)
    ex, _b, _ = _ai(db=suite_db, allow_writes=True)
    ex.model_id = ids["SpeedProvider"]           # the model the chat is about

    out = ex.execute("save_target_test", {"code": GOOD, "model": "Brake", "rationale": ""})
    assert "test suite Brake" in out
    brake_test = int(out.split("draft test ")[1].split(" ")[0])
    assert suite_db.get_target_test(brake_test)["model_id"] == ids["Brake"]

    # No model named: the test's port (SpeedProvider.Speed) names it.
    ex.model_id = ids["Brake"]
    out = ex.execute("save_target_test", {"code": GOOD, "rationale": ""})
    assert "test suite SpeedProvider" in out

    out = ex.execute("save_target_test", {"code": GOOD, "model": "Engine", "rationale": ""})
    assert out.startswith("ERROR") and "SpeedProvider, Brake" in out
    assert len(suite_db.list_target_tests()) == 2

    listing = ex.execute("list_target_tests", {}).splitlines()
    assert listing[0] == "Suite SpeedProvider: 1 test" and listing[2] == "Suite Brake: 1 test"
    assert '"suite": "Brake"' in ex.execute("get_target_test", {"id": brake_test})


def test_ai_must_name_a_suite_when_nothing_else_does(suite_db):
    ex, _b, _ = _ai(db=suite_db)
    code = GOOD.replace('port="SpeedProvider.Speed"', 'port="Shared"')
    out = ex.execute("save_target_test", {"code": code, "rationale": ""})
    assert out.startswith("ERROR") and "SpeedProvider, Brake" in out
    assert suite_db.list_target_tests() == []


def test_the_system_prompt_names_the_suites():
    from Application_Logic import Logic_AI_Target_Tools as at
    text = at.system_prompt(suites=["SpeedProvider", "Brake"], current="Brake")
    assert "SpeedProvider, Brake" in text and "working on: Brake" in text
    many = at.system_prompt(suites=[f"M{i}" for i in range(at.MAX_SUITES_IN_PROMPT + 5)])
    assert "and 5 more" in many


def test_model_is_not_a_script_keyword():
    code = GOOD.replace('requirement="REQ-123"', 'requirement="REQ-123", model="Brake"')
    assert any("@test takes" in i["message"] for i in tt.validate(code))


def test_suites_follow_the_models(suite_db):
    ids = _ids(suite_db)
    ap = tt.Approvals(suite_db, "alex")
    tid = ap.create(GOOD, tt.ORIGIN_USER, model_id=ids["Brake"])
    assert [s["name"] for s in tt.suites(suite_db)] == ["SpeedProvider", "Brake"]

    ap.move(tid, ids["SpeedProvider"])
    t = suite_db.get_target_test(tid)
    assert t["model_id"] == ids["SpeedProvider"]
    assert tt.status_of(t) == tt.STATUS_APPROVED          # moving isn't a change to review
    with pytest.raises(tt.ApprovalError):
        ap.move(tid, 999)

    # A renamed model keeps its tests; a deleted one keeps its suite while it has tests.
    suite_db.update_model(ids["SpeedProvider"], name="Speed", is_deleted=1)
    suite_db.update_model(ids["Brake"], is_deleted=1)
    assert tt.suites(suite_db) == [
        {"model_id": ids["SpeedProvider"], "name": "Speed", "status": "In Work", "deleted": True}]

    # The model is gone for good: the test is in the suite with no model.
    suite_db.delete_model(ids["SpeedProvider"])
    assert suite_db.get_target_test(tid) is not None
    assert tt.suites(suite_db)[-1]["model_id"] is None


def test_api_lists_suites_and_moves_tests(client):
    models = _ok(client.get("/api/models", headers=AUTH))
    a = next(m["id"] for m in models["models"] if m["name"] == "A")
    b = _ok(client.post("/api/models", json={"name": "B"}, headers=AUTH))["id"]

    t = _ok(client.post("/api/target-tests", json={"title": "x"}, headers=AUTH))
    assert t["model_id"] == a                          # the active model's suite
    t2 = _ok(client.post("/api/target-tests", json={"title": "y", "model_id": b}, headers=AUTH))
    assert t2["model_id"] == b

    listing = _ok(client.get("/api/target-tests", headers=AUTH))
    assert [s["name"] for s in listing["suites"]] == ["A", "B"]
    assert {x["id"]: x["model_id"] for x in listing["tests"]} == {t["id"]: a, t2["id"]: b}

    moved = _ok(client.put(f"/api/target-tests/{t['id']}/suite", json={"model_id": b},
                           headers=AUTH))
    assert moved["model_id"] == b and moved["status"] == "approved"
    assert client.put(f"/api/target-tests/{t['id']}/suite", json={"model_id": 999},
                      headers=AUTH).status_code == 409


# ----------------------------------------------------------------------
# More of the debugger: data breakpoints, frames, symbols, memory, calls, flash
# ----------------------------------------------------------------------
def test_a_data_breakpoint_stops_where_the_variable_is_written():
    b = sim()
    b.set_data_breakpoint("speed_out", "write")
    b.run()
    assert b.status()["function"] == "Speed_Main" and b.status()["line"] == 126
    assert b.call_stack()[-1] == "Speed_Main"
    assert b.read("speed_out") == "1"            # stopped after the write

    b.clear_breakpoints()
    b.reset()
    b.set_data_breakpoint("a", "read")           # read by `if (a == 5)`
    b.run()
    assert b.status()["line"] == 123
    with pytest.raises(br.TargetError):
        b.set_data_breakpoint("a", "sometimes")
    with pytest.raises(br.TargetError):
        b.set_data_breakpoint("nope")


def test_scripts_can_use_data_breakpoints_symbols_and_calls():
    code = '''from target_tests import test, target, expect


@test(id="TT-0100", title="speed_out is written by Speed_Main")
def tt_0100():
    target.break_on_write("speed_out")
    target.run_until_stop(timeout_s=2)
    expect.stopped_at("Speed_Main")
    expect.true(target.symbol("speed_out")["size"] == 4, "speed_out is 4 bytes")
    expect.value("speed_out", 1)
    target.clear_breakpoints()
    target.call("Speed_Main")
    expect.value("speed_out", 2)
    expect.true(target.read("a", frame=1) == "0", "the caller sees a too")
'''
    assert tt.validate(code) == []
    r = tt.execute(code, sim())
    assert r["verdict"] == "passed", r
    assert [s["call"] for s in r["steps"]][:3] == ["reset", "target.break_on_write",
                                                   "target.run_until_stop"]


def test_winidea_bridge_makes_the_sdk_calls_for_the_new_tools():
    calls = []

    class Value:
        def __init__(self, text): self.text = text
        def getResult(self): return self.text

    class Info:
        def getAddress(self): return 0x20001000
        def getSizeMAUs(self): return 4
        def toString(self): return "uint32_t"

    class FakeIC:
        ACCESS_OK = 1

        class IConnectDebug:
            fRealTime, fMonitor, gafExpression = 1, 2, 0
            bHW_accRD, bHW_accWR, bHW_accRW, bHW_SizeDefault = 10, 11, 12, 0

        class CBreakpointController:
            def __init__(self, _): pass
            def setHWBP(self, *args): calls.append(("setHWBP", args))
            def deleteAllHWBP(self): calls.append(("deleteAllHWBP",))

        class CDebugFacade:
            def __init__(self, _): pass
            def deleteAll(self): calls.append(("deleteAll",))
            def evaluate(self, flags, expr): return Value(f"7 ({expr})")

        class CExecutionController:
            def __init__(self, _): pass
            def call(self, *args):
                calls.append(("call", args))
                return "42"

        class CDataController:
            def __init__(self, _): pass
            def readMemory(self, flags, area, address, n, per):
                return [0xDE, 0xAD] + [FakeIC.ACCESS_OK] * 2 if address == 0x100 else [0, 0, 0, 0]
            def readRegister(self, flags, name): return Value("0x1000")

        class CDataController2:
            def __init__(self, _): pass
            def setStackFrameContext(self, flags, frame): calls.append(("frame", frame))

        class CAddressController:
            def __init__(self, _): pass
            def getSymbolInfo(self, flags, name): return Info()

        class CLoaderController:
            def __init__(self, _): pass
            def verifyDownload(self): calls.append(("verifyDownload",))

        class CSessionCtrl:
            def __init__(self, _): pass
            def begin_program(self): calls.append(("begin_program",))

        class StrVector(list):
            def push_back(self, v): self.append(v)

    b = br.WinIdeaBridge(sdk=FakeIC)
    b.cmgr = object()
    b.dbg, b.exe, b.bpc = (FakeIC.CDebugFacade(None), FakeIC.CExecutionController(None),
                           FakeIC.CBreakpointController(None))

    b.set_data_breakpoint("speed_out", "write")
    assert calls[-1] == ("setHWBP", (11, 0, 0, True, "speed_out", 0))
    b.clear_breakpoints()
    assert calls[-2:] == [("deleteAll",), ("deleteAllHWBP",)]

    assert b.read_frame("arg", 2) == "7 (arg)"
    assert [c for c in calls if c[0] == "frame"] == [("frame", 2), ("frame", 0)]
    assert b.symbol_info("speed_out") == {"name": "speed_out", "address": "0x20001000",
                                          "size": 4, "type": "uint32_t"}
    assert b.read_memory(0x100, 2) == b"\xde\xad"
    with pytest.raises(br.TargetError):
        b.read_memory(0x200, 2)                   # the access bytes say unreadable
    assert b.read_register("PC") == "0x1000"
    assert "matches" in b.verify_image() and calls[-1] == ("verifyDownload",)
    assert b.call_function("Speed_Get", [1, 2]) == "42"
    assert calls[-1] == ("call", ("Speed_Get", "1", "2"))
    b.call_function("f", [1, 2, 3, 4])
    assert calls[-1] == ("call", ("f", ["1", "2", "3", "4"]))
    b.flash()
    assert calls[-1] == ("begin_program",)


def test_ai_tools_for_data_breakpoints_symbols_calls_and_flash():
    ex, b, log = _ai()
    assert "stops on write" in ex.execute("target_set_data_breakpoint",
                                          {"variable": "speed_out"})
    assert "Speed_Main" in ex.execute("target_run_until_stop", {})
    assert ex.execute("target_read", {"expr": "a", "frame": 1}) == "a (frame 1) = 0"
    assert '"type": "int"' in ex.execute("target_symbol_info", {"name": "speed_out"})
    assert ex.execute("target_symbol_info", {"name": "nope"}).startswith("ERROR")
    assert ex.execute("target_read_register", {"name": "PC"}).startswith("PC = 0x")
    assert ex.execute("target_read_memory", {"address": "0x100", "size": 4}).startswith("ERROR")
    assert "no image" in ex.execute("target_verify_image", {})

    # Calling a function and flashing each need their own switch.
    assert ex.execute("target_call_function", {"function": "Speed_Main"}).startswith("ERROR")
    assert ex.execute("target_flash", {}).startswith("ERROR")
    assert "flash" not in b.log
    ex.allow_writes = ex.allow_flash = True
    assert "returned" in ex.execute("target_call_function", {"function": "Speed_Main"})
    assert "programmed" in ex.execute("target_flash", {}) and "flash" in b.log
    assert [e["tool"] for e in log][-2:] == ["target_call_function", "target_flash"]

    ex.allow_writes = False
    code = GOOD.replace('target.write("a", 5)', 'target.call("Speed_Main")')
    assert ex.execute("target_try_test", {"code": code}).startswith("ERROR")


def test_the_prompt_says_whether_flashing_is_allowed():
    from Application_Logic import Logic_AI_Target_Tools as at
    assert "Flashing is NOT allowed" in at.system_prompt()
    assert "Flashing is ALLOWED" in at.system_prompt(allow_flash=True)
